from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from browser_navigation import reveal_navigation

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import AnalysisConfig, CloneThresholds, PhaseThreeLimits
from code_analysis.project import discover_context
from code_analysis.service import scan
from code_analysis.ui_handoff import WebHandoffSelection
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


@pytest.fixture
def facet_index(ordinary_report, tmp_path):
    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), directory)
    return BrowseIndex.open(directory, report_sha256=manifest.report_sha256)


def test_terminal_facets_are_typed_scoped_and_reset(facet_index) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Input, Select, Static

    from code_analysis.models import SourceLanguage
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.ui_rows import BrowseTable

    async def exercise() -> None:
        app = AnalysisTerminal(facet_index, page_size=5)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("f2")
            await app.workers.wait_for_complete()
            app.query_one("#facet-language", Select).value = "python"
            app.query_one("#facet-module", Input).value = "sample"
            await pilot.click("#apply")
            await app.workers.wait_for_complete()
            assert app.selection.facets.language is SourceLanguage.PYTHON
            assert app.selection.facets.module_prefix == "sample"
            assert app.selection.facets.owner == ""
            accepted = app.selection
            app.query_one("#facet-module", Input).value = "sample*"
            await pilot.click("#apply")
            assert app.selection == accepted
            assert "not a glob" in str(app.query_one("#page-status", Static).content)

            await pilot.press("f8")
            await app.workers.wait_for_complete()
            assert app.selection.table is BrowseTable.STATE_EFFECTS
            assert app.selection.facets.language is None
            assert app.selection.facets.module_prefix == ""
            app.query_one("#facet-owner", Input).value = "saved.owner"
            await pilot.click("#apply")
            await app.workers.wait_for_complete()
            assert app.selection.facets.owner == "saved.owner"

            for key, table in (("f10", BrowseTable.CONTRACTS), ("f11", BrowseTable.SHAPES)):
                await pilot.press(key)
                await app.workers.wait_for_complete()
                assert app.selection.table is table
                assert app.query_one("#facet-owner", Input).display
                app.query_one("#facet-language", Select).value = "python"
                app.query_one("#facet-module", Input).value = "sample"
                app.query_one("#facet-owner", Input).value = "sample.owner"
                await pilot.click("#apply")
                await app.workers.wait_for_complete()
                assert app.selection.facets.language is SourceLanguage.PYTHON
                assert app.selection.facets.module_prefix == "sample"
                assert app.selection.facets.owner == "sample.owner"

            await pilot.press("f3")
            await app.workers.wait_for_complete()
            assert app.selection.table is BrowseTable.FINDINGS
            assert app.selection.facets.owner == ""
            assert not app.query_one("#facet-owner", Input).display

            for key, table in (
                ("f4", BrowseTable.CANDIDATES),
                ("f9", BrowseTable.CLONES),
                ("f12", BrowseTable.TEST_IMPACT),
            ):
                await pilot.press(key)
                await app.workers.wait_for_complete()
                assert app.selection.table is table
                assert app.query_one("#facet-filters").display
                assert not app.query_one("#facet-owner", Input).display
                app.query_one("#facet-language", Select).value = "python"
                app.query_one("#facet-module", Input).value = "sample"
                await pilot.click("#apply")
                await app.workers.wait_for_complete()
                assert app.selection.facets.language is SourceLanguage.PYTHON
                assert app.selection.facets.module_prefix == "sample"
                assert app.selection.facets.owner == ""
            await pilot.press("ctrl+q")

    asyncio.run(exercise())


