from __future__ import annotations

import asyncio
import json
import shutil

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.browse_export import build_browse_export
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseRequest
from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness, SourceLanguage
from code_analysis.project import discover_context
from code_analysis.service import scan, scan_contract_inventory
from code_analysis.test_browsing import TestImpactRow as ImpactRow
from code_analysis.test_browsing import test_impact_rows as impact_rows
from code_analysis.ui_rows import BrowseTable
from code_analysis.ui_source import SourcePreviewIdentity, preview_source_record
from code_analysis.web_service import ReportViewService


@pytest.fixture
def impact_index(tmp_path):
    root = tmp_path / "source"
    shutil.copytree(FIXTURES / "test_impact_project", root)
    shutil.copytree(FIXTURES / "javascript", root, dirs_exist_ok=True)
    service = ReportViewService(scan(discover_context(root, AnalysisConfig())))
    index_root = tmp_path / "index"
    manifest = build_browse_index(service, index_root)
    return root, service, BrowseIndex.open(index_root, report_sha256=manifest.report_sha256)


def test_impact_index_preserves_every_inventory_and_link(impact_index, monkeypatch):
    root, service, index = impact_index
    expected = tuple(impact_rows(service.primary))
    assert {row.record_kind for row in expected} == {"module", "test", "fixture", "relationship"}
    assert {row.language for row in expected} == {SourceLanguage.PYTHON, SourceLanguage.JAVASCRIPT}
    assert len(expected) == sum(
        len(records)
        for records in (
            service.primary.test_modules,
            service.primary.test_cases,
            service.primary.test_fixtures,
            service.primary.test_relationships,
        )
    )
    relationships = {
        row.detail.relationship.relationship_id: row.detail.relationship
        for row in expected
        if row.record_kind == "relationship"
    }
    assert relationships == {
        item.relationship_id: item for item in service.primary.test_relationships
    }
    assert any(row.resolved is False for row in expected)
    assert all(row.resolved is None for row in expected if row.record_kind != "relationship")
    shutil.rmtree(root)
    monkeypatch.setattr(ReportViewService, "load", lambda *args: pytest.fail("report reload"))
    actual = tuple(
        row
        for offset in range(len(expected))
        for row in index.page(
            BrowseRequest(table=BrowseTable.TEST_IMPACT, offset=offset, limit=1)
        ).records
    )
    assert actual == expected
    for query in ("", "javascript", "module", "unresolved", "fixture", "calculate", "' OR 1=1"):
        page = index.page(BrowseRequest(table=BrowseTable.TEST_IMPACT, limit=500, query=query))
        assert (
            page.model_dump_json()
            == service.test_impact(offset=0, limit=500, query=query).model_dump_json()
        )
    projection = build_browse_export(index, BrowseRequest(table=BrowseTable.TEST_IMPACT, limit=500))
    assert projection.record_count == len(expected)
    assert any("not execution" in value for value in projection.limitations)


@pytest.mark.parametrize(
    "field,value",
    [
        ("identity", "bad"),
        ("record_kind", "fixture"),
        ("module", "wrong"),
        ("language", "ruby"),
        ("resolved", 1),
        ("resolved", False),
        ("target", "invented"),
    ],
)
def test_impact_display_contradictions_fail(impact_index, field, value):
    row = impact_index[2].page(BrowseRequest(table=BrowseTable.TEST_IMPACT)).records[0]
    payload = json.loads(row.model_dump_json())
    payload[field] = value
    with pytest.raises(ValidationError):
        ImpactRow.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("damage", ["source", "target", "module", "extra"])
def test_impact_relationship_context_must_reconcile(impact_index, damage):
    rows = impact_index[2].page(BrowseRequest(table=BrowseTable.TEST_IMPACT, limit=500)).records
    row = next(
        row
        for row in rows
        if row.detail.relationship and row.detail.target_subject and row.detail.case
    )
    payload = json.loads(row.model_dump_json())
    if damage == "source":
        payload["detail"]["case"]["test_id"] = "test-case-" + "0" * 20
    elif damage == "target":
        payload["detail"]["target_subject"] = None
    elif damage == "module":
        payload["detail"]["module"]["test_module_id"] = "test-module-" + "0" * 20
    else:
        payload["detail"]["extra"] = True
    with pytest.raises(ValidationError):
        ImpactRow.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("focused", [True, False])
def test_empty_test_inventory_preserves_disabled_or_unavailable(tmp_path, focused):
    context = discover_context(FIXTURES / "excluded", AnalysisConfig())
    service = ReportViewService((scan_contract_inventory if focused else scan)(context))
    root = tmp_path / "index"
    manifest = build_browse_index(service, root)
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    request = BrowseRequest(table=BrowseTable.TEST_IMPACT)
    page = index.page(request)
    assert page.coverage.state.value == ("disabled" if focused else "unavailable")
    assert not page.records
    assert build_browse_export(index, request).completeness is not Completeness.COMPLETE


def test_terminal_test_inventory_details_and_hash_bound_source(impact_index):
    pytest.importorskip("textual")
    from textual.widgets import DataTable

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_source import TerminalSource
    from code_analysis.tui_test_impact import TestInvestigationScreen

    root, service, index = impact_index
    assert any(
        module.path not in {item.logical_path for item in service.primary.source_files}
        for module in service.primary.test_modules
    )

    async def exercise():
        app = AnalysisTerminal(index, source_root=root, page_size=2)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.press("f12")
            await app.workers.wait_for_complete()
            assert app.selection.table is BrowseTable.TEST_IMPACT
            first = app.current_page.records[0]
            await pilot.press("ctrl+n")
            await app.workers.wait_for_complete()
            assert app.current_page.offset == 2
            await pilot.press("ctrl+p")
            await app.workers.wait_for_complete()
            assert app.current_page.records[0] == first
            await pilot.press("enter")
            assert isinstance(app.screen, TestInvestigationScreen)
            investigation = app.screen
            await pilot.click("#test-evidence")
            assert isinstance(app.screen, RowDetails)
            assert first.identity in app.screen.detail
            await pilot.press("escape")
            choices = investigation.investigation.sources
            selected = next(i for i, choice in enumerate(choices) if choice.role == "test")
            table = investigation.query_one("#test-source-choices", DataTable)
            table.move_cursor(row=selected)
            table.focus()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, TerminalSource)
            assert app.screen.preview.state == "bound"
            assert app.screen.preview.logical_path == first.detail.module.path
            assert app.screen.preview.lines
            path = root / first.detail.module.path
            path.write_text("// changed after analysis\n")
            await pilot.press("escape")
            table.focus()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert app.screen.preview.state == "mismatch"
            assert not app.screen.preview.lines

    asyncio.run(exercise())


def test_test_module_binding_uses_confined_preview_reader(impact_index):
    root, service, _ = impact_index
    module = service.primary.test_modules[0]
    identity = SourcePreviewIdentity(logical_path=module.path, source_sha256=module.source_sha256)
    assert preview_source_record(identity, None).state == "disabled"
    assert preview_source_record(identity, root).state == "bound"
    path = root / module.path
    original = path.with_suffix(path.suffix + ".original")
    path.rename(original)
    path.symlink_to(original.name)
    result = preview_source_record(identity, root)
    assert result.state == "unavailable"
    assert not result.lines
    escaped = identity.model_copy(update={"logical_path": "../outside.py"})
    with pytest.raises(ValueError):
        preview_source_record(escaped, root)
