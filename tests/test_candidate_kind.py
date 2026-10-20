from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.cli import ExitCode, build_parser, main
from code_analysis.projection_models import ProjectionSelection
from code_analysis.projections import ProjectionBuildError, ProjectionName, build_projection
from code_analysis.report_models import RefactorCandidateKind
from code_analysis.reports.json_report import load_json
from code_analysis.ui_projections import UIProjectionCatalog, UIProjectionRequest, project_report
from code_analysis.web_server import _projection_request

GOLDEN = Path(__file__).parent / "golden" / "phase3-report.json"


@pytest.fixture(scope="module")
def report():
    return load_json(GOLDEN.read_text(encoding="utf-8"))


def test_candidate_kind_is_a_strict_sorted_projection_selection() -> None:
    selection = ProjectionSelection(
        candidate_kinds=(
            RefactorCandidateKind.MODULE_COMBINATION,
            RefactorCandidateKind.RESPONSIBILITY_EXTRACTION,
        )
    )
    assert selection.model_dump(mode="json")["candidate_kinds"] == [
        "module-combination",
        "responsibility-extraction",
    ]
    with pytest.raises(ValidationError):
        ProjectionSelection(candidate_kinds=("unknown",))
    with pytest.raises(ValidationError, match="unique and sorted"):
        ProjectionSelection(
            candidate_kinds=(
                RefactorCandidateKind.RESPONSIBILITY_EXTRACTION,
                RefactorCandidateKind.MODULE_COMBINATION,
            )
        )
    with pytest.raises(ValidationError, match="unique and sorted"):
        ProjectionSelection(
            candidate_kinds=(
                RefactorCandidateKind.MODULE_COMBINATION,
                RefactorCandidateKind.MODULE_COMBINATION,
            )
        )


def test_candidate_kind_filters_authoritative_worklist_and_identity(report) -> None:
    selected = build_projection(
        report,
        ProjectionName.CANDIDATE_WORKLIST,
        candidate_kinds=(RefactorCandidateKind.MODULE_COMBINATION,),
    )
    unfiltered = build_projection(report, ProjectionName.CANDIDATE_WORKLIST)
    assert selected.projection_id != unfiltered.projection_id
    assert selected.selection.candidate_kinds == (RefactorCandidateKind.MODULE_COMBINATION,)
    assert selected.record_count == 1
    assert selected.rows[0].cells[1].string_value == "module-combination"
    with pytest.raises(ProjectionBuildError, match="candidate-worklist"):
        build_projection(
            report,
            ProjectionName.MODULES,
            candidate_kinds=(RefactorCandidateKind.MODULE_COMBINATION,),
        )
    with pytest.raises(ProjectionBuildError, match="candidate-worklist"):
        build_projection(
            report,
            ProjectionName.SUBJECT_FINDINGS,
            candidate_kinds=(RefactorCandidateKind.MODULE_COMBINATION,),
        )


def test_ui_and_web_candidate_kind_plumbing_is_strict(report) -> None:
    assert UIProjectionCatalog().candidate_kinds == tuple(RefactorCandidateKind)
    request = _projection_request(
        {
            "name": ["candidate-worklist"],
            "candidate_kinds": ["module-combination", "module-combination"],
        }
    )
    assert request.candidate_kinds == (RefactorCandidateKind.MODULE_COMBINATION,)
    assert project_report(report, request).selection.candidate_kinds == request.candidate_kinds
    with pytest.raises(ValidationError):
        UIProjectionRequest(
            name=ProjectionName.CANDIDATE_WORKLIST,
            candidate_kinds=("unknown",),
        )
    with pytest.raises(ValidationError, match="candidate-worklist"):
        UIProjectionRequest(
            name=ProjectionName.MODULES,
            candidate_kinds=(RefactorCandidateKind.MODULE_COMBINATION,),
        )


def test_candidates_and_export_cli_accept_repeatable_kind(tmp_path: Path, capsys) -> None:
    parsed = build_parser().parse_args(
        [
            "candidates",
            str(GOLDEN),
            "--kind",
            "module-combination",
            "--kind",
            "responsibility-extraction",
        ]
    )
    assert parsed.kind == ["module-combination", "responsibility-extraction"]

    result = main(
        [
            "candidates",
            str(GOLDEN),
            "--format",
            "tsv",
            "--kind",
            "module-combination",
        ]
    )
    captured = capsys.readouterr()
    assert result in {ExitCode.SUCCESS, ExitCode.ENODATA}
    assert "module-combination" in captured.out
    assert "responsibility-extraction" not in captured.out
    assert captured.err == ""

    output = tmp_path / "candidate.json"
    result = main(
        [
            "export",
            str(GOLDEN),
            "--projection",
            "candidate-worklist",
            "--kind",
            "responsibility-extraction",
            "--output",
            str(output),
        ]
    )
    assert result in {ExitCode.SUCCESS, ExitCode.ENODATA}
    assert '"candidate_kinds": [\n      "responsibility-extraction"' in output.read_text(
        encoding="utf-8"
    )
