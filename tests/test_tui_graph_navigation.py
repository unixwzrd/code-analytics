from __future__ import annotations

import asyncio
import errno
from pathlib import Path

import pytest

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness, SourceLanguage, SourceSpan
from code_analysis.project import discover_context
from code_analysis.service import scan
from code_analysis.static_graph_browse import StaticGraphRequest
from code_analysis.ui_navigation import NavigationItem, NavigationPage
from code_analysis.web_service import ReportViewService

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def static_graph_index(tmp_path: Path) -> BrowseIndex:
    report = scan(discover_context(FIXTURES / "structure_project", AnalysisConfig()))
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), destination)
    return BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def _available_node(index: BrowseIndex) -> tuple[StaticGraphRequest, str]:
    import sqlite3

    with sqlite3.connect(index.database) as connection:
        row = connection.execute(
            "SELECT node_id FROM static_graph_navigation_subjects "
            "WHERE json_extract(payload, '$.available') = 1 ORDER BY node_id LIMIT 1"
        ).fetchone()
    assert row is not None
    for summary in index.manifest.static_graphs:
        if not summary.available:
            continue
        request = StaticGraphRequest(name=summary.name, max_nodes=100, max_edges=250)
        if row[0] in {node.node_id for node in index.static_graph(request).nodes}:
            return request, row[0]
    raise AssertionError("fixture has no retained navigable graph node")


def test_navigation_read_quota_is_unavailable_not_malformed(static_graph_index, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Static

    import code_analysis.static_graph_browse as storage
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_graph_navigation import StaticGraphNavigation

    request, identity = _available_node(static_graph_index)
    monkeypatch.setattr(storage, "_MAX_RESULT_BYTES", 1)

    async def exercise():
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphNavigation(
                static_graph_index, request, identity, app.evidence_closed
            )
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.error_status == errno.ENODATA
            assert screen.page is None
            message = str(screen.query_one("#static-graph-navigation-status", Static).content)
            assert "unavailable" in message and "byte budget" in message
            assert "malformed" not in message

    asyncio.run(exercise())


def test_terminal_graph_navigation_uses_saved_spans_and_explicit_preview(
    static_graph_index, tmp_path: Path
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable, Static

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_graph_navigation import StaticGraphNavigation
    from code_analysis.tui_source import TerminalSource

    request, identity = _available_node(static_graph_index)

    async def exercise() -> None:
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphNavigation(
                static_graph_index,
                request,
                identity,
                app.evidence_closed,
                FIXTURES / "structure_project",
            )
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None
            widgets = (
                screen.query_one("#static-graph-navigation-status", Static),
                screen.query_one("#static-graph-navigation-records", DataTable),
                screen.query_one("#static-graph-navigation-previous", Button),
                screen.query_one("#static-graph-navigation-next", Button),
                screen.query_one("#static-graph-navigation-close", Button),
            )
            assert all(
                widget.region.width > 0
                and widget.region.height > 0
                and widget.region.right <= 80
                and widget.region.bottom <= 24
                for widget in widgets
            )
            assert "exact saved spans" in str(
                screen.query_one("#static-graph-navigation-status", Static).content
            )
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RowDetails)
            assert app.screen.source_action is not None
            app.screen.query_one("#preview-source", Button).press()
            await pilot.pause()
            assert isinstance(app.screen, TerminalSource)
            await app.workers.wait_for_complete()
            assert app.screen.preview is not None
            assert app.screen.preview.state == "bound"
            await pilot.press("escape", "escape", "ctrl+c")

    asyncio.run(exercise())


def test_terminal_graph_navigation_pages_without_source_and_handles_selection_errors(
    static_graph_index, monkeypatch
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_graph_navigation import StaticGraphNavigation

    request, identity = _available_node(static_graph_index)
    records = tuple(
        NavigationItem(
            identity=f"item-{number}",
            label=f"Item {number}",
            kind="function",
            span=SourceSpan(
                path="package/module.py",
                start_line=number + 1,
                start_column=0,
                end_line=number + 1,
                end_column=1,
                language=SourceLanguage.PYTHON,
            ),
        )
        for number in range(30)
    )

    def paged(_index, _request, current_identity, offset=0):
        if current_identity == "unknown":
            from code_analysis.static_graph_browse import StaticGraphSelectionError

            raise StaticGraphSelectionError("selection is not a retained graph node")
        subset = records[offset : offset + 25]
        return NavigationPage(
            identity=current_identity,
            label=current_identity,
            completeness=Completeness.COMPLETE,
            offset=offset,
            eligible=len(records),
            records=subset,
            next_offset=offset + len(subset) if offset + len(subset) < len(records) else None,
        )

    monkeypatch.setattr(BrowseIndex, "graph_navigation", paged)

    async def exercise() -> None:
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphNavigation(
                static_graph_index, request, identity, app.evidence_closed
            )
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None and len(screen.page.records) == 25
            assert not screen.query_one("#static-graph-navigation-next", Button).disabled
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RowDetails)
            assert app.screen.source_action is None
            await pilot.press("escape", "ctrl+n")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None and screen.page.offset == 25
            assert screen.query_one("#static-graph-navigation-records", DataTable).row_count == 5
            await pilot.press("ctrl+p")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None and screen.page.offset == 0
            await pilot.press("escape", "ctrl+c")

        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            unknown = StaticGraphNavigation(
                static_graph_index, request, "unknown", app.evidence_closed
            )
            app.push_screen(unknown)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.exit_status == errno.EINVAL
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())


