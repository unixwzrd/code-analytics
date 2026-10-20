"""Immutable language-neutral facts produced by parser front ends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from code_analysis.config import AnalysisConfig
from code_analysis.models import (
    Completeness,
    Diagnostic,
    ModuleSystem,
    ParserProvenance,
    ProviderAvailability,
    SourceLanguage,
)
from code_analysis.report_models import AdapterCapabilityRecord


@dataclass(frozen=True, slots=True)
class SourceUnit:
    logical_path: str
    module_name: str
    source: str
    source_bytes: bytes
    source_sha256: str
    language: SourceLanguage


@dataclass(frozen=True, slots=True)
class FrontendContext:
    config: AnalysisConfig
    configuration_sha256: str
    module_system_override: ModuleSystem | None = None


@dataclass(frozen=True, slots=True)
class NormalizedComplexityFact:
    physical_loc: int
    logical_loc: int
    branches: int
    nesting_depth: int
    argument_count: int
    cyclomatic_complexity: int
    weighted_score: int
    returns: int
    raises: int


@dataclass(frozen=True, slots=True)
class NormalizedSymbolFact:
    qualified_name: str
    name: str
    kind: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    parameters: tuple[str, ...]
    returns: str | None
    complexity: NormalizedComplexityFact | None
    async_: bool = False
    generator: bool = False


@dataclass(frozen=True, slots=True)
class NormalizedImportFact:
    specifier: str
    kind: str
    dynamic: bool
    start_line: int
    start_column: int
    end_line: int
    end_column: int


@dataclass(frozen=True, slots=True)
class NormalizedExportFact:
    name: str
    kind: Literal["default", "named", "re-export", "commonjs"]
    local_name: str | None
    source_specifier: str | None
    start_line: int
    start_column: int
    end_line: int
    end_column: int


@dataclass(frozen=True, slots=True)
class NormalizedFunctionFact:
    qualified_name: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    body_start_byte: int
    body_end_byte: int
    parameters: tuple[str, ...]
    callees: tuple[str, ...]
    unresolved_calls: tuple[str, ...]
    control_events: tuple[str, ...]
    exceptions: tuple[str, ...]
    side_effects: tuple[str, ...]
    attributes_globals: tuple[str, ...]
    schema_operations: tuple[str, ...]
    statement_signatures: tuple[str, ...]
    detailed_statement_signatures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NormalizedStateFact:
    owner_qualified_name: str | None
    name: str
    class_state: bool
    start_line: int
    start_column: int
    end_line: int
    end_column: int


@dataclass(frozen=True, slots=True)
class FrontendResult:
    unit: SourceUnit
    symbols: tuple[NormalizedSymbolFact, ...]
    imports: tuple[NormalizedImportFact, ...]
    exports: tuple[NormalizedExportFact, ...]
    functions: tuple[NormalizedFunctionFact, ...]
    states: tuple[NormalizedStateFact, ...]
    module_system: ModuleSystem
    syntax_mode: str
    provenance: ParserProvenance
    diagnostics: tuple[Diagnostic, ...]
    completeness: Completeness


class LanguageFrontend(Protocol):
    frontend_id: str
    source_language: SourceLanguage

    @property
    def capabilities(self) -> tuple[AdapterCapabilityRecord, ...]: ...

    def availability(self, context: FrontendContext) -> ProviderAvailability: ...

    def analyze(self, unit: SourceUnit, context: FrontendContext) -> FrontendResult: ...
