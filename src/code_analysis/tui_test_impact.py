"""Test-to-target investigation over prepared evidence, not test execution."""

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from code_analysis.browse_index import BrowseIndex
from code_analysis.projections import ProjectionName
from code_analysis.test_browsing import TestImpactRow, investigate_test
from code_analysis.tui_app import RowDetails, plain_text
from code_analysis.ui_source import SourcePreviewIdentity


class TestInvestigationScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "close", "Close")]
    CSS = """
    TestInvestigationScreen { align: center middle; }
    #test-investigation { width: 95%; height: 90%; border: round $accent;
                          background: $surface; padding: 1; }
    #test-coverage-summary { height: auto; max-height: 7; overflow-y: auto; }
    #test-source-choices { height: 1fr; }
    #test-actions { height: 3; }
    """

    def __init__(
        self,
        index: BrowseIndex,
        row: TestImpactRow,
        root: Path | None,
        on_status: Callable[[int], None],
    ) -> None:
        super().__init__()
        self.index, self.root, self.on_status = index, root, on_status
        self.investigation = investigate_test(row)

    def compose(self) -> ComposeResult:
        row = self.investigation.row
        coverage = row.detail.target_coverage
        summary = (
            f"{row.relationship_kind or row.record_kind}; resolved: {row.resolved}\n"
            f"Target: {row.target or 'No exact target'}\n"
            "Static references are not execution or behavioral coverage.\n"
            f"{self.investigation.omitted_sources} source choices omitted.\n"
        )
        if coverage is None:
            summary += "No exact application subject; target coverage cannot be inferred."
        elif not coverage.observations:
            summary += (
                f"Coverage section: {coverage.section_state.value}. "
                "No target observation; not zero coverage."
            )
        else:
            summary += (
                f"Coverage section: {coverage.section_state.value}; "
                f"{coverage.omitted} observations omitted.\n"
            )
            summary += "\n".join(
                f"{item.binding.value}/{item.completeness.value}: {item.covered_lines} covered, "
                f"{item.missing_lines} missing lines; {item.contexts} contexts"
                for item in coverage.observations
            )
        with Vertical(id="test-investigation"):
            yield Static(plain_text(summary), id="test-coverage-summary")
            yield DataTable(id="test-source-choices", cursor_type="row")
            with Horizontal(id="test-actions"):
                yield Button("Evidence", id="test-evidence")
                yield Button(
                    "Target coverage",
                    id="test-coverage",
                    disabled=coverage is None
                    or not any(
                        item.name is ProjectionName.COVERAGE_MATRIX
                        for item in self.index.manifest.static_projections
                    ),
                )
                yield Button("Close", id="test-close")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Role", "Recorded source (Enter previews)")
        for number, source in enumerate(self.investigation.sources):
            table.add_row(
                source.role,
                plain_text(f"{source.path}:{source.start}-{source.end}"),
                key=str(number),
            )
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        from code_analysis.tui_source import TerminalSource

        event.stop()
        source = self.investigation.sources[int(event.row_key.value)]
        self.app.push_screen(
            TerminalSource(
                self.index,
                self.root,
                source.path,
                source.start,
                source.end,
                self.on_status,
                source_identity=SourcePreviewIdentity(
                    logical_path=source.path, source_sha256=source.source_sha256
                ),
            )
        )

    def action_close(self) -> None:
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        from code_analysis.tui_static_projections import StaticProjectionBrowser

        event.stop()
        coverage = self.investigation.row.detail.target_coverage
        if event.button.id == "test-evidence":
            self.app.push_screen(
                RowDetails(
                    self.investigation.model_dump_json(indent=2), "Saved test/target evidence"
                )
            )
        elif event.button.id == "test-coverage" and coverage is not None:
            self.app.push_screen(
                StaticProjectionBrowser(
                    self.index,
                    self.on_status,
                    initial_name=ProjectionName.COVERAGE_MATRIX,
                    initial_query=coverage.subject_id,
                )
            )
        else:
            self.action_close()
