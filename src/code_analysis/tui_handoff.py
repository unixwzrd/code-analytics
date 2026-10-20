"""Explicit terminal-to-Web handoff over one saved report artifact."""

from __future__ import annotations

import asyncio
import errno
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TextArea

from code_analysis.artifact_viewer import ViewerCommand, launch_display
from code_analysis.browse_index import BrowseIndex
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_index import RuntimeIndex
from code_analysis.tui_app import plain_text
from code_analysis.ui_handoff import OwnedWebHandoff, RuntimeHandoffSelection, WebHandoffSelection

HandoffSelection = WebHandoffSelection | RuntimeHandoffSelection


def _bounded_error(exc: BaseException) -> str:
    text = str(exc)
    return text if len(text) <= 2048 else text[:2045] + "…"


def runtime_selection(
    index: BrowseIndex | RuntimeIndex, request: RuntimeGraphRequest
) -> RuntimeHandoffSelection | None:
    """Bind the handoff to the canonical profile digest and workload identity in the index."""
    summary = index.manifest.runtime
    if summary is None or summary.overview.profile_identity is None:
        return None
    return RuntimeHandoffSelection(
        profile_report_sha256=summary.profile_report_sha256,
        profile_identity_sha256=summary.overview.profile_identity,
        graph_request=request,
    )


