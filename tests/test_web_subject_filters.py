"""Bounded Web subject-finding filters proven against the saved-report CLI builder."""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlencode

import pytest
from browser_navigation import reveal_navigation
from pydantic import ValidationError

from code_analysis.config import load_config_file
from code_analysis.models import SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projection_models import (
    ProjectionFindingCategory,
    ProjectionFindingSeverity,
    TableProjection,
)
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.service import scan_refactor
from code_analysis.ui_projections import UIProjectionCatalog, UIProjectionRequest, project_report
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "subject-filter-test"
UI_BOUNDS = {"max_records": 100, "max_nodes": 50, "max_edges": 100}
DUPLICATION_QUERY = (
    ("name", "subject-findings"),
    ("finding_categories", "duplication"),
    ("finding_severities", "warning"),
    ("finding_rule_ids", "duplication.normalized-ast"),
)
DUPLICATION_OPTIONS = {
    "finding_categories": (ProjectionFindingCategory.DUPLICATION,),
    "finding_severities": (ProjectionFindingSeverity.WARNING,),
    "finding_rule_ids": ("duplication.normalized-ast",),
}


@pytest.fixture(scope="module")
def deleted_source_report(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[AnalysisReport, Path]:
    """Scan a copied project, then delete it: filters must use the saved report only."""
    root = tmp_path_factory.mktemp("web-subject-filters")
    source = root / "source"
    shutil.copytree(FIXTURES / "proposal_project", source)
    context = discover_context(source, load_config_file(FIXTURES / "proposal-config.toml"))
    retained = AnalysisReport.model_validate_json(scan_refactor(context).model_dump_json())
    shutil.rmtree(source)
    return retained, source


@contextmanager
def _session(report: AnalysisReport) -> Iterator[HTTPConnection]:
    server = create_server(ReportViewService(report), token=TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            yield connection
        finally:
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _browser_session(report: AnalysisReport) -> Iterator[tuple[object, str]]:
    playwright = pytest.importorskip("playwright.sync_api")
    server = create_server(ReportViewService(report), token=TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                yield browser, f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}"
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(
    connection: HTTPConnection, path: str, query: tuple[tuple[str, str], ...]
) -> tuple[int, bytes]:
    connection.request(
        "GET", f"{path}?{urlencode(query)}", headers={"X-Code-Analysis-Token": TOKEN}
    )
    response = connection.getresponse()
    return response.status, response.read()


def _cli_projection(report: AnalysisReport, **options: object) -> TableProjection:
    projection = build_projection(report, ProjectionName.SUBJECT_FINDINGS, **UI_BOUNDS, **options)
    assert isinstance(projection, TableProjection)
    return projection


def _positions(projection: TableProjection) -> dict[str, int]:
    return {column.column_id: column.position for column in projection.columns}


def _cell(payload: dict, positions: dict[str, int], row_id: str, column_id: str) -> object:
    row = next(item for item in payload["rows"] if item["row_id"] == row_id)
    cell = row["cells"][positions[column_id]]
    return cell["integer_value"] if cell["integer_value"] is not None else cell["string_value"]


def _subject_with_findings(report: AnalysisReport, module: str) -> str:
    return next(
        subject.subject_id
        for subject in report.subjects
        if subject.module == module
        and any(subject.subject_id in finding.subject_ids for finding in report.findings)
    )


def _filter_field(page: object, field: str) -> object:
    return page.locator(f'#finding-projection-controls [name="{field}"]')


def _language_field(page: object) -> object:
    return page.locator('#projection-controls select[name="language"]')


def _language_scope(page: object) -> list[str]:
    return _language_field(page).evaluate(
        "select => [...select.selectedOptions].map(option => option.value)"
    )


# Mount the explorer directly with a saved request: restoration must be proven without a handoff.
_RESTORE_EXPLORER = """async ({token, initialRequest}) => {
    const {projectionExplorer} = await import('/projections.js');
    const headers = {'X-Code-Analysis-Token': token};
    const request = async (path, signal) => {
        const response = await fetch(path, {signal, headers});
        if (!response.ok) throw new Error((await response.json()).error.message);
        return response;
    };
    const api = (path, signal) => request(path, signal).then(response => response.json());
    const escape = value => String(value).replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
    const container = document.createElement('div');
    container.id = 'restored-explorer';
    document.querySelector('#content').replaceChildren(container);
    await projectionExplorer(container, api, request, escape,
        new AbortController().signal, async () => {}, {initialRequest});
}"""


def _restore_explorer(page: object, initial_request: dict[str, object]) -> None:
    page.evaluate(_RESTORE_EXPLORER, {"token": TOKEN, "initialRequest": initial_request})


def _subject_without_findings(report: AnalysisReport) -> str:
    return next(
        subject.subject_id
        for subject in report.subjects
        if all(subject.subject_id not in finding.subject_ids for finding in report.findings)
    )


def test_filtered_totals_are_recomputed_and_match_the_cli_builder(
    deleted_source_report: tuple[AnalysisReport, Path],
) -> None:
    report, source = deleted_source_report
    assert not source.exists()
    subject = _subject_with_findings(report, "package.combine_a")
    unfiltered = _cli_projection(report)
    expected = _cli_projection(
        report, module_prefixes=("package.combine_a",), **DUPLICATION_OPTIONS
    )
    positions = _positions(expected)
    with _session(report) as connection:
        status, payload = _get(
            connection,
            "/api/projection",
            (*DUPLICATION_QUERY, ("module_prefixes", "package.combine_a")),
        )
        assert status == 200
        body = json.loads(payload)
        assert body == expected.model_dump(mode="json")
        assert body["selection"]["module_prefixes"] == ["package.combine_a"]
        assert body["selection"]["finding_categories"] == ["duplication"]
        assert body["selection"]["finding_severities"] == ["warning"]
        assert body["selection"]["finding_rule_ids"] == ["duplication.normalized-ast"]
        # Subject totals are recomputed before aggregation, not hidden after it.
        unfiltered_row = next(item for item in unfiltered.rows if item.row_id == subject)
        assert unfiltered_row.cells[positions["finding-count"]].integer_value > 1
        assert _cell(body, positions, subject, "finding-count") == 1
        assert _cell(body, positions, subject, "warnings") == 1
        assert _cell(body, positions, subject, "info") == 0
        assert _cell(body, positions, subject, "category-counts") == "duplication=1"
        assert _cell(body, positions, subject, "rule-counts") == "duplication.normalized-ast=1"
        # The retained module prefix scope does not widen to sibling modules.
        assert {item["cells"][positions["module"]]["string_value"] for item in body["rows"]} == {
            "package.combine_a"
        }


def test_default_requests_retain_the_existing_projection_identity(
    deleted_source_report: tuple[AnalysisReport, Path],
) -> None:
    report, _ = deleted_source_report
    baseline = _cli_projection(report)
    assert project_report(report, UIProjectionRequest(name=ProjectionName.SUBJECT_FINDINGS)) == (
        baseline
    )
    with _session(report) as connection:
        status, payload = _get(connection, "/api/projection", (("name", "subject-findings"),))
        assert status == 200
        body = json.loads(payload)
        assert body["projection_id"] == baseline.projection_id
        assert body == baseline.model_dump(mode="json")
        assert body["selection"]["module_prefixes"] == []
        assert body["selection"]["finding_categories"] == []
        assert body["selection"]["finding_severities"] == []
        assert body["selection"]["finding_rule_ids"] == []
        status, payload = _get(connection, "/api/projections", ())
        assert status == 200
        assert json.loads(payload) == UIProjectionCatalog().model_dump(mode="json")
        assert json.loads(payload)["finding_severities"] == ["info", "warning", "error"]


def test_filter_values_match_by_or_within_and_by_and_across_dimensions(
    deleted_source_report: tuple[AnalysisReport, Path],
) -> None:
    report, _ = deleted_source_report
    subject = _subject_with_findings(report, "package.combine_a")
    positions = _positions(_cli_projection(report))
    with _session(report) as connection:
        status, payload = _get(
            connection,
            "/api/projection",
            (
                ("name", "subject-findings"),
                ("finding_categories", "duplication"),
                ("finding_categories", "module-combination"),
            ),
        )
        assert status == 200
        category_or = json.loads(payload)
        assert category_or == _cli_projection(
            report,
            finding_categories=(
                ProjectionFindingCategory.DUPLICATION,
                ProjectionFindingCategory.MODULE_COMBINATION,
            ),
        ).model_dump(mode="json")
        assert _cell(category_or, positions, subject, "finding-count") == 2
        assert (
            _cell(category_or, positions, subject, "category-counts")
            == "duplication=1, module-combination=1"
        )

        status, payload = _get(
            connection,
            "/api/projection",
            (
                ("name", "subject-findings"),
                ("finding_rule_ids", "configured-layer-direction"),
                ("finding_rule_ids", "duplication.normalized-ast"),
            ),
        )
        assert status == 200
        rule_or = json.loads(payload)
        assert rule_or["record_count"] > 0
        assert rule_or["projection_id"] != category_or["projection_id"]

        # Duplication is recorded as a warning, so an info severity retains nothing.
        status, payload = _get(
            connection,
            "/api/projection",
            (
                ("name", "subject-findings"),
                ("finding_categories", "duplication"),
                ("finding_severities", "info"),
            ),
        )
        assert status == 200
        conjunction = json.loads(payload)
        assert conjunction["record_count"] == 0
        assert conjunction["rows"] == []


def test_selected_subject_without_matching_findings_retains_zero_totals(
    deleted_source_report: tuple[AnalysisReport, Path],
) -> None:
    report, _ = deleted_source_report
    subject = _subject_without_findings(report)
    positions = _positions(_cli_projection(report))
    with _session(report) as connection:
        status, payload = _get(
            connection, "/api/projection", (*DUPLICATION_QUERY, ("select", subject))
        )
        assert status == 200
        body = json.loads(payload)
        assert body == _cli_projection(
            report, selected_ids=(subject,), **DUPLICATION_OPTIONS
        ).model_dump(mode="json")
        assert body["record_count"] == 1
        assert _cell(body, positions, subject, "finding-count") == 0
        assert _cell(body, positions, subject, "category-counts") == ""


@pytest.mark.parametrize(
    ("query", "message"),
    (
        (
            (("name", "modules"), ("finding_severities", "warning")),
            "finding filters require the subject-findings projection",
        ),
        (
            (("name", "findings"), ("module_prefixes", "package")),
            "finding filters require the subject-findings projection",
        ),
        (
            (
                ("name", "subject-findings"),
                ("select", "subject-000"),
                ("module_prefixes", "package"),
            ),
            "exact subject selection cannot be combined with module-prefix selection",
        ),
        ((("name", "subject-findings"), ("finding_severities", "critical")), "critical"),
        ((("name", "subject-findings"), ("finding_categories", "duplication ")), "duplication"),
        (
            (("name", "subject-findings"), ("module_prefixes", "package.*")),
            "module prefixes must be dotted module names",
        ),
        (
            (("name", "subject-findings"), ("module_prefixes", "")),
            "module prefixes must be dotted module names",
        ),
        (
            (("name", "subject-findings"), ("finding_rule_ids", " duplication")),
            "rule IDs must be nonempty and trimmed",
        ),
        (
            (("name", "subject-findings"), ("finding_rule_ids", "")),
            "rule IDs must be nonempty and trimmed",
        ),
        (
            (("name", "subject-findings"), ("finding_category", "duplication")),
            "unsupported projection parameters: finding_category",
        ),
        (
            (("name", "subject-findings"), ("finding_severity", "warning")),
            "unsupported projection parameters: finding_severity",
        ),
        (
            (("name", "subject-findings"), ("language", "python")),
            "language selection is unsupported for the subject-findings projection",
        ),
        (
            (
                ("name", "subject-findings"),
                ("language", "python"),
                ("finding_categories", "duplication"),
            ),
            "findings are aggregated across all recorded languages",
        ),
    ),
)
def test_invalid_filter_requests_fail_explicitly(
    deleted_source_report: tuple[AnalysisReport, Path],
    query: tuple[tuple[str, str], ...],
    message: str,
) -> None:
    report, _ = deleted_source_report
    with _session(report) as connection:
        status, payload = _get(connection, "/api/projection", query)
        assert status == 400
        error = json.loads(payload)["error"]
        assert error["code"] == "invalid-request"
        assert message in error["message"]


def test_language_scope_is_rejected_for_subject_findings_and_retained_for_other_views(
    deleted_source_report: tuple[AnalysisReport, Path],
) -> None:
    report, _ = deleted_source_report
    # The shared aggregation ignores languages, so the request must refuse the scope outright.
    with pytest.raises(ValidationError, match="language selection is unsupported"):
        UIProjectionRequest(
            name=ProjectionName.SUBJECT_FINDINGS, languages=(SourceLanguage.PYTHON,)
        )
    with pytest.raises(ValidationError, match="language selection is unsupported"):
        UIProjectionRequest(
            name=ProjectionName.SUBJECT_FINDINGS,
            languages=(SourceLanguage.PYTHON,),
            finding_categories=(ProjectionFindingCategory.DUPLICATION,),
        )
    default = UIProjectionRequest(name=ProjectionName.SUBJECT_FINDINGS)
    assert default.languages == ()
    assert project_report(report, default) == _cli_projection(report)
    scoped = UIProjectionRequest(name=ProjectionName.MODULES, languages=(SourceLanguage.PYTHON,))
    assert project_report(report, scoped).selection.languages == (SourceLanguage.PYTHON,)
    with _session(report) as connection:
        status, payload = _get(
            connection, "/api/projection", (("name", "modules"), ("language", "python"))
        )
        assert status == 200
        assert json.loads(payload)["selection"]["languages"] == ["python"]
        status, payload = _get(
            connection, "/api/download", (("name", "subject-findings"), ("language", "python"))
        )
        assert status == 400
        assert "language selection is unsupported" in json.loads(payload)["error"]["message"]


def test_filtered_downloads_match_the_cli_rendered_bytes(
    deleted_source_report: tuple[AnalysisReport, Path],
) -> None:
    report, _ = deleted_source_report
    expected = _cli_projection(
        report, module_prefixes=("package.combine_a",), **DUPLICATION_OPTIONS
    )
    query = (*DUPLICATION_QUERY, ("module_prefixes", "package.combine_a"))
    with _session(report) as connection:
        for output_format in (ProjectionFormat.JSON, ProjectionFormat.TSV):
            status, payload = _get(
                connection, "/api/download", (*query, ("format", output_format.value))
            )
            assert status == 200
            assert payload.decode() == render_projection(expected, output_format)


def test_browser_applies_filters_to_totals_and_clears_inapplicable_controls(
    deleted_source_report: tuple[AnalysisReport, Path],
) -> None:
    report, _ = deleted_source_report
    subject = _subject_with_findings(report, "package.combine_a")
    positions = _positions(_cli_projection(report))
    with _browser_session(report) as (browser, url):
        page = browser.new_page()
        errors: list[str] = []
        projection_urls: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                projection_urls.append(request.url) if "/api/projection?" in request.url else None
            ),
        )
        page.goto(url)
        page.locator(".cards").wait_for()
        reveal_navigation(page, "Explore evidence").click()
        page.get_by_role("button", name="Download JSON", exact=True).wait_for()
        assert page.locator("#finding-projection-controls").is_hidden()

        page.get_by_label("Evidence view", exact=True).select_option("subject-findings")
        page.locator("#finding-projection-controls").wait_for(state="visible")
        page.get_by_role("button", name="Show evidence", exact=True).click()
        page.get_by_role("heading", name="Findings grouped by analysis subject").wait_for()
        unfiltered_row = page.locator("#projection-result tbody tr", has_text=subject)
        unfiltered_cells = unfiltered_row.locator("td")
        assert int(unfiltered_cells.nth(positions["finding-count"]).inner_text()) > 1

        _filter_field(page, "finding_categories").select_option(["duplication"])
        _filter_field(page, "finding_severities").select_option(["warning"])
        _filter_field(page, "module_prefixes").fill("package.combine_a")
        _filter_field(page, "finding_rule_ids").fill("duplication.normalized-ast")
        page.get_by_role("button", name="Show evidence", exact=True).click()
        page.get_by_role("heading", name="Findings grouped by analysis subject").wait_for()
        filtered_cells = page.locator("#projection-result tbody tr", has_text=subject).locator("td")
        assert int(filtered_cells.nth(positions["finding-count"]).inner_text()) == 1
        assert filtered_cells.nth(positions["category-counts"]).inner_text() == "duplication=1"
        assert page.locator("#projection-result tbody tr").count() == 1
        filtered_request = projection_urls[-1]
        for expected_pair in (
            "finding_categories=duplication",
            "finding_severities=warning",
            "finding_rule_ids=duplication.normalized-ast",
            "module_prefixes=package.combine_a",
        ):
            assert expected_pair in filtered_request.replace("%2E", ".")

        with page.expect_download() as download:
            page.get_by_role("button", name="Download JSON", exact=True).click()
        downloaded = json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
        assert downloaded == _cli_projection(
            report, module_prefixes=("package.combine_a",), **DUPLICATION_OPTIONS
        ).model_dump(mode="json")

        # A different evidence view has no finding vocabulary: the filters must not persist.
        page.get_by_label("Evidence view", exact=True).select_option("modules")
        assert page.locator("#finding-projection-controls").is_hidden()
        for field in ("module_prefixes", "finding_rule_ids", "finding_categories"):
            assert _filter_field(page, field).input_value() == ""
        page.get_by_role("button", name="Show evidence", exact=True).click()
        page.get_by_role("heading", name="Module metrics", exact=True).wait_for()
        assert "finding_" not in projection_urls[-1]
        assert "module_prefixes" not in projection_urls[-1]

        page.get_by_label("Evidence view", exact=True).select_option("subject-findings")
        page.locator("#finding-projection-controls").wait_for(state="visible")
        assert _filter_field(page, "finding_severities").input_value() == ""
        page.get_by_role("button", name="Show evidence", exact=True).click()
        page.get_by_role("heading", name="Findings grouped by analysis subject").wait_for()
        assert page.locator("#projection-result tbody tr").count() > 1
        assert not errors


