"""Real-browser coverage for the read-only static-graph Web handoff."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from browser_navigation import reveal_navigation

from code_analysis.config import AnalysisConfig
from code_analysis.models import SourceLanguage
from code_analysis.profiling import render_profile_json
from code_analysis.project import discover_context
from code_analysis.projection_models import ProjectionCollapse
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json
from code_analysis.service import scan
from code_analysis.static_graph_browse import StaticGraphRequest
from code_analysis.ui_handoff import WebHandoffSelection
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_server import ReportHTTPServer, create_server
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import GenerationKind, commit_generation, prepare_workspace

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "graph-browser-handoff"


@pytest.fixture(scope="module")
def structure_service() -> ReportViewService:
    report = scan(discover_context(FIXTURES / "structure_project", AnalysisConfig()))
    return ReportViewService(report)


@contextmanager
def served(server: ReportHTTPServer) -> Iterator[ReportHTTPServer]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def saved_catalog_run(report: AnalysisReport, root: Path) -> tuple[Path, str]:
    """A workspace whose only saved generation is a report the session does not own."""
    workspace = prepare_workspace(
        discover_context(FIXTURES / "ordinary", AnalysisConfig()),
        explicit_path=root / "workspace",
    )
    payload = canonical_json(report).encode()
    manifest = commit_generation(
        workspace,
        kind=GenerationKind.ANALYSIS,
        artifacts={"report.json": payload},
        command=("scan",),
        interpreter_path="/python",
        portable_identity_sha256=hashlib.sha256(payload).hexdigest(),
    )
    return workspace.root, manifest.generation_id


def fragment(url: str) -> str:
    return "#" + urlsplit(url).fragment


class Session:
    """A page plus the request log needed to check exact handoff query fields."""

    def __init__(self, page) -> None:
        self.page = page
        self.page_errors: list[str] = []
        self.requests: list[str] = []
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.on("request", lambda request: self.requests.append(request.url))

    def query(self, path: str) -> dict[str, list[str]]:
        url = next(item for item in reversed(self.requests) if path in item)
        return parse_qs(urlsplit(url).query)

    def open_saved_run(self, identity: str) -> None:
        self.page.get_by_role("button", name="Saved runs", exact=True).click()
        self.page.locator("#baseline-run").select_option(identity)
        self.page.get_by_role("button", name="Open selected runs", exact=True).click()
        self.page.locator(".cards").wait_for()

    def open_saved_profile(self, identity: str) -> None:
        self.page.get_by_role("button", name="Saved runs", exact=True).click()
        self.page.locator("#baseline-profile").select_option(identity)
        self.page.get_by_role("button", name="Open selected runs", exact=True).click()
        self.page.locator(".cards").wait_for()

    def set_fragment(self, value: str) -> None:
        self.page.evaluate("value => { location.hash = value; }", value)


def _assert_no_source_or_collection_routes(requests: list[str]) -> None:
    for url in requests:
        path = urlsplit(url).path
        assert path != "/api/source"
        if path.startswith("/api/collection"):
            assert path == "/api/collection"


def test_browser_opens_dependency_graph_exact_id_handoff_with_keyboard_evidence(
    ordinary_report,
) -> None:
    """Bounded dependencies projection handoff keeps exact IDs through render and keyboard flow."""
    playwright = pytest.importorskip("playwright.sync_api")
    module_id = "alpha.a"
    service = ReportViewService(ordinary_report)
    request = StaticGraphRequest(
        name=ProjectionName.DEPENDENCIES,
        selected_ids=(module_id,),
    )
    selection = WebHandoffSelection(
        report_sha256=service.bootstrap().reports[0].report_sha256,
        kind="projection",
        graph_request=request,
    )
    with served(create_server(service, token=TOKEN)) as server:
        base = f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}"
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                session = Session(browser.new_page())
                page = session.page
                page.goto(selection.url(base))
                page.get_by_role("heading", name="Explore", exact=True).wait_for()
                page.locator("#projection-result svg").wait_for()
                assert page.get_by_label("Evidence view", exact=True).input_value() == (
                    ProjectionName.DEPENDENCIES.value
                )
                assert page.locator('input[name="select"]').input_value() == module_id
                projection = session.query("/api/projection")
                assert projection["name"] == [ProjectionName.DEPENDENCIES.value]
                assert projection["select"] == [module_id]

                graph = page.locator("#projection-result svg")
                node = graph.locator(f'[data-projection-node="{module_id}"]')
                playwright.expect(node).to_be_visible()
                nodes = graph.locator("[data-projection-node]").evaluate_all(
                    "items => items.map(item => item.dataset.projectionNode).sort()"
                )
                assert module_id in nodes

                assert not page.locator("#evidence-dialog").is_visible()
                node.focus()
                page.keyboard.press("Enter")
                dialog = page.get_by_role("dialog")
                dialog.wait_for()
                playwright.expect(page.locator("#evidence-content")).to_contain_text(
                    "Symbols and source"
                )
                page.keyboard.press("Escape")
                assert not dialog.is_visible()
                assert node.evaluate("element => element === document.activeElement")

                _assert_no_source_or_collection_routes(session.requests)
                assert not session.page_errors
            finally:
                browser.close()


def test_browser_opens_exact_static_graph_handoff_and_clears_it_on_navigation(
    structure_service,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    graph = build_projection(structure_service.primary, ProjectionName.STRUCTURE_LIFECYCLE_GRAPH)
    assert len(graph.nodes) >= 2
    request = StaticGraphRequest(
        name=ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
        selected_ids=tuple(sorted(node.node_id for node in graph.nodes[:2])),
        languages=(SourceLanguage.JAVASCRIPT, SourceLanguage.PYTHON),
        collapse=ProjectionCollapse.MODULE,
        max_records=30,
        max_nodes=12,
        max_edges=0,
    )
    selection = WebHandoffSelection(
        report_sha256=structure_service.bootstrap().reports[0].report_sha256,
        kind="projection",
        graph_request=request,
    )
    server = create_server(structure_service, token="graph-browser-handoff")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                observed: list[dict[str, list[str]]] = []
                page.on(
                    "request",
                    lambda browser_request: (
                        observed.append(parse_qs(urlsplit(browser_request.url).query))
                        if "/api/projection?" in browser_request.url
                        else None
                    ),
                )
                base = f"http://127.0.0.1:{server.server_address[1]}/?token=graph-browser-handoff"
                page.goto(selection.url(base))
                page.get_by_role("heading", name="Explore", exact=True).wait_for()
                page.locator("#projection-result h2").wait_for()
                assert not page.locator("#evidence-dialog").is_visible()
                assert page.locator('input[name="select"]').evaluate_all(
                    "inputs => inputs.map(input => input.value)"
                ) == list(request.selected_ids)
                assert page.locator('select[name="language"]').evaluate(
                    "select => [...select.selectedOptions].map(option => option.value)"
                ) == [item.value for item in request.languages]
                assert page.locator('input[name="max_edges"]').input_value() == "0"
                assert page.locator('select[name="collapse"]').input_value() == "module"
                assert observed[-1]["select"] == list(request.selected_ids)
                assert observed[-1]["language"] == [item.value for item in request.languages]
                assert observed[-1]["max_edges"] == ["0"]

                reveal_navigation(page, "Modules").click()
                page.get_by_role("columnheader", name="Module", exact=True).wait_for()
                reveal_navigation(page, "Explore evidence").click()
                page.get_by_label("Evidence view", exact=True).wait_for()
                assert page.locator('input[name="select"]').count() == 1
                assert page.locator('input[name="select"]').input_value() == ""
                assert page.locator('input[name="max_edges"]').input_value() == "100"

                page.goto(WebHandoffSelection(report_sha256="0" * 64).url(base))
                page.locator("#content").get_by_text(
                    "handoff report identity does not match", exact=False
                ).wait_for()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_handoff_after_a_saved_run_selection_rebinds_the_owned_report(
    ordinary_report, structure_service, tmp_path
) -> None:
    """A catalog selection may neither travel with the handoff nor outlive it."""
    playwright = pytest.importorskip("playwright.sync_api")
    workspace, identity = saved_catalog_run(structure_service.primary, tmp_path)
    owned = ReportViewService(ordinary_report)
    owned_sha256 = owned.bootstrap().reports[0].report_sha256
    selection = WebHandoffSelection(report_sha256=owned_sha256)
    with served(create_server(owned, workspace=workspace, token=TOKEN)) as server:
        base = f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}"
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                session = Session(browser.new_page())
                page = session.page
                page.goto(base)
                page.locator(".cards").wait_for()
                session.open_saved_run(identity)
                assert page.locator("dd.mono").inner_text() == (
                    structure_service.bootstrap().reports[0].report_sha256
                )

                bootstrap_requests = sum("/api/bootstrap" in url for url in session.requests)
                session.set_fragment(fragment(selection.url(base)))
                playwright.expect(page.locator("dd.mono")).to_have_text(owned_sha256)
                # Restoration is atomic from the owned snapshot, not an async fetch
                # whose abort handler could overwrite a newer catalog selection.
                assert (
                    sum("/api/bootstrap" in url for url in session.requests) == bootstrap_requests
                )

                # The handoff is validated against the owned report by its exact fields.
                assert session.query("/api/handoff") == {
                    "report_sha256": [owned_sha256],
                    "kind": ["report"],
                }
                # Later browsing follows the restored report, not the superseded catalog run.
                reveal_navigation(page, "Modules").click()
                page.get_by_role("columnheader", name="Module", exact=True).wait_for()
                modules = session.query("/api/modules")
                assert "run" not in modules and "compare_run" not in modules
                assert not session.page_errors
            finally:
                browser.close()


def test_static_handoff_after_saved_profile_clears_catalog_from_runtime_routes(
    ordinary_report, tmp_path
) -> None:
    """A verified static handoff must drop saved-profile catalog params like runtime handoff."""
    playwright = pytest.importorskip("playwright.sync_api")
    workspace = prepare_workspace(
        discover_context(FIXTURES / "ordinary", AnalysisConfig()),
        explicit_path=tmp_path / "workspace",
    )
    from test_code_browse_runtime import _build_profile, _project_frame

    template = RuntimeViewService.load(
        Path(__file__).parent / "golden" / "phase4-profile-report.json"
    ).profile
    frame = _project_frame(
        template,
        frame_id="profile-frame-" + "a" * 20,
        path="alpha/a.py",
        line=20,
        function="calculate",
        subject_id="alpha.a:calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    manifest = commit_generation(
        workspace,
        kind=GenerationKind.PROFILE,
        artifacts={
            "profile.json": render_profile_json(profile, compact=True).encode(),
        },
        command=("profile",),
        interpreter_path="/python",
        portable_identity_sha256=profile.profile_identity_sha256,
    )
    saved = (
        workspace.kind_directory(GenerationKind.PROFILE) / manifest.generation_id / "profile.json"
    )
    owned = ReportViewService(ordinary_report)
    owned_sha256 = owned.bootstrap().reports[0].report_sha256
    selection = WebHandoffSelection(report_sha256=owned_sha256)
    with served(
        create_server(
            owned,
            workspace=workspace.root,
            profile_report=saved,
            source_root=FIXTURES / "ordinary",
            token=TOKEN,
        )
    ) as server:
        base = f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}"
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                session = Session(browser.new_page())
                page = session.page
                page.goto(base)
                page.locator(".cards").wait_for()
                session.open_saved_profile(manifest.generation_id)
                reveal_navigation(page, "Runtime").click()
                page.get_by_role("columnheader", name="function", exact=True).wait_for()
                assert session.query("/api/runtime")["profile_run"] == [manifest.generation_id]

                session.requests.clear()
                session.set_fragment(fragment(selection.url(base)))
                playwright.expect(page.locator("dd.mono")).to_have_text(owned_sha256)

                reveal_navigation(page, "Runtime").click()
                page.get_by_role("columnheader", name="function", exact=True).wait_for()
                runtime_urls = [
                    item
                    for item in session.requests
                    if "/api/runtime" in item and "/api/runtime/handoff" not in item
                ]
                assert runtime_urls
                for url in runtime_urls:
                    assert "profile_run" not in url and "compare_profile_run" not in url

                session.requests.clear()
                page.get_by_text("Browse recorded frame source", exact=True).click()
                page.locator("[data-runtime-browse-frame]").first.click()
                page.locator(".code-browser-source").wait_for()
                runtime_source_urls = [
                    item for item in session.requests if "/api/code-browser/runtime-source" in item
                ]
                assert runtime_source_urls
                for url in runtime_source_urls:
                    assert "profile_run" not in url and "compare_profile_run" not in url

                session.requests.clear()
                with page.expect_response(
                    lambda response: "/api/code-browser/graph" in response.url
                    and "runtime-calls" in response.url
                ):
                    page.get_by_label("Relationship layer", exact=True).select_option(
                        "runtime-calls"
                    )
                page.locator(".code-browser-map").get_by_text(
                    "Loading saved relationships", exact=False
                ).wait_for(state="hidden")
                runtime_graph_urls = [
                    item
                    for item in session.requests
                    if "/api/code-browser/graph" in item and "runtime-calls" in item
                ]
                assert runtime_graph_urls
                for url in runtime_graph_urls:
                    assert "profile_run" not in url and "compare_profile_run" not in url
                assert not session.page_errors
            finally:
                browser.close()


def test_mismatched_handoff_after_a_saved_run_selection_is_refused_without_rebinding(
    ordinary_report, structure_service, tmp_path
) -> None:
    """A refused handoff may not switch report authority to either report."""
    playwright = pytest.importorskip("playwright.sync_api")
    workspace, identity = saved_catalog_run(structure_service.primary, tmp_path)
    catalog_sha256 = structure_service.bootstrap().reports[0].report_sha256
    selection = WebHandoffSelection(report_sha256=catalog_sha256)
    with served(
        create_server(ReportViewService(ordinary_report), workspace=workspace, token=TOKEN)
    ) as server:
        base = f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}"
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                session = Session(browser.new_page())
                page = session.page
                page.goto(base)
                page.locator(".cards").wait_for()
                session.open_saved_run(identity)

                # The selected catalog run is not the owned report, so its digest is refused.
                session.set_fragment(fragment(selection.url(base)))
                page.locator("#content").get_by_text(
                    "handoff report identity does not match", exact=False
                ).wait_for()
                assert session.query("/api/handoff") == {
                    "report_sha256": [catalog_sha256],
                    "kind": ["report"],
                }

                # The prior selection is intact: nothing was broadened or silently switched.
                reveal_navigation(page, "Modules").click()
                page.get_by_role("columnheader", name="Module", exact=True).wait_for()
                assert session.query("/api/modules")["run"] == [identity]
                assert session.query("/api/modules")["compare_run"] == ["none"]
                page.get_by_role("button", name="Overview", exact=True).click()
                page.locator(".cards").wait_for()
                assert page.locator("dd.mono").inner_text() == catalog_sha256
                assert not session.page_errors
            finally:
                browser.close()
