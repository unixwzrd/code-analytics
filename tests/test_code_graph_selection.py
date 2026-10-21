"""Graph node activation updates outline and verified source without a redundant source button."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from browser_navigation import reveal_navigation
from test_graph_handoff_browser import served

from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.service import scan
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

TOKEN = "code-graph-selection-test"


@pytest.fixture
def browser_source(tmp_path):
    source = tmp_path / "demo.py"
    source.write_text(
        'def outer():\n    value = "<script>window.pwned = true</script>"\n'
        "    def inner():\n        return value\n    return inner()\n"
    )
    report = scan(discover_context(tmp_path, AnalysisConfig(test_roots=())))
    return source, ReportViewService(report)


def _launch(playwright):
    if not Path(playwright.chromium.executable_path).is_file():
        pytest.skip("chromium binary is unavailable; no automatic installation")
    return playwright.chromium.launch()


def _open_file_graph(page):
    page.locator(".cards").wait_for()
    reveal_navigation(page, "Browse code").click()
    page.locator('.code-browser-list [data-browse-identity^="source-file-"]').click()
    graph = page.locator(".code-browser-map")
    graph.locator("[data-code-graph-node]").first.wait_for()
    return graph


def test_pointer_graph_selection_updates_outline_and_source(browser_source):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    module = service.primary.modules[0]
    inner = next(symbol for symbol in module.symbols if symbol.name == "inner")
    identity = f"{module.name}:{inner.qualified_name}"
    node_id = f"code-browse-contained:{identity}"
    before = source.read_bytes()
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch(runtime)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            graph = _open_file_graph(page)
            graph.locator(f'[data-code-graph-node="{node_id}"]').click()
            playwright.expect(graph.locator(".code-browser-node-card h3")).to_have_text(
                inner.qualified_name
            )
            playwright.expect(
                page.locator(f'.code-browser-list [data-browse-identity="{identity}"]')
            ).to_have_attribute("aria-pressed", "true")
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                inner.qualified_name
            )
            playwright.expect(page.locator(".code-browser-source")).to_contain_text(
                "bound: Source bytes match"
            )
            assert source.read_bytes() == before and not errors
        finally:
            browser.close()


def test_keyboard_graph_selection_updates_outline_and_source(browser_source):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    module = service.primary.modules[0]
    outer = next(symbol for symbol in module.symbols if symbol.name == "outer")
    identity = f"{module.name}:{outer.qualified_name}"
    node_id = f"code-browse-contained:{identity}"
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch(runtime)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            graph = _open_file_graph(page)
            node = graph.locator(f'[data-code-graph-node="{node_id}"]')
            node.focus()
            page.keyboard.press("Enter")
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                outer.qualified_name
            )
            playwright.expect(
                page.locator(f'.code-browser-list [data-browse-identity="{identity}"]')
            ).to_have_attribute("aria-pressed", "true")
            inner = next(symbol for symbol in module.symbols if symbol.name == "inner")
            inner_node = f"code-browse-contained:{module.name}:{inner.qualified_name}"
            graph.locator(f'[data-code-graph-node="{inner_node}"]').focus()
            page.keyboard.press(" ")
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                inner.qualified_name
            )
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
def test_completed_source_load_preserves_newer_graph_keyboard_focus(browser_source, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    module = service.primary.modules[0]
    outer = next(symbol for symbol in module.symbols if symbol.name == "outer")
    inner = next(symbol for symbol in module.symbols if symbol.name == "inner")
    outer_identity = f"{module.name}:{outer.qualified_name}"
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            graph = _open_file_graph(page)
            held = []

            def hold_selected_relationships(route):
                identity = parse_qs(urlsplit(route.request.url).query).get("identity")
                if identity == [outer_identity]:
                    held.append(route)
                else:
                    route.continue_()

            page.route("**/api/code-browser/relationships?*", hold_selected_relationships)
            node = graph.locator(f'[data-code-graph-node="code-browse-contained:{outer_identity}"]')
            node.focus()
            with page.expect_request(
                lambda request: "/api/code-browser/relationships?" in request.url
                and parse_qs(urlsplit(request.url).query).get("identity") == [outer_identity]
            ):
                page.keyboard.press("Enter")
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                outer.qualified_name
            )
            next_node = graph.locator(
                f'[data-code-graph-node="code-browse-contained:{module.name}:{inner.qualified_name}"]'
            )
            next_node.focus()
            assert len(held) == 1
            held[0].continue_()
            page.locator(".source-relationships").wait_for()
            playwright.expect(next_node).to_be_focused()
            page.keyboard.press(" ")
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                inner.qualified_name
            )
        finally:
            browser.close()


def test_graph_selection_history_and_rejected_dirty_navigation(browser_source):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    module = service.primary.modules[0]
    outer = next(symbol for symbol in module.symbols if symbol.name == "outer")
    inner = next(symbol for symbol in module.symbols if symbol.name == "inner")
    outer_id = f"code-browse-contained:{module.name}:{outer.qualified_name}"
    inner_id = f"code-browse-contained:{module.name}:{inner.qualified_name}"
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch(runtime)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            graph = _open_file_graph(page)
            graph.locator(f'[data-code-graph-node="{inner_id}"]').click()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                inner.qualified_name
            )
            graph.locator(f'[data-code-graph-node="{outer_id}"]').click()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                outer.qualified_name
            )
            page.go_back()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                inner.qualified_name
            )
            page.go_forward()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                outer.qualified_name
            )

            page.get_by_role(
                "button", name="Edit draft from verified original source", exact=True
            ).click()
            editor = page.locator(".code-drafts-editor textarea")
            editor.wait_for()
            editor.fill("# dirty session draft\n")
            page.once("dialog", lambda dialog: dialog.dismiss())
            graph.locator(f'[data-code-graph-node="{inner_id}"]').click()
            playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                outer.qualified_name
            )
            playwright.expect(editor).to_have_value("# dirty session draft\n")
        finally:
            browser.close()


def test_unresolved_graph_node_stays_inspection_only(browser_source):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch(runtime)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            graph = _open_file_graph(page)
            graph.get_by_label("Relationship layer", exact=True).select_option("static-call-sites")
            graph.locator(
                '[data-relationship-layer="static-call-sites"] [data-code-graph-node]'
            ).first.wait_for()
            found = False
            for node in graph.locator("[data-code-graph-node]").all():
                heading_before = page.locator(".code-browser-source h3").text_content() or ""
                node.click()
                card = graph.locator(".code-browser-node-card")
                if not card.get_by_text(
                    "No exact saved source identity is bound to this node", exact=False
                ).count():
                    continue
                found = True
                playwright.expect(card).to_contain_text(
                    "No exact saved source identity is bound to this node"
                )
                assert (
                    graph.get_by_role("button", name="Browse recorded source", exact=True).count()
                    == 0
                )
                playwright.expect(page.locator(".code-browser-source h3")).to_have_text(
                    heading_before
                )
                break
            assert found, "expected an inspection-only call-site node"
        finally:
            browser.close()


def test_layer_change_removes_previous_graph_before_outline_response(browser_source):
    playwright = pytest.importorskip("playwright.sync_api")
    source, service = browser_source
    with (
        served(create_server(service, source_root=source.parent, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = _launch(runtime)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            graph = _open_file_graph(page)
            held = []
            page.route("**/api/code-browser?*", lambda route: held.append(route))
            with page.expect_request("**/api/code-browser?*"):
                graph.get_by_label("Relationship layer", exact=True).select_option(
                    "static-call-sites"
                )
            playwright.expect(graph.locator("[data-code-graph-node]")).to_have_count(0)
            playwright.expect(graph).to_contain_text("Loading selected relationships")
            assert len(held) == 1
            held[0].continue_()
            graph.locator(
                '[data-relationship-layer="static-call-sites"] [data-code-graph-node]'
            ).first.wait_for()
        finally:
            browser.close()
