from __future__ import annotations

from pathlib import Path

import pytest

from code_analysis.browse_export import export_browse_page, render_browse_artifact
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseIndexManifest
from code_analysis.models import Completeness
from code_analysis.projection_models import MatrixProjection, ScalarKind
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.report_models import CoverageBindingState
from code_analysis.reports.json_report import load_json
from code_analysis.reports.projection_views import ProjectionFormat
from code_analysis.static_browse import (
    StaticProjectionRequest,
    _sparse_matrix_table,
    prepared_static_projection,
    static_page_projection,
)
from code_analysis.web_service import ReportViewService

GOLDEN = Path(__file__).parent / "golden"


def _coverage_report():
    return load_json((GOLDEN / "phase4-coverage-report.json").read_text(encoding="utf-8"))


def _index(report, destination: Path) -> BrowseIndex:
    manifest = build_browse_index(ReportViewService(report), destination)
    return BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def test_coverage_views_are_source_free_and_match_full_builders(tmp_path: Path) -> None:
    report = _coverage_report()
    index = _index(report, tmp_path / "index")
    # The saved report names module.py, but browsing must not require that source or a report file.
    assert not (tmp_path / "module.py").exists()
    assert {item.name for item in index.manifest.static_projections} >= {
        ProjectionName.COVERAGE_FILES,
        ProjectionName.COVERAGE_MATRIX,
    }

    files = index.static_projection(
        StaticProjectionRequest(name=ProjectionName.COVERAGE_FILES, limit=500)
    )
    assert (
        files.records
        == build_projection(report, ProjectionName.COVERAGE_FILES, max_records=100_000).rows
    )
    matrix = index.static_projection(
        StaticProjectionRequest(name=ProjectionName.COVERAGE_MATRIX, limit=500)
    )
    expected_matrix = prepared_static_projection(report, ProjectionName.COVERAGE_MATRIX)
    assert matrix.records == expected_matrix.rows
    assert matrix.summary.source == expected_matrix.inputs[0]
    assert matrix.summary.completeness is expected_matrix.completeness
    assert matrix.summary.limitations == expected_matrix.limitations

    path, exported = export_browse_page(
        index,
        matrix.request,
        "coverage-contexts",
        ProjectionFormat.JSON_COMPACT,
    )
    assert exported == static_page_projection(matrix)
    assert path.read_bytes() == render_browse_artifact(exported, ProjectionFormat.JSON_COMPACT)


def test_uncovered_proposal_subjects_are_prepared_without_source_reads(tmp_path: Path) -> None:
    report = load_json((GOLDEN / "phase3-report.json").read_text(encoding="utf-8"))
    index = _index(report, tmp_path / "index")
    page = index.static_projection(
        StaticProjectionRequest(name=ProjectionName.UNCOVERED_PROPOSAL_SUBJECTS, limit=500)
    )
    expected = build_projection(
        report, ProjectionName.UNCOVERED_PROPOSAL_SUBJECTS, max_records=100_000
    )
    assert page.records == expected.rows
    assert page.summary.records == expected.record_count
    assert page.records
    assert {row.cells[2].string_value for row in page.records} == {"qualification-unresolved"}


def test_manifest_binds_optional_coverage_presence_counts_and_order(tmp_path: Path) -> None:
    index = _index(_coverage_report(), tmp_path / "index")
    receipt = index.manifest.model_dump()
    assert receipt["coverage_files_count"] == 1
    assert receipt["coverage_cells_count"] == 1
    assert receipt["proposal_coverage_subject_count"] == 0

    omitted = {
        **receipt,
        "static_projections": tuple(
            item
            for item in receipt["static_projections"]
            if item["name"] != ProjectionName.COVERAGE_FILES
        ),
    }
    with pytest.raises(ValueError, match="coverage-files availability"):
        BrowseIndexManifest.model_validate(omitted)

    contradicted = {**receipt, "coverage_cells_count": 2}
    with pytest.raises(ValueError, match="coverage-matrix count"):
        BrowseIndexManifest.model_validate(contradicted)

    reordered = list(receipt["static_projections"])
    first = next(
        index
        for index, item in enumerate(reordered)
        if item["name"] == ProjectionName.COVERAGE_FILES
    )
    second = next(
        index
        for index, item in enumerate(reordered)
        if item["name"] == ProjectionName.COVERAGE_MATRIX
    )
    reordered[first], reordered[second] = reordered[second], reordered[first]
    with pytest.raises(ValueError, match="each prepared static projection exactly once"):
        BrowseIndexManifest.model_validate({**receipt, "static_projections": tuple(reordered)})


