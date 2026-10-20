"""A delayed source failure must not follow a changed comparison row."""

import asyncio
import threading

import pytest
from test_runtime_comparison_source_browsing import GOLDEN, build_comparison_index

from code_analysis.browse_index import BrowseIndex
from code_analysis.profile_models import FrameClassification
from code_analysis.runtime_source_browse import RuntimeSourceUnavailable
from code_analysis.ui_runtime import RuntimeViewService


def test_delayed_error_does_not_open_for_another_row(ordinary_report, tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import DataTable

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser

    original = RuntimeViewService.load(GOLDEN).profile.frames[0]
    frames = tuple(
        original.model_copy(
            update={
                "frame_id": f"profile-frame-{number:020x}",
                "path": "alpha/a.py",
                "line": number,
                "function": f"function_{number}",
                "classification": FrameClassification.PROJECT,
                "artifact_sha256": None,
            }
        )
        for number in (1, 2)
    )
    _, index, _ = build_comparison_index(
        ordinary_report, tmp_path, baseline_frames=frames, candidate_frames=frames
    )
    entered, release = threading.Event(), threading.Event()

    def delayed_failure(self, identity, side):
        entered.set()
        if not release.wait(5):
            raise AssertionError("test did not release the held source lookup")
        raise RuntimeSourceUnavailable("held failure for the previous row")

    monkeypatch.setattr(BrowseIndex, "runtime_comparison_source", delayed_failure)

    async def exercise():
        app = AnalysisTerminal(index, source_root=tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+r")
            await app.workers.wait_for_complete()
            await pilot.click("#runtime-compare")
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, RuntimeComparisonBrowser)
            try:
                await pilot.press("c")
                assert await asyncio.to_thread(entered.wait, 2)
                screen.query_one("#runtime-comparison-records", DataTable).move_cursor(row=1)
                await pilot.pause()
            finally:
                release.set()
            await app.workers.wait_for_complete()
            assert app.screen is screen

    asyncio.run(exercise())
