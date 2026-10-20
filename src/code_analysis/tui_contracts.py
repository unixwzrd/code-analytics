"""Keyboard field exploration preserving normalized types, variants, and defaults."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from code_analysis.browse_index import BrowseIndex
from code_analysis.contract_browsing import ContractRow, ShapeRow
from code_analysis.report_models import ContractMember, ShapeField, ShapeVariant
from code_analysis.tui_app import RowDetails, plain_text


@dataclass(frozen=True, slots=True)
class FieldEntry:
    field: ContractMember | ShapeField
    variant: ShapeVariant | None


def field_entries(row: ContractRow | ShapeRow) -> tuple[FieldEntry, ...]:
    if isinstance(row, ContractRow):
        return tuple(FieldEntry(item, None) for item in row.detail.contract.members)
    return tuple(
        FieldEntry(item, variant)
        for variant in row.detail.shape.variants
        for item in variant.fields
    )


class ContractExplorer(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "close", "Close"),
        ("ctrl+n", "next", "Next fields"),
        ("ctrl+p", "previous", "Previous fields"),
    ]
    CSS = """
    ContractExplorer { align: center middle; }
    #contract-dialog { width: 95%; height: 90%; border: round $accent;
                       background: $surface; padding: 1; }
    #contract-summary { height: auto; }
    #contract-buttons { height: 3; }
    """

    def __init__(
        self,
        index: BrowseIndex,
        row: ContractRow | ShapeRow,
        on_error: Callable[[int], None],
        source_root: Path | None = None,
    ) -> None:
        super().__init__()
        self.index = index
        self.row = row
        self.on_error = on_error
        self.source_root = source_root
        self.entries = field_entries(row)
        self.field_offset = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="contract-dialog"):
            yield Static("Fields and variants — static evidence, not runtime validation")
            yield Static("", id="contract-summary")
            yield DataTable(id="contract-fields", cursor_type="row")
            with Horizontal(id="contract-buttons"):
                yield Button("Previous", id="contract-previous")
                yield Button("Next", id="contract-next")
                yield Button("Full definition", id="contract-definition")
                yield Button("Close", id="contract-close")

    def on_mount(self) -> None:
        self.render_fields()

    def render_fields(self) -> None:
        entries = self.entries[self.field_offset : self.field_offset + 25]
        table = self.query_one("#contract-fields", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "Variant",
            "Field",
            "Type kind",
            "Requiredness",
            "Producer defaults",
            "Consumer fallbacks",
        )
        for number, entry in enumerate(entries, self.field_offset):
            field = entry.field
            requiredness = (
                (field.requiredness,)
                if isinstance(field, ContractMember)
                else field.requiredness_observations
            )
            values = (
                entry.variant.name if entry.variant else "declared contract",
                field.name,
                field.type_expression.kind.value,
                ", ".join(item.kind.value for item in requiredness),
                len(field.producer_defaults),
                len(field.consumer_fallbacks),
            )
            table.add_row(*(plain_text(str(value)) for value in values), key=str(number))
        first = self.field_offset + 1 if entries else 0
        self.query_one("#contract-summary", Static).update(
            plain_text(
                f"{self.row.module} · {self.row.owner} · {self.row.language.value}. "
                f"Field occurrences {first}-{self.field_offset + len(entries)} "
                f"of {len(self.entries)}. "
                "Enter: complete type tree, conditions, defaults and evidence. "
                "Full definition: boundaries, variants, lifecycle and unresolved behavior."
            )
        )
        self.query_one("#contract-previous", Button).disabled = self.field_offset == 0
        self.query_one("#contract-next", Button).disabled = self.field_offset + 25 >= len(
            self.entries
        )
        table.focus()

    def action_next(self) -> None:
        if self.field_offset + 25 < len(self.entries):
            self.field_offset += 25
            self.render_fields()

    def action_previous(self) -> None:
        if self.field_offset:
            self.field_offset = max(0, self.field_offset - 25)
            self.render_fields()

    def action_close(self) -> None:
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "contract-next":
            self.action_next()
        elif event.button.id == "contract-previous":
            self.action_previous()
        elif event.button.id == "contract-definition":
            self.app.push_screen(
                RowDetails(
                    self.row.detail.model_dump_json(indent=2),
                    "Full saved definition — no inferred runtime validity",
                )
            )
        else:
            self.action_close()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        entry = self.entries[int(event.row_key.value)]

        def preview() -> None:
            from code_analysis.tui_source import TerminalSource

            span = entry.field.evidence[0].span
            self.app.push_screen(
                TerminalSource(
                    self.index,
                    self.source_root,
                    span.path,
                    span.start_line,
                    span.end_line,
                    self.on_error,
                )
            )

        self.app.push_screen(
            RowDetails(
                json.dumps(
                    {
                        "field": entry.field.model_dump(mode="json"),
                        "variant": entry.variant.model_dump(mode="json") if entry.variant else None,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                "Field evidence — variants remain separate; preview uses first recorded span",
                preview,
            )
        )
