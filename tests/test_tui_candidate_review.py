from __future__ import annotations

import asyncio
import shutil
import threading
from datetime import UTC, datetime

import pytest
from conftest import FIXTURES

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import load_config_file
from code_analysis.decision_models import MaintainerDecision
from code_analysis.decisions import set_decision
from code_analysis.project import discover_context
from code_analysis.service import scan_refactor
from code_analysis.ui_models import BrowseFacets
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import prepare_workspace


@pytest.fixture
def review_index(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(FIXTURES / "proposal_project", source)
    context = discover_context(source, load_config_file(FIXTURES / "proposal-config.toml"))
    report = scan_refactor(context)
    workspace = prepare_workspace(context, explicit_path=tmp_path / "workspace")
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), destination)
    shutil.rmtree(source)
    assert not source.exists()
    return report, workspace, BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def _decision(report, workspace, decision=MaintainerDecision.REJECTED_ABSTRACTION_COST):
    return set_decision(
        workspace,
        report,
        candidate_id=report.refactor_candidates[0].candidate_id,
        proposal_id=None,
        decision=decision,
        reason="Synthetic terminal candidate review decision.",
        recorded_at=datetime(2026, 9, 19, tzinfo=UTC),
    )


def test_primary_candidate_inventory_opens_separate_worklist_and_indexed_packet(review_index):
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_candidate import CandidatePacket
    from code_analysis.tui_static_projections import StaticProjectionBrowser

    report, workspace, index = review_index
    _decision(report, workspace, MaintainerDecision.ACCEPTED)

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=25, workspace=workspace.root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("f4")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.selection.table.value == "candidates"
            review = app.query_one("#review-worklist", Button)
            assert review.display and not review.disabled
            assert review.region.x + review.region.width <= app.size.width

            # The inventory may be scoped independently; those filters never become
            # an implicit worklist policy.
            app.selection = app.selection.model_copy(
                update={
                    "query": "inventory-only",
                    "facets": BrowseFacets(module_prefix="package"),
                }
            )
            inventory_selection = app.selection
            review.press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            worklist = app.screen
            assert isinstance(worklist, StaticProjectionBrowser)
            assert worklist.request.name.value == "candidate-worklist"
            assert worklist.request.query == ""
            assert worklist.request.candidate_selection is None
            worklist.query_one("#static-projection-attach-decisions", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert worklist.decisions is not None
            request_before = worklist.request
            decisions_before = worklist.decisions
            rows_before = tuple(row.row_id for row in worklist.page.records)

            # Enter uses the exact saved row ID and the existing packet continuation.
            assert worklist.query_one("#static-projection-records", DataTable).row_count
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            packet = app.screen
            assert isinstance(packet, CandidatePacket)
            assert packet.page.candidate.candidate_id == rows_before[0]
            assert packet.page == index.candidate_packet(rows_before[0])
            # CandidatePacket owns its pre-existing continuation controls; closing it
            # cannot replace the worklist request or page.
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is worklist
            assert worklist.request == request_before
            assert worklist.decisions == decisions_before
            assert tuple(row.row_id for row in worklist.page.records) == rows_before
            await pilot.press("escape")
            await pilot.pause()
            assert app.selection == inventory_selection
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_worklist_queued_packet_cannot_escape_the_visible_page(review_index, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Button, Input

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_candidate import CandidatePacket
    from code_analysis.tui_static_projections import StaticProjectionBrowser

    _report, workspace, index = review_index
    original = BrowseIndex.candidate_packet
    started = threading.Event()
    release = threading.Event()

    def delayed_packet(self, identity: str, *, offset: int = 0):
        started.set()
        assert release.wait(5)
        return original(self, identity, offset=offset)

    monkeypatch.setattr(BrowseIndex, "candidate_packet", delayed_packet)

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=25, workspace=workspace.root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("f4")
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.query_one("#review-worklist", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            worklist = app.screen
            assert isinstance(worklist, StaticProjectionBrowser)
            await pilot.press("enter")
            await asyncio.to_thread(started.wait, 5)
            # A changed search invalidates the visible page and cancels the queued
            # packet task before its delayed index read is allowed to complete.
            worklist.query_one("#static-projection-search", Input).value = "no-match"
            release.set()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.screen is worklist
            assert not isinstance(app.screen, CandidatePacket)
            assert worklist.page is None
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())


def test_worklist_packet_rejects_stale_attached_ledger_without_source_reload(review_index):
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_candidate import CandidatePacket
    from code_analysis.tui_static_projections import StaticProjectionBrowser

    report, workspace, index = review_index
    _decision(report, workspace)

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=25, workspace=workspace.root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("f4")
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.query_one("#review-worklist", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            worklist = app.screen
            assert isinstance(worklist, StaticProjectionBrowser)
            worklist.query_one("#static-projection-attach-decisions", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert worklist.decisions is not None and worklist.page is not None

            # This changes only the owned synthetic ledger after the explicit
            # attachment. Packet selection must not reuse it under the old hash.
            _decision(report, workspace, MaintainerDecision.ACCEPTED)
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.screen is worklist
            assert not isinstance(app.screen, CandidatePacket)
            assert worklist.decisions is None and worklist.page is None
            assert worklist.query_one("#static-projection-records", DataTable).row_count == 0
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())


def test_primary_review_action_is_absent_without_a_prepared_candidate_worklist(
    ordinary_report, tmp_path
):
    pytest.importorskip("textual")
    from textual.widgets import Button

    from code_analysis.browse_index_build import build_browse_index
    from code_analysis.tui_app import AnalysisTerminal

    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), destination)
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=25)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("f4")
            await pilot.pause()
            await app.workers.wait_for_complete()
            review = app.query_one("#review-worklist", Button)
            assert not review.display and review.disabled
            await pilot.press("ctrl+c")

    asyncio.run(exercise())
