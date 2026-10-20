from __future__ import annotations

import asyncio
import errno

import pytest

from code_analysis.collection_commands import CollectionPlanner
from code_analysis.collection_service import CollectionSession


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
def test_report_free_collect_and_verified_result(tmp_path, size):
    pytest.importorskip("textual")
    from textual.widgets import Button, Checkbox, Input

    from code_analysis.tui_start import CollectionTerminal

    (tmp_path / "sample.py").write_text("def answer():\n    return 42\n")
    session = CollectionSession(CollectionPlanner(tmp_path))

    async def exercise():
        app = CollectionTerminal(tmp_path, session)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            app.screen.query_one("#collection-target", Input).value = "sample.py"
            await pilot.pause()
            app.screen.query_one("#collection-build", Button).press()
            await pilot.pause()
            app.screen.query_one("#jobs-confirm", Checkbox).value = True
            await pilot.pause()
            app.screen.query_one("#jobs-launch", Button).press()
            for _ in range(150):
                await pilot.pause(0.05)
                jobs = session.catalog().records
                if jobs and jobs[0].state not in {"queued", "running"}:
                    break
            assert jobs[0].state == "completed"
            await pilot.pause(1.1)
            app.screen.query_one("#jobs-open", Button).press()
            for _ in range(100):
                await pilot.pause(0.05)
                if app.return_value is not None:
                    break
            assert app.return_value is not None
        return app.return_value

    try:
        index = asyncio.run(exercise())
        assert index.bootstrap().reports[0].report_sha256 == index.manifest.report_sha256
        assert not session._closed
    finally:
        session.close()
        session.planner.close()


def test_preview_only_landing_closes_without_artifacts(tmp_path):
    pytest.importorskip("textual")
    from code_analysis.tui_start import CollectionTerminal

    async def exercise():
        app = CollectionTerminal(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            planner = app.screen.planner
            await pilot.press("escape")
        assert app.return_value is None
        assert planner._closed

    asyncio.run(exercise())
    assert not (tmp_path / ".code-analysis").exists()


@pytest.mark.parametrize(
    "index,sha,root", [(None, None, None), (None, "a" * 64, True), (True, None, True)]
)
def test_index_hash_and_report_free_selection_contract(tmp_path, index, sha, root):
    from code_analysis.tui_cli import tui_command

    assert (
        tui_command(
            tmp_path if index else None, sha, 25, collection_root=tmp_path if root else None
        )
        == errno.EINVAL
    )


def test_cli_switches_from_landing_to_report_and_keeps_session(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from code_analysis import tui_app, tui_cli, tui_start
    from code_analysis.collection_service import CollectionWorkspace
    from code_analysis.config import AnalysisConfig
    from code_analysis.project import discover_context
    from code_analysis.workspace import prepare_workspace

    (tmp_path / "sample.py").write_text("value = 1\n")
    workspace = prepare_workspace(discover_context(tmp_path, AnalysisConfig()))
    index = object()
    seen = []
    sessions = []
    closed_hosts = []

    class Landing:
        return_code = 0

        def __init__(self, root, session, *, collection_pythons=(), browser=None):
            assert root == tmp_path and session is not None
            assert collection_pythons == ()
            self.browser = browser
            sessions.append(session)
            session.connect(CollectionWorkspace(workspace=".code-analysis"))

        def run(self):
            return index

        def close_web_handoff(self):
            closed_hosts.append("landing")

    class Report:
        return_code = exit_status = 0

        def __init__(self, selected, **kwargs):
            seen.append(selected)
            assert kwargs["collection_session"] is sessions[0]
            assert not sessions[0]._closed
            assert kwargs["workspace"] == workspace.root

        def run(self):
            return errno.ENODATA

        def close_web_handoff(self):
            closed_hosts.append("report")

    monkeypatch.setattr(tui_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(tui_cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(tui_start, "CollectionTerminal", Landing)
    monkeypatch.setattr(tui_app, "AnalysisTerminal", Report)
    assert (
        tui_cli.tui_command(None, None, 25, collection_root=tmp_path, allow_collection=True)
        == errno.ENODATA
    )
    assert seen == [index]
    assert closed_hosts == ["landing", "report"]
    assert sessions[0]._closed and sessions[0].planner._closed
