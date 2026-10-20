from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

import code_analysis.static_graph_browse as static_graph_browse
from code_analysis.browse_export import export_browse_page, render_browse_artifact
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.graph_selection import GraphEdgeIdentity, GraphNodeRank, retain_graph
from code_analysis.models import ImportEdge, Module, SourceLanguage
from code_analysis.projection_models import ProjectionCollapse
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.reports.projection_views import ProjectionFormat
from code_analysis.static_graph_browse import (
    StaticGraphRequest,
    read_static_graph,
    validate_static_graphs,
    write_static_graphs,
)
from code_analysis.web_service import ReportViewService


@pytest.fixture
def indexed_dependencies(ordinary_report, tmp_path):
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(ordinary_report), destination)
    return ordinary_report, BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def _display_bounds():
    return {"max_records": 500, "max_nodes": 60, "max_edges": 120}


def test_indexed_dependency_graph_matches_direct_builder(indexed_dependencies) -> None:
    report, index = indexed_dependencies
    request = StaticGraphRequest(name=ProjectionName.DEPENDENCIES)
    actual = index.static_graph(request)
    expected = build_projection(report, ProjectionName.DEPENDENCIES, **_display_bounds())
    assert actual.nodes == expected.nodes
    assert actual.edges == expected.edges
    assert actual.clusters == expected.clusters
    assert actual == index.static_graph(request)


def test_dependency_graph_preserves_external_targets_without_invented_ownership(
    ordinary_report,
) -> None:
    external = Module(
        name="stdlib.external",
        path="<external>",
        source_sha256="0" * 64,
        line_count=0,
        language=SourceLanguage.PYTHON,
    )
    edge = ImportEdge(
        source="alpha.a",
        target="stdlib.external",
        local=False,
    )
    report = ordinary_report.model_copy(
        update={
            "modules": (*ordinary_report.modules, external),
            "import_edges": (*ordinary_report.import_edges, edge),
        }
    )
    with sqlite3.connect(":memory:") as connection:
        summaries = write_static_graphs(connection, report)
        validate_static_graphs(connection, summaries)
        summary = next(item for item in summaries if item.name is ProjectionName.DEPENDENCIES)
        assert summary.available
        projection = read_static_graph(
            connection,
            summaries,
            StaticGraphRequest(name=ProjectionName.DEPENDENCIES),
        )
    expected = build_projection(report, ProjectionName.DEPENDENCIES, **_display_bounds())
    assert {node.node_id for node in projection.nodes} == {node.node_id for node in expected.nodes}
    external_node = next(node for node in projection.nodes if node.node_id == "stdlib.external")
    assert external_node.kind == "module"
    assert "stdlib.external" in {item.module for item in summary.owner_modules}


def test_mixed_language_dependency_filter_matches_builder(ordinary_report) -> None:
    changed_name = ordinary_report.modules[-1].name
    modules = tuple(
        (
            item.model_copy(update={"language": SourceLanguage.JAVASCRIPT})
            if item.name == changed_name
            else item
        )
        for item in ordinary_report.modules
    )
    report = ordinary_report.model_copy(update={"modules": modules})
    with sqlite3.connect(":memory:") as connection:
        summaries = write_static_graphs(connection, report)
        for language in (SourceLanguage.PYTHON, SourceLanguage.JAVASCRIPT):
            request = StaticGraphRequest(name=ProjectionName.DEPENDENCIES, languages=(language,))
            actual = read_static_graph(connection, summaries, request)
            expected = build_projection(
                report,
                ProjectionName.DEPENDENCIES,
                languages=(language,),
                **_display_bounds(),
            )
            assert actual.nodes == expected.nodes
            assert actual.edges == expected.edges
        validate_static_graphs(connection, summaries)


def test_isolated_modules_remain_language_scoped(ordinary_report) -> None:
    isolated = Module(
        name="isolated.module",
        path="isolated.py",
        source_sha256="f" * 64,
        line_count=1,
        language=SourceLanguage.PYTHON,
    )
    report = ordinary_report.model_copy(update={"modules": (*ordinary_report.modules, isolated)})
    projection = build_projection(report, ProjectionName.DEPENDENCIES, **_display_bounds())
    assert any(node.node_id == "isolated.module" for node in projection.nodes)
    node_languages, _edge_languages = static_graph_browse._language_memberships(
        report, ProjectionName.DEPENDENCIES, projection
    )
    assert node_languages["isolated.module"] == {SourceLanguage.PYTHON}


