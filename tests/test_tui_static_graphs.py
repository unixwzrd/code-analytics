from __future__ import annotations

import asyncio
import errno
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName
from code_analysis.service import scan
from code_analysis.static_graph_browse import GRAPH_PROJECTIONS, StaticGraphRequest
from code_analysis.web_service import ReportViewService

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def structure_report():
    return scan(discover_context(FIXTURES / "structure_project", AnalysisConfig()))


@pytest.fixture
def static_graph_index(structure_report, tmp_path):
    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(structure_report), directory)
    return BrowseIndex.open(directory, report_sha256=manifest.report_sha256)


def _available_with_nodes(index):
    return next(item for item in index.manifest.static_graphs if item.available and item.node_count)


def test_valid_collapse_read_quota_is_enodata_not_malformed(static_graph_index, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Button, Select

    import code_analysis.static_graph_browse as storage
    from code_analysis.browse_export import BrowseExportLimitError, export_browse_page
    from code_analysis.projection_models import ProjectionCollapse
    from code_analysis.reports.projection_views import ProjectionFormat
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_static_graphs import StaticGraphBrowser

    async def exercise():
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphBrowser(static_graph_index, app.evidence_closed)
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.graph is not None
            monkeypatch.setattr(storage, "_MAX_RESULT_BYTES", 1)
            await pilot.press("ctrl+l")
            await pilot.pause()
            app.screen.query_one("#graph-collapse-mode", Select).value = "module"
            app.screen.query_one("#graph-language-use", Button).press()
            await pilot.pause()
            screen.apply()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.error_status == errno.ENODATA
            assert screen.graph is None and not screen._rows
            assert screen.query_one("#static-graph-export", Button).disabled
            assert screen.request.collapse is ProjectionCollapse.MODULE
            with pytest.raises(storage.StaticGraphReadLimitError):
                static_graph_index.static_graph(screen.request)
            with pytest.raises(BrowseExportLimitError):
                export_browse_page(
                    static_graph_index, screen.request, "over-budget", ProjectionFormat.JSON_COMPACT
                )
            assert not (static_graph_index.database.parent / "exports").exists()

    asyncio.run(exercise())


def test_graph_collapse_options_export_and_restore(static_graph_index):
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable, Select, SelectionList, Static

    from code_analysis.models import SourceLanguage
    from code_analysis.projection_models import ProjectionCollapse
    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_export import ExportPage
    from code_analysis.tui_static_graphs import StaticGraphBrowser, StaticGraphOptions

    async def exercise():
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphBrowser(static_graph_index, app.evidence_closed)
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            original = screen.graph
            for mode in (
                ProjectionCollapse.MODULE,
                ProjectionCollapse.PACKAGE,
                ProjectionCollapse.NONE,
            ):
                await pilot.press("ctrl+l")
                await pilot.pause()
                editor = app.screen
                assert isinstance(editor, StaticGraphOptions)
                control = editor.query_one("#graph-collapse-mode", Select)
                control.value = mode.value
                editor.query_one("#graph-language-choices", SelectionList).select("python")
                for widget in (control, *editor.query(Button)):
                    assert 0 <= widget.region.x < widget.region.right <= 80
                    assert 0 <= widget.region.y < widget.region.bottom <= 24
                editor.query_one("#graph-language-use", Button).press()
                await pilot.pause()
                assert screen.graph is None
                assert screen.query_one("#static-graph-export", Button).disabled
                screen.query_one("#static-graph-apply", Button).press()
                await pilot.pause()
                await app.workers.wait_for_complete()
                assert screen.graph is not None
                assert screen.graph.selection.collapse is mode
                assert screen.graph.selection.languages == (SourceLanguage.PYTHON,)
                assert f"Collapse: {mode.value}" in str(
                    screen.query_one("#static-graph-help", Static).content
                )
                assert screen.query_one("#static-graph-table", DataTable).region.height >= 4
                if mode is not ProjectionCollapse.NONE:
                    table = screen.query_one("#static-graph-table", DataTable)
                    table.move_cursor(row=0)
                    table.focus()
                    await pilot.press("enter")
                    await pilot.pause()
                    assert isinstance(app.screen, RowDetails)
                    assert "Grouped view node" in app.screen.detail_title
                    assert not list(app.screen.query("#navigate-recorded-spans"))
                    await pilot.press("escape")
                    await pilot.pause()
                screen.action_export()
                await pilot.pause()
                assert isinstance(app.screen, ExportPage)
                assert app.screen.request.collapse is mode
                await pilot.press("escape")
                await pilot.pause()
            assert screen.graph.nodes == original.nodes
            assert screen.graph.edges == original.edges

    asyncio.run(exercise())


def test_invalid_graph_apply_synchronously_clears_stale_evidence(static_graph_index):
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable, Input

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_static_graphs import StaticGraphBrowser

    async def exercise():
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphBrowser(static_graph_index, app.evidence_closed)
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.graph is not None
            screen.query_one("#static-graph-nodes", Input).value = ""
            # Apply before Textual delivers the pending Input.Changed event.
            screen.apply()
            assert screen.error_status == errno.EINVAL
            assert screen.graph is None and not screen._rows
            assert screen.query_one("#static-graph-table", DataTable).row_count == 0
            assert screen.query_one("#static-graph-details", Button).disabled
            assert screen.query_one("#static-graph-export", Button).disabled

    asyncio.run(exercise())


def test_graph_languages_edit_cancel_apply_and_recover(static_graph_index):
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable, SelectionList, Static

    from code_analysis.models import SourceLanguage
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_static_graphs import StaticGraphBrowser, StaticGraphOptions

    async def exercise():
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphBrowser(static_graph_index, app.evidence_closed)
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            original = screen.graph
            assert original is not None
            original_status = screen.error_status
            button = screen.query_one("#static-graph-languages", Button)
            assert 0 <= button.region.x < button.region.right <= 80
            assert 0 <= button.region.y < button.region.bottom <= 24
            assert screen.query_one("#static-graph-table", DataTable).region.height >= 4
            await pilot.press("ctrl+l")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, StaticGraphOptions)
            choices = editor.query_one("#graph-language-choices", SelectionList)
            choices.select("python")
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is screen and screen.graph is original
            assert screen.languages == ()

            for selected, expected_status in (("python", None), ("javascript", errno.EINVAL)):
                button.press()
                await pilot.pause()
                editor = app.screen
                assert isinstance(editor, StaticGraphOptions)
                choices = editor.query_one("#graph-language-choices", SelectionList)
                choices.deselect_all()
                choices.select(selected)
                for widget in (choices, *editor.query(Button)):
                    assert 0 <= widget.region.x < widget.region.right <= 80
                    assert 0 <= widget.region.y < widget.region.bottom <= 24
                editor.query_one("#graph-language-use", Button).press()
                await pilot.pause()
                assert screen.graph is None
                assert selected in str(screen.query_one("#static-graph-help", Static).content)
                assert screen.query_one("#static-graph-export", Button).disabled
                screen.query_one("#static-graph-apply", Button).press()
                await pilot.pause()
                await app.workers.wait_for_complete()
                assert screen.request.languages == (SourceLanguage(selected),)
                if expected_status is None:
                    assert screen.graph is not None
                    assert screen.graph.nodes == original.nodes
                    assert screen.graph.selection.languages == (SourceLanguage.PYTHON,)
                else:
                    assert screen.graph is None and screen.error_status == expected_status
                    assert screen.query_one("#static-graph-table", DataTable).row_count == 0

            button.press()
            await pilot.pause()
            app.screen.query_one("#graph-language-choices", SelectionList).deselect_all()
            app.screen.query_one("#graph-language-use", Button).press()
            await pilot.pause()
            screen.query_one("#static-graph-apply", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.request.languages == ()
            assert screen.graph == original
            assert screen.error_status == original_status

    asyncio.run(exercise())


def test_static_graph_browser_selects_exact_neighborhood_bounds_details_and_export(
    static_graph_index,
):
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable, Input, Select, Static

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_export import ExportPage
    from code_analysis.tui_static_graphs import StaticGraphBrowser

    summary = _available_with_nodes(static_graph_index)
    full = static_graph_index.static_graph(
        StaticGraphRequest(name=summary.name, max_nodes=100, max_edges=250)
    )
    selected = full.nodes[0].node_id

    async def exercise():
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphBrowser(
                static_graph_index, app.evidence_closed, initial_name=summary.name
            )
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            labels = list(screen.query(".static-graph-bound-label").results(Static))
            assert [str(label.content) for label in labels] == ["Records", "Nodes", "Edges"]
            visible = [
                screen.query_one("#static-graph-name", Select),
                *labels,
                screen.query_one("#static-graph-records", Input),
                screen.query_one("#static-graph-nodes", Input),
                screen.query_one("#static-graph-edges", Input),
                screen.query_one("#static-graph-apply", Button),
                screen.query_one("#static-graph-table", DataTable),
                screen.query_one("#static-graph-export", Button),
                screen.query_one("#static-graph-close", Button),
            ]
            for widget in visible:
                assert widget.region.width > 0 and widget.region.height > 0
                assert widget.region.x >= 0 and widget.region.y >= 0
                assert widget.region.right <= 80 and widget.region.bottom <= 24
            bounds = [
                labels[0],
                screen.query_one("#static-graph-records", Input),
                labels[1],
                screen.query_one("#static-graph-nodes", Input),
                labels[2],
                screen.query_one("#static-graph-edges", Input),
                screen.query_one("#static-graph-apply", Button),
            ]
            assert len({widget.region.y for widget in bounds}) == 1
            assert all(left.region.right <= right.region.x for left, right in pairwise(bounds))
            assert screen.query_one("#static-graph-table", DataTable).region.height >= 4
            screen.query_one("#static-graph-selected", Input).value = selected
            screen.query_one("#static-graph-records", Input).value = "6"
            screen.query_one("#static-graph-nodes", Input).value = "4"
            screen.query_one("#static-graph-edges", Input).value = "2"
            await pilot.pause()
            assert screen.graph is None
            assert screen.query_one("#static-graph-export", Button).disabled
            screen.query_one("#static-graph-apply", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.request == StaticGraphRequest(
                name=summary.name,
                selected_ids=(selected,),
                max_records=6,
                max_nodes=4,
                max_edges=2,
            )
            assert screen.graph is not None
            assert selected in {node.node_id for node in screen.graph.nodes}
            assert len(screen.graph.nodes) <= 4 and len(screen.graph.edges) <= 2
            assert screen.query_one("#static-graph-table", DataTable).row_count == len(
                screen.graph.nodes
            ) + len(screen.graph.edges)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RowDetails)
            assert '"node"' in app.screen.detail
            await pilot.press("escape")
            await pilot.pause()
            screen.query_one("#static-graph-details", Button).press()
            await pilot.pause()
            assert isinstance(app.screen, RowDetails)
            assert '"preparation"' in app.screen.detail
            assert '"display_counts"' in app.screen.detail
            await pilot.press("escape", "ctrl+e")
            await pilot.pause()
            assert isinstance(app.screen, ExportPage)
            assert app.screen.request == screen.request
            await pilot.press("escape")
            await pilot.pause()
            # Even before Textual delivers Input.Changed, the exact control
            # comparison prevents a stale export or detail action.
            screen.query_one("#static-graph-nodes", Input).value = "5"
            screen.action_export()
            screen.action_status_details()
            assert app.screen is screen
            await pilot.pause()
            screen.query_one("#static-graph-nodes", Input).value = "4"
            screen.query_one("#static-graph-selected", Input).value = "not-indexed"
            screen.query_one("#static-graph-apply", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.graph is None
            assert app.exit_status == errno.EINVAL
            assert "Invalid static graph selection" in str(
                screen.query_one("#static-graph-status", Static).content
            )
            await pilot.press("escape", "escape", "ctrl+c")

    asyncio.run(exercise())


def test_static_projection_browser_dispatches_structure_graphs(static_graph_index):
    pytest.importorskip("textual")

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_static_graphs import StaticGraphBrowser
    from code_analysis.tui_static_projections import StaticProjectionBrowser

    async def exercise():
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            parent = StaticProjectionBrowser(static_graph_index, app.evidence_closed)
            app.push_screen(parent)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, StaticGraphBrowser)
            await pilot.press("escape", "escape", "ctrl+c")

    asyncio.run(exercise())


def test_static_graph_browser_reports_unavailable_summary_explicitly(static_graph_index):
    pytest.importorskip("textual")
    from textual.widgets import Button, Static

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_static_graphs import StaticGraphBrowser

    summaries = tuple(
        item.model_copy(
            update={
                "available": False,
                "unavailable_reason": "fixture has no authoritative graph evidence",
                "node_count": 0,
                "edge_count": 0,
                "cluster_count": 0,
                "original_node_count": 0,
                "original_edge_count": 0,
                "truncated": False,
                "truncation_reason": None,
            }
        )
        for item in static_graph_index.manifest.static_graphs
    )
    fake_index = SimpleNamespace(
        manifest=SimpleNamespace(
            static_graphs=summaries,
            overview=static_graph_index.manifest.overview,
        )
    )

    async def exercise():
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphBrowser(fake_index, app.evidence_closed)
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            status = str(screen.query_one("#static-graph-status", Static).content)
            assert "unavailable" in status
            assert "fixture has no authoritative graph evidence" in status
            assert screen.graph is None
            assert screen.query_one("#static-graph-export", Button).disabled
            assert app.exit_status == errno.ENODATA
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())


