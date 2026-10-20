"""Keyboard-first indexed tables. No source report or analyzed code is loaded here."""

from __future__ import annotations

import asyncio
import errno
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Literal

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Static

from code_analysis.artifact_viewer import ViewerCommand
from code_analysis.browse_index import BrowseIndex, BrowsePage
from code_analysis.browse_index_models import BrowseRequest
from code_analysis.clone_browsing import ClonePage, CloneRow
from code_analysis.collection_service import CollectionSession
from code_analysis.contract_browsing import ContractPage, ContractRow, ShapePage, ShapeRow
from code_analysis.models import Completeness, Confidence, SourceLanguage
from code_analysis.projection_models import ProjectionFindingCategory
from code_analysis.projections import ProjectionName
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_index import RuntimeIndex
from code_analysis.static_graph_browse import StaticGraphRequest
from code_analysis.test_browsing import TestImpactPage, TestImpactRow
from code_analysis.ui_models import UIFindingRow, UIStateEffectPage, UIStateEffectRow
from code_analysis.ui_rows import BrowseTable

WebHandoffKind = Literal["report", "finding", "symbol", "graph", "projection"]


def plain_text(value: str) -> Text:
    """Keep report text literal; never emit terminal escape or bidi control codes."""
    return Text(
        "".join(
            char if char in "\n\t" or char.isprintable() else f"\\u{ord(char):04x}"
            for char in value
        )
    )


class RowDetails(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "dismiss", "Close")]
    CSS = """
    RowDetails { align: center middle; }
    #row-dialog { width: 90%; height: 80%; border: round $accent;
                  background: $surface; padding: 1; }
    """

    def __init__(
        self,
        detail: str,
        title: str = "Indexed row details — not a full evidence/review packet",
        source_action: Callable[[], None] | None = None,
        web_action: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.detail = detail
        self.detail_title = title
        self.source_action = source_action
        self.web_action = web_action

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="row-dialog"):
            yield Static(plain_text(self.detail_title))
            if self.source_action is not None:
                yield Button("Preview source", id="preview-source")
            if self.web_action is not None:
                yield Button("Open in Web UI", id="row-web")
            yield Static(plain_text(self.detail), id="row-detail")
            yield Button("Close (Escape)", id="close-detail")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "preview-source" and self.source_action is not None:
            self.source_action()
        elif event.button.id == "row-web" and self.web_action is not None:
            self.web_action()
        else:
            self.dismiss()


