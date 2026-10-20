from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.languages.ruby.call_occurrences import (
    CallExecutionDisposition,
    RubyCallEvaluationContext,
    RubyCallOccurrenceBatch,
    RubyCallOccurrenceBatchPayload,
    RubyCallOccurrenceFactPayload,
    RubyCallSiteKind,
    RubyLexicalScopeKind,
    ruby_call_occurrence_batch,
    static_call_batch_from_driver,
)
from code_analysis.languages.ruby.frontend import _PrismDocument

DRIVER = Path(__file__).resolve().parents[1] / "src/code_analysis/languages/ruby/prism_driver.rb"
RUBY = shutil.which("ruby") or "ruby"
PY = sys.executable

_WALKER = r"""
require "json"
require "prism"
source = STDIN.read
result = Prism.parse(source)
spans = []
walk = lambda do |node|
  type = node.type.to_s
  if %w[call_node super_node forwarding_super_node yield_node].include?(type)
    loc = node.location
    spans << {
      "site_kind" => type,
      "start_byte" => loc.start_offset,
      "end_byte" => loc.end_offset,
      "start_line" => loc.start_line,
      "start_column" => loc.start_column,
      "end_line" => loc.end_line,
      "end_column" => loc.end_column,
    }
  end
  node.compact_child_nodes.each { |child| walk.call(child) }
end
walk.call(result.value)
spans.sort_by! do |item|
  [
    item["start_byte"],
    item["start_line"],
    item["start_column"],
    item["end_byte"],
    item["end_column"],
    item["site_kind"],
  ]
end
puts JSON.generate({"count" => spans.length, "spans" => spans})
"""


def _run_driver(
    source: str, *, cap: int | None = None, extra_argv: list[str] | None = None
) -> dict:
    command = [RUBY, str(DRIVER)]
    if cap is not None:
        command.append(str(cap))
    if extra_argv:
        command.extend(extra_argv)
    completed = subprocess.run(
        command,
        input=source.encode("utf-8"),
        check=False,
        capture_output=True,
    )
    return completed


def _run_driver_json(source: str, *, cap: int | None = None) -> dict:
    completed = _run_driver(source, cap=cap)
    assert completed.returncode == 0, completed.stderr.decode()
    return json.loads(completed.stdout)


def _document(source: str, *, cap: int | None = None) -> _PrismDocument:
    completed = _run_driver(source, cap=cap)
    assert completed.returncode == 0, completed.stderr.decode()
    return _PrismDocument.model_validate_json(completed.stdout)


def _batch(source: str, *, cap: int | None = None) -> RubyCallOccurrenceBatch:
    document = _document(source, cap=cap)
    return ruby_call_occurrence_batch(document.call_occurrences)


def _facts(source: str, **kwargs: object):
    return _batch(source, **kwargs).occurrences


def _slice_span(source: str, fact) -> str:
    encoded = source.encode("utf-8")
    line_starts = [0]
    for index, byte in enumerate(encoded):
        if byte == ord("\n"):
            line_starts.append(index + 1)
    start_byte = line_starts[fact.start_line - 1] + fact.start_column
    end_byte = line_starts[fact.end_line - 1] + fact.end_column
    return encoded[start_byte:end_byte].decode("utf-8")


def _scope_names(fact) -> tuple[str | None, ...]:
    return tuple(record.name for record in fact.scope_path)


def _scope_kinds(fact) -> tuple[RubyLexicalScopeKind, ...]:
    return tuple(record.kind for record in fact.scope_path)


def _independent_walk(source: str) -> dict:
    completed = subprocess.run(
        [RUBY, "-rprism", "-e", _WALKER],
        input=source.encode("utf-8"),
        check=True,
        capture_output=True,
    )
    return json.loads(completed.stdout)


def _baseline_document_payload(source: str) -> dict:
    payload = _run_driver_json(source)
    payload.pop("call_occurrences", None)
    return payload


def test_prism_primary_shapes_for_call_read_super_yield() -> None:
    assert _facts("f")[0].site_kind is RubyCallSiteKind.CALL_NODE
    assert _facts("f")[0].callee_spelling == "f"
    assert _facts("x=1;x") == ()
    source = "class C\n  def m\n    super\n  end\nend\n"
    assert _facts(source)[0].site_kind is RubyCallSiteKind.FORWARDING_SUPER_NODE
    assert _facts("yield 1")[0].site_kind is RubyCallSiteKind.YIELD_NODE


