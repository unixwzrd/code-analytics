"""Browser acceptance for saved invocation evidence, without target source."""

from pathlib import Path

import pytest
from test_graph_handoff_browser import served
from test_static_call_validation import _occurrence, _synthetic_shell, _with_static_calls

from code_analysis.projections import ProjectionName
from code_analysis.static_graph_browse import StaticGraphRequest
from code_analysis.ui_handoff import WebHandoffSelection
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


def test_browser_call_site_graph_keyboard_navigation():
    playwright = pytest.importorskip("playwright.sync_api")
    call = _occurrence(callee_spelling=None)
    report = _with_static_calls(_synthetic_shell(), occurrences=(call,))
    service = ReportViewService(report)
    handoff = WebHandoffSelection(
        report_sha256=service.bootstrap().reports[0].report_sha256,
        kind="projection",
        graph_request=StaticGraphRequest(name=ProjectionName.STATIC_CALL_GRAPH),
    )
    with (
        served(create_server(service, token="call-sites")) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            base = f"http://127.0.0.1:{server.server_port}/?token=call-sites"
            page.goto(handoff.url(base))
            graph = page.locator("#projection-result svg")
            graph.wait_for()
            node = graph.locator(f'[data-projection-node="{call.occurrence_id}"]')
            node.focus()
            page.keyboard.press("Enter")
            dialog = page.get_by_role("dialog")
            dialog.wait_for()
            playwright.expect(dialog).to_contain_text("Dynamic callee")
            playwright.expect(dialog).to_contain_text("static-call-site")
            assert not errors
            output = Path(__file__).resolve().parents[1] / "output" / "playwright"
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / "20260922-static-call-navigation.png"))
            page.keyboard.press("Escape")
            assert not dialog.is_visible()
            assert node.evaluate("element => element === document.activeElement")
        finally:
            browser.close()
