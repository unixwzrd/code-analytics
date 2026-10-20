from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from code_analysis.browse_export import (
    BrowseExportSelectionError,
    export_browse_page,
    render_browse_artifact,
)
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseIndexManifest
from code_analysis.config import AnalysisConfig
from code_analysis.models import SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projection_models import ProjectionCollapse, ProjectionInputRole
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.reports.projection_views import ProjectionFormat
from code_analysis.service import scan
from code_analysis.static_graph_browse import (
    GRAPH_PROJECTIONS,
    StaticGraphRequest,
    StaticGraphSelectionError,
)
from code_analysis.ui_navigation import NavigationUnavailable, navigation_page
from code_analysis.web_service import ReportViewService


@pytest.fixture
def indexed_structures(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(Path(__file__).parent / "fixtures" / "structure_project", source)
    report = scan(discover_context(source, AnalysisConfig()))
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), destination)
    shutil.rmtree(source)
    return report, BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


@pytest.mark.parametrize("name", GRAPH_PROJECTIONS)
def test_indexed_graphs_match_saved_evidence_and_focused_selection(indexed_structures, name):
    report, index = indexed_structures
    request = StaticGraphRequest(name=name)
    actual = index.static_graph(request)
    expected = build_projection(report, name, max_records=500, max_nodes=60, max_edges=120)
    assert actual.nodes == expected.nodes
    assert actual.edges == expected.edges
    assert actual.clusters == expected.clusters
    assert actual.completeness == expected.completeness
    assert actual.truncated == expected.truncated
    assert actual == index.static_graph(request)
    assert actual.nodes
    selected = (actual.nodes[0].node_id,)
    focused = index.static_graph(StaticGraphRequest(name=name, selected_ids=selected))
    expected_focus = build_projection(
        report, name, selected_ids=selected, max_records=500, max_nodes=60, max_edges=120
    )
    assert focused.nodes == expected_focus.nodes
    assert focused.edges == expected_focus.edges
    assert focused.truncated == expected_focus.truncated


@pytest.mark.parametrize(
    "output_format",
    (
        ProjectionFormat.JSON_COMPACT,
        ProjectionFormat.MARKDOWN,
        ProjectionFormat.DOT,
        ProjectionFormat.MERMAID,
        ProjectionFormat.SVG,
        ProjectionFormat.PNG,
    ),
)
def test_indexed_graph_export_is_source_free_and_never_overwrites(
    indexed_structures, output_format
):
    if output_format in (ProjectionFormat.SVG, ProjectionFormat.PNG) and not shutil.which("dot"):
        pytest.skip("optional Graphviz unavailable")
    _, index = indexed_structures
    request = StaticGraphRequest(name=ProjectionName.STRUCTURE_LIFECYCLE_GRAPH)
    path, graph = export_browse_page(index, request, "structure", output_format)
    assert path.read_bytes() == render_browse_artifact(graph, output_format)
    with pytest.raises(FileExistsError):
        export_browse_page(index, request, "structure", output_format)


def test_static_graph_manifest_rejects_missing_foreign_and_duplicate_summaries(indexed_structures):
    _, index = indexed_structures
    original = index.manifest.model_dump()
    graphs = original["static_graphs"]
    for malformed in (graphs[:-1], (graphs[0], *graphs)):
        with pytest.raises(ValueError, match="each static graph summary exactly once"):
            BrowseIndexManifest.model_validate({**original, "static_graphs": malformed})
    foreign = {**graphs[0], "source": {**graphs[0]["source"], "report_sha256": "a" * 64}}
    with pytest.raises(ValueError, match="belongs to another report"):
        BrowseIndexManifest.model_validate({**original, "static_graphs": (foreign, *graphs[1:])})
    for change in (
        {"role": ProjectionInputRole.BASELINE},
        {"run_identity_sha256": "b" * 64},
    ):
        foreign = {**graphs[0], "source": {**graphs[0]["source"], **change}}
        with pytest.raises(ValueError, match="belongs to another report"):
            BrowseIndexManifest.model_validate(
                {**original, "static_graphs": (foreign, *graphs[1:])}
            )


def test_unknown_graph_identity_is_invalid_selection_not_malformed_artifact(indexed_structures):
    _, index = indexed_structures
    with pytest.raises(BrowseExportSelectionError, match="absent from the prepared graph"):
        export_browse_page(
            index,
            StaticGraphRequest(
                name=ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
                selected_ids=("not-recorded",),
            ),
            "unknown",
            ProjectionFormat.JSON_COMPACT,
        )
    assert not (index.database.parent / "exports").exists()


@pytest.mark.parametrize("name", GRAPH_PROJECTIONS)
def test_indexed_language_selection_is_source_free_and_matches_shared_builder(
    indexed_structures, name
):
    report, index = indexed_structures
    request = StaticGraphRequest(name=name, languages=(SourceLanguage.PYTHON,))
    actual = index.static_graph(request)
    expected = build_projection(
        report,
        name,
        languages=(SourceLanguage.PYTHON,),
        max_records=500,
        max_nodes=60,
        max_edges=120,
    )
    assert actual.nodes == expected.nodes
    assert actual.edges == expected.edges
    assert actual.selection.languages == expected.selection.languages
    with pytest.raises(StaticGraphSelectionError, match="no prepared graph records match"):
        index.static_graph(StaticGraphRequest(name=name, languages=(SourceLanguage.JAVASCRIPT,)))


