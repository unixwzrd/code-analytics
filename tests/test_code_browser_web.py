"""Authenticated browser/source boundaries and real-browser synchronization."""

from __future__ import annotations

import json
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlencode

import pytest
from browser_navigation import reveal_navigation
from test_graph_handoff_browser import served

from code_analysis.config import AnalysisConfig
from code_analysis.models import SourceParseState
from code_analysis.project import discover_context
from code_analysis.report_models import AnalysisReport
from code_analysis.service import scan
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

TOKEN = "code-browser-test"


@pytest.fixture
def browser_source(tmp_path):
    source = tmp_path / "demo.py"
    source.write_text(
        'def outer():\n    value = "<script>window.pwned = true</script>"\n'
        "    def inner():\n        return value\n    return inner()\n"
    )
    report = scan(discover_context(tmp_path, AnalysisConfig(test_roots=())))
    return source, ReportViewService(report)


@pytest.mark.parametrize(
    "fixture",
    ("javascript/src/shared.js", "javascript/src/widget.jsx", "ruby_project/lib/a.rb"),
)
def test_saved_language_outline_and_verified_source_without_rescan(tmp_path, monkeypatch, fixture):
    playwright = pytest.importorskip("playwright.sync_api")
    original = Path(__file__).parent / "fixtures" / fixture
    source = tmp_path / original.name
    source.write_bytes(original.read_bytes())
    report = AnalysisReport.model_validate_json(
        scan(discover_context(source, AnalysisConfig(test_roots=()))).model_dump_json()
    )
    module = report.modules[0]
    if module.parse_state is SourceParseState.UNAVAILABLE:
        pytest.skip("optional language parser unavailable; no automatic installation")
    assert module.symbols, "available parser must produce the synthetic declaration inventory"
    symbol = next(
        (item for item in module.symbols if item.kind in ("function", "method")),
        module.symbols[0],
    )
    identity = f"{module.name}:{symbol.qualified_name}"
    before = source.read_bytes()

    def forbidden_scan(*args, **kwargs):
        pytest.fail("saved browser navigation must not rescan source")

    monkeypatch.setattr("code_analysis.service.scan", forbidden_scan)
    with (
        served(
            create_server(ReportViewService(report), source_root=tmp_path, token=TOKEN)
        ) as server,
        playwright.sync_playwright() as runtime,
    ):
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("chromium binary is unavailable; no automatic installation")
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            reveal_navigation(page, "Browse code").click()
            page.locator('.code-browser-list [data-browse-identity^="source-file-"]').click()
            page.locator(f'.code-browser-list [data-browse-identity="{identity}"]').click()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                symbol.qualified_name
            )
            playwright.expect(page.locator(".code-browser-source")).to_contain_text(
                "bound: Source bytes match"
            )
            page.locator(".code-browser-map [data-code-graph-node]").first.wait_for()
            assert source.read_bytes() == before and not errors
        finally:
            browser.close()


def get(connection, route, params, *, token=TOKEN):
    connection.request(
        "GET",
        route + "?" + urlencode(params),
        headers={"X-Code-Analysis-Token": token},
    )
    response = connection.getresponse()
    return response.status, json.loads(response.read())


def test_browser_api_authentication_binding_and_closed_parameters(browser_source):
    source, service = browser_source
    sha = service.bootstrap().reports[0].report_sha256
    with served(create_server(service, source_root=source.parent, token=TOKEN)) as server:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            params = {"report_sha256": sha, "revision": 7}
            assert get(connection, "/api/code-browser", params, token="wrong")[0] == 403
            status, files = get(connection, "/api/code-browser", params)
            assert status == 200 and files["revision"] == 7
            assert files["records"][0]["logical_path"] == "demo.py"
            assert "<script>" not in json.dumps(files)
            assert (
                get(connection, "/api/code-browser", {**params, "report_sha256": "0" * 64})[0]
                == 400
            )
            assert get(connection, "/api/code-browser", {**params, "unknown": "value"})[0] == 400
            assert get(connection, "/api/code-browser", {**params, "path": "../demo.py"})[0] == 400
            status, preview = get(
                connection, "/api/code-browser/source", {**params, "path": "demo.py"}
            )
            assert status == 200 and preview["report_sha256"] == sha
            assert preview["revision"] == 7 and preview["preview"]["state"] == "bound"
            assert "<script>" in preview["preview"]["lines"][1]["text"]
            source.write_text("changed = True\n")
            status, stale = get(
                connection, "/api/code-browser/source", {**params, "path": "demo.py"}
            )
            assert status == 200 and stale["preview"]["state"] == "mismatch"
            assert stale["preview"]["lines"] == []
            assert get(connection, "/api/code-browser", {**params, "path": "demo.py"})[0] == 200
        finally:
            connection.close()


