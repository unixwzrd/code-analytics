"""Strict loading of coverage JSON and CoverageData artifacts."""

from __future__ import annotations

import errno
import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, JsonValue, StrictBool, StrictFloat, StrictInt, StrictStr

from code_analysis.models import StrictModel


class CoverageJsonMeta(StrictModel):
    format: StrictInt
    version: StrictStr
    timestamp: StrictStr
    branch_coverage: StrictBool
    show_contexts: StrictBool


class CoverageJsonSummary(StrictModel):
    covered_lines: StrictInt
    num_statements: StrictInt
    percent_covered: StrictFloat
    percent_covered_display: StrictStr
    missing_lines: StrictInt
    excluded_lines: StrictInt
    num_branches: StrictInt = 0
    num_partial_branches: StrictInt = 0
    covered_branches: StrictInt = 0
    missing_branches: StrictInt = 0
    percent_statements_covered: StrictFloat | None = None
    percent_statements_covered_display: StrictStr | None = None
    percent_branches_covered: StrictFloat | None = None
    percent_branches_covered_display: StrictStr | None = None


class CoverageJsonFile(StrictModel):
    executed_lines: tuple[StrictInt, ...]
    summary: CoverageJsonSummary
    missing_lines: tuple[StrictInt, ...]
    excluded_lines: tuple[StrictInt, ...]
    executed_branches: tuple[tuple[StrictInt, StrictInt], ...] = ()
    missing_branches: tuple[tuple[StrictInt, StrictInt], ...] = ()
    contexts: dict[StrictStr, tuple[StrictStr, ...]] = Field(default_factory=dict)
    functions: dict[StrictStr, JsonValue] = Field(default_factory=dict)
    classes: dict[StrictStr, JsonValue] = Field(default_factory=dict)


class CoverageJsonDocument(StrictModel):
    meta: CoverageJsonMeta
    files: dict[StrictStr, CoverageJsonFile]
    totals: CoverageJsonSummary


@dataclass(frozen=True, slots=True)
class ImportedCoverageFile:
    original_path: str
    covered_lines: tuple[int, ...]
    missing_lines: tuple[int, ...]
    arcs: tuple[tuple[int, int], ...]
    missing_arcs: tuple[tuple[int, int], ...]
    contexts_by_line: tuple[tuple[int, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class ImportedCoverageArtifact:
    artifact_sha256: str
    files: tuple[ImportedCoverageFile, ...]
    limitations: tuple[str, ...]


def load_coverage_artifact(path: Path) -> ImportedCoverageArtifact:
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if path.suffix == ".json":
        return _load_json_artifact(path, payload, digest)
    return _load_coverage_data(path, digest)


def coverage_version() -> str | None:
    try:
        import coverage
    except ImportError:
        return None
    return coverage.__version__


def _load_json_artifact(path: Path, payload: bytes, digest: str) -> ImportedCoverageArtifact:
    try:
        document = CoverageJsonDocument.model_validate_json(payload)
    except ValueError as exc:
        raise OSError(errno.EBADMSG, "coverage JSON is malformed", str(path)) from exc
    files = tuple(
        ImportedCoverageFile(
            original_path=name,
            covered_lines=tuple(sorted(set(item.executed_lines))),
            missing_lines=tuple(sorted(set(item.missing_lines))),
            arcs=tuple(sorted(set(item.executed_branches))),
            missing_arcs=tuple(sorted(set(item.missing_branches))),
            contexts_by_line=tuple(
                sorted(
                    (
                        int(line),
                        tuple(sorted({_normalized_context(value) for value in values})),
                    )
                    for line, values in item.contexts.items()
                )
            ),
        )
        for name, item in sorted(document.files.items())
    )
    return ImportedCoverageArtifact(digest, files, ())


def _load_coverage_data(path: Path, digest: str) -> ImportedCoverageArtifact:
    try:
        from coverage import CoverageData
    except ImportError as exc:
        raise OSError(errno.ENOSYS, "coverage.py is unavailable") from exc
    try:
        data = CoverageData(basename=str(path))
        data.read()
        files = tuple(
            ImportedCoverageFile(
                original_path=name,
                covered_lines=tuple(sorted(set(data.lines(name) or ()))),
                missing_lines=(),
                arcs=tuple(sorted(set(data.arcs(name) or ()))),
                missing_arcs=(),
                contexts_by_line=tuple(
                    sorted(
                        (
                            line,
                            tuple(sorted({_normalized_context(value) for value in contexts})),
                        )
                        for line, contexts in data.contexts_by_lineno(name).items()
                    )
                ),
            )
            for name in sorted(data.measured_files())
        )
    except Exception as exc:
        raise OSError(errno.EBADMSG, "coverage data is malformed", str(path)) from exc
    return ImportedCoverageArtifact(
        digest,
        files,
        ("CoverageData does not identify unexecuted statements without bound source analysis.",),
    )


def _normalized_context(value: str) -> str:
    return value if value else "<default-context>"
