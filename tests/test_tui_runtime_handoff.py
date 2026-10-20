from __future__ import annotations

import asyncio
import errno
import json
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_index import RuntimeIndex, build_runtime_index
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService

GOLDEN = Path(__file__).parent / "golden" / "phase4-profile-report.json"
SIZES = [(80, 24), (120, 40)]


@pytest.fixture
def bound_index(ordinary_report, tmp_path):
    """A report index that also carries runtime evidence, as the analysis host sees it."""
    root = tmp_path / "index"
    manifest = build_browse_index(
        ReportViewService(ordinary_report), root, runtime=RuntimeViewService.load(GOLDEN)
    )
    return BrowseIndex.open(root, report_sha256=manifest.report_sha256)


@pytest.fixture
def runtime_index(tmp_path):
    directory = tmp_path / "runtime"
    manifest = build_runtime_index(RuntimeViewService.load(GOLDEN), directory)
    return RuntimeIndex.open(directory, profile_sha256=manifest.runtime.profile_report_sha256)


def status(url: str, endpoint: str) -> int:
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    try:
        token = parse_qs(parsed.query)["token"][0]
        connection.request("GET", endpoint, headers={"X-Code-Analysis-Token": token})
        return connection.getresponse().status
    finally:
        connection.close()


async def open_runtime_graph(pilot) -> object:
    """Reach the graph from the runtime browser the way a keyboard user does."""
    from textual.widgets import Button

    pilot.app.screen.query_one("#runtime-graph", Button).press()
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    return pilot.app.screen


