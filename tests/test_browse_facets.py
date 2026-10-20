from __future__ import annotations

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.browse_export import build_browse_export
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseRequest
from code_analysis.models import SourceLanguage
from code_analysis.ui_models import BrowseFacets
from code_analysis.ui_rows import BrowseTable
from code_analysis.web_service import ReportViewService


def test_saved_state_owner_matches_exact_index_and_report(tmp_path):
    from code_analysis.config import AnalysisConfig
    from code_analysis.project import discover_context
    from code_analysis.service import scan

    report = scan(discover_context(FIXTURES / "state_effect_project", AnalysisConfig()))
    service = ReportViewService(report)
    root = tmp_path / "index"
    manifest = build_browse_index(service, root)
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    rows = service.state_effects(offset=0, limit=500).records
    assert rows
    selected = rows[0]
    facets = BrowseFacets(
        language=selected.language, module_prefix=selected.module, owner=selected.owner
    )
    expected = service.state_effects(offset=0, limit=500, facets=facets)
    assert expected.records and all(row.owner == selected.owner for row in expected.records)
    assert (
        index.page(BrowseRequest(table=BrowseTable.STATE_EFFECTS, limit=500, facets=facets))
        == expected
    )
    mismatch = BrowseFacets(owner=selected.owner + ".child")
    assert index.page(BrowseRequest(table=BrowseTable.STATE_EFFECTS, facets=mismatch)).eligible == 0


def test_mixed_language_module_index_uses_language_specific_boundaries(tmp_path):
    pytest.importorskip("tree_sitter_javascript")
    from code_analysis.config import AnalysisConfig
    from code_analysis.project import discover_context
    from code_analysis.service import scan

    report = scan(discover_context(FIXTURES / "interface_project", AnalysisConfig()))
    service = ReportViewService(report)
    root = tmp_path / "index"
    manifest = build_browse_index(service, root)
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    javascript = [
        row
        for row in service.modules(offset=0, limit=500).records
        if row.language is SourceLanguage.JAVASCRIPT
    ]
    assert javascript
    module = javascript[0].module
    assert "/" in module
    prefix = module.split("/")[0]
    for selection in (prefix, module, prefix + "_other", module.rsplit(".", 1)[0]):
        facets = BrowseFacets(language=SourceLanguage.JAVASCRIPT, module_prefix=selection)
        expected = service.modules(offset=0, limit=500, facets=facets)
        assert (
            index.page(BrowseRequest(table=BrowseTable.MODULES, limit=500, facets=facets))
            == expected
        )
        if selection in {prefix, module}:
            assert expected.records
        else:
            assert not expected.records


def test_http_facets_and_scope(ordinary_report):
    import json
    import threading
    from http.client import HTTPConnection

    from code_analysis.web_server import create_server

    server = create_server(ReportViewService(ordinary_report), token="facets")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port)
    try:
        for path, status in (
            ("/api/modules?language=python&module_prefix=alpha.a", 200),
            ("/api/findings?language=javascript&module_prefix=alpha", 200),
            ("/api/state-effects?owner=missing", 200),
            ("/api/modules?owner=wrong", 400),
            ("/api/providers?language=python", 400),
            ("/api/modules?module_prefix=alpha*", 400),
            ("/api/modules?language=python&language=ruby", 400),
        ):
            connection.request("GET", path, headers={"X-Code-Analysis-Token": "facets"})
            response = connection.getresponse()
            assert response.status == status
            result = json.loads(response.read())
            if path.startswith("/api/modules?language=python&module_prefix"):
                assert [row["module"] for row in result["records"]] == ["alpha.a"]
            if path.startswith("/api/findings?language=javascript"):
                assert result["eligible"] == 0
    finally:
        connection.close()
        server.shutdown()
        thread.join(5)
        server.server_close()