def test_terminal_graph_navigation_mismatched_source_discloses_no_text(
    static_graph_index, tmp_path: Path
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Button

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_graph_navigation import StaticGraphNavigation
    from code_analysis.tui_source import TerminalSource

    request, identity = _available_node(static_graph_index)

    async def exercise() -> None:
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphNavigation(
                static_graph_index, request, identity, app.evidence_closed, tmp_path
            )
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RowDetails)
            app.screen.query_one("#preview-source", Button).press()
            await pilot.pause()
            assert isinstance(app.screen, TerminalSource)
            await app.workers.wait_for_complete()
            assert app.screen.preview is not None
            assert app.screen.preview.state == "unavailable"
            assert not app.screen.preview.lines
            await pilot.press("escape", "escape", "ctrl+c")

    asyncio.run(exercise())


def test_graph_node_details_open_navigation_without_replacing_graph_selection(
    static_graph_index,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable, Input

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_graph_navigation import GraphNodeDetails, StaticGraphNavigation
    from code_analysis.tui_static_graphs import StaticGraphBrowser

    request, identity = _available_node(static_graph_index)

    async def exercise() -> None:
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            browser = StaticGraphBrowser(
                static_graph_index, app.evidence_closed, initial_name=request.name
            )
            app.push_screen(browser)
            await pilot.pause()
            await app.workers.wait_for_complete()
            browser.query_one("#static-graph-selected", Input).value = identity
            browser.query_one("#static-graph-apply", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert browser.graph is not None
            selected_rows = [
                position
                for position, node in enumerate(browser.graph.nodes)
                if node.node_id == identity
            ]
            assert selected_rows == [0]
            table = browser.query_one("#static-graph-table", DataTable)
            table.cursor_coordinate = (0, selected_rows[0])
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, GraphNodeDetails)
            app.screen.query_one("#navigate-recorded-spans", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, StaticGraphNavigation)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, GraphNodeDetails)
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is browser
            assert browser.request.selected_ids == (identity,)
            assert browser.graph is not None
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())


def test_navigation_rejects_stale_row_actions_and_close_before_slow_read(
    static_graph_index, monkeypatch
) -> None:
    pytest.importorskip("textual")

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_graph_navigation import StaticGraphNavigation

    request, identity = _available_node(static_graph_index)
    records = tuple(
        NavigationItem(
            identity=f"item-{number}",
            label=f"Item {number}",
            kind="function",
            span=SourceSpan(
                path="package/module.py",
                start_line=number + 1,
                start_column=0,
                end_line=number + 1,
                end_column=1,
                language=SourceLanguage.PYTHON,
            ),
        )
        for number in range(30)
    )

    def paged(_index, _request, current_identity, offset=0):
        subset = records[offset : offset + 25]
        return NavigationPage(
            identity=current_identity,
            label=current_identity,
            completeness=Completeness.COMPLETE,
            offset=offset,
            eligible=len(records),
            records=subset,
            next_offset=offset + len(subset) if offset + len(subset) < len(records) else None,
        )

    monkeypatch.setattr(BrowseIndex, "graph_navigation", paged)

    async def stale_page() -> None:
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphNavigation(
                static_graph_index, request, identity, app.evidence_closed
            )
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None
            old_item = screen.page.records[0]
            screen.action_next()
            # Paging invalidates current records synchronously; a late selection
            # or row-detail callback cannot open a stale span.
            assert screen.page is None
            screen.open_item(old_item)
            assert app.screen is screen
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None and screen.page.offset == 25
            await pilot.press("escape", "ctrl+c")

    asyncio.run(stale_page())

    original = paged

    def delayed(index, selected_request, selected_identity, offset=0):
        import time

        time.sleep(0.04)
        return original(index, selected_request, selected_identity, offset)

    monkeypatch.setattr(BrowseIndex, "graph_navigation", delayed)

    async def close_before_resolution() -> None:
        app = AnalysisTerminal(static_graph_index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphNavigation(
                static_graph_index, request, identity, app.evidence_closed
            )
            app.push_screen(screen)
            await pilot.pause(0.005)
            await pilot.press("escape")
            await pilot.pause(0.08)
            assert app.screen is not screen
            assert screen.page is None
            await pilot.press("ctrl+c")

    asyncio.run(close_before_resolution())
