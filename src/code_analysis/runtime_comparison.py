"""Field-by-field comparison eligibility and missing-aware runtime deltas."""

from __future__ import annotations

import errno

from code_analysis.models import Completeness
from code_analysis.profile_models import (
    ProfileDiffRecord,
    ProfileDiffReport,
    ProfileFrame,
    ProfileProducer,
    ProfileReport,
)
from code_analysis.runtime_models import (
    CompatibilityDecision,
    ProcessProofState,
    SourceBindingState,
)


def format_comparison_frame_identity(
    frame: ProfileFrame,
    *,
    baseline_artifact_count: int,
    candidate_artifact_count: int,
) -> str:
    # Multiple independent artifacts are not silently paired by coincident frame names.
    prefix = (
        f"{frame.artifact_sha256}:"
        if baseline_artifact_count > 1 or candidate_artifact_count > 1
        else ""
    )
    return f"{prefix}{frame.path}:{frame.line}:{frame.function}"


def comparison_frame_key(
    baseline: ProfileReport, candidate: ProfileReport, frame: ProfileFrame
) -> str:
    return format_comparison_frame_identity(
        frame,
        baseline_artifact_count=len(baseline.runtime.artifacts),
        candidate_artifact_count=len(candidate.runtime.artifacts),
    )


def comparison_frame_bindings(
    baseline: ProfileReport, candidate: ProfileReport
) -> tuple[tuple[str, str | None, str | None], ...]:
    inventories: list[dict[str, ProfileFrame]] = []
    for profile in (baseline, candidate):
        inventory: dict[str, ProfileFrame] = {}
        for frame in profile.frames:
            identity = comparison_frame_key(baseline, candidate, frame)
            if identity in inventory:
                raise ValueError("runtime comparison source identity is ambiguous")
            inventory[identity] = frame
        inventories.append(inventory)
    left, right = inventories
    bindings: list[tuple[str, str | None, str | None]] = []
    for key in sorted(set(left) | set(right)):
        before, after = left.get(key), right.get(key)
        bindings.append(
            (
                key,
                before.frame_id if before is not None else None,
                after.frame_id if after is not None else None,
            )
        )
    return tuple(bindings)


def compare_runtime_profiles(
    baseline: ProfileReport, candidate: ProfileReport, *, diagnostic_only: bool = False
) -> ProfileDiffReport:
    decisions = []
    producer_outcome = (
        "unresolved"
        if ProfileProducer.UNKNOWN in (baseline.producer, candidate.producer)
        else "matched" if baseline.producer == candidate.producer else "mismatched"
    )
    decisions.append(
        CompatibilityDecision(
            field="profiler_producer",
            outcome=producer_outcome,
            reason=(
                f"Producer baseline={baseline.producer.value}; "
                f"candidate={candidate.producer.value}."
            ),
        )
    )
    edge_times_available = all(
        edge.total_seconds is not None and edge.cumulative_seconds is not None
        for report in (baseline, candidate)
        for edge in report.runtime_edges
    )
    decisions.append(
        CompatibilityDecision(
            field="caller_edge_timings",
            outcome="matched" if edge_times_available else "unresolved",
            reason=(
                "Caller-edge timings are available; this comparison reports frame deltas only."
                if edge_times_available
                else "Integer-only callers have unavailable edge timings; "
                "edge-time comparisons are unavailable. "
                "Frame deltas do not apportion time to callers."
            ),
        )
    )
    for field in type(baseline.runtime.collection).model_fields:
        before = getattr(baseline.runtime.collection, field)
        after = getattr(candidate.runtime.collection, field)
        outcome = (
            "unresolved"
            if before is None or after is None
            else ("matched" if before == after else "mismatched")
        )
        decisions.append(
            CompatibilityDecision(
                field=field, outcome=outcome, reason=f"Semantic {field}: {outcome}."
            )
        )
    for field, valid in (
        (
            "source_binding",
            all(
                report.runtime.source_binding == SourceBindingState.MATCHED
                for report in (baseline, candidate)
            ),
        ),
        (
            "process_proof",
            all(
                report.runtime.process.state == ProcessProofState.VERIFIED
                for report in (baseline, candidate)
            ),
        ),
        (
            "complete_capture",
            all(report.completeness == Completeness.COMPLETE for report in (baseline, candidate)),
        ),
    ):
        decisions.append(
            CompatibilityDecision(
                field=field,
                outcome="matched" if valid else "unresolved",
                reason=f"Both captures require proven {field}.",
            )
        )
    for field in ("source_report_sha256", "workload_identity_sha256"):
        before, after = getattr(baseline, field), getattr(candidate, field)
        outcome = (
            "unresolved"
            if before is None or after is None
            else ("matched" if before == after else "mismatched")
        )
        decisions.append(
            CompatibilityDecision(field=field, outcome=outcome, reason=f"Bound {field}: {outcome}.")
        )
    eligible = all(item.outcome == "matched" for item in decisions)
    if not eligible and not diagnostic_only:
        raise OSError(
            errno.EINVAL,
            "runtime comparison requires complete matching collection "
            "identity; use diagnostic-only for claim-ineligible inspection",
        )

    left, right = (
        {comparison_frame_key(baseline, candidate, item): item for item in report.frames}
        for report in (baseline, candidate)
    )
    records = []
    for key in sorted(set(left) | set(right)):
        before, after = left.get(key), right.get(key)
        bt, at = before.total_seconds if before else None, after.total_seconds if after else None
        bc, ac = before.cumulative_seconds if before else None, (
            after.cumulative_seconds if after else None
        )
        records.append(
            ProfileDiffRecord(
                frame_identity=key,
                baseline_total_seconds=bt,
                candidate_total_seconds=at,
                absolute_total_seconds_delta=_delta(bt, at),
                relative_total_seconds_delta=_relative(bt, at),
                baseline_cumulative_seconds=bc,
                candidate_cumulative_seconds=ac,
                absolute_cumulative_seconds_delta=_delta(bc, ac),
                relative_cumulative_seconds_delta=_relative(bc, ac),
            )
        )
    return ProfileDiffReport(
        baseline_profile_identity_sha256=baseline.profile_identity_sha256,
        candidate_profile_identity_sha256=candidate.profile_identity_sha256,
        source_report_sha256=baseline.source_report_sha256,
        workload_identity_sha256=baseline.workload_identity_sha256,
        diagnostic_only=diagnostic_only,
        claim_eligible=eligible and not diagnostic_only,
        decisions=tuple(decisions),
        records=tuple(records),
    )


def _delta(before: float | None, after: float | None) -> float | None:
    return after - before if before is not None and after is not None else None


def _relative(before: float | None, after: float | None) -> float | None:
    return (
        (after - before) / before
        if before is not None and before != 0 and after is not None
        else None
    )
