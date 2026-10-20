"""Explicit export dialog; file publication and projection semantics remain backend-owned."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Select, Static

from code_analysis.artifact_viewer import ViewerCommand, artifact_digest, open_artifact
from code_analysis.browse_export import (
    BrowseExportLimitError,
    BrowseExportSelectionError,
    export_browse_page,
    render_browse_artifact,
)
from code_analysis.browse_index import BrowseIndex, index_directory
from code_analysis.browse_index_models import BrowseRequest
from code_analysis.comparison_browse import ComparisonPageRequest
from code_analysis.dependency_graph import DependencyGraphRequest
from code_analysis.models import Completeness
from code_analysis.reports.projection_views import ProjectionFormat
from code_analysis.runtime_browse_models import RuntimeBrowseRequest
from code_analysis.runtime_comparison_browse import RuntimeComparisonRequest
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_index import RuntimeIndex
from code_analysis.static_browse import StaticProjectionLimitError, StaticProjectionRequest
from code_analysis.static_graph_browse import StaticGraphRequest
from code_analysis.tui_app import plain_text
from code_analysis.ui_decisions import DecisionSnapshot, read_index_decision_snapshot
from code_analysis.workspace import WorkspacePaths


class ExportPage(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "close", "Close")]
    CSS = """
    ExportPage { align: center middle; }
    #export-dialog { width: 90%; height: auto; border: round $accent;
                     background: $surface; padding: 1; }
    #export-status { height: auto; }
    """

    def __init__(
        self,
        index: BrowseIndex | RuntimeIndex,
        request: (
            BrowseRequest
            | RuntimeBrowseRequest
            | RuntimeComparisonRequest
            | RuntimeGraphRequest
            | DependencyGraphRequest
            | StaticProjectionRequest
            | StaticGraphRequest
            | ComparisonPageRequest
        ),
        on_status: Callable[[int], None],
        viewer: ViewerCommand | None = None,
        *,
        workspace: WorkspacePaths | None = None,
        decisions: DecisionSnapshot | None = None,
    ) -> None:
        super().__init__()
        self.index = index
        self.request = request
        self.on_status = on_status
        self.writing = False
        self.viewer = viewer
        self.workspace = workspace
        self.decisions = decisions
        self.exported_path: Path | None = None
        self.exported_sha256: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="export-dialog"):
            yield Static("Export this filtered page — existing files are never overwritten")
            yield Static(plain_text(self.request.model_dump_json()))
            yield Input(
                placeholder="Artifact name (letters, digits, hyphens, underscores)",
                id="export-name",
                max_length=80,
            )
            formats = [
                ("JSON (canonical)", "json-compact"),
                ("TSV (import as text)", "tsv"),
                ("Markdown", "markdown"),
            ]
            if isinstance(
                self.request, (RuntimeGraphRequest, DependencyGraphRequest, StaticGraphRequest)
            ):
                formats = [
                    ("JSON (canonical)", "json-compact"),
                    ("Markdown", "markdown"),
                    ("DOT", "dot"),
                    ("Mermaid", "mermaid"),
                    ("SVG", "svg"),
                    ("PNG", "png"),
                    ("HTML", "html"),
                ]
            yield Select(
                formats,
                value="json-compact",
                allow_blank=False,
                id="export-format",
            )
            request = self.request
            graph_request = (
                request
                if isinstance(
                    request, (RuntimeGraphRequest, DependencyGraphRequest, StaticGraphRequest)
                )
                else None
            )
            bound = (
                500
                if isinstance(
                    request, (BrowseRequest, StaticProjectionRequest, ComparisonPageRequest)
                )
                else 100
            )
            scope = (
                f"Selected graph is bounded to {graph_request.max_nodes} nodes and "
                f"{graph_request.max_edges} edges."
                if graph_request is not None
                else f"Selected scope is bounded to {bound} rows."
            )
            yield Checkbox(
                (
                    f"Graph bounds: {graph_request.max_nodes} nodes / "
                    f"{graph_request.max_edges} edges"
                    if graph_request is not None
                    else f"Export selected rows from start (up to {bound})"
                ),
                id="export-filtered",
                disabled=graph_request is not None,
            )
            yield Static(
                "Saved under this index's exports directory. " + scope,
                id="export-status",
            )
            yield Button("Export page", id="export-write")
            yield Button("Open exported artifact", id="export-open", disabled=True)
            yield Button("Close", id="export-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "export-write" and not self.writing:
            self.write_export()
        elif event.button.id == "export-close":
            self.action_close()
        elif event.button.id == "export-open" and not self.writing:
            self.open_export()

    def action_close(self) -> None:
        if not self.writing:
            self.dismiss()

    @work(group="export")
    async def write_export(self) -> None:
        self.writing = True
        self.query_one("#export-write", Button).disabled = True
        self.query_one("#export-open", Button).disabled = True
        self.exported_path = None
        self.exported_sha256 = None
        try:
            request = self.request
            if self.query_one("#export-filtered", Checkbox).value and not isinstance(
                request, (RuntimeGraphRequest, DependencyGraphRequest, StaticGraphRequest)
            ):
                bound = (
                    500
                    if isinstance(
                        request, (BrowseRequest, StaticProjectionRequest, ComparisonPageRequest)
                    )
                    else 100
                )
                request = request.model_copy(update={"offset": 0, "limit": bound})
            output_format = ProjectionFormat(str(self.query_one("#export-format", Select).value))
            decisions = None
            if isinstance(request, StaticProjectionRequest) and self.decisions is not None:
                if self.workspace is None or not isinstance(self.index, BrowseIndex):
                    raise OSError(
                        errno.EBADMSG, "attached decision ledger has no owned index workspace"
                    )
                decisions = await asyncio.to_thread(
                    read_index_decision_snapshot, self.index.manifest, self.workspace
                )
                if (
                    decisions is None
                    or decisions.report_sha256 != self.decisions.report_sha256
                    or decisions.ledger_sha256 != self.decisions.ledger_sha256
                    or request.decision_report_sha256 != decisions.report_sha256
                    or request.decision_ledger_sha256 != decisions.ledger_sha256
                ):
                    raise OSError(
                        errno.EBADMSG,
                        "attached decision ledger changed; close this dialog and reselect it",
                    )
            arguments = (
                self.index,
                request,
                self.query_one("#export-name", Input).value,
                output_format,
            )
            if decisions is None:
                path, projection = await asyncio.to_thread(export_browse_page, *arguments)
            else:
                path, projection = await asyncio.to_thread(
                    export_browse_page, *arguments, decisions=decisions
                )
            rendered = await asyncio.to_thread(render_browse_artifact, projection, output_format)
            digest = hashlib.sha256(rendered).hexdigest()
            if await asyncio.to_thread(artifact_digest, path) != digest:
                raise OSError(errno.EBADMSG, "exported artifact changed after publication")
        except (ValueError, OSError, sqlite3.Error) as exc:
            status = errno.EBADMSG
            if isinstance(exc, OSError):
                status = exc.errno or errno.EIO
            elif isinstance(exc, BrowseExportSelectionError):
                status = errno.EINVAL
            elif isinstance(exc, (BrowseExportLimitError, StaticProjectionLimitError)):
                status = errno.ENODATA
            self.on_status(status)
            if self.is_mounted:
                self.query_one("#export-status", Static).update(
                    plain_text(f"Export failed ({status}): {exc}")
                )
        else:
            self.exported_path = path
            self.exported_sha256 = digest
            if projection.completeness is not Completeness.COMPLETE or projection.truncated:
                self.on_status(errno.ENODATA)
            if self.is_mounted:
                self.query_one("#export-status", Static).update(
                    plain_text(
                        f"Saved {path}\n{projection.record_count} rows; "
                        f"{projection.completeness.value}; "
                        f"{projection.truncation_reason or 'No filtered rows omitted.'}"
                    )
                )
        finally:
            self.writing = False
            if self.is_mounted:
                self.query_one("#export-write", Button).disabled = False
                self.query_one("#export-open", Button).disabled = self.exported_path is None

    @work(group="export")
    async def open_export(self) -> None:
        path, digest = self.exported_path, self.exported_sha256
        if path is None or digest is None:
            return
        self.writing = True
        self.query_one("#export-open", Button).disabled = True
        self.query_one("#export-write", Button).disabled = True
        try:
            root = await asyncio.to_thread(index_directory, self.index.database.parent)
            if path.parent != root / "exports" or path.parent.is_symlink():
                raise ValueError("artifact is outside the owned export directory")
            opened = await asyncio.to_thread(open_artifact, path, digest, self.viewer)
            message = (
                f"Viewer launch request completed: {path}"
                if opened
                else f"No viewer configured. Open this path explicitly: {path}"
            )
        except (ValueError, OSError) as exc:
            status = exc.errno or errno.EIO if isinstance(exc, OSError) else errno.EBADMSG
            self.on_status(status)
            message = f"Viewer unavailable ({status}): {exc}\nArtifact: {path}"
        finally:
            self.writing = False
            if self.is_mounted:
                self.query_one("#export-open", Button).disabled = False
                self.query_one("#export-write", Button).disabled = False
        if self.is_mounted:
            self.query_one("#export-status", Static).update(plain_text(message))
