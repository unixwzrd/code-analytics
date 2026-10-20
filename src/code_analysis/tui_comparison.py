"""Bounded baseline/candidate browsing over an explicitly prepared comparison."""

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
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Select, Static

from code_analysis.browse_index import BrowseIndex
from code_analysis.comparison_browse import ComparisonFilters, ComparisonPageRequest
from code_analysis.projection_models import ComparisonChange
from code_analysis.static_comparison_source import (
    StaticComparisonSourceBinding,
    StaticComparisonSourceQuery,
    resolve_static_comparison_source_option,
)
from code_analysis.tui_app import RowDetails, plain_text
from code_analysis.ui_models import UIComparisonPage


class ComparisonBrowser(ModalScreen[int]):
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+n", "next", "Next page"),
        ("ctrl+p", "previous", "Previous page"),
        ("s", "sources", "Source choices"),
        ("ctrl+e", "export", "Export page"),
    ]
    CSS = """
    ComparisonBrowser { align: center middle; }
    #comparison-dialog { width: 95%; height: 90%; border: round $accent;
                         background: $surface; padding: 1; }
    #comparison-status { height: auto; }
    #comparison-rows { height: 1fr; }
    #comparison-buttons { height: 3; }
    #comparison-filters { height: 3; }
    """

    def __init__(
        self,
        index: BrowseIndex,
        on_status: Callable[[int], None],
        page_size: int = 25,
        *,
        source_root: Path | None = None,
    ) -> None:
        super().__init__()
        if type(page_size) is not int or not 1 <= page_size <= 500:
            raise ValueError("comparison page size requires an integer from 1 to 500")
        self.index = index
        self.page_size = page_size
        self.on_status = on_status
        self.source_root = source_root
        self.record_offset = 0
        self.page: UIComparisonPage | None = None
        self.error_status = 0
        self.filters = ComparisonFilters()

    def compose(self) -> ComposeResult:
        with Vertical(id="comparison-dialog"):
            summary = self.index.manifest.comparison
            text = "Comparison unavailable — prepare index BASELINE --compare CANDIDATE."
            if summary is not None:
                text = (
                    f"Baseline: {summary.baseline_sha256}\nCandidate: {summary.candidate_sha256}\n"
                )
                text += (
                    "No canonical report changes detected.\n"
                    if summary.identical
                    else "Canonical reports differ.\n"
                )
                if summary.counts is not None:
                    counts = summary.counts
                    text += (
                        f"Global: +{counts.added} / -{counts.removed} / "
                        f"changed {counts.changed} / unchanged {counts.unchanged}\n"
                    )
                text += (
                    f"Comparison: {summary.completeness.value}; eligible {summary.eligible}; "
                    f"omitted {summary.omitted}; includes unchanged: {summary.includes_unchanged}"
                )
            yield Static(plain_text(text))
            yield Static("", id="comparison-status")
            with Horizontal(id="comparison-filters"):
                yield Select(
                    [("All changes", ""), *((item.value, item.value) for item in ComparisonChange)],
                    value="",
                    allow_blank=False,
                    id="comparison-change",
                )
                yield Input(placeholder="Kind (e.g. finding)", id="comparison-kind")
                yield Input(placeholder="Case-sensitive search", id="comparison-search")
                yield Button("Apply", id="comparison-apply")
            yield DataTable(id="comparison-rows", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="comparison-buttons"):
                yield Button("Previous", id="comparison-previous", disabled=True)
                yield Button("Next", id="comparison-next", disabled=True)
                yield Button("Sources", id="comparison-sources")
                yield Button("Export", id="comparison-export")
                yield Button("Close", id="comparison-close")

    def on_mount(self) -> None:
        if self.index.manifest.comparison is not None:
            self.load_page()

    @work(exclusive=True, group="comparison")
    async def load_page(self) -> None:
        offset = self.record_offset
        self.page = None
        table = self.query_one("#comparison-rows", DataTable)
        table.clear(columns=True)
        for name in ("next", "previous"):
            self.query_one(f"#comparison-{name}", Button).disabled = True
        try:
            page = await asyncio.to_thread(
                self.index.comparison_records,
                offset=offset,
                limit=self.page_size,
                filters=self.filters,
            )
        except (ValueError, OSError, sqlite3.Error) as exc:
            if self.is_mounted:
                self.error_status = errno.EBADMSG
                self.on_status(self.error_status)
                self.query_one("#comparison-status", Static).update(
                    plain_text(f"Comparison unavailable: {exc}")
                )
            return
        if not self.is_mounted or offset != self.record_offset:
            return
        self.page = page
        table.add_columns("Subject", "Change", "Explanation")
        for number, row in enumerate(page.records):
            table.add_row(
                plain_text(row.subject_id),
                plain_text(row.change),
                plain_text(row.explanation),
                key=str(number),
            )
        self.query_one("#comparison-status", Static).update(
            f"Rows {offset + page.retained}/{page.eligible} retained; "
            f"{page.omitted} analytically omitted. "
            f"{page.filtered_out} filtered out. "
            + ("More pages available." if page.truncated else "End of retained changes.")
        )
        self.query_one("#comparison-next", Button).disabled = not page.truncated
        self.query_one("#comparison-previous", Button).disabled = offset == 0
        table.focus()

    def action_next(self) -> None:
        if self.page is not None and self.page.truncated and self.page.offset == self.record_offset:
            self.record_offset += self.page_size
            self.load_page()

    def action_previous(self) -> None:
        if self.record_offset:
            self.record_offset = max(0, self.record_offset - self.page_size)
            self.load_page()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if self.page is not None:
            self.load_record(self.page.records[int(event.row_key.value)].position)

    @work(exclusive=True, group="comparison-detail")
    async def load_record(self, position: int) -> None:
        offset = self.record_offset
        try:
            record = await asyncio.to_thread(self.index.comparison_record, position)
        except (ValueError, OSError, sqlite3.Error) as exc:
            if self.is_mounted:
                self.error_status = errno.EBADMSG
                self.on_status(self.error_status)
                self.query_one("#comparison-status", Static).update(
                    plain_text(f"Record unavailable: {exc}")
                )
            return
        if self.is_mounted and offset == self.record_offset:
            self.app.push_screen(
                RowDetails(
                    json.dumps(record.model_dump(mode="json"), indent=2, ensure_ascii=False),
                    "Saved change — baseline and candidate values",
                )
            )

    def action_close(self) -> None:
        self.workers.cancel_group(self, "comparison")
        self.workers.cancel_group(self, "comparison-detail")
        self.workers.cancel_group(self, "comparison-sources")
        self.dismiss(self.error_status)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "comparison-next":
            self.action_next()
        elif event.button.id == "comparison-previous":
            self.action_previous()
        elif event.button.id == "comparison-sources":
            self.action_sources()
        elif event.button.id == "comparison-export":
            self.action_export()
        elif event.button.id == "comparison-apply":
            self.apply_filters()
        else:
            self.action_close()

    def apply_filters(self) -> None:
        change = self.query_one("#comparison-change", Select).value
        kind = self.query_one("#comparison-kind", Input).value.strip()
        try:
            self.filters = ComparisonFilters(
                changes=(ComparisonChange(change),) if change else (),
                kinds=(kind,) if kind else (),
                search=self.query_one("#comparison-search", Input).value,
            )
        except ValueError as exc:
            self.query_one("#comparison-status", Static).update(plain_text(str(exc)))
            return
        self.record_offset = 0
        self.load_page()

    def action_sources(self) -> None:
        if self.page is None or not self.page.records:
            return
        row = self.query_one("#comparison-rows", DataTable).cursor_row
        self.load_sources(self.page.records[row].position)

    def action_export(self) -> None:
        from code_analysis.tui_export import ExportPage

        summary = self.index.manifest.comparison
        if summary is None or self.page is None:
            return
        self.app.push_screen(
            ExportPage(
                self.index,
                ComparisonPageRequest(
                    baseline_sha256=summary.baseline_sha256,
                    candidate_sha256=summary.candidate_sha256,
                    filters=self.filters,
                    offset=self.page.offset,
                    limit=self.page.limit,
                ),
                self.on_status,
            )
        )

    @work(exclusive=True, group="comparison-sources")
    async def load_sources(self, position: int) -> None:
        offset = self.record_offset
        try:
            binding = await asyncio.to_thread(self.index.static_comparison_source_detail, position)
        except (ValueError, OSError, sqlite3.Error) as exc:
            if self.is_mounted:
                self.on_status(errno.EBADMSG)
                self.query_one("#comparison-status", Static).update(plain_text(str(exc)))
            return
        if (
            self.is_mounted
            and offset == self.record_offset
            and self.page is not None
            and self.page.records[self.query_one("#comparison-rows", DataTable).cursor_row].position
            == position
        ):
            self.app.push_screen(
                ComparisonSources(self.index, binding, self.source_root, self.on_status)
            )


