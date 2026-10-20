"""Public registry façade for bounded deterministic projections."""

from __future__ import annotations

from code_analysis.graph_collapse import COLLAPSE_LIMITATION, collapse_graph
from code_analysis.graph_ownership import graph_ownership
from code_analysis.graph_selection import GraphEdgeIdentity, GraphNodeRank, retain_graph
from code_analysis.models import Completeness, SourceLanguage
from code_analysis.projection_models import (
    GraphNode,
    GraphProjection,
    Projection,
    ProjectionCollapse,
    ProjectionFindingCategory,
    ProjectionFindingSeverity,
    ProjectionSelection,
)
from code_analysis.projections._catalog import (
    ProjectionBuildError,
    ProjectionName,
    SelectionPolicy,
)
from code_analysis.projections._registry import projection_spec
from code_analysis.report_models import AnalysisReport, RefactorCandidateKind

__all__ = ("ProjectionBuildError", "ProjectionName", "build_projection")


def build_projection(
    report: AnalysisReport,
    name: ProjectionName,
    *,
    max_records: int = 500,
    max_nodes: int = 100,
    max_edges: int = 250,
    collapse: ProjectionCollapse = ProjectionCollapse.NONE,
    selected_ids: tuple[str, ...] = (),
    edge_kinds: tuple[str, ...] = (),
    depth: int | None = None,
    module_prefixes: tuple[str, ...] = (),
    finding_categories: tuple[ProjectionFindingCategory, ...] = (),
    finding_severities: tuple[ProjectionFindingSeverity, ...] = (),
    finding_rule_ids: tuple[str, ...] = (),
    languages: tuple[SourceLanguage, ...] = (),
    candidate_module_patterns: tuple[str, ...] = (),
    candidate_exclude_patterns: tuple[str, ...] = (),
    candidate_kinds: tuple[RefactorCandidateKind, ...] = (),
    candidate_states: tuple[str, ...] = (),
    candidate_decisions: tuple[str, ...] = (),
    cross_module_only: bool = False,
    production_only: bool = False,
    min_shared_dimensions: int = 0,
    prefer_existing_owner: bool = False,
) -> Projection:
    """Build a registered projection using its explicit selection policy."""
    spec = projection_spec(name)
    selection = _selection(
        spec.selection_policy,
        max_records=max_records,
        max_nodes=max_nodes,
        max_edges=max_edges,
        collapse=collapse,
        selected_ids=tuple(sorted(set(selected_ids))),
        edge_kinds=tuple(sorted(set(edge_kinds))),
        depth=depth,
        module_prefixes=tuple(sorted(set(module_prefixes))),
        finding_categories=tuple(sorted(set(finding_categories), key=lambda item: item.value)),
        finding_severities=tuple(sorted(set(finding_severities), key=lambda item: item.value)),
        finding_rule_ids=tuple(sorted(set(finding_rule_ids))),
        languages=tuple(sorted(set(languages), key=lambda item: item.value)),
        candidate_module_patterns=tuple(sorted(set(candidate_module_patterns))),
        candidate_exclude_patterns=tuple(sorted(set(candidate_exclude_patterns))),
        candidate_kinds=tuple(sorted(set(candidate_kinds), key=lambda item: item.value)),
        candidate_states=tuple(sorted(set(candidate_states))),
        candidate_decisions=tuple(sorted(set(candidate_decisions))),
        cross_module_only=cross_module_only,
        production_only=production_only,
        min_shared_dimensions=min_shared_dimensions,
        prefer_existing_owner=prefer_existing_owner,
    )
    builder_selection = (
        selection.model_copy(update={"max_records": 100_000})
        if spec.selection_policy
        in {
            SelectionPolicy.GRAPH,
            SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
            SelectionPolicy.DEPENDENCY_NEIGHBORHOOD,
        }
        else selection
    )
    projection = spec.builder(report, builder_selection)
    if isinstance(projection, GraphProjection):
        return _finalize_graph(report, projection, selection, name)
    return projection


