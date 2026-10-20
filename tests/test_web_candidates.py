from __future__ import annotations

import threading
from pathlib import Path

import pytest
from browser_navigation import reveal_navigation
from conftest import FIXTURES

from code_analysis.config import load_config_file
from code_analysis.project import discover_context
from code_analysis.projection_models import ProjectionSelection
from code_analysis.projections.candidates import build_candidate_worklist_projection
from code_analysis.reports.json_report import canonical_json
from code_analysis.service import scan_refactor
from code_analysis.ui_models import UICandidatePacket
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


@pytest.fixture(scope="module")
def candidate_report():
    return scan_refactor(
        discover_context(
            FIXTURES / "proposal_project", load_config_file(FIXTURES / "proposal-config.toml")
        )
    )


def test_candidate_packet_preserves_saved_evidence(candidate_report, tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(canonical_json(candidate_report))
    service = ReportViewService.load(path)
    identity = candidate_report.refactor_candidates[0].candidate_id
    packet = service.candidate_packet(identity)
    worklist = build_candidate_worklist_projection(
        candidate_report, ProjectionSelection(), {}, None
    )
    assert tuple(
        item.candidate_id for item in service.candidates(offset=0, limit=500).records
    ) == tuple(item.row_id for item in worklist.rows)
    assert packet.proposals and packet.owners and packet.qualification and packet.cost
    assert packet.proposals == tuple(
        item for item in candidate_report.refactor_proposals if item.candidate_id == identity
    )
    assert packet.cost in candidate_report.abstraction_cost_estimates
    assert packet.subjects
    assert all(item.value == 0 for item in packet.omitted)
    assert packet == service.candidate_packet(identity)
    bounded = service.candidate_packet(identity, limit=1)
    assert len(bounded.qualification) == 1
    assert (
        next(item.value for item in bounded.omitted if item.label == "qualification")
        == len(packet.qualification) - 1
    )
    with pytest.raises(ValueError, match="not present"):
        service.candidate_packet("not-a-candidate")
    with pytest.raises(ValueError, match="limit"):
        service.candidate_packet(identity, limit=51)
    with pytest.raises(ValueError, match="omission categories"):
        UICandidatePacket.model_validate({**packet.model_dump(), "omitted": ()})
    other = next(
        item for item in candidate_report.candidate_observations if item.candidate_id != identity
    )
    with pytest.raises(ValueError, match="different candidate"):
        UICandidatePacket.model_validate({**packet.model_dump(), "observation": other})


def test_candidate_packet_continuation_preserves_all_sections(candidate_report):
    service = ReportViewService(candidate_report)
    identity = candidate_report.refactor_candidates[0].candidate_id
    full = service.candidate_packet(identity, limit=50)
    pages = []
    offset = 0
    while True:
        page = service.candidate_packet(identity, limit=1, offset=offset)
        pages.append(page)
        assert page.candidate == full.candidate and page.cost == full.cost
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert len(pages) > 1
    for count in full.totals:
        assert tuple(item for page in pages for item in getattr(page, count.label)) == getattr(
            full, count.label
        )
    for offset in (True, -1, 1_000_001):
        with pytest.raises(ValueError):
            service.candidate_packet(identity, offset=offset)
    with pytest.raises(ValueError):
        service.candidate_packet(identity, limit=True)
    for change in ({"next_offset": None}, {"offset": True}, {"totals": ()}):
        with pytest.raises(ValueError):
            UICandidatePacket.model_validate({**pages[0].model_dump(), **change})


def test_candidate_packet_browser_review(candidate_report, monkeypatch) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    original = ReportViewService.candidate_packet
    monkeypatch.setattr(
        ReportViewService,
        "candidate_packet",
        lambda self, identity, *, limit=25, offset=0: original(
            self, identity, limit=1, offset=offset
        ),
    )
    server = create_server(ReportViewService(candidate_report), token="candidate-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=candidate-test")
                page.locator(".cards").wait_for(state="attached")
                reveal_navigation(page, "Candidates").click()
                page.get_by_role("button", name="Inspect candidate", exact=True).first.click()
                page.get_by_role("heading", name="Abstraction cost", exact=True).wait_for()
                for title in (
                    "Owner alternatives",
                    "Proposal decisions",
                    "Qualification requirements",
                    "Behavior differences",
                ):
                    assert page.get_by_role("heading", name=title, exact=True).is_visible()
                body = page.locator("#evidence-content").inner_text()
                assert "undefined" not in body
                assert "Net production LOC" in body
                assert "Rollback criteria" in body
                assert "Gate decisions" in body
                page.get_by_role("button", name="Next packet page", exact=True).click()
                playwright.expect(page.locator("#evidence-content")).to_contain_text(
                    "Section offset: 1."
                )
                page.get_by_role("button", name="Previous packet page", exact=True).click()
                playwright.expect(page.locator("#evidence-content")).to_contain_text(
                    "Section offset: 0."
                )
                assert not errors
                page.keyboard.press("Escape")
                assert not page.get_by_role("dialog").is_visible()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
