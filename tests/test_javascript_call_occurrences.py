from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import pytest

from code_analysis.languages.javascript.call_occurrences import (
    CallExecutionDisposition,
    JavascriptCallEvaluationContext,
    JavascriptCallSiteKind,
    JavascriptLexicalScopeKind,
    discover_javascript_call_occurrences,
)


def _parse(source: str) -> tuple[Any, bytes]:
    import tree_sitter_javascript
    from tree_sitter import Language, Parser

    source_bytes = source.encode("utf-8")
    language = Language(tree_sitter_javascript.language())
    root = Parser(language).parse(source_bytes).root_node
    return root, source_bytes


def test_foreign_source_bytes_fail_closed() -> None:
    root, _ = _parse("f();")
    with pytest.raises(ValueError, match="does not match"):
        discover_javascript_call_occurrences(root, b"g();")


def test_parameter_default_is_deferred_and_conditional() -> None:
    facts = {
        fact.callee_dotted: fact for fact in _facts("function f(x=defaultValue()) { body(); }")
    }
    assert facts["defaultValue"].deferred
    assert facts["defaultValue"].conditional
    assert facts["body"].deferred


def test_foreign_leading_coordinates_fail_closed() -> None:
    root, _ = _parse("\nf();")
    with pytest.raises(ValueError, match="coordinates"):
        discover_javascript_call_occurrences(root, b" f();")


@pytest.mark.parametrize("source", ["  f();\n", "   ", "\nf()", ""])
def test_program_whitespace_binding(source: str) -> None:
    root, data = _parse(source)
    assert discover_javascript_call_occurrences(root, data).tree_complete


def _batch(source: str, **kwargs: object):
    root, source_bytes = _parse(source)
    return discover_javascript_call_occurrences(root, source_bytes, **kwargs)


def _facts(source: str, **kwargs: object):
    return _batch(source, **kwargs).occurrences


def _slice_span(source_bytes: bytes, fact) -> str:
    return source_bytes[fact.start_byte : fact.end_byte].decode("utf-8")


def _scope_names(fact) -> tuple[str | None, ...]:
    return tuple(record.name for record in fact.scope_path)


def _scope_kinds(fact) -> tuple[JavascriptLexicalScopeKind, ...]:
    return tuple(record.kind for record in fact.scope_path)


def _explicit_call_new_count(root: Any) -> int:
    total = 0

    def walk(node: Any) -> None:
        nonlocal total
        if node.type in {"call_expression", "new_expression"}:
            total += 1
        for child in node.children:
            walk(child)

    walk(root)
    return total


def test_repeated_calls_are_distinct_occurrences() -> None:
    source = "function f() {\n  g();\n  g();\n}\n"
    batch = _batch(source)
    assert batch.total_observed == 2
    assert batch.total_retained == 2
    source_bytes = source.encode("utf-8")
    texts = [_slice_span(source_bytes, fact) for fact in batch.occurrences]
    assert texts == ["g()", "g()"]
    assert all(fact.callee_dotted == "g" for fact in batch.occurrences)


def test_multiline_and_unicode_byte_spans() -> None:
    source = "function f() {\n  h(\n    1,\n    2,\n  );\n  café();\n}\n"
    source_bytes = source.encode("utf-8")
    batch = _batch(source)
    by_callee = {fact.callee_dotted: fact for fact in batch.occurrences}
    assert _slice_span(source_bytes, by_callee["h"]) == "h(\n    1,\n    2,\n  )"
    cafe = by_callee["café"]
    assert _slice_span(source_bytes, cafe) == "café()"
    assert cafe.start_byte == source_bytes.index("café".encode())


def test_tagged_template_counts_tag_and_inner_call() -> None:
    source = "tag`x ${inner()}`;\n"
    batch = _batch(source)
    assert batch.total_observed == 2
    callees = {fact.callee_dotted for fact in batch.occurrences}
    assert callees == {"tag", "inner"}


def test_new_expression_site_kind() -> None:
    source = "const x = new Foo();\n"
    fact = _facts(source)[0]
    assert fact.site_kind is JavascriptCallSiteKind.NEW
    assert fact.callee_dotted == "Foo"


def test_parameter_defaults_use_called_function_scope() -> None:
    source = "function fn(x = setup()) { body(); }\n"
    facts = {fact.callee_dotted: fact for fact in _facts(source)}
    setup = facts["setup"]
    assert setup.evaluation_context is JavascriptCallEvaluationContext.PARAMETER_DEFAULT
    assert _scope_names(setup) == (None, "fn")
    body = facts["body"]
    assert body.evaluation_context is JavascriptCallEvaluationContext.FUNCTION_BODY
    assert body.deferred


def test_destructuring_default_only_in_parameter_context() -> None:
    assign = "const {a = side()} = obj;\n"
    assign_fact = next(f for f in _facts(assign) if f.callee_dotted == "side")
    assert assign_fact.evaluation_context is JavascriptCallEvaluationContext.MODULE_BODY
    param = "function f({a = side()} = {}) {}\n"
    param_fact = next(f for f in _facts(param) if f.callee_dotted == "side")
    assert param_fact.evaluation_context is JavascriptCallEvaluationContext.PARAMETER_DEFAULT
    assert _scope_names(param_fact) == (None, "f")


