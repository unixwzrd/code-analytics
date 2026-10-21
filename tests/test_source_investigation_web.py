"""Source-focused journeys qualify actual editor geometry and saved evidence."""

from __future__ import annotations

from http.client import HTTPConnection

import pytest
from browser_navigation import reveal_navigation
from test_code_browser_web import TOKEN, get
from test_graph_handoff_browser import served

from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.service import scan
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


@pytest.fixture
def investigation(tmp_path):
    source = tmp_path / "investigation.py"
    text = "def helper(value):\n    return value + 1\n\ndef caller(value):\n"
    text += "".join(f"    if value == {i}:\n        value += 1\n" for i in range(20))
    text += "    return helper(value)\n"
    source.write_text(text)
    service = ReportViewService(scan(discover_context(source, AnalysisConfig(test_roots=()))))
    assert service.primary.findings
    return source, service


def test_source_investigation_routes_are_bound_authenticated_and_saved_only(
    investigation, monkeypatch
):
    source, service = investigation
    sha = service.bootstrap().reports[0].report_sha256
    monkeypatch.setattr("code_analysis.service.scan", lambda *args: pytest.fail("source rescan"))
    with served(create_server(service, token=TOKEN)) as server:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            base = {"report_sha256": sha, "revision": 3, "path": source.name}
            for route in ("findings", "relationships"):
                params = (
                    base if route == "findings" else {**base, "identity": "investigation:caller"}
                )
                assert get(connection, "/api/code-browser/" + route, params, token="bad")[0] == 403
                status, result = get(connection, "/api/code-browser/" + route, params)
                assert status == 200 and result["revision"] == 3 and result["records"]
                assert (
                    get(connection, "/api/code-browser/" + route, {**params, "extra": 1})[0] == 400
                )
                assert (
                    get(
                        connection,
                        "/api/code-browser/" + route,
                        {**params, "report_sha256": "0" * 64},
                    )[0]
                    == 400
                )
                assert (
                    get(
                        connection,
                        "/api/code-browser/" + route,
                        {**params, "path": "../outside.py"},
                    )[0]
                    == 400
                )
        finally:
            connection.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
@pytest.mark.parametrize("width", (1280, 1440))
@pytest.mark.parametrize("theme", ("light", "dark"))
def test_wide_source_finding_markers_drafts_and_relationship_navigation(
    investigation, engine, width, theme
):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = investigation
    original = source.read_bytes()
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000}, color_scheme=theme)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            reveal_navigation(page, "Modules").click()
            page.locator('[data-direct-source="true"]').click()
            playwright.expect(page.locator("#evidence-dialog")).not_to_be_visible()
            page.locator('[data-browse-identity="investigation:caller"]').click()
            page.locator('.code-browser-line[data-severity="warning"]').first.wait_for()
            playwright.expect(
                page.get_by_role("region", name="Recorded source findings")
            ).to_have_count(1)
            playwright.expect(page.locator(".source-findings")).to_contain_text(
                service.primary.findings[0].rule_id
            )
            playwright.expect(page.locator(".source-relationships")).to_contain_text(
                "Selected function → callees"
            )
            page.get_by_role("button", name="Edit draft from verified original source").click()
            editor = page.locator(".code-drafts-editor .lui-editor")
            textarea = editor.locator("textarea")
            textarea.wait_for()
            page.wait_for_function("document.fonts.status === 'loaded'")
            measured = textarea.evaluate("""node => {
                const style = getComputedStyle(node), measure = document.createElement('span');
                measure.style.cssText = 'position:absolute;visibility:hidden;white-space:pre';
                measure.style.font = style.font; measure.style.letterSpacing = style.letterSpacing;
                measure.textContent = '0'.repeat(80); document.body.append(measure);
                const cells = measure.getBoundingClientRect().width; measure.remove();
                const usable = node.clientWidth - parseFloat(style.paddingLeft)
                  - parseFloat(style.paddingRight);
                return {cells, usable, wrap: node.wrap};
            }""")
            assert measured["usable"] >= measured["cells"], measured
            assert measured["wrap"] == "off"
            playwright.expect(
                editor.get_by_role("listbox", name="Source annotations")
            ).to_contain_text("warning")
            baseline = textarea.input_value()
            textarea.fill(baseline + "# unanalysed draft\n")
            playwright.expect(editor.get_by_role("option")).to_have_count(0)
            playwright.expect(page.locator(".code-drafts-status")).to_contain_text("Draft")
            editor.get_by_role("button", name="Revert", exact=True).click()
            playwright.expect(editor.get_by_role("option")).not_to_have_count(0)
            assert textarea.input_value() == baseline
            page.get_by_role("button", name="Expand code", exact=True).click()
            playwright.expect(page.locator(".code-browser-map")).not_to_be_visible()
            page.get_by_role("button", name="Restore layout", exact=True).click()
            playwright.expect(page.locator(".code-browser-map")).to_be_visible()
            assert source.read_bytes() == original and not errors
        finally:
            browser.close()
