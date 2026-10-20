"""Explicit session source connections and bounded, non-analytical project documents."""

from __future__ import annotations

import errno
import hashlib
import os
import re
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from code_analysis.collection_commands import CollectionPlanner
from code_analysis.models import StrictModel
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json
from code_analysis.ui_source import _open_confined, _read_confined, _source_path, preview_source

_MAX_DOCUMENT_BYTES = 128 * 1024
_MAX_DOCUMENTS = 50
_DOCUMENT_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")
_TEXT_SUFFIXES = {".md", ".markdown", ".rst", ".txt"}
_LINK = re.compile(r"\[[^\]\n]{0,512}\]\(([^\s)]+)(?:\s+[^)\n]*)?\)")


class SourceConnectionRequest(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    directory: StrictStr = Field(min_length=1, max_length=2048)
    logical_path: StrictStr = Field(default="", max_length=4096)
    run: StrictStr = Field(default="current", min_length=1, max_length=256)
    compare_run: StrictStr = Field(default="none", min_length=1, max_length=256)
    confirmed: StrictBool = False


class SourceConnection(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    configured: StrictBool
    can_connect: StrictBool
    source_root: StrictStr | None
    collection_root: StrictStr | None
    root_identity: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    message: StrictStr = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.configured != (self.source_root is not None and self.root_identity is not None):
            raise ValueError("connected source requires its root and directory identity")
        if not self.configured and (self.source_root is not None or self.root_identity is not None):
            raise ValueError("unconfigured source cannot carry a root")
        if self.can_connect != (self.collection_root is not None):
            raise ValueError("source connection requires a configured collection boundary")
        return self


class ProjectDocumentLink(StrictModel):
    path: StrictStr = Field(min_length=1, max_length=4096)
    label: StrictStr = Field(min_length=1, max_length=4096)
    sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: StrictInt = Field(ge=0, le=_MAX_DOCUMENT_BYTES)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        _document_path(value)
        return value


class ProjectDocumentCatalog(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    root_identity: StrictStr | None = Field(pattern=r"^[a-f0-9]{64}$")
    state: Literal["available", "disabled"]
    records: tuple[ProjectDocumentLink, ...] = Field(max_length=_MAX_DOCUMENTS)
    omitted: StrictInt = Field(default=0, ge=0)
    limitations: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.state == "disabled" and (self.root_identity or self.records or self.omitted):
            raise ValueError("disabled documents cannot expose a root or records")
        if self.state == "available" and self.root_identity is None:
            raise ValueError("available documents require a connected directory identity")
        if len({record.path for record in self.records}) != len(self.records):
            raise ValueError("duplicate project-document paths")
        return self


class ProjectDocument(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    root_identity: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    path: StrictStr
    sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    text: StrictStr = Field(max_length=_MAX_DOCUMENT_BYTES)
    links: tuple[ProjectDocumentLink, ...] = Field(max_length=_MAX_DOCUMENTS)
    omitted_links: StrictInt = Field(default=0, ge=0)
    limitations: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def validate_text(self) -> Self:
        _document_path(self.path)
        payload = self.text.encode("utf-8")
        if len(payload) > _MAX_DOCUMENT_BYTES or hashlib.sha256(payload).hexdigest() != self.sha256:
            raise ValueError("project document must match its bounded original UTF-8 bytes")
        if len({link.path for link in self.links}) != len(self.links):
            raise ValueError("duplicate project-document links")
        return self


class ProjectDocumentAccess:
    """Bounded transient grants; callers hold the server lifecycle lock."""

    def __init__(self) -> None:
        self._grants: OrderedDict[tuple[str, str, str], str] = OrderedDict()

    def allow(
        self, report_sha256: str, root: str | None, links: tuple[ProjectDocumentLink, ...]
    ) -> None:
        if root is None:
            return
        for link in links:
            key = (report_sha256, root, link.path)
            self._grants[key] = link.sha256
            self._grants.move_to_end(key)
            while len(self._grants) > 500:
                self._grants.popitem(last=False)

    def require(self, report_sha256: str, root: str, path: str, sha256: str) -> None:
        key = (report_sha256, root, path)
        if self._grants.get(key) != sha256:
            raise OSError(
                errno.EPERM, "open a listed README or follow its offered local links first"
            )
        self._grants.move_to_end(key)


def report_identity(report: AnalysisReport) -> str:
    return hashlib.sha256(canonical_json(report).encode()).hexdigest()


def root_identity(root: Path) -> str:
    info = root.stat(follow_symlinks=False)
    return hashlib.sha256(f"{root}\n{info.st_dev}\n{info.st_ino}".encode()).hexdigest()


def source_connection(
    report: AnalysisReport, root: Path | None, planner: CollectionPlanner | None
) -> SourceConnection:
    return SourceConnection(
        report_sha256=report_identity(report),
        configured=root is not None,
        can_connect=planner is not None,
        source_root=str(root) if root else None,
        collection_root=str(planner.root) if planner else None,
        root_identity=root_identity(root) if root else None,
        message=(
            "Source folder connected. Each file is still checked against the selected report "
            "before display."
            if root
            else "Source files are not connected. Choose the project folder containing the "
            "report-relative source paths. Saved relationships remain available."
        ),
    )


def validate_source_connection(
    report: AnalysisReport, planner: CollectionPlanner, request: SourceConnectionRequest
) -> Path:
    if not request.confirmed:
        raise OSError(errno.EPERM, "Confirm read-only source and project-document access first.")
    if request.report_sha256 != report_identity(report):
        raise ValueError("source connection belongs to another saved report")
    if not report.source_files:
        raise OSError(errno.ENODATA, "The selected report has no recorded source files.")
    root = planner._path(request.directory, directory=True)
    logical_path = request.logical_path or report.source_files[0].logical_path
    preview = preview_source(report, root, logical_path, start=1, end=1)
    if preview.state != "bound":
        raise OSError(
            errno.ENODATA,
            f"Cannot connect this folder: {logical_path}: {preview.message} "
            "Choose its project root, not its src subdirectory.",
        )
    return root


def _document_path(value: str) -> PurePosixPath:
    path = _source_path(value)
    if any(part.startswith(".") or part.casefold() == "private" for part in path.parts):
        raise ValueError("hidden and private project documents are not browsable")
    if path.suffix.casefold() not in _TEXT_SUFFIXES and path.name.casefold() != "readme":
        raise ValueError(
            "only Markdown, reStructuredText and plain-text project documents are browsable"
        )
    return path


def _document_bytes(root: Path, value: str) -> bytes:
    path = _document_path(value)
    payload = _read_confined(root, path.parts, _MAX_DOCUMENT_BYTES)
    if len(payload) > _MAX_DOCUMENT_BYTES:
        raise OSError(errno.ENODATA, "project document exceeds the 128 KiB read bound")
    payload.decode("utf-8")
    return payload


def _document_link(root: Path, value: str) -> ProjectDocumentLink | None:
    try:
        payload = _document_bytes(root, value)
    except (OSError, ValueError, UnicodeError):
        return None
    label = value
    if PurePosixPath(value).name.casefold() == "solutions.md":
        label += " — solutions (spoilers)"
    elif PurePosixPath(value).name.casefold() == "hints.md":
        label += " — optional hints"
    return ProjectDocumentLink(
        path=value, label=label, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload)
    )


def document_catalog(
    report: AnalysisReport, root: Path | None, logical_path: str
) -> ProjectDocumentCatalog:
    digest = report_identity(report)
    if root is None:
        return ProjectDocumentCatalog(
            report_sha256=digest,
            root_identity=None,
            state="disabled",
            records=(),
            limitations=("Connect a source folder to browse its project documents.",),
        )
    if logical_path and logical_path not in {source.logical_path for source in report.source_files}:
        raise ValueError("document context must be a recorded source file")
    parent = _source_path(logical_path).parent if logical_path else PurePosixPath()
    directories = (parent, *parent.parents)
    candidates = _readme_paths(root, directories[:16])
    records = tuple(
        record
        for value in sorted(candidates)
        if (record := _document_link(root, value)) is not None
    )
    return ProjectDocumentCatalog(
        report_sha256=digest,
        root_identity=root_identity(root),
        state="available",
        records=records[:_MAX_DOCUMENTS],
        omitted=max(0, len(records) - _MAX_DOCUMENTS),
        limitations=(
            "Project documents are current read-only text, not collected analytical evidence. "
            "Only READMEs in the closest 16 ancestor directories and explicitly followed "
            "local text links are offered.",
        ),
    )


def _readme_paths(root: Path, directories: tuple[PurePosixPath, ...]) -> set[str]:
    candidates: set[str] = set()
    allowed = {name.casefold() for name in _DOCUMENT_NAMES}
    for directory in directories:
        if any(part.startswith(".") or part.casefold() == "private" for part in directory.parts):
            continue
        descriptor = _open_confined(root, directory.parts, directory=True)
        try:
            with os.scandir(descriptor) as entries:
                for count, entry in enumerate(entries):
                    if count >= 10_000:
                        raise OSError(errno.ENODATA, "README directory exceeds 10,000 entries")
                    if entry.name.casefold() in allowed:
                        candidates.add(str(directory / entry.name))
        finally:
            os.close(descriptor)
    return candidates


def _local_link(document: str, value: str) -> str | None:
    # Resolve literal local links lexically; never interpret schemes, queries or escapes.
    if re.search(r"[:?%\\\x00-\x20]", value) or value.startswith(("/", "#")):
        return None
    parts = list(PurePosixPath(document).parent.parts)
    for part in value.split("#", 1)[0].split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    result = "/".join(parts)
    try:
        _document_path(result)
    except ValueError:
        return None
    return result


def read_project_document(
    report: AnalysisReport, root: Path | None, selected_root: str, path: str, expected_sha256: str
) -> ProjectDocument:
    if root is None:
        raise OSError(errno.EPERM, "Connect a source folder before opening project documents.")
    identity = root_identity(root)
    if selected_root != identity:
        raise ValueError("source folder changed; reopen the project-document list")
    payload = _document_bytes(root, path)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected_sha256:
        raise OSError(
            errno.ENODATA, "Project document changed. Reopen its README or document list."
        )
    text = payload.decode("utf-8")
    targets = sorted(
        {target for match in _LINK.finditer(text) if (target := _local_link(path, match[1]))}
    )
    links = tuple(
        record for value in targets[:_MAX_DOCUMENTS] if (record := _document_link(root, value))
    )
    return ProjectDocument(
        report_sha256=report_identity(report),
        root_identity=identity,
        path=path,
        sha256=digest,
        text=text,
        links=links,
        omitted_links=max(0, len(targets) - _MAX_DOCUMENTS),
        limitations=(
            "Current project document; not saved analysis evidence. Text and HTML-like content "
            "are displayed literally. External links, images, scripts and examples are not "
            "executed or loaded.",
        ),
    )
