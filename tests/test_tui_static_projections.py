from __future__ import annotations

import asyncio

import pytest

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.projection_models import ProjectionScalar, ScalarKind
from code_analysis.projections import ProjectionName
from code_analysis.static_browse import StaticProjectionRequest
from code_analysis.web_service import ReportViewService


def test_static_scalar_cells_preserve_zero_false_and_null():
    from code_analysis.tui_static_projections import scalar_text

    assert scalar_text(ProjectionScalar(kind=ScalarKind.INTEGER, integer_value=0)) == "0"
    assert scalar_text(ProjectionScalar(kind=ScalarKind.BOOLEAN, boolean_value=False)) == "false"
    assert scalar_text(ProjectionScalar(kind=ScalarKind.NULL)) == "null"


@pytest.fixture
def static_index(ordinary_report, tmp_path):
    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), directory)
    return BrowseIndex.open(directory, report_sha256=manifest.report_sha256)


def test_static_projection_browser_uses_prepared_rows_cycle_page_and_export(static_index):
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable, Input, Select, Static

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_export import ExportPage
    from code_analysis.tui_static_projections import StaticProjectionBrowser

    async def exercise():
        app = AnalysisTerminal(static_index, page_size=25)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, StaticProjectionBrowser)
            assert screen.page is not None
            assert screen.page.request.name is ProjectionName.COMPLEXITY
            table = screen.query_one("#static-projection-records", DataTable)
            assert table.row_count == screen.page.retained
            assert table.get_cell_at((2, 6)).plain == "0"
            # The cycle inventory is a distinct prepared table, not a source scan.
            screen.query_one("#static-projection-name", Select).value = "cycle-list"
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None
            assert screen.page.request.name is ProjectionName.CYCLE_LIST
            assert table.row_count == screen.page.retained == 1
            screen.query_one("#static-projection-search", Input).value = "alpha"
            await pilot.pause()
            assert screen.page is None
            assert screen.query_one("#static-projection-export", Button).disabled
            screen.query_one("#static-projection-search", Input).value = ""
            screen.query_one("#static-projection-apply", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None and screen.page.request.name is ProjectionName.CYCLE_LIST
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RowDetails)
            assert "evidence_ids" in app.screen.detail
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is screen
            await pilot.press("ctrl+e")
            await pilot.pause()
            assert isinstance(app.screen, ExportPage)
            assert app.screen.request == StaticProjectionRequest(name=ProjectionName.CYCLE_LIST)
            export = app.screen
            export.query_one("#export-name", Input).value = "cycle-page"
            export.query_one("#export-write", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert export.exported_path is not None, str(
                export.query_one("#export-status", Static).content
            )
            assert export.exported_path.is_file()
            await pilot.press("escape", "escape", "ctrl+c")

    asyncio.run(exercise())


def test_edited_selection_cannot_reload_previous_stale_page(static_index):
    pytest.importorskip("textual")
    from textual.widgets import Input

    from code_analysis.tui_app import AnalysisTerminal

    async def exercise():
        app = AnalysisTerminal(static_index, page_size=1)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            await pilot.press("ctrl+n")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None and screen.request.offset == 1
            screen.query_one("#static-projection-search", Input).value = "edited selection"
            await pilot.pause()
            assert screen.page is None
            await pilot.press("ctrl+p")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is None and screen.request.offset == 1
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())
