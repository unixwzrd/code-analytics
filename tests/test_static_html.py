from __future__ import annotations

import base64
import errno
import hashlib
import itertools
import math
import re
from html import escape, unescape
from pathlib import Path

import pytest

from code_analysis.cli import main
from code_analysis.models import Completeness
from code_analysis.projection_models import (
    GraphEdge,
    GraphNode,
    GraphProjection,
    ProjectionInput,
    ProjectionInputRole,
)
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.reports.html_report import render_html_document
from code_analysis.reports.json_report import canonical_json, render_json
from code_analysis.reports.projection_views import ProjectionFormat, render_projection

OUTPUT = Path(__file__).resolve().parents[1] / "output" / "playwright"
SCREENSHOT = OUTPUT / "20260922-static-html-labels.png"

# Independent copies of the static renderer's layout contract; the tests recompute the
# deterministic column stack from these rather than importing the renderer's arithmetic.
CANVAS_WIDTH, CENTER_X, RING_RADIUS_X = 944.0, 472.0, 200.0
MIN_HEIGHT, MARGIN, SPACING = 360.0, 30.0, 24.0
# Rendered coordinates are emitted at two decimals and measured as float32 by getBBox.
SPACING_TOLERANCE = 0.05
RING_MARGIN, MIN_RADIUS_Y, NODE_RADIUS = 60.0, 90.0, 7.0
LEFT_COLUMN_X, RIGHT_COLUMN_X, LABEL_CHARS = 238.0, 706.0, 24
GRAPH_COUNTS = (0, 1, 36, 100)
LONG_LABEL = "deeply/nested/package/path/module_{index:03d}_with_a_very_long_name.py"

NODE_PATTERN = re.compile(
    r'<g class="node" role="group" tabindex="0" aria-label="(?P<aria>[^"]*)">'
    r"<title>(?P<title>.*?)</title>"
    r'<line class="leader" aria-hidden="true" x1="(?P<leader_x1>[-\d.]+)" '
    r'y1="(?P<leader_y1>[-\d.]+)" x2="(?P<leader_x2>[-\d.]+)" y2="(?P<leader_y2>[-\d.]+)"/>'
    r'<circle cx="(?P<cx>[-\d.]+)" cy="(?P<cy>[-\d.]+)" r="(?P<r>[-\d.]+)"/>'
    r'<text x="(?P<label_x>[-\d.]+)" y="(?P<label_y>[-\d.]+)" '
    r'text-anchor="(?P<anchor>\w+)" dominant-baseline="middle" aria-hidden="true">'
    r"(?P<text>.*?)</text></g>"
)
SVG_PATTERN = re.compile(
    r'<div class="graph-scroll"><svg width="(?P<width>\d+)" height="(?P<height>\d+)" '
    r'viewBox="0 0 (?P<view_width>\d+) (?P<view_height>\d+)" role="group" '
    r'aria-label="(?P<aria>[^"]*)">'
)
GEOMETRY_SCRIPT = """() => {
  const svg = document.querySelector('.graph-scroll svg');
  const rect = svg.getBoundingClientRect();
  return {
    view_width: svg.viewBox.baseVal.width,
    view_height: svg.viewBox.baseVal.height,
    rendered_width: rect.width,
    rendered_height: rect.height,
    edges: [...svg.querySelectorAll('line.edge')].map(line => line.textContent),
    nodes: [...svg.querySelectorAll('g.node')].map(group => {
      const text = group.querySelector('text');
      const circle = group.querySelector('circle');
      const leader = group.querySelector('line.leader');
      const box = text.getBBox();
      return {
        aria: group.getAttribute('aria-label'),
        label: text.textContent,
        font_size: getComputedStyle(text).fontSize,
        dash: getComputedStyle(leader).strokeDasharray,
        left: box.x,
        top: box.y,
        right: box.x + box.width,
        bottom: box.y + box.height,
        cx: Number(circle.getAttribute('cx')),
        cy: Number(circle.getAttribute('cy')),
        r: Number(circle.getAttribute('r')),
      };
    }),
  };
}"""
FOCUS_SCRIPT = """() => {
  const active = document.activeElement;
  if (!active || !active.matches || !active.matches('g.node')) return null;
  return {
    aria: active.getAttribute('aria-label'),
    stroke: getComputedStyle(active.querySelector('circle')).strokeWidth,
  };
}"""
CONTAINMENT_SCRIPT = """() => {
  const wrap = document.querySelector('.graph-scroll');
  return {
    document_scroll: document.documentElement.scrollWidth,
    document_client: document.documentElement.clientWidth,
    wrap_scroll: wrap.scrollWidth,
    wrap_client: wrap.clientWidth,
    svg_width: wrap.querySelector('svg').getBoundingClientRect().width,
  };
}"""