def test_browser_source_disabled_and_continuation(browser_source):
    source, service = browser_source
    params = {
        "report_sha256": service.bootstrap().reports[0].report_sha256,
        "revision": 1,
        "path": "demo.py",
    }
    with served(create_server(service, token=TOKEN)) as server:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            status, result = get(connection, "/api/code-browser/source", params)
            assert status == 200 and result["preview"]["state"] == "disabled"
        finally:
            connection.close()
    source.write_text("# harmless\n" * 205)
    service = ReportViewService(
        scan(discover_context(source.parent, AnalysisConfig(test_roots=())))
    )
    params["report_sha256"] = service.bootstrap().reports[0].report_sha256
    with served(create_server(service, source_root=source.parent, token=TOKEN)) as server:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            status, result = get(connection, "/api/code-browser/source", params)
            assert status == 200 and len(result["preview"]["lines"]) == 200
            assert result["preview"]["next_line"] == 201
            status, result = get(connection, "/api/code-browser/source", {**params, "line": 201})
            assert status == 200 and len(result["preview"]["lines"]) == 5
            assert result["preview"]["next_line"] is None
        finally:
            connection.close()


def test_graph_api_is_bound_closed_and_source_independent(browser_source):
    source, service = browser_source
    params = {
        "report_sha256": service.bootstrap().reports[0].report_sha256,
        "revision": 8,
        "path": "demo.py",
        "layer": "containment",
    }
    source.unlink()
    with served(create_server(service, token=TOKEN)) as server:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            assert get(connection, "/api/code-browser/graph", params, token="wrong")[0] == 403
            status, result = get(connection, "/api/code-browser/graph", params)
            assert status == 200 and result["revision"] == 8
            assert result["state"] == "available" and result["links"]
            assert result["graph"]["nodes"]
            status, bounded = get(
                connection, "/api/code-browser/graph", {**params, "max_nodes": 1, "max_edges": 0}
            )
            assert status == 200 and bounded["graph"]["truncated"]
            assert bounded["completeness"] == "partial"
            for override in (
                {"report_sha256": "0" * 64},
                {"unexpected": "x"},
                {"max_nodes": 101},
                {"max_edges": 251},
                {"path": "../demo.py"},
                {"layer": "guessed-runtime"},
            ):
                assert get(connection, "/api/code-browser/graph", {**params, **override})[0] == 400
            status, unavailable = get(
                connection, "/api/code-browser/graph", {**params, "layer": "data-flow"}
            )
            assert status == 200 and unavailable["state"] == "unavailable"
            assert unavailable["graph"] is None
        finally:
            connection.close()


