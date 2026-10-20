"""Bounded, source-free dependency exploration over validated adjacency pages."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal, Protocol, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.models import Completeness, StrictModel
from code_analysis.module_navigation import ModuleNavigationPage, ModuleView
from code_analysis.ui_models import UIGraphEdge


class ModulePageReader(Protocol):
    def module_navigation(
        self, module: str, view: ModuleView = "outgoing", offset: int = 0
    ) -> ModuleNavigationPage: ...


class DependencyRequest(StrictModel):
    source: StrictStr = Field(min_length=1, max_length=4096)
    target: StrictStr | None = Field(default=None, min_length=1, max_length=4096)
    mode: Literal["path", "component"] = "path"
    max_nodes: StrictInt = Field(default=500, ge=1, le=2000)
    max_edges: StrictInt = Field(default=5000, ge=1, le=20000)

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if (self.mode == "path") != (self.target is not None):
            raise ValueError("path requires a target; component does not accept a target")
        return self


class DependencyResult(StrictModel):
    request: DependencyRequest
    source_completeness: Completeness
    truncated: StrictBool
    visited_nodes: StrictInt = Field(ge=1, le=2000)
    examined_edges: StrictInt = Field(ge=0, le=20000)
    path: tuple[StrictStr, ...] = ()
    members: tuple[StrictStr, ...] = ()
    edges: tuple[UIGraphEdge, ...] = Field(default=(), max_length=20000)
    limitations: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if (
            self.visited_nodes > self.request.max_nodes
            or self.examined_edges > self.request.max_edges
        ):
            raise ValueError("dependency result exceeds requested bounds")
        if self.request.mode == "path":
            if self.members or (
                self.path
                and (self.path[0] != self.request.source or self.path[-1] != self.request.target)
            ):
                raise ValueError("dependency path identity mismatch")
        elif self.path or self.request.source not in self.members:
            raise ValueError("component identity mismatch")
        if (
            len(set(self.path)) != len(self.path)
            or tuple(sorted(set(self.members))) != self.members
        ):
            raise ValueError("dependency identities must be unique and component members sorted")
        if len(self.path) > self.visited_nodes or len(self.members) > self.visited_nodes:
            raise ValueError("result identities exceed visited nodes")
        keys = [(edge.source_id, edge.target_id, edge.kind) for edge in self.edges]
        if keys != sorted(set(keys)) or len(keys) > self.examined_edges:
            raise ValueError("dependency edges must be unique, sorted and examined")
        selected = set(self.path or self.members)
        if any(
            edge.source_id not in selected or edge.target_id not in selected for edge in self.edges
        ):
            raise ValueError("dependency edge endpoint is outside selected result")
        return self


@dataclass(slots=True)
class _Budget:
    request: DependencyRequest
    nodes: set[str] = field(default_factory=set)
    edges: int = 0
    truncated: bool = False
    observed: dict[tuple[str, str, str], UIGraphEdge] = field(default_factory=dict)


def _walk(
    reader: ModulePageReader, budget: _Budget, view: ModuleView, completeness: Completeness
) -> dict[str, str | None]:
    source = budget.request.source
    parents: dict[str, str | None] = {source: None}
    pending = deque([source])
    while pending:
        current = pending.popleft()
        offset = 0
        while True:
            page = reader.module_navigation(current, view, offset)
            if page.source_completeness is not completeness:
                raise ValueError("dependency source completeness changed")
            for edge in page.edges:
                if budget.edges == budget.request.max_edges:
                    budget.truncated = True
                    return parents
                budget.edges += 1
                budget.observed[(edge.source_id, edge.target_id, edge.kind)] = edge
                neighbor = edge.target_id if view == "outgoing" else edge.source_id
                if neighbor in parents:
                    continue
                if neighbor not in budget.nodes and len(budget.nodes) == budget.request.max_nodes:
                    budget.truncated = True
                    return parents
                budget.nodes.add(neighbor)
                parents[neighbor] = current
                if budget.request.mode == "path" and neighbor == budget.request.target:
                    return parents
                pending.append(neighbor)
            if not page.truncated:
                break
            offset += page.limit
    return parents


def investigate_dependencies(
    reader: ModulePageReader, request: DependencyRequest
) -> DependencyResult:
    """BFS path or intersected reachability; never infer absent external/dynamic edges."""
    first = reader.module_navigation(request.source)
    completeness = first.source_completeness
    if request.target is not None:
        target = reader.module_navigation(request.target)
        if target.source_completeness is not completeness:
            raise ValueError("dependency source completeness mismatch")
    budget = _Budget(request, {request.source})
    path: tuple[str, ...] = ()
    members: tuple[str, ...] = ()
    if request.mode == "path" and request.source == request.target:
        path = (request.source,)
    else:
        outgoing = _walk(reader, budget, "outgoing", completeness)
        if request.mode == "component":
            incoming = _walk(reader, budget, "incoming", completeness)
            members = tuple(sorted(outgoing.keys() & incoming.keys()))
        elif request.target in outgoing:
            reverse: list[str] = []
            cursor: str | None = request.target
            while cursor is not None:
                reverse.append(cursor)
                cursor = outgoing[cursor]
            path = tuple(reversed(reverse))
    limitations: tuple[str, ...] = (
        "Known local saved import edges only; external and unresolved imports are excluded.",
        "A path proves saved dependency reachability, not runtime execution.",
    )
    if completeness is not Completeness.COMPLETE:
        limitations += (
            "Source evidence is incomplete: absent paths are unresolved and "
            "component membership may be incomplete even when traversal finishes.",
        )
    if budget.truncated:
        limitations += (
            "Traversal bound reached: an absent path is unresolved; "
            "component members are a proven subset.",
        )
    selected = set(path or members)
    steps = set(pairwise(path))
    edges = tuple(
        edge
        for key, edge in sorted(budget.observed.items())
        if edge.source_id in selected
        and edge.target_id in selected
        and (request.mode == "component" or key[:2] in steps)
    )
    return DependencyResult(
        request=request,
        source_completeness=completeness,
        truncated=budget.truncated,
        visited_nodes=len(budget.nodes),
        examined_edges=budget.edges,
        path=path,
        members=members,
        edges=edges,
        limitations=limitations,
    )
