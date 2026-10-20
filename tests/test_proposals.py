from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.cohesion_reporting import _estimate_combination, _ownership_counter_reason
from code_analysis.config import (
    AnalysisConfig,
    ArchitectureLayer,
    CloneThresholds,
    PhaseThreeLimits,
)
from code_analysis.models import Completeness
from code_analysis.project import discover_context
from code_analysis.proposal_reconciliation import reconcile_proposal_evidence
from code_analysis.report_models import (
    AnalysisReport,
    CoverageArtifactRecord,
    CoverageBindingState,
    ProposalGate,
    ProposalGateOutcome,
    RefactorProposalKind,
    RefactorProposalState,
    SectionCoverage,
    SectionState,
    SummaryCounts,
    SymbolCoverageRecord,
)
from code_analysis.report_models import (
    TestCaseRecord as CaseRecord,
)
from code_analysis.report_models import (
    TestModuleRecord as ModuleRecord,
)
from code_analysis.report_models import (
    TestRelationshipKind as RelationshipKind,
)
from code_analysis.report_models import (
    TestRelationshipRecord as RelationshipRecord,
)
from code_analysis.service import scan


def _config(*, maximum_proposals: int = 100) -> AnalysisConfig:
    return AnalysisConfig(
        clone_thresholds=CloneThresholds(commonality_max_feature_subject_fraction=1.0),
        architecture_layers=(
            ArchitectureLayer(
                name="application",
                module_patterns=(
                    "package.combine_a",
                    "package.combine_b",
                    "package.work",
                ),
                may_import_layers=("domain",),
            ),
            ArchitectureLayer(
                name="domain",
                module_patterns=("package.dependencies", "package.domain_bad"),
            ),
        ),
        phase_three_limits=PhaseThreeLimits(max_proposals=maximum_proposals),
    )


def _write_signal_fixture(root: Path) -> None:
    package = root / "package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    short_helper = """def _span(item):
    return (
        item.path,
        item.start_line,
        item.start_column,
        item.end_line,
        item.end_column,
    )
"""
    pure_helper = """def _shape(value: int) -> int:
    if value > 0:
        return value + 1
    return 0
"""
    (package / "left.py").write_text(
        "from . import right\n\n" + short_helper + "\n" + pure_helper,
        encoding="utf-8",
    )
    (package / "right.py").write_text(short_helper + "\n" + pure_helper, encoding="utf-8")


def _write_call_only_fixture(root: Path) -> None:
    package = root / "package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "work.py").write_text(
        """def _normalize(value: int) -> int:
    return value + 1


def first(callback, value: int) -> int:
    result = {"value": _normalize(value)}["value"]
    callback(result)
    return result


def second(callback, value: int) -> int:
    result = {"value": _normalize(value)}["value"]
    callback(result)
    return result
