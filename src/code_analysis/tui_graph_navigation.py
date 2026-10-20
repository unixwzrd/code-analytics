"""Saved-span navigation reached from a selected prepared static graph node."""

from __future__ import annotations

import asyncio
import errno
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from code_analysis.browse_index import BrowseIndex
from code_analysis.static_graph_browse import (
    StaticGraphReadLimitError,
    StaticGraphRequest,
    StaticGraphSelectionError,
)
from code_analysis.tui_app import RowDetails, plain_text
from code_analysis.ui_navigation import NavigationItem, NavigationPage, NavigationUnavailable


class GraphNodeDetails(RowDetails):
    """Recorded node details with an explicit, source-free span-navigation action."""

    def __init__(
        self,
        detail: str,
        *,
        open_navigation: Callable[[], None],
    ) -> None:
        super().__init__(
            detail,
            "Recorded static graph node; span navigation uses only the prepared index.",
        )
        self.open_navigation = open_navigation

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="row-dialog"):
            yield Static(plain_text(self.detail_title))
            yield Button("Navigate recorded spans", id="navigate-recorded-spans")
            yield Static(plain_text(self.detail), id="row-detail")
            yield Button("Close (Escape)", id="close-detail")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.prevent_default()
        event.stop()
        if event.button.id == "navigate-recorded-spans":
            self.open_navigation()
        else:
            self.dismiss()


