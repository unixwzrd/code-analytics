"""Named candidate eligibility/ranking facts shared by reports and saved indexes."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.decision_models import MaintainerDecision
from code_analysis.models import SourceLanguage, StrictModel
from code_analysis.projection_models import ProjectionSelection
from code_analysis.report_models import RefactorCandidateKind, RefactorCandidateState

STATE_RANK = {
    RefactorCandidateState.READY_FOR_MAINTAINER_REVIEW: 0,
    RefactorCandidateState.REVIEWABLE_INCOMPLETE: 1,
    RefactorCandidateState.OBSERVED: 2,
    RefactorCandidateState.BLOCKED: 3,
}
DECISION_RANK = {
    MaintainerDecision.ACCEPTED.value: 0,
    MaintainerDecision.IMPLEMENTED.value: 1,
    "changed-since-decision": 2,
    "undecided": 3,
    MaintainerDecision.DEFERRED_MISSING_COVERAGE.value: 4,
    MaintainerDecision.DEFERRED_PREREQUISITE.value: 5,
    MaintainerDecision.REJECTED_SEMANTIC_DIVERGENCE.value: 6,
    MaintainerDecision.REJECTED_ABSTRACTION_COST.value: 7,
    MaintainerDecision.REJECTED_OWNERSHIP_MISMATCH.value: 8,
    MaintainerDecision.REJECTED_BEHAVIOR_CHANGE.value: 9,
    MaintainerDecision.SUPERSEDED.value: 10,
}


def suppresses_unchanged_candidate(
    decision: MaintainerDecision | None,
    *,
    changed_since_decision: bool,
    requested_decisions: tuple[str, ...],
) -> bool:
    """Whether an unchanged maintainer rejection is hidden by default.

    Candidate evidence is never removed.  Explicit decision filtering always
    exposes it, and a changed reconsideration signature always reopens it.
    """

    return (
        decision is not None
        and not changed_since_decision
        and decision.suppresses_unchanged
        and not requested_decisions
    )


class CandidatePolicyFacts(StrictModel):
    candidate_id: StrictStr = Field(pattern=r"^refactor-candidate-[0-9a-f]{20}$")
    kind: RefactorCandidateKind
    state: RefactorCandidateState
    languages: tuple[SourceLanguage, ...] = Field(min_length=1)
    modules: tuple[StrictStr, ...] = Field(min_length=1)
    has_test_subjects: StrictBool
    shared_dimensions: StrictInt = Field(ge=0)
    failed_gates: StrictInt = Field(ge=0)
    unresolved_gates: StrictInt = Field(ge=0)
    owner_rank: StrictInt = Field(ge=0, le=4)
    net_rank: StrictInt = Field(ge=0, le=3)
    deleted_loc: StrictInt = Field(ge=-1)
    consumer_count: StrictInt = Field(ge=0)
    graph_change: StrictInt
    subject_count: StrictInt = Field(ge=1)
    decision_name: StrictStr = "undecided"

    @model_validator(mode="after")
    def validate_facts(self) -> Self:
        if self.decision_name not in DECISION_RANK:
            raise ValueError("unknown effective candidate decision")
        if self.modules != tuple(sorted(set(self.modules))) or any(not x for x in self.modules):
            raise ValueError("candidate modules must be nonempty, unique and sorted")
        if self.languages != tuple(sorted(set(self.languages))):
            raise ValueError("candidate languages must be unique and sorted")
        if len(self.modules) > self.subject_count:
            raise ValueError("candidate modules exceed participating subjects")
        return self

    def matches(self, selection: ProjectionSelection) -> bool:
        return (
            (not selection.languages or bool(set(self.languages) & set(selection.languages)))
            and (not selection.candidate_states or self.state.value in selection.candidate_states)
            and (not selection.candidate_kinds or self.kind in selection.candidate_kinds)
            and (
                not selection.candidate_decisions
                or self.decision_name in selection.candidate_decisions
            )
            and (not selection.cross_module_only or len(self.modules) >= 2)
            and (not selection.production_only or not self.has_test_subjects)
            and self.shared_dimensions >= selection.min_shared_dimensions
            and (
                not selection.candidate_module_patterns
                or any(
                    fnmatchcase(module, pattern)
                    for module in self.modules
                    for pattern in selection.candidate_module_patterns
                )
            )
            and not any(
                fnmatchcase(module, pattern)
                for module in self.modules
                for pattern in selection.candidate_exclude_patterns
            )
        )

    def ordering(self, prefer_existing_owner: bool) -> tuple[int | str, ...]:
        return (
            DECISION_RANK[self.decision_name],
            STATE_RANK[self.state],
            self.failed_gates,
            self.unresolved_gates,
            self.owner_rank if prefer_existing_owner else 0,
            self.net_rank,
            -self.deleted_loc,
            -self.consumer_count,
            self.graph_change,
            self.subject_count,
            len(self.modules),
            self.candidate_id,
        )
