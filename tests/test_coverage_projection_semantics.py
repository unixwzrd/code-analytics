from pathlib import Path

from code_analysis.projections import ProjectionName, build_projection
from code_analysis.reports.json_report import load_json

GOLDEN = Path(__file__).parent / "golden"


def test_missing_coverage_never_proves_proposal_subject_was_not_executed():
    report = load_json((GOLDEN / "phase3-report.json").read_text())
    assert not report.coverage_observations
    assert (
        next(item for item in report.section_coverage if item.section == "coverage").state.value
        == "disabled"
    )
    projection = build_projection(report, ProjectionName.UNCOVERED_PROPOSAL_SUBJECTS)
    assert projection.rows
    assert {row.cells[2].string_value for row in projection.rows} == {"qualification-unresolved"}
    assert all(row.cells[3].integer_value == 0 for row in projection.rows)


def test_context_matrix_discloses_observation_aggregate_not_per_context_counts():
    report = load_json((GOLDEN / "phase4-coverage-report.json").read_text())
    projection = build_projection(report, ProjectionName.COVERAGE_MATRIX)
    assert projection.cells
    assert any("not independently measured per-context" in item for item in projection.limitations)