def _graph_projection(count: int, *, label: str | None = None) -> GraphProjection:
    nodes = tuple(
        GraphNode(
            node_id=f"node-{index:03d}",
            label=label if label is not None else LONG_LABEL.format(index=index),
            kind="module",
        )
        for index in range(count)
    )
    edges = tuple(
        GraphEdge(
            edge_id=f"edge-{index:03d}",
            source_id=f"node-{index:03d}",
            target_id=f"node-{(index + 1) % count:03d}",
            kind="import",
        )
        for index in range(count if count > 1 else 0)
    )
    return GraphProjection(
        projection_id=f"projection-{count:020x}",
        title="Bounded dependency graph",
        inputs=(
            ProjectionInput(
                role=ProjectionInputRole.SOURCE,
                report_schema_name="code-analysis-report",
                report_schema_version="1.0.0",
                report_sha256="1" * 64,
            ),
        ),
        completeness=Completeness.COMPLETE,
        record_count=count + len(edges),
        nodes=nodes,
        edges=edges,
    )


def _expected_layout(count: int) -> tuple[float, list[dict[str, float | str]]]:
    """Recompute the deterministic ellipse-and-column layout from the published contract."""
    angles = [2 * math.pi * index / count - math.pi / 2 for index in range(count)]
    sides = ["left" if math.cos(angle) <= 0 else "right" for angle in angles]
    tallest = max(sides.count("left"), sides.count("right"))
    height = max(MIN_HEIGHT, 2 * MARGIN + SPACING * (tallest - 1))
    center_y = height / 2
    radius_y = max(MIN_RADIUS_Y, center_y - RING_MARGIN)
    points = [
        (CENTER_X + math.cos(angle) * RING_RADIUS_X, center_y + math.sin(angle) * radius_y)
        for angle in angles
    ]
    placements: dict[int, dict[str, float | str]] = {}
    for side, label_x, anchor in (
        ("left", LEFT_COLUMN_X, "end"),
        ("right", RIGHT_COLUMN_X, "start"),
    ):
        members = sorted(
            (points[index][1], index) for index in range(count) if sides[index] == side
        )
        step = min(SPACING, (height - 2 * MARGIN) / max(1, len(members) - 1))
        cursor = MARGIN
        for position, (node_y, index) in enumerate(members):
            latest = height - MARGIN - step * (len(members) - position - 1)
            label_y = min(max(node_y, cursor), latest)
            placements[index] = {
                "cx": points[index][0],
                "cy": points[index][1],
                "label_x": label_x,
                "label_y": label_y,
                "anchor": anchor,
                "side": side,
            }
            cursor = label_y + step
    return height, [placements[index] for index in range(count)]


def _identity(projection: GraphProjection, index: int) -> str:
    node = projection.nodes[index]
    return (
        f"Node {index + 1} of {len(projection.nodes)}: {node.label}; "
        f"kind {node.kind}; identity {node.node_id}"
    )


def _disjoint(first: tuple[float, ...], second: tuple[float, ...]) -> bool:
    return (
        first[2] <= second[0]
        or second[2] <= first[0]
        or first[3] <= second[1]
        or second[3] <= first[1]
    )


