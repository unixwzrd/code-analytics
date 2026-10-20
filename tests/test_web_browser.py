"""Real-browser regression gates; run with the optional browser tools installed."""

from __future__ import annotations

import threading

import pytest
from browser_navigation import reveal_navigation

from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


@pytest.mark.ui_smoke
def test_new_view_survives_delayed_previous_response(ordinary_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    server = create_server(ReportViewService(ordinary_report), token="browser-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=browser-test")
                page.locator(".cards").wait_for()
                # Hold a successfully received response body to reproduce a
                # response completing after the user has changed views.
                page.evaluate("""() => {
                    const original = window.fetch;
                    window.fetch = async (...args) => {
                        const response = await original(...args);
                        if (String(args[0]).startsWith('/api/modules')) {
                            const body = await response.json();
                            response.json = () => new Promise(resolve => {
                                window.releaseOldResponse = () => resolve(body);
                                document.body.dataset.oldResponseReady = 'true';
                            });
                        }
                        return response;
                    };
                }""")
                reveal_navigation(page, "Modules").click()
                playwright.expect(page.locator("body")).to_have_attribute(
                    "data-old-response-ready", "true"
                )
                page.get_by_role("button", name="Providers", exact=True).click()
                page.get_by_role("columnheader", name="Provider", exact=True).wait_for()
                page.evaluate("window.releaseOldResponse()")
                page.evaluate(
                    "() => new Promise(resolve => "
                    "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
                )
                assert page.get_by_role("columnheader", name="Provider", exact=True).count() == 1
                assert page.get_by_role("columnheader", name="Module", exact=True).count() == 0
                page.get_by_role("button", name="Diagnostics", exact=True).click()
                playwright.expect(page.locator("#title")).to_have_text("Diagnostics")
                module = next(item for item in ordinary_report.modules if item.symbols)
                identity = f"{module.name}:{module.symbols[0].qualified_name}"
                page.evaluate(
                    """async identity => {
                    const {evidenceViewer} = await import('/evidence.js');
                    const api = async (path, signal) => {
                        const response = await fetch(path, {signal, headers: {
                            'X-Code-Analysis-Token': 'browser-test'
                        }});
                        return response.json();
                    };
                    const escape = value => String(value).replaceAll('&', '&amp;')
                        .replaceAll('<', '&lt;').replaceAll('>', '&gt;');
                    await evidenceViewer(api, escape)('symbol', identity);
                }""",
                    identity,
                )
                assert page.get_by_role("dialog").is_visible()
                assert "Source completeness" in page.locator("#evidence-content").inner_text()
                assert "undefined" not in page.locator("#evidence-content").inner_text()
                page.keyboard.press("Escape")
                assert not page.get_by_role("dialog").is_visible()
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
