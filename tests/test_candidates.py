from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXTURES

from code_analysis.behavior_reporting import evaluate_candidate_behavior
from code_analysis.cli import ExitCode, main
from code_analysis.config import load_config_file
from code_analysis.cost_reporting import estimate_candidate_cost
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.report_models import (
    AbstractionWarningKind,
    CandidateQualificationKind,
    CandidateQualificationState,
    EstimateRange,
    ProposalGate,
    ProposalGateOutcome,
    RefactorCandidateKind,
)
from code_analysis.report_models import TestModuleRecord as _TestModuleRecord
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.service import scan_refactor


def _report():
    return scan_refactor(
        discover_context(
            FIXTURES / "proposal_project",
            load_config_file(FIXTURES / "proposal-config.toml"),
        )
    )


def test_candidates_have_stable_report_bound_observations() -> None:
    first = _report()
    second = _report()
    assert first.refactor_candidates == second.refactor_candidates
    assert first.candidate_observations == second.candidate_observations
    assert {item.kind for item in first.refactor_candidates} == {
        RefactorCandidateKind.RESPONSIBILITY_EXTRACTION,
        RefactorCandidateKind.MODULE_COMBINATION,
    }
    observations = {item.candidate_id: item for item in first.candidate_observations}
    assert all(observations[item.candidate_id].proposal_ids for item in first.refactor_candidates)
    changed_proposal = first.refactor_proposals[0].model_copy(
        update={"proposed_logical_ownership": "package.alternative"}
    )
    assert changed_proposal.candidate_id == first.refactor_proposals[0].candidate_id
    assert len(first.candidate_qualification_requirements) == len(first.refactor_candidates) * len(
        CandidateQualificationKind
    )
    assert {item.state for item in first.candidate_qualification_requirements} == {
        CandidateQualificationState.UNRESOLVED
    }
    assert all(item.qualification_requirement_ids for item in first.candidate_observations)
    assert build_projection(first, ProjectionName.CANDIDATE_QUALIFICATION).record_count


def test_candidate_worklist_filters_before_bounds() -> None:
    report = _report()
    selected = build_projection(
        report,
        ProjectionName.CANDIDATE_WORKLIST,
        candidate_module_patterns=("package.combine_*",),
        cross_module_only=True,
        min_shared_dimensions=2,
        max_records=1,
    )
    assert selected.record_count == 1
    assert selected.truncated
    assert any("1 omitted" in item for item in selected.limitations)
    excluded = build_projection(
        report,
        ProjectionName.CANDIDATE_WORKLIST,
        candidate_exclude_patterns=("package.combine_*",),
    )
    assert excluded.record_count == 0
    assert not excluded.truncated


def test_candidate_worklist_accepts_recorded_test_modules() -> None:
    report = _report()
    report = report.model_copy(
        update={
            "test_modules": (
                _TestModuleRecord(
                    test_module_id="test-module-" + "a" * 20,
                    module="tests.synthetic",
                    path="tests/test_synthetic.py",
                    source_sha256="b" * 64,
                    framework="pytest",
                ),
            )
        }
    )
    projection = build_projection(report, ProjectionName.CANDIDATE_WORKLIST)
    assert projection.record_count == len(report.refactor_candidates)


