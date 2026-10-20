"""Report-free terminal entry; hand a verified index to the existing report application."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Header, Static

from code_analysis.artifact_viewer import ViewerCommand
from code_analysis.browse_index import BrowseIndex
from code_analysis.collection_commands import CollectionPlanner
from code_analysis.collection_service import CollectionSession
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_index import RuntimeIndex
from code_analysis.tui_collection import CollectionPlan
from code_analysis.ui_handoff import OwnedWebHandoff


class CollectionTerminal(App[BrowseIndex | None]):
    TITLE = "Code Analysis · Collect"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "finish", "Quit", priority=True),
        Binding("ctrl+c", "finish", "Quit", priority=True),
    ]

    def __init__(
        self,
        root: Path,
        session: CollectionSession | None = None,
        collection_pythons: tuple[Path, ...] = (),
        browser: ViewerCommand | None = None,
    ) -> None:
        super().__init__()
        self.root, self.session = root, session
        self.collection_pythons = collection_pythons
        self.browser = browser
        self.web_handoff = OwnedWebHandoff()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "No report selected. Collect or connect saved jobs, then open a verified result."
        )
        yield Footer()

    def on_mount(self) -> None:
        planner = (
            self.session.planner
            if self.session is not None
            else CollectionPlanner(self.root, interpreters=self.collection_pythons)
        )
        self.push_screen(CollectionPlan(planner, self.session), self.exit)

    def open_runtime_handoff(
        self, index: BrowseIndex | RuntimeIndex, request: RuntimeGraphRequest
    ) -> None:
        """This host has no saved report; only the indexed profile can be handed off."""
        from code_analysis.tui_handoff import WebHandoffDialog, runtime_selection

        selection = runtime_selection(index, request)
        if selection is not None:
            self.push_screen(WebHandoffDialog(self.web_handoff, selection, self.browser))

    def close_web_handoff(self) -> None:
        self.web_handoff.close()

    def on_unmount(self) -> None:
        self.close_web_handoff()

    def action_finish(self) -> None:
        self.exit(None)
