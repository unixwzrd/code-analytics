"""Saved-runtime-report query projections with no source or provider access."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from enum import StrEnum

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.models import Completeness, StrictModel
from code_analysis.profile_models import (
    ProfileFrame,
    ProfileReport,
    ProfileStatistic,
    ResourceObservation,
    RuntimeCallEdge,
)
from code_analysis.profiling import render_profile_json
from code_analysis.runtime_models import (
    CategoryMatch,
    LifecycleOutcome,
    RuntimeCorrelation,
    RuntimeProcessProof,
    RuntimeStepObservation,
)


class RuntimeQuerySection(StrEnum):
    HOTSPOTS = "hotspots"
    CALLS = "calls"
    VARIANCE = "variance"
    RESOURCES = "resources"
    CATEGORIES = "categories"
    CORRELATIONS = "correlations"
    PROCESS = "process"
    LIFECYCLE = "lifecycle"
    STEPS = "steps"


type RuntimeQueryRecord = (
    ProfileFrame
    | ProfileStatistic
    | ResourceObservation
    | RuntimeCallEdge
    | CategoryMatch
    | RuntimeCorrelation
    | RuntimeProcessProof
    | LifecycleOutcome
    | RuntimeStepObservation
)
type SectionReader = Callable[[ProfileReport], tuple[RuntimeQueryRecord, ...]]


def _categories(report: ProfileReport) -> tuple[RuntimeQueryRecord, ...]:
    return report.runtime.categories


def _correlations(report: ProfileReport) -> tuple[RuntimeQueryRecord, ...]:
    return report.runtime.correlations


def _process(report: ProfileReport) -> tuple[RuntimeQueryRecord, ...]:
    return (report.runtime.process,)


def _lifecycle(report: ProfileReport) -> tuple[RuntimeQueryRecord, ...]:
    return report.runtime.lifecycle


def _steps(report: ProfileReport) -> tuple[RuntimeQueryRecord, ...]:
    return report.runtime.steps


SECTION_REGISTRY: dict[RuntimeQuerySection, SectionReader] = {
    RuntimeQuerySection.HOTSPOTS: lambda report: tuple(
        sorted(report.frames, key=lambda frame: (-(frame.total_seconds or 0.0), frame.frame_id))
    ),
    RuntimeQuerySection.CALLS: lambda report: report.runtime_edges,
    RuntimeQuerySection.VARIANCE: lambda report: report.statistics,
    RuntimeQuerySection.RESOURCES: lambda report: report.resources,
    RuntimeQuerySection.CATEGORIES: _categories,
    RuntimeQuerySection.CORRELATIONS: _correlations,
    RuntimeQuerySection.PROCESS: _process,
    RuntimeQuerySection.LIFECYCLE: _lifecycle,
    RuntimeQuerySection.STEPS: _steps,
}
if set(SECTION_REGISTRY) != set(RuntimeQuerySection):  # pragma: no cover - import-time guard
    raise RuntimeError("runtime query section registry is incomplete")

_SECTION_RECORD_TYPES: dict[RuntimeQuerySection, type[RuntimeQueryRecord]] = {
    RuntimeQuerySection.HOTSPOTS: ProfileFrame,
    RuntimeQuerySection.CALLS: RuntimeCallEdge,
    RuntimeQuerySection.VARIANCE: ProfileStatistic,
    RuntimeQuerySection.RESOURCES: ResourceObservation,
    RuntimeQuerySection.CATEGORIES: CategoryMatch,
    RuntimeQuerySection.CORRELATIONS: RuntimeCorrelation,
    RuntimeQuerySection.PROCESS: RuntimeProcessProof,
    RuntimeQuerySection.LIFECYCLE: LifecycleOutcome,
    RuntimeQuerySection.STEPS: RuntimeStepObservation,
}


def runtime_record_type(section: RuntimeQuerySection) -> type[RuntimeQueryRecord]:
    """Return the typed record contract, including for empty saved sections."""
    return _SECTION_RECORD_TYPES[section]


class RuntimeQueryRequest(StrictModel):
    section: RuntimeQuerySection
    max_records: StrictInt = Field(default=500, ge=1, le=10000)
    frame_id: StrictStr | None = Field(default=None, pattern=r"^profile-frame-[0-9a-f]{20}$")


class RuntimeQueryProjection(StrictModel):
    """A bounded view over retained portable runtime-report evidence."""

    schema_name: StrictStr = Field(
        default="code-analysis-runtime-query", pattern=r"^code-analysis-runtime-query$"
    )
    schema_version: StrictStr = Field(default="1.0.0", pattern=r"^1\.0\.0$")
    source_report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_profile_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    section: RuntimeQuerySection
    frame_id: StrictStr | None = Field(default=None, pattern=r"^profile-frame-[0-9a-f]{20}$")
    max_records: StrictInt = Field(ge=1, le=10000)
    eligible_count: StrictInt = Field(ge=0)
    retained_count: StrictInt = Field(ge=0)
    omitted_count: StrictInt = Field(ge=0)
    truncated: StrictBool
    source_completeness: Completeness
    records: tuple[RuntimeQueryRecord, ...]
    limitations: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def bounded_records(self) -> RuntimeQueryProjection:
        if self.retained_count != len(self.records):
            raise ValueError("retained count must match query records")
        if self.eligible_count != self.retained_count + self.omitted_count:
            raise ValueError("eligible query records must be retained or omitted")
        if self.truncated != (self.omitted_count > 0):
            raise ValueError("query truncation must match omitted records")
        if self.retained_count > self.max_records:
            raise ValueError("query records exceed configured bound")
        expected = _SECTION_RECORD_TYPES[self.section]
        if any(not isinstance(record, expected) for record in self.records):
            raise ValueError("query records do not match the selected section")
        if self.frame_id is not None:
            if self.section not in {
                RuntimeQuerySection.HOTSPOTS,
                RuntimeQuerySection.CALLS,
                RuntimeQuerySection.VARIANCE,
            }:
                raise ValueError("section does not support frame selection")
            for record in self.records:
                if isinstance(record, (ProfileFrame, ProfileStatistic)):
                    matches = record.frame_id == self.frame_id
                elif isinstance(record, RuntimeCallEdge):
                    matches = self.frame_id in (record.caller_frame_id, record.callee_frame_id)
                else:
                    matches = False
                if not matches:
                    raise ValueError("record does not match selected frame")
        return self


def build_runtime_query(
    report: ProfileReport, request: RuntimeQueryRequest
) -> RuntimeQueryProjection:
    """Select only evidence already retained in ``report``."""
    eligible = SECTION_REGISTRY[request.section](report)
    if request.frame_id is not None:
        if request.frame_id not in {frame.frame_id for frame in report.frames}:
            raise ValueError("selected frame is not in the saved profile")
        if request.section not in {
            RuntimeQuerySection.HOTSPOTS,
            RuntimeQuerySection.CALLS,
            RuntimeQuerySection.VARIANCE,
        }:
            raise ValueError("frame selection applies only to hotspots, calls, or variance")
        eligible = tuple(
            record
            for record in eligible
            if (
                isinstance(record, (ProfileFrame, ProfileStatistic))
                and record.frame_id == request.frame_id
            )
            or (
                isinstance(record, RuntimeCallEdge)
                and request.frame_id in (record.caller_frame_id, record.callee_frame_id)
            )
        )
    records = eligible[: request.max_records]
    omitted = len(eligible) - len(records)
    limitations = set(report.limitations)
    if report.completeness is not Completeness.COMPLETE:
        limitations.add("Source runtime report is not complete.")
    if omitted:
        limitations.add("Query records exceed the requested presentation bound.")
    source = render_profile_json(report, compact=True).encode()
    return RuntimeQueryProjection(
        source_report_sha256=hashlib.sha256(source).hexdigest(),
        source_profile_identity_sha256=report.profile_identity_sha256,
        section=request.section,
        frame_id=request.frame_id,
        max_records=request.max_records,
        eligible_count=len(eligible),
        retained_count=len(records),
        omitted_count=omitted,
        truncated=bool(omitted),
        source_completeness=report.completeness,
        records=records,
        limitations=tuple(sorted(limitations)),
    )


def render_runtime_query_json(projection: RuntimeQueryProjection) -> str:
    return (
        json.dumps(
            projection.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _cell(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_runtime_query_tsv(projection: RuntimeQueryProjection) -> str:
    lines = ["section\tindex\trecord_json"]
    lines.extend(
        f"{projection.section.value}\t{index}\t{_cell(record.model_dump(mode='json'))}"
        for index, record in enumerate(projection.records)
    )
    return "\n".join(lines) + "\n"


def render_runtime_query_markdown(projection: RuntimeQueryProjection) -> str:
    lines = [
        "# Runtime query",
        "",
        f"- Section: `{projection.section.value}`",
        f"- Source report: `{projection.source_report_sha256}`",
        f"- Completeness: `{projection.source_completeness.value}`",
        f"- Eligible: {projection.eligible_count}",
        f"- Retained: {projection.retained_count}",
        f"- Omitted: {projection.omitted_count}",
        "",
        "| Index | Record |",
        "| ---: | --- |",
    ]
    lines.extend(
        f"| {index} | `{_cell(record.model_dump(mode='json')).replace('|', '\\|')}` |"
        for index, record in enumerate(projection.records)
    )
    if projection.limitations:
        lines.extend(("", "## Limitations", ""))
        lines.extend(f"- {item}" for item in projection.limitations)
    return "\n".join(lines) + "\n"