class AnalysisTerminal(App[int]):
    TITLE = "Code Analysis Suite"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "finish", "Quit", show=False, priority=True),
        ("f1", "overview", "Overview"),
        ("f2", "table('modules')", "Modules"),
        ("f3", "table('findings')", "Findings"),
        ("f4", "table('candidates')", "Candidates"),
        ("f5", "table('providers')", "Providers"),
        ("f6", "table('diagnostics')", "Diagnostics"),
        ("f7", "comparison", "Comparison"),
        ("f8", "table('state-effects')", "State/effects"),
        ("f9", "table('clones')", "Clones"),
        ("f10", "table('contracts')", "Contracts"),
        ("f11", "table('dictionary-shapes')", "Dictionary shapes"),
        ("f12", "table('test-impact')", "Test impact"),
        ("ctrl+r", "runtime", "Runtime"),
        ("ctrl+o", "runs", "Saved runs"),
        ("ctrl+n", "next_page", "Next page"),
        ("ctrl+p", "previous_page", "Previous page"),
        ("ctrl+f", "filter", "Filter"),
        ("ctrl+e", "export", "Export page"),
        ("ctrl+w", "web_handoff", "Open in Web UI"),
        ("ctrl+l", "collection", "Collect / plan"),
        ("ctrl+g", "static_projections", "Static views"),
        ("ctrl+q", "finish", "Quit"),
    ]
    CSS = """
    #identity { height: auto; padding: 0 1; }
    #controls { height: 3; }
    #search { width: 1fr; }
    #severity { width: 20; }
    #finding-filters { height: 3; }
    #finding-category, #finding-confidence { width: 1fr; }
    #facet-filters { height: 3; }
    #facet-language { width: 20; }
    #facet-module, #facet-owner { width: 1fr; }
    #apply { min-width: 9; width: 9; }
    #overview { height: 1fr; padding: 1 2; }
    #records { height: 1fr; }
    #page-status { height: auto; padding: 0 1; }
    #pagination { height: 3; }
    #review-worklist { width: 18; }
    """

    def __init__(
        self,
        index: BrowseIndex,
        *,
        page_size: int = 25,
        source_root: Path | None = None,
        viewer: ViewerCommand | None = None,
        workspace: Path | None = None,
        collection_root: Path | None = None,
        collection_session: CollectionSession | None = None,
        collection_pythons: tuple[Path, ...] = (),
        browser: ViewerCommand | None = None,
        report_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.index = index
        self.preview_root = source_root
        self.viewer = viewer
        self.workspace_root = workspace
        self.collection_root = collection_root
        self.collection_session = collection_session
        self.collection_pythons = collection_pythons
        self.browser = browser
        self.report_path = report_path
        from code_analysis.ui_handoff import OwnedWebHandoff

        self.web_handoff = OwnedWebHandoff()
        self.selection = BrowseRequest(table=BrowseTable.MODULES, limit=page_size)
        self.current_page: BrowsePage | None = None
        self.showing_overview = True
        self.revision = 0
        self.exit_status = (
            0
            if index.bootstrap().reports[0].completeness is Completeness.COMPLETE
            else errno.ENODATA
        )
        comparison = index.manifest.comparison
        if comparison is not None and (
            comparison.completeness is not Completeness.COMPLETE or comparison.omitted
        ):
            self.exit_status = errno.ENODATA
        if any(
            item.completeness is not Completeness.COMPLETE or item.source_truncated
            for item in index.manifest.static_projections
        ):
            self.exit_status = errno.ENODATA

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        source = self.index.bootstrap().reports[0]
        yield Static(
            plain_text(
                f"Source: {source.completeness.value} · {source.command} · {source.report_sha256}"
            ),
            id="identity",
        )
        yield Select(
            [
                ("Overview", "overview"),
                *((item.value.title(), item.value) for item in BrowseTable),
                ("Static views", "static-projections"),
                ("Comparison", "comparison"),
                ("Runtime", "runtime"),
            ],
            value="overview",
            allow_blank=False,
            id="view-choice",
        )
        with Horizontal(id="controls"):
            yield Input(
                placeholder="Search this table; Enter applies", id="search", max_length=4096
            )
            yield Select(
                [
                    ("All severities", ""),
                    ("Error", "error"),
                    ("Warning", "warning"),
                    ("Info", "info"),
                ],
                value="",
                allow_blank=False,
                id="severity",
            )
            yield Button("Apply", id="apply")
        with Horizontal(id="finding-filters"):
            yield Select(
                [("All categories", "")], value="", allow_blank=False, id="finding-category"
            )
            yield Select(
                [("All confidence levels", "")],
                value="",
                allow_blank=False,
                id="finding-confidence",
            )
        with Horizontal(id="facet-filters"):
            yield Select(
                [
                    ("All languages", ""),
                    ("Python", "python"),
                    ("JavaScript", "javascript"),
                    ("Ruby", "ruby"),
                ],
                value="",
                allow_blank=False,
                id="facet-language",
            )
            yield Input(placeholder="Logical module prefix", id="facet-module", max_length=4096)
            yield Input(placeholder="Exact saved owner", id="facet-owner", max_length=4096)
        with VerticalScroll(id="overview"):
            yield Static(plain_text(self.overview_text()), id="overview-text")
        yield DataTable(id="records", cursor_type="row", zebra_stripes=True)
        yield Static("Overview", id="page-status")
        with Horizontal(id="pagination"):
            yield Button("Previous", id="previous", disabled=True)
            yield Button("Next", id="next", disabled=True)
            yield Button("Review worklist", id="review-worklist", disabled=True)
            yield Button("Export page", id="export")
        yield Footer()

    def on_mount(self) -> None:
        self.action_overview()

    def overview_text(self) -> str:
        overview = self.index.bootstrap()
        text = "\n".join(f"{count.label}: {count.value}" for count in overview.counts)
        comparison = self.index.manifest.comparison
        if comparison is not None:
            text += (
                f"\nComparison: {comparison.completeness.value}; "
                f"{comparison.omitted} records omitted; F7 shows both report identities."
            )
        text += "\n\nSource limitations\n" + "\n".join(overview.limitations)
        text += "\n\nF2-F12 opens indexed tables. Tab moves focus; Enter inspects a row."
        text += (
            "\nSource previews require --source-root. F7 opens prepared comparisons. "
            "Ctrl+R opens saved runtime evidence; Ctrl+O selects owned saved runs. "
            "Ctrl+W prepares an exact read-only Web handoff."
        )
        return text

    def action_overview(self) -> None:
        self.revision += 1
        self.showing_overview = True
        self.query_one("#view-choice", Select).value = "overview"
        self.workers.cancel_group(self, "page")
        for identity in (
            "controls",
            "finding-filters",
            "facet-filters",
            "records",
            "pagination",
        ):
            self.query_one(f"#{identity}").display = False
        self.query_one("#overview").display = True
        self.query_one("#page-status", Static).update(
            "Overview — source completeness is not changed by paging"
        )

    def action_table(self, table: str) -> None:
        self.showing_overview = False
        self.current_page = None
        self.selection = BrowseRequest(table=BrowseTable(table), limit=self.selection.limit)
        self.query_one("#view-choice", Select).value = table
        self.query_one("#search", Input).value = ""
        self.query_one("#severity", Select).value = ""
        self.query_one("#facet-language", Select).value = ""
        self.query_one("#facet-module", Input).value = ""
        self.query_one("#facet-owner", Input).value = ""
        self.query_one("#search", Input).disabled = table == "providers"
        self.query_one("#severity", Select).disabled = table != "findings"
        self.query_one("#finding-filters").display = table == "findings"
        supports_facets = table in {
            "modules",
            "findings",
            "state-effects",
            "contracts",
            "dictionary-shapes",
            "candidates",
            "clones",
            "test-impact",
        }
        self.query_one("#facet-filters").display = supports_facets
        self.query_one("#facet-owner", Input).display = table in {
            "state-effects",
            "contracts",
            "dictionary-shapes",
        }
        has_worklist = ProjectionName.CANDIDATE_WORKLIST in {
            item.name for item in self.index.manifest.static_projections
        }
        review = self.query_one("#review-worklist", Button)
        review.display = table == BrowseTable.CANDIDATES.value and has_worklist
        review.disabled = not (table == BrowseTable.CANDIDATES.value and has_worklist)
        for identity, label, choices in (
            ("finding-category", "All categories", self.index.manifest.overview.finding_categories),
            (
                "finding-confidence",
                "All confidence levels",
                self.index.manifest.overview.finding_confidences,
            ),
        ):
            widget = self.query_one(f"#{identity}", Select)
            widget.set_options([(label, ""), *((item.value, item.value) for item in choices)])
            widget.value = ""
        self.query_one("#overview").display = False
        for identity in ("controls", "records", "pagination"):
            self.query_one(f"#{identity}").display = True
        self.load_page()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "view-choice":
            return
        value = str(event.value)
        if value == "comparison":
            self.action_comparison()
        elif value == "runtime":
            self.action_runtime()
        elif value == "static-projections":
            self.action_static_projections()
        elif value == "overview":
            if not self.showing_overview:
                self.action_overview()
        elif self.showing_overview or value != self.selection.table.value:
            self.action_table(value)

    def action_runs(self) -> None:
        if len(self.screen_stack) == 1:
            from code_analysis.tui_runs import RunPicker

            self.push_screen(
                RunPicker(self.workspace_root, self.evidence_closed), self.run_selected
            )

    def action_collection(self) -> None:
        if len(self.screen_stack) != 1:
            return
        if self.collection_root is None:
            self.query_one("#page-status", Static).update(
                "Collection planning requires explicit --collection-root PATH."
            )
            return
        from code_analysis.collection_commands import CollectionPlanner
        from code_analysis.tui_collection import CollectionPlan

        try:
            planner = (
                self.collection_session.planner
                if self.collection_session is not None
                else CollectionPlanner(self.collection_root, interpreters=self.collection_pythons)
            )
        except (ValueError, OSError) as exc:
            self.query_one("#page-status", Static).update(plain_text(str(exc)))
            return
        self.push_screen(CollectionPlan(planner, self.collection_session), self.collection_selected)

    def collection_selected(self, index: BrowseIndex | None) -> None:
        if index is not None:
            if self.collection_session is not None:
                self.workspace_root = self.collection_session.selected().workspace.root
            self.run_selected(index)

    def run_selected(self, index: BrowseIndex | RuntimeIndex | None) -> None:
        if not isinstance(index, BrowseIndex):
            return
        previous_report = self.index.bootstrap().reports[0].report_sha256
        selected_report = index.bootstrap().reports[0].report_sha256
        if selected_report != previous_report:
            self.report_path = None
        self.revision += 1
        self.workers.cancel_group(self, "page")
        self.index = index
        self.current_page = None
        self.selection = BrowseRequest(table=BrowseTable.MODULES, limit=self.selection.limit)
        if index.manifest.overview.reports[0].completeness is not Completeness.COMPLETE or (
            index.manifest.comparison is not None
            and index.manifest.comparison.completeness is not Completeness.COMPLETE
        ):
            self.exit_status = errno.ENODATA
        if any(
            item.completeness is not Completeness.COMPLETE or item.source_truncated
            for item in index.manifest.static_projections
        ):
            self.exit_status = errno.ENODATA
        source = index.bootstrap().reports[0]
        self.query_one("#identity", Static).update(
            plain_text(
                f"Source: {source.completeness.value} · {source.command} · {source.report_sha256}"
            )
        )
        self.query_one("#overview-text", Static).update(plain_text(self.overview_text()))
        self.action_overview()
        runtime = index.manifest.runtime
        if runtime is not None and (
            not runtime.overview.source_matches
            or runtime.overview.completeness is not Completeness.COMPLETE
            or (
                runtime.comparison is not None
                and (
                    not runtime.comparison.candidate_overview.source_matches
                    or runtime.comparison.candidate_overview.completeness
                    is not Completeness.COMPLETE
                    or any(
                        item.outcome != "matched" for item in runtime.comparison.header.decisions
                    )
                )
            )
        ):
            self.exit_status = errno.ENODATA
        self.query_one("#page-status", Static).update(
            "Selected saved run with explicit runtime profile; Ctrl+R shows binding and health."
            if runtime is not None
            else "Selected saved run. No runtime profile attached."
        )

    def action_runtime(self) -> None:
        if len(self.screen_stack) != 1:
            return
        from code_analysis.tui_runtime import RuntimeBrowser

        self.push_screen(
            RuntimeBrowser(
                self.index,
                self.evidence_closed,
                self.viewer,
                self.preview_root,
                web_action=lambda request: self.open_runtime_handoff(self.index, request),
            ),
            self.comparison_closed,
        )

    def action_static_projections(self) -> None:
        if len(self.screen_stack) != 1:
            return
        from code_analysis.tui_static_projections import StaticProjectionBrowser

        self.push_screen(
            StaticProjectionBrowser(
                self.index,
                self.evidence_closed,
                self.viewer,
                page_size=self.selection.limit,
                workspace_root=self.workspace_root,
                source_root=self.preview_root,
            ),
            self.static_projections_closed,
        )

    def action_review_worklist(self) -> None:
        """Open a separate candidate-review policy; inventory facets do not transfer."""
        if len(self.screen_stack) != 1 or self.selection.table is not BrowseTable.CANDIDATES:
            return
        if ProjectionName.CANDIDATE_WORKLIST not in {
            item.name for item in self.index.manifest.static_projections
        }:
            self.exit_status = errno.ENODATA
            self.query_one("#page-status", Static).update(
                "Candidate worklist is not prepared for this saved index."
            )
            return
        from code_analysis.tui_static_projections import StaticProjectionBrowser

        self.push_screen(
            StaticProjectionBrowser(
                self.index,
                self.evidence_closed,
                self.viewer,
                page_size=self.selection.limit,
                workspace_root=self.workspace_root,
                source_root=self.preview_root,
                initial_name=ProjectionName.CANDIDATE_WORKLIST,
            ),
            self.evidence_closed,
        )

    def static_projections_closed(self, status: int | None) -> None:
        self.evidence_closed(status)
        self.query_one("#view-choice", Select).value = (
            "overview" if self.showing_overview else self.selection.table.value
        )

    def action_web_handoff(self) -> None:
        if len(self.screen_stack) == 1:
            self.open_web_handoff()

    def open_web_handoff(
        self,
        kind: WebHandoffKind = "report",
        identity: str | None = None,
        *,
        graph_request: StaticGraphRequest | None = None,
    ) -> None:
        from code_analysis.tui_handoff import WebHandoffDialog
        from code_analysis.ui_handoff import WebHandoffSelection

        source = self.index.bootstrap().reports[0]
        try:
            selection = WebHandoffSelection(
                report_sha256=source.report_sha256,
                kind=kind,
                identity=identity,
                graph_request=graph_request,
            )
        except ValueError as exc:
            self.exit_status = errno.EINVAL
            self.query_one("#page-status", Static).update(plain_text(str(exc)))
            return
        self.push_screen(
            WebHandoffDialog(
                self.web_handoff,
                selection,
                self.browser,
                self.report_path,
                self.remember_handoff_report,
            ),
            self.evidence_closed,
        )

    def open_runtime_handoff(
        self, index: BrowseIndex | RuntimeIndex, request: RuntimeGraphRequest
    ) -> None:
        from code_analysis.tui_handoff import WebHandoffDialog, runtime_selection

        selection = runtime_selection(index, request)
        if selection is None:
            self.exit_status = errno.EINVAL
            return
        self.push_screen(
            WebHandoffDialog(self.web_handoff, selection, self.browser), self.evidence_closed
        )

    def remember_handoff_report(self, report: Path) -> None:
        self.report_path = report

    def action_comparison(self) -> None:
        if len(self.screen_stack) != 1:
            return
        from code_analysis.tui_comparison import ComparisonBrowser

        self.push_screen(
            ComparisonBrowser(
                self.index,
                self.evidence_closed,
                self.selection.limit,
                source_root=self.preview_root,
            ),
            self.comparison_closed,
        )

    def comparison_closed(self, status: int | None) -> None:
        self.evidence_closed(status)
        self.query_one("#view-choice", Select).value = (
            "overview" if self.showing_overview else self.selection.table.value
        )

    def action_filter(self) -> None:
        if not self.showing_overview and self.selection.table is not BrowseTable.PROVIDERS:
            self.query_one("#search", Input).focus()

    def apply_filters(self) -> None:
        if self.showing_overview:
            return
        try:
            selection = BrowseRequest.model_validate(
                {
                    "table": self.selection.table,
                    "limit": self.selection.limit,
                    "query": (
                        self.query_one("#search", Input).value
                        if self.selection.table is not BrowseTable.PROVIDERS
                        else ""
                    ),
                    "severity": (
                        str(self.query_one("#severity", Select).value)
                        if self.selection.table is BrowseTable.FINDINGS
                        else ""
                    ),
                    "category": (
                        ProjectionFindingCategory(
                            str(self.query_one("#finding-category", Select).value)
                        )
                        if self.selection.table is BrowseTable.FINDINGS
                        and self.query_one("#finding-category", Select).value
                        else None
                    ),
                    "confidence": (
                        Confidence(str(self.query_one("#finding-confidence", Select).value))
                        if self.selection.table is BrowseTable.FINDINGS
                        and self.query_one("#finding-confidence", Select).value
                        else None
                    ),
                    "facets": {
                        "language": (
                            SourceLanguage(str(self.query_one("#facet-language", Select).value))
                            if self.selection.table
                            in {
                                BrowseTable.MODULES,
                                BrowseTable.FINDINGS,
                                BrowseTable.STATE_EFFECTS,
                                BrowseTable.CONTRACTS,
                                BrowseTable.SHAPES,
                                BrowseTable.CANDIDATES,
                                BrowseTable.CLONES,
                                BrowseTable.TEST_IMPACT,
                            }
                            and self.query_one("#facet-language", Select).value
                            else None
                        ),
                        "module_prefix": (
                            self.query_one("#facet-module", Input).value
                            if self.selection.table
                            in {
                                BrowseTable.MODULES,
                                BrowseTable.FINDINGS,
                                BrowseTable.STATE_EFFECTS,
                                BrowseTable.CONTRACTS,
                                BrowseTable.SHAPES,
                                BrowseTable.CANDIDATES,
                                BrowseTable.CLONES,
                                BrowseTable.TEST_IMPACT,
                            }
                            else ""
                        ),
                        "owner": (
                            self.query_one("#facet-owner", Input).value
                            if self.selection.table
                            in {
                                BrowseTable.STATE_EFFECTS,
                                BrowseTable.CONTRACTS,
                                BrowseTable.SHAPES,
                            }
                            else ""
                        ),
                    },
                }
            )
        except ValueError as exc:
            self.query_one("#page-status", Static).update(plain_text(str(exc)))
            return
        self.selection = selection
        self.load_page()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.apply_filters()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply":
            self.apply_filters()
        elif event.button.id == "next":
            self.action_next_page()
        elif event.button.id == "previous":
            self.action_previous_page()
        elif event.button.id == "review-worklist":
            self.action_review_worklist()
        elif event.button.id == "export":
            self.action_export()

    def action_export(self) -> None:
        if len(self.screen_stack) != 1:
            return
        if (
            self.current_page is not None
            and not self.showing_overview
            and self.current_page.offset == self.selection.offset
        ):
            from code_analysis.tui_export import ExportPage

            self.push_screen(
                ExportPage(self.index, self.selection, self.evidence_closed, self.viewer)
            )

    def action_next_page(self) -> None:
        if (
            self.current_page
            and self.current_page.truncated
            and not self.showing_overview
            and self.current_page.offset == self.selection.offset
        ):
            self.selection = self.selection.model_copy(
                update={"offset": self.selection.offset + self.selection.limit}
            )
            self.load_page()

    def action_previous_page(self) -> None:
        if self.selection.offset and not self.showing_overview:
            self.selection = self.selection.model_copy(
                update={"offset": max(0, self.selection.offset - self.selection.limit)}
            )
            self.load_page()

    @work(exclusive=True, group="page")
    async def load_page(self) -> None:
        self.revision += 1
        revision = self.revision
        request = self.selection
        self.current_page = None
        table = self.query_one("#records", DataTable)
        table.clear(columns=True)
        self.query_one("#next", Button).disabled = True
        self.query_one("#previous", Button).disabled = True
        self.query_one("#page-status", Static).update("Loading bounded page…")
        try:
            page = await asyncio.to_thread(self.index.page, request)
        except (ValueError, OSError, sqlite3.Error) as exc:
            if (
                revision == self.revision
                and request == self.selection
                and not self.showing_overview
            ):
                self.exit_status = errno.EBADMSG
                self.query_one("#page-status", Static).update(
                    plain_text(f"Index unavailable: {exc}")
                )
            return
        if revision != self.revision or request != self.selection or self.showing_overview:
            return
        self.current_page = page
        if page.records:
            fields = tuple(
                name
                for name in type(page.records[0]).model_fields
                if not (
                    isinstance(
                        page.records[0],
                        (UIStateEffectRow, CloneRow, ContractRow, ShapeRow, TestImpactRow),
                    )
                    and name == "detail"
                )
                and not (isinstance(page.records[0], UIFindingRow) and name == "suppression")
            )
            table.add_columns(*(name.replace("_", " ").title() for name in fields))
            for number, record in enumerate(page.records):
                values = record.model_dump(mode="json")
                table.add_row(*(plain_text(str(values[name])) for name in fields), key=str(number))
        self.query_one("#page-status", Static).update(
            f"{self.selection.table.value}: {page.offset + 1 if page.retained else 0}"
            f"-{page.offset + page.retained} of {page.eligible} · "
            + ("More pages available" if page.truncated else "End of filtered results")
            + (
                f" · Section {page.coverage.state.value}; unresolved {page.coverage.unresolved}; "
                f"truncated {page.coverage.truncated}; failed {page.coverage.failed}. "
                "Syntax only, not runtime proof."
                if isinstance(
                    page, (UIStateEffectPage, ClonePage, ContractPage, ShapePage, TestImpactPage)
                )
                else ""
            )
        )
        self.query_one("#next", Button).disabled = not page.truncated
        self.query_one("#previous", Button).disabled = page.offset == 0
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self.current_page is not None:
            row_key = event.row_key.value
            if row_key is None:
                return
            record = self.current_page.records[int(row_key)]
            if self.selection.table is BrowseTable.MODULES:
                from code_analysis.tui_modules import ModuleBrowser
                from code_analysis.ui_models import UIModuleRow

                if isinstance(record, UIModuleRow):
                    self.push_screen(
                        ModuleBrowser(
                            self.index,
                            record.module,
                            self.evidence_closed,
                            self.preview_root,
                            self.open_web_handoff,
                            self.viewer,
                        ),
                        self.evidence_closed,
                    )
                return
            if self.selection.table is BrowseTable.FINDINGS:
                from code_analysis.tui_evidence import FindingEvidence
                from code_analysis.ui_models import UIFindingRow

                if isinstance(record, UIFindingRow):
                    self.push_screen(
                        FindingEvidence(
                            self.index,
                            record.finding_id,
                            self.evidence_closed,
                            self.preview_root,
                            lambda: self.open_web_handoff("finding", record.finding_id),
                        ),
                        self.evidence_closed,
                    )
                return
            if self.selection.table is BrowseTable.CANDIDATES:
                from code_analysis.ui_models import UICandidateRow

                if isinstance(record, UICandidateRow):
                    self.load_candidate_packet(record.candidate_id)
                return
            if isinstance(record, CloneRow):
                from code_analysis.tui_clones import CloneComparison

                self.push_screen(
                    CloneComparison(self.index, record, self.evidence_closed, self.preview_root)
                )
                return
            if isinstance(record, (ContractRow, ShapeRow)):
                from code_analysis.tui_contracts import ContractExplorer

                self.push_screen(
                    ContractExplorer(self.index, record, self.evidence_closed, self.preview_root)
                )
                return
            if isinstance(record, TestImpactRow):
                self.open_test_record(record)
                return
            self.push_screen(
                RowDetails(
                    json.dumps(record.model_dump(mode="json"), indent=2, ensure_ascii=False),
                    (
                        "State/effect evidence — syntax observations, not runtime proof"
                        if isinstance(record, UIStateEffectRow)
                        else "Indexed row details — not a full evidence/review packet"
                    ),
                )
            )

    def open_test_record(self, row: TestImpactRow) -> None:
        from code_analysis.tui_test_impact import TestInvestigationScreen

        self.push_screen(
            TestInvestigationScreen(self.index, row, self.preview_root, self.evidence_closed)
        )

    def evidence_closed(self, status: int | None) -> None:
        if status:
            self.exit_status = status

    @work(exclusive=True, group="detail")
    async def load_candidate_packet(self, identity: str) -> None:
        revision = self.revision
        request = self.selection
        try:
            packet = await asyncio.to_thread(self.index.candidate_packet, identity)
        except (ValueError, OSError, sqlite3.Error) as exc:
            if revision == self.revision and request == self.selection:
                self.exit_status = errno.EBADMSG
                self.query_one("#page-status", Static).update(
                    plain_text(f"Candidate packet unavailable: {exc}")
                )
            return
        if revision != self.revision or request != self.selection or self.showing_overview:
            return
        from code_analysis.tui_candidate import CandidatePacket

        self.push_screen(CandidatePacket(self.index, packet, self.evidence_closed))

    def action_finish(self) -> None:
        self.exit(self.exit_status)

    def close_web_handoff(self) -> None:
        self.web_handoff.close()

    def on_unmount(self) -> None:
        self.close_web_handoff()

    async def action_quit(self) -> None:
        self.action_finish()
