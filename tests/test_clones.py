from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.cli import main
from code_analysis.config import AnalysisConfig, CloneThresholds, PhaseThreeLimits
from code_analysis.models import Completeness, ProviderState
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.report_models import (
    AnalysisReport,
    CloneKind,
    CommonalityAdmissionFunnel,
    SectionState,
)
from code_analysis.reports.json_report import canonical_json, load_json
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.service import scan


def _clone_config(*, max_candidate_pairs: int = 50_000) -> AnalysisConfig:
    return AnalysisConfig(
        clone_thresholds=CloneThresholds(
            exact_min_lines=3,
            token_min_tokens=5,
            ast_min_nodes=5,
            statement_window=2,
            sequence_min_events=2,
            sequence_min_coverage=0.5,
            commonality_min_ownership_dimensions=2,
            commonality_min_bilateral_coverage=0.25,
            commonality_max_feature_subject_fraction=0.10,
        ),
        phase_three_limits=PhaseThreeLimits(max_candidate_pairs=max_candidate_pairs),
    )


def test_clone_pipeline_is_deterministic_and_structurally_transparent() -> None:
    context = discover_context(FIXTURES / "clone_project", _clone_config())
    first = scan(context)
    second = scan(context)
    assert first == second
    assert first.completeness is Completeness.COMPLETE
    assert next(item for item in first.section_coverage if item.section == "clones").state is (
        SectionState.COMPLETE
    )
    kinds = {item.kind for item in first.clone_clusters}
    assert CloneKind.EXACT_TEXT in kinds
    assert CloneKind.NORMALIZED_AST in kinds
    exact = next(item for item in first.clone_clusters if item.kind is CloneKind.EXACT_TEXT)
    logical_names = {
        subject.logical_name
        for subject in first.subjects
        if subject.subject_id in exact.subject_ids
    }
    assert logical_names == {"package.first:alpha", "package.second:beta"}
    assert all("docstring" not in item.evidence[0].observation for item in first.clone_fragments)
    assert any(item.category == "duplication" for item in first.findings)
    assert next(item for item in first.providers if item.provider_id == "native-clones").state is (
        ProviderState.SUCCESS
    )
    subjects = {item.subject_id: item for item in first.subjects}
    assert all(":" in subjects[item.subject_id].logical_name for item in first.clone_fragments)


def test_clone_window_bound_suppresses_broad_body_fingerprints() -> None:
    config = _clone_config().model_copy(
        update={
            "clone_thresholds": _clone_config().clone_thresholds.model_copy(
                update={"max_clone_window_statements": 4}
            )
        }
    )
    report = scan(discover_context(FIXTURES / "clone_project", config))
    broad = {
        CloneKind.EXACT_TEXT,
        CloneKind.NORMALIZED_TOKEN,
        CloneKind.NORMALIZED_AST,
    }
    assert all(
        item.kind not in broad or item.statement_count <= 4 for item in report.clone_fragments
    )


def test_candidate_pair_bound_is_partial_not_empty_success() -> None:
    report = scan(
        discover_context(
            FIXTURES / "clone_project",
            _clone_config(max_candidate_pairs=1),
        )
    )
    assert report.completeness is Completeness.PARTIAL
    coverage = next(item for item in report.section_coverage if item.section == "clones")
    assert coverage.state is SectionState.PARTIAL
    assert coverage.truncated > 0
    assert any(item.code == "clone-candidate-bound-truncated" for item in report.diagnostics)
    assert next(item for item in report.providers if item.provider_id == "native-clones").state is (
        ProviderState.PARTIAL
    )


def test_commonality_records_each_dimension_and_ignores_weak_name_similarity() -> None:
    report = scan(discover_context(FIXTURES / "clone_project", _clone_config()))
    subjects = {item.subject_id: item.logical_name for item in report.subjects}
    alpha_pair = next(
        item
        for item in report.function_commonalities
        if {subjects[item.left_subject_id], subjects[item.right_subject_id]}
        == {"package.first:alpha", "package.second:beta"}
    )
    assert len(alpha_pair.dimensions) >= 2
    assert all(item.shared_identities for item in alpha_pair.dimensions)
    assert all(item.left_total > 0 and item.right_total > 0 for item in alpha_pair.dimensions)
    weak_pair = {"package.third:process", "package.fourth:process"}
    assert not any(
        {subjects[item.left_subject_id], subjects[item.right_subject_id]} == weak_pair
        for item in report.function_commonalities
    )
    profiles = {subjects[item.subject_id]: item for item in report.function_profiles}
    assert "name:process" in profiles["package.third:process"].weak_semantic_signals
    assert (
        next(
            item for item in report.section_coverage if item.section == "function-commonality"
        ).state
        is SectionState.COMPLETE
    )
    admission = report.signal_quality.commonality_admission
    assert admission is not None
    assert admission.candidate_bound_omissions == 0
    assert admission.candidate_bound_omissions_exact is True
    assert admission.admitted_pairs == len(report.function_commonalities)
    assert (
        admission.admitted_pairs + admission.coverage_rejections + admission.dimension_rejections
        == admission.unique_candidates
    )
    assert admission.postings_considered + admission.clone_admissions >= admission.unique_candidates


