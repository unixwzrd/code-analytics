from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.projections import ProjectionName
from code_analysis.reports.json_report import load_json
from code_analysis.static_browse import StaticProjectionRequest
from code_analysis.web_service import ReportViewService

GOLDEN = Path(__file__).parent / "golden"


@pytest.fixture
def coverage_index(tmp_path: Path) -> BrowseIndex:
    report = load_json((GOLDEN / "phase4-coverage-report.json").read_text(encoding="utf-8"))
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), destination)
    assert not (tmp_path / "module.py").exists()
    return BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def test_terminal_coverage_selection_evidence_search_and_export_are_source_free(
    coverage_index: BrowseIndex,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Input, Select

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_export import ExportPage
    from code_analysis.tui_static_projections import StaticProjectionBrowser

    async def exercise() -> None:
        app = AnalysisTerminal(coverage_index, page_size=1)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, StaticProjectionBrowser)
            choices = screen.query_one("#static-projection-name", Select)
            choices.value = ProjectionName.COVERAGE_MATRIX.value
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None
            assert screen.page.request.name is ProjectionName.COVERAGE_MATRIX
            table = screen.query_one("#static-projection-records", DataTable)
            assert table.row_count == 1
            assert tuple(column.label.plain for column in table.columns.values())[:4] == (
                "Row Id",
                "Column Id",
                "Row Label",
                "Column Label",
            )

            row = screen.page.records[0]
            context = row.cells[1].string_value
            assert context is not None
            screen.query_one("#static-projection-search", Input).value = context
            screen.query_one("#static-projection-apply").press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None
            assert screen.page.eligible == screen.page.retained == 1

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RowDetails)
            assert row.row_id in app.screen.detail
            assert context in app.screen.detail
            assert row.evidence_ids
            assert all(evidence_id in app.screen.detail for evidence_id in row.evidence_ids)
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("ctrl+e")
            await pilot.pause()
            export = app.screen
            assert isinstance(export, ExportPage)
            assert export.request == screen.request
            export.query_one("#export-name", Input).value = "coverage-context"
            export.query_one("#export-format", Select).value = "tsv"
            export.query_one("#export-write").press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert export.exported_path is not None
            exported = export.exported_path.read_text(encoding="utf-8")
            assert exported.startswith("row-id\tcolumn-id\trow-label\tcolumn-label\t")
            assert context in exported
            subject_id = row.cells[0].string_value
            assert subject_id is not None and subject_id in exported
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is screen
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())


def test_terminal_lists_only_coverage_views_actually_prepared(
    ordinary_report, tmp_path: Path
) -> None:
    pytest.importorskip("textual")
    from code_analysis.tui_static_projections import StaticProjectionBrowser

    manifest = build_browse_index(ReportViewService(ordinary_report), tmp_path / "index")
    index = BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256)
    screen = StaticProjectionBrowser(index, lambda _status: None)
    assert ProjectionName.COVERAGE_FILES not in screen.available
    assert ProjectionName.COVERAGE_MATRIX not in screen.available
    with pytest.raises(ValueError, match="not prepared"):
        StaticProjectionBrowser(
            index, lambda _status: None, initial_name=ProjectionName.COVERAGE_FILES
        )
    with pytest.raises(ValueError, match="no matching evidence"):
        index.static_projection(StaticProjectionRequest(name=ProjectionName.COVERAGE_FILES))
