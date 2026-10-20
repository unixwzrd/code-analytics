from __future__ import annotations

import errno
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import FIXTURES

from code_analysis.cli import ExitCode, main
from code_analysis.config import load_config_file
from code_analysis.decision_models import DecisionLedger, MaintainerDecision
from code_analysis.decisions import (
    candidate_signature,
    effective_decisions,
    import_ledger,
    load_ledger,
    set_decision,
)
from code_analysis.project import discover_context
from code_analysis.reports.json_report import render_json
from code_analysis.service import scan_refactor
from code_analysis.workspace import prepare_workspace


def _context():
    return discover_context(
        FIXTURES / "proposal_project",
        load_config_file(FIXTURES / "proposal-config.toml"),
    )


def _report():
    return scan_refactor(_context())


def _workspace(tmp_path: Path, name: str):
    return prepare_workspace(_context(), explicit_path=tmp_path / name)


def _record(workspace, report, decision=MaintainerDecision.ACCEPTED):
    candidate = report.refactor_candidates[0]
    observation = next(
        item
        for item in report.candidate_observations
        if item.candidate_id == candidate.candidate_id
    )
    return set_decision(
        workspace,
        report,
        candidate_id=candidate.candidate_id,
        proposal_id=observation.proposal_ids[0],
        decision=decision,
        reason="Reviewed against the retained evidence.",
        recorded_at=datetime(2026, 8, 26, tzinfo=UTC),
    )


def test_ledger_is_atomic_append_only_and_supersedes_effective_decision(tmp_path: Path) -> None:
    report = _report()
    workspace = _workspace(tmp_path, "workspace")
    first = _record(workspace, report)
    second = set_decision(
        workspace,
        report,
        candidate_id=first.candidate_id,
        proposal_id=first.proposal_id,
        decision=MaintainerDecision.DEFERRED_MISSING_COVERAGE,
        reason="Coverage is not yet bound.",
        recorded_at=datetime(2026, 8, 27, tzinfo=UTC),
    )
    ledger = load_ledger(workspace)
    assert ledger.records == (first, second)
    assert second.superseded_decision_id == first.decision_id
    assert effective_decisions(ledger)[first.candidate_id].record == second
    assert not tuple(workspace.decisions.glob("*.tmp"))


def test_reconsideration_signature_detects_changed_strategy(tmp_path: Path) -> None:
    report = _report()
    workspace = _workspace(tmp_path, "workspace")
    record = _record(workspace, report, MaintainerDecision.REJECTED_ABSTRACTION_COST)
    proposal = report.refactor_proposals[0]
    changed = report.model_copy(
        update={
            "refactor_proposals": (
                proposal.model_copy(update={"proposed_logical_ownership": "package.changed"}),
                *report.refactor_proposals[1:],
            )
        }
    )
    assert (
        candidate_signature(report, record.candidate_id) == record.reconsideration_signature_sha256
    )
    assert effective_decisions(load_ledger(workspace), changed)[
        record.candidate_id
    ].changed_since_decision


def test_import_conflicts_fail_without_write_and_use_incoming_supersedes(
    tmp_path: Path,
) -> None:
    report = _report()
    local = _workspace(tmp_path, "local")
    incoming = _workspace(tmp_path, "incoming")
    local_record = _record(local, report, MaintainerDecision.ACCEPTED)
    incoming_record = _record(incoming, report, MaintainerDecision.REJECTED_SEMANTIC_DIVERGENCE)
    incoming_path = incoming.decisions / "ledger.json"
    before = (local.decisions / "ledger.json").read_bytes()
    with pytest.raises(OSError) as exc_info:
        import_ledger(local, incoming_path)
    assert exc_info.value.errno == errno.EEXIST
    assert (local.decisions / "ledger.json").read_bytes() == before
    merged = import_ledger(local, incoming_path, conflict="use-incoming")
    effective = effective_decisions(merged)[local_record.candidate_id].record
    assert effective.decision is incoming_record.decision
    assert effective.superseded_decision_id == local_record.decision_id


def test_malformed_ledger_and_target_mismatch_fail_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "workspace")
    (workspace.decisions / "ledger.json").write_text("{}", encoding="utf-8")
    with pytest.raises(OSError) as exc_info:
        load_ledger(workspace)
    assert exc_info.value.errno == errno.EBADMSG
    (workspace.decisions / "ledger.json").unlink()
    record = _record(workspace, _report())
    conflicting = record.model_copy(update={"decision_id": "maintainer-decision-" + "f" * 20})
    with pytest.raises(ValueError, match="multiple unresolved"):
        DecisionLedger(
            target_identity_sha256=load_ledger(workspace).target_identity_sha256,
            records=(record, conflicting),
        )


def test_decision_aware_worklist_suppresses_and_reconsiders_from_saved_json(
    tmp_path: Path, capsys
) -> None:
    report = _report()
    workspace = _workspace(tmp_path, "workspace")
    record = _record(workspace, report, MaintainerDecision.REJECTED_BEHAVIOR_CHANGE)
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(report), encoding="utf-8")
    result = main(
        [
            "candidates",
            str(report_path),
            "--workspace",
            str(workspace.root),
            "--format",
            "tsv",
        ]
    )
    output = capsys.readouterr().out
    assert result == ExitCode.SUCCESS
    assert record.candidate_id not in output

    proposal = report.refactor_proposals[0]
    changed = report.model_copy(
        update={
            "refactor_proposals": (
                proposal.model_copy(update={"proposed_logical_ownership": "package.changed"}),
                *report.refactor_proposals[1:],
            )
        }
    )
    changed_path = tmp_path / "changed.json"
    changed_path.write_text(render_json(changed), encoding="utf-8")
    result = main(
        [
            "candidates",
            str(changed_path),
            "--workspace",
            str(workspace.root),
            "--decision",
            "changed-since-decision",
            "--format",
            "tsv",
        ]
    )
    output = capsys.readouterr().out
    assert result == ExitCode.SUCCESS
    assert record.candidate_id in output
    assert "changed-since-decision" in output
