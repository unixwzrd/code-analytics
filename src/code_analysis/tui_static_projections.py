"""Bounded terminal access to explicitly prepared static projection tables.

This is not a claim that every report projection has a terminal screen. Sparse
matrices are available only when ``static_browse`` prepares an explicit table
whose rows retain the authoritative identities, metrics, and evidence links.
"""

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

from code_analysis.artifact_viewer import ViewerCommand
from code_analysis.browse_index import BrowseIndex
from code_analysis.projection_models import ProjectionSelection
from code_analysis.projections import ProjectionName
from code_analysis.reports.projection_views import _scalar_text as scalar_text
from code_analysis.static_browse import (
    StaticProjectionLimitError,
    StaticProjectionPage,
    StaticProjectionRequest,
)
from code_analysis.subject_finding_browse import validate_indexed_finding_selection
from code_analysis.subject_findings import unscoped_scope_limitation
from code_analysis.tui_app import RowDetails, plain_text
from code_analysis.tui_subject_filters import scope_summary
from code_analysis.ui_decisions import DecisionSnapshot, read_index_decision_snapshot
from code_analysis.workspace import WorkspacePaths, open_workspace

FILTER_LABELS = {
    ProjectionName.CANDIDATE_WORKLIST: "Candidate policy",
    ProjectionName.SUBJECT_FINDINGS: "Finding filters",
}


def _filter_label(name: ProjectionName) -> str:
    """Name the one context-sensitive selection editor this projection owns."""

    return FILTER_LABELS.get(name, FILTER_LABELS[ProjectionName.CANDIDATE_WORKLIST])


