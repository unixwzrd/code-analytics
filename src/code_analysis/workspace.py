"""Owned project-local workspace and host-local run metadata."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from code_analysis import __version__
from code_analysis.context import AnalysisContext
from code_analysis.models import ProviderAvailability, StrictModel

WORKSPACE_SCHEMA_NAME: Literal["code-analysis-workspace"] = "code-analysis-workspace"
WORKSPACE_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
RUN_SCHEMA_NAME: Literal["code-analysis-run"] = "code-analysis-run"
RUN_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
GENERATION_SCHEMA_NAME: Literal["code-analysis-generation"] = "code-analysis-generation"
GENERATION_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
CACHE_LAYOUT_VERSION: Literal["current"] = "current"
WORKSPACE_DIRECTORY = ".code-analysis"
WORKSPACE_IGNORE = "*\n"


class WorkspaceMarker(StrictModel):
    """Ownership marker required before the tool writes inside a workspace."""

    schema_name: Literal["code-analysis-workspace"] = WORKSPACE_SCHEMA_NAME
    schema_version: Literal["1.0.0"] = WORKSPACE_SCHEMA_VERSION
    target_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    project_root: StrictStr
    logical_root: StrictStr
    source_roots: tuple[StrictStr, ...]
    created_by_version: StrictStr
    cache_layout_version: Literal["current"] = CACHE_LAYOUT_VERSION

    @model_validator(mode="after")
    def validate_local_identity(self) -> WorkspaceMarker:
        if not Path(self.project_root).is_absolute():
            raise ValueError("workspace project root must be absolute")
        return self


class CacheStatistics(StrictModel):
    enabled: StrictBool = True
    hits: StrictInt = Field(default=0, ge=0)
    misses: StrictInt = Field(default=0, ge=0)
    invalidations: StrictInt = Field(default=0, ge=0)
    evictions: StrictInt = Field(default=0, ge=0)
    bytes: StrictInt = Field(default=0, ge=0)


class LocalRunRecord(StrictModel):
    """Host-specific operational metadata excluded from portable reports."""

    schema_name: Literal["code-analysis-run"] = RUN_SCHEMA_NAME
    schema_version: Literal["1.0.0"] = RUN_SCHEMA_VERSION
    invocation_id: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: StrictStr
    duration_ms: StrictInt = Field(ge=0)
    project_root: StrictStr
    workspace_path: StrictStr
    target_path: StrictStr
    interpreter_path: StrictStr
    command: tuple[StrictStr, ...]
    storage_configuration_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    providers: tuple[ProviderAvailability, ...]
    cache: CacheStatistics = CacheStatistics()
    portable_report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("started_at")
    @classmethod
    def validate_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("run timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_local_paths(self) -> LocalRunRecord:
        for value in (
            self.project_root,
            self.workspace_path,
            self.target_path,
            self.interpreter_path,
        ):
            if not Path(value).is_absolute():
                raise ValueError("local run paths must be absolute")
        if not self.command:
            raise ValueError("local run command cannot be empty")
        return self


class GenerationKind(StrEnum):
    ANALYSIS = "analysis"
    COVERAGE = "coverage"
    PROFILE = "profile"


class GenerationArtifact(StrictModel):
    name: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: StrictInt = Field(ge=0)


class GenerationManifest(StrictModel):
    """Host-local manifest for one immutable workspace generation."""

    schema_name: Literal["code-analysis-generation"] = GENERATION_SCHEMA_NAME
    schema_version: Literal["1.0.0"] = GENERATION_SCHEMA_VERSION
    generation_id: StrictStr = Field(pattern=r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{12}$")
    kind: GenerationKind
    created_at: StrictStr
    process_id: StrictInt = Field(gt=0)
    process_start_time: StrictFloat | None = Field(default=None, gt=0, allow_inf_nan=False)
    project_root: StrictStr
    workspace_path: StrictStr
    command: tuple[StrictStr, ...]
    interpreter_path: StrictStr
    portable_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[GenerationArtifact, ...]

    @model_validator(mode="after")
    def validate_manifest(self) -> GenerationManifest:
        parsed = datetime.fromisoformat(self.created_at)
        if parsed.tzinfo is None:
            raise ValueError("generation timestamp must include a timezone")
        if not Path(self.project_root).is_absolute() or not Path(self.workspace_path).is_absolute():
            raise ValueError("generation local paths must be absolute")
        if not self.command:
            raise ValueError("generation command cannot be empty")
        names = [item.name for item in self.artifacts]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("generation artifacts must be unique and sorted")
        return self


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    root: Path
    marker: Path
    reports: Path
    runs: Path
    generations: Path
    pins: Path
    cache: Path
    decisions: Path

    def kind_directory(self, kind: GenerationKind) -> Path:
        return self.generations / kind.value


def prepare_workspace(
    context: AnalysisContext,
    *,
    explicit_path: Path | None = None,
) -> WorkspacePaths:
    """Create or validate the workspace before analysis begins."""
    project_root = context.source.physical_root.resolve()
    root = (
        explicit_path.expanduser().absolute()
        if explicit_path is not None
        else project_root / WORKSPACE_DIRECTORY
    )
    if root == project_root:
        raise OSError(errno.EINVAL, "workspace cannot be the project root", str(root))
    if explicit_path is None and root.parent != project_root:
        raise OSError(errno.EPERM, "default workspace escaped the project root", str(root))
    _reject_symlink(root)
    marker = _expected_marker(context, root)
    paths = _validate_existing(root, marker) if root.exists() else _initialize(root, marker)
    _ensure_owned_directories(paths)
    return paths


def open_workspace(root: Path) -> WorkspacePaths:
    """Open an existing owned workspace without analyzing or claiming a project."""
    resolved = root.expanduser().absolute()
    paths = _paths(resolved)
    validate_workspace_ownership(paths)
    return paths


def create_run_record(
    context: AnalysisContext,
    workspace: WorkspacePaths,
    *,
    started_at: datetime,
    duration_ms: int,
    command: tuple[str, ...],
    providers: tuple[ProviderAvailability, ...],
    portable_report_sha256: str,
    interpreter_path: str,
    cache: CacheStatistics | None = None,
) -> LocalRunRecord:
    started_at_text = started_at.astimezone(UTC).isoformat()
    project_root = str(context.source.physical_root)
    workspace_path = str(workspace.root)
    target_path = str(context.target)
    payload = {
        "started_at": started_at_text,
        "project_root": project_root,
        "workspace_path": workspace_path,
        "target_path": target_path,
        "command": command,
        "storage_configuration_sha256": context.config.workspace.identity(),
        "portable_report_sha256": portable_report_sha256,
    }
    invocation_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return LocalRunRecord(
        invocation_id=invocation_id,
        started_at=started_at_text,
        duration_ms=duration_ms,
        project_root=project_root,
        workspace_path=workspace_path,
        target_path=target_path,
        interpreter_path=interpreter_path,
        command=command,
        storage_configuration_sha256=context.config.workspace.identity(),
        providers=providers,
        cache=cache or CacheStatistics(),
        portable_report_sha256=portable_report_sha256,
    )


def write_workspace_results(
    workspace: WorkspacePaths,
    *,
    report_json: str,
    report_markdown: str,
    run: LocalRunRecord,
) -> GenerationManifest:
    """Commit an immutable analysis generation and atomically advance its alias."""
    validate_workspace_ownership(workspace)
    run_json = (
        json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    manifest = commit_generation(
        workspace,
        kind=GenerationKind.ANALYSIS,
        artifacts={
            "report.json": report_json.encode(),
            "report.md": report_markdown.encode(),
            "run.json": run_json.encode(),
        },
        command=run.command,
        interpreter_path=run.interpreter_path,
        portable_identity_sha256=run.portable_report_sha256,
    )
    for superseded_path in (
        workspace.reports / "latest.json",
        workspace.reports / "latest.md",
        workspace.runs / "latest.json",
    ):
        superseded_path.unlink(missing_ok=True)
    return manifest


def commit_generation(
    workspace: WorkspacePaths,
    *,
    kind: GenerationKind,
    artifacts: dict[str, bytes],
    command: tuple[str, ...],
    interpreter_path: str,
    portable_identity_sha256: str,
    created_at: datetime | None = None,
) -> GenerationManifest:
    """Assemble, validate, and atomically publish a workspace generation."""
    marker = validate_workspace_ownership(workspace)
    kind_directory = workspace.kind_directory(kind)
    _validate_kind_directory(workspace, kind_directory)
    instant = (created_at or datetime.now(UTC)).astimezone(UTC)
    prefix = instant.strftime("%Y%m%dT%H%M%S.%fZ")
    generation_id = f"{prefix}-{uuid4().hex[:12]}"
    temporary = kind_directory / f".{generation_id}.tmp-{os.getpid()}"
    destination = kind_directory / generation_id
    if not artifacts:
        raise OSError(errno.ENODATA, "generation must contain at least one artifact")
    artifact_records = tuple(
        GenerationArtifact(
            name=name, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload)
        )
        for name, payload in sorted(artifacts.items())
    )
    manifest = GenerationManifest(
        generation_id=generation_id,
        kind=kind,
        created_at=instant.isoformat(),
        process_id=os.getpid(),
        process_start_time=_current_process_start(),
        project_root=marker.project_root,
        workspace_path=str(workspace.root.resolve()),
        command=command,
        interpreter_path=interpreter_path,
        portable_identity_sha256=portable_identity_sha256,
        artifacts=artifact_records,
    )
    try:
        temporary.mkdir(mode=0o700)
        for name, payload in sorted(artifacts.items()):
            if Path(name).name != name or name in {"manifest.json", ".", ".."}:
                raise OSError(errno.EINVAL, "invalid generation artifact name", name)
            _atomic_write_bytes(temporary / name, payload)
        manifest_text = (
            json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
        _atomic_write(temporary / "manifest.json", manifest_text)
        _validate_generation_directory(workspace, temporary, expected=manifest, temporary=True)
        os.replace(temporary, destination)
        _replace_latest_alias(kind_directory, generation_id)
    finally:
        if temporary.exists() and temporary.is_dir() and not temporary.is_symlink():
            shutil.rmtree(temporary)
    prune_generations(workspace, kind=kind)
    return manifest


def list_generations(
    workspace: WorkspacePaths, *, kind: GenerationKind | None = None
) -> tuple[GenerationManifest, ...]:
    validate_workspace_ownership(workspace)
    kinds = (kind,) if kind is not None else tuple(GenerationKind)
    records: list[GenerationManifest] = []
    for selected in kinds:
        directory = workspace.kind_directory(selected)
        _validate_kind_directory(workspace, directory)
        for child in sorted(directory.iterdir()):
            if child.name == "latest" or child.name.startswith("."):
                continue
            records.append(_validate_generation_directory(workspace, child))
    return tuple(sorted(records, key=lambda item: (item.kind.value, item.generation_id)))


def read_generation(
    workspace: WorkspacePaths, kind: GenerationKind, generation_id: str
) -> GenerationManifest:
    """Validate one exact retained generation; never interpret a latest alias."""
    validate_workspace_ownership(workspace)
    if re.fullmatch(r"[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{12}", generation_id) is None:
        raise OSError(errno.EINVAL, "invalid exact generation identity")
    directory = workspace.kind_directory(kind)
    _validate_kind_directory(workspace, directory)
    return _validate_generation_directory(workspace, directory / generation_id)


def set_generation_pin(workspace: WorkspacePaths, generation_id: str, *, pinned: bool) -> None:
    manifest = _find_generation(workspace, generation_id)
    pin = workspace.pins / generation_id
    _reject_symlink(pin)
    if pinned:
        _atomic_write(pin, f"{manifest.kind.value}\n")
    else:
        pin.unlink(missing_ok=True)


def prune_generations(
    workspace: WorkspacePaths,
    *,
    kind: GenerationKind | None = None,
    keep_older: int | None = None,
    max_unpinned_bytes: int = 4 * 1024**3,
    dry_run: bool = False,
) -> tuple[str, ...]:
    marker = validate_workspace_ownership(workspace)
    del marker
    keep = keep_older if keep_older is not None else 5
    if keep < 0:
        raise OSError(errno.EINVAL, "keep-older cannot be negative")
    if max_unpinned_bytes < 0:
        raise OSError(errno.EINVAL, "generation byte budget cannot be negative")
    removed: list[str] = []
    for selected in ((kind,) if kind is not None else tuple(GenerationKind)):
        records = list(list_generations(workspace, kind=selected))
        newest_first = sorted(records, key=lambda item: item.generation_id, reverse=True)
        retained_unpinned: list[GenerationManifest] = []
        for record in newest_first:
            if (workspace.pins / record.generation_id).is_file():
                continue
            if len(retained_unpinned) <= keep:
                retained_unpinned.append(record)
                continue
            target = workspace.kind_directory(selected) / record.generation_id
            _validate_generation_directory(workspace, target, expected=record)
            removed.append(record.generation_id)
            if not dry_run:
                shutil.rmtree(target)
        retained_bytes = sum(_generation_size(workspace, record) for record in retained_unpinned)
        for record in reversed(retained_unpinned[1:]):
            if retained_bytes <= max_unpinned_bytes:
                break
            target = workspace.kind_directory(selected) / record.generation_id
            retained_bytes -= _generation_size(workspace, record)
            removed.append(record.generation_id)
            if not dry_run:
                _validate_generation_directory(workspace, target, expected=record)
                shutil.rmtree(target)
    return tuple(sorted(removed))


def validate_workspace_ownership(workspace: WorkspacePaths) -> WorkspaceMarker:
    """Validate the marker and resolved child paths before cache mutation."""
    _validate_workspace_paths(workspace)
    try:
        marker = WorkspaceMarker.model_validate_json(_read_owned_metadata(workspace.marker))
    except OSError:
        raise
    except ValueError as exc:
        raise OSError(
            errno.EBADMSG, "workspace marker is malformed", str(workspace.marker)
        ) from exc
    if marker.cache_layout_version != CACHE_LAYOUT_VERSION:
        raise OSError(errno.EBADMSG, "workspace cache layout is unsupported", str(workspace.marker))
    return marker


def _expected_marker(context: AnalysisContext, root: Path) -> WorkspaceMarker:
    project_root = str(context.source.physical_root.resolve())
    payload = {
        "project_root": project_root,
        "logical_root": context.source.logical_root,
        "source_roots": context.source.logical_source_roots,
    }
    identity = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return WorkspaceMarker(
        target_identity_sha256=identity,
        project_root=project_root,
        logical_root=context.source.logical_root,
        source_roots=context.source.logical_source_roots,
        created_by_version=__version__,
    )


def _initialize(root: Path, marker: WorkspaceMarker) -> WorkspacePaths:
    parent = root.parent
    if not parent.is_dir():
        raise FileNotFoundError(errno.ENOENT, "workspace parent does not exist", str(parent))
    temporary = Path(tempfile.mkdtemp(prefix=f".{root.name}.init-", dir=parent))
    try:
        (temporary / "reports").mkdir()
        (temporary / "runs").mkdir()
        for kind in GenerationKind:
            (temporary / "generations" / kind.value).mkdir(parents=True, exist_ok=True)
        (temporary / "pins").mkdir()
        (temporary / "cache" / CACHE_LAYOUT_VERSION).mkdir(parents=True)
        _atomic_write(temporary / ".gitignore", WORKSPACE_IGNORE)
        _atomic_write(
            temporary / "workspace.json",
            json.dumps(marker.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        )
        os.replace(temporary, root)
    except FileExistsError:
        if root.exists():
            return _validate_existing(root, marker)
        raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return _paths(root)


def _validate_existing(root: Path, expected: WorkspaceMarker) -> WorkspacePaths:
    if not root.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "workspace path is not a directory", str(root))
    marker_path = root / "workspace.json"
    try:
        actual = WorkspaceMarker.model_validate_json(_read_owned_metadata(marker_path))
    except FileNotFoundError as exc:
        raise OSError(errno.EBADMSG, "workspace marker is missing", str(marker_path)) from exc
    except OSError:
        raise
    except ValueError as exc:
        raise OSError(errno.EBADMSG, "workspace marker is malformed", str(marker_path)) from exc
    if (
        actual.target_identity_sha256 != expected.target_identity_sha256
        or actual.project_root != expected.project_root
        or actual.logical_root != expected.logical_root
        or actual.source_roots != expected.source_roots
        or actual.cache_layout_version != expected.cache_layout_version
    ):
        raise OSError(
            errno.EBADMSG, "workspace marker does not match this project", str(marker_path)
        )
    ignore = root / ".gitignore"
    try:
        if ignore.read_text(encoding="utf-8") != WORKSPACE_IGNORE:
            raise OSError(errno.EBADMSG, "workspace ignore policy is invalid", str(ignore))
    except FileNotFoundError as exc:
        raise OSError(errno.EBADMSG, "workspace ignore policy is missing", str(ignore)) from exc
    return _paths(root)


def _paths(root: Path) -> WorkspacePaths:
    return WorkspacePaths(
        root=root,
        marker=root / "workspace.json",
        reports=root / "reports",
        runs=root / "runs",
        generations=root / "generations",
        pins=root / "pins",
        cache=root / "cache" / CACHE_LAYOUT_VERSION,
        decisions=root / "decisions",
    )


def _ensure_owned_directories(paths: WorkspacePaths) -> None:
    _validate_workspace_paths(paths, require_children=False)
    for directory in (
        paths.reports,
        paths.runs,
        paths.generations,
        paths.pins,
        paths.cache,
        paths.decisions,
        *(paths.kind_directory(kind) for kind in GenerationKind),
    ):
        _reject_symlink(directory)
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise NotADirectoryError(
                errno.ENOTDIR, "workspace child is not a directory", str(directory)
            )


def _validate_workspace_paths(paths: WorkspacePaths, *, require_children: bool = True) -> None:
    _reject_symlink(paths.root)
    if not paths.root.is_dir():
        raise NotADirectoryError(
            errno.ENOTDIR, "workspace path is not a directory", str(paths.root)
        )
    expected = paths.root.resolve()
    for child in (
        paths.marker,
        paths.reports,
        paths.runs,
        paths.generations,
        paths.pins,
        paths.cache,
        paths.decisions,
        *(paths.kind_directory(kind) for kind in GenerationKind),
    ):
        resolved = child.resolve(strict=False)
        if expected not in resolved.parents:
            raise OSError(errno.EPERM, "workspace child escaped its root", str(child))
        _reject_symlink(child)
        if require_children and child != paths.marker and not child.is_dir():
            raise NotADirectoryError(
                errno.ENOTDIR, "workspace child is not a directory", str(child)
            )


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise OSError(errno.ELOOP, "symlinked workspace paths are not allowed", str(path))


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_kind_directory(workspace: WorkspacePaths, directory: Path) -> None:
    if directory.parent != workspace.generations or directory.name not in {
        kind.value for kind in GenerationKind
    }:
        raise OSError(errno.EPERM, "invalid generation kind directory", str(directory))
    _reject_symlink(directory)
    if not directory.is_dir():
        raise NotADirectoryError(
            errno.ENOTDIR, "generation kind is not a directory", str(directory)
        )


def _validate_generation_directory(
    workspace: WorkspacePaths,
    directory: Path,
    *,
    expected: GenerationManifest | None = None,
    temporary: bool = False,
) -> GenerationManifest:
    if directory.is_symlink() or directory.parent.parent != workspace.generations:
        raise OSError(errno.ELOOP, "generation must be a direct non-symlink child", str(directory))
    if not directory.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "generation is not a directory", str(directory))
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        manifest = _read_generation_manifest(fd)
        _validate_generation_contents(fd, manifest)
    finally:
        os.close(fd)
    if expected is not None and manifest != expected:
        raise OSError(
            errno.EBADMSG, "generation manifest changed during validation", str(directory)
        )
    if not temporary and directory.name != manifest.generation_id:
        raise OSError(errno.EBADMSG, "generation directory identity mismatch", str(directory))
    if directory.parent.name != manifest.kind.value:
        raise OSError(errno.EBADMSG, "generation kind directory mismatch", str(directory))
    if manifest.workspace_path != str(workspace.root.resolve()):
        raise OSError(errno.EBADMSG, "generation workspace identity mismatch", str(directory))
    return manifest


def _read_generation_manifest(directory_fd: int) -> GenerationManifest:
    try:
        return GenerationManifest.model_validate_json(
            _read_owned_metadata("manifest.json", directory_fd=directory_fd)
        )
    except (OSError, ValueError) as exc:
        raise OSError(errno.EBADMSG, "generation manifest is malformed or unsafe") from exc


def _current_process_start() -> float | None:
    """Optional local provenance; historical/unavailable identity is not invented."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        return psutil.Process(os.getpid()).create_time()
    except psutil.Error:
        return None


