"""Real-browser checks for shared, display-only graph controls."""

from __future__ import annotations

import itertools
import math
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from browser_navigation import reveal_navigation

from code_analysis.models import SourceLanguage
from code_analysis.ui_models import UIArchitectureGraph, UIGraphEdge, UIGraphNode
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

GOLDEN = Path(__file__).parent / "golden" / "phase4-profile-report.json"
OUTPUT = Path(__file__).resolve().parents[1] / "output" / "playwright"
SCREENSHOT = OUTPUT / "20260922-architecture-labels.png"


def test_architecture_short_labels_preserve_language_path_components(ordinary_report, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    cases = (
        (SourceLanguage.PYTHON, "package.module", "module"),
        (SourceLanguage.JAVASCRIPT, "web/static/lib/dom.js", "dom.js"),
        (SourceLanguage.JAVASCRIPT, "flat.mjs", "flat.mjs"),
        (SourceLanguage.JAVASCRIPT, "lib/parser.cjs", "parser.cjs"),
        (SourceLanguage.JAVASCRIPT, "ui/view.jsx", "view.jsx"),
        (SourceLanguage.RUBY, "lib/tools/parser.rb", "parser.rb"),
    )
    nodes = tuple(
        UIGraphNode(node_id=name, label=name, language=language, lines=1, finding_count=0)
        for language, name, _ in cases
    )
    graph = UIArchitectureGraph(
        eligible_nodes=len(nodes),
        retained_nodes=len(nodes),
        omitted_nodes=0,
        eligible_edges=0,
        retained_edges=0,
        omitted_edges=0,
        truncated=False,
        nodes=nodes,
        edges=(),
    )
    monkeypatch.setattr(ReportViewService, "architecture", lambda self, **kwargs: graph)
    with _server(ordinary_report, "graph-labels") as url, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url)
            toggle = page.locator(".lui-shell__menu")
            if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
                toggle.click()
            reveal_navigation(page, "Architecture").click()
            for _, identity, short in cases:
                node = page.get_by_role("button", name=f"Open module {identity}", exact=True)
                assert node.locator("text").text_content() == short
                assert node.get_attribute("data-module") == identity
        finally:
            browser.close()


