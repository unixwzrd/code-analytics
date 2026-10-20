from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from code_analysis.models import Confidence, Evidence, SourceLanguage, SourceSpan
from code_analysis.static_call_models import (
    StaticCallEvaluationContext,
    StaticCallOccurrence,
    StaticCallScope,
    StaticCallScopeKind,
    StaticCallSourceCoverage,
    StaticCallSourceCoverageState,
    StaticCallSyntaxKind,
    validate_callee_spelling,
)


def _call_span(
    *,
    language: SourceLanguage = SourceLanguage.PYTHON,
    start_line: int = 2,
    start_column: int = 4,
    end_line: int = 2,
    end_column: int = 9,
) -> SourceSpan:
    return SourceSpan(
        path="src/pkg/mod.py",
        start_line=start_line,
        start_column=start_column,
        end_line=end_line,
        end_column=end_column,
        language=language,
    )


def _evidence(
    *,
    language: SourceLanguage = SourceLanguage.PYTHON,
    span: SourceSpan | None = None,
) -> Evidence:
    return Evidence(
        span=span or _call_span(language=language),
        provider="static-call-synthetic",
        observation="syntactic call site",
        confidence=Confidence.EXACT,
        source_sha256="a" * 64,
        language=language,
    )


def _module_scope(**changes: object) -> StaticCallScope:
    payload = {
        "kind": StaticCallScopeKind.MODULE,
        "name": None,
        "definition_line": 1,
        "definition_column": 0,
    }
    payload.update(changes)
    return StaticCallScope.model_validate(payload)


def _function_scope(name: str = "run", line: int = 5) -> StaticCallScope:
    return StaticCallScope(
        kind=StaticCallScopeKind.FUNCTION,
        name=name,
        definition_line=line,
        definition_column=0,
    )


def _occurrence_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "occurrence_id": "call-occurrence-0123456789abcdef0123",
        "analysis_identity_sha256": "f" * 64,
        "language": SourceLanguage.PYTHON,
        "source_file_id": "source-file-0123456789abcdef0123",
        "module_name": "pkg.mod",
        "enclosing_subject_id": None,
        "callee_spelling": "helper",
        "syntax_kind": StaticCallSyntaxKind.CALL,
        "evaluation_context": StaticCallEvaluationContext.FUNCTION_BODY,
        "conditional": False,
        "deferred": False,
        "evaluation_unknown": False,
        "scope_path": (_module_scope(), _function_scope()),
        "evidence": (_evidence(),),
        "limitations": (),
    }
    payload.update(changes)
    return payload


def test_static_call_occurrence_accepts_canonical_json_repeat() -> None:
    first = StaticCallOccurrence.model_validate(_occurrence_payload())
    second = StaticCallOccurrence.model_validate_json(first.model_dump_json())
    assert second == first
    assert json.loads(first.model_dump_json()) == json.loads(second.model_dump_json())


@pytest.mark.parametrize(
    "callee",
    [
        "helper",
        "pkg.helper",
        "Object.getOwnProperty",
        "import.meta",
        "this.emit",
        "super.run",
        "Foo::Bar.baz",
        "valid?",
        "notify!",
        "writer=",
        "résumé",
        "π",
        "$emit",
    ],
)
def test_callee_spelling_accepts_safe_chains(callee: str) -> None:
    assert validate_callee_spelling(callee) == callee
    StaticCallOccurrence.model_validate(_occurrence_payload(callee_spelling=callee))


