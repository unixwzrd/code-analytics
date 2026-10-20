"""Bounded syntactic Ruby call/super/yield occurrence facts from Prism driver JSON."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.models import StrictModel

RubyDriverSiteKind = Literal[
    "call_node",
    "super_node",
    "forwarding_super_node",
    "yield_node",
]

RubyDriverScopeKind = Literal["module", "class", "method", "block", "lambda"]

RubyDriverEvaluationContext = Literal[
    "module-body",
    "class-body",
    "singleton-class-body",
    "class-initialization",
    "class-heritage",
    "method-body",
    "parameter-default",
    "block-body",
    "lambda-body",
]

RubyDriverExecutionDisposition = Literal["eager", "deferred", "conditional", "unknown"]

RubyDriverLimitation = Literal[
    "syntactic-context-is-not-runtime-execution-or-call-order",
    "callee-spelling-does-not-establish-name-binding-or-dispatch",
    "operator-callee-spelling-is-not-representable-as-identifier-chain",
    "truncated-at-max-occurrences",
    "parse-contains-errors",
]


class RubyLexicalScopeKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    METHOD = "method"
    BLOCK = "block"
    LAMBDA = "lambda"


class RubyCallSiteKind(StrEnum):
    CALL_NODE = "call"
    SUPER_NODE = "super"
    FORWARDING_SUPER_NODE = "forwarding_super"
    YIELD_NODE = "yield"


class RubyCallEvaluationContext(StrEnum):
    MODULE_BODY = "module-body"
    CLASS_BODY = "class-body"
    SINGLETON_CLASS_BODY = "singleton-class-body"
    CLASS_INITIALIZATION = "class-initialization"
    CLASS_HERITAGE = "class-heritage"
    METHOD_BODY = "method-body"
    PARAMETER_DEFAULT = "parameter-default"
    BLOCK_BODY = "block-body"
    LAMBDA_BODY = "lambda-body"


class CallExecutionDisposition(StrEnum):
    EAGER = "eager"
    DEFERRED = "deferred"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


_SITE_KIND_FROM_DRIVER: dict[RubyDriverSiteKind, RubyCallSiteKind] = {
    "call_node": RubyCallSiteKind.CALL_NODE,
    "super_node": RubyCallSiteKind.SUPER_NODE,
    "forwarding_super_node": RubyCallSiteKind.FORWARDING_SUPER_NODE,
    "yield_node": RubyCallSiteKind.YIELD_NODE,
}


class RubyCallOccurrenceScopePayload(StrictModel):
    kind: RubyDriverScopeKind
    name: StrictStr | None = None
    definition_line: StrictInt = Field(ge=1)
    definition_column: StrictInt = Field(ge=0)


class RubyCallOccurrenceFactPayload(StrictModel):
    site_kind: RubyDriverSiteKind
    start_byte: StrictInt = Field(ge=0)
    end_byte: StrictInt = Field(ge=0)
    start_line: StrictInt = Field(ge=1)
    start_column: StrictInt = Field(ge=0)
    end_line: StrictInt = Field(ge=1)
    end_column: StrictInt = Field(ge=0)
    callee_spelling: StrictStr | None = None
    scope_path: tuple[RubyCallOccurrenceScopePayload, ...]
    evaluation_context: RubyDriverEvaluationContext
    execution_disposition: RubyDriverExecutionDisposition
    conditional: StrictBool
    deferred: StrictBool

    @model_validator(mode="after")
    def validate_span(self) -> RubyCallOccurrenceFactPayload:
        if self.end_byte < self.start_byte:
            raise ValueError("end_byte must not precede start_byte")
        if self.callee_spelling is not None and not self.callee_spelling:
            raise ValueError("callee_spelling must be null or a non-empty string")
        return self


class RubyCallOccurrenceBatchPayload(StrictModel):
    occurrences: tuple[RubyCallOccurrenceFactPayload, ...] = ()
    total_observed: StrictInt = Field(ge=0)
    total_retained: StrictInt = Field(ge=0)
    total_omitted: StrictInt = Field(ge=0)
    parse_complete: StrictBool
    limitations: tuple[RubyDriverLimitation, ...] = ()

    @model_validator(mode="after")
    def validate_batch(self) -> RubyCallOccurrenceBatchPayload:
        if self.total_retained != len(self.occurrences):
            raise ValueError("total_retained must match occurrences length")
        if self.total_observed != self.total_retained + self.total_omitted:
            raise ValueError("observed counts must reconcile retained and omitted")
        ordered = tuple(sorted(set(self.limitations)))
        if ordered != self.limitations:
            raise ValueError("limitations must be unique and sorted")
        if self.total_omitted > 0 and "truncated-at-max-occurrences" not in self.limitations:
            raise ValueError("truncated batches must include truncated-at-max-occurrences")
        if not self.parse_complete and "parse-contains-errors" not in self.limitations:
            raise ValueError("incomplete parse batches must include parse-contains-errors")
        return self


@dataclass(frozen=True, slots=True)
class RubyLexicalScopeRecord:
    kind: RubyLexicalScopeKind
    name: str | None
    definition_line: int
    definition_column: int

    def __post_init__(self) -> None:
        if isinstance(self.definition_line, bool) or not isinstance(self.definition_line, int):
            raise TypeError("definition_line must be an exact int")
        if isinstance(self.definition_column, bool) or not isinstance(self.definition_column, int):
            raise TypeError("definition_column must be an exact int")
        if self.definition_line < 1:
            raise ValueError("definition_line must be positive")
        if self.definition_column < 0:
            raise ValueError("definition_column must be nonnegative")


@dataclass(frozen=True, slots=True)
class RubyCallOccurrenceFact:
    site_kind: RubyCallSiteKind
    start_byte: int
    end_byte: int
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    callee_spelling: str | None
    scope_path: tuple[RubyLexicalScopeRecord, ...]
    evaluation_context: RubyCallEvaluationContext
    execution_disposition: CallExecutionDisposition
    conditional: bool
    deferred: bool

    def __post_init__(self) -> None:
        for name in (
            "start_byte",
            "end_byte",
            "start_line",
            "start_column",
            "end_line",
            "end_column",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an exact int")
        if self.end_byte < self.start_byte:
            raise ValueError("end_byte must not precede start_byte")


@dataclass(frozen=True, slots=True)
class RubyCallOccurrenceBatch:
    occurrences: tuple[RubyCallOccurrenceFact, ...]
    total_observed: int
    total_retained: int
    total_omitted: int
    parse_complete: bool
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.total_retained != len(self.occurrences):
            raise ValueError("total_retained must match occurrences length")
        if self.total_observed != self.total_retained + self.total_omitted:
            raise ValueError("observed counts must reconcile retained and omitted")
        if tuple(sorted(set(self.limitations))) != self.limitations:
            raise ValueError("limitations must be unique and sorted")


def ruby_call_occurrence_batch(payload: RubyCallOccurrenceBatchPayload) -> RubyCallOccurrenceBatch:
    occurrences = tuple(_occurrence_fact(item) for item in payload.occurrences)
    return RubyCallOccurrenceBatch(
        occurrences=occurrences,
        total_observed=payload.total_observed,
        total_retained=payload.total_retained,
        total_omitted=payload.total_omitted,
        parse_complete=payload.parse_complete,
        limitations=payload.limitations,
    )


def _occurrence_fact(item: RubyCallOccurrenceFactPayload) -> RubyCallOccurrenceFact:
    scope_path = tuple(_scope_record(record) for record in item.scope_path)
    return RubyCallOccurrenceFact(
        site_kind=_SITE_KIND_FROM_DRIVER[item.site_kind],
        start_byte=item.start_byte,
        end_byte=item.end_byte,
        start_line=item.start_line,
        start_column=item.start_column,
        end_line=item.end_line,
        end_column=item.end_column,
        callee_spelling=item.callee_spelling,
        scope_path=scope_path,
        evaluation_context=RubyCallEvaluationContext(item.evaluation_context),
        execution_disposition=CallExecutionDisposition(item.execution_disposition),
        conditional=item.conditional,
        deferred=item.deferred,
    )


def _scope_record(record: RubyCallOccurrenceScopePayload) -> RubyLexicalScopeRecord:
    return RubyLexicalScopeRecord(
        kind=RubyLexicalScopeKind(record.kind),
        name=record.name,
        definition_line=record.definition_line,
        definition_column=record.definition_column,
    )


@dataclass(frozen=True, slots=True)
class RubyStaticCallDriverResult:
    batch: RubyCallOccurrenceBatch | None
    prism_version: str | None
    failure_code: str | None
    failure_detail: str | None


def static_call_batch_from_driver(
    executable: str,
    source_bytes: bytes,
    *,
    max_occurrences: int,
    timeout_seconds: float,
) -> RubyStaticCallDriverResult:
    """Run the tool-owned Prism driver on captured bytes without shell invocation."""

    import subprocess
    from pathlib import Path

    driver = Path(__file__).with_name("prism_driver.rb")
    if max_occurrences <= 0:
        raise ValueError("max_occurrences must be positive")
    try:
        completed = subprocess.run(
            [executable, str(driver), str(max_occurrences)],
            input=source_bytes,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return RubyStaticCallDriverResult(None, None, "ruby-prism-timeout", "driver timed out")
    except OSError as exc:
        return RubyStaticCallDriverResult(
            None,
            None,
            "ruby-prism-execution-error",
            f"driver failed: {type(exc).__name__}",
        )
    if completed.returncode:
        return RubyStaticCallDriverResult(
            None,
            None,
            "ruby-prism-nonzero",
            f"driver exited with status {completed.returncode}",
        )
    try:
        from pydantic import ValidationError

        from code_analysis.languages.ruby.frontend import _PrismDocument

        document = _PrismDocument.model_validate_json(completed.stdout)
    except ValidationError:
        return RubyStaticCallDriverResult(
            None,
            None,
            "ruby-prism-malformed",
            "driver output failed validation: ValidationError",
        )
    return RubyStaticCallDriverResult(
        ruby_call_occurrence_batch(document.call_occurrences),
        document.prism_version,
        None,
        None,
    )
