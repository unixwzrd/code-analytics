"""Coverage ingestion orchestration and report reconciliation."""

from __future__ import annotations

import errno
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from code_analysis.config import PhaseFourLimits
from code_analysis.coverage_artifacts import coverage_version, load_coverage_artifact
from code_analysis.coverage_binding import (
    CoverageBindingCounters,
    bind_coverage_artifact,
    build_binding_context,
)
from code_analysis.models import (
    Completeness,
    Diagnostic,
    DiagnosticLevel,
    ProviderResult,
    ProviderState,
)
from code_analysis.proposal_reconciliation import reconcile_proposal_evidence
from code_analysis.report_models import (
    AnalysisReport,
    CoverageArtifactRecord,
    CoverageBindingState,
    CoverageFileRecord,
    SectionCoverage,
    SectionState,
    SummaryCounts,
    SymbolCoverageRecord,
)
from code_analysis.reports.json_report import canonical_json


@dataclass(frozen=True, slots=True)
class _CoverageCollection:
    artifacts: tuple[CoverageArtifactRecord, ...]
    files: tuple[CoverageFileRecord, ...]
    observations: tuple[SymbolCoverageRecord, ...]
    bound_failures: int
    truncated: int


def import_coverage(
    report: AnalysisReport,
    data_paths: tuple[Path, ...],
    *,
    source_root: Path | None,
    workload_identity_sha256: str,
    replace: bool = False,
    limits: PhaseFourLimits | None = None,
) -> AnalysisReport:
    """Load, bind, and deterministically reconcile coverage with a saved report."""
    if not data_paths:
        raise OSError(errno.EINVAL, "coverage import requires at least one data artifact")
    selected_limits = limits or PhaseFourLimits()
    collection = _collect_coverage(
        report,
        data_paths,
        source_root.resolve() if source_root is not None else None,
        workload_identity_sha256,
        replace,
        selected_limits,
    )
    prior_count = 0 if replace else len(report.coverage_artifacts)
    if len(collection.artifacts) == prior_count:
        return report
    return _reconcile_coverage(report, collection)


def _collect_coverage(
    report: AnalysisReport,
    data_paths: tuple[Path, ...],
    source_root: Path | None,
    workload_identity_sha256: str,
    replace: bool,
    limits: PhaseFourLimits,
) -> _CoverageCollection:
    artifacts = list(() if replace else report.coverage_artifacts)
    files = list(() if replace else report.coverage_files)
    observations = list(() if replace else report.coverage_observations)
    known_ids = {item.coverage_id for item in artifacts}
    counters = CoverageBindingCounters(
        arc_count=sum(len(item.arcs) + len(item.missing_arcs) for item in files),
        context_file_count=sum(len(item.contexts) + 1 for item in files),
    )
    source_report_sha = _source_report_sha(report)
    binding_context = build_binding_context(report, source_root, limits)
    bound_failures = 0
    truncated = 0
    for path in data_paths:
        imported = load_coverage_artifact(path)
        coverage_id = _stable_id("coverage", imported.artifact_sha256)
        if coverage_id in known_ids:
            continue
        bound = bind_coverage_artifact(binding_context, imported, coverage_id, counters)
        counters = bound.counters
        bound_failures += bound.bound_failures
        truncated += bound.truncated
        artifacts.append(
            _artifact_record(
                coverage_id,
                imported.artifact_sha256,
                imported.limitations,
                workload_identity_sha256,
                source_report_sha,
                bound.bindings,
                bound.contexts,
            )
        )
        files.extend(bound.files)
        observations.extend(bound.observations)
        known_ids.add(coverage_id)
    unique_observations = {item.coverage_observation_id: item for item in observations}
    return _CoverageCollection(
        artifacts=tuple(sorted(artifacts, key=lambda item: item.coverage_id)),
        files=tuple(sorted(files, key=lambda item: item.coverage_file_id)),
        observations=tuple(
            sorted(unique_observations.values(), key=lambda item: item.coverage_observation_id)
        ),
        bound_failures=bound_failures,
        truncated=truncated,
    )


def _artifact_record(
    coverage_id: str,
    artifact_sha256: str,
    limitations: tuple[str, ...],
    workload_identity_sha256: str,
    source_report_sha256: str,
    bindings: tuple[CoverageBindingState, ...],
    contexts: tuple[str, ...],
) -> CoverageArtifactRecord:
    complete = bool(bindings) and all(item is CoverageBindingState.BOUND for item in bindings)
    binding = (
        CoverageBindingState.BOUND
        if complete
        else (
            CoverageBindingState.SOURCE_MISMATCH
            if CoverageBindingState.SOURCE_MISMATCH in bindings
            else CoverageBindingState.UNBOUND
        )
    )
    return CoverageArtifactRecord(
        coverage_id=coverage_id,
        artifact_sha256=artifact_sha256,
        workload_identity_sha256=workload_identity_sha256,
        source_report_sha256=source_report_sha256,
        binding=binding,
        contexts=contexts,
        completeness=Completeness.COMPLETE if complete else Completeness.PARTIAL,
        limitations=limitations,
    )


