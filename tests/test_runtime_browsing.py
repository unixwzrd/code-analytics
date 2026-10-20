from __future__ import annotations

import asyncio
import errno
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseIndexManifest
from code_analysis.profile_models import ProfileReport
from code_analysis.runtime_browse_models import RuntimeBrowsePage, RuntimeBrowseRequest
from code_analysis.runtime_queries import SECTION_REGISTRY, RuntimeQueryRequest, RuntimeQuerySection
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService

GOLDEN = Path(__file__).parent / "golden" / "phase4-profile-report.json"


def test_runtime_cli_source_free_and_deterministic(ordinary_report, tmp_path, capsys):
    from code_analysis.cli import main
    from code_analysis.reports.json_report import canonical_json

    report = tmp_path / "report.json"
    profile = tmp_path / "profile.json"
    report.write_text(canonical_json(ordinary_report))
    profile.write_bytes(GOLDEN.read_bytes())
    manifests = []
    for name in ("first", "second"):
        assert (
            main(
                [
                    "index",
                    str(report),
                    "-o",
                    str(tmp_path / name),
                    "--profile-report",
                    str(profile),
                ]
            )
            == errno.ENODATA
        )
        output = capsys.readouterr()
        manifests.append(BrowseIndexManifest.model_validate_json(output.out))
        assert "incomplete" in output.err
    assert manifests[0] == manifests[1]
    report.unlink()
    profile.unlink()
    code = """
import sys
from pathlib import Path
from code_analysis.browse_index import BrowseIndex
from code_analysis.runtime_browse_models import RuntimeBrowseRequest
from code_analysis.runtime_queries import RuntimeQuerySection
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService
def forbidden(*args, **kwargs):
    raise AssertionError('saved input reloaded')
RuntimeViewService.load = forbidden
ReportViewService.load = forbidden
index = BrowseIndex.open(Path(sys.argv[1]), report_sha256=sys.argv[2])
page = index.runtime_page(RuntimeBrowseRequest(section=RuntimeQuerySection.HOTSPOTS))
assert len(page.panel.projection.records) == 2
"""
    subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "first"), manifests[0].report_sha256],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=30,
    )


@pytest.mark.parametrize("failure", ["malformed", "bound"])
def test_runtime_cli_failure_does_not_publish(
    ordinary_report, tmp_path, monkeypatch, capsys, failure
):
    from code_analysis import browse_index_build
    from code_analysis.cli import main
    from code_analysis.reports.json_report import canonical_json
    from code_analysis.runtime_browse_storage import RuntimeBrowseLimitError

    report = tmp_path / "report.json"
    profile = tmp_path / "profile.json"
    report.write_text(canonical_json(ordinary_report))
    profile.write_bytes(b"{}" if failure == "malformed" else GOLDEN.read_bytes())
    if failure == "bound":

        def fail(*args, **kwargs):
            raise RuntimeBrowseLimitError("synthetic preparation bound")

        monkeypatch.setattr(browse_index_build, "write_runtime_records", fail)
    destination = tmp_path / "index"
    assert main(
        ["index", str(report), "-o", str(destination), "--profile-report", str(profile)]
    ) == (errno.EBADMSG if failure == "malformed" else errno.ENODATA)
    output = capsys.readouterr()
    assert not output.out
    assert output.err
    assert not destination.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["profile.json", "report.json"]


@pytest.fixture
def runtime_index(ordinary_report, tmp_path):
    service = RuntimeViewService.load(GOLDEN)
    root = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), root, runtime=service)
    return service, BrowseIndex.open(root, report_sha256=manifest.report_sha256)


@pytest.mark.parametrize("section", tuple(RuntimeQuerySection))
def test_runtime_pages_equal_shared_queries_without_profile_loading(
    runtime_index, monkeypatch, section
):
    service, index = runtime_index
    monkeypatch.setattr(RuntimeViewService, "load", lambda *args: pytest.fail("profile reload"))
    monkeypatch.setattr(ReportViewService, "load", lambda *args: pytest.fail("static reload"))
    expected = service.panel(RuntimeQueryRequest(section=section, max_records=1))
    page = index.runtime_page(RuntimeBrowseRequest(section=section, limit=1))
    assert page.panel == expected
    records = tuple(
        row
        for offset in range(page.panel.projection.eligible_count)
        for row in index.runtime_page(
            RuntimeBrowseRequest(section=section, offset=offset, limit=1)
        ).panel.projection.records
    )
    assert records == SECTION_REGISTRY[section](service.profile)


def test_runtime_frame_selection_and_missing_frame(runtime_index):
    service, index = runtime_index
    frame = service.profile.frames[0].frame_id
    for section in (
        RuntimeQuerySection.HOTSPOTS,
        RuntimeQuerySection.CALLS,
        RuntimeQuerySection.VARIANCE,
    ):
        expected = service.panel(
            RuntimeQueryRequest(section=section, frame_id=frame, max_records=25)
        )
        assert (
            index.runtime_page(RuntimeBrowseRequest(section=section, frame_id=frame)).panel
            == expected
        )
    with pytest.raises(ValueError, match="absent"):
        index.runtime_page(
            RuntimeBrowseRequest(
                section=RuntimeQuerySection.CALLS, frame_id="profile-frame-" + "0" * 20
            )
        )
    with pytest.raises(ValidationError):
        RuntimeBrowseRequest(section=RuntimeQuerySection.RESOURCES, frame_id=frame)