def test_candidates_cli_uses_saved_report_only(tmp_path: Path, capsys) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(_report()), encoding="utf-8")
    retained = load_json(report_path.read_text(encoding="utf-8"))
    assert retained.refactor_candidates
    result = main(
        [
            "candidates",
            str(report_path),
            "--format",
            "tsv",
            "--state",
            "reviewable",
            "--top",
            "25",
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.SUCCESS
    assert "candidate\tkind\tdecision\tdecision-rank\tstate" in captured.out
    assert "reviewable-incomplete" in captured.out
    assert captured.err == ""


def test_abstraction_costs_and_owner_alternatives_are_transparent() -> None:
    report = _report()
    assert len(report.abstraction_cost_estimates) == len(report.refactor_candidates)
    assert report.owner_alternatives
    assert any(
        AbstractionWarningKind.GENERIC_COMMON_MODULE in item.warnings
        for item in report.owner_alternatives
    )
    for estimate in report.abstraction_cost_estimates:
        assert estimate.net_production_loc.lower is not None
        assert estimate.net_production_loc.lower <= estimate.net_production_loc.likely
        assert estimate.net_production_loc.likely <= estimate.net_production_loc.upper
    for proposal in report.refactor_proposals:
        cost_gate = next(
            item for item in proposal.gate_decisions if item.gate is ProposalGate.ABSTRACTION_COST
        )
        assert cost_gate.outcome is ProposalGateOutcome.PASSED
        assert not cost_gate.blocking
    for name in (
        ProjectionName.ABSTRACTION_COST,
        ProjectionName.EXISTING_OWNER_ALTERNATIVES,
    ):
        projection = build_projection(report, name)
        assert projection.record_count


def test_cost_ranges_reject_partial_and_coercive_values() -> None:
    with pytest.raises(ValueError, match="all present"):
        EstimateRange(lower=1, likely=None, upper=2)
    with pytest.raises(ValueError):
        EstimateRange(lower=True, likely=1, upper=2)


def test_behavior_gate_uses_exact_differences_and_unresolved_calls() -> None:
    report = _report()
    candidate = report.refactor_candidates[0]
    profile_ids = set(candidate.participating_subject_ids)
    first_profile = next(
        item for item in report.function_profiles if item.subject_id in profile_ids
    )
    divergent_profiles = tuple(
        (
            item.model_copy(update={"exceptions": ("RuntimeError",)})
            if item.subject_id == first_profile.subject_id
            else item
        )
        for item in report.function_profiles
    )
    divergent = report.model_copy(update={"function_profiles": divergent_profiles})
    _events, _invariants, differences, outcome, reason = evaluate_candidate_behavior(
        divergent,
        candidate_id=candidate.candidate_id,
        subject_ids=candidate.participating_subject_ids,
    )
    assert outcome is ProposalGateOutcome.FAILED
    assert differences and all(item.blocking for item in differences)
    assert "incompatible" in reason

    unresolved_profiles = tuple(
        (
            item.model_copy(update={"unresolved_call_count": 1})
            if item.subject_id == first_profile.subject_id
            else item
        )
        for item in report.function_profiles
    )
    unresolved = report.model_copy(update={"function_profiles": unresolved_profiles})
    *_facts, outcome, reason = evaluate_candidate_behavior(
        unresolved,
        candidate_id=candidate.candidate_id,
        subject_ids=candidate.participating_subject_ids,
    )
    assert outcome is ProposalGateOutcome.UNRESOLVED
    assert "unresolved calls" in reason


def test_behavior_difference_preserves_pairwise_absence_evidence() -> None:
    report = _report()
    candidate = report.refactor_candidates[0]
    selected = set(candidate.participating_subject_ids)
    last_subject = candidate.participating_subject_ids[-1]
    profiles = tuple(
        (
            item.model_copy(
                update={"exceptions": () if item.subject_id == last_subject else ("RuntimeError",)}
            )
            if item.subject_id in selected
            else item
        )
        for item in report.function_profiles
    )
    changed = report.model_copy(update={"function_profiles": profiles})
    _events, _invariants, differences, outcome, _reason = evaluate_candidate_behavior(
        changed,
        candidate_id=candidate.candidate_id,
        subject_ids=candidate.participating_subject_ids,
    )
    assert outcome is ProposalGateOutcome.FAILED
    assert differences
    assert all(item.left_subject_id != item.right_subject_id for item in differences)
    assert all(item.left_identities != item.right_identities for item in differences)


def test_cost_ranges_remain_ordered_when_clone_and_span_loc_disagree() -> None:
    report = _report()
    candidate = report.refactor_candidates[0]
    selected = set(candidate.participating_subject_ids)
    fragments = tuple(
        (item.model_copy(update={"nonblank_lines": 100}) if item.subject_id in selected else item)
        for item in report.clone_fragments
    )
    changed = report.model_copy(update={"clone_fragments": fragments})
    proposal = next(
        item for item in report.refactor_proposals if item.candidate_id == candidate.candidate_id
    )
    estimate, _owners = estimate_candidate_cost(
        changed,
        candidate_id=candidate.candidate_id,
        subject_ids=candidate.participating_subject_ids,
        proposed_owner=proposal.proposed_logical_ownership,
        consumer_ids=proposal.consumer_identities,
        evidence=proposal.evidence,
        graph_edge_delta=0,
    )
    for value in (
        estimate.expected_deleted_loc,
        estimate.added_owner_loc,
        estimate.migration_adapter_loc,
    ):
        assert value.lower <= value.likely <= value.upper
