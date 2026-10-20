"""Terminal editor for the authoritative saved candidate-worklist policy."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, SelectionList, Static, TextArea

from code_analysis.candidate_policy import DECISION_RANK
from code_analysis.models import SourceLanguage
from code_analysis.projection_models import ProjectionSelection
from code_analysis.report_models import RefactorCandidateKind, RefactorCandidateState
from code_analysis.tui_app import plain_text


def _patterns(value: str) -> tuple[str, ...]:
    """Canonical repeated portable patterns, one per nonblank line."""

    return tuple(sorted({item.strip() for item in value.splitlines() if item.strip()}))


class CandidatePolicyEditor(ModalScreen[ProjectionSelection | None]):
    """Edit worklist selection only; eligibility and ranking remain backend-owned."""

    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "apply", "Apply policy"),
    ]
    CSS = """
    CandidatePolicyEditor { align: center middle; }
    #candidate-policy-dialog { width: 72; height: 92%; border: round $accent;
                               background: $surface; padding: 1; }
    #candidate-policy-scroll { height: 1fr; }
    .candidate-policy-label { margin-top: 1; height: auto; }
    .candidate-policy-list { height: 5; }
    .candidate-policy-patterns { height: 4; }
    #candidate-policy-number { width: 18; }
    #candidate-policy-error { height: auto; color: $error; }
    #candidate-policy-buttons { height: 3; }
    """

    def __init__(self, selection: ProjectionSelection, *, decisions_attached: bool = False) -> None:
        super().__init__()
        # A decision filter is meaningful only while the caller holds an explicit,
        # verified ledger snapshot.  Do not retain it merely because this dialog
        # was opened after a stale attachment.
        self.decisions_attached = decisions_attached
        self.selection = (
            selection
            if decisions_attached
            else selection.model_copy(update={"candidate_decisions": ()})
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="candidate-policy-dialog"):
            yield Static(
                "Candidate worklist policy · repeated choices are OR; policy dimensions are AND"
            )
            with ScrollableContainer(id="candidate-policy-scroll"):
                yield Label("Languages", classes="candidate-policy-label")
                yield SelectionList(
                    *(
                        (item.value.title(), item.value, item in self.selection.languages)
                        for item in SourceLanguage
                    ),
                    id="candidate-policy-languages",
                    classes="candidate-policy-list",
                )
                yield Label("Candidate states", classes="candidate-policy-label")
                yield SelectionList(
                    *(
                        (
                            item.value.replace("-", " ").title(),
                            item.value,
                            item.value in self.selection.candidate_states,
                        )
                        for item in RefactorCandidateState
                    ),
                    id="candidate-policy-states",
                    classes="candidate-policy-list",
                )
                yield Label("Candidate kinds", classes="candidate-policy-label")
                yield SelectionList(
                    *(
                        (
                            item.value.replace("-", " ").title(),
                            item.value,
                            item in self.selection.candidate_kinds,
                        )
                        for item in RefactorCandidateKind
                    ),
                    id="candidate-policy-kinds",
                    classes="candidate-policy-list",
                )
                yield Label("Maintainer decisions", classes="candidate-policy-label")
                yield SelectionList(
                    *(
                        (
                            item.replace("-", " ").title(),
                            item,
                            item in self.selection.candidate_decisions,
                        )
                        for item in DECISION_RANK
                    ),
                    id="candidate-policy-decisions",
                    classes="candidate-policy-list",
                    disabled=not self.decisions_attached,
                )
                yield Static(
                    (
                        "Decision filters use the explicitly attached, read-only ledger."
                        if self.decisions_attached
                        else (
                            "Attach a saved decision ledger from the worklist before filtering "
                            "by decision."
                        )
                    ),
                    classes="candidate-policy-label",
                )
                yield Label(
                    "Include module patterns (one full-string fnmatch pattern per line)",
                    classes="candidate-policy-label",
                )
                yield TextArea(
                    "\n".join(self.selection.candidate_module_patterns),
                    id="candidate-policy-includes",
                    classes="candidate-policy-patterns",
                )
                yield Label(
                    "Exclude module patterns (exclusions win)", classes="candidate-policy-label"
                )
                yield TextArea(
                    "\n".join(self.selection.candidate_exclude_patterns),
                    id="candidate-policy-excludes",
                    classes="candidate-policy-patterns",
                )
                yield Checkbox(
                    "Cross-module candidates only",
                    self.selection.cross_module_only,
                    id="candidate-policy-cross-module",
                )
                yield Checkbox(
                    "Production subjects only",
                    self.selection.production_only,
                    id="candidate-policy-production",
                )
                yield Checkbox(
                    "Prefer existing owner when ordering",
                    self.selection.prefer_existing_owner,
                    id="candidate-policy-owner",
                )
                yield Label("Minimum shared dimensions (0-32)", classes="candidate-policy-label")
                yield Input(
                    str(self.selection.min_shared_dimensions),
                    type="integer",
                    id="candidate-policy-number",
                    max_length=2,
                )
            yield Static("", id="candidate-policy-error")
            with Horizontal(id="candidate-policy-buttons"):
                yield Button("Apply policy", id="candidate-policy-apply", variant="primary")
                yield Button("Reset", id="candidate-policy-reset")
                yield Button("Cancel", id="candidate-policy-cancel")

    def policy(self) -> ProjectionSelection:
        return ProjectionSelection(
            languages=tuple(
                sorted(
                    SourceLanguage(item)
                    for item in self.query_one(
                        "#candidate-policy-languages", SelectionList
                    ).selected
                )
            ),
            candidate_states=tuple(
                sorted(
                    str(item)
                    for item in self.query_one("#candidate-policy-states", SelectionList).selected
                )
            ),
            candidate_kinds=tuple(
                sorted(
                    RefactorCandidateKind(item)
                    for item in self.query_one("#candidate-policy-kinds", SelectionList).selected
                )
            ),
            candidate_decisions=(
                tuple(
                    sorted(
                        str(item)
                        for item in self.query_one(
                            "#candidate-policy-decisions", SelectionList
                        ).selected
                    )
                )
                if self.decisions_attached
                else ()
            ),
            candidate_module_patterns=_patterns(
                self.query_one("#candidate-policy-includes", TextArea).text
            ),
            candidate_exclude_patterns=_patterns(
                self.query_one("#candidate-policy-excludes", TextArea).text
            ),
            cross_module_only=self.query_one("#candidate-policy-cross-module", Checkbox).value,
            production_only=self.query_one("#candidate-policy-production", Checkbox).value,
            prefer_existing_owner=self.query_one("#candidate-policy-owner", Checkbox).value,
            min_shared_dimensions=int(self.query_one("#candidate-policy-number", Input).value),
        )

    def action_apply(self) -> None:
        try:
            selection = self.policy()
        except (TypeError, ValueError) as exc:
            self.query_one("#candidate-policy-error", Static).update(plain_text(str(exc)))
            return
        self.dismiss(selection)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def reset(self) -> None:
        self.dismiss(ProjectionSelection())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        actions = {
            "candidate-policy-apply": self.action_apply,
            "candidate-policy-reset": self.reset,
            "candidate-policy-cancel": self.action_cancel,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()
