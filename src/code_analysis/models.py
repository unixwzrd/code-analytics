from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Completeness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class SourceLanguage(StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    RUBY = "ruby"


class JavaScriptSyntaxMode(StrEnum):
    JAVASCRIPT = "javascript"
    JSX = "jsx"


class ModuleSystem(StrEnum):
    ESM = "esm"
    COMMONJS = "commonjs"
    SCRIPT = "script"
    MIXED = "mixed"
    UNRESOLVED = "unresolved"


class SourceParseState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    TRUNCATED = "truncated"


class Confidence(StrEnum):
    EXACT = "exact"
    RESOLVED = "resolved"
    INFERRED = "inferred"
    HEURISTIC = "heuristic"
    RUNTIME_OBSERVED = "runtime-observed"


class ProviderState(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    NONZERO = "nonzero"
    MALFORMED = "malformed"
    FAILED = "failed"


class AvailabilityState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class DiagnosticLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Diagnostic(StrictModel):
    code: StrictStr
    level: DiagnosticLevel
    message: StrictStr
    path: StrictStr | None = None
    provider: StrictStr | None = None
    language: SourceLanguage | None = None


class ParserProvenance(StrictModel):
    frontend_id: StrictStr
    logical_path: StrictStr = Field(min_length=1)
    parser_name: StrictStr
    parser_version: StrictStr
    grammar_name: StrictStr
    grammar_version: StrictStr
    grammar_abi: StrictInt | None = Field(default=None, ge=1)
    language: SourceLanguage
    syntax_mode: JavaScriptSyntaxMode | None = None
    module_system: ModuleSystem | None = None
    configuration_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    normalization_identity: StrictStr


class ProviderAvailability(StrictModel):
    provider_id: StrictStr
    state: AvailabilityState
    executable: StrictStr | None = None
    version: StrictStr | None = None
    detail: StrictStr | None = None


class ProviderResult(StrictModel):
    provider_id: StrictStr
    state: ProviderState
    executable: StrictStr | None = None
    version: StrictStr | None = None
    exit_code: StrictInt | None = None
    duration_ms: StrictInt = Field(ge=0)
    stdout_sha256: StrictStr | None = None
    stderr_sha256: StrictStr | None = None
    observations: tuple[StrictStr, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    completeness: Completeness


class SourceSpan(StrictModel):
    path: StrictStr
    start_line: StrictInt = Field(ge=1)
    start_column: StrictInt = Field(ge=0)
    end_line: StrictInt = Field(ge=1)
    end_column: StrictInt = Field(ge=0)
    language: SourceLanguage = SourceLanguage.PYTHON


class ComplexityObservation(StrictModel):
    physical_loc: StrictInt = Field(ge=0)
    logical_loc: StrictInt = Field(ge=0)
    branches: StrictInt = Field(ge=0)
    nesting_depth: StrictInt = Field(ge=0)
    argument_count: StrictInt = Field(ge=0)
    cyclomatic_complexity: StrictInt = Field(ge=1)
    weighted_score: StrictInt = Field(ge=0)
    returns: StrictInt = Field(ge=0)
    raises: StrictInt = Field(ge=0)
    language: SourceLanguage = SourceLanguage.PYTHON


class Symbol(StrictModel):
    qualified_name: StrictStr
    name: StrictStr
    kind: Literal[
        "class",
        "module",
        "function",
        "method",
        "getter",
        "setter",
        "constructor",
        "field",
        "nested_function",
        "nested_class",
    ]
    span: SourceSpan
    decorators: tuple[StrictStr, ...] = ()
    parameters: tuple[StrictStr, ...] = ()
    returns: StrictStr | None = None
    complexity: ComplexityObservation | None = None
    language: SourceLanguage = SourceLanguage.PYTHON
    async_: StrictBool = False
    generator: StrictBool = False


class Module(StrictModel):
    name: StrictStr
    path: StrictStr
    source_sha256: StrictStr
    line_count: StrictInt = Field(ge=0)
    nonblank_line_count: StrictInt = Field(default=0, ge=0)
    symbols: tuple[Symbol, ...] = ()
    fan_in: StrictInt = Field(default=0, ge=0)
    fan_out: StrictInt = Field(default=0, ge=0)
    language: SourceLanguage = SourceLanguage.PYTHON
    parse_state: SourceParseState = SourceParseState.COMPLETE
    syntax_mode: JavaScriptSyntaxMode | None = None
    module_system: ModuleSystem | None = None

    @model_validator(mode="after")
    def validate_language(self) -> Module:
        if any(symbol.language is not self.language for symbol in self.symbols):
            raise ValueError("module symbols must use the module source language")
        if self.language is not SourceLanguage.JAVASCRIPT and (
            self.syntax_mode is not None or self.module_system is not None
        ):
            raise ValueError("non-JavaScript modules cannot carry JavaScript modes")
        return self


class ImportEdge(StrictModel):
    source: StrictStr
    target: StrictStr
    local: StrictBool
    kind: Literal[
        "runtime",
        "type_checking",
        "optional",
        "lazy",
        "esm-static",
        "esm-dynamic",
        "commonjs",
        "dynamic-unresolved",
    ] = "runtime"
    source_language: SourceLanguage = SourceLanguage.PYTHON
    target_language: SourceLanguage | None = SourceLanguage.PYTHON


class StrongComponent(StrictModel):
    modules: tuple[StrictStr, ...]


class ImportCycle(StrictModel):
    modules: tuple[StrictStr, ...]


class Evidence(StrictModel):
    span: SourceSpan
    provider: StrictStr
    observation: StrictStr
    confidence: Confidence
    source_sha256: StrictStr
    language: SourceLanguage = SourceLanguage.PYTHON

    @model_validator(mode="after")
    def validate_language(self) -> Evidence:
        if self.span.language is not self.language:
            raise ValueError("evidence language must match its source span")
        return self


def evidence_identity(evidence: Evidence) -> str:
    """Return the catalog identity for one normalized evidence observation."""

    payload = json.dumps(
        evidence.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"evidence-{hashlib.sha256(payload).hexdigest()[:20]}"


class StaticFinding(StrictModel):
    finding_id: StrictStr
    rule_id: StrictStr
    category: Literal["complexity", "orchestration", "import-cycle"]
    severity: Literal["info", "warning", "error"]
    title: StrictStr
    explanation: StrictStr
    module: StrictStr
    symbol: StrictStr | None = None
    evidence: tuple[Evidence, ...]


class TargetIdentity(StrictModel):
    logical_root: StrictStr
    source_roots: tuple[StrictStr, ...]
    target_kind: Literal["file", "directory"]
    languages: tuple[SourceLanguage, ...] = (SourceLanguage.PYTHON,)


class EnvironmentProvenance(StrictModel):
    python_implementation: StrictStr
    python_version: StrictStr
    executable_identity: StrictStr


class CommandIdentity(StrictModel):
    name: Literal["scan", "contracts", "dict-shapes", "clones", "refactor"]
    arguments: tuple[StrictStr, ...]


class ThresholdIdentity(StrictModel):
    function_loc: StrictInt = Field(ge=1)
    branches: StrictInt = Field(ge=0)
    nesting: StrictInt = Field(ge=0)
    arguments: StrictInt = Field(ge=0)
    weighted_score: StrictInt = Field(ge=0)


class ConfigurationIdentity(StrictModel):
    digest_sha256: StrictStr
    strictness: StrictStr
    thresholds: ThresholdIdentity
    only_local_imports: StrictBool
    max_findings: StrictInt | None = None
    symbol_filter: StrictStr | None = None


class StaticSummaryCounts(StrictModel):
    discovered_files: StrictInt = Field(ge=0)
    parsed_files: StrictInt = Field(ge=0)
    failed_files: StrictInt = Field(ge=0)
    modules: StrictInt = Field(ge=0)
    classes: StrictInt = Field(ge=0)
    functions: StrictInt = Field(ge=0)
    methods: StrictInt = Field(ge=0)
    import_edges: StrictInt = Field(ge=0)
    strongly_connected_components: StrictInt = Field(ge=0)
    cycles: StrictInt = Field(ge=0)
    findings: StrictInt = Field(ge=0)


class StaticAnalysisResult(StrictModel):
    tool_version: StrictStr
    command: CommandIdentity
    target: TargetIdentity
    environment: EnvironmentProvenance
    configuration: ConfigurationIdentity
    modules: tuple[Module, ...]
    complexity_observations: tuple[ComplexityObservation, ...]
    import_edges: tuple[ImportEdge, ...]
    strongly_connected_components: tuple[StrongComponent, ...]
    cycles: tuple[ImportCycle, ...]
    findings: tuple[StaticFinding, ...]
    diagnostics: tuple[Diagnostic, ...]
    providers: tuple[ProviderResult, ...]
    completeness: Completeness
    limitations: tuple[StrictStr, ...]
    summary: StaticSummaryCounts
