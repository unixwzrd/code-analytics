"""Pure deterministic graph retention policy."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GraphNodeRank:
    node_id: str
    selected: bool
    scc_member: bool
    fan_in: int
    fan_out: int


@dataclass(frozen=True, slots=True)
class GraphEdgeIdentity:
    edge_id: str
    source_id: str
    target_id: str


@dataclass(frozen=True, slots=True)
class GraphRetention:
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]


def _require_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{label} must be a non-empty string")
    return value


def _require_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be a bool")
    return value


def _require_bounded_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an int")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def _require_nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an int")
    if value < 0:
        raise ValueError(f"{label} must be nonnegative")
    return value


def retain_graph(
    nodes: tuple[GraphNodeRank, ...],
    edges: tuple[GraphEdgeIdentity, ...],
    *,
    max_records: int,
    max_nodes: int,
    max_edges: int,
) -> GraphRetention:
    """Retain deterministic node and edge identities within hard record bounds."""
    if not isinstance(nodes, tuple):
        raise TypeError("nodes must be a tuple")
    if not isinstance(edges, tuple):
        raise TypeError("edges must be a tuple")
    max_records = _require_bounded_int(max_records, "max_records", 1, 100_000)
    max_nodes = _require_bounded_int(max_nodes, "max_nodes", 1, 100_000)
    max_edges = _require_bounded_int(max_edges, "max_edges", 0, 250_000)

    node_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, GraphNodeRank):
            raise TypeError("nodes must contain GraphNodeRank values")
        node_id = _require_identity(node.node_id, "node_id")
        _require_bool(node.selected, "selected")
        _require_bool(node.scc_member, "scc_member")
        _require_nonnegative_int(node.fan_in, "fan_in")
        _require_nonnegative_int(node.fan_out, "fan_out")
        if node_id in node_ids:
            raise ValueError(f"duplicate node identity: {node_id}")
        node_ids.add(node_id)

    edge_ids: set[str] = set()
    for edge in edges:
        if not isinstance(edge, GraphEdgeIdentity):
            raise TypeError("edges must contain GraphEdgeIdentity values")
        edge_id = _require_identity(edge.edge_id, "edge_id")
        source_id = _require_identity(edge.source_id, "source_id")
        target_id = _require_identity(edge.target_id, "target_id")
        if edge_id in edge_ids:
            raise ValueError(f"duplicate edge identity: {edge_id}")
        edge_ids.add(edge_id)
        if source_id not in node_ids or target_id not in node_ids:
            raise ValueError(f"edge {edge_id} has a dangling endpoint")

    ranked_nodes = sorted(
        nodes,
        key=lambda node: (
            0 if node.selected else 1,
            0 if node.scc_member else 1,
            -node.fan_in,
            -node.fan_out,
            node.node_id,
        ),
    )
    if len(nodes) + len(edges) > max_records and edges:
        node_rank = {node.node_id: index for index, node in enumerate(ranked_nodes)}
        ranked_edges = sorted(
            edges,
            key=lambda edge: (
                min(node_rank[edge.source_id], node_rank[edge.target_id]),
                max(node_rank[edge.source_id], node_rank[edge.target_id]),
                edge.edge_id,
            ),
        )
        seed_edges = ranked_edges[: min(max_edges, max_records // 3)]
        required_ids = {
            endpoint for edge in seed_edges for endpoint in (edge.source_id, edge.target_id)
        } | {node.node_id for node in nodes if node.selected}
        node_limit = min(max_nodes, max_records - len(seed_edges))
        chosen_ids = set(sorted(required_ids, key=node_rank.__getitem__)[:node_limit])
        for node in ranked_nodes:
            if len(chosen_ids) >= node_limit:
                break
            chosen_ids.add(node.node_id)
    else:
        node_limit = min(max_nodes, max_records)
        chosen_ids = {node.node_id for node in ranked_nodes[:node_limit]}

    retained_node_ids = tuple(sorted(chosen_ids))
    retained_node_set = set(retained_node_ids)
    edge_limit = min(max_edges, max(0, max_records - len(retained_node_ids)))
    retained_edge_ids = tuple(
        sorted(
            edge.edge_id
            for edge in edges
            if edge.source_id in retained_node_set and edge.target_id in retained_node_set
        )[:edge_limit]
    )
    return GraphRetention(node_ids=retained_node_ids, edge_ids=retained_edge_ids)
