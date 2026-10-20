from __future__ import annotations

import builtins
import errno
import hashlib
import json
import sys
from pathlib import Path

import pytest

from code_analysis.cli import ExitCode, main
from code_analysis.config import AnalysisConfig, PhaseFourLimits
from code_analysis.coverage_artifacts import load_coverage_artifact
from code_analysis.coverage_evidence import import_coverage
from code_analysis.models import Completeness
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.report_models import CoverageBindingState, SectionState
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.service import scan

GOLDEN = Path(__file__).parent / "golden"


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    source = root / "module.py"
    source.write_text("def calculate(value):\n    return value + 1\n", encoding="utf-8")
    return root, source


def _coverage_json(path: Path, source_name: str = "module.py") -> None:
    document = {
        "meta": {
            "format": 3,
            "version": "7.14.0",
            "timestamp": "2026-08-24T12:00:00Z",
            "branch_coverage": True,
            "show_contexts": True,
        },
        "files": {
            source_name: {
                "executed_lines": [1, 2],
                "summary": {
                    "covered_lines": 2,
                    "num_statements": 2,
                    "percent_covered": 100.0,
                    "percent_covered_display": "100",
                    "missing_lines": 0,
                    "excluded_lines": 0,
                    "num_branches": 0,
                    "num_partial_branches": 0,
                    "covered_branches": 0,
                    "missing_branches": 0,
                },
                "missing_lines": [],
                "excluded_lines": [],
                "executed_branches": [[1, 2]],
                "missing_branches": [],
                "contexts": {
                    "1": ["tests.test_module.test_calculate"],
                    "2": ["tests.test_module.test_calculate"],
                },
                "functions": {},
                "classes": {},
            }
        },
        "totals": {
            "covered_lines": 2,
            "num_statements": 2,
            "percent_covered": 100.0,
            "percent_covered_display": "100",
            "missing_lines": 0,
            "excluded_lines": 0,
            "num_branches": 0,
            "num_partial_branches": 0,
            "covered_branches": 0,
            "missing_branches": 0,
        },
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def test_coverage_json_binds_lines_contexts_arcs_and_symbols(tmp_path: Path) -> None:
    root, _source = _project(tmp_path)
    report = scan(discover_context(root, AnalysisConfig()))
    data = tmp_path / "coverage.json"
    _coverage_json(data)
    updated = load_json(
        render_json(
            import_coverage(
                report,
                (data,),
                source_root=root,
                workload_identity_sha256="1" * 64,
            )
        )
    )
    assert updated.coverage_artifacts[0].binding is CoverageBindingState.BOUND
    assert updated.coverage_files[0].covered_lines == (1, 2)
    assert updated.coverage_files[0].arcs == ((1, 2),)
    assert updated.coverage_files[0].contexts == ("tests.test_module.test_calculate",)
    assert updated.coverage_observations
    section = next(item for item in updated.section_coverage if item.section == "coverage")
    assert section.state is SectionState.COMPLETE
    assert updated.completeness is Completeness.COMPLETE
    assert (
        import_coverage(
            updated,
            (data,),
            source_root=root,
            workload_identity_sha256="1" * 64,
        )
        == updated
    )
    for name, output_format in (
        (ProjectionName.COVERAGE_FILES, ProjectionFormat.TSV),
        (ProjectionName.COVERAGE_FILES, ProjectionFormat.MARKDOWN),
        (ProjectionName.COVERAGE_MATRIX, ProjectionFormat.TSV),
        (ProjectionName.COVERAGE_MATRIX, ProjectionFormat.JSON),
    ):
        rendered = render_projection(build_projection(updated, name), output_format)
        assert rendered == render_projection(build_projection(updated, name), output_format)
        assert str(tmp_path) not in rendered


def test_coverage_source_mismatch_is_partial_and_cannot_look_bound(tmp_path: Path) -> None:
    root, source = _project(tmp_path)
    report = scan(discover_context(root, AnalysisConfig()))
    data = tmp_path / "coverage.json"
    _coverage_json(data)
    source.write_text("def calculate(value):\n    return value + 2\n", encoding="utf-8")
    updated = import_coverage(
        report,
        (data,),
        source_root=root,
        workload_identity_sha256="2" * 64,
    )
    assert updated.coverage_artifacts[0].binding is CoverageBindingState.SOURCE_MISMATCH
    assert updated.completeness is Completeness.PARTIAL
    assert any(item.code == "coverage-source-unbound" for item in updated.diagnostics)


def test_coverage_binds_discovered_test_module_hashes(tmp_path: Path) -> None:
    root, _source = _project(tmp_path)
    tests = root / "tests"
    tests.mkdir()
    test_source = tests / "test_module.py"
    test_source.write_text("def test_value():\n    assert True\n", encoding="utf-8")
    report = scan(discover_context(root, AnalysisConfig()))
    data = tmp_path / "test-coverage.json"
    _coverage_json(data, "tests/test_module.py")
    updated = import_coverage(
        report,
        (data,),
        source_root=root,
        workload_identity_sha256="5" * 64,
    )
    assert updated.coverage_files[0].binding is CoverageBindingState.BOUND
    assert updated.coverage_files[0].source_sha256 == report.test_modules[0].source_sha256


def test_malformed_coverage_fails_closed(tmp_path: Path) -> None:
    root, _source = _project(tmp_path)
    report = scan(discover_context(root, AnalysisConfig()))
    malformed = tmp_path / "coverage.json"
    malformed.write_text("{}\n", encoding="utf-8")
    with pytest.raises(OSError) as exc_info:
        import_coverage(
            report,
            (malformed,),
            source_root=root,
            workload_identity_sha256="3" * 64,
        )
    assert exc_info.value.errno == errno.EBADMSG


def test_coverage_data_uses_public_api_and_binds_source(tmp_path: Path) -> None:
    coverage = pytest.importorskip("coverage")
    root, source = _project(tmp_path)
    report = scan(discover_context(root, AnalysisConfig()))
    data_path = tmp_path / ".coverage"
    data = coverage.CoverageData(basename=str(data_path))
    data.set_context("tests.test_module.test_calculate")
    data.add_lines({str(source): {1, 2}})
    data.write()
    updated = import_coverage(
        report,
        (data_path,),
        source_root=root,
        workload_identity_sha256="6" * 64,
    )
    assert updated.coverage_files[0].binding is CoverageBindingState.BOUND
    assert updated.coverage_files[0].covered_lines == (1, 2)
    assert updated.coverage_files[0].contexts == ("tests.test_module.test_calculate",)


def test_coverage_data_unavailable_is_explicit(tmp_path: Path, monkeypatch) -> None:
    data_path = tmp_path / ".coverage"
    data_path.write_bytes(b"not coverage data")
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "coverage":
            raise ImportError("coverage deliberately unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(OSError) as exc_info:
        load_coverage_artifact(data_path)
    assert exc_info.value.errno == errno.ENOSYS


def test_coverage_bounds_are_truncated_and_diagnostic(tmp_path: Path) -> None:
    root, _source = _project(tmp_path)
    report = scan(discover_context(root, AnalysisConfig()))
    data_path = tmp_path / "coverage.json"
    _coverage_json(data_path)
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    payload["files"]["module.py"]["executed_branches"] = [[1, 2], [2, 1]]
    data_path.write_text(json.dumps(payload), encoding="utf-8")
    updated = import_coverage(
        report,
        (data_path,),
        source_root=root,
        workload_identity_sha256="7" * 64,
        limits=PhaseFourLimits(max_coverage_arcs=1),
    )
    section = next(item for item in updated.section_coverage if item.section == "coverage")
    assert section.state is SectionState.TRUNCATED
    assert section.truncated == 1
    assert any(item.code == "coverage-bound-truncated" for item in updated.diagnostics)


def test_coverage_replace_discards_previous_artifact(tmp_path: Path) -> None:
    root, _source = _project(tmp_path)
    report = scan(discover_context(root, AnalysisConfig()))
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    _coverage_json(first_path)
    _coverage_json(second_path)
    payload = json.loads(second_path.read_text(encoding="utf-8"))
    payload["meta"]["timestamp"] = "2026-08-24T12:00:01Z"
    second_path.write_text(json.dumps(payload), encoding="utf-8")
    first = import_coverage(
        report,
        (first_path,),
        source_root=root,
        workload_identity_sha256="8" * 64,
    )
    second = import_coverage(
        first,
        (second_path,),
        source_root=root,
        workload_identity_sha256="9" * 64,
        replace=True,
    )
    assert len(second.coverage_artifacts) == 1
    assert second.coverage_artifacts[0].coverage_id != first.coverage_artifacts[0].coverage_id


def test_coverage_run_requires_explicit_authorization(tmp_path: Path, capsys) -> None:
    root, _source = _project(tmp_path)
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(scan(discover_context(root, AnalysisConfig()))))
    result = main(
        [
            "coverage-run",
            str(report_path),
            "--cwd",
            str(root),
            "--",
            sys.executable,
            "-c",
            "print('forbidden')",
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.EPERM
    assert captured.out == ""
    assert "requires --allow-execution" in captured.err
    assert not (root / ".code-analysis").exists()


def test_coverage_run_captures_bound_evidence_and_local_streams(tmp_path: Path, capsys) -> None:
    root, _source = _project(tmp_path)
    runner = root / "runner.py"
    runner.write_text("from module import calculate\nprint(calculate(1))\n", encoding="utf-8")
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(scan(discover_context(root, AnalysisConfig()))))
    result = main(
        [
            "coverage-run",
            str(report_path),
            "--allow-execution",
            "--cwd",
            str(root),
            "--quiet",
            "--",
            sys.executable,
            str(runner),
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.SUCCESS
    assert captured.out == ""
    assert "untrusted project code" in captured.err
    latest = root / ".code-analysis" / "generations" / "coverage" / "latest"
    assert latest.is_symlink()
    updated = load_json((latest / "report.json").read_text(encoding="utf-8"))
    assert updated.coverage_artifacts
    assert all(item.binding is CoverageBindingState.BOUND for item in updated.coverage_artifacts)
    assert (latest / "stdout-001.bin").read_text(encoding="utf-8") == "2\n"
    assert hashlib.sha256((latest / "stdout-001.bin").read_bytes()).hexdigest() == next(
        item.stdout_sha256 for item in updated.providers if item.provider_id == "coverage-run"
    )


def test_coverage_run_unexpected_status_is_eproto_but_retained(tmp_path: Path, capsys) -> None:
    root, _source = _project(tmp_path)
    runner = root / "fail.py"
    runner.write_text("raise SystemExit(3)\n", encoding="utf-8")
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(scan(discover_context(root, AnalysisConfig()))))
    result = main(
        [
            "coverage-run",
            str(report_path),
            "--allow-execution",
            "--cwd",
            str(root),
            "--quiet",
            "--",
            sys.executable,
            str(runner),
        ]
    )
    capsys.readouterr()
    assert result == ExitCode.EPROTO
    assert (root / ".code-analysis" / "generations" / "coverage" / "latest").is_symlink()


def test_coverage_import_cli_writes_bound_generation(tmp_path: Path, capsys) -> None:
    root, _source = _project(tmp_path)
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(scan(discover_context(root, AnalysisConfig()))))
    data = tmp_path / "coverage.json"
    _coverage_json(data)
    result = main(
        [
            "coverage-import",
            str(report_path),
            str(data),
            "--source-root",
            str(root),
        ]
    )
    payload = load_json(capsys.readouterr().out)
    assert result == ExitCode.SUCCESS
    assert payload.coverage_artifacts[0].binding is CoverageBindingState.BOUND
    assert (root / ".code-analysis" / "generations" / "coverage" / "latest").is_symlink()


def test_coverage_run_symbolic_expected_status_and_timeout(tmp_path: Path, capsys) -> None:
    root, _source = _project(tmp_path)
    expected_runner = root / "expected.py"
    expected_runner.write_text("import errno\nraise SystemExit(errno.ENODATA)\n", encoding="utf-8")
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(scan(discover_context(root, AnalysisConfig()))))
    result = main(
        [
            "coverage-run",
            str(report_path),
            "--allow-execution",
            "--cwd",
            str(root),
            "--expected-status",
            "ENODATA",
            "--quiet",
            "--",
            sys.executable,
            str(expected_runner),
        ]
    )
    capsys.readouterr()
    assert result == ExitCode.SUCCESS

    timeout_runner = root / "timeout.py"
    timeout_runner.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    result = main(
        [
            "coverage-run",
            str(report_path),
            "--allow-execution",
            "--cwd",
            str(root),
            "--timeout",
            "0.05",
            "--quiet",
            "--",
            sys.executable,
            str(timeout_runner),
        ]
    )
    capsys.readouterr()
    assert result == ExitCode.ETIMEDOUT


def test_coverage_run_rejects_nonallowlisted_environment(tmp_path: Path, capsys) -> None:
    root, _source = _project(tmp_path)
    runner = root / "runner.py"
    runner.write_text("pass\n", encoding="utf-8")
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(scan(discover_context(root, AnalysisConfig()))))
    result = main(
        [
            "coverage-run",
            str(report_path),
            "--allow-execution",
            "--cwd",
            str(root),
            "--env",
            "SECRET_VALUE=hidden",
            "--quiet",
            "--",
            sys.executable,
            str(runner),
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.EPERM
    assert "hidden" not in captured.err


def test_coverage_report_and_projections_are_authoritative_goldens() -> None:
    report_json = (GOLDEN / "phase4-coverage-report.json").read_text(encoding="utf-8")
    report = load_json(report_json)
    assert render_json(report) == report_json
    assert all(item.binding is CoverageBindingState.BOUND for item in report.coverage_files)
    cases = (
        (ProjectionName.COVERAGE_FILES, ProjectionFormat.TSV, "phase4-coverage-files.tsv"),
        (
            ProjectionName.COVERAGE_FILES,
            ProjectionFormat.MARKDOWN,
            "phase4-coverage-files.md",
        ),
        (ProjectionName.COVERAGE_MATRIX, ProjectionFormat.TSV, "phase4-coverage-matrix.tsv"),
    )
    for name, output_format, filename in cases:
        assert render_projection(build_projection(report, name), output_format) == (
            GOLDEN / filename
        ).read_text(encoding="utf-8")
    for forbidden in ("/Users/", "/Volumes/", "/tmp/", "/private/var/"):
        assert forbidden not in report_json
