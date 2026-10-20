from __future__ import annotations

import json
import pstats
import subprocess
import sys
from pathlib import Path

import pytest

from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.reports.json_report import canonical_json
from code_analysis.service import scan
from code_analysis.web_service import ReportViewService


@pytest.mark.parametrize("scope", ["load", "requests"])
def test_ui_measurement_profiles_only_selected_stage(ordinary_report, tmp_path, scope) -> None:
    report = tmp_path / "report.json"
    report.write_text(canonical_json(ordinary_report), encoding="utf-8")
    output = tmp_path / "measurement.json"
    profile = tmp_path / "measurement.prof"
    script = Path(__file__).resolve().parents[1] / "tools" / "measure_ui_service.py"
    subprocess.run(
        [
            sys.executable,
            str(script),
            str(report),
            str(output),
            "--profile",
            str(profile),
            "--profile-scope",
            scope,
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=30,
    )
    measured = json.loads(output.read_text())
    assert measured["profiled"] is True
    assert measured["profile_scope"] == scope
    assert len(measured["samples"]) == 16
    functions = {key[2] for key in pstats.Stats(str(profile)).stats}
    assert ("load_json" in functions) == (scope == "load")
    assert ("bootstrap" in functions) == (scope == "requests")


@pytest.mark.parametrize("mode", ["full", "indexed", "graphs"])
def test_fixed_measurement_repeats_only_four_inputs(ordinary_report, tmp_path, mode) -> None:
    tools = Path(__file__).resolve().parents[1] / "tools"
    output = tmp_path / "measurement.json"
    if mode == "graphs":
        ordinary_report = scan(
            discover_context(
                Path(__file__).parent / "fixtures" / "structure_project", AnalysisConfig()
            )
        )
    if mode != "full":
        target = tmp_path / "index"
        manifest = build_browse_index(ReportViewService(ordinary_report), target)
        script = tools / "measure_browse_index.py"
        extra = ["--report-sha256", manifest.report_sha256]
        if mode == "graphs":
            extra.append("--static-graphs")
    else:
        target = tmp_path / "report.json"
        target.write_text(canonical_json(ordinary_report), encoding="utf-8")
        script = tools / "measure_ui_service.py"
        extra = []
    subprocess.run(
        [sys.executable, str(script), str(target), str(output), "--fixed-pages", *extra],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=30,
    )
    measured = json.loads(output.read_text())
    assert measured["fixed_pages"] is True
    assert measured["profiled"] is False
    assert len(measured["samples"]) == 16
    operations = (
        (
            "ownership-alias-flow",
            "structure-lifecycle-graph",
            "dictionary-flow",
            "lexical-call-graph",
        )
        if mode == "graphs"
        else ("bootstrap", "modules", "findings", "candidates")
    )
    if mode != "full":
        assert measured["static_graphs"] is (mode == "graphs")
    assert {item["operation"] for item in measured["samples"]} == set(operations)
    for operation in operations:
        samples = [item for item in measured["samples"] if item["operation"] == operation]
        assert len(samples) == 4
        assert len({item["output_sha256"] for item in samples}) == 1