@contextmanager
def _server(report, token: str, *, profile_report: Path | None = None):
    server = create_server(ReportViewService(report), profile_report=profile_report, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _semantic_graph(graph) -> dict[str, object]:
    return graph.evaluate("""svg => ({
          viewBox: svg.getAttribute('viewBox'),
          nodes: [...svg.querySelectorAll('[data-projection-node], [data-module]')]
            .map(node => node.dataset.projectionNode || node.dataset.module).sort(),
          edges: [...svg.querySelectorAll('.graph-edge')].map(edge => edge.textContent).sort(),
        })""")


def _assert_display_contract(page, graph) -> None:
    wrap = graph.locator("xpath=ancestor::div[contains(@class, 'graph-wrap')]")
    display = wrap.locator("xpath=preceding-sibling::*[@role='group'][1]")
    assert display.count() == 1
    assert display.get_attribute("aria-label") == "Graph display"
    title = graph.get_attribute("aria-label")
    assert title
    assert title in (wrap.get_attribute("aria-label") or "")
    assert wrap.get_attribute("role") == "region"
    assert wrap.get_attribute("tabindex") == "0"
    assert (
        graph.evaluate("""svg => {
          return {
            styledWidth: Number(svg.style.width.replace('px', '')),
            styledHeight: Number(svg.style.height.replace('px', '')),
            viewBoxWidth: svg.viewBox.baseVal.width,
            viewBoxHeight: svg.viewBox.baseVal.height,
          };
        }""")
        == graph.evaluate("""svg => ({
          styledWidth: svg.viewBox.baseVal.width,
          styledHeight: svg.viewBox.baseVal.height,
          viewBoxWidth: svg.viewBox.baseVal.width,
          viewBoxHeight: svg.viewBox.baseVal.height,
        })""")
    )
    assert graph.locator("text").first.evaluate("node => getComputedStyle(node).fontSize") == "14px"
    status = display.get_by_role("status")
    assert status.inner_text().startswith("100%")

    zoom_out = display.get_by_role("button", name="Zoom out", exact=True)
    zoom_in = display.get_by_role("button", name="Zoom in", exact=True)
    actual = display.get_by_role("button", name="Actual size", exact=True)
    fit = display.get_by_role("button", name="Fit overview", exact=True)
    semantics = _semantic_graph(graph)

    zoom_in.focus()
    page.keyboard.press("Enter")
    assert status.inner_text().startswith("125%")
    assert _semantic_graph(graph) == semantics
    zoom_out.click()
    assert status.inner_text().startswith("100%")
    for _ in range(2):
        zoom_out.click()
    assert status.inner_text().startswith("50%")
    assert zoom_out.is_disabled()
    for _ in range(10):
        zoom_in.click()
    assert status.inner_text().startswith("300%")
    assert zoom_in.is_disabled()
    actual.click()
    assert status.inner_text().startswith("100%")
    fit.click()
    fitted = int(re.match(r"(\d+)%", status.inner_text()).group(1))
    assert fitted <= 100
    actual.click()
    assert status.inner_text().startswith("100%")
    assert _semantic_graph(graph) == semantics


def test_architecture_display_is_local_readable_and_keeps_dependency_download_parameters(
    ordinary_report,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with _server(ordinary_report, "graph-display") as url, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            requests: list[str] = []
            page.on("request", lambda request: requests.append(request.url))
            page.goto(url)
            toggle = page.locator(".lui-shell__menu")
            if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
                toggle.click()
            reveal_navigation(page, "Architecture").click()
            graph = page.locator("#content > .graph-wrap svg")
            graph.wait_for()
            baseline = len(requests)
            _assert_display_contract(page, graph)
            assert len(requests) == baseline
            assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
            wrap = graph.locator("xpath=ancestor::div[contains(@class, 'graph-wrap')]")
            assert wrap.evaluate("node => node.scrollWidth >= node.clientWidth")
            wrap.focus()
            before = wrap.evaluate("node => node.scrollLeft")
            page.keyboard.press("ArrowRight")
            assert wrap.evaluate("node => node.scrollLeft") >= before
            assert wrap.evaluate("""viewport => {
                  viewport.scrollLeft = viewport.scrollWidth - viewport.clientWidth;
                  const left = viewport.getBoundingClientRect().left;
                  const node = [...viewport.querySelectorAll('.graph-node')]
                    .find(item => item.getBoundingClientRect().left < left);
                  if (!node) return false;
                  const before = viewport.scrollLeft;
                  node.focus();
                  return viewport.scrollLeft < before;
                }""")

            page.get_by_label("Source logical module", exact=True).fill("alpha.a")
            page.get_by_label("Target logical module", exact=True).fill("alpha.b")
            page.get_by_role("button", name="Apply investigation", exact=True).click()
            page.get_by_role("heading", name="Retained path", exact=True).wait_for()
            dependency_graph = page.locator("#dependency-graph svg")
            baseline = len(requests)
            _assert_display_contract(page, dependency_graph)
            assert len(requests) == baseline
            before_download = len(requests)
            with page.expect_download():
                page.get_by_role("button", name="Download JSON", exact=True).click()
            download_url = next(
                url for url in requests[before_download:] if "/api/dependencies/download" in url
            )
            params = parse_qs(urlsplit(download_url).query)
            assert params["source"] == ["alpha.a"]
            assert params["target"] == ["alpha.b"]
            assert params["graph_max_nodes"] == ["100"]
            assert _semantic_graph(dependency_graph)["nodes"]
        finally:
            browser.close()


def test_explore_runtime_and_dependency_graphs_share_display_only_controls(ordinary_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        _server(ordinary_report, "graph-display-runtime", profile_report=GOLDEN) as url,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 768, "height": 900})
            requests: list[str] = []
            page.on("request", lambda request: requests.append(request.url))
            page.goto(url)
            reveal_navigation(page, "Explore evidence").click()
            page.get_by_label("Evidence view", exact=True).select_option("symbol-graph")
            page.get_by_role("button", name="Show evidence", exact=True).click()
            explore_graph = page.locator("#projection-result .graph-wrap svg")
            explore_graph.wait_for()
            baseline = len(requests)
            _assert_display_contract(page, explore_graph)
            assert len(requests) == baseline

            reveal_navigation(page, "Runtime").click()
            assert page.get_by_role("group", name="Graph display", exact=True).count() == 0
            selector = page.get_by_label("Runtime view", exact=True)
            selector.select_option("graph")
            page.get_by_role("button", name="Show runtime evidence", exact=True).click()
            runtime_graph = page.locator("#runtime-results .graph-wrap svg")
            runtime_graph.wait_for()
            baseline = len(requests)
            _assert_display_contract(page, runtime_graph)
            assert page.get_by_role("group", name="Graph display", exact=True).count() == 1
            assert len(requests) == baseline
            assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        finally:
            browser.close()


CANVAS_WIDTH, CANVAS_HEIGHT = 900.0, 620.0
RING_CENTER_X, RING_CENTER_Y, RING_RADIUS = 450.0, 310.0, 245.0
LABEL_MARGIN, LABEL_SPACING, LEADER_GAP = 30.0, 24.0, 34.0
MAXIMAL_FINDINGS = 900
LABEL_GEOMETRY = """svg => {
      const view = svg.viewBox.baseVal;
      return {
        width: view.width,
        height: view.height,
        lines: svg.querySelectorAll('line').length,
        edges: [...svg.querySelectorAll('.graph-edge')].map(edge => ({
          title: edge.textContent,
          x1: Number(edge.getAttribute('x1')),
          y1: Number(edge.getAttribute('y1')),
          x2: Number(edge.getAttribute('x2')),
          y2: Number(edge.getAttribute('y2')),
        })),
        nodes: [...svg.querySelectorAll('g.graph-node')].map(group => {
          const circle = group.querySelector('circle');
          const texts = [...group.querySelectorAll('text')];
          const bounds = texts[0].getBBox();
          return {
            module: group.dataset.module,
            aria: group.getAttribute('aria-label'),
            title: circle.querySelector('title').textContent,
            text_count: texts.length,
            label: texts[0].textContent,
            anchor: texts[0].getAttribute('text-anchor'),
            label_x: Number(texts[0].getAttribute('x')),
            label_y: Number(texts[0].getAttribute('y')),
            left: bounds.x,
            top: bounds.y,
            right: bounds.x + bounds.width,
            bottom: bounds.y + bounds.height,
            cx: Number(circle.getAttribute('cx')),
            cy: Number(circle.getAttribute('cy')),
            r: Number(circle.getAttribute('r')),
            leaders: [...group.querySelectorAll('line')].map(line => ({
              classes: line.getAttribute('class'),
              hidden: line.getAttribute('aria-hidden'),
              dash: getComputedStyle(line).strokeDasharray,
              pointer: getComputedStyle(line).pointerEvents,
              stroke: getComputedStyle(line).stroke,
              x1: Number(line.getAttribute('x1')),
              y1: Number(line.getAttribute('y1')),
              x2: Number(line.getAttribute('x2')),
              y2: Number(line.getAttribute('y2')),
            })),
          };
        }),
      };
    }"""


def _short_label(index: int) -> str:
    """Fifteen-character short label, the widest the view retains."""
    return "w" * 13 + f"{index:02d}"


def _bounded_edges(count: int) -> tuple[tuple[str, str], ...]:
    return tuple(
        (f"deep.pkg.{_short_label(index)}", f"deep.pkg.{_short_label((index + step) % count)}")
        for step in (1, 2)
        for index in range(count)
        if count > step
    )[:72]


def _bounded_graph(count: int) -> UIArchitectureGraph:
    nodes = tuple(
        UIGraphNode(
            node_id=f"deep.pkg.{_short_label(index)}",
            label=f"deep.pkg.{_short_label(index)}",
            language=SourceLanguage.PYTHON,
            lines=400 + index,
            finding_count=MAXIMAL_FINDINGS,
        )
        for index in range(count)
    )
    edges = tuple(
        UIGraphEdge(source_id=source, target_id=target, kind="import")
        for source, target in _bounded_edges(count)
    )
    return UIArchitectureGraph(
        eligible_nodes=len(nodes),
        retained_nodes=len(nodes),
        omitted_nodes=0,
        eligible_edges=len(edges),
        retained_edges=len(edges),
        omitted_edges=0,
        truncated=False,
        nodes=nodes,
        edges=edges,
    )


def _ring_positions(count: int) -> list[tuple[float, float]]:
    positions = []
    for index in range(count):
        angle = (math.pi * 2 * index / count) - math.pi / 2
        positions.append(
            (
                RING_CENTER_X + math.cos(angle) * RING_RADIUS,
                RING_CENTER_Y + math.sin(angle) * RING_RADIUS,
            )
        )
    return positions


def _expected_columns(
    centers: list[tuple[float, float]],
) -> dict[int, tuple[float, float, str, float]]:
    """Reimplement the deterministic two-column stack from the rendered node centers."""
    top, bottom = LABEL_MARGIN, CANVAS_HEIGHT - LABEL_MARGIN
    placements: dict[int, tuple[float, float, str, float]] = {}
    for column_x, anchor, members in (
        (
            RING_CENTER_X - RING_RADIUS - LEADER_GAP,
            "end",
            [index for index, point in enumerate(centers) if point[0] <= RING_CENTER_X],
        ),
        (
            RING_CENTER_X + RING_RADIUS + LEADER_GAP,
            "start",
            [index for index, point in enumerate(centers) if point[0] > RING_CENTER_X],
        ),
    ):
        ordered = sorted(members, key=lambda index: (centers[index][1], index))
        step = min(LABEL_SPACING, (bottom - top) / max(1, len(ordered) - 1))
        cursor = top
        for position, index in enumerate(ordered):
            latest = bottom - step * (len(ordered) - position - 1)
            label_y = min(max(centers[index][1], cursor), latest)
            placements[index] = (column_x, label_y, anchor, step)
            cursor = label_y + step
    return placements


def _disjoint(first: tuple[float, ...], second: tuple[float, ...]) -> bool:
    return (
        first[2] <= second[0]
        or second[2] <= first[0]
        or first[3] <= second[1]
        or second[3] <= first[1]
    )


def _assert_labels_readable(geometry: dict, count: int) -> None:
    nodes = geometry["nodes"]
    assert geometry["width"] == CANVAS_WIDTH and geometry["height"] == CANVAS_HEIGHT
    assert [node["module"] for node in nodes] == [
        f"deep.pkg.{_short_label(index)}" for index in range(count)
    ]
    expected_positions = _ring_positions(count)
    for index, node in enumerate(nodes):
        assert node["text_count"] == 1
        assert node["label"] == _short_label(index)
        assert len(node["label"]) == 15
        assert node["aria"] == f"Open module {node['module']}"
        identity = f"{node['module']} · {400 + index} lines · {MAXIMAL_FINDINGS} findings"
        assert node["title"] == identity
        assert node["cx"] == pytest.approx(expected_positions[index][0], abs=1e-6)
        assert node["cy"] == pytest.approx(expected_positions[index][1], abs=1e-6)
        assert node["r"] == pytest.approx(15.0, abs=1e-9)

    placements = _expected_columns([(node["cx"], node["cy"]) for node in nodes])
    for index, node in enumerate(nodes):
        column_x, label_y, anchor, _ = placements[index]
        assert node["anchor"] == anchor
        assert node["label_x"] == pytest.approx(column_x, abs=1e-6)
        assert node["label_y"] == pytest.approx(label_y, abs=1e-6)
        assert len(node["leaders"]) == 1
        leader = node["leaders"][0]
        assert leader["classes"] == "graph-label-leader"
        assert leader["hidden"] == "true"
        assert leader["pointer"] == "none"
        assert leader["dash"] not in ("", "none")
        assert leader["stroke"] != geometry["edge_stroke"]
        assert math.hypot(leader["x1"] - node["cx"], leader["y1"] - node["cy"]) == pytest.approx(
            node["r"], abs=0.01
        )
        assert leader["y2"] == pytest.approx(label_y, abs=1e-6)
        if anchor == "end":
            assert node["right"] <= leader["x2"] <= node["cx"] - node["r"] + 1e-6
        else:
            assert node["cx"] + node["r"] - 1e-6 <= leader["x2"] <= node["left"]

    for anchor in ("end", "start"):
        column = sorted(
            ((index, nodes[index]) for index in range(count) if nodes[index]["anchor"] == anchor),
            key=lambda item: (item[1]["cy"], item[0]),
        )
        expected_step = placements[column[0][0]][3] if column else LABEL_SPACING
        for (_, previous), (_, current) in itertools.pairwise(column):
            assert current["label_y"] - previous["label_y"] >= expected_step - 1e-6

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
    for box in boxes:
        assert box[0] >= 2 and box[2] <= CANVAS_WIDTH - 2
        assert box[1] >= 2 and box[3] <= CANVAS_HEIGHT - 2
        assert all(_disjoint(box, circle) for circle in circles)
    for first, second in itertools.combinations(boxes, 2):
        assert _disjoint(first, second)

    expected_edges = _bounded_edges(count)
    assert geometry["lines"] == len(expected_edges) + count
    assert len(geometry["edges"]) == len(expected_edges)
    centers = {node["module"]: node for node in nodes}
    for edge, (source, target) in zip(geometry["edges"], expected_edges, strict=True):
        assert edge["title"] == f"{source} → {target} (import)"
        assert edge["x1"] == pytest.approx(centers[source]["cx"], abs=1e-9)
        assert edge["y1"] == pytest.approx(centers[source]["cy"], abs=1e-9)
        assert edge["x2"] == pytest.approx(centers[target]["cx"], abs=1e-9)
        assert edge["y2"] == pytest.approx(centers[target]["cy"], abs=1e-9)


def _label_geometry(graph) -> dict:
    geometry = graph.evaluate(LABEL_GEOMETRY)
    geometry["edge_stroke"] = graph.evaluate(
        "svg => { const edge = svg.querySelector('.graph-edge');"
        " return edge ? getComputedStyle(edge).stroke : ''; }"
    )
    return geometry


def test_architecture_column_labels_never_overlap_or_clip_for_bounded_counts(
    ordinary_report, monkeypatch
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    state = {"count": 1}
    bounds: list[dict[str, int]] = []

    def architecture(self, **kwargs) -> UIArchitectureGraph:
        bounds.append(dict(kwargs))
        return _bounded_graph(state["count"])

    monkeypatch.setattr(ReportViewService, "architecture", architecture)
    with _server(ordinary_report, "graph-columns") as url, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1100})
            page.goto(url)
            for count in range(1, 37):
                state["count"] = count
                reveal_navigation(page, "Architecture").click()
                page.wait_for_function(
                    "count => document.querySelectorAll("
                    "'#content > .graph-wrap g.graph-node').length === count",
                    arg=count,
                )
                graph = page.locator("#content > .graph-wrap svg")
                _assert_labels_readable(_label_geometry(graph), count)
                if count == 36:
                    SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)
                    page.locator("#content > .graph-wrap").screenshot(path=SCREENSHOT)
        finally:
            browser.close()
    assert bounds and all(item == {"max_nodes": 36, "max_edges": 72} for item in bounds)
    assert SCREENSHOT.exists()


