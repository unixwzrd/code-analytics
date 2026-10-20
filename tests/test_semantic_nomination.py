from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.cli import ExitCode, main
from code_analysis.comparisons import ComparisonBuildError
from code_analysis.config import AnalysisConfig, SemanticConfig, load_config_file
from code_analysis.project import discover_context
from code_analysis.providers.registry import inventory_tools
from code_analysis.reports.json_report import render_json
from code_analysis.semantic_models import SemanticNominationDisposition
from code_analysis.semantic_nomination import (
    SemanticProviderUnavailable,
    _candidate_pairs,
    _vectors,
    build_semantic_nomination_report,
    render_semantic_nomination,
)
from code_analysis.service import scan_refactor

FIXTURES = Path(__file__).parent / "fixtures"


def _report():
    return scan_refactor(
        discover_context(
            FIXTURES / "proposal_project",
            load_config_file(FIXTURES / "proposal-config.toml"),
        )
    )


def _model_directory(tmp_path: Path) -> Path:
    model = tmp_path / "local-model"
    model.mkdir()
    (model / "config.json").write_text('{"model_type":"test"}\n', encoding="utf-8")
    (model / "LICENSE").write_text("synthetic test license\n", encoding="utf-8")
    return model


def _mock_vectors(_model: Path, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
    return tuple((1.0, 0.0) for _ in texts)


def test_semantic_nomination_is_local_bounded_and_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _report()
    model = _model_directory(tmp_path)
    monkeypatch.setattr("code_analysis.semantic_nomination._vectors", _mock_vectors)
    monkeypatch.setattr("code_analysis.semantic_nomination._provider_version", lambda: "synthetic")
    first = build_semantic_nomination_report(report, model, max_nominations=2)
    second = build_semantic_nomination_report(report, model, max_nominations=2)
    assert first == second
    assert first.considered_pairs == first.retained_nominations + first.omitted_pairs
    assert first.retained_nominations == 2
    assert first.supported_nominations + first.rejected_nominations == 2
    assert first.omitted_pairs > 0
    assert first.provenance.vector_dimension == 2
    assert first.provenance.license_identity is not None
    assert first.provenance.license_files == ("LICENSE",)
    assert set(first.rejection_example_ids) <= {
        item.nomination_id
        for item in first.nominations
        if item.disposition is SemanticNominationDisposition.REJECTED_DETERMINISTIC_EVIDENCE
    }
    assert {item.disposition for item in first.nominations} <= set(SemanticNominationDisposition)
    rendered = render_semantic_nomination(first, compact=True)
    assert str(model) not in rendered
    assert json.loads(rendered)["schema_name"] == "code-analysis-semantic-nomination-report"


def test_semantic_pair_bounds_count_unique_omissions() -> None:
    report = _report()
    all_pairs, omitted = _candidate_pairs(report, 50_000)
    bounded, bounded_omitted = _candidate_pairs(report, 1)
    assert omitted == 0
    assert len(bounded) == 1
    assert bounded_omitted == len(all_pairs) - 1


def test_semantic_configuration_is_host_local_and_excluded_from_scan_identity(
    tmp_path: Path,
) -> None:
    model = _model_directory(tmp_path)
    configured = AnalysisConfig(
        semantic=SemanticConfig(enabled=True, model_directory=str(model), max_nominations=17)
    )
    assert configured.identity() == AnalysisConfig().identity()
    assert configured.phase_two_identity() == AnalysisConfig().phase_two_identity()
    assert configured.phase_three_identity() == AnalysisConfig().phase_three_identity()
    assert configured.phase_four_identity() == AnalysisConfig().phase_four_identity()
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.code-analysis.semantic]\n"
        "enabled = true\n"
        f'model-directory = "{model}"\n'
        "max-nominations = 17\n",
        encoding="utf-8",
    )
    assert load_config_file(pyproject).semantic == configured.semantic
    with pytest.raises(ValidationError):
        SemanticConfig(enabled=True, model_directory=str(model), max_nominations=True)
    with pytest.raises(ValidationError):
        SemanticConfig(enabled=True, model_directory="relative/model")