def test_browser_restores_saved_filters_and_reports_rejected_combinations(
    deleted_source_report: tuple[AnalysisReport, Path],
) -> None:
    report, _ = deleted_source_report
    subject = _subject_with_findings(report, "package.combine_a")
    with _browser_session(report) as (browser, url):
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url)
        page.locator(".cards").wait_for()
        _restore_explorer(
            page,
            {
                "name": "subject-findings",
                "module_prefixes": ["package.combine_a"],
                "finding_categories": ["duplication"],
                "finding_severities": ["warning"],
                "finding_rule_ids": ["duplication.normalized-ast"],
            },
        )
        page.locator("#restored-explorer #finding-projection-controls").wait_for(state="visible")
        assert _filter_field(page, "module_prefixes").input_value() == "package.combine_a"
        assert _filter_field(page, "finding_rule_ids").input_value() == (
            "duplication.normalized-ast"
        )
        assert _filter_field(page, "finding_categories").input_value() == "duplication"
        assert _filter_field(page, "finding_severities").input_value() == "warning"
        assert _language_field(page).is_disabled()
        assert _language_scope(page) == [""]
        page.get_by_role("heading", name="Findings grouped by analysis subject").wait_for()
        assert page.locator("#restored-explorer tbody tr").count() == 1

        page.get_by_label("Exact selection ID", exact=True).first.fill(subject)
        page.get_by_role("button", name="Show evidence", exact=True).click()
        message = page.locator("#projection-result")
        message.get_by_text("cannot be combined", exact=False).wait_for()
        assert "Unable to build this view" in message.inner_text()
        assert not errors


