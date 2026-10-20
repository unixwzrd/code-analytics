"""Finding investigations return to session-only, canonically bound context."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from browser_navigation import reveal_navigation
from test_code_browser_web import TOKEN
from test_code_browser_web import browser_source as _browser_source
from test_graph_handoff_browser import served

from code_analysis.models import Completeness, Confidence, Evidence, SourceSpan
from code_analysis.report_models import Finding
from code_analysis.ui_handoff import WebHandoffSelection
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

browser_source = _browser_source


@pytest.fixture
def investigation(browser_source, monkeypatch):
    source, original = browser_source
    evidence = Evidence(
        span=SourceSpan(path=source.name, start_line=2, end_line=2, start_column=0, end_column=0),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        provider="synthetic",
        observation="Saved synthetic source evidence",
        confidence=Confidence.EXACT,
    )
    findings = tuple(
        Finding(
            finding_id=f"return-{index:03}",
            rule_id="synthetic-return",
            category="complexity",
            severity="warning",
            title=f"Return observation {index:03}",
            explanation="Synthetic saved evidence for return navigation.",
            module=original.primary.modules[0].name,
            evidence=(evidence,),
            confidence=Confidence.EXACT,
            completeness=Completeness.COMPLETE,
        )
        for index in range(61)
    )
    service = ReportViewService(original.primary.model_copy(update={"findings": findings}))

    def forbidden(*args, **kwargs):
        pytest.fail("investigation navigation must not scan or collect source")

    monkeypatch.setattr("code_analysis.service.scan", forbidden)
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("chromium unavailable; no automatic installation")
        browser = runtime.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append((request.method, request.url)))
        page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
        page.locator(".cards").wait_for()
        try:
            yield page, playwright, service, source, requests
            assert not errors
            assert all(method == "GET" for method, _ in requests)
            assert not any("/api/collection/start" in url for _, url in requests)
        finally:
            browser.close()


def open_finding(page):
    reveal_navigation(page, "Findings").click()
    page.locator('#content [data-kind="finding"]').first.wait_for()
    page.locator('#content [data-kind="finding"]').first.click()
    page.get_by_role("button", name="Browse code at this source", exact=True).wait_for()


def browse_source(page):
    page.get_by_role("button", name="Browse code at this source", exact=True).click()
    page.locator("#return-to-investigation").wait_for()
    page.locator(".code-browser-source .code-browser-line").first.wait_for()


def test_finding_scope_page_focus_and_source_survive_return_and_history(investigation):
    page, expect_api, _, source, requests = investigation
    before = source.read_bytes()
    open_finding(page)
    page.locator("#evidence-dialog").evaluate("dialog => dialog.close()")
    page.locator("#filter").fill("Return observation")
    page.locator("#filter").press("Enter")
    page.locator("#facet-language").select_option("python")
    page.locator("#facet-module").fill("demo")
    page.get_by_role("button", name="Apply scope filters", exact=True).click()
    page.locator("#finding-severity").select_option("warning")
    page.locator("#finding-category").select_option("complexity")
    page.locator("#finding-confidence").select_option("exact")
    page.get_by_role("button", name="Apply finding filters", exact=True).click()
    page.locator("#next-page").click()
    expect_api.expect(page.locator(".pager")).to_contain_text("51\u201361")
    selected = page.locator('#content [data-kind="finding"]').first
    identity = selected.get_attribute("data-identity")
    selected.click()
    # Saved evidence itself does not read source. Only the explicit source jump may.
    assert not any("/api/code-browser/source?" in url for _, url in requests)
    browse_source(page)
    page.get_by_role("button", name="Select original line 4", exact=True).click()
    page.locator("#return-to-investigation").click()
    expect_api.expect(page.locator(".pager")).to_contain_text("51\u201361")
    expect_api.expect(page.locator("#filter")).to_have_value("Return observation")
    expect_api.expect(page.locator("#facet-language")).to_have_value("python")
    expect_api.expect(page.locator("#facet-module")).to_have_value("demo")
    for name, value in (
        ("severity", "warning"),
        ("category", "complexity"),
        ("confidence", "exact"),
    ):
        expect_api.expect(page.locator(f"#finding-{name}")).to_have_value(value)
    expect_api.expect(page.locator(f'[data-identity="{identity}"]')).to_be_focused()
    state = page.evaluate("history.state")
    assert set(state) == {"codeAnalysisInvestigation"}
    assert isinstance(state["codeAnalysisInvestigation"], int)
    assert "Return observation" not in json.dumps(state)
    page.go_back()
    expect_api.expect(page.locator("#return-to-investigation")).to_be_visible()
    assert page.evaluate("history.state.codeAnalysisBrowse.line") == 4
    page.go_forward()
    expect_api.expect(page.locator(".pager")).to_contain_text("51\u201361")
    assert source.read_bytes() == before


def test_browser_back_returns_to_origin_and_forward_restores_browse(investigation):
    page, playwright, _, _, _ = investigation
    open_finding(page)
    browse_source(page)
    page.go_back()
    playwright.expect(page.locator("#finding-controls")).to_be_visible()
    page.go_forward()
    playwright.expect(page.locator("#return-to-investigation")).to_be_visible()
    playwright.expect(page.locator(".code-browser-source")).to_contain_text(
        "bound: Source bytes match"
    )


def test_dirty_and_composing_drafts_guard_explicit_and_history_return(investigation):
    page, playwright, _, _, _ = investigation
    open_finding(page)
    browse_source(page)
    page.get_by_role("button", name="Edit draft from verified original source", exact=True).click()
    editor = page.locator(".code-drafts-editor textarea")
    editor.wait_for()
    editor.fill("# unsaved session-only edit\n")
    page.on("dialog", lambda dialog: dialog.dismiss())
    page.locator("#return-to-investigation").click()
    playwright.expect(editor).to_have_value("# unsaved session-only edit\n")
    editor.dispatch_event("compositionstart")
    page.locator("#return-to-investigation").click()
    playwright.expect(page.locator(".code-drafts-status")).to_contain_text("IME composition")
    page.go_back()
    page.wait_for_function("location.hash === '#browse'")
    playwright.expect(editor).to_have_value("# unsaved session-only edit\n")
    editor.dispatch_event("compositionend")


def test_cancelled_source_jump_never_installs_return_context(investigation):
    page, playwright, _, _, _ = investigation
    open_finding(page)
    page.route("**/api/code-browser?*", lambda route: route.abort())
    page.get_by_role("button", name="Browse code at this source", exact=True).click()
    playwright.expect(page.locator("#evidence-content")).to_contain_text("Code browser unavailable")
    assert not page.locator("#return-to-investigation").count()
    assert not page.evaluate("Boolean(history.state?.codeAnalysisInvestigation)")


def test_changed_run_binding_invalidates_session_return(investigation):
    page, playwright, _, _, _ = investigation
    open_finding(page)
    browse_source(page)
    bootstrap = page.request.get(
        page.url.split("#")[0].split("?")[0] + "api/bootstrap",
        headers={"X-Code-Analysis-Token": TOKEN},
    ).json()
    page.route(
        "**/api/runs?*",
        lambda route: route.fulfill(
            json={
                "records": [
                    {"generation_id": "synthetic-other-run", "created_at": "Synthetic run"}
                ],
                "omitted": 0,
            }
        ),
    )
    page.route("**/api/bootstrap?*", lambda route: route.fulfill(json=bootstrap))
    page.route("**/api/runtime?*", lambda route: route.fulfill(json={"available": False}))
    page.locator('.nav[data-view="runs"]').click()
    page.locator("#baseline-run").select_option("synthetic-other-run")
    page.locator("#apply-runs").click()
    page.locator(".cards").wait_for()
    reveal_navigation(page, "Browse code").click()
    page.locator(".code-browser").wait_for()
    assert not page.locator("#return-to-investigation").count()
    page.go_back()
    playwright.expect(page.locator("#title")).to_have_text("Browse code")
    assert not page.locator("#finding-controls").count()


def test_return_preserves_validated_handoff_url_without_replaying_it(investigation):
    page, playwright, service, _, requests = investigation
    selection = WebHandoffSelection(
        report_sha256=service.bootstrap().reports[0].report_sha256,
        kind="finding",
        identity="return-050",
    )
    origin = selection.url(page.url)
    page.goto(origin)
    page.get_by_role("button", name="Browse code at this source", exact=True).wait_for()
    handoffs = sum("/api/handoff?" in url for _, url in requests)
    browse_source(page)
    page.go_back()
    playwright.expect(page.locator("#finding-controls")).to_be_visible()
    assert page.url == origin
    assert sum("/api/handoff?" in url for _, url in requests) == handoffs
    page.go_forward()
    page.locator("#return-to-investigation").click()
    playwright.expect(page.locator("#finding-controls")).to_be_visible()
    assert page.url == origin


def test_help_preserves_dirty_draft_dom_value_and_source_selection(investigation):
    page, playwright, _, _, requests = investigation
    open_finding(page)
    browse_source(page)
    page.get_by_role("button", name="Edit draft from verified original source", exact=True).click()
    editor = page.locator(".code-drafts-editor textarea")
    editor.wait_for()
    editor.fill("# keep this unexported draft\n")
    editor.evaluate("node => { window.investigationDraftNode = node; }")
    selection = page.evaluate("history.state.codeAnalysisBrowse")
    before = len(requests)
    page.locator("[data-global-help]").click()
    page.get_by_role("button", name="Expand help", exact=True).click()
    page.get_by_role("button", name="Close help", exact=True).click()
    playwright.expect(editor).to_have_value("# keep this unexported draft\n")
    assert editor.evaluate("node => node === window.investigationDraftNode")
    assert page.evaluate("history.state.codeAnalysisBrowse") == selection
    playwright.expect(page.locator("#return-to-investigation")).to_be_visible()
    assert not any("/api/collection" in url for _, url in requests[before:])
