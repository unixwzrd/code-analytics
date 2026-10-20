from __future__ import annotations

import shutil

import pytest

from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness, SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionBuildError, ProjectionName, build_projection
from code_analysis.service import scan
from code_analysis.ui_projections import (
    UIProjectionRequest,
    graph_node_selection,
    project_report,
)


def test_hierarchy_selection_is_bound_to_exact_saved_projection(ordinary_report):
    request = UIProjectionRequest(name=ProjectionName.HIERARCHY)
    projection = project_report(ordinary_report, request)
    for node in projection.nodes:
        response = graph_node_selection(
            ordinary_report, request, projection.projection_id, node.node_id
        )
        if node.kind == "module":
            assert response.navigation_identity == node.node_id
        else:
            assert response.navigation_identity is None and response.unavailable_reason
    with pytest.raises(ValueError, match="does not match"):
        graph_node_selection(ordinary_report, request, "projection-" + "0" * 20, "alpha")
    with pytest.raises(ValueError, match="not retained"):
        graph_node_selection(ordinary_report, request, projection.projection_id, "missing")


def test_hierarchy_bounds_keep_parents_and_source_partiality(ordinary_report):
    graph = build_projection(ordinary_report, ProjectionName.HIERARCHY, max_records=1)
    assert graph.truncated and graph.completeness is Completeness.PARTIAL
    assert len(graph.nodes) == 1 and graph.nodes[0].parent_id is None


def test_hierarchy_rejects_ambiguous_module_names_instead_of_dropping_nodes(ordinary_report):
    malformed = ordinary_report.model_copy(
        update={"modules": (*ordinary_report.modules, ordinary_report.modules[0])}
    )
    with pytest.raises(ProjectionBuildError, match="ambiguous recorded module"):
        build_projection(malformed, ProjectionName.HIERARCHY)


def test_mixed_hierarchy_preserves_path_extensions_and_language_filter(tmp_path):
    pytest.importorskip("tree_sitter_javascript")
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (source / "web").mkdir()
    (source / "web" / "client.test.js").write_text(
        "export function run() { return 1; }\n", encoding="utf-8"
    )
    report = scan(discover_context(source, AnalysisConfig()))
    shutil.rmtree(source)
    request = UIProjectionRequest(
        name=ProjectionName.HIERARCHY, languages=(SourceLanguage.JAVASCRIPT,)
    )
    graph = project_report(report, request)
    modules = [node for node in graph.nodes if node.kind == "module"]
    assert len(modules) == 1 and modules[0].label == "client.test.js"
    assert modules[0].node_id == "web/client.test.js"
    assert modules[0].parent_id == "hierarchy-group:javascript:web"
    assert (
        graph_node_selection(
            report, request, graph.projection_id, modules[0].node_id
        ).navigation_identity
        == modules[0].node_id
    )
    python = build_projection(report, ProjectionName.HIERARCHY, languages=(SourceLanguage.PYTHON,))
    assert all(node.node_id != modules[0].node_id for node in python.nodes)
    assert any(node.node_id == "app" for node in python.nodes)


def test_hierarchy_nested_namespace_groups_do_not_claim_source(tmp_path):
    source = tmp_path / "source"
    package = source / "outer" / "inner"
    package.mkdir(parents=True)
    (package / "leaf.py").write_text("def work():\n    pass\n", encoding="utf-8")
    report = scan(discover_context(source, AnalysisConfig()))
    shutil.rmtree(source)
    request = UIProjectionRequest(name=ProjectionName.HIERARCHY)
    graph = project_report(report, request)
    packages = [node for node in graph.nodes if node.kind == "package"]
    assert packages
    for node in packages:
        response = graph_node_selection(report, request, graph.projection_id, node.node_id)
        assert response.navigation_identity is None and response.unavailable_reason
    assert all(
        node.parent_id is None or node.parent_id in {n.node_id for n in graph.nodes}
        for node in graph.nodes
    )
