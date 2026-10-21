"""Bounded finding markers derived exclusively from validated saved evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from code_analysis.code_browse import (
    CodeBrowseBindingError,
    CodeBrowseReader,
    CodeBrowseSelectionError,
)
from code_analysis.models import Completeness, SourceLanguage, StrictModel, evidence_identity
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json
from code_analysis.reports.source_bindings import SourceBinding

MAX_PAGE_BYTES = 256 * 1024


def _source_path(value: str) -> str:
    SourceBinding(logical_path=value, language=SourceLanguage.PYTHON, source_sha256="0" * 64)
    return value


def _marker_id(finding_id: str, path: str, key: tuple[int, int, int, int]) -> str:
    payload = json.dumps([finding_id, path, *key], separators=(",", ":"))
    return "finding-marker-" + hashlib.sha256(payload.encode()).hexdigest()[:32]


class SourceFindingRequest(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    revision: StrictInt = Field(ge=0)
    path: StrictStr = Field(min_length=1)
    max_records: StrictInt = Field(default=500, ge=1, le=1000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _source_path(value)


class SourceFindingRecord(StrictModel):
    annotation_id: StrictStr = Field(pattern=r"^finding-marker-[0-9a-f]{32}$")
    finding_id: StrictStr = Field(min_length=1, max_length=512)
    label: StrictStr = Field(min_length=1, max_length=256)
    description: StrictStr = Field(min_length=1, max_length=2048)
    severity: Literal["info", "warning", "error"]
    rule_id: StrictStr = Field(min_length=1, max_length=256)
    start_line: StrictInt = Field(ge=1)
    end_line: StrictInt = Field(ge=1)
    start_column: StrictInt = Field(ge=0)
    end_column: StrictInt = Field(ge=0)
    evidence_ids: tuple[StrictStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if (self.end_line, self.end_column) < (self.start_line, self.start_column):
            raise ValueError("finding marker source range is reversed")
        if self.evidence_ids != tuple(sorted(set(self.evidence_ids))) or any(
            not item.startswith("evidence-")
            or len(item) != 29
            or any(character not in "0123456789abcdef" for character in item[9:])
            for item in self.evidence_ids
        ):
            raise ValueError("finding marker evidence identities must be valid, unique and sorted")
        return self


class SourceFindingPage(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    revision: StrictInt = Field(ge=0)
    path: StrictStr = Field(min_length=1)
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    completeness: Completeness
    eligible: StrictInt = Field(ge=0)
    omitted: StrictInt = Field(ge=0)
    truncated: StrictBool
    records: tuple[SourceFindingRecord, ...] = Field(max_length=1000)
    limitations: tuple[StrictStr, ...] = ()

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _source_path(value)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.eligible != len(self.records) + self.omitted:
            raise ValueError("finding marker counts do not reconcile")
        if self.truncated != (self.omitted > 0):
            raise ValueError("finding marker truncation must match omissions")
        if self.truncated and self.completeness is Completeness.COMPLETE:
            raise ValueError("truncated finding markers cannot be complete")
        identities = [item.annotation_id for item in self.records]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate finding marker identities")
        for item in self.records:
            key = (item.start_line, item.start_column, item.end_line, item.end_column)
            if item.annotation_id != _marker_id(item.finding_id, self.path, key):
                raise ValueError("finding marker identity disagrees with its source range")
        if len(self.model_dump_json().encode("utf-8")) > MAX_PAGE_BYTES:
            raise ValueError("finding marker page exceeds its byte bound")
        return self


def _concise(value: str, maximum: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= maximum else text[: maximum - 1] + "…"


def read_source_findings(
    report: AnalysisReport,
    request: SourceFindingRequest,
    *,
    reader: CodeBrowseReader | None = None,
) -> SourceFindingPage:
    """Return original-source ranges, not inferred faulty lines or draft diagnostics."""

    # Prepared readers are report-owned service state, not caller-supplied input.
    # Their canonical binding has already been computed when the report loaded.
    digest = (
        reader.report_sha256
        if reader
        else hashlib.sha256(canonical_json(report).encode()).hexdigest()
    )
    if digest != request.report_sha256:
        raise CodeBrowseBindingError("finding marker request is bound to another report")
    prepared = reader or CodeBrowseReader.from_report(report, digest)
    if request.path not in prepared.paths:
        raise CodeBrowseSelectionError("finding marker path is not present in this saved report")
    source = next(item for item in report.source_files if item.logical_path == request.path)
    records: list[SourceFindingRecord] = []
    partial = report.completeness is not Completeness.COMPLETE
    for finding in sorted(report.findings, key=lambda item: item.finding_id):
        ranges: dict[tuple[int, int, int, int], set[str]] = {}
        for evidence in finding.evidence:
            if evidence.span.path != request.path:
                continue
            if (
                evidence.source_sha256 != source.source_sha256
                or evidence.language is not source.language
                or evidence.span.language is not source.language
                or evidence.span.end_line > max(1, source.physical_loc)
            ):
                raise CodeBrowseBindingError("finding marker evidence disagrees with saved source")
            span = evidence.span
            key = (span.start_line, span.start_column, span.end_line, span.end_column)
            ranges.setdefault(key, set()).add(evidence_identity(evidence))
        if ranges and finding.completeness is not Completeness.COMPLETE:
            partial = True
        for key, evidence_ids in sorted(ranges.items()):
            records.append(
                SourceFindingRecord(
                    annotation_id=_marker_id(finding.finding_id, request.path, key),
                    finding_id=finding.finding_id,
                    label=_concise(finding.title or finding.rule_id, 256),
                    description=_concise(
                        finding.explanation or "Recorded finding applies to this source range.",
                        2048,
                    ),
                    severity=finding.severity,
                    rule_id=finding.rule_id,
                    start_line=key[0],
                    start_column=key[1],
                    end_line=key[2],
                    end_column=key[3],
                    evidence_ids=tuple(sorted(evidence_ids)),
                )
            )
    records.sort(
        key=lambda item: (
            item.start_line,
            item.start_column,
            item.end_line,
            item.end_column,
            item.finding_id,
        )
    )
    retained: list[SourceFindingRecord] = []
    # Reserve bounded metadata space before admitting a record. Prefix retention
    # remains deterministic under both record and serialized-byte limits.
    used_bytes = 2048 + len(request.path.encode())
    for record in records[: request.max_records]:
        size = len(record.model_dump_json().encode()) + 1
        if used_bytes + size > MAX_PAGE_BYTES:
            break
        retained.append(record)
        used_bytes += size
    omitted = len(records) - len(retained)
    limitations: tuple[str, ...] = (
        "Markers identify recorded evidence ranges; a function-level range does not identify "
        "an exact faulty statement.",
        "Only findings evidenced in this file are shown; counterpart evidence remains available "
        "in finding details.",
    )
    if omitted:
        limitations += (f"Finding marker bounds omitted {omitted} of {len(records)} ranges.",)
    return SourceFindingPage(
        report_sha256=digest,
        revision=request.revision,
        path=request.path,
        source_sha256=source.source_sha256,
        completeness=(
            Completeness.FAILED
            if report.completeness is Completeness.FAILED
            else Completeness.PARTIAL if partial or omitted else Completeness.COMPLETE
        ),
        eligible=len(records),
        omitted=omitted,
        truncated=bool(omitted),
        records=tuple(retained),
        limitations=limitations,
    )
