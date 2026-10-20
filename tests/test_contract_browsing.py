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
from code_analysis.config import AnalysisConfig
from code_analysis.contract_browsing import ContractRow, ShapeRow, contract_rows, shape_rows
from code_analysis.models import Completeness
from code_analysis.project import discover_context
from code_analysis.service import scan, scan_contract_inventory
from code_analysis.ui_models import BrowseFacets
from code_analysis.ui_rows import BrowseTable
from code_analysis.web_service import ReportViewService


@pytest.fixture(
    params=[("contract_golden", BrowseTable.CONTRACTS), ("dictionary_project", BrowseTable.SHAPES)]
)
def inventory(request, tmp_path):
    fixture, table = request.param
    report = scan(discover_context(FIXTURES / fixture, AnalysisConfig()))
    service = ReportViewService(report)
    root = tmp_path / "index"
    manifest = build_browse_index(service, root)
    return service, BrowseIndex.open(root, report_sha256=manifest.report_sha256), table


def test_contract_and_shape_index_matches_complete_typed_records(inventory, monkeypatch):
    service, index, table = inventory
    expected = tuple(
        (contract_rows if table is BrowseTable.CONTRACTS else shape_rows)(service.primary)
    )
    assert expected
    actual = tuple(
        row
        for offset in range(len(expected))
        for row in index.page(BrowseRequest(table=table, limit=1, offset=offset)).records
    )
    assert actual == expected
    method = getattr(service, table.value.replace("-", "_"))
    monkeypatch.setattr(ReportViewService, "load", lambda *args: pytest.fail("report reload"))
    for query in ("", expected[0].owner.upper(), "payload", "missing-entry", "' OR 1=1"):
        page = index.page(BrowseRequest(table=table, limit=500, query=query))
        assert page.model_dump_json() == method(offset=0, limit=500, query=query).model_dump_json()
    projection = build_browse_export(index, BrowseRequest(table=table, limit=500))
    assert projection.record_count == len(expected)
    assert any("producer defaults" in value for value in projection.limitations)
    if table is BrowseTable.SHAPES:
        assert any(len(row.detail.shape.variants) > 1 for row in actual)
        assert tuple(row.detail.shape for row in actual) == tuple(
            sorted(service.primary.dictionary_shapes, key=lambda item: item.shape_id)
        )
    else:
        assert tuple(row.detail.contract for row in actual) == tuple(
            sorted(service.primary.contracts, key=lambda item: item.contract_id)
        )


def test_contract_and_shape_facets_match_live_index_and_export(inventory, monkeypatch):
    service, index, table = inventory
    method = service.contracts if table is BrowseTable.CONTRACTS else service.dictionary_shapes
    first = method(offset=0, limit=500).records[0]
    selections = (
        BrowseFacets(language=first.language),
        BrowseFacets(module_prefix=first.module),
        BrowseFacets(owner=first.owner),
        BrowseFacets(language=first.language, module_prefix=first.module, owner=first.owner),
        BrowseFacets(owner=first.owner + ".nonexistent"),
        BrowseFacets(module_prefix=first.module + "_nonexistent"),
    )
    monkeypatch.setattr(ReportViewService, "load", lambda *args: pytest.fail("report reload"))
    for facets in selections:
        expected = method(offset=0, limit=1, facets=facets)
        request = BrowseRequest(table=table, facets=facets, limit=1)
        assert index.page(request) == expected
        assert all(facets.matches(row.language, row.module, row.owner) for row in expected.records)
        projection = build_browse_export(index, request)
        assert projection.record_count == expected.retained
        assert projection == build_browse_export(index, request)
        assert (
            projection.projection_id
            != build_browse_export(index, BrowseRequest(table=table, limit=1)).projection_id
        )
    assert method(offset=0, limit=1, facets=selections[-1]).eligible == 0
    assert method(offset=0, limit=1, facets=selections[-2]).eligible == 0


