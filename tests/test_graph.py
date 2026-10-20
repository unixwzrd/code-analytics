from __future__ import annotations

import cProfile
import random
from collections.abc import Iterable
from itertools import pairwise, permutations
from types import CodeType

from code_analysis.graph import canonical_cycles, strongly_connected_components


def test_scc_and_cycle_canonicalization_deduplicate_rotations() -> None:
    nodes = ("a", "b", "c")
    edges = (("a", "b"), ("b", "c"), ("c", "a"), ("a", "b"))
    assert strongly_connected_components(nodes, edges) == (("a", "b", "c"),)
    assert canonical_cycles(nodes, edges) == (("a", "b", "c", "a"),)


def test_directed_reverse_cycle_is_distinct() -> None:
    nodes = ("a", "b", "c")
    edges = (
        ("a", "b"),
        ("b", "c"),
        ("c", "a"),
        ("a", "c"),
        ("c", "b"),
        ("b", "a"),
    )
    cycles = canonical_cycles(nodes, edges)
    assert ("a", "b", "c", "a") in cycles
    assert ("a", "c", "b", "a") in cycles
    assert len(cycles) == 5


def test_self_cycle_is_reported_once() -> None:
    assert canonical_cycles(("a",), (("a", "a"), ("a", "a"))) == (("a", "a"),)


def _reference_canonical_cycles(
    nodes: Iterable[str], edges: Iterable[tuple[str, str]]
) -> tuple[tuple[str, ...], ...]:
    members = tuple(sorted(set(nodes)))
    edge_set = set(edges)
    cycles: set[tuple[str, ...]] = set()
    for length in range(1, len(members) + 1):
        for body in permutations(members, length):
            if body[0] != min(body):
                continue
            closed = (*body, body[0])
            if all(edge in edge_set for edge in pairwise(closed)):
                cycles.add(closed)
    return tuple(sorted(cycles))


def _with_graph_visit_count(
    nodes: Iterable[str], edges: Iterable[tuple[str, str]]
) -> tuple[tuple[tuple[str, ...], ...], int]:
    profiler = cProfile.Profile()
    result = profiler.runcall(canonical_cycles, nodes, edges)
    visits = sum(
        entry.callcount
        for entry in profiler.getstats()
        if isinstance(entry.code, CodeType)
        and entry.code.co_filename == canonical_cycles.__code__.co_filename
        and entry.code.co_name == "visit"
    )
    return result, visits


THREE_NODES = ("a", "b", "c")
THREE_NODE_EDGES = tuple((source, target) for source in THREE_NODES for target in THREE_NODES)


def test_all_three_node_directed_graphs_match_reference_oracle() -> None:
    for edge_mask in range(1 << len(THREE_NODE_EDGES)):
        edges = tuple(
            THREE_NODE_EDGES[index]
            for index in range(len(THREE_NODE_EDGES))
            if edge_mask & (1 << index)
        )
        assert canonical_cycles(THREE_NODES, edges) == _reference_canonical_cycles(
            THREE_NODES, edges
        )


def test_seeded_four_and_five_node_graphs_match_reference_oracle() -> None:
    rng = random.Random(20260922)
    for node_count in (4, 5):
        nodes = tuple(f"n{index}" for index in range(node_count))
        all_edges = tuple((source, target) for source in nodes for target in nodes)
        for _ in range(48):
            edges = tuple(edge for edge in all_edges if rng.random() < 0.35)
            assert canonical_cycles(nodes, edges) == _reference_canonical_cycles(nodes, edges)


def _complete_dag_nodes(count: int) -> tuple[str, ...]:
    return tuple(f"n{index:02}" for index in range(count))


def _complete_dag_edges(nodes: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple((nodes[i], nodes[j]) for i in range(len(nodes)) for j in range(i + 1, len(nodes)))


def test_complete_dag18_has_no_cycles_and_only_scc_visits() -> None:
    nodes = _complete_dag_nodes(18)
    edges = _complete_dag_edges(nodes)
    cycles, visit_count = _with_graph_visit_count(nodes, edges)
    assert cycles == ()
    assert visit_count == len(nodes)


def test_intercomponent_edges_do_not_create_cross_scc_cycles() -> None:
    nodes = ("a", "b", "c", "d", "e")
    edges = (
        ("a", "b"),
        ("b", "a"),
        ("c", "d"),
        ("d", "c"),
        ("b", "c"),
        ("a", "e"),
        ("e", "d"),
    )
    cycles = canonical_cycles(nodes, edges)
    assert cycles == (("a", "b", "a"), ("c", "d", "c"))


def test_acyclic_tail_off_cycle_component_adds_no_cycles() -> None:
    nodes = ("a", "b", "c", "d")
    edges = (("a", "b"), ("b", "a"), ("b", "c"), ("c", "d"))
    assert canonical_cycles(nodes, edges) == (("a", "b", "a"),)


def test_unknown_endpoints_are_ignored() -> None:
    nodes = ("a", "b")
    edges = (("a", "x"), ("x", "b"), ("a", "b"), ("b", "a"))
    assert canonical_cycles(nodes, edges) == (("a", "b", "a"),)


def test_one_shot_iterable_inputs() -> None:
    nodes = iter(["a", "b"])
    edges = iter([("a", "b"), ("b", "a")])
    assert canonical_cycles(nodes, edges) == (("a", "b", "a"),)


def test_duplicate_edges_and_sorted_output() -> None:
    nodes = ("x", "y")
    edges = (("x", "y"), ("y", "x"), ("x", "y"))
    assert canonical_cycles(nodes, edges) == (("x", "y", "x"),)


def test_cyclic_scc_with_intercomponent_generator_cases() -> None:
    cases = (
        (
            ("p", "q", "r", "s"),
            (("p", "q"), ("q", "p"), ("q", "r"), ("r", "s"), ("s", "r")),
            (("p", "q", "p"), ("r", "s", "r")),
        ),
        (
            ("m", "n", "o"),
            (("m", "n"), ("n", "m"), ("n", "o")),
            (("m", "n", "m"),),
        ),
    )
    for nodes, edges, expected in cases:
        assert canonical_cycles(nodes, edges) == expected


def test_bidirected_triangle_has_bounded_small_fixture_visit_count() -> None:
    nodes = THREE_NODES
    edges = (
        ("a", "b"),
        ("b", "c"),
        ("c", "a"),
        ("a", "c"),
        ("c", "b"),
        ("b", "a"),
    )
    _, visit_count = _with_graph_visit_count(nodes, edges)
    assert visit_count < 50
