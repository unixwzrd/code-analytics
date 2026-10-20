from __future__ import annotations

import asyncio
import json
import threading
from http.client import HTTPConnection

import pytest
from browser_navigation import reveal_navigation
from pydantic import ValidationError

from code_analysis.browse_export import build_browse_export
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseRequest
from code_analysis.models import Confidence
from code_analysis.projection_models import ProjectionFindingCategory
from code_analysis.report_models import AnalysisReport
from code_analysis.ui_rows import BrowseTable
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


@pytest.fixture
def filtered_report(ordinary_report):
    example = ordinary_report.findings[0]
    additions = tuple(
        example.model_copy(
            update={
                "finding_id": f"synthetic-filter-{number}",
                "title": f"Facet finding {number}",
                "category": category,
                "severity": severity,
                "confidence": confidence,
            }
        )
        for number, (category, severity, confidence) in enumerate(
            (
                ("complexity", "warning", Confidence.EXACT),
                ("complexity", "warning", Confidence.EXACT),
                ("complexity", "error", Confidence.EXACT),
                ("complexity", "warning", Confidence.HEURISTIC),
                ("orchestration", "warning", Confidence.EXACT),
            )
        )
    )
    payload = ordinary_report.model_dump()
    payload["findings"] = (*ordinary_report.findings, *additions)
    payload["summary"]["findings"] = len(payload["findings"])
    return AnalysisReport.model_validate(payload)


@pytest.fixture
def finding_index(filtered_report, tmp_path):
    service = ReportViewService(filtered_report)
    destination = tmp_path / "index"
    manifest = build_browse_index(service, destination)
    return service, BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


@pytest.mark.parametrize(
    "category,confidence,severity",
    [
        ("", "", ""),
        ("complexity", "", ""),
        ("", "exact", ""),
        ("complexity", "exact", "warning"),
        ("orchestration", "heuristic", "error"),
    ],
)
def test_index_and_report_filters_have_exact_paging_and_export_parity(
    finding_index, category, confidence, severity
):
    service, index = finding_index
    request = BrowseRequest(
        table=BrowseTable.FINDINGS,
        query="Facet finding",
        limit=1,
        category=ProjectionFindingCategory(category) if category else None,
        confidence=Confidence(confidence) if confidence else None,
        severity=severity,
    )
    expected = service.findings(
        offset=0,
        limit=500,
        query=request.query,
        category=category,
        confidence=confidence,
        severity=severity,
    )
    rows = []
    for offset in range(max(1, expected.eligible)):
        selection = request.model_copy(update={"offset": offset})
        page = index.page(selection)
        assert page.eligible == expected.eligible
        rows.extend(page.records)
        projection = build_browse_export(index, selection)
        assert projection.record_count == page.retained
        assert any(
            '"confidence":' in value and '"category":' in value for value in projection.limitations
        )
    assert tuple(rows) == expected.records
    filtered = build_browse_export(index, request)
    unfiltered = build_browse_export(index, BrowseRequest(table=BrowseTable.FINDINGS, limit=1))
    assert filtered.projection_id != unfiltered.projection_id


@pytest.mark.parametrize(
    "field,value", [("category", "bogus"), ("confidence", "bogus"), ("confidence", True)]
)
def test_invalid_or_inapplicable_finding_filters_fail(field, value):
    with pytest.raises(ValidationError):
        BrowseRequest.model_validate({"table": BrowseTable.FINDINGS, field: value})
    with pytest.raises(ValidationError):
        BrowseRequest(table=BrowseTable.MODULES, confidence=Confidence.EXACT)


def test_filter_http_validation_and_backend_facets(filtered_report):
    server = create_server(ReportViewService(filtered_report), token="filters")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_address[1])

    def get(path):
        connection.request("GET", path, headers={"X-Code-Analysis-Token": "filters"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    try:
        status, facets = get("/api/bootstrap")
        assert status == 200 and "exact" in facets["finding_confidences"]
        assert "complexity" in facets["finding_categories"]
        status, page = get(
            "/api/findings?category=complexity&confidence=exact&severity=warning&q=Facet"
        )
        assert status == 200 and page["eligible"] == 2
        for path in (
            "/api/findings?confidence=bogus",
            "/api/modules?category=complexity",
            "/api/findings?category=complexity&category=orchestration",
        ):
            assert get(path)[0] == 400
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_terminal_finding_filters_are_keyboard_applicable(finding_index):
    pytest.importorskip("textual")
    from textual.widgets import Input, Select

    from code_analysis.tui_app import AnalysisTerminal

    _, index = finding_index

    async def exercise():
        app = AnalysisTerminal(index, page_size=1)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("f3")
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.query_one("#search", Input).value = "Facet"
            app.query_one("#severity", Select).value = "warning"
            app.query_one("#finding-category", Select).value = "complexity"
            app.query_one("#finding-confidence", Select).value = "exact"
            app.query_one("#search", Input).focus()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.current_page.eligible == 2
            assert app.selection.confidence is Confidence.EXACT
            await pilot.press("ctrl+n")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.current_page.offset == 1 and app.current_page.eligible == 2
            await pilot.press("f2")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not app.query_one("#finding-filters").display
            assert app.selection.category is None

    asyncio.run(exercise())


def test_browser_finding_filters_use_backend_choices(filtered_report):
    playwright = pytest.importorskip("playwright.sync_api")
    server = create_server(ReportViewService(filtered_report), token="filters")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 768, "height": 1024})
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=filters")
                page.locator(".cards").wait_for()
                reveal_navigation(page, "Findings").click()
                page.get_by_role("combobox", name="Category", exact=True).select_option(
                    "complexity"
                )
                page.get_by_role("combobox", name="Confidence", exact=True).select_option("exact")
                page.get_by_role("combobox", name="Severity", exact=True).select_option("warning")
                page.get_by_role("button", name="Apply finding filters").click()
                playwright.expect(page.locator("tbody tr")).to_have_count(2)
                page.get_by_role("columnheader", name="Confidence", exact=True).wait_for()
                page.get_by_role("combobox", name="Confidence", exact=True).select_option(
                    "heuristic"
                )
                page.get_by_role("button", name="Apply finding filters").click()
                playwright.expect(page.locator("tbody tr")).to_have_count(1)
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