def test_repeated_calls_are_distinct_occurrences() -> None:
    source = "def f\n  g()\n  g()\nend\n"
    batch = _batch(source)
    assert batch.total_observed == 2
    assert batch.total_retained == 2
    texts = [_slice_span(source, fact) for fact in batch.occurrences]
    assert texts == ["g()", "g()"]


def test_multiline_and_unicode_spans() -> None:
    source = "def f\n  h(\n    1,\n    2,\n  )\n  café()\nend\n"
    by_callee = {fact.callee_spelling: fact for fact in _facts(source) if fact.callee_spelling}
    assert _slice_span(source, by_callee["h"]) == "h(\n    1,\n    2,\n  )"
    cafe = by_callee["café"]
    assert _slice_span(source, cafe) == "café()"
    assert cafe.start_byte == source.encode("utf-8").index("café".encode())


def test_nested_definitions_blocks_and_defaults() -> None:
    source = (
        "class Outer\n"
        "  def outer\n"
        "    1.times { inner() }\n"
        "    def inner(x = setup())\n"
        "      leaf()\n"
        "    end\n"
        "  end\n"
        "end\n"
    )
    facts = {fact.callee_spelling: fact for fact in _facts(source) if fact.callee_spelling}
    inner = facts["inner"]
    assert inner.deferred
    assert inner.evaluation_context is RubyCallEvaluationContext.BLOCK_BODY
    setup = facts["setup"]
    assert setup.evaluation_context is RubyCallEvaluationContext.PARAMETER_DEFAULT
    assert setup.deferred
    assert _scope_names(setup)[-1] == "inner"
    leaf = facts["leaf"]
    assert leaf.evaluation_context is RubyCallEvaluationContext.METHOD_BODY
    assert leaf.deferred
    assert _scope_kinds(leaf)[-2:] == (RubyLexicalScopeKind.METHOD, RubyLexicalScopeKind.METHOD)


def test_class_initialization_and_singleton_method_scope() -> None:
    source = (
        "class Widget\n"
        "  @@boot = init_class()\n"
        "  def self.factory\n"
        "    build()\n"
        "  end\n"
        "end\n"
    )
    facts = {fact.callee_spelling: fact for fact in _facts(source) if fact.callee_spelling}
    init = facts["init_class"]
    assert init.evaluation_context is RubyCallEvaluationContext.CLASS_INITIALIZATION
    build = facts["build"]
    assert _scope_kinds(build)[-1] is RubyLexicalScopeKind.METHOD
    assert _scope_names(build)[-1] == "factory"


def test_super_yield_and_dynamic_receiver_spelling() -> None:
    source = "class C < Base(); def m; super(1); yield 1; (recv()).go; end; end\n"
    batch = _batch(source)
    kinds = {fact.site_kind for fact in batch.occurrences}
    assert RubyCallSiteKind.SUPER_NODE in kinds
    assert RubyCallSiteKind.YIELD_NODE in kinds
    dynamic = next(
        fact
        for fact in batch.occurrences
        if fact.site_kind is RubyCallSiteKind.CALL_NODE
        and _slice_span(source, fact).startswith("(recv())")
    )
    assert dynamic.callee_spelling is None


def test_safe_navigation_and_rescue_are_conditional() -> None:
    source = "begin\n  safe&.run(arg())\nrescue\n  fallback()\nensure\n  cleanup()\nend\n"
    facts = {fact.callee_spelling: fact for fact in _facts(source) if fact.callee_spelling}
    safe_nav = next(
        fact
        for fact in _facts(source)
        if fact.callee_spelling is None
        and fact.conditional
        and fact.site_kind is RubyCallSiteKind.CALL_NODE
    )
    assert safe_nav.execution_disposition is CallExecutionDisposition.CONDITIONAL
    assert facts["arg"].conditional
    assert facts["fallback"].conditional
    assert facts["cleanup"].conditional


def test_if_branch_and_singleton_receiver_regressions() -> None:
    source = "if condition(); branch(); end\n"
    branch = next(fact for fact in _facts(source) if fact.callee_spelling == "branch")
    assert branch.conditional
    assert not branch.deferred
    singleton = _facts("class << receiver(); body(); end\n")
    receiver = next(fact for fact in singleton if fact.callee_spelling == "receiver")
    body = next(fact for fact in singleton if fact.callee_spelling == "body")
    assert receiver.evaluation_context is RubyCallEvaluationContext.MODULE_BODY
    assert body.evaluation_context is RubyCallEvaluationContext.SINGLETON_CLASS_BODY


