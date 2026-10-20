"""Real-browser coverage for the read-only runtime graph Web handoff.

Every session here is started from a preloaded profile snapshot, so the receiver
has no analysis report, no workspace and no source root to read.
"""

from __future__ import annotations

import json
import marshal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from browser_navigation import reveal_navigation

from code_analysis.config import AnalysisConfig
from code_analysis.profile_ingestion import ingest_profiles
from code_analysis.profile_models import ProfileReport
from code_analysis.profiling import render_profile_json
from code_analysis.project import discover_context
from code_analysis.projections.runtime import build_runtime_graph
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_identity import CollectionMetadata, RuntimeRoot
from code_analysis.runtime_models import CollectionIdentity, RuntimeSelection
from code_analysis.ui_handoff import (
    RuntimeHandoffSelection,
    WebHandoffSelection,
    canonical_profile_sha256,
)
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_server import ReportHTTPServer, create_server
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import GenerationKind, commit_generation, prepare_workspace

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "runtime-handoff-browser"
DRIFT = "Call graph bounds changed since this graph was rendered"


@pytest.fixture(scope="module")
def profile_report(tmp_path_factory) -> ProfileReport:
    """Two project frames and one native frame, so ``project_only`` omits evidence."""
    root = tmp_path_factory.mktemp("workload") / "declared"
    (root / "pkg").mkdir(parents=True)
    handler = (str(root / "pkg" / "app.py"), 10, "handler")
    stats = {
        handler: (3, 3, 0.4, 1.5, {}),
        (str(root / "pkg" / "helper.py"), 4, "helper"): (
            5,
            5,
            0.3,
            0.6,
            {handler: (5, 5, 0.3, 0.6)},
        ),
        ("", 0, "len"): (7, 7, 0.2, 0.2, {handler: (7, 7, 0.2, 0.2)}),
    }
    capture = root / "capture.prof"
    capture.write_bytes(marshal.dumps(stats))
    report, _ = ingest_profiles(
        (capture,),
        metadata=CollectionMetadata(
            collection=CollectionIdentity(),
            roots=(RuntimeRoot(physical=root, logical="project", classification="project"),),
        ),
    )
    return report


def selection(report: ProfileReport, **bounds: object) -> RuntimeHandoffSelection:
    return RuntimeHandoffSelection(
        profile_report_sha256=canonical_profile_sha256(report),
        profile_identity_sha256=report.profile_identity_sha256,
        graph_request=RuntimeGraphRequest(**bounds),
    )


def expected_graph(report: ProfileReport, **bounds: object) -> dict:
    request = RuntimeGraphRequest(**bounds)
    return build_runtime_graph(report, RuntimeSelection(**request.model_dump())).model_dump(
        mode="json"
    )


def fragment(url: str) -> str:
    return "#" + urlsplit(url).fragment


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


@contextmanager
def runtime_only_session(report: ProfileReport) -> Iterator[ReportHTTPServer]:
    with served(
        create_server(None, token=TOKEN, runtime_service=RuntimeViewService(report))
    ) as server:
        yield server


def base_url(server: ReportHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}"


class Session:
    """A page plus the request log and console faults needed for exactness checks."""

    def __init__(self, page) -> None:
        self.page = page
        self.page_errors: list[str] = []
        self.console_errors: list[str] = []
        self.requests: list[str] = []
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                self.console_errors.append(message.text) if message.type == "error" else None
            ),
        )
        page.on("request", lambda request: self.requests.append(request.url))

    @property
    def faults(self) -> list[str]:
        """Refused requests are reported by the browser itself; scripting faults are not."""
        return self.page_errors + [
            item for item in self.console_errors if "Failed to load resource" not in item
        ]

    def query(self, path: str) -> dict[str, list[str]]:
        url = next(item for item in reversed(self.requests) if path in item)
        return parse_qs(urlsplit(url).query)

    def count(self, path: str) -> int:
        return len([item for item in self.requests if path in item])

    def set_fragment(self, value: str) -> None:
        self.page.evaluate("value => { location.hash = value; }", value)

    def bounds(self) -> str:
        return self.page.locator("#runtime-graph-bounds").inner_text()

    def controls(self) -> tuple[str, str, bool]:
        return (
            self.page.get_by_label("Maximum nodes", exact=True).input_value(),
            self.page.get_by_label("Maximum edges", exact=True).input_value(),
            self.page.get_by_label("Project frames only", exact=True).is_checked(),
        )