def test_architecture_column_labels_hold_through_zoom_fit_and_mobile_containment(
    ordinary_report, monkeypatch
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr(
        ReportViewService, "architecture", lambda self, **kwargs: _bounded_graph(36)
    )
    with (
        _server(ordinary_report, "graph-columns-mobile") as url,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            requests: list[str] = []
            page.on("request", lambda request: requests.append(request.url))
            page.goto(url)
            toggle = page.locator(".lui-shell__menu")
            if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
                toggle.click()
            reveal_navigation(page, "Architecture").click()
            graph = page.locator("#content > .graph-wrap svg")
            graph.wait_for()
            baseline = len(requests)
            _assert_display_contract(page, graph)
            assert len(requests) == baseline
            assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
            _assert_labels_readable(_label_geometry(graph), 36)
        finally:
            browser.close()


def test_architecture_column_labels_keep_two_node_keyboard_dialog_navigation(
    ordinary_report,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        _server(ordinary_report, "graph-columns-keys") as url,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url)
            reveal_navigation(page, "Architecture").click()
            graph = page.locator("#content > .graph-wrap svg")
            graph.wait_for()
            modules = graph.locator(".graph-node").evaluate_all(
                "nodes => nodes.map(node => node.dataset.module)"
            )
            assert len(modules) >= 2
            for module in modules[:2]:
                node = graph.locator(f'.graph-node[data-module="{module}"]')
                node.focus()
                page.keyboard.press("Enter")
                dialog = page.get_by_role("dialog")
                dialog.wait_for()
                page.get_by_role("heading", name="Symbols and source").wait_for()
                source = page.get_by_label("Source logical module", exact=True)
                assert source.input_value() == module
                page.keyboard.press("Escape")
                assert not dialog.is_visible()
                assert node.evaluate("element => element === document.activeElement")
                assert node.get_attribute("aria-pressed") == "true"
                assert graph.locator('.graph-node[aria-pressed="true"]').count() == 1
                assert node.locator("text").count() == 1
        finally:
            browser.close()