def test_singleton_def_receiver_in_enclosing_context() -> None:
    source = "def (recv()).m; body(); end\n"
    facts = {fact.callee_spelling: fact for fact in _facts(source) if fact.callee_spelling}
    assert facts["recv"].evaluation_context is RubyCallEvaluationContext.MODULE_BODY
    assert facts["body"].evaluation_context is RubyCallEvaluationContext.METHOD_BODY


def test_method_defaults_and_body_are_deferred() -> None:
    source = "def f(x=default()); body(); end\n"
    facts = {fact.callee_spelling: fact for fact in _facts(source) if fact.callee_spelling}
    assert facts["default"].deferred
    assert facts["default"].conditional
    assert facts["body"].deferred
    assert facts["body"].evaluation_context is RubyCallEvaluationContext.METHOD_BODY


def test_block_parameter_default_is_retained_in_deferred_scope() -> None:
    facts = {fact.callee_spelling: fact for fact in _facts("items.each { |x=default()| body() }")}
    assert facts["default"].deferred
    assert facts["default"].conditional
    assert facts["default"].scope_path[-1].kind is RubyLexicalScopeKind.BLOCK
    assert facts["default"].evaluation_context is RubyCallEvaluationContext.PARAMETER_DEFAULT
    assert facts["body"].evaluation_context is RubyCallEvaluationContext.BLOCK_BODY


def test_receiver_literal_not_leaked_in_occurrence_payload() -> None:
    source = (
        'lit = "secret"; lit.meth; obj.meth; Foo::Bar.meth; '
        "self.meth; @iv.meth; @@cv.meth; $gv.meth\n"
    )
    payload = json.dumps(_run_driver_json(source)["call_occurrences"])
    assert "secret" not in payload
    spellings = {fact.callee_spelling for fact in _facts(source)}
    assert "lit.meth" in spellings
    assert "Foo::Bar.meth" in spellings
    assert "self.meth" in spellings
    assert any(spelling is None for spelling in spellings)


def test_malformed_parse_reports_partial_batch() -> None:
    batch = _batch("def broken(\n")
    assert not batch.parse_complete
    assert batch.total_observed == 0
    assert "parse-contains-errors" in batch.limitations
    partial = _batch("f(); def broken(\n")
    assert not partial.parse_complete
    assert partial.total_observed == 1


def test_batch_rejects_inconsistent_counts() -> None:
    payload = _run_driver_json("f()\n")["call_occurrences"]
    payload["total_observed"] = 99
    with pytest.raises(ValidationError):
        RubyCallOccurrenceBatchPayload.model_validate(payload)


def test_batch_payload_rejects_malformed_booleans_and_extras() -> None:
    payload = _run_driver_json("f()\n")["call_occurrences"]
    payload["parse_complete"] = "true"
    with pytest.raises(ValidationError):
        RubyCallOccurrenceBatchPayload.model_validate(payload)
    payload = _run_driver_json("f()\n")["call_occurrences"]
    payload["extra"] = 1
    with pytest.raises(ValidationError):
        RubyCallOccurrenceBatchPayload.model_validate(payload)


def test_batch_payload_rejects_unsorted_limitations_and_bad_enum() -> None:
    payload = _run_driver_json("f()\n")["call_occurrences"]
    payload["limitations"] = list(reversed(payload["limitations"]))
    with pytest.raises(ValidationError):
        RubyCallOccurrenceBatchPayload.model_validate(payload)
    fact = payload["occurrences"][0]
    fact["evaluation_context"] = "not-a-context"
    with pytest.raises(ValidationError):
        RubyCallOccurrenceFactPayload.model_validate(fact)


def test_bounded_retention_counts_and_order() -> None:
    source = "a(); b(); c();\n"
    batch = _batch(source, cap=2)
    assert batch.total_observed == 3
    assert batch.total_retained == 2
    assert batch.total_omitted == 1
    assert [fact.callee_spelling for fact in batch.occurrences] == ["a", "b"]
    assert "truncated-at-max-occurrences" in batch.limitations


