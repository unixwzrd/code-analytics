"""Portable source binding and symbol overlap for imported coverage artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from code_analysis.config import PhaseFourLimits
from code_analysis.coverage_artifacts import ImportedCoverageArtifact, ImportedCoverageFile
from code_analysis.report_models import (
    AnalysisReport,
    AnalysisSubject,
    CoverageBindingState,
    CoverageFileRecord,
    SymbolCoverageRecord,
)


@dataclass(frozen=True, slots=True)
class CoverageBindingContext:
    source_root: Path | None
    limits: PhaseFourLimits
    module_hashes: Mapping[str, str]
    subjects_by_path: Mapping[str, tuple[AnalysisSubject, ...]]


@dataclass(frozen=True, slots=True)
class CoverageBindingCounters:
    arc_count: int
    context_file_count: int


@dataclass(frozen=True, slots=True)
class BoundCoverageArtifact:
    files: tuple[CoverageFileRecord, ...]
    observations: tuple[SymbolCoverageRecord, ...]
    bindings: tuple[CoverageBindingState, ...]
    contexts: tuple[str, ...]
    bound_failures: int
    truncated: int
    counters: CoverageBindingCounters


def build_binding_context(
    report: AnalysisReport,
    source_root: Path | None,
    limits: PhaseFourLimits,
) -> CoverageBindingContext:
    module_hashes = {item.path: item.source_sha256 for item in report.modules}
    module_hashes.update({item.path: item.source_sha256 for item in report.test_modules})
    subjects: dict[str, list[AnalysisSubject]] = {}
    for subject in report.subjects:
        for evidence in subject.evidence:
            subjects.setdefault(evidence.span.path, []).append(subject)
    return CoverageBindingContext(
        source_root=source_root,
        limits=limits,
        module_hashes=MappingProxyType(module_hashes),
        subjects_by_path=MappingProxyType(
            {path: tuple(values) for path, values in subjects.items()}
        ),
    )


def bind_coverage_artifact(
    context: CoverageBindingContext,
    imported: ImportedCoverageArtifact,
    coverage_id: str,
    counters: CoverageBindingCounters,
) -> BoundCoverageArtifact:
    files: list[CoverageFileRecord] = []
    observations: list[SymbolCoverageRecord] = []
    bindings: list[CoverageBindingState] = []
    contexts: set[str] = set()
    bound_failures = 0
    truncated = 0
    for imported_file in imported.files:
        file_result = _bind_file(
            context, imported_file, imported.limitations, coverage_id, counters
        )
        counters = file_result.counters
        truncated += file_result.truncated
        if file_result.file is None:
            break
        files.append(file_result.file)
        observations.extend(file_result.observations)
        bindings.append(file_result.file.binding)
        contexts.update(file_result.file.contexts)
        bound_failures += int(file_result.file.binding is not CoverageBindingState.BOUND)
    return BoundCoverageArtifact(
        files=tuple(files),
        observations=tuple(observations),
        bindings=tuple(bindings),
        contexts=tuple(sorted(contexts)),
        bound_failures=bound_failures,
        truncated=truncated,
        counters=counters,
    )


@dataclass(frozen=True, slots=True)
class _BoundCoverageFile:
    file: CoverageFileRecord | None
    observations: tuple[SymbolCoverageRecord, ...]
    truncated: int
    counters: CoverageBindingCounters


def _bind_file(
    context: CoverageBindingContext,
    imported: ImportedCoverageFile,
    artifact_limitations: tuple[str, ...],
    coverage_id: str,
    counters: CoverageBindingCounters,
) -> _BoundCoverageFile:
    portable, physical = _resolve_path(
        imported.original_path, context.source_root, context.module_hashes
    )
    binding, source_sha, limitations = _bind_source(
        portable, physical, context.module_hashes, artifact_limitations
    )
    portable = portable or _unbound_logical_path(imported.original_path)
    contexts = tuple(
        sorted(
            {value for _line, line_contexts in imported.contexts_by_line for value in line_contexts}
        )
    )
    context_file_count = counters.context_file_count + len(contexts) + 1
    if context_file_count > context.limits.max_coverage_contexts_files:
        return _BoundCoverageFile(
            None,
            (),
            1,
            CoverageBindingCounters(counters.arc_count, context_file_count),
        )
    arcs, missing_arcs, truncated = _bounded_arcs(imported, counters, context.limits)
    file_record = CoverageFileRecord(
        coverage_file_id=_stable_id("coverage-file", coverage_id, portable),
        coverage_id=coverage_id,
        path=portable,
        source_sha256=source_sha,
        binding=binding,
        covered_lines=imported.covered_lines,
        missing_lines=imported.missing_lines,
        arcs=arcs,
        missing_arcs=missing_arcs,
        contexts=contexts,
        limitations=limitations,
    )
    observations = _symbol_observations(context, imported, coverage_id, portable, missing_arcs)
    return _BoundCoverageFile(
        file_record,
        observations,
        truncated,
        CoverageBindingCounters(
            counters.arc_count + len(arcs) + len(missing_arcs), context_file_count
        ),
    )


def _bind_source(
    portable: str | None,
    physical: Path | None,
    module_hashes: Mapping[str, str],
    base_limitations: tuple[str, ...],
) -> tuple[CoverageBindingState, str | None, tuple[str, ...]]:
    limitations = list(base_limitations)
    if portable is not None and physical is not None and physical.is_file():
        source_sha = hashlib.sha256(physical.read_bytes()).hexdigest()
        binding = (
            CoverageBindingState.BOUND
            if module_hashes.get(portable) == source_sha
            else CoverageBindingState.SOURCE_MISMATCH
        )
        if binding is CoverageBindingState.SOURCE_MISMATCH:
            limitations.append("Coverage source hash does not match the saved report.")
        return binding, source_sha, tuple(sorted(set(limitations)))
    limitations.append("Coverage source was unavailable for hash binding.")
    return CoverageBindingState.UNBOUND, None, tuple(sorted(set(limitations)))


def _bounded_arcs(
    imported: ImportedCoverageFile,
    counters: CoverageBindingCounters,
    limits: PhaseFourLimits,
) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...], int]:
    prospective = len(imported.arcs) + len(imported.missing_arcs)
    if counters.arc_count + prospective <= limits.max_coverage_arcs:
        return imported.arcs, imported.missing_arcs, 0
    available = max(0, limits.max_coverage_arcs - counters.arc_count)
    combined = tuple(sorted((*imported.arcs, *imported.missing_arcs)))[:available]
    arcs = tuple(item for item in combined if item in imported.arcs)
    missing_arcs = tuple(item for item in combined if item in imported.missing_arcs)
    return arcs, missing_arcs, prospective - len(combined)


def _symbol_observations(
    context: CoverageBindingContext,
    imported: ImportedCoverageFile,
    coverage_id: str,
    portable: str,
    missing_arcs: tuple[tuple[int, int], ...],
) -> tuple[SymbolCoverageRecord, ...]:
    line_contexts = dict(imported.contexts_by_line)
    records = []
    for subject in context.subjects_by_path.get(portable, ()):
        span = subject.evidence[0].span
        covered = tuple(
            line for line in imported.covered_lines if span.start_line <= line <= span.end_line
        )
        missing = tuple(
            line for line in imported.missing_lines if span.start_line <= line <= span.end_line
        )
        records.append(
            SymbolCoverageRecord(
                coverage_observation_id=_stable_id(
                    "coverage-observation", coverage_id, subject.subject_id
                ),
                coverage_id=coverage_id,
                subject_id=subject.subject_id,
                covered_lines=covered,
                missing_lines=missing,
                contexts=tuple(
                    sorted({value for line in covered for value in line_contexts.get(line, ())})
                ),
                missing_arcs=tuple(
                    arc for arc in missing_arcs if span.start_line <= abs(arc[0]) <= span.end_line
                ),
            )
        )
    return tuple(records)


def _resolve_path(
    original: str,
    root: Path | None,
    module_hashes: Mapping[str, str],
) -> tuple[str | None, Path | None]:
    candidate = Path(original)
    physical = candidate if candidate.is_absolute() else (root / candidate if root else None)
    if root is not None and physical is not None:
        try:
            resolved = physical.resolve()
            relative = resolved.relative_to(root).as_posix()
        except (OSError, ValueError):
            return None, None
        if relative in module_hashes:
            return relative, resolved
    normalized = PurePosixPath(original.replace("\\", "/")).as_posix()
    matches = [
        path for path in module_hashes if normalized == path or normalized.endswith("/" + path)
    ]
    if len(matches) == 1:
        matched = matches[0]
        return matched, root / matched if root is not None else None
    return None, None


def _unbound_logical_path(original: str) -> str:
    normalized = PurePosixPath(original.replace("\\", "/"))
    return f"unbound/{normalized.name or 'unknown'}"


def _stable_id(prefix: str, *values: str) -> str:
    return f"{prefix}-{hashlib.sha256(chr(31).join(values).encode()).hexdigest()[:20]}"
