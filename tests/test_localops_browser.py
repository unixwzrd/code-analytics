"""Real-browser checks for LocalOps operator shell adoption."""

from __future__ import annotations

import pytest
from browser_navigation import reveal_navigation
from test_graph_handoff_browser import served

from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

TOKEN = "localops-browser"


def test_localops_table_preserves_order_and_evidence_after_column_changes(ordinary_report):
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(create_server(ReportViewService(ordinary_report), token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(_base_url(server))
            page.locator(".cards").wait_for()
            reveal_navigation(page, "Modules").click()
            table = page.locator('[data-localops-report-table="Modules"]')
            table.locator("tbody tr").first.wait_for()
            identities = table.locator("tbody [data-identity]").evaluate_all(
                "elements => elements.map(element => element.dataset.identity)"
            )
            assert identities
            table.locator("summary").click()
            table.get_by_role("checkbox", name="Show Language column", exact=True).uncheck()
            playwright.expect(
                table.get_by_role("columnheader", name="Language", exact=True)
            ).to_have_count(0)
            table.get_by_role("checkbox", name="Show Language column", exact=True).check()
            playwright.expect(
                table.get_by_role("columnheader", name="Language", exact=True)
            ).to_have_count(1)
            assert (
                table.locator("tbody [data-identity]").evaluate_all(
                    "elements => elements.map(element => element.dataset.identity)"
                )
                == identities
            )
            table.locator("tbody [data-identity]").first.click()
            playwright.expect(page.locator("#evidence-dialog")).not_to_be_visible()
            playwright.expect(page.locator(".code-browser-source")).to_be_visible()
            playwright.expect(page.locator(".code-browser-source")).to_contain_text(identities[0])
        finally:
            browser.close()


DOCUMENT_WIDTH_CONTAINED = """() =>
  document.documentElement.scrollWidth <= document.documentElement.clientWidth"""


def _base_url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}"


def _assert_kit_assets_loaded(page, requests: list[str]) -> None:
    assert any("/localops/styles.css" in url for url in requests)
    assert any("/localops/index.js" in url for url in requests)
    assert page.locator(".lui-shell").count() == 1
    assert page.evaluate("() => document.body.dataset.localopsShell === 'mounted'")


@pytest.mark.parametrize(
    ("theme", "color_scheme"),
    (
        ("dark", "dark"),
        ("light", "light"),
    ),
)
@pytest.mark.parametrize("width", (1280, 390))
def test_operator_shell_mounts_with_theme(
    ordinary_report, theme: str, color_scheme: str, width: int
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(create_server(ReportViewService(ordinary_report), token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(
                viewport={"width": width, "height": 900},
                color_scheme=color_scheme,
            )
            errors: list[str] = []
            requests: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            page.goto(_base_url(server))
            page.evaluate(
                """theme => { document.documentElement.dataset.theme = theme; }""",
                theme,
            )
            page.locator(".cards").wait_for()
            _assert_kit_assets_loaded(page, requests)
            assert page.locator('nav[aria-label="Report views"] button.nav').count() == 18
            assert page.evaluate(DOCUMENT_WIDTH_CONTAINED)
            assert not errors
        finally:
            browser.close()


def test_mobile_accepted_navigation_closes_drawer_and_focuses_title(
    ordinary_report,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(create_server(ReportViewService(ordinary_report), token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(_base_url(server))
            page.locator(".cards").wait_for()
            toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
            toggle.click()
            sidebar = page.locator(".lui-shell__sidebar--open")
            playwright.expect(sidebar).to_have_count(1)
            modules = reveal_navigation(page, "Modules")
            modules.click()
            playwright.expect(page.locator("#title")).to_have_text("Modules")
            playwright.expect(sidebar).to_have_count(0)
            assert page.evaluate(
                "() => document.activeElement && document.activeElement.id === 'title'"
            )
            assert not errors
        finally:
            browser.close()


def test_mobile_drawer_escape_closes_and_restores_toggle_focus(ordinary_report) -> None:
    """The consumer invokes the kit's deliberate closure API on Escape."""
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(create_server(ReportViewService(ordinary_report), token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(_base_url(server))
            page.locator(".cards").wait_for()
            toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
            toggle.click()
            playwright.expect(page.locator(".lui-shell__sidebar--open")).to_have_count(1)
            page.keyboard.press("Escape")
            playwright.expect(page.locator(".lui-shell__sidebar--open")).to_have_count(0)
            playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
            playwright.expect(toggle).to_be_focused()
        finally:
            browser.close()


def test_desktop_navigation_keeps_button_focus(ordinary_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(create_server(ReportViewService(ordinary_report), token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(_base_url(server))
            page.locator(".cards").wait_for()
            modules = reveal_navigation(page, "Modules")
            modules.focus()
            page.keyboard.press("Enter")
            playwright.expect(page.locator("#title")).to_have_text("Modules")
            assert modules.evaluate("element => element === document.activeElement")
        finally:
            browser.close()


def test_domain_views_still_render_core_markers(ordinary_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(create_server(ReportViewService(ordinary_report), token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(_base_url(server))
            page.locator(".cards").wait_for()
            overview_html = page.locator("#content").inner_html()
            page.get_by_role("button", name="Collect / plan", exact=True).click()
            playwright.expect(page.locator("#workflow-notice")).to_contain_text(
                "Configure a collection root"
            )
            playwright.expect(page.locator("#title")).to_have_text("Overview")
            collection_html = page.locator("#workflow-notice").inner_html()
            reveal_navigation(page, "Candidates").click()
            playwright.expect(page.locator("#title")).to_have_text("Candidates")
            candidates_html = page.locator("#content").inner_html()
            assert overview_html.strip()
            assert collection_html.strip()
            assert candidates_html.strip()
            assert not errors
        finally:
            browser.close()