@pytest.mark.parametrize(
    ("initial_request", "reason", "field"),
    (
        (
            {"name": "subject-findings", "finding_categories": ["mystery-category"]},
            "finding categories mystery-category are not recorded in this report vocabulary",
            "finding_categories",
        ),
        (
            {
                "name": "subject-findings",
                "finding_categories": ["duplication"],
                "finding_rule_ids": "duplication.normalized-ast",
            },
            "the saved finding rule IDs are not a list of recorded text values",
            "finding_rule_ids",
        ),
        (
            {"name": "subject-findings", "finding_severities": ["warning", 3]},
            "the saved finding severities are not a list of recorded text values",
            "finding_severities",
        ),
    ),
)
def test_browser_refuses_to_restore_unknown_or_malformed_finding_filters(
    deleted_source_report: tuple[AnalysisReport, Path],
    initial_request: dict[str, object],
    reason: str,
    field: str,
) -> None:
    report, _ = deleted_source_report
    with _browser_session(report) as (browser, url):
        page = browser.new_page()
        errors: list[str] = []
        projection_urls: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                projection_urls.append(request.url) if "/api/projection?" in request.url else None
            ),
        )
        page.goto(url)
        page.locator(".cards").wait_for()
        _restore_explorer(page, initial_request)
        result = page.locator("#projection-result")
        result.get_by_text("Unable to restore", exact=False).wait_for()
        # No broadened view is built and no download identity is offered for it.
        assert reason in result.inner_text()
        assert "no download is offered" in result.inner_text()
        assert projection_urls == []
        assert page.get_by_role("button", name="Download JSON", exact=True).count() == 0
        assert _filter_field(page, field).input_value() == ""

        # Submitting again keeps refusing: the saved selection is still unrestorable.
        page.get_by_role("button", name="Show evidence", exact=True).click()
        result.get_by_text("Unable to restore", exact=False).wait_for()
        assert projection_urls == []

        page.get_by_role("button", name="Reset finding filters", exact=True).click()
        assert projection_urls == []
        page.get_by_role("button", name="Show evidence", exact=True).click()
        page.get_by_role("heading", name="Findings grouped by analysis subject").wait_for()
        assert len(projection_urls) == 1
        assert "finding_" not in projection_urls[-1]
        assert page.locator("#restored-explorer tbody tr").count() > 1
        assert not errors


