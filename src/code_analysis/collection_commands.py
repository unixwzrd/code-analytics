"""Typed, non-executing collection command planning shared by presentation clients."""

from __future__ import annotations

import errno
import hashlib
import os
import platform
import re
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.models import StrictModel

Operation = Literal["scan", "clones", "refactor", "profile", "import-profile"]
Language = Literal["auto", "python", "javascript", "ruby"]
_SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".jsx", ".rb"}


class CollectionRequest(StrictModel):
    operation: Operation = "scan"
    interpreter_id: StrictStr = Field(default="current", min_length=1, max_length=64)
    target: StrictStr = Field(default=".", min_length=1, max_length=2048)
    cwd: StrictStr = Field(default=".", min_length=1, max_length=2048)
    languages: tuple[Language, ...] = ("auto",)
    strictness: Literal["lenient", "normal", "strict"] = "normal"
    exclude: tuple[StrictStr, ...] = Field(default=(), max_length=50)
    workspace: StrictStr | None = Field(default=None, min_length=1, max_length=2048)
    source_report: StrictStr | None = Field(default=None, min_length=1, max_length=2048)
    workload_kind: Literal["script", "module"] = "script"
    workload: StrictStr | None = Field(default=None, min_length=1, max_length=2048)
    arguments: tuple[StrictStr, ...] = Field(default=(), max_length=128)
    timeout: StrictInt = Field(default=300, ge=1, le=86_400)
    repeat: StrictInt = Field(default=1, ge=1, le=20)
    expected_status: StrictStr = "SUCCESS"
    allow_execution: StrictBool = False
    profiles: tuple[StrictStr, ...] = Field(default=(), max_length=100)
    metadata: StrictStr | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if self.operation != "import-profile" and (self.profiles or self.metadata is not None):
            raise ValueError("profile input files apply only to import")
        if self.operation == "import-profile":
            if not self.profiles or len(set(self.profiles)) != len(self.profiles):
                raise ValueError("import requires unique saved profile paths")
            if (
                self.target != "."
                or self.languages != ("auto",)
                or self.exclude
                or self.strictness != "normal"
                or self.workload is not None
                or self.arguments
                or self.allow_execution
                or self.workload_kind != "script"
                or self.timeout != 300
                or self.repeat != 1
                or self.expected_status != "SUCCESS"
            ):
                raise ValueError("static and workload options do not apply to saved-profile import")
            for path in self.profiles:
                _relative(path)
            return self
        if not self.languages or len(set(self.languages)) != len(self.languages):
            raise ValueError("choose unique languages")
        if "auto" in self.languages and len(self.languages) != 1:
            raise ValueError("auto cannot be combined with another language")
        for value in (*self.exclude, *self.arguments):
            if len(value) > 4096 or any(ord(character) < 32 for character in value):
                raise ValueError("arguments and exclusions must be bounded single-line values")
        if any(not value for value in self.exclude):
            raise ValueError("exclusion patterns must not be empty")
        if any(value in {".", ".."} or "/" in value or "\\" in value for value in self.exclude):
            raise ValueError("exclusions are exact directory names, not paths or globs")
        if self.expected_status != "SUCCESS" and self.expected_status not in {
            errno.errorcode[code] for code in errno.errorcode
        }:
            raise ValueError("expected status must be SUCCESS or a host errno symbol")
        if self.operation == "profile":
            if not self.allow_execution:
                raise ValueError("acknowledge runtime execution before generating its command")
            if not self.source_report or not self.workload:
                raise ValueError("profiling requires a saved source report and workload")
            if self.target != "." or self.languages != ("auto",) or self.exclude:
                raise ValueError(
                    "static target/language/exclusion options do not apply to profiling"
                )
            if self.strictness != "normal":
                raise ValueError("static strictness does not apply to profiling")
        elif (
            self.source_report is not None
            or self.workload is not None
            or self.arguments
            or self.allow_execution
            or self.workload_kind != "script"
            or self.timeout != 300
            or self.repeat != 1
            or self.expected_status != "SUCCESS"
        ):
            raise ValueError("runtime options do not apply to static collection")
        return self


class CollectionInterpreter(StrictModel):
    id: StrictStr
    path: StrictStr


@dataclass(frozen=True, slots=True)
class _InterpreterBinding:
    choice: CollectionInterpreter
    device: int
    inode: int
    size: int
    modified_ns: int


def _bind_interpreter(path: Path, identity: str) -> _InterpreterBinding:
    # Do not resolve symlinks: a venv launcher may share the base Python binary.
    value = str(path)
    if not path.is_absolute() or ".." in path.parts or any(ord(c) < 32 for c in value):
        raise ValueError("trusted collection interpreters require absolute paths")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise ValueError("trusted collection interpreter must be an executable regular file")
    return _InterpreterBinding(
        CollectionInterpreter(id=identity, path=value),
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
    )


