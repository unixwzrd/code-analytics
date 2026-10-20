"""Strict saved-report regression evidence contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from code_analysis.models import Completeness, StrictModel


class RegressionKind(StrEnum):
    DUPLICATION_ADDED = "duplication-added"
    DUPLICATION_REMOVED = "duplication-removed"
    OWNERSHIP_WORSENED = "ownership-worsened"
    INTERNAL_CONTRACT_ADDED = "internal-contract-added"
    MODULE_GROWTH = "module-growth"
    CANDIDATE_NEWLY_ACTIONABLE = "candidate-newly-actionable"
    REJECTED_CANDIDATE_CHANGED = "rejected-candidate-changed"
    IMPORT_EDGE_ADDED = "import-edge-added"
    IMPORT_EDGE_REMOVED = "import-edge-removed"
    SCC_ADDED = "scc-added"
    SCC_REMOVED = "scc-removed"
    CYCLE_ADDED = "cycle-added"
    CYCLE_REMOVED = "cycle-removed"


class RegressionObservation(StrictModel):
    observation_id: StrictStr = Field(pattern=r"^regression-observation-[0-9a-f]{20}$")
    kind: RegressionKind
    subject_id: StrictStr = Field(min_length=1)
    module: StrictStr | None = None
    baseline_value: StrictInt | None = None
    candidate_value: StrictInt | None = None
    absolute_change: StrictInt | None = None
    relative_change: StrictFloat | None = None
    explanation: StrictStr = Field(min_length=1)
    source_ids: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if list(self.source_ids) != sorted(self.source_ids) or len(self.source_ids) != len(
            set(self.source_ids)
        ):
            raise ValueError("regression source IDs must be unique and sorted")
        if self.kind is RegressionKind.MODULE_GROWTH:
            if None in {
                self.baseline_value,
                self.candidate_value,
                self.absolute_change,
                self.relative_change,
            }:
                raise ValueError("module growth requires complete numeric evidence")
            assert self.absolute_change is not None
            assert self.relative_change is not None
            if self.absolute_change < 25 or self.relative_change < 0.2:
                raise ValueError("module growth must cross both declared thresholds")
        elif any(
            value is not None
            for value in (
                self.baseline_value,
                self.candidate_value,
                self.absolute_change,
                self.relative_change,
            )
        ):
            raise ValueError("only module growth carries numeric LOC evidence")
        return self


class RegressionCounts(StrictModel):
    eligible: StrictInt = Field(ge=0)
    retained: StrictInt = Field(ge=0)
    omitted: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.retained + self.omitted != self.eligible:
            raise ValueError("regression counts do not reconcile")
        return self


class RegressionReport(StrictModel):
    schema_name: Literal["code-analysis-regression-report"] = "code-analysis-regression-report"
    schema_version: Literal["1.0.0"] = "1.0.0"
    tool_version: StrictStr = Field(min_length=1)
    regression_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    counts: RegressionCounts
    observations: tuple[RegressionObservation, ...] = ()
    truncated: StrictBool
    completeness: Completeness
    limitations: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        identities = [item.observation_id for item in self.observations]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("regression observation IDs must be unique and sorted")
        if self.counts.retained != len(self.observations):
            raise ValueError("regression retained count must match observations")
        if self.truncated != bool(self.counts.omitted):
            raise ValueError("regression truncation must match omitted count")
        if list(self.limitations) != sorted(self.limitations) or len(self.limitations) != len(
            set(self.limitations)
        ):
            raise ValueError("regression limitations must be unique and sorted")
        return self
