"""Invocation navigation uses retained evidence without consulting source."""

from pathlib import Path

import pytest
from test_static_call_validation import _occurrence, _synthetic_shell, _with_static_calls

from code_analysis.graph_ownership import graph_ownership
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.ui_navigation import NavigationUnavailable, navigation_page


def test_call_navigation_uses_invocation_not_subject_span(monkeypatch: pytest.MonkeyPatch):
    call = _occurrence()
    report = load_json(render_json(_with_static_calls(_synthetic_shell(), occurrences=(call,))))

    def no_source(*args, **kwargs):
        pytest.fail("navigation read source")

    monkeypatch.setattr(Path, "read_text", no_source)
    monkeypatch.setattr(Path, "read_bytes", no_source)
    page = navigation_page(report, call.occurrence_id)
    assert page.eligible == 1
    assert page.records[0].identity == call.occurrence_id
    assert page.records[0].span == call.evidence[0].span
    assert page.records[0].kind == "static-call-site"
    assert page.records[0].span != report.subjects[0].evidence[0].span
    assert navigation_page(report, call.source_file_id).records == page.records
    ownership = graph_ownership(report)
    assert ownership.module_for(call.occurrence_id) == call.module_name
    assert ownership.module_for(call.source_file_id) == call.module_name


def test_dynamic_call_navigation_does_not_invent_target():
    call = _occurrence(callee_spelling=None, enclosing_subject_id=None)
    report = _with_static_calls(_synthetic_shell(), occurrences=(call,))
    page = navigation_page(report, call.occurrence_id)
    assert page.records[0].label == "Dynamic callee"
    with pytest.raises(NavigationUnavailable):
        navigation_page(report, "Dynamic callee")
