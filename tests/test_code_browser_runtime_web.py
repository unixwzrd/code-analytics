"""Runtime/static binding through the authenticated browser; no workload execution."""

from http.client import HTTPConnection
from pathlib import Path

import pytest
from browser_navigation import reveal_navigation
from test_code_browse_runtime import GOLDEN, _build_profile, _project_frame
from test_code_browser_web import TOKEN, get
from test_code_browser_web import browser_source as browser_source
from test_graph_handoff_browser import served

from code_analysis.profile_models import CallerRepresentation, FrameClassification
from code_analysis.profiling import render_profile_json
from code_analysis.runtime_models import RuntimeSourceHash
from code_analysis.ui_handoff import canonical_profile_sha256
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_server import create_server


@pytest.fixture
def runtime_browser(browser_source, tmp_path):
    source, service = browser_source
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-" + "a" * 20,
        path="demo.py",
        line=3,
        function="captured-worker",
    )
    native = _project_frame(
        original,
        frame_id="profile-frame-" + "b" * 20,
        path="<native>",
        line=0,
        function="opaque-device-work",
        classification=FrameClassification.NATIVE,
    )
    edge = original.runtime_edges[0].model_copy(
        update={
            "caller_frame_id": frame.frame_id,
            "callee_frame_id": native.frame_id,
            "caller_representation": CallerRepresentation.COUNT,
            "primitive_calls": None,
            "total_calls": 7,
            "total_seconds": None,
            "cumulative_seconds": None,
        }
    )
    record = service.primary.source_files[0]
    profile = _build_profile(
        service.primary,
        frames=(frame, native),
        runtime_edges=(edge,),
        source_hashes=(RuntimeSourceHash(path="demo.py", sha256=record.source_sha256),),
    )
    profile_path = tmp_path / "saved-profile.json"
    profile_path.write_text(render_profile_json(profile, compact=True), encoding="utf-8")
    pins = {
        "report_sha256": service.bootstrap().reports[0].report_sha256,
        "revision": 4,
        "profile_report_sha256": canonical_profile_sha256(profile),
        "profile_identity_sha256": profile.profile_identity_sha256,
    }
    return source, service, profile_path, frame, native, pins


def test_runtime_browser_http_identity_scope_and_unavailable_locations(runtime_browser):
    source, service, profile_path, frame, native, pins = runtime_browser
    source.unlink()  # Saved mapping and graph cannot read source.
    with served(create_server(service, profile_report=profile_path, token=TOKEN)) as server:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            params = {**pins, "frame_id": frame.frame_id, "profile_run": "current"}
            route = "/api/code-browser/runtime-source"
            assert get(connection, route, params, token="wrong")[0] == 403
            status, mapping = get(connection, route, params)
            assert status == 200 and mapping["state"] == "available"
            assert mapping["selector"]["path"] == "demo.py"
            assert mapping["selector"]["line"] == 3
            assert mapping["selector"]["identity"] == ""  # No name-based function binding.
            for override in (
                {"report_sha256": "0" * 64},
                {"profile_report_sha256": "0" * 64},
                {"profile_identity_sha256": "0" * 64},
                {"unknown": "x"},
            ):
                assert get(connection, route, {**params, **override})[0] == 400
            status, unavailable = get(connection, route, {**params, "frame_id": native.frame_id})
            assert status == 200 and unavailable["state"] == "unavailable"
            assert unavailable["selector"] is None
            graph_params = {**pins, "layer": "runtime-calls", "path": "demo.py"}
            status, result = get(connection, "/api/code-browser/graph", graph_params)
            assert status == 200 and result["state"] == "available"
            assert result["graph"]["inputs"][0]["report_sha256"] == pins["profile_report_sha256"]
            assert result["graph"]["edges"][0]["label"] == "7 calls; edge timing unavailable"
            assert {link["node_id"] for link in result["links"]} == {frame.frame_id}
            assert (
                get(
                    connection, "/api/code-browser/graph", {**graph_params, "layer": "containment"}
                )[0]
                == 400
            )
        finally:
            connection.close()


