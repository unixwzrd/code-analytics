from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from conftest import FIXTURES

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import load_config_file
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName
from code_analysis.service import scan_refactor
from code_analysis.static_browse import StaticProjectionRequest
from code_analysis.web_service import ReportViewService


@pytest.fixture
def commonality_index(tmp_path: Path) -> BrowseIndex:
    source = tmp_path / "source"
    shutil.copytree(FIXTURES / "proposal_project", source)
    report = scan_refactor(
        discover_context(source, load_config_file(FIXTURES / "proposal-config.toml"))
    )
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), destination)
    shutil.rmtree(source)
    assert not source.exists()
    return BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def test_terminal_commonality_search_details_and_exports_are_source_free(commonality_index):
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Input, Select

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_export import ExportPage
    from code_analysis.tui_static_projections import StaticProjectionBrowser

    async def exercise() -> None:
        app = AnalysisTerminal(commonality_index, page_size=1)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, StaticProjectionBrowser)
            screen.query_one("#static-projection-name", Select).value = (
                ProjectionName.FUNCTION_COMMONALITY.value
            )
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None
            assert screen.page.request.name is ProjectionName.FUNCTION_COMMONALITY
            assert screen.page.eligible > screen.page.retained == 1
            table = screen.query_one("#static-projection-records", DataTable)
            assert table.row_count == 1
            assert tuple(column.label.plain for column in table.columns.values())[:4] == (
                "Left Subject Id",
                "Right Subject Id",
                "Left Subject",
                "Right Subject",
            )
            assert "null" in tuple(cell.plain for cell in table.get_row_at(0))

            # Filtering happens in the retained index before the one-row display bound.
            later = commonality_index.static_projection(
                StaticProjectionRequest(
                    name=ProjectionName.FUNCTION_COMMONALITY,
                    offset=1,
                    limit=1,
                )
            ).records[0]
            screen.query_one("#static-projection-search", Input).value = later.row_id
            screen.query_one("#static-projection-apply").press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None
            assert screen.page.eligible == screen.page.retained == 1
            assert screen.page.records[0] == later

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RowDetails)
            assert later.row_id in app.screen.detail
            assert later.evidence_ids
            assert all(item in app.screen.detail for item in later.evidence_ids)
            left_id = later.cells[0].string_value
            right_id = later.cells[1].string_value
            assert left_id is not None and left_id in app.screen.detail
            assert right_id is not None and right_id in app.screen.detail
            await pilot.press("escape")
            await pilot.pause()

            for output_format, artifact_name, suffix in (
                ("json-compact", "commonality-pair", ".json"),
                ("tsv", "commonality-pair-table", ".tsv"),
            ):
                await pilot.press("ctrl+e")
                await pilot.pause()
                export = app.screen
                assert isinstance(export, ExportPage)
                assert export.request == screen.request
                export.query_one("#export-name", Input).value = artifact_name
                export.query_one("#export-format", Select).value = output_format
                export.query_one("#export-write").press()
                await pilot.pause()
                await app.workers.wait_for_complete()
                assert export.exported_path is not None
                assert export.exported_path.suffix == suffix
                assert export.exported_path.is_file()
                exported = export.exported_path.read_text(encoding="utf-8")
                if output_format == "json-compact":
                    assert later.row_id in exported
                else:
                    assert exported.startswith("left-subject-id\tright-subject-id\t")
                assert left_id in exported and right_id in exported
                await pilot.press("escape")
                await pilot.pause()
                assert app.screen is screen

            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())