@pytest.mark.parametrize("count", GRAPH_COUNTS)
def test_static_graph_canvas_grows_for_ordered_readable_label_columns(count: int) -> None:
    projection = _graph_projection(count)
    html = render_html_document("Static graph", (projection,))
    assert html == render_html_document("Static graph", (projection,))
    assert (
        f"<p>{count} of {count} nodes; {len(projection.edges)} of "
        f"{len(projection.edges)} edges. "
        "Edge direction and complete identities are listed below.</p>"
    ) in html
    assert f"{projection.record_count} retained records" in html
    assert "Completeness: complete" in html
    assert projection.projection_id in html
    for index, node in enumerate(projection.nodes):
        assert escape(node.label) in html
        assert escape(_identity(projection, index), quote=True) in html
    if not count:
        assert "<svg" not in html
        assert "No nodes were retained" in html
        return

    height, expected = _expected_layout(count)
    canvas = SVG_PATTERN.search(html)
    assert canvas is not None
    assert (int(canvas["width"]), int(canvas["view_width"])) == (CANVAS_WIDTH, CANVAS_WIDTH)
    assert (int(canvas["height"]), int(canvas["view_height"])) == (height, height)
    assert height >= MIN_HEIGHT

    nodes = list(NODE_PATTERN.finditer(html))
    assert len(nodes) == count
    assert len(re.findall(r'<line class="edge"', html)) == len(projection.edges)
    for index, (node, place) in enumerate(zip(nodes, expected, strict=True)):
        assert node["aria"] == escape(_identity(projection, index), quote=True)
        assert node["title"] == escape(_identity(projection, index))
        assert node["text"] == escape(
            projection.nodes[index].label
            if len(projection.nodes[index].label) <= LABEL_CHARS
            else "\u2026" + projection.nodes[index].label[1 - LABEL_CHARS :]
        )
        assert len(unescape(node["text"])) <= LABEL_CHARS
        assert float(node["cx"]) == pytest.approx(place["cx"], abs=0.01)
        assert float(node["cy"]) == pytest.approx(place["cy"], abs=0.01)
        assert float(node["r"]) == pytest.approx(NODE_RADIUS, abs=1e-9)
        assert float(node["label_x"]) == pytest.approx(place["label_x"], abs=0.01)
        assert float(node["label_y"]) == pytest.approx(place["label_y"], abs=0.01)
        assert node["anchor"] == place["anchor"]
        assert float(node["leader_y2"]) == pytest.approx(place["label_y"], abs=0.01)
        assert math.hypot(
            float(node["leader_x1"]) - float(node["cx"]),
            float(node["leader_y1"]) - float(node["cy"]),
        ) == pytest.approx(NODE_RADIUS, abs=0.01)

    for side in ("left", "right"):
        column = sorted(
            float(nodes[index]["label_y"])
            for index in range(count)
            if expected[index]["side"] == side
        )
        for previous, current in itertools.pairwise(column):
            assert current - previous >= SPACING - SPACING_TOLERANCE


def test_static_graph_escapes_hostile_labels_and_binds_policy_hashes() -> None:
    hostile = '</text><script>alert("x")</script>'
    html = render_html_document("Static graph", (_graph_projection(2, label=hostile),))
    assert '<script>alert("x")</script>' not in html
    assert "&lt;/text&gt;&lt;script&gt;" in html
    assert "&quot;" in html
    policy = re.search(r'Content-Security-Policy" content="([^"]+)"', html)
    assert policy is not None
    directives = unescape(policy.group(1))
    style = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert style is not None and script is not None
    for inline in (style.group(1), script.group(1)):
        digest = base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in directives
    assert "default-src 'none'" in directives
    assert "http" not in html.replace("http-equiv", "")


def _assert_natural_scale_labels(page, projection: GraphProjection) -> None:
    count = len(projection.nodes)
    height, expected = _expected_layout(count)
    geometry = page.evaluate(GEOMETRY_SCRIPT)
    assert (geometry["view_width"], geometry["view_height"]) == (CANVAS_WIDTH, height)
    assert geometry["rendered_width"] == pytest.approx(CANVAS_WIDTH, abs=1.0)
    assert geometry["rendered_height"] == pytest.approx(height, abs=1.0)
    assert geometry["edges"] == [
        f"{edge.source_id} to {edge.target_id}: {edge.kind}" for edge in projection.edges
    ]
    nodes = geometry["nodes"]
    assert [node["aria"] for node in nodes] == [_identity(projection, i) for i in range(count)]

    boxes = [(node["left"], node["top"], node["right"], node["bottom"]) for node in nodes]
    circles = [
        (
            node["cx"] - node["r"],
            node["cy"] - node["r"],
            node["cx"] + node["r"],
            node["cy"] + node["r"],
        )
        for node in nodes
    ]
    for node, box in zip(nodes, boxes, strict=True):
        assert node["font_size"] == "14px"
        assert node["dash"] not in ("", "none")
        assert box[0] >= 1.0 and box[2] <= CANVAS_WIDTH - 1.0
        assert box[1] >= 1.0 and box[3] <= height - 1.0
        assert all(_disjoint(box, circle) for circle in circles)
    for first, second in itertools.combinations(boxes, 2):
        assert _disjoint(first, second)
    for side in ("left", "right"):
        column = sorted(
            nodes[index]["top"] for index in range(count) if expected[index]["side"] == side
        )
        for previous, current in itertools.pairwise(column):
            assert current - previous >= SPACING - SPACING_TOLERANCE


def _assert_keyboard_identities(page, projection: GraphProjection) -> None:
    expected = [_identity(projection, index) for index in range(len(projection.nodes))]
    page.get_by_label("Filter table rows").focus()
    reached: list[str] = []
    strokes: set[str] = set()
    for _ in range(len(expected) + 12):
        page.keyboard.press("Tab")
        focused = page.evaluate(FOCUS_SCRIPT)
        if focused is not None:
            reached.append(focused["aria"])
            strokes.add(focused["stroke"])
        if len(reached) == len(expected):
            break
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    resting = page.evaluate(
        "() => getComputedStyle(document.querySelector('g.node circle')).strokeWidth"
    )
    assert reached == expected
    assert strokes == {"4px"}
    assert resting == "1.5px"


