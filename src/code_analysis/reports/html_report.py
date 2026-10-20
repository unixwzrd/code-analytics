"""Self-contained HTML views of validated saved evidence, with no source access."""

from __future__ import annotations

import base64
import hashlib
import math
from dataclasses import dataclass
from html import escape

from code_analysis.projection_models import (
    GraphEdge,
    GraphNode,
    GraphProjection,
    Projection,
    ProjectionScalar,
    TableProjection,
)

_STYLE = """
:root{font-family:system-ui,sans-serif;color:#152238;background:#f6f8fc}
body{max-width:1200px;margin:auto;padding:24px}h1,h2{overflow-wrap:anywhere}
section{background:white;padding:20px;margin:20px 0;border:1px solid #ccd5df;border-radius:10px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:8px;border-bottom:1px solid #ddd;vertical-align:top}
td{overflow-wrap:anywhere}th{background:#e8eff8}
pre{white-space:pre-wrap;overflow-wrap:anywhere}.scroll{overflow:auto}
code,li{overflow-wrap:anywhere}
input{font:inherit;padding:10px;width:min(90%,500px)}
a{color:#125888}.status{font-weight:600}nav a{display:inline-block;margin:5px}
tr[hidden]{display:none}
.graph-scroll{max-width:100%;overflow:auto;border:1px solid #ccd5df;border-radius:8px}
.graph-scroll svg{display:block;width:auto;height:auto;background:#fdfeff}
.edge{stroke:#748daa;stroke-width:1.5}
.leader{stroke:#8ea6be;stroke-width:1;stroke-dasharray:3 3;pointer-events:none}
.node circle{fill:#087f6b;stroke:white;stroke-width:1.5}
.node text{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:14px;
fill:#152238;pointer-events:none}
.node:focus circle{stroke:#0b3d91;stroke-width:4}
.node:focus .leader{stroke:#0b3d91;opacity:1}
.node:focus text{fill:#0b3d91;text-decoration:underline}
"""
_SCRIPT = """
document.querySelector('#filter').addEventListener('input', event => {
const text = event.target.value.toLocaleLowerCase();
for (const row of document.querySelectorAll('tbody tr')) {
row.hidden = !row.textContent.toLocaleLowerCase().includes(text);
}
});
"""


def _scalar(value: ProjectionScalar) -> str:
    for item in (value.string_value, value.integer_value, value.number_value, value.boolean_value):
        if item is not None:
            return str(item)
    return "Unresolved / not recorded"


GRAPH_LABEL_CHARS = 24
GRAPH_LABEL_SPACING = 24.0
GRAPH_LABEL_FONT_SIZE = 14
GRAPH_LABEL_COLUMN_WIDTH = 208.0
GRAPH_MARGIN = 30.0
GRAPH_LEADER_GAP = 34.0
GRAPH_LEADER_INSET = 7.0
GRAPH_RING_RADIUS_X = 200.0
GRAPH_RING_MARGIN = 60.0
GRAPH_RING_MIN_RADIUS_Y = 90.0
GRAPH_NODE_RADIUS = 7.0
GRAPH_MIN_HEIGHT = 360.0
GRAPH_CENTER_X = GRAPH_MARGIN + GRAPH_LABEL_COLUMN_WIDTH + GRAPH_LEADER_GAP + GRAPH_RING_RADIUS_X
GRAPH_CANVAS_WIDTH = 2 * GRAPH_CENTER_X


@dataclass(frozen=True)
class _NodePlacement:
    node_x: float
    node_y: float
    label_x: float
    label_y: float
    anchor: str
    side: str


def _ring_angle(index: int, count: int) -> float:
    return 2 * math.pi * index / count - math.pi / 2


def graph_label_sides(node_count: int) -> tuple[str, ...]:
    """Assign each ring position to the label column on its own side of the vertical axis."""
    return tuple(
        "left" if math.cos(_ring_angle(index, node_count)) <= 0 else "right"
        for index in range(node_count)
    )