def test_arrow_callback_body_scope() -> None:
    source = "function outer() { const cb = () => run(); }\n"
    run = next(f for f in _facts(source) if f.callee_dotted == "run")
    assert run.evaluation_context is JavascriptCallEvaluationContext.ARROW_BODY
    assert _scope_kinds(run)[-1] is JavascriptLexicalScopeKind.ARROW_FUNCTION
    assert _scope_names(run) == (None, "outer", None)


def test_class_heritage_static_and_instance_fields() -> None:
    source = (
        "class Widget extends Base() {\n"
        "  static s = initStatic();\n"
        "  field = initInstance();\n"
        "}\n"
    )
    facts = {fact.callee_dotted: fact for fact in _facts(source)}
    base = facts["Base"]
    assert base.evaluation_context is JavascriptCallEvaluationContext.CLASS_HERITAGE
    assert _scope_kinds(base) == (JavascriptLexicalScopeKind.MODULE,)
    static = facts["initStatic"]
    assert static.evaluation_context is JavascriptCallEvaluationContext.CLASS_STATIC_FIELD
    assert not static.deferred
    inst = facts["initInstance"]
    assert inst.evaluation_context is JavascriptCallEvaluationContext.CLASS_INSTANCE_FIELD
    assert inst.deferred


def test_computed_key_not_method_body() -> None:
    source = "class C { [key()]() { body(); } }\n"
    facts = {fact.callee_dotted: fact for fact in _facts(source)}
    assert facts["key"].evaluation_context is JavascriptCallEvaluationContext.COMPUTED_KEY
    assert facts["body"].evaluation_context is JavascriptCallEvaluationContext.FUNCTION_BODY


def test_jsx_expression_call_is_retained() -> None:
    source = "const el = <Foo bar={baz()} />;\n"
    batch = _batch(source)
    assert batch.total_observed == 1
    assert batch.occurrences[0].callee_dotted == "baz"


def test_optional_chain_call_is_conditional() -> None:
    source = "obj?.meth(arg());\n"
    facts = {fact.callee_dotted: fact for fact in _facts(source)}
    assert facts["obj.meth"].conditional
    assert facts["obj.meth"].execution_disposition is CallExecutionDisposition.CONDITIONAL
    assert facts["arg"].conditional


def test_short_circuit_right_side_conditional() -> None:
    source = "a() && b();\n"
    facts = {fact.callee_dotted: fact for fact in _facts(source)}
    assert not facts["a"].conditional
    assert facts["b"].conditional


def test_dynamic_callee_spelling_is_null_inner_retained() -> None:
    source = "(get())(); obj.attr();\n"
    facts = _facts(source)
    dynamic = next(f for f in facts if f.callee_dotted is None)
    assert _slice_span(source.encode(), dynamic) == "(get())()"
    assert any(f.callee_dotted == "get" for f in facts)
    assert any(f.callee_dotted == "obj.attr" for f in facts)


def test_parse_error_yields_partial_batch_with_zero_sites() -> None:
    root, source_bytes = _parse("function broken(:\n")
    batch = discover_javascript_call_occurrences(root, source_bytes)
    assert not batch.tree_complete
    assert batch.total_observed == 0
    assert "parse-tree-contains-errors-or-missing-nodes" in batch.limitations


def test_error_tree_still_reports_valid_calls_as_partial() -> None:
    import tree_sitter_javascript
    from tree_sitter import Language, Parser

    source_bytes = b"f(); g(;\n"
    root = Parser(Language(tree_sitter_javascript.language())).parse(source_bytes).root_node
    batch = discover_javascript_call_occurrences(root, source_bytes)
    assert not batch.tree_complete
    assert batch.total_observed >= 1
    assert "parse-tree-contains-errors-or-missing-nodes" in batch.limitations


@pytest.mark.parametrize("invalid", [0, -3, True, 1.5])
def test_invalid_max_occurrences_rejected(invalid: object) -> None:
    root, source_bytes = _parse("f();\n")
    with pytest.raises((TypeError, ValueError)):
        discover_javascript_call_occurrences(
            root,
            source_bytes,
            max_occurrences=invalid,  # type: ignore[arg-type]
        )


def test_bounded_retention_is_deterministic_by_span() -> None:
    source = "a();\nb();\nc();\n"
    first = _batch(source, max_occurrences=2)
    second = _batch(source, max_occurrences=2)
    assert first.total_observed == 3
    assert first.total_retained == 2
    assert first.occurrences == second.occurrences
    source_bytes = source.encode("utf-8")
    assert [_slice_span(source_bytes, fact) for fact in first.occurrences] == ["a()", "b()"]
    assert "truncated-at-max-occurrences" in first.limitations