@pytest.mark.parametrize("name", GRAPH_PROJECTIONS)
@pytest.mark.parametrize("collapse", (ProjectionCollapse.MODULE, ProjectionCollapse.PACKAGE))
def test_indexed_collapse_matches_shared_builder(indexed_structures, name, collapse):
    report, index = indexed_structures
    request = StaticGraphRequest(name=name, collapse=collapse)
    actual = index.static_graph(request)
    expected = build_projection(
        report,
        name,
        collapse=collapse,
        max_records=500,
        max_nodes=60,
        max_edges=120,
    )
    assert actual.nodes == expected.nodes
    assert actual.edges == expected.edges
    assert actual.clusters == expected.clusters
    assert actual.original_node_count == expected.original_node_count
    assert actual.original_edge_count == expected.original_edge_count
    assert actual.omitted_node_count == expected.omitted_node_count
    assert actual.omitted_edge_count == expected.omitted_edge_count
    assert actual.selection.collapse is collapse


@pytest.mark.parametrize("collapse", (ProjectionCollapse.MODULE, ProjectionCollapse.PACKAGE))
def test_indexed_focus_and_language_precede_collapse(indexed_structures, collapse):
    report, index = indexed_structures
    name = ProjectionName.STRUCTURE_LIFECYCLE_GRAPH
    original = build_projection(report, name, languages=(SourceLanguage.PYTHON,))
    selected = (original.nodes[0].node_id,)
    request = StaticGraphRequest(
        name=name,
        selected_ids=selected,
        languages=(SourceLanguage.PYTHON,),
        collapse=collapse,
    )
    actual = index.static_graph(request)
    expected = build_projection(
        report,
        name,
        selected_ids=selected,
        languages=(SourceLanguage.PYTHON,),
        collapse=collapse,
        max_records=500,
        max_nodes=60,
        max_edges=120,
    )
    assert actual.nodes == expected.nodes
    assert actual.edges == expected.edges
    assert actual.original_node_count == expected.original_node_count
    assert actual.original_edge_count == expected.original_edge_count


def test_indexed_collapsed_bounds_preserve_original_and_omitted_counts(indexed_structures):
    report, index = indexed_structures
    name = ProjectionName.STRUCTURE_LIFECYCLE_GRAPH
    request = StaticGraphRequest(
        name=name,
        collapse=ProjectionCollapse.PACKAGE,
        max_records=2,
        max_nodes=2,
        max_edges=0,
    )
    actual = index.static_graph(request)
    expected = build_projection(
        report,
        name,
        collapse=ProjectionCollapse.PACKAGE,
        max_records=2,
        max_nodes=2,
        max_edges=0,
    )
    assert actual.nodes == expected.nodes
    assert actual.edges == expected.edges
    assert actual.original_node_count == expected.original_node_count
    assert actual.original_edge_count == expected.original_edge_count
    assert actual.omitted_node_count == expected.omitted_node_count
    assert actual.omitted_edge_count == expected.omitted_edge_count
    assert actual.truncated == expected.truncated


@pytest.mark.parametrize("name", GRAPH_PROJECTIONS)
def test_indexed_graph_navigation_matches_web_navigation_without_source(
    indexed_structures, name, monkeypatch
):
    report, index = indexed_structures
    request = StaticGraphRequest(name=name)
    graph = index.static_graph(request)

    def no_source(*_args, **_kwargs):
        raise AssertionError("graph navigation must not reopen report or target source")

    monkeypatch.setattr(Path, "read_text", no_source)
    for node in graph.nodes:
        try:
            expected = navigation_page(report, node.node_id)
        except NavigationUnavailable:
            with pytest.raises(NavigationUnavailable):
                index.graph_navigation(request, node.node_id)
        else:
            assert index.graph_navigation(request, node.node_id) == expected
            assert index.graph_navigation(request, node.node_id, expected.eligible + 1) == (
                navigation_page(report, node.node_id, expected.eligible + 1)
            )


def test_graph_navigation_rejects_nodes_outside_display_and_invalid_offsets(indexed_structures):
    _, index = indexed_structures
    request = StaticGraphRequest(name=ProjectionName.STRUCTURE_LIFECYCLE_GRAPH)
    complete = index.static_graph(request)
    bounded = request.model_copy(update={"max_nodes": 1, "max_edges": 0})
    retained = {item.node_id for item in index.static_graph(bounded).nodes}
    omitted = next(item.node_id for item in complete.nodes if item.node_id not in retained)
    with pytest.raises(StaticGraphSelectionError, match="not a retained graph node"):
        index.graph_navigation(bounded, omitted)
    for offset in (-1, True):
        with pytest.raises(StaticGraphSelectionError, match="nonnegative integer"):
            index.graph_navigation(request, complete.nodes[0].node_id, offset)


def test_graph_navigation_receipt_cannot_omit_rows(indexed_structures):
    _, index = indexed_structures
    from code_analysis.browse_index import _connect, _validate_database

    receipt = index.manifest.graph_navigation
    assert receipt.identity_count and receipt.record_count
    malformed = index.manifest.model_copy(
        update={"graph_navigation": receipt.model_copy(update={"record_count": 0})}
    )
    connection = _connect(index.database)
    try:
        with pytest.raises(ValueError, match="navigation counts disagree"):
            _validate_database(connection, malformed)
    finally:
        connection.close()