class StaticProjectionBrowser(ModalScreen[int]):
    """Keyboard-first paged static projection table browser."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+n", "next", "Next page"),
        ("ctrl+p", "previous", "Previous page"),
        ("ctrl+e", "export", "Export page"),
        ("ctrl+f", "row_filters", "Filter rows"),
        ("ctrl+d", "attach_decisions", "Use decisions"),
        ("ctrl+g", "static_graphs", "Structure graphs"),
        ("f2", "status_details", "Projection status"),
    ]
    CSS = """
    StaticProjectionBrowser { align: center middle; }
    #static-projection-dialog { width: 95%; height: 90%; border: round $accent;
                                background: $surface; padding: 1; }
    #static-projection-controls, #static-projection-pagination,
    #static-projection-actions { height: 3; }
    #static-projection-name { width: 35; }
    #static-projection-search { width: 1fr; }
    #static-projection-attach-decisions { min-width: 0; width: 14; }
    #static-projection-clear-decisions { min-width: 0; width: 16; }
    #static-projection-decision-identity { min-width: 0; width: 18; }
    #static-projection-export { min-width: 0; width: 12; }
    #static-projection-close { min-width: 0; width: 8; }
    #static-projection-status, #static-projection-decisions { height: 1; }
    #static-projection-scope { height: auto; }
    #static-projection-records { height: 1fr; }
    """

    def __init__(
        self,
        index: BrowseIndex,
        on_status: Callable[[int], None],
        viewer: ViewerCommand | None = None,
        *,
        page_size: int = 25,
        workspace_root: Path | None = None,
        source_root: Path | None = None,
        initial_name: ProjectionName | None = None,
        initial_query: str = "",
    ) -> None:
        super().__init__()
        self.index = index
        self.on_status = on_status
        self.viewer = viewer
        self.page_size = page_size
        self.workspace_root = workspace_root
        self.source_root = source_root
        self.workspace: WorkspacePaths | None = None
        self.decisions: DecisionSnapshot | None = None
        available = tuple(item.name for item in index.manifest.static_projections)
        if not available:
            raise ValueError("browse index contains no prepared static projections")
        self.available = available
        if initial_name is not None and initial_name not in self.available:
            raise ValueError("requested static projection is not prepared in this index")
        self.request = StaticProjectionRequest(
            name=initial_name or available[0], limit=page_size, query=initial_query
        )
        self.page: StaticProjectionPage | None = None
        self.revision = 0
        self.error_status = 0

    def compose(self) -> ComposeResult:
        choices = tuple(
            (name.value.replace("-", " ").title(), name.value) for name in self.available
        )
        with Vertical(id="static-projection-dialog"):
            yield Static("Saved static evidence · Enter inspects a row; Ctrl+E exports this page")
            with Horizontal(id="static-projection-controls"):
                yield Select(
                    choices,
                    value=self.request.name.value,
                    allow_blank=False,
                    id="static-projection-name",
                )
                yield Input(
                    value=self.request.query,
                    placeholder="Search this projection; Enter applies",
                    max_length=4096,
                    id="static-projection-search",
                )
                yield Button("Apply", id="static-projection-apply")
            yield Static("Loading…", id="static-projection-status")
            yield Static(
                "Maintainer decisions are detached. Attach an owned saved ledger explicitly "
                "for the candidate worklist.",
                id="static-projection-decisions",
            )
            yield Static("", id="static-projection-scope")
            yield DataTable(id="static-projection-records", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="static-projection-pagination"):
                yield Button("Previous", id="static-projection-previous", disabled=True)
                yield Button("Next", id="static-projection-next", disabled=True)
                yield Button(
                    _filter_label(self.request.name),
                    id="static-projection-policy",
                    disabled=self.request.name not in FILTER_LABELS,
                )
                yield Button("Close", id="static-projection-close")
            with Horizontal(id="static-projection-actions"):
                yield Button(
                    "Use decisions",
                    id="static-projection-attach-decisions",
                    disabled=(
                        self.request.name is not ProjectionName.CANDIDATE_WORKLIST
                        or self.workspace_root is None
                    ),
                )
                yield Button(
                    "Clear decisions",
                    id="static-projection-clear-decisions",
                    disabled=True,
                )
                yield Button(
                    "Decision identity",
                    id="static-projection-decision-identity",
                    disabled=True,
                )
                yield Button("Export page", id="static-projection-export", disabled=True)
                yield Button("Graphs", id="static-projection-graphs")

    def on_mount(self) -> None:
        self._refresh_decision_controls()
        self.load_page()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.apply()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "static-projection-search":
            self.invalidate_page()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "static-projection-name" and event.value != Select.BLANK:
            self.apply()

    def apply(self) -> None:
        try:
            name = ProjectionName(str(self.query_one("#static-projection-name", Select).value))
            candidate_selection = (
                self.request.candidate_selection
                if name is ProjectionName.CANDIDATE_WORKLIST
                and self.request.name is ProjectionName.CANDIDATE_WORKLIST
                else (ProjectionSelection() if name is ProjectionName.CANDIDATE_WORKLIST else None)
            )
            # Search and paging retain the finding scope; switching projection clears it.
            finding_selection = (
                self.request.finding_selection or ProjectionSelection()
                if name is ProjectionName.SUBJECT_FINDINGS
                and self.request.name is ProjectionName.SUBJECT_FINDINGS
                else (ProjectionSelection() if name is ProjectionName.SUBJECT_FINDINGS else None)
            )
            snapshot = self.decisions if name is ProjectionName.CANDIDATE_WORKLIST else None
            self.request = StaticProjectionRequest(
                name=name,
                query=self.query_one("#static-projection-search", Input).value,
                limit=self.page_size,
                candidate_selection=candidate_selection,
                finding_selection=finding_selection,
                decision_ledger_sha256=snapshot.ledger_sha256 if snapshot is not None else None,
                decision_report_sha256=snapshot.report_sha256 if snapshot is not None else None,
            )
        except ValueError as exc:
            self.error_status = errno.EINVAL
            self.on_status(self.error_status)
            self.query_one("#static-projection-status", Static).update(plain_text(str(exc)))
            return
        if self.request.name is not ProjectionName.CANDIDATE_WORKLIST:
            self.detach_decisions()
        self._refresh_decision_controls()
        self.load_page()

    def action_row_filters(self) -> None:
        """Open the one selection editor this projection supports, or nothing."""

        if self.request.name is ProjectionName.CANDIDATE_WORKLIST:
            self.action_candidate_policy()
        elif self.request.name is ProjectionName.SUBJECT_FINDINGS:
            self.action_subject_filters()

    def action_subject_filters(self) -> None:
        if self.request.name is not ProjectionName.SUBJECT_FINDINGS:
            return
        from code_analysis.tui_subject_filters import SubjectFindingFilterEditor

        self.app.push_screen(
            SubjectFindingFilterEditor(self.request.finding_selection),
            self.subject_filters_closed,
        )

    def subject_filters_closed(self, selection: ProjectionSelection | None) -> None:
        if selection is None or self.request.name is not ProjectionName.SUBJECT_FINDINGS:
            return
        self.invalidate_page()
        try:
            # Reuse the backend's own validator: an unsupported dimension must fail
            # here rather than reach a read as a silently unfiltered page.
            validate_indexed_finding_selection(selection)
        except (TypeError, ValueError) as exc:
            self.error_status = errno.EINVAL
            self.on_status(self.error_status)
            self.query_one("#static-projection-status", Static).update(
                plain_text(f"Finding filters rejected: {exc}")
            )
            return
        self.request = self.request.model_copy(update={"offset": 0, "finding_selection": selection})
        self._refresh_scope()
        self.load_page()

    def action_candidate_policy(self) -> None:
        if self.request.name is not ProjectionName.CANDIDATE_WORKLIST:
            return
        from code_analysis.tui_candidate_policy import CandidatePolicyEditor

        self.app.push_screen(
            CandidatePolicyEditor(
                self.request.candidate_selection or ProjectionSelection(),
                decisions_attached=self.decisions is not None,
            ),
            self.candidate_policy_closed,
        )

    def candidate_policy_closed(self, selection: ProjectionSelection | None) -> None:
        if selection is None or self.request.name is not ProjectionName.CANDIDATE_WORKLIST:
            return
        self.invalidate_page()
        self.request = self._with_decision_snapshot(
            self.request.model_copy(update={"offset": 0, "candidate_selection": selection})
        )
        self.load_page()

    def _workspace(self) -> WorkspacePaths:
        if self.workspace is not None:
            return self.workspace
        if self.workspace_root is None:
            raise OSError(errno.ENODATA, "no owned workspace is selected")
        self.workspace = open_workspace(self.workspace_root)
        return self.workspace

    def _with_decision_snapshot(self, request: StaticProjectionRequest) -> StaticProjectionRequest:
        """Bind a worklist request to the one explicit snapshot, never implicitly."""
        if request.name is not ProjectionName.CANDIDATE_WORKLIST or self.decisions is None:
            selection = request.candidate_selection
            if selection is not None and selection.candidate_decisions:
                selection = selection.model_copy(update={"candidate_decisions": ()})
            return request.model_copy(
                update={
                    "candidate_selection": selection,
                    "decision_ledger_sha256": None,
                    "decision_report_sha256": None,
                }
            )
        return request.model_copy(
            update={
                "decision_ledger_sha256": self.decisions.ledger_sha256,
                "decision_report_sha256": self.decisions.report_sha256,
            }
        )

    def _refresh_scope(self) -> None:
        """State the active finding scope without hiding the unscoped disclosure."""

        scope = self.query_one("#static-projection-scope", Static)
        if self.request.name is not ProjectionName.SUBJECT_FINDINGS:
            scope.update("")
            return
        text = scope_summary(self.request.finding_selection) + "."
        page = self.page
        if page is not None and page.unscoped_finding_count is not None:
            text += " " + (
                unscoped_scope_limitation(page.unscoped_finding_count)
                if page.unscoped_finding_count
                else "No filtered finding lacks a subject identity."
            )
        scope.update(plain_text(text))

    def _refresh_decision_controls(self) -> None:
        worklist = self.request.name is ProjectionName.CANDIDATE_WORKLIST
        policy = self.query_one("#static-projection-policy", Button)
        policy.label = _filter_label(self.request.name)
        policy.disabled = self.request.name not in FILTER_LABELS
        self._refresh_scope()
        self.query_one("#static-projection-attach-decisions", Button).disabled = (
            not worklist or self.workspace_root is None
        )
        self.query_one("#static-projection-clear-decisions", Button).disabled = (
            not worklist or self.decisions is None
        )
        self.query_one("#static-projection-decision-identity", Button).disabled = (
            not worklist or self.decisions is None
        )
        status = self.query_one("#static-projection-decisions", Static)
        if self.decisions is None:
            status.update(
                plain_text(
                    "Maintainer decisions are detached. "
                    + (
                        "No owned workspace is selected."
                        if self.workspace_root is None
                        else "Use decisions to attach the selected workspace ledger."
                    )
                )
            )
            return
        status.update(
            plain_text(
                "Using explicit read-only decisions: "
                f"{len(self.decisions.ledger.records)} records; Decision identity shows "
                "both SHA-256 values."
            )
        )

    def action_decision_identity(self) -> None:
        if self.decisions is None:
            return
        self.app.push_screen(
            RowDetails(
                json.dumps(
                    {
                        "report_sha256": self.decisions.report_sha256,
                        "ledger_sha256": self.decisions.ledger_sha256,
                        "record_count": len(self.decisions.ledger.records),
                    },
                    indent=2,
                ),
                "Explicit read-only decision snapshot identity",
            )
        )

    def action_attach_decisions(self) -> None:
        if self.request.name is not ProjectionName.CANDIDATE_WORKLIST:
            return
        # Discard the old page before touching the owned ledger.  A failed
        # attachment must not leave a previously unbound page exportable.
        self.detach_decisions()
        self.attach_decisions()

    @work(exclusive=True, group="static-projection-decisions")
    async def attach_decisions(self) -> None:
        revision = self.revision
        try:
            snapshot = await asyncio.to_thread(self._read_snapshot_from_workspace)
            if snapshot is None:
                raise OSError(errno.ENODATA, "the selected workspace has no saved decision ledger")
        except OSError as exc:
            if not self.is_mounted or revision != self.revision:
                return
            self.error_status = exc.errno or errno.EIO
            self.on_status(self.error_status)
            self.query_one("#static-projection-decisions", Static).update(
                plain_text("Decision ledger unavailable or invalid; no ledger was attached.")
            )
            return
        if not self.is_mounted or revision != self.revision:
            return
        self.decisions = snapshot
        self.request = self._with_decision_snapshot(self.request.model_copy(update={"offset": 0}))
        self._refresh_decision_controls()
        self.load_page()

    def detach_decisions(self, message: str | None = None) -> None:
        """Drop the explicit attachment and every decision filter before another read."""
        selection = self.request.candidate_selection
        if selection is not None and selection.candidate_decisions:
            selection = selection.model_copy(update={"candidate_decisions": ()})
        self.decisions = None
        self.request = self.request.model_copy(
            update={
                "offset": 0,
                "candidate_selection": selection,
                "decision_ledger_sha256": None,
                "decision_report_sha256": None,
            }
        )
        self.invalidate_page()
        if self.is_mounted:
            self._refresh_decision_controls()
            if message is not None:
                self.query_one("#static-projection-status", Static).update(plain_text(message))

    def action_clear_decisions(self) -> None:
        if self.decisions is None:
            return
        self.detach_decisions(
            "Decision ledger detached; select it again before decision-aware browsing."
        )
        self.load_page()

    def _verified_decisions(self, request: StaticProjectionRequest) -> DecisionSnapshot | None:
        if self.decisions is None:
            return None
        current = read_index_decision_snapshot(self.index.manifest, self._workspace())
        if current is None:
            raise OSError(
                errno.EBADMSG, "attached decision ledger is no longer available; reselect it"
            )
        if (
            current.report_sha256 != self.decisions.report_sha256
            or current.ledger_sha256 != self.decisions.ledger_sha256
            or request.decision_report_sha256 != current.report_sha256
            or request.decision_ledger_sha256 != current.ledger_sha256
        ):
            raise OSError(errno.EBADMSG, "attached decision ledger changed; reselect it")
        return current

    def _read_snapshot_from_workspace(self) -> DecisionSnapshot | None:
        """Blocking owned-metadata read for a worker thread only."""
        return read_index_decision_snapshot(self.index.manifest, self._workspace())

    def invalidate_page(self) -> None:
        """Do not export a page once controls no longer describe its request."""

        self.revision += 1
        self.workers.cancel_group(self, "static-candidate-packet")
        self.page = None
        if self.is_mounted:
            self.query_one("#static-projection-records", DataTable).clear(columns=True)
            for item in ("next", "previous", "export"):
                self.query_one(f"#static-projection-{item}", Button).disabled = True
            # The recomputed unscoped count described the discarded page only.
            self._refresh_scope()

    @work(exclusive=True, group="static-projection")
    async def load_page(self) -> None:
        self.workers.cancel_group(self, "static-candidate-packet")
        self.revision += 1
        revision, request = self.revision, self.request
        self.page = None
        table = self.query_one("#static-projection-records", DataTable)
        table.clear(columns=True)
        for item in ("next", "previous", "export"):
            self.query_one(f"#static-projection-{item}", Button).disabled = True
        self._refresh_scope()
        self.query_one("#static-projection-status", Static).update(
            "Loading bounded static projection page…"
        )
        try:
            decisions = await asyncio.to_thread(self._verified_decisions, request)
            page = await asyncio.to_thread(
                self.index.static_projection, request, decisions=decisions
            )
        except OSError as exc:
            status = exc.errno or errno.EIO
            if self.is_mounted and revision == self.revision and request == self.request:
                self.error_status = status
                self.on_status(status)
                if request.decision_ledger_sha256 is not None:
                    self.detach_decisions(
                        "Decision ledger changed; attach it again before browsing."
                    )
                else:
                    self.query_one("#static-projection-status", Static).update(
                        plain_text(f"Index unavailable: {exc}")
                    )
            return
        except StaticProjectionLimitError as exc:
            if self.is_mounted and revision == self.revision and request == self.request:
                self.error_status = errno.ENODATA
                self.on_status(self.error_status)
                self.query_one("#static-projection-status", Static).update(
                    plain_text(f"Prepared page exceeds its display bound: {exc}")
                )
            return
        except (ValueError, sqlite3.Error) as exc:
            if self.is_mounted and revision == self.revision and request == self.request:
                self.error_status = errno.EBADMSG
                self.on_status(self.error_status)
                if request.decision_ledger_sha256 is not None:
                    self.detach_decisions(
                        "Decision ledger changed; attach it again before browsing."
                    )
                else:
                    self.query_one("#static-projection-status", Static).update(
                        plain_text(f"Projection unavailable: {exc}")
                    )
            return
        if not self.is_mounted or revision != self.revision or request != self.request:
            return
        self.page = page
        self._refresh_scope()
        table.add_columns(*(column.label for column in page.summary.columns))
        for number, row in enumerate(page.records):
            table.add_row(*(plain_text(scalar_text(cell)) for cell in row.cells), key=str(number))
        source_partial = (
            page.summary.source_truncated or page.summary.completeness.value != "complete"
        )
        if source_partial:
            self.error_status = errno.ENODATA
            self.on_status(self.error_status)
        first = page.request.offset + 1 if page.retained else 0
        self.query_one("#static-projection-status", Static).update(
            plain_text(
                f"Source {page.summary.completeness.value}; truncated "
                f"{page.summary.source_truncated}; {first}-{page.request.offset + page.retained} "
                f"of {page.eligible} filtered. "
                f"{'More pages available.' if page.truncated else 'End of filtered results.'} "
                "F2 shows title and limitations."
            )
        )
        self.query_one("#static-projection-next", Button).disabled = not page.truncated
        self.query_one("#static-projection-previous", Button).disabled = page.request.offset == 0
        self.query_one("#static-projection-export", Button).disabled = False
        table.focus()

    def action_status_details(self) -> None:
        if self.page is None:
            return
        page = self.page
        self.app.push_screen(
            RowDetails(
                json.dumps(
                    {
                        "title": page.summary.title,
                        "completeness": page.summary.completeness.value,
                        "source_truncated": page.summary.source_truncated,
                        "page_truncated": page.truncated,
                        "eligible": page.eligible,
                        "retained": page.retained,
                        "prepared_records": page.summary.records,
                        "unscoped_finding_count": page.unscoped_finding_count,
                        "prepared_unscoped_finding_count": page.summary.unscoped_finding_count,
                        "limitations": page.summary.limitations,
                        "request": page.request.model_dump(mode="json"),
                    },
                    indent=2,
                ),
                "Saved static projection status and limitations",
            )
        )

    def action_static_graphs(self) -> None:
        from code_analysis.tui_static_graphs import StaticGraphBrowser

        graph_names = {item.name for item in self.index.manifest.static_graphs}
        initial_name = self.request.name if self.request.name in graph_names else None
        self.app.push_screen(
            StaticGraphBrowser(
                self.index,
                self.on_status,
                self.viewer,
                source_root=self.source_root,
                initial_name=initial_name,
            )
        )

    def action_next(self) -> None:
        if self.page is not None and self.page.truncated:
            self.workers.cancel_group(self, "static-candidate-packet")
            self.request = self.request.model_copy(
                update={"offset": self.request.offset + self.request.limit}
            )
            self.load_page()

    def action_previous(self) -> None:
        if self.page is not None and self.request.offset:
            self.workers.cancel_group(self, "static-candidate-packet")
            self.request = self.request.model_copy(
                update={"offset": max(0, self.request.offset - self.request.limit)}
            )
            self.load_page()

    def action_export(self) -> None:
        if self.page is not None and self.page.request == self.request:
            from code_analysis.tui_export import ExportPage

            self.app.push_screen(
                ExportPage(
                    self.index,
                    self.request,
                    self.on_status,
                    self.viewer,
                    workspace=self.workspace,
                    decisions=self.decisions,
                )
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if self.page is None:
            return
        row_key = event.row_key.value
        if row_key is None:
            return
        row = self.page.records[int(row_key)]
        if self.request.name is ProjectionName.CANDIDATE_WORKLIST:
            self.load_candidate_packet(row.row_id)
            return
        payload = row.model_dump(mode="json")
        self.app.push_screen(
            RowDetails(
                json.dumps(
                    {"row": payload, "evidence_ids": row.evidence_ids},
                    indent=2,
                    ensure_ascii=False,
                ),
                "Static projection row — recorded values and evidence IDs only; "
                "source is not opened automatically.",
            )
        )

    @work(exclusive=True, group="static-candidate-packet")
    async def load_candidate_packet(self, identity: str) -> None:
        """Open exactly the selected indexed candidate packet after snapshot validation."""
        revision, request = self.revision, self.request
        page = self.page
        if (
            request.name is not ProjectionName.CANDIDATE_WORKLIST
            or page is None
            or page.request != request
            or identity not in {row.row_id for row in page.records}
        ):
            return
        try:
            await asyncio.to_thread(self._verified_decisions, request)
            packet = await asyncio.to_thread(self.index.candidate_packet, identity)
        except (OSError, ValueError, sqlite3.Error) as exc:
            if (
                self.is_mounted
                and self.app.screen is self
                and revision == self.revision
                and request == self.request
            ):
                self.error_status = errno.EBADMSG
                self.on_status(self.error_status)
                if request.decision_ledger_sha256 is not None:
                    self.detach_decisions(
                        f"Candidate packet unavailable: {exc}. Reselect workspace decisions "
                        "before retrying."
                    )
                else:
                    self.query_one("#static-projection-status", Static).update(
                        plain_text(f"Candidate packet unavailable: {exc}")
                    )
            return
        if (
            not self.is_mounted
            or self.app.screen is not self
            or revision != self.revision
            or request != self.request
            or self.page is not page
        ):
            return
        from code_analysis.tui_candidate import CandidatePacket

        self.app.push_screen(
            CandidatePacket(self.index, packet, self.on_status), self.candidate_packet_closed
        )

    def candidate_packet_closed(self, status: object) -> None:
        """Packet continuation cannot replace this worklist's policy or page."""
        if isinstance(status, int) and not isinstance(status, bool) and status:
            self.on_status(status)

    def action_close(self) -> None:
        self.workers.cancel_group(self, "static-projection")
        self.workers.cancel_group(self, "static-projection-decisions")
        self.workers.cancel_group(self, "static-candidate-packet")
        self.dismiss(self.error_status)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        actions = {
            "static-projection-apply": self.apply,
            "static-projection-policy": self.action_row_filters,
            "static-projection-attach-decisions": self.action_attach_decisions,
            "static-projection-clear-decisions": self.action_clear_decisions,
            "static-projection-decision-identity": self.action_decision_identity,
            "static-projection-next": self.action_next,
            "static-projection-previous": self.action_previous,
            "static-projection-export": self.action_export,
            "static-projection-graphs": self.action_static_graphs,
            "static-projection-close": self.action_close,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()
