"""Bounded terminal subject-findings filters proven against saved indexed facts only."""

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
from code_analysis.projection_models import (
    ProjectionFindingCategory,
    ProjectionFindingSeverity,
    ProjectionSelection,
)
from code_analysis.projections import ProjectionName
from code_analysis.service import scan_refactor
from code_analysis.static_browse import StaticProjectionRequest
from code_analysis.web_service import ReportViewService

DUPLICATION = ProjectionSelection(
    module_prefixes=("package.combine_a",),
    finding_categories=(ProjectionFindingCategory.DUPLICATION,),
    finding_severities=(ProjectionFindingSeverity.WARNING,),
    finding_rule_ids=("duplication.normalized-ast",),
)


@pytest.mark.parametrize(
    "selection",
    (
        ProjectionSelection(languages=(SourceLanguage.PYTHON,)),
        ProjectionSelection(selected_ids=("subject-00000000000000000000",)),
        ProjectionSelection(max_records=10),
    ),
)
def test_editor_rejects_initial_scope_it_cannot_display(selection):
    pytest.importorskip("textual")
    from code_analysis.tui_subject_filters import SubjectFindingFilterEditor

    with pytest.raises(ValueError):
        SubjectFindingFilterEditor(selection)


@pytest.fixture(scope="module")
def findings_index(tmp_path_factory):
    """Index a copied project, then delete the source: browsing reads facts only."""
    root = tmp_path_factory.mktemp("terminal-subject-filters").resolve()
    source = root / "source"
    shutil.copytree(FIXTURES / "proposal_project", source)
    report = scan_refactor(
        discover_context(source, load_config_file(FIXTURES / "proposal-config.toml"))
    )
    destination = root / "index"
    manifest = build_browse_index(ReportViewService(report), destination)
    shutil.rmtree(source)
    assert not source.exists()
    return source, BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def _open_subject_findings(app, pilot):
    from textual.widgets import Select

    from code_analysis.tui_static_projections import StaticProjectionBrowser

    screen = app.screen
    assert isinstance(screen, StaticProjectionBrowser)
    assert ProjectionName.SUBJECT_FINDINGS in screen.available
    screen.query_one("#static-projection-name", Select).value = (
        ProjectionName.SUBJECT_FINDINGS.value
    )
    return screen


async def _select_filters(editor, pilot, selection: ProjectionSelection) -> None:
    from textual.widgets import SelectionList, TextArea

    for widget_id, values in (
        ("#subject-filter-categories", tuple(item.value for item in selection.finding_categories)),
        ("#subject-filter-severities", tuple(item.value for item in selection.finding_severities)),
    ):
        widget = editor.query_one(widget_id, SelectionList)
        widget.deselect_all()
        for value in values:
            widget.select(value)
    editor.query_one("#subject-filter-prefixes", TextArea).text = "\n".join(
        selection.module_prefixes
    )
    editor.query_one("#subject-filter-rules", TextArea).text = "\n".join(selection.finding_rule_ids)
    await pilot.pause()


