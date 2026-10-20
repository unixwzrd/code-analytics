"""Shared typed dependency and symbol pages for renderer-independent navigation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Self

from pydantic import Field, model_validator

from code_analysis.models import Completeness, Module, Symbol
from code_analysis.report_models import AnalysisReport
from code_analysis.ui_models import UIGraphEdge, UIModuleRow, UIPage
from code_analysis.ui_rows import module_rows

type ModuleView = Literal["outgoing", "incoming", "symbols"]


class ModuleSelectionError(ValueError):
    """A requested logical module/page is absent from the selected saved index."""


class ModuleNavigationPage(UIPage):
    module: UIModuleRow
    view: ModuleView
    source_completeness: Completeness
    edges: tuple[UIGraphEdge, ...] = Field(default=(), max_length=25)
    symbols: tuple[Symbol, ...] = Field(default=(), max_length=25)

    @model_validator(mode="after")
    def validate_navigation(self) -> Self:
        if self.limit != 25 or self.offset % 25:
            raise ValueError("module navigation uses aligned 25-record pages")
        if self.retained != len(self.edges) + len(self.symbols):
            raise ValueError("module navigation record count mismatch")
        if (self.view == "symbols" and self.edges) or (self.view != "symbols" and self.symbols):
            raise ValueError("module navigation record kind mismatch")
        for edge in self.edges:
            endpoint = edge.source_id if self.view == "outgoing" else edge.target_id
            if endpoint != self.module.module:
                raise ValueError("module navigation edge has an unrelated endpoint")
        if any(
            symbol.language != self.module.language or symbol.span.path != self.module.path
            for symbol in self.symbols
        ):
            raise ValueError("module navigation symbol language/path mismatch")
        return self


@dataclass(frozen=True, slots=True)
class ReportModuleReader:
    """Immutable adjacency shared by live navigation and derivative index preparation."""

    modules: Mapping[str, Module]
    rows: Mapping[str, UIModuleRow]
    outgoing: Mapping[str, tuple[UIGraphEdge, ...]]
    incoming: Mapping[str, tuple[UIGraphEdge, ...]]
    completeness: Completeness

    @classmethod
    def from_report(cls, report: AnalysisReport) -> ReportModuleReader:
        known = {module.name: module for module in report.modules}
        outgoing: dict[str, list[UIGraphEdge]] = defaultdict(list)
        incoming: dict[str, list[UIGraphEdge]] = defaultdict(list)
        for edge in sorted(
            report.import_edges, key=lambda item: (item.source, item.target, item.kind)
        ):
            if edge.local and edge.source in known and edge.target in known:
                record = UIGraphEdge(source_id=edge.source, target_id=edge.target, kind=edge.kind)
                outgoing[edge.source].append(record)
                incoming[edge.target].append(record)
        return cls(
            MappingProxyType(known),
            MappingProxyType({row.module: row for row in module_rows(report)}),
            MappingProxyType({key: tuple(value) for key, value in outgoing.items()}),
            MappingProxyType({key: tuple(value) for key, value in incoming.items()}),
            report.completeness,
        )

    def module_navigation(
        self, module: str, view: ModuleView = "outgoing", offset: int = 0
    ) -> ModuleNavigationPage:
        if module not in self.modules or view not in ("outgoing", "incoming", "symbols"):
            raise ModuleSelectionError("module navigation selection is not present in this report")
        if type(offset) is not int or offset < 0 or offset % 25:
            raise ModuleSelectionError("invalid module navigation offset")
        edges = (self.outgoing if view == "outgoing" else self.incoming).get(module, ())
        symbols = self.modules[module].symbols
        count = len(symbols) if view == "symbols" else len(edges)
        if offset and offset >= count:
            raise ModuleSelectionError("module navigation page is not present in this report")
        retained = min(25, count - offset)
        return ModuleNavigationPage(
            module=self.rows[module],
            view=view,
            source_completeness=self.completeness,
            offset=offset,
            limit=25,
            eligible=count,
            retained=retained,
            truncated=offset + retained < count,
            edges=edges[offset : offset + 25] if view != "symbols" else (),
            symbols=symbols[offset : offset + 25] if view == "symbols" else (),
        )


def module_navigation_pages(report: AnalysisReport) -> Iterator[ModuleNavigationPage]:
    """Local, known-module adjacency only; external/unresolved edges stay in the report."""
    reader = ReportModuleReader.from_report(report)
    for module in reader.rows:
        for view in ("outgoing", "incoming", "symbols"):
            page = reader.module_navigation(module, view)
            yield page
            while page.truncated:
                page = reader.module_navigation(module, view, page.offset + page.limit)
                yield page