def test_runtime_only_bootstrap_applies_the_exact_handed_off_selection(profile_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    handoff = selection(profile_report, max_nodes=3, max_edges=2)
    with runtime_only_session(profile_report) as server, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            session = Session(browser.new_page())
            page = session.page
            page.goto(handoff.url(base_url(server)))
            page.get_by_role("heading", name="Runtime", exact=True).wait_for()
            page.get_by_role("img", name="Observed runtime calls", exact=True).wait_for()

            # No analysis report is fabricated for the receiver.
            assert "no static report" in page.locator("#report-state").inner_text()
            content = page.locator("#content").inner_text()
            assert "Does not match the selected static report" in content
            assert "no cross-report source binding is claimed" in content
            assert "3 of 3 nodes; 2 of 2 edges. Omitted: 0 nodes and 0 edges." in content
            assert "this graph is not exhaustive" in content

            # The handed-off bounds are applied exactly and stay editable.
            view = page.get_by_label("Runtime view", exact=True)
            assert view.input_value() == "graph"
            assert session.controls() == ("3", "2", False)
            assert session.query("/api/runtime/graph") == {
                "max_nodes": ["3"],
                "max_edges": ["2"],
                "project_only": ["false"],
            }
            assert "Rendered bounds: 3 nodes, 2 edges, project frames only: no." in (
                session.bounds()
            )

            # Baseline only: a single profile never offers a comparison section.
            assert "comparison" not in view.evaluate(
                "select => [...select.options].map(option => option.value)"
            )

            with page.expect_download() as transfer:
                page.get_by_role("button", name="Download JSON", exact=True).click()
            payload = json.loads(Path(transfer.value.path()).read_text(encoding="utf-8"))
            assert payload == expected_graph(profile_report, max_nodes=3, max_edges=2)
            download = session.query("/api/runtime/download")
            assert download["format"] == ["json"]
            assert download["max_nodes"] == ["3"] and download["max_edges"] == ["2"]
            assert download["project_only"] == ["false"]
            assert not session.faults
        finally:
            browser.close()


def test_later_fragments_supersede_and_refused_fragments_leave_no_graph(profile_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    first = selection(profile_report, max_nodes=3, max_edges=2)
    scoped = selection(profile_report, max_nodes=3, max_edges=2, project_only=True)
    mismatched = RuntimeHandoffSelection(
        profile_report_sha256="0" * 64,
        profile_identity_sha256=profile_report.profile_identity_sha256,
        graph_request=RuntimeGraphRequest(max_nodes=3, max_edges=2),
    )
    with runtime_only_session(profile_report) as server:
        base = base_url(server)
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                session = Session(browser.new_page())
                page = session.page
                page.goto(first.url(base))
                page.get_by_role("img", name="Observed runtime calls", exact=True).wait_for()
                assert "project frames only: no." in session.bounds()

                # A second fragment supersedes the first selection in place.
                session.set_fragment(fragment(scoped.url(base)))
                playwright.expect(page.locator("#runtime-graph-bounds")).to_contain_text(
                    "Rendered bounds: 3 nodes, 2 edges, project frames only: yes."
                )
                assert session.controls() == ("3", "2", True)
                assert session.query("/api/runtime/graph")["project_only"] == ["true"]
                content = page.locator("#content").inner_text()
                assert "2 of 3 nodes; 1 of 2 edges. Omitted: 1 nodes and 1 edges." in content
                page.locator("#runtime-results .projection-limitations summary").click()
                page.get_by_text(
                    "Only project-classified frames are retained", exact=False
                ).wait_for()
                with page.expect_download() as transfer:
                    page.get_by_role("button", name="Download JSON", exact=True).click()
                assert json.loads(Path(transfer.value.path()).read_text(encoding="utf-8")) == (
                    expected_graph(profile_report, max_nodes=3, max_edges=2, project_only=True)
                )

                # A mismatched profile digest is refused with no graph and no download left.
                session.set_fragment(fragment(mismatched.url(base)))
                page.get_by_text("handoff profile identity does not match", exact=False).wait_for()
                assert page.locator("#content svg").count() == 0
                assert page.locator("[data-runtime-format]").count() == 0
                assert page.locator("#runtime-graph-bounds").count() == 0

                # A superseded valid selection cannot return after the refusal either.
                page.evaluate(
                    "([valid, refused]) => { location.hash = valid; location.hash = refused; }",
                    [fragment(first.url(base)), fragment(mismatched.url(base))],
                )
                page.get_by_text("handoff profile identity does not match", exact=False).wait_for()
                page.wait_for_timeout(250)
                assert page.locator("#runtime-graph-bounds").count() == 0
                assert page.locator("[data-runtime-format]").count() == 0

                # Unknown and duplicated fragment fields fail visibly, never broadened.
                session.set_fragment(f"{fragment(first.url(base))}&kind=report")
                page.get_by_text("unsupported runtime handoff parameters", exact=False).wait_for()
                duplicate = (
                    f"{fragment(first.url(base))}"
                    f"&profile_report_sha256={canonical_profile_sha256(profile_report)}"
                )
                session.set_fragment(duplicate)
                page.get_by_text("must be supplied once", exact=False).wait_for()
                assert page.locator("#runtime-graph-bounds").count() == 0
                assert not session.faults
            finally:
                browser.close()


def test_unapplied_graph_controls_invalidate_downloads_until_reapplied(profile_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    handoff = selection(profile_report, max_nodes=3, max_edges=2)
    with runtime_only_session(profile_report) as server, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            session = Session(browser.new_page())
            page = session.page
            page.goto(handoff.url(base_url(server)))
            page.get_by_role("img", name="Observed runtime calls", exact=True).wait_for()
            status = page.locator(".runtime-download-status")
            download = page.get_by_role("button", name="Download JSON", exact=True)

            # A value set without an input event is still refused at download time.
            page.get_by_label("Maximum edges", exact=True).evaluate(
                "input => { input.value = '1'; }"
            )
            assert not download.is_disabled()
            before = session.count("/api/runtime/download")
            download.click()
            playwright.expect(status).to_contain_text(DRIFT)
            assert session.count("/api/runtime/download") == before
            assert "Rendered bounds: 3 nodes, 2 edges" in session.bounds()

            # Editing a control invalidates the download affordance outright.
            page.get_by_label("Maximum nodes", exact=True).fill("3")
            playwright.expect(status).to_contain_text(DRIFT)
            assert download.is_disabled()
            assert "Rendered bounds: 3 nodes, 2 edges" in session.bounds()

            # Applying the controls rebinds both the graph and its download.
            page.get_by_role("button", name="Show runtime evidence", exact=True).click()
            playwright.expect(page.locator("#runtime-graph-bounds")).to_contain_text(
                "Rendered bounds: 3 nodes, 1 edges, project frames only: no."
            )
            with page.expect_download() as transfer:
                page.get_by_role("button", name="Download JSON", exact=True).click()
            assert json.loads(Path(transfer.value.path()).read_text(encoding="utf-8")) == (
                expected_graph(profile_report, max_nodes=3, max_edges=1)
            )
            assert session.query("/api/runtime/download")["max_edges"] == ["1"]
            assert not session.faults
        finally:
            browser.close()


def test_controls_changed_during_graph_load_leave_download_disabled(profile_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    handoff = selection(profile_report, max_nodes=3, max_edges=2)
    with runtime_only_session(profile_report) as server, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            held = []
            page.route("**/api/runtime/graph?*", lambda route: held.append(route))
            page.goto(handoff.url(base_url(server)))
            page.get_by_label("Maximum nodes", exact=True).fill("2")
            assert len(held) == 1
            held[0].fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(expected_graph(profile_report, max_nodes=3, max_edges=2)),
            )
            page.get_by_role("img", name="Observed runtime calls", exact=True).wait_for()
            assert "Rendered bounds: 3 nodes" in page.locator("#runtime-graph-bounds").inner_text()
            assert page.get_by_label("Maximum nodes", exact=True).input_value() == "2"
            assert page.get_by_role("button", name="Download JSON", exact=True).is_disabled()
            playwright.expect(page.locator(".runtime-download-status")).to_contain_text(DRIFT)
        finally:
            browser.close()


def test_refused_handoff_cannot_be_replaced_by_held_graph_response(profile_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    handoff = selection(profile_report, max_nodes=3, max_edges=2)
    refused = handoff.model_copy(update={"profile_report_sha256": "0" * 64})
    with runtime_only_session(profile_report) as server, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            session = Session(browser.new_page())
            page = session.page
            held = []
            page.route("**/api/runtime/graph?*", lambda route: held.append(route))
            page.goto(handoff.url(base_url(server)))
            page.get_by_label("Maximum nodes", exact=True).wait_for()
            assert len(held) == 1
            session.set_fragment(fragment(refused.url(base_url(server))))
            error = page.get_by_text("handoff profile identity does not match", exact=False)
            error.wait_for()
            held[0].fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(expected_graph(profile_report, max_nodes=3, max_edges=2)),
            )
            page.wait_for_timeout(250)
            assert error.is_visible()
            assert page.locator("#content svg").count() == 0
            assert page.locator("[data-runtime-format]").count() == 0
            assert page.locator("#runtime-graph-bounds").count() == 0
            assert not session.faults
        finally:
            browser.close()


def test_ordinary_navigation_clears_the_runtime_handoff_selection(profile_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    handoff = selection(profile_report, max_nodes=3, max_edges=2, project_only=True)
    with runtime_only_session(profile_report) as server, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            session = Session(browser.new_page())
            page = session.page
            page.goto(handoff.url(base_url(server)))
            page.get_by_role("img", name="Observed runtime calls", exact=True).wait_for()
            assert session.controls() == ("3", "2", True)

            page.get_by_role("button", name="Saved runs", exact=True).click()
            page.get_by_role("heading", name="Runs", exact=True).wait_for()
            reveal_navigation(page, "Runtime").click()
            page.get_by_role("columnheader", name="function", exact=True).wait_for()

            # The fragment survives in the URL; the selection must not be re-derived.
            assert "profile_report_sha256" in page.evaluate("location.hash")
            assert page.locator("#runtime-graph-controls").is_hidden()
            assert session.controls() == ("60", "120", False)
            page.get_by_label("Runtime view", exact=True).select_option("graph")
            page.get_by_role("button", name="Show runtime evidence", exact=True).click()
            page.get_by_role("img", name="Observed runtime calls", exact=True).wait_for()
            assert session.query("/api/runtime/graph") == {
                "max_nodes": ["60"],
                "max_edges": ["120"],
                "project_only": ["false"],
            }
            assert not session.faults
        finally:
            browser.close()


def test_profile_switch_clears_the_handoff_and_static_handoffs_still_route(
    profile_report, ordinary_report, tmp_path
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    workspace = prepare_workspace(
        discover_context(FIXTURES / "ordinary", AnalysisConfig()),
        explicit_path=tmp_path / "workspace",
    )
    manifest = commit_generation(
        workspace,
        kind=GenerationKind.PROFILE,
        artifacts={"profile.json": render_profile_json(profile_report, compact=True).encode()},
        command=("profile",),
        interpreter_path="/python",
        portable_identity_sha256=profile_report.profile_identity_sha256,
    )
    saved = (
        workspace.kind_directory(GenerationKind.PROFILE) / manifest.generation_id / "profile.json"
    )
    service = ReportViewService(ordinary_report)
    static = WebHandoffSelection(report_sha256=service.bootstrap().reports[0].report_sha256)
    handoff = selection(profile_report, max_nodes=3, max_edges=2, project_only=True)
    with served(
        create_server(service, workspace=workspace.root, profile_report=saved, token=TOKEN)
    ) as server:
        base = base_url(server)
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                session = Session(browser.new_page())
                page = session.page
                page.goto(handoff.url(base))
                page.get_by_role("img", name="Observed runtime calls", exact=True).wait_for()
                assert session.controls() == ("3", "2", True)

                # A static selection on the same session keeps its own routing.
                session.set_fragment(fragment(static.url(base)))
                page.get_by_role("heading", name="Overview", exact=True).wait_for()
                page.locator(".cards").wait_for()
                assert not page.locator("#evidence-dialog").is_visible()

                # Re-enter the runtime selection, then switch the saved profile under it.
                session.set_fragment(fragment(handoff.url(base)))
                page.get_by_role("img", name="Observed runtime calls", exact=True).wait_for()
                assert session.controls() == ("3", "2", True)
                page.get_by_role("button", name="Saved runs", exact=True).click()
                page.locator("#baseline-profile").select_option(manifest.generation_id)
                page.get_by_role("button", name="Open selected runs", exact=True).click()
                page.locator(".cards").wait_for()
                reveal_navigation(page, "Runtime").click()
                page.get_by_role("columnheader", name="function", exact=True).wait_for()
                assert "profile_report_sha256" in page.evaluate("location.hash")
                assert page.locator("#runtime-graph-controls").is_hidden()
                assert session.controls() == ("60", "120", False)
                fresh = selection(profile_report, max_nodes=2, max_edges=1, project_only=True)
                session.set_fragment(fragment(fresh.url(base)))
                playwright.expect(page.locator("#runtime-graph-bounds")).to_contain_text(
                    "Rendered bounds: 2 nodes, 1 edges, project frames only: yes."
                )
                assert set(session.query("/api/runtime/handoff")) == {
                    "profile_report_sha256",
                    "profile_identity_sha256",
                    "graph_request",
                }
                assert session.query("/api/runtime/graph") == {
                    "max_nodes": ["2"],
                    "max_edges": ["1"],
                    "project_only": ["true"],
                }
                assert not session.faults
            finally:
                browser.close()


def test_runtime_handoff_is_contained_and_keyboard_operable_at_390px(profile_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    handoff = selection(profile_report, max_nodes=3, max_edges=2)
    with runtime_only_session(profile_report) as server, playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            session = Session(browser.new_page(viewport={"width": 390, "height": 960}))
            page = session.page
            page.goto(handoff.url(base_url(server)))
            page.get_by_role("img", name="Observed runtime calls", exact=True).wait_for()
            assert page.locator("body").evaluate("node => node.scrollWidth <= window.innerWidth")

            # Keyboard only: toggle the scope, apply it, then open a frame from the graph.
            page.get_by_label("Project frames only", exact=True).focus()
            page.keyboard.press("Space")
            page.get_by_role("button", name="Show runtime evidence", exact=True).press("Enter")
            playwright.expect(page.locator("#runtime-graph-bounds")).to_contain_text(
                "project frames only: yes."
            )
            page.locator("[data-projection-node]").first.focus()
            page.keyboard.press("Enter")
            page.get_by_role("columnheader", name="caller frame id").wait_for()

            # Graph controls are conditional, and null timings stay visible as such.
            assert page.locator("#runtime-graph-controls").is_hidden()
            page.get_by_label("Runtime view", exact=True).select_option("resources")
            page.get_by_label("Frame ID (hotspots/calls/variance only)", exact=True).fill("")
            page.get_by_role("button", name="Show runtime evidence", exact=True).click()
            page.get_by_role("columnheader", name="wall seconds").wait_for()
            assert page.locator("#runtime-graph-controls").is_hidden()
            table = page.locator("#runtime-results table").inner_text()
            assert "unavailable" in table and "null" not in table
            assert page.locator("body").evaluate("node => node.scrollWidth <= window.innerWidth")
            assert not session.faults
        finally:
            browser.close()


def test_runtime_handoff_session_denies_source_and_collection_routes(profile_report) -> None:
    graph_request = RuntimeGraphRequest(max_nodes=3, max_edges=2).model_dump_json()
    handoff = (
        f"profile_report_sha256={canonical_profile_sha256(profile_report)}"
        f"&profile_identity_sha256={profile_report.profile_identity_sha256}"
        f"&graph_request={graph_request}"
    )
    with runtime_only_session(profile_report) as server:

        def status(path: str, *, token: bool = True) -> int:
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            headers = {"X-Code-Analysis-Token": TOKEN} if token else {}
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            response.read()
            connection.close()
            return response.status

        assert status(f"/api/runtime/handoff?{handoff}") == 200
        assert status(f"/api/runtime/handoff?{handoff}", token=False) == 403
        assert status("/api/runtime/graph?max_nodes=3&max_edges=2&project_only=true") == 200
        assert status("/api/source?path=pkg/app.py") == 409
        assert status("/api/bootstrap") == 409
        # Collection is refused either as absent evidence or as a disabled capability.
        assert status("/api/collection/browse?path=.") in {403, 409}
        assert status("/api/collection/files?path=.") in {403, 409}
        assert status("/api/collection/jobs") in {403, 409}
    with pytest.raises(ValueError):
        create_server(
            None,
            token=TOKEN,
            runtime_service=RuntimeViewService(profile_report),
            source_root=FIXTURES / "ordinary",
        )
