"""Real-browser checks for explicit read-only decision-ledger selection."""

from __future__ import annotations

import hashlib
import json
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
def decision_evidence(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[AnalysisReport, WorkspacePaths, str]:
    root = tmp_path_factory.mktemp("web-decision-selection")
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
        reason="Behavior differs in the retained evidence.",
        recorded_at=datetime(2026, 9, 19, tzinfo=UTC),
    )
    retained = AnalysisReport.model_validate_json(report.model_dump_json())
    shutil.rmtree(source)
    return retained, workspace, candidate.candidate_id


@contextmanager
def _server(report: AnalysisReport, token: str, workspace: WorkspacePaths | None) -> Iterator[str]:
    server = create_server(
        ReportViewService(report),
        token=token,
        workspace=workspace.root if workspace is not None else None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/?token={token}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _open_worklist(page) -> None:
    page.locator(".cards").wait_for()
    toggle = page.locator(".lui-shell__menu")
    if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
        toggle.click()
    reveal_navigation(page, "Explore evidence").click()
    page.get_by_label("Evidence view", exact=True).select_option("candidate-worklist")


def _show(page) -> None:
    page.get_by_role("button", name="Show evidence", exact=True).click()
    page.get_by_role("heading", name="Actionable refactor candidate worklist").wait_for()


def test_decision_snapshot_is_explicit_bound_filterable_downloaded_and_clearable(
    decision_evidence: tuple[AnalysisReport, WorkspacePaths, str],
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report, workspace, candidate_id = decision_evidence
    expected_ledger = ledger_sha256(load_ledger(workspace))
    expected_report = hashlib.sha256(canonical_json(report).encode()).hexdigest()
    with (
        _server(report, "decision-browser", workspace) as url,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            download_urls: list[str] = []
            page.on(
                "request",
                lambda request: (
                    download_urls.append(request.url) if "/api/download?" in request.url else None
                ),
            )
            page.goto(url)
            _open_worklist(page)

            decisions = page.get_by_label("Decision states")
            assert decisions.is_disabled()
            _show(page)
            assert page.get_by_text(candidate_id, exact=True).count() == 1

            attach = page.get_by_role("button", name="Use workspace decisions", exact=True)
            attach.focus()
            page.keyboard.press("Enter")
            status = page.locator("#candidate-decision-status")
            playwright.expect(status).to_contain_text("1 decision records")
            playwright.expect(status).to_contain_text(expected_ledger)
            playwright.expect(status).to_contain_text(expected_report)
            assert not decisions.is_disabled()

            _show(page)
            assert page.get_by_text(candidate_id, exact=True).count() == 0

            decisions.select_option("rejected-behavior-change")
            _show(page)
            assert page.get_by_text(candidate_id, exact=True).count() == 1
            with page.expect_download() as transfer:
                page.get_by_role("button", name="Download JSON", exact=True).click()
            payload = json.loads(Path(transfer.value.path()).read_text(encoding="utf-8"))
            query = parse_qs(urlsplit(download_urls[-1]).query)
            assert query["decision_ledger_sha256"] == [expected_ledger]
            assert query["decision_report_sha256"] == [expected_report]
            assert query["candidate_decisions"] == ["rejected-behavior-change"]
            ledger_input = next(
                item for item in payload["inputs"] if item["role"] == "decision-ledger"
            )
            assert ledger_input["report_sha256"] == expected_ledger

            page.route(
                "**/api/candidate-ledger*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "available": False,
                            "report_sha256": expected_report,
                            "ledger_sha256": None,
                            "record_count": 0,
                            "unavailable_reason": "The replacement snapshot is unavailable.",
                        }
                    ),
                ),
            )
            attach.click()
            playwright.expect(page.locator("#projection-result")).to_contain_text("Options changed")
            assert page.get_by_role("button", name="Download JSON", exact=True).count() == 0
            playwright.expect(status).to_have_text("The replacement snapshot is unavailable.")
            assert decisions.is_disabled()
            page.unroute("**/api/candidate-ledger*")
            attach.click()
            playwright.expect(status).to_contain_text(expected_ledger)

            page.get_by_role("button", name="Clear decision selection", exact=True).click()
            playwright.expect(status).to_have_text("Workspace decisions are not attached.")
            assert decisions.is_disabled()
            _show(page)
            assert page.get_by_text(candidate_id, exact=True).count() == 1

            attach.click()
            playwright.expect(status).to_contain_text(expected_ledger)
            page.get_by_label("Evidence view", exact=True).select_option("modules")
            page.get_by_label("Evidence view", exact=True).select_option("candidate-worklist")
            playwright.expect(status).to_have_text("Workspace decisions are not attached.")
            assert decisions.is_disabled()
            assert decisions.input_value() == ""
        finally:
            browser.close()


def test_changed_snapshot_fails_closed_and_requires_reselection(
    decision_evidence: tuple[AnalysisReport, WorkspacePaths, str],
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report, workspace, candidate_id = decision_evidence
    with (
        _server(report, "decision-stale", workspace) as url,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url)
            _open_worklist(page)
            page.get_by_role("button", name="Use workspace decisions", exact=True).click()
            status = page.locator("#candidate-decision-status")
            playwright.expect(status).to_contain_text("decision records")

            observation = next(
                item for item in report.candidate_observations if item.candidate_id == candidate_id
            )
            set_decision(
                workspace,
                report,
                candidate_id=candidate_id,
                proposal_id=observation.proposal_ids[0] if observation.proposal_ids else None,
                decision=MaintainerDecision.DEFERRED_PREREQUISITE,
                reason="A prerequisite remains unresolved.",
                recorded_at=datetime(2026, 9, 20, tzinfo=UTC),
            )
            page.get_by_role("button", name="Show evidence", exact=True).click()
            playwright.expect(page.locator("#projection-result")).to_contain_text(
                "selected decision snapshot is stale"
            )
            playwright.expect(status).to_contain_text("Select Use workspace decisions again")
            assert page.get_by_label("Decision states").is_disabled()
            assert page.get_by_role("button", name="Download JSON", exact=True).count() == 0
        finally:
            browser.close()


def test_unavailable_ledger_is_not_attached_and_mobile_controls_fit(
    tmp_path: Path,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    source = tmp_path / "source"
    shutil.copytree(FIXTURES / "proposal_project", source)
    context = discover_context(source, load_config_file(FIXTURES / "proposal-config.toml"))
    report = scan_refactor(context)
    workspace = prepare_workspace(context, explicit_path=tmp_path / "workspace")
    shutil.rmtree(source)
    with (
        _server(report, "decision-unavailable", workspace) as url,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(url)
            _open_worklist(page)
            button = page.get_by_role("button", name="Use workspace decisions", exact=True)
            button.focus()
            page.keyboard.press("Enter")
            playwright.expect(page.locator("#candidate-decision-status")).to_have_text(
                "The selected workspace has no saved decision ledger."
            )
            assert page.get_by_label("Decision states").is_disabled()
            assert page.get_by_role("button", name="Clear decision selection").is_disabled()
            assert page.evaluate(
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
            )
            bounds = page.locator(".candidate-decision-selection").bounding_box()
            assert bounds is not None
            assert bounds["x"] >= 0
            assert bounds["x"] + bounds["width"] <= 390
        finally:
            browser.close()
