"""Bounded views of retained runtime evidence; no source or provider access."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass
from html import escape

from code_analysis.models import Completeness, Confidence
from code_analysis.profile_models import (
    FrameClassification,
    ProfileFrame,
    ProfileReport,
    RuntimeCallEdge,
)
from code_analysis.projection_models import (
    GraphEdge,
    GraphNode,
    GraphProjection,
    ProjectionInput,
    ProjectionInputRole,
    ProjectionSelection,
)
from code_analysis.runtime_models import RuntimeSelection


def build_runtime_graph(report: ProfileReport, selection: RuntimeSelection) -> GraphProjection:
    """Adapt the existing bounded runtime selector to the shared graph contract."""
    from code_analysis.profiling import render_profile_json

    view = select_runtime_graph(report, selection)
    digest = hashlib.sha256(render_profile_json(report, compact=True).encode()).hexdigest()
    return runtime_graph_projection(
        view,
        selection,
        ProjectionInput(
            role=ProjectionInputRole.SOURCE,
            report_schema_name=report.schema_name,
            report_schema_version=report.schema_version,
            report_sha256=digest,
            run_identity_sha256=report.profile_identity_sha256,
        ),
        report.completeness,
        report.limitations,
    )


def runtime_graph_projection(
    view: RuntimeGraphView,
    selection: RuntimeSelection,
    source: ProjectionInput,
    completeness: Completeness,
    source_limitations: tuple[str, ...],
) -> GraphProjection:
    """Render a validated bounded view identically from full or indexed evidence."""
    digest = source.report_sha256
    identity = hashlib.sha256((digest + selection.model_dump_json()).encode()).hexdigest()[:20]
    omitted_nodes = view.original_nodes - len(view.frames)
    omitted_edges = view.original_edges - len(view.edges)
    truncated = bool(omitted_nodes or omitted_edges)
    limitations = set(source_limitations)
    limitations.add("Observed workload calls only; absence is not proof that a call cannot occur.")
    limitations.add(
        "Nodes rank by cumulative seconds then frame ID; edges by call count then edge ID."
    )
    if selection.project_only:
        limitations.add(
            "Only project-classified frames are retained; other frames count as omitted."
        )
    return GraphProjection(
        projection_id=f"projection-{identity}",
        title="Observed runtime calls",
        inputs=(source,),
        selection=ProjectionSelection(max_nodes=selection.max_nodes, max_edges=selection.max_edges),
        completeness=Completeness.PARTIAL if truncated else completeness,
        truncated=truncated,
        truncation_reason="runtime graph selection bound" if truncated else None,
        limitations=tuple(sorted(limitations)),
        record_count=len(view.frames) + len(view.edges),
        nodes=tuple(
            sorted(
                (
                    GraphNode(
                        node_id=frame.frame_id,
                        label=f"{frame.path}:{frame.line}:{frame.function}",
                        kind=frame.classification.value,
                    )
                    for frame in view.frames
                ),
                key=lambda node: node.node_id,
            )
        ),
        edges=tuple(
            sorted(
                (
                    GraphEdge(
                        edge_id=edge.runtime_edge_id,
                        source_id=edge.caller_frame_id,
                        target_id=edge.callee_frame_id,
                        kind="runtime-observed-call",
                        label=(
                            f"{edge.total_calls} calls"
                            + ("; edge timing unavailable" if edge.total_seconds is None else "")
                        ),
                        confidence=Confidence.RUNTIME_OBSERVED,
                    )
                    for edge in view.edges
                ),
                key=lambda edge: edge.edge_id,
            )
        ),
        original_node_count=view.original_nodes,
        original_edge_count=view.original_edges,
        omitted_node_count=omitted_nodes,
        omitted_edge_count=omitted_edges,
    )


@dataclass(frozen=True, slots=True)
class RuntimeGraphView:
    frames: tuple[ProfileFrame, ...]
    edges: tuple[RuntimeCallEdge, ...]
    original_nodes: int
    original_edges: int


def select_runtime_graph(report: ProfileReport, selection: RuntimeSelection) -> RuntimeGraphView:
    eligible = (
        frame
        for frame in report.frames
        if not selection.project_only or frame.classification == FrameClassification.PROJECT
    )
    frames = tuple(
        sorted(eligible, key=lambda frame: (-(frame.cumulative_seconds or 0.0), frame.frame_id))[
            : selection.max_nodes
        ]
    )
    ids = {frame.frame_id for frame in frames}
    edges = tuple(
        sorted(
            (
                edge
                for edge in report.runtime_edges
                if edge.caller_frame_id in ids and edge.callee_frame_id in ids
            ),
            key=lambda edge: (-edge.total_calls, edge.runtime_edge_id),
        )[: selection.max_edges]
    )
    return RuntimeGraphView(frames, edges, len(report.frames), len(report.runtime_edges))


def runtime_dot(
    report: ProfileReport, selection: RuntimeSelection, *, overlay: bool = False
) -> str:
    view = select_runtime_graph(report, selection)
    colors = {
        "project": "#cce8ff",
        "standard-library": "#e5e7eb",
        "third-party": "#ffedcc",
        "environment": "#f2dcff",
        "native": "#ddf4df",
        "generated": "#faf1bd",
        "unresolved": "#ffd7d7",
    }
    category = {
        item.frame_id: item.primary_category or "unclassified" for item in report.runtime.categories
    }
    lines = [
        "digraph runtime_calls {",
        f"  rankdir={selection.direction};",
        "  node [shape=box, style=filled, fontname=Helvetica];",
    ]
    label = (
        f"Runtime call evidence ({report.completeness.value}) — "
        f"{len(view.frames)}/{view.original_nodes} nodes; "
        f"{len(view.edges)}/{view.original_edges} edges\n"
        "Width = 1 + log10(1 + calls). Fill = ownership. Activity is an independent label.\n"
        "Self and cumulative seconds are distinct; cumulative times overlap. Missing is not zero.\n"
        f"Producer: {report.producer.value}. "
        "Node weights are frame timings, not wall or GPU time.\n"
        "Edge width/label = observed call count; integer caller edge timing is unavailable.\n"
        "Ownership: blue project; gray stdlib; orange third party; "
        "purple environment; green native; yellow generated; red unresolved."
    )
    if overlay:
        label += (
            "\nStatic overlay: hash-bound subject annotations only. "
            "Static call-edge evidence unavailable; imports are not calls."
        )
    lines.append(f"  label={json.dumps(label, ensure_ascii=False)};")
    for frame in view.frames:
        calls = frame.total_calls if frame.total_calls is not None else "unknown"
        text = (
            f"{frame.path}:{frame.line}\n{frame.function}\ncalls={calls} "
            f"self={frame.total_seconds} cumulative={frame.cumulative_seconds}\n"
            f"activity={category.get(frame.frame_id, 'unclassified')}"
        )
        if overlay:
            text += f"\nsubject={frame.subject_id or 'unresolved'}"
        lines.append(
            f"  {json.dumps(frame.frame_id)} [label={json.dumps(text)}, "
            f"fillcolor={json.dumps(colors[frame.classification.value])}];"
        )
    for edge in view.edges:
        edge_label = str(edge.total_calls)
        if edge.total_seconds is None:
            edge_label += " calls; edge timing unavailable"
        lines.append(
            f"  {json.dumps(edge.caller_frame_id)} -> {json.dumps(edge.callee_frame_id)} "
            f"[label={json.dumps(edge_label)}, "
            f"penwidth={1 + math.log10(1 + edge.total_calls):.3f}];"
        )
    return "\n".join([*lines, "}", ""])


def runtime_table(report: ProfileReport, *, kind: str, maximum: int) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
    if kind == "functions":
        writer.writerow(
            (
                "frame_id",
                "path",
                "line",
                "function",
                "ownership",
                "calls",
                "self_seconds",
                "cumulative_seconds",
                "self_per_call",
                "cumulative_per_primitive_call",
            )
        )
        for frame in sorted(
            report.frames, key=lambda item: (-(item.total_seconds or 0.0), item.frame_id)
        )[:maximum]:
            writer.writerow(
                (
                    frame.frame_id,
                    frame.path,
                    frame.line,
                    frame.function,
                    frame.classification.value,
                    frame.total_calls,
                    frame.total_seconds,
                    frame.cumulative_seconds,
                    frame.per_call_seconds,
                    frame.cumulative_per_primitive_call_seconds,
                )
            )
    else:
        writer.writerow(
            (
                "edge_id",
                "caller",
                "callee",
                "calls",
                "self_seconds",
                "cumulative_seconds",
                "caller_representation",
                "primitive_calls",
            )
        )
        ordering = (
            (lambda edge: (edge.callee_frame_id, edge.caller_frame_id))
            if kind == "callers"
            else (lambda edge: (edge.caller_frame_id, edge.callee_frame_id))
        )
        for edge in sorted(report.runtime_edges, key=ordering)[:maximum]:
            writer.writerow(
                (
                    edge.runtime_edge_id,
                    edge.caller_frame_id,
                    edge.callee_frame_id,
                    edge.total_calls,
                    edge.total_seconds,
                    edge.cumulative_seconds,
                    edge.caller_representation.value,
                    edge.primitive_calls,
                )
            )
    return stream.getvalue()


def hotspot_svg(report: ProfileReport, maximum: int) -> str:
    frames = sorted(
        ((item, item.total_seconds) for item in report.frames if item.total_seconds is not None),
        key=lambda item: (-item[1], item[0].frame_id),
    )[: min(maximum, 50)]
    height = 110 + len(frames) * 36
    peak = max((seconds for _frame, seconds in frames), default=0.0) or 1.0
    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" '
        f'height="{height}" viewBox="0 0 1200 {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<g font-family="sans-serif" font-size="13" fill="#172033">',
        '<text x="20" y="25">Runtime hotspots — self seconds '
        "(not cumulative or unprofiled elapsed time)</text>",
        f'<text x="20" y="48">{len(frames)} / {len(report.frames)} frames shown; '
        f"{report.completeness.value}; unknown timings excluded</text>",
    ]
    for index, (frame, seconds) in enumerate(frames):
        y = 85 + index * 36
        label = escape(f"{frame.path}:{frame.line}:{frame.function}"[:120])
        lines.append(
            f'<text x="20" y="{y}">{label}</text><rect x="730" y="{y - 12}" '
            f'width="{350 * seconds / peak:.3f}" height="16" fill="#357abd"/>'
            f'<text x="1090" y="{y}">{seconds:.6f}</text>'
        )
    return "\n".join([*lines, "</g></svg>\n"])
