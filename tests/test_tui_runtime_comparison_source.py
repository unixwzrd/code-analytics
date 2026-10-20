from __future__ import annotations

import asyncio
import errno
import shutil
import time
from pathlib import Path

import pytest
from test_runtime_comparison_source_browsing import build_comparison_index

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.profile_models import ProfileReport
from code_analysis.profiling import build_profile_diff
from code_analysis.runtime_models import SourceBindingState
from code_analysis.runtime_source_browse import RuntimeSourceUnavailable
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService

GOLDEN_PROFILE = Path(__file__).parent / "golden" / "phase4-profile-report.json"


@pytest.fixture
def compared_index(ordinary_report, tmp_path):
    original = RuntimeViewService.load(GOLDEN_PROFILE).profile
    frames = tuple(
        original.frames[0].model_copy(
            update={
                "frame_id": f"profile-frame-{number:020x}",
                "function": f"function_{number:03d}",
            }
        )
        for number in range(131)
    )
    baseline = ProfileReport.model_validate(
        {
            **original.model_dump(),
            "frames": frames,
            "runtime_edges": (),
            "statistics": (),
        }
    )
    candidate = ProfileReport.model_validate(
        {
            **baseline.model_dump(),
            "profile_identity_sha256": "b" * 64,
            "source_report_sha256": None,
        }
    )
    delta = build_profile_diff(baseline, candidate, diagnostic_only=True)
    service = RuntimeViewService(baseline, candidate, delta)
    root = tmp_path / "paged-comparison"
    manifest = build_browse_index(ReportViewService(ordinary_report), root, runtime=service)
    return service, BrowseIndex.open(root, report_sha256=manifest.report_sha256)


def _source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    shutil.copytree(Path(__file__).parent / "fixtures" / "ordinary", root)
    return root


async def _open_comparison_screen(app, pilot, index, source_root: Path | None) -> None:
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser

    app.push_screen(RuntimeComparisonBrowser(index, app.evidence_closed, source_root=source_root))
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()
    assert app.screen.page is not None


@pytest.mark.parametrize("side", ("baseline", "candidate"))
def test_comparison_source_previews_both_bound_sides(ordinary_report, tmp_path, side):
    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser
    from code_analysis.tui_source import TerminalSource

    _, index, _ = build_comparison_index(ordinary_report, tmp_path)
    root = _source_tree(tmp_path)

    async def exercise():
        app = AnalysisTerminal(index, source_root=root)
        async with app.run_test(size=(80, 24)) as pilot:
            await _open_comparison_screen(app, pilot, index, root)
            screen = app.screen
            assert isinstance(screen, RuntimeComparisonBrowser)
            assert screen.page is not None and screen.page.report.records
            key = "b" if side == "baseline" else "c"
            await pilot.press(key)
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, TerminalSource)
            preview = app.screen.preview
            assert preview is not None
            assert preview.state == "bound"
            assert preview.logical_path == "alpha/a.py"
            assert app.screen.last_line - app.screen.first_line == 39
            assert preview.expected_sha256 == index.source_file("alpha/a.py").source_sha256
            await pilot.press("escape", "escape")

    asyncio.run(exercise())


