from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from http.client import HTTPConnection
from urllib.parse import urlencode

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import load_config_file
from code_analysis.decision_models import MaintainerDecision
from code_analysis.decisions import effective_decisions, set_decision
from code_analysis.project import discover_context
from code_analysis.projection_models import ProjectionSelection
from code_analysis.projections import ProjectionName
from code_analysis.projections.candidates import build_candidate_worklist_projection
from code_analysis.service import scan_refactor
from code_analysis.ui_decisions import (
    UIDecisionLedger,
    decision_ledger_status,
    index_decision_ledger_status,
    read_decision_snapshot,
    read_index_decision_snapshot,
)
from code_analysis.ui_projections import UIProjectionRequest, project_report
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import prepare_workspace


@pytest.fixture
def saved(tmp_path):
    context = discover_context(
        FIXTURES / "proposal_project", load_config_file(FIXTURES / "proposal-config.toml")
    )
    report = scan_refactor(context)
    workspace = prepare_workspace(context, explicit_path=tmp_path / "workspace")
    return report, workspace


def record(report, workspace, decision=MaintainerDecision.REJECTED_ABSTRACTION_COST):
    return set_decision(
        workspace,
        report,
        candidate_id=report.refactor_candidates[0].candidate_id,
        proposal_id=None,
        decision=decision,
        reason="Synthetic review evidence.",
        recorded_at=datetime(2026, 9, 19, tzinfo=UTC),
    )


def request(snapshot, **options):
    return UIProjectionRequest(
        name=ProjectionName.CANDIDATE_WORKLIST,
        decision_ledger_sha256=snapshot.ledger_sha256,
        decision_report_sha256=snapshot.report_sha256,
        **options,
    )


def test_missing_ledger_is_unavailable_and_no_file_is_created(saved):
    report, workspace = saved
    assert not decision_ledger_status(report, None).available
    assert read_decision_snapshot(report, workspace) is None
    status = decision_ledger_status(report, workspace)
    assert not status.available and status.unavailable_reason and status.record_count == 0
    assert not (workspace.decisions / "ledger.json").exists()


def test_index_snapshot_uses_retained_target_without_report_loading(saved, tmp_path, monkeypatch):
    report, workspace = saved
    manifest = build_browse_index(ReportViewService(report), tmp_path / "index")
    expected = decision_ledger_status(report, workspace)

    def forbidden(*args, **kwargs):
        raise AssertionError("index decision selection must not serialize a report")

    monkeypatch.setattr("code_analysis.ui_decisions.canonical_json", forbidden)
    assert index_decision_ledger_status(manifest, workspace) == expected
    assert not index_decision_ledger_status(manifest, None).available
    record(report, workspace)
    snapshot = read_index_decision_snapshot(manifest, workspace)
    assert snapshot is not None
    assert snapshot.report_sha256 == manifest.report_sha256
    status = index_decision_ledger_status(manifest, workspace)
    assert status.available and status.record_count == 1
    assert status.ledger_sha256 == snapshot.ledger_sha256
    mismatched = manifest.model_copy(
        update={"target": manifest.target.model_copy(update={"source_roots": ("other",)})}
    )
    with pytest.raises(OSError, match="logical target"):
        read_index_decision_snapshot(mismatched, workspace)


def test_explicit_snapshot_matches_authoritative_decision_projection(saved):
    report, workspace = saved
    item = record(report, workspace)
    path = workspace.decisions / "ledger.json"
    before = path.read_bytes()
    snapshot = read_decision_snapshot(report, workspace)
    assert snapshot is not None
    policy = request(snapshot)
    projected = project_report(report, policy, snapshot)
    expected = build_candidate_worklist_projection(
        report,
        ProjectionSelection(max_records=100, max_nodes=50, max_edges=100),
        effective_decisions(snapshot.ledger, report),
        snapshot.ledger_sha256,
    )
    assert projected == expected
    assert item.candidate_id not in {row.row_id for row in projected.rows}
    selected = project_report(
        report, request(snapshot, candidate_decisions=(item.decision.value,)), snapshot
    )
    assert [row.row_id for row in selected.rows] == [item.candidate_id]
    assert selected.inputs[-1].report_sha256 == snapshot.ledger_sha256
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="explicit selection"):
        project_report(
            report, UIProjectionRequest(name=ProjectionName.CANDIDATE_WORKLIST), snapshot
        )


