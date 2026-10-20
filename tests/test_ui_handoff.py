from __future__ import annotations

import asyncio
import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from code_analysis.ui_handoff import WebHandoffSelection
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


def selections(service):
    report = service.primary
    module = next(item for item in report.modules if item.symbols)
    common = {"report_sha256": service.bootstrap().reports[0].report_sha256}
    return (
        WebHandoffSelection(**common),
        WebHandoffSelection(
            **common, kind="symbol", identity=f"{module.name}:{module.symbols[0].qualified_name}"
        ),
        WebHandoffSelection(**common, kind="graph", identity=module.name),
        WebHandoffSelection(**common, kind="graph"),
        WebHandoffSelection(**common, kind="finding", identity=report.findings[0].finding_id),
    )


def test_handoff_is_bound_and_source_free(ordinary_report):
    service = ReportViewService(ordinary_report)
    for selection in selections(service):
        selection.validate_report(service)
        url = selection.url("http://127.0.0.1:8000/?token=owned")
        assert parse_qs(urlsplit(url).query) == {"token": ["owned"]}
        assert parse_qs(urlsplit(url).fragment)["report_sha256"] == [selection.report_sha256]
    with pytest.raises(ValueError, match="identity does not match"):
        WebHandoffSelection(report_sha256="0" * 64).validate_report(service)
    with pytest.raises(ValueError, match="subject does not exist"):
        WebHandoffSelection(
            report_sha256=service.bootstrap().reports[0].report_sha256,
            kind="symbol",
            identity="unknown:unknown",
        ).validate_report(service)


@pytest.mark.parametrize(
    "options",
    [
        {"kind": "unknown"},
        {"kind": "finding"},
        {"identity": "unexpected"},
        {"kind": "symbol", "identity": True},
        {"extra": 1},
    ],
)
def test_handoff_closed_contract(options):
    with pytest.raises(ValidationError):
        WebHandoffSelection(report_sha256="0" * 64, **options)


def test_handoff_url_rejects_nonlocal_or_ambiguous_targets():
    selection = WebHandoffSelection(report_sha256="0" * 64)
    for url in (
        "https://example.com",
        "http://user@localhost",
        "file:///tmp/report",
        "http://localhost/other",
        "http://localhost/#old",
    ):
        with pytest.raises(ValueError):
            selection.url(url)


@pytest.fixture
def server(ordinary_report):
    instance = create_server(ReportViewService(ordinary_report), token="handoff-test")
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    thread.join(5)
    instance.server_close()


def test_handoff_endpoint_validates_hash_and_keeps_authentication(server):
    selection = selections(server.app.service)[1]
    path = "/api/handoff?" + urlsplit(selection.url("http://localhost/")).fragment
    connection = HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        for request_path, status in (
            (path, 200),
            (path.replace(selection.report_sha256, "0" * 64), 400),
        ):
            connection.request(
                "GET", request_path, headers={"X-Code-Analysis-Token": "handoff-test"}
            )
            response = connection.getresponse()
            assert response.status == status
            value = json.loads(response.read())
            if status == 200:
                assert value == selection.model_dump(mode="json")
    finally:
        connection.close()


@pytest.mark.parametrize("selected", [0, 1, 2, 3, 4])
def test_browser_opens_exact_selection_and_rejects_wrong_report(server, selected):
    playwright = pytest.importorskip("playwright.sync_api")
    selection = selections(server.app.service)[selected]
    base = f"http://127.0.0.1:{server.server_port}/?token=handoff-test"
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(selection.url(base))
            if selection.identity:
                playwright.expect(page.locator("#evidence-title")).to_have_text(selection.identity)
                playwright.expect(page.locator("#evidence-content")).to_contain_text(
                    "Source completeness" if selection.kind == "graph" else "Source evidence"
                )
            else:
                playwright.expect(page.locator("#title")).to_have_text(
                    "Architecture" if selection.kind == "graph" else "Overview"
                )
            page.goto(WebHandoffSelection(report_sha256="0" * 64).url(base))
            playwright.expect(page.locator("#content")).to_contain_text(
                "handoff report identity does not match"
            )
            assert not page.locator("#evidence-dialog").is_visible()
        finally:
            browser.close()