def test_retained_storage_bounded_by_cap_via_driver() -> None:
    source = "".join(f"n{i}()\n" for i in range(50))
    cap = 7
    batch = _batch(source, cap=cap)
    assert batch.total_observed == 50
    assert batch.total_retained == cap
    assert len(batch.occurrences) == cap


def test_driver_rejects_invalid_cap_tokens() -> None:
    for token in ("0", "-1", "1.5", "10junk", "01"):
        completed = _run_driver("f()\n", extra_argv=[token])
        assert completed.returncode != 0
    completed = _run_driver("f()\n", extra_argv=["5", "6"])
    assert completed.returncode != 0


def test_occurrence_spans_match_independent_prism_walker() -> None:
    source = (
        "module M\n"
        "  class C < Base()\n"
        "    def m(a = d())\n"
        "      super(1)\n"
        "      yield 1\n"
        "      1.times { cb() }\n"
        "      -> { lam() }.call\n"
        "    end\n"
        "    def (recv()).singleton_m; sm(); end\n"
        "  end\n"
        "  class << expr(); sc(); end\n"
        "end\n"
        "if cond(); then_br(); elsif other(); elsif_br(); else else_br(); end\n"
        "while w(); loop_body(); end\n"
        "obj&.safe(arg())\n"
    )
    batch = _batch(source)
    walked = _independent_walk(source)
    assert batch.total_observed == walked["count"]
    retained_spans = [
        {
            "site_kind": {
                RubyCallSiteKind.CALL_NODE: "call_node",
                RubyCallSiteKind.SUPER_NODE: "super_node",
                RubyCallSiteKind.FORWARDING_SUPER_NODE: "forwarding_super_node",
                RubyCallSiteKind.YIELD_NODE: "yield_node",
            }[fact.site_kind],
            "start_byte": fact.start_byte,
            "end_byte": fact.end_byte,
            "start_line": fact.start_line,
            "start_column": fact.start_column,
            "end_line": fact.end_line,
            "end_column": fact.end_column,
        }
        for fact in batch.occurrences
    ]
    assert retained_spans == walked["spans"][: batch.total_retained]


def test_baseline_driver_fields_unchanged_by_occurrence_capture() -> None:
    source = "module A; class B; def c; d(); end; end; end\n"
    first = _baseline_document_payload(source)
    second = _baseline_document_payload(source)
    assert first == second
    document = _document(source)
    assert document.symbols
    assert document.calls


def test_limitations_include_syntactic_gaps() -> None:
    batch = _batch("f()\n")
    assert "syntactic-context-is-not-runtime-execution-or-call-order" in batch.limitations
    assert "callee-spelling-does-not-establish-name-binding-or-dispatch" in batch.limitations


def test_nested_class_body_inherits_block_deferral() -> None:
    source = "1.times { class Inner; def m; call(); end; end }\n"
    call = next(fact for fact in _facts(source) if fact.callee_spelling == "call")
    assert call.deferred
    assert call.evaluation_context is RubyCallEvaluationContext.METHOD_BODY


def test_lambda_body_is_deferred() -> None:
    source = "-> { run() }\n"
    run = next(fact for fact in _facts(source) if fact.callee_spelling == "run")
    assert run.deferred
    assert run.evaluation_context is RubyCallEvaluationContext.LAMBDA_BODY


def test_parsed_dangerous_ruby_is_never_executed() -> None:
    source = 'system("rm -rf /")\n'
    batch = _batch(source)
    assert batch.total_observed >= 1
    assert batch.occurrences[0].callee_spelling == "system"