def test_static_graph_edits_and_close_reject_stale_async_result(static_graph_index, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Input

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_static_graphs import StaticGraphBrowser

    summary = _available_with_nodes(static_graph_index)
    original = BrowseIndex.static_graph
    calls = 0

    def delayed(index, request):
        nonlocal calls
        calls += 1
        import time

        time.sleep(0.04)
        return original(index, request)

    monkeypatch.setattr(BrowseIndex, "static_graph", delayed)

    async def exercise():
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphBrowser(
                static_graph_index, app.evidence_closed, initial_name=summary.name
            )
            app.push_screen(screen)
            await pilot.pause(0.005)
            screen.query_one("#static-graph-selected", Input).value = "edited-before-load"
            await pilot.pause(0.08)
            assert screen.graph is None
            await pilot.press("escape")
            await pilot.pause(0.08)
            assert app.screen is not screen
            assert screen.graph is None
            assert calls == 1
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_static_graph_request_names_remain_canonical(static_graph_index):
    assert (
        tuple(item.name for item in static_graph_index.manifest.static_graphs) == GRAPH_PROJECTIONS
    )
    assert set(GRAPH_PROJECTIONS) == {
        ProjectionName.OWNERSHIP_ALIAS_FLOW,
        ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
        ProjectionName.CONVERSION_GRAPH,
        ProjectionName.DEPENDENCIES,
        ProjectionName.DICTIONARY_FLOW,
        ProjectionName.LEXICAL_CALL_GRAPH,
        ProjectionName.STATIC_CALL_GRAPH,
    }
