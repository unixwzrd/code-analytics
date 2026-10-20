from __future__ import annotations

import asyncio
import sys

import pytest

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.collection_commands import CollectionRequest
from code_analysis.web_service import ReportViewService


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
def test_terminal_collection_preview_browse_and_invalidation(tmp_path, ordinary_report, size):
    pytest.importorskip("textual")
    from textual.widgets import Button, Checkbox, Input, Select, TextArea

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_collection import CollectionPlan

    source = tmp_path / "sample.py"
    source.write_text("def answer():\n    return 42\n")
    (tmp_path / "report.json").write_text("{}")
    (tmp_path / "capture.prof").write_bytes(b"not parsed during planning")
    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), directory)
    index = BrowseIndex.open(directory, report_sha256=manifest.report_sha256)
    alternate = tmp_path / "trusted-python"
    alternate.symlink_to(sys.executable)

    async def exercise():
        app = AnalysisTerminal(index, collection_root=tmp_path, collection_pythons=(alternate,))
        async with app.run_test(size=size) as pilot:
            await pilot.press("ctrl+l")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CollectionPlan)
            choices = screen.planner.context(report_available=True).interpreters
            assert len(choices) == 2 and choices[1].path == str(alternate)
            screen.query_one("#collection-target", Input).value = "sample.py"
            await pilot.pause()
            screen.query_one("#collection-build", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            expected = screen.planner.preview(
                CollectionRequest(target="sample.py", workspace=".code-analysis")
            )
            assert screen.preview == expected
            assert screen.query_one("#collection-preview", TextArea).text == expected.command
            copied = []
            app.copy_to_clipboard = copied.append
            screen.query_one("#collection-copy", Button).press()
            await pilot.pause()
            assert copied == [expected.command]
            screen.query_one("#collection-interpreter", Select).value = choices[1].id
            await pilot.pause()
            assert screen.preview is None
            screen.query_one("#collection-build", Button).press()
            await pilot.pause()
            expected = screen.planner.preview(
                CollectionRequest(
                    target="sample.py",
                    workspace=".code-analysis",
                    interpreter_id=choices[1].id,
                )
            )
            assert screen.preview == expected
            assert expected.argv[0] == str(alternate)
            screen.query_one("#collection-interpreter", Select).value = "current"
            screen.query_one("#collection-target", Input).value = "."
            await pilot.pause()
            assert screen.preview is None
            assert screen.query_one("#collection-copy", Button).disabled
            screen.query_one("#collection-list", Button).press()
            await pilot.pause()
            screen.query_one("#collection-entry", Select).value = "sample.py"
            screen.query_one("#collection-use", Button).press()
            await pilot.pause()
            assert screen.value("target") == "sample.py"
            screen.query_one("#collection-target", Input).value = "../outside"
            await pilot.pause()
            screen.query_one("#collection-build", Button).press()
            await pilot.pause()
            assert screen.preview is None
            assert not (tmp_path / ".code-analysis").exists()
            screen.query_one("#collection-operation", Select).value = "profile"
            screen.query_one("#collection-source-report", Input).value = "report.json"
            screen.query_one("#collection-workload", Input).value = "sample.py"
            screen.query_one("#collection-arguments", TextArea).load_text("one two\n\nthree")
            await pilot.pause()
            screen.query_one("#collection-build", Button).press()
            await pilot.pause()
            assert screen.preview is None
            screen.query_one("#collection-allow", Checkbox).value = True
            await pilot.pause()
            screen.query_one("#collection-build", Button).press()
            await pilot.pause()
            expected = screen.planner.preview(
                CollectionRequest(
                    operation="profile",
                    source_report="report.json",
                    workload="sample.py",
                    workspace=".code-analysis",
                    allow_execution=True,
                    arguments=("one two", "", "three"),
                )
            )
            assert screen.preview == expected
            assert "--allow-execution" in expected.argv
            screen.query_one("#collection-operation", Select).value = "import-profile"
            screen.query_one("#collection-list", Button).press()
            await pilot.pause()
            screen.query_one("#collection-entry", Select).value = "capture.prof"
            screen.query_one("#collection-destination", Select).value = "profiles"
            screen.query_one("#collection-use", Button).press()
            await pilot.pause()
            screen.query_one("#collection-use", Button).press()
            await pilot.pause()
            assert screen.query_one("#collection-profiles", TextArea).text == "capture.prof"
            screen.query_one("#collection-build", Button).press()
            await pilot.pause()
            expected = screen.planner.preview(
                CollectionRequest(
                    operation="import-profile",
                    profiles=("capture.prof",),
                    source_report="report.json",
                    workspace=".code-analysis",
                )
            )
            assert screen.preview == expected
            assert "--allow-execution" not in expected.argv
            screen.query_one("#collection-metadata", Input).value = "missing.json"
            await pilot.pause()
            assert screen.preview is None
            screen.query_one("#collection-build", Button).press()
            await pilot.pause()
            assert screen.preview is None
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert screen.planner._closed
        assert not (tmp_path / ".code-analysis").exists()

    asyncio.run(exercise())
