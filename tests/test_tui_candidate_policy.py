from __future__ import annotations

import asyncio
import json
import shutil

import pytest
from conftest import FIXTURES

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import load_config_file
from code_analysis.models import SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projection_models import ProjectionSelection
from code_analysis.projections import ProjectionName
from code_analysis.projections.candidates import build_candidate_worklist_projection
from code_analysis.report_models import RefactorCandidateKind
from code_analysis.service import scan_refactor
from code_analysis.web_service import ReportViewService


@pytest.fixture(scope="module")
def candidate_index(tmp_path_factory):
    root = tmp_path_factory.mktemp("terminal-candidate-policy")
    source = root / "source"
    shutil.copytree(FIXTURES / "proposal_project", source)
    report = scan_refactor(
        discover_context(source, load_config_file(FIXTURES / "proposal-config.toml"))
    )
    destination = root / "index"
    manifest = build_browse_index(ReportViewService(report), destination)
    shutil.rmtree(source)
    assert not source.exists()
    return report, BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def test_terminal_candidate_policy_is_authoritative_source_free_and_exported(candidate_index):
    pytest.importorskip("textual")
    from textual.widgets import Checkbox, Input, Select, SelectionList, TextArea

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_candidate_policy import CandidatePolicyEditor
    from code_analysis.tui_export import ExportPage
    from code_analysis.tui_static_projections import StaticProjectionBrowser

    report, index = candidate_index
    selection = ProjectionSelection(
        languages=(SourceLanguage.PYTHON,),
        candidate_module_patterns=("package.*",),
        candidate_exclude_patterns=("*.missing",),
        candidate_kinds=(RefactorCandidateKind.RESPONSIBILITY_EXTRACTION,),
        candidate_states=("reviewable-incomplete",),
        cross_module_only=True,
        production_only=True,
        min_shared_dimensions=3,
        prefer_existing_owner=True,
    )
    expected = build_candidate_worklist_projection(report, selection, {}, None)
    assert expected.rows

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=1)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, StaticProjectionBrowser)
            assert ProjectionName.CANDIDATE_WORKLIST in screen.available
            screen.query_one("#static-projection-name", Select).value = (
                ProjectionName.CANDIDATE_WORKLIST.value
            )
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None and screen.page.eligible == 2

            # Paging changes only the root request and retains the exact policy.
            await pilot.press("ctrl+n")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.request.offset == 1
            assert screen.request.candidate_selection == ProjectionSelection()
            await pilot.press("ctrl+p", "ctrl+f")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, CandidatePolicyEditor)
            for widget_id, value in (
                ("#candidate-policy-languages", SourceLanguage.PYTHON.value),
                ("#candidate-policy-states", "reviewable-incomplete"),
                (
                    "#candidate-policy-kinds",
                    RefactorCandidateKind.RESPONSIBILITY_EXTRACTION.value,
                ),
            ):
                widget = editor.query_one(widget_id, SelectionList)
                widget.deselect_all()
                widget.select(value)
            editor.query_one("#candidate-policy-includes", TextArea).text = "package.*\n"
            editor.query_one("#candidate-policy-excludes", TextArea).text = "*.missing\n"
            editor.query_one("#candidate-policy-cross-module", Checkbox).value = True
            editor.query_one("#candidate-policy-production", Checkbox).value = True
            editor.query_one("#candidate-policy-owner", Checkbox).value = True
            editor.query_one("#candidate-policy-number", Input).value = "3"
            await pilot.press("ctrl+s")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.screen is screen
            assert screen.request.offset == 0
            assert screen.request.candidate_selection == selection
            assert screen.page is not None
            assert tuple(item.row_id for item in screen.page.records) == tuple(
                item.row_id for item in expected.rows
            )

            await pilot.press("ctrl+e")
            await pilot.pause()
            export = app.screen
            assert isinstance(export, ExportPage)
            assert export.request == screen.request
            export.query_one("#export-name", Input).value = "candidate-policy"
            export.query_one("#export-write").press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert export.exported_path is not None
            payload = json.loads(export.exported_path.read_text(encoding="utf-8"))
            assert payload["selection"]["candidate_kinds"] == ["responsibility-extraction"]
            assert payload["selection"]["candidate_module_patterns"] == ["package.*"]
            assert payload["rows"][0]["row_id"] == expected.rows[0].row_id
            await pilot.press("escape", "escape", "ctrl+c")

    asyncio.run(exercise())


def test_terminal_does_not_offer_unrecorded_candidate_worklist(ordinary_report, tmp_path):
    pytest.importorskip("textual")
    from code_analysis.tui_static_projections import StaticProjectionBrowser

    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), directory)
    index = BrowseIndex.open(directory, report_sha256=manifest.report_sha256)
    browser = StaticProjectionBrowser(index, lambda _: None)
    assert ProjectionName.CANDIDATE_WORKLIST not in browser.available


def test_candidate_policy_editor_rejects_invalid_portable_pattern():
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    from code_analysis.tui_candidate_policy import CandidatePolicyEditor

    async def exercise() -> None:
        # The editor is a strict request constructor even when no report is loaded.
        from textual.app import App

        host = App()
        async with host.run_test(size=(80, 24)) as pilot:
            host.push_screen(CandidatePolicyEditor(ProjectionSelection()))
            await pilot.pause()
            editor = host.screen
            assert isinstance(editor, CandidatePolicyEditor)
            editor.query_one("#candidate-policy-includes", TextArea).text = "../outside"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert host.screen is editor
            assert "portable logical patterns" in str(
                editor.query_one("#candidate-policy-error", Static).content
            )
            await pilot.press("escape")

    asyncio.run(exercise())
