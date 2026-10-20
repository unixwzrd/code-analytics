from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

import code_analysis.static_graph_browse as static_graph_browse
from code_analysis.config import AnalysisConfig
from code_analysis.graph_selection import GraphEdgeIdentity, GraphNodeRank, retain_graph
from code_analysis.models import SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projection_models import GraphNode, GraphProjection, ProjectionCollapse
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.service import scan
from code_analysis.static_graph_browse import (
    GRAPH_PROJECTIONS,
    StaticGraphRequest,
    StaticGraphSelectionError,
    read_static_graph,
    validate_static_graphs,
    write_static_graphs,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def structure_report():
    return scan(discover_context(FIXTURES / "structure_project", AnalysisConfig()))


@pytest.fixture
def graph_index(structure_report):
    connection = sqlite3.connect(":memory:")
    summaries = write_static_graphs(connection, structure_report)
    yield connection, summaries
    connection.close()


def test_request_is_strict_canonical_and_bounded() -> None:
    request = StaticGraphRequest.model_validate(
        {
            "name": ProjectionName.CONVERSION_GRAPH,
            "selected_ids": ("a", "b"),
            "max_records": 500,
            "max_nodes": 100,
            "max_edges": 250,
        }
    )
    assert request.selected_ids == ("a", "b")
    assert request.languages == ()
    for payload in (
        {"name": ProjectionName.MODULES},
        {"name": ProjectionName.CONVERSION_GRAPH, "max_nodes": True},
        {"name": ProjectionName.CONVERSION_GRAPH, "selected_ids": ("b", "a")},
        {
            "name": ProjectionName.CONVERSION_GRAPH,
            "languages": (SourceLanguage.PYTHON, SourceLanguage.JAVASCRIPT),
        },
        {
            "name": ProjectionName.CONVERSION_GRAPH,
            "languages": (SourceLanguage.PYTHON, SourceLanguage.PYTHON),
        },
        {"name": ProjectionName.CONVERSION_GRAPH, "languages": ("unknown",)},
        {
            "name": ProjectionName.CONVERSION_GRAPH,
            "selected_ids": tuple(str(i) for i in range(26)),
        },
        {"name": ProjectionName.CONVERSION_GRAPH, "unknown": 1},
    ):
        with pytest.raises(ValidationError):
            StaticGraphRequest.model_validate(payload)


@pytest.mark.parametrize("name", GRAPH_PROJECTIONS)
def test_language_filter_matches_builder_and_nonmatch_is_explicit(
    structure_report, graph_index, name
) -> None:
    connection, summaries = graph_index
    request = StaticGraphRequest(name=name, languages=(SourceLanguage.PYTHON,))
    loaded = read_static_graph(connection, summaries, request)
    expected = build_projection(
        structure_report,
        name,
        languages=(SourceLanguage.PYTHON,),
        max_records=500,
        max_nodes=60,
        max_edges=120,
    )
    assert loaded.nodes == expected.nodes
    assert loaded.edges == expected.edges
    assert loaded.selection.languages == (SourceLanguage.PYTHON,)
    with pytest.raises(StaticGraphSelectionError, match="no prepared graph records match"):
        read_static_graph(
            connection,
            summaries,
            StaticGraphRequest(name=name, languages=(SourceLanguage.JAVASCRIPT,)),
        )


def test_validation_rejects_malformed_and_inconsistent_language_membership(graph_index) -> None:
    connection, summaries = graph_index
    name, edge_id = connection.execute(
        "SELECT name, edge_id FROM static_graph_edges ORDER BY name, edge_id LIMIT 1"
    ).fetchone()
    connection.execute(
        "UPDATE static_graph_edges SET languages = ? WHERE name = ? AND edge_id = ?",
        ('["python","python"]', name, edge_id),
    )
    with pytest.raises(ValueError, match="invalid static graph language membership"):
        validate_static_graphs(connection, summaries)


def test_validation_rejects_malformed_or_forged_owner_metadata(graph_index) -> None:
    connection, summaries = graph_index
    name, node_id = connection.execute(
        "SELECT name, node_id FROM static_graph_nodes "
        "WHERE owner_module IS NOT NULL ORDER BY name, node_id LIMIT 1"
    ).fetchone()
    connection.execute(
        "UPDATE static_graph_nodes SET owner_language = ? WHERE name = ? AND node_id = ?",
        (SourceLanguage.JAVASCRIPT.value, name, node_id),
    )
    with pytest.raises(ValueError, match="owner metadata disagrees with receipt"):
        validate_static_graphs(connection, summaries)


def test_validation_rejects_owner_receipt_not_used_by_nodes(graph_index) -> None:
    connection, summaries = graph_index
    target = next(summary for summary in summaries if summary.owner_modules)
    malformed = tuple(
        summary.model_copy(update={"owner_modules": ()}) if summary.name is target.name else summary
        for summary in summaries
    )
    with pytest.raises(ValueError, match="owner metadata disagrees with receipt"):
        validate_static_graphs(connection, malformed)


def test_validation_bounds_language_metadata_before_json_parsing(graph_index) -> None:
    connection, summaries = graph_index
    name, node_id = connection.execute(
        "SELECT name, node_id FROM static_graph_nodes ORDER BY name, node_id LIMIT 1"
    ).fetchone()
    connection.execute(
        "UPDATE static_graph_nodes SET languages = ? WHERE name = ? AND node_id = ?",
        ("[" + " " * 1000 + "]", name, node_id),
    )
    with pytest.raises(ValueError, match="invalid static graph language membership"):
        validate_static_graphs(connection, summaries)


@pytest.mark.parametrize("name", GRAPH_PROJECTIONS)
def test_cross_language_membership_matches_exact_builder_scope(structure_report, name) -> None:
    if name is ProjectionName.STATIC_CALL_GRAPH:
        # Call-site language is owned by the source/occurrence, not its navigation subject.
        connection = sqlite3.connect(":memory:")
        summaries = write_static_graphs(connection, structure_report)
        with pytest.raises(ValueError, match="language"):
            read_static_graph(
                connection,
                summaries,
                StaticGraphRequest(name=name, languages=(SourceLanguage.JAVASCRIPT,)),
            )
        with pytest.raises(ValueError, match="language"):
            build_projection(structure_report, name, languages=(SourceLanguage.JAVASCRIPT,))
        connection.close()
        return
    if name in {
        ProjectionName.OWNERSHIP_ALIAS_FLOW,
        ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
    }:
        changed_subject_id = structure_report.data_structures[-1].owner_subject_id
    elif name is ProjectionName.LEXICAL_CALL_GRAPH:
        changed_subject_id = structure_report.function_profiles[-1].subject_id
    elif name is ProjectionName.DEPENDENCIES:
        changed_module_name = structure_report.modules[-1].name
        modules = tuple(
            (
                item.model_copy(update={"language": SourceLanguage.JAVASCRIPT})
                if item.name == changed_module_name
                else item
            )
            for item in structure_report.modules
        )
        mixed = structure_report.model_copy(update={"modules": modules})
        connection = sqlite3.connect(":memory:")
        summaries = write_static_graphs(connection, mixed)
        actual = read_static_graph(
            connection,
            summaries,
            StaticGraphRequest(name=name, languages=(SourceLanguage.JAVASCRIPT,)),
        )
        expected = build_projection(
            mixed,
            name,
            languages=(SourceLanguage.JAVASCRIPT,),
            max_records=500,
            max_nodes=60,
            max_edges=120,
        )
        assert actual.nodes == expected.nodes
        assert actual.edges == expected.edges
        validate_static_graphs(connection, summaries)
        connection.close()
        return
    else:
        relevant_flows = tuple(
            flow
            for flow in structure_report.flow_edges
            if name is ProjectionName.DICTIONARY_FLOW
            or flow.kind.value in {"conversion", "serialization"}
        )
        changed_subject_id = relevant_flows[-1].target_id
    subjects = tuple(
        (
            item.model_copy(update={"language": SourceLanguage.JAVASCRIPT})
            if item.subject_id == changed_subject_id
            else item
        )
        for item in structure_report.subjects
    )
    profiles = tuple(
        (
            item.model_copy(update={"language": SourceLanguage.JAVASCRIPT})
            if name is ProjectionName.LEXICAL_CALL_GRAPH and item.subject_id == changed_subject_id
            else item
        )
        for item in structure_report.function_profiles
    )
    mixed = structure_report.model_copy(
        update={"subjects": subjects, "function_profiles": profiles}
    )
    connection = sqlite3.connect(":memory:")
    summaries = write_static_graphs(connection, mixed)
    actual = read_static_graph(
        connection,
        summaries,
        StaticGraphRequest(name=name, languages=(SourceLanguage.JAVASCRIPT,)),
    )
    expected = build_projection(
        mixed,
        name,
        languages=(SourceLanguage.JAVASCRIPT,),
        max_records=500,
        max_nodes=60,
        max_edges=120,
    )
    assert actual.nodes == expected.nodes
    assert actual.edges == expected.edges
    validate_static_graphs(connection, summaries)
    connection.close()


def test_call_sites_use_actual_source_language_membership(tmp_path: Path) -> None:
    pytest.importorskip("tree_sitter_javascript")
    (tmp_path / "app.py").write_text("def run():\n    helper()\n")
    (tmp_path / "app.js").write_text("function run() { helper(); }\n")
    report = scan(discover_context(tmp_path, AnalysisConfig()))
    with sqlite3.connect(":memory:") as connection:
        summaries = write_static_graphs(connection, report)
        for language in (SourceLanguage.PYTHON, SourceLanguage.JAVASCRIPT):
            request = StaticGraphRequest(
                name=ProjectionName.STATIC_CALL_GRAPH, languages=(language,)
            )
            actual = read_static_graph(connection, summaries, request)
            expected = build_projection(
                report,
                request.name,
                languages=request.languages,
                max_records=500,
                max_nodes=60,
                max_edges=120,
            )
            assert actual.nodes == expected.nodes
            assert actual.edges == expected.edges
            assert len(actual.edges) == 1
        validate_static_graphs(connection, summaries)


def test_package_collapse_uses_python_dots_and_javascript_slashes(structure_report) -> None:
    python_connection = sqlite3.connect(":memory:")
    python_summaries = write_static_graphs(python_connection, structure_report)
    python_graph = read_static_graph(
        python_connection,
        python_summaries,
        StaticGraphRequest(
            name=ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
            collapse=ProjectionCollapse.PACKAGE,
        ),
    )
    assert any(node.node_id == "collapsed-package:package" for node in python_graph.nodes)
    python_connection.close()

    module_names = {"package": "web", "package.state": "web/state"}
    modules = tuple(
        module.model_copy(
            update={
                "name": module_names[module.name],
                "language": SourceLanguage.JAVASCRIPT,
            }
        )
        for module in structure_report.modules
    )
    subjects = tuple(
        subject.model_copy(
            update={
                "module": module_names.get(subject.module, subject.module),
                "language": (
                    SourceLanguage.JAVASCRIPT
                    if subject.module in module_names
                    else subject.language
                ),
            }
        )
        for subject in structure_report.subjects
    )
    javascript_report = structure_report.model_copy(
        update={
            "modules": modules,
            "subjects": subjects,
            "function_profiles": tuple(
                (
                    profile.model_copy(update={"language": SourceLanguage.JAVASCRIPT})
                    if profile.subject_id in {subject.subject_id for subject in subjects}
                    else profile
                )
                for profile in structure_report.function_profiles
            ),
        }
    )
    javascript_connection = sqlite3.connect(":memory:")
    javascript_summaries = write_static_graphs(javascript_connection, javascript_report)
    actual = read_static_graph(
        javascript_connection,
        javascript_summaries,
        StaticGraphRequest(
            name=ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
            collapse=ProjectionCollapse.PACKAGE,
        ),
    )
    expected = build_projection(
        javascript_report,
        ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
        collapse=ProjectionCollapse.PACKAGE,
        max_records=500,
        max_nodes=60,
        max_edges=120,
    )
    assert actual.nodes == expected.nodes
    assert actual.edges == expected.edges
    assert any(node.node_id == "collapsed-package:web" for node in actual.nodes)
    assert not any(node.node_id == "collapsed-package:web/state" for node in actual.nodes)
    javascript_connection.close()


def test_dependency_membership_covers_isolated_modules_and_cross_language_edges(
    ordinary_report,
) -> None:
    projection = build_projection(ordinary_report, ProjectionName.DEPENDENCIES)
    node_languages, edge_languages = static_graph_browse._language_memberships(
        ordinary_report, ProjectionName.DEPENDENCIES, projection
    )
    modules = {item.name: item.language for item in ordinary_report.modules}
    for node in projection.nodes:
        assert node_languages[node.node_id] == {modules[node.node_id]}
    for edge in projection.edges:
        assert edge_languages[edge.edge_id] == {
            modules[edge.source_id],
            modules[edge.target_id],
        }


@pytest.mark.parametrize("name", (ProjectionName.DICTIONARY_FLOW, ProjectionName.CONVERSION_GRAPH))
def test_flow_membership_covers_prepared_nodes_when_all_edges_are_omitted(
    structure_report, name
) -> None:
    projection = build_projection(structure_report, name, max_edges=0)
    assert projection.nodes and not projection.edges and projection.truncated
    node_languages, edge_languages = static_graph_browse._language_memberships(
        structure_report, name, projection
    )
    assert not edge_languages
    assert all(languages == {SourceLanguage.PYTHON} for languages in node_languages.values())


def test_conversion_membership_ignores_unrelated_flow_languages(structure_report) -> None:
    unrelated = next(
        flow
        for flow in structure_report.flow_edges
        if flow.kind.value not in {"conversion", "serialization"}
    )
    subjects = tuple(
        (
            item.model_copy(update={"language": SourceLanguage.JAVASCRIPT})
            if item.subject_id == unrelated.target_id
            else item
        )
        for item in structure_report.subjects
    )
    mixed = structure_report.model_copy(update={"subjects": subjects})
    projection = build_projection(mixed, ProjectionName.CONVERSION_GRAPH, max_edges=0)
    node_languages, _edge_languages = static_graph_browse._language_memberships(
        mixed, ProjectionName.CONVERSION_GRAPH, projection
    )
    assert unrelated.source_id in node_languages
    assert node_languages[unrelated.source_id] == {SourceLanguage.PYTHON}


def test_writes_one_summary_per_canonical_graph_and_exact_projection_values(
    structure_report, graph_index
) -> None:
    connection, summaries = graph_index
    assert tuple(summary.name for summary in summaries) == GRAPH_PROJECTIONS
    assert all(summary.available for summary in summaries)
    validate_static_graphs(connection, summaries)
    for summary in summaries:
        prepared = build_projection(
            structure_report,
            summary.name,
            max_records=100_000,
            max_nodes=100_000,
            max_edges=250_000,
        )
        loaded = read_static_graph(
            connection,
            summaries,
            StaticGraphRequest(name=summary.name, max_records=500, max_nodes=100, max_edges=250),
        )
        assert isinstance(prepared, GraphProjection)
        assert loaded.nodes == prepared.nodes
        assert loaded.edges == prepared.edges
        assert loaded.clusters == prepared.clusters


def test_empty_evidence_has_precise_unavailable_summaries(ordinary_report) -> None:
    connection = sqlite3.connect(":memory:")
    summaries = write_static_graphs(connection, ordinary_report)
    assert tuple(summary.name for summary in summaries) == GRAPH_PROJECTIONS
    assert all(
        summary.available or (summary.unavailable_reason and summary.node_count == 0)
        for summary in summaries
    )
    validate_static_graphs(connection, summaries)


def test_summary_rejects_forged_original_counts_and_preparation(graph_index) -> None:
    _connection, summaries = graph_index
    summary = next(item for item in summaries if item.available and not item.truncated)
    payload = summary.model_dump()
    with pytest.raises(ValidationError, match="original records require truncation"):
        type(summary).model_validate({**payload, "original_node_count": summary.node_count + 1})
    preparation = summary.preparation.model_copy(update={"max_nodes": 99_999})
    with pytest.raises(ValidationError, match="canonical bounds"):
        type(summary).model_validate({**payload, "preparation": preparation})


def test_reader_is_source_free(monkeypatch, graph_index) -> None:
    connection, summaries = graph_index

    def fail_source_read(*_args, **_kwargs):
        raise AssertionError("source read attempted")

    monkeypatch.setattr(Path, "read_text", fail_source_read)
    projection = read_static_graph(
        connection,
        summaries,
        StaticGraphRequest(name=ProjectionName.STRUCTURE_LIFECYCLE_GRAPH),
    )
    assert projection.nodes


def test_selected_identity_retains_exact_one_hop_and_core_ranking(
    structure_report, graph_index
) -> None:
    connection, summaries = graph_index
    name = ProjectionName.STRUCTURE_LIFECYCLE_GRAPH
    node_rows = connection.execute(
        "SELECT node_id, scc_member, fan_in, fan_out FROM static_graph_nodes "
        "WHERE name = ? ORDER BY node_id",
        (name.value,),
    ).fetchall()
    edge_rows = connection.execute(
        "SELECT edge_id, source_id, target_id FROM static_graph_edges "
        "WHERE name = ? ORDER BY edge_id",
        (name.value,),
    ).fetchall()
    selected_id = edge_rows[0][1]
    neighbors = {
        endpoint
        for _edge_id, source_id, target_id in edge_rows
        if selected_id in (source_id, target_id)
        for endpoint in (source_id, target_id)
    } | {selected_id}
    eligible_nodes = tuple(
        GraphNodeRank(node_id, node_id == selected_id, bool(scc), fan_in, fan_out)
        for node_id, scc, fan_in, fan_out in node_rows
        if node_id in neighbors
    )
    eligible_edges = tuple(
        GraphEdgeIdentity(*row) for row in edge_rows if row[1] in neighbors and row[2] in neighbors
    )
    expected = retain_graph(eligible_nodes, eligible_edges, max_records=5, max_nodes=3, max_edges=2)
    actual = read_static_graph(
        connection,
        summaries,
        StaticGraphRequest(
            name=name,
            selected_ids=(selected_id,),
            max_records=5,
            max_nodes=3,
            max_edges=2,
        ),
    )
    assert tuple(node.node_id for node in actual.nodes) == expected.node_ids
    assert tuple(edge.edge_id for edge in actual.edges) == expected.edge_ids
    assert all(
        edge.source_id in expected.node_ids and edge.target_id in expected.node_ids
        for edge in actual.edges
    )
    public = build_projection(
        structure_report,
        name,
        selected_ids=(selected_id,),
        max_records=500,
        max_nodes=100,
        max_edges=250,
    )
    focused = read_static_graph(
        connection,
        summaries,
        StaticGraphRequest(name=name, selected_ids=(selected_id,)),
    )
    assert isinstance(public, GraphProjection)
    assert focused.nodes == public.nodes
    assert focused.edges == public.edges
    assert focused.original_node_count == public.original_node_count
    assert focused.original_edge_count == public.original_edge_count
    assert not focused.truncated


def test_display_bounds_preserve_preparation_counts_and_unknown_selection_fails(
    graph_index,
) -> None:
    connection, summaries = graph_index
    summary = next(
        item for item in summaries if item.name is ProjectionName.STRUCTURE_LIFECYCLE_GRAPH
    )
    projection = read_static_graph(
        connection,
        summaries,
        StaticGraphRequest(name=summary.name, max_records=2, max_nodes=2, max_edges=0),
    )
    assert projection.truncated
    assert projection.original_node_count == summary.original_node_count
    assert projection.omitted_node_count == summary.original_node_count - len(projection.nodes)
    with pytest.raises(StaticGraphSelectionError, match="absent from the prepared graph"):
        read_static_graph(
            connection,
            summaries,
            StaticGraphRequest(name=summary.name, selected_ids=("not-indexed",)),
        )


def test_validation_rejects_tampered_payload_and_dangling_endpoint(graph_index) -> None:
    connection, summaries = graph_index
    name, edge_id = connection.execute(
        "SELECT name, edge_id FROM static_graph_edges ORDER BY name, edge_id LIMIT 1"
    ).fetchone()
    connection.execute(
        "UPDATE static_graph_edges SET target_id = 'missing' WHERE name = ? AND edge_id = ?",
        (name, edge_id),
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_static_graphs(connection, summaries)


def test_reader_rejects_individual_and_aggregate_payload_bounds(graph_index) -> None:
    connection, summaries = graph_index
    name, node_id = connection.execute(
        "SELECT name, node_id FROM static_graph_nodes ORDER BY name, node_id LIMIT 1"
    ).fetchone()
    connection.execute(
        "UPDATE static_graph_nodes SET payload = ? WHERE name = ? AND node_id = ?",
        ("x" * (1024**2 + 1), name, node_id),
    )
    with pytest.raises(ValueError, match="one-MiB"):
        read_static_graph(
            connection,
            summaries,
            StaticGraphRequest(name=ProjectionName(name), max_nodes=100, max_edges=250),
        )

    aggregate_name = ProjectionName.STRUCTURE_LIFECYCLE_GRAPH
    rows = connection.execute(
        "SELECT node_id, payload FROM static_graph_nodes WHERE name = ?",
        (aggregate_name.value,),
    ).fetchall()
    for aggregate_node_id, payload in rows:
        node = GraphNode.model_validate_json(payload).model_copy(update={"label": "x" * 950_000})
        connection.execute(
            "UPDATE static_graph_nodes SET payload = ? WHERE name = ? AND node_id = ?",
            (node.model_dump_json(), aggregate_name.value, aggregate_node_id),
        )
    with pytest.raises(ValueError, match="eight-MiB byte budget"):
        read_static_graph(
            connection,
            summaries,
            StaticGraphRequest(name=aggregate_name, max_nodes=100, max_edges=250),
        )


def test_validation_rejects_non_text_payload(graph_index) -> None:
    connection, summaries = graph_index
    name, node_id = connection.execute(
        "SELECT name, node_id FROM static_graph_nodes ORDER BY name, node_id LIMIT 1"
    ).fetchone()
    connection.execute(
        "UPDATE static_graph_nodes SET payload = ? WHERE name = ? AND node_id = ?",
        (sqlite3.Binary(b"{}"), name, node_id),
    )
    with pytest.raises(ValueError, match="payload must be text"):
        validate_static_graphs(connection, summaries)


def test_reader_checks_running_budget_before_parsing_next_record(monkeypatch, graph_index) -> None:
    connection, summaries = graph_index
    name = ProjectionName.STRUCTURE_LIFECYCLE_GRAPH
    payloads = [
        row[0]
        for row in connection.execute(
            "SELECT payload FROM static_graph_nodes WHERE name = ? ORDER BY node_id LIMIT 2",
            (name.value,),
        )
    ]
    assert len(payloads) == 2
    parsed = 0
    validate = GraphNode.model_validate_json

    def count_validation(payload):
        nonlocal parsed
        parsed += 1
        return validate(payload)

    monkeypatch.setattr(static_graph_browse, "_MAX_RESULT_BYTES", len(payloads[0].encode()))
    monkeypatch.setattr(GraphNode, "model_validate_json", count_validation)
    with pytest.raises(ValueError, match=r"read exceeds .* byte budget"):
        read_static_graph(
            connection,
            summaries,
            StaticGraphRequest(name=name, max_nodes=100, max_edges=250),
        )
    assert parsed == 1


def test_collapsed_reader_charges_all_focused_payloads_before_parsing(
    monkeypatch, graph_index
) -> None:
    connection, summaries = graph_index
    name = ProjectionName.STRUCTURE_LIFECYCLE_GRAPH
    payloads = [
        row[0]
        for row in connection.execute(
            "SELECT payload FROM static_graph_nodes WHERE name = ? ORDER BY node_id LIMIT 2",
            (name.value,),
        )
    ]
    assert len(payloads) == 2
    parsed = 0
    validate = GraphNode.model_validate_json

    def count_validation(payload):
        nonlocal parsed
        parsed += 1
        return validate(payload)

    monkeypatch.setattr(static_graph_browse, "_MAX_RESULT_BYTES", len(payloads[0].encode()))
    monkeypatch.setattr(GraphNode, "model_validate_json", count_validation)
    with pytest.raises(ValueError, match=r"read exceeds .* byte budget"):
        read_static_graph(
            connection,
            summaries,
            StaticGraphRequest(
                name=name,
                collapse=ProjectionCollapse.MODULE,
                max_nodes=1,
                max_edges=0,
            ),
        )
    assert parsed == 1
