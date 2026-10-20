"""Terminal controls over the shared durable collection session; no executor of its own."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable

from pydantic import ValidationError
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Checkbox, Input, Select, Static

from code_analysis.browse_index import BrowseIndex
from code_analysis.collection_commands import CollectionCommand, CollectionRequest
from code_analysis.collection_execution import CollectionExecutionLimits
from code_analysis.collection_service import (
    CollectionLaunch,
    CollectionSession,
    CollectionWorkspace,
)
from code_analysis.collection_views import collection_index
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_index import RuntimeIndex
from code_analysis.tui_app import plain_text


class CollectionJobsPanel(Vertical):
    DEFAULT_CSS = "CollectionJobsPanel { height: auto; } #jobs-status { height: auto; }"

    def __init__(self, session: CollectionSession, on_open: Callable[[BrowseIndex], None]) -> None:
        super().__init__()
        self.session = session
        self.on_open = on_open
        self.review: tuple[CollectionRequest, CollectionCommand] | None = None
        self.busy = False
        self.polling = False

    def compose(self) -> ComposeResult:
        yield Static(
            "Run and monitor. Jobs continue when this dialog closes. "
            "Quitting the application stops its owned job. Profiling is not sandboxed."
        )
        yield Input(
            value=".code-analysis",
            placeholder="Existing workspace to reconnect",
            id="jobs-workspace",
        )
        yield Button("Connect saved jobs", id="jobs-connect")
        yield Static("Worker wall seconds / per-stream capture MiB")
        yield Input(value="300", id="jobs-wall")
        yield Input(value="64", id="jobs-stream")
        yield Checkbox("I confirm the reviewed command and workspace writes", id="jobs-confirm")
        yield Button("Run reviewed command", id="jobs-launch", disabled=True)
        yield Select([], prompt="Retained job", id="jobs-selection")
        yield Static("", id="jobs-detail")
        yield Button("Refresh jobs", id="jobs-refresh")
        yield Button("Cancel selected job", id="jobs-cancel")
        yield Button("Show verified CLI diagnostics", id="jobs-log")
        yield Button("Open verified result", id="jobs-open")
        yield Button("Browse / compare retained profiles", id="jobs-profiles")
        yield Static("No jobs selected", id="jobs-status")

    def set_review(
        self, request: CollectionRequest | None, command: CollectionCommand | None
    ) -> None:
        self.review = (request, command) if request is not None and command is not None else None
        self.query_one("#jobs-confirm", Checkbox).value = False
        self.query_one("#jobs-launch", Button).disabled = True

    def on_mount(self) -> None:
        self.set_interval(1, self.refresh_jobs)
        self.refresh_jobs()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.query_one("#jobs-confirm", Checkbox).value = False

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        event.stop()
        self.query_one("#jobs-launch", Button).disabled = (
            not event.value or self.review is None or self.busy
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()

    @work(group="collection-poll")
    async def refresh_jobs(self) -> None:
        if self.polling or self.busy:
            return
        self.polling = True
        try:
            catalog = await asyncio.to_thread(self.session.catalog)
            if not self.is_mounted:
                return
            selection = self.query_one("#jobs-selection", Select)
            old = selection.value
            identities = {job.job_id for job in catalog.records}
            selection.set_options(
                [
                    (f"{job.command.operation} · {job.state} · {job.job_id}", job.job_id)
                    for job in catalog.records
                ]
            )
            if old in identities:
                selection.value = old
            elif catalog.records:
                selection.value = catalog.records[0].job_id
            job = next((item for item in catalog.records if item.job_id == selection.value), None)
            if job is not None:
                generation = job.binding.generation_id if job.binding else "unverified/unavailable"
                self.query_one("#jobs-detail", Static).update(
                    plain_text(
                        f"{job.state} · native status {job.status}\n{job.reason}\n"
                        f"Generation: {generation}"
                    )
                )
        except (OSError, ValueError) as exc:
            self.query_one("#jobs-status", Static).update(plain_text(str(exc)))
        finally:
            self.polling = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "jobs-refresh":
            self.refresh_jobs()
        elif not self.busy:
            self.command(event.button.id or "")

    def _runtime_web_action(
        self, index: RuntimeIndex
    ) -> Callable[[RuntimeGraphRequest], None] | None:
        """Report-bound and report-free hosts both own a session; neither is assumed."""
        from code_analysis.tui_app import AnalysisTerminal
        from code_analysis.tui_start import CollectionTerminal

        host = self.app
        if not isinstance(host, (AnalysisTerminal, CollectionTerminal)):
            return None
        return lambda request: host.open_runtime_handoff(index, request)

    def profile_selected(self, index: BrowseIndex | RuntimeIndex | None) -> None:
        if isinstance(index, RuntimeIndex):
            from code_analysis.tui_runtime import RuntimeBrowser

            self.app.push_screen(
                RuntimeBrowser(
                    index,
                    lambda code: self.query_one("#jobs-status", Static).update(
                        f"Runtime view status: {code}"
                    ),
                    web_action=self._runtime_web_action(index),
                )
            )

    @work(group="collection-action")
    async def command(self, action: str) -> None:
        if self.busy:
            return
        self.busy = True
        self.query_one("#jobs-launch", Button).disabled = True
        status = self.query_one("#jobs-status", Static)
        try:
            if action == "jobs-launch":
                if self.review is None or not self.query_one("#jobs-confirm", Checkbox).value:
                    raise ValueError("review and confirm a command first")
                request, command = self.review
                launch = CollectionLaunch(
                    request=request,
                    command=command,
                    confirmed=True,
                    limits=CollectionExecutionLimits(
                        wall_seconds=int(self.query_one("#jobs-wall", Input).value),
                        stream_bytes=int(self.query_one("#jobs-stream", Input).value) * 1024**2,
                    ),
                )
                job = await asyncio.to_thread(self.session.launch, launch)
                status.update(plain_text(f"Submitted {job.job_id}; {job.state}."))
                self.query_one("#jobs-confirm", Checkbox).value = False
            elif action == "jobs-connect":
                selection = CollectionWorkspace(
                    workspace=self.query_one("#jobs-workspace", Input).value
                )
                await asyncio.to_thread(self.session.connect, selection)
                status.update("Connected owned workspace.")
            elif action == "jobs-profiles":
                from code_analysis.tui_runs import RunPicker

                workspace = self.session.selected().workspace
                self.app.push_screen(
                    RunPicker(
                        workspace.root,
                        lambda code: status.update(f"Profile selection status: {code}"),
                        profile_only=True,
                    ),
                    self.profile_selected,
                )
            else:
                identity = self.query_one("#jobs-selection", Select).value
                if identity is Select.BLANK:
                    raise ValueError("select a retained job")
                jobs = self.session.selected()
                if action == "jobs-cancel":
                    job = await asyncio.to_thread(jobs.cancel, str(identity))
                    status.update(
                        plain_text(f"Cancellation requested: {job.state}; status {job.status}.")
                    )
                elif action == "jobs-log":
                    log = await asyncio.to_thread(jobs.log, str(identity))
                    status.update(
                        plain_text(
                            f"CLI stderr: {log.shown_bytes}/{log.total_bytes} bytes; "
                            f"{log.omitted_bytes} omitted.\n{log.text}"
                        )
                    )
                elif action == "jobs-open":
                    status.update("Verifying saved artifacts and preparing index; no source scan.")
                    index = await asyncio.to_thread(collection_index, self.session, str(identity))
                    if self.is_mounted:
                        if isinstance(index, RuntimeIndex):
                            self.profile_selected(index)
                        else:
                            self.on_open(index)
        except (OSError, ValueError, sqlite3.Error) as exc:
            reason = "invalid job options" if isinstance(exc, ValidationError) else str(exc)
            status.update(plain_text(f"Collection action failed: {reason}"))
        finally:
            self.busy = False
            if self.is_mounted:
                self.refresh_jobs()