def test_browser_relationship_layers_keyboard_and_collapse(browser_source):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("chromium binary is unavailable; no automatic installation")
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            reveal_navigation(page, "Browse code").click()
            page.locator('.code-browser-list [data-browse-identity^="source-file-"]').click()
            graph = page.locator(".code-browser-map")
            graph.locator("[data-code-graph-node]").first.wait_for()
            module_id = service.primary.modules[0].name
            node = graph.locator(f'[data-code-graph-node="code-browse-contained:{module_id}"]')
            node.focus()
            page.keyboard.press("Enter")
            graph.get_by_role("button", name="Collapse children", exact=True).click()
            playwright.expect(graph).to_contain_text("retained nodes hidden by display collapse")
            assert graph.locator("[data-code-graph-node]").count() == 1
            graph.get_by_role("button", name="Expand all retained nodes", exact=True).click()
            assert graph.locator("[data-code-graph-node]").count() == 3
            graph.get_by_label("Relationship layer", exact=True).select_option("imports")
            playwright.expect(graph).to_contain_text("Code browser import neighborhood retained")
            graph.get_by_label("Relationship layer", exact=True).select_option("data-flow")
            playwright.expect(graph).to_contain_text(
                "Dictionary flow graph requires saved flow evidence"
            )
            assert graph.locator("svg").count() == 0
            graph.get_by_label("Relationship layer", exact=True).select_option("containment")
            graph.locator("[data-code-graph-node]").first.wait_for()
            node = graph.locator(f'[data-code-graph-node="code-browse-contained:{module_id}"]')
            node.click()
            graph.get_by_role("button", name="Browse recorded source", exact=True).click()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(module_id)
        finally:
            browser.close()


@pytest.mark.parametrize("mismatch", ("report", "revision"))
def test_browser_source_identity_mismatch_is_explicit_not_endless_loading(browser_source, mismatch):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("chromium binary is unavailable; no automatic installation")
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            digest = service.bootstrap().reports[0].report_sha256
            page.evaluate(
                """async ({digest, token, mismatch}) => {
              const {codeBrowser} = await import('/code_browser.js');
              const api = async path => {
                const response = await fetch(path, {headers: {'X-Code-Analysis-Token': token}});
                const data = await response.json();
                if (path.startsWith('/api/code-browser/source?')) {
                  if (mismatch === 'report') data.report_sha256 = '0'.repeat(64);
                  else data.revision += 1;
                }
                return data;
              };
              await codeBrowser(document.querySelector('#content'), api,
                new AbortController().signal, digest,
                {report_sha256: digest, path: 'demo.py', revision: 0});
            }""",
                {"digest": digest, "token": TOKEN, "mismatch": mismatch},
            )
            source_panel = page.locator(".code-browser-source")
            playwright.expect(source_panel).to_contain_text(
                "saved preview identity disagrees with the selected report or revision"
            )
            playwright.expect(source_panel).not_to_contain_text("Checking original source identity")
        finally:
            browser.close()


