from __future__ import annotations

import hashlib
import os
import threading
from http.client import HTTPConnection
from urllib.parse import urlencode

import pytest
from browser_navigation import reveal_navigation

from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.service import scan
from code_analysis.ui_source import SourceLine, SourcePreview, preview_source
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


@pytest.fixture
def source_report(tmp_path):
    source = tmp_path / "sample.py"
    source.write_text('def sample():\n    return "<script>not executed</script>"\n')
    report = scan(discover_context(tmp_path, AnalysisConfig(test_roots=())))
    return source, report


def test_preview_requires_opt_in_and_matching_source(source_report) -> None:
    source, report = source_report
    assert preview_source(report, None, "sample.py").state == "disabled"
    bound = preview_source(report, source.parent, "sample.py")
    assert bound.state == "bound"
    assert bound.lines[1].text == '    return "<script>not executed</script>"'
    source.write_text("changed = True\n")
    changed = preview_source(report, source.parent, "sample.py")
    assert changed.state == "mismatch"
    assert not changed.lines
    updated = scan(discover_context(source.parent, AnalysisConfig(test_roots=())))
    assert preview_source(updated, source.parent, "sample.py").state == "bound"
    source.unlink()
    assert preview_source(report, source.parent, "sample.py").state == "unavailable"


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../sample.py", "./sample.py", "x//y", "x\\y", "other.py"]
)
def test_preview_rejects_unconfined_or_unknown_paths(source_report, path) -> None:
    source, report = source_report
    with pytest.raises(ValueError):
        preview_source(report, source.parent, path)


def test_preview_rejects_symlinks_and_fifo(source_report) -> None:
    source, report = source_report
    target = source.with_name("outside.py")
    source.rename(target)
    source.symlink_to(target)
    assert preview_source(report, source.parent, "sample.py").state == "unavailable"
    source.unlink()
    os.mkfifo(source)
    assert preview_source(report, source.parent, "sample.py").state == "unavailable"


def test_preview_bounds_and_strict_states(source_report) -> None:
    source, report = source_report
    source.write_text("# comment\n" * 300)
    record = report.source_files[0].model_copy(
        update={"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    )
    report = report.model_copy(update={"source_files": (record,)})
    first = preview_source(report, source.parent, "sample.py", end=300)
    assert len(first.lines) == 200 and first.next_line == 201
    last = preview_source(report, source.parent, "sample.py", start=201, end=300)
    assert len(last.lines) == 100 and last.next_line is None
    with pytest.raises(ValueError):
        preview_source(report, source.parent, "sample.py", start=True)
    with pytest.raises(ValueError):
        SourcePreview(
            logical_path="sample.py",
            expected_sha256=record.source_sha256,
            state="mismatch",
            message="changed",
            lines=(SourceLine(number=1, text="secret"),),
        )
    source.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    assert preview_source(report, source.parent, "sample.py").state == "too-large"


def test_source_endpoint_requires_token(source_report) -> None:
    source, report = source_report
    server = create_server(ReportViewService(report), source_root=source.parent, token="test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        path = "/api/source?" + urlencode({"path": "sample.py"})
        connection.request("GET", path)
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("GET", path, headers={"X-Code-Analysis-Token": "test"})
        response = connection.getresponse()
        assert response.status == 200
        assert SourcePreview.model_validate_json(response.read()).state == "bound"
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_preview_rejects_directory_symlink_and_invalid_encoding(source_report) -> None:
    source, report = source_report
    root = source.parent / "confined"
    root.mkdir()
    (root / "linked").symlink_to(source.parent, target_is_directory=True)
    record = report.source_files[0].model_copy(update={"logical_path": "linked/sample.py"})
    linked = report.model_copy(update={"source_files": (record,)})
    assert preview_source(linked, root, "linked/sample.py").state == "unavailable"
    source.write_bytes(b"\xff")
    record = report.source_files[0].model_copy(
        update={"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    )
    invalid = report.model_copy(update={"source_files": (record,)})
    preview = preview_source(invalid, source.parent, "sample.py")
    assert preview.state == "invalid-encoding" and not preview.lines


def test_ui_forwards_explicit_source_root(source_report, monkeypatch) -> None:
    import code_analysis.cli as cli
    from code_analysis.reports.json_report import canonical_json

    source, report = source_report
    saved = source.parent / "report.json"
    saved.write_text(canonical_json(report))
    seen = []
    monkeypatch.setattr(cli, "_web", lambda args: seen.append(args.source_root) or 0)
    assert cli.main(["ui", str(saved), "--source-root", str(source.parent), "--no-open"]) == 0
    assert seen == [source.parent]


def test_browser_source_preview_is_text_and_rechecks_hash(source_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    source, report = source_report
    server = create_server(ReportViewService(report), source_root=source.parent, token="test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=test")
                reveal_navigation(page, "Architecture").click()
                node = page.get_by_role("button", name="Open module sample", exact=True)
                node.focus()
                page.keyboard.press("Enter")
                page.get_by_role("button", name="Preview source").first.click()
                preview = page.locator(".source-preview").first
                page.wait_for_function(
                    "() => document.querySelector('.source-preview')"
                    ".textContent.startsWith('bound:')"
                )
                assert "<script>not executed</script>" in preview.inner_text()
                assert preview.locator("script").count() == 0
                source.write_text("changed = True\n")
                page.get_by_role("button", name="Preview source").first.click()
                page.wait_for_function(
                    "() => document.querySelector('.source-preview')"
                    ".textContent.startsWith('mismatch:')"
                )
                assert preview.locator("pre").count() == 0
                page.keyboard.press("Escape")
                assert not page.get_by_role("dialog").is_visible()
                assert node.evaluate("element => element === document.activeElement")
                reveal_navigation(page, "Modules").click()
                page.get_by_role("button", name="Module details for sample", exact=True).click()
                page.get_by_role("heading", name="Symbols and source").wait_for()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