def test_retained_heap_never_exceeds_max_occurrences(monkeypatch: pytest.MonkeyPatch) -> None:
    from code_analysis.languages.javascript.call_occurrences import _CallCollector

    source = "".join(f"n{i}();\n" for i in range(50))
    cap = 7
    root, source_bytes = _parse(source)
    original = _CallCollector._record_site
    sizes: list[int] = []

    def checked_record(self, node, frame):
        original(self, node, frame)
        sizes.append(len(self._retained_heap))
        assert len(self._retained_heap) <= cap

    monkeypatch.setattr(_CallCollector, "_record_site", checked_record)
    batch = discover_javascript_call_occurrences(root, source_bytes, max_occurrences=cap)
    assert len(sizes) == 50
    assert batch.total_observed == 50
    assert batch.total_retained == cap


def test_bounded_output_stable_across_process_hash_seed() -> None:
    script = (
        "import dataclasses, json; "
        "import tree_sitter_javascript; "
        "from tree_sitter import Language, Parser; "
        "from code_analysis.languages.javascript.call_occurrences import "
        "discover_javascript_call_occurrences; "
        "source = 'a();\\nb();\\nc();\\nd();\\n'; "
        "b = source.encode(); "
        "root = Parser(Language(tree_sitter_javascript.language())).parse(b).root_node; "
        "batch = discover_javascript_call_occurrences(root, b, max_occurrences=2); "
        "print(json.dumps(dataclasses.asdict(batch), sort_keys=True))"
    )
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        for seed in ("0", "12345")
    ]
    assert outputs[0] == outputs[1]


def test_no_spurious_duplicate_sites() -> None:
    source = "foo.bar();\n"
    batch = _batch(source)
    assert batch.total_observed == 1


def test_varied_fixture_matches_independent_tree_walk() -> None:
    source = (
        "import x from 'm';\n"
        "class C extends Base() {\n"
        "  static { boot(); }\n"
        "  tag`a ${inner()}`;\n"
        "  async m(a = d()) { await work(); return new Thing(); }\n"
        "  [k()]() {}\n"
        "  field = late();\n"
        "}\n"
        "export function outer(x = p()) {\n"
        "  if (test()) { taken(); } else { other(); }\n"
        "  const jsx = <Item v={val()} />;\n"
        "  return obj?.run(side());\n"
        "}\n"
        "() => def();\n"
        "a() && b();\n"
    )
    root, source_bytes = _parse(source)
    batch = discover_javascript_call_occurrences(root, source_bytes)
    explicit = _explicit_call_new_count(root)
    assert batch.total_observed == explicit
    assert batch.total_omitted == 0
    kinds = {fact.site_kind for fact in batch.occurrences}
    assert JavascriptCallSiteKind.CALL in kinds
    assert JavascriptCallSiteKind.NEW in kinds


def test_limitations_include_syntactic_gaps() -> None:
    batch = _batch("f();\n")
    assert "syntactic-context-is-not-runtime-execution-or-call-order" in batch.limitations
    assert "callee-spelling-does-not-establish-name-binding-or-dispatch" in batch.limitations


def test_await_call_in_deferred_function_body() -> None:
    source = "async function f() { return await work(); }\n"
    work = next(f for f in _facts(source) if f.callee_dotted == "work")
    assert work.deferred
    assert work.evaluation_context is JavascriptCallEvaluationContext.FUNCTION_BODY


def test_optional_call_form_marks_outer_and_argument() -> None:
    source = "foo?.(bar());\n"
    facts = {fact.callee_dotted: fact for fact in _facts(source)}
    assert facts["foo"].conditional
    assert facts["bar"].conditional


@pytest.mark.parametrize("source", ["obj?.[key()](arg());", "obj?.m()(arg());", "obj?.m.n(arg());"])
def test_optional_chain_continuation_keeps_arguments_conditional(source: str) -> None:
    facts = {fact.callee_dotted: fact for fact in _facts(source)}
    assert facts["arg"].conditional


def test_parentheses_end_optional_chain_before_arguments() -> None:
    facts = {fact.callee_dotted: fact for fact in _facts("(obj?.m)(arg());")}
    assert not facts["arg"].conditional


def test_optional_computed_member_keeps_key_conditional() -> None:
    facts = {fact.callee_dotted: fact for fact in _facts("obj?.[key()](arg());")}
    assert facts["key"].conditional
    assert facts["arg"].conditional


def test_static_block_context() -> None:
    source = "class C { static { boot(); } }\n"
    boot = next(f for f in _facts(source) if f.callee_dotted == "boot")
    assert boot.evaluation_context is JavascriptCallEvaluationContext.CLASS_STATIC_BLOCK
    assert not boot.deferred


def test_conditional_and_deferred_flags_are_independent() -> None:
    source = "function f() { if (c()) { branch(); } }\na() && b();\n"
    facts = {fact.callee_dotted: fact for fact in _facts(source)}
    assert facts["branch"].conditional and facts["branch"].deferred
    assert facts["b"].conditional and not facts["b"].deferred
