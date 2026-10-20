"""Bounded, source-free terminal browsing for prepared static structure graphs."""

from __future__ import annotations

import asyncio
import errno
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Select, SelectionList, Static

from code_analysis.artifact_viewer import ViewerCommand
from code_analysis.browse_index import BrowseIndex
from code_analysis.models import SourceLanguage
from code_analysis.projection_models import (
    GraphEdge,
    GraphNode,
    GraphProjection,
    ProjectionCollapse,
)
from code_analysis.projections import ProjectionName
from code_analysis.static_graph_browse import (
    GRAPH_PROJECTIONS,
    StaticGraphReadLimitError,
    StaticGraphRequest,
    StaticGraphSelectionError,
    StaticGraphSummary,
)
from code_analysis.tui_app import RowDetails, plain_text
from code_analysis.tui_graph_navigation import GraphNodeDetails, StaticGraphNavigation


@dataclass(frozen=True, slots=True)
class GraphDisplayOptions:
    languages: tuple[SourceLanguage, ...]
    collapse: ProjectionCollapse


class StaticGraphOptions(ModalScreen[GraphDisplayOptions | None]):
    """Edit display choices; interpreting ownership stays in the backend."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel", "Cancel")]
    CSS = """
    StaticGraphOptions { align: center middle; }
    #graph-language-dialog { width: 62; height: auto; border: round $accent;
                             background: $surface; padding: 1; }
    #graph-language-help { height: 3; }
    #graph-language-choices { height: 5; }
    #graph-language-actions { height: 3; }
    """

    def __init__(self, options: GraphDisplayOptions) -> None:
        super().__init__()
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="graph-language-dialog"):
            yield Static(
                "Languages · Space toggles · none selected means all.\n"
                "Related subjects in other languages may remain in a flow.",
                id="graph-language-help",
            )
            yield SelectionList(
                *(
                    (item.value.title(), item.value, item in self.options.languages)
                    for item in SourceLanguage
                ),
                id="graph-language-choices",
            )
            yield Static("Group by recorded owner (collapse)")
            yield Select(
                tuple((item.value.title(), item.value) for item in ProjectionCollapse),
                value=self.options.collapse.value,
                allow_blank=False,
                id="graph-collapse-mode",
            )
            with Horizontal(id="graph-language-actions"):
                yield Button("Use selection", id="graph-language-use")
                yield Button("Cancel", id="graph-language-cancel")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "graph-language-use":
            values = self.query_one("#graph-language-choices", SelectionList).selected
            self.dismiss(
                GraphDisplayOptions(
                    tuple(
                        sorted(
                            (SourceLanguage(value) for value in values), key=lambda item: item.value
                        )
                    ),
                    ProjectionCollapse(str(self.query_one("#graph-collapse-mode", Select).value)),
                )
            )
        elif event.button.id == "graph-language-cancel":
            self.action_cancel()


class StaticGraphBrowser(ModalScreen[int]):
    """Inspect nodes and edges retained from one prepared static graph."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+e", "export", "Export graph"),
        ("ctrl+l", "graph_options", "Graph filters"),
        ("ctrl+w", "web_handoff", "Open graph in Web UI"),
        ("f2", "status_details", "Graph status"),
    ]
    CSS = """
    StaticGraphBrowser { align: center middle; }
    #static-graph-dialog { width: 96%; height: 94%; border: round $accent;
                           background: $surface; padding: 1; }
    #static-graph-controls, #static-graph-bounds, #static-graph-actions { height: 3; }
    #static-graph-name { width: 31; }
    #static-graph-selected { width: 1fr; }
    .static-graph-bound-label { width: auto; padding: 0 1; content-align: center middle; }
    #static-graph-records, #static-graph-nodes, #static-graph-edges { width: 9; }
    #static-graph-status { height: auto; max-height: 2; }
    #static-graph-table { height: 1fr; }
    """

    def __init__(
        self,
        index: BrowseIndex,
        on_status: Callable[[int], None],
        viewer: ViewerCommand | None = None,
        *,
        source_root: Path | None = None,
        initial_name: ProjectionName | None = None,
    ) -> None:
        super().__init__()
        self.index = index
        self.on_status = on_status
        self.viewer = viewer
        self.source_root = source_root
        names = tuple(summary.name for summary in index.manifest.static_graphs)
        if names != GRAPH_PROJECTIONS:
            raise ValueError("browse index does not contain the canonical static graph summaries")
        if initial_name is not None and initial_name not in names:
            raise ValueError("requested static graph is not prepared in this index")
        self.request = StaticGraphRequest(name=initial_name or names[0])
        self.languages: tuple[SourceLanguage, ...] = ()
        self.collapse = ProjectionCollapse.NONE
        self.graph: GraphProjection | None = None
        self.controls_ready = False
        self.controls_changed = False
        self.revision = 0
        self.error_status = 0
        self._rows: tuple[GraphNode | GraphEdge, ...] = ()

    def compose(self) -> ComposeResult:
        choices = tuple(
            (summary.title, summary.name.value) for summary in self.index.manifest.static_graphs
        )
        with Vertical(id="static-graph-dialog"):
            yield Static(
                "Languages: all · Collapse: none · Enter: details · Ctrl+W: Web",
                id="static-graph-help",
            )
            with Horizontal(id="static-graph-controls"):
                yield Select(
                    choices,
                    value=self.request.name.value,
                    allow_blank=False,
                    id="static-graph-name",
                )
                yield Input(
                    placeholder="Exact node IDs, comma separated (up to 25)",
                    id="static-graph-selected",
                    max_length=4096,
                )
            with Horizontal(id="static-graph-bounds"):
                yield Static("Records", classes="static-graph-bound-label")
                yield Input("500", type="integer", id="static-graph-records")
                yield Static("Nodes", classes="static-graph-bound-label")
                yield Input("60", type="integer", id="static-graph-nodes")
                yield Static("Edges", classes="static-graph-bound-label")
                yield Input("120", type="integer", id="static-graph-edges")
                yield Button("Apply", id="static-graph-apply")
            yield Static("Loading…", id="static-graph-status")
            yield DataTable(id="static-graph-table", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="static-graph-actions"):
                yield Button("Graph filters", id="static-graph-languages")
                yield Button("Status / limits", id="static-graph-details", disabled=True)
                yield Button("Export graph", id="static-graph-export", disabled=True)
                yield Button("Close", id="static-graph-close")

    def on_mount(self) -> None:
        self.controls_ready = True
        self.load_graph()

    def _request_from_controls(self) -> StaticGraphRequest:
        selected_text = self.query_one("#static-graph-selected", Input).value
        selected_ids = tuple(
            sorted({value.strip() for value in selected_text.split(",") if value.strip()})
        )
        return StaticGraphRequest(
            name=ProjectionName(str(self.query_one("#static-graph-name", Select).value)),
            selected_ids=selected_ids,
            languages=self.languages,
            collapse=self.collapse,
            max_records=int(self.query_one("#static-graph-records", Input).value),
            max_nodes=int(self.query_one("#static-graph-nodes", Input).value),
            max_edges=int(self.query_one("#static-graph-edges", Input).value),
        )

    def _controls_match_request(self) -> bool:
        try:
            return self._request_from_controls() == self.request
        except (TypeError, ValueError):
            return False

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.controls_ready and event.input.id in {
            "static-graph-selected",
            "static-graph-records",
            "static-graph-nodes",
            "static-graph-edges",
        }:
            self.invalidate_graph()

    def on_select_changed(self, event: Select.Changed) -> None:
        if self.controls_ready and event.select.id == "static-graph-name":
            self.invalidate_graph()

    def invalidate_graph(self) -> None:
        """Invalidate every action as soon as controls stop describing loaded evidence."""
        if self._controls_match_request():
            return
        self.revision += 1
        self.controls_changed = True
        self.graph = None
        self._rows = ()
        self.workers.cancel_group(self, "static-graph")
        if self.is_mounted:
            self.query_one("#static-graph-table", DataTable).clear(columns=True)
            self.query_one("#static-graph-details", Button).disabled = True
            self.query_one("#static-graph-export", Button).disabled = True
            self.query_one("#static-graph-status", Static).update(
                "Controls changed; press Apply to load exact bounded graph evidence."
            )

    def apply(self) -> None:
        try:
            self.request = self._request_from_controls()
        except (TypeError, ValueError) as exc:
            self.invalidate_graph()
            self.error_status = errno.EINVAL
            self.on_status(self.error_status)
            self.query_one("#static-graph-status", Static).update(
                plain_text(f"Invalid static graph request: {exc}")
            )
            return
        self.controls_changed = False
        self.load_graph()

    def _summary(self) -> StaticGraphSummary:
        return next(
            item for item in self.index.manifest.static_graphs if item.name is self.request.name
        )

    def _source_completeness(self) -> str:
        return self.index.manifest.overview.reports[0].completeness.value

    @work(exclusive=True, group="static-graph")
    async def load_graph(self) -> None:
        self.revision += 1
        revision, request = self.revision, self.request
        self.graph = None
        self._rows = ()
        table = self.query_one("#static-graph-table", DataTable)
        table.clear(columns=True)
        self.query_one("#static-graph-details", Button).disabled = True
        self.query_one("#static-graph-export", Button).disabled = True
        summary = self._summary()
        if not summary.available:
            if self.is_mounted and revision == self.revision and request == self.request:
                self.error_status = errno.ENODATA
                self.on_status(self.error_status)
                self.query_one("#static-graph-status", Static).update(
                    plain_text(
                        f"{summary.title} is unavailable in this prepared index: "
                        f"{summary.unavailable_reason or 'no reason recorded'}"
                    )
                )
            return
        self.query_one("#static-graph-status", Static).update(
            "Loading bounded prepared graph records…"
        )
        try:
            graph = await asyncio.to_thread(self.index.static_graph, request)
        except OSError as exc:
            status = exc.errno or errno.EIO
            if self.is_mounted and revision == self.revision and request == self.request:
                self.error_status = status
                self.on_status(status)
                self.query_one("#static-graph-status", Static).update(
                    plain_text(f"Static graph index unavailable: {exc}")
                )
            return
        except StaticGraphSelectionError as exc:
            if self.is_mounted and revision == self.revision and request == self.request:
                self.error_status = errno.EINVAL
                self.on_status(self.error_status)
                self.query_one("#static-graph-status", Static).update(
                    plain_text(f"Invalid static graph selection: {exc}")
                )
            return
        except (ValueError, sqlite3.Error) as exc:
            if self.is_mounted and revision == self.revision and request == self.request:
                self.error_status = (
                    errno.ENODATA if isinstance(exc, StaticGraphReadLimitError) else errno.EBADMSG
                )
                self.on_status(self.error_status)
                self.query_one("#static-graph-status", Static).update(
                    plain_text(f"Static graph unavailable: {exc}")
                )
            return
        if (
            not self.is_mounted
            or revision != self.revision
            or request != self.request
            or self.controls_changed
        ):
            return
        self.graph = graph
        self._rows = (*graph.nodes, *graph.edges)
        table.add_columns("Record", "Identity", "From", "To", "Kind", "Label")
        for number, node in enumerate(graph.nodes):
            table.add_row(
                "node",
                plain_text(node.node_id),
                "",
                "",
                plain_text(node.kind),
                plain_text(node.label),
                key=str(number),
            )
        offset = len(graph.nodes)
        for number, edge in enumerate(graph.edges, start=offset):
            table.add_row(
                "edge",
                plain_text(edge.edge_id),
                plain_text(edge.source_id),
                plain_text(edge.target_id),
                plain_text(edge.kind),
                plain_text(edge.label or ""),
                key=str(number),
            )
        self.error_status = (
            errno.ENODATA if graph.completeness.value != "complete" or graph.truncated else 0
        )
        self.on_status(self.error_status)
        self.query_one("#static-graph-status", Static).update(
            plain_text(
                f"Source {self._source_completeness()} · prep {summary.completeness.value} "
                f"{summary.node_count}/{summary.original_node_count}N "
                f"{summary.edge_count}/{summary.original_edge_count}E · display "
                f"{graph.completeness.value} {len(graph.nodes)}/{graph.original_node_count}N "
                f"{len(graph.edges)}/{graph.original_edge_count}E\n"
                "F2: bounds, truncation, source identity, and limitations."
            )
        )
        self.query_one("#static-graph-details", Button).disabled = False
        self.query_one("#static-graph-export", Button).disabled = False
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if self.graph is None or self.controls_changed or not self._controls_match_request():
            return
        try:
            row_key = event.row_key.value
            if row_key is None:
                return
            record = self._rows[int(row_key)]
        except (IndexError, TypeError, ValueError):
            return
        if isinstance(record, GraphNode):
            if self.request.collapse is not ProjectionCollapse.NONE and record.node_id.startswith(
                f"collapsed-{self.request.collapse.value}:"
            ):
                self.app.push_screen(
                    RowDetails(
                        json.dumps(
                            {"node": record.model_dump(mode="json")}, indent=2, ensure_ascii=False
                        ),
                        "Grouped view node, not a recorded source span. Set collapse to None "
                        "to navigate individual recorded nodes.",
                    )
                )
                return
            self.app.push_screen(
                GraphNodeDetails(
                    json.dumps(
                        {"node": record.model_dump(mode="json")}, indent=2, ensure_ascii=False
                    ),
                    open_navigation=lambda: self.open_navigation(record.node_id),
                )
            )
            return
        self.app.push_screen(
            RowDetails(
                json.dumps({"edge": record.model_dump(mode="json")}, indent=2, ensure_ascii=False),
                "Recorded static graph edge only; no source span is inferred from an edge.",
            )
        )

    def open_navigation(self, identity: str) -> None:
        """Open one exact retained node's saved-span page without changing this graph."""
        if self.graph is None or self.controls_changed or not self._controls_match_request():
            return
        self.app.push_screen(
            StaticGraphNavigation(
                self.index,
                self.request,
                identity,
                self.on_status,
                self.source_root,
            )
        )

    def action_status_details(self) -> None:
        if self.graph is None or self.controls_changed or not self._controls_match_request():
            return
        summary = self._summary()
        self.app.push_screen(
            RowDetails(
                json.dumps(
                    {
                        "source": summary.source.model_dump(mode="json"),
                        "source_completeness": self._source_completeness(),
                        "prepared_graph_completeness": summary.completeness.value,
                        "preparation": summary.preparation.model_dump(mode="json"),
                        "preparation_truncated": summary.truncated,
                        "preparation_truncation_reason": summary.truncation_reason,
                        "prepared_counts": {
                            "nodes": summary.node_count,
                            "edges": summary.edge_count,
                            "original_nodes": summary.original_node_count,
                            "original_edges": summary.original_edge_count,
                        },
                        "display_request": self.request.model_dump(mode="json"),
                        "display_completeness": self.graph.completeness.value,
                        "display_truncated": self.graph.truncated,
                        "display_counts": {
                            "nodes": len(self.graph.nodes),
                            "edges": len(self.graph.edges),
                            "omitted_nodes": self.graph.omitted_node_count,
                            "omitted_edges": self.graph.omitted_edge_count,
                        },
                        "limitations": self.graph.limitations,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                "Static graph source, preparation, display bounds, and limitations",
            )
        )

    def action_export(self) -> None:
        if self.graph is None or self.controls_changed or not self._controls_match_request():
            return
        from code_analysis.tui_export import ExportPage

        self.app.push_screen(ExportPage(self.index, self.request, self.on_status, self.viewer))

    def action_web_handoff(self) -> None:
        if self.graph is None or self.controls_changed or not self._controls_match_request():
            return
        from code_analysis.tui_app import AnalysisTerminal

        if isinstance(self.app, AnalysisTerminal):
            self.app.open_web_handoff("projection", graph_request=self.request)

    def action_graph_options(self) -> None:
        self.app.push_screen(
            StaticGraphOptions(GraphDisplayOptions(self.languages, self.collapse)),
            self._options_selected,
        )

    def _options_selected(self, options: GraphDisplayOptions | None) -> None:
        if options is None or not self.is_mounted:
            return
        self.languages = options.languages
        self.collapse = options.collapse
        label = ", ".join(item.value for item in self.languages) or "all"
        self.query_one("#static-graph-help", Static).update(
            f"Languages: {label} · Collapse: {self.collapse.value} · Enter: details · Ctrl+W: Web"
        )
        self.invalidate_graph()

    def action_close(self) -> None:
        self.revision += 1
        self.workers.cancel_group(self, "static-graph")
        self.dismiss(self.error_status)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        actions = {
            "static-graph-languages": self.action_graph_options,
            "static-graph-apply": self.apply,
            "static-graph-details": self.action_status_details,
            "static-graph-export": self.action_export,
            "static-graph-close": self.action_close,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()
