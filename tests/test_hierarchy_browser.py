"""Real-browser checks for the saved package and module hierarchy."""

from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from browser_navigation import reveal_navigation

from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.report_models import AnalysisReport
from code_analysis.service import scan
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

FIXTURES = Path(__file__).parent / "fixtures"


def _source_free_report(tmp_path: Path, fixture: str) -> AnalysisReport:
    source = tmp_path / "source"
    shutil.copytree(FIXTURES / fixture, source)
    report = scan(discover_context(source, AnalysisConfig()))
    # The browser receives only the retained report. This proves hierarchy
    # presentation does not rediscover the analyzed tree.
    shutil.rmtree(source)
    return AnalysisReport.model_validate_json(report.model_dump_json())


@contextmanager
def _server(report: AnalysisReport, token: str) -> Iterator[str]:
    server = create_server(ReportViewService(report), token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _open_hierarchy(page) -> None:
    page.locator(".cards").wait_for()
    toggle = page.locator(".lui-shell__menu")
    if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
        toggle.click()
    reveal_navigation(page, "Explore evidence").click()
    page.get_by_label("Evidence view", exact=True).select_option("hierarchy")
    page.get_by_role("button", name="Show evidence", exact=True).click()
    page.locator(".hierarchy-tree").wait_for()


def test_hierarchy_is_expandable_and_parent_module_opens_saved_navigation(tmp_path: Path) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report = _source_free_report(tmp_path, "ordinary")
    with _server(report, "hierarchy-browser") as url, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            node_requests: list[str] = []
            page.on(
                "request",
                lambda request: (
                    node_requests.append(request.url)
                    if "/api/projection-node?" in request.url
                    else None
                ),
            )
            page.goto(url)
            _open_hierarchy(page)

            assert page.locator("#projection-result .report-text").count() == 0
            root = page.locator(".hierarchy-branch").first
            playwright.expect(root).to_have_attribute("open", "")
            playwright.expect(root.locator("summary")).to_contain_text("alpha")
            root.locator("summary").click()
            playwright.expect(root).not_to_have_attribute("open", "")
            root.locator("summary").click()
            playwright.expect(root).to_have_attribute("open", "")
            assert node_requests == []

            parent_module = root.locator('[data-projection-node="alpha"]')
            playwright.expect(parent_module).to_contain_text("Open module evidence")
            playwright.expect(parent_module).to_contain_text("alpha")
            parent_module.focus()
            page.keyboard.press("Enter")
            page.get_by_role("dialog").wait_for()
            playwright.expect(page.locator("#evidence-content")).to_contain_text(
                "Symbols and source"
            )
            playwright.expect(page.locator("#evidence-content")).to_contain_text(
                "alpha/__init__.py"
            )
            assert len(node_requests) == 1
            assert page.get_by_label("Exact selection ID").input_value() == ""
        finally:
            browser.close()


def test_namespace_packages_disclose_children_and_space_opens_module(tmp_path: Path) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report = _source_free_report(tmp_path, "namespace")
    with _server(report, "hierarchy-namespace") as url, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            node_requests: list[str] = []
            page.on(
                "request",
                lambda request: (
                    node_requests.append(request.url)
                    if "/api/projection-node?" in request.url
                    else None
                ),
            )
            page.goto(url)
            _open_hierarchy(page)

            branches = page.locator(".hierarchy-branch")
            assert branches.count() == 2
            assert [
                summary.strip() for summary in branches.locator("summary").all_inner_texts()
            ] == ["acme\npackage", "tools\npackage"]
            branches.nth(1).locator("summary").click()
            playwright.expect(branches.nth(1)).to_have_attribute("open", "")
            assert node_requests == []
            module = page.locator('[data-projection-node="acme.tools.worker"]')
            module.focus()
            page.keyboard.press(" ")
            page.get_by_role("dialog").wait_for()
            playwright.expect(page.locator("#evidence-title")).to_have_text("acme.tools.worker")
            playwright.expect(page.locator("#evidence-content")).to_contain_text(
                "src/acme/tools/worker.py"
            )
            assert len(node_requests) == 1
        finally:
            browser.close()


def test_hierarchy_remains_usable_at_mobile_width(tmp_path: Path) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report = _source_free_report(tmp_path, "namespace")
    with _server(report, "hierarchy-mobile") as url, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(url)
            _open_hierarchy(page)
            nested = page.locator(".hierarchy-branch").nth(1)
            nested.locator("summary").click()
            module = page.locator('[data-projection-node="acme.tools.worker"]')
            playwright.expect(module).to_be_visible()
            bounds = module.bounding_box()
            assert bounds is not None
            assert bounds["x"] >= 0
            assert bounds["x"] + bounds["width"] <= 390
            assert page.locator(".hierarchy-wrap").evaluate(
                "element => element.scrollWidth <= element.clientWidth"
            )
        finally:
            browser.close()