def test_terminal_finding_filters_scope_counts_and_export_from_saved_facts(findings_index):
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable, Input, Static

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_export import ExportPage
    from code_analysis.tui_subject_filters import SubjectFindingFilterEditor

    source, index = findings_index
    assert not source.exists()
    unfiltered = index.static_projection(
        StaticProjectionRequest(name=ProjectionName.SUBJECT_FINDINGS, limit=25)
    )
    expected = index.static_projection(
        StaticProjectionRequest(
            name=ProjectionName.SUBJECT_FINDINGS, limit=25, finding_selection=DUPLICATION
        )
    )
    assert 0 < expected.eligible < unfiltered.eligible

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=25)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = _open_subject_findings(app, pilot)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.page is not None
            assert screen.page.eligible == unfiltered.eligible
            assert screen.request.finding_selection == ProjectionSelection()
            policy = screen.query_one("#static-projection-policy", Button)
            assert not policy.disabled and str(policy.label) == "Finding filters"
            scope = screen.query_one("#static-projection-scope", Static)
            assert "every prepared subject row" in str(scope.content)

            await pilot.press("ctrl+f")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, SubjectFindingFilterEditor)
            await _select_filters(editor, pilot, DUPLICATION)
            await pilot.press("ctrl+s")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.screen is screen
            assert screen.request.finding_selection == DUPLICATION
            assert screen.request.offset == 0
            assert screen.page is not None
            assert screen.page.eligible == expected.eligible
            assert tuple(row.row_id for row in screen.page.records) == tuple(
                row.row_id for row in expected.records
            )
            # Counts come from the filtered aggregation, not from a hidden total.
            assert screen.page.unscoped_finding_count == expected.unscoped_finding_count
            table = screen.query_one("#static-projection-records", DataTable)
            assert table.row_count == screen.page.retained
            assert "1 prefix" in str(screen.query_one("#static-projection-scope", Static).content)

            # A search retains the scope and pages exactly the filtered rows.
            screen.query_one("#static-projection-search", Input).value = "package.combine_a"
            screen.query_one("#static-projection-apply", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.request.finding_selection == DUPLICATION
            assert screen.page is not None and screen.page.eligible == expected.eligible

            await pilot.press("f2")
            await pilot.pause()
            details = json.loads(app.screen.detail)
            assert details["request"]["finding_selection"]["finding_categories"] == ["duplication"]
            assert details["unscoped_finding_count"] == expected.unscoped_finding_count
            assert details["prepared_records"] == unfiltered.eligible
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("ctrl+e")
            await pilot.pause()
            export = app.screen
            assert isinstance(export, ExportPage)
            assert export.request == screen.request
            export.query_one("#export-name", Input).value = "finding-scope"
            export.query_one("#export-write", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert export.exported_path is not None, str(
                export.query_one("#export-status", Static).content
            )
            payload = json.loads(export.exported_path.read_text(encoding="utf-8"))
            assert payload["selection"]["module_prefixes"] == ["package.combine_a"]
            assert payload["selection"]["finding_categories"] == ["duplication"]
            assert payload["selection"]["finding_severities"] == ["warning"]
            assert payload["selection"]["finding_rule_ids"] == ["duplication.normalized-ast"]
            assert [row["row_id"] for row in payload["rows"]] == [
                row.row_id for row in expected.records
            ]
            await pilot.press("escape", "escape", "ctrl+c")

    asyncio.run(exercise())


def test_terminal_finding_filters_are_keyboard_usable_and_scrollable_at_80x24(findings_index):
    pytest.importorskip("textual")
    from textual.containers import ScrollableContainer
    from textual.widgets import Button, SelectionList

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_subject_filters import SubjectFindingFilterEditor

    _, index = findings_index

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=25)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = _open_subject_findings(app, pilot)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("ctrl+f")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, SubjectFindingFilterEditor)
            dialog = editor.query_one("#subject-filter-dialog")
            assert dialog.size.width <= 80 and dialog.size.height <= 24
            scroll = editor.query_one("#subject-filter-scroll", ScrollableContainer)
            assert scroll.size.height > 0
            categories = editor.query_one("#subject-filter-categories", SelectionList)
            # The bounded list is taller than its viewport, so it must scroll.
            assert categories.virtual_size.height > categories.size.height
            for identity in (
                "#subject-filter-categories",
                "#subject-filter-severities",
                "#subject-filter-prefixes",
                "#subject-filter-rules",
                "#subject-filter-apply",
                "#subject-filter-reset",
                "#subject-filter-cancel",
            ):
                widget = editor.query_one(identity)
                widget.focus()
                await pilot.pause()
                assert editor.focused is widget

            # Selecting with the keyboard alone reaches the backend selection.
            categories.focus()
            await pilot.pause()
            categories.highlighted = tuple(ProjectionFindingCategory).index(
                ProjectionFindingCategory.DUPLICATION
            )
            await pilot.press("space")
            await pilot.pause()
            assert editor.filters().finding_categories == (ProjectionFindingCategory.DUPLICATION,)
            apply_button = editor.query_one("#subject-filter-apply", Button)
            apply_button.focus()
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.screen is screen
            assert screen.request.finding_selection == ProjectionSelection(
                finding_categories=(ProjectionFindingCategory.DUPLICATION,)
            )
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())


