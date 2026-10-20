"""Real-browser gates for bounded, source-free projection heatmaps."""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest
from browser_navigation import reveal_navigation

from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.service import scan
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

FIXTURES = Path(__file__).parent / "fixtures"


def _source_free_report(tmp_path: Path, fixture: str):
    source = tmp_path / fixture
    shutil.copytree(FIXTURES / fixture, source)
    report = scan(discover_context(source, AnalysisConfig()))
    shutil.rmtree(source)
    assert not source.exists()
    return report


def _serve(report, token: str):
    server = create_server(ReportViewService(report), token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_complexity_heatmap_uses_exact_saved_values_and_keyboard_table_toggle(
    tmp_path: Path,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report = _source_free_report(tmp_path, "ordinary")
    projection = build_projection(report, ProjectionName.COMPLEXITY, max_records=100)
    expected = [
        [row.cells[position].integer_value for position in (5, 6, 7, 8, 9)]
        for row in projection.rows
    ]
    server, thread = _serve(report, "complexity-heatmap")
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(
                    f"http://127.0.0.1:{server.server_address[1]}/" "?token=complexity-heatmap"
                )
                page.locator(".cards").wait_for()
                page.locator(".lui-shell__menu").click()
                reveal_navigation(page, "Explore evidence").click()
                page.get_by_label("Evidence view", exact=True).select_option("complexity")
                page.get_by_role("button", name="Show evidence", exact=True).click()

                heatmap = page.locator('[data-projection-panel="heatmap"]')
                playwright.expect(
                    heatmap.get_by_role("heading", name="Complexity heatmap", exact=True)
                ).to_be_visible()
                rows = heatmap.locator("tbody tr")
                assert rows.count() == len(expected)
                for row_index, values in enumerate(expected):
                    rendered = [
                        int(value) for value in rows.nth(row_index).locator("td").all_inner_texts()
                    ]
                    assert rendered == values
                legend = heatmap.locator(".heatmap-legends")
                playwright.expect(legend).to_contain_text("Physical LOC")
                playwright.expect(legend).to_contain_text("retained values")
                playwright.expect(heatmap).to_contain_text(
                    "Weighted score remains available in the table"
                )
                limitations = page.locator(".projection-limitations")
                playwright.expect(limitations).not_to_have_attribute("open", "")
                limitations.locator("summary").focus()
                page.keyboard.press("Enter")
                playwright.expect(limitations).to_have_attribute("open", "")
                playwright.expect(limitations).to_contain_text(
                    "Complexity and orchestration classifications"
                )
                wrap = heatmap.locator(".heatmap-wrap")
                assert wrap.evaluate("node => node.scrollWidth >= node.clientWidth")
                playwright.expect(wrap).to_have_attribute("tabindex", "0")
                playwright.expect(wrap).to_have_attribute("aria-label", "Complexity heatmap values")
                wrap.focus()
                playwright.expect(wrap).to_be_focused()
                playwright.expect(heatmap).to_contain_text(
                    "A constant all-zero column uses the lowest shade"
                )

                table_button = page.get_by_role("button", name="Table", exact=True)
                table_button.focus()
                page.keyboard.press("Enter")
                playwright.expect(table_button).to_have_attribute("aria-pressed", "true")
                playwright.expect(heatmap).to_be_hidden()
                table_panel = page.locator('[data-projection-panel="table"]')
                playwright.expect(table_panel).to_be_visible()
                playwright.expect(
                    table_panel.get_by_role("columnheader", name="Weighted score", exact=True)
                ).to_be_visible()
                heatmap_button = page.get_by_role("button", name="Heatmap", exact=True)
                heatmap_button.focus()
                page.keyboard.press("Space")
                playwright.expect(heatmap).to_be_visible()
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_ownership_heatmap_discloses_static_roles_and_preserves_lifecycle_table(
    tmp_path: Path,
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report = _source_free_report(tmp_path, "structure_project")
    projection = build_projection(report, ProjectionName.STRUCTURE_LIFECYCLE)
    expected = [
        [row.cells[position].integer_value for position in (4, 5, 6, 7, 8, 10, 11)]
        for row in projection.rows
    ]
    server, thread = _serve(report, "ownership-heatmap")
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(
                    f"http://127.0.0.1:{server.server_address[1]}/" "?token=ownership-heatmap"
                )
                page.locator(".cards").wait_for()
                reveal_navigation(page, "Explore evidence").click()
                page.get_by_label("Evidence view", exact=True).select_option("structure-lifecycle")
                page.get_by_role("button", name="Show evidence", exact=True).click()
                heatmap = page.locator('[data-projection-panel="heatmap"]')
                playwright.expect(
                    heatmap.get_by_role("heading", name="Ownership evidence heatmap", exact=True)
                ).to_be_visible()
                playwright.expect(heatmap).to_contain_text("do not prove runtime ownership")
                rows = heatmap.locator("tbody tr")
                assert rows.count() == len(expected)
                for row_index, values in enumerate(expected):
                    rendered = [
                        int(value) for value in rows.nth(row_index).locator("td").all_inner_texts()
                    ]
                    assert rendered == values
                page.get_by_role("button", name="Table", exact=True).click()
                table_panel = page.locator('[data-projection-panel="table"]')
                playwright.expect(
                    table_panel.get_by_role("columnheader", name="Lifetime", exact=True)
                ).to_be_visible()
                playwright.expect(
                    table_panel.get_by_role("columnheader", name="Unresolved", exact=True)
                ).to_be_visible()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_missing_heatmap_value_is_not_rendered_as_zero(tmp_path: Path) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report = _source_free_report(tmp_path, "ordinary")
    server, thread = _serve(report, "missing-heatmap")
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()

                def remove_first_complexity_value(route) -> None:
                    response = route.fetch()
                    payload = response.json()
                    if payload.get("title") == "Complexity hotspots":
                        position = next(
                            index
                            for index, column in enumerate(payload["columns"])
                            if column["column_id"] == "branches"
                        )
                        payload["rows"][0]["cells"][position]["integer_value"] = None
                    route.fulfill(
                        status=response.status,
                        headers={"content-type": "application/json"},
                        body=json.dumps(payload),
                    )

                page.route("**/api/projection?*", remove_first_complexity_value)
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/" "?token=missing-heatmap")
                page.locator(".cards").wait_for()
                reveal_navigation(page, "Explore evidence").click()
                page.get_by_label("Evidence view", exact=True).select_option("complexity")
                page.get_by_role("button", name="Show evidence", exact=True).click()
                heatmap = page.locator('[data-projection-panel="heatmap"]')
                missing = heatmap.locator("tbody tr").first.locator("td").nth(1)
                playwright.expect(missing).to_have_class("heatmap-cell missing")
                playwright.expect(missing).to_have_attribute("aria-label", "Branches: missing")
                playwright.expect(missing).to_contain_text("Missing")
                playwright.expect(heatmap).to_contain_text(
                    "An em dash means the saved projection has no numeric value"
                )
                legend = heatmap.locator(".heatmap-legends li").filter(has_text="Branches")
                playwright.expect(legend).to_contain_text("3 retained values")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_bounded_and_empty_heatmaps_keep_explicit_retained_state(tmp_path: Path) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    report = _source_free_report(tmp_path, "ordinary")
    assert len(build_projection(report, ProjectionName.COMPLEXITY).rows) > 1
    server, thread = _serve(report, "bounded-empty-heatmap")
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                bounded = browser.new_page()
                bounded.goto(
                    f"http://127.0.0.1:{server.server_address[1]}/" "?token=bounded-empty-heatmap"
                )
                bounded.locator(".cards").wait_for()
                reveal_navigation(bounded, "Explore evidence").click()
                bounded.get_by_label("Evidence view", exact=True).select_option("complexity")
                bounded.locator("#projection-controls details.lui-disclosure > summary").click()
                bounded.get_by_label("Records", exact=True).fill("1")
                bounded.get_by_role("button", name="Show evidence", exact=True).click()
                result = bounded.locator("#projection-result")
                playwright.expect(result).to_contain_text("Bounded partial view")
                heatmap = bounded.locator('[data-projection-panel="heatmap"]')
                assert heatmap.locator("tbody tr").count() == 1
                legends = heatmap.locator(".heatmap-legends li")
                assert legends.count() == 5
                for index in range(legends.count()):
                    playwright.expect(legends.nth(index)).to_contain_text("1 retained values")

                empty = browser.new_page()
                errors: list[str] = []
                empty.on("pageerror", lambda error: errors.append(str(error)))

                def remove_complexity_rows(route) -> None:
                    response = route.fetch()
                    payload = response.json()
                    if payload.get("title") == "Complexity hotspots":
                        payload["rows"] = []
                        payload["record_count"] = 0
                        payload["truncated"] = False
                        payload["truncation_reason"] = None
                    route.fulfill(
                        status=response.status,
                        headers={"content-type": "application/json"},
                        body=json.dumps(payload),
                    )

                empty.route("**/api/projection?*", remove_complexity_rows)
                empty.goto(
                    f"http://127.0.0.1:{server.server_address[1]}/" "?token=bounded-empty-heatmap"
                )
                empty.locator(".cards").wait_for()
                reveal_navigation(empty, "Explore evidence").click()
                empty.get_by_label("Evidence view", exact=True).select_option("complexity")
                empty.get_by_role("button", name="Show evidence", exact=True).click()
                empty_heatmap = empty.locator('[data-projection-panel="heatmap"]')
                playwright.expect(
                    empty_heatmap.get_by_role("heading", name="Complexity heatmap", exact=True)
                ).to_be_visible()
                assert empty_heatmap.locator("tbody tr").count() == 0
                empty_legends = empty_heatmap.locator(".heatmap-legends li")
                assert empty_legends.count() == 5
                for index in range(empty_legends.count()):
                    playwright.expect(empty_legends.nth(index)).to_contain_text(
                        "no retained numeric values"
                    )
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
