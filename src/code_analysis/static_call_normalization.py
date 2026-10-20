"""Portable normalization from language-owned static call occurrence batches."""

from __future__ import annotations

import hashlib
import json
import platform
from collections.abc import Mapping
from dataclasses import dataclass

from code_analysis.languages.javascript.call_occurrences import (
    CallExecutionDisposition as JavascriptCallExecutionDisposition,
)
from code_analysis.languages.javascript.call_occurrences import (
    JavascriptCallEvaluationContext,
    JavascriptCallOccurrenceFact,
    JavascriptCallSiteKind,
    JavascriptLexicalScopeKind,
    JavascriptLexicalScopeRecord,
)
from code_analysis.languages.python.call_occurrences import (
    CallExecutionDisposition as PythonCallExecutionDisposition,
)
from code_analysis.languages.python.call_occurrences import (
    LexicalScopeKind as PythonLexicalScopeKind,
)
from code_analysis.languages.python.call_occurrences import (
    PythonCallOccurrenceFact,
    PythonLexicalScopeRecord,
)
from code_analysis.languages.ruby.call_occurrences import (
    CallExecutionDisposition as RubyCallExecutionDisposition,
)
from code_analysis.languages.ruby.call_occurrences import (
    RubyCallEvaluationContext,
    RubyCallOccurrenceFact,
    RubyCallSiteKind,
    RubyLexicalScopeKind,
    RubyLexicalScopeRecord,
)
from code_analysis.static_call_models import (
    StaticCallEvaluationContext,
    StaticCallScope,
    StaticCallScopeKind,
    StaticCallSyntaxKind,
)

NORMALIZATION_IDENTITY = "static-call-normalization-2026-09-22"

_PYTHON_SCOPE_KIND = {
    PythonLexicalScopeKind.MODULE: StaticCallScopeKind.MODULE,
    PythonLexicalScopeKind.CLASS: StaticCallScopeKind.CLASS,
    PythonLexicalScopeKind.FUNCTION: StaticCallScopeKind.FUNCTION,
    PythonLexicalScopeKind.ASYNC_FUNCTION: StaticCallScopeKind.ASYNC_FUNCTION,
    PythonLexicalScopeKind.LAMBDA: StaticCallScopeKind.LAMBDA,
    PythonLexicalScopeKind.COMPREHENSION: StaticCallScopeKind.COMPREHENSION,
}

_JS_SCOPE_KIND = {
    JavascriptLexicalScopeKind.MODULE: StaticCallScopeKind.MODULE,
    JavascriptLexicalScopeKind.CLASS: StaticCallScopeKind.CLASS,
    JavascriptLexicalScopeKind.FUNCTION: StaticCallScopeKind.FUNCTION,
    JavascriptLexicalScopeKind.GENERATOR_FUNCTION: StaticCallScopeKind.GENERATOR_FUNCTION,
    JavascriptLexicalScopeKind.ASYNC_FUNCTION: StaticCallScopeKind.ASYNC_FUNCTION,
    JavascriptLexicalScopeKind.ARROW_FUNCTION: StaticCallScopeKind.ARROW,
    JavascriptLexicalScopeKind.METHOD: StaticCallScopeKind.METHOD,
}

_JS_EVALUATION_CONTEXT = {
    JavascriptCallEvaluationContext.MODULE_BODY: StaticCallEvaluationContext.MODULE_BODY,
    JavascriptCallEvaluationContext.CLASS_BODY: StaticCallEvaluationContext.CLASS_BODY,
    JavascriptCallEvaluationContext.CLASS_HERITAGE: StaticCallEvaluationContext.CLASS_BASE,
    JavascriptCallEvaluationContext.FUNCTION_BODY: StaticCallEvaluationContext.FUNCTION_BODY,
    JavascriptCallEvaluationContext.ARROW_BODY: StaticCallEvaluationContext.ARROW_BODY,
    JavascriptCallEvaluationContext.PARAMETER_DEFAULT: (
        StaticCallEvaluationContext.PARAMETER_DEFAULT
    ),
    JavascriptCallEvaluationContext.CLASS_STATIC_FIELD: (
        StaticCallEvaluationContext.STATIC_INITIALIZER
    ),
    JavascriptCallEvaluationContext.CLASS_STATIC_BLOCK: (
        StaticCallEvaluationContext.STATIC_INITIALIZER
    ),
    JavascriptCallEvaluationContext.CLASS_INSTANCE_FIELD: (
        StaticCallEvaluationContext.INSTANCE_INITIALIZER
    ),
    JavascriptCallEvaluationContext.COMPUTED_KEY: StaticCallEvaluationContext.COMPUTED_KEY,
    JavascriptCallEvaluationContext.DECORATOR: StaticCallEvaluationContext.DECORATOR,
}

_RUBY_SCOPE_KIND = {
    RubyLexicalScopeKind.CLASS: StaticCallScopeKind.CLASS,
    RubyLexicalScopeKind.METHOD: StaticCallScopeKind.METHOD,
    RubyLexicalScopeKind.BLOCK: StaticCallScopeKind.BLOCK,
    RubyLexicalScopeKind.LAMBDA: StaticCallScopeKind.LAMBDA,
}

