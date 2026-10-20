from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from code_analysis.browse_export import export_browse_page, render_browse_artifact
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseIndexManifest
from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.reports.projection_views import ProjectionFormat
from code_analysis.service import scan
from code_analysis.static_browse import (
    STATIC_PROJECTIONS,
    StaticProjectionPage,
    StaticProjectionRequest,
    static_page_projection,
    validate_static_projections,
)
from code_analysis.web_service import ReportViewService


@pytest.fixture
def static_index(ordinary_report, tmp_path):
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), destination)
    return BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def test_prepared_rows_match_authoritative_projection_builders(ordinary_report, static_index):
    for name in STATIC_PROJECTIONS:
        if name is ProjectionName.FUNCTION_COMMONALITY:
            continue  # Sparse matrix mapping is checked independently in test_static_commonality.
        expected = build_projection(ordinary_report, name, max_records=100_000)
        page = static_index.static_projection(StaticProjectionRequest(name=name))
        assert page.records == expected.rows
        assert page.summary.columns == expected.columns
        assert page.summary.sort == expected.sort
        assert page.summary.records == expected.record_count
        assert page.summary.source == expected.inputs[0]
        assert page.summary.completeness is expected.completeness
        assert not page.truncated


def test_source_free_late_paging_and_filter_before_bounds(tmp_path):
    source = tmp_path / "module.py"
    source.write_text(
        "\n".join(f"def function_{number:03}():\n    return {number}\n" for number in range(80))
    )
    report = scan(discover_context(source, AnalysisConfig()))
    manifest = build_browse_index(ReportViewService(report), tmp_path / "index")
    source.unlink()
    index = BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256)
    first = index.static_projection(StaticProjectionRequest(name=ProjectionName.COMPLEXITY))
    assert first.eligible == 80 and first.retained == 25 and first.truncated
    late = index.static_projection(
        StaticProjectionRequest(name=ProjectionName.COMPLEXITY, offset=75)
    )
    assert late.retained == 5 and not late.truncated
    selected = index.static_projection(
        StaticProjectionRequest(name=ProjectionName.COMPLEXITY, query="FUNCTION_079", limit=1)
    )
    assert selected.eligible == 1 and selected.retained == 1
    assert not selected.truncated
    assert "function_079" in selected.records[0].model_dump_json()
    assert static_page_projection(first).truncated
    assert static_page_projection(late).truncated
    assert not static_page_projection(selected).truncated


@pytest.mark.parametrize(
    "format", [ProjectionFormat.JSON_COMPACT, ProjectionFormat.MARKDOWN, ProjectionFormat.TSV]
)
def test_static_exports_are_deterministic_and_no_overwrite(static_index, format):
    request = StaticProjectionRequest(name=ProjectionName.CYCLE_LIST)
    path, projection = export_browse_page(static_index, request, "cycles", format)
    assert path.read_bytes() == render_browse_artifact(projection, format)
    assert projection.inputs[0].report_sha256 == static_index.manifest.report_sha256
    with pytest.raises(FileExistsError):
        export_browse_page(static_index, request, "cycles", format)


@pytest.mark.parametrize(
    "options",
    [
        {"name": ProjectionName.DEPENDENCIES},
        {"limit": True},
        {"limit": 0},
        {"limit": 501},
        {"offset": -1},
        {"query": 1},
        {"unknown": 1},
    ],
)
def test_strict_static_request(options):
    with pytest.raises(ValidationError):
        StaticProjectionRequest.model_validate({"name": ProjectionName.COMPLEXITY, **options})


def test_receipt_and_page_reject_dangling_or_contradictory_metadata(static_index):
    receipt = static_index.manifest.model_dump()
    receipt["static_projections"] = tuple(
        item for item in receipt["static_projections"] if item["name"] != ProjectionName.COMPLEXITY
    )
    with pytest.raises(ValidationError, match="each prepared"):
        BrowseIndexManifest.model_validate(receipt)
    page = static_index.static_projection(StaticProjectionRequest(name=ProjectionName.COMPLEXITY))
    with pytest.raises(ValidationError):
        StaticProjectionPage.model_validate({**page.model_dump(), "retained": page.retained + 1})
    receipt = static_index.manifest.model_dump()
    receipt["static_projections"][0]["source"]["report_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="another report"):
        BrowseIndexManifest.model_validate(receipt)


def test_malformed_projection_index_counts_fail_closed(static_index):
    with sqlite3.connect(static_index.database) as connection:
        connection.execute("DELETE FROM static_projection_rows WHERE name = 'complexity'")
        with pytest.raises(ValueError, match="disagree"):
            validate_static_projections(connection, static_index.manifest.static_projections)


def test_partial_source_and_prepared_truncation_survive_export(static_index):
    page = static_index.static_projection(StaticProjectionRequest(name=ProjectionName.COMPLEXITY))
    summary = page.summary.model_dump()
    summary.update(
        completeness=Completeness.PARTIAL,
        source_truncated=True,
        truncation_reason="Synthetic prepared-record bound",
    )
    partial = StaticProjectionPage.model_validate({**page.model_dump(), "summary": summary})
    projection = static_page_projection(partial)
    assert projection.completeness is Completeness.PARTIAL
    assert projection.truncated
    summary["completeness"] = Completeness.COMPLETE
    with pytest.raises(ValidationError, match="cannot be complete"):
        StaticProjectionPage.model_validate({**page.model_dump(), "summary": summary})


def test_malformed_stored_row_fails_closed(static_index):
    from code_analysis.static_browse import read_static_projection

    with sqlite3.connect(static_index.database) as connection:
        connection.execute(
            "UPDATE static_projection_rows SET payload = '{}' WHERE name = 'complexity'"
        )
        with pytest.raises(ValidationError):
            read_static_projection(
                connection,
                static_index.manifest.static_projections,
                StaticProjectionRequest(name=ProjectionName.COMPLEXITY),
            )
