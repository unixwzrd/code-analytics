"""Saved-report call-site query preserves invocation identity and evidence."""

from pathlib import Path

import pytest
from test_static_call_validation import _occurrence, _synthetic_shell, _with_static_calls

from code_analysis.cli import main
from code_analysis.queries import QueryBuildError, build_query, render_query_json
from code_analysis.query_models import QueryItemKind, QuerySelector, QuerySelectorKind
from code_analysis.reports.json_report import load_json, render_json


def test_call_query_is_source_free_and_deterministic(monkeypatch: pytest.MonkeyPatch):
    call = _occurrence()
    report = load_json(render_json(_with_static_calls(_synthetic_shell(), occurrences=(call,))))

    def forbidden(*args, **kwargs):
        pytest.fail("query attempted to read source")

    monkeypatch.setattr(Path, "read_text", forbidden)
    selector = QuerySelector(kind=QuerySelectorKind.CALL_SITE, identity=call.occurrence_id)
    first = build_query(report, selector)
    assert render_query_json(first) == render_query_json(build_query(report, selector))
    item = next(item for item in first.items if item.kind is QueryItemKind.CALL_SITE)
    assert item.identity == call.occurrence_id
    assert item.evidence == call.evidence
    assert "callee binding" in " ".join(item.limitations)
    assert not any("helper" in related for related in item.related_ids)
    with pytest.raises(QueryBuildError, match="not a call-site"):
        build_query(report, selector.model_copy(update={"identity": report.subjects[0].subject_id}))


def test_call_query_cli_from_saved_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    call = _occurrence(callee_spelling=None)
    report = _with_static_calls(_synthetic_shell(), occurrences=(call,))
    saved = tmp_path / "report.json"
    saved.write_text(render_json(report))
    status = main(["query", str(saved), "--call-site", call.occurrence_id, "--json"])
    assert status == 0
    captured = capsys.readouterr()
    assert "dynamic callee" in captured.out
    assert not captured.err
