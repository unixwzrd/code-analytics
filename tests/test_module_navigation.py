from __future__ import annotations

import asyncio
import errno

import pytest
from pydantic import ValidationError

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.module_navigation import ModuleNavigationPage, module_navigation_pages
from code_analysis.web_service import ReportViewService


@pytest.fixture
def navigation_index(ordinary_report, tmp_path):
    destination = tmp_path / "navigation"
    manifest = build_browse_index(ReportViewService(ordinary_report), destination)
    return BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def test_navigation_exact_pages_and_strict_selection(ordinary_report, navigation_index):
    pages = tuple(module_navigation_pages(ordinary_report))
    assert any(page.edges for page in pages)
    assert any(page.symbols for page in pages)
    for expected in pages:
        actual = navigation_index.module_navigation(
            expected.module.module, expected.view, expected.offset
        )
        assert actual.model_dump_json() == expected.model_dump_json()
    module = pages[0].module.module
    for offset in (-1, 1, True):
        with pytest.raises(ValueError):
            navigation_index.module_navigation(module, "outgoing", offset)
    with pytest.raises(ValueError):
        navigation_index.module_navigation("' OR 1=1 --")
    with pytest.raises(ValueError):
        navigation_index.module_navigation(module, "unresolved")
    edge_page = next(page for page in pages if page.edges)
    raw = edge_page.model_dump(mode="json")
    raw["edges"][0]["source_id" if edge_page.view == "outgoing" else "target_id"] = "wrong"
    with pytest.raises(ValidationError):
        ModuleNavigationPage.model_validate(raw)


def test_symbol_pages_are_bounded_and_reconcile(ordinary_report):
    module = next(module for module in ordinary_report.modules if module.symbols)
    symbols = tuple(
        module.symbols[0].model_copy(update={"qualified_name": f"symbol{n:03d}"}) for n in range(61)
    )
    report = ordinary_report.model_copy(
        update={"modules": (module.model_copy(update={"symbols": symbols}),)}
    )
    pages = [page for page in module_navigation_pages(report) if page.view == "symbols"]
    assert [page.retained for page in pages] == [25, 25, 11]
    assert [page.offset for page in pages] == [0, 25, 50]
    assert [page.truncated for page in pages] == [True, True, False]
    assert tuple(symbol for page in pages for symbol in page.symbols) == symbols


def test_terminal_follows_dependencies_and_inspects_symbols(
    ordinary_report, navigation_index, monkeypatch
):
    pytest.importorskip("textual")
    from textual.widgets import Select

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_modules import ModuleBrowser

    initial = next(
        page
        for page in module_navigation_pages(ordinary_report)
        if page.view == "outgoing" and page.edges and page.module.symbols
    )

    async def exercise():
        app = AnalysisTerminal(navigation_index)
        async with app.run_test(size=(100, 32)) as pilot:
            browser = ModuleBrowser(navigation_index, initial.module.module, app.evidence_closed)
            app.push_screen(browser)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert browser.page == initial
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert browser.module == initial.edges[0].target_id
            browser.action_back()
            await app.workers.wait_for_complete()
            assert browser.module == initial.module.module
            browser.query_one("#module-view", Select).value = "symbols"
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert browser.page.view == "symbols"
            assert browser.page.symbols
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RowDetails)
            await pilot.press("escape")

            def failed(*args, **kwargs):
                raise ValueError("changed index")

            monkeypatch.setattr(BrowseIndex, "module_navigation", failed)
            browser.load_page()
            await app.workers.wait_for_complete()
            assert app.exit_status == errno.EBADMSG and browser.page is None
            await pilot.press("ctrl+c")
        assert app.return_value == errno.EBADMSG

    asyncio.run(exercise())
