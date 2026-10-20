"""Independent regressions for static-call assembly boundaries."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_analysis.config import AnalysisConfig, LanguageSelection
from code_analysis.project import discover_context
from code_analysis.service import scan_contract_inventory
from code_analysis.static_call_reporting import integrate_static_call_report

_PRISM_CALL_SITE_COUNTER = (
    'require "json"; source = STDIN.read; result = Prism.parse(source); count = 0; '
    "sites = %w[call_node super_node forwarding_super_node yield_node]; "
    "walk = lambda do |node| "
    "count += 1 if sites.include?(node.type.to_s); "
    "node.compact_child_nodes.each { |c| walk.call(c) }; end; walk.call(result.value); "
    'puts JSON.generate({"count" => count})'
)


def test_auto_python_does_not_probe_javascript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (tmp_path / "app.py").write_text("f()\n")
    context = discover_context(tmp_path, AnalysisConfig())
    base = scan_contract_inventory(context)

    def forbidden():
        pytest.fail("Python-only source collection probed JavaScript")

    monkeypatch.setattr(
        "code_analysis.static_call_reporting.javascript_static_call_provider_identity", forbidden
    )
    report = integrate_static_call_report(context, base)
    assert report.summary.static_calls_observed == 1


def test_exhausted_global_budget_uses_one_slot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from code_analysis import static_call_reporting as reporting

    (tmp_path / "a.py").write_text("f()\ng()\n")
    (tmp_path / "b.py").write_text("h()\ni()\nj()\n")
    context = discover_context(
        tmp_path,
        AnalysisConfig(languages=(LanguageSelection.PYTHON,), max_static_call_occurrences=2),
    )
    base = scan_contract_inventory(context)
    original = reporting.static_call_batch_from_source
    caps: list[int] = []

    def observed(source: str, *, max_occurrences: int):
        caps.append(max_occurrences)
        return original(source, max_occurrences=max_occurrences)

    monkeypatch.setattr(reporting, "static_call_batch_from_source", observed)
    report = integrate_static_call_report(context, base)
    assert caps == [2, 1]
    assert report.summary.static_calls_observed == 5
    assert report.summary.static_calls_omitted == 3
    assert any(
        "truncat" in diagnostic.code or "limit" in diagnostic.code
        for diagnostic in report.diagnostics
    )


@pytest.mark.parametrize("payload", [b"{}", b"[]", b"null", b'{"call_occurrences": null}'])
def test_ruby_malformed_document_is_structured_failure(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
):
    from code_analysis.languages.ruby.call_occurrences import static_call_batch_from_driver

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=payload, stderr=b""),
    )
    result = static_call_batch_from_driver(
        "/tool-owned/ruby", b"f()", max_occurrences=1, timeout_seconds=1.0
    )
    assert result.batch is None
    assert result.failure_code == "ruby-prism-malformed"


@pytest.mark.parametrize("invalid_field", ("span", "scope", "strict-bool"))
def test_invalid_normalized_span_is_isolated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, invalid_field: str
):
    from code_analysis import static_call_reporting as reporting

    (tmp_path / "a.py").write_text("bad()\n")
    (tmp_path / "b.py").write_text("good()\n")
    context = discover_context(tmp_path, AnalysisConfig(languages=(LanguageSelection.PYTHON,)))
    base = scan_contract_inventory(context)
    original = reporting.normalize_python_call_fact

    def invalid(fact):
        normalized = original(fact)
        if normalized.callee_spelling == "bad":
            if invalid_field == "span":
                return replace(normalized, end_line=100)
            if invalid_field == "scope":
                return replace(
                    normalized,
                    scope_path=(
                        normalized.scope_path[0].model_copy(update={"definition_line": 100}),
                    ),
                )
            return replace(normalized, conditional=1)
        return normalized

    monkeypatch.setattr(reporting, "normalize_python_call_fact", invalid)
    report = integrate_static_call_report(context, base)
    assert [item.callee_spelling for item in report.static_call_occurrences] == ["good"]
    assert report.completeness.value == "partial"
    assert any(item.state.value == "failed" for item in report.static_call_coverage)
    assert any("invalid" in item.code for item in report.diagnostics)
    assert not any(item.code == "static-call-global-retention-limit" for item in report.diagnostics)


def test_ruby_operator_calls_retained_without_invalid_static_records(tmp_path: Path):
    import json
    import subprocess

    from code_analysis.languages.base import FrontendContext
    from code_analysis.languages.ruby.frontend import RubyPrismFrontend
    from code_analysis.models import AvailabilityState
    from code_analysis.service import scan

    config = AnalysisConfig(languages=(LanguageSelection.RUBY,))
    availability = RubyPrismFrontend().availability(FrontendContext(config, config.identity()))
    if availability.state is not AvailabilityState.AVAILABLE:
        pytest.skip("optional Ruby/Prism unavailable")
    ruby_source = (
        "def m(items, other, name, pattern)\n"
        "  items[0]\n"
        "  items << 1\n"
        "  self == other\n"
        "  name =~ pattern\n"
        "  name !~ pattern\n"
        "  1 + 2\n"
        "end\n"
        "ready?\n"
    )
    (tmp_path / "app.rb").write_text(ruby_source)
    prism_walk = subprocess.run(
        [
            "/usr/local/bin/ruby",
            "-rprism",
            "-e",
            _PRISM_CALL_SITE_COUNTER,
        ],
        input=ruby_source.encode("utf-8"),
        check=True,
        capture_output=True,
    )
    expected_prism_sites = json.loads(prism_walk.stdout)["count"]
    context = discover_context(tmp_path, config)
    report = scan(context)
    operator_limitation = "operator-callee-spelling-is-not-representable-as-identifier-chain"
    assert report.summary.static_calls_observed == expected_prism_sites
    assert report.summary.static_call_occurrences == expected_prism_sites
    assert report.summary.static_calls_omitted == 0
    assert report.completeness.value == "complete"
    assert not any("invalid" in item.code for item in report.diagnostics)
    assert not any(item.state.value == "failed" for item in report.static_call_coverage)
    assert any(operator_limitation in call.limitations for call in report.static_call_occurrences)
    assert any(call.callee_spelling is None for call in report.static_call_occurrences)
    assert any(call.callee_spelling == "ready?" for call in report.static_call_occurrences)
    assert not any(
        call.callee_spelling and ".=~" in call.callee_spelling
        for call in report.static_call_occurrences
    )


def test_ruby_malformed_callee_spelling_remains_strict_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from code_analysis import static_call_reporting as reporting
    from code_analysis.languages.base import FrontendContext
    from code_analysis.languages.ruby.frontend import RubyPrismFrontend
    from code_analysis.models import AvailabilityState
    from code_analysis.service import scan

    config = AnalysisConfig(languages=(LanguageSelection.RUBY,))
    availability = RubyPrismFrontend().availability(FrontendContext(config, config.identity()))
    if availability.state is not AvailabilityState.AVAILABLE:
        pytest.skip("optional Ruby/Prism unavailable")

    (tmp_path / "app.rb").write_text("good(); bad()\n")
    original = reporting.normalize_ruby_call_fact

    def invalid(fact):
        normalized = original(fact)
        if normalized.callee_spelling == "bad":
            return replace(normalized, callee_spelling="not a valid chain!")
        return normalized

    monkeypatch.setattr(reporting, "normalize_ruby_call_fact", invalid)
    context = discover_context(tmp_path, config)
    report = scan(context)
    assert [item.callee_spelling for item in report.static_call_occurrences] == ["good"]
    assert report.completeness.value == "partial"
    assert any(item.state.value in {"failed", "partial"} for item in report.static_call_coverage)
    assert any("invalid" in item.code for item in report.diagnostics)


def test_mixed_frontends_retain_all_sites_and_repeat_deterministically(tmp_path: Path):
    from code_analysis.languages.base import FrontendContext
    from code_analysis.languages.ruby.frontend import RubyPrismFrontend
    from code_analysis.models import AvailabilityState
    from code_analysis.reports.json_report import canonical_json, load_json
    from code_analysis.service import scan

    pytest.importorskip("tree_sitter_javascript")
    config = AnalysisConfig(
        languages=(
            LanguageSelection.PYTHON,
            LanguageSelection.JAVASCRIPT,
            LanguageSelection.RUBY,
        )
    )
    availability = RubyPrismFrontend().availability(FrontendContext(config, config.identity()))
    if availability.state is not AvailabilityState.AVAILABLE:
        pytest.skip("optional Ruby/Prism unavailable")
    (tmp_path / "app.py").write_text("def run():\n    helper()\nrun()\n")
    (tmp_path / "app.js").write_text("function run() { helper(); }\nrun();\n")
    (tmp_path / "app.rb").write_text("def run\n  helper()\nend\nrun()\n")
    context = discover_context(tmp_path, config)
    first = scan(context)
    second = scan(context)
    assert canonical_json(first) == canonical_json(second)
    retained = load_json(canonical_json(first))
    assert len(retained.static_call_coverage) == 3
    assert {row.state.value for row in retained.static_call_coverage} == {"complete"}
    assert {row.observed_count for row in retained.static_call_coverage} == {2}
    assert retained.summary.static_call_occurrences == 6
    assert {call.language.value for call in retained.static_call_occurrences} == {
        "python",
        "javascript",
        "ruby",
    }
