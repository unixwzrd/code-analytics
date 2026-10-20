from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.browse_export import export_browse_page
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.candidate_browse import validate_candidate_policies
from code_analysis.config import load_config_file
from code_analysis.decision_models import MaintainerDecision
from code_analysis.decisions import effective_decisions, set_decision
from code_analysis.project import discover_context
from code_analysis.projection_models import ProjectionSelection
from code_analysis.projections import ProjectionName
from code_analysis.projections.candidates import build_candidate_worklist_projection
from code_analysis.reports.projection_views import ProjectionFormat
from code_analysis.service import scan_refactor
from code_analysis.static_browse import StaticProjectionRequest, static_page_projection
from code_analysis.ui_decisions import read_index_decision_snapshot
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import prepare_workspace


@pytest.fixture
def saved(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(FIXTURES / "proposal_project", source)
    context = discover_context(source, load_config_file(FIXTURES / "proposal-config.toml"))
    report = scan_refactor(context)
    workspace = prepare_workspace(context, explicit_path=tmp_path / "workspace")
    manifest = build_browse_index(ReportViewService(report), tmp_path / "index")
    return (
        report,
        workspace,
        BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256),
        source,
    )


def _record(report, workspace, decision=MaintainerDecision.REJECTED_ABSTRACTION_COST):
    return set_decision(
        workspace,
        report,
        candidate_id=report.refactor_candidates[0].candidate_id,
        proposal_id=None,
        decision=decision,
        reason="Synthetic indexed decision evidence.",
        recorded_at=datetime(2026, 9, 19, tzinfo=UTC),
    )


def _request(snapshot, **kwargs):
    return StaticProjectionRequest(
        name=ProjectionName.CANDIDATE_WORKLIST,
        decision_ledger_sha256=snapshot.ledger_sha256,
        decision_report_sha256=snapshot.report_sha256,
        **kwargs,
    )


def test_saved_index_reconciles_explicit_ledger_without_source(saved):
    report, workspace, index, source = saved
    record = _record(report, workspace)
    snapshot = read_index_decision_snapshot(index.manifest, workspace)
    assert snapshot is not None
    expected = build_candidate_worklist_projection(
        report,
        ProjectionSelection(),
        effective_decisions(snapshot.ledger, report),
        snapshot.ledger_sha256,
    )
    shutil.rmtree(source)
    page = index.static_projection(_request(snapshot), decisions=snapshot)
    assert page.eligible == len(expected.rows)
    assert page.records == expected.rows
    assert record.candidate_id not in {item.row_id for item in page.records}
    revealed = index.static_projection(
        _request(
            snapshot,
            query=record.decision.value,
            candidate_selection=ProjectionSelection(candidate_decisions=(record.decision.value,)),
        ),
        decisions=snapshot,
    )
    assert [item.row_id for item in revealed.records] == [record.candidate_id]
    assert record.decision.value in static_page_projection(revealed).model_dump_json()