class ComparisonSources(ModalScreen[None]):
    """Bounded side-specific choices; Enter reuses the existing confined reader."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "close", "Close")]
    CSS = """
    ComparisonSources { align: center middle; }
    #comparison-source-dialog { width: 95%; height: 90%; border: round $accent;
                               background: $surface; padding: 1; }
    #comparison-source-status { height: auto; max-height: 6; overflow-y: auto; }
    #comparison-source-choices { height: 1fr; }
    """

    def __init__(
        self,
        index: BrowseIndex,
        binding: StaticComparisonSourceBinding,
        source_root: Path | None,
        on_status: Callable[[int], None],
    ) -> None:
        super().__init__()
        self.index, self.binding, self.source_root, self.on_status = (
            index,
            binding,
            source_root,
            on_status,
        )
        self.choices = tuple(
            StaticComparisonSourceQuery(
                baseline_sha256=binding.baseline.report_sha256,
                candidate_sha256=binding.candidate.report_sha256,
                position=binding.position,
                side=side,
                option_id=option.option_id,
            )
            for side, detail in (("baseline", binding.baseline), ("candidate", binding.candidate))
            for option in detail.options
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="comparison-source-dialog"):
            yield Static(
                plain_text(
                    "\n".join(
                        f"{side}: {detail.message}; {detail.omitted_option_count} choices omitted"
                        for side, detail in (
                            ("Baseline", self.binding.baseline),
                            ("Candidate", self.binding.candidate),
                        )
                    )
                ),
                id="comparison-source-status",
            )
            yield DataTable(id="comparison-source-choices", cursor_type="row")
            yield Button("Close", id="comparison-source-close")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Side", "Recorded source")
        for number, query in enumerate(self.choices):
            option = resolve_static_comparison_source_option(self.binding, query)
            table.add_row(query.side, plain_text(option.label), key=str(number))
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        from code_analysis.tui_source import TerminalSource
        from code_analysis.ui_source import SourcePreviewIdentity

        event.stop()
        query = self.choices[int(event.row_key.value)]
        option = resolve_static_comparison_source_option(self.binding, query)
        self.app.push_screen(
            TerminalSource(
                self.index,
                self.source_root,
                option.logical_path,
                option.start_line,
                option.end_line,
                self.on_status,
                source_identity=SourcePreviewIdentity(
                    logical_path=option.logical_path, source_sha256=option.source_sha256
                ),
            )
        )

    def action_close(self) -> None:
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_close()