def test_browser_facets_forward_language_module_and_owner(ordinary_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    server = create_server(ReportViewService(ordinary_report), token="facet-browser")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                requests: list[str] = []
                page.on(
                    "request",
                    lambda request: (
                        requests.append(request.url)
                        if any(
                            route in request.url
                            for route in (
                                "/api/modules?",
                                "/api/findings?",
                                "/api/state-effects?",
                                "/api/contracts?",
                                "/api/dictionary-shapes?",
                                "/api/candidates?",
                                "/api/clones?",
                                "/api/test-impact?",
                            )
                        )
                        else None
                    ),
                )
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=facet-browser")
                page.locator(".cards").wait_for()
                page.locator(".lui-shell__menu").click()
                reveal_navigation(page, "Modules").click()
                page.get_by_role("columnheader", name="Module", exact=True).wait_for()
                page.get_by_role("combobox", name="Language", exact=True).select_option("ruby")
                page.get_by_role("button", name="Apply scope filters").click()
                page.get_by_role("heading", name="No matching evidence").wait_for()

                module = ordinary_report.modules[0].name
                page.get_by_role("combobox", name="Language", exact=True).select_option("python")
                page.get_by_label("Logical module prefix").fill(module)
                page.get_by_role("button", name="Apply scope filters").click()
                page.get_by_role("columnheader", name="Module", exact=True).wait_for()
                assert page.locator("tbody tr").count() >= 1
                module_links = page.locator('tbody tr td:first-child [data-direct-source="true"]')
                assert module_links.count() == page.locator("tbody tr").count()
                assert all(
                    value == module or value.startswith(module + ".")
                    for value in module_links.all_text_contents()
                )
                assert any("language=ruby" in request for request in requests)
                assert any(
                    "language=python" in request and "module_prefix=" in request
                    for request in requests
                )

                page.locator(".lui-shell__menu").click()
                reveal_navigation(page, "Findings").click()
                page.get_by_role("columnheader", name="Finding", exact=True).wait_for()
                finding_module = ordinary_report.findings[0].module
                page.get_by_role("combobox", name="Language", exact=True).select_option("python")
                page.get_by_label("Logical module prefix").fill(finding_module)
                page.get_by_role("button", name="Apply scope filters").click()
                page.get_by_role("columnheader", name="Finding", exact=True).wait_for()
                module_column = page.get_by_role(
                    "columnheader", name="Module", exact=True
                ).evaluate("header => header.cellIndex + 1")
                assert all(
                    value == finding_module or value.startswith(finding_module + ".")
                    for value in page.locator(
                        f"tbody tr td:nth-child({module_column})"
                    ).all_text_contents()
                )
                assert any("/api/findings?" in request for request in requests)

                page.locator(".lui-shell__menu").click()
                reveal_navigation(page, "State / effects").click()
                page.get_by_label("Exact saved owner").fill("owner.not.present")
                page.get_by_role("button", name="Apply scope filters").click()
                page.get_by_role("heading", name="No matching evidence").wait_for()
                assert any(
                    "/api/state-effects?" in request and "owner=owner.not.present" in request
                    for request in requests
                )

                for label, route in (
                    ("Contracts", "/api/contracts?"),
                    ("Dictionary shapes", "/api/dictionary-shapes?"),
                ):
                    page.locator(".lui-shell__menu").click()
                    reveal_navigation(page, label).click()
                    page.get_by_role("combobox", name="Language", exact=True).select_option(
                        "python"
                    )
                    page.get_by_label("Logical module prefix").fill("sample")
                    page.get_by_label("Exact saved owner").fill("owner.not.present")
                    page.get_by_role("button", name="Apply scope filters").click()
                    page.get_by_role("heading", name="No matching evidence").wait_for()
                    assert any(
                        route in request
                        and "language=python" in request
                        and "module_prefix=sample" in request
                        and "owner=owner.not.present" in request
                        for request in requests
                    )

                for label, route in (
                    ("Candidates", "/api/candidates?"),
                    ("Clones", "/api/clones?"),
                    ("Test impact", "/api/test-impact?"),
                ):
                    page.locator(".lui-shell__menu").click()
                    reveal_navigation(page, label).click()
                    if label == "Test impact":
                        section = next(
                            item
                            for item in ordinary_report.section_coverage
                            if item.section == "test-impact"
                        )
                        assert section.state.value == "unavailable"
                        playwright.expect(page.locator("#workflow-notice")).to_contain_text(
                            "Required analyzer evidence"
                        )
                        assert not any("/api/test-impact?" in request for request in requests)
                        continue
                    assert page.get_by_label("Exact saved owner").count() == 0
                    page.get_by_role("combobox", name="Language", exact=True).select_option(
                        "python"
                    )
                    page.get_by_label("Logical module prefix").fill("sample")
                    page.get_by_role("button", name="Apply scope filters").click()
                    page.get_by_role("heading", name="No matching evidence").wait_for()
                    assert any(
                        route in request
                        and "language=python" in request
                        and "module_prefix=sample" in request
                        and "owner=" not in request
                        for request in requests
                    )
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                )
                selected_module = next(item for item in ordinary_report.modules if item.symbols)
                identity = f"{selected_module.name}:{selected_module.symbols[0].qualified_name}"
                selection = WebHandoffSelection(
                    report_sha256=server.app.service.bootstrap().reports[0].report_sha256,
                    kind="symbol",
                    identity=identity,
                )
                # A fragment-only handoff must clear the previous view's owner facet.
                page.goto(
                    selection.url(
                        f"http://127.0.0.1:{server.server_address[1]}/?token=facet-browser"
                    )
                )
                playwright.expect(page.locator("#evidence-title")).to_have_text(identity)
                playwright.expect(page.locator("#evidence-content")).to_contain_text(
                    "Source evidence"
                )
                page.keyboard.press("Escape")
                playwright.expect(page.get_by_label("Logical module prefix")).to_have_value("")
                playwright.expect(
                    page.get_by_role("combobox", name="Language", exact=True)
                ).to_have_value("")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_browser_contract_and_shape_identities_open_saved_evidence() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report = scan(
        discover_context(Path(__file__).parent / "fixtures" / "contracts_project", AnalysisConfig())
    )
    server = create_server(ReportViewService(report), token="domain-browser")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=domain-browser")
                page.locator(".cards").wait_for()
                for label, identity in (
                    ("Contracts", report.contracts[0].contract_id),
                    ("Dictionary shapes", report.dictionary_shapes[0].shape_id),
                ):
                    reveal_navigation(page, label).click()
                    page.get_by_role("button", name=identity, exact=True).click()
                    playwright.expect(page.locator("#evidence-title")).to_have_text(identity)
                    playwright.expect(page.locator("#evidence-content")).to_contain_text(
                        "Source evidence"
                    )
                    page.keyboard.press("Escape")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("fixture", "config", "view", "identity"),
    (
        (
            "clone_project",
            AnalysisConfig(
                clone_thresholds=CloneThresholds(
                    exact_min_lines=3,
                    token_min_tokens=5,
                    ast_min_nodes=5,
                    statement_window=2,
                    sequence_min_events=2,
                    sequence_min_coverage=0.5,
                    commonality_min_ownership_dimensions=2,
                    commonality_min_bilateral_coverage=0.25,
                    commonality_max_feature_subject_fraction=0.10,
                ),
                phase_three_limits=PhaseThreeLimits(max_candidate_pairs=50_000),
            ),
            "Clones",
            lambda report: report.clone_clusters[0].cluster_id,
        ),
        (
            "test_impact_project",
            AnalysisConfig(),
            "Test impact",
            lambda report: report.test_modules[0].test_module_id,
        ),
    ),
)
def test_browser_clone_and_test_identities_open_saved_evidence(
    fixture, config, view, identity
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report = scan(discover_context(Path(__file__).parent / "fixtures" / fixture, config))
    selected = identity(report)
    server = create_server(ReportViewService(report), token="inventory-browser")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=inventory-browser")
                page.locator(".cards").wait_for()
                reveal_navigation(page, view).click()
                page.get_by_role("button", name=selected, exact=True).click()
                playwright.expect(page.locator("#evidence-title")).to_have_text(selected)
                playwright.expect(page.locator("#evidence-content")).to_contain_text(
                    "Recorded source choices" if view == "Test impact" else "Source evidence"
                )
                if view == "Test impact":
                    playwright.expect(page.locator("#evidence-content")).to_contain_text(
                        "not execution or behavioral coverage"
                    )
                    playwright.expect(
                        page.get_by_role("button", name="Preview test source", exact=True)
                    ).to_be_visible()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