_RUBY_EVALUATION_CONTEXT = {
    RubyCallEvaluationContext.MODULE_BODY: StaticCallEvaluationContext.MODULE_BODY,
    RubyCallEvaluationContext.CLASS_BODY: StaticCallEvaluationContext.CLASS_BODY,
    RubyCallEvaluationContext.SINGLETON_CLASS_BODY: StaticCallEvaluationContext.CLASS_BODY,
    RubyCallEvaluationContext.CLASS_INITIALIZATION: (
        StaticCallEvaluationContext.INSTANCE_INITIALIZER
    ),
    RubyCallEvaluationContext.CLASS_HERITAGE: StaticCallEvaluationContext.CLASS_BASE,
    RubyCallEvaluationContext.METHOD_BODY: StaticCallEvaluationContext.METHOD_BODY,
    RubyCallEvaluationContext.PARAMETER_DEFAULT: StaticCallEvaluationContext.PARAMETER_DEFAULT,
    RubyCallEvaluationContext.BLOCK_BODY: StaticCallEvaluationContext.BLOCK_BODY,
    RubyCallEvaluationContext.LAMBDA_BODY: StaticCallEvaluationContext.LAMBDA_BODY,
}


class StaticCallNormalizationError(ValueError):
    """Raised when a language-owned fact cannot be normalized safely."""


@dataclass(frozen=True, slots=True)
class PythonStaticCallProviderRecord:
    frontend_id: str
    parser: str
    python_version: str


@dataclass(frozen=True, slots=True)
class JavascriptStaticCallProviderRecord:
    available: bool
    grammar_abi: int | None
    grammar_version: str | None
    parser_version: str | None


@dataclass(frozen=True, slots=True)
class RubyStaticCallProviderRecord:
    provider_id: str
    availability_state: str
    ruby_version: str | None
    prism_version: str | None


@dataclass(frozen=True, slots=True)
class StaticCallProviderRecords:
    python: PythonStaticCallProviderRecord | None = None
    javascript: JavascriptStaticCallProviderRecord | None = None
    ruby: RubyStaticCallProviderRecord | None = None

    def identity_providers(self) -> dict[str, dict[str, object]]:
        providers: dict[str, dict[str, object]] = {}
        if self.python is not None:
            providers["python"] = {
                "frontend_id": self.python.frontend_id,
                "parser": self.python.parser,
                "python_version": self.python.python_version,
            }
        if self.javascript is not None:
            providers["javascript"] = {
                "available": self.javascript.available,
                "grammar_abi": self.javascript.grammar_abi,
                "grammar_version": self.javascript.grammar_version,
                "parser_version": self.javascript.parser_version,
            }
        if self.ruby is not None:
            providers["ruby"] = {
                "availability_state": self.ruby.availability_state,
                "prism_version": self.ruby.prism_version,
                "provider_id": self.ruby.provider_id,
                "ruby_version": self.ruby.ruby_version,
            }
        return providers


@dataclass(frozen=True, slots=True)
class NormalizedStaticCallFact:
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    callee_spelling: str | None
    syntax_kind: StaticCallSyntaxKind
    evaluation_context: StaticCallEvaluationContext
    conditional: bool
    deferred: bool
    evaluation_unknown: bool
    scope_path: tuple[StaticCallScope, ...]
    limitations: tuple[str, ...]


