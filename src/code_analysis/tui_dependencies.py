"""Bounded static dependency path and component navigation for the terminal UI."""

from __future__ import annotations

import asyncio
import errno
import sqlite3
from collections.abc import Callable
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Select, Static

from code_analysis.artifact_viewer import ViewerCommand
from code_analysis.browse_index import BrowseIndex
from code_analysis.dependency_graph import DependencyGraphRequest
from code_analysis.dependency_navigation import (
    DependencyRequest,
    DependencyResult,
    ModulePageReader,
    investigate_dependencies,
)
from code_analysis.module_navigation import ModuleSelectionError
from code_analysis.runtime_index import RuntimeIndex
from code_analysis.tui_app import RowDetails, plain_text


class DependencyBrowser(ModalScreen[str | None]):
    """Explore one module's bounded local dependency path or SCC evidence."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "close", "Close")]
    CSS = """
    DependencyBrowser { align: center middle; }
    #dependency-dialog { width: 95%; height: 95%; border: round $accent;
                         background: $surface; padding: 1; }
    #dependency-status { height: auto; max-height: 7; }
    #dependency-controls, #dependency-buttons { height: 3; }
    #dependency-nodes, #dependency-edges { width: 10; }
    #dependency-mode { width: 18; }
    #dependency-target { width: 1fr; }
    """

    def __init__(
        self,
        reader: ModulePageReader,
        source: str,
        on_status: Callable[[int], None],
        viewer: ViewerCommand | None = None,
    ) -> None:
        super().__init__()
        self.reader = reader
        self.source = source
        self.on_status = on_status
        self.viewer = viewer
        # A self-target makes the initial path request valid without silently
        # changing the requested operation to an SCC investigation.
        self.request = DependencyRequest(source=source, target=source)
        self.result: DependencyResult | None = None
        self.controls_changed = False
        self.controls_ready = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dependency-dialog"):
            yield Static("Bounded local dependency investigation · Enter a module to follow it")
            with Horizontal(id="dependency-controls"):
                yield Select(
                    [("Path to target", "path"), ("Mutual component (SCC)", "component")],
                    value="path",
                    allow_blank=False,
                    id="dependency-mode",
                )
                yield Input(
                    self.source, placeholder="Target module (path mode)", id="dependency-target"
                )
                yield Input("500", placeholder="Max nodes", id="dependency-nodes", type="integer")
                yield Input("5000", placeholder="Max edges", id="dependency-edges", type="integer")
                yield Button("Apply", id="dependency-apply")
            yield Static("Loading…", id="dependency-status")
            yield DataTable(id="dependency-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="dependency-buttons"):
                yield Button("Evidence", id="dependency-detail", disabled=True)
                yield Button("Export graph", id="dependency-export", disabled=True)
                yield Button("Close", id="dependency-close")

    def on_mount(self) -> None:
        self.controls_ready = True
        self.load_result()

    def _request_from_controls(self) -> DependencyRequest:
        mode = self.query_one("#dependency-mode", Select).value
        target = self.query_one("#dependency-target", Input).value.strip()
        if mode not in ("path", "component"):
            raise ValueError("choose path or component mode")
        return DependencyRequest(
            source=self.source,
            target=target or None,
            mode=mode,
            max_nodes=int(self.query_one("#dependency-nodes", Input).value),
            max_edges=int(self.query_one("#dependency-edges", Input).value),
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        if not self.controls_ready or event.select.id != "dependency-mode":
            return
        if event.value == self.request.mode:
            return
        target = self.query_one("#dependency-target", Input)
        if event.value == "component":
            target.value = ""
            target.disabled = True
        elif event.value == "path":
            target.disabled = False
            if not target.value:
                target.value = self.source
        self._invalidate_controls()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.controls_ready and event.input.id in {
            "dependency-target",
            "dependency-nodes",
            "dependency-edges",
        }:
            self._invalidate_controls()

    def _invalidate_controls(self) -> None:
        """Keep visible evidence, but never export a request that has not been applied."""
        if self._controls_match_request():
            return
        self.controls_changed = True
        self.result = None
        if self.is_mounted:
            self.query_one("#dependency-detail", Button).disabled = True
            self.query_one("#dependency-export", Button).disabled = True
            self.query_one("#dependency-status", Static).update(
                "Controls changed; press Apply to refresh dependency evidence before exporting."
            )

    def _controls_match_request(self) -> bool:
        return (
            self.query_one("#dependency-mode", Select).value == self.request.mode
            and self.query_one("#dependency-target", Input).value.strip()
            == (self.request.target or "")
            and self.query_one("#dependency-nodes", Input).value == str(self.request.max_nodes)
            and self.query_one("#dependency-edges", Input).value == str(self.request.max_edges)
        )

    @work(exclusive=True, group="dependencies")
    async def load_result(self) -> None:
        request = self.request
        self.result = None
        table = self.query_one("#dependency-table", DataTable)
        table.clear(columns=True)
        self.query_one("#dependency-detail", Button).disabled = True
        self.query_one("#dependency-export", Button).disabled = True
        self.query_one("#dependency-status", Static).update(
            "Investigating bounded local dependencies…"
        )
        try:
            result = await asyncio.to_thread(investigate_dependencies, self.reader, request)
        except ModuleSelectionError as exc:
            if self.is_mounted and self.request == request:
                self.on_status(errno.EINVAL)
                self.query_one("#dependency-status", Static).update(
                    plain_text(f"Invalid module selection: {exc}")
                )
            return
        except ValueError as exc:
            if self.is_mounted and self.request == request:
                self.on_status(errno.EBADMSG)
                self.query_one("#dependency-status", Static).update(
                    plain_text(f"Dependency evidence unavailable: {exc}")
                )
            return
        except OSError as exc:
            if self.is_mounted and self.request == request:
                self.on_status(exc.errno or errno.EBADMSG)
                self.query_one("#dependency-status", Static).update(
                    plain_text(f"Dependency evidence unavailable: {exc}")
                )
            return
        except sqlite3.Error as exc:
            if self.is_mounted and self.request == request:
                self.on_status(errno.EBADMSG)
                self.query_one("#dependency-status", Static).update(
                    plain_text(f"Dependency evidence unavailable: {exc}")
                )
            return
        if not self.is_mounted or self.request != request or self.controls_changed:
            return
        self.result = result
        if result.source_completeness.value != "complete" or result.truncated:
            self.on_status(errno.ENODATA)
        if request.mode == "path":
            table.add_columns("Step", "Module")
            for step, module in enumerate(result.path):
                table.add_row(str(step), plain_text(module), key=module)
        else:
            table.add_columns("Mutually reachable module")
            for module in result.members:
                table.add_row(plain_text(module), key=module)
        finding = (
            "No local path found."
            if request.mode == "path"
            and not result.path
            and not result.truncated
            and result.source_completeness.value == "complete"
            else (
                "No path evidenced; absence remains unresolved with incomplete source or traversal."
                if request.mode == "path" and not result.path
                else (
                    f"{len(result.path)} modules in retained path."
                    if request.mode == "path"
                    else f"{len(result.members)} proven mutually reachable modules."
                )
            )
        )
        limitations = " ".join(result.limitations) or "Known local modules only."
        self.query_one("#dependency-status", Static).update(
            plain_text(
                f"{self.source} · source {result.source_completeness.value} · "
                f"{result.visited_nodes} nodes / {result.examined_edges} edges · "
                f"truncated: {result.truncated}. {finding} {limitations}"
            )
        )
        self.query_one("#dependency-detail", Button).disabled = False
        self.query_one("#dependency-export", Button).disabled = False
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if self.result is not None:
            self.workers.cancel_group(self, "dependencies")
            self.dismiss(str(event.row_key.value))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "dependency-apply":
            try:
                self.request = self._request_from_controls()
            except (TypeError, ValueError) as exc:
                self.on_status(errno.EINVAL)
                self.query_one("#dependency-status", Static).update(
                    plain_text(f"Invalid request: {exc}")
                )
                return
            self.controls_changed = False
            self.load_result()
        elif event.button.id == "dependency-detail" and self.result is not None:
            self.app.push_screen(
                RowDetails(self.result.model_dump_json(indent=2), "Static dependency evidence")
            )
        elif event.button.id == "dependency-export":
            from code_analysis.tui_export import ExportPage

            if self.controls_changed or self.result is None:
                self.query_one("#dependency-status", Static).update(
                    "Press Apply to refresh dependency evidence before exporting."
                )
                return
            index = self.reader
            if not isinstance(index, (BrowseIndex, RuntimeIndex)):
                self.query_one("#dependency-status", Static).update(
                    "Export requires a browse or runtime index reader."
                )
                return
            self.app.push_screen(
                ExportPage(
                    index,
                    DependencyGraphRequest(investigation=self.request),
                    self.on_status,
                    self.viewer,
                )
            )
        elif event.button.id == "dependency-close":
            self.action_close()

    def action_close(self) -> None:
        self.workers.cancel_group(self, "dependencies")
        self.dismiss(None)