def test_candidate_only_row_baseline_unavailable(ordinary_report, tmp_path):
    pytest.importorskip("textual")
    from test_runtime_comparison_source_browsing import GOLDEN, _project_frame, _source_hash

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser
    from code_analysis.tui_source import TerminalSource
    from code_analysis.ui_runtime import RuntimeViewService

    original = RuntimeViewService.load(GOLDEN).profile
    source = _source_hash(ordinary_report)
    only_baseline = _project_frame(
        original,
        path=source.path,
        line=1,
        function="only_baseline",
        frame_id="profile-frame-cccccccccccccccccccc",
    )
    only_candidate = only_baseline.model_copy(
        update={
            "frame_id": "profile-frame-dddddddddddddddddddd",
            "function": "only_candidate",
            "line": 3,
        }
    )
    _, index, delta = build_comparison_index(
        ordinary_report,
        tmp_path,
        baseline_frames=(only_baseline,),
        candidate_frames=(only_candidate,),
    )
    candidate_only = next(
        record for record in delta.records if record.candidate_total_seconds is not None
    )
    root = _source_tree(tmp_path)

    async def exercise():
        app = AnalysisTerminal(index, source_root=root)
        async with app.run_test(size=(80, 24)) as pilot:
            await _open_comparison_screen(app, pilot, index, root)
            screen = app.screen
            assert isinstance(screen, RuntimeComparisonBrowser)
            table = screen.query_one("#runtime-comparison-records")
            for position, row in enumerate(screen.page.report.records):
                if row.frame_identity == candidate_only.frame_identity:
                    table.move_cursor(row=position)
                    break
            await pilot.press("b")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, RowDetails)
            assert "no baseline frame" in app.screen.detail.lower()
            await pilot.press("escape")
            assert isinstance(app.screen, RuntimeComparisonBrowser)
            await pilot.press("c")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, TerminalSource)
            assert app.screen.preview is not None
            assert app.screen.first_line == 3
            await pilot.press("escape", "escape")

    asyncio.run(exercise())


def test_unbound_candidate_does_not_borrow_baseline(ordinary_report, tmp_path):
    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser
    from code_analysis.tui_source import TerminalSource

    _, index, _ = build_comparison_index(
        ordinary_report,
        tmp_path,
        candidate_binding=SourceBindingState.UNBOUND,
    )
    assert index.manifest.runtime.comparison.candidate_source_binding is SourceBindingState.UNBOUND
    root = _source_tree(tmp_path)

    async def exercise():
        app = AnalysisTerminal(index, source_root=root)
        async with app.run_test(size=(80, 24)) as pilot:
            await _open_comparison_screen(app, pilot, index, root)
            assert isinstance(app.screen, RuntimeComparisonBrowser)
            await pilot.press("b")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, TerminalSource)
            await pilot.press("escape")
            assert isinstance(app.screen, RuntimeComparisonBrowser)
            await pilot.press("c")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, RowDetails)
            assert "candidate source binding" in app.screen.detail
            await pilot.press("escape", "escape")

    asyncio.run(exercise())


def test_missing_source_root_reports_enodata(ordinary_report, tmp_path):
    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser

    _, index, _ = build_comparison_index(ordinary_report, tmp_path)

    async def exercise():
        app = AnalysisTerminal(index, source_root=None)
        async with app.run_test(size=(80, 24)) as pilot:
            await _open_comparison_screen(app, pilot, index, None)
            assert isinstance(app.screen, RuntimeComparisonBrowser)
            await pilot.press("b")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, RowDetails)
            assert "disabled" in app.screen.detail
            assert app.exit_status == errno.ENODATA
            await pilot.press("escape")

    asyncio.run(exercise())


def test_changed_on_disk_source_fails_hash_check(ordinary_report, tmp_path):
    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser
    from code_analysis.tui_source import TerminalSource

    _, index, _ = build_comparison_index(ordinary_report, tmp_path)
    root = _source_tree(tmp_path)
    (root / "alpha" / "a.py").write_text("mutated = True\n", encoding="utf-8")

    async def exercise():
        app = AnalysisTerminal(index, source_root=root)
        async with app.run_test(size=(80, 24)) as pilot:
            await _open_comparison_screen(app, pilot, index, root)
            assert isinstance(app.screen, RuntimeComparisonBrowser)
            await pilot.press("b")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, TerminalSource)
            assert app.screen.preview is not None
            assert app.screen.preview.state == "mismatch"
            assert not app.screen.preview.lines
            await pilot.press("escape", "escape")

    asyncio.run(exercise())


def test_stale_source_response_after_navigation(compared_index, tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser
    from code_analysis.tui_source import TerminalSource

    _, index = compared_index
    root = _source_tree(tmp_path)
    original = BrowseIndex.runtime_comparison_source

    def slow_source(self, frame_identity: str, side: str):
        time.sleep(0.35)
        return original(self, frame_identity, side)

    monkeypatch.setattr(BrowseIndex, "runtime_comparison_source", slow_source)

    async def exercise():
        app = AnalysisTerminal(index, source_root=root)
        async with app.run_test(size=(80, 24)) as pilot:
            await _open_comparison_screen(app, pilot, index, root)
            screen = app.screen
            assert isinstance(screen, RuntimeComparisonBrowser)
            assert screen.page is not None and screen.page.next_offset is not None
            await pilot.press("b")
            await pilot.press("ctrl+n")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, RuntimeComparisonBrowser)
            assert not isinstance(app.screen, TerminalSource)
            await pilot.press("escape")

    asyncio.run(exercise())


