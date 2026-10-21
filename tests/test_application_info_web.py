"""Privacy-reviewed issue links remain optional and independent of saved evidence."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from test_workflow_web import get, served


def test_application_info_endpoint_is_authenticated_and_non_mutating(tmp_path):
    before = tuple(tmp_path.iterdir())
    with served(None, tmp_path) as server:
        assert get(server, "/api/application-info", token="wrong")[0] == 403
        status, result = get(server, "/api/application-info")
        assert status == 200
        assert result == get(server, "/api/application-info")[1]
        assert set(result) == {"application_version", "os_family", "python_version"}
        assert get(server, "/api/application-info?path=/private")[0] == 400
    assert tuple(tmp_path.iterdir()) == before


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1440, 768])
def test_issue_summary_is_reviewed_bounded_and_does_not_change_workflow(tmp_path, engine, width):
    playwright = pytest.importorskip("playwright.sync_api")
    with served(None, tmp_path) as server, playwright.sync_playwright() as runtime:
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000})
            # Exercise the actual module and toolkit, independently of app bootstrap.
            page.route(
                "**/identity-test",
                lambda route: route.fulfill(
                    content_type="text/html",
                    body='<link rel="stylesheet" href="/localops/styles.css">'
                    '<h1 id="title" tabindex="-1">Investigation retained</h1>'
                    '<input id="retained-draft" value="unshared draft">',
                ),
            )
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/identity-test")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.evaluate("""async () => {
                  const {mountApplicationInfo} = await import('/application_info.js');
                  window.issueInfo = mountApplicationInfo({
                    api: async () => ({application_version: '0.2.0b2',
                      os_family: 'macOS', python_version: '3.12.13'}),
                    currentView: () => 'findings'});
                  await window.issueInfo.ready;
                }""")
            assert page.get_by_text("Code Analysis 0.2.0b2", exact=True).is_visible()
            page.get_by_role("button", name="Report an issue", exact=True).click()
            summary = page.get_by_label("Review or edit troubleshooting summary")
            assert summary.input_value().splitlines() == [
                "Application version: 0.2.0b2",
                "OS family: macOS",
                "Python version: 3.12.13",
                "Current view: findings",
            ]
            assert page.get_by_text("GitHub issues are public.", exact=False).is_visible()
            edited = 'Reviewed & corrected "details"? #no-secret'
            summary.fill(edited)
            link = page.get_by_role("link", name="Open GitHub bug report")
            parsed = urlparse(link.get_attribute("href"))
            assert parsed.netloc == "github.com"
            assert parsed.path == "/unixwzrd/code-analytics/issues/new"
            assert parse_qs(parsed.query) == {
                "template": ["bug_report.yml"],
                "environment": [edited],
            }
            assert link.get_attribute("rel") == "noopener noreferrer"
            assert link.get_attribute("target") == "_blank"
            summary.fill("界" * 500)
            assert "environment=" not in link.get_attribute("href")
            assert page.get_by_text("The summary is too long", exact=False).is_visible()
            summary.press("Escape")
            assert not page.locator("#application-issue-report").is_visible()
            assert page.get_by_role("button", name="Report an issue", exact=True).evaluate(
                "node => node === document.activeElement"
            )
            assert page.locator("#retained-draft").input_value() == "unshared draft"
            assert page.evaluate("localStorage.length + sessionStorage.length") == 0
            assert not errors
        finally:
            browser.close()


def test_identity_failure_does_not_prevent_manual_reporting(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with served(None, tmp_path) as server, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.route(
                "**/identity-test",
                lambda route: route.fulfill(
                    content_type="text/html", body='<h1 id="title">Investigation</h1>'
                ),
            )
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/identity-test")
            page.evaluate("""async () => {
                  const {mountApplicationInfo} = await import('/application_info.js');
                  const controls = mountApplicationInfo({api: async () => {throw Error('offline');},
                    currentView: () => 'overview'});
                  await controls.ready;
                }""")
            assert page.get_by_text("Code Analysis version unavailable", exact=True).is_visible()
            page.get_by_role("button", name="Report an issue", exact=True).click()
            assert page.get_by_text(
                "Running application details could not be loaded.", exact=False
            ).is_visible()
            assert page.get_by_role("link", name="Open GitHub bug report").is_visible()
        finally:
            browser.close()


def test_issue_template_contains_expected_review_fields():
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[1] / ".github/ISSUE_TEMPLATE/bug_report.yml"
    ).read_text()
    assert all(
        f"id: {field}" in template for field in ("steps", "expected", "actual", "environment")
    )
    assert "Issues are public" in template