def _watch(page) -> tuple[list[str], list[str]]:
    """Bind per-page sinks for remote requests and page errors."""
    remote: list[str] = []
    errors: list[str] = []
    page.on(
        "request",
        lambda request: (
            remote.append(request.url) if request.url.startswith(("http:", "https:")) else None
        ),
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    return remote, errors


def _assert_mobile_containment(page) -> None:
    page.set_viewport_size({"width": 375, "height": 720})
    metrics = page.evaluate(CONTAINMENT_SCRIPT)
    assert metrics["document_scroll"] <= metrics["document_client"] + 1
    assert metrics["wrap_scroll"] > metrics["wrap_client"]
    assert metrics["svg_width"] == pytest.approx(CANVAS_WIDTH, abs=1.0)


def test_static_graph_labels_are_readable_focusable_and_offline(tmp_path: Path) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            for count in GRAPH_COUNTS:
                projection = _graph_projection(count)
                path = tmp_path / f"graph-{count:03d}.html"
                path.write_text(render_html_document("Static graph", (projection,)))
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                remote, errors = _watch(page)
                page.goto(path.as_uri())
                summary = page.locator("section > p").nth(1).inner_text()
                assert summary.startswith(
                    f"{count} of {count} nodes; "
                    f"{len(projection.edges)} of {len(projection.edges)} edges."
                )
                listing = page.locator("section > pre").inner_text()
                for node in projection.nodes:
                    assert node.node_id in listing and node.label in listing
                for edge in projection.edges:
                    assert (
                        f"`{edge.source_id}` | {edge.kind} | `{edge.target_id}` | "
                        f"{edge.resolution.value} |"
                    ) in listing
                if count:
                    _assert_natural_scale_labels(page, projection)
                    _assert_keyboard_identities(page, projection)
                    if count == 36:
                        SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)
                        page.locator(".graph-scroll").screenshot(path=SCREENSHOT)
                    _assert_mobile_containment(page)
                else:
                    assert page.locator("svg").count() == 0
                assert not remote
                assert not errors
                page.close()
        finally:
            browser.close()
    assert SCREENSHOT.exists()


def test_html_is_deterministic_portable_and_escaped(ordinary_report) -> None:
    projection = build_projection(ordinary_report, ProjectionName.MODULES)
    hostile = projection.model_copy(update={"title": '</script><img src=x onerror="alert(1)">'})
    html = render_projection(hostile, ProjectionFormat.HTML)
    assert html == render_projection(hostile, ProjectionFormat.HTML)
    assert "<img src=x" not in html
    assert "&lt;/script&gt;" in html
    assert projection.inputs[0].report_sha256 in html
    assert "Content-Security-Policy" in html
    assert not any(path in html for path in ("/Users/", "/Volumes/", "/tmp/"))


def test_static_html_cli_saved_only_and_partial_status(ordinary_report, tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    output = tmp_path / "report.html"
    report.write_text(canonical_json(ordinary_report))
    assert main(["html", str(report), "--max-records", "1", "-o", str(output)]) == errno.ENODATA
    assert "projection truncated: true" in output.read_text()
    original = report.read_bytes()
    assert main(["html", str(report), "-o", str(report)]) == errno.EINVAL
    assert report.read_bytes() == original
    assert main(["html", str(report), "--compare", str(report), "-o", str(output)]) == 0
    first = output.read_text()
    assert "No canonical report changes detected" in first
    report.write_text(render_json(ordinary_report))
    assert main(["html", str(report), "--compare", str(report), "-o", str(output)]) == 0
    assert output.read_text() == first


def test_static_html_browser_is_offline_and_filterable(ordinary_report, tmp_path: Path) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    projections = (
        build_projection(ordinary_report, ProjectionName.MODULES),
        build_projection(ordinary_report, ProjectionName.DEPENDENCIES),
    )
    path = tmp_path / "offline.html"
    path.write_text(render_html_document("Offline report", projections))
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            remote_requests = []
            errors = []
            page.on(
                "request",
                lambda request: (
                    remote_requests.append(request.url)
                    if request.url.startswith(("http:", "https:"))
                    else None
                ),
            )
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(path.as_uri())
            assert page.locator("svg").count() == 1
            assert page.locator("tbody tr:visible").count() == len(ordinary_report.modules)
            page.get_by_label("Filter table rows").fill("no-such-module-999")
            assert page.locator("tbody tr:visible").count() == 0
            page.get_by_label("Filter table rows").fill("")
            assert page.locator("tbody tr:visible").count() == len(ordinary_report.modules)
            assert not remote_requests
            assert not errors
        finally:
            browser.close()