def test_terminal_finding_filters_report_invalid_scope_and_keep_the_previous_request(
    findings_index,
):
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_subject_filters import SubjectFindingFilterEditor

    _, index = findings_index

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=25)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = _open_subject_findings(app, pilot)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("ctrl+f")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, SubjectFindingFilterEditor)
            await _select_filters(editor, pilot, DUPLICATION)

            # A glob is not a dotted module prefix; the error stays visible.
            editor.query_one("#subject-filter-prefixes", TextArea).text = "package.*"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.screen is editor
            assert "dotted module names" in str(
                editor.query_one("#subject-filter-error", Static).content
            )
            # An empty dotted segment is not a module name either.
            editor.query_one("#subject-filter-prefixes", TextArea).text = "package..combine_a"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.screen is editor
            assert "dotted module names" in str(
                editor.query_one("#subject-filter-error", Static).content
            )

            # Cancel leaves the browser request untouched.
            await pilot.press("escape")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.screen is screen
            assert screen.request.finding_selection == ProjectionSelection()

            # Reset returns to the unfiltered prepared scope.
            await pilot.press("ctrl+f")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, SubjectFindingFilterEditor)
            await _select_filters(editor, pilot, DUPLICATION)
            await pilot.press("ctrl+s")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.request.finding_selection == DUPLICATION
            await pilot.press("ctrl+f")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, SubjectFindingFilterEditor)
            editor.query_one("#subject-filter-reset").press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.screen is screen
            assert screen.request.finding_selection == ProjectionSelection()
            assert screen.page is not None
            assert (
                screen.page.eligible
                == index.static_projection(
                    StaticProjectionRequest(name=ProjectionName.SUBJECT_FINDINGS, limit=25)
                ).eligible
            )
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())


def test_repeated_values_are_canonicalised_and_unsupported_dimensions_are_refused(
    findings_index,
):
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    from code_analysis.models import SourceLanguage
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_subject_filters import SubjectFindingFilterEditor

    _, index = findings_index
    statuses: list[int] = []

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=25)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = _open_subject_findings(app, pilot)
            screen.on_status = statuses.append
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("ctrl+f")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, SubjectFindingFilterEditor)
            # Repeated values within one dimension are unique and sorted, not ordered input.
            editor.query_one("#subject-filter-rules", TextArea).text = (
                "  duplication.normalized-ast \nextraction-candidate\n\n"
                "duplication.normalized-ast\n"
            )
            editor.query_one("#subject-filter-prefixes", TextArea).text = (
                "package.combine_b\npackage.combine_a\npackage.combine_b\n"
            )
            await pilot.press("ctrl+s")
            await pilot.pause()
            await app.workers.wait_for_complete()
            selection = screen.request.finding_selection
            assert selection is not None
            assert selection.finding_rule_ids == (
                "duplication.normalized-ast",
                "extraction-candidate",
            )
            assert selection.module_prefixes == ("package.combine_a", "package.combine_b")

            # A dimension the index cannot resolve is refused by the backend validator.
            retained = screen.request
            statuses.clear()
            screen.subject_filters_closed(
                selection.model_copy(update={"languages": (SourceLanguage.PYTHON,)})
            )
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.request == retained
            assert statuses == [22]
            assert "language selection" in str(
                screen.query_one("#static-projection-status", Static).content
            )
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())


