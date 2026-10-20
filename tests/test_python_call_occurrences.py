from __future__ import annotations

import ast
import os
import subprocess
import sys

import pytest

from code_analysis.languages.python.call_occurrences import (
    CallEvaluationContext,
    CallExecutionDisposition,
    LexicalScopeKind,
    discover_python_call_occurrences,
)


def _slice_invocation_span(
    source: str,
    *,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
) -> str:
    encoded = source.encode("utf-8")
    line_starts = [0]
    for index, byte in enumerate(encoded):
        if byte == ord("\n"):
            line_starts.append(index + 1)
    start_byte = line_starts[start_line - 1] + start_column
    end_byte = line_starts[end_line - 1] + end_column
    return encoded[start_byte:end_byte].decode("utf-8")


def _invocation_span_text(source: str, fact) -> str:
    return _slice_invocation_span(
        source,
        start_line=fact.start_line,
        start_column=fact.start_column,
        end_line=fact.end_line,
        end_column=fact.end_column,
    )


def _facts(source: str, **kwargs: object):
    return discover_python_call_occurrences(source, **kwargs).occurrences


def _batch(source: str, **kwargs: object):
    return discover_python_call_occurrences(source, **kwargs)


def _scope_names(fact) -> tuple[str | None, ...]:
    return tuple(record.name for record in fact.scope_path)


def _scope_kinds(fact) -> tuple[LexicalScopeKind, ...]:
    return tuple(record.kind for record in fact.scope_path)


def _explicit_call_count(source: str) -> int:
    tree = ast.parse(source)
    return sum(1 for node in ast.walk(tree) if isinstance(node, ast.Call))


def test_repeated_calls_are_distinct_occurrences() -> None:
    source = "def f():\n    g()\n    g()\n"
    batch = _batch(source)
    assert batch.total_observed == 2
    assert batch.total_retained == 2
    assert batch.total_omitted == 0
    texts = [_invocation_span_text(source, fact) for fact in batch.occurrences]
    assert texts == ["g()", "g()"]
    assert all(fact.callee_dotted == "g" for fact in batch.occurrences)
    assert all(
        fact.evaluation_context == CallEvaluationContext.FUNCTION_BODY for fact in batch.occurrences
    )


def test_multiline_call_span_is_exact_invocation() -> None:
    source = "def f():\n    h(\n        1,\n        2,\n    )\n"
    fact = _facts(source)[0]
    assert _invocation_span_text(source, fact) == "h(\n        1,\n        2,\n    )"
    assert fact.callee_dotted == "h"


def test_unicode_source_uses_utf8_byte_columns() -> None:
    source = "def f():\n    café()\n"
    fact = _facts(source)[0]
    assert _invocation_span_text(source, fact) == "café()"
    lines = source.encode("utf-8").splitlines()
    assert fact.start_line == 2
    assert fact.start_column == len(lines[1].split(b"caf")[0])


def test_nested_definitions_scope_path() -> None:
    source = "def outer():\n" "    def inner():\n" "        leaf()\n" "    inner()\n"
    by_callee = {fact.callee_dotted: fact for fact in _facts(source)}
    leaf = by_callee["leaf"]
    assert _scope_kinds(leaf) == (
        LexicalScopeKind.MODULE,
        LexicalScopeKind.FUNCTION,
        LexicalScopeKind.FUNCTION,
    )
    assert _scope_names(leaf) == (None, "outer", "inner")
    inner_call = by_callee["inner"]
    assert _scope_kinds(inner_call) == (LexicalScopeKind.MODULE, LexicalScopeKind.FUNCTION)
    assert _scope_names(inner_call) == (None, "outer")


