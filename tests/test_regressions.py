from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.cli import ExitCode, main
from code_analysis.comparisons import ComparisonBuildError
from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.regression_models import RegressionKind, RegressionObservation
from code_analysis.regressions import build_regression_report, render_regression
from code_analysis.reports.json_report import render_json
from code_analysis.service import scan


def _reports(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "module.py").write_text("value = 1\n", encoding="utf-8")
    baseline = scan(discover_context(project, AnalysisConfig()))
    source = baseline.source_files[0]
    module = baseline.modules[0]
    candidate = baseline.model_copy(
        update={
            "source_files": (source.model_copy(update={"physical_loc": 30, "nonblank_loc": 25}),),
            "modules": (module.model_copy(update={"line_count": 30, "nonblank_line_count": 25}),),
        }
    )
    return baseline, candidate


def test_regression_report_records_module_growth_without_source_access(tmp_path: Path) -> None:
    baseline, candidate = _reports(tmp_path)
    report = build_regression_report(baseline, candidate)
    assert [item.kind for item in report.observations] == [RegressionKind.MODULE_GROWTH]
    observation = report.observations[0]
    assert observation.baseline_value == 1
    assert observation.candidate_value == 30
    assert observation.absolute_change == 29
    assert observation.relative_change == 29.0
    for output_format in ("json", "json-compact", "tsv", "markdown"):
        rendered = render_regression(report, output_format)
        assert rendered == render_regression(report, output_format)
        assert str(tmp_path) not in rendered


def test_regression_bounds_compatibility_and_strict_models(tmp_path: Path) -> None:
    baseline, candidate = _reports(tmp_path)
    report = build_regression_report(baseline, candidate, max_records=1)
    assert report.counts.eligible == report.counts.retained == 1
    incompatible = candidate.model_copy(
        update={
            "configuration": candidate.configuration.model_copy(update={"strictness": "strict"})
        }
    )
    with pytest.raises(ComparisonBuildError, match="configuration identity"):
        build_regression_report(baseline, incompatible)
    with pytest.raises(ComparisonBuildError, match="between 1 and 10000"):
        build_regression_report(baseline, candidate, max_records=0)
    with pytest.raises(ValidationError, match="thresholds"):
        RegressionObservation(
            observation_id="regression-observation-" + "a" * 20,
            kind=RegressionKind.MODULE_GROWTH,
            subject_id="source-file-" + "b" * 20,
            baseline_value=100,
            candidate_value=110,
            absolute_change=10,
            relative_change=0.1,
            explanation="insufficient growth",
        )


def test_regress_cli_reads_only_saved_reports(tmp_path: Path, capsys) -> None:
    baseline, candidate = _reports(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline_path.write_text(render_json(baseline), encoding="utf-8")
    candidate_path.write_text(render_json(candidate), encoding="utf-8")
    result = main(
        [
            "regress",
            str(baseline_path),
            str(candidate_path),
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.SUCCESS
    assert json.loads(captured.out)["counts"] == {"eligible": 1, "omitted": 0, "retained": 1}
    assert captured.err == ""