@pytest.mark.parametrize("size", SIZES)
def test_runtime_graph_handoff_carries_applied_request(bound_index, monkeypatch, size):
    pytest.importorskip("textual")
    from textual.widgets import Button, Checkbox, Input

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_handoff import WebHandoffDialog
    from code_analysis.tui_runtime_graph import RuntimeGraphBrowser
    from code_analysis.ui_handoff import RuntimeHandoffSelection

    launches: list[object] = []
    monkeypatch.setattr(
        "code_analysis.tui_handoff.launch_display", lambda *args: launches.append(args)
    )
    summary = bound_index.manifest.runtime

    async def exercise():
        app = AnalysisTerminal(bound_index)
        async with app.run_test(size=size) as pilot:
            await pilot.press("ctrl+r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            screen = await open_runtime_graph(pilot)
            assert isinstance(screen, RuntimeGraphBrowser)
            assert screen.graph is not None
            screen.query_one("#runtime-graph-nodes", Input).value = "40"
            screen.query_one("#runtime-graph-project", Checkbox).value = True
            screen.query_one("#runtime-graph-apply", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.request == RuntimeGraphRequest(
                max_nodes=40, max_edges=120, project_only=True
            )
            await pilot.press("ctrl+w")
            await pilot.pause()
            assert isinstance(app.screen, WebHandoffDialog)
            selection = app.screen.selection
            assert isinstance(selection, RuntimeHandoffSelection)
            assert selection.graph_request == screen.request
            assert selection.profile_report_sha256 == summary.profile_report_sha256
            assert selection.profile_identity_sha256 == summary.overview.profile_identity
            assert app.screen.query_one("#handoff-report", Input).value == ""
            await pilot.press("escape")
            await pilot.pause()
        assert not launches

    asyncio.run(exercise())


@pytest.mark.parametrize("size", SIZES)
def test_runtime_graph_handoff_refuses_stale_controls_and_missing_graph(
    bound_index, ordinary_report, tmp_path, size
):
    pytest.importorskip("textual")
    from textual.widgets import Input

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_runtime_graph import RuntimeGraphBrowser

    plain = tmp_path / "plain"
    manifest = build_browse_index(ReportViewService(ordinary_report), plain)
    without_runtime = BrowseIndex.open(plain, report_sha256=manifest.report_sha256)

    async def exercise():
        app = AnalysisTerminal(bound_index)
        async with app.run_test(size=size) as pilot:
            await pilot.press("ctrl+r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            screen = await open_runtime_graph(pilot)
            assert screen.graph is not None
            screen.query_one("#runtime-graph-edges", Input).value = "7"
            await pilot.pause()
            await pilot.press("ctrl+w")
            await pilot.pause()
            assert app.screen is screen
            await pilot.press("escape", "escape")
            await pilot.pause()

            absent = RuntimeGraphBrowser(
                without_runtime,
                app.evidence_closed,
                web_action=lambda request: pytest.fail("handoff without a graph"),
            )
            app.push_screen(absent)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert absent.graph is None
            await pilot.press("ctrl+w")
            await pilot.pause()
            assert app.screen is absent

    asyncio.run(exercise())


@pytest.mark.parametrize("configured", [True, False])
def test_report_free_host_prepares_runtime_only_session(runtime_index, tmp_path, configured):
    pytest.importorskip("textual")
    from textual.widgets import Button, Input, Static, TextArea

    from code_analysis.artifact_viewer import ViewerCommand
    from code_analysis.collection_commands import CollectionPlanner
    from code_analysis.collection_service import CollectionSession
    from code_analysis.tui_collection_jobs import CollectionJobsPanel
    from code_analysis.tui_handoff import WebHandoffDialog
    from code_analysis.tui_start import CollectionTerminal
    from code_analysis.ui_handoff import RuntimeHandoffSelection

    summary = runtime_index.manifest.runtime
    session = CollectionSession(CollectionPlanner(tmp_path))
    browser = ViewerCommand(arguments=("/bin/echo",)) if configured else None

    async def exercise(app):
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.screen.query_one(CollectionJobsPanel).profile_selected(runtime_index)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            screen = await open_runtime_graph(pilot)
            assert screen.graph is not None
            await pilot.press("ctrl+w")
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, WebHandoffDialog)
            assert isinstance(dialog.selection, RuntimeHandoffSelection)
            assert dialog.selection.profile_report_sha256 == summary.profile_report_sha256
            assert dialog.selection.profile_identity_sha256 == summary.overview.profile_identity
            dialog.query_one("#handoff-report", Input).value = str(GOLDEN)
            await pilot.pause()
            dialog.prepare()
            await app.workers.wait_for_complete()
            await pilot.pause()
            url = dialog.query_one("#handoff-url", TextArea).text
            assert url == dialog.url
            assert dialog.query_one("#handoff-open", Button).disabled is not configured
            assert not dialog.query_one("#handoff-copy", Button).disabled
            if not configured:
                assert "copy" in str(dialog.query_one("#handoff-status", Static).content)
            assert await asyncio.to_thread(status, url, "/api/runtime") == 200
            assert await asyncio.to_thread(status, url, "/api/bootstrap") == 409
            assert await asyncio.to_thread(status, url, "/api/source?path=setup.py") == 409
            return url

    app = CollectionTerminal(tmp_path, session, browser=browser)
    try:
        url = asyncio.run(exercise(app))
        assert parse_qs(urlsplit(url).fragment)["profile_report_sha256"] == [
            summary.profile_report_sha256
        ]
        assert json.loads(parse_qs(urlsplit(url).fragment)["graph_request"][0]) == (
            RuntimeGraphRequest().model_dump(mode="json")
        )
    finally:
        app.close_web_handoff()
        session.close()
        session.planner.close()
    # Leaving the report-free host ends its session; nothing can be published afterwards.
    with pytest.raises(OSError):
        status(url, "/api/runtime")
    with pytest.raises(OSError) as closed:
        app.web_handoff.open_runtime(GOLDEN, app_selection(runtime_index))
    assert closed.value.errno == errno.ECANCELED


def app_selection(index):
    from code_analysis.tui_handoff import runtime_selection

    selection = runtime_selection(index, RuntimeGraphRequest())
    assert selection is not None
    return selection


def test_dialog_dispatches_each_selection_kind_to_its_owner_entry_point(
    ordinary_report, runtime_index, tmp_path
):
    pytest.importorskip("textual")
    from textual.widgets import Input, Static

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_handoff import WebHandoffDialog
    from code_analysis.ui_handoff import WebHandoffSelection

    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), directory)
    index = BrowseIndex.open(directory, report_sha256=manifest.report_sha256)
    runtime_calls, static_calls = [], []

    class RuntimeOwner:
        """Runtime selections must never reach the report-bound entry point."""

        def open(self, report, selection):
            raise AssertionError("runtime selection used the saved-report entry point")

        def open_runtime(self, profile, selection):
            runtime_calls.append((profile, selection))
            return selection.url("http://127.0.0.1:8123/?token=synthetic")

    class StaticOwner:
        def open(self, report, selection):
            static_calls.append((report, selection))
            return selection.url("http://127.0.0.1:8123/?token=synthetic")

    async def prepared(app, pilot, owner, selection):
        dialog = WebHandoffDialog(owner, selection, None)
        app.push_screen(dialog)
        await pilot.pause()
        dialog.prepare()
        kind = "profile" if isinstance(owner, RuntimeOwner) else "report"
        assert str(dialog.query_one("#handoff-status", Static).content) == (
            f"Select a saved {kind} JSON first."
        )
        dialog.query_one("#handoff-report", Input).value = "artifact.json"
        await pilot.pause()
        assert str(dialog.query_one("#handoff-status", Static).content) == (
            f"{kind.capitalize()} path changed; prepare and verify the handoff again."
        )
        dialog.prepare()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert dialog.url is not None
        url = dialog.url
        await pilot.press("escape")
        await pilot.pause()
        return url

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(80, 24)) as pilot:
            runtime_url = await prepared(app, pilot, RuntimeOwner(), app_selection(runtime_index))
            static_url = await prepared(
                app,
                pilot,
                StaticOwner(),
                WebHandoffSelection(report_sha256=manifest.report_sha256),
            )
        return runtime_url, static_url

    runtime_url, static_url = asyncio.run(exercise())
    assert len(runtime_calls) == 1 and len(static_calls) == 1
    assert runtime_calls[0][0] == Path("artifact.json").absolute()
    assert "profile_report_sha256" in parse_qs(urlsplit(runtime_url).fragment)
    assert parse_qs(urlsplit(static_url).fragment)["report_sha256"] == [manifest.report_sha256]
    assert "profile_report_sha256" not in parse_qs(urlsplit(static_url).fragment)