def test_history_retains_only_named_selection_metadata(browser_source):
    playwright = pytest.importorskip("playwright.sync_api")
    _, service = browser_source
    with (
        served(create_server(service, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("chromium binary is unavailable; no automatic installation")
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            result = page.evaluate(
                """async ({digest, token}) => {
                  const {codeBrowser} = await import('/code_browser.js');
                  const api = async path => (await fetch(path,
                    {headers: {'X-Code-Analysis-Token': token}})).json();
                  const saved = JSON.parse('{"__proto__":{"polluted":true}}');
                  Object.assign(saved, {report_sha256: digest, path: 'demo.py',
                    source_text: 'DO_NOT_STORE_SOURCE', draft_text: 'DO_NOT_STORE_DRAFT',
                    active_pane: 'invalid', revision: 0});
                  history.replaceState({codeAnalysisBrowse: saved}, '', '#browse');
                  const session = {source_text: 'LOCAL_SOURCE', draft_text: 'LOCAL_DRAFT'};
                  await codeBrowser(document.querySelector('#content'), api,
                    new AbortController().signal, digest, session);
                  return {history: history.state.codeAnalysisBrowse,
                    polluted: session.polluted === true};
                }""",
                {"digest": service.bootstrap().reports[0].report_sha256, "token": TOKEN},
            )
            assert set(result["history"]) <= {
                "report_sha256",
                "path",
                "identity",
                "search",
                "offset",
                "line",
                "revision",
                "layer",
                "collapsed",
                "active_pane",
            }
            assert result["history"]["active_pane"] == "source"
            assert not result["polluted"]
            assert "DO_NOT_STORE" not in json.dumps(result) and "LOCAL_" not in json.dumps(result)
        finally:
            browser.close()


def test_obsolete_source_and_graph_responses_cannot_replace_new_selection(browser_source):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("chromium binary is unavailable; no automatic installation")
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            digest = service.bootstrap().reports[0].report_sha256
            page.evaluate(
                """async ({digest, token}) => {
              const {codeBrowser} = await import('/code_browser.js');
              const held = new Set(); window.releaseObsolete = [];
              const api = async path => {
                const response = await fetch(path, {headers: {'X-Code-Analysis-Token': token}});
                const data = await response.json();
                const kind = path.startsWith('/api/code-browser/graph?') ? 'graph'
                  : path.startsWith('/api/code-browser/source?') ? 'source' : null;
                if (kind && !held.has(kind)) {
                  held.add(kind);
                  if (kind === 'graph') data.message = 'OBSOLETE GRAPH';
                  else data.preview.message = 'OBSOLETE SOURCE';
                  return new Promise(resolve => window.releaseObsolete.push(() => resolve(data)));
                }
                return data;
              };
              window.browserLoad = codeBrowser(document.querySelector('#content'), api,
                new AbortController().signal, digest,
                {report_sha256: digest, path: 'demo.py', revision: 0});
            }""",
                {"digest": digest, "token": TOKEN},
            )
            page.wait_for_function("() => window.releaseObsolete.length === 2")
            page.get_by_label("Relationship layer", exact=True).select_option("imports")
            playwright.expect(page.locator(".code-browser-map")).to_contain_text(
                "Code browser import neighborhood retained"
            )
            playwright.expect(page.locator(".code-browser-source")).to_contain_text(
                "bound: Source bytes match"
            )
            page.evaluate("""async () => {
                  window.releaseObsolete.forEach(release => release());
                  await window.browserLoad;
                }""")
            playwright.expect(page.locator(".code-browser-map")).not_to_contain_text(
                "OBSOLETE GRAPH"
            )
            playwright.expect(page.locator(".code-browser-source")).not_to_contain_text(
                "OBSOLETE SOURCE"
            )
            assert page.get_by_label("Relationship layer", exact=True).input_value() == "imports"
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
def test_obsolete_outline_selection_cannot_steal_pane_or_focus(browser_source, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser_type = getattr(runtime, engine)
        if not Path(browser_type.executable_path).is_file():
            pytest.skip(f"{engine} binary is unavailable; no automatic installation")
        browser = browser_type.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            page.evaluate(
                """async ({digest, token}) => {
                  const {codeBrowser} = await import('/code_browser.js');
                  let held = false;
                  const api = async path => {
                    const response = await fetch(path,
                      {headers: {'X-Code-Analysis-Token': token}});
                    const data = await response.json();
                    const url = new URL(path, location.origin);
                    if (url.pathname === '/api/code-browser'
                        && url.searchParams.get('identity') === 'demo:outer' && !held) {
                      held = true;
                      return new Promise(resolve => {
                        window.releaseOldOutline = () => resolve(data);
                      });
                    }
                    return data;
                  };
                  await codeBrowser(document.querySelector('#content'), api,
                    new AbortController().signal, digest,
                    {report_sha256: digest, path: 'demo.py', revision: 0});
                }""",
                {"digest": service.bootstrap().reports[0].report_sha256, "token": TOKEN},
            )
            outline_pane = page.locator(".code-browser-toolbar").get_by_role(
                "button", name="Files and entities", exact=True
            )
            outline_pane.focus()
            outline_pane.press("Enter")
            outer = page.locator('[data-browse-identity="demo:outer"]')
            outer.focus()
            outer.press("Enter")
            page.wait_for_function("() => typeof window.releaseOldOutline === 'function'")
            inner = page.locator('[data-browse-identity="demo:outer.inner"]')
            inner.focus()
            inner.press("Enter")
            source_heading = page.locator(".code-browser-source h2")
            playwright.expect(source_heading).to_be_focused()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text("outer.inner")
            pane = page.locator(".code-browser-toolbar").get_by_role(
                "button", name="Recorded relationships", exact=True
            )
            pane.focus()
            pane.press("Enter")
            map_heading = page.locator(".code-browser-map h2")
            playwright.expect(map_heading).to_be_focused()
            page.evaluate("""async () => {
                  window.releaseOldOutline();
                  await new Promise(resolve => setTimeout(resolve, 0));
                }""")
            playwright.expect(page.locator(".code-browser")).to_have_attribute(
                "data-active-pane", "map"
            )
            playwright.expect(map_heading).to_be_focused()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text("outer.inner")
        finally:
            browser.close()


@pytest.mark.parametrize("mismatch", ("report", "revision"))
@pytest.mark.parametrize("engine", ("chromium", "webkit"))
def test_active_outline_mismatch_is_explicit_not_loading(browser_source, mismatch, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    _, service = browser_source
    with (
        served(create_server(service, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser_type = getattr(runtime, engine)
        if not Path(browser_type.executable_path).is_file():
            pytest.skip(f"{engine} binary is unavailable; no automatic installation")
        browser = browser_type.launch()
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            page.evaluate(
                """async ({digest, token, mismatch}) => {
                  const {codeBrowser} = await import('/code_browser.js');
                  const api = async path => {
                    const response = await fetch(path,
                      {headers: {'X-Code-Analysis-Token': token}});
                    const data = await response.json();
                    if (path.startsWith('/api/code-browser?')) {
                      if (mismatch === 'report') data.report_sha256 = '0'.repeat(64);
                      else data.revision += 1;
                    }
                    return data;
                  };
                  await codeBrowser(document.querySelector('#content'), api,
                    new AbortController().signal, digest, {});
                }""",
                {
                    "digest": service.bootstrap().reports[0].report_sha256,
                    "token": TOKEN,
                    "mismatch": mismatch,
                },
            )
            playwright.expect(page.locator(".code-browser-status")).to_contain_text(
                "Saved outline identity disagrees"
            )
            playwright.expect(page.locator(".code-browser-status")).not_to_contain_text(
                "Loading saved outline"
            )
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
def test_deliberate_pane_choice_wins_over_pending_current_selection(browser_source, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser_type = getattr(runtime, engine)
        if not Path(browser_type.executable_path).is_file():
            pytest.skip(f"{engine} binary is unavailable; no automatic installation")
        browser = browser_type.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            page.evaluate(
                """async ({digest, token}) => {
                  const {codeBrowser} = await import('/code_browser.js');
                  const api = async path => {
                    const response = await fetch(path,
                      {headers: {'X-Code-Analysis-Token': token}});
                    const data = await response.json();
                    const url = new URL(path, location.origin);
                    if (url.pathname === '/api/code-browser/graph'
                        && url.searchParams.get('identity') === 'demo:outer.inner') {
                      return new Promise(resolve => {
                        window.releaseCurrentGraph = () => resolve(data);
                      });
                    }
                    return data;
                  };
                  await codeBrowser(document.querySelector('#content'), api,
                    new AbortController().signal, digest,
                    {report_sha256: digest, path: 'demo.py', revision: 0});
                }""",
                {"digest": service.bootstrap().reports[0].report_sha256, "token": TOKEN},
            )
            pane = page.locator(".code-browser-toolbar").get_by_role(
                "button", name="Files and entities", exact=True
            )
            pane.focus()
            pane.press("Enter")
            inner = page.locator('[data-browse-identity="demo:outer.inner"]')
            inner.focus()
            inner.press("Enter")
            page.wait_for_function("() => typeof window.releaseCurrentGraph === 'function'")
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text("outer.inner")
            pane = page.locator(".code-browser-toolbar").get_by_role(
                "button", name="Recorded relationships", exact=True
            )
            pane.focus()
            pane.press("Enter")
            heading = page.locator(".code-browser-map h2")
            playwright.expect(heading).to_be_focused()
            page.evaluate("""async () => {
                  window.releaseCurrentGraph();
                  await new Promise(resolve => setTimeout(resolve, 0));
                }""")
            playwright.expect(page.locator(".code-browser")).to_have_attribute(
                "data-active-pane", "map"
            )
            playwright.expect(heading).to_be_focused()
            assert page.evaluate("() => history.state.codeAnalysisBrowse.active_pane") == "map"
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
@pytest.mark.parametrize("width", (1440, 390))
def test_existing_module_evidence_opens_code_browser(browser_source, engine, width):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser_type = getattr(runtime, engine)
        if not Path(browser_type.executable_path).is_file():
            pytest.skip(f"{engine} binary is unavailable; no automatic installation")
        browser = browser_type.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
            if toggle.is_visible():
                toggle.click()
            reveal_navigation(page, "Modules").click()
            page.locator('[data-kind="navigation"][data-identity]').first.click()
            dialog = page.locator("#evidence-dialog")
            dialog.get_by_role(
                "button", name="Browse code at this source", exact=True
            ).first.click()
            playwright.expect(dialog).not_to_be_visible()
            playwright.expect(page.locator(".code-browser-source")).to_be_visible()
            playwright.expect(page.locator(".code-browser-source")).to_contain_text(
                "bound: Source bytes match"
            )
            assert page.evaluate("() => history.state.codeAnalysisBrowse.path") == "demo.py"
            assert page.evaluate("() => history.state.codeAnalysisBrowse.active_pane") == "source"
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
@pytest.mark.parametrize("width", (1440, 820, 390))
@pytest.mark.parametrize("theme", ("light", "dark"))
def test_read_only_browser_selection_source_and_history(browser_source, engine, width, theme):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    before = source.read_bytes()
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser_type = getattr(runtime, engine)
        if not Path(browser_type.executable_path).is_file():
            pytest.skip(f"{engine} binary is unavailable; no automatic installation")
        browser = browser_type.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme=theme)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
            if toggle.is_visible():
                toggle.click()
            reveal_navigation(page, "Browse code").click()
            page.locator('.code-browser-list [data-browse-identity^="source-file-"]').click()
            if width <= 820:
                page.locator(".code-browser-toolbar").get_by_role(
                    "button", name="Files and entities", exact=True
                ).click()
            module = service.primary.modules[0]
            inner = next(symbol for symbol in module.symbols if symbol.name == "inner")
            identity = f"{module.name}:{inner.qualified_name}"
            page.locator(".code-browser-list [data-browse-identity]").filter(
                has_text="inner ·"
            ).click()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                inner.qualified_name
            )
            playwright.expect(page.locator(".code-browser-source h2")).to_be_focused()
            playwright.expect(page.locator(".code-browser-source")).to_contain_text(
                "bound: Source bytes match"
            )
            assert page.locator('.code-browser-line[data-selected="true"]').count() == 2
            page.get_by_role("button", name="Select original line 5", exact=True).click()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text("outer")
            playwright.expect(page.locator(".code-browser-source h2")).to_be_focused()
            page.go_back()
            playwright.expect(page.locator(".code-browser-source")).to_be_visible()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                inner.qualified_name
            )
            playwright.expect(page.locator(".code-browser-source h2")).to_be_focused()
            assert (
                page.locator(
                    f'.code-browser-list [data-browse-identity="{identity}"]'
                ).get_attribute("aria-pressed")
                == "true"
            )
            page.go_forward()
            playwright.expect(page.locator(".code-browser-source")).to_be_visible()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text("outer")
            playwright.expect(page.locator(".code-browser-source h2")).to_be_focused()
            assert page.evaluate("() => window.pwned") is None
            assert page.evaluate(
                "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
            )
            assert source.read_bytes() == before
            assert not errors
            output = Path(__file__).resolve().parents[1] / "output" / "playwright"
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(output / f"20261011-browser-history-{engine}-{width}-{theme}.png")
            )
        finally:
            browser.close()
