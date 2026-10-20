"""Strict, bounded saved-profile comparison pages for the disposable index."""

from __future__ import annotations

import sqlite3

from pydantic import Field, StrictInt, StrictStr, model_validator

from code_analysis.models import StrictModel
from code_analysis.profile_models import ProfileDiffRecord, ProfileDiffReport
from code_analysis.runtime_models import SourceBindingState
from code_analysis.ui_runtime import RuntimeOverview


class RuntimeComparisonFrameBinding(StrictModel):
    frame_identity: StrictStr = Field(min_length=1)
    baseline_frame_id: StrictStr | None = Field(
        default=None, pattern=r"^profile-frame-[0-9a-f]{20}$"
    )
    candidate_frame_id: StrictStr | None = Field(
        default=None, pattern=r"^profile-frame-[0-9a-f]{20}$"
    )

    @model_validator(mode="after")
    def reconcile(self) -> RuntimeComparisonFrameBinding:
        if self.baseline_frame_id is None and self.candidate_frame_id is None:
            raise ValueError("comparison frame binding requires at least one side")
        return self


class RuntimeComparisonSummary(StrictModel):
    candidate_report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_static_sha256: StrictStr | None = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_overview: RuntimeOverview
    candidate_source_binding: SourceBindingState = SourceBindingState.UNBOUND
    candidate_source_hash_count: StrictInt = Field(default=0, ge=0, le=1_000_000)
    header: ProfileDiffReport
    records: StrictInt = Field(ge=0, le=1_000_000)
    comparison_frame_count: StrictInt = Field(ge=0, le=1_000_000)
    candidate_frame_count: StrictInt = Field(ge=0, le=1_000_000)
    baseline_artifact_count: StrictInt = Field(ge=0, le=1_000_000)
    candidate_artifact_count: StrictInt = Field(ge=0, le=1_000_000)

    @model_validator(mode="after")
    def reconcile(self) -> RuntimeComparisonSummary:
        if self.header.records or not self.header.diagnostic_only or self.header.claim_eligible:
            raise ValueError("indexed comparison requires an empty diagnostic-only header")
        if (
            not self.candidate_overview.available
            or self.candidate_overview.comparison_available
            or self.header.candidate_profile_identity_sha256
            != self.candidate_overview.profile_identity
        ):
            raise ValueError("comparison candidate provenance does not reconcile")
        if (
            self.candidate_source_binding is SourceBindingState.MATCHED
            and not self.candidate_source_hash_count
        ):
            raise ValueError("matched candidate runtime binding requires saved source hashes")
        if self.comparison_frame_count != self.records:
            raise ValueError("comparison frame map count must match delta records")
        return self


class RuntimeComparisonRequest(StrictModel):
    offset: StrictInt = Field(default=0, ge=0, le=1_000_000)
    limit: StrictInt = Field(default=25, ge=1, le=100)


class RuntimeComparisonPage(StrictModel):
    request: RuntimeComparisonRequest
    eligible: StrictInt = Field(ge=0, le=1_000_000)
    report: ProfileDiffReport
    next_offset: StrictInt | None = Field(ge=1, le=1_000_000)

    @model_validator(mode="after")
    def reconcile(self) -> RuntimeComparisonPage:
        retained = min(self.request.limit, max(0, self.eligible - self.request.offset))
        end = self.request.offset + retained
        if len(self.report.records) != retained or self.next_offset != (
            end if end < self.eligible else None
        ):
            raise ValueError("runtime comparison continuation does not reconcile")
        if not self.report.diagnostic_only or self.report.claim_eligible:
            raise ValueError("indexed comparison cannot authorize performance claims")
        return self


def read_comparison_page(
    connection: sqlite3.Connection,
    summary: RuntimeComparisonSummary,
    request: RuntimeComparisonRequest,
) -> RuntimeComparisonPage:
    rows = connection.execute(
        "SELECT payload FROM runtime_records WHERE section = 'comparison' "
        "ORDER BY position LIMIT ? OFFSET ?",
        (request.limit, request.offset),
    ).fetchall()
    records = []
    size = 0
    for row in rows:
        if not isinstance(row[0], str) or len(row[0].encode()) > 1024**2:
            raise ValueError("runtime comparison record exceeds byte bound")
        size += len(row[0].encode())
        if size > 8 * 1024**2:
            raise ValueError("runtime comparison page exceeds byte bound")
        records.append(ProfileDiffRecord.model_validate_json(row[0]))
    report = ProfileDiffReport.model_validate(
        {**summary.header.model_dump(), "records": tuple(records)}
    )
    end = request.offset + len(records)
    return RuntimeComparisonPage(
        request=request,
        eligible=summary.records,
        report=report,
        next_offset=end if end < summary.records else None,
    )
