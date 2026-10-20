"""Strict paging and provenance for indexed saved-runtime evidence."""

from __future__ import annotations

from pydantic import Field, StrictInt, StrictStr, model_validator

from code_analysis.models import StrictModel
from code_analysis.profile_models import ProfileFrame, ProfileStatistic, RuntimeCallEdge
from code_analysis.runtime_comparison_browse import RuntimeComparisonSummary
from code_analysis.runtime_models import SourceBindingState
from code_analysis.runtime_queries import RuntimeQuerySection
from code_analysis.ui_runtime import RuntimeOverview, RuntimePanel


class RuntimeSectionCount(StrictModel):
    section: RuntimeQuerySection
    records: StrictInt = Field(ge=0, le=1_000_000)


class RuntimeIndexSummary(StrictModel):
    profile_report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    static_report_sha256: StrictStr | None = Field(pattern=r"^[0-9a-f]{64}$")
    overview: RuntimeOverview
    sections: tuple[RuntimeSectionCount, ...]
    limitations: tuple[StrictStr, ...]
    comparison: RuntimeComparisonSummary | None = None
    source_binding: SourceBindingState = SourceBindingState.UNBOUND
    source_hash_count: StrictInt = Field(default=0, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def reconcile(self) -> RuntimeIndexSummary:
        if self.source_binding is SourceBindingState.MATCHED and not self.source_hash_count:
            raise ValueError("matched runtime binding requires saved source hashes")
        if not self.overview.available:
            raise ValueError("runtime index requires an available profile")
        if self.overview.comparison_available != (self.comparison is not None):
            raise ValueError("runtime comparison availability must match saved evidence")
        if self.comparison is not None and (
            self.comparison.header.baseline_profile_identity_sha256
            != self.overview.profile_identity
        ):
            raise ValueError("runtime comparison baseline identity mismatch")
        if tuple(item.section for item in self.sections) != tuple(RuntimeQuerySection):
            raise ValueError("runtime index requires every section exactly once")
        if self.limitations != tuple(sorted(set(self.limitations))):
            raise ValueError("runtime limitations must be sorted and unique")
        return self


class RuntimeBrowseRequest(StrictModel):
    section: RuntimeQuerySection
    offset: StrictInt = Field(default=0, ge=0, le=1_000_000)
    limit: StrictInt = Field(default=25, ge=1, le=100)
    frame_id: StrictStr | None = Field(default=None, pattern=r"^profile-frame-[0-9a-f]{20}$")

    @model_validator(mode="after")
    def reconcile(self) -> RuntimeBrowseRequest:
        if self.frame_id is not None and self.section not in {
            RuntimeQuerySection.HOTSPOTS,
            RuntimeQuerySection.CALLS,
            RuntimeQuerySection.VARIANCE,
        }:
            raise ValueError("this runtime section does not support frame selection")
        return self


class RuntimeBrowsePage(StrictModel):
    offset: StrictInt = Field(ge=0, le=1_000_000)
    panel: RuntimePanel
    next_offset: StrictInt | None = Field(ge=1, le=1_000_000)

    @model_validator(mode="after")
    def reconcile(self) -> RuntimeBrowsePage:
        projection = self.panel.projection
        retained = min(projection.max_records, max(0, projection.eligible_count - self.offset))
        if projection.retained_count != retained:
            raise ValueError("runtime page records do not match the selected offset")
        next_offset = (
            self.offset + retained if self.offset + retained < projection.eligible_count else None
        )
        if self.next_offset != next_offset:
            raise ValueError("runtime page continuation does not reconcile")
        identities: set[str] = set()
        for record in projection.records:
            if isinstance(record, (ProfileFrame, ProfileStatistic)):
                identities.add(record.frame_id)
            elif isinstance(record, RuntimeCallEdge):
                identities.update((record.caller_frame_id, record.callee_frame_id))
        if tuple(label.frame_id for label in self.panel.labels) != tuple(sorted(identities)):
            raise ValueError("runtime page labels must match all retained frame identities")
        return self
