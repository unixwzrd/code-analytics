"""Indexed runtime exploration without reading profile JSON or executing workloads."""

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
from textual.containers import HorizontalScroll, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Select, Static

from code_analysis.artifact_viewer import ViewerCommand
from code_analysis.browse_index import BrowseIndex
from code_analysis.models import Completeness
from code_analysis.profile_models import ProfileFrame, ProfileStatistic
from code_analysis.runtime_browse_models import RuntimeBrowsePage, RuntimeBrowseRequest
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_index import RuntimeIndex
from code_analysis.runtime_queries import RuntimeQuerySection
from code_analysis.runtime_source_browse import RuntimeSourceUnavailable
from code_analysis.tui_app import RowDetails, plain_text


class RuntimeBrowser(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+n", "next", "Next"),
        ("ctrl+p", "previous", "Previous"),
        ("i", "inspect", "Inspect record"),
        ("ctrl+e", "export", "Export page"),
        ("s", "source", "Preview source"),
        ("m", "compare", "Compare profiles"),
    ]
    CSS = """
    RuntimeBrowser { align: center middle; }
    #runtime-dialog { width: 95%; height: 95%; border: round $accent;
                      background: $surface; padding: 1; }
    #runtime-status { height: auto; max-height: 7; }
    #runtime-buttons { height: 3; }
    """

    def __init__(
        self,
        index: BrowseIndex | RuntimeIndex,
        on_status: Callable[[int], None],
        viewer: ViewerCommand | None = None,
        source_root: Path | None = None,
        web_action: Callable[[RuntimeGraphRequest], None] | None = None,
    ) -> None:
        super().__init__()
        self.index = index
        self.on_status = on_status
        self.viewer = viewer
        self.source_root = source_root
        self.web_action = web_action
        self.request = RuntimeBrowseRequest(section=RuntimeQuerySection.HOTSPOTS)
        self.page: RuntimeBrowsePage | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="runtime-dialog"):
            yield Static("Saved Python runtime evidence — no execution or optimization claim")
            yield Select(
                [(item.value.title(), item.value) for item in RuntimeQuerySection],
                value=self.request.section.value,
                allow_blank=False,
                id="runtime-section",
            )
            yield Static("Loading…", id="runtime-status")
            yield DataTable(id="runtime-records", cursor_type="row")
            with HorizontalScroll(id="runtime-buttons"):
                yield Button("Previous", id="runtime-previous", disabled=True)
                yield Button("Next", id="runtime-next", disabled=True)
                yield Button("All frames", id="runtime-clear")
                yield Button("Inspect", id="runtime-inspect")
                yield Button("Health", id="runtime-health")
                yield Button("Compare", id="runtime-compare")
                yield Button("Graph", id="runtime-graph")
                yield Button("Close", id="runtime-close")

    def on_mount(self) -> None:
        self.load_page()

    @work(exclusive=True, group="runtime-pages")
    async def load_page(self) -> None:
        request = self.request
        self.page = None
        self.query_one("#runtime-records", DataTable).clear(columns=True)
        self.query_one("#runtime-next", Button).disabled = True
        self.query_one("#runtime-previous", Button).disabled = True
        summary = self.index.manifest.runtime
        if summary is None:
            self.on_status(errno.ENODATA)
            self.query_one("#runtime-status", Static).update(
                "Runtime unavailable. Prepare a new index with --profile-report PROFILE.json."
            )
            return
        try:
            page = await asyncio.to_thread(self.index.runtime_page, request)
        except (ValueError, OSError, sqlite3.Error) as exc:
            if self.is_mounted and request == self.request:
                self.on_status(errno.EBADMSG)
                self.query_one("#runtime-status", Static).update(
                    plain_text(f"Runtime index unavailable: {exc}")
                )
            return
        if not self.is_mounted or request != self.request:
            return
        self.page = page
        if (
            summary.overview.completeness is not Completeness.COMPLETE
            or not summary.overview.source_matches
        ):
            self.on_status(errno.ENODATA)
        self.render_page()

    def render_page(self) -> None:
        if self.page is None:
            return
        projection = self.page.panel.projection
        labels = {item.frame_id: item.label for item in self.page.panel.labels}
        table = self.query_one("#runtime-records", DataTable)
        if projection.records:
            fields = tuple(type(projection.records[0]).model_fields)
            table.add_columns(*(name.replace("_", " ").title() for name in fields))
            for position, record in enumerate(projection.records):
                values = record.model_dump(mode="json")
                cells = []
                for name in fields:
                    value = values[name]
                    if name in {"frame_id", "caller_frame_id", "callee_frame_id"}:
                        value = labels.get(value, value)
                    cells.append(plain_text("unavailable" if value is None else str(value)))
                table.add_row(*cells, key=str(position))
        summary = self.index.manifest.runtime
        self.query_one("#runtime-status", Static).update(
            plain_text(
                f"Profile {summary.profile_report_sha256}; {projection.source_completeness.value}; "
                f"static report match: {summary.overview.source_matches}. "
                f"Rows {self.page.offset + 1 if projection.records else 0}"
                f"-{self.page.offset + projection.retained_count} of {projection.eligible_count}. "
                f"Frame: {projection.frame_id or 'all'}. "
                "Enter a hotspot/variance row for calls; "
                "I inspects, S previews source, M compares profiles, Ctrl+E exports. "
                "Other page records remain accessible; Health retains provider limitations."
            )
        )
        self.query_one("#runtime-next", Button).disabled = self.page.next_offset is None
        self.query_one("#runtime-previous", Button).disabled = self.page.offset == 0
        table.focus()

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        section = RuntimeQuerySection(str(event.value))
        if section == self.request.section:
            return
        frame = (
            self.request.frame_id
            if section
            in {
                RuntimeQuerySection.HOTSPOTS,
                RuntimeQuerySection.CALLS,
                RuntimeQuerySection.VARIANCE,
            }
            else None
        )
        self.request = RuntimeBrowseRequest(section=section, frame_id=frame)
        self.load_page()

    def action_next(self) -> None:
        if self.page is not None and self.page.next_offset is not None:
            self.request = self.request.model_copy(update={"offset": self.page.next_offset})
            self.load_page()

    def action_previous(self) -> None:
        if self.page is not None and self.page.offset:
            self.request = self.request.model_copy(
                update={"offset": max(0, self.page.offset - self.request.limit)}
            )
            self.load_page()

    def action_inspect(self) -> None:
        if self.page is None or not self.page.panel.projection.records:
            return
        position = self.query_one("#runtime-records", DataTable).cursor_row
        record = self.page.panel.projection.records[position]
        self.app.push_screen(
            RowDetails(
                record.model_dump_json(indent=2), "Saved runtime record — workload-scoped evidence"
            )
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if self.page is None:
            return
        record = self.page.panel.projection.records[int(event.row_key.value)]
        if isinstance(record, (ProfileFrame, ProfileStatistic)):
            self.request = RuntimeBrowseRequest(
                section=RuntimeQuerySection.CALLS, frame_id=record.frame_id
            )
            self.query_one("#runtime-section", Select).value = RuntimeQuerySection.CALLS.value
            self.load_page()
        else:
            self.action_inspect()

    def graph_closed(self, frame: str | None) -> None:
        if frame is not None:
            self.request = RuntimeBrowseRequest(section=RuntimeQuerySection.CALLS, frame_id=frame)
            self.query_one("#runtime-section", Select).value = RuntimeQuerySection.CALLS.value
            self.load_page()

    @work(exclusive=True, group="runtime-source")
    async def action_source(self) -> None:
        if self.page is None:
            return
        if self.source_root is None:
            self.on_status(errno.ENODATA)
            self.app.push_screen(
                RowDetails(
                    "Source preview disabled; launch tui with --source-root.",
                    "Runtime source unavailable",
                )
            )
            return
        records = self.page.panel.projection.records
        position = self.query_one("#runtime-records", DataTable).cursor_row
        record = records[position] if 0 <= position < len(records) else None
        frame = (
            record.frame_id
            if isinstance(record, (ProfileFrame, ProfileStatistic))
            else self.request.frame_id
        )
        if frame is None:
            self.on_status(errno.ENODATA)
            self.app.push_screen(
                RowDetails(
                    "Select a hotspot, variance record, or focused call frame first.",
                    "Runtime source unavailable",
                )
            )
            return
        request = self.request
        try:
            preview = await asyncio.to_thread(self.index.runtime_source, frame)
        except (ValueError, OSError, sqlite3.Error) as exc:
            if self.is_mounted and self.request == request:
                self.on_status(
                    errno.ENODATA if isinstance(exc, RuntimeSourceUnavailable) else errno.EBADMSG
                )
                self.app.push_screen(RowDetails(str(exc), "Runtime source unavailable"))
            return
        if not self.is_mounted or self.request != request:
            return
        from code_analysis.tui_source import TerminalSource

        assert isinstance(self.index, BrowseIndex)
        self.app.push_screen(
            TerminalSource(
                self.index,
                self.source_root,
                preview.source.logical_path,
                preview.start,
                preview.end,
                self.on_status,
                source_identity=preview.source,
            )
        )

    def action_export(self) -> None:
        if self.page is not None:
            from code_analysis.tui_export import ExportPage

            self.app.push_screen(ExportPage(self.index, self.request, self.on_status, self.viewer))

    def action_compare(self) -> None:
        from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser

        self.app.push_screen(
            RuntimeComparisonBrowser(self.index, self.on_status, self.viewer, self.source_root)
        )

    def action_close(self) -> None:
        self.workers.cancel_group(self, "runtime-pages")
        self.workers.cancel_group(self, "runtime-source")
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "runtime-next":
            self.action_next()
        elif event.button.id == "runtime-previous":
            self.action_previous()
        elif event.button.id == "runtime-inspect":
            self.action_inspect()
        elif event.button.id == "runtime-clear":
            self.request = RuntimeBrowseRequest(section=self.request.section)
            self.load_page()
        elif event.button.id == "runtime-graph":
            from code_analysis.tui_runtime_graph import RuntimeGraphBrowser

            self.app.push_screen(
                RuntimeGraphBrowser(
                    self.index, self.on_status, self.viewer, web_action=self.web_action
                ),
                self.graph_closed,
            )
        elif event.button.id == "runtime-compare":
            self.action_compare()
        elif event.button.id == "runtime-health":
            summary = self.index.manifest.runtime
            self.app.push_screen(
                RowDetails(
                    (
                        summary.model_dump_json(indent=2)
                        if summary
                        else json.dumps({"available": False})
                    ),
                    "Runtime identity, source binding, providers, diagnostics and limitations",
                )
            )
        else:
            self.action_close()
