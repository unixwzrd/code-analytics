"""Strict portable records for bounded static call occurrence evidence."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from code_analysis.models import Evidence, SourceLanguage, SourceSpan, StrictModel

MODULE_NAME_MAX_LENGTH = 512
SCOPE_NAME_MAX_LENGTH = 256
CALLEE_SPELLING_MAX_LENGTH = 512
LIMITATION_MAX_LENGTH = 256
LIMITATIONS_MAX_COUNT = 64

_KEYWORD_CALLEE_SEGMENTS = frozenset({"this", "super", "import"})
_OPERATOR_NAMES = frozenset(
    {
        "[]",
        "[]=",
        "+",
        "-",
        "*",
        "/",
        "%",
        "**",
        "<<",
        ">>",
        "&",
        "|",
        "^",
        "~",
        "+@",
        "-@",
        "<=>",
        "===",
        "==",
        "!=",
        "<=",
        ">=",
        "<",
        ">",
        "!",
    }
)
_FORBIDDEN_CALLEE_CHARS = frozenset("[](){}`\"'\\;,|&^~%/<>+*")


class StaticCallScopeKind(StrEnum):
    MODULE = "module"
    NAMESPACE = "namespace"
    CLASS = "class"
    FUNCTION = "function"
    ASYNC_FUNCTION = "async-function"
    GENERATOR_FUNCTION = "generator-function"
    LAMBDA = "lambda"
    COMPREHENSION = "comprehension"
    ARROW = "arrow"
    METHOD = "method"
    BLOCK = "block"
    ANNOTATION = "annotation"
    STATIC_INITIALIZER = "static-initializer"
    INSTANCE_INITIALIZER = "instance-initializer"


class StaticCallSyntaxKind(StrEnum):
    CALL = "call"
    CONSTRUCTOR = "constructor"
    TAGGED_TEMPLATE = "tagged-template"
    SUPER = "super"
    YIELD = "yield"


class StaticCallEvaluationContext(StrEnum):
    MODULE_BODY = "module-body"
    CLASS_BODY = "class-body"
    FUNCTION_BODY = "function-body"
    PARAMETER_DEFAULT = "parameter-default"
    DECORATOR = "decorator"
    ANNOTATION = "annotation"
    CLASS_BASE = "class-base"
    CLASS_KEYWORD = "class-keyword"
    LAMBDA_BODY = "lambda-body"
    LAMBDA_DEFAULT = "lambda-default"
    COMPREHENSION_OUTER_ITER = "comprehension-outer-iter"
    COMPREHENSION_INNER = "comprehension-inner"
    ARROW_BODY = "arrow-body"
    METHOD_BODY = "method-body"
    COMPUTED_KEY = "computed-key"
    STATIC_INITIALIZER = "static-initializer"
    INSTANCE_INITIALIZER = "instance-initializer"
    BLOCK_BODY = "block-body"
    UNKNOWN = "unknown"


class StaticCallSourceCoverageState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    TRUNCATED = "truncated"


def _require_sorted_unique(label: str, values: tuple[str, ...]) -> None:
    if list(values) != sorted(values) or len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique and sorted")


def _validate_limitations(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if len(values) > LIMITATIONS_MAX_COUNT:
        raise ValueError(f"{label} exceed the bounded count")
    for item in values:
        if item != item.strip() or not item:
            raise ValueError(f"{label} must be trimmed and nonempty")
        if len(item) > LIMITATION_MAX_LENGTH:
            raise ValueError(f"{label} exceed the bounded length")
    _require_sorted_unique(label, values)
    return values


def _validate_module_name(value: str) -> str:
    if value != value.strip() or not value:
        raise ValueError("module_name must be trimmed and nonempty")
    if len(value) > MODULE_NAME_MAX_LENGTH:
        raise ValueError("module_name exceeds the bounded length")
    return value


def _validate_scope_name(value: str | None) -> str | None:
    if value is None:
        return None
    if value != value.strip() or not value:
        raise ValueError("scope name must be trimmed and nonempty when present")
    if len(value) > SCOPE_NAME_MAX_LENGTH:
        raise ValueError("scope name exceeds the bounded length")
    if any(character in value for character in ('"', "'", "`", "(", ")", "{", "}")):
        raise ValueError("scope name cannot contain literal or grouping syntax")
    if value in _OPERATOR_NAMES:
        return value
    try:
        validate_callee_spelling(value)
    except ValueError as exc:
        raise ValueError("scope name must use identifier or operator syntax") from exc
    return value


def _validate_identifier_segment(segment: str) -> None:
    # Safe identifier spellings only; this does not establish language binding.
    if segment.startswith("@@"):
        segment = segment[2:]
    elif segment.startswith(("@", "#")):
        segment = segment[1:]
    if not segment.replace("$", "_").isidentifier():
        raise ValueError("callee_spelling segment must be a safe identifier")


def _validate_callee_segment(segment: str) -> None:
    if segment in _KEYWORD_CALLEE_SEGMENTS:
        return
    core = segment
    if segment.endswith("=") and len(segment) > 1:
        core = segment[:-1]
    elif segment.endswith("?") or segment.endswith("!"):
        if len(segment) == 1:
            raise ValueError("callee_spelling segment cannot be bare ? or !")
        core = segment[:-1]
    if "?" in core:
        raise ValueError("callee_spelling cannot use optional-chaining or interior ?")
    _validate_identifier_segment(core)


def validate_callee_spelling(value: str) -> str:
    if value != value.strip() or not value:
        raise ValueError("callee_spelling must be trimmed and nonempty")
    if len(value) > CALLEE_SPELLING_MAX_LENGTH:
        raise ValueError("callee_spelling exceeds the bounded length")
    if any(character in value for character in _FORBIDDEN_CALLEE_CHARS):
        raise ValueError("callee_spelling cannot contain unsafe expression syntax")
    if " " in value or "?." in value:
        raise ValueError("callee_spelling cannot contain unsafe expression syntax")
    dotted_parts = value.removeprefix("::").split(".")
    if not dotted_parts or any(not part for part in dotted_parts):
        raise ValueError("callee_spelling must be a safe identifier chain")
    for dotted_part in dotted_parts:
        for segment in dotted_part.split("::"):
            if not segment:
                raise ValueError("callee_spelling must be a safe identifier chain")
            _validate_callee_segment(segment)
    return value


def _span_is_forward_nonempty(span: SourceSpan) -> bool:
    if span.end_line < span.start_line:
        return False
    return not (span.end_line == span.start_line and span.end_column <= span.start_column)


class StaticCallScope(StrictModel):
    kind: StaticCallScopeKind
    name: StrictStr | None = Field(default=None, max_length=SCOPE_NAME_MAX_LENGTH)
    definition_line: StrictInt = Field(ge=1)
    definition_column: StrictInt = Field(ge=0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return _validate_scope_name(value)


def _scope_position(scope: StaticCallScope) -> tuple[int, int]:
    return (scope.definition_line, scope.definition_column)


class StaticCallOccurrence(StrictModel):
    occurrence_id: StrictStr = Field(pattern=r"^call-occurrence-[0-9a-f]{20}$")
    analysis_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    language: SourceLanguage
    source_file_id: StrictStr = Field(pattern=r"^source-file-[0-9a-f]{20}$")
    module_name: StrictStr = Field(min_length=1, max_length=MODULE_NAME_MAX_LENGTH)
    enclosing_subject_id: StrictStr | None = Field(default=None, pattern=r"^subject-[0-9a-f]{20}$")
    callee_spelling: StrictStr | None = Field(default=None, max_length=CALLEE_SPELLING_MAX_LENGTH)
    syntax_kind: StaticCallSyntaxKind
    evaluation_context: StaticCallEvaluationContext
    conditional: StrictBool
    deferred: StrictBool
    evaluation_unknown: StrictBool
    scope_path: tuple[StaticCallScope, ...] = Field(min_length=1, max_length=256)
    evidence: tuple[Evidence, ...] = Field(min_length=1, max_length=1)
    limitations: tuple[StrictStr, ...] = Field(default=(), max_length=LIMITATIONS_MAX_COUNT)

    @field_validator("module_name")
    @classmethod
    def validate_module_name_field(cls, value: str) -> str:
        return _validate_module_name(value)

    @field_validator("callee_spelling")
    @classmethod
    def validate_callee_spelling_field(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_callee_spelling(value)

    @field_validator("limitations")
    @classmethod
    def validate_limitations_field(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_limitations(value, label="static call occurrence limitations")

    @model_validator(mode="after")
    def validate_occurrence(self) -> Self:
        evidence = self.evidence[0]
        if evidence.language is not self.language:
            raise ValueError("static call occurrence language must match evidence language")
        if evidence.span.language is not self.language:
            raise ValueError("static call occurrence language must match evidence span language")
        if not _span_is_forward_nonempty(evidence.span):
            raise ValueError("static call evidence span must be forward with a nonempty call range")
        if self.scope_path[0].kind is not StaticCallScopeKind.MODULE:
            raise ValueError("static call scope path must begin with a module scope")
        module_scopes = [
            index
            for index, scope in enumerate(self.scope_path)
            if scope.kind is StaticCallScopeKind.MODULE
        ]
        if len(module_scopes) != 1:
            raise ValueError("static call scope path may contain only one module scope")
        previous = _scope_position(self.scope_path[0])
        for scope in self.scope_path[1:]:
            position = _scope_position(scope)
            if position < previous:
                raise ValueError("static call scope positions must be nondecreasing along the path")
            previous = position
        _validate_limitations(self.limitations, label="static call occurrence limitations")
        return self


class StaticCallSourceCoverage(StrictModel):
    source_file_id: StrictStr = Field(pattern=r"^source-file-[0-9a-f]{20}$")
    language: SourceLanguage
    state: StaticCallSourceCoverageState
    observed_count: StrictInt = Field(ge=0)
    retained_count: StrictInt = Field(ge=0)
    omitted_count: StrictInt = Field(ge=0)
    limitations: tuple[StrictStr, ...] = Field(default=(), max_length=LIMITATIONS_MAX_COUNT)

    @field_validator("limitations")
    @classmethod
    def validate_limitations_field(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_limitations(value, label="static call source coverage limitations")

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if self.observed_count != self.retained_count + self.omitted_count:
            raise ValueError(
                "static call coverage counts must reconcile observed, retained, and omitted"
            )
        if self.state is StaticCallSourceCoverageState.COMPLETE and self.omitted_count != 0:
            raise ValueError("complete static call coverage cannot omit occurrences")
        if (
            self.state
            in {
                StaticCallSourceCoverageState.FAILED,
                StaticCallSourceCoverageState.UNAVAILABLE,
                StaticCallSourceCoverageState.UNSUPPORTED,
            }
            and self.retained_count != 0
        ):
            raise ValueError(
                "failed, unavailable, or unsupported static call coverage cannot retain rows"
            )
        if self.state is StaticCallSourceCoverageState.TRUNCATED and self.omitted_count <= 0:
            raise ValueError("truncated static call coverage requires positive omissions")
        if self.state is StaticCallSourceCoverageState.PARTIAL and not self.limitations:
            raise ValueError("partial static call coverage requires an explicit limitation")
        if (
            self.state
            in {
                StaticCallSourceCoverageState.FAILED,
                StaticCallSourceCoverageState.UNAVAILABLE,
                StaticCallSourceCoverageState.UNSUPPORTED,
            }
            and not self.limitations
        ):
            raise ValueError(
                "failed, unavailable, or unsupported static call coverage requires limitations"
            )
        return self
