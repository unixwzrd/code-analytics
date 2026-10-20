"""Read-only UI access to explicitly supplied portable runtime reports."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.models import Completeness, Diagnostic, ProviderResult, StrictModel
from code_analysis.profile_models import (
    FrameClassification,
    ProfileDiffRecord,
    ProfileDiffReport,
    ProfileFrame,
    ProfileReport,
    ProfileStatistic,
    RuntimeCallEdge,
)
from code_analysis.profiling import build_profile_diff, load_profile_json, render_profile_diff
from code_analysis.report_models import AnalysisReport, SourceFileRecord
from code_analysis.runtime_comparison import comparison_frame_bindings
from code_analysis.runtime_models import RuntimeSourceHash, SourceBindingState
from code_analysis.runtime_queries import (
    RuntimeQueryProjection,
    RuntimeQueryRequest,
    build_runtime_query,
)
from code_analysis.ui_source import SourcePreview, preview_source_record
from code_analysis.web_service import ReportViewService

RUNTIME_COMPARISON_SOURCE_ALLOWED_PARAMS: Final = frozenset(
    {
        "frame_identity",
        "side",
        "start",
        "end",
        "run",
        "compare_run",
        "profile_run",
        "compare_profile_run",
    }
)


class RuntimeComparisonSourceQuery(StrictModel):
    frame_identity: StrictStr = Field(min_length=1, max_length=4096)
    side: Literal["baseline", "candidate"]
    start: StrictInt | None = Field(default=None, ge=1)
    end: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> RuntimeComparisonSourceQuery:
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be supplied together")
        if self.start is not None:
            assert self.end is not None
            if self.end < self.start or self.end - self.start > 199:
                raise ValueError("source range requires end >= start and at most 200 lines")
        return self


class RuntimeComparisonSourcePreview(StrictModel):
    side: Literal["baseline", "candidate"]
    frame_identity: StrictStr
    state: Literal["bound", "disabled", "unavailable"]
    message: StrictStr
    frame_id: StrictStr | None = Field(default=None, pattern=r"^profile-frame-[0-9a-f]{20}$")
    preview: SourcePreview | None = None

    @model_validator(mode="after")
    def reconcile(self) -> RuntimeComparisonSourcePreview:
        if self.state == "bound":
            if self.frame_id is None or self.preview is None or self.preview.state != "bound":
                raise ValueError("bound comparison source requires a matched preview")
        elif self.frame_id is not None or self.preview is not None:
            raise ValueError("unavailable comparison source cannot disclose preview evidence")
        return self


def parse_runtime_comparison_source_query(
    params: dict[str, list[str]],
) -> RuntimeComparisonSourceQuery:
    if unknown := set(params) - RUNTIME_COMPARISON_SOURCE_ALLOWED_PARAMS:
        raise ValueError(
            "unsupported runtime comparison source parameters: " + ", ".join(sorted(unknown))
        )
    for name in RUNTIME_COMPARISON_SOURCE_ALLOWED_PARAMS:
        values = params.get(name)
        if values is not None and len(values) != 1:
            raise ValueError(f"{name} must be supplied once")

    def single(name: str, default: str = "") -> str:
        values = params.get(name)
        if not values:
            return default
        return values[0]

    def optional_int(name: str) -> int | None:
        if name not in params:
            return None
        try:
            return int(single(name, ""))
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc

    from code_analysis.runtime_source_browse import require_comparison_source_side

    side = require_comparison_source_side(single("side", ""))
    frame_identity = single("frame_identity", "")
    if not frame_identity:
        raise ValueError("frame_identity must be supplied once")
    return RuntimeComparisonSourceQuery(
        frame_identity=frame_identity,
        side=side,
        start=optional_int("start"),
        end=optional_int("end"),
    )


def selected_static_report_shas(
    service: ReportViewService,
    *,
    compare_run: str,
) -> tuple[str, ...]:
    reports = service.bootstrap().reports
    primary = reports[0].report_sha256
    if compare_run == "none":
        if service.comparison is not None and len(reports) > 1:
            return (primary, reports[1].report_sha256)
        return (primary,)
    if len(reports) < 2:
        raise ValueError("comparison static report selected without a paired report")
    return (primary, reports[1].report_sha256)


def loaded_static_reports(service: ReportViewService) -> dict[str, AnalysisReport]:
    reports = service.bootstrap().reports
    loaded = {reports[0].report_sha256: service.primary}
    if service.comparison is not None and len(reports) > 1:
        loaded[reports[1].report_sha256] = service.comparison
    return loaded


def _comparison_frame_binding_map(
    runtime: RuntimeViewService,
) -> dict[str, tuple[str | None, str | None]]:
    if runtime.comparison is None or runtime.delta is None:
        raise ValueError("no comparison profile was supplied")
    bindings = comparison_frame_bindings(runtime.profile, runtime.comparison)
    if len(bindings) != len(runtime.delta.records):
        raise ValueError("comparison frame bindings disagree with validated delta")
    mapping: dict[str, tuple[str | None, str | None]] = {}
    for index, (identity, baseline_id, candidate_id) in enumerate(bindings):
        if runtime.delta.records[index].frame_identity != identity:
            raise ValueError("comparison frame identity order disagrees with delta")
        if identity in mapping:
            raise ValueError("comparison frame identity is ambiguous")
        mapping[identity] = (baseline_id, candidate_id)
    return mapping


def _profile_frame(profile: ProfileReport, frame_id: str) -> ProfileFrame:
    matches = [frame for frame in profile.frames if frame.frame_id == frame_id]
    if not matches:
        raise ValueError("runtime comparison frame is absent from the selected profile")
    if len(matches) != 1:
        raise ValueError("runtime comparison frame identity is ambiguous")
    return matches[0]


def _captured_source_hash(profile: ProfileReport, path: str) -> RuntimeSourceHash:
    matches = [item for item in profile.runtime.source_hashes if item.path == path]
    if not matches:
        from code_analysis.runtime_source_browse import RuntimeSourceUnavailable

        raise RuntimeSourceUnavailable("runtime frame has no captured source hash")
    if len(matches) != 1:
        raise ValueError("runtime captured source hash is ambiguous")
    return matches[0]


def _static_source_record(
    service: ReportViewService, report_sha256: str, logical_path: str
) -> SourceFileRecord:
    from code_analysis.runtime_source_browse import RuntimeSourceUnavailable

    loaded = loaded_static_reports(service)
    if report_sha256 not in loaded:
        raise RuntimeSourceUnavailable(
            "runtime comparison source preview unavailable: profile static report "
            "is not among explicitly loaded reports"
        )
    report = loaded[report_sha256]
    matches = [item for item in report.source_files if item.logical_path == logical_path]
    if not matches:
        raise RuntimeSourceUnavailable(
            "runtime comparison source preview unavailable: static report has no "
            "matching source record"
        )
    if len(matches) != 1:
        raise ValueError("static source record identity is ambiguous")
    return matches[0]


def runtime_comparison_source_preview(
    service: ReportViewService,
    runtime: RuntimeViewService,
    *,
    source_root: Path | None,
    compare_run: str,
    query: RuntimeComparisonSourceQuery,
) -> RuntimeComparisonSourcePreview:
    from code_analysis.runtime_source_browse import RuntimeSourceUnavailable

    side = query.side
    if runtime.delta is None or runtime.comparison is None:
        raise RuntimeSourceUnavailable(
            "runtime comparison source preview unavailable: no comparison was indexed"
        )

    if source_root is None:
        return runtime_comparison_source_unavailable(
            query,
            "Source preview requires an explicit --source-root.",
            state="disabled",
        )

    bindings = _comparison_frame_binding_map(runtime)
    if query.frame_identity not in bindings:
        raise ValueError("frame identity is not in the selected runtime comparison")
    baseline_id, candidate_id = bindings[query.frame_identity]
    frame_id = baseline_id if side == "baseline" else candidate_id
    if frame_id is None:
        raise RuntimeSourceUnavailable(
            f"runtime comparison has no {side} frame at the selected identity"
        )

    profile = runtime.profile if side == "baseline" else runtime.comparison
    if profile is None:
        raise RuntimeSourceUnavailable(
            f"runtime comparison has no {side} profile at the selected identity"
        )

    static_shas = selected_static_report_shas(service, compare_run=compare_run)
    allowed_shas = (static_shas[0],) if side == "baseline" else static_shas
    if profile.source_report_sha256 not in allowed_shas:
        raise RuntimeSourceUnavailable(
            f"runtime comparison source preview unavailable: {side} profile does not match "
            "the explicitly selected static report"
        )
    if profile.runtime.source_binding is not SourceBindingState.MATCHED:
        raise RuntimeSourceUnavailable(
            f"runtime comparison source preview unavailable: {side} source binding "
            "is not matched"
        )

    frame = _profile_frame(profile, frame_id)
    if frame.classification is not FrameClassification.PROJECT or frame.line < 1:
        raise RuntimeSourceUnavailable("runtime frame has no project source location")

    captured = _captured_source_hash(profile, frame.path)
    static_source = _static_source_record(service, profile.source_report_sha256, frame.path)
    if captured.sha256 != static_source.source_sha256:
        raise RuntimeSourceUnavailable(
            "runtime comparison source preview unavailable: runtime captured hash "
            "does not match static source identity"
        )

    if query.start is None and query.end is None:
        start, end = frame.line, frame.line + 39
    else:
        assert query.start is not None and query.end is not None
        start, end = query.start, query.end
    preview = preview_source_record(static_source, source_root, start=start, end=end)
    if preview.state != "bound":
        return runtime_comparison_source_unavailable(query, preview.message)
    return RuntimeComparisonSourcePreview(
        side=side,
        frame_identity=query.frame_identity,
        state="bound",
        message=preview.message,
        frame_id=frame.frame_id,
        preview=preview,
    )


def runtime_comparison_source_unavailable(
    query: RuntimeComparisonSourceQuery,
    message: str,
    *,
    state: Literal["disabled", "unavailable"] = "unavailable",
) -> RuntimeComparisonSourcePreview:
    return RuntimeComparisonSourcePreview(
        side=query.side,
        frame_identity=query.frame_identity,
        state=state,
        message=message,
    )


class RuntimeFrameLabel(StrictModel):
    frame_id: StrictStr
    label: StrictStr


class RuntimePanel(StrictModel):
    projection: RuntimeQueryProjection
    labels: tuple[RuntimeFrameLabel, ...] = Field(max_length=200)


class RuntimeComparison(StrictModel):
    report: ProfileDiffReport
    omitted: StrictInt = Field(ge=0)
    eligible: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> RuntimeComparison:
        if (
            len(self.report.records) > 100
            or self.eligible != len(self.report.records) + self.omitted
        ):
            raise ValueError("runtime comparison counts do not reconcile")
        return self


class RuntimeOverview(StrictModel):
    available: StrictBool
    message: StrictStr
    profile_identity: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    profile_report_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_matches: StrictBool = False
    completeness: Completeness | None = None
    comparison_available: StrictBool = False
    providers: tuple[ProviderResult, ...] = Field(default=(), max_length=25)
    diagnostics: tuple[Diagnostic, ...] = Field(default=(), max_length=100)
    omitted_providers: StrictInt = Field(default=0, ge=0)
    omitted_diagnostics: StrictInt = Field(default=0, ge=0)
    limitations: tuple[StrictStr, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def validate_availability(self) -> RuntimeOverview:
        if self.available != (
            self.profile_identity is not None
            and self.profile_report_sha256 is not None
            and self.completeness is not None
        ):
            raise ValueError("runtime availability requires profile identities and completeness")
        if not self.available and (
            self.source_matches
            or self.comparison_available
            or self.providers
            or self.profile_identity is not None
            or self.profile_report_sha256 is not None
            or self.completeness is not None
        ):
            raise ValueError("absent runtime evidence cannot claim matching or available providers")
        return self


@dataclass(frozen=True, slots=True)
class RuntimeViewService:
    profile: ProfileReport
    comparison: ProfileReport | None = None
    delta: ProfileDiffReport | None = None

    def panel(self, request: RuntimeQueryRequest) -> RuntimePanel:
        if request.max_records > 100:
            raise ValueError("runtime UI record bound is 100")
        projection = build_runtime_query(self.profile, request)
        identities: set[str] = set()
        for record in projection.records:
            if isinstance(record, (ProfileFrame, ProfileStatistic)):
                identities.add(record.frame_id)
            elif isinstance(record, RuntimeCallEdge):
                identities.update((record.caller_frame_id, record.callee_frame_id))
        return RuntimePanel(
            projection=projection,
            labels=tuple(
                RuntimeFrameLabel(
                    frame_id=frame.frame_id, label=f"{frame.path}:{frame.line}:{frame.function}"
                )
                for frame in self.profile.frames
                if frame.frame_id in identities
            ),
        )

    def comparison_view(self) -> RuntimeComparison:
        if self.delta is None:
            raise ValueError("no comparison profile was supplied")
        return RuntimeComparison(
            report=self.delta.model_copy(update={"records": self.delta.records[:100]}),
            omitted=max(0, len(self.delta.records) - 100),
            eligible=len(self.delta.records),
        )

    @classmethod
    def load(
        cls,
        path: Path,
        comparison: Path | None = None,
        *,
        expected_sha256: str | None = None,
        expected_comparison_sha256: str | None = None,
    ) -> RuntimeViewService:
        if comparison is None and expected_comparison_sha256 is not None:
            raise ValueError("comparison artifact checksum requires a comparison profile")

        def read(selected: Path, expected: str | None) -> ProfileReport:
            selected = selected / "profile.json" if selected.is_dir() else selected
            with selected.open("rb") as stream:
                payload = stream.read(256 * 1024 * 1024 + 1)
            if len(payload) > 256 * 1024 * 1024:
                raise ValueError("profile exceeds UI input bound")
            if expected is not None and hashlib.sha256(payload).hexdigest() != expected:
                raise ValueError("profile bytes do not match the expected artifact checksum")
            return load_profile_json(payload.decode("utf-8"))

        primary = read(path, expected_sha256)
        candidate = read(comparison, expected_comparison_sha256) if comparison is not None else None
        # Diagnostic comparisons preserve compatibility decisions and never
        # turn mismatched workloads into an optimization claim.
        delta = build_profile_diff(primary, candidate, diagnostic_only=True) if candidate else None
        return cls(primary, candidate, delta)

    def overview(self, static_identity: str | None) -> RuntimeOverview:
        from code_analysis.ui_handoff import canonical_profile_sha256

        return RuntimeOverview(
            available=True,
            message="Saved runtime evidence only; execution is CLI-owned.",
            profile_identity=self.profile.profile_identity_sha256,
            profile_report_sha256=canonical_profile_sha256(self.profile),
            source_matches=static_identity is not None
            and self.profile.source_report_sha256 == static_identity,
            completeness=self.profile.completeness,
            comparison_available=self.delta is not None,
            providers=self.profile.providers[:25],
            diagnostics=self.profile.diagnostics[:100],
            omitted_providers=max(0, len(self.profile.providers) - 25),
            omitted_diagnostics=max(0, len(self.profile.diagnostics) - 100),
            limitations=self.profile.limitations[:99]
            + (
                ("Additional profile limitations omitted; consult the original JSON.",)
                if len(self.profile.limitations) > 99
                else ()
            ),
        )


def render_runtime_comparison_json(comparison: RuntimeComparison) -> str:
    return (
        json.dumps(
            comparison.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def _comparison_tsv_cell(value: object) -> str:
    return "unavailable" if value is None else str(value)


def render_runtime_comparison_tsv(comparison: RuntimeComparison) -> str:
    fields = tuple(ProfileDiffRecord.model_fields)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(fields)
    for record in comparison.report.records:
        payload = record.model_dump(mode="json")
        writer.writerow(_comparison_tsv_cell(payload[field]) for field in fields)
    return buffer.getvalue()


def render_runtime_comparison_markdown(
    comparison: RuntimeComparison,
    runtime: RuntimeViewService,
    *,
    static_identity: str | None,
) -> str:
    candidate = runtime.comparison
    if candidate is None:
        raise ValueError("no comparison profile was supplied")
    baseline = runtime.profile
    baseline_matches = (
        static_identity is not None and baseline.source_report_sha256 == static_identity
    )
    candidate_matches = (
        static_identity is not None and candidate.source_report_sha256 == static_identity
    )
    preamble = [
        "# Runtime comparison export",
        "",
        f"- Retained: {len(comparison.report.records)}",
        f"- Eligible: {comparison.eligible}",
        f"- Omitted by the 100-record view bound: {comparison.omitted}",
        "",
        "## Source partiality",
        "",
        f"- Baseline completeness: `{baseline.completeness.value}`",
        f"- Candidate completeness: `{candidate.completeness.value}`",
        f"- Baseline matches selected static report: {str(baseline_matches).lower()}",
        f"- Candidate matches selected static report: {str(candidate_matches).lower()}",
        "- Saved runtime evidence only; not a performance claim.",
        "",
    ]
    return "\n".join(preamble) + render_profile_diff(comparison.report, markdown=True)


def runtime_comparison_download_headers(
    comparison: RuntimeComparison,
    runtime: RuntimeViewService,
) -> dict[str, str]:
    candidate = runtime.comparison
    if candidate is None:
        raise ValueError("no comparison profile was supplied")
    source_states = (runtime.profile.completeness, candidate.completeness)
    incomplete = (
        comparison.omitted > 0
        or any(state is not Completeness.COMPLETE for state in source_states)
        or any(item.outcome != "matched" for item in comparison.report.decisions)
    )
    completeness = (
        Completeness.FAILED
        if Completeness.FAILED in source_states
        else Completeness.PARTIAL if incomplete else Completeness.COMPLETE
    )
    return {
        "X-Analysis-Completeness": completeness.value,
        "X-Projection-Truncated": str(comparison.omitted > 0).lower(),
        "X-Runtime-Comparison-Omitted": str(comparison.omitted),
        "X-Runtime-Comparison-Eligible": str(comparison.eligible),
    }