""",
        encoding="utf-8",
    )


def test_short_helpers_remain_clone_evidence_without_responsibility(tmp_path: Path) -> None:
    _write_signal_fixture(tmp_path)
    report = scan(discover_context(tmp_path, AnalysisConfig()))
    subject_ids = {
        subject.subject_id
        for subject in report.subjects
        if subject.logical_name.endswith("._span") or subject.logical_name.endswith("._shape")
    }
    assert any(subject_ids <= set(cluster.subject_ids) for cluster in report.clone_clusters)
    assert report.function_commonalities
    assert not any(
        subject_ids & set(cluster.member_subject_ids) for cluster in report.responsibility_clusters
    )
    assert not any(
        subject_ids & set(proposal.affected_subject_ids) for proposal in report.refactor_proposals
    )


def test_call_resolution_only_candidates_are_rejected_before_simulation(
    tmp_path: Path,
) -> None:
    _write_call_only_fixture(tmp_path)
    report = scan(discover_context(tmp_path, AnalysisConfig()))
    funnel = report.signal_quality.proposal_admission
    assert funnel is not None
    assert funnel.responsibility_clusters_considered == 1
    assert funnel.proposal_candidates_considered == 1
    assert funnel.admitted_candidates == 0
    assert funnel.call_only_rejections == 1
    assert funnel.proposal_bound_omissions == 0
    assert funnel.retained_proposals == 0
    assert len(funnel.rejection_examples) == 1
    assert report.responsibility_clusters
    assert report.function_commonalities
    assert not report.architecture_estimates
    assert not report.refactor_proposals
    assert not any(
        finding.rule_id in {"extraction-candidate", "module-combination-candidate"}
        for finding in report.findings
    )
    assert report.completeness is Completeness.COMPLETE
    payload = report.model_dump()
    rejection = payload["signal_quality"]["proposal_admission"]["rejection_examples"][0]
    payload["signal_quality"]["proposal_admission"]["rejection_examples"] = (
        {**rejection, "responsibility_id": "responsibility-" + "f" * 20},
    )
    with pytest.raises(ValidationError, match=r"proposal rejection.*responsibility"):
        AnalysisReport.model_validate(payload)


def test_retained_proposals_never_have_call_resolution_as_the_only_counter_evidence() -> None:
    report = scan(discover_context(FIXTURES / "proposal_project", _config()))
    for proposal in report.refactor_proposals:
        observations = {item.observation for item in proposal.counter_evidence}
        assert observations != {"one or both call sets are incomplete"}


def test_proposals_retain_graph_deltas_and_cannot_be_ready_or_authorized() -> None:
    report = scan(discover_context(FIXTURES / "proposal_project", _config()))
    assert report.responsibility_clusters
    assert report.refactor_proposals
    assert any(
        item.kind is RefactorProposalKind.COMBINE_MODULES for item in report.refactor_proposals
    )
    allowed_states = {
        RefactorProposalState.REVIEWABLE_INCOMPLETE,
        RefactorProposalState.BLOCKED_INSUFFICIENT_EVIDENCE,
    }
    assert {item.state for item in report.refactor_proposals} <= allowed_states
    assert any(
        item.state is RefactorProposalState.REVIEWABLE_INCOMPLETE
        for item in report.refactor_proposals
    )
    estimates = {item.estimate_id: item for item in report.architecture_estimates}
    for proposal in report.refactor_proposals:
        assert tuple(item.gate for item in proposal.gate_decisions) == tuple(ProposalGate)
        assert proposal.primary_decision_reason
        if proposal.state is RefactorProposalState.BLOCKED_INSUFFICIENT_EVIDENCE:
            first_blocking = next(item for item in proposal.gate_decisions if item.blocking)
            assert proposal.primary_decision_reason == first_blocking.reason
            assert any(
                item.blocking
                and item.outcome in {ProposalGateOutcome.FAILED, ProposalGateOutcome.UNRESOLVED}
                for item in proposal.gate_decisions
            )
        assert "Phase 4 test-impact evidence is unavailable." in proposal.limitations
        assert proposal.rollback_criteria
        assert proposal.validation_needs
        estimate = estimates[proposal.architecture_estimate_id]
        if proposal.state is RefactorProposalState.REVIEWABLE_INCOMPLETE:
            assert (
                estimate.expected_after.cross_boundary_edge_count
                < estimate.before.cross_boundary_edge_count
            )
            assert estimate.expected_after.scc_count <= estimate.before.scc_count
            assert estimate.expected_after.cycle_count <= estimate.before.cycle_count
            assert (
                estimate.expected_after.layer_violation_count
                <= estimate.before.layer_violation_count
            )
    funnel = report.signal_quality.proposal_decisions
    assert funnel is not None
    assert funnel.total_proposals == len(report.refactor_proposals)
    assert all(
        "Unresolved calls or divergent exception behavior" not in item.primary_decision_reason
        for item in report.refactor_proposals
    )


def test_ownership_counter_reasons_name_exact_evidence(ordinary_report) -> None:
    evidence = ordinary_report.subjects[0].evidence[0]

    def observed(*values: str):
        return tuple(evidence.model_copy(update={"observation": value}) for value in values)

    assert _ownership_counter_reason(observed("one or both call sets are incomplete")) == (
        "Incomplete resolved-call evidence prevents complete ownership evaluation."
    )
    assert (
        _ownership_counter_reason(observed("different exception behavior is syntax-evidenced"))
        == "Divergent exception behavior conflicts with common ownership."
    )
    assert _ownership_counter_reason(
        observed(
            "one or both call sets are incomplete",
            "different exception behavior is syntax-evidenced",
        )
    ) == (
        "Ownership remains unresolved: resolved-call evidence is incomplete; "
        "exception behavior diverges."
    )
    assert _ownership_counter_reason(observed("new unresolved ownership condition")) == (
        "Other unresolved ownership evidence prevents complete ownership evaluation."
    )


def test_only_configured_layers_create_violations_and_suggestions_are_report_only() -> None:
    report = scan(discover_context(FIXTURES / "proposal_project", _config()))
    configured = [item for item in report.architecture_contract_suggestions if item.configured_rule]
    assert configured
    assert all("[importlinter:contract:" in item.import_linter_text for item in configured)
    assert any(item.rule_id == "configured-layer-direction" for item in report.findings)
    unconfigured = scan(discover_context(FIXTURES / "proposal_project", AnalysisConfig()))
    assert not any(item.rule_id == "configured-layer-direction" for item in unconfigured.findings)


def test_proposal_bound_is_partial_and_cannot_be_successful_truncation() -> None:
    report = scan(
        discover_context(
            FIXTURES / "proposal_project",
            _config(maximum_proposals=1),
        )
    )
    assert report.completeness is Completeness.PARTIAL
    coverage = next(
        item for item in report.section_coverage if item.section == "cohesion-refactoring"
    )
    assert coverage.state is SectionState.PARTIAL
    assert coverage.truncated > 0
    assert any(item.code == "refactor-proposal-bound-truncated" for item in report.diagnostics)


def test_architecture_estimate_identity_includes_its_responsibility() -> None:
    config = _config()
    report = scan(discover_context(FIXTURES / "proposal_project", config))
    responsibility = report.responsibility_clusters[0]
    modules = tuple(
        sorted(
            {
                subject.module
                for subject in report.subjects
                if subject.subject_id in responsibility.member_subject_ids
            }
        )
    )
    assert len(modules) >= 2
    first = _estimate_combination(
        report,
        modules,
        modules[0],
        responsibility.evidence,
        config.architecture_layers,
        "responsibility-a",
    )[0]
    second = _estimate_combination(
        report,
        modules,
        modules[0],
        responsibility.evidence,
        config.architecture_layers,
        "responsibility-b",
    )[0]
    assert first.estimate_id != second.estimate_id


def test_bound_test_contexts_promote_only_to_maintainer_review() -> None:
    report = scan(discover_context(FIXTURES / "proposal_project", _config()))
    subject_ids = tuple(
        sorted({value for item in report.refactor_proposals for value in item.affected_subject_ids})
    )
    module = ModuleRecord(
        test_module_id="test-module-11111111111111111111",
        module="tests.test_refactor",
        path="tests/test_refactor.py",
        source_sha256="1" * 64,
        framework="pytest",
    )
    tests = tuple(
        CaseRecord(
            test_id=f"test-case-{index:020x}",
            test_module_id=module.test_module_id,
            qualified_name=f"tests.test_refactor:test_subject_{index}",
            evidence=(report.subjects[index - 1].evidence[0],),
        )
        for index, _subject_id in enumerate(subject_ids, start=1)
    )
    relationships = tuple(
        RelationshipRecord(
            relationship_id=f"test-relationship-{index:020x}",
            source_test_id=test.test_id,
            kind=RelationshipKind.DIRECT_SYMBOL,
            target_subject_id=subject_id,
            resolved=True,
            evidence=test.evidence,
        )
        for index, (test, subject_id) in enumerate(zip(tests, subject_ids, strict=True), start=1)
    )
    artifact = CoverageArtifactRecord(
        coverage_id="coverage-11111111111111111111",
        artifact_sha256="2" * 64,
        workload_identity_sha256="3" * 64,
        source_report_sha256="4" * 64,
        binding=CoverageBindingState.BOUND,
        contexts=tuple(test.qualified_name.replace(":", ".") for test in tests),
        completeness=Completeness.COMPLETE,
    )
    observations = tuple(
        SymbolCoverageRecord(
            coverage_observation_id=f"coverage-observation-{index:020x}",
            coverage_id=artifact.coverage_id,
            subject_id=subject_id,
            covered_lines=(1,),
            contexts=(test.qualified_name.replace(":", "."),),
        )
        for index, (test, subject_id) in enumerate(zip(tests, subject_ids, strict=True), start=1)
    )
    sections = tuple(
        (
            SectionCoverage(
                section=item.section,
                state=(
                    SectionState.PARTIAL if item.section == "test-impact" else SectionState.COMPLETE
                ),
                unresolved=1 if item.section == "test-impact" else 0,
            )
            if item.section in {"test-impact", "coverage"}
            else item
        )
        for item in report.section_coverage
    )
    prepared = report.model_copy(
        update={
            "test_modules": (module,),
            "test_cases": tests,
            "test_relationships": relationships,
            "coverage_artifacts": (artifact,),
            "coverage_observations": observations,
            "section_coverage": sections,
            "completeness": Completeness.PARTIAL,
            "summary": SummaryCounts(
                **{
                    **report.summary.model_dump(),
                    "test_modules": 1,
                    "test_cases": len(tests),
                    "test_relationships": len(relationships),
                    "coverage_artifacts": 1,
                    "coverage_observations": len(observations),
                }
            ),
        }
    )
    reconciled = reconcile_proposal_evidence(prepared)
    assert reconciled.refactor_proposals
    promoted = [
        item
        for item in reconciled.refactor_proposals
        if item.state is RefactorProposalState.READY_FOR_MAINTAINER_REVIEW
    ]
    assert promoted
    assert all(
        item.state is RefactorProposalState.BLOCKED_INSUFFICIENT_EVIDENCE
        for item in reconciled.refactor_proposals
        if item not in promoted
    )
    for proposal in promoted:
        assert proposal.affected_test_ids
        assert proposal.coverage_observation_ids
        assert "Phase 4 test-impact evidence is unavailable." not in proposal.limitations
        assert proposal.unresolved_maintainer_decisions

    incomplete = prepared.model_copy(
        update={
            "coverage_observations": observations[:-1],
            "summary": prepared.summary.model_copy(
                update={"coverage_observations": len(observations) - 1}
            ),
        }
    )
    blocked = reconcile_proposal_evidence(incomplete)
    assert all(
        item.state is not RefactorProposalState.READY_FOR_MAINTAINER_REVIEW
        for item in blocked.refactor_proposals
    )

    unresolved = prepared.model_copy(
        update={
            "test_relationships": (
                relationships[0].model_copy(update={"resolved": False}),
                *relationships[1:],
            )
        }
    )
    blocked = reconcile_proposal_evidence(unresolved)
    assert all(
        item.state is not RefactorProposalState.READY_FOR_MAINTAINER_REVIEW
        for item in blocked.refactor_proposals
    )