def test_batch_output_stable_in_separate_python_processes() -> None:
    script = (
        "import dataclasses, json, subprocess; "
        "from pathlib import Path; "
        "from code_analysis.languages.ruby.call_occurrences import "
        "ruby_call_occurrence_batch; "
        "from code_analysis.languages.ruby.frontend import _PrismDocument; "
        f"driver = {str(DRIVER)!r}; "
        "source = 'a();\\nb();\\nc();\\n'; "
        "out = subprocess.check_output("
        "['/usr/local/bin/ruby', driver, '2'], input=source.encode()); "
        "doc = _PrismDocument.model_validate_json(out); "
        "batch = ruby_call_occurrence_batch(doc.call_occurrences); "
        "print(json.dumps(dataclasses.asdict(batch), sort_keys=True))"
    )
    outputs = [
        subprocess.check_output(
            [PY, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        for seed in ("0", "12345")
    ]
    assert outputs[0] == outputs[1]


def test_static_call_batch_from_driver_positive_simple_calls() -> None:
    result = static_call_batch_from_driver(
        RUBY,
        b"a(); b(); c();\n",
        max_occurrences=10,
        timeout_seconds=30.0,
    )
    assert result.failure_code is None
    assert result.failure_detail is None
    assert result.prism_version
    assert result.batch is not None
    assert result.batch.total_observed == 3
    assert result.batch.total_retained == 3
    assert result.batch.total_omitted == 0
    assert [fact.callee_spelling for fact in result.batch.occurrences] == ["a", "b", "c"]


def test_static_call_batch_from_driver_respects_retention_cap() -> None:
    result = static_call_batch_from_driver(
        RUBY,
        b"a(); b(); c();\n",
        max_occurrences=2,
        timeout_seconds=30.0,
    )
    assert result.failure_code is None
    assert result.batch is not None
    assert result.batch.total_observed == 3
    assert result.batch.total_retained == 2
    assert result.batch.total_omitted == 1
    assert len(result.batch.occurrences) == 2


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[]",
        b"null",
        b"not-json",
        b'{"call_occurrences": null}',
    ],
)
def test_static_call_batch_from_driver_rejects_malformed_document(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=payload, stderr=b""
        ),
    )
    result = static_call_batch_from_driver(
        "/tool-owned/ruby", b"f()", max_occurrences=1, timeout_seconds=1.0
    )
    assert result.batch is None
    assert result.prism_version is None
    assert result.failure_code == "ruby-prism-malformed"
    assert "ValidationError" in (result.failure_detail or "")


def test_static_call_batch_from_driver_rejects_extra_root_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _run_driver("f()\n")
    assert document.returncode == 0
    payload = json.loads(document.stdout)
    payload["unexpected_root_field"] = 1
    stdout = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=stdout, stderr=b""
        ),
    )
    result = static_call_batch_from_driver(
        "/tool-owned/ruby", b"f()", max_occurrences=1, timeout_seconds=1.0
    )
    assert result.batch is None
    assert result.failure_code == "ruby-prism-malformed"


def test_static_call_batch_from_driver_rejects_loose_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _run_driver("f()\n")
    assert document.returncode == 0
    payload = json.loads(document.stdout)
    payload["call_occurrences"]["parse_complete"] = "true"
    stdout = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout=stdout, stderr=b""
        ),
    )
    result = static_call_batch_from_driver(
        "/tool-owned/ruby", b"f()", max_occurrences=1, timeout_seconds=1.0
    )
    assert result.batch is None
    assert result.failure_code == "ruby-prism-malformed"


def test_static_call_batch_from_driver_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=2, stdout=b"", stderr=b"cap rejected"
        ),
    )
    result = static_call_batch_from_driver(RUBY, b"f()\n", max_occurrences=1, timeout_seconds=30.0)
    assert result.batch is None
    assert result.failure_code == "ruby-prism-nonzero"
    assert "status 2" in (result.failure_detail or "")


_OPERATOR_LIMITATION = "operator-callee-spelling-is-not-representable-as-identifier-chain"

_OPERATOR_SYNTHETIC_SOURCE = (
    "def m(a, b, x, y)\n"
    "  a[1]\n"
    "  a[1] = 2\n"
    "  a << 3\n"
    "  a == b\n"
    "  a =~ b\n"
    "  a !~ b\n"
    "  a < b\n"
    "  a > b\n"
    "  a <= b\n"
    "  a >= b\n"
    "  a != b\n"
    "  1 + 2\n"
    "  -x\n"
    "  !y\n"
    "end\n"
    "foo?\n"
    "bar!\n"
)

_OPERATOR_NIL_CALLEE_SPANS = (
    "a[1]",
    "a[1] = 2",
    "a << 3",
    "a == b",
    "a =~ b",
    "a !~ b",
    "a < b",
    "a > b",
    "a <= b",
    "a >= b",
    "a != b",
    "1 + 2",
    "-x",
    "!y",
)

_OPERATOR_IDENTIFIER_CALLEE_SPELLINGS = ("foo?", "bar!")