def test_decorators_and_defaults_use_enclosing_scope() -> None:
    source = "@deco()\n" "def fn(x: ann(), y=default()):\n" "    body()\n"
    by_text = {_invocation_span_text(source, fact): fact for fact in _facts(source)}
    deco = by_text["deco()"]
    assert deco.evaluation_context == CallEvaluationContext.DECORATOR
    assert _scope_kinds(deco) == (LexicalScopeKind.MODULE,)
    default = by_text["default()"]
    assert default.evaluation_context == CallEvaluationContext.PARAMETER_DEFAULT
    assert _scope_kinds(default) == (LexicalScopeKind.MODULE,)
    ann = by_text["ann()"]
    assert ann.evaluation_context == CallEvaluationContext.ANNOTATION
    assert ann.execution_disposition == CallExecutionDisposition.UNKNOWN
    body = by_text["body()"]
    assert body.evaluation_context == CallEvaluationContext.FUNCTION_BODY
    assert body.execution_disposition == CallExecutionDisposition.DEFERRED
    assert _scope_names(body) == (None, "fn")


def test_lambda_defaults_enclosing_body_lambda_scope() -> None:
    source = "def outer():\n    cb = lambda x=setup(): run(x)\n"
    facts = _facts(source)
    setup = next(f for f in facts if f.callee_dotted == "setup")
    run = next(f for f in facts if f.callee_dotted == "run")
    assert setup.evaluation_context == CallEvaluationContext.LAMBDA_DEFAULT
    assert _scope_names(setup) == (None, "outer")
    assert run.evaluation_context == CallEvaluationContext.LAMBDA_BODY
    assert run.execution_disposition == CallExecutionDisposition.DEFERRED
    assert _scope_kinds(run)[-1] == LexicalScopeKind.LAMBDA


def test_lambda_keyword_only_defaults_are_observed() -> None:
    source = "def outer():\n    fn = lambda *, a=default(): body()\n"
    batch = _batch(source)
    assert batch.total_observed == 2
    assert {_facts(source)[0].callee_dotted, _facts(source)[1].callee_dotted} == {
        "default",
        "body",
    }


def test_class_bases_fields_and_methods() -> None:
    source = (
        "class Widget(Base()):\n"
        "    field = init()\n"
        "    def method(self):\n"
        "        self.method()\n"
    )
    facts = _facts(source)
    base = next(f for f in facts if f.callee_dotted == "Base")
    assert base.evaluation_context == CallEvaluationContext.CLASS_BASE
    assert _scope_kinds(base) == (LexicalScopeKind.MODULE,)
    init = next(f for f in facts if f.callee_dotted == "init")
    assert init.evaluation_context == CallEvaluationContext.CLASS_BODY
    assert _scope_names(init) == (None, "Widget")
    call = next(f for f in facts if f.callee_dotted == "self.method")
    assert call.evaluation_context == CallEvaluationContext.FUNCTION_BODY
    assert call.execution_disposition == CallExecutionDisposition.DEFERRED
    assert _scope_names(call) == (None, "Widget", "method")


def test_class_body_inside_deferred_function_is_not_module_eager() -> None:
    source = "def outer():\n    class Inner:\n        mark()\n"
    mark = next(f for f in _facts(source) if f.callee_dotted == "mark")
    assert mark.evaluation_context == CallEvaluationContext.CLASS_BODY
    assert mark.execution_disposition == CallExecutionDisposition.DEFERRED
    assert _scope_names(mark) == (None, "outer", "Inner")


def test_future_annotations_defer_annotation_calls() -> None:
    source = "from __future__ import annotations\n\ndef fn(x: tag()):\n    pass\n"
    batch = _batch(source)
    tag = batch.occurrences[0]
    assert tag.evaluation_context == CallEvaluationContext.ANNOTATION
    assert tag.execution_disposition == CallExecutionDisposition.DEFERRED
    assert "future-annotations-defer-annotation-execution" in batch.limitations


