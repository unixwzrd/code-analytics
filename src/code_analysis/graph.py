from __future__ import annotations

from collections.abc import Callable, Iterable


def strongly_connected_components(
    nodes: Iterable[str], edges: Iterable[tuple[str, str]]
) -> tuple[tuple[str, ...], ...]:
    graph = {node: set() for node in nodes}
    for source, target in edges:
        if source in graph and target in graph:
            graph[source].add(target)
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for neighbor in sorted(graph[node]):
            if neighbor not in indices:
                visit(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            components.append(tuple(sorted(component)))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return tuple(sorted(components))


def _collect_cycles_in_component(
    component: tuple[str, ...],
    adjacency: dict[str, tuple[str, ...]],
    cycles: set[tuple[str, ...]],
    canonical: Callable[[list[str]], tuple[str, ...]],
) -> None:
    def cycles_from(start: str) -> None:
        def visit(node: str, path: list[str], active: set[str]) -> None:
            for neighbor in adjacency[node]:
                if neighbor == start:
                    cycles.add(canonical([*path, start]))
                elif neighbor not in active and neighbor >= start:
                    visit(neighbor, [*path, neighbor], {*active, neighbor})

        visit(start, [start], {start})

    for start in component:
        cycles_from(start)


def canonical_cycles(
    nodes: Iterable[str], edges: Iterable[tuple[str, str]]
) -> tuple[tuple[str, ...], ...]:
    node_list = list(nodes)
    edge_list = list(edges)
    graph = {node: set() for node in node_list}
    for source, target in edge_list:
        if source in graph and target in graph:
            graph[source].add(target)
    components = strongly_connected_components(node_list, edge_list)
    cycles: set[tuple[str, ...]] = set()

    def canonical(path: list[str]) -> tuple[str, ...]:
        body = path[:-1]
        rotations = [tuple(body[index:] + body[:index]) for index in range(len(body))]
        best = min(rotations)
        return (*best, best[0])

    for component in components:
        if len(component) == 1:
            only = component[0]
            if only not in graph[only]:
                continue
        component_set = set(component)
        adjacency = {
            member: tuple(
                sorted(neighbor for neighbor in graph[member] if neighbor in component_set)
            )
            for member in component
        }
        _collect_cycles_in_component(component, adjacency, cycles, canonical)

    return tuple(sorted(cycles))
