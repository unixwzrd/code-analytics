"""Saved-report-only module-to-symbol membership graph."""

from __future__ import annotations

from code_analysis.projection_models import (
    EdgeDirection,
    GraphCluster,
    GraphEdge,
    GraphNode,
    GraphProjection,
    ProjectionSelection,
)
from code_analysis.projections._catalog import ProjectionBuildError, ProjectionName
from code_analysis.projections._shared import (
    _edge_id,
    _limitations,
    _projection_id,
    _projection_state,
    _report_input,
)
from code_analysis.report_models import AnalysisReport


def _symbol_graph_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> GraphProjection:
    """Show only exact module-to-symbol membership recorded in the saved report."""
    modules = tuple(
        module
        for module in report.modules
        if not selection.languages or module.language in selection.languages
    )
    if not modules and report.modules:
        raise ProjectionBuildError("symbol-graph has no modules for the selected languages")

    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    clusters: list[GraphCluster] = []
    for module in sorted(modules, key=lambda item: item.name):
        if module.name in nodes:
            raise ProjectionBuildError(
                f"symbol-graph has ambiguous recorded module identity: {module.name}"
            )
        nodes[module.name] = GraphNode(
            node_id=module.name,
            label=module.name,
            kind="module",
        )
        symbol_identities: set[str] = set()
        for symbol in sorted(
            module.symbols,
            key=lambda item: (item.qualified_name, item.span.start_line, item.span.start_column),
        ):
            identity = f"{module.name}:{symbol.qualified_name}"
            if identity in symbol_identities:
                raise ProjectionBuildError(
                    "symbol-graph has duplicate recorded navigable symbol identity "
                    f"within module {module.name}: {identity}"
                )
            symbol_identities.add(identity)
            if identity in nodes:
                raise ProjectionBuildError(
                    f"symbol-graph has colliding recorded node identity: {identity}"
                )
            nodes[identity] = GraphNode(
                node_id=identity,
                label=symbol.qualified_name,
                kind=symbol.kind,
                group_id=module.name,
            )
            edges.append(
                GraphEdge(
                    edge_id=_edge_id(module.name, identity, "module-membership"),
                    source_id=module.name,
                    target_id=identity,
                    kind="module-membership",
                    label="recorded member",
                )
            )
        if symbol_identities:
            clusters.append(
                GraphCluster(
                    cluster_id=module.name,
                    label=module.name,
                    node_ids=tuple(sorted(symbol_identities)),
                )
            )

    missing = set(selection.selected_ids) - set(nodes)
    if missing:
        raise ProjectionBuildError(
            "selected symbol-graph identities are absent from this graph: "
            + ", ".join(sorted(missing))
        )

    source = _report_input(report)
    limitations = set(_limitations(report, False))
    limitations.update(
        {
            "Module-to-symbol edges represent recorded membership only; they do not "
            "establish lexical ownership or parentage.",
            "This projection does not infer calls, inheritance, or runtime relationships.",
        }
    )
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.SYMBOL_GRAPH, selection),
        title="Module and symbol membership graph",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, False),
        record_count=len(nodes) + len(edges),
        truncated=False,
        truncation_reason=None,
        limitations=tuple(sorted(limitations)),
        direction=EdgeDirection.DIRECTED,
        nodes=tuple(sorted(nodes.values(), key=lambda item: item.node_id)),
        edges=tuple(sorted(edges, key=lambda item: item.edge_id)),
        clusters=tuple(sorted(clusters, key=lambda item: item.cluster_id)),
    )
