from __future__ import annotations

import errno
import json
import sys
from pathlib import Path

import pytest
from conftest import FIXTURES

import code_analysis.cli as cli
from code_analysis.cli import (
    ExitCode,
    _report_exit,
    _resolve_ui_report,
    build_parser,
    main,
)
from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness, ProviderResult, ProviderState
from code_analysis.project import discover_context
from code_analysis.reports.json_report import canonical_json, load_json, render_json
from code_analysis.workspace import GenerationKind, commit_generation, prepare_workspace


def test_exit_codes_use_host_errno_symbols() -> None:
    assert ExitCode.SUCCESS == 0
    assert ExitCode.EPERM == errno.EPERM
    assert ExitCode.EINVAL == errno.EINVAL
    assert ExitCode.ENODATA == errno.ENODATA
    assert ExitCode.EBADMSG == errno.EBADMSG
    assert ExitCode.EPROTO == errno.EPROTO
    assert ExitCode.EFAULT == errno.EFAULT


def test_argument_parser_uses_einval_for_usage_error(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["scan", "--not-a-real-option"])
    assert exc_info.value.code == ExitCode.EINVAL
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unrecognized arguments" in captured.err


def test_bare_command_routes_to_ui(monkeypatch) -> None:
    captured = {}

    def fake_ui(args):
        captured["args"] = args
        return ExitCode.SUCCESS

    monkeypatch.setattr(cli, "_ui", fake_ui)
    assert main([]) == ExitCode.SUCCESS
    assert captured["args"].command == "ui"
    assert captured["args"].report is None