def _finalize_graph(
    report: AnalysisReport,
    projection: GraphProjection,
    selection: ProjectionSelection,
    name: ProjectionName,
) -> GraphProjection:
    from code_analysis.projections._shared import _projection_id

    nodes, edges = projection.nodes, projection.edges
    if selection.selected_ids:
        selected = set(selection.selected_ids)
        retained_ids = {
            endpoint
            for edge in edges
            if edge.source_id in selected
            or edge.target_id in selected
            or edge.label in selected
            or edge.kind in selected
            for endpoint in (edge.source_id, edge.target_id)
        } | ({item.node_id for item in nodes} & selected)
        if not retained_ids:
            raise ProjectionBuildError("selected identities are absent from this graph")
        nodes = tuple(item for item in nodes if item.node_id in retained_ids)
        edges = tuple(
            item
            for item in edges
            if item.source_id in retained_ids and item.target_id in retained_ids
        )
    ownership = graph_ownership(report)
    nodes, edges, collapsed_keys = collapse_graph(nodes, edges, selection.collapse, ownership)
    modules = {item.name: item for item in report.modules}
    scc_modules = {
        name
        for component in report.strongly_connected_components
        if len(component.modules) > 1
        for name in component.modules
    }
    selected_ids = {collapsed_keys.get(identity, identity) for identity in selection.selected_ids}

    def rank_context(node: GraphNode) -> GraphNodeRank:
        module_name = ownership.module_for(node.node_id, node.group_id)
        module = modules.get(module_name or "")
        return GraphNodeRank(
            node_id=node.node_id,
            selected=node.node_id in selected_ids,
            scc_member=module_name in scc_modules,
            fan_in=module.fan_in if module is not None else 0,
            fan_out=module.fan_out if module is not None else 0,
        )

    original_node_count = len(nodes)
    original_edge_count = len(edges)
    retention = retain_graph(
        tuple(rank_context(node) for node in nodes),
        tuple(
            GraphEdgeIdentity(
                edge_id=edge.edge_id,
                source_id=edge.source_id,
                target_id=edge.target_id,
            )
            for edge in edges
        ),
        max_records=selection.max_records,
        max_nodes=selection.max_nodes,
        max_edges=selection.max_edges,
    )
    retained_node_ids = set(retention.node_ids)
    retained_edge_ids = set(retention.edge_ids)
    retained_nodes = tuple(item for item in nodes if item.node_id in retained_node_ids)
    retained_nodes = tuple(sorted(retained_nodes, key=lambda item: item.node_id))
    retained_ids = {item.node_id for item in retained_nodes}
    retained_edges = tuple(item for item in edges if item.edge_id in retained_edge_ids)
    retained_edges = tuple(sorted(retained_edges, key=lambda item: item.edge_id))
    omitted_nodes = original_node_count - len(retained_nodes)
    omitted_edges = original_edge_count - len(retained_edges)
    bounded_here = bool(omitted_nodes or omitted_edges)
    truncated = projection.truncated or bounded_here
    truncation_reasons = {projection.truncation_reason} if projection.truncation_reason else set()
    if bounded_here:
        truncation_reasons.add("graph node or edge limit reached")
    limitations = set(projection.limitations)
    if selection.collapse is not ProjectionCollapse.NONE:
        limitations.add(COLLAPSE_LIMITATION)
    if bounded_here:
        limitations.add("Configured graph node or edge bounds omitted records.")
    retained_clusters = tuple(
        item.model_copy(update={"node_ids": tuple(sorted(set(item.node_ids) & retained_ids))})
        for item in projection.clusters
        if set(item.node_ids) & retained_ids
    )
    retained_cluster_ids = {item.cluster_id for item in retained_clusters}
    retained_nodes = tuple(
        item.model_copy(
            update={"group_id": (item.group_id if item.group_id in retained_cluster_ids else None)}
        )
        for item in retained_nodes
    )
    return GraphProjection.model_validate(
        {
            **projection.model_dump(),
            "projection_id": _projection_id(projection.inputs[0], name, selection),
            "selection": selection,
            "completeness": (
                Completeness.PARTIAL
                if truncated and projection.completeness is Completeness.COMPLETE
                else projection.completeness
            ),
            "record_count": len(retained_nodes) + len(retained_edges),
            "truncated": truncated,
            "truncation_reason": "; ".join(sorted(truncation_reasons)) if truncated else None,
            "limitations": tuple(sorted(limitations)),
            "nodes": retained_nodes,
            "edges": retained_edges,
            "clusters": retained_clusters,
            "original_node_count": original_node_count,
            "original_edge_count": original_edge_count,
            "omitted_node_count": omitted_nodes,
            "omitted_edge_count": omitted_edges,
        }
    )


