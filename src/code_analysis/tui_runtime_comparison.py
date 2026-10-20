"""Paged diagnostic-only runtime comparison from a validated index."""

from __future__ import annotations

import asyncio
import errno
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Literal

from textual import work
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from code_analysis.artifact_viewer import ViewerCommand
from code_analysis.browse_index import BrowseIndex
from code_analysis.models import Completeness
from code_analysis.runtime_comparison_browse import RuntimeComparisonPage, RuntimeComparisonRequest
from code_analysis.runtime_index import RuntimeIndex
from code_analysis.runtime_source_browse import RuntimeSourceUnavailable
from code_analysis.tui_app import RowDetails, plain_text

ComparisonSourceSide = Literal["baseline", "candidate"]


class RuntimeComparisonBrowser(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+n", "next", "Next"),
        ("ctrl+p", "previous", "Previous"),
        ("ctrl+e", "export", "Export page"),
        ("b", "baseline_source", "Baseline source"),
        ("c", "candidate_source", "Candidate source"),
    ]
    CSS = """
    RuntimeComparisonBrowser { align: center middle; }
    #runtime-comparison-dialog { width: 95%; height: 95%; border: round $accent;
                                background: $surface; padding: 1; }
    #runtime-comparison-status { height: auto; max-height: 8; }
    #runtime-comparison-buttons { height: 3; }
    """

    def __init__(
        self,
        index: BrowseIndex | RuntimeIndex,
        on_status: Callable[[int], None],
        viewer: ViewerCommand | None = None,
        source_root: Path | None = None,
    ) -> None:
        super().__init__()
        self.index = index
        self.on_status = on_status
        self.viewer = viewer
        self.source_root = source_root
        self.request = RuntimeComparisonRequest()
        self.page: RuntimeComparisonPage | None = None
        self._source_token: tuple[RuntimeComparisonRequest, int, ComparisonSourceSide] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="runtime-comparison-dialog"):
            yield Static("Loading…", id="runtime-comparison-status")
            yield DataTable(id="runtime-comparison-records", cursor_type="row")
            with Horizontal(id="runtime-comparison-buttons"):
                yield Button("Previous", id="runtime-comparison-previous", disabled=True)
                yield Button("Next", id="runtime-comparison-next", disabled=True)
                yield Button("Baseline source", id="runtime-comparison-baseline-source")
                yield Button("Candidate source", id="runtime-comparison-candidate-source")
                yield Button("Compatibility / health", id="runtime-comparison-health")
                yield Button("Close", id="runtime-comparison-close")

    def on_mount(self) -> None:
        self.call_after_refresh(self.load_page)

    @work(exclusive=True, group="runtime-comparison-pages")
    async def load_page(self) -> None:
        request = self.request
        self._source_token = None
        self.page = None
        table = self.query_one("#runtime-comparison-records", DataTable)
        table.clear(columns=True)
        for direction in ("next", "previous"):
            self.query_one(f"#runtime-comparison-{direction}", Button).disabled = True
        runtime = self.index.manifest.runtime
        if runtime is None or runtime.comparison is None:
            self.on_status(errno.ENODATA)
            self.query_one("#runtime-comparison-status", Static).update(
                "Comparison unavailable; prepare an index with "
                "--profile-report and --compare-profile."
            )
            return
        summary = runtime.comparison
        try:
            page = await asyncio.to_thread(self.index.runtime_comparison, request)
        except (ValueError, OSError, sqlite3.Error) as exc:
            if self.is_mounted and self.request == request:
                self.on_status(errno.EBADMSG)
                self.query_one("#runtime-comparison-status", Static).update(plain_text(str(exc)))
            return
        if not self.is_mounted or self.request != request:
            return
        self.page = page
        if (
            runtime.overview.completeness is not Completeness.COMPLETE
            or not runtime.overview.source_matches
            or summary.candidate_overview.completeness is not Completeness.COMPLETE
            or not summary.candidate_overview.source_matches
            or any(item.outcome != "matched" for item in page.report.decisions)
        ):
            self.on_status(errno.ENODATA)
        table.add_columns(
            "Frame", "Baseline total s", "Candidate total s", "Delta s", "Relative delta"
        )
        for number, row in enumerate(page.report.records):
            table.add_row(
                *(
                    plain_text("unavailable" if value is None else str(value))
                    for value in (
                        row.frame_identity,
                        row.baseline_total_seconds,
                        row.candidate_total_seconds,
                        row.absolute_total_seconds_delta,
                        row.relative_total_seconds_delta,
                    )
                ),
                key=str(number),
            )
        self.query_one("#runtime-comparison-status", Static).update(
            plain_text(
                "Diagnostic only — no performance claim. "
                f"Baseline {runtime.profile_report_sha256}; "
                f"candidate {summary.candidate_report_sha256}. "
                f"Rows {request.offset + 1 if page.report.records else 0}"
                f"-{request.offset + len(page.report.records)} of {page.eligible}. "
                "Enter inspects all total/cumulative deltas. "
                "B/C preview baseline or candidate source for the selected row. "
                "Health retains compatibility and binding decisions."
            )
        )
        self.query_one("#runtime-comparison-next", Button).disabled = page.next_offset is None
        self.query_one("#runtime-comparison-previous", Button).disabled = request.offset == 0
        table.focus()

    def action_next(self) -> None:
        if self.page is not None and self.page.next_offset is not None:
            self.request = self.request.model_copy(update={"offset": self.page.next_offset})
            self.load_page()

    def action_previous(self) -> None:
        if self.page is not None and self.request.offset:
            self.request = self.request.model_copy(
                update={"offset": max(0, self.request.offset - self.request.limit)}
            )
            self.load_page()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if self.page is not None:
            row = self.page.report.records[int(str(event.row_key.value))]
            self.app.push_screen(
                RowDetails(row.model_dump_json(indent=2), "Runtime comparison record")
            )

    def _selected_record(self) -> tuple[int, str] | None:
        if self.page is None or not self.page.report.records:
            return None
        position = self.query_one("#runtime-comparison-records", DataTable).cursor_row
        records = self.page.report.records
        if position < 0 or position >= len(records):
            return None
        return position, records[position].frame_identity

    @work(exclusive=True, group="runtime-comparison-source")
    async def action_baseline_source(self) -> None:
        await self._open_comparison_source("baseline")

    @work(exclusive=True, group="runtime-comparison-source")
    async def action_candidate_source(self) -> None:
        await self._open_comparison_source("candidate")

    async def _open_comparison_source(self, side: ComparisonSourceSide) -> None:
        if self.page is None:
            return
        if self.source_root is None:
            self.on_status(errno.ENODATA)
            self.app.push_screen(
                RowDetails(
                    "Source preview disabled; launch tui with --source-root.",
                    "Runtime comparison source unavailable",
                )
            )
            return
        selected = self._selected_record()
        if selected is None:
            self.on_status(errno.ENODATA)
            self.app.push_screen(
                RowDetails(
                    "Select a comparison row with a saved frame identity first.",
                    "Runtime comparison source unavailable",
                )
            )
            return
        position, frame_identity = selected
        request = self.request
        token = (request, position, side)
        self._source_token = token
        try:
            preview = await asyncio.to_thread(
                self.index.runtime_comparison_source, frame_identity, side
            )
        except (ValueError, OSError, sqlite3.Error) as exc:
            if (
                self.is_mounted
                and self._source_token == token
                and self.request == request
                and self._selected_record() == (position, frame_identity)
            ):
                self.on_status(
                    errno.ENODATA if isinstance(exc, RuntimeSourceUnavailable) else errno.EBADMSG
                )
                self.app.push_screen(RowDetails(str(exc), "Runtime comparison source unavailable"))
            return
        if (
            not self.is_mounted
            or self._source_token != token
            or self.request != request
            or self.page is None
        ):
            return
        current = self._selected_record()
        if current is None or current != (position, frame_identity):
            return
        from code_analysis.tui_source import TerminalSource

        if not isinstance(self.index, BrowseIndex):
            self.on_status(errno.ENODATA)
            self.app.push_screen(
                RowDetails(
                    "Comparison source preview requires a browse index with static sources.",
                    "Runtime comparison source unavailable",
                )
            )
            return
        table = self.query_one("#runtime-comparison-records", DataTable)
        table.focus()
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "runtime-comparison-next":
            self.action_next()
        elif event.button.id == "runtime-comparison-previous":
            self.action_previous()
        elif event.button.id == "runtime-comparison-baseline-source":
            self.action_baseline_source()
        elif event.button.id == "runtime-comparison-candidate-source":
            self.action_candidate_source()
        elif event.button.id == "runtime-comparison-health":
            runtime = self.index.manifest.runtime
            self.app.push_screen(
                RowDetails(
                    runtime.model_dump_json(indent=2) if runtime else '{"available":false}',
                    "Runtime comparison compatibility and provenance",
                )
            )
        else:
            self.action_close()

    def action_export(self) -> None:
        if self.page is not None:
            from code_analysis.tui_export import ExportPage

            self.app.push_screen(ExportPage(self.index, self.request, self.on_status, self.viewer))

    def action_close(self) -> None:
        self._source_token = None
        self.workers.cancel_group(self, "runtime-comparison-pages")
        self.workers.cancel_group(self, "runtime-comparison-source")
        self.dismiss()