def test_ui_explicit_missing_report_fails_closed(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "missing-report.json"
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        _resolve_ui_report(missing)
    captured = []

    def fake_web(args):
        captured.append(args)
        return ExitCode.SUCCESS

    monkeypatch.setattr(cli, "_web", fake_web)
    assert main(["ui", str(missing), "--no-open"]) == errno.ENOENT
    assert captured == []


def test_ui_empty_workspace_defaults_to_collection_planning(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "sample.py").write_text("value = 1\n", encoding="utf-8")
    prepare_workspace(discover_context(project, AnalysisConfig()))
    captured = []

    def fake_web(args):
        captured.append(args)
        return ExitCode.SUCCESS

    monkeypatch.chdir(project)
    monkeypatch.setattr(cli, "_web", fake_web)
    assert main(["ui", "--no-open"]) == ExitCode.SUCCESS
    assert captured[0].report is None
    assert captured[0].collection_root == project.resolve()
    assert captured[0].allow_collection is True


def test_ui_no_collection_is_read_only_override(tmp_path: Path, monkeypatch) -> None:
    report = tmp_path / "report.json"
    report.write_text("{}\n", encoding="utf-8")
    captured = []

    def fake_web(args):
        captured.append(args)
        return ExitCode.SUCCESS

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_web", fake_web)
    assert main(["ui", str(report), "--no-collection", "--no-open"]) == ExitCode.SUCCESS
    assert captured[0].allow_collection is False
    assert captured[0].collection_root == tmp_path.resolve()


def test_ui_saved_report_defaults_collection_enabled(
    ordinary_report, tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "package.py"
    source.write_text("value = 1\n", encoding="utf-8")
    context = discover_context(tmp_path, AnalysisConfig())
    workspace = prepare_workspace(context)
    commit_generation(
        workspace,
        kind=GenerationKind.ANALYSIS,
        artifacts={"report.json": canonical_json(ordinary_report).encode()},
        command=("code-analysis", "scan", "."),
        interpreter_path=sys.executable,
        portable_identity_sha256=ordinary_report.run_identity_sha256,
    )
    captured = []

    def fake_web(args):
        captured.append(args)
        return ExitCode.SUCCESS

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_web", fake_web)
    assert main(["ui", "--no-open"]) == ExitCode.SUCCESS
    assert captured[0].report is not None
    assert captured[0].collection_root == tmp_path.resolve()
    assert captured[0].allow_collection is True


def test_bare_command_defaults_collection_enabled(tmp_path: Path, monkeypatch) -> None:
    captured = []

    def fake_web(args):
        captured.append(args)
        return ExitCode.SUCCESS

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_web", fake_web)
    assert main([]) == ExitCode.SUCCESS
    assert captured[0].report is None
    assert captured[0].collection_root == tmp_path.resolve()
    assert captured[0].allow_collection is True


def test_ui_collection_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["ui", "--allow-collection", "--no-collection"])


@pytest.mark.parametrize("explicit_root", [False, True])
def test_ui_damaged_workspace_and_generation_are_not_hidden(
    ordinary_report, tmp_path: Path, monkeypatch, explicit_root
) -> None:
    captured = []

    def fake_web(args):
        captured.append(args)
        return ExitCode.SUCCESS

    monkeypatch.setattr(cli, "_web", fake_web)
    damaged_root = tmp_path / "damaged"
    damaged_root.mkdir()
    workspace_root = damaged_root / ".code-analysis"
    workspace_root.mkdir()
    monkeypatch.chdir(damaged_root)
    argv = ["ui", "--no-open"]
    if explicit_root:
        argv.extend(["--collection-root", str(tmp_path)])
    assert main(argv) != ExitCode.SUCCESS
    assert captured == []
    (workspace_root / "workspace.json").write_text("{", encoding="utf-8")
    monkeypatch.chdir(damaged_root)
    assert main(argv) != ExitCode.SUCCESS
    assert captured == []

    project = tmp_path / "project"
    project.mkdir()
    (project / "sample.py").write_text("value = 1\n", encoding="utf-8")
    context = discover_context(project, AnalysisConfig())
    workspace = prepare_workspace(context)
    generation = commit_generation(
        workspace,
        kind=GenerationKind.ANALYSIS,
        artifacts={"run.json": b"{}\n"},
        command=("code-analysis", "scan", "."),
        interpreter_path=sys.executable,
        portable_identity_sha256=ordinary_report.run_identity_sha256,
    )
    report_path = (
        workspace.kind_directory(GenerationKind.ANALYSIS) / generation.generation_id / "report.json"
    )
    assert not report_path.is_file()
    monkeypatch.chdir(project)
    captured.clear()
    assert main(argv) == errno.EBADMSG
    assert captured == []


def test_ui_discovers_latest_workspace_report(ordinary_report, tmp_path: Path) -> None:
    source = tmp_path / "package.py"
    source.write_text("value = 1\n", encoding="utf-8")
    context = discover_context(tmp_path, AnalysisConfig())
    workspace = prepare_workspace(context)
    first = commit_generation(
        workspace,
        kind=GenerationKind.ANALYSIS,
        artifacts={"report.json": canonical_json(ordinary_report).encode()},
        command=("code-analysis", "scan", "."),
        interpreter_path=sys.executable,
        portable_identity_sha256=ordinary_report.run_identity_sha256,
    )
    nested = tmp_path / "nested"
    nested.mkdir()

    resolved = _resolve_ui_report(None, cwd=nested)

    assert (
        resolved
        == workspace.kind_directory(GenerationKind.ANALYSIS) / first.generation_id / "report.json"
    )
    assert _resolve_ui_report(resolved.parent) == resolved


def test_ui_opens_by_default_on_macos(ordinary_report, tmp_path: Path, monkeypatch) -> None:
    report = tmp_path / "report.json"
    report.write_text(canonical_json(ordinary_report), encoding="utf-8")
    captured = {}

    def fake_web(args):
        captured["args"] = args
        return ExitCode.SUCCESS

    monkeypatch.setattr(cli, "_web", fake_web)
    monkeypatch.setattr(cli.sys, "platform", "darwin")

    assert main(["ui", str(report)]) == ExitCode.SUCCESS
    assert captured["args"].report == report
    assert captured["args"].open is True

    assert main(["ui", str(report), "--no-open"]) == ExitCode.SUCCESS
    assert captured["args"].open is False


def test_provider_and_internal_exit_statuses_are_distinct(ordinary_report) -> None:
    failed_provider = ProviderResult(
        provider_id="synthetic-timeout",
        state=ProviderState.TIMEOUT,
        duration_ms=1,
        completeness=Completeness.FAILED,
    )
    failed_report = ordinary_report.model_copy(update={"providers": (failed_provider,)})
    assert _report_exit(failed_report, fail_on_findings=False) == ExitCode.EPROTO
    assert _report_exit(object(), fail_on_findings=False) == ExitCode.EFAULT


def test_scan_json_keeps_diagnostics_on_stderr(capsys) -> None:
    result = main(["scan", str(FIXTURES / "malformed"), "--json", "--no-workspace"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == ExitCode.ENODATA
    assert payload["completeness"] == "partial"
    assert "source-syntax-error" not in captured.out.splitlines()[0]
    assert "source-syntax-error" in captured.err


def test_complete_scan_exit_and_fail_on_findings(capsys) -> None:
    result = main(["scan", str(FIXTURES / "ordinary"), "--json-compact", "--no-workspace"])
    capsys.readouterr()
    assert result == ExitCode.SUCCESS
    result = main(
        [
            "scan",
            str(FIXTURES / "ordinary"),
            "--json-compact",
            "--fail-on-findings",
            "--no-workspace",
        ]
    )
    capsys.readouterr()
    assert result == ExitCode.EPERM


def test_contracts_cli_emits_authoritative_json_without_workspace(capsys) -> None:
    result = main(
        [
            "contracts",
            str(FIXTURES / "contracts_project"),
            "--json",
            "--no-workspace",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == ExitCode.SUCCESS
    assert captured.err == ""
    assert payload["schema_version"] == "1.0.0"
    assert payload["summary"]["contracts"] == 9
    assert (
        next(item for item in payload["section_coverage"] if item["section"] == "contracts")[
            "state"
        ]
        == "complete"
    )
    assert "/Users/" not in captured.out
    assert not (FIXTURES / "contracts_project" / ".code-analysis").exists()


def test_focused_commands_accept_python_and_reject_javascript_language(
    capsys, tmp_path: Path
) -> None:
    source = tmp_path / "sample.py"
    source.write_text("def value():\n    return {}\n", encoding="utf-8")

    assert (
        main(
            [
                "contracts",
                str(source),
                "--language",
                "python",
                "--no-workspace",
                "--quiet",
            ]
        )
        == ExitCode.SUCCESS
    )
    assert main(
        [
            "dict-shapes",
            str(source),
            "--language",
            "auto",
            "--no-workspace",
            "--quiet",
        ]
    ) in {ExitCode.SUCCESS, ExitCode.ENODATA}
    assert (
        main(
            [
                "contracts",
                str(source),
                "--language",
                "javascript",
                "--no-workspace",
            ]
        )
        == ExitCode.EINVAL
    )
    captured = capsys.readouterr()
    assert "support only auto or python" in captured.err


def test_contracts_cli_markdown_and_partial_exit_preserve_streams(capsys) -> None:
    assert (
        main(
            [
                "contracts",
                str(FIXTURES / "contracts_project"),
                "--markdown",
                "--no-workspace",
            ]
        )
        == ExitCode.SUCCESS
    )
    captured = capsys.readouterr()
    assert "## Contract inventory" in captured.out
    assert "pydantic-model" in captured.out
    assert captured.err == ""

    assert (
        main(
            [
                "contracts",
                str(FIXTURES / "malformed"),
                "--json-compact",
                "--no-workspace",
            ]
        )
        == ExitCode.ENODATA
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["completeness"] == "partial"
    assert "source-syntax-error" in captured.err


def test_dictionary_cli_emits_authoritative_partial_evidence_without_workspace(
    tmp_path: Path, capsys
) -> None:
    result = main(
        [
            "dict-shapes",
            str(FIXTURES / "dictionary_project"),
            "--json",
            "--no-workspace",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == ExitCode.ENODATA
    assert captured.err == ""
    assert payload["command"]["name"] == "dict-shapes"
    assert payload["summary"]["dictionary_shapes"] == 3
    assert payload["summary"]["flow_edges"] >= 2
    assert payload["summary"]["unresolved_edges"] == 1
    sections = {item["section"]: item for item in payload["section_coverage"]}
    assert sections["dictionary-shapes"]["state"] == "partial"
    assert "/Users/" not in captured.out
    assert not (FIXTURES / "dictionary_project" / ".code-analysis").exists()

    report_path = tmp_path / "dictionary-report.json"
    report_path.write_text(captured.out, encoding="utf-8")
    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "shape-variants",
                "--format",
                "tsv",
            ]
        )
        == ExitCode.ENODATA
    )
    projected = capsys.readouterr()
    assert projected.err == ""
    assert projected.out.startswith("owner\tshape-id\tvariant")

    result = main(
        [
            "dict-shapes",
            str(FIXTURES / "dictionary_project"),
            "--markdown",
            "--no-workspace",
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.ENODATA
    assert "## Dictionary shapes" in captured.out
    assert "package.producer:build:payload" in captured.out


def test_contracts_cli_rejects_duplicate_logical_contract_identity(tmp_path: Path, capsys) -> None:
    project = tmp_path / "duplicate-contract"
    project.mkdir()
    (project / "models.py").write_text(
        "from dataclasses import dataclass\n"
        "@dataclass\nclass Item:\n    first: str\n"
        "@dataclass\nclass Item:\n    second: int\n",
        encoding="utf-8",
    )
    assert main(["contracts", str(project), "--json", "--no-workspace"]) == ExitCode.EBADMSG
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "subject_id records must be unique" in captured.err


def test_missing_target_preserves_enoent_and_uses_stderr(capsys) -> None:
    result = main(["scan", str(FIXTURES / "does-not-exist"), "--json"])
    captured = capsys.readouterr()
    assert result == errno.ENOENT
    assert captured.out == ""
    assert "I/O error" in captured.err
    assert "does not exist" in captured.err


def test_invalid_file_target_uses_einval(tmp_path: Path, capsys) -> None:
    target = tmp_path / "not-python.txt"
    target.write_text("text", encoding="utf-8")
    result = main(["scan", str(target), "--json"])
    captured = capsys.readouterr()
    assert result == ExitCode.EINVAL
    assert captured.out == ""
    assert "invalid argument" in captured.err


def test_missing_summary_report_preserves_enoent(tmp_path: Path, capsys) -> None:
    result = main(["summary", str(tmp_path / "missing.json")])
    captured = capsys.readouterr()
    assert result == errno.ENOENT
    assert captured.out == ""
    assert "I/O error" in captured.err


def test_doctor_reports_exact_active_interpreter(capsys) -> None:
    assert main(["doctor", "--json"]) == ExitCode.SUCCESS
    document = json.loads(capsys.readouterr().out)
    assert document["python"]["path"] == sys.executable
    assert document["python"]["version"] == "3.12.13"


def test_providers_json_exposes_unavailable_state(capsys) -> None:
    assert main(["providers", "--json"]) == ExitCode.SUCCESS
    providers = {item["provider_id"]: item for item in json.loads(capsys.readouterr().out)}
    assert providers["libcst"]["state"] == "unavailable"
    assert providers["import-linter"]["state"] == "unavailable"


def test_summary_reads_saved_report(tmp_path: Path, capsys) -> None:
    report_path = tmp_path / "report.json"
    assert (
        main(
            [
                "scan",
                str(FIXTURES / "excluded"),
                "--json",
                "-o",
                str(report_path),
                "--no-workspace",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["summary", str(report_path)]) == 0
    assert capsys.readouterr().out.startswith("# Code Analysis Summary")


def test_export_reads_saved_report_without_workspace_or_rescan(
    ordinary_report, tmp_path: Path, capsys
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(ordinary_report), encoding="utf-8")
    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "modules",
                "--format",
                "tsv",
            ]
        )
        == ExitCode.SUCCESS
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("module\tpath\tline-count")
    assert not (tmp_path / ".code-analysis").exists()


def test_export_validation_divergence_reads_saved_report_only(tmp_path: Path, capsys) -> None:
    assert (
        main(
            [
                "scan",
                str(FIXTURES / "validation_project"),
                "--json",
                "--no-workspace",
            ]
        )
        == ExitCode.SUCCESS
    )
    report_path = tmp_path / "validation.json"
    report_path.write_text(capsys.readouterr().out, encoding="utf-8")

    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "validation-divergence",
                "--format",
                "tsv",
            ]
        )
        == ExitCode.SUCCESS
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("validation-id\tclassification\trule")
    assert captured.out.count("\ttrue\tfalse\t") == 2
    assert not (tmp_path / ".code-analysis").exists()


def test_export_structure_views_read_saved_report_only(tmp_path: Path, capsys) -> None:
    assert (
        main(
            [
                "scan",
                str(FIXTURES / "structure_project"),
                "--json",
                "--no-workspace",
            ]
        )
        == ExitCode.SUCCESS
    )
    report_path = tmp_path / "structures.json"
    report_path.write_text(capsys.readouterr().out, encoding="utf-8")

    for projection, output_format, prefix in (
        ("structure-lifecycle", "tsv", "structure-id\towner\tscope"),
        ("ownership-alias-flow", "mermaid", "flowchart LR"),
        ("conversion-churn", "tsv", "structure-id\towner\tflow-id"),
        (
            "representation-candidates",
            "tsv",
            "candidate-id\tsubject\tclassification",
        ),
        ("review-packet", "markdown", "# Bounded refactoring evidence review"),
    ):
        assert (
            main(
                [
                    "export",
                    str(report_path),
                    "--projection",
                    projection,
                    "--format",
                    output_format,
                ]
            )
            == ExitCode.SUCCESS
        )
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out.startswith(prefix)
    assert not (tmp_path / ".code-analysis").exists()


def test_query_cli_reads_saved_report_and_preserves_streams(tmp_path: Path, capsys) -> None:
    assert (
        main(
            [
                "scan",
                str(FIXTURES / "ordinary"),
                "--json",
                "--no-workspace",
            ]
        )
        == ExitCode.SUCCESS
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(capsys.readouterr().out, encoding="utf-8")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    symbol = next(
        f'{module["name"]}:{symbol["qualified_name"]}'
        for module in payload["modules"]
        for symbol in module["symbols"]
    )

    assert main(["query", str(report_path), "--symbol", symbol, "--json-compact"]) == 0
    captured = capsys.readouterr()
    response = json.loads(captured.out)
    assert captured.err == ""
    assert response["selector"]["kind"] == "symbol"
    assert response["returned_count"] > 0
    assert not (tmp_path / ".code-analysis").exists()


def test_query_cli_rejects_missing_identity_on_stderr(
    ordinary_report, tmp_path: Path, capsys
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(ordinary_report), encoding="utf-8")
    result = main(
        [
            "query",
            str(report_path),
            "--contract",
            "contract-00000000000000000000",
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.EINVAL
    assert captured.out == ""
    assert "unknown contract identity" in captured.err


def test_diff_cli_compares_saved_reports_without_workspace(tmp_path: Path, capsys) -> None:
    assert (
        main(
            [
                "scan",
                str(FIXTURES / "structure_project"),
                "--json",
                "--no-workspace",
            ]
        )
        == ExitCode.SUCCESS
    )
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    saved = capsys.readouterr().out
    baseline.write_text(saved, encoding="utf-8")
    candidate.write_text(saved, encoding="utf-8")

    assert (
        main(
            [
                "diff",
                str(baseline),
                str(candidate),
                "--format",
                "tsv",
            ]
        )
        == ExitCode.SUCCESS
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("subject-id\tchange\tbaseline-sha256")
    assert captured.out.count("\n") == 1
    assert not (tmp_path / ".code-analysis").exists()


def test_diff_cli_defaults_to_changes_only_and_reports_visible_truncation(
    ordinary_report, tmp_path: Path, capsys
) -> None:
    finding = ordinary_report.findings[0]
    candidate_report = ordinary_report.model_copy(
        update={
            "findings": (
                finding.model_copy(update={"title": finding.title + " changed"}),
                *ordinary_report.findings[1:],
            )
        }
    )
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(render_json(ordinary_report), encoding="utf-8")
    candidate.write_text(render_json(candidate_report), encoding="utf-8")

    result = main(["diff", str(baseline), str(candidate)])
    captured = capsys.readouterr()
    assert result == ExitCode.SUCCESS
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["includes_unchanged"] is False
    assert payload["change_counts"]["changed"] == 1
    assert payload["change_counts"]["unchanged"] > 0
    assert payload["eligible_record_count"] == 1
    assert len(payload["records"]) == 1

    result = main(
        [
            "diff",
            str(baseline),
            str(candidate),
            "--include-unchanged",
            "--max-records",
            "1",
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.ENODATA
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["includes_unchanged"] is True
    assert payload["truncated"] is True
    assert payload["eligible_record_count"] > payload["record_count"]


def test_subject_findings_cli_filters_saved_report_only(tmp_path: Path, capsys) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        (Path(__file__).parent / "golden" / "phase3-report.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    result = main(
        [
            "export",
            str(report_path),
            "--projection",
            "subject-findings",
            "--module-prefix",
            "package.combine_a",
            "--category",
            "duplication",
            "--severity",
            "warning",
            "--rule",
            "duplication.normalized-ast",
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.SUCCESS
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["selection"]["module_prefixes"] == ["package.combine_a"]
    assert payload["selection"]["finding_categories"] == ["duplication"]
    assert payload["selection"]["finding_severities"] == ["warning"]
    assert payload["selection"]["finding_rule_ids"] == ["duplication.normalized-ast"]
    assert payload["rows"]

    result = main(
        [
            "export",
            str(report_path),
            "--projection",
            "findings",
            "--severity",
            "warning",
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.EINVAL
    assert captured.out == ""
    assert "finding filters require the subject-findings projection" in captured.err

    result = main(
        [
            "export",
            str(report_path),
            "--projection",
            "subject-findings",
            "--module-prefix",
            "package.*",
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.EINVAL
    assert captured.out == ""
    assert "dotted module names" in captured.err


def test_export_contract_boundaries_requires_and_uses_saved_evidence(
    ordinary_report, tmp_path: Path, capsys
) -> None:
    assert (
        main(
            [
                "contracts",
                str(FIXTURES / "contracts_project"),
                "--json",
                "--no-workspace",
            ]
        )
        == ExitCode.SUCCESS
    )
    report_path = tmp_path / "contracts.json"
    report_path.write_text(capsys.readouterr().out, encoding="utf-8")
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    subjects = {subject["subject_id"]: subject for subject in report_payload["subjects"]}
    response_contract_id = next(
        contract["contract_id"]
        for contract in report_payload["contracts"]
        if subjects[contract["defining_subject_id"]]["logical_name"] == "package.contracts:Response"
    )
    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "contract-boundaries",
                "--format",
                "tsv",
            ]
        )
        == ExitCode.SUCCESS
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("contract\tcontract-id\tcontract-kind\tboundary-class")
    assert "package.contracts:Response" in captured.out
    assert "/Users/" not in captured.out

    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "contracts",
                "--select",
                response_contract_id,
                "--format",
                "json",
            ]
        )
        == ExitCode.SUCCESS
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    selected = json.loads(captured.out)
    assert selected["record_count"] == 1
    assert selected["selection"]["selected_ids"] == [response_contract_id]

    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "contract-boundaries",
                "--select",
                "contract-missing",
            ]
        )
        == ExitCode.EINVAL
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "selected contract is not present" in captured.err

    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "contracts",
                "--format",
                "markdown",
            ]
        )
        == ExitCode.SUCCESS
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("# Contract inventory\n")
    assert "package.contracts:Response" in captured.out
    assert "json, mapping" in captured.out
    assert "/Users/" not in captured.out

    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "contract-boundary-graph",
                "--format",
                "mermaid",
                "--max-records",
                "20",
            ]
        )
        == ExitCode.ENODATA
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("flowchart LR\n")
    assert "cross-subsystem-internal" in captured.out
    assert "/Users/" not in captured.out

    phase_one_path = tmp_path / "phase-one.json"
    phase_one_path.write_text(render_json(ordinary_report), encoding="utf-8")
    assert (
        main(
            [
                "export",
                str(phase_one_path),
                "--projection",
                "contract-boundaries",
            ]
        )
        == ExitCode.EINVAL
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "requires recorded contract evidence" in captured.err


def test_export_graph_and_invalid_format_exit_behavior(
    ordinary_report, tmp_path: Path, capsys
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(ordinary_report), encoding="utf-8")
    output = tmp_path / "dependencies.mmd"
    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "dependencies",
                "--format",
                "mermaid",
                "-o",
                str(output),
            ]
        )
        == ExitCode.SUCCESS
    )
    assert capsys.readouterr().out == ""
    assert output.read_text(encoding="utf-8").startswith("flowchart LR\n")
    hierarchy_output = tmp_path / "hierarchy.dot"
    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "hierarchy",
                "--format",
                "dot",
                "-o",
                str(hierarchy_output),
            ]
        )
        == ExitCode.SUCCESS
    )
    assert capsys.readouterr().out == ""
    assert hierarchy_output.read_text(encoding="utf-8").startswith("digraph projection {\n")
    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "dependencies",
                "--format",
                "tsv",
            ]
        )
        == ExitCode.EINVAL
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "TSV requires a table, matrix, or comparison projection" in captured.err