def test_changed_ledger_or_report_and_forged_snapshot_are_rejected(saved):
    report, workspace = saved
    record(report, workspace)
    snapshot = read_decision_snapshot(report, workspace)
    assert snapshot is not None
    policy = request(snapshot)
    record(report, workspace, MaintainerDecision.ACCEPTED)
    new = read_decision_snapshot(report, workspace)
    assert new is not None
    for incorrect in (None, new, replace(snapshot, ledger=new.ledger)):
        with pytest.raises(ValueError, match="snapshot"):
            project_report(report, policy, incorrect)
    changed = report.model_copy(update={"tool_version": "0.1.1"})
    with pytest.raises(ValueError, match="snapshot"):
        project_report(changed, policy, snapshot)


@pytest.mark.parametrize("fault", ["symlink", "oversized", "malformed", "wrong-target"])
def test_bad_ledger_fails_closed(saved, tmp_path, fault):
    report, workspace = saved
    record(report, workspace)
    path = workspace.decisions / "ledger.json"
    if fault == "symlink":
        target = tmp_path / "outside.json"
        path.rename(target)
        path.symlink_to(target)
    elif fault == "oversized":
        path.write_bytes(b" " * (1024**2 + 1))
    elif fault == "malformed":
        path.write_text("{", encoding="utf-8")
    else:
        payload = json.loads(path.read_text())
        payload["target_identity_sha256"] = "0" * 64
        path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises((OSError, ValueError)):
        read_decision_snapshot(report, workspace)


def test_strict_binding_models():
    with pytest.raises(ValidationError):
        UIDecisionLedger(available=True, report_sha256="0" * 64)
    with pytest.raises(ValidationError):
        UIProjectionRequest(name=ProjectionName.CANDIDATE_WORKLIST, decision_ledger_sha256="0" * 64)
    with pytest.raises(ValidationError):
        UIProjectionRequest(
            name=ProjectionName.MODULES,
            decision_ledger_sha256="0" * 64,
            decision_report_sha256="1" * 64,
        )


def test_report_logical_target_must_match_workspace(saved):
    report, workspace = saved
    record(report, workspace)
    foreign = report.model_copy(
        update={"target": report.target.model_copy(update={"logical_root": "foreign"})}
    )
    with pytest.raises(OSError, match="logical target"):
        read_decision_snapshot(foreign, workspace)


def test_http_snapshot_requires_authority_and_download_rejects_changed_ledger(saved):
    report, workspace = saved
    record(report, workspace)
    server = create_server(ReportViewService(report), token="ledger-test", workspace=workspace.root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def get(path, authenticated=True):
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request(
            "GET", path, headers={"X-Code-Analysis-Token": "ledger-test"} if authenticated else {}
        )
        response = connection.getresponse()
        status, payload = response.status, response.read()
        connection.close()
        return status, json.loads(payload)

    try:
        assert get("/api/candidate-ledger", False)[0] == 403
        status, snapshot = get("/api/candidate-ledger")
        assert status == 200 and snapshot["available"]
        params = urlencode(
            {
                "name": "candidate-worklist",
                "decision_ledger_sha256": snapshot["ledger_sha256"],
                "decision_report_sha256": snapshot["report_sha256"],
            }
        )
        status, projected = get("/api/projection?" + params)
        assert (
            status == 200 and projected["inputs"][-1]["report_sha256"] == snapshot["ledger_sha256"]
        )
        record(report, workspace, MaintainerDecision.ACCEPTED)
        assert get("/api/download?" + params + "&format=json")[0] == 400
        assert get("/api/candidate-ledger?path=/tmp/foreign")[0] == 400
        ledger_path = workspace.decisions / "ledger.json"
        malformed = json.loads(ledger_path.read_text())
        malformed["records"][0]["unknown"] = "PRIVATE-MALFORMED-LEDGER-MARKER"
        ledger_path.write_text(json.dumps(malformed), encoding="utf-8")
        status, error = get("/api/candidate-ledger")
        assert status == 400
        assert "PRIVATE-MALFORMED-LEDGER-MARKER" not in json.dumps(error)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