def test_browser_refuses_to_restore_a_language_scope_for_subject_findings(
    deleted_source_report: tuple[AnalysisReport, Path],
) -> None:
    report, _ = deleted_source_report
    with _browser_session(report) as (browser, url):
        page = browser.new_page()
        errors: list[str] = []
        projection_urls: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                projection_urls.append(request.url) if "/api/projection?" in request.url else None
            ),
        )
        page.goto(url)
        page.locator(".cards").wait_for()
        _restore_explorer(
            page,
            {
                "name": "subject-findings",
                "languages": ["python"],
                "finding_categories": ["duplication"],
            },
        )
        result = page.locator("#projection-result")
        result.get_by_text("Unable to restore", exact=False).wait_for()
        assert "aggregated across all recorded languages" in result.inner_text()
        assert projection_urls == []
        # The unsupported scope fails visibly instead of being cleared behind the user.
        assert _language_scope(page) == ["python"]
        assert not _language_field(page).is_disabled()
        assert page.locator("#projection-language-note").is_visible()
        assert _filter_field(page, "finding_categories").input_value() == "duplication"

        # Another unsupported scope is not a repair and must not broaden the view.
        _language_field(page).select_option(["ruby"])
        page.get_by_role("button", name="Show evidence", exact=True).click()
        result.get_by_text("Unable to restore", exact=False).wait_for()
        assert _language_scope(page) == ["ruby"]
        assert not _language_field(page).is_disabled()
        assert projection_urls == []
        assert page.get_by_role("button", name="Download JSON", exact=True).count() == 0

        _language_field(page).select_option(label="All languages")
        assert _language_scope(page) == [""]
        assert _language_field(page).is_disabled()
        page.get_by_role("button", name="Show evidence", exact=True).click()
        page.get_by_role("heading", name="Findings grouped by analysis subject").wait_for()
        assert len(projection_urls) == 1
        assert "language=" not in projection_urls[-1]
        assert "finding_categories=duplication" in projection_urls[-1]
        assert not errors


