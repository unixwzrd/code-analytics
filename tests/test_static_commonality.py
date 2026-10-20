from pathlib import Path

import pytest

from code_analysis.projection_models import MatrixProjection, ScalarKind
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.reports.json_report import canonical_json, load_json
from code_analysis.static_browse import prepared_static_projection


def test_sparse_commonality_preserves_exact_cells_and_missing_dimensions():
    report = load_json(
        (Path(__file__).parent / "golden" / "phase3-report.json").read_text(encoding="utf-8")
    )
    matrix = build_projection(report, ProjectionName.FUNCTION_COMMONALITY, max_records=100_000)
    assert isinstance(matrix, MatrixProjection)
    table = prepared_static_projection(report, ProjectionName.FUNCTION_COMMONALITY)
    assert table.record_count == len(matrix.cells) > 0
    assert table.inputs == matrix.inputs
    assert table.projection_id != matrix.projection_id
    assert table.completeness == matrix.completeness
    assert table.truncated == matrix.truncated
    left = {item.item_id: item.label for item in matrix.rows}
    right = {item.item_id: item.label for item in matrix.columns}
    missing = 0
    for row, cell in zip(table.rows, matrix.cells, strict=True):
        assert row.row_id == cell.cell_id
        assert row.evidence_ids == cell.evidence_ids
        assert tuple(value.string_value for value in row.cells[:4]) == (
            cell.row_id,
            cell.column_id,
            left[cell.row_id],
            right[cell.column_id],
        )
        metrics = {item.name: item.value for item in cell.metrics}
        for column, value in zip(table.columns[4:], row.cells[4:], strict=True):
            if column.column_id in metrics:
                assert value == metrics[column.column_id]
            else:
                assert value.kind is ScalarKind.NULL
                missing += 1
    assert missing
    assert (
        table.model_dump_json()
        == prepared_static_projection(report, ProjectionName.FUNCTION_COMMONALITY).model_dump_json()
    )


def test_empty_commonality_has_stable_named_columns(ordinary_report):
    table = prepared_static_projection(ordinary_report, ProjectionName.FUNCTION_COMMONALITY)
    assert table.record_count == 0
    assert not table.rows
    assert len(table.columns) == 13
    assert table.columns[0].column_id == "left-subject-id"


def test_repeated_commonality_evidence_is_one_matrix_reference():
    report = load_json(
        (Path(__file__).parent / "golden" / "phase3-report.json").read_text(encoding="utf-8")
    )
    expected = build_projection(report, ProjectionName.FUNCTION_COMMONALITY)
    pair = report.function_commonalities[0]
    repeated = pair.model_copy(update={"evidence": (*pair.evidence, pair.evidence[0])})
    report = report.model_copy(
        update={"function_commonalities": (repeated, *report.function_commonalities[1:])}
    )
    report = load_json(canonical_json(report))
    actual = build_projection(report, ProjectionName.FUNCTION_COMMONALITY)
    assert actual.cells == expected.cells
    assert len(report.function_commonalities[0].evidence) == len(pair.evidence) + 1
    table = prepared_static_projection(report, ProjectionName.FUNCTION_COMMONALITY)
    assert table.rows[0].evidence_ids == actual.cells[0].evidence_ids
    assert (
        actual.model_dump_json()
        == build_projection(report, ProjectionName.FUNCTION_COMMONALITY).model_dump_json()
    )


def test_unsupported_static_projection_fails(ordinary_report):
    with pytest.raises(ValueError, match="supported sparse matrix"):
        prepared_static_projection(ordinary_report, ProjectionName.DEPENDENCIES)
