"""Saved static-call projections: prepared index browsing without target source."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_static_call_projections import _report_with_calls
from test_static_call_validation import (
    _occurrence,
    _synthetic_shell,
    _with_static_calls,
    _zero_coverage,
)

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseIndexManifest
from code_analysis.models import Completeness, SourceLanguage
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.report_models import SectionState
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.static_browse import (
    StaticProjectionRequest,
    read_static_projection,
    static_page_projection,
)
from code_analysis.static_call_models import StaticCallSourceCoverageState
from code_analysis.static_graph_browse import (
    GRAPH_PROJECTIONS,
    StaticGraphRequest,
    StaticGraphSelectionError,
)
from code_analysis.ui_navigation import NavigationUnavailable, navigation_page
from code_analysis.web_service import ReportViewService


def _index(report, tmp_path: Path) -> tuple[BrowseIndex, BrowseIndexManifest]:
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), destination)
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    return index, manifest


def test_disabled_section_omits_table_and_marks_graph_unavailable(tmp_path: Path) -> None:
    report = load_json(render_json(_synthetic_shell(), compact=True))
    index, manifest = _index(report, tmp_path)
    names = {item.name for item in manifest.static_projections}
    assert ProjectionName.STATIC_CALL_SITES not in names
    graph = next(
        item for item in manifest.static_graphs if item.name is ProjectionName.STATIC_CALL_GRAPH
    )
    assert not graph.available
    assert graph.unavailable_reason == "static-call-occurrences section is disabled"
    with pytest.raises(ValueError, match="disabled"):
        index.static_graph(StaticGraphRequest(name=ProjectionName.STATIC_CALL_GRAPH))


def test_complete_zero_calls_prepares_empty_table_and_graph(tmp_path: Path) -> None:
    report = _report_with_calls()
    index, manifest = _index(report, tmp_path)
    sites = next(
        item
        for item in manifest.static_projections
        if item.name is ProjectionName.STATIC_CALL_SITES
    )
    graph = next(
        item for item in manifest.static_graphs if item.name is ProjectionName.STATIC_CALL_GRAPH
    )
    assert sites.records == 0 and graph.available and graph.node_count == 0
    page = index.static_projection(StaticProjectionRequest(name=ProjectionName.STATIC_CALL_SITES))
    assert page.records == ()
    projection = static_page_projection(page)
    expected = build_projection(report, ProjectionName.STATIC_CALL_SITES)
    assert projection.rows == expected.rows
    assert projection.inputs[0].report_sha256 == expected.inputs[0].report_sha256
    actual_graph = index.static_graph(StaticGraphRequest(name=ProjectionName.STATIC_CALL_GRAPH))
    expected_graph = build_projection(
        report,
        ProjectionName.STATIC_CALL_GRAPH,
        max_records=500,
        max_nodes=60,
        max_edges=120,
    )
    assert actual_graph.nodes == expected_graph.nodes
    assert actual_graph.edges == expected_graph.edges


def test_indexed_table_and_graph_match_direct_projections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _occurrence()
    second = _occurrence(
        evidence=(
            first.evidence[0].model_copy(
                update={
                    "span": first.evidence[0].span.model_copy(
                        update={"start_column": 10, "end_column": 15}
                    )
                }
            ),
        ),
    )
    report = _report_with_calls(first, second)
    index, _manifest = _index(report, tmp_path)

    def forbid_source(*_args: object, **_kwargs: object) -> str:
        pytest.fail("indexed static-call browsing must not read target source")

    monkeypatch.setattr(Path, "read_text", forbid_source)
    monkeypatch.setattr(Path, "read_bytes", forbid_source)

    page = index.static_projection(StaticProjectionRequest(name=ProjectionName.STATIC_CALL_SITES))
    table = static_page_projection(page)
    expected_table = build_projection(report, ProjectionName.STATIC_CALL_SITES)
    assert table.rows == expected_table.rows
    assert {row.evidence_ids for row in table.rows} == {
        row.evidence_ids for row in expected_table.rows
    }
    assert table.inputs[0].report_sha256 == expected_table.inputs[0].report_sha256

    graph = index.static_graph(StaticGraphRequest(name=ProjectionName.STATIC_CALL_GRAPH))
    expected_graph = build_projection(
        report,
        ProjectionName.STATIC_CALL_GRAPH,
        max_records=500,
        max_nodes=60,
        max_edges=120,
    )
    assert graph.nodes == expected_graph.nodes
    assert graph.edges == expected_graph.edges
    assert graph.inputs[0].report_sha256 == expected_graph.inputs[0].report_sha256


def test_partial_retained_calls_remain_browseable(tmp_path: Path) -> None:
    coverage = (
        _zero_coverage(
            state=StaticCallSourceCoverageState.PARTIAL,
            retained_count=1,
            observed_count=1,
            limitations=("bounded traversal",),
        ),
    )
    report = load_json(
        render_json(
            _with_static_calls(
                _synthetic_shell().model_copy(update={"completeness": Completeness.PARTIAL}),
                section_state=SectionState.PARTIAL,
                occurrences=(_occurrence(),),
                coverage=coverage,
            ),
            compact=True,
        )
    )
    index, manifest = _index(report, tmp_path)
    sites = next(
        item
        for item in manifest.static_projections
        if item.name is ProjectionName.STATIC_CALL_SITES
    )
    assert sites.completeness is Completeness.PARTIAL
    page = index.static_projection(StaticProjectionRequest(name=ProjectionName.STATIC_CALL_SITES))
    assert len(page.records) == 1
    assert "bounded traversal" in page.summary.limitations


def test_dynamic_callee_and_repeated_calls_preserve_distinct_nodes(tmp_path: Path) -> None:
    dynamic = _occurrence(callee_spelling=None, enclosing_subject_id=None)
    repeated = _occurrence()
    report = _report_with_calls(repeated, dynamic)
    index, _manifest = _index(report, tmp_path)
    page = index.static_projection(StaticProjectionRequest(name=ProjectionName.STATIC_CALL_SITES))
    assert len(page.records) == 2
    graph = index.static_graph(StaticGraphRequest(name=ProjectionName.STATIC_CALL_GRAPH))
    call_nodes = [item for item in graph.nodes if item.kind == "call-site"]
    assert len(call_nodes) == 2
    assert any(item.label == "dynamic callee" for item in call_nodes)


def test_language_filter_and_bounds(tmp_path: Path) -> None:
    report = _report_with_calls(_occurrence())
    index, _manifest = _index(report, tmp_path)
    filtered = index.static_graph(
        StaticGraphRequest(
            name=ProjectionName.STATIC_CALL_GRAPH,
            languages=(SourceLanguage.PYTHON,),
        )
    )
    expected = build_projection(
        report,
        ProjectionName.STATIC_CALL_GRAPH,
        languages=(SourceLanguage.PYTHON,),
        max_records=500,
        max_nodes=60,
        max_edges=120,
    )
    assert filtered.nodes == expected.nodes
    with pytest.raises(StaticGraphSelectionError, match="no prepared graph records match"):
        index.static_graph(
            StaticGraphRequest(
                name=ProjectionName.STATIC_CALL_GRAPH,
                languages=(SourceLanguage.JAVASCRIPT,),
            )
        )
    bounded = index.static_graph(
        StaticGraphRequest(
            name=ProjectionName.STATIC_CALL_GRAPH,
            max_nodes=1,
            max_edges=0,
        )
    )
    assert len(bounded.nodes) == 1
    assert bounded.truncated


def test_graph_navigation_uses_call_site_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = _occurrence()
    report = _report_with_calls(call)
    index, _manifest = _index(report, tmp_path)
    request = StaticGraphRequest(name=ProjectionName.STATIC_CALL_GRAPH)
    graph = index.static_graph(request)

    def forbid_source(*_args: object, **_kwargs: object) -> str:
        pytest.fail("graph navigation must not read target source")

    monkeypatch.setattr(Path, "read_text", forbid_source)
    for node in graph.nodes:
        try:
            expected = navigation_page(report, node.node_id)
        except NavigationUnavailable:
            with pytest.raises(NavigationUnavailable):
                index.graph_navigation(request, node.node_id)
        else:
            assert index.graph_navigation(request, node.node_id) == expected


def test_manifest_rejects_static_call_availability_mismatch(tmp_path: Path) -> None:
    report = _report_with_calls(_occurrence())
    index, _manifest = _index(report, tmp_path)
    payload = index.manifest.model_dump()
    graphs = list(payload["static_graphs"])
    static_graph = next(
        item for item in graphs if item["name"] == ProjectionName.STATIC_CALL_GRAPH.value
    )
    static_graph["available"] = False
    static_graph["unavailable_reason"] = "static-call-occurrences section is disabled"
    static_graph.update(
        {
            "node_count": 0,
            "edge_count": 0,
            "cluster_count": 0,
            "original_node_count": 0,
            "original_edge_count": 0,
            "owner_modules": (),
        }
    )
    with pytest.raises(ValueError, match="static-call-sites availability must match"):
        BrowseIndexManifest.model_validate({**payload, "static_graphs": tuple(graphs)})


def test_manifest_rejects_foreign_static_projection_source(tmp_path: Path) -> None:
    report = _report_with_calls(_occurrence())
    index, _manifest = _index(report, tmp_path)
    payload = index.manifest.model_dump()
    projections = list(payload["static_projections"])
    sites = next(
        item for item in projections if item["name"] == ProjectionName.STATIC_CALL_SITES.value
    )
    foreign = {**sites, "source": {**sites["source"], "report_sha256": "a" * 64}}
    projections[projections.index(sites)] = foreign
    with pytest.raises(ValueError, match="belongs to another report"):
        BrowseIndexManifest.model_validate({**payload, "static_projections": tuple(projections)})


def test_malformed_static_projection_row_fails_closed(tmp_path: Path) -> None:
    from pydantic import ValidationError

    report = _report_with_calls(_occurrence())
    index, manifest = _index(report, tmp_path)
    with sqlite3.connect(index.database) as connection:
        connection.execute(
            "UPDATE static_projection_rows SET payload = ? WHERE name = ?",
            ("not-json", ProjectionName.STATIC_CALL_SITES.value),
        )
        with pytest.raises(ValidationError):
            read_static_projection(
                connection,
                manifest.static_projections,
                StaticProjectionRequest(name=ProjectionName.STATIC_CALL_SITES),
            )


def test_static_call_graph_is_in_canonical_graph_projection_order() -> None:
    assert ProjectionName.STATIC_CALL_GRAPH in GRAPH_PROJECTIONS
