from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from code_analysis.config import AnalysisConfig
from code_analysis.history import build_generation_history, render_change_report
from code_analysis.project import discover_context
from code_analysis.reports.json_report import render_json
from code_analysis.reports.markdown import render_markdown
from code_analysis.service import scan
from code_analysis.workspace import (
    CacheStatistics,
    create_run_record,
    prepare_workspace,
    write_workspace_results,
)


def _write_generation(project: Path, second: int):
    context = discover_context(project, AnalysisConfig())
    workspace = prepare_workspace(context)
    report = scan(context)
    report_json = render_json(report)
    run = create_run_record(
        context,
        workspace,
        started_at=datetime(2026, 8, 24, 12, 0, second, tzinfo=UTC),
        duration_ms=1,
        command=("code-analysis", "scan", str(project)),
        providers=(),
        portable_report_sha256=hashlib.sha256(report_json.encode()).hexdigest(),
        interpreter_path="/host/python",
        cache=CacheStatistics(),
    )
    write_workspace_results(
        workspace,
        report_json=report_json,
        report_markdown=render_markdown(report),
        run=run,
    )
    return workspace


def test_generation_history_uses_retained_json_without_source_access(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    workspace = _write_generation(project, 1)
    source.write_text("value = 2\n", encoding="utf-8")
    _write_generation(project, 2)
    source.unlink()
    report = build_generation_history(workspace)
    assert len(report.generation_changes) == 1
    assert report.generation_changes[0].changed_paths == ("module.py",)
    first = render_change_report(report)
    assert first == render_change_report(build_generation_history(workspace))
    assert str(tmp_path) not in first
    assert "snapshot evidence" in render_change_report(report, markdown=True)


def test_generation_history_records_add_and_remove_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    first = project / "first.py"
    first.write_text("value = 1\n", encoding="utf-8")
    workspace = _write_generation(project, 1)
    first.unlink()
    (project / "second.py").write_text("value = 2\n", encoding="utf-8")
    _write_generation(project, 2)
    change = build_generation_history(workspace).generation_changes[0]
    assert change.added_paths == ("second.py",)
    assert change.removed_paths == ("first.py",)
