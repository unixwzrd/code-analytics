from __future__ import annotations

import asyncio
import errno

import pytest

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.web_service import ReportViewService


@pytest.fixture
def terminal_index(ordinary_report, tmp_path):
    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), directory)
    return BrowseIndex.open(directory, report_sha256=manifest.report_sha256)


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
def test_terminal_keyboard_paging_filtering_and_details(terminal_index, size) -> None:
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Input

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_modules import ModuleBrowser

    async def exercise():
        app = AnalysisTerminal(terminal_index, page_size=1)
        async with app.run_test(size=size) as pilot:
            await pilot.press("f2")
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#records", DataTable)
            assert table.row_count == 1 and app.current_page.eligible > 1
            await pilot.press("ctrl+n")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.current_page.offset == 1
            await pilot.press("ctrl+p")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.current_page.offset == 0
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ModuleBrowser)
            await pilot.press("escape")
            await pilot.pause()
            assert app.focused is table
            await pilot.press("ctrl+f")
            app.query_one("#search", Input).value = "no-such-module"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert app.current_page.eligible == 0 and table.row_count == 0
            await pilot.press("f5")
            await app.workers.wait_for_complete()
            assert app.current_page.eligible == next(
                item.records
                for item in terminal_index.manifest.tables
                if item.table.value == "providers"
            )
            await pilot.press("f1")
            assert app.showing_overview
            await pilot.press("ctrl+q")
        assert app.return_value == app.exit_status

    asyncio.run(exercise())


def test_terminal_cli_optional_provider_and_non_tty(monkeypatch, capsys, tmp_path) -> None:
    from code_analysis import tui_cli
    from code_analysis.cli import main

    args = ["tui", str(tmp_path), "--report-sha256", "a" * 64]
    monkeypatch.setattr(tui_cli.importlib.util, "find_spec", lambda name: None)
    assert main(args) == errno.ENODATA
    assert "unavailable" in capsys.readouterr().err
    monkeypatch.setattr(tui_cli.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(tui_cli.sys.stdin, "isatty", lambda: False)
    assert main(args) == errno.ENOTTY
    assert main([*args, "--page-size", "0"]) == errno.EINVAL


def test_terminal_report_text_cannot_emit_markup_or_terminal_controls() -> None:
    pytest.importorskip("textual")
    from code_analysis.tui_app import plain_text

    value = plain_text("[bold]literal[/]\x1b]52;c;payload\x07\u202e\nnext")
    assert not value.spans
    assert value.plain == "[bold]literal[/]\\u001b]52;c;payload\\u0007\\u202e\nnext"


def test_terminal_discards_superseded_page_and_shows_artifact_failure(
    terminal_index, monkeypatch
) -> None:
    import threading

    pytest.importorskip("textual")
    from textual.widgets import DataTable, Static

    from code_analysis.tui_app import AnalysisTerminal

    started = threading.Event()
    release = threading.Event()
    original = BrowseIndex.page

    def delayed(self, request):
        if request.table.value == "modules":
            started.set()
            release.wait(5)
        return original(self, request)

    monkeypatch.setattr(BrowseIndex, "page", delayed)

    async def exercise():
        app = AnalysisTerminal(terminal_index)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.press("f2")
                assert await asyncio.to_thread(started.wait, 2)
                await pilot.press("f5")
                await pilot.pause()
                assert app.selection.table.value == "providers"
                release.set()
                await pilot.pause()
                assert app.current_page.eligible == next(
                    item.records
                    for item in terminal_index.manifest.tables
                    if item.table.value == "providers"
                )
                terminal_index.database.write_bytes(b"changed artifact")
                await pilot.press("f6")
                await app.workers.wait_for_complete()
                assert app.current_page is None
                assert app.query_one("#records", DataTable).row_count == 0
                assert "Index unavailable" in str(app.query_one("#page-status", Static).content)
                await pilot.press("ctrl+c")
            assert app.return_value == errno.EBADMSG
        finally:
            release.set()

    asyncio.run(exercise())
