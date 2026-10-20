"""Bounded terminal evidence pages over the shared saved-query contract."""

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
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static

from code_analysis.browse_index import BrowseIndex
from code_analysis.models import SourceSpan
from code_analysis.queries import render_query_markdown
from code_analysis.query_models import QueryResponse
from code_analysis.tui_app import plain_text


class FindingEvidence(ModalScreen[int]):
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+n", "next", "Next evidence"),
    ]
    CSS = """
    FindingEvidence { align: center middle; }
    #evidence-dialog { width: 95%; height: 90%; border: round $accent;
                       background: $surface; padding: 1; }
    #evidence-buttons { height: 3; }
    """

    def __init__(
        self,
        index: BrowseIndex,
        identity: str,
        on_error: Callable[[int], None],
        source_root: Path | None = None,
        web_action: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.index = index
        self.preview_root = source_root
        self.source_spans: tuple[SourceSpan, ...] = ()
        self.identity = identity
        self.page: QueryResponse | None = None
        self.error_status = 0
        self.on_error = on_error
        self.web_action = web_action

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="evidence-dialog"):
            yield Static("Finding evidence — bounded saved-query pages")
            yield Static("Source spans from this evidence page", id="source-options")
            yield Select([], id="evidence-source", prompt="Choose a source span")
            yield Button("Preview selected source", id="evidence-preview", disabled=True)
            yield Static("Loading…", id="evidence-content")
            with Horizontal(id="evidence-buttons"):
                yield Button(
                    "Open finding in Web UI", id="evidence-web", disabled=self.web_action is None
                )
                yield Button("Next evidence", id="evidence-next", disabled=True)
                yield Button("Close (Escape)", id="evidence-close")

    def on_mount(self) -> None:
        self.load_page(None)

    @work(exclusive=True, group="evidence")
    async def load_page(self, after: str | None) -> None:
        self.page = None
        self.source_spans = ()
        self.query_one("#evidence-preview", Button).disabled = True
        self.query_one("#evidence-next", Button).disabled = True
        self.query_one("#evidence-content", Static).update("Loading bounded evidence page…")
        try:
            page = await asyncio.to_thread(self.index.finding_evidence, self.identity, after)
        except (ValueError, OSError, sqlite3.Error) as exc:
            if self.is_mounted:
                self.error_status = errno.EBADMSG
                self.on_error(self.error_status)
                self.query_one("#evidence-content", Static).update(
                    plain_text(f"Evidence unavailable: {exc}")
                )
            return
        if not self.is_mounted:
            return
        self.page = page
        spans = sorted(
            {evidence.span for item in page.items for evidence in item.evidence},
            key=lambda span: (span.path, span.start_line, span.end_line),
        )
        self.source_spans = tuple(spans[:200])
        self.query_one("#source-options", Static).update(
            f"Source spans: {len(self.source_spans)} of {len(spans)} on this page"
        )
        self.query_one("#evidence-source", Select).set_options(
            [
                (plain_text(f"{span.path}:{span.start_line}-{span.end_line}"), number)
                for number, span in enumerate(self.source_spans)
            ]
        )
        if self.source_spans:
            self.query_one("#evidence-source", Select).value = 0
            self.query_one("#evidence-preview", Button).disabled = False
        if len(spans) > 200:
            self.on_error(errno.ENODATA)
        self.query_one("#evidence-content", Static).update(plain_text(render_query_markdown(page)))
        self.query_one("#evidence-next", Button).disabled = page.next_identity is None

    def action_next(self) -> None:
        if self.page is not None and self.page.next_identity is not None:
            self.load_page(self.page.next_identity)

    def action_close(self) -> None:
        self.workers.cancel_group(self, "evidence")
        self.dismiss(self.error_status)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "evidence-next":
            self.action_next()
        elif event.button.id == "evidence-web" and self.web_action is not None:
            self.web_action()
        elif event.button.id == "evidence-preview":
            from code_analysis.tui_source import TerminalSource

            selection = self.query_one("#evidence-source", Select).value
            if type(selection) is int and 0 <= selection < len(self.source_spans):
                span = self.source_spans[selection]
                self.app.push_screen(
                    TerminalSource(
                        self.index,
                        self.preview_root,
                        span.path,
                        span.start_line,
                        span.end_line,
                        self.on_error,
                    )
                )
        else:
            self.action_close()
