from pathlib import Path

import pytest

from code_analysis.config import AnalysisConfig
from code_analysis.graph_ownership import GraphOwnership, graph_ownership
from code_analysis.models import Completeness, SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projection_models import ProjectionCollapse
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.service import scan


@pytest.fixture(scope="module")
def structure_report():
    return scan(
        discover_context(Path(__file__).parent / "fixtures" / "structure_project", AnalysisConfig())
    )


def test_ownership_is_detached_and_does_not_infer_from_labels():
    modules = {"pkg.module": SourceLanguage.PYTHON}
    owners = {"subject": "pkg.module"}
    context = GraphOwnership(modules, owners)
    modules.clear()
    owners.clear()
    assert context.module_for("subject") == "pkg.module"
    assert context.module_for("unknown") is None
    assert context.module_for("unknown", "pkg.module") == "pkg.module"
    with pytest.raises(TypeError):
        context.node_modules["subject"] = "elsewhere"


def test_structures_shapes_and_states_use_recorded_owner(structure_report):
    context = graph_ownership(structure_report)
    subjects = {item.subject_id: item for item in structure_report.subjects}
    for structure in structure_report.data_structures:
        assert (
            context.module_for(structure.structure_id)
            == subjects[structure.owner_subject_id].module
        )
    assert any(shape.lifecycle_states for shape in structure_report.dictionary_shapes)
    for shape in structure_report.dictionary_shapes:
        expected = subjects[shape.owner_subject_id].module
        assert context.module_for(shape.shape_id) == expected
        assert all(
            context.module_for(state.state_id) == expected for state in shape.lifecycle_states
        )


@pytest.mark.parametrize(
    "name", (ProjectionName.OWNERSHIP_ALIAS_FLOW, ProjectionName.STRUCTURE_LIFECYCLE_GRAPH)
)
@pytest.mark.parametrize("collapse", (ProjectionCollapse.MODULE, ProjectionCollapse.PACKAGE))
def test_structure_collapse_uses_exact_owner_and_retains_evidence(structure_report, name, collapse):
    full = build_projection(structure_report, name)
    grouped = build_projection(structure_report, name, collapse=collapse)
    unresolved = {item.subject_id for item in structure_report.subjects if item.module == "json"}
    expected_unresolved = {node.node_id for node in full.nodes} & unresolved
    assert len(grouped.nodes) == 1 + len(expected_unresolved)
    owner = "package.state" if collapse is ProjectionCollapse.MODULE else "package"
    assert grouped.nodes[0].node_id == f"collapsed-{collapse.value}:{owner}"
    assert {node.node_id for node in grouped.nodes[1:]} == expected_unresolved
    assert not grouped.truncated
    assert grouped.nodes[0].evidence_ids == tuple(
        sorted(
            {
                identity
                for node in full.nodes
                if node.node_id not in unresolved
                for identity in node.evidence_ids
            }
        )
    )
    state_ids = {
        state.state_id
        for shape in structure_report.dictionary_shapes
        for state in shape.lifecycle_states
    }
    if name is ProjectionName.STRUCTURE_LIFECYCLE_GRAPH:
        selected = build_projection(
            structure_report, name, selected_ids=(min(state_ids),), collapse=collapse, max_records=1
        )
        assert selected.nodes[0].node_id == grouped.nodes[0].node_id
        assert not selected.truncated


def test_finalizer_builds_ownership_once(structure_report, monkeypatch):
    import code_analysis.projections as projections

    calls = []
    original = projections.graph_ownership

    def counted(report):
        calls.append(report)
        return original(report)

    monkeypatch.setattr(projections, "graph_ownership", counted)
    build_projection(
        structure_report,
        ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
        collapse=ProjectionCollapse.MODULE,
    )
    assert calls == [structure_report]


def test_collapse_does_not_erase_upstream_truncation(structure_report):
    from code_analysis.projections import _finalize_graph

    graph = build_projection(structure_report, ProjectionName.STRUCTURE_LIFECYCLE_GRAPH)
    partial = graph.model_copy(
        update={
            "truncated": True,
            "truncation_reason": "upstream preparation limit",
            "completeness": Completeness.PARTIAL,
        }
    )
    selection = graph.selection.model_copy(update={"collapse": ProjectionCollapse.MODULE})
    result = _finalize_graph(
        structure_report, partial, selection, ProjectionName.STRUCTURE_LIFECYCLE_GRAPH
    )
    assert result.truncated and result.completeness is Completeness.PARTIAL
    assert result.truncation_reason == "upstream preparation limit"
    assert "Configured graph node or edge bounds omitted records." not in result.limitations


def test_group_navigation_does_not_invent_a_source_span(structure_report, tmp_path, monkeypatch):
    from code_analysis.browse_index import BrowseIndex
    from code_analysis.browse_index_build import build_browse_index
    from code_analysis.static_graph_browse import StaticGraphRequest
    from code_analysis.ui_navigation import NavigationUnavailable
    from code_analysis.web_service import ReportViewService

    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(structure_report), directory)
    index = BrowseIndex.open(directory, report_sha256=manifest.report_sha256)

    def no_source_read(*_args, **_kwargs):
        raise AssertionError("source/report text must not be read")

    monkeypatch.setattr(Path, "read_text", no_source_read)
    request = StaticGraphRequest(
        name=ProjectionName.STRUCTURE_LIFECYCLE_GRAPH, collapse=ProjectionCollapse.MODULE
    )
    graph = index.static_graph(request)
    identity = next(node.node_id for node in graph.nodes if node.node_id.startswith("collapsed-"))
    with pytest.raises(NavigationUnavailable):
        index.graph_navigation(request, identity)


def test_valid_inputs_exceeding_generated_group_quota_are_not_malformed(
    structure_report, tmp_path, monkeypatch
):
    import sqlite3

    import code_analysis.static_graph_browse as storage
    from code_analysis.browse_index import BrowseIndex
    from code_analysis.browse_index_build import build_browse_index
    from code_analysis.web_service import ReportViewService

    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(structure_report), directory)
    index = BrowseIndex.open(directory, report_sha256=manifest.report_sha256)
    name = ProjectionName.STRUCTURE_LIFECYCLE_GRAPH
    request = storage.StaticGraphRequest(name=name, collapse=ProjectionCollapse.MODULE)
    grouped = index.static_graph(request)
    with sqlite3.connect(index.database) as connection:
        payloads = connection.execute(
            "SELECT payload FROM static_graph_nodes WHERE name = ? UNION ALL "
            "SELECT payload FROM static_graph_edges WHERE name = ?",
            (name.value, name.value),
        ).fetchall()
    input_limit = max(len(payload.encode()) for (payload,) in payloads)
    assert max(len(node.model_dump_json().encode()) for node in grouped.nodes) > input_limit
    monkeypatch.setattr(storage, "_MAX_PAYLOAD_BYTES", input_limit)
    assert index.static_graph(storage.StaticGraphRequest(name=name)).nodes
    with pytest.raises(storage.StaticGraphReadLimitError, match="generated collapsed"):
        index.static_graph(request)
