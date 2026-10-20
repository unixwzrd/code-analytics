from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseIndexManifest
from code_analysis.comparisons import build_report_delta
from code_analysis.web_service import ReportViewService


@pytest.fixture
def changed_report(ordinary_report):
    finding = ordinary_report.findings[0]
    return ordinary_report.model_copy(
        update={
            "findings": (
                finding.model_copy(update={"title": finding.title + " changed"}),
                *ordinary_report.findings[1:],
            )
        }
    )


@pytest.mark.parametrize("identical", (True, False))
def test_comparison_index_matches_live_pages_and_records(
    ordinary_report, changed_report, tmp_path, identical
):
    service = ReportViewService(ordinary_report, ordinary_report if identical else changed_report)
    path = tmp_path / "index"
    manifest = build_browse_index(service, path)
    index = BrowseIndex.open(path, report_sha256=manifest.report_sha256)
    assert manifest.comparison.identical is identical
    for offset in range(len(service.delta.records) + 1):
        assert (
            index.comparison_records(offset=offset, limit=1).model_dump_json()
            == service.comparison_records(offset=offset, limit=1).model_dump_json()
        )
    for position, record in enumerate(service.delta.records):
        assert index.comparison_record(position) == record
    if identical:
        assert manifest.comparison.eligible == 0 and manifest.comparison.counts is None
    else:
        assert manifest.comparison.counts.changed >= 1
    for value in (True, -1):
        with pytest.raises(ValueError):
            index.comparison_records(offset=value)
    raw = manifest.model_dump(mode="json")
    raw["comparison"]["candidate_sha256"] = "f" * 64
    with pytest.raises(ValidationError):
        BrowseIndexManifest.model_validate_json(json.dumps(raw))


def test_comparison_omissions_are_not_page_availability(ordinary_report, changed_report, tmp_path):
    delta = build_report_delta(
        ordinary_report, changed_report, include_unchanged=True, max_records=1
    )
    service = ReportViewService(ordinary_report, changed_report, delta)
    manifest = build_browse_index(service, tmp_path / "index")
    index = BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256)
    page = index.comparison_records()
    assert page.retained == 1 and not page.truncated
    assert page.omitted > 0 and manifest.comparison.omitted == page.omitted


@pytest.mark.parametrize("identical", (True, False))
def test_terminal_comparison_records_and_identical_state(
    ordinary_report, changed_report, tmp_path, identical
):
    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_comparison import ComparisonBrowser

    candidate = ordinary_report if identical else changed_report
    delta = build_report_delta(
        ordinary_report, candidate, include_unchanged=True, max_records=100_000
    )
    manifest = build_browse_index(
        ReportViewService(ordinary_report, candidate, delta), tmp_path / "index"
    )
    index = BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256)

    async def exercise():
        app = AnalysisTerminal(index, page_size=10)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("f7")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, ComparisonBrowser)
            if identical:
                assert app.screen.page.retained == 0 and not app.screen.page.truncated
            else:
                assert app.screen.page.retained == 10 and app.screen.page.truncated
                await pilot.press("ctrl+n")
                await app.workers.wait_for_complete()
                assert app.screen.page.offset == 10
                await pilot.press("ctrl+p")
                await app.workers.wait_for_complete()
                assert app.screen.page.offset == 0
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert isinstance(app.screen, RowDetails)
                assert (
                    '"baseline_value"' in app.screen.detail
                    and '"candidate_value"' in app.screen.detail
                )
                await pilot.press("escape")
            await pilot.press("escape")
            await pilot.press("ctrl+c")
        assert app.return_value == app.exit_status

    asyncio.run(exercise())


def test_index_compare_cli_preserves_partial_equality(ordinary_report, tmp_path, capsys):
    from code_analysis.cli import main
    from code_analysis.models import Completeness
    from code_analysis.reports.json_report import canonical_json

    report = tmp_path / "report.json"
    report.write_text(canonical_json(ordinary_report))
    result = main(
        ["index", str(report), "--compare", str(report), "--output", str(tmp_path / "index")]
    )
    assert result == (0 if ordinary_report.completeness is Completeness.COMPLETE else errno.ENODATA)
    manifest = BrowseIndexManifest.model_validate_json(capsys.readouterr().out)
    assert manifest.comparison.identical and manifest.comparison.eligible == 0


def test_terminal_comparison_source_choices_at_80_columns(
    ordinary_report, changed_report, tmp_path
):
    from textual.widgets import Button, Input

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_comparison import ComparisonSources
    from code_analysis.tui_export import ExportPage
    from code_analysis.tui_source import TerminalSource

    service = ReportViewService(ordinary_report, changed_report)
    manifest = build_browse_index(service, tmp_path / "index")
    index = BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256)

    async def exercise():
        app = AnalysisTerminal(index, source_root=Path(__file__).parent / "fixtures/ordinary")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("f7")
            await app.workers.wait_for_complete()
            app.screen.query_one("#comparison-kind", Input).value = "module"
            app.screen.apply_filters()
            await app.workers.wait_for_complete()
            assert app.screen.page.retained == 0
            assert app.screen.page.filtered_out > 0
            app.screen.query_one("#comparison-kind", Input).value = "finding"
            app.screen.apply_filters()
            await app.workers.wait_for_complete()
            await pilot.press("ctrl+e")
            await pilot.pause()
            assert isinstance(app.screen, ExportPage)
            assert app.screen.request.filters.kinds == ("finding",)
            app.screen.query_one("#export-name", Input).value = "comparison"
            app.screen.query_one("#export-write", Button).press()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen.exported_path is not None
            assert json.loads(app.screen.exported_path.read_text())["record_count"] == 1
            await pilot.press("escape")
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, ComparisonSources)
            assert {query.side for query in app.screen.choices} == {"baseline", "candidate"}
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, TerminalSource)
            assert app.screen.preview.state == "bound"
            assert app.screen.preview.lines
            await pilot.press("escape", "escape", "escape", "ctrl+c")

    asyncio.run(exercise())