def _selection(
    policy: SelectionPolicy,
    *,
    max_records: int,
    max_nodes: int,
    max_edges: int,
    collapse: ProjectionCollapse,
    selected_ids: tuple[str, ...],
    edge_kinds: tuple[str, ...],
    depth: int | None,
    module_prefixes: tuple[str, ...],
    finding_categories: tuple[ProjectionFindingCategory, ...],
    finding_severities: tuple[ProjectionFindingSeverity, ...],
    finding_rule_ids: tuple[str, ...],
    languages: tuple[SourceLanguage, ...],
    candidate_module_patterns: tuple[str, ...],
    candidate_exclude_patterns: tuple[str, ...],
    candidate_kinds: tuple[RefactorCandidateKind, ...],
    candidate_states: tuple[str, ...],
    candidate_decisions: tuple[str, ...],
    cross_module_only: bool,
    production_only: bool,
    min_shared_dimensions: int,
    prefer_existing_owner: bool,
) -> ProjectionSelection:
    finding_filters = bool(
        module_prefixes or finding_categories or finding_severities or finding_rule_ids
    )
    candidate_filters = bool(
        candidate_module_patterns
        or candidate_exclude_patterns
        or candidate_kinds
        or candidate_states
        or candidate_decisions
        or cross_module_only
        or production_only
        or min_shared_dimensions
        or prefer_existing_owner
    )
    if candidate_filters and policy is not SelectionPolicy.CANDIDATE_WORKLIST:
        raise ProjectionBuildError("candidate filters require the candidate-worklist projection")
    if policy is SelectionPolicy.SUBJECT_FINDINGS:
        if edge_kinds or depth is not None:
            raise ProjectionBuildError(
                "edge-kind and depth options require dependency-neighborhood"
            )
        if collapse is not ProjectionCollapse.NONE:
            raise ProjectionBuildError("collapse requires a graph projection")
        if selected_ids and module_prefixes:
            raise ProjectionBuildError(
                "exact subject selection cannot be combined with module-prefix selection"
            )
        return ProjectionSelection(
            selected_ids=selected_ids,
            module_prefixes=module_prefixes,
            finding_categories=finding_categories,
            finding_severities=finding_severities,
            finding_rule_ids=finding_rule_ids,
            max_records=max_records,
            max_nodes=max_nodes,
            max_edges=max_edges,
            collapse=collapse,
            languages=languages,
        )
    if policy is SelectionPolicy.CANDIDATE_WORKLIST:
        if selected_ids or edge_kinds or depth is not None or finding_filters:
            raise ProjectionBuildError("candidate worklist uses its dedicated candidate filters")
        if collapse is not ProjectionCollapse.NONE:
            raise ProjectionBuildError("collapse requires a graph projection")
        return ProjectionSelection(
            candidate_module_patterns=candidate_module_patterns,
            candidate_exclude_patterns=candidate_exclude_patterns,
            candidate_kinds=candidate_kinds,
            candidate_states=candidate_states,
            candidate_decisions=candidate_decisions,
            cross_module_only=cross_module_only,
            production_only=production_only,
            min_shared_dimensions=min_shared_dimensions,
            prefer_existing_owner=prefer_existing_owner,
            max_records=max_records,
            max_nodes=max_nodes,
            max_edges=max_edges,
            languages=languages,
        )
    if finding_filters:
        raise ProjectionBuildError("finding filters require the subject-findings projection")
    if policy is SelectionPolicy.DEPENDENCY_NEIGHBORHOOD:
        if collapse is not ProjectionCollapse.NONE:
            raise ProjectionBuildError("dependency-neighborhood does not support collapse")
        return _dependency_selection(
            max_records, max_nodes, max_edges, selected_ids, edge_kinds, depth
        ).model_copy(update={"languages": languages})
    if policy is SelectionPolicy.BOUNDED_REVIEW:
        if edge_kinds or depth is not None:
            raise ProjectionBuildError(
                "edge-kind and depth options require dependency-neighborhood"
            )
        if collapse is not ProjectionCollapse.NONE:
            raise ProjectionBuildError("collapse requires a graph projection")
        return ProjectionSelection(
            selected_ids=selected_ids,
            max_records=max_records,
            max_nodes=max_nodes,
            max_edges=max_edges,
            collapse=collapse,
            languages=languages,
        )
    if policy is SelectionPolicy.IDENTITY_FOCUSED_GRAPH:
        if edge_kinds or depth is not None:
            raise ProjectionBuildError(
                "edge-kind and depth options require dependency-neighborhood"
            )
        return ProjectionSelection(
            selected_ids=selected_ids,
            max_records=max_records,
            max_nodes=max_nodes,
            max_edges=max_edges,
            collapse=collapse,
            languages=languages,
        )
    if selected_ids or edge_kinds or depth is not None:
        raise ProjectionBuildError(
            "selection, edge-kind, and depth options require dependency-neighborhood"
        )
    if policy not in {SelectionPolicy.GRAPH} and collapse is not ProjectionCollapse.NONE:
        raise ProjectionBuildError("collapse requires a graph projection")
    return ProjectionSelection(
        max_records=max_records,
        max_nodes=max_nodes,
        max_edges=max_edges,
        collapse=collapse,
        languages=languages,
    )


def _dependency_selection(
    max_records: int,
    max_nodes: int,
    max_edges: int,
    selected_ids: tuple[str, ...],
    edge_kinds: tuple[str, ...],
    depth: int | None,
) -> ProjectionSelection:
    if len(selected_ids) != 1:
        raise ProjectionBuildError("dependency-neighborhood requires exactly one selected module")
    unknown = set(edge_kinds) - {
        "runtime",
        "type_checking",
        "optional",
        "lazy",
        "esm-static",
        "esm-dynamic",
        "commonjs",
        "dynamic-unresolved",
    }
    if unknown:
        raise ProjectionBuildError("unknown dependency edge kind: " + ", ".join(sorted(unknown)))
    return ProjectionSelection(
        selected_ids=selected_ids,
        edge_kinds=edge_kinds,
        max_records=max_records,
        max_nodes=max_nodes,
        max_edges=max_edges,
        depth=1 if depth is None else depth,
    )
