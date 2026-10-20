"""Candidate-specific static test and coverage qualification requirements."""

from __future__ import annotations

import hashlib

from code_analysis.report_models import (
    AnalysisReport,
    CandidateQualificationKind,
    CandidateQualificationRequirement,
    CandidateQualificationState,
    ProposalDecisionFunnel,
    RefactorCandidateState,
    RefactorProposalState,
    SectionState,
    SignalQualitySummary,
)


def _stable_id(candidate_id: str, kind: CandidateQualificationKind) -> str:
    digest = hashlib.sha256(f"{candidate_id}\0{kind.value}".encode()).hexdigest()[:20]
    return f"candidate-qualification-{digest}"


def integrate_candidate_qualification(report: AnalysisReport) -> AnalysisReport:
    if not report.refactor_candidates:
        return report
    sections = {item.section: item for item in report.section_coverage}
    test_section = sections["test-impact"]
    coverage_section = sections["coverage"]
    complete_discovery = (
        test_section.state is SectionState.COMPLETE
        and not test_section.truncated
        and coverage_section.state is SectionState.COMPLETE
        and not coverage_section.truncated
    )
    tests = {item.test_id: item for item in report.test_cases}
    relationships_by_subject: dict[str, set[str]] = {}
    for relationship in report.test_relationships:
        if relationship.target_subject_id and relationship.source_test_id:
            relationships_by_subject.setdefault(relationship.target_subject_id, set()).add(
                relationship.source_test_id
            )
    proposals_by_candidate = {
        candidate.candidate_id: tuple(
            item
            for item in report.refactor_proposals
            if item.candidate_id == candidate.candidate_id
        )
        for candidate in report.refactor_candidates
    }
    observations = {item.candidate_id: item for item in report.candidate_observations}
    requirements: list[CandidateQualificationRequirement] = []
    requirement_ids: dict[str, tuple[str, ...]] = {}
    qualification_complete: dict[str, bool] = {}
    for candidate in report.refactor_candidates:
        observation = observations[candidate.candidate_id]
        proposal_records = proposals_by_candidate[candidate.candidate_id]
        subject_test_ids = tuple(
            sorted(
                {
                    test_id
                    for subject_id in candidate.participating_subject_ids
                    for test_id in relationships_by_subject.get(subject_id, ())
                    if test_id in tests
                }
            )
        )
        coverage_ids = tuple(
            sorted(
                {
                    item
                    for proposal in proposal_records
                    for item in proposal.coverage_observation_ids
                }
            )
        )
        names = {test_id: tests[test_id].qualified_name.lower() for test_id in subject_test_ids}
        kind_evidence = {
            CandidateQualificationKind.DIRECT_TESTS: subject_test_ids,
            CandidateQualificationKind.LIKELY_CALL_PATH_TESTS: subject_test_ids,
            CandidateQualificationKind.BOUND_COVERAGE: coverage_ids,
            CandidateQualificationKind.POSITIVE_CASES: tuple(
                item
                for item, name in names.items()
                if not any(token in name for token in ("fail", "error", "invalid", "rollback"))
            ),
            CandidateQualificationKind.NEGATIVE_ADVERSARIAL_CASES: tuple(
                item
                for item, name in names.items()
                if any(token in name for token in ("fail", "error", "invalid", "adversarial"))
            ),
            CandidateQualificationKind.PARITY_FIXTURES: tuple(
                item for item, name in names.items() if "parity" in name or "fixture" in name
            ),
            CandidateQualificationKind.ROLLBACK_TESTS: tuple(
                item for item, name in names.items() if "rollback" in name
            ),
            CandidateQualificationKind.FAILURE_PATH_TESTS: tuple(
                item
                for item, name in names.items()
                if any(token in name for token in ("fail", "error", "exception"))
            ),
            CandidateQualificationKind.CANONICAL_TEST_DISCOVERY: subject_test_ids,
        }
        candidate_requirements: list[CandidateQualificationRequirement] = []
        for kind in CandidateQualificationKind:
            identities = kind_evidence[kind]
            evidenced = bool(identities)
            state = (
                CandidateQualificationState.EVIDENCED
                if evidenced
                else (
                    CandidateQualificationState.MISSING
                    if complete_discovery
                    else CandidateQualificationState.UNRESOLVED
                )
            )
            is_coverage = kind is CandidateQualificationKind.BOUND_COVERAGE
            candidate_requirements.append(
                CandidateQualificationRequirement(
                    requirement_id=_stable_id(candidate.candidate_id, kind),
                    candidate_id=candidate.candidate_id,
                    kind=kind,
                    state=state,
                    test_ids=() if is_coverage else tuple(identities),
                    coverage_observation_ids=tuple(identities) if is_coverage else (),
                    evidence=observation.evidence if evidenced else (),
                    reason=(
                        f"{kind.value} is evidenced for this candidate."
                        if evidenced
                        else (
                            f"{kind.value} is absent from complete bound qualification evidence."
                            if complete_discovery
                            else (
                                f"{kind.value} remains unresolved because qualification is "
                                "incomplete."
                            )
                        )
                    ),
                    limitations=(
                        "Static test names and relationships do not prove behavioral coverage.",
                    ),
                )
            )
        requirements.extend(candidate_requirements)
        requirement_ids[candidate.candidate_id] = tuple(
            sorted(item.requirement_id for item in candidate_requirements)
        )
        qualification_complete[candidate.candidate_id] = all(
            item.state is CandidateQualificationState.EVIDENCED for item in candidate_requirements
        )
    updated_proposals = tuple(
        (
            proposal.model_copy(
                update={
                    "state": RefactorProposalState.REVIEWABLE_INCOMPLETE,
                    "primary_decision_reason": (
                        "Static gates passed, but candidate-specific qualification requirements "
                        "remain missing or unresolved."
                    ),
                }
            )
            if proposal.state is RefactorProposalState.READY_FOR_MAINTAINER_REVIEW
            and not qualification_complete[proposal.candidate_id]
            else proposal
        )
        for proposal in report.refactor_proposals
    )
    updated_observations = tuple(
        item.model_copy(
            update={
                "qualification_requirement_ids": requirement_ids[item.candidate_id],
                "state": (
                    RefactorCandidateState.READY_FOR_MAINTAINER_REVIEW
                    if qualification_complete[item.candidate_id]
                    and any(
                        proposal.state is RefactorProposalState.READY_FOR_MAINTAINER_REVIEW
                        for proposal in proposals_by_candidate[item.candidate_id]
                    )
                    else item.state
                ),
            }
        )
        for item in report.candidate_observations
    )
    decision_funnel = ProposalDecisionFunnel(
        total_proposals=len(updated_proposals),
        blocked_proposals=sum(
            item.state is RefactorProposalState.BLOCKED_INSUFFICIENT_EVIDENCE
            for item in updated_proposals
        ),
        reviewable_incomplete_proposals=sum(
            item.state is RefactorProposalState.REVIEWABLE_INCOMPLETE for item in updated_proposals
        ),
        ready_for_maintainer_review_proposals=sum(
            item.state is RefactorProposalState.READY_FOR_MAINTAINER_REVIEW
            for item in updated_proposals
        ),
        failed_gates=sum(
            decision.outcome.value == "failed"
            for item in updated_proposals
            for decision in item.gate_decisions
        ),
        unresolved_gates=sum(
            decision.outcome.value == "unresolved"
            for item in updated_proposals
            for decision in item.gate_decisions
        ),
    )
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "refactor_proposals": updated_proposals,
            "candidate_observations": updated_observations,
            "candidate_qualification_requirements": tuple(
                sorted(requirements, key=lambda item: item.requirement_id)
            ),
            "signal_quality": SignalQualitySummary(
                commonality_admission=report.signal_quality.commonality_admission,
                proposal_admission=report.signal_quality.proposal_admission,
                proposal_decisions=decision_funnel,
            ),
            "summary": {
                **report.summary.model_dump(),
                "candidate_qualification_requirements": len(requirements),
            },
        }
    )