def graph_canvas_height(node_count: int) -> float:
    """Natural canvas height that keeps every column label GRAPH_LABEL_SPACING apart."""
    sides = graph_label_sides(node_count)
    tallest = max(sides.count("left"), sides.count("right")) if sides else 0
    return max(GRAPH_MIN_HEIGHT, 2 * GRAPH_MARGIN + GRAPH_LABEL_SPACING * max(0, tallest - 1))


def graph_label_text(label: str) -> str:
    """Column text for a node, kept inside GRAPH_LABEL_COLUMN_WIDTH at the monospace label size."""
    if len(label) <= GRAPH_LABEL_CHARS:
        return label
    return "\u2026" + label[1 - GRAPH_LABEL_CHARS :]


def _graph_layout(nodes: tuple[GraphNode, ...]) -> dict[str, _NodePlacement]:
    """Place nodes on an ellipse and stack their labels in ordered columns outside it."""
    count = len(nodes)
    height = graph_canvas_height(count)
    center_y = height / 2
    radius_y = max(GRAPH_RING_MIN_RADIUS_Y, center_y - GRAPH_RING_MARGIN)
    sides = graph_label_sides(count)
    columns: dict[str, tuple[float, str, list[tuple[float, int]]]] = {
        "left": (GRAPH_MARGIN + GRAPH_LABEL_COLUMN_WIDTH, "end", []),
        "right": (GRAPH_CENTER_X + GRAPH_RING_RADIUS_X + GRAPH_LEADER_GAP, "start", []),
    }
    points = []
    for index in range(count):
        angle = _ring_angle(index, count)
        points.append(
            (
                GRAPH_CENTER_X + math.cos(angle) * GRAPH_RING_RADIUS_X,
                center_y + math.sin(angle) * radius_y,
            )
        )
        columns[sides[index]][2].append((points[index][1], index))
    top, bottom = GRAPH_MARGIN, height - GRAPH_MARGIN
    placements: dict[str, _NodePlacement] = {}
    for side, (label_x, anchor, members) in columns.items():
        members.sort()
        step = min(GRAPH_LABEL_SPACING, (bottom - top) / max(1, len(members) - 1))
        cursor = top
        for position, (node_y, index) in enumerate(members):
            latest = bottom - step * (len(members) - position - 1)
            label_y = min(max(node_y, cursor), latest)
            placements[nodes[index].node_id] = _NodePlacement(
                node_x=points[index][0],
                node_y=node_y,
                label_x=label_x,
                label_y=label_y,
                anchor=anchor,
                side=side,
            )
            cursor = label_y + step
    return placements


def _graph_edge(edge: GraphEdge, placements: dict[str, _NodePlacement]) -> str:
    source, target = placements[edge.source_id], placements[edge.target_id]
    return (
        f'<line class="edge" x1="{source.node_x:.2f}" y1="{source.node_y:.2f}" '
        f'x2="{target.node_x:.2f}" y2="{target.node_y:.2f}">'
        f"<title>{escape(edge.source_id)} to "
        f"{escape(edge.target_id)}: {escape(edge.kind)}</title></line>"
    )


def _graph_node(node: GraphNode, place: _NodePlacement, position: int, count: int) -> str:
    inset = GRAPH_LEADER_INSET if place.side == "left" else -GRAPH_LEADER_INSET
    tip_x = place.label_x + inset
    span = math.hypot(tip_x - place.node_x, place.label_y - place.node_y) or 1.0
    start_x = place.node_x + (tip_x - place.node_x) / span * GRAPH_NODE_RADIUS
    start_y = place.node_y + (place.label_y - place.node_y) / span * GRAPH_NODE_RADIUS
    identity = (
        f"Node {position} of {count}: {node.label}; kind {node.kind}; identity {node.node_id}"
    )
    return (
        f'<g class="node" role="group" tabindex="0" aria-label="{escape(identity, quote=True)}">'
        f"<title>{escape(identity)}</title>"
        f'<line class="leader" aria-hidden="true" x1="{start_x:.2f}" y1="{start_y:.2f}" '
        f'x2="{tip_x:.2f}" y2="{place.label_y:.2f}"/>'
        f'<circle cx="{place.node_x:.2f}" cy="{place.node_y:.2f}" r="{GRAPH_NODE_RADIUS:.2f}"/>'
        f'<text x="{place.label_x:.2f}" y="{place.label_y:.2f}" text-anchor="{place.anchor}" '
        f'dominant-baseline="middle" aria-hidden="true">'
        f"{escape(graph_label_text(node.label))}</text></g>"
    )