@pytest.mark.parametrize("size", [(100, 32), (80, 24)])
def test_terminal_prepares_explicit_report_and_launches_only_on_request(
    ordinary_report, tmp_path, monkeypatch, size
):
    pytest.importorskip("textual")
    from textual.widgets import Button, Input, TextArea

    from code_analysis.artifact_viewer import ViewerCommand
    from code_analysis.browse_index import BrowseIndex
    from code_analysis.browse_index_build import build_browse_index
    from code_analysis.reports.json_report import canonical_json
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_handoff import WebHandoffDialog

    report = tmp_path / "report.json"
    report.write_text(canonical_json(ordinary_report))
    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), directory)
    index = BrowseIndex.open(directory, report_sha256=manifest.report_sha256)
    launches = []
    monkeypatch.setattr(
        "code_analysis.tui_handoff.launch_display",
        lambda url, command: launches.append((url, command)),
    )
    browser = ViewerCommand(arguments=("/bin/browser",))
    copies = []

    async def exercise():
        app = AnalysisTerminal(index, browser=browser)
        monkeypatch.setattr(app, "copy_to_clipboard", copies.append)
        async with app.run_test(size=size) as pilot:
            await pilot.press("ctrl+w")
            await pilot.pause()
            assert isinstance(app.screen, WebHandoffDialog)
            assert app.screen.selection.kind == "report"
            app.screen.query_one("#handoff-report", Input).value = str(report)
            await pilot.click("#handoff-prepare")
            await app.screen.workers.wait_for_complete()
            await pilot.pause()
            url = app.screen.query_one("#handoff-url", TextArea).text
            assert url.startswith("http://127.0.0.1:")
            assert urlsplit(url).fragment.endswith("kind=report")
            assert not app.screen.query_one("#handoff-open", Button).disabled
            assert not app.screen.query_one("#handoff-copy", Button).disabled
            assert launches == []
            await pilot.click("#handoff-copy")
            assert copies == [url]
            await pilot.click("#handoff-open")
            await app.screen.workers.wait_for_complete()
            assert launches == [(url, browser)]
            await pilot.press("escape")
            assert app.report_path == report
            await pilot.press("ctrl+q")
        selection = WebHandoffSelection(report_sha256=manifest.report_sha256)
        with pytest.raises(OSError, match="closed"):
            app.web_handoff.open(report, selection)

    asyncio.run(exercise())


def test_terminal_handoff_controls_preserve_exact_finding_module_and_symbol(
    ordinary_report, tmp_path
):
    pytest.importorskip("textual")
    from textual.widgets import Button, Select

    from code_analysis.browse_index import BrowseIndex
    from code_analysis.browse_index_build import build_browse_index
    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_evidence import FindingEvidence
    from code_analysis.tui_handoff import WebHandoffDialog
    from code_analysis.tui_modules import ModuleBrowser

    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), directory)
    index = BrowseIndex.open(directory, report_sha256=manifest.report_sha256)
    module = next(item for item in ordinary_report.modules if item.symbols)

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f3")
            await app.workers.wait_for_complete()
            finding_id = app.current_page.records[0].finding_id
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, FindingEvidence)
            app.screen.query_one("#evidence-web", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, WebHandoffDialog)
            assert app.screen.selection.kind == "finding"
            assert app.screen.selection.identity == finding_id
            await pilot.press("escape", "escape")

            browser = ModuleBrowser(
                index, module.name, app.evidence_closed, web_action=app.open_web_handoff
            )
            app.push_screen(browser)
            await pilot.pause()
            await browser.workers.wait_for_complete()
            await pilot.click("#module-web")
            await pilot.pause()
            assert isinstance(app.screen, WebHandoffDialog)
            assert app.screen.selection.kind == "graph"
            assert app.screen.selection.identity == module.name
            await pilot.press("escape")
            browser.query_one("#module-view", Select).value = "symbols"
            await browser.workers.wait_for_complete()
            await pilot.pause()
            symbol = browser.page.symbols[0]
            await pilot.press("enter")
            assert isinstance(app.screen, RowDetails)
            await pilot.click("#row-web")
            await pilot.pause()
            assert isinstance(app.screen, WebHandoffDialog)
            assert app.screen.selection.kind == "symbol"
            assert app.screen.selection.identity == (f"{module.name}:{symbol.qualified_name}")
            await pilot.press("ctrl+q")

    asyncio.run(exercise())


def test_terminal_discards_prepare_result_after_report_path_changes(ordinary_report, tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Button, Input, Static, TextArea

    from code_analysis.browse_index import BrowseIndex
    from code_analysis.browse_index_build import build_browse_index
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_handoff import WebHandoffDialog

    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), directory)
    index = BrowseIndex.open(directory, report_sha256=manifest.report_sha256)
    started = threading.Event()
    release = threading.Event()

    class DelayedOwner:
        def open(self, report: Path, selection: WebHandoffSelection) -> str:
            started.set()
            release.wait(5)
            return selection.url("http://127.0.0.1:8000/?token=delayed")

        def close(self) -> None:
            release.set()

    async def exercise():
        app = AnalysisTerminal(index)
        app.web_handoff = DelayedOwner()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("ctrl+w")
            screen = app.screen
            assert isinstance(screen, WebHandoffDialog)
            screen.query_one("#handoff-report", Input).value = "first.json"
            await pilot.click("#handoff-prepare")
            assert await asyncio.to_thread(started.wait, 2)
            screen.query_one("#handoff-report", Input).value = "second.json"
            release.set()
            await screen.workers.wait_for_complete()
            await pilot.pause()
            assert screen.query_one("#handoff-url", TextArea).text == ""
            assert screen.query_one("#handoff-open", Button).disabled
            assert "changed" in str(screen.query_one("#handoff-status", Static).content)
            await pilot.press("ctrl+q")

    asyncio.run(exercise())
