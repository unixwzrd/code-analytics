"""Bounded runtime graph selection directly from validated indexed records."""

from __future__ import annotations

import sqlite3

from pydantic import Field, StrictBool, StrictInt

from code_analysis.models import StrictModel
from code_analysis.profile_models import ProfileFrame, RuntimeCallEdge
from code_analysis.projection_models import GraphProjection, ProjectionInput, ProjectionInputRole
from code_analysis.projections.runtime import RuntimeGraphView, runtime_graph_projection
from code_analysis.runtime_browse_models import RuntimeIndexSummary
from code_analysis.runtime_models import RuntimeSelection
from code_analysis.runtime_queries import RuntimeQuerySection


class RuntimeGraphRequest(StrictModel):
    max_nodes: StrictInt = Field(default=60, ge=1, le=100)
    max_edges: StrictInt = Field(default=120, ge=1, le=250)
    project_only: StrictBool = False


def _payloads(rows: list[tuple[str]]) -> tuple[str, ...]:
    total = 0
    result = []
    for row in rows:
        if not isinstance(row[0], str) or len(row[0].encode()) > 1024**2:
            raise ValueError("runtime graph record exceeds byte bound")
        total += len(row[0].encode())
        if total > 8 * 1024**2:
            raise ValueError("runtime graph selection exceeds byte bound")
        result.append(row[0])
    return tuple(result)


def read_runtime_graph(
    connection: sqlite3.Connection,
    summary: RuntimeIndexSummary,
    request: RuntimeGraphRequest,
) -> GraphProjection:
    rows = connection.execute(
        "SELECT payload FROM runtime_records WHERE section = 'hotspots' "
        "AND (? = 0 OR json_extract(payload, '$.classification') = 'project') "
        "ORDER BY coalesce(json_extract(payload, '$.cumulative_seconds'), 0) DESC, frame_a "
        "LIMIT ?",
        (int(request.project_only), request.max_nodes),
    ).fetchall()
    frames = tuple(ProfileFrame.model_validate_json(item) for item in _payloads(rows))
    ids = tuple(frame.frame_id for frame in frames)
    edges: tuple[RuntimeCallEdge, ...] = ()
    if ids:
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            "SELECT payload FROM runtime_records WHERE section = 'calls' "
            f"AND frame_a IN ({placeholders}) AND frame_b IN ({placeholders}) "
            "ORDER BY json_extract(payload, '$.total_calls') DESC, "
            "json_extract(payload, '$.runtime_edge_id') LIMIT ?",
            (*ids, *ids, request.max_edges),
        ).fetchall()
        edges = tuple(RuntimeCallEdge.model_validate_json(item) for item in _payloads(rows))
    counts = {item.section: item.records for item in summary.sections}
    view = RuntimeGraphView(
        frames, edges, counts[RuntimeQuerySection.HOTSPOTS], counts[RuntimeQuerySection.CALLS]
    )
    return runtime_graph_projection(
        view,
        RuntimeSelection(**request.model_dump()),
        ProjectionInput(
            role=ProjectionInputRole.SOURCE,
            report_schema_name="code-analysis-profile-report",
            report_schema_version="1.0.0",
            report_sha256=summary.profile_report_sha256,
            run_identity_sha256=summary.overview.profile_identity,
        ),
        summary.overview.completeness,
        summary.limitations,
    )