def _graph(projection: GraphProjection) -> str:
    count = len(projection.nodes)
    summary = (
        f"<p>{count} of {projection.original_node_count} nodes; "
        f"{len(projection.edges)} of {projection.original_edge_count} edges. "
        "Edge direction and complete identities are listed below.</p>"
    )
    if not count:
        return summary + "<p>No nodes were retained, so no diagram is drawn.</p>"
    placements = _graph_layout(projection.nodes)
    height = graph_canvas_height(count)
    edges = "".join(_graph_edge(edge, placements) for edge in projection.edges)
    nodes = "".join(
        _graph_node(node, placements[node.node_id], position, count)
        for position, node in enumerate(projection.nodes, start=1)
    )
    return (
        f"{summary}<p>Node labels are stacked in ordered side columns at "
        f"{GRAPH_LABEL_FONT_SIZE}px; the dotted leaders are display aids, not edges. "
        "Truncated column text is shown in full on each node and in the listing below; "
        "scroll the diagram sideways on narrow screens.</p>"
        f'<div class="graph-scroll"><svg width="{GRAPH_CANVAS_WIDTH:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {GRAPH_CANVAS_WIDTH:.0f} {height:.0f}" '
        f'role="group" aria-label="{escape(projection.title, quote=True)}">'
        f"<g>{edges}</g><g>{nodes}</g></svg></div>"
    )


def projection_section(projection: Projection) -> str:
    from code_analysis.reports.projection_views import render_projection_markdown

    status = (
        f'<p class="status">Completeness: {escape(projection.completeness.value)}; '
        f"{projection.record_count} retained records; projection truncated: "
        f"{str(projection.truncated).lower()}.</p>"
    )
    if isinstance(projection, TableProjection):
        headers = "".join(
            f'<th scope="col">{escape(item.label)}</th>' for item in projection.columns
        )
        rows = "".join(
            "<tr>" + "".join(f"<td>{escape(_scalar(cell))}</td>" for cell in row.cells) + "</tr>"
            for row in projection.rows
        )
        body = (
            f'<div class="scroll"><table><thead><tr>{headers}</tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
        )
    else:
        body = _graph(projection) if isinstance(projection, GraphProjection) else ""
        body += f"<pre>{escape(render_projection_markdown(projection))}</pre>"
    limitations = "".join(f"<li>{escape(item)}</li>" for item in projection.limitations)
    identities = "".join(
        f"<li>{item.role.value} report SHA-256: <code>{item.report_sha256}</code></li>"
        for item in projection.inputs
    )
    return (
        f'<section id="{projection.projection_id}"><h2>{escape(projection.title)}</h2>'
        f"{status}<ul>{identities}{limitations}</ul>{body}</section>"
    )


def render_html_document(title: str, projections: tuple[Projection, ...]) -> str:
    script_hash = base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()
    style_hash = base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode()
    policy = (
        f"default-src 'none'; script-src 'sha256-{script_hash}'; "
        f"style-src 'sha256-{style_hash}'; img-src data:; base-uri 'none'; form-action 'none'"
    )
    links = "".join(
        f'<a href="#{item.projection_id}">{escape(item.title)}</a>' for item in projections
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{escape(policy, quote=True)}">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head><body>"
        f"<h1>{escape(title)}</h1><p>Saved evidence snapshot. Filtering changes visibility only; "
        "counts and completeness describe the exported projections.</p>"
        '<label for="filter">Filter table rows</label><br><input id="filter" type="search">'
        f'<nav aria-label="Report sections">{links}</nav>'
        + "".join(projection_section(item) for item in projections)
        + f"<script>{_SCRIPT}</script></body></html>\n"
    )
