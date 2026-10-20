from __future__ import annotations

import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from browser_navigation import reveal_navigation

from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName
from code_analysis.service import scan
from code_analysis.ui_projections import (
    UIProjectionRequest,
    graph_node_selection,
    project_report,
)
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

FIXTURES = Path(__file__).parent / "fixtures"


def _open_projection_graph(page, projection: str) -> None:
    page.locator(".cards").wait_for()
    reveal_navigation(page, "Explore evidence").click()
    page.get_by_label("Evidence view", exact=True).select_option(projection)
    page.get_by_role("button", name="Show evidence", exact=True).click()
    page.locator("#projection-result [data-projection-node]").first.wait_for()


def _activate_graph_node_with_keyboard(page, node) -> None:
    node.focus()
    page.keyboard.press("Enter")


def _parse_projection_node_request(url: str) -> dict[str, list[str]]:
    parsed = urlparse(url)
    if not parsed.path.endswith("/api/projection-node"):
        return {}
    return parse_qs(parsed.query)


@pytest.mark.parametrize(
    ("fixture", "projection", "node_prefix", "activation", "preview_text"),
    (
        ("ordinary", "symbol-graph", "alpha.a:", "Enter", "bound:"),
        ("structure_project", "ownership-alias-flow", "structure-", "click", "bound:"),
        ("structure_project", "structure-lifecycle-graph", "state-", "Enter", "bound:"),
        ("structure_project", "conversion-graph", "subject-", " ", "bound:"),
        (
            "test_impact_project",
            "test-impact-graph",
            "test-case-",
            "Enter",
            "source path is absent from the selected report",
        ),
        (
            "interface_project",
            "ownership-contract-flow",
            "interface-endpoint-",
            " ",
            "bound:",
        ),
    ),
)
def test_projection_nodes_resolve_saved_source_without_changing_selection(
    fixture: str,
    projection: str,
    node_prefix: str,
    activation: str,
    preview_text: str,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    source_root = FIXTURES / fixture
    report = scan(discover_context(source_root, AnalysisConfig()))
    server = create_server(
        ReportViewService(report), source_root=source_root, token="projection-node-test"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                node_requests: list[str] = []
                page.on(
                    "request",
                    lambda request: (
                        node_requests.append(request.url)
                        if "/api/projection-node?" in request.url
                        else None
                    ),
                )
                page.goto(
                    f"http://127.0.0.1:{server.server_address[1]}/?token=projection-node-test"
                )
                page.locator(".cards").wait_for()
                reveal_navigation(page, "Explore evidence").click()
                page.get_by_label("Evidence view", exact=True).select_option(projection)
                page.get_by_role("button", name="Show evidence", exact=True).click()
                node = page.locator(
                    f'#projection-result [data-projection-node^="{node_prefix}"]'
                ).first
                node.wait_for()
                if projection == "conversion-graph":
                    relationships = page.locator(".projection-edges")
                    relationships.locator("summary").focus()
                    page.keyboard.press("Enter")
                    playwright.expect(relationships).to_have_attribute("open", "")
                    assert relationships.locator("tbody tr").count() == 2
                    assert all(
                        "flow-" in row
                        for row in relationships.locator("tbody tr").all_inner_texts()
                    )
                selected = page.get_by_label("Exact selection ID")
                assert selected.input_value() == ""
                if activation == "click":
                    node.locator("circle").click(force=True)
                else:
                    node.focus()
                    page.keyboard.press(activation)
                page.get_by_role("dialog").wait_for()
                playwright.expect(page.locator("#evidence-content")).to_contain_text(
                    "Symbols and source"
                )
                preview = page.get_by_role("button", name="Preview source", exact=True).first
                preview.click()
                playwright.expect(page.locator(".source-preview").first).to_contain_text(
                    preview_text
                )
                assert selected.input_value() == ""
                assert any(
                    f"name={projection}" in request
                    and "projection_id=projection-" in request
                    and "node=" in request
                    for request in node_requests
                )
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_contract_boundary_graph_contract_node_opens_contract_evidence_via_keyboard() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    source_root = FIXTURES / "contract_golden"
    report = scan(discover_context(source_root, AnalysisConfig()))
    request = UIProjectionRequest(name=ProjectionName.CONTRACT_BOUNDARY_GRAPH)
    graph = project_report(report, request)
    contract = next(node for node in graph.nodes if node.node_id.startswith("contract-"))
    selection = graph_node_selection(report, request, graph.projection_id, contract.node_id)
    assert selection.navigation_identity == contract.node_id
    expected_line = (source_root / "golden_contract.py").read_text(encoding="utf-8").splitlines()[5]
    server = create_server(
        ReportViewService(report),
        source_root=source_root,
        token="projection-node-contract-boundary",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                node_requests: list[str] = []
                page.on(
                    "request",
                    lambda request: (
                        node_requests.append(request.url)
                        if "/api/projection-node?" in request.url
                        else None
                    ),
                )
                page.goto(
                    f"http://127.0.0.1:{server.server_address[1]}/"
                    "?token=projection-node-contract-boundary"
                )
                _open_projection_graph(page, ProjectionName.CONTRACT_BOUNDARY_GRAPH.value)
                node = page.locator(
                    f'#projection-result [data-projection-node="{contract.node_id}"]'
                )
                node.wait_for()
                selected = page.get_by_label("Exact selection ID")
                assert selected.input_value() == ""
                with page.expect_response(
                    lambda response: "/api/projection-node" in response.url
                ) as response_info:
                    _activate_graph_node_with_keyboard(page, node)
                resolution = response_info.value.json()
                page.get_by_role("dialog").wait_for()
                playwright.expect(page.locator("#evidence-title")).to_have_text(
                    selection.navigation_identity
                )
                playwright.expect(page.locator("#evidence-content")).to_contain_text(
                    "contract-evidence"
                )
                playwright.expect(page.locator("#evidence-content")).to_contain_text(
                    "golden_contract.py"
                )
                preview = page.get_by_role("button", name="Preview source", exact=True).first
                preview.click()
                playwright.expect(page.locator(".source-preview").first).to_contain_text("bound:")
                playwright.expect(page.locator(".source-preview").first).to_contain_text(
                    expected_line
                )
                assert selected.input_value() == ""
                assert len(node_requests) == 1
                params = _parse_projection_node_request(node_requests[0])
                assert params["name"] == [ProjectionName.CONTRACT_BOUNDARY_GRAPH.value]
                assert params["projection_id"] == [graph.projection_id]
                assert params["node"] == [contract.node_id]
                assert resolution == selection.model_dump(mode="json")
                page.keyboard.press("Escape")
                playwright.expect(page.get_by_role("dialog")).not_to_be_visible()
                playwright.expect(node).to_be_focused()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_test_impact_graph_resolved_application_subject_opens_bound_source_via_keyboard() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    source_root = FIXTURES / "test_impact_project"
    report = scan(discover_context(source_root, AnalysisConfig()))
    request = UIProjectionRequest(name=ProjectionName.TEST_IMPACT_GRAPH)
    graph = project_report(report, request)
    subject = next(node for node in graph.nodes if node.kind == "application-symbol")
    selection = graph_node_selection(report, request, graph.projection_id, subject.node_id)
    assert selection.navigation_identity == subject.node_id
    expected_line = (
        (source_root / "src" / "sample" / "core.py").read_text(encoding="utf-8").splitlines()[4]
    )
    server = create_server(
        ReportViewService(report),
        source_root=source_root,
        token="projection-node-test-impact-subject",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                node_requests: list[str] = []
                page.on(
                    "request",
                    lambda request: (
                        node_requests.append(request.url)
                        if "/api/projection-node?" in request.url
                        else None
                    ),
                )
                page.goto(
                    f"http://127.0.0.1:{server.server_address[1]}/"
                    "?token=projection-node-test-impact-subject"
                )
                _open_projection_graph(page, ProjectionName.TEST_IMPACT_GRAPH.value)
                node = page.locator(
                    f'#projection-result [data-projection-node="{subject.node_id}"]'
                )
                node.wait_for()
                selected = page.get_by_label("Exact selection ID")
                assert selected.input_value() == ""
                with page.expect_response(
                    lambda response: "/api/projection-node" in response.url
                ) as response_info:
                    _activate_graph_node_with_keyboard(page, node)
                resolution = response_info.value.json()
                page.get_by_role("dialog").wait_for()
                playwright.expect(page.locator("#evidence-title")).to_have_text(
                    selection.navigation_identity
                )
                playwright.expect(page.locator("#evidence-content")).to_contain_text(subject.label)
                playwright.expect(page.locator("#evidence-content")).to_contain_text(
                    "src/sample/core.py"
                )
                preview = page.get_by_role("button", name="Preview source", exact=True).first
                preview.click()
                playwright.expect(page.locator(".source-preview").first).to_contain_text("bound:")
                playwright.expect(page.locator(".source-preview").first).to_contain_text(
                    expected_line
                )
                assert selected.input_value() == ""
                assert len(node_requests) == 1
                params = _parse_projection_node_request(node_requests[0])
                assert params["name"] == [ProjectionName.TEST_IMPACT_GRAPH.value]
                assert params["projection_id"] == [graph.projection_id]
                assert params["node"] == [subject.node_id]
                assert resolution == selection.model_dump(mode="json")
                page.keyboard.press("Escape")
                playwright.expect(page.get_by_role("dialog")).not_to_be_visible()
                playwright.expect(node).to_be_focused()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_unavailable_projection_node_shows_reason_without_empty_source_dialog() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report = scan(discover_context(FIXTURES / "test_impact_project", AnalysisConfig()))
    server = create_server(ReportViewService(report), token="projection-node-unavailable")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(
                    f"http://127.0.0.1:{server.server_address[1]}/"
                    "?token=projection-node-unavailable"
                )
                page.locator(".cards").wait_for()
                reveal_navigation(page, "Explore evidence").click()
                page.get_by_label("Evidence view", exact=True).select_option("test-impact-graph")
                page.get_by_role("button", name="Show evidence", exact=True).click()
                node = page.locator(
                    '#projection-result [data-projection-node^="test-target-"]'
                ).first
                node.wait_for()
                node.dispatch_event("click")
                status = page.locator("#projection-node-status")
                playwright.expect(status).not_to_have_text("")
                playwright.expect(status).to_have_text(
                    "This logical group or unresolved target has no proven source declaration."
                )
                assert not page.get_by_role("dialog").is_visible()
                assert page.get_by_label("Exact selection ID").input_value() == ""
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_stale_projection_node_resolution_cannot_open_the_previous_node(monkeypatch) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    from code_analysis import web_server

    report = scan(discover_context(FIXTURES / "structure_project", AnalysisConfig()))
    original = web_server.graph_node_selection
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    first = True

    def delayed(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            started.set()
            try:
                assert release.wait(timeout=5)
                return original(*args, **kwargs)
            finally:
                completed.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(web_server, "graph_node_selection", delayed)
    server = create_server(ReportViewService(report), token="projection-node-stale")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(
                    f"http://127.0.0.1:{server.server_address[1]}/?token=projection-node-stale"
                )
                page.locator(".cards").wait_for()
                reveal_navigation(page, "Explore evidence").click()
                page.get_by_label("Evidence view", exact=True).select_option("ownership-alias-flow")
                page.get_by_role("button", name="Show evidence", exact=True).click()
                nodes = page.locator('#projection-result [data-projection-node^="structure-"]')
                nodes.nth(0).wait_for()
                first_id = nodes.nth(0).get_attribute("data-projection-node")
                nodes.nth(0).dispatch_event("click")
                assert started.wait(timeout=5)
                page.get_by_label("Evidence view", exact=True).select_option("modules")
                page.get_by_role("button", name="Show evidence", exact=True).click()
                release.set()
                assert completed.wait(timeout=5)
                page.get_by_role("heading", name="Module metrics", exact=True).wait_for()
                assert not page.get_by_role("dialog").is_visible()
                assert page.get_by_label("Exact selection ID").input_value() == ""
                assert first_id not in page.locator("#evidence-title").inner_text()
            finally:
                release.set()
                browser.close()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
