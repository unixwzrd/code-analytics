from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest
from browser_navigation import reveal_navigation

from code_analysis.reports.json_report import canonical_json
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


def test_dependency_endpoints_are_authenticated_and_source_free(ordinary_report, tmp_path) -> None:
    saved = tmp_path / "report.json"
    saved.write_text(canonical_json(ordinary_report), encoding="utf-8")
    service = ReportViewService.load(saved)
    server = create_server(service, token="dependency-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        path = "/api/dependencies?source=alpha.a&target=alpha.b&max_nodes=2&max_edges=3"
        connection.request("GET", path)
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("GET", path, headers={"X-Code-Analysis-Token": "dependency-test"})
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("X-Dependency-Max-Nodes") == "2"
        result = json.loads(response.read())
        assert result["path"] == ["alpha.a", "alpha.b"]
        graph_path = path + "&graph_max_nodes=2&graph_max_edges=3"
        connection.request(
            "GET",
            "/api/dependencies/graph" + graph_path[len("/api/dependencies") :],
            headers={"X-Code-Analysis-Token": "dependency-test"},
        )
        response = connection.getresponse()
        assert response.status == 200
        graph = json.loads(response.read())
        assert {node["node_id"] for node in graph["nodes"]} == {"alpha.a", "alpha.b"}
        connection.request(
            "GET",
            "/api/dependencies/download" + graph_path[len("/api/dependencies") :] + "&format=dot",
            headers={"X-Code-Analysis-Token": "dependency-test"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("X-Dependency-Graph-Max-Nodes") == "2"
        assert b"digraph" in response.read()
        connection.request(
            "GET",
            "/api/dependencies/download"
            + graph_path[len("/api/dependencies") :]
            + "&format=json-compact",
            headers={"X-Code-Analysis-Token": "dependency-test"},
        )
        response = connection.getresponse()
        assert response.status == 200
        compact = response.read()
        assert compact.startswith(b'{"clusters"')
        assert b"\n" not in compact.rstrip(b"\n")
        for invalid in (
            "/api/dependencies?source=alpha.a&target=alpha.b&graph_max_nodes=101",
            "/api/dependencies?source=alpha.a&target=alpha.b&graph_max_edges=-1",
            "/api/dependencies?source=missing&target=alpha.b",
            "/api/dependencies?source=alpha.a&source=alpha.b&target=alpha.b",
        ):
            connection.request("GET", invalid, headers={"X-Code-Analysis-Token": "dependency-test"})
            response = connection.getresponse()
            assert response.status == 400
            response.read()
        connection.request(
            "GET",
            "/api/dependencies?source=alpha.a&target=alpha.b&max_nodes=1",
            headers={"X-Code-Analysis-Token": "dependency-test"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("X-Analysis-Completeness") == "partial"
        assert response.getheader("X-Source-Completeness") == "complete"
        assert json.loads(response.read())["truncated"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dependency_browser_mobile_keyboard_flow(ordinary_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    server = create_server(ReportViewService(ordinary_report), token="dependency-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=dependency-test")
                page.locator(".lui-shell__menu").click()
                reveal_navigation(page, "Architecture").click()
                source = page.get_by_label("Source logical module", exact=True)
                source.fill("alpha.a")
                page.get_by_label("Target logical module", exact=True).fill("alpha.b")
                page.get_by_role("button", name="Apply investigation", exact=True).press("Enter")
                page.get_by_role("heading", name="Retained path", exact=True).wait_for()
                assert page.locator("#dependency-graph").evaluate("""element => {
                    const line = element.querySelector('line.graph-edge');
                    const node = element.querySelector('[data-projection-node="alpha.b"] circle');
                    return Math.hypot(
                        Number(line.getAttribute('x2')) - Number(node.getAttribute('cx')),
                        Number(line.getAttribute('y2')) - Number(node.getAttribute('cy'))) > 7;
                }""")
                with page.expect_download() as download:
                    page.get_by_role("button", name="Download JSON-COMPACT", exact=True).click()
                assert download.value.suggested_filename == "dependencies.json-compact"
                module = page.get_by_role("button", name="alpha.a", exact=True).last
                module.focus()
                page.keyboard.press("Enter")
                page.get_by_role("dialog").wait_for()
                page.keyboard.press("Escape")
                assert not page.get_by_role("dialog").is_visible()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dependency_control_edit_aborts_stale_investigation(ordinary_report, monkeypatch) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    started, release = threading.Event(), threading.Event()
    original = ReportViewService.dependencies

    def delayed(self, request):
        started.set()
        assert release.wait(timeout=5)
        return original(self, request)

    monkeypatch.setattr(ReportViewService, "dependencies", delayed)
    server = create_server(ReportViewService(ordinary_report), token="dependency-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=dependency-test")
                reveal_navigation(page, "Architecture").click()
                page.get_by_label("Source logical module", exact=True).fill("alpha.a")
                page.get_by_label("Target logical module", exact=True).fill("alpha.b")
                page.get_by_role("button", name="Apply investigation", exact=True).click()
                assert started.wait(timeout=5)
                page.get_by_label("Target logical module", exact=True).fill("alpha.a")
                release.set()
                page.get_by_text("Controls changed. Apply investigation", exact=False).wait_for()
                page.wait_for_timeout(200)
                assert not page.get_by_role("button", name="Download JSON", exact=True).count()
            finally:
                release.set()
                browser.close()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
