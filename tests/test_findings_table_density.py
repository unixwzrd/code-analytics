"""Chromium regressions for Findings table row density and long text disclosure."""

from __future__ import annotations

import threading

import pytest
from browser_navigation import reveal_navigation
from test_graph_handoff_browser import served

from code_analysis.config import AnalysisConfig
from code_analysis.finding_suppression_models import AcceptedFindingSuppression
from code_analysis.finding_suppressions import apply_finding_suppressions
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

TOKEN = "findings-density"
PROBE_TITLE = "Density probe finding"
INLINE_TITLE = "Inline probe finding"
INLINE_EXPLANATION = "W" * 72
LONG_EXPLANATION = (
    "Off-screen explanation probe: " + "alpha " * 40 + ' <tag attr="x">&amp;quoted'
).strip()
LONG_RATIONALE = ("Suppression rationale probe: " + "beta " * 35 + " <evil>&'quoted").strip()
FINDING_COLUMNS = (
    "Severity",
    "Confidence",
    "Finding",
    "Suppression",
    "Explanation",
    "Language",
    "Module",
    "Rule",
    "Category",
)
MAX_COLLAPSED_ROW_HEIGHT = 80

DOCUMENT_WIDTH_CONTAINED = """() =>
  document.documentElement.scrollWidth <= document.documentElement.clientWidth"""


@pytest.fixture
def density_report(ordinary_report):
    identity = ordinary_report.findings[0].finding_id
    finding = ordinary_report.findings[0].model_copy(
        update={"title": PROBE_TITLE, "explanation": LONG_EXPLANATION}
    )
    report = ordinary_report.model_copy(
        update={"findings": (finding, *ordinary_report.findings[1:])}
    )
    config = AnalysisConfig(
        accepted_suppressions=(
            AcceptedFindingSuppression(
                finding_id=identity,
                rationale=LONG_RATIONALE,
                expires_on="2026-09-23",
            ),
        ),
        suppression_evaluation_date="2026-09-22",
    )
    return apply_finding_suppressions(report, config)


def _open_findings(page) -> None:
    toggle = page.locator(".lui-shell__menu")
    if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
        toggle.click()
    reveal_navigation(page, "Findings").click()
    for name in FINDING_COLUMNS:
        page.get_by_role("columnheader", name=name, exact=True).wait_for()


@pytest.mark.parametrize(("width", "height"), ((768, 1024), (390, 844)))
def test_findings_table_keeps_compact_rows_and_discloses_long_text(
    density_report, width: int, height: int
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    server = create_server(ReportViewService(density_report), token=TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": width, "height": height})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
                page.locator(".cards").wait_for()
                _open_findings(page)
                assert page.locator(".findings-table").count() == 1
                assert page.evaluate(DOCUMENT_WIDTH_CONTAINED)

                probe_row = page.locator("tbody tr").filter(has_text=PROBE_TITLE)
                probe_row.wait_for()
                collapsed_height = probe_row.evaluate("row => row.getBoundingClientRect().height")
                assert collapsed_height <= MAX_COLLAPSED_ROW_HEIGHT

                explanation = probe_row.locator("td").nth(4).locator(".findings-text-details")
                rationale = probe_row.locator(
                    ".findings-suppression-rationale .findings-text-details"
                )
                assert explanation.count() == 1
                assert rationale.count() == 1
                assert not explanation.evaluate("el => el.open")
                assert not rationale.evaluate("el => el.open")

                table_wrap = page.locator(".findings-table .lui-data-table__scroll")
                assert table_wrap.evaluate("wrap => wrap.scrollWidth > wrap.clientWidth")

                explanation.locator("summary").focus()
                page.keyboard.press("Enter")
                playwright.expect(explanation).to_have_attribute("open", "")
                assert explanation.locator(".findings-text-full").inner_text() == LONG_EXPLANATION
                explanation_html = explanation.locator(".findings-text-full").inner_html()
                assert '&lt;tag attr="x"&gt;' in explanation_html
                assert "<tag" not in explanation_html
                assert "&amp;amp;quoted" in explanation_html

                rationale.locator("summary").focus()
                page.keyboard.press(" ")
                playwright.expect(rationale).to_have_attribute("open", "")
                assert rationale.locator(".findings-text-full").inner_text() == LONG_RATIONALE
                rationale_html = rationale.locator(".findings-text-full").inner_html()
                assert "&lt;evil&gt;" in rationale_html
                assert "<evil>" not in rationale_html
                assert "&amp;'quoted" in rationale_html
                assert page.evaluate(DOCUMENT_WIDTH_CONTAINED)

                held = []
                page.route("**/api/evidence?*", lambda route: held.append(route))
                page.get_by_role("button", name=PROBE_TITLE, exact=True).click()
                page.get_by_role("dialog").wait_for()
                content = page.locator("#evidence-content")
                playwright.expect(content).to_have_text("Loading evidence…")
                assert len(held) == 1
                held[0].continue_()
                playwright.expect(content).to_contain_text("Source completeness")
                page.keyboard.press("Escape")
                assert not page.get_by_role("dialog").is_visible()
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("width", (768, 390))
def test_inline_finding_text_is_not_clipped_without_disclosure(ordinary_report, width):
    playwright = pytest.importorskip("playwright.sync_api")
    finding = ordinary_report.findings[0].model_copy(
        update={"title": INLINE_TITLE, "explanation": INLINE_EXPLANATION}
    )
    report = ordinary_report.model_copy(
        update={"findings": (finding, *ordinary_report.findings[1:])}
    )
    with (
        served(create_server(ReportViewService(report), token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            _open_findings(page)
            inline = (
                page.locator("tbody tr")
                .filter(has_text=INLINE_TITLE)
                .locator(".findings-text-inline")
            )
            assert inline.inner_text() == INLINE_EXPLANATION
            assert inline.evaluate("el => el.scrollWidth <= el.clientWidth")
            assert page.evaluate(DOCUMENT_WIDTH_CONTAINED)
        finally:
            browser.close()