def test_browser_disables_and_clears_the_language_control_for_subject_findings(
    deleted_source_report: tuple[AnalysisReport, Path],
) -> None:
    report, _ = deleted_source_report
    with _browser_session(report) as (browser, url):
        page = browser.new_page()
        errors: list[str] = []
        projection_urls: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                projection_urls.append(request.url) if "/api/projection?" in request.url else None
            ),
        )
        page.goto(url)
        page.locator(".cards").wait_for()
        reveal_navigation(page, "Explore evidence").click()
        page.get_by_role("button", name="Download JSON", exact=True).wait_for()
        assert not _language_field(page).is_disabled()
        assert page.locator("#projection-language-note").is_hidden()
        _language_field(page).select_option(["python"])
        assert _language_scope(page) == ["python"]

        page.get_by_label("Evidence view", exact=True).select_option("subject-findings")
        assert _language_field(page).is_disabled()
        assert _language_scope(page) == [""]
        assert page.locator("#projection-language-note").is_visible()
        assert (
            "aggregated across all recorded languages"
            in page.locator("#projection-language-note").inner_text()
        )
        page.get_by_role("button", name="Show evidence", exact=True).click()
        page.get_by_role("heading", name="Findings grouped by analysis subject").wait_for()
        assert "language=" not in projection_urls[-1]

        # Other evidence views keep their language control and scope.
        page.get_by_label("Evidence view", exact=True).select_option("modules")
        assert not _language_field(page).is_disabled()
        assert page.locator("#projection-language-note").is_hidden()
        _language_field(page).select_option(["python"])
        page.get_by_role("button", name="Show evidence", exact=True).click()
        page.get_by_role("heading", name="Module metrics", exact=True).wait_for()
        assert "language=python" in projection_urls[-1]
        assert not errors