def test_indexed_decision_filters_and_changed_signatures_match_authoritative(saved):
    report, workspace, index, source = saved
    rejected = _record(report, workspace)
    accepted = set_decision(
        workspace,
        report,
        candidate_id=report.refactor_candidates[1].candidate_id,
        proposal_id=None,
        decision=MaintainerDecision.ACCEPTED,
        reason="Synthetic acceptance evidence.",
        recorded_at=datetime(2026, 9, 19, 0, 1, tzinfo=UTC),
    )
    from code_analysis.decisions import load_ledger

    ledger = load_ledger(workspace)
    changed_records = tuple(
        (
            item.model_copy(update={"reconsideration_signature_sha256": "0" * 64})
            if item.candidate_id == rejected.candidate_id
            else item
        )
        for item in ledger.records
    )
    (workspace.decisions / "ledger.json").write_text(
        json.dumps(
            ledger.model_copy(update={"records": changed_records}).model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    snapshot = read_index_decision_snapshot(index.manifest, workspace)
    assert snapshot is not None
    effective = effective_decisions(snapshot.ledger, report)
    assert effective[rejected.candidate_id].changed_since_decision
    expected_by_decision = {}
    for decision_name in ("changed-since-decision", accepted.decision.value):
        selection = ProjectionSelection(candidate_decisions=(decision_name,))
        expected_by_decision[decision_name] = build_candidate_worklist_projection(
            report, selection, effective, snapshot.ledger_sha256
        )
    shutil.rmtree(source)
    for decision_name, expected in expected_by_decision.items():
        page = index.static_projection(
            _request(
                snapshot,
                candidate_selection=ProjectionSelection(candidate_decisions=(decision_name,)),
            ),
            decisions=snapshot,
        )
        assert page.records == expected.rows
        assert page.summary.sort == expected.sort


def test_indexed_decision_snapshot_is_explicit_and_hash_bound(saved):
    report, workspace, index, _source = saved
    _record(report, workspace)
    snapshot = read_index_decision_snapshot(index.manifest, workspace)
    assert snapshot is not None
    request = _request(snapshot)
    with pytest.raises(ValueError, match="explicit decision ledger snapshot"):
        index.static_projection(request)
    with pytest.raises(ValueError, match="requires explicit request"):
        index.static_projection(
            StaticProjectionRequest(name=ProjectionName.CANDIDATE_WORKLIST), decisions=snapshot
        )
    with pytest.raises(ValueError, match="does not match"):
        index.static_projection(
            request.model_copy(update={"decision_report_sha256": "0" * 64}), decisions=snapshot
        )
    with pytest.raises(ValueError, match="snapshot identity"):
        index.static_projection(request, decisions=replace(snapshot, ledger_sha256="0" * 64))
    with pytest.raises(ValueError, match="snapshot identity"):
        index.static_projection(
            request,
            decisions=replace(snapshot, ledger=snapshot.ledger.model_copy(update={"records": ()})),
        )
    with pytest.raises(ValueError, match="decision ledger"):
        StaticProjectionRequest(
            name=ProjectionName.COMPLEXITY,
            decision_ledger_sha256=snapshot.ledger_sha256,
            decision_report_sha256=snapshot.report_sha256,
        )
    with pytest.raises(ValueError, match="requires an explicit"):
        StaticProjectionRequest(
            name=ProjectionName.CANDIDATE_WORKLIST,
            candidate_selection=ProjectionSelection(
                candidate_decisions=(MaintainerDecision.ACCEPTED.value,)
            ),
        )
    with pytest.raises(ValueError, match="supplied together"):
        StaticProjectionRequest(
            name=ProjectionName.CANDIDATE_WORKLIST,
            decision_ledger_sha256=snapshot.ledger_sha256,
        )
    with pytest.raises(ValueError, match="unknown candidate decision"):
        index.static_projection(
            _request(
                snapshot,
                candidate_selection=ProjectionSelection(candidate_decisions=("not-a-decision",)),
            ),
            decisions=snapshot,
        )


@pytest.mark.parametrize(
    "output_format",
    (ProjectionFormat.JSON_COMPACT, ProjectionFormat.TSV, ProjectionFormat.MARKDOWN),
)
def test_indexed_export_carries_explicit_ledger_input(saved, output_format):
    report, workspace, index, source = saved
    _record(report, workspace, MaintainerDecision.ACCEPTED)
    snapshot = read_index_decision_snapshot(index.manifest, workspace)
    assert snapshot is not None
    shutil.rmtree(source)
    _path, projection = export_browse_page(
        index,
        _request(snapshot),
        "decision-bound",
        output_format,
        decisions=snapshot,
    )
    assert projection.inputs[-1].report_sha256 == snapshot.ledger_sha256
    assert len(projection.inputs) == 2


def test_index_rejects_missing_or_malformed_signatures(saved):
    _report, _workspace, index, _source = saved
    with sqlite3.connect(index.database) as connection:
        count = connection.execute("SELECT count(*) FROM candidate_policy_rows").fetchone()[0]
        connection.execute(
            "UPDATE candidate_policy_rows SET reconsideration_signature_sha256 = ? "
            "WHERE position = 0",
            ("invalid",),
        )
        with pytest.raises(ValueError, match="signature"):
            validate_candidate_policies(connection, count)


def test_manifest_rejects_target_root_mismatch(saved):
    _report, _workspace, index, _source = saved
    with pytest.raises(ValidationError, match="target"):
        type(index.manifest).model_validate(
            {
                **index.manifest.model_dump(),
                "target": {**index.manifest.target.model_dump(), "logical_root": "other"},
            }
        )
