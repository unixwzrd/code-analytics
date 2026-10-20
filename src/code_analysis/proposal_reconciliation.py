"""Reconcile static tests and bound coverage with Phase 3 proposals."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from code_analysis.models import Completeness, Confidence
from code_analysis.report_models import (
    AnalysisReport,
    CoverageArtifactRecord,
    CoverageBindingState,
    ProposalDecisionFunnel,
    ProposalGate,
    ProposalGateOutcome,
    RefactorProposal,
    RefactorProposalState,
    SectionCoverage,
    SectionState,
    SignalQualitySummary,
    SymbolCoverageRecord,
    TestCaseRecord,
    TestRelationshipRecord,
)

MISSING_TEST_EVIDENCE = "Phase 4 test-impact evidence is unavailable."


def reconcile_proposal_evidence(report: AnalysisReport) -> AnalysisReport:
    """Attach test/coverage identities and conservatively promote eligible proposals."""
    if not report.refactor_proposals:
        return report
    tests = {item.test_id: item for item in report.test_cases}
    artifacts = {item.coverage_id: item for item in report.coverage_artifacts}
    coverage_by_subject = {
        subject_id: tuple(
            sorted(
                (item for item in report.coverage_observations if item.subject_id == subject_id),
                key=lambda item: item.coverage_observation_id,
            )
        )
        for subject_id in {item.subject_id for item in report.coverage_observations}
    }
    relationships_by_subject = {
        subject_id: tuple(
            sorted(
                (
                    item
                    for item in report.test_relationships
                    if item.target_subject_id == subject_id
                ),
                key=lambda item: item.relationship_id,
            )
        )
        for subject_id in {item.target_subject_id for item in report.test_relationships}
        if subject_id is not None
    }
    sections: dict[str, SectionCoverage] = {item.section: item for item in report.section_coverage}
    proposals = tuple(
        _reconcile_proposal(
            proposal,
            tests=tests,
            artifacts=artifacts,
            coverage_by_subject=coverage_by_subject,
            relationships_by_subject=relationships_by_subject,
            sections=sections,
        )
        for proposal in report.refactor_proposals
    )
    if proposals == report.refactor_proposals:
        return report
    decision_funnel = _decision_funnel(proposals)
    identity = hashlib.sha256(
        json.dumps(
            {
                "prior": report.run_identity_sha256,
                "proposals": [item.model_dump(mode="json") for item in proposals],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "run_identity_sha256": identity,
            "refactor_proposals": proposals,
            "signal_quality": SignalQualitySummary(
                commonality_admission=report.signal_quality.commonality_admission,
                proposal_admission=report.signal_quality.proposal_admission,
                proposal_decisions=decision_funnel,
            ),
        }
    )


def _decision_funnel(proposals: tuple[RefactorProposal, ...]) -> ProposalDecisionFunnel:
    return ProposalDecisionFunnel(
        total_proposals=len(proposals),
        blocked_proposals=sum(
            item.state is RefactorProposalState.BLOCKED_INSUFFICIENT_EVIDENCE for item in proposals
        ),
        reviewable_incomplete_proposals=sum(
            item.state is RefactorProposalState.REVIEWABLE_INCOMPLETE for item in proposals
        ),
        ready_for_maintainer_review_proposals=sum(
            item.state is RefactorProposalState.READY_FOR_MAINTAINER_REVIEW for item in proposals
        ),
        failed_gates=sum(
            decision.outcome is ProposalGateOutcome.FAILED
            for item in proposals
            for decision in item.gate_decisions
        ),
        unresolved_gates=sum(
            decision.outcome is ProposalGateOutcome.UNRESOLVED
            for item in proposals
            for decision in item.gate_decisions
        ),
    )


def _reconcile_proposal(
    proposal: RefactorProposal,
    *,
    tests: dict[str, TestCaseRecord],
    artifacts: dict[str, CoverageArtifactRecord],
    coverage_by_subject: dict[str, tuple[SymbolCoverageRecord, ...]],
    relationships_by_subject: dict[str, tuple[TestRelationshipRecord, ...]],
    sections: Mapping[str, SectionCoverage],
) -> RefactorProposal:
    affected_tests: set[str] = set()
    coverage_ids: set[str] = set()
    every_subject_qualified = True
    unresolved_relationship = False
    every_subject_tested = True
    for subject_id in proposal.affected_subject_ids:
        relationships = relationships_by_subject.get(subject_id, ())
        resolved_tests = {
            item.source_test_id
            for item in relationships
            if item.resolved and item.source_test_id in tests
        }
        if not resolved_tests:
            every_subject_tested = False
        unresolved_relationship |= any(not item.resolved for item in relationships)
        observations = coverage_by_subject.get(subject_id, ())
        qualified_observations = []
        for observation in observations:
            artifact = artifacts.get(observation.coverage_id)
            if artifact is None or artifact.binding is not CoverageBindingState.BOUND:
                continue
            matching_tests = {
                test_id
                for test_id in resolved_tests
                if _test_matches_context(tests[test_id].qualified_name, observation.contexts)
            }
            if matching_tests:
                affected_tests.update(matching_tests)
                coverage_ids.add(observation.coverage_observation_id)
                qualified_observations.append(observation)
        if not qualified_observations:
            every_subject_qualified = False
    test_section = sections.get("test-impact")
    coverage_section = sections.get("coverage")
    sections_eligible = bool(
        test_section
        and coverage_section
        and test_section.state in {SectionState.COMPLETE, SectionState.PARTIAL}
        and test_section.truncated == 0
        and coverage_section.state is SectionState.COMPLETE
    )
    coverage_passed = bool(
        sections_eligible
        and every_subject_qualified
        and not unresolved_relationship
        and affected_tests
        and coverage_ids
    )
    test_passed = bool(
        test_section
        and test_section.state in {SectionState.COMPLETE, SectionState.PARTIAL}
        and test_section.truncated == 0
        and every_subject_tested
        and not unresolved_relationship
    )
    decisions = tuple(
        decision.model_copy(
            update=(
                {
                    "outcome": (
                        ProposalGateOutcome.PASSED
                        if test_passed
                        else ProposalGateOutcome.UNRESOLVED
                    ),
                    "reason": (
                        "Every affected symbol has an exact resolved test relationship."
                        if test_passed
                        else "Affected-test evidence is missing, unresolved, or truncated."
                    ),
                }
                if decision.gate is ProposalGate.TEST_IMPACT
                else (
                    {
                        "outcome": (
                            ProposalGateOutcome.PASSED
                            if coverage_passed
                            else ProposalGateOutcome.UNRESOLVED
                        ),
                        "reason": (
                            "Every affected symbol has source-bound executed coverage context."
                            if coverage_passed
                            else "Source-bound executed coverage is missing or incomplete."
                        ),
                    }
                    if decision.gate is ProposalGate.COVERAGE_BINDING
                    else {}
                )
            )
        )
        for decision in proposal.gate_decisions
    )
    static_blocked = any(decision.blocking for decision in decisions)
    ready = coverage_passed and test_passed and not static_blocked
    state = (
        RefactorProposalState.BLOCKED_INSUFFICIENT_EVIDENCE
        if static_blocked
        else (
            RefactorProposalState.READY_FOR_MAINTAINER_REVIEW
            if ready
            else RefactorProposalState.REVIEWABLE_INCOMPLETE
        )
    )
    limitations = set(proposal.limitations)
    if ready:
        limitations.discard(MISSING_TEST_EVIDENCE)
    else:
        limitations.add(MISSING_TEST_EVIDENCE)
    return proposal.model_copy(
        update={
            "state": state,
            "primary_decision_reason": (
                proposal.primary_decision_reason
                if static_blocked
                else (
                    "All static, test-impact, and coverage gates passed."
                    if ready
                    else "Static gates passed; Phase 4 qualification evidence remains incomplete."
                )
            ),
            "gate_decisions": decisions,
            "affected_test_ids": tuple(sorted(affected_tests)),
            "coverage_observation_ids": tuple(sorted(coverage_ids)),
            "confidence": Confidence.RUNTIME_OBSERVED if ready else proposal.confidence,
            "completeness": Completeness.COMPLETE if ready else proposal.completeness,
            "limitations": tuple(sorted(limitations)),
        }
    )


def _test_matches_context(qualified_name: str, contexts: tuple[str, ...]) -> bool:
    dotted = qualified_name.replace(":", ".")
    return any(context in (qualified_name, dotted) for context in contexts)
