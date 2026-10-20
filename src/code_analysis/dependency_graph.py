"""Shared bounded graph projection of saved dependency traversal evidence."""

from __future__ import annotations

import hashlib

from pydantic import Field, StrictInt

from code_analysis.dependency_navigation import (
    DependencyRequest,
    ModulePageReader,
    investigate_dependencies,
)
from code_analysis.models import Completeness, StrictModel
from code_analysis.projection_models import (
    GraphEdge,
    GraphNode,
    GraphProjection,
    ProjectionInput,
    ProjectionSelection,
)


class DependencyGraphRequest(StrictModel):
    investigation: DependencyRequest
    max_nodes: StrictInt = Field(default=100, ge=1, le=100)
    max_edges: StrictInt = Field(default=250, ge=0, le=250)


def build_dependency_graph(
    reader: ModulePageReader, request: DependencyGraphRequest, source: ProjectionInput
) -> GraphProjection:
    result = investigate_dependencies(reader, request.investigation)
    ordered = result.path or tuple(
        sorted(result.members, key=lambda item: (item != request.investigation.source, item))
    )
    retained = set(ordered[: request.max_nodes])
    selected_edges = tuple(
        edge for edge in result.edges if edge.source_id in retained and edge.target_id in retained
    )[: request.max_edges]
    edges = tuple(
        sorted(
            (
                GraphEdge(
                    edge_id="dependency-"
                    + hashlib.sha256(edge.model_dump_json().encode()).hexdigest()[:20],
                    source_id=edge.source_id,
                    target_id=edge.target_id,
                    kind=edge.kind,
                    label=edge.kind,
                )
                for edge in selected_edges
            ),
            key=lambda edge: edge.edge_id,
        )
    )
    omitted_nodes = len(ordered) - len(retained)
    omitted_edges = len(result.edges) - len(edges)
    truncated = result.truncated or bool(omitted_nodes or omitted_edges)
    limitations = set(result.limitations)
    limitations.add(
        "Graph counts describe the proven selected traversal result, not the whole repository; "
        "undiscovered paths and component members cannot be counted."
    )
    limitations.add(
        "Path nodes retain path order; component nodes retain the selected source then module ID. "
        "Edges retain source, target and import-kind order before display sorting."
    )
    limitations.add("Traversal selection: " + request.investigation.model_dump_json())
    identity = hashlib.sha256(
        (source.model_dump_json() + request.model_dump_json()).encode()
    ).hexdigest()
    return GraphProjection(
        projection_id=f"projection-{identity[:20]}",
        title=(
            "Saved dependency path"
            if request.investigation.mode == "path"
            else "Saved mutual dependency component"
        ),
        inputs=(source,),
        selection=ProjectionSelection(
            selected_ids=tuple(
                sorted(
                    {request.investigation.source}
                    | ({request.investigation.target} if request.investigation.target else set())
                )
            ),
            max_nodes=request.max_nodes,
            max_edges=request.max_edges,
        ),
        completeness=(
            Completeness.PARTIAL
            if truncated and result.source_completeness is Completeness.COMPLETE
            else result.source_completeness
        ),
        truncated=truncated,
        truncation_reason="dependency traversal or graph display bound" if truncated else None,
        limitations=tuple(sorted(limitations)),
        nodes=tuple(
            GraphNode(node_id=identity, label=identity, kind="module")
            for identity in sorted(retained)
        ),
        edges=edges,
        record_count=len(retained) + len(edges),
        original_node_count=len(ordered),
        original_edge_count=len(result.edges),
        omitted_node_count=omitted_nodes,
        omitted_edge_count=omitted_edges,
    )