class StaticGraphNavigation(ModalScreen[int]):
    """Page exact retained source spans for one explicitly selected graph node."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+n", "next", "Next page"),
        ("ctrl+p", "previous", "Previous page"),
    ]
    CSS = """
    StaticGraphNavigation { align: center middle; }
    #static-graph-navigation-dialog { width: 95%; height: 90%; border: round $accent;
                                      background: $surface; padding: 1; }
    #static-graph-navigation-status { height: auto; max-height: 3; }
    #static-graph-navigation-records { height: 1fr; }
    #static-graph-navigation-actions { height: 3; }
    """

    def __init__(
        self,
        index: BrowseIndex,
        request: StaticGraphRequest,
        identity: str,
        on_status: Callable[[int], None],
        source_root: Path | None = None,
    ) -> None:
        super().__init__()
        self.index = index
        self.request = request
        self.identity = identity
        self.on_status = on_status
        self.source_root = source_root
        self.page_offset = 0
        self.page: NavigationPage | None = None
        self.revision = 0
        self.error_status = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="static-graph-navigation-dialog"):
            yield Static(
                "Prepared graph node → exact saved spans · Enter details; "
                "source preview is explicit."
            )
            yield Static("Loading…", id="static-graph-navigation-status")
            yield DataTable(
                id="static-graph-navigation-records", cursor_type="row", zebra_stripes=True
            )
            with Horizontal(id="static-graph-navigation-actions"):
                yield Button("Previous", id="static-graph-navigation-previous", disabled=True)
                yield Button("Next", id="static-graph-navigation-next", disabled=True)
                yield Button("Close", id="static-graph-navigation-close")

    def on_mount(self) -> None:
        # A node-detail action can push this screen while its parent is still
        # dispatching a button event.  Wait until this screen has composed its
        # own widgets before starting the background read.
        self.call_after_refresh(self.load_page)

    @work(exclusive=True, group="static-graph-navigation")
    async def load_page(self) -> None:
        self.revision += 1
        revision, offset = self.revision, self.page_offset
        self.page = None
        table = self.query_one("#static-graph-navigation-records", DataTable)
        table.clear(columns=True)
        for name in ("previous", "next"):
            self.query_one(f"#static-graph-navigation-{name}", Button).disabled = True
        self.query_one("#static-graph-navigation-status", Static).update(
            "Loading exact prepared source-span records…"
        )
        try:
            page = await asyncio.to_thread(
                self.index.graph_navigation, self.request, self.identity, offset
            )
        except StaticGraphSelectionError as exc:
            status = errno.EINVAL
            message = f"Unknown retained graph node: {exc}"
        except NavigationUnavailable as exc:
            status = errno.ENODATA
            message = f"Saved source navigation is unavailable: {exc}"
        except (ValueError, sqlite3.Error) as exc:
            status = errno.ENODATA if isinstance(exc, StaticGraphReadLimitError) else errno.EBADMSG
            state = "unavailable" if isinstance(exc, StaticGraphReadLimitError) else "malformed"
            message = f"Saved source navigation is {state}: {exc}"
        except OSError as exc:
            status = exc.errno or errno.EIO
            message = f"Saved source navigation is unavailable: {exc}"
        else:
            if (
                not self.is_mounted
                or revision != self.revision
                or offset != self.page_offset
                or self.app.screen is not self
            ):
                return
            self.page = page
            table.add_columns("Label", "Kind", "Path", "Start", "End")
            for number, item in enumerate(page.records):
                table.add_row(
                    plain_text(item.label),
                    plain_text(item.kind),
                    plain_text(item.span.path),
                    f"{item.span.start_line}:{item.span.start_column}",
                    f"{item.span.end_line}:{item.span.end_column}",
                    key=str(number),
                )
            if page.completeness.value != "complete":
                self.error_status = errno.ENODATA
                self.on_status(self.error_status)
            first = page.offset + 1 if page.records else 0
            self.query_one("#static-graph-navigation-status", Static).update(
                plain_text(
                    f"{page.label} · source {page.completeness.value}; {first}-"
                    f"{page.offset + len(page.records)} of {page.eligible} exact saved spans. "
                    + ("More pages available." if page.next_offset is not None else "End of spans.")
                )
            )
            self.query_one("#static-graph-navigation-previous", Button).disabled = page.offset == 0
            self.query_one("#static-graph-navigation-next", Button).disabled = (
                page.next_offset is None
            )
            table.focus()
            return
        if (
            self.is_mounted
            and revision == self.revision
            and offset == self.page_offset
            and self.app.screen is self
        ):
            self.error_status = status
            self.on_status(status)
            self.query_one("#static-graph-navigation-status", Static).update(plain_text(message))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if self.page is None or self.page.offset != self.page_offset:
            return
        try:
            row_key = event.row_key.value
            if row_key is None:
                return
            position = int(row_key)
            if position < 0:
                return
            item = self.page.records[position]
        except (IndexError, TypeError, ValueError):
            return
        self.open_item(item)

    def open_item(self, item: NavigationItem) -> None:
        if (
            self.page is None
            or self.page.offset != self.page_offset
            or item not in self.page.records
        ):
            return
        self.app.push_screen(
            RowDetails(
                json.dumps(item.model_dump(mode="json"), indent=2, ensure_ascii=False),
                "Exact saved source span; source text is not opened automatically.",
                source_action=(
                    (lambda: self.preview(item)) if self.source_root is not None else None
                ),
            )
        )

    def preview(self, item: NavigationItem) -> None:
        if (
            self.source_root is None
            or self.page is None
            or self.page.offset != self.page_offset
            or item not in self.page.records
        ):
            return
        from code_analysis.tui_source import TerminalSource

        self.app.push_screen(
            TerminalSource(
                self.index,
                self.source_root,
                item.span.path,
                item.span.start_line,
                item.span.end_line,
                self.on_status,
            )
        )

    def action_next(self) -> None:
        if self.page is not None and self.page.next_offset is not None:
            next_offset = self.page.next_offset
            self.page = None
            self.page_offset = next_offset
            self.load_page()

    def action_previous(self) -> None:
        if self.page_offset:
            self.page = None
            self.page_offset = max(0, self.page_offset - 25)
            self.load_page()

    def action_close(self) -> None:
        self.revision += 1
        self.workers.cancel_group(self, "static-graph-navigation")
        self.dismiss(self.error_status)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        actions = {
            "static-graph-navigation-previous": self.action_previous,
            "static-graph-navigation-next": self.action_next,
            "static-graph-navigation-close": self.action_close,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()