def test_contract_and_shape_http_facets_preserve_typed_rows(inventory):
    import threading
    from http.client import HTTPConnection
    from urllib.parse import urlencode

    from code_analysis.web_server import create_server

    service, _, table = inventory
    method = service.contracts if table is BrowseTable.CONTRACTS else service.dictionary_shapes
    first = method(offset=0, limit=1).records[0]
    facets = BrowseFacets(language=first.language, module_prefix=first.module, owner=first.owner)
    server = create_server(service, token="contract-facets")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port)
    try:
        query = urlencode({**facets.model_dump(mode="json"), "limit": 1})
        connection.request(
            "GET",
            f"/api/{table.value}?{query}",
            headers={"X-Code-Analysis-Token": "contract-facets"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == method(offset=0, limit=1, facets=facets).model_dump(
            mode="json"
        )
        connection.request(
            "GET",
            f"/api/{table.value}?module_prefix=bad*",
            headers={"X-Code-Analysis-Token": "contract-facets"},
        )
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        connection.request("GET", f"/api/{table.value}")
        response = connection.getresponse()
        assert response.status == 403
        response.read()
    finally:
        connection.close()
        server.shutdown()
        thread.join(5)
        server.server_close()


@pytest.mark.parametrize(
    "field,value",
    [("identity", "wrong"), ("module", "wrong"), ("owner", "wrong"), ("language", "javascript")],
)
def test_contract_inventory_display_mismatch_rejected(inventory, field, value):
    _, index, table = inventory
    row = index.page(BrowseRequest(table=table)).records[0]
    payload = json.loads(row.model_dump_json())
    payload[field] = value
    with pytest.raises(ValidationError):
        type(row).model_validate_json(json.dumps(payload))


def test_contract_inventory_coercion_and_dangling_owner_rejected(inventory):
    _, index, table = inventory
    row = index.page(BrowseRequest(table=table)).records[0]
    count_field = "member_count" if isinstance(row, ContractRow) else "variant_count"
    for field, value in ((count_field, True), ("unknown", "extra")):
        payload = json.loads(row.model_dump_json())
        payload[field] = value
        with pytest.raises(ValidationError):
            type(row).model_validate_json(json.dumps(payload))
    payload = json.loads(row.model_dump_json())
    payload["detail"]["owner"]["subject_id"] = "subject-" + "0" * 20
    with pytest.raises(ValidationError):
        type(row).model_validate_json(json.dumps(payload))


def test_disabled_dictionary_inventory_remains_explicit(tmp_path):
    report = scan_contract_inventory(
        discover_context(FIXTURES / "contract_golden", AnalysisConfig())
    )
    root = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), root)
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    request = BrowseRequest(table=BrowseTable.SHAPES)
    assert index.page(request).coverage.state.value == "disabled"
    assert build_browse_export(index, request).completeness is not Completeness.COMPLETE


def test_terminal_fields_preserve_details_and_disabled_preview(inventory):
    pytest.importorskip("textual")
    from textual.widgets import DataTable

    from code_analysis.tui_app import AnalysisTerminal, RowDetails
    from code_analysis.tui_contracts import ContractExplorer
    from code_analysis.tui_source import TerminalSource

    _, index, table = inventory

    async def exercise():
        app = AnalysisTerminal(index, page_size=1)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.press("f10" if table is BrowseTable.CONTRACTS else "f11")
            await app.workers.wait_for_complete()
            await pilot.press("enter")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ContractExplorer)
            assert screen.query_one("#contract-fields", DataTable).row_count == min(
                25, len(screen.entries)
            )
            await pilot.click("#contract-definition")
            assert isinstance(app.screen, RowDetails)
            assert (
                "boundaries" if table is BrowseTable.CONTRACTS else "variants"
            ) in app.screen.detail
            await pilot.press("escape")
            screen.query_one("#contract-fields", DataTable).focus()
            await pilot.press("enter")
            assert isinstance(app.screen, RowDetails)
            for name in ("type_expression", "producer_defaults", "consumer_fallbacks"):
                assert name in app.screen.detail
            await pilot.click("#preview-source")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, TerminalSource)
            assert app.screen.preview.state == "disabled"
            assert not app.screen.preview.lines

    asyncio.run(exercise())


def test_field_continuation_preserves_variant_boundaries(inventory):
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_contracts import ContractExplorer

    _, index, table = inventory
    row = index.page(BrowseRequest(table=table)).records[0]
    payload = json.loads(row.model_dump_json())
    if isinstance(row, ContractRow):
        container = payload["detail"]["contract"]
        field_key = "members"
    else:
        container = payload["detail"]["shape"]["variants"][0]
        field_key = "fields"
        container["discriminator"] = None
    template = container[field_key][0]
    container[field_key] = [
        dict(copy.deepcopy(template), name=f"field_{number:03d}") for number in range(61)
    ]
    if isinstance(row, ContractRow):
        payload["member_count"] = 61
    else:
        payload["field_occurrences"] = sum(
            len(v["fields"]) for v in payload["detail"]["shape"]["variants"]
        )
    expanded = type(row).model_validate_json(json.dumps(payload))

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(140, 42)) as pilot:
            await app.workers.wait_for_complete()
            screen = ContractExplorer(index, expanded, lambda status: pytest.fail(str(status)))
            await app.push_screen(screen)
            seen = []
            while True:
                grid = screen.query_one("#contract-fields", DataTable)
                assert grid.row_count <= 25
                seen.extend(int(key.value) for key in grid.rows)
                if screen.query_one("#contract-next", Button).disabled:
                    break
                await pilot.press("ctrl+n")
            assert seen == list(range(len(screen.entries)))
            if isinstance(expanded, ShapeRow):
                assert {entry.variant.variant_id for entry in screen.entries} == {
                    variant.variant_id
                    for variant in expanded.detail.shape.variants
                    if variant.fields
                }
            await pilot.press("ctrl+p")
            assert screen.field_offset == ((len(screen.entries) - 1) // 25 - 1) * 25

    asyncio.run(exercise())