@pytest.mark.parametrize(
    "callee",
    [
        "obj[key]",
        'obj["key"]',
        "foo()",
        "foo?.bar",
        "getattr(x, y)",
        "`tag`",
        "foo + bar",
        "",
        "  trimmed  ",
    ],
)
def test_callee_spelling_rejects_unsafe_expressions(callee: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        if callee in {"", "  trimmed  "}:
            validate_callee_spelling(callee)
        else:
            StaticCallOccurrence.model_validate(_occurrence_payload(callee_spelling=callee))


def test_dynamic_callee_spelling_is_none() -> None:
    record = StaticCallOccurrence.model_validate(_occurrence_payload(callee_spelling=None))
    assert record.callee_spelling is None


@pytest.mark.parametrize(
    "spelling",
    ["$", "foo$bar.run", "this.#private", "@client.run", "@@cache.clear", "::Demo::Writer.new"],
)
def test_safe_language_identifier_variants(spelling: str) -> None:
    assert validate_callee_spelling(spelling) == spelling


@pytest.mark.parametrize(
    "name", ["$", "foo$bar", "ready?", "save!", "writer=", "[]", "[]=", "Demo::Writer"]
)
def test_scope_names_preserve_safe_method_and_class_syntax(name: str) -> None:
    assert _function_scope(name).name == name


@pytest.mark.parametrize(
    "changes",
    [
        {"conditional": 0},
        {"deferred": 1},
        {"evaluation_unknown": 0},
        {"extra": "forbidden"},
        {"occurrence_id": "call-occurrence-NOTHEX"},
        {"source_file_id": "source-file-NOTHEX"},
        {"enclosing_subject_id": "subject-NOTHEX"},
        {"module_name": ""},
        {"module_name": " spaced "},
        {"evidence": ()},
        {"evidence": (_evidence(), _evidence())},
        {"limitations": ("z-limit", "a-limit")},
    ],
)
def test_static_call_occurrence_rejects_coercion_extras_and_ordering(changes) -> None:
    with pytest.raises(ValidationError):
        StaticCallOccurrence.model_validate(_occurrence_payload(**changes))


def test_static_call_occurrence_rejects_wrong_language_and_span_direction() -> None:
    span = _call_span(start_line=3, start_column=4, end_line=2, end_column=9)
    with pytest.raises(ValidationError, match="language"):
        StaticCallOccurrence.model_validate(
            _occurrence_payload(
                language=SourceLanguage.JAVASCRIPT,
                evidence=(_evidence(language=SourceLanguage.PYTHON, span=span),),
            )
        )
    reversed_span = _call_span(start_line=2, start_column=8, end_line=2, end_column=4)
    with pytest.raises(ValidationError, match="nonempty call range"):
        StaticCallOccurrence.model_validate(
            _occurrence_payload(evidence=(_evidence(span=reversed_span),))
        )
    empty_span = _call_span(start_line=2, start_column=4, end_line=2, end_column=4)
    with pytest.raises(ValidationError, match="nonempty call range"):
        StaticCallOccurrence.model_validate(
            _occurrence_payload(evidence=(_evidence(span=empty_span),))
        )


def test_static_call_occurrence_rejects_malformed_scope_path() -> None:
    with pytest.raises(ValidationError, match="module scope"):
        StaticCallOccurrence.model_validate(_occurrence_payload(scope_path=(_function_scope(),)))
    with pytest.raises(ValidationError, match="only one module scope"):
        StaticCallOccurrence.model_validate(
            _occurrence_payload(
                scope_path=(
                    _module_scope(),
                    _module_scope(definition_line=2),
                )
            )
        )
    with pytest.raises(ValidationError, match="nondecreasing"):
        StaticCallOccurrence.model_validate(
            _occurrence_payload(
                scope_path=(
                    _module_scope(),
                    _function_scope(line=10),
                    StaticCallScope(
                        kind=StaticCallScopeKind.CLASS,
                        name="Box",
                        definition_line=3,
                        definition_column=0,
                    ),
                )
            )
        )
    with pytest.raises(ValidationError, match="scope name"):
        StaticCallOccurrence.model_validate(
            _occurrence_payload(
                scope_path=(
                    _module_scope(),
                    StaticCallScope(
                        kind=StaticCallScopeKind.CLASS,
                        name='["computed"]',
                        definition_line=3,
                        definition_column=0,
                    ),
                )
            )
        )


@pytest.mark.parametrize(
    ("language", "module_name", "callee"),
    [
        (SourceLanguage.PYTHON, "pkg.mod", "print"),
        (SourceLanguage.JAVASCRIPT, "src/app/index", "console.log"),
        (SourceLanguage.RUBY, "MyApp::Services", "Kernel.puts"),
    ],
)
def test_sample_language_spellings(language, module_name, callee) -> None:
    span = _call_span(language=language)
    record = StaticCallOccurrence.model_validate(
        _occurrence_payload(
            language=language,
            module_name=module_name,
            callee_spelling=callee,
            evidence=(_evidence(language=language, span=span),),
        )
    )
    assert record.language is language
    assert record.callee_spelling == callee


def test_static_call_source_coverage_count_and_state_invariants() -> None:
    complete = StaticCallSourceCoverage(
        source_file_id="source-file-0123456789abcdef0123",
        language=SourceLanguage.PYTHON,
        state=StaticCallSourceCoverageState.COMPLETE,
        observed_count=3,
        retained_count=3,
        omitted_count=0,
        limitations=(),
    )
    assert complete.observed_count == complete.retained_count

    partial = StaticCallSourceCoverage(
        source_file_id="source-file-0123456789abcdef0123",
        language=SourceLanguage.PYTHON,
        state=StaticCallSourceCoverageState.PARTIAL,
        observed_count=5,
        retained_count=2,
        omitted_count=3,
        limitations=("implicit-decorator-application-not-modeled",),
    )
    assert partial.omitted_count > 0

    truncated = StaticCallSourceCoverage(
        source_file_id="source-file-0123456789abcdef0123",
        language=SourceLanguage.PYTHON,
        state=StaticCallSourceCoverageState.TRUNCATED,
        observed_count=4,
        retained_count=1,
        omitted_count=3,
        limitations=(),
    )
    assert truncated.omitted_count > 0


@pytest.mark.parametrize(
    "payload",
    [
        {
            "state": StaticCallSourceCoverageState.COMPLETE,
            "observed_count": 2,
            "retained_count": 1,
            "omitted_count": 1,
        },
        {
            "state": StaticCallSourceCoverageState.FAILED,
            "observed_count": 1,
            "retained_count": 1,
            "omitted_count": 0,
            "limitations": ("parse-failed",),
        },
        {
            "state": StaticCallSourceCoverageState.PARTIAL,
            "observed_count": 2,
            "retained_count": 1,
            "omitted_count": 1,
            "limitations": (),
        },
        {
            "state": StaticCallSourceCoverageState.UNAVAILABLE,
            "observed_count": 0,
            "retained_count": 0,
            "omitted_count": 0,
            "limitations": (),
        },
        {
            "observed_count": 3,
            "retained_count": 1,
            "omitted_count": 1,
        },
    ],
)
def test_static_call_source_coverage_rejects_invalid_state_counts(payload) -> None:
    base = {
        "source_file_id": "source-file-0123456789abcdef0123",
        "language": SourceLanguage.PYTHON,
        "limitations": ("explicit-limitation",),
    }
    base.update(payload)
    with pytest.raises(ValidationError):
        StaticCallSourceCoverage.model_validate(base)
