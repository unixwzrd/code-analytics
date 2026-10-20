"""Deterministic graph grouping shared by saved-report and indexed views."""

from __future__ import annotations

import hashlib

from code_analysis.graph_ownership import GraphOwnership
from code_analysis.models import SourceLanguage
from code_analysis.projection_models import GraphEdge, GraphNode, ProjectionCollapse

COLLAPSE_LIMITATION = (
    "Grouped nodes and retained inter-owner edges carry merged evidence IDs; "
    "within-owner edges are not displayed. Grouping is a view, not a refactoring recommendation."
)


def collapse_graph(
    source_nodes: tuple[GraphNode, ...],
    source_edges: tuple[GraphEdge, ...],
    collapse: ProjectionCollapse,
    ownership: GraphOwnership,
) -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...], dict[str, str]]:
    if collapse is ProjectionCollapse.NONE:
        return source_nodes, source_edges, {node.node_id: node.node_id for node in source_nodes}
    node_keys: dict[str, str] = {}
    grouped: dict[str, list[GraphNode]] = {}
    module_languages = ownership.module_languages
    for node in source_nodes:
        module = ownership.module_for(node.node_id, node.group_id)
        if module is None:
            key = node.node_id
        else:
            separator = "." if module_languages[module] is SourceLanguage.PYTHON else "/"
            owner = (
                module if collapse is ProjectionCollapse.MODULE else module.split(separator, 1)[0]
            )
            key = f"collapsed-{collapse.value}:{owner}"
        node_keys[node.node_id] = key
        grouped.setdefault(key, []).append(node)
    nodes = tuple(
        GraphNode(
            node_id=key,
            label=(key.split(":", 1)[1] if key.startswith("collapsed-") else values[0].label),
            kind=f"collapsed-{collapse.value}" if len(values) > 1 else values[0].kind,
            evidence_ids=tuple(
                sorted({identity for value in values for identity in value.evidence_ids})
            ),
        )
        for key, values in sorted(grouped.items())
    )
    collapsed_edges: dict[tuple[str, str, str], GraphEdge] = {}
    for edge in source_edges:
        source = node_keys[edge.source_id]
        target = node_keys[edge.target_id]
        if source == target:
            continue
        key = (source, target, edge.kind)
        identity = "edge-" + hashlib.sha256("\0".join(key).encode()).hexdigest()[:20]
        previous = collapsed_edges.get(key)
        evidence_ids = set(edge.evidence_ids)
        if previous is not None:
            evidence_ids.update(previous.evidence_ids)
        collapsed_edges[key] = GraphEdge(
            edge_id=identity,
            source_id=source,
            target_id=target,
            kind=edge.kind,
            label=edge.label,
            confidence=edge.confidence,
            resolution=edge.resolution,
            evidence_ids=tuple(sorted(evidence_ids)),
        )
    return nodes, tuple(sorted(collapsed_edges.values(), key=lambda item: item.edge_id)), node_keys
