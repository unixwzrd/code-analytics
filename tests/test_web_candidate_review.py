"""Browser checks for the primary candidate inventory review bridge."""

from __future__ import annotations

import hashlib
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from browser_navigation import reveal_navigation

from code_analysis.config import load_config_file
from code_analysis.decision_models import MaintainerDecision
from code_analysis.decisions import ledger_sha256, load_ledger, set_decision
from code_analysis.project import discover_context
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json
from code_analysis.service import scan_refactor
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import WorkspacePaths, prepare_workspace

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def review_evidence(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[AnalysisReport, WorkspacePaths, str]:
    root = tmp_path_factory.mktemp("web-candidate-review")
    source = root / "source"
    shutil.copytree(FIXTURES / "proposal_project", source)
    context = discover_context(source, load_config_file(FIXTURES / "proposal-config.toml"))
    report = scan_refactor(context)
    workspace = prepare_workspace(context, explicit_path=root / "workspace")
    candidate = report.refactor_candidates[0]
    observation = next(
        item
        for item in report.candidate_observations
        if item.candidate_id == candidate.candidate_id
    )
    set_decision(
        workspace,
        report,
        candidate_id=candidate.candidate_id,
        proposal_id=observation.proposal_ids[0] if observation.proposal_ids else None,
        decision=MaintainerDecision.REJECTED_BEHAVIOR_CHANGE,
        reason="A synthetic review records behavior divergence.",
        recorded_at=datetime(2026, 9, 19, tzinfo=UTC),
    )
    retained = AnalysisReport.model_validate_json(report.model_dump_json())
    shutil.rmtree(source)
    return retained, workspace, candidate.candidate_id


@contextmanager
def _server(report: AnalysisReport, workspace: WorkspacePaths, token: str) -> Iterator[str]:
    server = create_server(ReportViewService(report), token=token, workspace=workspace.root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _open_candidates(page) -> None:
    page.locator(".cards").wait_for()
    toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
    if toggle.is_visible():
        toggle.click()
    reveal_navigation(page, "Candidates").click()
    page.get_by_role("heading", name="Ranked refactor review", exact=True).wait_for()


def test_candidate_inventory_opens_bound_worklist_and_returns_without_losing_facets(
    review_evidence: tuple[AnalysisReport, WorkspacePaths, str],
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report, workspace, candidate_id = review_evidence
    with (
        _server(report, workspace, "candidate-review") as url,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            observed_urls: list[str] = []
            page.on(
                "request",
                lambda request: (
                    observed_urls.append(request.url)
                    if "/api/evidence?" in request.url or "/api/candidate?" in request.url
                    else None
                ),
            )
            page.goto(url)
            _open_candidates(page)
            page.get_by_label("Language", exact=True).select_option("python")
            page.get_by_label("Logical module prefix", exact=True).fill("package")
            page.get_by_role("button", name="Apply scope filters", exact=True).click()
            page.get_by_role("button", name="Review ranked worklist", exact=True).focus()
            page.keyboard.press("Enter")
            page.get_by_label("Evidence view", exact=True).wait_for()
            assert (
                page.get_by_label("Evidence view", exact=True).input_value() == "candidate-worklist"
            )
            assert (
                page.get_by_text(
                    "Inventory search and participant facets are not transferred", exact=False
                ).count()
                == 1
            )

            page.locator("#candidate-projection-controls").wait_for()
            decisions = page.locator('select[name="candidate_decisions"]')
            assert decisions.is_disabled()
            page.get_by_role("button", name="Use workspace decisions", exact=True).click()
            page.locator("#candidate-decision-status").get_by_text(
                "decision records", exact=False
            ).wait_for()
            decisions.select_option("rejected-behavior-change")
            page.get_by_role("button", name="Show evidence", exact=True).click()
            page.get_by_role(
                "heading", name="Actionable refactor candidate worklist", exact=True
            ).wait_for()
            inspect = page.locator("#projection-result [data-candidate-review-id]")
            assert inspect.count() == 1
            assert inspect.first.get_attribute("data-candidate-review-id") == candidate_id
            inspect.focus()
            page.keyboard.press("Enter")
            assert page.get_by_role("dialog").is_visible()
            page.get_by_role("heading", name=candidate_id, exact=True).wait_for()
            page.get_by_text("Affected subjects", exact=True).wait_for()
            page.keyboard.press("Escape")
            assert not page.get_by_role("dialog").is_visible()
            assert inspect.evaluate("element => document.activeElement === element")
            assert decisions.input_value() == "rejected-behavior-change"
            assert page.locator("#projection-result [data-candidate-review-id]").count() == 1
            expected_report = hashlib.sha256(canonical_json(report).encode("utf-8")).hexdigest()
            expected_ledger = ledger_sha256(load_ledger(workspace))
            for observed in observed_urls:
                values = parse_qs(urlsplit(observed).query)
                assert values["report_sha256"] == [expected_report]
                assert values["decision_ledger_sha256"] == [expected_ledger]

            page.get_by_role("button", name="Return to candidate inventory", exact=True).click()
            page.get_by_role("heading", name="Ranked refactor review", exact=True).wait_for()
            assert page.get_by_label("Language", exact=True).input_value() == "python"
            assert page.get_by_label("Logical module prefix", exact=True).input_value() == "package"
            assert page.get_by_role("button", name="Review ranked worklist", exact=True).evaluate(
                "element => document.activeElement === element"
            )
        finally:
            browser.close()


def test_candidate_review_handles_mobile_render_change_and_invalid_saved_row_id(
    review_evidence: tuple[AnalysisReport, WorkspacePaths, str],
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report, workspace, _candidate_id = review_evidence
    with (
        _server(report, workspace, "candidate-review-invalid") as url,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(url)
            _open_candidates(page)
            page.get_by_role("button", name="Review ranked worklist", exact=True).click()
            page.get_by_label("Evidence view", exact=True).wait_for()
            assert page.evaluate(
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
            )

            def malformed_row_id(route) -> None:
                response = route.fetch()
                payload = response.json()
                payload["rows"][0]["row_id"] = " malformed saved row "
                route.fulfill(response=response, json=payload)

            page.route(
                "**/api/projection*",
                malformed_row_id,
            )
            page.get_by_role("button", name="Show evidence", exact=True).click()
            page.get_by_role(
                "heading", name="Actionable refactor candidate worklist", exact=True
            ).wait_for()
            page.get_by_text("Unavailable", exact=True).wait_for()
            assert all(
                item != " malformed saved row "
                for item in page.locator(
                    "#projection-result [data-candidate-review-id]"
                ).evaluate_all(
                    "elements => elements.map(element => element.dataset.candidateReviewId)"
                )
            )
            assert page.get_by_text("Unavailable", exact=True).count() == 1
            page.unroute("**/api/projection*")

            page.get_by_role("button", name="Show evidence", exact=True).click()
            page.get_by_role(
                "heading", name="Actionable refactor candidate worklist", exact=True
            ).wait_for()
            page.locator("#projection-result [data-candidate-review-id]").first.click()
            assert page.get_by_role("dialog").is_visible()
            page.keyboard.press("Escape")
            assert not page.get_by_role("dialog").is_visible()
            toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
            if toggle.is_visible():
                toggle.click()
            reveal_navigation(page, "Candidates").click()
            page.get_by_role("heading", name="Ranked refactor review", exact=True).wait_for()
        finally:
            browser.close()
