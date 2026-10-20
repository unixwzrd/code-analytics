from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest
from browser_navigation import reveal_navigation

from code_analysis.models import Completeness
from code_analysis.projections.runtime import build_runtime_graph, select_runtime_graph
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.runtime_models import RuntimeSelection
from code_analysis.runtime_queries import (
    RuntimeQueryRequest,
    RuntimeQuerySection,
    build_runtime_query,
)
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

GOLDEN = Path(__file__).parent / "golden" / "phase4-profile-report.json"


def test_runtime_graph_reuses_selector_and_keeps_explicit_omissions() -> None:
    report = RuntimeViewService.load(GOLDEN).profile
    selection = RuntimeSelection(max_nodes=1, max_edges=1)
    expected = select_runtime_graph(report, selection)
    graph = build_runtime_graph(report, selection)
    assert {node.node_id for node in graph.nodes} == {frame.frame_id for frame in expected.frames}
    assert graph.omitted_node_count == len(report.frames) - len(graph.nodes)
    assert graph.omitted_edge_count == len(report.runtime_edges) - len(graph.edges)
    assert graph.truncated and graph.completeness is Completeness.PARTIAL
    assert graph == build_runtime_graph(report, selection)
    assert graph.projection_id != build_runtime_graph(report, RuntimeSelection()).projection_id
    for format_ in (
        ProjectionFormat.JSON,
        ProjectionFormat.MARKDOWN,
        ProjectionFormat.DOT,
        ProjectionFormat.MERMAID,
        ProjectionFormat.HTML,
    ):
        rendered = render_projection(graph, format_)
        assert rendered and "/Users/" not in rendered