class WebHandoffDialog(ModalScreen[int]):
    """Prepare an exact report-bound URL, then let the user open it explicitly."""

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "close", "Close")]
    CSS = """
    WebHandoffDialog { align: center middle; }
    #handoff-dialog { width: 90%; height: auto; max-height: 90%; border: round $accent;
                      background: $surface; padding: 1; }
    #handoff-report, #handoff-url { width: 1fr; }
    #handoff-url { height: 3; }
    #handoff-buttons { height: 3; }
    #handoff-status { height: auto; }
    """

    def __init__(
        self,
        owner: OwnedWebHandoff,
        selection: HandoffSelection,
        browser: ViewerCommand | None,
        initial_report: Path | None = None,
        on_verified: Callable[[Path], None] | None = None,
    ) -> None:
        super().__init__()
        self.owner = owner
        self.selection = selection
        self.artifact_kind = (
            "profile" if isinstance(selection, RuntimeHandoffSelection) else "report"
        )
        self.browser = browser
        self.initial_report = initial_report
        self.on_verified = on_verified
        self.url: str | None = None
        self.verified_input: str | None = None
        self.error_status = 0

    def compose(self) -> ComposeResult:
        if isinstance(self.selection, RuntimeHandoffSelection):
            graph = self.selection.graph_request
            description = (
                f"runtime graph: {graph.max_nodes} nodes / {graph.max_edges} edges"
                f"{'; project frames only' if graph.project_only else ''}"
            )
            artifact = (
                "Enter the saved profile JSON that produced this indexed runtime view. "
                "The canonical profile hash, workload identity, and exact graph request "
                "are verified before serving."
            )
            placeholder = "/absolute/or/relative/path/to/profile.json"
        else:
            description = (
                "whole saved report"
                if self.selection.kind == "report"
                else f"{self.selection.kind}: {self.selection.identity or 'module overview'}"
            )
            if self.selection.graph_request is not None:
                description = (
                    f"graph: {self.selection.graph_request.name.value} (exact filters and bounds)"
                )
            artifact = (
                "Enter the saved report JSON that produced this indexed view. "
                "The report hash and exact selection are verified before serving."
            )
            placeholder = "/absolute/or/relative/path/to/report.json"
        with Vertical(id="handoff-dialog"):
            yield Static(plain_text(f"Prepare read-only Web handoff · {description}"))
            yield Static(artifact)
            yield Input(
                value=str(self.initial_report) if self.initial_report is not None else "",
                placeholder=placeholder,
                id="handoff-report",
                max_length=4096,
            )
            yield Button("Prepare Web handoff", id="handoff-prepare", variant="primary")
            yield TextArea(
                "",
                placeholder="Prepared local URL appears here",
                id="handoff-url",
                read_only=True,
                show_line_numbers=False,
            )
            yield Static(
                "No browser opens automatically. The URL remains local and read-only.",
                id="handoff-status",
            )
            with Horizontal(id="handoff-buttons"):
                yield Button("Open browser", id="handoff-open", disabled=True)
                yield Button("Copy URL", id="handoff-copy", disabled=True)
                yield Button("Close", id="handoff-close")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "handoff-report":
            return
        self._clear_prepared()
        self.query_one("#handoff-status", Static).update(
            f"{self.artifact_kind.capitalize()} path changed; prepare and verify the handoff again."
        )

    def _clear_prepared(self) -> None:
        self.url = None
        self.verified_input = None
        self.query_one("#handoff-url", TextArea).text = ""
        self.query_one("#handoff-open", Button).disabled = True
        self.query_one("#handoff-copy", Button).disabled = True

    def _prepared_input_matches(self) -> bool:
        return (
            self.is_mounted
            and self.url is not None
            and self.verified_input == self.query_one("#handoff-report", Input).value.strip()
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "handoff-prepare":
            self.prepare()
        elif event.button.id == "handoff-open":
            self.open_browser()
        elif event.button.id == "handoff-copy":
            if self._prepared_input_matches():
                assert self.url is not None
                self.app.copy_to_clipboard(self.url)
                self.query_one("#handoff-status", Static).update(
                    "Copied the local capability URL. Do not share it."
                )
        elif event.button.id == "handoff-close":
            self.action_close()

    def prepare(self) -> None:
        value = self.query_one("#handoff-report", Input).value.strip()
        self._clear_prepared()
        if not value:
            self.query_one("#handoff-status", Static).update(
                f"Select a saved {self.artifact_kind} JSON first."
            )
            return
        self._prepare(value)

    def _publish(self, artifact: Path) -> str:
        if isinstance(self.selection, RuntimeHandoffSelection):
            return self.owner.open_runtime(artifact, self.selection)
        return self.owner.open(artifact, self.selection)

    @work(exclusive=True, group="handoff-prepare")
    async def _prepare(self, value: str) -> None:
        report = Path(value).expanduser().absolute()
        self.query_one("#handoff-prepare", Button).disabled = True
        self.query_one("#handoff-open", Button).disabled = True
        self.query_one("#handoff-status", Static).update(
            f"Validating saved {self.artifact_kind} and selection…"
        )
        try:
            url = await asyncio.to_thread(self._publish, report)
        except (ValueError, UnicodeError, OSError) as exc:
            if self.is_mounted:
                self.error_status = (
                    exc.errno if isinstance(exc, OSError) and exc.errno else errno.EBADMSG
                )
                self.query_one("#handoff-status", Static).update(
                    plain_text(f"Web handoff unavailable: {_bounded_error(exc)}")
                )
            return
        finally:
            if self.is_mounted:
                self.query_one("#handoff-prepare", Button).disabled = False
        if not self.is_mounted:
            return
        if self.query_one("#handoff-report", Input).value.strip() != value:
            self.query_one("#handoff-status", Static).update(
                f"{self.artifact_kind.capitalize()} path changed during validation; "
                "prepare the handoff again."
            )
            return
        self.error_status = 0
        self.url = url
        self.verified_input = value
        self.initial_report = report
        if self.on_verified is not None:
            self.on_verified(report)
        self.query_one("#handoff-url", TextArea).text = url
        self.query_one("#handoff-open", Button).disabled = self.browser is None
        self.query_one("#handoff-copy", Button).disabled = False
        self.query_one("#handoff-status", Static).update(
            "Verified. Open explicitly, or select and copy the URL from the field."
            if self.browser is not None
            else "Verified. No browser launcher is configured; select and copy the URL."
        )

    @work(exclusive=True, group="handoff-open")
    async def open_browser(self) -> None:
        if not self._prepared_input_matches() or self.browser is None:
            return
        assert self.url is not None
        url = self.url
        self.query_one("#handoff-open", Button).disabled = True
        self.query_one("#handoff-status", Static).update("Launching the configured browser…")
        try:
            await asyncio.to_thread(launch_display, url, self.browser)
        except OSError as exc:
            if self.is_mounted:
                self.error_status = exc.errno or errno.EPROTO
                self.query_one("#handoff-status", Static).update(
                    plain_text(f"Browser launch failed: {_bounded_error(exc)}")
                )
            return
        finally:
            if self.is_mounted and self.url == url:
                self.query_one("#handoff-open", Button).disabled = False
        if self.is_mounted and self.url == url:
            self.error_status = 0
            self.query_one("#handoff-status", Static).update(
                "Browser launch request completed; rendering remains browser-owned."
            )

    def action_close(self) -> None:
        self.workers.cancel_group(self, "handoff-prepare")
        self.workers.cancel_group(self, "handoff-open")
        self.dismiss(self.error_status)
