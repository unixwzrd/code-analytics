"""Terminal editor for the indexed subject-findings selection the index can resolve.

Only the four dimensions retained as saved facts are offered. Aggregation,
filter-before-grouping order and the recomputed unscoped disclosure remain
backend-owned; this screen constructs a selection and nothing else.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, SelectionList, Static, TextArea

from code_analysis.projection_models import (
    ProjectionFindingCategory,
    ProjectionFindingSeverity,
    ProjectionSelection,
)
from code_analysis.subject_finding_browse import validate_indexed_finding_selection
from code_analysis.tui_app import plain_text


def _values(value: str) -> tuple[str, ...]:
    """Canonical repeated values, one trimmed nonblank line each."""

    return tuple(sorted({item.strip() for item in value.splitlines() if item.strip()}))


def scope_summary(selection: ProjectionSelection | None) -> str:
    """Describe the active finding scope in one line for a caller's status row."""

    active = selection or ProjectionSelection()
    counts = tuple(
        f"{len(values)} {label}"
        for label, values in (
            ("prefix", active.module_prefixes),
            ("category", active.finding_categories),
            ("severity", active.finding_severities),
            ("rule", active.finding_rule_ids),
        )
        if values
    )
    if not counts:
        return "Finding scope: every prepared subject row"
    return "Finding scope: " + ", ".join(counts)


class SubjectFindingFilterEditor(ModalScreen[ProjectionSelection | None]):
    """Edit the indexed finding selection only; scoring and totals stay backend-owned."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "apply", "Apply filters"),
    ]
    CSS = """
    SubjectFindingFilterEditor { align: center middle; }
    #subject-filter-dialog { width: 72; height: 92%; border: round $accent;
                             background: $surface; padding: 1; }
    #subject-filter-scroll { height: 1fr; }
    .subject-filter-label { margin-top: 1; height: auto; }
    .subject-filter-list { height: 5; }
    .subject-filter-lines { height: 4; }
    #subject-filter-error { height: auto; color: $error; }
    #subject-filter-buttons { height: 3; }
    """

    def __init__(self, selection: ProjectionSelection | None = None) -> None:
        super().__init__()
        self.selection = selection or ProjectionSelection()
        validate_indexed_finding_selection(self.selection)

    def compose(self) -> ComposeResult:
        with Vertical(id="subject-filter-dialog"):
            yield Static(
                "Subject-findings scope · repeated choices are OR; dimensions are AND. "
                "The index filters findings before aggregating subjects."
            )
            with ScrollableContainer(id="subject-filter-scroll"):
                yield Label("Finding categories", classes="subject-filter-label")
                yield SelectionList(
                    *(
                        (
                            item.value.replace("-", " ").title(),
                            item.value,
                            item in self.selection.finding_categories,
                        )
                        for item in ProjectionFindingCategory
                    ),
                    id="subject-filter-categories",
                    classes="subject-filter-list",
                )
                yield Label("Finding severities", classes="subject-filter-label")
                yield SelectionList(
                    *(
                        (
                            item.value.title(),
                            item.value,
                            item in self.selection.finding_severities,
                        )
                        for item in ProjectionFindingSeverity
                    ),
                    id="subject-filter-severities",
                    classes="subject-filter-list",
                )
                yield Label(
                    "Logical module prefixes (one exact dotted module prefix per line)",
                    classes="subject-filter-label",
                )
                yield TextArea(
                    "\n".join(self.selection.module_prefixes),
                    id="subject-filter-prefixes",
                    classes="subject-filter-lines",
                )
                yield Label(
                    "Finding rule IDs (one recorded rule ID per line)",
                    classes="subject-filter-label",
                )
                yield TextArea(
                    "\n".join(self.selection.finding_rule_ids),
                    id="subject-filter-rules",
                    classes="subject-filter-lines",
                )
                yield Static(
                    "Exact subject selection and language filtering are not supported in this "
                    "indexed view. These controls do not apply either scope.",
                    classes="subject-filter-label",
                )
            yield Static("", id="subject-filter-error")
            with Horizontal(id="subject-filter-buttons"):
                yield Button("Apply filters", id="subject-filter-apply", variant="primary")
                yield Button("Reset", id="subject-filter-reset")
                yield Button("Cancel", id="subject-filter-cancel")

    def filters(self) -> ProjectionSelection:
        selection = ProjectionSelection(
            module_prefixes=_values(self.query_one("#subject-filter-prefixes", TextArea).text),
            finding_categories=tuple(
                sorted(
                    ProjectionFindingCategory(item)
                    for item in self.query_one("#subject-filter-categories", SelectionList).selected
                )
            ),
            finding_severities=tuple(
                sorted(
                    ProjectionFindingSeverity(item)
                    for item in self.query_one("#subject-filter-severities", SelectionList).selected
                )
            ),
            finding_rule_ids=_values(self.query_one("#subject-filter-rules", TextArea).text),
        )
        # Refuse anything the index cannot resolve from retained facts here, so an
        # unsupported dimension can never reach a request the backend must reject.
        validate_indexed_finding_selection(selection)
        return selection

    def action_apply(self) -> None:
        try:
            selection = self.filters()
        except (TypeError, ValueError) as exc:
            self.query_one("#subject-filter-error", Static).update(plain_text(str(exc)))
            return
        self.dismiss(selection)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def reset(self) -> None:
        self.dismiss(ProjectionSelection())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        actions = {
            "subject-filter-apply": self.action_apply,
            "subject-filter-reset": self.reset,
            "subject-filter-cancel": self.action_cancel,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()