def test_absent_coverage_is_unavailable_not_a_successful_empty_projection(
    ordinary_report, tmp_path: Path
) -> None:
    index = _index(ordinary_report, tmp_path / "index")
    available = {item.name for item in index.manifest.static_projections}
    assert ProjectionName.COVERAGE_FILES not in available
    assert ProjectionName.COVERAGE_MATRIX not in available
    with pytest.raises(ValueError, match="no matching evidence"):
        index.static_projection(StaticProjectionRequest(name=ProjectionName.COVERAGE_FILES))


def test_unbound_and_partial_coverage_remain_explicit(tmp_path: Path) -> None:
    report = _coverage_report()
    unbound_file = report.coverage_files[0].model_copy(
        update={
            "binding": CoverageBindingState.SOURCE_MISMATCH,
            "source_sha256": None,
            "limitations": ("Coverage source hash does not match the analyzed source.",),
        }
    )
    partial = report.model_copy(
        update={
            "coverage_files": (unbound_file,),
            "completeness": Completeness.PARTIAL,
            "limitations": tuple(
                sorted(
                    {
                        *report.limitations,
                        "Coverage source hash does not match the analyzed source.",
                    }
                )
            ),
        }
    )
    page = _index(partial, tmp_path / "index").static_projection(
        StaticProjectionRequest(name=ProjectionName.COVERAGE_FILES)
    )
    assert page.summary.completeness is Completeness.PARTIAL
    assert page.records[0].cells[1].string_value == "source-mismatch"
    assert any("does not match" in item for item in page.summary.limitations)


def test_sparse_coverage_rows_preserve_null_metrics_identities_and_evidence() -> None:
    report = _coverage_report()
    observation = report.coverage_observations[0].model_copy(
        update={"contexts": ("tests.test_module.test_calculate", "tests.test_module.test_other")}
    )
    expanded = report.model_copy(update={"coverage_observations": (observation,)})
    matrix = build_projection(expanded, ProjectionName.COVERAGE_MATRIX, max_records=100_000)
    assert isinstance(matrix, MatrixProjection)
    first, second = matrix.cells
    sparse = matrix.model_copy(
        update={"cells": (first, second.model_copy(update={"metrics": second.metrics[1:]}))}
    )
    table = _sparse_matrix_table(sparse)
    columns = {column.column_id: column.position for column in table.columns}
    assert table.rows[1].cells[columns["covered-lines"]].kind is ScalarKind.NULL
    assert table.rows[1].cells[0].string_value == second.row_id
    assert table.rows[1].cells[1].string_value == second.column_id
    assert table.rows[1].evidence_ids == second.evidence_ids


def test_coverage_search_filters_before_page_bounds(tmp_path: Path) -> None:
    report = _coverage_report()
    observation = report.coverage_observations[0].model_copy(
        update={"contexts": ("aaa-context", "zzz-context")}
    )
    index = _index(
        report.model_copy(update={"coverage_observations": (observation,)}), tmp_path / "index"
    )
    first = index.static_projection(
        StaticProjectionRequest(name=ProjectionName.COVERAGE_MATRIX, limit=1)
    )
    assert first.eligible == 2 and first.retained == 1 and first.truncated
    selected = index.static_projection(
        StaticProjectionRequest(name=ProjectionName.COVERAGE_MATRIX, query="zzz-context", limit=1)
    )
    assert selected.eligible == selected.retained == 1
    assert not selected.truncated
    assert selected.records[0].cells[1].string_value == "zzz-context"