def test_phase_three_commands_and_saved_report_queries(tmp_path: Path, capsys) -> None:
    project = FIXTURES / "proposal_project"
    proposal_config = FIXTURES / "proposal-config.toml"
    assert (
        main(
            [
                "clones",
                str(FIXTURES / "clone_project"),
                "--strictness",
                "strict",
                "--json",
                "--no-workspace",
            ]
        )
        == ExitCode.SUCCESS
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    clone_report = load_json(captured.out)
    assert clone_report.command.name == "clones"
    assert clone_report.clone_clusters

    assert (
        main(
            [
                "refactor",
                str(project),
                "--config",
                str(proposal_config),
                "--json",
                "--no-workspace",
            ]
        )
        == ExitCode.SUCCESS
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    refactor_report = load_json(captured.out)
    assert refactor_report.responsibility_clusters
    assert refactor_report.refactor_proposals

    assert (
        main(
            [
                "refactor",
                str(project),
                "--config",
                str(proposal_config),
                "--no-workspace",
            ]
        )
        == ExitCode.SUCCESS
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Phase 4" in captured.out

    cases = (
        ("--clone", clone_report.clone_clusters[0].cluster_id, clone_report),
        (
            "--responsibility",
            refactor_report.responsibility_clusters[0].responsibility_id,
            refactor_report,
        ),
        ("--proposal", refactor_report.refactor_proposals[0].proposal_id, refactor_report),
    )
    for index, (selector, identity, report) in enumerate(cases):
        report_path = tmp_path / f"report-{index}.json"
        report_path.write_text(render_json(report), encoding="utf-8")
        assert main(["query", str(report_path), selector, identity, "--json"]) == ExitCode.SUCCESS
        captured = capsys.readouterr()
        assert captured.err == ""
        assert identity in captured.out


def test_phase_three_bound_returns_posix_enodata(tmp_path: Path, capsys) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text(
        "[tool.code-analysis.clone-thresholds]\n"
        "commonality-max-feature-subject-fraction = 1.0\n"
        "\n"
        "[tool.code-analysis.phase-three-limits]\n"
        "max-candidate-pairs = 50000\n"
        "max-clusters = 10000\n"
        "max-cluster-members = 100\n"
        "max-proposals = 1\n",
        encoding="utf-8",
    )
    assert (
        main(
            [
                "refactor",
                str(FIXTURES / "proposal_project"),
                "--config",
                str(config),
                "--json",
                "--no-workspace",
            ]
        )
        == ExitCode.ENODATA
    )
    captured = capsys.readouterr()
    assert '"completeness": "partial"' in captured.out
    assert "refactor-proposal-bound-truncated" in captured.err


def test_export_dependency_neighborhood_selector_contract(
    ordinary_report, tmp_path: Path, capsys
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(ordinary_report), encoding="utf-8")
    output = tmp_path / "neighborhood.mmd"
    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "dependency-neighborhood",
                "--select",
                "alpha.a",
                "--depth",
                "1",
                "--edge-kind",
                "runtime",
                "--format",
                "mermaid",
                "--output",
                str(output),
            ]
        )
        == ExitCode.SUCCESS
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert output.read_text(encoding="utf-8").startswith("flowchart LR\n")

    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "dependency-neighborhood",
            ]
        )
        == ExitCode.EINVAL
    )
    assert "requires exactly one selected module" in capsys.readouterr().err

    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "modules",
                "--select",
                "alpha.a",
            ]
        )
        == ExitCode.EINVAL
    )
    assert "require dependency-neighborhood" in capsys.readouterr().err

    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "dependency-neighborhood",
                "--select",
                "missing",
            ]
        )
        == ExitCode.EINVAL
    )
    assert "not present in the report" in capsys.readouterr().err


