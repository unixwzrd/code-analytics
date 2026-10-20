"""Explicit owned-generation selection; preparation is never a source scan."""

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
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Select, Static

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import BrowseIndexLimitError
from code_analysis.comparisons import ComparisonBuildError
from code_analysis.indexed_runs import IndexedRunRepository
from code_analysis.runtime_index import RuntimeIndex
from code_analysis.tui_app import plain_text
from code_analysis.ui_runs import UIRunCatalog


class RunPicker(ModalScreen[BrowseIndex | RuntimeIndex | None]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "close", "Close")]
    CSS = """
    RunPicker { align: center middle; }
    #run-dialog { width: 95%; height: auto; border: round $accent;
                  background: $surface; padding: 1; }
    #run-status { height: auto; }
    """

    def __init__(
        self, root: Path | None, on_status: Callable[[int], None], *, profile_only: bool = False
    ) -> None:
        super().__init__()
        self.root, self.on_status = root, on_status
        self.profile_only = profile_only
        self.repository: IndexedRunRepository | None = None
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="run-dialog"):
            yield Static(
                "Choose saved baseline and optional candidate. "
                "Open explicitly prepares/reuses an index; no source scan. "
                + (
                    "Profile comparisons are diagnostic-only; "
                    "no static binding or optimization claim."
                    if self.profile_only
                    else "Only explicitly selected runtime profiles are attached."
                )
            )
            yield Static("Loading owned generation catalog…", id="run-status")
            yield Select([], prompt="Baseline run", id="run-baseline")
            yield Select(
                [("No comparison", "none")], value="none", allow_blank=False, id="run-candidate"
            )
            yield Select(
                [("No runtime profile", "none")], value="none", allow_blank=False, id="run-profile"
            )
            yield Select(
                [("No profile comparison", "none")],
                value="none",
                allow_blank=False,
                id="run-profile-candidate",
            )
            yield Button("Refresh catalog", id="run-refresh")
            yield Button("Open / prepare index", id="run-open", disabled=True)
            yield Button("Close", id="run-close")

    def on_mount(self) -> None:
        if self.profile_only:
            self.query_one("#run-baseline", Select).display = False
            self.query_one("#run-candidate", Select).display = False
        self.refresh_catalog()

    @work(exclusive=True, group="run-picker")
    async def refresh_catalog(self) -> None:
        if self.root is None:
            self.query_one("#run-status", Static).update(
                "Run selection unavailable; launch tui with --workspace PATH."
            )
            self.on_status(errno.ENODATA)
            return
        self.busy = True
        self.query_one("#run-open", Button).disabled = True
        try:
            if self.repository is None:
                self.repository = await asyncio.to_thread(
                    IndexedRunRepository, self.root, include_analysis=not self.profile_only
                )
            catalog = (
                UIRunCatalog(records=())
                if self.profile_only
                else await asyncio.to_thread(self.repository.refresh)
            )
            profiles = await asyncio.to_thread(self.repository.profiles)
            options = [
                (f"{item.generation_id} · {item.report_sha256[:12]}", item.generation_id)
                for item in catalog.records
            ]
            self.query_one("#run-baseline", Select).set_options(options)
            self.query_one("#run-candidate", Select).set_options(
                [("No comparison", "none"), *options]
            )
            self.query_one("#run-candidate", Select).value = "none"
            profile_options = [
                (
                    f"{item.generation_id} · profile {item.profile_identity_sha256[:12]}",
                    item.generation_id,
                )
                for item in profiles.records
            ]
            for selector, label in (
                ("run-profile", "No runtime profile"),
                ("run-profile-candidate", "No profile comparison"),
            ):
                self.query_one(f"#{selector}", Select).set_options(
                    [(label, "none"), *profile_options]
                )
                self.query_one(f"#{selector}", Select).value = "none"
            if options:
                self.query_one("#run-baseline", Select).value = options[0][1]
                self.query_one("#run-open", Button).disabled = False
            if self.profile_only and profile_options:
                self.query_one("#run-profile", Select).value = profile_options[0][1]
                self.query_one("#run-open", Button).disabled = False
            self.query_one("#run-status", Static).update(
                (
                    "No static report selected; "
                    if self.profile_only
                    else f"{len(options)} analysis runs ({catalog.omitted} omitted); "
                )
                + f"{len(profiles.records)} profile runs ({profiles.omitted} omitted). "
                "Compatibility/source binding is validated on open, not guessed from dates."
            )
            if (
                catalog.omitted
                or profiles.omitted
                or not (profile_options if self.profile_only else options)
            ):
                self.on_status(errno.ENODATA)
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.on_status(errno.EBADMSG)
            self.query_one("#run-status", Static).update(plain_text(f"Catalog unavailable: {exc}"))
        finally:
            self.busy = False

    @work(group="run-picker")
    async def open_selection(self) -> None:
        if self.repository is None:
            return
        profile = str(self.query_one("#run-profile", Select).value)
        compare_profile = str(self.query_one("#run-profile-candidate", Select).value)
        if profile == "none" and (compare_profile != "none" or self.profile_only):
            self.on_status(errno.EINVAL)
            self.query_one("#run-status", Static).update(
                "Choose a baseline profile before a comparison profile."
            )
            return
        self.busy = True
        self.query_one("#run-open", Button).disabled = True
        self.query_one("#run-status", Static).update(
            "Preparing selected saved reports; this may take time on first open. No source scan."
        )
        try:
            baseline = str(self.query_one("#run-baseline", Select).value)
            candidate = str(self.query_one("#run-candidate", Select).value)
            repository = self.repository
            if self.profile_only:
                runtime_index = await asyncio.to_thread(
                    repository.select_runtime,
                    profile,
                    None if compare_profile == "none" else compare_profile,
                )
                selected: BrowseIndex | RuntimeIndex = runtime_index
            else:
                browse_index = await asyncio.to_thread(
                    repository.select,
                    baseline,
                    None if candidate == "none" else candidate,
                    profile=None if profile == "none" else profile,
                    compare_profile=None if compare_profile == "none" else compare_profile,
                )
                selected = browse_index
        except (ValueError, OSError, sqlite3.Error) as exc:
            status = (
                errno.ENODATA
                if isinstance(exc, BrowseIndexLimitError)
                else errno.EINVAL if isinstance(exc, ComparisonBuildError) else errno.EBADMSG
            )
            self.on_status(status)
            self.query_one("#run-status", Static).update(plain_text(f"Run selection failed: {exc}"))
            self.query_one("#run-open", Button).disabled = False
        else:
            self.dismiss(selected)
        finally:
            self.busy = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self.busy:
            return
        if event.button.id == "run-open":
            self.open_selection()
        elif event.button.id == "run-refresh":
            self.refresh_catalog()
        else:
            self.action_close()

    def action_close(self) -> None:
        if not self.busy:
            self.dismiss(None)