def test_eager_list_comprehension_and_deferred_generator() -> None:
    list_source = "[body() for x in outer() if gate()]"
    list_facts = {f.callee_dotted: f for f in _facts(list_source)}
    assert list_facts["outer"].evaluation_context == CallEvaluationContext.COMPREHENSION_OUTER_ITER
    assert list_facts["outer"].execution_disposition == CallExecutionDisposition.EAGER
    assert list_facts["body"].evaluation_context == CallEvaluationContext.COMPREHENSION_INNER
    assert list_facts["body"].execution_disposition == CallExecutionDisposition.CONDITIONAL
    assert _scope_kinds(list_facts["body"])[-1] == LexicalScopeKind.COMPREHENSION

    gen_source = "(body() for x in outer() if gate())"
    gen_facts = {f.callee_dotted: f for f in _facts(gen_source)}
    assert gen_facts["body"].execution_disposition == CallExecutionDisposition.DEFERRED


def test_conditional_branch_calls_are_not_eager() -> None:
    source = (
        "def choose(flag):\n"
        "    if test():\n"
        "        taken()\n"
        "    else:\n"
        "        other()\n"
    )
    facts = {f.callee_dotted: f for f in _facts(source)}
    assert facts["test"].execution_disposition == CallExecutionDisposition.CONDITIONAL
    assert facts["taken"].execution_disposition == CallExecutionDisposition.CONDITIONAL
    assert facts["other"].execution_disposition == CallExecutionDisposition.CONDITIONAL


def test_computed_callee_is_null() -> None:
    source = "def f():\n    (get())()\n    obj.attr()\n"
    facts = _facts(source)
    dynamic = next(f for f in facts if f.callee_dotted is None)
    assert _invocation_span_text(source, dynamic) == "(get())()"
    assert next(f for f in facts if f.callee_dotted == "get") is not None
    assert next(f for f in facts if f.callee_dotted == "obj.attr") is not None


@pytest.mark.parametrize(
    ("source", "expected_observed"),
    [
        ("async def f():\n    return await work()\n", 1),
        ('x=f"{format_value()}"\n', 1),
        ("def f():\n    yield produce()\n", 1),
        ("def outer():\n    fn = lambda *, a=default(): body()\n", 2),
        ("registry[key()]=value()\n", 2),
        ("for registry[key()] in source():\n    pass\n", 2),
        ("def f[T: bound()]():\n    pass\n", 1),
    ],
)
def test_regression_missing_call_families(source: str, expected_observed: int) -> None:
    batch = _batch(source)
    assert batch.total_observed == expected_observed
    assert batch.total_observed == _explicit_call_count(source)


def test_async_await_call_is_deferred_in_function_body() -> None:
    source = "async def f():\n    return await work()\n"
    work = next(f for f in _facts(source) if f.callee_dotted == "work")
    assert work.execution_disposition == CallExecutionDisposition.DEFERRED


def test_type_parameter_bound_is_unknown_not_eager() -> None:
    source = "def f[T: bound()]():\n    pass\n"
    bound = next(f for f in _facts(source) if f.callee_dotted == "bound")
    assert bound.evaluation_context == CallEvaluationContext.ANNOTATION
    assert bound.execution_disposition == CallExecutionDisposition.UNKNOWN


def test_chained_comparison_marks_later_comparators_conditional() -> None:
    source = "def f():\n    if left() < mid() < right():\n        pass\n"
    facts = {f.callee_dotted: f for f in _facts(source)}
    assert facts["left"].execution_disposition == CallExecutionDisposition.CONDITIONAL
    assert facts["mid"].execution_disposition == CallExecutionDisposition.CONDITIONAL
    assert facts["right"].execution_disposition == CallExecutionDisposition.CONDITIONAL


def test_syntax_error_propagates() -> None:
    with pytest.raises(SyntaxError):
        discover_python_call_occurrences("def broken(:\n")


