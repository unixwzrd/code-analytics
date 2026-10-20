"""Real-browser checks for candidate-worklist policy controls."""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from browser_navigation import reveal_navigation

from code_analysis.config import load_config_file
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.report_models import AnalysisReport, RefactorCandidateKind
from code_analysis.service import scan_refactor
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def candidate_report(tmp_path_factory: pytest.TempPathFactory) -> AnalysisReport:
    root = tmp_path_factory.mktemp("candidate-explorer")
    source = root / "source"
    shutil.copytree(FIXTURES / "proposal_project", source)
    report = scan_refactor(
        discover_context(source, load_config_file(FIXTURES / "proposal-config.toml"))
    )
    retained = AnalysisReport.model_validate_json(report.model_dump_json())
    shutil.rmtree(source)
    assert retained.refactor_candidates
    return retained


@contextmanager
def _server(report: AnalysisReport, token: str) -> Iterator[str]:
    server = create_server(ReportViewService(report), token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _open_explorer(page) -> None:
    page.locator(".cards").wait_for()
    toggle = page.locator(".lui-shell__menu")
    if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
        toggle.click()
    reveal_navigation(page, "Explore evidence").click()
    page.get_by_label("Evidence view", exact=True).select_option("candidate-worklist")


def test_candidate_controls_apply_authoritative_filters_and_preserve_download_identity(
    candidate_report: AnalysisReport,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    expected = build_projection(
        candidate_report,
        ProjectionName.CANDIDATE_WORKLIST,
        candidate_module_patterns=("package.combine_*", "package.work"),
        candidate_kinds=(RefactorCandidateKind.MODULE_COMBINATION,),
        candidate_states=("blocked",),
        cross_module_only=True,
        production_only=True,
        min_shared_dimensions=4,
        prefer_existing_owner=True,
        max_records=100,
        max_nodes=50,
        max_edges=100,
    )
    with (
        _server(candidate_report, "candidate-browser") as url,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            projection_urls: list[str] = []
            page.on(
                "request",
                lambda request: (
                    projection_urls.append(request.url)
                    if "/api/projection?" in request.url
                    else None
                ),
            )
            page.goto(url)
            _open_explorer(page)

            controls = page.locator("#candidate-projection-controls")
            playwright.expect(controls).to_be_visible()
            page.get_by_label("Include module patterns").fill(
                "package.work\npackage.combine_*\npackage.work\n"
            )
            page.get_by_label("Candidate states").select_option("blocked")
            page.get_by_label("Candidate kinds").select_option("module-combination")
            page.get_by_label("Minimum shared dimensions").fill("4")
            page.get_by_label("Cross-module candidates only").check()
            page.get_by_label("Production subjects only").check()
            page.get_by_label("Prefer an existing recorded owner").check()
            page.get_by_role("button", name="Show evidence", exact=True).click()
            page.get_by_role("heading", name="Actionable refactor candidate worklist").wait_for()

            assert page.locator("#projection-result tbody tr").count() == len(expected.rows) == 1
            assert page.locator("#projection-result tbody tr td").first.text_content() == (
                expected.rows[0].cells[0].string_value
            )
            query = parse_qs(urlsplit(projection_urls[-1]).query)
            assert query["candidate_module_patterns"] == ["package.combine_*", "package.work"]
            assert query["candidate_states"] == ["blocked"]
            assert query["candidate_kinds"] == ["module-combination"]
            assert query["cross_module_only"] == ["true"]
            assert query["production_only"] == ["true"]
            assert query["min_shared_dimensions"] == ["4"]
            assert query["prefer_existing_owner"] == ["true"]

            with page.expect_download() as transfer:
                page.get_by_role("button", name="Download JSON", exact=True).click()
            downloaded = json.loads(Path(transfer.value.path()).read_text(encoding="utf-8"))
            assert downloaded["projection_id"] == expected.projection_id
            assert downloaded["selection"]["candidate_module_patterns"] == [
                "package.combine_*",
                "package.work",
            ]
            assert downloaded["selection"]["candidate_states"] == ["blocked"]
            assert downloaded["selection"]["candidate_kinds"] == ["module-combination"]
            assert downloaded["selection"]["prefer_existing_owner"] is True

            page.get_by_label("Exclude module patterns").fill("package.combine_*")
            playwright.expect(page.locator("#projection-result")).to_contain_text("Options changed")
            assert page.get_by_role("button", name="Download JSON", exact=True).count() == 0
        finally:
            browser.close()


def test_candidate_controls_reset_disable_and_remain_keyboard_usable_at_mobile_width(
    candidate_report: AnalysisReport,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        _server(candidate_report, "candidate-mobile") as url,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            projection_urls: list[str] = []
            page.on(
                "request",
                lambda request: (
                    projection_urls.append(request.url)
                    if "/api/projection?" in request.url
                    else None
                ),
            )
            page.goto(url)
            _open_explorer(page)
            controls = page.locator("#candidate-projection-controls")
            page.get_by_label("Include module patterns").fill("package.combine_*")
            page.get_by_label("Candidate states").select_option("reviewable-incomplete")
            page.get_by_label("Candidate kinds").select_option("responsibility-extraction")
            page.get_by_label("Production subjects only").check()

            reset = page.get_by_role("button", name="Reset candidate filters", exact=True)
            reset.focus()
            page.keyboard.press("Enter")
            assert page.get_by_label("Include module patterns").input_value() == ""
            assert (
                page.get_by_label("Candidate states").evaluate(
                    "element => [...element.selectedOptions].map(option => option.value)"
                )
                == []
            )
            assert (
                page.get_by_label("Candidate kinds").evaluate(
                    "element => [...element.selectedOptions].map(option => option.value)"
                )
                == []
            )
            assert not page.get_by_label("Production subjects only").is_checked()

            page.get_by_label("Candidate states").select_option("blocked")
            minimum = page.get_by_label("Minimum shared dimensions")
            minimum.fill("4")
            minimum.focus()
            page.keyboard.press("Enter")
            page.get_by_role("heading", name="Actionable refactor candidate worklist").wait_for()
            assert "candidate_states=blocked" in projection_urls[-1]

            page.get_by_label("Evidence view", exact=True).select_option("modules")
            playwright.expect(controls).to_be_hidden()
            assert controls.evaluate("element => element.disabled")
            page.get_by_role("button", name="Show evidence", exact=True).click()
            page.get_by_role("heading", name="Module metrics").wait_for()
            assert "candidate_" not in projection_urls[-1]
            assert "cross_module_only" not in projection_urls[-1]
            assert "production_only" not in projection_urls[-1]

            assert page.evaluate(
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
            )
            page.get_by_label("Evidence view", exact=True).select_option("candidate-worklist")
            playwright.expect(controls).to_be_visible()
            bounds = controls.bounding_box()
            assert bounds is not None
            assert bounds["x"] >= 0
            assert bounds["x"] + bounds["width"] <= 390
        finally:
            browser.close()