def _reconcile_coverage(report: AnalysisReport, collection: _CoverageCollection) -> AnalysisReport:
    diagnostics = _coverage_diagnostics(collection)
    state = _coverage_state(collection)
    overall = _overall_completeness(report.completeness, state)
    provider = ProviderResult(
        provider_id="coverage-import",
        state=ProviderState.SUCCESS if state is SectionState.COMPLETE else ProviderState.PARTIAL,
        executable="coverage.py",
        version=coverage_version(),
        duration_ms=0,
        observations=("branches", "contexts", "lines", "source-hash-binding"),
        diagnostics=diagnostics,
        completeness=overall,
    )
    updated = report.model_copy(
        update={
            "run_identity_sha256": _coverage_identity(report, collection),
            "coverage_artifacts": collection.artifacts,
            "coverage_files": collection.files,
            "coverage_observations": collection.observations,
            "section_coverage": _section_coverage(report, collection, state, diagnostics),
            "providers": (
                *(item for item in report.providers if item.provider_id != "coverage-import"),
                provider,
            ),
            "diagnostics": tuple(
                item for item in report.diagnostics if item.provider != "coverage-import"
            )
            + diagnostics,
            "completeness": overall,
            "limitations": tuple(
                sorted(
                    {
                        *report.limitations,
                        "Coverage proves only that the supplied workload traversed a line or "
                        "branch.",
                        "Threads, subprocesses, native extensions, and unobserved code may be "
                        "absent.",
                    }
                )
            ),
            "summary": SummaryCounts(
                **{
                    **report.summary.model_dump(),
                    "coverage_artifacts": len(collection.artifacts),
                    "coverage_files": len(collection.files),
                    "coverage_observations": len(collection.observations),
                }
            ),
        }
    )
    return reconcile_proposal_evidence(updated)


def _coverage_diagnostics(collection: _CoverageCollection) -> tuple[Diagnostic, ...]:
    diagnostics = []
    if collection.truncated:
        diagnostics.append(
            Diagnostic(
                code="coverage-bound-truncated",
                level=DiagnosticLevel.WARNING,
                message=(
                    f"coverage bounds omitted {collection.truncated} contexts, files, or arcs"
                ),
                provider="coverage-import",
            )
        )
    if collection.bound_failures:
        diagnostics.append(
            Diagnostic(
                code="coverage-source-unbound",
                level=DiagnosticLevel.WARNING,
                message=(
                    f"{collection.bound_failures} coverage files were not bound to matching "
                    "source hashes"
                ),
                provider="coverage-import",
            )
        )
    return tuple(diagnostics)


def _coverage_state(collection: _CoverageCollection) -> SectionState:
    if collection.truncated:
        return SectionState.TRUNCATED
    if collection.bound_failures or not collection.artifacts:
        return SectionState.PARTIAL
    return SectionState.COMPLETE


def _overall_completeness(current: Completeness, state: SectionState) -> Completeness:
    if current is Completeness.FAILED:
        return Completeness.FAILED
    if current is Completeness.PARTIAL or state in {SectionState.PARTIAL, SectionState.TRUNCATED}:
        return Completeness.PARTIAL
    return Completeness.COMPLETE


def _section_coverage(
    report: AnalysisReport,
    collection: _CoverageCollection,
    state: SectionState,
    diagnostics: tuple[Diagnostic, ...],
) -> tuple[SectionCoverage, ...]:
    replacement = SectionCoverage(
        section="coverage",
        state=state,
        parsed=len(collection.files),
        unresolved=collection.bound_failures,
        truncated=collection.truncated,
        diagnostic_codes=tuple(sorted(item.code for item in diagnostics)),
    )
    return tuple(
        replacement if item.section == "coverage" else item for item in report.section_coverage
    )


def _coverage_identity(report: AnalysisReport, collection: _CoverageCollection) -> str:
    payload = {
        "prior": report.run_identity_sha256,
        "artifacts": [item.model_dump(mode="json") for item in collection.artifacts],
        "files": [item.model_dump(mode="json") for item in collection.files],
        "observations": [item.model_dump(mode="json") for item in collection.observations],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _source_report_sha(report: AnalysisReport) -> str:
    if report.coverage_artifacts:
        return report.coverage_artifacts[0].source_report_sha256
    return hashlib.sha256(canonical_json(report).encode()).hexdigest()


def _stable_id(prefix: str, *values: str) -> str:
    return f"{prefix}-{hashlib.sha256(chr(31).join(values).encode()).hexdigest()[:20]}"