@pytest.mark.parametrize("entry", ("browser-layer", "runtime-table"))
def test_runtime_frames_open_same_verified_source_browser(runtime_browser, entry):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service, profile_path, frame, native, _pins = runtime_browser
    before = source.read_bytes()
    with (
        served(
            create_server(
                service, source_root=source.parent, profile_report=profile_path, token=TOKEN
            )
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
            if entry == "browser-layer":
                reveal_navigation(page, "Browse code").click()
                page.locator('.code-browser-list [data-browse-identity^="source-file-"]').click()
                page.get_by_label("Relationship layer", exact=True).select_option("runtime-calls")
                graph = page.locator(".code-browser-map")
                playwright.expect(graph).to_contain_text(
                    "Code browser runtime observed calls retained"
                )
                graph.locator(f'[data-code-graph-node="{native.frame_id}"]').click()
                playwright.expect(graph).to_contain_text("No exact saved source identity")
                graph.locator(f'[data-code-graph-node="{frame.frame_id}"]').click()
            else:
                reveal_navigation(page, "Runtime").click()
                page.get_by_text("Browse recorded frame source", exact=True).click()
                page.locator(f'[data-runtime-browse-frame="{native.frame_id}"]').click()
                playwright.expect(page.locator("#runtime-results")).to_contain_text(
                    "no exact static source mapping"
                )
                page.locator(f'[data-runtime-browse-frame="{frame.frame_id}"]').click()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text("outer.inner")
            playwright.expect(page.locator(".code-browser-source")).to_contain_text(
                "bound: Source bytes match"
            )
            assert source.read_bytes() == before and not errors
        finally:
            browser.close()


def test_obsolete_runtime_frame_mapping_cannot_override_later_selection(runtime_browser):
    playwright = pytest.importorskip("playwright.sync_api")
    _source, service, profile_path, frame, native, pins = runtime_browser
    with (
        served(create_server(service, profile_report=profile_path, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("chromium binary is unavailable; no automatic installation")
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            page.evaluate(
                """async ({digest, token, frame}) => {
                  const {runtimeView} = await import('/runtime.js');
                  window.runtimeNavigations = [];
                  const api = async path => {
                    const response = await fetch(path, {headers: {'X-Code-Analysis-Token': token}});
                    const data = await response.json();
                    if (path.startsWith('/api/code-browser/runtime-source?')
                      && new URL(path, location.origin).searchParams.get('frame_id') === frame) {
                      return new Promise(resolve => {
                        window.releaseRuntimeMapping = () => resolve(data);
                      });
                    }
                    return data;
                  };
                  await runtimeView(document.querySelector('#content'), api, String,
                    new AbortController().signal, fetch, {staticReportSha256: digest,
                      onBrowseSource: location => window.runtimeNavigations.push(location)});
                }""",
                {"digest": pins["report_sha256"], "token": TOKEN, "frame": frame.frame_id},
            )
            page.get_by_text("Browse recorded frame source", exact=True).click()
            page.locator(f'[data-runtime-browse-frame="{frame.frame_id}"]').click()
            page.wait_for_function("() => typeof window.releaseRuntimeMapping === 'function'")
            page.locator(f'[data-runtime-browse-frame="{native.frame_id}"]').click()
            playwright.expect(page.locator("#runtime-results")).to_contain_text(
                "no exact static source mapping"
            )
            page.evaluate("() => window.releaseRuntimeMapping()")
            playwright.expect(
                page.locator(f'[data-runtime-browse-frame="{frame.frame_id}"]')
            ).to_be_enabled()
            assert page.evaluate("() => window.runtimeNavigations") == []
            playwright.expect(page.locator("#runtime-results")).to_contain_text(
                "no exact static source mapping"
            )
        finally:
            browser.close()


def test_runtime_browse_preserves_explicit_subject_over_innermost_line(runtime_browser):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service, profile_path, frame, _native, _pins = runtime_browser
    profile = RuntimeViewService.load(profile_path).profile
    outer = next(
        symbol
        for module in service.primary.modules
        for symbol in module.symbols
        if symbol.qualified_name == "outer"
    )
    module = service.primary.modules[0]
    identity = f"{module.name}:{outer.qualified_name}"
    updated = profile.model_copy(
        update={
            "frames": tuple(
                (
                    item.model_copy(update={"subject_id": identity})
                    if item.frame_id == frame.frame_id
                    else item
                )
                for item in profile.frames
            )
        }
    )
    profile_path.write_text(render_profile_json(updated, compact=True))
    with (
        served(
            create_server(
                service, source_root=source.parent, profile_report=profile_path, token=TOKEN
            )
        ) as server,
        playwright.sync_playwright() as runtime,
    ):
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("chromium binary is unavailable; no automatic installation")
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            reveal_navigation(page, "Runtime").click()
            page.get_by_text("Browse recorded frame source", exact=True).click()
            page.locator(f'[data-runtime-browse-frame="{frame.frame_id}"]').click()
            # Line 3 is also inside outer.inner; the saved subject must win.
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text("outer")
            assert page.evaluate("() => history.state.codeAnalysisBrowse.identity") == identity
            playwright.expect(
                page.locator(
                    f'.code-browser-map [data-code-graph-node="code-browse-contained:{identity}"]'
                )
            ).to_have_attribute("aria-pressed", "true")
        finally:
            browser.close()


def test_runtime_graph_identity_mismatch_is_explicit_not_endless_loading(runtime_browser):
    playwright = pytest.importorskip("playwright.sync_api")
    _source, service, profile_path, _frame, _native, pins = runtime_browser
    with (
        served(create_server(service, profile_report=profile_path, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip("chromium binary is unavailable; no automatic installation")
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            page.locator(".cards").wait_for()
            page.evaluate(
                """async ({digest, token}) => {
                  const {codeBrowser} = await import('/code_browser.js');
                  const api = async path => {
                    const response = await fetch(path, {headers: {'X-Code-Analysis-Token': token}});
                    const data = await response.json();
                    if (path.startsWith('/api/code-browser/graph?')) {
                      data.profile_report_sha256 = '0'.repeat(64);
                    }
                    return data;
                  };
                  await codeBrowser(document.querySelector('#content'), api,
                    new AbortController().signal, digest,
                    {report_sha256: digest, path: 'demo.py', layer: 'runtime-calls', revision: 0});
                }""",
                {"digest": pins["report_sha256"], "token": TOKEN},
            )
            graph = page.locator(".code-browser-map")
            playwright.expect(graph).to_contain_text("Saved graph identities disagree")
            playwright.expect(graph).not_to_contain_text("Loading saved relationships")
            assert graph.locator("svg").count() == 0
        finally:
            browser.close()
