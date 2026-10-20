"""Explicitly opted-in, confined and hash-bound read-only source previews."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.models import StrictModel
from code_analysis.report_models import AnalysisReport, SourceFileRecord

_MAX_BYTES = 4 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_DOCUMENT_LINES = 20_000
type PreviewState = Literal[
    "bound", "disabled", "unavailable", "mismatch", "too-large", "invalid-encoding"
]


class SourcePreviewIdentity(StrictModel):
    """A saved path/hash binding without inventing a source parser record."""

    logical_path: StrictStr = Field(min_length=1, max_length=4096)
    source_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")


class SourceLine(StrictModel):
    number: StrictInt = Field(ge=1)
    text: StrictStr


class SourcePreview(StrictModel):
    logical_path: StrictStr
    expected_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    state: PreviewState
    message: StrictStr
    lines: tuple[SourceLine, ...] = Field(default=(), max_length=200)
    next_line: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_lines(self) -> SourcePreview:
        if self.state != "bound" and (self.lines or self.next_line is not None):
            raise ValueError("unbound previews cannot disclose source")
        numbers = tuple(item.number for item in self.lines)
        if numbers and numbers != tuple(range(numbers[0], numbers[0] + len(numbers))):
            raise ValueError("preview lines must be consecutive")
        if self.next_line is not None and (not numbers or self.next_line != numbers[-1] + 1):
            raise ValueError("preview continuation must follow retained lines")
        return self


class SourceBrowsePreview(StrictModel):
    """Bind transient verified text to its originating report and UI selection."""

    report_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    revision: StrictInt = Field(ge=0)
    preview: SourcePreview


type NewlineStyle = Literal["none", "lf", "crlf", "mixed"]


def _document_newlines(text: str) -> NewlineStyle:
    breaks = set(re.findall(r"\r\n|[\r\n]", text))
    if not breaks:
        return "none"
    if breaks == {"\n"}:
        return "lf"
    if breaks == {"\r\n"}:
        return "crlf"
    return "mixed"


class SourceDocument(StrictModel):
    """Transient verified original bytes for explicit full-file draft creation."""

    logical_path: StrictStr
    expected_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    state: PreviewState
    message: StrictStr
    text: StrictStr | None = None
    editable: StrictBool = False
    newline_style: NewlineStyle | None = None
    has_final_newline: StrictBool | None = None
    line_count: StrictInt | None = Field(default=None, ge=0, le=_MAX_DOCUMENT_LINES)
    byte_count: StrictInt | None = Field(default=None, ge=0, le=_MAX_DOCUMENT_BYTES)

    @model_validator(mode="after")
    def validate_content(self) -> SourceDocument:
        _source_path(self.logical_path)
        metadata = (self.newline_style, self.has_final_newline, self.line_count, self.byte_count)
        if self.state != "bound":
            if self.text is not None or self.editable or any(item is not None for item in metadata):
                raise ValueError("unbound full documents cannot disclose source or metadata")
            return self
        if self.text is None or any(item is None for item in metadata):
            raise ValueError("bound full documents require original text and newline metadata")
        payload = self.text.encode("utf-8")
        if (
            len(payload) != self.byte_count
            or hashlib.sha256(payload).hexdigest() != self.expected_sha256
        ):
            raise ValueError("full document must match its recorded source hash and byte count")
        if self.line_count != len(self.text.splitlines()):
            raise ValueError("full document line count disagrees with original text")
        if self.newline_style != _document_newlines(
            self.text
        ) or self.has_final_newline != self.text.endswith(("\n", "\r")):
            raise ValueError("full document newline metadata disagrees with original text")
        if self.editable != (self.newline_style != "mixed"):
            raise ValueError("mixed or unsupported newline sequences must stay read-only")
        return self


class SourceBrowseDocument(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    revision: StrictInt = Field(ge=0)
    document: SourceDocument


def _read_confined(root: Path, parts: tuple[str, ...], max_bytes: int = _MAX_BYTES) -> bytes:
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("source is not a regular file")
            return stream.read(max_bytes + 1)
    finally:
        os.close(directory)


def _source_path(logical_path: str) -> PurePosixPath:
    path = PurePosixPath(logical_path)
    if (
        not logical_path
        or path.is_absolute()
        or "\\" in logical_path
        or any(part in {"", ".", ".."} for part in logical_path.split("/"))
    ):
        raise ValueError("source path must be a normalized report-relative path")
    return path


def read_source_document(source: SourceFileRecord, root: Path | None) -> SourceDocument:
    """Read only an explicitly selected hash-bound original, never a draft/save."""
    path = _source_path(source.logical_path)

    def result(state: PreviewState, message: str) -> SourceDocument:
        return SourceDocument(
            logical_path=source.logical_path,
            expected_sha256=source.source_sha256,
            state=state,
            message=message,
        )

    if root is None:
        return result("disabled", "Full source requires an explicit --source-root.")
    try:
        payload = _read_confined(root, path.parts, _MAX_DOCUMENT_BYTES)
    except OSError:
        return result("unavailable", "Source is unavailable or violates path confinement.")
    if len(payload) > _MAX_DOCUMENT_BYTES:
        return result(
            "too-large",
            "Source exceeds the 1 MiB full-document bound; use paged read-only previews.",
        )
    if hashlib.sha256(payload).hexdigest() != source.source_sha256:
        return result("mismatch", "Source has changed; no full document is disclosed.")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return result("invalid-encoding", "Source is not valid UTF-8.")
    count = len(text.splitlines())
    if count > _MAX_DOCUMENT_LINES:
        return result("too-large", "Source exceeds 20,000 lines; use paged read-only previews.")
    newline = _document_newlines(text)
    return SourceDocument(
        logical_path=source.logical_path,
        expected_sha256=source.source_sha256,
        state="bound",
        message=(
            "Verified original; mixed or unsupported newlines remain read-only."
            if newline == "mixed"
            else "Verified original full document; draft creation requires an explicit user action."
        ),
        text=text,
        editable=newline != "mixed",
        newline_style=newline,
        has_final_newline=text.endswith(("\n", "\r")),
        line_count=count,
        byte_count=len(payload),
    )


def preview_source(
    report: AnalysisReport,
    root: Path | None,
    logical_path: str,
    *,
    start: int = 1,
    end: int = 40,
) -> SourcePreview:
    source = next((s for s in report.source_files if s.logical_path == logical_path), None)
    if source is None:
        raise ValueError("source path is absent from the selected report")
    return preview_source_record(source, root, start=start, end=end)


def preview_source_record(
    source: SourceFileRecord | SourcePreviewIdentity,
    root: Path | None,
    *,
    start: int = 1,
    end: int = 40,
) -> SourcePreview:
    """Shared confined reader for a validated report-bound source identity."""
    logical_path = source.logical_path
    if type(start) is not int or type(end) is not int or start < 1 or end < start:
        raise ValueError("source range requires positive integers and end >= start")
    path = _source_path(logical_path)

    def result(state: PreviewState, message: str) -> SourcePreview:
        return SourcePreview(
            logical_path=logical_path,
            expected_sha256=source.source_sha256,
            state=state,
            message=message,
        )

    if root is None:
        return result("disabled", "Source preview requires an explicit --source-root.")
    try:
        payload = _read_confined(root, path.parts)
    except OSError:
        return result("unavailable", "Source is unavailable or violates path confinement.")
    if len(payload) > _MAX_BYTES:
        return result("too-large", "Source exceeds the 4 MiB preview input bound.")
    if hashlib.sha256(payload).hexdigest() != source.source_sha256:
        return result("mismatch", "Source has changed; no text is shown for this saved report.")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return result("invalid-encoding", "Source is not valid UTF-8.")
    if start > max(1, len(lines)):
        raise ValueError("source range starts beyond the file")
    stop = min(end, start + 199, len(lines))
    return SourcePreview(
        logical_path=logical_path,
        expected_sha256=source.source_sha256,
        state="bound",
        message="Source bytes match the selected report; read-only preview.",
        lines=tuple(SourceLine(number=i + 1, text=lines[i]) for i in range(start - 1, stop)),
        next_line=stop + 1 if stop < min(end, len(lines)) else None,
    )