def test_baseline_selection_stays_exact_and_invents_no_comparison(tmp_path, ordinary_report):
    from code_analysis.tui_handoff import runtime_selection

    directory = tmp_path / "paired"
    manifest = build_runtime_index(RuntimeViewService.load(GOLDEN, GOLDEN), directory)
    paired = RuntimeIndex.open(directory, profile_sha256=manifest.runtime.profile_report_sha256)
    assert paired.manifest.runtime.comparison is not None

    baseline = runtime_selection(paired, RuntimeGraphRequest())
    project_only = runtime_selection(paired, RuntimeGraphRequest(project_only=True))
    assert baseline is not None and project_only is not None
    assert set(baseline.model_dump()) == {
        "profile_report_sha256",
        "profile_identity_sha256",
        "graph_request",
    }
    assert baseline.profile_report_sha256 == paired.manifest.runtime.profile_report_sha256
    assert baseline.profile_identity_sha256 == paired.manifest.runtime.overview.profile_identity
    assert baseline.graph_request != project_only.graph_request
    assert baseline.profile_report_sha256 == project_only.profile_report_sha256

    plain = tmp_path / "plain"
    report = build_browse_index(ReportViewService(ordinary_report), plain)
    without_runtime = BrowseIndex.open(plain, report_sha256=report.report_sha256)
    assert runtime_selection(without_runtime, RuntimeGraphRequest()) is None


def test_cli_closes_landing_host_session_before_any_analysis_app(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from code_analysis import tui_cli, tui_start
    from code_analysis.artifact_viewer import default_viewer
    from code_analysis.display_preferences import load_display_preferences

    hosts = []

    class Landing(tui_start.CollectionTerminal):
        return_code = 0

        def run(self, *args, **kwargs):
            hosts.append(self)
            return None

    monkeypatch.setattr(tui_start, "CollectionTerminal", Landing)
    monkeypatch.setattr(tui_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(tui_cli.sys.stdout, "isatty", lambda: True)
    assert tui_cli.tui_command(None, None, 25, collection_root=tmp_path) == 0
    expected = load_display_preferences(None).browser_command or default_viewer()
    assert len(hosts) == 1
    assert hosts[0].browser == expected
    assert hosts[0].web_handoff._closed.is_set()