def _read_owned_metadata(name: str | Path, *, directory_fd: int | None = None) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise OSError(errno.EBADMSG, "owned metadata is not a regular file")
        payload = source.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise OSError(errno.EBADMSG, "owned metadata exceeds byte bound")
    return payload


def _validate_generation_contents(directory_fd: int, manifest: GenerationManifest) -> None:
    actual_names = sorted(name for name in os.listdir(directory_fd) if name != "manifest.json")
    expected_names = [artifact.name for artifact in manifest.artifacts]
    if actual_names != expected_names:
        raise OSError(errno.EBADMSG, "generation artifact set mismatch")
    for artifact in manifest.artifacts:
        fd = os.open(
            artifact.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise OSError(errno.ELOOP, "generation artifact must be a regular file")
            digest = hashlib.sha256()
            size = 0
            while payload := source.read(min(65536, artifact.bytes - size + 1)):
                size += len(payload)
                if size > artifact.bytes:
                    raise OSError(errno.EBADMSG, "generation artifact length mismatch")
                digest.update(payload)
        if size != artifact.bytes or digest.hexdigest() != artifact.sha256:
            raise OSError(errno.EBADMSG, "generation artifact hash mismatch")


def _replace_latest_alias(kind_directory: Path, generation_id: str) -> None:
    alias = kind_directory / "latest"
    temporary = kind_directory / f".latest-{uuid4().hex}.tmp"
    try:
        temporary.symlink_to(generation_id, target_is_directory=True)
        os.replace(temporary, alias)
    finally:
        temporary.unlink(missing_ok=True)


def _find_generation(workspace: WorkspacePaths, generation_id: str) -> GenerationManifest:
    matches: list[GenerationManifest] = []
    for kind in GenerationKind:
        candidate = workspace.kind_directory(kind) / generation_id
        if candidate.exists() and not candidate.is_symlink():
            matches.append(_validate_generation_directory(workspace, candidate))
    if len(matches) != 1:
        raise FileNotFoundError(errno.ENOENT, "generation identity was not found", generation_id)
    return matches[0]


def _generation_size(workspace: WorkspacePaths, manifest: GenerationManifest) -> int:
    directory = workspace.kind_directory(manifest.kind) / manifest.generation_id
    manifest_path = directory / "manifest.json"
    return sum(item.bytes for item in manifest.artifacts) + manifest_path.stat().st_size
