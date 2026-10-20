"""Exact saved runtime-to-static source binding; no path guesses or source reads."""

from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import Field, StrictInt, StrictStr, model_validator

from code_analysis.models import StrictModel
from code_analysis.profile_models import FrameClassification, ProfileFrame
from code_analysis.runtime_comparison_browse import RuntimeComparisonFrameBinding
from code_analysis.runtime_models import RuntimeSourceHash
from code_analysis.ui_source import SourcePreviewIdentity

HOTSPOTS_SECTION = "hotspots"
SOURCE_HASHES_SECTION = "source-hashes"
CANDIDATE_HOTSPOTS_SECTION = "candidate-hotspots"
CANDIDATE_SOURCE_HASHES_SECTION = "candidate-source-hashes"
COMPARISON_FRAMES_SECTION = "comparison-frames"

ComparisonSourceSide = Literal["baseline", "candidate"]


def require_comparison_source_side(side: str) -> ComparisonSourceSide:
    if side == "baseline":
        return "baseline"
    if side == "candidate":
        return "candidate"
    raise ValueError("runtime comparison source side must be baseline or candidate")


class RuntimeSourceUnavailable(ValueError):
    """Valid saved evidence cannot establish a project source binding."""


class RuntimeSourcePreview(StrictModel):
    frame_id: StrictStr = Field(pattern=r"^profile-frame-[0-9a-f]{20}$")
    source: SourcePreviewIdentity
    start: StrictInt = Field(ge=1)
    end: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def reconcile(self) -> RuntimeSourcePreview:
        if self.end != self.start + 39:
            raise ValueError("runtime source preview requires its bounded 40-line window")
        return self


def read_runtime_comparison_frame_binding(
    connection: sqlite3.Connection, frame_identity: str
) -> RuntimeComparisonFrameBinding:
    row = connection.execute(
        "SELECT payload FROM runtime_records WHERE section = ? AND frame_a = ?",
        (COMPARISON_FRAMES_SECTION, frame_identity),
    ).fetchone()
    if row is None or not isinstance(row[0], str) or len(row[0].encode()) > 1024**2:
        raise ValueError("runtime comparison frame binding unavailable or outside byte bound")
    binding = RuntimeComparisonFrameBinding.model_validate_json(row[0])
    if binding.frame_identity != frame_identity:
        raise ValueError("runtime comparison frame identity mismatch")
    return binding


def read_runtime_source(
    connection: sqlite3.Connection,
    frame_id: str,
    *,
    hotspots_section: str = HOTSPOTS_SECTION,
    hashes_section: str = SOURCE_HASHES_SECTION,
) -> RuntimeSourcePreview:
    row = connection.execute(
        "SELECT payload FROM runtime_records WHERE section = ? AND frame_a = ?",
        (hotspots_section, frame_id),
    ).fetchone()
    if row is None or not isinstance(row[0], str) or len(row[0].encode()) > 1024**2:
        raise ValueError("runtime frame unavailable or outside byte bound")
    frame = ProfileFrame.model_validate_json(row[0])
    if (
        frame.frame_id != frame_id
        or frame.classification is not FrameClassification.PROJECT
        or frame.line < 1
    ):
        raise RuntimeSourceUnavailable("runtime frame has no project source location")
    row = connection.execute(
        "SELECT payload FROM runtime_records WHERE section = ? AND frame_a = ?",
        (hashes_section, frame.path),
    ).fetchone()
    if row is None or not isinstance(row[0], str) or len(row[0].encode()) > 1024**2:
        raise RuntimeSourceUnavailable("runtime frame has no captured source hash")
    source = RuntimeSourceHash.model_validate_json(row[0])
    if source.path != frame.path:
        raise ValueError("runtime frame/source path mismatch")
    return RuntimeSourcePreview(
        frame_id=frame_id,
        source=SourcePreviewIdentity(logical_path=source.path, source_sha256=source.sha256),
        start=frame.line,
        end=frame.line + 39,
    )
