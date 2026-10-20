from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from code_analysis.browse_export import export_browse_page
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.models import Completeness
from code_analysis.projection_models import TableProjection
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.runtime_browse_models import RuntimeBrowseRequest
from code_analysis.runtime_comparison_browse import RuntimeComparisonRequest
from code_analysis.runtime_queries import RuntimeQuerySection
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService


@pytest.fixture
def runtime_export_index(ordinary_report, tmp_path):
    golden = Path(__file__).parent / "golden" / "phase4-profile-report.json"
    runtime = RuntimeViewService.load(golden, golden)
    root = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), root, runtime=runtime)
    return BrowseIndex.open(root, report_sha256=manifest.report_sha256)


@pytest.mark.parametrize("section", (*tuple(RuntimeQuerySection), "comparison"))
@pytest.mark.parametrize(
    "output_format",
    (
        ProjectionFormat.JSON_COMPACT,
        ProjectionFormat.TSV,
        ProjectionFormat.MARKDOWN,
    ),
)
def test_runtime_exports_retain_exact_records(
    runtime_export_index, section, output_format, monkeypatch
):
    index = runtime_export_index
    monkeypatch.setattr(RuntimeViewService, "load", lambda *a: pytest.fail("profile reload"))
    request = (
        RuntimeComparisonRequest(limit=1)
        if section == "comparison"
        else RuntimeBrowseRequest(section=section, limit=1)
    )
    records = (
        index.runtime_comparison(request).report.records
        if section == "comparison"
        else index.runtime_page(request).panel.projection.records
    )
    path, projection = export_browse_page(index, request, "runtime", output_format)
    assert path.read_text() == render_projection(projection, output_format)
    assert projection.record_count == len(records)
    assert (
        projection.completeness is Completeness.PARTIAL
    )  # static binding is intentionally unmatched
    assert len(projection.inputs) == 1
    if section == "comparison":
        assert projection.inputs[0].report_schema_name == "code-analysis-profile-diff"
    else:
        assert projection.inputs[0].report_sha256 == index.manifest.runtime.profile_report_sha256
    assert any("Runtime provenance and compatibility:" in item for item in projection.limitations)
    assert any('"offset":0' in item for item in projection.limitations)
    for row, record in zip(projection.rows, records, strict=True):
        values = record.model_dump(mode="json")
        for field, cell in zip(values, row.cells, strict=True):
            expected = values[field]
            assert (
                cell.string_value if isinstance(expected, str) else json.loads(cell.string_value)
            ) == expected
    if output_format is ProjectionFormat.JSON_COMPACT:
        assert TableProjection.model_validate_json(path.read_bytes()) == projection
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        export_browse_page(index, request, "runtime", output_format)
    assert path.read_bytes() == before


@pytest.mark.parametrize("comparison", [False, True])
def test_terminal_runtime_export_and_explicit_viewer(runtime_export_index, monkeypatch, comparison):
    pytest.importorskip("textual")
    from textual.widgets import Input

    from code_analysis import tui_export
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_export import ExportPage

    opened = []
    monkeypatch.setattr(tui_export, "open_artifact", lambda *args: opened.append(args) or False)

    async def exercise():
        app = AnalysisTerminal(runtime_export_index)
        async with app.run_test(size=(180, 48)) as pilot:
            await pilot.press("ctrl+r")
            await app.workers.wait_for_complete()
            if comparison:
                await pilot.click("#runtime-compare")
                await app.workers.wait_for_complete()
            await pilot.press("ctrl+e")
            assert isinstance(app.screen, ExportPage)
            screen = app.screen
            screen.query_one("#export-name", Input).value = "saved-runtime"
            await pilot.click("#export-write")
            await app.workers.wait_for_complete()
            assert screen.exported_path is not None
            projection = TableProjection.model_validate_json(screen.exported_path.read_bytes())
            assert projection.record_count == 2
            assert not opened
            await pilot.click("#export-open")
            await app.workers.wait_for_complete()
            assert len(opened) == 1
            assert opened[0][0] == screen.exported_path
            await pilot.press("escape")

    asyncio.run(exercise())
