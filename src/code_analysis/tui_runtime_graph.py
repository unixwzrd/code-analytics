"""Indexed observed-call graph navigation and explicit artifact export."""

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
from textual.widgets import Button, Checkbox, DataTable, Input, Static

from code_analysis.artifact_viewer import ViewerCommand
from code_analysis.browse_index import BrowseIndex
from code_analysis.models import Completeness
from code_analysis.projection_models import GraphProjection
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_index import RuntimeIndex
from code_analysis.tui_app import RowDetails, plain_text


class RuntimeGraphBrowser(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+e", "export", "Export graph"),
        ("ctrl+w", "web_handoff", "Open graph in Web UI"),
    ]
    CSS = """
    RuntimeGraphBrowser { align: center middle; }
    #runtime-graph-dialog { width: 95%; height: 95%; border: round $accent;
                           background: $surface; padding: 1; }
    #runtime-graph-status { height: auto; max-height: 6; }
    #runtime-graph-controls, #runtime-graph-buttons { height: 3; }
    #runtime-graph-controls Input { width: 12; }
    """

    def __init__(
        self,
        index: BrowseIndex | RuntimeIndex,
        on_status: Callable[[int], None],
        viewer: ViewerCommand | None = None,
        web_action: Callable[[RuntimeGraphRequest], None] | None = None,
    ) -> None:
        super().__init__()
        self.index, self.on_status, self.viewer = index, on_status, viewer
        self.web_action = web_action
        self.request = RuntimeGraphRequest()
        self.graph: GraphProjection | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="runtime-graph-dialog"):
            yield Static(
                "Observed call graph: Enter a node for calls; export SVG/PNG to view layout."
            )
            with Horizontal(id="runtime-graph-controls"):
                yield Input("60", placeholder="Max nodes", id="runtime-graph-nodes", type="integer")
                yield Input(
                    "120", placeholder="Max edges", id="runtime-graph-edges", type="integer"
                )
                yield Checkbox("Project frames only", id="runtime-graph-project")
                yield Button("Apply bounds", id="runtime-graph-apply")
            yield Static("Loading…", id="runtime-graph-status")
            yield DataTable(id="runtime-graph-table", cursor_type="row")
            with Horizontal(id="runtime-graph-buttons"):
                yield Button("Graph evidence", id="runtime-graph-detail")
                yield Button("Export / view", id="runtime-graph-export")
                yield Button("Close", id="runtime-graph-close")

    def on_mount(self) -> None:
        self.load_graph()

    @work(exclusive=True, group="runtime-graph")
    async def load_graph(self) -> None:
        request = self.request
        self.graph = None
        table = self.query_one("#runtime-graph-table", DataTable)
        table.clear(columns=True)
        if self.index.manifest.runtime is None:
            self.on_status(errno.ENODATA)
            self.query_one("#runtime-graph-status", Static).update("Runtime graph unavailable.")
            return
        try:
            graph = await asyncio.to_thread(self.index.runtime_graph, request)
        except (ValueError, OSError, sqlite3.Error) as exc:
            if self.is_mounted and self.request == request:
                self.on_status(errno.EBADMSG)
                self.query_one("#runtime-graph-status", Static).update(plain_text(str(exc)))
            return
        if not self.is_mounted or self.request != request:
            return
        self.graph = graph
        matches = self.index.manifest.runtime.overview.source_matches
        if graph.completeness is not Completeness.COMPLETE or not matches:
            self.on_status(errno.ENODATA)
        table.add_columns("Frame", "Classification", "Retained callers", "Retained callees")
        for node in graph.nodes:
            table.add_row(
                plain_text(node.label),
                plain_text(node.kind),
                str(sum(edge.target_id == node.node_id for edge in graph.edges)),
                str(sum(edge.source_id == node.node_id for edge in graph.edges)),
                key=node.node_id,
            )
        self.query_one("#runtime-graph-status", Static).update(
            plain_text(
                f"{len(graph.nodes)}/{graph.original_node_count} nodes; "
                f"{len(graph.edges)}/{graph.original_edge_count} edges; "
                f"{graph.completeness.value}; "
                f"static binding: {matches}. Counts describe selected edges, not full fan-in/out. "
                "Observed workload only; absent edges do not prove a call impossible."
            )
        )
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if self.graph is not None:
            self.workers.cancel_group(self, "runtime-graph")
            self.dismiss(str(event.row_key.value))

    def _controls_match_request(self) -> bool:
        return (
            self.query_one("#runtime-graph-nodes", Input).value == str(self.request.max_nodes)
            and self.query_one("#runtime-graph-edges", Input).value == str(self.request.max_edges)
            and self.query_one("#runtime-graph-project", Checkbox).value
            == self.request.project_only
        )

    def action_export(self) -> None:
        if self.graph is not None:
            from code_analysis.tui_export import ExportPage

            self.app.push_screen(ExportPage(self.index, self.request, self.on_status, self.viewer))

    def action_web_handoff(self) -> None:
        """Hand off only the applied request; staged control edits are not what is shown."""
        if self.graph is None or self.web_action is None or not self._controls_match_request():
            return
        self.web_action(self.request)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "runtime-graph-apply":
            try:
                self.request = RuntimeGraphRequest(
                    max_nodes=int(self.query_one("#runtime-graph-nodes", Input).value),
                    max_edges=int(self.query_one("#runtime-graph-edges", Input).value),
                    project_only=self.query_one("#runtime-graph-project", Checkbox).value,
                )
            except ValueError:
                self.on_status(errno.EINVAL)
                self.query_one("#runtime-graph-status", Static).update(
                    "Use 1-100 nodes and 1-250 edges."
                )
                return
            self.load_graph()
        elif event.button.id == "runtime-graph-export":
            self.action_export()
        elif event.button.id == "runtime-graph-detail" and self.graph is not None:
            self.app.push_screen(
                RowDetails(self.graph.model_dump_json(indent=2), "Observed graph evidence")
            )
        elif event.button.id == "runtime-graph-close":
            self.action_close()

    def action_close(self) -> None:
        self.workers.cancel_group(self, "runtime-graph")
        self.dismiss(None)