def compute_analysis_identity_sha256(
    *,
    tool_version: str,
    configuration_digest_sha256: str,
    providers: StaticCallProviderRecords | Mapping[str, Mapping[str, object]],
) -> str:
    if isinstance(providers, StaticCallProviderRecords):
        provider_payload = providers.identity_providers()
    else:
        provider_payload = {key: dict(providers[key]) for key in sorted(providers)}
    payload = {
        "configuration_digest_sha256": configuration_digest_sha256,
        "normalization_identity": NORMALIZATION_IDENTITY,
        "providers": {key: provider_payload[key] for key in sorted(provider_payload)},
        "python_version": platform.python_version(),
        "tool_version": tool_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_python_call_fact(fact: PythonCallOccurrenceFact) -> NormalizedStaticCallFact:
    scope_path = _normalize_python_scope_path(fact.scope_path)
    return NormalizedStaticCallFact(
        start_line=fact.start_line,
        start_column=fact.start_column,
        end_line=fact.end_line,
        end_column=fact.end_column,
        callee_spelling=fact.callee_dotted,
        syntax_kind=StaticCallSyntaxKind.CALL,
        evaluation_context=StaticCallEvaluationContext(fact.evaluation_context.value),
        conditional=fact.conditional,
        deferred=fact.deferred,
        evaluation_unknown=fact.execution_disposition is PythonCallExecutionDisposition.UNKNOWN,
        scope_path=scope_path,
        limitations=(),
    )


def normalize_javascript_call_fact(fact: JavascriptCallOccurrenceFact) -> NormalizedStaticCallFact:
    scope_path = _normalize_javascript_scope_path(fact.scope_path)
    evaluation_context = _JS_EVALUATION_CONTEXT.get(fact.evaluation_context)
    if evaluation_context is None:
        raise StaticCallNormalizationError(
            f"unsupported JavaScript evaluation context: {fact.evaluation_context.value}"
        )
    syntax_kind = (
        StaticCallSyntaxKind.CONSTRUCTOR
        if fact.site_kind is JavascriptCallSiteKind.NEW
        else StaticCallSyntaxKind.CALL
    )
    return NormalizedStaticCallFact(
        start_line=fact.start_line,
        start_column=fact.start_column,
        end_line=fact.end_line,
        end_column=fact.end_column,
        callee_spelling=fact.callee_dotted,
        syntax_kind=syntax_kind,
        evaluation_context=evaluation_context,
        conditional=fact.conditional,
        deferred=fact.deferred,
        evaluation_unknown=fact.execution_disposition is JavascriptCallExecutionDisposition.UNKNOWN,
        scope_path=scope_path,
        limitations=(),
    )


def normalize_ruby_call_fact(fact: RubyCallOccurrenceFact) -> NormalizedStaticCallFact:
    scope_path = _normalize_ruby_scope_path(fact.scope_path)
    evaluation_context = _RUBY_EVALUATION_CONTEXT.get(fact.evaluation_context)
    if evaluation_context is None:
        raise StaticCallNormalizationError(
            f"unsupported Ruby evaluation context: {fact.evaluation_context.value}"
        )
    syntax_kind = _ruby_syntax_kind(fact.site_kind)
    return NormalizedStaticCallFact(
        start_line=fact.start_line,
        start_column=fact.start_column,
        end_line=fact.end_line,
        end_column=fact.end_column,
        callee_spelling=fact.callee_spelling,
        syntax_kind=syntax_kind,
        evaluation_context=evaluation_context,
        conditional=fact.conditional,
        deferred=fact.deferred,
        evaluation_unknown=fact.execution_disposition is RubyCallExecutionDisposition.UNKNOWN,
        scope_path=scope_path,
        limitations=(),
    )


def _normalize_python_scope_path(
    scope_path: tuple[PythonLexicalScopeRecord, ...],
) -> tuple[StaticCallScope, ...]:
    normalized: list[StaticCallScope] = []
    for record in scope_path:
        kind = _PYTHON_SCOPE_KIND.get(record.kind)
        if kind is None:
            raise StaticCallNormalizationError(f"unsupported Python scope kind: {record.kind}")
        normalized.append(
            StaticCallScope(
                kind=kind,
                name=record.name,
                definition_line=record.definition_line,
                definition_column=record.definition_column,
            )
        )
    return tuple(normalized)


def _normalize_javascript_scope_path(
    scope_path: tuple[JavascriptLexicalScopeRecord, ...],
) -> tuple[StaticCallScope, ...]:
    normalized: list[StaticCallScope] = []
    for record in scope_path:
        kind = _JS_SCOPE_KIND.get(record.kind)
        if kind is None:
            raise StaticCallNormalizationError(
                f"unsupported JavaScript scope kind: {record.kind.value}"
            )
        normalized.append(
            StaticCallScope(
                kind=kind,
                name=record.name,
                definition_line=record.definition_line,
                definition_column=record.definition_column,
            )
        )
    return tuple(normalized)


def _normalize_ruby_scope_path(
    scope_path: tuple[RubyLexicalScopeRecord, ...],
) -> tuple[StaticCallScope, ...]:
    normalized: list[StaticCallScope] = []
    for record in scope_path:
        kind: StaticCallScopeKind | None
        if record.kind is RubyLexicalScopeKind.MODULE:
            kind = StaticCallScopeKind.NAMESPACE if record.name else StaticCallScopeKind.MODULE
        else:
            kind = _RUBY_SCOPE_KIND.get(record.kind)
            if kind is None:
                raise StaticCallNormalizationError(f"unsupported Ruby scope kind: {record.kind}")
        normalized.append(
            StaticCallScope(
                kind=kind,
                name=record.name,
                definition_line=record.definition_line,
                definition_column=record.definition_column,
            )
        )
    return tuple(normalized)


def _ruby_syntax_kind(site_kind: RubyCallSiteKind) -> StaticCallSyntaxKind:
    if site_kind is RubyCallSiteKind.CALL_NODE:
        return StaticCallSyntaxKind.CALL
    if site_kind in {RubyCallSiteKind.SUPER_NODE, RubyCallSiteKind.FORWARDING_SUPER_NODE}:
        return StaticCallSyntaxKind.SUPER
    if site_kind is RubyCallSiteKind.YIELD_NODE:
        return StaticCallSyntaxKind.YIELD
    raise StaticCallNormalizationError(f"unsupported Ruby call site kind: {site_kind.value}")


__all__ = [
    "NORMALIZATION_IDENTITY",
    "JavascriptStaticCallProviderRecord",
    "NormalizedStaticCallFact",
    "PythonStaticCallProviderRecord",
    "RubyStaticCallProviderRecord",
    "StaticCallNormalizationError",
    "StaticCallProviderRecords",
    "compute_analysis_identity_sha256",
    "normalize_javascript_call_fact",
    "normalize_python_call_fact",
    "normalize_ruby_call_fact",
]