@pytest.mark.parametrize(
    "language,module,prefix,expected",
    [
        (SourceLanguage.PYTHON, "pkg.core", "pkg", True),
        (SourceLanguage.PYTHON, "pkg_extra.core", "pkg", False),
        (SourceLanguage.PYTHON, "pkg.core", "pkg.core", True),
        (SourceLanguage.JAVASCRIPT, "pkg/core.js", "pkg", True),
        (SourceLanguage.JAVASCRIPT, "pkg/core.js", "pkg/core", False),
        (SourceLanguage.JAVASCRIPT, "pkg/core.js", "pkg/core.js", True),
        (SourceLanguage.JAVASCRIPT, "pkg_extra/core.js", "pkg", False),
        (SourceLanguage.RUBY, "pkg/core.rb", "pkg", True),
    ],
)
def test_logical_module_prefixes_are_segment_exact(language, module, prefix, expected):
    assert BrowseFacets(module_prefix=prefix).matches(language, module) is expected


@pytest.mark.parametrize(
    "values",
    [
        {"language": True},
        {"module_prefix": True},
        {"owner": 1},
        {"module_prefix": "pkg*"},
        {"module_prefix": "/pkg"},
        {"module_prefix": "pkg/../x"},
        {"module_prefix": "pkg\\x"},
        {"module_prefix": "pkg//x"},
        {"owner": "a\nb"},
        {"extra": "x"},
    ],
)
def test_facets_are_closed_literals(values):
    with pytest.raises(ValidationError):
        BrowseFacets(**values)


def test_facets_combine_with_and_and_owner_is_exact():
    facets = BrowseFacets(language=SourceLanguage.PYTHON, module_prefix="pkg", owner="pkg:Owner")
    assert facets.matches(SourceLanguage.PYTHON, "pkg.core", "pkg:Owner")
    assert not facets.matches(SourceLanguage.PYTHON, "pkg.core", "pkg:Owner.child")
    assert not facets.matches(SourceLanguage.JAVASCRIPT, "pkg/core.js", "pkg:Owner")
    assert not facets.matches(SourceLanguage.PYTHON, "pkg_extra.core", "pkg:Owner")


def test_unsupported_facets_fail_instead_of_being_ignored(ordinary_report):
    with pytest.raises(ValidationError):
        BrowseRequest(table=BrowseTable.FINDINGS, facets=BrowseFacets(owner="owner"))
    with pytest.raises(ValidationError):
        BrowseRequest(table=BrowseTable.CANDIDATES, facets=BrowseFacets(owner="owner"))
    service = ReportViewService(ordinary_report)
    for method in (service.modules, service.findings):
        with pytest.raises(ValueError, match="ownership"):
            method(offset=0, limit=1, facets=BrowseFacets(owner="owner"))


@pytest.mark.parametrize(
    "table", [BrowseTable.MODULES, BrowseTable.FINDINGS, BrowseTable.STATE_EFFECTS]
)
def test_index_live_and_export_match_without_source(ordinary_report, tmp_path, table):
    service = ReportViewService(ordinary_report)
    directory = tmp_path / "index"
    manifest = build_browse_index(service, directory)
    index = BrowseIndex.open(directory, report_sha256=manifest.report_sha256)
    method = {
        BrowseTable.MODULES: service.modules,
        BrowseTable.FINDINGS: service.findings,
        BrowseTable.STATE_EFFECTS: service.state_effects,
    }[table]
    for prefix in ("", "alpha", "alpha.a", "alph", "no-match"):
        for language in (None, SourceLanguage.PYTHON, SourceLanguage.JAVASCRIPT):
            facets = BrowseFacets(language=language, module_prefix=prefix)
            request = BrowseRequest(table=table, facets=facets, limit=1)
            expected = method(offset=0, limit=1, facets=facets)
            assert index.page(request) == expected
            projection = build_browse_export(index, request)
            assert projection.record_count == expected.retained
            assert projection == build_browse_export(index, request)
    empty = BrowseRequest(table=table, facets=BrowseFacets(module_prefix="missing"))
    assert (
        build_browse_export(index, empty).projection_id
        != build_browse_export(index, BrowseRequest(table=table)).projection_id
    )
