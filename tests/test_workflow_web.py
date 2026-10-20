"""Workflow requests are authenticated, read-only views of selected evidence."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.client import HTTPConnection

import pytest

from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


@contextmanager
def served(service, root, **options):
    server = create_server(service, collection_root=root, token="workflow-test", **options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get(server, path, *, token="workflow-test"):
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        connection.request("GET", path, headers={"X-Code-Analysis-Token": token})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_workflow_empty_and_static_are_authenticated_and_non_mutating(tmp_path, ordinary_report):
    (tmp_path / "never_execute.py").write_text("raise RuntimeError('must not run')\n")
    before = sorted(tmp_path.rglob("*"))
    for service in (None, ReportViewService(ordinary_report)):
        with served(service, tmp_path) as server:
            assert get(server, "/api/workflow", token="wrong")[0] == 403
            status, first = get(server, "/api/workflow")
            assert status == 200
            assert first == get(server, "/api/workflow")[1]
            assert len(first["views"]) == 18
            assert bool(first["reports"]) == (service is not None)
            assert not first["collection"]["execution_available"]
            assert first["source_access"] == "not-configured"
            assert (
                service is None
                or first["reports"][0]["report_sha256"]
                == service.bootstrap().reports[0].report_sha256
            )
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    "query", ["path=/etc/passwd", "profile_run=none&profile_run=current", "run=current", "scan=1"]
)
def test_workflow_rejects_unknown_or_inapplicable_selectors(tmp_path, query):
    with served(None, tmp_path) as server:
        assert get(server, f"/api/workflow?{query}")[0] == 400


def test_bundled_help_routes_and_workflow_topics_are_closed_and_resolved(tmp_path):
    with served(None, tmp_path) as server:
        status, index = get(server, "/help/index.json", token="")
        assert status == 200
        documents = {item["document_id"]: item for item in index["documents"]}
        workflow = get(server, "/api/workflow")[1]
        for view in workflow["views"]:
            document = documents[view["help_document_id"]]
            assert view["help_anchor"] in {item["anchor"] for item in document["headings"]}
        for path in (
            "/help/REQUIREMENTS.md",
            "/help/../../docs/private.md",
            "/help/%2e%2e/private.md",
            "/help/ux-workflow-refinement-plan.html",
            "/help/not-listed.html",
        ):
            assert get(server, path, token="")[0] == 404


def test_corrupt_help_does_not_change_workflow_or_report(tmp_path, ordinary_report, monkeypatch):
    import code_analysis.user_help_store as help_store

    def unavailable(*args, **kwargs):
        raise help_store.HelpUnavailableError("synthetic corrupt asset")

    with served(ReportViewService(ordinary_report), tmp_path) as server:
        before = get(server, "/api/workflow")[1]
        monkeypatch.setattr(help_store, "read_help_asset", unavailable)
        assert get(server, "/help/index.json")[0] == 503
        assert get(server, "/api/workflow")[1] == before
        assert get(server, "/api/bootstrap")[0] == 200


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1440, 768, 390])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_empty_workflow_guides_without_requests_for_missing_evidence(
    tmp_path, engine, width, theme
):
    playwright = pytest.importorskip("playwright.sync_api")
    with served(None, tmp_path) as server, playwright.sync_playwright() as runtime:
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000}, color_scheme=theme)
            errors = []
            requested = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requested.append(request.url))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=workflow-test")
            page.get_by_role("heading", name="What would you like to investigate?").wait_for()
            assert page.locator("[data-workflow-group]").count() == 5
            assert page.locator(".nav").count() == 18
            group = page.locator("details[data-workflow-group]").filter(
                has=page.locator('.nav[data-view="modules"]')
            )
            assert group.get_attribute("open") is None
            # Destinations remain operable for a corrective explanation, not a failing request.
            if page.get_by_role("button", name="Toggle navigation", exact=True).is_visible():
                page.get_by_role("button", name="Toggle navigation", exact=True).click()
            group.locator("summary").first.click()
            page.locator('.nav[data-view="modules"]').click()
            playwright.expect(page.locator("#workflow-notice")).to_be_visible()
            assert not any("/api/modules" in url for url in requested)
            assert page.get_by_role(
                "heading", name="What would you like to investigate?"
            ).is_visible()
            assert page.locator("body").evaluate("node => node.scrollWidth <= innerWidth")
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_collection_retains_settings_but_not_authorization_or_preview(tmp_path, engine):
    (tmp_path / "example.py").write_text("print('not executed')\n")
    playwright = pytest.importorskip("playwright.sync_api")
    with served(None, tmp_path) as server, playwright.sync_playwright() as runtime:
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=workflow-test")
            page.locator('.nav[data-view="collection"]').click()
            page.locator("#target").fill("example.py")
            page.get_by_role("button", name="Build command", exact=True).click()
            playwright.expect(page.locator("#preview-status")).to_contain_text("Preview ready")
            page.locator("#operation").select_option("profile")
            page.locator("#workload").fill("example.py")
            page.locator("#allow_execution").check()
            page.locator('.nav[data-view="overview"]').click()
            page.locator('.nav[data-view="collection"]').click()
            assert page.locator("#operation").input_value() == "profile"
            assert page.locator("#target").input_value() == "example.py"
            assert page.locator("#workload").input_value() == "example.py"
            assert not page.locator("#allow_execution").is_checked()
            assert page.locator("#collection-command").input_value() == ""
            assert page.get_by_role("button", name="Copy command").is_disabled()
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1920, 1440, 768, 390])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_task_layout_keeps_actions_near_inputs_and_preview_before_confirmation(
    tmp_path, engine, width, theme
):
    playwright = pytest.importorskip("playwright.sync_api")
    before = sorted(tmp_path.rglob("*"))
    with (
        served(None, tmp_path, allow_collection=True) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000}, color_scheme=theme)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=workflow-test")
            primary = page.locator(".workflow-primary")
            playwright.expect(primary).to_have_text("Analyze code")
            assert page.locator(".workflow-primary").count() == 1
            assert page.locator("details[data-workflow-group][open]").count() == 1
            primary.click()
            page.locator("#target").wait_for()
            playwright.expect(page.locator("#launch-job")).to_be_disabled()
            assert (
                page.locator(".collection-preview").evaluate(
                    "node => getComputedStyle(node).position"
                )
                == "static"
            )
            assert page.locator(".collection-preview").evaluate(
                """node => Boolean(node.compareDocumentPosition(
                  document.querySelector('.collection-jobs')) & Node.DOCUMENT_POSITION_FOLLOWING)"""
            )
            for operation, mode in (("scan", "static"), ("profile", "runtime")):
                page.locator("#operation").select_option(operation)
                page.locator(f'fieldset[data-mode="{mode}"] details summary').click()
                assert page.locator("#collection-form").evaluate(
                    "node => node.getBoundingClientRect().width <= 769"
                )
                for field in page.locator(
                    f'fieldset[data-mode="{mode}"] .collection-path-field'
                ).all():
                    field.scroll_into_view_if_needed()
                    group = field.bounding_box()
                    control = field.locator("input").bounding_box()
                    action = field.get_by_role("button").bounding_box()
                    assert group and control and action
                    assert group["width"] <= 705
                    if width > 720:
                        assert 0 <= action["x"] - control["x"] - control["width"] <= 13
                        assert (
                            abs(action["y"] + action["height"] - control["y"] - control["height"])
                            <= 3
                        )
                    else:
                        assert action["y"] >= control["y"] + control["height"]
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.locator("#operation").select_option("scan")
            page.get_by_role("button", name="Browse for Target path", exact=True).click()
            playwright.expect(page.get_by_role("dialog")).to_have_attribute(
                "aria-labelledby", "collection-browser-title"
            )
            playwright.expect(page.locator("#collection-browser-title")).to_have_text(
                "Choose existing source"
            )
            page.keyboard.press("Escape")
            assert not errors
        finally:
            browser.close()
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.ui_smoke
def test_saved_evidence_has_one_context_specific_starting_action(tmp_path, ordinary_report, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(ReportViewService(ordinary_report), tmp_path) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=workflow-test")
            expected = (
                "Investigate findings" if ordinary_report.summary.findings else "Explore modules"
            )
            playwright.expect(page.locator(".workflow-primary")).to_have_text(expected)
            assert page.locator(".workflow-primary").count() == 1
            page.locator(".workflow-primary").click()
            playwright.expect(page.locator("#title")).to_have_text(
                "Findings" if ordinary_report.summary.findings else "Modules"
            )
            active = page.locator("details[data-workflow-group]").filter(
                has=page.locator(".nav.active")
            )
            playwright.expect(active).to_have_attribute("open", "")
        finally:
            browser.close()


def test_starting_action_preserves_partial_failed_and_profile_only_distinctions(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with served(None, tmp_path) as server, playwright.sync_playwright() as runtime:
        workflow = get(server, "/api/workflow")[1]
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=workflow-test")
            page.locator(".workflow-primary").wait_for()
            actual = page.evaluate(
                """async original => {
                  const {workflowOverview} = await import('/workflow.js');
                  const render = (state, reports, available, count) => {
                    const fixture = structuredClone(original);
                    fixture.overall_readiness.state = state;
                    fixture.reports = reports
                      ? [{target_root: 'synthetic', report_sha256: 'a'.repeat(64)}] : [];
                    fixture.runtime.available = available;
                    const root = workflowOverview(fixture, () => {}, count);
                    return root.querySelector('.workflow-primary').textContent;
                  };
                  return [render('partial', true, false, 1), render('failed', true, false, 1),
                    render('missing', true, false, 0), render('partial', false, true, undefined),
                    render('available', true, false, 0)];
                }""",
                workflow,
            )
            assert actual == [
                "Investigate findings",
                "Check collection issues",
                "Check collection issues",
                "Explore Python profile",
                "Explore modules",
            ]
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1920, 768, 390])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_collection_hints_are_below_their_controls(tmp_path, engine, width, theme):
    playwright = pytest.importorskip("playwright.sync_api")
    with served(None, tmp_path) as server, playwright.sync_playwright() as runtime:
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000}, color_scheme=theme)
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=workflow-test")
            page.get_by_role("heading", name="What would you like to investigate?").wait_for()
            toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
            if toggle.is_visible():
                toggle.click()
            page.locator('.nav[data-view="collection"]').click()
            page.locator("#target").wait_for()
            for operation, mode, expected_count in (
                ("scan", "static", 7),
                ("profile", "runtime", 5),
            ):
                page.locator("#operation").select_option(operation)
                page.locator(f'fieldset[data-mode="{mode}"] details summary').click()
                metrics = page.locator("#collection-form").evaluate("""form => {
                  return [...form.querySelectorAll('[aria-describedby^="collection-help-"]')]
                    .filter(control => control.getClientRects().length)
                    .map(control => {
                      const hintId = control.getAttribute('aria-describedby');
                      const hint = document.getElementById(hintId);
                      const box = control.getBoundingClientRect();
                      const help = hint.getBoundingClientRect();
                      return {id: control.id, below: help.top >= box.bottom,
                        aligned: Math.abs(help.left - box.left) <= 2,
                        readable: help.width <= 760,
                        visible: Boolean(hint.getClientRects().length)};
                    });
                }""")
                assert len(metrics) == expected_count
                assert all(item["below"] and item["aligned"] for item in metrics), metrics
                assert all(item["visible"] and item["readable"] for item in metrics), metrics
                assert page.locator("#collection-help-interpreter_id").count() == 1
                assert page.locator("body").evaluate("node => node.scrollWidth <= innerWidth")
                if operation == "profile":
                    assert page.locator('label[for="allow_execution"] code').evaluate("""node => {
                      const text = document.createRange(); text.selectNodeContents(node);
                      return text.getClientRects().length;
                    }""") == 1, "Authorization flags must not become narrow columns of letters"
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1440, 768, 390])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_help_panel_preserves_collection_and_restores_focus(tmp_path, engine, width, theme):
    playwright = pytest.importorskip("playwright.sync_api")
    with served(None, tmp_path) as server, playwright.sync_playwright() as runtime:
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000}, color_scheme=theme)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=workflow-test")
            toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
            if toggle.is_visible():
                toggle.click()
            page.locator('.nav[data-view="collection"]').click()
            page.locator("#target").fill("uncollected-project")
            opener = page.locator("[data-global-help]")
            opener.focus()
            opener.press("Enter")
            dialog = page.get_by_role("dialog", name="Code Analysis guide", exact=True)
            playwright.expect(dialog).to_be_visible()
            dialog.get_by_role("article").get_by_role("heading").first.wait_for()
            assert "Help is unavailable" not in dialog.inner_text()
            dialog.get_by_role("combobox", name="Documentation topic").select_option("user-guide")
            heading = dialog.get_by_role("columnheader", name="Readiness", exact=True)
            heading.wait_for()
            assert heading.evaluate("""node => {
              const text = document.createRange();
              text.selectNodeContents(node);
              return text.getClientRects().length;
            }""") == 1, "Help table headings must not split individual words"
            dialog.get_by_role("button", name="Expand help", exact=True).click()
            assert dialog.get_attribute("data-expanded") == "true"
            dialog.get_by_role("button", name="Restore help width", exact=True).click()
            dialog.get_by_role("searchbox").fill("collection")
            playwright.expect(dialog.locator(".help-results")).to_contain_text("matches")
            assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth")
            page.keyboard.press("Escape")
            playwright.expect(dialog).not_to_be_visible()
            playwright.expect(opener).to_be_focused()
            assert page.locator('#user-help input[type="search"]').input_value() == "collection"
            assert page.locator("#target").input_value() == "uncollected-project"
            assert page.locator("#collection-command").input_value() == ""
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_help_history_links_and_stale_response_are_isolated(tmp_path, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    with served(None, tmp_path) as server, playwright.sync_playwright() as runtime:
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=workflow-test")
            page.locator("[data-global-help]").click()
            dialog = page.get_by_role("dialog", name="Code Analysis guide", exact=True)
            topic = dialog.get_by_role("combobox", name="Documentation topic")
            article = dialog.get_by_role("article")
            article.get_by_role("heading").first.wait_for()
            topic.select_option("cli-reference")
            playwright.expect(article.get_by_role("heading").first).to_have_text("CLI reference")
            initial_heading = article.get_by_role("heading").first.inner_text()
            scroller = page.locator("#user-help .lui-content-panel__body")
            scroller.evaluate("node => {node.scrollTop = 1000}")
            position = scroller.evaluate("node => node.scrollTop")
            assert position == 1000
            # Delay a real response after reading its body so AbortController
            # alone cannot hide a missing stale-result check.
            page.evaluate("""() => {
              const originalFetch = window.fetch;
              let delayed = false;
              window.fetch = async (...args) => {
                const response = await originalFetch(...args);
                if (!delayed && String(args[0]) === '/help/user-guide.html') {
                  delayed = true;
                  const text = await response.text();
                  return {ok: response.ok, text: () => new Promise(resolve => {
                    window.releaseOldHelp = () => resolve(text);
                    document.body.dataset.oldHelpReady = 'true';
                  })};
                }
                return response;
              };
            }""")
            topic.select_option("user-guide")
            playwright.expect(page.locator("body")).to_have_attribute("data-old-help-ready", "true")
            # Closing a pending topic must not overwrite the last document's
            # saved position with the transient loading panel's scroll position.
            page.keyboard.press("Escape")
            page.locator("[data-global-help]").click()
            playwright.expect(article.get_by_role("heading").first).to_have_text(initial_heading)
            assert scroller.evaluate("node => node.scrollTop") == position
            topic.select_option("examples")
            playwright.expect(topic).to_have_value("examples")
            playwright.expect(article.get_by_role("heading").first).not_to_have_text(
                initial_heading
            )
            example_heading = article.get_by_role("heading").first.inner_text()
            page.evaluate("() => window.releaseOldHelp()")
            # Flush queued promise continuations before checking the retained topic.
            page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame("
                "() => requestAnimationFrame(resolve)))"
            )
            playwright.expect(topic).to_have_value("examples")
            playwright.expect(article.get_by_role("heading").first).to_have_text(example_heading)
            dialog.get_by_role("button", name="Previous help topic", exact=True).click()
            playwright.expect(article.get_by_role("heading").first).to_have_text(initial_heading)
            dialog.get_by_role("button", name="Next help topic", exact=True).click()
            playwright.expect(article.get_by_role("heading").first).to_have_text(example_heading)
            # Local links stay in this panel, not a new browser navigation.
            link = article.locator('a[href*="/help/"]').first
            link.scroll_into_view_if_needed()
            target = link.get_attribute("href").split("/help/", 1)[1].split(".html", 1)[0]
            location = page.url
            link.click()
            playwright.expect(topic).to_have_value(target)
            assert page.url == location
            assert not errors
        finally:
            browser.close()


def test_help_failure_does_not_block_workflow_navigation(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with served(None, tmp_path) as server, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.route(
                "**/help/index.json", lambda route: route.fulfill(status=503, body="unavailable")
            )
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=workflow-test")
            page.locator("[data-global-help]").click()
            page.get_by_role("heading", name="Help is unavailable").wait_for()
            page.get_by_role("button", name="Retry help").wait_for()
            page.keyboard.press("Escape")
            page.locator('.nav[data-view="collection"]').click()
            page.get_by_role("heading", name="Collection setup").wait_for()
            playwright.expect(page.locator("#target")).to_be_visible()
        finally:
            browser.close()
