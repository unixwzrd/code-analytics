from __future__ import annotations

import asyncio
import errno

import pytest

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.service import scan
from code_analysis.ui_source import preview_source, preview_source_record
from code_analysis.web_service import ReportViewService


@pytest.fixture
def source_index(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    path = root / "sample.py"
    path.write_text(
        "# line\n" * 230
        + "def sample(value):\n"
        + "    if value:\n        value -= 1\n" * 20
        + "    return value\n"
    )
    report = scan(discover_context(root, AnalysisConfig(test_roots=())))
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), destination)
    return path, report, BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def test_indexed_source_identity_reuses_confined_reader(source_index):
    path, report, index = source_index
    record = index.source_file("sample.py")
    assert record == report.source_files[0]
    assert preview_source_record(record, None).state == "disabled"
    assert preview_source_record(record, path.parent, end=300) == preview_source(
        report, path.parent, "sample.py", end=300
    )
    for selection in ("../sample.py", "/etc/passwd", "other.py"):
        with pytest.raises(ValueError):
            index.source_file(selection)
    path.write_text("changed = True\n")
    changed = preview_source_record(index.source_file("sample.py"), path.parent)
    assert changed.state == "mismatch" and not changed.lines


def test_terminal_preview_rechecks_each_page(source_index):
    pytest.importorskip("textual")
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_source import TerminalSource

    path, _, index = source_index

    async def exercise():
        app = AnalysisTerminal(index, source_root=path.parent)
        async with app.run_test(size=(100, 32)) as pilot:
            screen = TerminalSource(index, path.parent, "sample.py", 1, 300, app.evidence_closed)
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.preview.state == "bound" and len(screen.preview.lines) == 200
            assert screen.preview.next_line == 201
            path.write_text("changed = True\n")
            await pilot.press("ctrl+n")
            await app.workers.wait_for_complete()
            assert screen.preview.state == "mismatch" and not screen.preview.lines
            assert app.exit_status == errno.ENODATA
            await pilot.press("ctrl+c")
        assert app.return_value == errno.ENODATA

    asyncio.run(exercise())


def test_terminal_symbol_to_source_route(source_index):
    pytest.importorskip("textual")
    from textual.widgets import Select

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_modules import ModuleBrowser
    from code_analysis.tui_source import TerminalSource

    path, report, index = source_index

    async def exercise():
        app = AnalysisTerminal(index, source_root=path.parent)
        async with app.run_test(size=(100, 32)) as pilot:
            browser = ModuleBrowser(index, report.modules[0].name, app.evidence_closed, path.parent)
            app.push_screen(browser)
            await pilot.pause()
            browser.query_one("#module-view", Select).value = "symbols"
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RowDetails)
            await pilot.click("#preview-source")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, TerminalSource)
            assert app.screen.preview.state == "bound"
            assert (
                app.screen.preview.lines[0].number == report.modules[0].symbols[0].span.start_line
            )
            await pilot.press("escape")
            assert isinstance(app.screen, RowDetails)
            await pilot.press("ctrl+c")

    asyncio.run(exercise())


def test_terminal_finding_to_source_is_explicitly_opted_in(source_index, tmp_path):
    pytest.importorskip("textual")
    from code_analysis.models import Confidence, Evidence
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_evidence import FindingEvidence
    from code_analysis.tui_source import TerminalSource

    path, report, _ = source_index
    evidence = Evidence(
        span=report.modules[0].symbols[0].span,
        provider="native-ast",
        observation="Synthetic qualified source witness",
        confidence=Confidence.EXACT,
        source_sha256=report.source_files[0].source_sha256,
    )
    finding = report.findings[0].model_copy(update={"evidence": (evidence,)})
    report = report.model_copy(update={"findings": (finding, *report.findings[1:])})
    destination = tmp_path / "evidence-index"
    manifest = build_browse_index(ReportViewService(report), destination)
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(100, 32)) as pilot:
            screen = FindingEvidence(index, finding.finding_id, app.evidence_closed)
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.source_spans
            await pilot.click("#evidence-preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, TerminalSource)
            assert app.screen.preview.state == "disabled" and not app.screen.preview.lines
            await pilot.press("escape")
            screen.preview_root = path.parent
            await pilot.pause(0.25)
            await pilot.click("#evidence-preview")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen.preview.state == "bound"
            await pilot.press("ctrl+c")

    asyncio.run(exercise())
