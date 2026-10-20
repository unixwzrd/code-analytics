"""Bounded SQLite storage of runtime records; never executes a workload."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from code_analysis.models import Completeness
from code_analysis.profile_models import (
    ProfileDiffRecord,
    ProfileFrame,
    ProfileStatistic,
    RuntimeCallEdge,
)
from code_analysis.profiling import render_profile_json
from code_analysis.runtime_browse_models import (
    RuntimeBrowsePage,
    RuntimeBrowseRequest,
    RuntimeIndexSummary,
    RuntimeSectionCount,
)
from code_analysis.runtime_comparison import (
    comparison_frame_bindings,
    format_comparison_frame_identity,
)
from code_analysis.runtime_comparison_browse import (
    RuntimeComparisonFrameBinding,
    RuntimeComparisonSummary,
)
from code_analysis.runtime_queries import SECTION_REGISTRY, RuntimeQuerySection
from code_analysis.runtime_source_browse import (
    CANDIDATE_HOTSPOTS_SECTION,
    CANDIDATE_SOURCE_HASHES_SECTION,
    COMPARISON_FRAMES_SECTION,
    HOTSPOTS_SECTION,
)
from code_analysis.ui_runtime import RuntimeViewService

RUNTIME_TABLE_SQL = (
    "CREATE TABLE runtime_records (section TEXT NOT NULL, position INTEGER NOT NULL, "
    "frame_a TEXT, frame_b TEXT, payload TEXT NOT NULL, "
    "PRIMARY KEY (section, position)) WITHOUT ROWID"
)


class RuntimeBrowseLimitError(ValueError):
    """An explicit runtime cache preparation bound was crossed."""


def write_runtime_records(
    connection: sqlite3.Connection,
    service: RuntimeViewService | None,
    static_identity: str | None,
    candidate_static_identity: str | None = None,
) -> RuntimeIndexSummary | None:
    connection.execute(RUNTIME_TABLE_SQL)
    if service is None:
        return None
    if (service.comparison is None) != (service.delta is None):
        raise ValueError("runtime comparison requires candidate and validated delta")
    counts = []
    total_bytes = 0
    for section, reader in SECTION_REGISTRY.items():
        records = reader(service.profile)
        if len(records) > 1_000_000:
            raise RuntimeBrowseLimitError(
                "runtime section exceeds one-million-record preparation bound"
            )
        for position, record in enumerate(records):
            payload = record.model_dump_json()
            size = len(payload.encode())
            total_bytes += size
            if size > 1024**2 or total_bytes > 2 * 1024**3:
                raise RuntimeBrowseLimitError("runtime records exceed preparation byte bounds")
            frame_a = frame_b = None
            if isinstance(record, (ProfileFrame, ProfileStatistic)):
                frame_a = record.frame_id
            elif isinstance(record, RuntimeCallEdge):
                frame_a, frame_b = record.caller_frame_id, record.callee_frame_id
            connection.execute(
                "INSERT INTO runtime_records VALUES (?, ?, ?, ?, ?)",
                (section.value, position, frame_a, frame_b, payload),
            )
        counts.append(RuntimeSectionCount(section=section, records=len(records)))
    sources = service.profile.runtime.source_hashes
    if len(sources) > 1_000_000:
        raise RuntimeBrowseLimitError("runtime source hashes exceed preparation bound")
    for position, source in enumerate(sources):
        payload = source.model_dump_json()
        total_bytes += len(payload.encode())
        if len(payload.encode()) > 1024**2 or total_bytes > 2 * 1024**3:
            raise RuntimeBrowseLimitError("runtime source hashes exceed preparation byte bounds")
        connection.execute(
            "INSERT INTO runtime_records VALUES ('source-hashes', ?, ?, NULL, ?)",
            (position, source.path, payload),
        )
    comparison = None
    if service.comparison is not None and service.delta is not None:
        candidate = service.comparison
        if len(service.delta.records) > 1_000_000:
            raise RuntimeBrowseLimitError("runtime comparison exceeds record bound")
        bindings = comparison_frame_bindings(service.profile, candidate)
        if len(bindings) != len(service.delta.records):
            raise ValueError("comparison frame bindings disagree with validated delta")
        for position, comparison_record in enumerate(service.delta.records):
            payload = comparison_record.model_dump_json()
            size = len(payload.encode())
            total_bytes += size
            if size > 1024**2 or total_bytes > 2 * 1024**3:
                raise RuntimeBrowseLimitError("runtime comparison exceeds preparation byte bounds")
            connection.execute(
                "INSERT INTO runtime_records VALUES ('comparison', ?, NULL, NULL, ?)",
                (position, payload),
            )
        candidate_frames = candidate.frames
        if len(candidate_frames) > 1_000_000:
            raise RuntimeBrowseLimitError("candidate runtime frames exceed preparation bound")
        for position, frame in enumerate(candidate_frames):
            payload = frame.model_dump_json()
            size = len(payload.encode())
            total_bytes += size
            if size > 1024**2 or total_bytes > 2 * 1024**3:
                raise RuntimeBrowseLimitError(
                    "candidate runtime frames exceed preparation byte bounds"
                )
            connection.execute(
                "INSERT INTO runtime_records VALUES (?, ?, ?, NULL, ?)",
                (CANDIDATE_HOTSPOTS_SECTION, position, frame.frame_id, payload),
            )
        candidate_sources = candidate.runtime.source_hashes
        if len(candidate_sources) > 1_000_000:
            raise RuntimeBrowseLimitError(
                "candidate runtime source hashes exceed preparation bound"
            )
        for position, source in enumerate(candidate_sources):
            payload = source.model_dump_json()
            total_bytes += len(payload.encode())
            if len(payload.encode()) > 1024**2 or total_bytes > 2 * 1024**3:
                raise RuntimeBrowseLimitError(
                    "candidate runtime source hashes exceed preparation byte bounds"
                )
            connection.execute(
                "INSERT INTO runtime_records VALUES (?, ?, ?, NULL, ?)",
                (CANDIDATE_SOURCE_HASHES_SECTION, position, source.path, payload),
            )
        for position, (identity, baseline_id, candidate_id) in enumerate(bindings):
            if service.delta.records[position].frame_identity != identity:
                raise ValueError("comparison frame identity order disagrees with delta")
            binding = RuntimeComparisonFrameBinding(
                frame_identity=identity,
                baseline_frame_id=baseline_id,
                candidate_frame_id=candidate_id,
            )
            payload = binding.model_dump_json()
            size = len(payload.encode())
            total_bytes += size
            if size > 1024**2 or total_bytes > 2 * 1024**3:
                raise RuntimeBrowseLimitError(
                    "comparison frame bindings exceed preparation byte bounds"
                )
            connection.execute(
                "INSERT INTO runtime_records VALUES (?, ?, ?, NULL, ?)",
                (COMPARISON_FRAMES_SECTION, position, identity, payload),
            )
        indexed_static_sha256s = {
            item for item in (static_identity, candidate_static_identity) if item
        }
        overview_static = (
            candidate.source_report_sha256
            if candidate.source_report_sha256 in indexed_static_sha256s
            else None
        )
        comparison = RuntimeComparisonSummary(
            candidate_report_sha256=hashlib.sha256(
                render_profile_json(candidate, compact=True).encode()
            ).hexdigest(),
            candidate_static_sha256=candidate.source_report_sha256,
            candidate_overview=RuntimeViewService(candidate).overview(overview_static),
            candidate_source_binding=candidate.runtime.source_binding,
            candidate_source_hash_count=len(candidate_sources),
            header=service.delta.model_copy(update={"records": ()}),
            records=len(service.delta.records),
            comparison_frame_count=len(bindings),
            candidate_frame_count=len(candidate_frames),
            baseline_artifact_count=len(service.profile.runtime.artifacts),
            candidate_artifact_count=len(candidate.runtime.artifacts),
        )
    return RuntimeIndexSummary(
        profile_report_sha256=hashlib.sha256(
            render_profile_json(service.profile, compact=True).encode()
        ).hexdigest(),
        static_report_sha256=service.profile.source_report_sha256,
        overview=service.overview(static_identity),
        sections=tuple(counts),
        limitations=tuple(sorted(set(service.profile.limitations))),
        comparison=comparison,
        source_binding=service.profile.runtime.source_binding,
        source_hash_count=len(sources),
    )


def validate_runtime_records(
    connection: sqlite3.Connection, summary: RuntimeIndexSummary | None
) -> None:
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'runtime_records'"
    ).fetchone() != (RUNTIME_TABLE_SQL,):
        raise ValueError("runtime index schema mismatch")
    actual = {
        row[0]: (row[1], row[2], row[3])
        for row in connection.execute(
            "SELECT section, count(*), min(position), max(position) "
            "FROM runtime_records GROUP BY section"
        )
    }
    expected = (
        {}
        if summary is None
        else {
            item.section.value: (item.records, 0, item.records - 1)
            for item in summary.sections
            if item.records
        }
    )
    if summary is not None and summary.source_hash_count:
        count = summary.source_hash_count
        expected["source-hashes"] = (count, 0, count - 1)
    if summary is not None and summary.comparison is not None:
        comparison = summary.comparison
        if comparison.records:
            count = comparison.records
            expected["comparison"] = (count, 0, count - 1)
            expected[COMPARISON_FRAMES_SECTION] = (count, 0, count - 1)
        if comparison.candidate_source_hash_count:
            count = comparison.candidate_source_hash_count
            expected[CANDIDATE_SOURCE_HASHES_SECTION] = (count, 0, count - 1)
        if comparison.candidate_frame_count:
            count = comparison.candidate_frame_count
            expected[CANDIDATE_HOTSPOTS_SECTION] = (count, 0, count - 1)
    if actual != expected:
        raise ValueError("runtime index counts disagree with receipt")
    if summary is not None and summary.comparison is not None:
        _validate_comparison_frame_map(connection, summary)


def _validate_comparison_frame_map(
    connection: sqlite3.Connection, summary: RuntimeIndexSummary
) -> None:
    comparison = summary.comparison
    assert comparison is not None
    baseline_frame_count = next(
        item.records for item in summary.sections if item.section is RuntimeQuerySection.HOTSPOTS
    )
    baseline_frames = _load_runtime_hotspot_inventory(
        connection, HOTSPOTS_SECTION, baseline_frame_count
    )
    candidate_frames = _load_runtime_hotspot_inventory(
        connection, CANDIDATE_HOTSPOTS_SECTION, comparison.candidate_frame_count
    )
    left = _comparison_frame_id_by_identity(
        baseline_frames,
        baseline_artifact_count=comparison.baseline_artifact_count,
        candidate_artifact_count=comparison.candidate_artifact_count,
    )
    right = _comparison_frame_id_by_identity(
        candidate_frames,
        baseline_artifact_count=comparison.baseline_artifact_count,
        candidate_artifact_count=comparison.candidate_artifact_count,
    )
    expected_identities = sorted(set(left) | set(right))
    rows = connection.execute(
        "SELECT frame_a, payload FROM runtime_records WHERE section = ? ORDER BY position",
        (COMPARISON_FRAMES_SECTION,),
    ).fetchall()
    if len(rows) != comparison.comparison_frame_count:
        raise ValueError("comparison frame map size disagrees with receipt")
    delta_rows = connection.execute(
        "SELECT payload FROM runtime_records WHERE section = 'comparison' ORDER BY position"
    ).fetchall()
    if len(delta_rows) != comparison.records:
        raise ValueError("comparison delta size disagrees with receipt")
    expected_delta = [
        ProfileDiffRecord.model_validate_json(row[0]).frame_identity
        for row in delta_rows
        if isinstance(row[0], str)
    ]
    if len(expected_delta) != comparison.records:
        raise ValueError("comparison delta records are malformed")
    if expected_delta != expected_identities:
        raise ValueError("comparison frame map order disagrees with delta")
    for index, identity in enumerate(expected_identities):
        frame_a, payload = rows[index]
        if not isinstance(frame_a, str) or not isinstance(payload, str):
            raise ValueError("comparison frame map record is malformed")
        if frame_a != identity:
            raise ValueError("comparison frame map contains duplicate or mismatched identity")
        if len(payload.encode()) > 1024**2:
            raise ValueError("comparison frame map record exceeds byte bound")
        binding = RuntimeComparisonFrameBinding.model_validate_json(payload)
        if binding.frame_identity != identity:
            raise ValueError("comparison frame map contains duplicate or mismatched identity")
        expected_baseline = left.get(identity)
        expected_candidate = right.get(identity)
        if binding.baseline_frame_id != expected_baseline:
            raise ValueError("comparison frame map baseline binding disagrees with saved inventory")
        if binding.candidate_frame_id != expected_candidate:
            raise ValueError(
                "comparison frame map candidate binding disagrees with saved inventory"
            )


def _load_runtime_hotspot_inventory(
    connection: sqlite3.Connection, section: str, expected_count: int
) -> tuple[ProfileFrame, ...]:
    rows = connection.execute(
        "SELECT frame_a, payload FROM runtime_records WHERE section = ? ORDER BY position",
        (section,),
    ).fetchall()
    if len(rows) != expected_count:
        raise ValueError("runtime hotspot inventory disagrees with receipt")
    frames: list[ProfileFrame] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row[1], str) or len(row[1].encode()) > 1024**2:
            raise ValueError("runtime hotspot inventory record is malformed")
        frame = ProfileFrame.model_validate_json(row[1])
        if row[0] != frame.frame_id:
            raise ValueError("runtime hotspot lookup identity disagrees with stored frame")
        if frame.frame_id in seen:
            raise ValueError("runtime hotspot inventory contains duplicate frame identity")
        seen.add(frame.frame_id)
        frames.append(frame)
    return tuple(frames)


def _comparison_frame_id_by_identity(
    frames: tuple[ProfileFrame, ...],
    *,
    baseline_artifact_count: int,
    candidate_artifact_count: int,
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for frame in frames:
        identity = format_comparison_frame_identity(
            frame,
            baseline_artifact_count=baseline_artifact_count,
            candidate_artifact_count=candidate_artifact_count,
        )
        if identity in mapping:
            raise ValueError("runtime hotspot inventory contains ambiguous comparison identity")
        mapping[identity] = frame.frame_id
    return mapping


def _payload(value: object) -> dict[str, object]:
    if not isinstance(value, str) or len(value.encode()) > 1024**2:
        raise ValueError("runtime record exceeds byte bound")
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("runtime record must be an object")
    return decoded


def read_runtime_page(
    connection: sqlite3.Connection,
    summary: RuntimeIndexSummary,
    request: RuntimeBrowseRequest,
) -> RuntimeBrowsePage:
    # RuntimeIndexSummary requires an available, strictly validated overview.
    completeness = summary.overview.completeness
    assert completeness is not None
    conditions = "section = ?"
    parameters: list[str | int] = [request.section.value]
    if request.frame_id is not None:
        if (
            connection.execute(
                "SELECT 1 FROM runtime_records WHERE section = 'hotspots' AND frame_a = ? LIMIT 1",
                (request.frame_id,),
            ).fetchone()
            is None
        ):
            raise ValueError("selected frame is absent from the indexed profile")
        conditions += " AND (frame_a = ? OR frame_b = ?)"
        parameters.extend((request.frame_id, request.frame_id))
    eligible = connection.execute(
        f"SELECT count(*) FROM runtime_records WHERE {conditions}", parameters
    ).fetchone()[0]
    rows = connection.execute(
        f"SELECT payload FROM runtime_records WHERE {conditions} "
        "ORDER BY position LIMIT ? OFFSET ?",
        (*parameters, request.limit, request.offset),
    ).fetchall()
    if sum(len(row[0].encode()) for row in rows if isinstance(row[0], str)) > 8 * 1024**2:
        raise ValueError("runtime page exceeds eight-MiB bound")
    records = [_payload(row[0]) for row in rows]
    identities = (
        {
            value
            for record in records
            for key in ("frame_id", "caller_frame_id", "callee_frame_id")
            if isinstance(value := record.get(key), str)
        }
        if request.section
        in {RuntimeQuerySection.HOTSPOTS, RuntimeQuerySection.CALLS, RuntimeQuerySection.VARIANCE}
        else set()
    )
    labels = []
    for identity in sorted(identities):
        row = connection.execute(
            "SELECT payload FROM runtime_records WHERE section = 'hotspots' AND frame_a = ?",
            (identity,),
        ).fetchone()
        if row is None:
            raise ValueError("runtime page references an absent frame")
        frame = ProfileFrame.model_validate_json(row[0])
        if frame.frame_id != identity:
            raise ValueError("runtime frame lookup identity mismatch")
        labels.append(
            {"frame_id": identity, "label": f"{frame.path}:{frame.line}:{frame.function}"}
        )
    omitted = eligible - len(records)
    limitations = set(summary.limitations)
    if summary.overview.completeness is not Completeness.COMPLETE:
        limitations.add("Source runtime report is not complete.")
    if omitted:
        limitations.add("Query records exceed the requested presentation bound.")
    return RuntimeBrowsePage.model_validate_json(
        json.dumps(
            {
                "offset": request.offset,
                "next_offset": (
                    request.offset + len(records)
                    if request.offset + len(records) < eligible
                    else None
                ),
                "panel": {
                    "labels": labels,
                    "projection": {
                        "source_report_sha256": summary.profile_report_sha256,
                        "source_profile_identity_sha256": summary.overview.profile_identity,
                        "section": request.section.value,
                        "frame_id": request.frame_id,
                        "max_records": request.limit,
                        "eligible_count": eligible,
                        "retained_count": len(records),
                        "omitted_count": omitted,
                        "truncated": bool(omitted),
                        "source_completeness": completeness.value,
                        "records": records,
                        "limitations": sorted(limitations),
                    },
                },
            }
        )
    )
