from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.profile_models import FrameClassification, ProfileReport
from code_analysis.runtime_models import RuntimeSourceHash, SourceBindingState
from code_analysis.runtime_source_browse import RuntimeSourcePreview, RuntimeSourceUnavailable
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService


def prepare(ordinary_report, tmp_path, mode="matched"):
    service = ReportViewService(ordinary_report)
    source = next(
        item for item in ordinary_report.source_files if item.logical_path == "alpha/a.py"
    )
    original = RuntimeViewService.load(
        Path(__file__).parent / "golden" / "phase4-profile-report.json"
    ).profile
    frame = original.frames[0].model_copy(
        update={
            "path": source.logical_path,
            "line": 1,
            "classification": (
                FrameClassification.PROJECT
                if mode != "environment"
                else FrameClassification.ENVIRONMENT
            ),
        }
    )
    hashes = (
        RuntimeSourceHash(
            path=source.logical_path if mode != "missing" else "other.py",
            sha256=source.source_sha256 if mode != "hash" else "0" * 64,
        ),
    )
    runtime = original.runtime.model_copy(
        update={
            "source_binding": (
                SourceBindingState.UNBOUND if mode == "unbound" else SourceBindingState.MATCHED
            ),
            "source_hashes": hashes,
        }
    )
    profile = ProfileReport.model_validate(
        {
            **original.model_dump(),
            "frames": (frame,),
            "runtime_edges": (),
            "statistics": (),
            "source_report_sha256": (
                service.bootstrap().reports[0].report_sha256 if mode != "report" else "f" * 64
            ),
            "runtime": runtime,
        }
    )
    root = tmp_path / "index"
    manifest = build_browse_index(service, root, runtime=RuntimeViewService(profile))
    return BrowseIndex.open(root, report_sha256=manifest.report_sha256), frame


def test_runtime_source_requires_exact_saved_hashes(ordinary_report, tmp_path, monkeypatch):
    index, frame = prepare(ordinary_report, tmp_path)
    monkeypatch.setattr(RuntimeViewService, "load", lambda *a: pytest.fail("profile reload"))
    preview = index.runtime_source(frame.frame_id)
    assert preview.source.logical_path == "alpha/a.py"
    assert preview.start == 1 and preview.end == 40
    assert preview.source.source_sha256 == index.source_file("alpha/a.py").source_sha256
    payload = json.loads(preview.model_dump_json())
    payload["end"] = 10000
    with pytest.raises(ValidationError):
        RuntimeSourcePreview.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("mode", ["unbound", "missing", "report", "environment", "hash"])
def test_runtime_source_unbound_mismatch_and_external_frames(ordinary_report, tmp_path, mode):
    index, frame = prepare(ordinary_report, tmp_path, mode)
    with pytest.raises(ValueError if mode == "hash" else RuntimeSourceUnavailable):
        index.runtime_source(frame.frame_id)


@pytest.mark.parametrize("mode", ["bound", "changed", "symlink", "disabled"])
def test_runtime_terminal_confined_preview(ordinary_report, tmp_path, mode):
    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_source import TerminalSource

    index, frame = prepare(ordinary_report, tmp_path)
    root = tmp_path / "source"
    shutil.copytree(Path(__file__).parent / "fixtures" / "ordinary", root)
    source = root / frame.path
    if mode == "changed":
        source.write_text("changed = True\n")
    elif mode == "symlink":
        moved = root / "moved.py"
        source.rename(moved)
        source.symlink_to(moved)

    async def exercise():
        app = AnalysisTerminal(index, source_root=None if mode == "disabled" else root)
        async with app.run_test(size=(180, 48)) as pilot:
            await pilot.press("ctrl+r")
            await app.workers.wait_for_complete()
            await pilot.press("s")
            await app.workers.wait_for_complete()
            if mode == "disabled":
                assert isinstance(app.screen, RowDetails)
                assert "disabled" in app.screen.detail
            else:
                assert isinstance(app.screen, TerminalSource)
                preview = app.screen.preview
                assert (
                    preview.state
                    == {"bound": "bound", "changed": "mismatch", "symlink": "unavailable"}[mode]
                )
                if mode == "bound":
                    assert preview.lines
                else:
                    assert not preview.lines
            await pilot.press("escape", "escape")

    asyncio.run(exercise())
