"""Shared target-source and honest coverage navigation from static test relationships."""

import asyncio
from urllib.parse import urlencode

import pytest
from browser_navigation import reveal_navigation
from test_coverage import _coverage_json, _project
from test_web_runtime_comparison_source import TOKEN, fetch, served

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseRequest
from code_analysis.config import AnalysisConfig
from code_analysis.coverage_evidence import import_coverage
from code_analysis.project import discover_context
from code_analysis.report_models import CoverageBindingState
from code_analysis.service import scan
from code_analysis.test_browsing import investigate_test
from code_analysis.test_browsing import test_impact_rows as impact_rows
from code_analysis.ui_rows import BrowseTable
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


@pytest.fixture
def investigation(tmp_path):
    root, _ = _project(tmp_path)
    (root / "tests").mkdir()
    (root / "tests/test_module.py").write_text(
        "from module import calculate\n\ndef test_calculate():\n    assert calculate(1) == 2\n"
    )
    report = scan(discover_context(root, AnalysisConfig()))
    data = tmp_path / "coverage.json"
    _coverage_json(data)
    bound = import_coverage(report, (data,), source_root=root, workload_identity_sha256="a" * 64)
    return root, report, bound, data


def _target(report):
    return next(
        row
        for row in impact_rows(report)
        if row.detail.target_subject
        and row.detail.target_subject.logical_name.endswith(":calculate")
    )


def test_test_and_application_sources_keep_separate_authority(investigation):
    _, report, bound, _ = investigation
    original = investigate_test(_target(report))
    detail = investigate_test(_target(bound))
    assert {item.role for item in detail.sources} == {"test", "application"}
    test = next(item for item in detail.sources if item.role == "test")
    application = next(item for item in detail.sources if item.role == "application")
    assert test.path == "tests/test_module.py" and application.path == "module.py"
    assert test.source_sha256 != application.source_sha256
    assert not original.row.detail.target_coverage.observations
    assert detail.row.detail.target_coverage.observations[0].binding is CoverageBindingState.BOUND


def test_unbound_coverage_is_retained_as_unbound(investigation):
    _, report, _, data = investigation
    unbound = import_coverage(report, (data,), source_root=None, workload_identity_sha256="a" * 64)
    coverage = _target(unbound).detail.target_coverage
    assert coverage.observations
    assert all(item.binding is not CoverageBindingState.BOUND for item in coverage.observations)


def test_http_test_and_target_preview_and_stale_identity(investigation):
    root, _, bound, _ = investigation
    service = ReportViewService(bound)
    row = _target(bound)
    params = {
        "identity": row.identity,
        "report_sha256": service.bootstrap().reports[0].report_sha256,
    }
    with served(create_server(service, source_root=root, token=TOKEN)) as server:
        status, detail = fetch(server, "/api/test-detail?" + urlencode(params))
        assert status == 200
        for choice in detail["sources"]:
            status, preview = fetch(
                server,
                "/api/test-source?" + urlencode({**params, "choice_id": choice["choice_id"]}),
            )
            assert status == 200 and preview["state"] == "bound"
            assert preview["expected_sha256"] == choice["source_sha256"]
        assert (
            fetch(server, "/api/test-detail?" + urlencode({**params, "report_sha256": "0" * 64}))[0]
            == 400
        )
        assert (
            fetch(server, "/api/test-source?" + urlencode({**params, "choice_id": "foreign"}))[0]
            == 400
        )


def test_terminal_target_source_and_coverage_at_80_columns(investigation, tmp_path):
    from textual.widgets import Button

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_source import TerminalSource
    from code_analysis.tui_static_projections import StaticProjectionBrowser
    from code_analysis.tui_test_impact import TestInvestigationScreen

    root, _, bound, _ = investigation
    service = ReportViewService(bound)
    manifest = build_browse_index(service, tmp_path / "index")
    index = BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256)
    row = index.page(
        BrowseRequest(table=BrowseTable.TEST_IMPACT, query=_target(bound).identity)
    ).records[0]

    async def exercise():
        app = AnalysisTerminal(index, source_root=root)
        async with app.run_test(size=(80, 24)) as pilot:
            app.open_test_record(row)
            await pilot.pause()
            assert isinstance(app.screen, TestInvestigationScreen)
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, TerminalSource)
            assert app.screen.preview.state == "bound"
            assert app.screen.preview.logical_path == "module.py"
            await pilot.press("escape")
            app.screen.query_one("#test-coverage", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, StaticProjectionBrowser)
            assert app.screen.request.query == row.detail.target_subject.subject_id
            assert app.screen.page.eligible > 0
            await pilot.press("escape", "escape", "ctrl+c")

    asyncio.run(exercise())


def test_index_test_target_coverage_requires_no_source(investigation, tmp_path):
    root, _, bound, _ = investigation
    row = _target(bound)
    manifest = build_browse_index(ReportViewService(bound), tmp_path / "index")
    root.rename(tmp_path / "source-unavailable")
    index = BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256)
    loaded = index.page(BrowseRequest(table=BrowseTable.TEST_IMPACT, query=row.identity)).records[0]
    assert loaded == row
    assert investigate_test(loaded) == investigate_test(row)


def test_browser_test_target_and_coverage_navigation(investigation):
    playwright = pytest.importorskip("playwright.sync_api")
    root, _, bound, _ = investigation
    row = _target(bound)
    with (
        served(create_server(ReportViewService(bound), source_root=root, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 768, "height": 1024})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            reveal_navigation(page, "Test impact").click()
            page.locator(f'[data-kind="test"][data-identity="{row.identity}"]').click()
            content = page.locator("#evidence-content")
            playwright.expect(content).to_contain_text(
                "These counts do not prove this test executed"
            )
            page.get_by_role("button", name="Preview application source").click()
            playwright.expect(content.locator("pre")).to_contain_text("def calculate")
            page.get_by_role("button", name="Preview test source").click()
            playwright.expect(content).to_contain_text("assert calculate(1)")
            page.get_by_role("button", name="Coverage artifact evidence").click()
            playwright.expect(content).to_contain_text("coverage-artifact")
        finally:
            browser.close()


def test_test_coverage_bounds_and_malformed_choices(investigation):
    from pydantic import ValidationError

    from code_analysis.test_browsing import (
        TestSourceChoice as SourceChoice,
    )
    from code_analysis.test_browsing import (
        TestTargetCoverage as TargetCoverage,
    )

    _, _, bound, _ = investigation
    row = _target(bound)
    source = investigate_test(row).sources[0]
    with pytest.raises(ValidationError, match="identity disagrees"):
        SourceChoice.model_validate({**source.model_dump(), "source_sha256": "0" * 64})
    original = row.detail.target_coverage
    with pytest.raises(ValidationError, match="exact application subject"):
        type(row.detail).model_validate({**row.detail.model_dump(), "target_subject": None})
    with pytest.raises(ValidationError, match="full retained page"):
        TargetCoverage.model_validate({**original.model_dump(), "omitted": 1})
    observation = next(
        item for item in bound.coverage_observations if item.subject_id == original.subject_id
    )
    observations = tuple(
        observation.model_copy(
            update={"coverage_observation_id": f"coverage-observation-{number:020x}"}
        )
        for number in range(30)
    )
    augmented = bound.model_copy(update={"coverage_observations": observations})
    coverage = _target(augmented).detail.target_coverage
    assert len(coverage.observations) == 25 and coverage.omitted == 5
