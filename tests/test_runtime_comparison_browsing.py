from __future__ import annotations

import asyncio
import errno
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseIndexManifest
from code_analysis.profile_models import ProfileReport
from code_analysis.profiling import build_profile_diff
from code_analysis.runtime_comparison import comparison_frame_key
from code_analysis.runtime_comparison_browse import RuntimeComparisonPage, RuntimeComparisonRequest
from code_analysis.runtime_source_browse import read_runtime_comparison_frame_binding
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService

GOLDEN = Path(__file__).parent / "golden" / "phase4-profile-report.json"


@pytest.fixture
def compared_index(ordinary_report, tmp_path):
    original = RuntimeViewService.load(GOLDEN).profile
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
    root = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), root, runtime=service)
    return service, BrowseIndex.open(root, report_sha256=manifest.report_sha256)


def test_comparison_retains_every_record_without_loading(compared_index, monkeypatch):
    service, index = compared_index
    monkeypatch.setattr(RuntimeViewService, "load", lambda *a: pytest.fail("profile reload"))
    assert index.manifest.runtime.comparison.records == 131
    assert (
        index.manifest.runtime.comparison.comparison_frame_count
        == index.manifest.runtime.comparison.records
    )
    records = []
    offset = 0
    while True:
        page = index.runtime_comparison(RuntimeComparisonRequest(offset=offset))
        assert page.report.diagnostic_only
        assert not page.report.claim_eligible
        assert page.report.decisions == service.delta.decisions
        records.extend(page.report.records)
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert tuple(records) == service.delta.records
    assert index.manifest.runtime.comparison.candidate_static_sha256 is None
    assert not index.manifest.runtime.comparison.candidate_overview.source_matches
    with closing(sqlite3.connect(index.database)) as connection:
        for record in service.delta.records:
            binding = read_runtime_comparison_frame_binding(connection, record.frame_identity)
            baseline, candidate = service.profile, service.comparison
            if binding.baseline_frame_id is not None:
                frame = next(
                    item for item in baseline.frames if item.frame_id == binding.baseline_frame_id
                )
                assert record.frame_identity == comparison_frame_key(baseline, candidate, frame)
            if binding.candidate_frame_id is not None:
                frame = next(
                    item for item in candidate.frames if item.frame_id == binding.candidate_frame_id
                )
                assert record.frame_identity == comparison_frame_key(baseline, candidate, frame)


@pytest.mark.parametrize(
    "damage", ["binding", "identity", "availability", "claim", "header", "candidate_binding"]
)
def test_comparison_receipt_rejects_contradictions(compared_index, damage):
    payload = json.loads(compared_index[1].manifest.model_dump_json())
    runtime = payload["runtime"]
    comparison = runtime["comparison"]
    if damage == "binding":
        comparison["candidate_overview"]["source_matches"] = True
    elif damage == "identity":
        comparison["header"]["baseline_profile_identity_sha256"] = "0" * 64
    elif damage == "availability":
        runtime["overview"]["comparison_available"] = False
    elif damage == "claim":
        comparison["header"]["claim_eligible"] = True
    elif damage == "candidate_binding":
        comparison["candidate_source_binding"] = "matched"
        comparison["candidate_source_hash_count"] = 0
    else:
        comparison["header"]["records"] = [compared_index[0].delta.records[0].model_dump()]
    with pytest.raises(ValidationError):
        BrowseIndexManifest.model_validate_json(json.dumps(payload))


def test_comparison_strict_bounds_and_continuation(compared_index):
    for payload in ({"limit": True}, {"limit": 101}, {"offset": -1}, {"unexpected": 1}):
        with pytest.raises(ValidationError):
            RuntimeComparisonRequest.model_validate(payload)
    page = compared_index[1].runtime_comparison(RuntimeComparisonRequest())
    payload = json.loads(page.model_dump_json())
    payload["next_offset"] = None
    with pytest.raises(ValidationError):
        RuntimeComparisonPage.model_validate_json(json.dumps(payload))


def test_comparison_cli_requires_baseline(tmp_path, capsys):
    from code_analysis.cli import main

    assert (
        main(
            [
                "index",
                str(tmp_path / "missing.json"),
                "-o",
                str(tmp_path / "index"),
                "--compare-profile",
                str(GOLDEN),
            ]
        )
        == errno.EINVAL
    )
    output = capsys.readouterr()
    assert not output.out
    assert "requires --profile-report" in output.err


def test_comparison_cli_is_saved_only(ordinary_report, tmp_path, capsys, monkeypatch):
    from code_analysis.cli import main
    from code_analysis.reports.json_report import canonical_json

    report = tmp_path / "report.json"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    report.write_text(canonical_json(ordinary_report))
    baseline.write_bytes(GOLDEN.read_bytes())
    candidate.write_bytes(GOLDEN.read_bytes())
    destination = tmp_path / "index"
    assert (
        main(
            [
                "index",
                str(report),
                "-o",
                str(destination),
                "--profile-report",
                str(baseline),
                "--compare-profile",
                str(candidate),
            ]
        )
        == errno.ENODATA
    )
    output = capsys.readouterr()
    manifest = BrowseIndexManifest.model_validate_json(output.out)
    for path in (report, baseline, candidate):
        path.unlink()
    monkeypatch.setattr(RuntimeViewService, "load", lambda *a: pytest.fail("profile reload"))
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    page = index.runtime_comparison(RuntimeComparisonRequest())
    assert len(page.report.records) == 2
    assert page.report.diagnostic_only and not page.report.claim_eligible
    assert all(record.absolute_total_seconds_delta == 0.0 for record in page.report.records)


def test_comparison_absence_is_explicit(ordinary_report, tmp_path):
    root = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), root)
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    with pytest.raises(ValueError, match="unavailable"):
        index.runtime_comparison(RuntimeComparisonRequest())


def test_terminal_comparison_pages_and_health(compared_index):
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Static

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser

    service, index = compared_index

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(180, 48)) as pilot:
            await pilot.press("ctrl+r")
            await app.workers.wait_for_complete()
            await pilot.click("#runtime-compare")
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, RuntimeComparisonBrowser)
            records = []
            for offset in (0, 25, 50, 75, 100, 125):
                assert screen.page.request.offset == offset
                assert screen.query_one("#runtime-comparison-records", DataTable).row_count == min(
                    25, 131 - offset
                )
                records.extend(screen.page.report.records)
                await pilot.press("ctrl+n")
                await app.workers.wait_for_complete()
            assert tuple(records) == service.delta.records
            assert "Diagnostic only" in str(
                screen.query_one("#runtime-comparison-status", Static).render()
            )
            await pilot.press("ctrl+p")
            await app.workers.wait_for_complete()
            assert screen.page.request.offset == 100
            await pilot.press("enter")
            assert isinstance(app.screen, RowDetails)
            assert "absolute_cumulative_seconds_delta" in app.screen.detail
            await pilot.press("escape")
            await pilot.click("#runtime-comparison-health")
            assert isinstance(app.screen, RowDetails)
            assert "diagnostic_only" in app.screen.detail
            assert app.exit_status == errno.ENODATA
            await pilot.press("escape", "escape", "escape")

    asyncio.run(exercise())
