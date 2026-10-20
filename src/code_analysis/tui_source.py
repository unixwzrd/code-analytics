"""Explicit terminal previews through the shared confined source reader."""

from __future__ import annotations

import asyncio
import errno
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from code_analysis.browse_index import BrowseIndex, file_stamp
from code_analysis.tui_app import plain_text
from code_analysis.ui_source import SourcePreview, SourcePreviewIdentity, preview_source_record


class TerminalSource(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+n", "next", "Next lines"),
    ]
    CSS = """
    TerminalSource { align: center middle; }
    #source-dialog { width: 95%; height: 90%; border: round $accent;
                     background: $surface; padding: 1; }
    """

    def __init__(
        self,
        index: BrowseIndex,
        root: Path | None,
        path: str,
        start: int,
        end: int,
        on_status: Callable[[int], None],
        *,
        source_identity: SourcePreviewIdentity | None = None,
    ) -> None:
        super().__init__()
        self.index = index
        self.preview_root = root
        self.path = path
        self.first_line = start
        self.last_line = end
        self.on_status = on_status
        self.preview: SourcePreview | None = None
        if source_identity is not None and source_identity.logical_path != path:
            raise ValueError("preview binding path mismatch")
        self.source_identity = source_identity

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="source-dialog"):
            yield Static("Read-only source preview · hash checked on every page")
            yield Static("Loading…", id="source-text")
            yield Button("Next lines", id="source-next", disabled=True)
            yield Button("Close", id="source-close")

    def on_mount(self) -> None:
        self.load_lines(self.first_line)

    def _read(self, start: int) -> SourcePreview:
        if file_stamp(self.index.database) != self.index.stamp:
            raise ValueError("browse index database changed")
        source = self.source_identity or self.index.source_file(self.path)
        return preview_source_record(source, self.preview_root, start=start, end=self.last_line)

    @work(exclusive=True, group="source")
    async def load_lines(self, start: int) -> None:
        self.preview = None
        self.query_one("#source-next", Button).disabled = True
        self.query_one("#source-text", Static).update("Checking source identity…")
        try:
            preview = await asyncio.to_thread(self._read, start)
        except (ValueError, OSError, sqlite3.Error) as exc:
            if self.is_mounted:
                self.on_status(errno.EBADMSG)
                self.query_one("#source-text", Static).update(
                    plain_text(f"Preview unavailable: {exc}")
                )
            return
        if not self.is_mounted:
            return
        self.preview = preview
        if preview.state not in ("bound", "disabled"):
            self.on_status(errno.ENODATA)
        rendered = (
            f"{preview.logical_path} · {preview.state}\n{preview.message}\n"
            f"{preview.expected_sha256}\n\n"
        )
        rendered += "\n".join(f"{line.number:6d}  {line.text}" for line in preview.lines)
        self.query_one("#source-text", Static).update(plain_text(rendered))
        self.query_one("#source-next", Button).disabled = preview.next_line is None

    def action_next(self) -> None:
        if self.preview is not None and self.preview.next_line is not None:
            self.load_lines(self.preview.next_line)

    def action_close(self) -> None:
        self.workers.cancel_group(self, "source")
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "source-next":
            self.action_next()
        else:
            self.action_close()
