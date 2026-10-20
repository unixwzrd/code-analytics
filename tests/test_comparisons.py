from __future__ import annotations

import json

import pytest
from conftest import FIXTURES
from test_static_call_validation import _occurrence, _with_static_calls, _zero_coverage
from test_static_call_validation import _synthetic_shell as static_call_report_shell

from code_analysis.comparison_browse import ComparisonFilters
from code_analysis.comparisons import ComparisonBuildError, build_report_delta
from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness
from code_analysis.project import discover_context
from code_analysis.projection_models import ComparisonChange
from code_analysis.reports.json_report import canonical_json, load_json, render_json
from code_analysis.reports.projection_json import render_projection_json
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.service import scan
from code_analysis.static_call_validation import static_call_identity
from code_analysis.static_comparison_source import (
    ComparisonSourceSideState,
    build_comparison_source_bindings,
    comparison_source_record_kinds,
)


def test_compatible_full_report_delta_is_deterministic_and_report_only(monkeypatch) -> None:
    report = scan(discover_context(FIXTURES / "structure_project", AnalysisConfig()))
    monkeypatch.setattr(
        "code_analysis.comparisons._records",
        lambda unused: (_ for _ in ()).throw(AssertionError("record expansion was attempted")),
    )
    first = build_report_delta(report, report)
    second = build_report_delta(report, report)
    full_policy = build_report_delta(report, report, include_unchanged=True)
    assert first == second
    assert first.projection_id != full_policy.projection_id
    assert first.identical
    assert not first.includes_unchanged
    assert first.change_counts is None
    assert first.eligible_record_count == 0
    assert first.records == ()
    assert first.record_count == 0
    assert not first.truncated
    assert first.completeness is report.completeness
    rendered = render_projection_json(first)
    assert rendered == render_projection_json(second)
    assert render_projection(first, ProjectionFormat.TSV).startswith(
        "subject-id\tchange\tbaseline-sha256\tcandidate-sha256\texplanation\n"
    )
    markdown = render_projection(first, ProjectionFormat.MARKDOWN)
    assert markdown.startswith("# Compatible full-report evidence delta\n")
    assert "No canonical report changes detected." in markdown
    assert "Selection mode: `changes-only`" in markdown
    for forbidden in ("/Users/", "/Volumes/", "/tmp/", "/private/var/"):
        assert forbidden not in rendered


def test_full_report_delta_reports_changed_records_and_truncation() -> None:
    baseline = scan(discover_context(FIXTURES / "structure_project", AnalysisConfig()))
    finding = baseline.findings[0]
    changed_finding = finding.model_copy(update={"title": finding.title + " changed"})
    candidate = baseline.model_copy(update={"findings": (changed_finding,)})
    delta = build_report_delta(baseline, candidate)
    assert not delta.identical
    assert not delta.includes_unchanged
    assert delta.change_counts is not None
    assert delta.change_counts.changed == 1
    assert delta.change_counts.unchanged > 0
    assert delta.eligible_record_count == 1
    assert not delta.truncated
    markdown = render_projection(delta, ProjectionFormat.MARKDOWN)
    assert "Global changes: added 0, removed 0, changed 1" in markdown
    changed = next(item for item in delta.records if item.subject_id == finding.finding_id)
    assert changed.change is ComparisonChange.CHANGED
    assert changed.baseline_value != changed.candidate_value

    bounded = build_report_delta(baseline, candidate, max_records=1, include_unchanged=True)
    assert bounded.truncated
    assert bounded.record_count == 1
    assert bounded.eligible_record_count > bounded.record_count
    assert bounded.projection_id != delta.projection_id

    complete = build_report_delta(baseline, candidate, include_unchanged=True)
    assert any(item.change is ComparisonChange.UNCHANGED for item in complete.records)
    assert complete.includes_unchanged


def test_full_report_delta_rejects_incompatible_configuration(ordinary_report) -> None:
    incompatible = ordinary_report.model_copy(
        update={
            "configuration": ordinary_report.configuration.model_copy(
                update={"digest_sha256": "0" * 64}
            )
        }
    )
    with pytest.raises(ComparisonBuildError, match="configuration identity"):
        build_report_delta(ordinary_report, incompatible)


def test_full_report_delta_allows_different_command_names(ordinary_report) -> None:
    candidate = ordinary_report.model_copy(
        update={"command": ordinary_report.command.model_copy(update={"name": "refactor"})}
    )
    comparison = build_report_delta(ordinary_report, candidate)
    assert not comparison.identical
    assert comparison.records == ()
    assert comparison.eligible_record_count == 0
    assert comparison.change_counts is not None
    assert comparison.change_counts.unchanged > 0


