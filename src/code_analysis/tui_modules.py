"""Renderer-independent local dependency navigation and saved symbol inspection."""

from __future__ import annotations

import asyncio
import errno
import json
import sqlite3
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Select, Static

from code_analysis.artifact_viewer import ViewerCommand
from code_analysis.browse_index import BrowseIndex
from code_analysis.module_navigation import ModuleNavigationPage, ModuleView
from code_analysis.tui_app import RowDetails, plain_text


class ModuleBrowser(ModalScreen[int]):
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+n", "next", "Next page"),
        ("ctrl+p", "previous", "Previous page"),
        ("alt+left", "back", "Back"),
        ("ctrl+d", "dependencies", "Explore paths / SCC"),
    ]
    CSS = """
    ModuleBrowser { align: center middle; }
    #module-dialog { width: 95%; height: 90%; border: round $accent;
                     background: $surface; padding: 1; }
    #module-status { height: auto; }
    #module-records { height: 1fr; }
    #module-buttons { height: 3; }
    """

    def __init__(
        self,
        index: BrowseIndex,
        module: str,
        on_error: Callable[[int], None],
        source_root: Path | None = None,
        web_action: Callable[[str, str | None], None] | None = None,
        viewer: ViewerCommand | None = None,
    ) -> None:
        super().__init__()
        self.index = index
        self.preview_root = source_root
        self.module = module
        self.on_error = on_error
        self.web_action = web_action
        self.viewer = viewer
        self.view: ModuleView = "outgoing"
        self.record_offset = 0
        self.page: ModuleNavigationPage | None = None
        self.history: deque[str] = deque(maxlen=100)
        self.error_status = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="module-dialog"):
            yield Static("Local dependency adjacency · Enter follows an edge or inspects a symbol")
            yield Select(
                [
                    ("Outgoing imports", "outgoing"),
                    ("Incoming imports", "incoming"),
                    ("Symbols", "symbols"),
                ],
                value="outgoing",
                allow_blank=False,
                id="module-view",
            )
            yield Static("Loading…", id="module-status")
            yield DataTable(id="module-records", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="module-buttons"):
                yield Button(
                    "Open graph in Web UI", id="module-web", disabled=self.web_action is None
                )
                yield Button("Explore paths / SCC", id="module-dependencies")
                yield Button("Back", id="module-back", disabled=True)
                yield Button("Previous", id="module-previous", disabled=True)
                yield Button("Next", id="module-next", disabled=True)
                yield Button("Close", id="module-close")

    def on_mount(self) -> None:
        self.load_page()

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        if (
            event.select.id == "module-view"
            and event.value in ("outgoing", "incoming", "symbols")
            and event.value != self.view
        ):
            self.view = event.value
            self.record_offset = 0
            self.load_page()

    @work(exclusive=True, group="module")
    async def load_page(self) -> None:
        selection = (self.module, self.view, self.record_offset)
        self.page = None
        table = self.query_one("#module-records", DataTable)
        table.clear(columns=True)
        for name in ("next", "previous"):
            self.query_one(f"#module-{name}", Button).disabled = True
        self.query_one("#module-status", Static).update("Loading bounded module page…")
        try:
            page = await asyncio.to_thread(self.index.module_navigation, *selection)
        except (ValueError, OSError, sqlite3.Error) as exc:
            if self.is_mounted and selection == (self.module, self.view, self.record_offset):
                self.error_status = errno.EBADMSG
                self.on_error(self.error_status)
                self.query_one("#module-status", Static).update(
                    plain_text(f"Navigation unavailable: {exc}")
                )
            return
        if not self.is_mounted or selection != (self.module, self.view, self.record_offset):
            return
        self.page = page
        if page.view == "symbols":
            table.add_columns("Symbol", "Kind", "Start", "End")
            for number, symbol in enumerate(page.symbols):
                table.add_row(
                    plain_text(symbol.qualified_name),
                    symbol.kind,
                    str(symbol.span.start_line),
                    str(symbol.span.end_line),
                    key=str(number),
                )
        else:
            table.add_columns("Source →", "Target", "Kind")
            for number, edge in enumerate(page.edges):
                table.add_row(
                    plain_text(edge.source_id),
                    plain_text(edge.target_id),
                    plain_text(edge.kind),
                    key=str(number),
                )
        self.query_one("#module-status", Static).update(
            plain_text(
                f"{self.module} · source {page.source_completeness.value} · "
                f"{page.offset + page.retained}/{page.eligible} {page.view}\n"
                "Known local modules only; external/unresolved imports excluded. "
                "History retains 100 steps."
            )
        )
        self.query_one("#module-next", Button).disabled = not page.truncated
        self.query_one("#module-previous", Button).disabled = page.offset == 0
        self.query_one("#module-back", Button).disabled = not self.history
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if self.page is None:
            return
        position = int(event.row_key.value)
        if self.view == "symbols":
            symbol = self.page.symbols[position]
            self.app.push_screen(
                RowDetails(
                    json.dumps(symbol.model_dump(mode="json"), indent=2, ensure_ascii=False),
                    "Saved symbol — source text is not loaded",
                    source_action=lambda: self.show_source(
                        symbol.span.path, symbol.span.start_line, symbol.span.end_line
                    ),
                    web_action=(
                        (
                            lambda: self.web_action(
                                "symbol", f"{self.module}:{symbol.qualified_name}"
                            )
                        )
                        if self.web_action is not None
                        else None
                    ),
                )
            )
        else:
            edge = self.page.edges[position]
            self.history.append(self.module)
            self.module = edge.target_id if self.view == "outgoing" else edge.source_id
            self.record_offset = 0
            self.load_page()

    def show_source(self, path: str, start: int, end: int) -> None:
        from code_analysis.tui_source import TerminalSource

        self.app.push_screen(
            TerminalSource(self.index, self.preview_root, path, start, end, self.on_error)
        )

    def action_next(self) -> None:
        if self.page is not None and self.page.truncated and self.record_offset == self.page.offset:
            self.record_offset += 25
            self.load_page()

    def action_previous(self) -> None:
        if self.record_offset:
            self.record_offset = max(0, self.record_offset - 25)
            self.load_page()

    def action_back(self) -> None:
        if self.history:
            self.module = self.history.pop()
            self.record_offset = 0
            self.load_page()

    def action_dependencies(self) -> None:
        from code_analysis.tui_dependencies import DependencyBrowser

        self.app.push_screen(
            DependencyBrowser(self.index, self.module, self.on_error, self.viewer),
            self.dependencies_closed,
        )

    def dependencies_closed(self, module: str | None) -> None:
        if module is None or module == self.module:
            return
        self.history.append(self.module)
        self.module = module
        self.record_offset = 0
        self.load_page()

    def action_close(self) -> None:
        self.workers.cancel_group(self, "module")
        self.dismiss(self.error_status)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        actions = {
            "module-web": lambda: (
                self.web_action("graph", self.module) if self.web_action is not None else None
            ),
            "module-next": self.action_next,
            "module-previous": self.action_previous,
            "module-back": self.action_back,
            "module-dependencies": self.action_dependencies,
            "module-close": self.action_close,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()