def test_semantic_cli_reads_saved_report_and_reports_partiality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(_report()), encoding="utf-8")
    model = _model_directory(tmp_path)
    monkeypatch.setattr("code_analysis.semantic_nomination._vectors", _mock_vectors)
    monkeypatch.setattr("code_analysis.semantic_nomination._provider_version", lambda: "synthetic")
    result = main(
        [
            "semantic-nominate",
            str(report_path),
            "--model-directory",
            str(model),
            "--top",
            "1",
            "--json-compact",
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.ENODATA
    assert json.loads(captured.out)["retained_nominations"] == 1
    assert captured.err == ""


def test_semantic_provider_and_invalid_inputs(tmp_path: Path) -> None:
    providers = {item.provider_id: item for item in inventory_tools(portable=True)}
    assert "sentence-transformers" in providers
    with pytest.raises(ComparisonBuildError, match="existing absolute"):
        build_semantic_nomination_report(_report(), tmp_path / "missing")
    with pytest.raises(ComparisonBuildError, match="between 1 and 50000"):
        build_semantic_nomination_report(_report(), tmp_path, max_nominations=True)


def test_missing_optional_model_provider_remains_unavailable(tmp_path, monkeypatch):
    from code_analysis.semantic_nomination import SemanticProviderUnavailable

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(SemanticProviderUnavailable, match="unavailable"):
        _vectors(tmp_path, ("one",))


def test_missing_provider_metadata_cannot_succeed_for_empty_candidates(
    tmp_path, monkeypatch, capsys
):
    import importlib.metadata

    project = tmp_path / "project"
    project.mkdir()
    (project / "empty.py").write_text('"""No candidate functions."""\n')
    report = scan_refactor(discover_context(project, AnalysisConfig()))
    assert report.function_profiles == ()
    model = _model_directory(tmp_path)
    original_version = importlib.metadata.version

    def missing_version(name):
        if name == "sentence-transformers":
            raise importlib.metadata.PackageNotFoundError(name)
        return original_version(name)

    monkeypatch.setattr(importlib.metadata, "version", missing_version)
    with pytest.raises(SemanticProviderUnavailable, match="metadata is unavailable"):
        build_semantic_nomination_report(report, model)
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(report))
    status = main(
        ["semantic-nominate", str(report_path), "--model-directory", str(model), "--json-compact"]
    )
    captured = capsys.readouterr()
    assert status == ExitCode.ENODATA
    assert captured.out == ""
    assert "metadata is unavailable" in captured.err


@pytest.mark.parametrize("factory", [None, lambda *args, **kwargs: object()])
def test_optional_model_provider_rejects_malformed_capabilities(tmp_path, monkeypatch, factory):
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=factory)
    )
    with pytest.raises(ComparisonBuildError, match="semantic provider"):
        _vectors(tmp_path, ("one",))


def test_semantic_loader_is_forced_local_and_disables_remote_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    class Vector:
        def tolist(self):
            return [1.0, 0.0]

    class Model:
        def __init__(self, path: str, **kwargs) -> None:
            calls.append((path, kwargs))

        def encode(self, texts, **kwargs):
            calls.append((tuple(texts), kwargs))
            return [Vector() for _ in texts]

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=Model)
    )
    model = _model_directory(tmp_path)

    assert _vectors(model, ("one", "two")) == ((1.0, 0.0), (1.0, 0.0))
    assert calls[0] == (
        str(model),
        {"local_files_only": True, "trust_remote_code": False},
    )


def test_semantic_cli_reports_unavailable_provider_as_enodata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(render_json(_report()), encoding="utf-8")
    model = _model_directory(tmp_path)

    def unavailable(_model: Path, _texts: tuple[str, ...]):
        raise SemanticProviderUnavailable("synthetic unavailable provider")

    monkeypatch.setattr("code_analysis.semantic_nomination._vectors", unavailable)
    result = main(
        [
            "semantic-nominate",
            str(report_path),
            "--model-directory",
            str(model),
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.ENODATA
    assert captured.out == ""
    assert "semantic provider unavailable" in captured.err
