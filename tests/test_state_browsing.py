from __future__ import annotations

import asyncio
import json

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.browse_export import build_browse_export
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseRequest
from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness
from code_analysis.project import discover_context
from code_analysis.service import scan, scan_contract_inventory
from code_analysis.ui_models import UIStateEffectRow
from code_analysis.ui_rows import BrowseTable, state_effect_rows
from code_analysis.web_service import ReportViewService


@pytest.fixture
def state_index(tmp_path):
    report = scan(discover_context(FIXTURES / "state_effect_project", AnalysisConfig()))
    service = ReportViewService(report)
    root = tmp_path / "index"
    manifest = build_browse_index(service, root)
    return service, BrowseIndex.open(root, report_sha256=manifest.report_sha256)


def test_state_index_preserves_typed_evidence_and_shared_search(state_index, monkeypatch):
    service, index = state_index
    rows = tuple(state_effect_rows(service.primary))
    assert {item.record_kind for item in rows} == {"resource", "side-effect"}
    assert tuple(row.detail for row in rows if row.record_kind == "resource") == tuple(
        sorted(service.primary.state_resources, key=lambda x: x.state_resource_id)
    )
    assert tuple(row.detail for row in rows if row.record_kind == "side-effect") == tuple(
        sorted(service.primary.side_effects, key=lambda x: x.side_effect_id)
    )
    for query in ("", "filesystem", "STATEFUL", "no-match", "' OR 1=1"):
        expected = service.state_effects(offset=0, limit=500, query=query)
        collected = []
        for offset in range(expected.eligible):
            actual = index.page(
                BrowseRequest(table=BrowseTable.STATE_EFFECTS, offset=offset, limit=1, query=query)
            )
            assert (
                actual.model_dump_json()
                == service.state_effects(offset=offset, limit=1, query=query).model_dump_json()
            )
            collected.extend(actual.records)
        assert tuple(collected) == expected.records
    monkeypatch.setattr(ReportViewService, "load", lambda *a: pytest.fail("read source report"))
    page = index.page(BrowseRequest(table=BrowseTable.STATE_EFFECTS))
    projection = build_browse_export(index, BrowseRequest(table=BrowseTable.STATE_EFFECTS))
    assert projection.record_count == page.retained
    assert any("runtime ownership" in value for value in projection.limitations)


@pytest.mark.parametrize(
    "field,value",
    [
        ("identity", "wrong"),
        ("record_kind", "side-effect"),
        ("category", "wrong"),
        ("target", "wrong"),
        ("confidence", "runtime-observed"),
        ("language", "javascript"),
    ],
)
def test_state_display_cannot_contradict_evidence(state_index, field, value):
    row = state_index[1].page(BrowseRequest(table=BrowseTable.STATE_EFFECTS)).records[0]
    payload = json.loads(row.model_dump_json())
    payload[field] = value
    with pytest.raises(ValidationError):
        UIStateEffectRow.model_validate_json(json.dumps(payload))


def test_disabled_state_section_stays_explicit_and_export_partial(tmp_path):
    report = scan_contract_inventory(
        discover_context(FIXTURES / "contract_golden", AnalysisConfig())
    )
    root = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), root)
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    request = BrowseRequest(table=BrowseTable.STATE_EFFECTS)
    page = index.page(request)
    assert page.coverage.state.value == "disabled" and not page.records
    assert build_browse_export(index, request).completeness is not Completeness.COMPLETE


def test_terminal_state_effect_details_and_filter(state_index):
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Input, Static

    from code_analysis.tui_app import AnalysisTerminal, RowDetails

    _, index = state_index

    async def exercise():
        app = AnalysisTerminal(index, page_size=1)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("f8")
            await app.workers.wait_for_complete()
            assert app.current_page.retained == 1
            assert "Syntax only" in str(app.query_one("#page-status", Static).render())
            assert app.query_one("#records", DataTable).row_count == 1
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RowDetails)
            assert "declaration_evidence" in app.screen.detail
            assert "not runtime proof" in app.screen.detail_title
            await pilot.press("escape")
            await pilot.press("ctrl+f")
            app.query_one("#search", Input).value = "filesystem"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert app.current_page.records[0].record_kind == "side-effect"

    asyncio.run(exercise())
