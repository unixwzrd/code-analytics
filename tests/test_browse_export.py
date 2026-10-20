from __future__ import annotations

import asyncio
import errno

import pytest

from code_analysis.browse_export import (
    BrowseExportSelectionError,
    build_browse_export,
    export_browse_page,
)
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseRequest
from code_analysis.projection_models import TableProjection
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.ui_rows import BrowseTable
from code_analysis.web_service import ReportViewService


@pytest.fixture
def export_index(ordinary_report, tmp_path):
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), destination)
    return BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


@pytest.mark.parametrize(
    "output_format",
    (ProjectionFormat.JSON_COMPACT, ProjectionFormat.TSV, ProjectionFormat.MARKDOWN),
)
def test_export_exact_deterministic_page_and_no_overwrite(export_index, output_format):
    request = BrowseRequest(table=BrowseTable.MODULES, limit=1, query="alpha")
    expected = build_browse_export(export_index, request)
    path, projection = export_browse_page(export_index, request, "selection", output_format)
    assert projection == expected
    assert path.read_text() == render_projection(expected, output_format)
    assert expected.truncated and expected.record_count == 1
    assert any('"query":"alpha"' in item for item in expected.limitations)
    assert expected.inputs[0].report_sha256 == export_index.manifest.report_sha256
    if output_format is ProjectionFormat.JSON_COMPACT:
        assert TableProjection.model_validate_json(path.read_bytes()) == expected
    with pytest.raises(FileExistsError):
        export_browse_page(export_index, request, "selection", output_format)
    assert path.read_text() == render_projection(expected, output_format)
    assert not tuple(path.parent.glob(".export-*"))
    assert build_browse_export(export_index, request) == expected


def test_export_rejects_paths_symlinks_and_preserves_empty_scope(export_index, tmp_path):
    request = BrowseRequest(table=BrowseTable.MODULES, query="no-match")
    for name in ("../escape", "/absolute", "a/b", ".hidden", "", "x" * 81):
        with pytest.raises(BrowseExportSelectionError):
            export_browse_page(export_index, request, name, ProjectionFormat.JSON_COMPACT)
    empty = build_browse_export(export_index, request)
    assert empty.record_count == 0 and not empty.truncated and empty.columns
    outside = tmp_path / "outside"
    outside.mkdir()
    (export_index.database.parent / "exports").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        export_browse_page(export_index, request, "escape", ProjectionFormat.JSON_COMPACT)
    assert not tuple(outside.iterdir())


def test_export_preserves_numeric_page_order_beyond_ten_rows(export_index, monkeypatch):
    request = BrowseRequest(table=BrowseTable.MODULES, limit=25)
    page = export_index.page(request)
    rows = tuple(
        page.records[0].model_copy(update={"module": f"module{number}"}) for number in range(20)
    )
    expanded = page.model_copy(
        update={"records": rows, "eligible": 20, "retained": 20, "truncated": False}
    )
    monkeypatch.setattr(BrowseIndex, "page", lambda self, selection: expanded)
    projection = build_browse_export(export_index, request)
    assert [row.cells[0].string_value for row in projection.rows] == [
        f"module{number}" for number in range(20)
    ]
    assert [row.row_id for row in projection.rows] == sorted(row.row_id for row in projection.rows)


def test_terminal_explicit_export(export_index, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Checkbox, Input, Static

    from code_analysis.artifact_viewer import ViewerCommand
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_export import ExportPage

    launches = []

    def launch(path, digest, viewer):
        launches.append((path, digest, viewer))
        return True

    monkeypatch.setattr("code_analysis.tui_export.open_artifact", launch)

    async def exercise():
        app = AnalysisTerminal(
            export_index, page_size=1, viewer=ViewerCommand(arguments=("/bin/viewer",))
        )
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("f2")
            await app.workers.wait_for_complete()
            await pilot.press("ctrl+e")
            await pilot.pause()
            assert isinstance(app.screen, ExportPage)
            dialog = app.screen
            await pilot.press("ctrl+e")
            assert app.screen is dialog and len(app.screen_stack) == 2
            app.screen.query_one("#export-name", Input).value = "keyboard-page"
            await pilot.click("#export-write")
            await app.workers.wait_for_complete()
            await pilot.pause()
            path = export_index.database.parent / "exports" / "keyboard-page.json"
            projection = TableProjection.model_validate_json(path.read_bytes())
            assert projection.record_count == 1 and projection.truncated
            assert app.exit_status == errno.ENODATA
            assert launches == []
            await pilot.click("#export-open")
            await app.workers.wait_for_complete()
            assert len(launches) == 1 and launches[0][0] == path
            assert "launch request completed" in str(
                app.screen.query_one("#export-status", Static).render()
            )
            app.screen.query_one("#export-name", Input).value = "all-filtered"
            app.screen.query_one("#export-filtered", Checkbox).value = True
            # Textual ignores clicks during its 0.2-second button active effect.
            await pilot.pause(0.25)
            await pilot.click("#export-write")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert (path.parent / "all-filtered.json").exists(), str(
                app.screen.query_one("#export-status", Static).render()
            )
            all_rows = TableProjection.model_validate_json(
                (path.parent / "all-filtered.json").read_bytes()
            )
            assert all_rows.record_count > 1 and not all_rows.truncated
            await pilot.press("escape")
            await pilot.press("ctrl+c")
        assert app.return_value == errno.ENODATA

    asyncio.run(exercise())


def test_terminal_refuses_artifact_changed_after_export(export_index, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Button, Input, Static

    from code_analysis.tui_app import AnalysisTerminal

    def changed_export(*args):
        path, projection = export_browse_page(*args)
        path.write_text("changed after publication")
        return path, projection

    monkeypatch.setattr("code_analysis.tui_export.export_browse_page", changed_export)

    async def exercise():
        app = AnalysisTerminal(export_index)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("f2")
            await app.workers.wait_for_complete()
            await pilot.press("ctrl+e")
            await pilot.pause()
            app.screen.query_one("#export-name", Input).value = "changed"
            await pilot.click("#export-write")
            await app.workers.wait_for_complete()
            assert app.screen.query_one("#export-open", Button).disabled
            assert "changed after publication" in str(
                app.screen.query_one("#export-status", Static).render()
            )
            assert app.exit_status == errno.EBADMSG

    asyncio.run(exercise())