def test_selected_module_neighborhood_bounds_and_omissions(indexed_dependencies) -> None:
    _report, index = indexed_dependencies
    root = "alpha.a"
    focused = index.static_graph(
        StaticGraphRequest(name=ProjectionName.DEPENDENCIES, selected_ids=(root,))
    )
    full = index.static_graph(StaticGraphRequest(name=ProjectionName.DEPENDENCIES))
    neighbor_ids = {root}
    for edge in full.edges:
        if root in (edge.source_id, edge.target_id):
            neighbor_ids.update((edge.source_id, edge.target_id))
    assert {node.node_id for node in focused.nodes} == neighbor_ids
    bounded = index.static_graph(
        StaticGraphRequest(
            name=ProjectionName.DEPENDENCIES,
            selected_ids=(root,),
            max_records=2,
            max_nodes=2,
            max_edges=0,
        )
    )
    assert bounded.truncated
    assert bounded.original_node_count == focused.original_node_count
    assert bounded.omitted_edge_count > 0
    with sqlite3.connect(index.database) as connection:
        node_rows = connection.execute(
            "SELECT node_id, scc_member, fan_in, fan_out FROM static_graph_nodes "
            "WHERE name = ? ORDER BY node_id",
            (ProjectionName.DEPENDENCIES.value,),
        ).fetchall()
        edge_rows = connection.execute(
            "SELECT edge_id, source_id, target_id FROM static_graph_edges "
            "WHERE name = ? ORDER BY edge_id",
            (ProjectionName.DEPENDENCIES.value,),
        ).fetchall()
    eligible_nodes = tuple(
        GraphNodeRank(node_id, node_id == root, bool(scc), fan_in, fan_out)
        for node_id, scc, fan_in, fan_out in node_rows
        if node_id in neighbor_ids
    )
    eligible_edges = tuple(
        GraphEdgeIdentity(*row)
        for row in edge_rows
        if row[1] in neighbor_ids and row[2] in neighbor_ids
    )
    expected = retain_graph(eligible_nodes, eligible_edges, max_records=2, max_nodes=2, max_edges=0)
    assert tuple(node.node_id for node in bounded.nodes) == expected.node_ids
    assert tuple(edge.edge_id for edge in bounded.edges) == expected.edge_ids


def test_package_collapse_matches_direct_builder(ordinary_report) -> None:
    with sqlite3.connect(":memory:") as connection:
        summaries = write_static_graphs(connection, ordinary_report)
        actual = read_static_graph(
            connection,
            summaries,
            StaticGraphRequest(
                name=ProjectionName.DEPENDENCIES,
                collapse=ProjectionCollapse.PACKAGE,
            ),
        )
    expected = build_projection(
        ordinary_report,
        ProjectionName.DEPENDENCIES,
        collapse=ProjectionCollapse.PACKAGE,
        **_display_bounds(),
    )
    assert actual.nodes == expected.nodes
    assert actual.edges == expected.edges


def test_dependency_reader_is_source_free(monkeypatch, indexed_dependencies) -> None:
    _, index = indexed_dependencies

    def fail_source_read(*_args, **_kwargs):
        raise AssertionError("source read attempted")

    monkeypatch.setattr(Path, "read_text", fail_source_read)
    projection = index.static_graph(StaticGraphRequest(name=ProjectionName.DEPENDENCIES))
    assert projection.nodes


def test_dependency_static_graph_browser_export_at_80x24(indexed_dependencies) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Button, Select

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_export import ExportPage
    from code_analysis.tui_static_graphs import StaticGraphBrowser

    _, index = indexed_dependencies

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(80, 24)) as pilot:
            screen = StaticGraphBrowser(
                index,
                app.evidence_closed,
                initial_name=ProjectionName.DEPENDENCIES,
            )
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.graph is not None
            assert screen.request.name is ProjectionName.DEPENDENCIES
            assert not screen.query_one("#static-graph-export", Button).disabled
            assert (
                screen.query_one("#static-graph-name", Select).value
                == ProjectionName.DEPENDENCIES.value
            )
            await pilot.click("#static-graph-export")
            assert isinstance(app.screen, ExportPage)
            await pilot.press("escape", "escape", "ctrl+c")

    asyncio.run(exercise())


def test_dependency_graph_export_is_source_free(indexed_dependencies) -> None:
    _, index = indexed_dependencies
    request = StaticGraphRequest(name=ProjectionName.DEPENDENCIES)
    path, graph = export_browse_page(index, request, "dependencies", ProjectionFormat.JSON_COMPACT)
    assert path.read_bytes() == render_browse_artifact(graph, ProjectionFormat.JSON_COMPACT)
