from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from test_runtime_comparison_source_browsing import build_comparison_index


def _source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    shutil.copytree(Path(__file__).parent / "fixtures" / "ordinary", root)
    return root


async def _open_runtime_from_terminal(pilot) -> object:
    from code_analysis.tui_runtime import RuntimeBrowser

    await pilot.press("ctrl+r")
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    screen = pilot.app.screen
    assert isinstance(screen, RuntimeBrowser)
    assert screen.page is not None
    return screen


async def _tab_to_button(pilot, screen, button_id: str, *, max_tabs: int = 24) -> None:
    from textual.widgets import Button

    for _ in range(max_tabs):
        focused = screen.focused
        if isinstance(focused, Button) and focused.id == button_id:
            return
        await pilot.press("tab")
        await pilot.pause()
    raise AssertionError(f"Tab focus did not reach {button_id}")


def test_runtime_compare_reachable_at_80x24_via_keyboard_and_tab(ordinary_report, tmp_path):
    pytest.importorskip("textual")
    from textual.containers import HorizontalScroll
    from textual.widgets import Button, DataTable

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_runtime import RuntimeBrowser
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser
    from code_analysis.tui_source import TerminalSource

    _, index, _ = build_comparison_index(ordinary_report, tmp_path)
    root = _source_tree(tmp_path)

    async def exercise() -> None:
        app = AnalysisTerminal(index, source_root=root)
        async with app.run_test(size=(80, 24)) as pilot:
            runtime = await _open_runtime_from_terminal(pilot)
            table = runtime.query_one("#runtime-records", DataTable)
            assert runtime.focused is table

            scroll = runtime.query_one("#runtime-buttons", HorizontalScroll)
            # At 80x24 the action row is wider than the dialog; horizontal scroll is required.
            assert scroll.max_scroll_x > 0

            await pilot.press("m")
            await pilot.pause()
            await app.workers.wait_for_complete()
            comparison = app.screen
            assert isinstance(comparison, RuntimeComparisonBrowser)
            assert comparison.source_root == root

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, RuntimeBrowser)
            assert app.screen.focused is table

            runtime = app.screen
            await _tab_to_button(pilot, runtime, "runtime-compare")
            focused = runtime.focused
            assert isinstance(focused, Button)
            assert focused.id == "runtime-compare"
            view = scroll.region
            assert focused.region.x < view.x + view.width
            assert focused.region.right > view.x

            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, RuntimeComparisonBrowser)

            await pilot.press("b")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, TerminalSource)
            assert app.screen.preview is not None
            assert app.screen.preview.logical_path == "alpha/a.py"
            await pilot.press("escape")
            assert isinstance(app.screen, RuntimeComparisonBrowser)

            await pilot.press("c")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, TerminalSource)
            assert app.screen.preview is not None
            assert app.screen.preview.logical_path == "alpha/a.py"
            await pilot.press("escape")
            assert isinstance(app.screen, RuntimeComparisonBrowser)

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, RuntimeBrowser)
            restored = app.screen.focused
            assert restored in {table, runtime.query_one("#runtime-compare", Button)}
            table.focus()
            await pilot.pause()
            assert app.screen.focused is table
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())
