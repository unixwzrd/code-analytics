from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness, SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionBuildError, ProjectionName, build_projection
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.reports.projection_views import (
    ProjectionFormat,
    render_projection,
    render_projection_binary,
)
from code_analysis.service import scan
from code_analysis.ui_navigation import navigation_page


@pytest.fixture
def saved_structures(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(Path(__file__).parent / "fixtures" / "structure_project", source)
    report = scan(discover_context(source, AnalysisConfig()))
    encoded = render_json(report, compact=True)
    shutil.rmtree(source)
    return load_json(encoded)


@pytest.mark.parametrize("name", ("ownership-alias-flow", "dictionary-flow"))
def test_structure_language_selection_is_applied_before_bounds(saved_structures, name):
    report = saved_structures
    projection_name = ProjectionName(name)
    unfiltered = build_projection(report, projection_name)
    assert unfiltered.nodes and unfiltered.edges
    python_only = build_projection(report, projection_name, languages=(SourceLanguage.PYTHON,))
    mixed = build_projection(
        report,
        projection_name,
        languages=(SourceLanguage.JAVASCRIPT, SourceLanguage.PYTHON),
    )
    assert python_only.nodes == mixed.nodes == unfiltered.nodes
    assert python_only.edges == mixed.edges == unfiltered.edges
    assert python_only.completeness == unfiltered.completeness
    assert not python_only.truncated
    assert python_only.projection_id != unfiltered.projection_id
    assert mixed.projection_id != python_only.projection_id
    for language in (SourceLanguage.JAVASCRIPT, SourceLanguage.RUBY):
        # A nonmatching filter must not return all Python records, even with a
        # display bound or a real identity from the excluded language.
        for options in ({}, {"max_records": 1}, {"selected_ids": (unfiltered.nodes[0].node_id,)}):
            with pytest.raises(ProjectionBuildError, match="selected languages"):
                build_projection(report, projection_name, languages=(language,), **options)
    for output_format in (
        ProjectionFormat.JSON,
        ProjectionFormat.MARKDOWN,
        ProjectionFormat.DOT,
        ProjectionFormat.MERMAID,
    ):
        assert render_projection(python_only, output_format) == render_projection(
            build_projection(report, projection_name, languages=(SourceLanguage.PYTHON,)),
            output_format,
        )


@pytest.mark.parametrize("name", ("ownership-alias-flow", "dictionary-flow"))
def test_language_scope_retains_related_foreign_subjects_before_bounds(saved_structures, name):
    # Isolate projection selection over typed saved facts. This deliberately
    # varies subject language metadata; it is not a parser or report-ingestion
    # fixture and is never serialized as an authoritative mixed-language scan.
    report = saved_structures
    if name == "ownership-alias-flow":
        chosen = report.data_structures[-1]
        subject_id = chosen.owner_subject_id
    else:
        subject_id = report.flow_edges[-1].target_id
    subjects = tuple(
        (
            item.model_copy(update={"language": SourceLanguage.JAVASCRIPT})
            if item.subject_id == subject_id
            else item
        )
        for item in report.subjects
    )
    mixed = report.model_copy(update={"subjects": subjects})
    projection_name = ProjectionName(name)
    all_records = build_projection(mixed, projection_name)
    if name == "ownership-alias-flow":
        expected_edges = tuple(
            edge
            for edge in all_records.edges
            if chosen.structure_id in (edge.source_id, edge.target_id)
        )
    else:
        expected_edges = tuple(
            edge for edge in all_records.edges if subject_id in (edge.source_id, edge.target_id)
        )
    expected_ids = {
        endpoint for edge in expected_edges for endpoint in (edge.source_id, edge.target_id)
    }
    assert expected_edges and len(expected_edges) < len(all_records.edges)
    expected_nodes = tuple(node for node in all_records.nodes if node.node_id in expected_ids)
    graph = build_projection(
        mixed,
        projection_name,
        languages=(SourceLanguage.JAVASCRIPT,),
        max_records=len(expected_nodes) + len(expected_edges),
        max_nodes=len(expected_nodes),
        max_edges=len(expected_edges),
    )
    assert graph.nodes == expected_nodes
    assert graph.edges == expected_edges
    assert not graph.truncated
    assert graph.original_node_count == len(expected_nodes)
    assert graph.original_edge_count == len(expected_edges)
    assert graph.omitted_node_count == graph.omitted_edge_count == 0
    assert any(
        item.subject_id in expected_ids and item.language is SourceLanguage.PYTHON
        for item in subjects
    )


@pytest.mark.parametrize("name", ("structure-lifecycle-graph", "conversion-graph"))
def test_structure_graphs_are_saved_only_deterministic_and_bounded(saved_structures, name):
    report = saved_structures
    graph = build_projection(report, ProjectionName(name))
    assert graph.nodes and graph.edges and not graph.truncated
    assert graph.completeness == report.completeness
    assert graph.record_count == len(graph.nodes) + len(graph.edges)
    assert graph.original_node_count == len(graph.nodes)
    assert len({edge.edge_id for edge in graph.edges}) == len(graph.edges)
    assert all(node.evidence_ids for node in graph.nodes)
    assert all(edge.evidence_ids for edge in graph.edges)
    again = build_projection(report, ProjectionName(name))
    for format in (
        ProjectionFormat.JSON,
        ProjectionFormat.MARKDOWN,
        ProjectionFormat.DOT,
        ProjectionFormat.MERMAID,
        ProjectionFormat.HTML,
    ):
        rendered = render_projection(graph, format)
        assert rendered == render_projection(again, format)
        assert "/Users/" not in rendered and "/private/" not in rendered
    bounded = build_projection(report, ProjectionName(name), max_nodes=1, max_edges=1)
    assert bounded.truncated and bounded.completeness is Completeness.PARTIAL
    assert bounded.omitted_node_count == len(graph.nodes) - len(bounded.nodes)
    assert bounded.omitted_edge_count == len(graph.edges) - len(bounded.edges)
    with pytest.raises(ProjectionBuildError):
        build_projection(report, ProjectionName(name), selected_ids=("not-recorded",))
    with pytest.raises(ProjectionBuildError):
        build_projection(report, ProjectionName(name), languages=(SourceLanguage.JAVASCRIPT,))
    selected = graph.nodes[0].node_id
    neighborhood = build_projection(report, ProjectionName(name), selected_ids=(selected,))
    assert selected in {node.node_id for node in neighborhood.nodes}
    assert not neighborhood.truncated
    assert (
        build_projection(report, ProjectionName(name), languages=(SourceLanguage.PYTHON,)).nodes
        == graph.nodes
    )


def test_lifecycle_roles_and_states_do_not_invent_temporal_edges(saved_structures):
    report = saved_structures
    graph = build_projection(report, ProjectionName("structure-lifecycle-graph"))
    state_ids = {
        state.state_id for shape in report.dictionary_shapes for state in shape.lifecycle_states
    }
    assert state_ids and state_ids <= {node.node_id for node in graph.nodes}
    assert not any(
        edge.source_id in state_ids and edge.target_id in state_ids for edge in graph.edges
    )
    assert "temporal" in " ".join(graph.limitations).lower()
    for state_id in state_ids:
        assert navigation_page(report, state_id).records
    structure_ids = {item.structure_id for item in report.data_structures}
    assert structure_ids <= {node.node_id for node in graph.nodes}
    assert {"producer", "writer", "mutator", "serializer", "alias"} <= {
        edge.kind for edge in graph.edges
    }


def test_conversion_edges_retain_actual_direction_and_distinct_sites(saved_structures):
    report = saved_structures
    graph = build_projection(report, ProjectionName("conversion-graph"))
    flows = [
        item for item in report.flow_edges if item.kind.value in {"conversion", "serialization"}
    ]
    edges = [edge for edge in graph.edges if edge.kind in {"conversion", "serialization"}]
    assert len(edges) == len(flows) == 2
    assert sorted((edge.source_id, edge.target_id, edge.kind) for edge in edges) == sorted(
        (flow.source_id, flow.target_id, flow.kind.value) for flow in flows
    )
    assert {edge.confidence for edge in edges} == {flow.confidence for flow in flows}
    assert {edge.edge_id for edge in edges} == {flow.flow_id for flow in flows}
    assert {edge.resolution.value for edge in edges} == {flow.resolution.value for flow in flows}


def test_structure_graphs_preserve_partial_source_evidence(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(Path(__file__).parent / "fixtures" / "structure_project", source)
    (source / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    report = scan(discover_context(source, AnalysisConfig()))
    assert report.completeness is Completeness.PARTIAL
    shutil.rmtree(source)
    for name in ("structure-lifecycle-graph", "conversion-graph"):
        graph = build_projection(report, ProjectionName(name))
        assert graph.completeness is Completeness.PARTIAL
        assert graph.nodes and graph.edges and not graph.truncated


@pytest.mark.parametrize("name", ("structure-lifecycle-graph", "conversion-graph"))
def test_structure_diagrams_render_graphviz_from_saved_data(saved_structures, name):
    if shutil.which("dot") is None:
        pytest.skip("Graphviz unavailable")
    graph = build_projection(saved_structures, ProjectionName(name))
    assert b"<svg" in render_projection_binary(graph, ProjectionFormat.SVG)
    assert render_projection_binary(graph, ProjectionFormat.PNG).startswith(b"\x89PNG")
