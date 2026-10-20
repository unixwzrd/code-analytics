"""Bounded presentation of saved finding suppressions across shared UI surfaces."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from browser_navigation import reveal_navigation

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseRequest
from code_analysis.config import AnalysisConfig
from code_analysis.finding_suppression_models import (
    AcceptedFindingSuppression,
    finding_suppression_annotation,
)
from code_analysis.finding_suppressions import apply_finding_suppressions
from code_analysis.projections import build_projection
from code_analysis.projections._catalog import ProjectionName
from code_analysis.queries import build_query, render_query_json, render_query_markdown
from code_analysis.query_models import QuerySelector, QuerySelectorKind
from code_analysis.ui_rows import BrowseTable, finding_rows
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

EVALUATED_ON = "2026-09-22"
EXPIRES_ON = "2026-09-23"
RATIONALE = "Reviewed exact finding for presentation parity"


def _config(finding_id: str, *, evaluated: str = EVALUATED_ON) -> AnalysisConfig:
    return AnalysisConfig(
        accepted_suppressions=(
            AcceptedFindingSuppression(
                finding_id=finding_id,
                rationale=RATIONALE,
                expires_on=EXPIRES_ON,
            ),
        ),
        suppression_evaluation_date=evaluated,
    )


def _annotated_report(ordinary_report):
    identity = ordinary_report.findings[0].finding_id
    return apply_finding_suppressions(ordinary_report, _config(identity)), identity


def test_unsuppressed_rows_and_queries_are_explicitly_not_suppressed(ordinary_report) -> None:
    identity = ordinary_report.findings[0].finding_id
    row = next(item for item in finding_rows(ordinary_report) if item.finding_id == identity)
    assert row.suppression.status == "not-suppressed"
    assert row.suppression_summary == "not suppressed"

    response = build_query(
        ordinary_report,
        QuerySelector(kind=QuerySelectorKind.FINDING, identity=identity),
    )
    item = next(record for record in response.items if record.item_id == identity)
    assert item.finding_suppression == row.suppression
    assert "Suppression: `not-suppressed`." in render_query_markdown(response)


def test_saved_accepted_annotation_parity_across_report_index_and_projection(
    ordinary_report, tmp_path
) -> None:
    report, identity = _annotated_report(ordinary_report)
    saved = report.findings[0].suppression
    assert saved is not None
    assert saved.status.value == "accepted"

    row = next(item for item in finding_rows(report) if item.finding_id == identity)
    assert row.suppression.status == "accepted"
    assert row.suppression.rationale == RATIONALE
    assert row.suppression.evaluated_on == EVALUATED_ON
    assert row.suppression.expires_on == EXPIRES_ON

    service = ReportViewService(report)
    index_dir = tmp_path / "index"
    manifest = build_browse_index(service, index_dir)
    index = BrowseIndex.open(index_dir, report_sha256=manifest.report_sha256)
    page = index.page(BrowseRequest(table=BrowseTable.FINDINGS, limit=50))
    indexed = next(record for record in page.records if record.finding_id == identity)
    assert indexed.suppression == row.suppression
    assert indexed.suppression_summary == row.suppression_summary

    projection = build_projection(report, ProjectionName.FINDINGS)
    positions = {column.column_id: column.position for column in projection.columns}
    finding_row = next(row for row in projection.rows if row.row_id == identity)
    assert finding_row.cells[positions["suppression-status"]].string_value == "accepted"
    assert finding_row.cells[positions["suppression-evaluated-on"]].string_value == EVALUATED_ON
    assert finding_row.cells[positions["suppression-expires-on"]].string_value == EXPIRES_ON
    assert finding_row.cells[positions["suppression-rationale"]].string_value == RATIONALE

    query = build_query(report, QuerySelector(kind=QuerySelectorKind.FINDING, identity=identity))
    item = next(record for record in query.items if record.item_id == identity)
    assert item.finding_suppression == finding_suppression_annotation(saved)
    payload = json.loads(render_query_json(query))
    assert payload["items"][-1]["finding_suppression"]["status"] == "accepted"
    markdown = render_query_markdown(query)
    assert RATIONALE in markdown
    assert f"`{EVALUATED_ON}`" in markdown
    assert f"`{EXPIRES_ON}`" in markdown


def test_expired_saved_status_is_presented_without_recomputing(ordinary_report) -> None:
    identity = ordinary_report.findings[0].finding_id
    report = apply_finding_suppressions(ordinary_report, _config(identity, evaluated=EXPIRES_ON))
    row = next(item for item in finding_rows(report) if item.finding_id == identity)
    assert row.suppression.status == "expired"
    assert row.suppression.evaluated_on == EXPIRES_ON


def test_web_findings_table_shows_saved_suppression_summary(ordinary_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report, _identity = _annotated_report(ordinary_report)
    server = create_server(ReportViewService(report), token="suppression-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=suppression-test")
                reveal_navigation(page, "Findings").click()
                page.get_by_role("columnheader", name="Suppression", exact=True).wait_for()
                row = page.locator("tbody tr").filter(has_text=RATIONALE)
                assert "accepted" in row.inner_text()
                assert EXPIRES_ON in row.inner_text()
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_terminal_findings_table_shows_suppression_summary(ordinary_report, tmp_path) -> None:
    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal

    report, _identity = _annotated_report(ordinary_report)
    service = ReportViewService(report)
    destination = tmp_path / "terminal-index"
    manifest = build_browse_index(service, destination)
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f3")
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.screen.query_one("#records")
            headers = {str(column.label) for column in table.columns.values()}
            assert "Suppression Summary" in headers
            assert "Suppression" not in headers
            annotated = next(
                record
                for record in app.current_page.records
                if record.suppression.status == "accepted"
            )
            assert annotated.suppression.rationale == RATIONALE
            return annotated.suppression_summary

    summary = asyncio.run(exercise())
    assert "accepted" in summary
    assert EXPIRES_ON in summary
