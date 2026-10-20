from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest
from browser_navigation import reveal_navigation

from code_analysis.projections import ProjectionName, build_projection
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.ui_projections import UIProjectionRequest, project_report
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


def test_ui_projection_preserves_cli_records_and_bounds(ordinary_report) -> None:
    request = UIProjectionRequest(name=ProjectionName.MODULES, max_records=1)
    projected = project_report(ordinary_report, request)
    assert projected == build_projection(
        ordinary_report, ProjectionName.MODULES, max_records=1, max_nodes=50, max_edges=100
    )
    assert projected.truncated
    with pytest.raises(ValueError):
        UIProjectionRequest(name=ProjectionName.MODULES, max_records=True)
    with pytest.raises(ValueError):
        UIProjectionRequest(name=ProjectionName.MODULES, max_nodes=1000)


def test_heatmap_asset_preserves_restrictive_browser_policy(ordinary_report) -> None:
    server = create_server(ReportViewService(ordinary_report), token="heatmap-asset-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request("GET", "/heatmaps.js")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/javascript; charset=utf-8"
        assert "style-src 'self';" in response.getheader("Content-Security-Policy")
        assert "unsafe-inline" not in response.getheader("Content-Security-Policy")
        assert b"export function heatmapView" in response.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_projection_download_matches_cli_and_checks_token(ordinary_report) -> None:
    server = create_server(ReportViewService(ordinary_report), token="projection-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        path = "/api/download?name=modules&format=tsv&max_records=1"
        connection.request("GET", path)
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("GET", path, headers={"X-Code-Analysis-Token": "projection-test"})
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("X-Projection-Truncated") == "true"
        assert "attachment;" in response.getheader("Content-Disposition")
        expected = render_projection(
            project_report(
                ordinary_report, UIProjectionRequest(name=ProjectionName.MODULES, max_records=1)
            ),
            ProjectionFormat.TSV,
        )
        assert response.read().decode() == expected
        connection.request(
            "GET",
            "/api/projection?name=not-a-view",
            headers={"X-Code-Analysis-Token": "projection-test"},
        )
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"]["code"] == "invalid-request"
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_browser_explores_and_downloads_saved_projections(ordinary_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    server = create_server(ReportViewService(ordinary_report), token="projection-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=projection-test")
                page.locator(".cards").wait_for()
                reveal_navigation(page, "Explore evidence").click()
                page.get_by_role("button", name="Download TSV", exact=True).wait_for()
                with page.expect_download() as download:
                    page.get_by_role("button", name="Download JSON", exact=True).click()
                payload = json.loads(download.value.path().read_text())
                assert payload["projection_kind"] == "table"
                assert payload["record_count"] == len(ordinary_report.modules)
                page.get_by_label("Evidence view", exact=True).select_option("dependencies")
                page.get_by_role("button", name="Show evidence", exact=True).click()
                page.locator("#projection-result svg").wait_for()
                assert page.get_by_role("button", name="Download SVG", exact=True).is_visible()
                assert "undefined" not in page.locator("#projection-result").inner_text()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
