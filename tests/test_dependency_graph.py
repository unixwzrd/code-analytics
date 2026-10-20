from __future__ import annotations

import pytest
from pydantic import ValidationError

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.dependency_graph import DependencyGraphRequest, build_dependency_graph
from code_analysis.dependency_navigation import DependencyRequest
from code_analysis.models import Completeness
from code_analysis.module_navigation import ReportModuleReader, module_navigation_pages
from code_analysis.projection_models import ProjectionInput, ProjectionInputRole
from code_analysis.web_service import ReportViewService


@pytest.fixture
def graph_context(ordinary_report, tmp_path):
    service = ReportViewService(ordinary_report)
    directory = tmp_path / "index"
    manifest = build_browse_index(service, directory)
    index = BrowseIndex.open(directory, report_sha256=manifest.report_sha256)
    known = {item.name for item in ordinary_report.modules}
    edge = next(
        item
        for item in ordinary_report.import_edges
        if item.local
        and item.source in known
        and item.target in known
        and item.source != item.target
    )
    request = DependencyGraphRequest(
        investigation=DependencyRequest(source=edge.source, target=edge.target)
    )
    source = ProjectionInput(
        role=ProjectionInputRole.SOURCE,
        report_schema_name=ordinary_report.schema_name,
        report_schema_version=ordinary_report.schema_version,
        report_sha256=manifest.report_sha256,
    )
    return service, index, request, source


def test_live_index_graph_identity_and_retained_edges(graph_context):
    service, index, request, source = graph_context
    graph = build_dependency_graph(index, request, source)
    assert graph.model_dump_json() == service.dependency_graph(request).model_dump_json()
    result = service.dependencies(request.investigation)
    assert len(result.path) == 2
    assert {(edge.source_id, edge.target_id, edge.kind) for edge in result.edges} == {
        (edge.source_id, edge.target_id, edge.kind) for edge in graph.edges
    }
    assert graph.inputs == (source,)
    assert not graph.truncated
    assert service._dependency_reader() is service._dependency_reader()


def test_component_graph_preserves_observed_internal_edges(graph_context):
    service, index, request, source = graph_context
    selection = DependencyGraphRequest(
        investigation=DependencyRequest(source=request.investigation.source, mode="component")
    )
    result = service.dependencies(selection.investigation)
    graph = build_dependency_graph(index, selection, source)
    assert {node.node_id for node in graph.nodes} == set(result.members)
    assert graph.model_dump_json() == service.dependency_graph(selection).model_dump_json()
    assert all(
        edge.source_id in result.members and edge.target_id in result.members
        for edge in graph.edges
    )


def test_display_and_traversal_omissions_remain_distinct(graph_context):
    service, index, request, source = graph_context
    full = build_dependency_graph(index, request, source)
    limited = build_dependency_graph(index, request.model_copy(update={"max_nodes": 1}), source)
    assert limited.truncated and limited.completeness is Completeness.PARTIAL
    assert limited.omitted_node_count == 1
    assert limited.original_node_count == full.original_node_count
    assert limited.omitted_edge_count == full.original_edge_count
    assert limited.projection_id != full.projection_id
    bounded = DependencyGraphRequest(
        investigation=DependencyRequest(
            source=request.investigation.source, target=request.investigation.target, max_nodes=1
        )
    )
    uncertain = service.dependency_graph(bounded)
    assert uncertain.truncated and not uncertain.nodes
    assert uncertain.omitted_node_count == 0  # No fabricated count of undiscovered path nodes.
    assert any("undiscovered" in item for item in uncertain.limitations)


def test_live_navigation_matches_all_index_preparation_pages(ordinary_report):
    reader = ReportModuleReader.from_report(ordinary_report)
    for page in module_navigation_pages(ordinary_report):
        assert reader.module_navigation(page.module.module, page.view, page.offset) == page
    with pytest.raises(TypeError):
        reader.rows["invented"] = next(iter(reader.rows.values()))


@pytest.mark.parametrize(
    "options",
    [
        {"max_nodes": True},
        {"max_nodes": 101},
        {"max_edges": -1},
        {"max_edges": 251},
        {"unknown": 1},
    ],
)
def test_graph_request_is_strict(options):
    with pytest.raises(ValidationError):
        DependencyGraphRequest(investigation=DependencyRequest(source="x", target="y"), **options)
