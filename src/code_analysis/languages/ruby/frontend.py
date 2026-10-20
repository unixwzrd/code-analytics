"""External Ruby 3.3+/Prism front end using a tool-owned JSON driver."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from pydantic import Field, StrictInt, StrictStr

from code_analysis.languages.base import (
    FrontendContext,
    FrontendResult,
    NormalizedComplexityFact,
    NormalizedFunctionFact,
    NormalizedImportFact,
    NormalizedSymbolFact,
    SourceUnit,
)
from code_analysis.languages.ruby.call_occurrences import RubyCallOccurrenceBatchPayload
from code_analysis.models import (
    AvailabilityState,
    Completeness,
    Diagnostic,
    DiagnosticLevel,
    ModuleSystem,
    ParserProvenance,
    ProviderAvailability,
    SourceLanguage,
    StrictModel,
)
from code_analysis.report_models import (
    AdapterCapabilityKind,
    AdapterCapabilityRecord,
    AdapterCapabilityState,
)

_VERSION = re.compile(r"ruby (?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)")


class _RubySymbol(StrictModel):
    qualified_name: StrictStr = Field(min_length=1)
    name: StrictStr = Field(min_length=1)
    kind: StrictStr = Field(min_length=1)
    start_line: StrictInt = Field(ge=1)
    start_column: StrictInt = Field(ge=0)
    end_line: StrictInt = Field(ge=1)
    end_column: StrictInt = Field(ge=0)
    parameters: tuple[StrictStr, ...] = ()


class _RubyImport(StrictModel):
    specifier: StrictStr = Field(min_length=1)
    kind: StrictStr = Field(min_length=1)
    start_line: StrictInt = Field(ge=1)
    start_column: StrictInt = Field(ge=0)
    end_line: StrictInt = Field(ge=1)
    end_column: StrictInt = Field(ge=0)


class _RubyCall(StrictModel):
    identity: StrictStr = Field(min_length=1)
    owner: StrictStr
    start_line: StrictInt = Field(ge=1)
    start_column: StrictInt = Field(ge=0)
    end_line: StrictInt = Field(ge=1)
    end_column: StrictInt = Field(ge=0)


class _RubySpan(StrictModel):
    owner: StrictStr
    start_line: StrictInt = Field(ge=1)
    end_line: StrictInt = Field(ge=1)


class _RubyException(StrictModel):
    kind: StrictStr = Field(min_length=1)
    owner: StrictStr
    start_line: StrictInt = Field(ge=1)


class _RubyError(StrictModel):
    message: StrictStr = Field(min_length=1)
    start_line: StrictInt = Field(ge=1)
    start_column: StrictInt = Field(ge=0)


class _PrismDocument(StrictModel):
    prism_version: StrictStr = Field(min_length=1)
    symbols: tuple[_RubySymbol, ...] = ()
    imports: tuple[_RubyImport, ...] = ()
    calls: tuple[_RubyCall, ...] = ()
    hashes: tuple[_RubySpan, ...] = ()
    exceptions: tuple[_RubyException, ...] = ()
    effects: tuple[StrictStr, ...] = ()
    errors: tuple[_RubyError, ...] = ()
    call_occurrences: RubyCallOccurrenceBatchPayload


class RubyPrismFrontend:
    frontend_id = "ruby-prism"
    source_language = SourceLanguage.RUBY

    @property
    def capabilities(self) -> tuple[AdapterCapabilityRecord, ...]:
        partial = {
            AdapterCapabilityKind.DATA_FLOW: "Ruby data flow is syntax-bounded.",
            AdapterCapabilityKind.CONTRACTS_SHAPES: (
                "Ruby hash and keyword evidence does not infer framework contracts."
            ),
            AdapterCapabilityKind.CLONE_NORMALIZATION: (
                "Ruby clone normalization initially retains method call sequences."
            ),
        }
        unsupported = {
            AdapterCapabilityKind.TEST_DISCOVERY: (
                "Ruby framework-specific test discovery is unsupported."
            ),
            AdapterCapabilityKind.COVERAGE_BINDING: "Ruby coverage binding is unsupported.",
        }
        return tuple(
            AdapterCapabilityRecord(
                capability=kind,
                state=(
                    AdapterCapabilityState.PARTIAL
                    if kind in partial
                    else (
                        AdapterCapabilityState.UNSUPPORTED
                        if kind in unsupported
                        else AdapterCapabilityState.SUPPORTED
                    )
                ),
                limitations=(
                    (partial[kind],)
                    if kind in partial
                    else ((unsupported[kind],) if kind in unsupported else ())
                ),
            )
            for kind in AdapterCapabilityKind
        )

    def availability(self, context: FrontendContext) -> ProviderAvailability:
        del context
        executable = shutil.which("ruby")
        if executable is None:
            return ProviderAvailability(
                provider_id=self.frontend_id,
                state=AvailabilityState.UNAVAILABLE,
                detail="Ruby executable was not found",
            )
        try:
            version = subprocess.run(
                [executable, "-v"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            match = _VERSION.search(version.stdout or version.stderr)
            prism = subprocess.run(
                [executable, "-rprism", "-e", "print Prism::VERSION"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProviderAvailability(
                provider_id=self.frontend_id,
                state=AvailabilityState.UNAVAILABLE,
                executable=executable,
                detail=f"Ruby/Prism availability failed: {type(exc).__name__}",
            )
        if (
            version.returncode
            or prism.returncode
            or match is None
            or (int(match["major"]), int(match["minor"])) < (3, 3)
        ):
            return ProviderAvailability(
                provider_id=self.frontend_id,
                state=AvailabilityState.UNAVAILABLE,
                executable=executable,
                version=(match.group(0) if match else None),
                detail="Ruby 3.3+ with a loadable Prism API is required",
            )
        return ProviderAvailability(
            provider_id=self.frontend_id,
            state=AvailabilityState.AVAILABLE,
            executable=executable,
            version=match.group(0),
            detail=f"Prism {prism.stdout.strip()}",
        )

    def analyze(self, unit: SourceUnit, context: FrontendContext) -> FrontendResult:
        availability = self.availability(context)
        if availability.state is AvailabilityState.UNAVAILABLE or availability.executable is None:
            return _failed(
                unit, context, "ruby-prism-unavailable", availability.detail or "unavailable"
            )
        driver = Path(__file__).with_name("prism_driver.rb")
        try:
            completed = subprocess.run(
                [availability.executable, str(driver)],
                input=unit.source_bytes,
                check=False,
                capture_output=True,
                timeout=min(context.config.runtime.timeout_seconds, 30.0),
            )
        except subprocess.TimeoutExpired:
            return _failed(unit, context, "ruby-prism-timeout", "Ruby/Prism driver timed out")
        except OSError as exc:
            return _failed(
                unit,
                context,
                "ruby-prism-execution-error",
                f"Ruby/Prism driver failed: {type(exc).__name__}",
            )
        if completed.returncode:
            return _failed(
                unit,
                context,
                "ruby-prism-nonzero",
                f"Ruby/Prism driver exited with status {completed.returncode}",
            )
        try:
            document = _PrismDocument.model_validate_json(completed.stdout)
        except ValueError:
            return _failed(
                unit, context, "ruby-prism-malformed", "Ruby/Prism output failed strict validation"
            )
        diagnostics = tuple(
            Diagnostic(
                code="ruby-syntax-error",
                level=DiagnosticLevel.ERROR,
                message=f"syntax error at line {item.start_line}: {item.message}",
                path=unit.logical_path,
                provider=self.frontend_id,
                language=SourceLanguage.RUBY,
            )
            for item in document.errors
        )
        symbols = tuple(_symbol(item, document) for item in document.symbols)
        functions = tuple(
            _function(unit, item, document) for item in document.symbols if item.kind == "method"
        )
        return FrontendResult(
            unit=unit,
            symbols=symbols,
            imports=tuple(
                NormalizedImportFact(
                    specifier=item.specifier,
                    kind=item.kind,
                    dynamic=False,
                    start_line=item.start_line,
                    start_column=item.start_column,
                    end_line=item.end_line,
                    end_column=item.end_column,
                )
                for item in document.imports
            ),
            exports=(),
            functions=functions,
            states=(),
            module_system=ModuleSystem.UNRESOLVED,
            syntax_mode="ruby",
            provenance=_provenance(unit, context, document.prism_version),
            diagnostics=diagnostics,
            completeness=Completeness.PARTIAL if diagnostics else Completeness.COMPLETE,
        )


def _symbol(item: _RubySymbol, document: _PrismDocument) -> NormalizedSymbolFact:
    physical = item.end_line - item.start_line + 1
    calls = tuple(call for call in document.calls if call.owner == item.qualified_name)
    exceptions = tuple(value for value in document.exceptions if value.owner == item.qualified_name)
    kind = item.kind if item.kind in {"module", "class"} else "method"
    return NormalizedSymbolFact(
        qualified_name=item.qualified_name,
        name=item.name,
        kind=kind,
        start_line=item.start_line,
        start_column=item.start_column,
        end_line=item.end_line,
        end_column=item.end_column,
        parameters=item.parameters,
        returns=None,
        complexity=(
            None
            if kind in {"module", "class"}
            else NormalizedComplexityFact(
                physical_loc=physical,
                logical_loc=len(calls) + len(exceptions),
                branches=sum(value.kind == "rescue" for value in exceptions),
                nesting_depth=0,
                argument_count=len(item.parameters),
                cyclomatic_complexity=1 + sum(value.kind == "rescue" for value in exceptions),
                weighted_score=physical // 25 + len(calls) + len(exceptions),
                returns=0,
                raises=sum(value.kind == "raise" for value in exceptions),
            )
        ),
    )


def _line_byte(source: bytes, line: int) -> int:
    if line <= 1:
        return 0
    return sum(len(value) for value in source.splitlines(keepends=True)[: line - 1])


def _function(
    unit: SourceUnit, item: _RubySymbol, document: _PrismDocument
) -> NormalizedFunctionFact:
    calls = tuple(call for call in document.calls if call.owner == item.qualified_name)
    exceptions = tuple(value for value in document.exceptions if value.owner == item.qualified_name)
    hashes = tuple(value for value in document.hashes if value.owner == item.qualified_name)
    events = tuple(
        sorted(
            (
                *("call:" + call.identity for call in calls),
                *("exception:" + value.kind for value in exceptions),
            )
        )
    )
    return NormalizedFunctionFact(
        qualified_name=item.qualified_name,
        start_line=item.start_line,
        start_column=item.start_column,
        end_line=item.end_line,
        end_column=item.end_column,
        body_start_byte=_line_byte(unit.source_bytes, item.start_line),
        body_end_byte=_line_byte(unit.source_bytes, item.end_line + 1),
        parameters=item.parameters,
        callees=tuple(sorted({call.identity for call in calls})),
        unresolved_calls=(),
        control_events=tuple(sorted({value.kind for value in exceptions})),
        exceptions=tuple(sorted({value.kind for value in exceptions})),
        side_effects=tuple(sorted(set(document.effects) & {call.identity for call in calls})),
        attributes_globals=(),
        schema_operations=("hash-literal",) if hashes else (),
        statement_signatures=events,
        detailed_statement_signatures=events,
    )


def _provenance(unit: SourceUnit, context: FrontendContext, prism: str) -> ParserProvenance:
    return ParserProvenance(
        frontend_id="ruby-prism",
        logical_path=unit.logical_path,
        parser_name="ruby",
        parser_version="3.3+",
        grammar_name="prism",
        grammar_version=prism,
        grammar_abi=None,
        language=SourceLanguage.RUBY,
        syntax_mode=None,
        module_system=None,
        configuration_sha256=context.configuration_sha256,
        source_sha256=unit.source_sha256,
        normalization_identity="ruby-prism:v1",
    )


def _failed(unit: SourceUnit, context: FrontendContext, code: str, message: str) -> FrontendResult:
    diagnostic = Diagnostic(
        code=code,
        level=DiagnosticLevel.ERROR,
        message=message,
        path=unit.logical_path,
        provider="ruby-prism",
        language=SourceLanguage.RUBY,
    )
    return FrontendResult(
        unit=unit,
        symbols=(),
        imports=(),
        exports=(),
        functions=(),
        states=(),
        module_system=ModuleSystem.UNRESOLVED,
        syntax_mode="ruby",
        provenance=_provenance(unit, context, "unavailable"),
        diagnostics=(diagnostic,),
        completeness=Completeness.FAILED,
    )