@pytest.mark.parametrize("failure", ["absent", "changed"])
def test_runtime_terminal_errors_are_not_empty_success(ordinary_report, tmp_path, failure):
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable, Static

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_runtime import RuntimeBrowser

    root = tmp_path / "index"
    manifest = build_browse_index(
        ReportViewService(ordinary_report),
        root,
        runtime=RuntimeViewService.load(GOLDEN) if failure == "changed" else None,
    )
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(160, 48)) as pilot:
            await app.workers.wait_for_complete()
            if failure == "changed":
                index.database.touch()
            await pilot.press("ctrl+r")
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, RuntimeBrowser)
            assert screen.page is None
            assert "unavailable" in str(screen.query_one("#runtime-status", Static).render())
            assert screen.query_one("#runtime-records", DataTable).row_count == 0
            assert screen.query_one("#runtime-next", Button).disabled
            assert app.exit_status == (errno.EBADMSG if failure == "changed" else errno.ENODATA)
            await pilot.press("escape")
            assert not isinstance(app.screen, RuntimeBrowser)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "payload",
    [
        {"section": "hotspots", "limit": True},
        {"section": "hotspots", "limit": 101},
        {"section": "hotspots", "offset": True},
        {"section": "hotspots", "offset": -1},
        {"section": "hotspots", "unexpected": 0},
    ],
)
def test_runtime_page_selection_is_strict(payload):
    with pytest.raises(ValidationError):
        RuntimeBrowseRequest.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("damage", ["next", "labels", "offset", "record"])
def test_runtime_page_metadata_reconciles(runtime_index, damage):
    page = runtime_index[1].runtime_page(
        RuntimeBrowseRequest(section=RuntimeQuerySection.HOTSPOTS, limit=1)
    )
    payload = json.loads(page.model_dump_json())
    if damage == "next":
        payload["next_offset"] = 100
    elif damage == "labels":
        payload["panel"]["labels"] = []
    elif damage == "offset":
        payload["offset"] = 999
    else:
        payload["panel"]["projection"]["records"][0]["unknown"] = True
    with pytest.raises(ValidationError):
        RuntimeBrowsePage.model_validate_json(json.dumps(payload))


def test_runtime_receipt_binding_and_absence(ordinary_report, runtime_index, tmp_path):
    _, index = runtime_index
    payload = json.loads(index.manifest.model_dump_json())
    payload["runtime"]["overview"]["source_matches"] = not payload["runtime"]["overview"][
        "source_matches"
    ]
    with pytest.raises(ValidationError):
        BrowseIndexManifest.model_validate_json(json.dumps(payload))
    root = tmp_path / "no-runtime"
    manifest = build_browse_index(ReportViewService(ordinary_report), root)
    absent = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    with pytest.raises(ValueError, match="unavailable"):
        absent.runtime_page(RuntimeBrowseRequest(section=RuntimeQuerySection.HOTSPOTS))


def test_unbound_profile_retains_null_source_binding(ordinary_report, tmp_path):
    original = RuntimeViewService.load(GOLDEN).profile
    profile = original.model_copy(update={"source_report_sha256": None})
    root = tmp_path / "index"
    manifest = build_browse_index(
        ReportViewService(ordinary_report), root, runtime=RuntimeViewService(profile)
    )
    assert manifest.runtime.static_report_sha256 is None
    assert not manifest.runtime.overview.source_matches
    assert (
        BrowseIndex.open(root, report_sha256=manifest.report_sha256)
        .runtime_page(RuntimeBrowseRequest(section=RuntimeQuerySection.HOTSPOTS))
        .panel.projection.records
    )


def test_runtime_large_page_continuation_and_terminal_navigation(
    ordinary_report, tmp_path, monkeypatch
):
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Select

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_runtime import RuntimeBrowser

    original = RuntimeViewService.load(GOLDEN).profile
    frames = tuple(
        original.frames[0].model_copy(update={"frame_id": f"profile-frame-{number:020x}"})
        for number in range(61)
    )
    profile = ProfileReport.model_validate(
        {**original.model_dump(), "frames": frames, "runtime_edges": (), "statistics": ()}
    )
    root = tmp_path / "index"
    manifest = build_browse_index(
        ReportViewService(ordinary_report), root, runtime=RuntimeViewService(profile)
    )
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    monkeypatch.setattr(RuntimeViewService, "load", lambda *args: pytest.fail("profile reload"))

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.press("ctrl+r")
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, RuntimeBrowser)
            seen = []
            for offset, count in ((0, 25), (25, 25), (50, 11)):
                assert screen.page.offset == offset
                assert screen.query_one("#runtime-records", DataTable).row_count == count
                seen.extend(row.frame_id for row in screen.page.panel.projection.records)
                await pilot.press("ctrl+n")
                await app.workers.wait_for_complete()
            assert seen == [frame.frame_id for frame in frames]
            await pilot.press("ctrl+p")
            await app.workers.wait_for_complete()
            assert screen.page.offset == 25
            await pilot.press("i")
            assert isinstance(app.screen, RowDetails)
            await pilot.press("escape")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert screen.request.section is RuntimeQuerySection.CALLS
            assert screen.request.frame_id == frames[25].frame_id
            screen.query_one("#runtime-section", Select).value = "resources"
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.request.frame_id is None
            assert screen.page.panel.projection.records
            await pilot.click("#runtime-health")
            assert isinstance(app.screen, RowDetails)
            assert "source_matches" in app.screen.detail
            await pilot.press("escape", "escape")
            assert not isinstance(app.screen, RuntimeBrowser)

    asyncio.run(exercise())
