"""Task-sized controls, wide saved-evidence results, and unchanged request contracts."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from browser_navigation import reveal_navigation

from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

OUTPUT = Path(__file__).resolve().parents[1] / "output" / "playwright" / "20261019-investigation"


def _assert_task_geometry(page, form) -> None:
    box = form.bounding_box()
    assert box is not None and box["width"] <= 768
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for control in form.locator("input:visible, select:visible").all():
        bounds = control.bounding_box()
        assert bounds is not None and bounds["width"] <= 672
        assert bounds["x"] >= box["x"]
        assert bounds["x"] + bounds["width"] <= box["x"] + box["width"] + 1
    button = form.locator('button[type="submit"]')
    assert button.bounding_box()["x"] - box["x"] <= 25


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
@pytest.mark.parametrize("width", (1920, 1024))
@pytest.mark.parametrize("theme", ("light", "dark"))
def test_investigation_controls_precede_wide_results_and_preserve_bounds(
    ordinary_report, engine: str, width: int, theme: str
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    server = create_server(ReportViewService(ordinary_report), token="investigation-layout")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = getattr(runtime, engine).launch()
            try:
                page = browser.new_page(
                    viewport={"width": width, "height": 1100}, color_scheme=theme
                )
                errors: list[str] = []
                requests: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: requests.append(request.url))
                page.goto(
                    f"http://127.0.0.1:{server.server_address[1]}/?token=investigation-layout"
                )
                reveal_navigation(page, "Architecture").click()
                form = page.locator("#dependency-controls")
                form.wait_for()
                graph = page.locator("#content > .graph-wrap")
                graph.wait_for()
                _assert_task_geometry(page, form)
                assert (
                    form.bounding_box()["y"] + form.bounding_box()["height"]
                    < graph.bounding_box()["y"]
                )
                if width == 1920:
                    assert graph.bounding_box()["width"] > form.bounding_box()["width"]
                advanced = form.locator("details.lui-disclosure")
                assert not advanced.evaluate("node => node.open")
                assert form.locator('[name="max_nodes"]').is_hidden()
                assert (
                    "500 nodes / 5000 edges"
                    in page.locator("#dependency-bounds-summary").inner_text()
                )
                before = len(requests)
                advanced.locator("summary").focus()
                page.keyboard.press("Enter")
                assert form.locator('[name="max_nodes"]').is_visible()
                assert len(requests) == before
                form.locator('[name="graph_max_nodes"]').fill("0")
                advanced.locator("summary").click()
                page.get_by_role("button", name="Apply investigation", exact=True).click()
                assert advanced.evaluate("node => node.open")
                assert not any("/api/dependencies?" in url for url in requests)
                form.locator('[name="graph_max_nodes"]').fill("2")
                assert "Graph: 2 nodes" in page.locator("#dependency-bounds-summary").inner_text()
                advanced.locator("summary").click()
                page.get_by_label("Source logical module", exact=True).fill("alpha.a")
                page.get_by_label("Target logical module", exact=True).fill("alpha.b")
                page.get_by_role("button", name="Apply investigation", exact=True).click()
                page.get_by_role("heading", name="Retained path", exact=True).wait_for()
                with page.expect_download() as download:
                    page.get_by_role("button", name="Download JSON-COMPACT", exact=True).click()
                path = parse_qs(
                    urlsplit(
                        next(
                            url
                            for url in reversed(requests)
                            if "/api/dependencies/download?" in url
                        )
                    ).query
                )
                assert path["graph_max_nodes"] == ["2"]
                assert path["max_nodes"] == ["500"]
                dependency_graph = json.loads(download.value.path().read_text())
                assert {node["node_id"] for node in dependency_graph["nodes"]} == {
                    "alpha.a",
                    "alpha.b",
                }
                page.get_by_label("Dependency mode").select_option("component")
                assert page.get_by_label("Target logical module", exact=True).is_disabled()
                assert "source module only" in page.locator("#dependency-target-note").inner_text()
                assert not page.locator("#dependency-result [data-dependency-format]").count()

                reveal_navigation(page, "Explore evidence").click()
                form = page.locator("#projection-controls")
                page.get_by_role("button", name="Download TSV", exact=True).wait_for()
                _assert_task_geometry(page, form)
                advanced = form.locator("details.lui-disclosure")
                assert not advanced.evaluate("node => node.open")
                assert form.locator('[name="max_records"]').is_hidden()
                assert (
                    "100 records; 50 nodes / 100 edges"
                    in page.locator("#projection-bounds-summary").inner_text()
                )
                advanced.locator("summary").click()
                form.locator('[name="max_records"]').fill("0")
                advanced.locator("summary").click()
                count = len(requests)
                page.get_by_role("button", name="Show evidence", exact=True).click()
                assert advanced.evaluate("node => node.open")
                assert len(requests) == count
                form.locator('[name="max_records"]').fill("1")
                assert "1 records" in page.locator("#projection-bounds-summary").inner_text()
                assert not page.get_by_role("button", name="Download JSON", exact=True).count()
                advanced.locator("summary").click()
                page.get_by_role("button", name="Show evidence", exact=True).click()
                page.get_by_role("button", name="Download JSON", exact=True).wait_for()
                with page.expect_download() as download:
                    page.get_by_role("button", name="Download JSON", exact=True).click()
                projection = json.loads(download.value.path().read_text())
                assert projection["record_count"] == 1 and projection["truncated"]
                params = parse_qs(
                    urlsplit(
                        next(url for url in reversed(requests) if "/api/download?" in url)
                    ).query
                )
                assert params["max_records"] == ["1"]
                assert params["max_nodes"] == ["50"]
                assert params["max_edges"] == ["100"]
                # Applicable policy controls stay explicit; unrelated filters are absent.
                page.get_by_label("Evidence view", exact=True).select_option("candidate-worklist")
                assert page.locator("#candidate-projection-controls").is_visible()
                assert page.locator("#finding-projection-controls").is_hidden()
                assert page.get_by_label("Exact selection ID", exact=True).is_disabled()
                assert (
                    "Depth/collapse: not used by candidate worklist."
                    in page.locator("#projection-bounds-summary").inner_text()
                )
                page.get_by_label("Evidence view", exact=True).select_option("subject-findings")
                assert page.locator("#candidate-projection-controls").is_hidden()
                assert page.locator("#finding-projection-controls").is_visible()
                assert form.locator('[name="language"]').is_disabled()
                assert not errors, errors
                assert not any("/api/collection/" in url for url in requests)
                OUTPUT.mkdir(parents=True, exist_ok=True)
                page.get_by_label("Evidence view", exact=True).select_option("modules")
                assert (
                    "Depth: default; collapse: none."
                    in page.locator("#projection-bounds-summary").inner_text()
                )
                page.get_by_role("button", name="Show evidence", exact=True).click()
                page.get_by_role("button", name="Download TSV", exact=True).wait_for()
                page.locator("#title").scroll_into_view_if_needed()
                page.screenshot(path=str(OUTPUT / f"{engine}-{width}-{theme}-explore.png"))
                reveal_navigation(page, "Architecture").click()
                page.locator("#dependency-controls").wait_for()
                page.locator("#title").scroll_into_view_if_needed()
                page.screenshot(path=str(OUTPUT / f"{engine}-{width}-{theme}-architecture.png"))
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
