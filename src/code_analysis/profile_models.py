"""Persisted runtime-profile evidence contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from code_analysis.models import Completeness, Diagnostic, ProviderResult, StrictModel
from code_analysis.runtime_models import (
    CompatibilityDecision,
    LifecycleState,
    ProcessProofState,
    RuntimeEvidence,
    SourceBindingState,
)


class RuntimeModel(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class FrameClassification(StrEnum):
    NATIVE = "native"
    PROJECT = "project"
    STANDARD_LIBRARY = "standard-library"
    ENVIRONMENT = "environment"
    GENERATED = "generated"
    THIRD_PARTY = "third-party"
    UNRESOLVED = "unresolved"


class ProfileFrame(RuntimeModel):
    frame_id: StrictStr = Field(pattern=r"^profile-frame-[0-9a-f]{20}$")
    path: StrictStr = Field(min_length=1)
    line: StrictInt = Field(ge=0)
    function: StrictStr = Field(min_length=1)
    classification: FrameClassification
    subject_id: StrictStr | None = None
    primitive_calls: StrictInt | None = Field(ge=0)
    total_calls: StrictInt | None = Field(ge=0)
    total_seconds: StrictFloat | None = Field(ge=0.0)
    cumulative_seconds: StrictFloat | None = Field(ge=0.0)
    per_call_seconds: StrictFloat | None = Field(ge=0.0)
    cumulative_per_primitive_call_seconds: StrictFloat | None = Field(default=None, ge=0.0)
    metrics_observed: StrictBool = True
    artifact_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_metrics(self) -> Self:
        metrics = (
            self.primitive_calls,
            self.total_calls,
            self.total_seconds,
            self.cumulative_seconds,
        )
        if self.metrics_observed != all(value is not None for value in metrics):
            raise ValueError("observed frame metrics must all be present")
        if not self.metrics_observed and any(value is not None for value in metrics):
            raise ValueError("unobserved frame metrics must all be absent")
        if (
            self.primitive_calls is not None
            and self.total_calls is not None
            and self.primitive_calls > self.total_calls
        ):
            raise ValueError("primitive calls cannot exceed total calls")
        if self.path.startswith("/") or "\\" in self.path or ".." in self.path.split("/"):
            raise ValueError("profile path must be portable")
        if "/Users/" in self.function or "/Volumes/" in self.function:
            raise ValueError("profile function contains physical path")
        return self


class CallerRepresentation(StrEnum):
    COUNT = "integer-count"
    TIMED = "cprofile-tuple"


class ProfileProducer(StrEnum):
    UNKNOWN = "unknown"
    CPROFILE = "cprofile"
    PROFILE = "profile"


class ProfileProducerBinding(RuntimeModel):
    """Supplied declaration bound to captured bytes, not authenticated attestation."""

    profile_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    producer: ProfileProducer
    evidence: Literal["declared"] = "declared"

    @model_validator(mode="after")
    def known_producer(self) -> Self:
        if self.producer is ProfileProducer.UNKNOWN:
            raise ValueError("producer declarations require a known producer")
        return self


class RuntimeCallEdge(RuntimeModel):
    runtime_edge_id: StrictStr = Field(pattern=r"^runtime-edge-[0-9a-f]{20}$")
    caller_frame_id: StrictStr = Field(pattern=r"^profile-frame-[0-9a-f]{20}$")
    callee_frame_id: StrictStr = Field(pattern=r"^profile-frame-[0-9a-f]{20}$")
    caller_representation: CallerRepresentation = CallerRepresentation.TIMED
    primitive_calls: StrictInt | None = Field(ge=0)
    total_calls: StrictInt = Field(ge=0)
    total_seconds: StrictFloat | None = Field(ge=0.0)
    cumulative_seconds: StrictFloat | None = Field(ge=0.0)

    @model_validator(mode="after")
    def available_metrics(self) -> Self:
        values = (self.primitive_calls, self.total_seconds, self.cumulative_seconds)
        if self.caller_representation == CallerRepresentation.COUNT:
            if any(value is not None for value in values):
                raise ValueError("integer callers provide only total call counts")
        elif any(value is None for value in values):
            raise ValueError("timed callers require all tuple metrics")
        if self.primitive_calls is not None and self.primitive_calls > self.total_calls:
            raise ValueError("primitive caller counts cannot exceed total calls")
        return self


class ResourceObservation(RuntimeModel):
    repetition: StrictInt = Field(ge=1, le=20)
    wall_seconds: StrictFloat | None = Field(default=None, ge=0.0)
    user_cpu_seconds: StrictFloat | None = Field(default=None, ge=0.0)
    system_cpu_seconds: StrictFloat | None = Field(default=None, ge=0.0)
    peak_rss_bytes: StrictInt | None = Field(default=None, ge=0)
    read_bytes: StrictInt | None = Field(default=None, ge=0)
    write_bytes: StrictInt | None = Field(default=None, ge=0)
    read_count: StrictInt | None = Field(default=None, ge=0)
    write_count: StrictInt | None = Field(default=None, ge=0)
    maximum_owned_processes: StrictInt | None = Field(default=None, ge=1)


class ProfileStatistic(RuntimeModel):
    frame_id: StrictStr = Field(pattern=r"^profile-frame-[0-9a-f]{20}$")
    call_count_mean: StrictFloat = Field(ge=0.0)
    total_seconds_mean: StrictFloat = Field(ge=0.0)
    total_seconds_median: StrictFloat = Field(ge=0.0)
    total_seconds_minimum: StrictFloat = Field(ge=0.0)
    total_seconds_maximum: StrictFloat = Field(ge=0.0)
    total_seconds_standard_deviation: StrictFloat = Field(ge=0.0)
    total_seconds_coefficient_of_variation: StrictFloat | None = Field(default=None, ge=0.0)


class ProfileDiffRecord(RuntimeModel):
    frame_identity: StrictStr = Field(min_length=1)
    baseline_total_seconds: StrictFloat | None = Field(ge=0.0)
    candidate_total_seconds: StrictFloat | None = Field(ge=0.0)
    absolute_total_seconds_delta: StrictFloat | None
    relative_total_seconds_delta: StrictFloat | None = None
    baseline_cumulative_seconds: StrictFloat | None = Field(ge=0.0)
    candidate_cumulative_seconds: StrictFloat | None = Field(ge=0.0)
    absolute_cumulative_seconds_delta: StrictFloat | None
    relative_cumulative_seconds_delta: StrictFloat | None = None


class ProfileDiffReport(RuntimeModel):
    schema_name: Literal["code-analysis-profile-diff"] = "code-analysis-profile-diff"
    schema_version: Literal["1.0.0"] = "1.0.0"
    baseline_profile_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_profile_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_report_sha256: StrictStr | None = Field(pattern=r"^[0-9a-f]{64}$")
    workload_identity_sha256: StrictStr | None = Field(pattern=r"^[0-9a-f]{64}$")
    diagnostic_only: StrictBool = False
    claim_eligible: StrictBool = False
    decisions: tuple[CompatibilityDecision, ...] = ()
    records: tuple[ProfileDiffRecord, ...]

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        identities = [item.frame_identity for item in self.records]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("profile diff frame identities must be unique and sorted")
        if self.claim_eligible and (
            self.diagnostic_only
            or not self.decisions
            or any(item.outcome != "matched" for item in self.decisions)
        ):
            raise ValueError("ineligible comparison cannot authorize claims")
        return self


class ProfileReport(RuntimeModel):
    schema_name: Literal["code-analysis-profile-report"] = "code-analysis-profile-report"
    schema_version: Literal["1.0.0"] = "1.0.0"
    tool_version: StrictStr
    producer: ProfileProducer = ProfileProducer.UNKNOWN
    producer_bindings: tuple[ProfileProducerBinding, ...] = ()
    profile_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_report_sha256: StrictStr | None = Field(pattern=r"^[0-9a-f]{64}$")
    workload_identity_sha256: StrictStr | None = Field(pattern=r"^[0-9a-f]{64}$")
    repetitions: StrictInt = Field(ge=1, le=20)
    expected_status: StrictStr = Field(min_length=1)
    observed_statuses: tuple[StrictInt | None, ...]
    runtime: RuntimeEvidence = RuntimeEvidence()
    frames: tuple[ProfileFrame, ...]
    runtime_edges: tuple[RuntimeCallEdge, ...]
    statistics: tuple[ProfileStatistic, ...]
    resources: tuple[ResourceObservation, ...]
    providers: tuple[ProviderResult, ...]
    diagnostics: tuple[Diagnostic, ...]
    completeness: Completeness
    limitations: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def validate_identity_graph(self) -> Self:
        if self.producer_bindings:
            hashes = [item.profile_sha256 for item in self.producer_bindings]
            inputs = {item.sha256 for item in self.runtime.artifacts if item.kind == "profile"}
            if hashes != sorted(set(hashes)) or set(hashes) - inputs:
                raise ValueError("producer bindings require unique sorted input profile hashes")
            producers = {item.producer for item in self.producer_bindings}
            expected = (
                next(iter(producers))
                if len(producers) == 1 and set(hashes) == inputs
                else ProfileProducer.UNKNOWN
            )
            if self.producer is not expected:
                raise ValueError("report producer disagrees with artifact declarations")
        if (
            len(self.observed_statuses) != self.repetitions
            or len(self.resources) != self.repetitions
        ):
            raise ValueError("profile repetitions must match statuses and resource observations")
        frame_ids = [frame.frame_id for frame in self.frames]
        if frame_ids != sorted(frame_ids) or len(frame_ids) != len(set(frame_ids)):
            raise ValueError("profile frame IDs must be unique and sorted")
        known = set(frame_ids)
        for group in (self.runtime.categories, self.runtime.correlations):
            identities = [item.frame_id for item in group]
            if identities != sorted(set(identities)) or set(identities) - known:
                raise ValueError("runtime annotations require unique sorted known frame IDs")
        if set(self.runtime.caller_only_frame_ids) - known:
            raise ValueError("caller-only identity references unknown frame")
        if (
            any(value is None for value in self.observed_statuses)
            and self.completeness == Completeness.COMPLETE
        ):
            raise ValueError("unknown workload status cannot claim complete runtime evidence")
        if self.completeness == Completeness.COMPLETE:
            if any(provider.completeness != Completeness.COMPLETE for provider in self.providers):
                raise ValueError("incomplete provider cannot claim complete runtime evidence")
            if self.runtime.lifecycle and (
                self.runtime.process.state != ProcessProofState.VERIFIED
                or self.runtime.source_binding != SourceBindingState.MATCHED
                or self.runtime.collection.missing_fields()
                or any(event.state == LifecycleState.FAILED for event in self.runtime.lifecycle)
            ):
                raise ValueError("complete managed capture requires proven identity and lifecycle")
            if any(
                not step.step.startswith("readiness-")
                and (
                    step.failure_status is not None
                    or step.shutdown_status is not None
                    or step.stream_truncated
                )
                for step in self.runtime.steps
            ):
                raise ValueError("failed runtime step cannot claim complete capture")
        edge_ids = [edge.runtime_edge_id for edge in self.runtime_edges]
        if edge_ids != sorted(edge_ids) or len(edge_ids) != len(set(edge_ids)):
            raise ValueError("runtime edge IDs must be unique and sorted")
        for edge in self.runtime_edges:
            if edge.caller_frame_id not in known or edge.callee_frame_id not in known:
                raise ValueError("runtime edge references an unknown frame")
        statistic_ids = [statistic.frame_id for statistic in self.statistics]
        if statistic_ids != sorted(statistic_ids) or len(statistic_ids) != len(set(statistic_ids)):
            raise ValueError("profile statistic frame IDs must be unique and sorted")
        if set(statistic_ids) - known:
            raise ValueError("profile statistics reference unknown frames")
        if list(self.limitations) != sorted(self.limitations) or len(self.limitations) != len(
            set(self.limitations)
        ):
            raise ValueError("profile limitations must be unique and sorted")
        return self
