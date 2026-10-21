from __future__ import annotations

import asyncio
import errno
import marshal
from threading import Event

import pytest

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.collection_commands import CollectionPlanner
from code_analysis.collection_service import CollectionSession, CollectionWorkspace
from code_analysis.web_service import ReportViewService


def test_catalog_refresh_does_not_query_children_removed_during_teardown(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from textual.widgets import Select

    from code_analysis.tui_collection_jobs import CollectionJobsPanel

    session = CollectionSession(CollectionPlanner(tmp_path))
    entered, release = Event(), Event()
    original = session.catalog

    def held_catalog():
        result = original()
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(session, "catalog", held_catalog)

    class TestApp(App):
        def compose(self) -> ComposeResult:
            yield CollectionJobsPanel(session, lambda index: None)

    async def exercise():
        app = TestApp()
        async with app.run_test() as pilot:
            assert await asyncio.to_thread(entered.wait, 5)
            panel = app.query_one(CollectionJobsPanel)
            await panel.query_one("#jobs-selection", Select).remove()
            assert panel.is_mounted
            release.set()
            await app.workers.wait_for_complete()
            await panel.remove()
            await pilot.pause()

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        session.close()
        session.planner.close()


@pytest.mark.parametrize("bound", [True, False])
def test_terminal_import_opens_verified_bound_index(tmp_path, ordinary_report, bound):
    pytest.importorskip("textual")
    from textual.widgets import Button, Checkbox, Input, Select, TextArea

    from code_analysis.config import AnalysisConfig
    from code_analysis.project import discover_context
    from code_analysis.reports.json_report import canonical_json
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_collection_jobs import CollectionJobsPanel
    from code_analysis.tui_runtime import RuntimeBrowser
    from code_analysis.tui_start import CollectionTerminal
    from code_analysis.workspace import prepare_workspace

    (tmp_path / "source.json").write_text(canonical_json(ordinary_report))
    payload = marshal.dumps({("unknown.py", 1, "work"): (2, 2, 0.1, 0.1, {})})
    (tmp_path / "capture.prof").write_bytes(payload)
    prepare_workspace(discover_context(tmp_path, AnalysisConfig()))
    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), directory)
    index = BrowseIndex.open(directory, report_sha256=manifest.report_sha256)
    session = CollectionSession(CollectionPlanner(tmp_path))

    async def exercise():
        app = (
            AnalysisTerminal(index, collection_root=tmp_path, collection_session=session)
            if bound
            else CollectionTerminal(tmp_path, session)
        )
        async with app.run_test(size=(100, 35)) as pilot:
            if bound:
                await pilot.press("ctrl+l")
            await pilot.pause()
            screen = app.screen
            screen.query_one("#collection-operation", Select).value = "import-profile"
            screen.query_one("#collection-source-report", Input).value = (
                "source.json" if bound else ""
            )
            screen.query_one("#collection-profiles", TextArea).load_text("capture.prof")
            await pilot.pause()
            screen.query_one("#collection-build", Button).press()
            await pilot.pause()
            assert screen.preview is not None
            assert "--allow-execution" not in screen.preview.argv
            panel = screen.query_one(CollectionJobsPanel)
            panel.query_one("#jobs-confirm", Checkbox).value = True
            await pilot.pause()
            panel.query_one("#jobs-launch", Button).press()
            for _ in range(100):
                await pilot.pause(0.05)
                records = session.catalog().records
                if records and records[0].state not in {"queued", "running"}:
                    break
            job = session.catalog().records[0]
            assert job.state == "partial", job.reason
            assert job.binding is not None
            panel.refresh_jobs()
            await pilot.pause(0.2)
            panel.query_one("#jobs-selection", Select).value = job.job_id
            panel.query_one("#jobs-open", Button).press()
            for _ in range(100):
                await pilot.pause(0.05)
                if (
                    bound and len(app.screen_stack) == 1 and app.index.manifest.runtime is not None
                ) or isinstance(app.screen, RuntimeBrowser):
                    break
            if bound:
                assert len(app.screen_stack) == 1
                assert app.index.manifest.runtime is not None
                assert app.index.manifest.runtime.overview.source_matches
            else:
                assert isinstance(app.screen, RuntimeBrowser)
                await pilot.pause(0.2)
                assert app.screen.page is not None
                assert not app.screen.index.manifest.runtime.overview.source_matches
                assert app.screen.index.manifest.report_sha256 is None
                await pilot.press("escape")
                await pilot.pause()
                assert app.screen is screen
            assert (tmp_path / "capture.prof").read_bytes() == payload

    try:
        asyncio.run(exercise())
    finally:
        session.close()
        session.planner.close()


