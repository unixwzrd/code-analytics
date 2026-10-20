"""Candidate section continuation over the report-bound prepared index."""

from __future__ import annotations

import asyncio
import errno
import json
import sqlite3
from collections.abc import Callable
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from code_analysis.browse_index import BrowseIndex
from code_analysis.tui_app import plain_text
from code_analysis.ui_models import UICandidatePacket


class CandidatePacket(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+n", "next", "Next"),
        ("ctrl+p", "previous", "Previous"),
    ]
    CSS = """
    CandidatePacket { align: center middle; }
    #candidate-dialog { width: 95%; height: 90%; border: round $accent;
                        background: $surface; padding: 1; }
    #candidate-buttons { height: 3; }
    #candidate-summary { height: auto; }
    """

    def __init__(
        self, index: BrowseIndex, page: UICandidatePacket, on_error: Callable[[int], None]
    ) -> None:
        super().__init__()
        self.index = index
        self.page = page
        self.on_error = on_error

    def compose(self) -> ComposeResult:
        with Vertical(id="candidate-dialog"):
            yield Static("Static candidate evidence — not a live decision or authorization")
            yield Static("", id="candidate-summary")
            with VerticalScroll():
                yield Static("", id="candidate-content")
            with Horizontal(id="candidate-buttons"):
                yield Button("Previous", id="candidate-previous")
                yield Button("Next", id="candidate-next")
                yield Button("Close", id="candidate-close")

    def on_mount(self) -> None:
        self.render_page()

    def render_page(self) -> None:
        page = self.page
        counts = "; ".join(
            f"{item.label}: {len(getattr(page, item.label))}/{item.value}" for item in page.totals
        )
        self.query_one("#candidate-summary", Static).update(
            plain_text(
                f"Source {page.source_completeness.value}; section offset {page.offset}; {counts}. "
                "Other page records remain available through continuation."
            )
        )
        self.query_one("#candidate-content", Static).update(
            plain_text(json.dumps(page.model_dump(mode="json"), indent=2, ensure_ascii=False))
        )
        self.query_one("#candidate-previous", Button).disabled = page.offset == 0
        self.query_one("#candidate-next", Button).disabled = page.next_offset is None

    @work(exclusive=True, group="candidate-pages")
    async def load_page(self, offset: int) -> None:
        try:
            page = await asyncio.to_thread(
                self.index.candidate_packet, self.page.candidate.candidate_id, offset=offset
            )
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.on_error(errno.EBADMSG)
            if self.is_mounted:
                self.query_one("#candidate-summary", Static).update(
                    plain_text(f"Candidate continuation unavailable: {exc}")
                )
            return
        if self.is_mounted:
            self.page = page
            self.render_page()

    def action_next(self) -> None:
        if self.page.next_offset is not None:
            self.load_page(self.page.next_offset)

    def action_previous(self) -> None:
        if self.page.offset:
            self.load_page(max(0, self.page.offset - self.page.limit))

    def action_close(self) -> None:
        self.workers.cancel_group(self, "candidate-pages")
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "candidate-next":
            self.action_next()
        elif event.button.id == "candidate-previous":
            self.action_previous()
        else:
            self.action_close()