def test_comparison_json_uses_declared_scalar_values() -> None:
    report = scan(discover_context(FIXTURES / "dictionary_project", AnalysisConfig()))
    payload = json.loads(render_projection_json(build_report_delta(report, report)))
    assert payload["projection_kind"] == "comparison"
    assert payload["identical"] is True
    assert payload["inputs"][0]["role"] == "baseline"
    assert payload["inputs"][1]["role"] == "candidate"
    assert payload["records"] == []


def test_comparison_identity_is_independent_of_input_json_whitespace(ordinary_report) -> None:
    pretty = load_json(render_json(ordinary_report, compact=False))
    compact = load_json(canonical_json(ordinary_report))
    comparison = build_report_delta(pretty, compact)
    assert comparison.identical
    assert comparison.records == ()


def test_identical_partial_reports_preserve_source_partiality(ordinary_report) -> None:
    partial = ordinary_report.model_copy(update={"completeness": Completeness.PARTIAL})
    comparison = build_report_delta(partial, partial)
    assert comparison.identical
    assert comparison.completeness is Completeness.PARTIAL
    assert not comparison.truncated
    assert comparison.change_counts is None


def test_identical_saved_reports_with_static_calls_use_canonical_fast_path(monkeypatch) -> None:
    report = _with_static_calls(static_call_report_shell(), occurrences=(_occurrence(),))
    monkeypatch.setattr(
        "code_analysis.comparisons._records",
        lambda unused: (_ for _ in ()).throw(AssertionError("record expansion was attempted")),
    )
    comparison = build_report_delta(report, report)
    assert comparison.identical
    assert comparison.records == ()
    assert comparison.eligible_record_count == 0
    assert comparison.change_counts is None


def test_static_call_occurrence_only_change_surfaces_in_delta() -> None:
    before_call = _occurrence(callee_spelling="helper")
    after_call = _occurrence(callee_spelling="other")
    after_call = after_call.model_copy(update={"occurrence_id": static_call_identity(after_call)})
    baseline = _with_static_calls(static_call_report_shell(), occurrences=(before_call,))
    candidate = _with_static_calls(static_call_report_shell(), occurrences=(after_call,))
    delta = build_report_delta(baseline, candidate)
    assert not delta.identical
    assert delta.eligible_record_count == 2
    assert delta.change_counts is not None
    assert delta.change_counts.added == 1
    assert delta.change_counts.removed == 1
    assert delta.change_counts.changed == 0
    kinds = {item.explanation.split()[0] for item in delta.records}
    assert kinds == {"static-call-occurrence"}
    bindings = build_comparison_source_bindings(baseline, candidate, delta)
    assert len(bindings) == 2
    binding = next(item for item in bindings if item.change is ComparisonChange.REMOVED)
    assert binding.record_kind == "static-call-occurrence"
    assert binding.subject_id == before_call.occurrence_id
    assert binding.baseline.state is ComparisonSourceSideState.AVAILABLE
    assert binding.candidate.state is ComparisonSourceSideState.MISSING_RECORD
    assert binding.baseline.options


def test_static_call_coverage_change_surfaces_in_delta() -> None:
    call = _occurrence()
    shell = static_call_report_shell()
    baseline = _with_static_calls(
        shell,
        occurrences=(call,),
        coverage=(_zero_coverage(retained_count=1, observed_count=1),),
    )
    candidate = _with_static_calls(
        shell,
        occurrences=(call,),
        coverage=(
            _zero_coverage(
                retained_count=1,
                observed_count=1,
                limitations=("coverage reconciliation changed",),
            ),
        ),
    )
    coverage_key = f"static-call-coverage:{call.source_file_id}"
    delta = build_report_delta(baseline, candidate)
    assert not delta.identical
    changed = [item for item in delta.records if item.subject_id == coverage_key]
    assert len(changed) == 1
    assert changed[0].change is ComparisonChange.CHANGED
    bindings = build_comparison_source_bindings(baseline, candidate, delta)
    coverage_binding = next(item for item in bindings if item.record_kind == "static-call-coverage")
    assert coverage_binding.baseline.state is ComparisonSourceSideState.UNAVAILABLE
    assert coverage_binding.candidate.state is ComparisonSourceSideState.UNAVAILABLE
    assert not coverage_binding.baseline.options
    assert not coverage_binding.candidate.options


def test_static_call_record_kinds_are_valid_comparison_filters() -> None:
    kinds = {"static-call-occurrence", "static-call-coverage"}
    assert kinds <= comparison_source_record_kinds()
    ComparisonFilters(kinds=("static-call-occurrence", "static-call-coverage"))