def test_stale_source_response_after_close(ordinary_report, tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser
    from code_analysis.tui_source import TerminalSource

    _, index, _ = build_comparison_index(ordinary_report, tmp_path)
    root = _source_tree(tmp_path)
    original = BrowseIndex.runtime_comparison_source

    def slow_source(self, frame_identity: str, side: str):
        time.sleep(0.35)
        return original(self, frame_identity, side)

    monkeypatch.setattr(BrowseIndex, "runtime_comparison_source", slow_source)

    async def exercise():
        app = AnalysisTerminal(index, source_root=root)
        async with app.run_test(size=(80, 24)) as pilot:
            await _open_comparison_screen(app, pilot, index, root)
            assert isinstance(app.screen, RuntimeComparisonBrowser)
            await pilot.press("b")
            await pilot.press("escape")
            await app.workers.wait_for_complete()
            assert not isinstance(app.screen, TerminalSource)

    asyncio.run(exercise())


def test_comparison_source_buttons_at_80x24(ordinary_report, tmp_path):
    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser
    from code_analysis.tui_source import TerminalSource

    _, index, _ = build_comparison_index(ordinary_report, tmp_path)
    root = _source_tree(tmp_path)

    async def exercise():
        app = AnalysisTerminal(index, source_root=root)
        async with app.run_test(size=(80, 24)) as pilot:
            await _open_comparison_screen(app, pilot, index, root)
            screen = app.screen
            assert isinstance(screen, RuntimeComparisonBrowser)
            assert screen.query_one("#runtime-comparison-baseline-source") is not None
            assert screen.query_one("#runtime-comparison-candidate-source") is not None
            assert await pilot.click("#runtime-comparison-candidate-source")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, TerminalSource)
            await pilot.press("escape", "escape")

    asyncio.run(exercise())


def test_runtime_browser_passes_source_root_to_comparison(ordinary_report, tmp_path):
    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser

    _, index, _ = build_comparison_index(ordinary_report, tmp_path)
    root = _source_tree(tmp_path)

    async def exercise():
        app = AnalysisTerminal(index, source_root=root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("ctrl+r")
            await app.workers.wait_for_complete()
            await pilot.click("#runtime-compare")
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, RuntimeComparisonBrowser)
            assert screen.source_root == root
            await pilot.press("escape", "escape")

    asyncio.run(exercise())


def test_standalone_runtime_index_rejects_comparison_source(ordinary_report, tmp_path):
    pytest.importorskip("textual")
    from code_analysis.runtime_index import RuntimeIndex, build_runtime_index
    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser
    from code_analysis.ui_runtime import RuntimeViewService

    service, browse_index, delta = build_comparison_index(ordinary_report, tmp_path)
    runtime_service = RuntimeViewService(service.profile, service.comparison, service.delta)
    runtime_dir = tmp_path / "runtime-only"
    manifest = build_runtime_index(runtime_service, runtime_dir)
    runtime_index = RuntimeIndex.open(
        runtime_dir, profile_sha256=manifest.runtime.profile_report_sha256
    )
    with pytest.raises(RuntimeSourceUnavailable):
        runtime_index.runtime_comparison_source(delta.records[0].frame_identity, "baseline")

    root = _source_tree(tmp_path)

    async def exercise():
        app = AnalysisTerminal(browse_index, source_root=root)
        async with app.run_test(size=(80, 24)) as pilot:
            await _open_comparison_screen(app, pilot, runtime_index, root)
            assert isinstance(app.screen, RuntimeComparisonBrowser)
            await pilot.press("b")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, RowDetails)
            assert "static source" in app.screen.detail.lower()

    asyncio.run(exercise())