def _commonality_bound_report(tmp_path: Path, maximum_pairs: int = 1):
    (tmp_path / "many.py").write_text(
        "def helper(value):\n    return value\n\n"
        + "\n".join(
            f"def f{index}(value):\n    if value == {index}:\n"
            "        raise ValueError('invalid')\n    return helper(value)\n"
            for index in range(4)
        )
    )
    config = _clone_config(max_candidate_pairs=maximum_pairs)
    config = config.model_copy(
        update={
            "clone_thresholds": config.clone_thresholds.model_copy(
                update={"commonality_max_feature_subject_fraction": 1.0}
            )
        }
    )
    return scan(discover_context(tmp_path, config))


def test_commonality_bound_is_a_lower_bound_in_saved_reports_and_views(
    tmp_path: Path, capsys
) -> None:
    bounded = _commonality_bound_report(tmp_path)
    full = _commonality_bound_report(tmp_path, 50_000)
    admission = bounded.signal_quality.commonality_admission
    assert admission is not None
    assert (
        full.signal_quality.commonality_admission.unique_candidates
        > admission.unique_candidates + 1
    )
    assert admission.candidate_bound_omissions == 1
    assert admission.candidate_bound_omissions_exact is False
    warning = next(
        d for d in bounded.diagnostics if d.code == "commonality-candidate-bound-truncated"
    )
    assert "at least 1" in warning.message and "total is unknown" in warning.message
    raw = canonical_json(bounded)
    assert canonical_json(load_json(raw)) == raw
    saved = tmp_path / "report.json"
    saved.write_text(raw)
    (tmp_path / "many.py").unlink()
    # Exports use only the saved report; this directory contains no target source.
    for name in (ProjectionName.COMMONALITY_ADMISSION, ProjectionName.SIGNAL_QUALITY):
        projection = build_projection(load_json(raw), name)
        for format_ in (ProjectionFormat.JSON, ProjectionFormat.TSV, ProjectionFormat.MARKDOWN):
            rendered = render_projection(projection, format_)
            assert "lower bound" in rendered or "lower-bound" in rendered
            assert "total unknown" in rendered
        assert (
            main(["export", str(saved), "--projection", name.value, "--format", "tsv"])
            == errno.ENODATA
        )
        output = capsys.readouterr()
        assert "total unknown" in output.out


@pytest.mark.parametrize(
    "patch",
    [
        {"candidate_bound_omissions_exact": 1},
        {"candidate_bound_omissions_exact": "false"},
        {"candidate_bound_omissions": True},
        {"candidate_bound_omissions_exact": False, "candidate_bound_omissions": 0},
        {"index_dimension_rejections_exact": 1},
        {"index_dimension_rejections": -1},
        {"index_dimension_rejections_exact": False},
    ],
)
def test_commonality_omission_certainty_rejects_invalid_values(patch) -> None:
    values = {
        "profile_count": 0,
        "postings_considered": 0,
        "prevalent_features_suppressed": 0,
        "unique_candidates": 0,
        "index_dimension_rejections": 0,
        "index_dimension_rejections_exact": True,
        "candidate_bound_omissions": 0,
        "candidate_bound_omissions_exact": True,
        "coverage_rejections": 0,
        "dimension_rejections": 0,
        "clone_admissions": 0,
        "admitted_pairs": 0,
    }
    with pytest.raises(ValidationError):
        CommonalityAdmissionFunnel.model_validate({**values, **patch})
    del values["candidate_bound_omissions_exact"]
    with pytest.raises(ValidationError):
        CommonalityAdmissionFunnel.model_validate(values)


def test_commonality_omissions_cannot_claim_complete_section(tmp_path: Path) -> None:
    report = _commonality_bound_report(tmp_path)
    payload = json.loads(report.model_dump_json())
    coverage = next(
        s for s in payload["section_coverage"] if s["section"] == "function-commonality"
    )
    coverage.update(state="complete", truncated=0)
    with pytest.raises(ValidationError, match="commonality omissions"):
        AnalysisReport.model_validate_json(json.dumps(payload))
