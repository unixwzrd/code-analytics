"""Chromium acceptance for responsive layout on core saved-report views."""

from __future__ import annotations

from pathlib import Path

import pytest
from browser_navigation import reveal_navigation
from test_graph_handoff_browser import served
from test_static_call_validation import _synthetic_shell

from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

TOKEN = "ui-responsive-acceptance"
OUTPUT = Path(__file__).resolve().parents[1] / "output" / "playwright"

DOCUMENT_WIDTH_CONTAINED = """() =>
  document.documentElement.scrollWidth <= document.documentElement.clientWidth"""


@pytest.fixture
def synthetic_service() -> ReportViewService:
    return ReportViewService(_synthetic_shell())


def _base_url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}"


def _assert_document_width_contained(page) -> None:
    assert page.evaluate(DOCUMENT_WIDTH_CONTAINED)


def _open_navigation(page) -> None:
    toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
    if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
        toggle.click()


def _open_overview(page) -> None:
    _open_navigation(page)
    page.get_by_role("button", name="Overview", exact=True).click()
    page.locator(".cards").wait_for()
    _assert_document_width_contained(page)


def _open_modules(page) -> None:
    _open_navigation(page)
    reveal_navigation(page, "Modules").click()
    page.get_by_role("columnheader", name="Module", exact=True).wait_for()
    _assert_document_width_contained(page)


def _open_explore_symbol_graph(page) -> None:
    _open_navigation(page)
    reveal_navigation(page, "Explore evidence").click()
    page.get_by_label("Evidence view", exact=True).select_option("symbol-graph")
    page.get_by_role("button", name="Show evidence", exact=True).click()
    page.locator("#projection-result .graph-wrap svg").wait_for()
    _assert_document_width_contained(page)


@pytest.mark.ui_smoke
@pytest.mark.parametrize(
    ("engine", "width"),
    (
        ("chromium", 1440),
        ("webkit", 1440),
        ("chromium", 1024),
        ("webkit", 1024),
        ("chromium", 768),
        ("chromium", 390),
    ),
)
def test_every_primary_view_is_keyboard_reachable_without_collection(
    ordinary_report, engine, width
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(create_server(ReportViewService(ordinary_report), token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 900})
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            page.goto(_base_url(server))
            page.locator(".cards").wait_for()
            navigation = page.locator('nav[aria-label="Report views"] button')
            views = navigation.evaluate_all("buttons => buttons.map(b => b.dataset.view)")
            assert len(views) == len(set(views)) == 18
            for view in views:
                _open_navigation(page)
                button = reveal_navigation(
                    page, page.locator(f'nav button[data-view="{view}"]').text_content()
                )
                assert button.is_enabled(), view
                purpose = button.get_attribute("title")
                assert purpose and purpose.strip(), view
                readiness = button.get_attribute("data-readiness")
                assert readiness in ("available", "partial", "missing", "failed"), view
                hint_id = button.get_attribute("aria-describedby")
                assert hint_id, view
                reason = (
                    page.locator(f"#{hint_id}")
                    .inner_text()
                    .removesuffix(" Activate for prerequisites and corrective actions.")
                )
                assert reason.strip(), view
                button.focus()
                page.keyboard.press("Enter")
                if readiness in ("missing", "failed"):
                    notice = page.locator("#workflow-notice")
                    playwright.expect(notice).to_be_visible()
                    playwright.expect(notice).to_be_focused()
                    playwright.expect(notice.locator("strong")).to_contain_text(readiness)
                    playwright.expect(notice.locator("p")).to_have_text(reason)
                    assert notice.get_by_role("button", name="Go to ").count() >= 1
                    playwright.expect(notice.locator("button").first).to_be_enabled()
                    _assert_document_width_contained(page)
                    continue
                expected = {"collection": "Collection setup", "browse": "Browse code"}.get(
                    view, view.capitalize()
                )
                playwright.expect(page.locator("#title")).to_have_text(expected)
                playwright.expect(page.locator("#content .loading")).to_have_count(0)
                playwright.expect(button).to_have_class("nav active")
                playwright.expect(page.locator("#view-description")).to_have_text(purpose)
                assert page.locator("#content").inner_text().strip(), view
                assert not page.locator("#content h2").filter(has_text="Unable to").count(), view
                guidance = page.get_by_role("region", name="View guidance", exact=True)
                summary = guidance.locator("summary").filter(has_text="How to use this view")
                playwright.expect(summary).to_be_visible()
                summary.focus()
                page.keyboard.press("Enter")
                playwright.expect(guidance.locator("details")).to_have_attribute("open", "")
                assert guidance.locator("p").first.inner_text().strip(), view
                playwright.expect(guidance.locator("p").last).to_have_text(reason)
                guide = guidance.get_by_role("link", name="Read the guide", exact=True)
                playwright.expect(guide).to_be_visible()
                assert guide.get_attribute("href").startswith("/help/"), view
                _assert_document_width_contained(page)
            assert not errors
            assert not any(
                "/api/collection/launch" in url or "/api/source?" in url for url in requests
            )
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("viewport_name", "width", "height"),
    (
        ("desktop", 1280, 900),
        ("tablet", 768, 1024),
        ("mobile", 390, 844),
    ),
)
def test_primary_views_keep_document_width_contained(
    synthetic_service: ReportViewService,
    viewport_name: str,
    width: int,
    height: int,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(create_server(synthetic_service, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(_base_url(server))
            page.locator(".cards").wait_for()
            _assert_document_width_contained(page)
            _open_modules(page)
            _open_explore_symbol_graph(page)
            _open_overview(page)
            assert not errors
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(OUTPUT / f"20260922-ui-responsive-{viewport_name}-{width}.png"),
                full_page=True,
            )
        finally:
            browser.close()


@pytest.mark.parametrize(
    ("viewport_name", "width", "height"),
    (
        ("desktop", 1280, 900),
        ("mobile", 390, 844),
    ),
)
def test_saved_evidence_dialog_keyboard_closes_and_restores_focus(
    synthetic_service: ReportViewService,
    viewport_name: str,
    width: int,
    height: int,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(create_server(synthetic_service, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(_base_url(server))
            page.locator(".cards").wait_for()

            _open_navigation(page)
            reveal_navigation(page, "Modules").click()
            page.get_by_role("columnheader", name="Module", exact=True).wait_for()
            module_button = page.get_by_role("button", name="visible", exact=True)
            module_button.focus()
            page.keyboard.press("Enter")
            dialog = page.get_by_role("dialog")
            dialog.wait_for()
            playwright.expect(page.locator("#evidence-content")).to_contain_text(
                "Symbols and source"
            )
            page.keyboard.press("Escape")
            assert not dialog.is_visible()
            assert module_button.evaluate("element => element === document.activeElement")

            _open_explore_symbol_graph(page)
            node = page.locator('#projection-result [data-projection-node="visible:visible"]')
            node.wait_for()
            node.focus()
            page.keyboard.press("Enter")
            dialog.wait_for()
            playwright.expect(page.locator("#evidence-content")).to_contain_text(
                "Symbols and source"
            )
            page.keyboard.press("Escape")
            assert not dialog.is_visible()
            assert node.evaluate("element => element === document.activeElement")
            assert not errors
        finally:
            browser.close()
