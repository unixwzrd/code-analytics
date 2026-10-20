from __future__ import annotations

import asyncio
import copy
import json

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.browse_export import build_browse_export
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseRequest
from code_analysis.clone_browsing import CloneRow, clone_rows
from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness
from code_analysis.project import discover_context
from code_analysis.service import scan, scan_contract_inventory
from code_analysis.ui_rows import BrowseTable
from code_analysis.web_service import ReportViewService


@pytest.fixture
def clone_index(tmp_path):
    report = scan(discover_context(FIXTURES / "clone_project", AnalysisConfig()))
    service = ReportViewService(report)
    root = tmp_path / "index"
    manifest = build_browse_index(service, root)
    return service, BrowseIndex.open(root, report_sha256=manifest.report_sha256)


def test_clone_inventory_preserves_all_members_and_saved_only_search(clone_index, monkeypatch):
    service, index = clone_index
    rows = tuple(clone_rows(service.primary))
    assert rows
    assert {row.cluster_id for row in rows} == {
        item.cluster_id for item in service.primary.clone_clusters
    }
    fragments = {item.fragment_id: item for item in service.primary.clone_fragments}
    for row in rows:
        assert tuple(member.fragment for member in row.detail.members) == tuple(
            fragments[identity] for identity in row.detail.cluster.fragment_ids
        )
    monkeypatch.setattr(
        ReportViewService, "load", lambda *args: pytest.fail("source report loaded")
    )
    for query in ("", "exact", "package", "not-present", "' OR 1=1"):
        expected = service.clones(offset=0, limit=500, query=query)
        actual = index.page(BrowseRequest(table=BrowseTable.CLONES, limit=500, query=query))
        assert actual.model_dump_json() == expected.model_dump_json()
    projection = build_browse_export(index, BrowseRequest(table=BrowseTable.CLONES, limit=500))
    assert projection.record_count == len(rows)
    assert any("not net deletion" in item for item in projection.limitations)


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject_count", True),
        ("fragment_count", 1000),
        ("matched_nonblank_lines", 999999),
        ("kind", "exact-text"),
        ("confidence", "runtime-observed"),
        ("language", "javascript"),
    ],
)
def test_clone_row_contradictions_fail(clone_index, field, value):
    row = clone_index[1].page(BrowseRequest(table=BrowseTable.CLONES)).records[0]
    payload = json.loads(row.model_dump_json())
    payload[field] = value
    assert getattr(row, field) != value
    with pytest.raises(ValidationError):
        CloneRow.model_validate_json(json.dumps(payload))


def test_clone_member_omission_rejected(clone_index):
    row = clone_index[1].page(BrowseRequest(table=BrowseTable.CLONES)).records[0]
    payload = json.loads(row.model_dump_json())
    payload["detail"]["members"].pop()
    with pytest.raises(ValidationError):
        CloneRow.model_validate_json(json.dumps(payload))


def test_disabled_clones_stay_explicit(tmp_path):
    report = scan_contract_inventory(
        discover_context(FIXTURES / "contract_golden", AnalysisConfig())
    )
    root = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), root)
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    request = BrowseRequest(table=BrowseTable.CLONES)
    assert index.page(request).coverage.state.value == "disabled"
    assert build_browse_export(index, request).completeness is not Completeness.COMPLETE


def test_terminal_clone_comparison_and_disabled_source_preview(clone_index):
    pytest.importorskip("textual")
    from textual.widgets import DataTable

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_clones import CloneComparison
    from code_analysis.tui_source import TerminalSource

    _, index = clone_index

    async def exercise():
        app = AnalysisTerminal(index, page_size=1)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("f9")
            await app.workers.wait_for_complete()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, CloneComparison)
            assert app.screen.query_one("#clone-members", DataTable).row_count == min(
                25, app.screen.row.fragment_count
            )
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RowDetails)
            assert "fingerprint_sha256" in app.screen.detail
            assert "counter_evidence" in app.screen.detail
            await pilot.click("#preview-source")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, TerminalSource)
            assert app.screen.preview is not None
            assert app.screen.preview.state == "disabled"
            assert not app.screen.preview.lines

    asyncio.run(exercise())


def test_terminal_clone_member_continuation_retains_every_identity(clone_index):
    """A synthetic UI contract exercises a cluster larger than one widget page."""
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_clones import CloneComparison

    _, index = clone_index
    row = index.page(BrowseRequest(table=BrowseTable.CLONES)).records[0]
    payload = json.loads(row.model_dump_json())
    template = payload["detail"]["members"][0]
    members = []
    for number in range(61):
        member = copy.deepcopy(template)
        subject_id = f"subject-{number:020x}"
        member["subject"]["subject_id"] = subject_id
        member["fragment"]["subject_id"] = subject_id
        member["fragment"]["fragment_id"] = f"clone-fragment-{number:020x}"
        members.append(member)
    payload["detail"]["members"] = members
    cluster = payload["detail"]["cluster"]
    cluster["fragment_ids"] = [item["fragment"]["fragment_id"] for item in members]
    cluster["subject_ids"] = [item["subject"]["subject_id"] for item in members]
    payload["fragment_count"] = payload["subject_count"] = 61
    payload["modules"] = [template["subject"]["module"]]
    payload["matched_nonblank_lines"] = 61 * template["fragment"]["nonblank_lines"]
    expanded = CloneRow.model_validate_json(json.dumps(payload))

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(120, 40)) as pilot:
            await app.workers.wait_for_complete()
            screen = CloneComparison(index, expanded, lambda status: pytest.fail(str(status)))
            await app.push_screen(screen)
            seen = []
            for offset, count in ((0, 25), (25, 25), (50, 11)):
                table = screen.query_one("#clone-members", DataTable)
                assert screen.member_offset == offset
                assert table.row_count == count
                seen.extend(key.value for key in table.rows)
                await pilot.press("ctrl+n")
            assert seen == cluster["fragment_ids"]
            assert screen.query_one("#clone-next", Button).disabled
            await pilot.press("ctrl+p")
            assert screen.member_offset == 25
            await pilot.press("ctrl+p")
            assert screen.member_offset == 0
            assert screen.query_one("#clone-previous", Button).disabled
            await pilot.press("escape")
            assert not isinstance(app.screen, CloneComparison)

    asyncio.run(exercise())