def test_terminal_launch_reopen_status_and_verified_logs(tmp_path, ordinary_report):
    pytest.importorskip("textual")
    from textual.widgets import Button, Checkbox, Input, Select, Static

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_collection_jobs import CollectionJobsPanel

    (tmp_path / "sample.py").write_text("def answer():\n    return 42\n")
    (tmp_path / "sleep.py").write_text("import time\ntime.sleep(30)\n")
    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), directory)
    index = BrowseIndex.open(directory, report_sha256=manifest.report_sha256)
    session = CollectionSession(CollectionPlanner(tmp_path))

    async def exercise():
        app = AnalysisTerminal(index, collection_root=tmp_path, collection_session=session)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+l")
            await pilot.pause()
            screen = app.screen
            screen.query_one("#collection-target", Input).value = "sample.py"
            await pilot.pause()
            screen.query_one("#collection-build", Button).press()
            await pilot.pause()
            assert screen.query_one("#jobs-launch", Button).disabled
            screen.query_one("#jobs-confirm", Checkbox).value = True
            await pilot.pause()
            screen.query_one("#jobs-launch", Button).press()
            for _ in range(100):
                await pilot.pause(0.05)
                if session.catalog().records:
                    break
            job = session.catalog().records[0]
            assert job.command == screen.preview
            await pilot.press("escape")
            await pilot.pause()
            assert not session.planner._closed
            for _ in range(100):
                await pilot.pause(0.05)
                job = session.catalog().records[0]
                if job.state not in {"queued", "running"}:
                    break
            assert job.state == "completed", job.reason
            await pilot.press("ctrl+l")
            await pilot.pause()
            panel = app.screen.query_one(CollectionJobsPanel)
            panel.refresh_jobs()
            await pilot.pause(0.2)
            assert "completed" in str(panel.query_one("#jobs-detail", Static).render())
            panel.query_one("#jobs-log", Button).press()
            await pilot.pause(0.2)
            assert "CLI stderr" in str(panel.query_one("#jobs-status", Static).render())
            assert session.selected().result(job.job_id).report is not None
            screen = app.screen
            screen.query_one("#collection-operation", Select).value = "profile"
            screen.query_one("#collection-source-report", Input).value = (
                f".code-analysis/generations/analysis/{job.binding.generation_id}/report.json"
            )
            screen.query_one("#collection-workload", Input).value = "sleep.py"
            screen.query_one("#collection-allow", Checkbox).value = True
            await pilot.pause()
            screen.query_one("#collection-build", Button).press()
            await pilot.pause()
            panel.query_one("#jobs-confirm", Checkbox).value = True
            await pilot.pause()
            panel.query_one("#jobs-launch", Button).press()
            for _ in range(100):
                await pilot.pause(0.05)
                latest = session.catalog().records[0]
                if latest.job_id != job.job_id and latest.state == "running":
                    break
            assert latest.state == "running"
            panel.refresh_jobs()
            await pilot.pause(0.2)
            panel.query_one("#jobs-selection", Select).value = latest.job_id
            panel.query_one("#jobs-cancel", Button).press()
            for _ in range(100):
                await pilot.pause(0.05)
                latest = session.selected().get(latest.job_id)
                if latest.state == "cancelled":
                    break
            assert latest.state == "cancelled" and latest.status == errno.ECANCELED
            panel.query_one("#jobs-selection", Select).value = job.job_id
            panel.query_one("#jobs-open", Button).press()
            for _ in range(100):
                await pilot.pause(0.05)
                # Dismissal precedes the result callback that selects the bound index.
                if (
                    len(app.screen_stack) == 1
                    and app.index.manifest.report_sha256 != index.manifest.report_sha256
                ):
                    break
            assert len(app.screen_stack) == 1
            assert app.index.manifest.report_sha256 != index.manifest.report_sha256
            assert app.workspace_root == session.selected().workspace.root

    try:
        asyncio.run(exercise())
    finally:
        session.close()
        session.planner.close()


def test_closed_session_cannot_launch_or_reconnect(tmp_path):
    session = CollectionSession(CollectionPlanner(tmp_path))
    session.close()
    try:
        with pytest.raises(OSError) as exc:
            session.connect(CollectionWorkspace(workspace=".code-analysis"))
        assert exc.value.errno == errno.EBADF
        assert not (tmp_path / ".code-analysis").exists()
    finally:
        session.planner.close()


def test_terminal_execution_requires_explicit_root(tmp_path):
    from code_analysis.tui_cli import tui_command

    assert tui_command(tmp_path, "a" * 64, 25, allow_collection=True) == errno.EINVAL


def test_cli_forwards_terminal_execution_flags(tmp_path, monkeypatch):
    from code_analysis import tui_cli
    from code_analysis.cli import main

    seen = []
    monkeypatch.setattr(tui_cli, "tui_command", lambda *args: seen.append(args) or 0)
    assert (
        main(
            [
                "tui",
                "index",
                "--report-sha256",
                "a" * 64,
                "--collection-root",
                str(tmp_path),
                "--allow-collection",
            ]
        )
        == 0
    )
    assert seen[0][-2:] == (tmp_path, True)


def test_cli_owns_terminal_session_and_closes_on_failure(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from code_analysis import tui_app, tui_cli

    seen = []

    class App:
        def __init__(self, *args, **kwargs):
            seen.append(kwargs["collection_session"])

        def run(self):
            raise RuntimeError("synthetic UI failure")

        def close_web_handoff(self):
            pass

    monkeypatch.setattr(tui_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(tui_cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(tui_cli.BrowseIndex, "open", lambda *args, **kwargs: object())
    monkeypatch.setattr(tui_app, "AnalysisTerminal", App)
    with pytest.raises(RuntimeError, match="synthetic"):
        tui_cli.tui_command(tmp_path, "a" * 64, 25, collection_root=tmp_path, allow_collection=True)
    assert seen[0]._closed and seen[0].planner._closed