@pytest.mark.parametrize("invalid", [0, -3, True, 1.5])
def test_invalid_max_occurrences_rejected(invalid: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        discover_python_call_occurrences("f()\n", max_occurrences=invalid)  # type: ignore[arg-type]


def test_bounded_retention_is_deterministic_by_span() -> None:
    source = "a()\nb()\nc()\n"
    first = _batch(source, max_occurrences=2)
    second = _batch(source, max_occurrences=2)
    assert first.total_observed == 3
    assert first.total_retained == 2
    assert first.total_omitted == 1
    assert first.occurrences == second.occurrences
    assert [_invocation_span_text(source, fact) for fact in first.occurrences] == ["a()", "b()"]
    assert "truncated-at-max-occurrences" in first.limitations


def test_retained_heap_never_exceeds_max_occurrences(monkeypatch: pytest.MonkeyPatch) -> None:
    from code_analysis.languages.python.call_occurrences import _CallCollector

    source = "".join(f"n{i}()\n" for i in range(50))
    cap = 7
    original = _CallCollector._record_call
    sizes = []

    def checked_record(self, node, frame):
        original(self, node, frame)
        sizes.append(len(self._retained_heap))
        assert len(self._retained_heap) <= cap

    monkeypatch.setattr(_CallCollector, "_record_call", checked_record)
    batch = _batch(source, max_occurrences=cap)
    assert len(sizes) == 50
    assert batch.total_observed == 50
    assert batch.total_retained == cap
    assert batch.total_omitted == 43
    assert len(batch.occurrences) == cap


def test_bounded_output_is_stable_across_process_hash_seed() -> None:
    script = (
        "import dataclasses, json; "
        "from code_analysis.languages.python.call_occurrences import "
        "discover_python_call_occurrences; "
        "print(json.dumps(dataclasses.asdict(discover_python_call_occurrences("
        "'a()\\nb()\\nc()\\nd()\\n', max_occurrences=2)), sort_keys=True))"
    )
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", script], env={**os.environ, "PYTHONHASHSEED": seed}
        )
        for seed in ("0", "12345")
    ]
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    "source",
    [
        "f(key=g())",
        "f(**g())",
        "type X[T: bound()] = alias()",
        "def f[*Ts, **P](): pass",
        "try:\n f()\nexcept* kind():\n g()",
    ],
)
def test_auxiliary_ast_nodes_preserve_all_explicit_calls(source: str) -> None:
    batch = _batch(source)
    assert batch.total_observed == _explicit_call_count(source)
    assert batch.total_omitted == 0


def test_type_alias_calls_do_not_claim_eager_execution() -> None:
    batch = _batch("type X[T: bound()] = alias()")
    assert len(batch.occurrences) == 2
    for fact in batch.occurrences:
        assert fact.evaluation_context is CallEvaluationContext.ANNOTATION
        assert fact.execution_disposition is CallExecutionDisposition.UNKNOWN


def test_conditional_and_deferred_contexts_are_independently_retained() -> None:
    source = (
        "def f():\n"
        "    if condition():\n"
        "        branch()\n"
        "    return [body() for x in items()]\n"
        "g = (later() for x in source())\n"
        "assert ok, error_message()\n"
        "for registry[key()] in values(): pass\n"
    )
    facts = {fact.callee_dotted: fact for fact in _batch(source).occurrences}
    for name in ("branch", "body", "later"):
        assert facts[name].conditional
        assert facts[name].deferred
    for name in ("error_message", "key"):
        assert facts[name].conditional
        assert not facts[name].deferred


def test_varied_composite_source_matches_ast_walk() -> None:
    source = (
        "@d()\n"
        "async def af():\n"
        "    return await aw()\n"
        "class C(B(), metaclass=m()):\n"
        "    label = f'{tag()}'\n"
        "    def f(a=default(), /, *args, b: ann(), **kw):\n"
        "        registry[key()]=value()\n"
        "        for item[k()] in src():\n"
        "            yield produce()\n"
        "        return (body() for _ in pool() if gate())\n"
        "def g[T: bound()]():\n"
        "    pass\n"
    )
    batch = _batch(source)
    explicit = _explicit_call_count(source)
    assert batch.total_observed == explicit
    assert batch.total_retained == explicit
    assert batch.total_omitted == 0


def test_limitations_include_implicit_call_gaps() -> None:
    batch = _batch("f()\n")
    assert "implicit-decorator-application-not-modeled" in batch.limitations
    assert "implicit-context-manager-calls-not-modeled" in batch.limitations