class CollectionContext(StrictModel):
    enabled: StrictBool
    root: StrictStr | None
    host: StrictStr
    interpreter: StrictStr
    interpreters: tuple[CollectionInterpreter, ...] = ()
    execution_available: StrictBool = False
    report_available: StrictBool
    runtime_available: StrictBool = False


class CollectionEntry(StrictModel):
    name: StrictStr
    path: StrictStr
    kind: Literal["directory", "source", "report", "profile"]


class CollectionDirectory(StrictModel):
    path: StrictStr
    parent: StrictStr | None
    entries: tuple[CollectionEntry, ...]
    omitted: StrictInt = Field(ge=0)
    skipped: StrictInt = Field(ge=0)


class CollectionCommand(StrictModel):
    operation: Operation
    argv: tuple[StrictStr, ...]
    cwd: StrictStr
    command: StrictStr
    interpreter: StrictStr
    host: StrictStr
    execution_available: Literal[False] = False
    required_inputs: tuple[StrictStr, ...]
    limitations: tuple[StrictStr, ...]


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 2048
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or path.as_posix() != value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("choose a normalized relative path within the collection root")
    return path


class CollectionPlanner:
    """Read-only root capability; no scanning, importing, writing or subprocesses."""

    def __init__(self, root: Path, *, interpreters: tuple[Path, ...] = ()):
        if len(interpreters) > 32:
            raise ValueError("at most 32 additional trusted interpreters may be configured")
        self._interpreters = {"current": _bind_interpreter(Path(sys.executable), "current")}
        seen = {sys.executable}
        for path in interpreters:
            if str(path) in seen:
                continue
            identity = "python-" + hashlib.sha256(str(path).encode()).hexdigest()[:16]
            self._interpreters[identity] = _bind_interpreter(path, identity)
            seen.add(str(path))
        self.root = root.expanduser().resolve(strict=True)
        self._root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            os.close(self._root_fd)
            self._closed = True

    def _open(self, relative: PurePosixPath, *, directory: bool = False) -> int:
        fd = os.dup(self._root_fd)
        try:
            for index, part in enumerate(relative.parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(relative.parts) - 1 or directory:
                    flags |= os.O_DIRECTORY
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            mode = os.fstat(fd).st_mode
            if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                raise ValueError("only regular files and directories may be selected")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _path(self, value: str, *, directory: bool | None = None) -> Path:
        relative = _relative(value)
        fd = self._open(relative, directory=directory is True)
        try:
            if directory is False and not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("select a regular file")
        finally:
            os.close(fd)
        return self.root / relative

    def context(self, *, report_available: bool) -> CollectionContext:
        return CollectionContext(
            enabled=True,
            root=str(self.root),
            host=platform.node(),
            interpreter=sys.executable,
            interpreters=tuple(binding.choice for binding in self._interpreters.values()),
            report_available=report_available,
        )

    def read_input(self, value: str, *, max_bytes: int) -> bytes:
        """Read bounded regular input through the same confined directory capability."""
        fd = self._open(_relative(value))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("select a regular input file")
            with os.fdopen(os.dup(fd), "rb") as source:
                payload = source.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise OSError(errno.ENODATA, "collection input exceeds its byte bound")
            return payload
        finally:
            os.close(fd)

    def browse(self, value: str) -> CollectionDirectory:
        relative = _relative(value)
        fd = self._open(relative, directory=True)
        entries: list[CollectionEntry] = []
        skipped = 0
        try:
            with os.scandir(fd) as items:
                for count, item in enumerate(items):
                    if count >= 10_000:
                        raise ValueError(
                            "directory exceeds the browse bound; enter a narrower path"
                        )
                    try:
                        path = _relative((relative / item.name).as_posix())
                        mode = item.stat(follow_symlinks=False).st_mode
                        if item.name in {".git", ".venv", "node_modules", "__pycache__"}:
                            skipped += 1
                            continue
                        kind: Literal["directory", "source", "report", "profile"]
                        if stat.S_ISDIR(mode):
                            kind = "directory"
                        elif stat.S_ISREG(mode) and path.suffix in _SOURCE_SUFFIXES:
                            kind = "source"
                        elif stat.S_ISREG(mode) and path.suffix == ".json":
                            kind = "report"
                        elif stat.S_ISREG(mode) and path.suffix in {".prof", ".pstats"}:
                            kind = "profile"
                        else:
                            skipped += 1
                            continue
                        entries.append(CollectionEntry(name=item.name, path=str(path), kind=kind))
                    except (OSError, ValueError):
                        skipped += 1
        finally:
            os.close(fd)
        entries.sort(key=lambda item: (item.kind != "directory", item.name.casefold(), item.name))
        return CollectionDirectory(
            path=str(relative),
            parent=str(relative.parent) if relative.parts else None,
            entries=tuple(entries[:200]),
            omitted=max(0, len(entries) - 200),
            skipped=skipped,
        )

    def preview(self, request: CollectionRequest) -> CollectionCommand:
        binding = self._interpreters.get(request.interpreter_id)
        if binding is None:
            raise ValueError("select an interpreter explicitly trusted at startup")
        if _bind_interpreter(Path(binding.choice.path), binding.choice.id) != binding:
            raise ValueError("trusted interpreter changed; restart to review its identity")
        interpreter = binding.choice.path
        cwd = self._path(request.cwd, directory=True)
        # The selected project's cwd/PYTHONPATH must never shadow the analyzer.
        argv = [interpreter, "-I", "-m", "code_analysis", request.operation]
        required: list[str] = []
        workload_tokens: tuple[str, ...] = ()
        if request.operation == "import-profile":
            argv[-1:] = ["runtime", "ingest"]
            for value in request.profiles:
                path = self._path(value, directory=False)
                required.append(str(path))
                argv.extend(("--profile", str(path)))
            for flag, optional_value in (
                ("--metadata", request.metadata),
                ("--source-report", request.source_report),
            ):
                if optional_value is not None:
                    path = self._path(optional_value, directory=False)
                    required.append(str(path))
                    argv.extend((flag, str(path)))
        elif request.operation == "profile":
            workload_tokens = self._profile_arguments(request, argv, required, cwd)
        else:
            target = self._path(request.target)
            if target.is_file() and target.suffix not in _SOURCE_SUFFIXES:
                raise ValueError("select a supported source file or source directory")
            required.append(str(target))
            argv.extend((str(target), "--strictness", request.strictness))
            for language in request.languages:
                argv.extend(("--language", language))
            argv.extend(f"--exclude-dir={name}" for name in request.exclude)
        if request.workspace is not None:
            relative = _relative(request.workspace)
            # A new workspace leaf is allowed; its parent must already be confined.
            self._path(str(relative.parent), directory=True)
            path = self.root / relative
            if path.exists() or path.is_symlink():
                self._path(request.workspace, directory=True)
            argv.extend(("--workspace", str(path)))
        if request.operation != "import-profile":
            argv.append("--json-compact")
        if workload_tokens:
            argv.extend(("--", *workload_tokens, *request.arguments))
        return CollectionCommand(
            operation=request.operation,
            argv=tuple(argv),
            cwd=str(cwd),
            command=f"cd {shlex.quote(str(cwd))} && {shlex.join(argv)}",
            interpreter=interpreter,
            host=platform.node(),
            required_inputs=tuple(required),
            limitations=(
                "Preview only: no analysis, workload, or filesystem write has occurred.",
                "Paths are checked now, not content-bound; the CLI revalidates inputs when run.",
                "The command uses the selected trusted server interpreter and filesystem.",
                "Interpreter trust is not package qualification; the selected environment "
                "must already contain Code Analysis and the required optional providers.",
                (
                    "Imports require an existing owned workspace for managed retention; "
                    "historical capture provenance is not supplied by the import job."
                    if request.operation == "import-profile"
                    else "CLI workspace creation/retention applies when run; "
                    "choose an external workspace for read-only targets."
                ),
            )
            + (
                (
                    "cProfile capture is scoped; worker threads, subprocesses and device work "
                    "are not whole-program measurements.",
                    "User-supplied arguments may contain secrets; "
                    "review before copying or sharing.",
                )
                if request.operation == "profile"
                else ()
            ),
        )

    def _profile_arguments(
        self, request: CollectionRequest, argv: list[str], required: list[str], cwd: Path
    ) -> tuple[str, ...]:
        assert request.source_report is not None and request.workload is not None
        report = self._path(request.source_report, directory=False)
        required.append(str(report))
        argv.extend(("--source-report", str(report), "--allow-execution", "--cwd", str(cwd)))
        argv.extend(("--timeout", str(request.timeout), "--repeat", str(request.repeat)))
        argv.extend(("--expected-status", request.expected_status))
        if request.workload_kind == "module":
            if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", request.workload):
                raise ValueError("select a dotted Python module name")
            return (argv[0], "-m", request.workload)
        workload = self._path(request.workload, directory=False)
        if workload.suffix != ".py":
            raise ValueError("select a Python script ending in .py")
        required.append(str(workload))
        return (argv[0], str(workload))