def test_runtime_graph_download_matches_saved_projection_and_fails_closed(
    ordinary_report, monkeypatch
) -> None:
    server = create_server(ReportViewService(ordinary_report), profile_report=GOLDEN, token="test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        path = "/api/runtime/download?format=dot&max_nodes=1&max_edges=1"
        connection.request("GET", path)
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("GET", path, headers={"X-Code-Analysis-Token": "test"})
        response = connection.getresponse()
        assert response.status == 200 and response.getheader("X-Projection-Truncated") == "true"
        graph = build_runtime_graph(
            RuntimeViewService.load(GOLDEN).profile, RuntimeSelection(max_nodes=1, max_edges=1)
        )
        assert response.read() == render_projection(graph, ProjectionFormat.DOT).encode()

        def unavailable(*args):
            raise OSError("synthetic unavailable Graphviz")

        monkeypatch.setattr("code_analysis.web_server.render_projection_binary", unavailable)
        connection.request(
            "GET", "/api/runtime/download?format=svg", headers={"X-Code-Analysis-Token": "test"}
        )
        response = connection.getresponse()
        assert response.status == 503 and b"renderer-unavailable" in response.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_runtime_views_reuse_shared_queries_and_preserve_mismatch(tmp_path) -> None:
    saved = tmp_path / "profile.json"
    saved.write_bytes(GOLDEN.read_bytes())
    service = RuntimeViewService.load(saved, saved)
    saved.unlink()
    assert not service.overview("0" * 64).source_matches
    assert service.overview(service.profile.source_report_sha256).source_matches
    for section in RuntimeQuerySection:
        request = RuntimeQueryRequest(section=section, max_records=1)
        assert service.panel(request).projection == build_runtime_query(service.profile, request)
    assert service.delta is not None and service.delta.diagnostic_only
    assert not service.delta.claim_eligible
    assert service.comparison_view().report.records == service.delta.records[:100]
    with pytest.raises(ValueError):
        service.panel(RuntimeQueryRequest(section=RuntimeQuerySection.HOTSPOTS, max_records=101))
    frame = service.profile.frames[0].frame_id
    projection = service.panel(
        RuntimeQueryRequest(section=RuntimeQuerySection.CALLS, frame_id=frame, max_records=100)
    )
    assert all(
        frame in (edge.caller_frame_id, edge.callee_frame_id)
        for edge in projection.projection.records
    )
    with pytest.raises(ValueError):
        service.panel(
            RuntimeQueryRequest(
                section=RuntimeQuerySection.RESOURCES, frame_id=frame, max_records=100
            )
        )


def test_runtime_ui_rejects_malformed_or_unpaired_input(ordinary_report, tmp_path) -> None:
    from code_analysis.ui_runtime import RuntimeOverview

    with pytest.raises(ValueError):
        RuntimeOverview(available=False, message="missing", profile_identity="a" * 64)
    saved = tmp_path / "bad.json"
    saved.write_text("{}")
    with pytest.raises(ValueError):
        RuntimeViewService.load(saved)
    with pytest.raises(ValueError, match="requires"):
        create_server(ReportViewService(ordinary_report), compare_profile=GOLDEN)


def test_runtime_routes_are_authenticated_and_absence_is_explicit(ordinary_report) -> None:
    from code_analysis.ui_runtime import RuntimeOverview

    server = create_server(ReportViewService(ordinary_report), token="test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request("GET", "/api/runtime")
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("GET", "/api/runtime", headers={"X-Code-Analysis-Token": "test"})
        response = connection.getresponse()
        assert response.status == 200
        overview = RuntimeOverview.model_validate_json(response.read())
        assert not overview.available and overview.completeness is None
        assert "--profile-report" in overview.message
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_browser_runtime_navigation_and_comparison(ordinary_report, tmp_path) -> None:
    from code_analysis.profile_models import RuntimeCallEdge
    from code_analysis.profiling import render_profile_json

    playwright = pytest.importorskip("playwright.sync_api")
    profile = RuntimeViewService.load(GOLDEN).profile
    recursive = RuntimeCallEdge(
        runtime_edge_id="runtime-edge-" + "f" * 20,
        caller_frame_id=profile.frames[0].frame_id,
        callee_frame_id=profile.frames[0].frame_id,
        primitive_calls=1,
        total_calls=2,
        total_seconds=0.01,
        cumulative_seconds=0.02,
    )
    saved_profile = tmp_path / "profile.json"
    saved_profile.write_text(
        render_profile_json(
            profile.model_copy(update={"runtime_edges": (*profile.runtime_edges, recursive)})
        )
    )
    server = create_server(
        ReportViewService(ordinary_report),
        profile_report=saved_profile,
        compare_profile=saved_profile,
        token="runtime-test",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=runtime-test")
                reveal_navigation(page, "Runtime").click()
                page.get_by_role("columnheader", name="function", exact=True).wait_for()
                assert "no cross-report source binding" in page.locator("#content").inner_text()
                page.locator("[data-frame]").first.click()
                page.get_by_role("columnheader", name="caller frame id").wait_for()
                selector = page.get_by_label("Runtime view", exact=True)
                selector.select_option("resources")
                page.locator("input[name=frame]").fill("")
                page.get_by_role("button", name="Show runtime evidence").click()
                page.get_by_role("columnheader", name="wall seconds").wait_for()
                selector.select_option("graph")
                page.get_by_role("button", name="Show runtime evidence").click()
                page.get_by_role("img", name="Observed runtime calls", exact=True).wait_for()
                assert page.locator("path.graph-edge").count() == 1
                with page.expect_download() as download:
                    page.get_by_role("button", name="Download JSON", exact=True).click()
                saved = json.loads(download.value.path().read_text())
                assert saved["projection_kind"] == "graph"
                assert len(saved["nodes"]) <= 60 and len(saved["edges"]) <= 120
                page.locator("[data-projection-node]").first.focus()
                page.keyboard.press("Enter")
                page.get_by_role("columnheader", name="caller frame id").wait_for()
                selector.select_option("comparison")
                page.get_by_role("button", name="Show runtime evidence").click()
                page.get_by_text("Diagnostic comparison only; claim eligible: false.").wait_for()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
