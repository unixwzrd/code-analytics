from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness, SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projection_models import ProjectionCollapse
from code_analysis.projections import ProjectionBuildError, ProjectionName, build_projection
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.service import scan
from code_analysis.ui_navigation import navigation_page

FIXTURES = Path(__file__).parent / "fixtures"


def _saved_report(tmp_path: Path, fixture: str = "ordinary"):
    source = tmp_path / fixture
    shutil.copytree(FIXTURES / fixture, source)
    report = scan(discover_context(source, AnalysisConfig()))
    saved = load_json(render_json(report, compact=True))
    shutil.rmtree(source)
    return saved


def test_symbol_graph_round_trips_source_free_and_renders_deterministically(tmp_path) -> None:
    report = _saved_report(tmp_path)
    graph = build_projection(report, ProjectionName.SYMBOL_GRAPH)
    again = build_projection(report, ProjectionName.SYMBOL_GRAPH)

    assert graph.nodes and graph.edges and not graph.truncated
    assert graph.record_count == len(graph.nodes) + len(graph.edges)
    assert graph.original_node_count == len(graph.nodes)
    for output_format in (
        ProjectionFormat.JSON,
        ProjectionFormat.JSON_COMPACT,
        ProjectionFormat.MARKDOWN,
        ProjectionFormat.DOT,
        ProjectionFormat.MERMAID,
        ProjectionFormat.HTML,
    ):
        rendered = render_projection(graph, output_format)
        assert rendered == render_projection(again, output_format)
        assert str(tmp_path) not in rendered


def test_nested_symbols_remain_direct_module_members_without_inferred_edges(tmp_path) -> None:
    report = _saved_report(tmp_path)
    graph = build_projection(report, ProjectionName.SYMBOL_GRAPH)
    nested_id = "alpha.a:Outer.method.nested"
    inner_id = "alpha.a:Outer.Inner"

    assert {nested_id, inner_id} <= {node.node_id for node in graph.nodes}
    assert all(edge.kind == "module-membership" for edge in graph.edges)
    assert all(edge.source_id in {module.name for module in report.modules} for edge in graph.edges)
    assert not any(edge.source_id in {nested_id, inner_id} for edge in graph.edges)
    assert "calls" in " ".join(graph.limitations).lower()
    assert "ownership" in " ".join(graph.limitations).lower()


def test_symbol_graph_language_filter_and_exact_navigation_identity(tmp_path) -> None:
    python_report = _saved_report(tmp_path, "ordinary")
    javascript_report = _saved_report(tmp_path, "javascript")
    report = python_report.model_copy(
        update={"modules": python_report.modules + javascript_report.modules}
    )

    javascript = build_projection(
        report,
        ProjectionName.SYMBOL_GRAPH,
        languages=(SourceLanguage.JAVASCRIPT,),
    )
    assert javascript.nodes
    expected_modules = {
        module.name for module in report.modules if module.language is SourceLanguage.JAVASCRIPT
    }
    assert {node.node_id for node in javascript.nodes if node.kind == "module"} == expected_modules
    symbol_id = next(node.node_id for node in javascript.nodes if node.kind != "module")
    focused = build_projection(
        report,
        ProjectionName.SYMBOL_GRAPH,
        selected_ids=(symbol_id,),
    )
    assert {node.node_id for node in focused.nodes} == {
        symbol_id,
        next(edge.source_id for edge in javascript.edges if edge.target_id == symbol_id),
    }
    assert navigation_page(report, symbol_id).records[0].identity == symbol_id


def test_symbol_graph_bounds_and_malformed_selection(tmp_path) -> None:
    report = _saved_report(tmp_path)
    complete = build_projection(report, ProjectionName.SYMBOL_GRAPH)
    bounded = build_projection(
        report,
        ProjectionName.SYMBOL_GRAPH,
        max_records=3,
        max_nodes=2,
        max_edges=1,
    )
    assert bounded.truncated and bounded.completeness is Completeness.PARTIAL
    assert bounded.record_count <= 3
    assert bounded.omitted_node_count == len(complete.nodes) - len(bounded.nodes)
    assert bounded.omitted_edge_count == len(complete.edges) - len(bounded.edges)
    with pytest.raises(ProjectionBuildError, match="absent"):
        build_projection(
            report,
            ProjectionName.SYMBOL_GRAPH,
            selected_ids=("alpha.a:not-recorded",),
        )
    valid_id = complete.nodes[0].node_id
    with pytest.raises(ProjectionBuildError, match="not-recorded"):
        build_projection(
            report,
            ProjectionName.SYMBOL_GRAPH,
            selected_ids=(valid_id, "alpha.a:not-recorded"),
        )
    with pytest.raises(ProjectionBuildError, match="selected languages"):
        build_projection(
            report,
            ProjectionName.SYMBOL_GRAPH,
            languages=(SourceLanguage.RUBY,),
        )