def test_export_rejects_invalid_limit_with_einval(ordinary_report, tmp_path: Path, capsys) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(ordinary_report), encoding="utf-8")
    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "modules",
                "--max-records",
                "0",
            ]
        )
        == ExitCode.EINVAL
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--max-records" in captured.err
    assert (
        main(
            [
                "export",
                str(report_path),
                "--projection",
                "dependency-neighborhood",
                "--select",
                "alpha.a",
                "--depth",
                "101",
            ]
        )
        == ExitCode.EINVAL
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--depth" in captured.err


def test_export_preserves_partial_and_malformed_report_statuses(
    ordinary_report, tmp_path: Path, capsys
) -> None:
    partial_path = tmp_path / "partial.json"
    partial_path.write_text(
        render_json(ordinary_report.model_copy(update={"completeness": Completeness.PARTIAL})),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "export",
                str(partial_path),
                "--projection",
                "health",
                "--format",
                "json",
            ]
        )
        == ExitCode.ENODATA
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["completeness"] == "partial"

    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_text("{}\n", encoding="utf-8")
    assert (
        main(
            [
                "export",
                str(malformed_path),
                "--projection",
                "health",
            ]
        )
        == ExitCode.EBADMSG
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "configuration error" in captured.err


def test_function_analysis_predecessor_flags_are_accepted(capsys) -> None:
    result = main(
        [
            "scan",
            str(FIXTURES / "ordinary"),
            "--summary",
            "--only-refactor-targets",
            "--only-local-imports",
            "--max-items",
            "1",
            "--god-loc",
            "20",
            "--god-branches",
            "2",
            "--god-nesting",
            "2",
            "--god-args",
            "2",
            "--god-score",
            "10",
            "--no-workspace",
        ]
    )
    capsys.readouterr()
    assert result == ExitCode.SUCCESS


def test_explicit_config_file_controls_thresholds(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "analysis.toml"
    config_path.write_text(
        '[tool.code-analysis]\nstrictness = "strict"\n'
        "[tool.code-analysis.thresholds]\nbranches = 0\n",
        encoding="utf-8",
    )
    result = main(
        [
            "scan",
            str(FIXTURES / "excluded"),
            "--config",
            str(config_path),
            "--json",
            "--no-workspace",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert result == ExitCode.SUCCESS
    assert payload["configuration"]["strictness"] == "strict"
    assert payload["configuration"]["thresholds"]["branches"] == 0


def test_scan_creates_and_reuses_project_workspace(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "sample.py").write_text("value = 1\n", encoding="utf-8")
    assert main(["scan", str(project), "--quiet"]) == ExitCode.SUCCESS
    captured = capsys.readouterr()
    assert captured.out == ""
    workspace = project / ".code-analysis"
    latest = workspace / "generations" / "analysis" / "latest"
    assert latest.is_symlink()
    report = (latest / "report.json").read_text(encoding="utf-8")
    run = json.loads((latest / "run.json").read_text(encoding="utf-8"))
    assert '"schema_name":"code-analysis-report"' in report
    assert str(tmp_path) not in report
    assert run["project_root"] == str(project)
    assert run["workspace_path"] == str(workspace)
    assert run["interpreter_path"] == sys.executable
    assert main(["scan", str(project), "--quiet"]) == ExitCode.SUCCESS
    capsys.readouterr()


def test_no_workspace_disables_project_local_writes(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "sample.py").write_text("value = 1\n", encoding="utf-8")
    assert main(["scan", str(project), "--quiet", "--no-workspace"]) == ExitCode.SUCCESS
    capsys.readouterr()
    assert not (project / ".code-analysis").exists()


def test_explicit_workspace_does_not_write_target(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "sample.py").write_text("value = 1\n", encoding="utf-8")
    external = tmp_path / "analysis-state"
    assert main(["scan", str(project), "--quiet", "--workspace", str(external)]) == ExitCode.SUCCESS
    capsys.readouterr()
    assert (external / "generations" / "analysis" / "latest" / "report.json").is_file()
    assert not (project / ".code-analysis").exists()


def test_unowned_workspace_fails_before_analysis(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "sample.py").write_text("value = 1\n", encoding="utf-8")
    workspace = project / ".code-analysis"
    workspace.mkdir()
    sentinel = workspace / "private.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    assert main(["scan", str(project), "--json"]) == errno.EBADMSG
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "workspace marker is missing" in captured.err
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_workspace_commands_and_generation_history(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    assert main(["scan", str(project), "--quiet"]) == ExitCode.SUCCESS
    source.write_text("value = 2\n", encoding="utf-8")
    assert main(["scan", str(project), "--quiet"]) == ExitCode.SUCCESS
    capsys.readouterr()

    assert main(["workspace", "list", str(project), "--json"]) == ExitCode.SUCCESS
    generations = json.loads(capsys.readouterr().out)
    assert len(generations) == 2
    generation_id = generations[0]["generation_id"]
    assert main(["workspace", "pin", str(project), generation_id]) == ExitCode.SUCCESS
    assert main(["workspace", "list", str(project), "--json"]) == ExitCode.SUCCESS
    assert any(item["pinned"] for item in json.loads(capsys.readouterr().out))
    assert main(["workspace", "prune", str(project), "--dry-run"]) == ExitCode.SUCCESS
    capsys.readouterr()
    assert main(["workspace", "unpin", str(project), generation_id]) == ExitCode.SUCCESS

    assert main(["history", str(project), "--generations"]) == ExitCode.SUCCESS
    history = json.loads(capsys.readouterr().out)
    assert history["schema_name"] == "code-analysis-change-report"
    assert len(history["generation_changes"]) == 1
    assert str(tmp_path) not in json.dumps(history)


def test_project_configuration_can_disable_workspace(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "sample.py").write_text("value = 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        "[tool.code-analysis.workspace]\nenabled = false\n",
        encoding="utf-8",
    )
    assert main(["scan", str(project), "--quiet"]) == ExitCode.SUCCESS
    capsys.readouterr()
    assert not (project / ".code-analysis").exists()


def test_cli_workspace_overrides_disabled_configuration(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "sample.py").write_text("value = 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        "[tool.code-analysis.workspace]\nenabled = false\n",
        encoding="utf-8",
    )
    external = tmp_path / "external"
    assert main(["scan", str(project), "--quiet", "--workspace", str(external)]) == ExitCode.SUCCESS
    capsys.readouterr()
    assert (external / "workspace.json").is_file()
