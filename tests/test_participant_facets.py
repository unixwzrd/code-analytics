from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import replace

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.browse_export import build_browse_export
from code_analysis.browse_index import BrowseIndex, _validate_scopes, file_stamp
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseRequest
from code_analysis.config import AnalysisConfig, load_config_file
from code_analysis.models import SourceLanguage
from code_analysis.project import discover_context
from code_analysis.service import scan_refactor
from code_analysis.ui_models import BrowseFacets, UICandidateRow, UIModuleScope
from code_analysis.ui_rows import BrowseTable, participant_matches
from code_analysis.web_service import ReportViewService


@pytest.fixture(
    params=[
        ("proposal_project", BrowseTable.CANDIDATES),
        ("clone_project", BrowseTable.CLONES),
        ("test_impact_project", BrowseTable.TEST_IMPACT),
    ]
)
def scoped_index(request, tmp_path):
    fixture, table = request.param
    source = tmp_path / "source"
    shutil.copytree(FIXTURES / fixture, source)
    config = (
        load_config_file(FIXTURES / "proposal-config.toml")
        if table is BrowseTable.CANDIDATES
        else AnalysisConfig()
    )
    service = ReportViewService(scan_refactor(discover_context(source, config)))
    destination = tmp_path / "index"
    manifest = build_browse_index(service, destination)
    shutil.rmtree(source)
    return service, BrowseIndex.open(destination, report_sha256=manifest.report_sha256), table


def test_saved_participant_facets_filter_before_paging_without_losing_members(scoped_index):
    service, index, table = scoped_index
    method = getattr(service, table.value.replace("-", "_"))
    all_rows = method(offset=0, limit=500).records
    assert all_rows
    first = all_rows[0]
    if table is BrowseTable.CANDIDATES:
        language, module = first.module_scopes[0].language, first.module_scopes[0].module
    elif table is BrowseTable.CLONES:
        language, module = first.language, first.modules[0]
    else:
        language, module = first.language, first.module
    selections = (
        BrowseFacets(language=language),
        BrowseFacets(module_prefix=module),
        BrowseFacets(language=language, module_prefix=module),
        BrowseFacets(module_prefix=module + "_absent"),
    )
    for facets in selections:
        expected = method(offset=0, limit=500, facets=facets)
        actual = []
        for offset in range(expected.eligible):
            request = BrowseRequest(table=table, offset=offset, limit=1, facets=facets)
            page = index.page(request)
            assert (
                page.model_dump_json()
                == method(offset=offset, limit=1, facets=facets).model_dump_json()
            )
            actual.extend(page.records)
            assert build_browse_export(index, request).record_count == 1
        assert tuple(row.model_dump_json() for row in actual) == tuple(
            row.model_dump_json() for row in expected.records
        )
        assert len({row.model_dump_json() for row in actual}) == expected.eligible
        assert all(
            row.model_dump_json() in {original.model_dump_json() for original in all_rows}
            for row in actual
        )
    assert index.page(BrowseRequest(table=table, facets=selections[-1])).eligible == 0
    with pytest.raises(ValueError, match="ownership"):
        method(offset=0, limit=1, facets=BrowseFacets(owner="invented"))


def test_cross_language_candidate_scope_is_same_participant_and_strict():
    row = UICandidateRow(
        candidate_id="candidate",
        kind="duplicate",
        state="observed",
        languages=(SourceLanguage.JAVASCRIPT, SourceLanguage.PYTHON),
        module_scopes=(
            UIModuleScope(language=SourceLanguage.JAVASCRIPT, module="web/client.js"),
            UIModuleScope(language=SourceLanguage.PYTHON, module="server.api"),
        ),
        subject_count=2,
        proposal_count=0,
        failed_gates=0,
        unresolved_gates=0,
    )
    assert participant_matches(
        row, BrowseFacets(language=SourceLanguage.PYTHON, module_prefix="server")
    )
    assert not participant_matches(
        row, BrowseFacets(language=SourceLanguage.PYTHON, module_prefix="web")
    )
    assert not participant_matches(
        row, BrowseFacets(language=SourceLanguage.JAVASCRIPT, module_prefix="server.api")
    )
    for scopes in (
        (row.module_scopes[0],) * 2,
        tuple(reversed(row.module_scopes)),
        (row.module_scopes[0],),
    ):
        with pytest.raises(ValidationError):
            UICandidateRow.model_validate({**row.model_dump(), "module_scopes": scopes})


def test_scope_relation_detects_missing_or_extra_participant(scoped_index):
    _, index, table = scoped_index
    if table is BrowseTable.TEST_IMPACT:
        return
    with sqlite3.connect(index.database) as connection:
        _validate_scopes(connection)
        connection.execute("DELETE FROM browse_scopes WHERE table_name = ?", (table.value,))
        with pytest.raises(ValueError, match="disagree"):
            _validate_scopes(connection)


def test_index_same_participant_sql_rejects_cross_product(tmp_path):
    service = ReportViewService(
        scan_refactor(
            discover_context(
                FIXTURES / "proposal_project", load_config_file(FIXTURES / "proposal-config.toml")
            )
        )
    )
    root = tmp_path / "index"
    manifest = build_browse_index(service, root)
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    row = index.page(BrowseRequest(table=BrowseTable.CANDIDATES, limit=1)).records[0]
    # Isolate the SQL predicate with a strict synthetic multi-language display row.
    payload = row.model_dump(mode="json")
    payload.update(
        languages=["javascript", "python"],
        module_scopes=[
            {"language": "javascript", "module": "web/client.js"},
            {"language": "python", "module": "server.api"},
        ],
    )
    UICandidateRow.model_validate_json(json.dumps(payload))
    with sqlite3.connect(index.database) as connection:
        connection.execute(
            "UPDATE browse_rows SET payload = ? WHERE table_name = 'candidates' AND position = 0",
            (json.dumps(payload),),
        )
        connection.execute(
            "DELETE FROM browse_scopes WHERE table_name = 'candidates' AND position = 0"
        )
        connection.executemany(
            "INSERT INTO browse_scopes VALUES ('candidates', 0, ?, ?)",
            [("javascript", "web/client.js"), ("python", "server.api")],
        )
        _validate_scopes(connection)
    index = replace(index, stamp=file_stamp(index.database))
    for language, prefix, count in (
        (SourceLanguage.PYTHON, "server", 1),
        (SourceLanguage.JAVASCRIPT, "web", 1),
        (SourceLanguage.PYTHON, "web", 0),
        (SourceLanguage.JAVASCRIPT, "server.api", 0),
    ):
        page = index.page(
            BrowseRequest(
                table=BrowseTable.CANDIDATES,
                facets=BrowseFacets(language=language, module_prefix=prefix),
            )
        )
        assert page.eligible == count