def test_terminal_finding_filters_page_and_cannot_export_a_stale_scope(findings_index):
    pytest.importorskip("textual")
    from textual.widgets import Button

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_subject_filters import SubjectFindingFilterEditor

    _, index = findings_index
    scoped = ProjectionSelection(finding_severities=(ProjectionFindingSeverity.INFO,))
    expected = index.static_projection(
        StaticProjectionRequest(
            name=ProjectionName.SUBJECT_FINDINGS, limit=1, finding_selection=scoped
        )
    )
    assert expected.truncated

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=1)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = _open_subject_findings(app, pilot)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("ctrl+f")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, SubjectFindingFilterEditor)
            await _select_filters(editor, pilot, scoped)
            await pilot.press("ctrl+s")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.request.finding_selection == scoped

            await pilot.press("ctrl+n")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.request.offset == 1
            assert screen.request.finding_selection == scoped
            assert screen.page is not None and screen.page.request == screen.request

            # An accepted edit discards the page before any read, so neither the
            # stale rows nor an export of them can survive the new request.
            screen.subject_filters_closed(DUPLICATION)
            assert screen.page is None
            assert screen.query_one("#static-projection-export", Button).disabled
            await pilot.press("ctrl+e")
            await pilot.pause()
            assert app.screen is screen
            await app.workers.wait_for_complete()
            assert screen.request.offset == 0
            assert screen.request.finding_selection == DUPLICATION
            assert screen.page is not None and screen.page.request == screen.request

            # The same continuation through the dialog reaches the same request.
            await pilot.press("ctrl+f")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, SubjectFindingFilterEditor)
            await _select_filters(editor, pilot, scoped)
            editor.query_one("#subject-filter-apply", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.screen is screen
            assert screen.request.offset == 0
            assert screen.request.finding_selection == scoped
            assert screen.page is not None and screen.page.request == screen.request
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())


def test_switching_projection_clears_the_finding_scope_and_its_control(findings_index):
    pytest.importorskip("textual")
    from textual.widgets import Button, Select, Static

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_subject_filters import SubjectFindingFilterEditor

    _, index = findings_index

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=25)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+g")
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = _open_subject_findings(app, pilot)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("ctrl+f")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, SubjectFindingFilterEditor)
            await _select_filters(editor, pilot, DUPLICATION)
            await pilot.press("ctrl+s")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.request.finding_selection == DUPLICATION

            screen.query_one("#static-projection-name", Select).value = (
                ProjectionName.CYCLE_LIST.value
            )
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.request == StaticProjectionRequest(
                name=ProjectionName.CYCLE_LIST, limit=25
            )
            policy = screen.query_one("#static-projection-policy", Button)
            assert policy.disabled and str(policy.label) == "Candidate policy"
            assert str(screen.query_one("#static-projection-scope", Static).content) == ""
            # Ctrl+F is inert where no prepared selection editor exists.
            await pilot.press("ctrl+f")
            await pilot.pause()
            assert app.screen is screen

            screen.query_one("#static-projection-name", Select).value = (
                ProjectionName.SUBJECT_FINDINGS.value
            )
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.request.finding_selection == ProjectionSelection()
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())


def test_candidate_policy_remains_the_worklist_editor(findings_index):
    pytest.importorskip("textual")
    from textual.widgets import Button, Select

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_candidate_policy import CandidatePolicyEditor
    from code_analysis.tui_static_projections import StaticProjectionBrowser

    _, index = findings_index

    async def exercise() -> None:
        app = AnalysisTerminal(index, page_size=25)
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
            policy = screen.query_one("#static-projection-policy", Button)
            assert not policy.disabled and str(policy.label) == "Candidate policy"
            assert screen.request.finding_selection is None
            await pilot.press("ctrl+f")
            await pilot.pause()
            assert isinstance(app.screen, CandidatePolicyEditor)
            await pilot.press("escape")
            await pilot.pause()
            policy.press()
            await pilot.pause()
            assert isinstance(app.screen, CandidatePolicyEditor)
            await pilot.press("escape", "escape", "ctrl+c")

    asyncio.run(exercise())
