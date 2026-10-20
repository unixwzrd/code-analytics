"""Strict local maintainer-decision ledger contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictStr, field_validator, model_validator

from code_analysis.models import StrictModel


class MaintainerDecision(StrEnum):
    ACCEPTED = "accepted"
    IMPLEMENTED = "implemented"
    REJECTED_SEMANTIC_DIVERGENCE = "rejected-semantic-divergence"
    REJECTED_ABSTRACTION_COST = "rejected-abstraction-cost"
    REJECTED_OWNERSHIP_MISMATCH = "rejected-ownership-mismatch"
    REJECTED_BEHAVIOR_CHANGE = "rejected-behavior-change"
    DEFERRED_MISSING_COVERAGE = "deferred-missing-coverage"
    DEFERRED_PREREQUISITE = "deferred-prerequisite"
    SUPERSEDED = "superseded"

    @property
    def suppresses_unchanged(self) -> bool:
        return self in {
            MaintainerDecision.REJECTED_SEMANTIC_DIVERGENCE,
            MaintainerDecision.REJECTED_ABSTRACTION_COST,
            MaintainerDecision.REJECTED_OWNERSHIP_MISMATCH,
            MaintainerDecision.REJECTED_BEHAVIOR_CHANGE,
        }


class CandidateDecisionRecord(StrictModel):
    decision_id: StrictStr = Field(pattern=r"^maintainer-decision-[0-9a-f]{20}$")
    candidate_id: StrictStr = Field(pattern=r"^refactor-candidate-[0-9a-f]{20}$")
    proposal_id: StrictStr | None = Field(default=None, pattern=r"^refactor-proposal-[0-9a-f]{20}$")
    decision: MaintainerDecision
    reason: StrictStr = Field(min_length=1)
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    observation_id: StrictStr = Field(pattern=r"^candidate-observation-[0-9a-f]{20}$")
    reconsideration_signature_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_at: StrictStr
    superseded_decision_id: StrictStr | None = Field(
        default=None, pattern=r"^maintainer-decision-[0-9a-f]{20}$"
    )

    @field_validator("recorded_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("decision timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if self.reason != self.reason.strip():
            raise ValueError("decision reason must be trimmed")
        if self.superseded_decision_id == self.decision_id:
            raise ValueError("decision cannot supersede itself")
        return self


class DecisionLedger(StrictModel):
    schema_name: Literal["code-analysis-decision-ledger"] = "code-analysis-decision-ledger"
    schema_version: Literal["1.0.0"] = "1.0.0"
    target_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    records: tuple[CandidateDecisionRecord, ...] = ()

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        identities = [item.decision_id for item in self.records]
        if len(identities) != len(set(identities)):
            raise ValueError("decision IDs must be unique")
        positions = {identity: position for position, identity in enumerate(identities)}
        for position, record in enumerate(self.records):
            superseded = record.superseded_decision_id
            if superseded is not None and (
                superseded not in positions or positions[superseded] >= position
            ):
                raise ValueError("decision supersession must reference an earlier record")
        superseded_ids = {
            item.superseded_decision_id
            for item in self.records
            if item.superseded_decision_id is not None
        }
        effective_by_candidate: dict[str, list[str]] = {}
        for record in self.records:
            if record.decision_id in superseded_ids:
                continue
            effective_by_candidate.setdefault(record.candidate_id, []).append(record.decision_id)
        if any(len(values) > 1 for values in effective_by_candidate.values()):
            raise ValueError("candidate has multiple unresolved effective decisions")
        return self
