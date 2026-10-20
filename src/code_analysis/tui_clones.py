"""Bounded clone-member comparison with explicitly opted-in source previews."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from code_analysis.browse_index import BrowseIndex
from code_analysis.clone_browsing import CloneRow
from code_analysis.tui_app import RowDetails, plain_text


class CloneComparison(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+n", "next", "Next"),
        ("ctrl+p", "previous", "Previous"),
    ]
    CSS = """
    CloneComparison { align: center middle; }
    #clone-dialog { width: 95%; height: 90%; border: round $accent;
                    background: $surface; padding: 1; }
    #clone-summary { height: auto; }
    #clone-buttons { height: 3; }
    """

    def __init__(
        self,
        index: BrowseIndex,
        row: CloneRow,
        on_error: Callable[[int], None],
        source_root: Path | None = None,
    ) -> None:
        super().__init__()
        self.index = index
        self.row = row
        self.on_error = on_error
        self.source_root = source_root
        self.member_offset = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="clone-dialog"):
            yield Static(
                "Clone members — syntax matches, not semantic equivalence or authorization"
            )
            yield Static("", id="clone-summary")
            yield DataTable(id="clone-members", cursor_type="row")
            with Horizontal(id="clone-buttons"):
                yield Button("Previous", id="clone-previous")
                yield Button("Next", id="clone-next")
                yield Button("Close", id="clone-close")

    def on_mount(self) -> None:
        self.render_members()

    def render_members(self) -> None:
        members = self.row.detail.members[self.member_offset : self.member_offset + 25]
        table = self.query_one("#clone-members", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "Symbol",
            "Source",
            "Lines",
            "Nonblank",
            "Tokens",
            "AST nodes",
            "Statements",
            "Events",
            "Coverage",
        )
        for member in members:
            fragment = member.fragment
            span = fragment.evidence[0].span
            values = (
                member.subject.logical_name,
                span.path,
                f"{span.start_line}-{span.end_line}",
                fragment.nonblank_lines,
                fragment.token_count,
                fragment.ast_node_count,
                fragment.statement_count,
                fragment.matched_event_count,
                fragment.sequence_coverage,
            )
            table.add_row(*(plain_text(str(value)) for value in values), key=fragment.fragment_id)
        self.query_one("#clone-summary", Static).update(
            plain_text(
                f"{self.row.kind.value}; {self.row.language.value}; {self.row.confidence.value}; "
                f"{self.row.completeness.value}. Members {self.member_offset + 1}"
                f"-{self.member_offset + len(members)} of {self.row.fragment_count}. "
                "Enter shows fingerprints, full evidence and limitations. "
                "Source column uses first evidence span."
            )
        )
        self.query_one("#clone-previous", Button).disabled = self.member_offset == 0
        self.query_one("#clone-next", Button).disabled = (
            self.member_offset + 25 >= self.row.fragment_count
        )
        table.focus()

    def action_next(self) -> None:
        if self.member_offset + 25 < self.row.fragment_count:
            self.member_offset += 25
            self.render_members()

    def action_previous(self) -> None:
        if self.member_offset:
            self.member_offset = max(0, self.member_offset - 25)
            self.render_members()

    def action_close(self) -> None:
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "clone-next":
            self.action_next()
        elif event.button.id == "clone-previous":
            self.action_previous()
        else:
            self.action_close()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        member = next(
            item
            for item in self.row.detail.members
            if item.fragment.fragment_id == event.row_key.value
        )

        def preview() -> None:
            from code_analysis.tui_source import TerminalSource

            span = member.fragment.evidence[0].span
            self.app.push_screen(
                TerminalSource(
                    self.index,
                    self.source_root,
                    span.path,
                    span.start_line,
                    span.end_line,
                    self.on_error,
                )
            )

        self.app.push_screen(
            RowDetails(
                json.dumps(
                    {
                        "member": member.model_dump(mode="json"),
                        "cluster": self.row.detail.cluster.model_dump(mode="json"),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                "Clone evidence — preview uses the first recorded span; "
                "other evidence remains listed",
                preview,
            )
        )
