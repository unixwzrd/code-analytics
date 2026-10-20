from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime

import pytest
from conftest import FIXTURES

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import load_config_file
from code_analysis.decision_models import MaintainerDecision
from code_analysis.decisions import set_decision
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName
from code_analysis.service import scan_refactor
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import prepare_workspace


@pytest.fixture
def decision_index(tmp_path):
    """A prepared index and owned ledger survive after the scanned source is gone."""
    source = tmp_path / "source"
    shutil.copytree(FIXTURES / "proposal_project", source)
    context = discover_context(source, load_config_file(FIXTURES / "proposal-config.toml"))
    report = scan_refactor(context)
    workspace = prepare_workspace(context, explicit_path=tmp_path / "workspace")
    manifest = build_browse_index(ReportViewService(report), tmp_path / "index")
    shutil.rmtree(source)
    assert not source.exists()
    return (
        report,
        workspace,
        BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256),
    )


def _record(report, workspace, decision=MaintainerDecision.REJECTED_ABSTRACTION_COST):
    return set_decision(
        workspace,
        report,
        candidate_id=report.refactor_candidates[0].candidate_id,
        proposal_id=None,
        decision=decision,
        reason="Synthetic terminal review evidence.",
        recorded_at=datetime(2026, 9, 19, tzinfo=UTC),
    )


def test_terminal_decisions_are_explicit_source_free_and_stale_safe(decision_index):
    pytest.importorskip("textual")
    from textual.widgets import Button, Input, Select, SelectionList, Static

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_candidate_policy import CandidatePolicyEditor
    from code_analysis.tui_export import ExportPage
    from code_analysis.tui_static_projections import StaticProjectionBrowser

    report, workspace, index = decision_index
    rejected = _record(report, workspace)

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=25, workspace=workspace.root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, StaticProjectionBrowser)
            screen.query_one("#static-projection-name", Select).value = (
                ProjectionName.CANDIDATE_WORKLIST.value
            )
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None and screen.page.eligible == 2
            assert screen.decisions is None
            assert screen.request.decision_ledger_sha256 is None
            # Both rows of controls stay reachable in the supported 80x24 viewport.
            assert screen.query_one("#static-projection-records").region.height >= 3
            for control in (
                "#static-projection-previous",
                "#static-projection-next",
                "#static-projection-policy",
                "#static-projection-attach-decisions",
                "#static-projection-clear-decisions",
                "#static-projection-decision-identity",
                "#static-projection-export",
                "#static-projection-close",
            ):
                region = screen.query_one(control, Button).region
                assert region.x >= 0 and region.x + region.width <= app.size.width
                assert region.y >= 0 and region.y + region.height <= app.size.height
            # Tab stays in the terminal's focus order; projection details use F2
            # so Ctrl+I remains available as terminal input rather than a modal key.
            for _ in range(4):
                await pilot.press("tab")
                assert app.screen is screen
            await pilot.press("f2")
            await pilot.pause()
            assert isinstance(app.screen, RowDetails)
            assert "source_truncated" in app.screen.detail
            await pilot.press("escape")
            await pilot.pause()

            # A policy dialog cannot manufacture or silently attach a ledger.
            await pilot.press("ctrl+f")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, CandidatePolicyEditor)
            assert editor.query_one("#candidate-policy-decisions", SelectionList).disabled
            await pilot.press("escape")
            await pilot.pause()

            screen.query_one("#static-projection-attach-decisions", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.decisions is not None
            assert screen.page is not None and screen.page.eligible == 1
            assert screen.request.decision_ledger_sha256 == screen.decisions.ledger_sha256
            assert rejected.candidate_id not in {row.row_id for row in screen.page.records}
            assert not screen.query_one("#static-projection-decision-identity", Button).disabled
            screen.query_one("#static-projection-decision-identity", Button).press()
            await pilot.pause()
            identity = app.screen
            assert screen.decisions.ledger_sha256 in identity.detail
            assert screen.decisions.report_sha256 in identity.detail
            await pilot.press("escape")
            await pilot.pause()

            # Explicit selection can inspect a suppressed rejection.
            await pilot.press("ctrl+f")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, CandidatePolicyEditor)
            decisions = editor.query_one("#candidate-policy-decisions", SelectionList)
            assert not decisions.disabled
            decisions.select(rejected.decision.value)
            await pilot.press("ctrl+s")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None
            assert [row.row_id for row in screen.page.records] == [rejected.candidate_id]

            # Clear drops both the digest pair and the decision filter.
            screen.query_one("#static-projection-clear-decisions", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.decisions is None
            assert screen.request.decision_ledger_sha256 is None
            assert screen.request.decision_report_sha256 is None
            assert not screen.request.candidate_selection.candidate_decisions
            assert screen.page is not None and screen.page.eligible == 2

            # The export re-reads the owned ledger; a changed snapshot fails closed.
            screen.query_one("#static-projection-attach-decisions", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("ctrl+e")
            await pilot.pause()
            export = app.screen
            assert isinstance(export, ExportPage)
            _record(report, workspace, MaintainerDecision.ACCEPTED)
            export.query_one("#export-name", Input).value = "stale-ledger"
            export.query_one("#export-write", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert export.exported_path is None
            assert "reselect" in str(export.query_one("#export-status", Static).content)
            await pilot.press("escape")
            await pilot.pause()

            # A later page read also invalidates stale results, rather than using
            # the newly written decision under the old request digest.
            screen.load_page()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.decisions is None
            assert screen.page is None
            assert screen.query_one("#static-projection-export", Button).disabled
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())


def test_terminal_missing_ledger_never_creates_or_attaches_one(decision_index):
    pytest.importorskip("textual")
    from textual.widgets import Button, Select, Static

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_static_projections import StaticProjectionBrowser

    _report, workspace, index = decision_index
    assert not (workspace.decisions / "ledger.json").exists()

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=25, workspace=workspace.root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, StaticProjectionBrowser)
            screen.query_one("#static-projection-name", Select).value = (
                ProjectionName.CANDIDATE_WORKLIST.value
            )
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen.query_one("#static-projection-attach-decisions", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.decisions is None and screen.page is None
            assert (
                "unavailable"
                in str(screen.query_one("#static-projection-decisions", Static).content).lower()
            )
            assert not (workspace.decisions / "ledger.json").exists()
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())
