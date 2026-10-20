"""Saved-only list disclosures and comparison inspection without losing table context."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from browser_navigation import reveal_navigation
from test_web_runtime_comparison_source import served

from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json
from code_analysis.service import scan
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

OUTPUT = Path(__file__).resolve().parents[1] / "output/playwright/20261019-review-details"
PROVENANCE = "Literal provenance <img src=x onerror='window.injected=true'>& " + "x" * 400
LONG_OBSERVATION = "Second exact observation.\n" * 20


@pytest.fixture(scope="module")
def review_report(tmp_path_factory):
    root = tmp_path_factory.mktemp("review-details") / "source"
    root.mkdir()
    for number in range(8):
        name = f"module_{number}_" + "long_recorded_module_identity_" * 4
        (root / f"{name}.py").write_text(
            "def inspect_record(value):\n"
            "    result = []\n"
            "    if not value:\n"
            "        raise ValueError('record required')\n"
            "    for item in value:\n"
            "        if item:\n"
            "            result.append(str(item))\n"
            "    return tuple(result)\n"
        )
    report = scan(discover_context(root, AnalysisConfig()))
    providers = list(report.providers)
    providers[0] = providers[0].model_copy(update={"observations": (PROVENANCE, LONG_OBSERVATION)})
    report = AnalysisReport.model_validate_json(
        report.model_copy(update={"providers": tuple(providers)}).model_dump_json()
    )
    assert report.clone_clusters and len(report.findings) >= 2
    shutil.rmtree(root)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "review-report.json").write_text(canonical_json(report))
    (OUTPUT / "candidate-report.json").write_text(canonical_json(_comparison(report).comparison))
    return report


def _comparison(report):
    candidate = report.model_copy(
        update={
            "findings": tuple(
                item.model_copy(update={"title": item.title + " candidate"})
                for item in report.findings
            )
        }
    )
    return ReportViewService(report, candidate)


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
@pytest.mark.parametrize("width", (1920, 1024))
@pytest.mark.parametrize("theme", ("light", "dark"))
def test_review_lists_and_comparison_return_keep_exact_saved_evidence(
    review_report, monkeypatch, engine, width, theme
):
    playwright = pytest.importorskip("playwright.sync_api")
    original = ReportViewService.comparison_records

    def small_page(self, **kwargs):
        # Exercise real stable positions/paging with a smaller server-owned page.
        return original(self, **{**kwargs, "limit": 1})

    monkeypatch.setattr(ReportViewService, "comparison_records", small_page)
    service = _comparison(review_report)
    with (
        served(create_server(service, token="review-layout")) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 1100}, color_scheme=theme)
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=review-layout")
            reveal_navigation(page, "Providers").click()
            details = page.locator(".provider-observations").first
            details.wait_for()
            assert not details.evaluate("node => node.open")
            before = len(requests)
            details.locator("summary").focus()
            page.keyboard.press("Enter")
            assert details.locator("li").all_text_contents() == [
                PROVENANCE,
                LONG_OBSERVATION,
            ]
            assert not details.locator("img").count()
            assert len(requests) == before
            assert details.bounding_box()["width"] <= 416
            assert details.locator("ol").evaluate("node => node.scrollWidth <= node.clientWidth")
            assert details.locator("ol").evaluate("node => node.scrollHeight <= node.clientHeight")
            details.locator("summary").click()
            assert details.locator("xpath=ancestor::tr").bounding_box()["height"] <= 90
            provider_geometry = page.locator(".providers-table .lui-data-table__scroll").evaluate(
                """node => ({left: node.scrollLeft, width: node.clientWidth,
                  table: node.querySelector('table').getBoundingClientRect().width,
                  columns: [...node.querySelectorAll('th')].map(cell => ({
                    width: cell.getBoundingClientRect().width,
                    style: cell.getAttribute('style')}))})"""
            )
            assert (
                provider_geometry["left"] == 0
                # The toolkit's resize handle intentionally overhangs the last header.
                and provider_geometry["table"] <= provider_geometry["width"] + 1
            ), provider_geometry
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            OUTPUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUTPUT / f"{engine}-{width}-{theme}-providers.png"))

            reveal_navigation(page, "Clones").click()
            details = page.locator(".clone-modules").first
            details.wait_for()
            assert not details.evaluate("node => node.open")
            details.locator("summary").click()
            expected = service.clones(offset=0, limit=50, query="").records[0].modules
            assert details.locator("li").all_text_contents() == list(expected)
            assert len(expected) == 8
            assert details.bounding_box()["width"] <= 416
            modules = details.get_by_role("region", name="Recorded clone module identities")
            # Names wrap inside the wider module column: no nested horizontal
            # scroll region is required to read an exact identity in either engine.
            assert modules.evaluate("node => node.scrollWidth <= node.clientWidth")
            assert modules.evaluate("node => node.scrollHeight <= node.clientHeight")
            page.screenshot(path=str(OUTPUT / f"{engine}-{width}-{theme}-clones.png"))

            reveal_navigation(page, "Comparison").click()
            kind = page.locator('#comparison-filters input[name="kind"]')
            kind.fill("finding")
            page.get_by_role("button", name="Apply comparison filters").click()
            page.get_by_text("Showing 1\u20131", exact=False).wait_for()
            page.get_by_role("button", name="Next", exact=True).click()
            page.get_by_text("Showing 2\u20132", exact=False).wait_for()
            context = page.get_by_role("region", name="Selected comparison reports")
            context.locator("summary").click()
            for identity in service.bootstrap().reports:
                assert identity.report_sha256 in context.inner_text()
            context.locator("summary").click()
            source = page.get_by_role("button", name="Source choices", exact=True)
            source.scroll_into_view_if_needed()
            scroller = page.locator(".lui-shell__content")
            table_scroller = source.locator(
                'xpath=ancestor::div[contains(@class,"lui-data-table__scroll")][1]'
            )
            scroll_top = scroller.evaluate("node => node.scrollTop")
            scroll_left = table_scroller.evaluate("node => node.scrollLeft")
            position = source.get_attribute("data-comparison-position")
            source.focus()
            page.keyboard.press("Enter")
            panel = page.locator("#comparison-source")
            playwright.expect(panel).to_be_focused()
            page.get_by_role("button", name="Preview baseline", exact=True).wait_for()
            assert panel.bounding_box()["y"] >= 0
            assert panel.bounding_box()["y"] < 1100
            assert not any("/api/comparison-source?" in url for url in requests)
            page.get_by_role("button", name="Preview baseline", exact=True).click()
            playwright.expect(panel.locator('[data-source-output="baseline"]')).to_contain_text(
                "disabled"
            )
            assert not panel.locator("pre").count()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(OUTPUT / f"{engine}-{width}-{theme}-comparison.png"))
            page.get_by_role("button", name="Return to selected row", exact=True).click()
            playwright.expect(panel).to_be_hidden()
            playwright.expect(source).to_be_focused()
            assert source.get_attribute("data-comparison-position") == position
            assert source.get_attribute("aria-expanded") == "false"
            assert kind.input_value() == "finding"
            assert abs(scroller.evaluate("node => node.scrollTop") - scroll_top) <= 2
            assert abs(table_scroller.evaluate("node => node.scrollLeft") - scroll_left) <= 2
            page.get_by_text("Showing 2\u20132", exact=False).wait_for()
            source.click()
            page.get_by_role("button", name="Preview baseline", exact=True).wait_for()
            kind.fill("module")
            playwright.expect(panel).to_be_hidden()
            assert not errors, errors
            assert not any("/api/collection/" in url for url in requests)
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
def test_bound_preview_and_return_survive_table_column_rebuild(tmp_path, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    source = tmp_path / "entry.py"
    source.write_text(
        "def inspect_record(value):\n"
        + f"    marker = {'x' * 400!r}\n"
        + "".join(f"    if value == {number}:\n        value += 1\n" for number in range(25))
        + "    return value, marker\n"
    )
    report = scan(discover_context(tmp_path, AnalysisConfig()))
    assert report.findings
    with (
        served(
            create_server(_comparison(report), source_root=tmp_path, token="bound-review")
        ) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=bound-review")
            reveal_navigation(page, "Comparison").click()
            page.locator('#comparison-filters input[name="kind"]').fill("finding")
            page.get_by_role("button", name="Apply comparison filters").click()
            source_button = page.get_by_role("button", name="Source choices", exact=True).first
            position = source_button.get_attribute("data-comparison-position")
            old_button = source_button.element_handle()
            source_button.click()
            page.get_by_role("button", name="Preview baseline", exact=True).click()
            preview = page.locator('[data-source-output="baseline"] pre')
            playwright.expect(preview).to_be_visible()
            assert "x" * 400 in preview.inner_text()
            assert preview.bounding_box()["height"] > 384
            assert preview.evaluate("node => node.scrollHeight <= node.clientHeight")
            assert preview.evaluate("node => node.scrollWidth <= node.clientWidth")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            table = page.locator('[data-localops-report-table="Comparison"]')
            table.locator("summary").click()
            table.get_by_title("Move Explanation left", exact=True).click()
            assert not old_button.evaluate("node => node.isConnected")
            playwright.expect(source_button).to_have_attribute("aria-expanded", "true")
            page.get_by_role("button", name="Return to selected row", exact=True).click()
            playwright.expect(source_button).to_be_focused()
            assert source_button.get_attribute("data-comparison-position") == position

            source_button.click()
            page.get_by_role("button", name="Preview baseline", exact=True).wait_for()
            columns = table.locator("details")
            if not columns.evaluate("node => node.open"):
                columns.locator("summary").click()
            table.get_by_role("checkbox", name="Show Source column", exact=True).uncheck()
            page.get_by_role("button", name="Return to selected row", exact=True).click()
            playwright.expect(page.locator("#comparison-source")).to_be_hidden()
            playwright.expect(
                page.get_by_role("button", name="Apply comparison filters", exact=True)
            ).to_be_focused()
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ("chromium", "webkit"))
def test_comparison_return_and_navigation_discard_delayed_source_results(review_report, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    with (
        served(create_server(_comparison(review_report), token="delayed-review")) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.add_init_script("""const originalFetch = window.fetch.bind(window);
                window.fetch = (...args) => {
                  const result = originalFetch(...args);
                  if (!String(args[0]).includes('/api/comparison-detail?')) return result;
                  return result.then(response => new Promise(resolve => {
                    window.releaseComparisonDetail = () => resolve(response);
                  }));
                };""")
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=delayed-review")
            reveal_navigation(page, "Comparison").click()
            page.get_by_role("button", name="Source choices", exact=True).first.click()
            page.wait_for_function("() => typeof window.releaseComparisonDetail === 'function'")
            page.get_by_role("button", name="Return to selected row", exact=True).click()
            page.evaluate("window.releaseComparisonDetail()")
            page.wait_for_timeout(100)
            assert page.locator("#comparison-source").is_hidden()
            assert not page.get_by_role("button", name="Preview baseline").count()
            page.evaluate("delete window.releaseComparisonDetail")
            page.get_by_role("button", name="Source choices", exact=True).first.click()
            page.wait_for_function("() => typeof window.releaseComparisonDetail === 'function'")
            reveal_navigation(page, "Modules").click()
            page.evaluate("window.releaseComparisonDetail()")
            page.wait_for_timeout(100)
            assert page.locator("#title").inner_text() == "Modules"
            assert not page.locator("#comparison-source").count()
            assert not page.get_by_role("button", name="Preview baseline").count()
        finally:
            browser.close()
