"""Selected-report authority and browser navigation for static comparison source."""

import json
from pathlib import Path
from urllib.parse import urlencode

import pytest
from browser_navigation import reveal_navigation
from test_web_runtime_comparison_source import TOKEN, fetch, served

from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


@pytest.fixture
def comparison(ordinary_report):
    finding = ordinary_report.findings[0]
    candidate = ordinary_report.model_copy(
        update={"findings": (finding.model_copy(update={"title": "Changed title"}),)}
    )
    service = ReportViewService(ordinary_report, candidate)
    position = next(
        number
        for number, row in enumerate(service.delta.records)
        if row.subject_id == finding.finding_id
    )
    detail = service.static_comparison_source_detail(position)
    selection = {
        "position": position,
        "baseline_sha256": detail.baseline.report_sha256,
        "candidate_sha256": detail.candidate.report_sha256,
    }
    return service, detail, selection


def test_http_static_source_selection_and_authorization(comparison):
    service, detail, selection = comparison
    root = Path(__file__).parent / "fixtures/ordinary"
    with served(create_server(service, source_root=root, token=TOKEN)) as server:
        route = "/api/comparison-detail?" + urlencode(selection)
        assert fetch(server, route, token=None)[0] == 403
        status, body = fetch(server, route)
        assert status == 200 and body == detail.model_dump(mode="json")
        status, filtered = fetch(server, "/api/comparison?kind=module&change=changed")
        assert status == 200 and filtered["records"] == []
        assert filtered["filtered_out"] > 0 and filtered["omitted"] == 0
        assert fetch(server, "/api/comparison?kind=unknown")[0] == 400
        for side in ("baseline", "candidate"):
            params = {
                **selection,
                "side": side,
                "option_id": getattr(detail, side).options[0].option_id,
            }
            status, preview = fetch(server, "/api/comparison-source?" + urlencode(params))
            assert status == 200 and preview["state"] == "bound"
            assert 1 <= len(preview["lines"]) <= 40
        for key in ("baseline_sha256", "candidate_sha256"):
            assert (
                fetch(server, "/api/comparison-detail?" + urlencode({**selection, key: "0" * 64}))[
                    0
                ]
                == 400
            )
        assert fetch(server, route + "&path=foreign.py")[0] == 400


@pytest.mark.parametrize("root_state", ["disabled", "mismatch"])
def test_http_static_source_does_not_disclose_unbound_text(comparison, tmp_path, root_state):
    service, detail, selection = comparison
    option = detail.baseline.options[0]
    root = None
    if root_state == "mismatch":
        root = tmp_path
        source = root / option.logical_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# not captured source\n")
    params = {**selection, "side": "baseline", "option_id": option.option_id}
    with served(create_server(service, source_root=root, token=TOKEN)) as server:
        status, preview = fetch(server, "/api/comparison-source?" + urlencode(params))
        assert status == 200 and preview["state"] == root_state
        assert preview["lines"] == []


def test_browser_static_comparison_source_choices(comparison, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    service, _, _ = comparison
    root = Path(__file__).parent / "fixtures/ordinary"
    with (
        served(create_server(service, source_root=root, token=TOKEN)) as server,
        playwright.sync_playwright() as runtime,
    ):
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 768, "height": 1024})
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}")
            reveal_navigation(page, "Comparison").click()
            page.locator('#comparison-filters input[name="kind"]').fill("module")
            page.get_by_role("button", name="Apply comparison filters").click()
            playwright.expect(page.get_by_role("button", name="Source choices")).to_have_count(0)
            page.locator('#comparison-filters input[name="kind"]').fill("finding")
            page.get_by_role("button", name="Apply comparison filters").click()
            page.get_by_role("button", name="Source choices").first.click()
            with page.expect_download() as download:
                page.locator('[data-comparison-export="json-compact"]').click()
            artifact = tmp_path / "comparison.json"
            download.value.save_as(artifact)
            exported = json.loads(artifact.read_text())
            assert {item["role"] for item in exported["inputs"]} == {"baseline", "candidate"}
            assert len(exported["rows"]) == 1
            page.get_by_role("button", name="Preview baseline").click()
            output = page.locator('[data-source-output="baseline"]')
            playwright.expect(output.locator("pre")).to_be_visible()
            playwright.expect(output).to_contain_text("SHA-256")
            page.get_by_role("button", name="Preview candidate").click()
            playwright.expect(page.locator('[data-source-output="candidate"] pre')).to_be_visible()
            page.locator('#comparison-filters input[name="kind"]').fill("module")
            playwright.expect(
                page.locator('[data-comparison-export="json-compact"]')
            ).to_be_disabled()
        finally:
            browser.close()
