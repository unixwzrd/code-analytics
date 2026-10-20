from __future__ import annotations

import itertools

import pytest

from code_analysis.graph_selection import (
    GraphEdgeIdentity,
    GraphNodeRank,
    GraphRetention,
    retain_graph,
)


def _node(
    node_id: str,
    *,
    selected: bool = False,
    scc_member: bool = False,
    fan_in: int = 0,
    fan_out: int = 0,
) -> GraphNodeRank:
    return GraphNodeRank(node_id, selected, scc_member, fan_in, fan_out)


def _edge(edge_id: str, source: str, target: str) -> GraphEdgeIdentity:
    return GraphEdgeIdentity(edge_id, source, target)


def test_tiny_bounds_and_loops_obey_every_limit() -> None:
    nodes = (_node("a"), _node("b", selected=True))
    edges = (_edge("loop", "b", "b"), _edge("pair", "a", "b"))

    assert retain_graph(nodes, edges, max_records=1, max_nodes=2, max_edges=2) == GraphRetention(
        ("b",), ()
    )
    assert retain_graph(nodes, edges, max_records=3, max_nodes=1, max_edges=2) == GraphRetention(
        ("b",), ("loop",)
    )


def test_selected_then_scc_then_fan_counts_then_identity_rank() -> None:
    nodes = (
        _node("plain", fan_in=100),
        _node("scc", scc_member=True),
        _node("fan-out", scc_member=True, fan_out=2),
        _node("fan-in", scc_member=True, fan_in=1),
        _node("selected", selected=True),
    )

    retained = retain_graph(nodes, (), max_records=4, max_nodes=4, max_edges=0)

    assert retained.node_ids == ("fan-in", "fan-out", "scc", "selected")


def test_seeded_edges_union_selected_nodes_before_filling_ranked_nodes() -> None:
    nodes = (
        _node("a", fan_in=3),
        _node("b", fan_in=2),
        _node("c"),
        _node("d"),
        _node("selected", selected=True),
    )
    edges = (_edge("ab", "a", "b"), _edge("cd", "c", "d"))

    retained = retain_graph(nodes, edges, max_records=4, max_nodes=3, max_edges=1)

    assert retained == GraphRetention(("a", "b", "selected"), ("ab",))


def test_input_permutations_do_not_change_retention() -> None:
    nodes = (_node("a", fan_in=2), _node("b", scc_member=True), _node("c", selected=True))
    edges = (_edge("ab", "a", "b"), _edge("bc", "b", "c"))
    expected = retain_graph(nodes, edges, max_records=4, max_nodes=3, max_edges=2)

    for node_order in itertools.permutations(nodes):
        for edge_order in itertools.permutations(edges):
            assert (
                retain_graph(node_order, edge_order, max_records=4, max_nodes=3, max_edges=2)
                == expected
            )


@pytest.mark.parametrize(
    ("nodes", "edges", "match"),
    (
        ((_node("a"), _node("a")), (), "duplicate node"),
        ((_node("a"),), (_edge("x", "a", "a"), _edge("x", "a", "a")), "duplicate edge"),
        ((_node("a"),), (_edge("x", "a", "missing"),), "dangling endpoint"),
    ),
)
def test_rejects_duplicate_and_dangling_identities(nodes, edges, match) -> None:
    with pytest.raises(ValueError, match=match):
        retain_graph(nodes, edges, max_records=2, max_nodes=1, max_edges=1)


@pytest.mark.parametrize("label", ("max_records", "max_nodes", "max_edges"))
@pytest.mark.parametrize("value", (True, -1, 1.5))
def test_rejects_bool_negative_and_noninteger_bounds(label, value) -> None:
    bounds = {"max_records": 1, "max_nodes": 1, "max_edges": 1, label: value}
    error = ValueError if value == -1 else TypeError
    with pytest.raises(error, match=label):
        retain_graph((_node("a"),), (), **bounds)


@pytest.mark.parametrize("label", ("max_records", "max_nodes"))
def test_rejects_zero_positive_bounds(label) -> None:
    bounds = {"max_records": 1, "max_nodes": 1, "max_edges": 0, label: 0}
    with pytest.raises(ValueError, match=label):
        retain_graph((_node("a"),), (), **bounds)


def test_validates_dataclass_fields_at_function_boundary() -> None:
    with pytest.raises(TypeError, match="selected"):
        retain_graph((_node("a", selected=1),), (), max_records=1, max_nodes=1, max_edges=0)
    with pytest.raises(ValueError, match="fan_in"):
        retain_graph((_node("a", fan_in=-1),), (), max_records=1, max_nodes=1, max_edges=0)
