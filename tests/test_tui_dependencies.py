from __future__ import annotations

import asyncio
import errno

import pytest

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.models import Completeness
from code_analysis.web_service import ReportViewService


@pytest.fixture
def dependency_index(ordinary_report, tmp_path):
    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), directory)
    return ordinary_report, BrowseIndex.open(directory, report_sha256=manifest.report_sha256)


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
def test_dependency_browser_applies_bounded_request_and_returns_selected_module(
    dependency_index, monkeypatch, size
):
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Input, Select

    import code_analysis.tui_dependencies as tui_dependencies
    from code_analysis.dependency_navigation import DependencyResult
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_dependencies import DependencyBrowser
    from code_analysis.tui_modules import ModuleBrowser

    report, index = dependency_index
    source, target = (module.name for module in report.modules[:2])
    calls = []

    def investigated(reader, request):
        calls.append((reader, request))
        return DependencyResult.model_validate(
            {
                "request": request,
                "source_completeness": Completeness.COMPLETE,
                "truncated": False,
                "visited_nodes": 2,
                "examined_edges": 1,
                "path": (
                    ((source,) if request.target == source else (source, request.target))
                    if request.mode == "path"
                    else ()
                ),
                "members": (source, target) if request.mode == "component" else (),
                "limitations": ("known local modules only",),
            }
        )

    monkeypatch.setattr(tui_dependencies, "investigate_dependencies", investigated)

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=size) as pilot:
            browser = ModuleBrowser(index, source, app.evidence_closed)
            app.push_screen(browser)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.click("#module-dependencies")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, DependencyBrowser)
            assert calls[-1][1].source == source
            screen.query_one("#dependency-nodes", Input).value = "2"
            screen.query_one("#dependency-target", Input).value = target
            screen.query_one("#dependency-apply").press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert calls[-1][1].max_nodes == 2
            table = screen.query_one("#dependency-table", DataTable)
            assert table.row_count == 2
            screen.query_one("#dependency-mode", Select).value = "component"
            await pilot.pause()
            assert screen.query_one("#dependency-target", Input).disabled
            screen.query_one("#dependency-apply").press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert calls[-1][1].mode == "component" and calls[-1][1].target is None
            assert table.row_count == 2
            await pilot.press("down", "enter")
            await app.workers.wait_for_complete()
            assert app.screen is browser and browser.module == target
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


@pytest.mark.parametrize("truncated", [False, True])
def test_dependency_browser_reports_truncated_or_partial_evidence_as_enodata(
    dependency_index, monkeypatch, truncated
):
    pytest.importorskip("textual")
    from textual.widgets import Static

    import code_analysis.tui_dependencies as tui_dependencies
    from code_analysis.dependency_navigation import DependencyResult
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_dependencies import DependencyBrowser

    report, index = dependency_index
    source = report.modules[0].name

    def investigated(_reader, request):
        return DependencyResult.model_validate(
            {
                "request": request,
                "source_completeness": Completeness.PARTIAL,
                "truncated": truncated,
                "visited_nodes": 1,
                "examined_edges": 1,
                "path": (),
                "members": (),
                "limitations": ("bounded search",),
            }
        )

    monkeypatch.setattr(tui_dependencies, "investigate_dependencies", investigated)

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = DependencyBrowser(index, source, app.evidence_closed)
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.exit_status == errno.ENODATA
            status = str(screen.query_one("#dependency-status", Static).content)
            assert f"truncated: {truncated}" in status
            assert "absence remains unresolved" in status
            assert "No local path found" not in status
            await pilot.press("escape")
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_dependency_browser_uses_saved_index_for_self_path_and_component(dependency_index):
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Select

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_dependencies import DependencyBrowser

    report, index = dependency_index
    source = report.modules[0].name

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = DependencyBrowser(index, source, app.evidence_closed)
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.result is not None
            assert screen.result.path == (source,)
            assert screen.query_one("#dependency-table", DataTable).row_count == 1
            screen.query_one("#dependency-mode", Select).value = "component"
            await pilot.pause()
            screen.query_one("#dependency-apply").press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.result is not None and source in screen.result.members
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())