@pytest.mark.parametrize(
    ("collapse", "prefix"),
    (
        (ProjectionCollapse.MODULE, "collapsed-module:"),
        (ProjectionCollapse.PACKAGE, "collapsed-package:"),
    ),
)
def test_symbol_graph_collapse_resolves_symbol_module_membership(
    tmp_path, collapse, prefix
) -> None:
    report = _saved_report(tmp_path)
    original = build_projection(report, ProjectionName.SYMBOL_GRAPH)
    collapsed = build_projection(
        report,
        ProjectionName.SYMBOL_GRAPH,
        collapse=collapse,
    )
    symbol_ids = {node.node_id for node in original.nodes if node.kind != "module"}
    assert collapsed.nodes
    assert all(node.node_id.startswith(prefix) for node in collapsed.nodes)
    assert symbol_ids.isdisjoint(node.node_id for node in collapsed.nodes)


def test_symbol_graph_rejects_duplicate_navigable_identity(tmp_path) -> None:
    report = _saved_report(tmp_path)
    module = next(item for item in report.modules if item.symbols)
    duplicate = module.model_copy(update={"symbols": (*module.symbols, module.symbols[0])})
    modules = tuple(duplicate if item.name == module.name else item for item in report.modules)
    malformed = report.model_copy(update={"modules": modules})

    with pytest.raises(ProjectionBuildError, match="duplicate recorded navigable symbol identity"):
        build_projection(malformed, ProjectionName.SYMBOL_GRAPH)


def test_symbol_graph_does_not_read_source_files(tmp_path, monkeypatch) -> None:
    report = _saved_report(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("saved-report projection attempted a source read")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    graph = build_projection(report, ProjectionName.SYMBOL_GRAPH)
    assert graph.nodes and graph.edges


def test_empty_failed_report_preserves_failed_state(tmp_path) -> None:
    report = _saved_report(tmp_path).model_copy(
        update={"modules": (), "completeness": Completeness.FAILED}
    )
    graph = build_projection(report, ProjectionName.SYMBOL_GRAPH)
    assert graph.completeness is Completeness.FAILED
    assert not graph.nodes and not graph.edges and not graph.truncated


@pytest.mark.parametrize("state", tuple(Completeness))
def test_tiny_graph_bounds_never_expand_or_soften_source_failure(tmp_path, state) -> None:
    report = _saved_report(tmp_path).model_copy(update={"completeness": state})
    graph = build_projection(
        report, ProjectionName.SYMBOL_GRAPH, max_records=3, max_nodes=1, max_edges=1
    )
    assert len(graph.nodes) == 1
    assert not graph.edges
    assert graph.truncated
    assert graph.omitted_node_count == graph.original_node_count - 1
    assert graph.omitted_edge_count == graph.original_edge_count
    assert graph.completeness is (
        Completeness.FAILED if state is Completeness.FAILED else Completeness.PARTIAL
    )


def test_one_record_graph_retains_selected_symbol_instead_of_reserving_an_edge(tmp_path) -> None:
    report = _saved_report(tmp_path)
    symbol_id = next(
        f"{module.name}:{symbol.qualified_name}"
        for module in report.modules
        for symbol in module.symbols
    )
    graph = build_projection(
        report, ProjectionName.SYMBOL_GRAPH, selected_ids=(symbol_id,), max_records=1
    )
    assert [node.node_id for node in graph.nodes] == [symbol_id]
    assert not graph.edges
    assert graph.record_count == 1 and graph.truncated


def test_symbol_selection_precedes_module_collapse(tmp_path) -> None:
    report = _saved_report(tmp_path)
    graph = build_projection(
        report,
        ProjectionName.SYMBOL_GRAPH,
        selected_ids=("alpha.a:Outer",),
        collapse=ProjectionCollapse.MODULE,
    )
    assert [node.node_id for node in graph.nodes] == ["collapsed-module:alpha.a"]
    assert not graph.edges and not graph.truncated


def test_javascript_package_collapse_keeps_flat_filename_extensions(tmp_path) -> None:
    report = _saved_report(tmp_path, "javascript")
    graph = build_projection(
        report, ProjectionName.SYMBOL_GRAPH, collapse=ProjectionCollapse.PACKAGE
    )
    expected = {f"collapsed-package:{module.name.split('/', 1)[0]}" for module in report.modules}
    assert {node.node_id for node in graph.nodes} == expected


def test_collapsed_selected_owner_has_priority_over_neighboring_module_under_bounds():
    report = load_json((Path(__file__).parent / "golden" / "phase3-report.json").read_text())
    graph = build_projection(report, ProjectionName.CLONE_GRAPH)
    present = {node.node_id for node in graph.nodes}
    selected = next(
        item.subject_id
        for item in report.subjects
        if item.module == "package.combine_b" and item.subject_id in present
    )
    focused = build_projection(
        report,
        ProjectionName.CLONE_GRAPH,
        selected_ids=(selected,),
        collapse=ProjectionCollapse.MODULE,
        max_nodes=1,
    )
    assert [node.node_id for node in focused.nodes] == ["collapsed-module:package.combine_b"]
    assert focused.truncated