def test_operator_method_calls_use_nil_spelling_and_batch_limitation() -> None:
    payload = _run_driver_json(_OPERATOR_SYNTHETIC_SOURCE)["call_occurrences"]
    assert _OPERATOR_LIMITATION in payload["limitations"]
    spellings = {item["callee_spelling"] for item in payload["occurrences"]}
    assert None in spellings
    for identifier in _OPERATOR_IDENTIFIER_CALLEE_SPELLINGS:
        assert identifier in spellings
    for operator_name in ("[]", "[]=", "<<", "==", "=~", "!~", "+", "-@", "!"):
        assert operator_name not in spellings
    assert "a.=~" not in spellings
    assert "a.!~" not in spellings
    batch = _batch(_OPERATOR_SYNTHETIC_SOURCE)
    assert _OPERATOR_LIMITATION in batch.limitations
    assert batch.total_observed == batch.total_retained
    assert batch.total_omitted == 0


@pytest.mark.parametrize("span_text", _OPERATOR_NIL_CALLEE_SPANS)
def test_operator_call_sites_use_nil_callee_spelling_for_exact_span(span_text: str) -> None:
    batch = _batch(_OPERATOR_SYNTHETIC_SOURCE)
    matches = [
        fact
        for fact in batch.occurrences
        if _slice_span(_OPERATOR_SYNTHETIC_SOURCE, fact) == span_text
    ]
    assert len(matches) == 1
    assert matches[0].callee_spelling is None


@pytest.mark.parametrize("identifier", _OPERATOR_IDENTIFIER_CALLEE_SPELLINGS)
def test_predicate_and_bang_calls_keep_identifier_callee_spelling(identifier: str) -> None:
    batch = _batch(_OPERATOR_SYNTHETIC_SOURCE)
    matches = [fact for fact in batch.occurrences if fact.callee_spelling == identifier]
    assert len(matches) == 1
    assert _slice_span(_OPERATOR_SYNTHETIC_SOURCE, matches[0]) == identifier


def test_bound_getter_predicate_bang_and_setter_keep_exact_spellings() -> None:
    source = (
        "def check(receiver)\n"
        "  receiver.value\n"
        "  receiver.ready?\n"
        "  receiver.save!\n"
        "  receiver.value = 3\n"
        "end\n"
    )
    batch = _batch(source)
    assert batch.total_observed == batch.total_retained == 4
    assert batch.total_omitted == 0
    assert _OPERATOR_LIMITATION not in batch.limitations
    assert tuple(
        (fact.callee_spelling, _slice_span(source, fact)) for fact in batch.occurrences
    ) == (
        ("receiver.value", "receiver.value"),
        ("receiver.ready?", "receiver.ready?"),
        ("receiver.save!", "receiver.save!"),
        ("receiver.value=", "receiver.value = 3"),
    )


def test_operator_occurrence_spans_match_independent_prism_walker() -> None:
    batch = _batch(_OPERATOR_SYNTHETIC_SOURCE)
    walked = _independent_walk(_OPERATOR_SYNTHETIC_SOURCE)
    assert batch.total_observed == walked["count"]
    retained_spans = [
        {
            "site_kind": {
                RubyCallSiteKind.CALL_NODE: "call_node",
                RubyCallSiteKind.SUPER_NODE: "super_node",
                RubyCallSiteKind.FORWARDING_SUPER_NODE: "forwarding_super_node",
                RubyCallSiteKind.YIELD_NODE: "yield_node",
            }[fact.site_kind],
            "start_byte": fact.start_byte,
            "end_byte": fact.end_byte,
            "start_line": fact.start_line,
            "start_column": fact.start_column,
            "end_line": fact.end_line,
            "end_column": fact.end_column,
        }
        for fact in batch.occurrences
    ]
    assert retained_spans == walked["spans"]


def test_plain_identifier_calls_unaffected_by_operator_correction() -> None:
    batch = _batch("def run; helper(); end\nrun()\n")
    assert _OPERATOR_LIMITATION not in batch.limitations
    assert [fact.callee_spelling for fact in batch.occurrences] == ["helper", "run"]


def test_static_call_batch_from_driver_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 1.0))

    monkeypatch.setattr("subprocess.run", timeout)
    result = static_call_batch_from_driver(RUBY, b"f()\n", max_occurrences=1, timeout_seconds=0.001)
    assert result.batch is None
    assert result.failure_code == "ruby-prism-timeout"
    assert result.failure_detail == "driver timed out"
