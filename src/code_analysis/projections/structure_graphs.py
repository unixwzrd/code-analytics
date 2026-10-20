"""Saved-report-only data-structure lifecycle and conversion graphs."""

from __future__ import annotations

from collections.abc import Callable
from typing import assert_never

from code_analysis.models import Evidence
from code_analysis.projection_models import (
    EdgeDirection,
    EdgeResolution,
    GraphEdge,
    GraphNode,
    GraphProjection,
    ProjectionSelection,
)
from code_analysis.projections._catalog import ProjectionBuildError, ProjectionName
from code_analysis.projections._shared import (
    _edge_id,
    _evidence_identity,
    _limitations,
    _projection_id,
    _projection_state,
    _report_input,
)
from code_analysis.report_models import (
    AnalysisReport,
    AnalysisSubject,
    DataStructureRecord,
    DictionaryShape,
    ResolutionState,
)

Report = AnalysisReport
SubjectIds = Callable[[DataStructureRecord], tuple[str, ...]]

_ROLE_RELATIONS: tuple[tuple[str, str, SubjectIds], ...] = (
    ("owner", "subject-to-structure", lambda item: (item.owner_subject_id,)),
    ("constructor", "subject-to-structure", lambda item: item.constructor_subject_ids),
    ("producer", "subject-to-structure", lambda item: item.producer_subject_ids),
    ("reader", "structure-to-subject", lambda item: item.reader_subject_ids),
    ("writer", "subject-to-structure", lambda item: item.writer_subject_ids),
    ("mutator", "subject-to-structure", lambda item: item.mutator_subject_ids),
    ("serializer", "structure-to-subject", lambda item: item.serializer_subject_ids),
    ("alias", "structure-to-subject", lambda item: item.alias_subject_ids),
)


def _evidence_ids(values: tuple[Evidence, ...]) -> tuple[str, ...]:
    return tuple(sorted({_evidence_identity(value) for value in values}))


def _shapes_by_owner(report: Report) -> dict[str, tuple[DictionaryShape, ...]]:
    indexed: dict[str, list[DictionaryShape]] = {}
    for shape in report.dictionary_shapes:
        if shape.lifecycle_states:
            indexed.setdefault(shape.owner_subject_id, []).append(shape)
    return {
        owner_id: tuple(sorted(shapes, key=lambda item: item.shape_id))
        for owner_id, shapes in indexed.items()
    }


def _add_role_evidence(
    structure: DataStructureRecord,
    subjects: dict[str, AnalysisSubject],
    nodes: dict[str, GraphNode],
    edges: dict[str, GraphEdge],
) -> None:
    evidence_ids = _evidence_ids(structure.evidence)
    owner = subjects[structure.owner_subject_id]
    nodes[structure.structure_id] = GraphNode(
        node_id=structure.structure_id,
        label=owner.logical_name,
        kind="data-structure",
        evidence_ids=evidence_ids,
    )
    for role, direction, accessor in _ROLE_RELATIONS:
        for subject_id in accessor(structure):
            subject = subjects[subject_id]
            nodes.setdefault(
                subject_id,
                GraphNode(
                    node_id=subject_id,
                    label=subject.logical_name,
                    kind="analysis-subject",
                    evidence_ids=_evidence_ids(subject.evidence),
                ),
            )
            source_id, target_id = (
                (subject_id, structure.structure_id)
                if direction == "subject-to-structure"
                else (structure.structure_id, subject_id)
            )
            identity = _edge_id(source_id, target_id, role)
            edges[identity] = GraphEdge(
                edge_id=identity,
                source_id=source_id,
                target_id=target_id,
                kind=role,
                label=role,
                confidence=structure.confidence,
                resolution=EdgeResolution.RESOLVED,
                evidence_ids=evidence_ids,
            )


def _add_shape_state_evidence(
    structure: DataStructureRecord,
    owner: AnalysisSubject,
    shapes: tuple[DictionaryShape, ...],
    nodes: dict[str, GraphNode],
    edges: dict[str, GraphEdge],
) -> None:
    structure_evidence_ids = _evidence_ids(structure.evidence)
    for shape in shapes:
        shape_evidence_ids = _evidence_ids(shape.evidence)
        nodes.setdefault(
            shape.shape_id,
            GraphNode(
                node_id=shape.shape_id,
                label=f"shape of {owner.logical_name}",
                kind="dictionary-shape",
                evidence_ids=shape_evidence_ids,
            ),
        )
        shared_owner_id = _edge_id(structure.structure_id, shape.shape_id, "shared-recorded-owner")
        edges[shared_owner_id] = GraphEdge(
            edge_id=shared_owner_id,
            source_id=structure.structure_id,
            target_id=shape.shape_id,
            kind="shared-recorded-owner",
            label="shared recorded owner",
            confidence=shape.confidence,
            evidence_ids=tuple(sorted(set(structure_evidence_ids) | set(shape_evidence_ids))),
        )
        for state in shape.lifecycle_states:
            state_evidence_ids = _evidence_ids(state.evidence)
            nodes.setdefault(
                state.state_id,
                GraphNode(
                    node_id=state.state_id,
                    label=state.name,
                    kind="terminal-lifecycle-state" if state.terminal else "lifecycle-state",
                    evidence_ids=state_evidence_ids,
                ),
            )
            observed_id = _edge_id(shape.shape_id, state.state_id, "observed-state")
            edges[observed_id] = GraphEdge(
                edge_id=observed_id,
                source_id=shape.shape_id,
                target_id=state.state_id,
                kind="observed-state",
                label="observed state",
                confidence=shape.confidence,
                evidence_ids=state_evidence_ids,
            )


def _edge_resolution(value: ResolutionState) -> EdgeResolution:
    match value:
        case ResolutionState.RESOLVED:
            return EdgeResolution.RESOLVED
        case ResolutionState.UNRESOLVED:
            return EdgeResolution.UNRESOLVED
        case ResolutionState.AMBIGUOUS:
            return EdgeResolution.AMBIGUOUS
        case ResolutionState.TRUNCATED:
            return EdgeResolution.TRUNCATED
    assert_never(value)


def _structure_lifecycle_graph_projection(
    report: Report, selection: ProjectionSelection
) -> GraphProjection:
    """Show recorded ownership roles and literal shape states without chronology."""
    if not report.data_structures:
        raise ProjectionBuildError(
            "structure-lifecycle-graph requires recorded data-structure evidence"
        )
    subjects = {item.subject_id: item for item in report.subjects}
    structures = tuple(
        item
        for item in report.data_structures
        if not selection.languages
        or subjects[item.owner_subject_id].language in selection.languages
    )
    if not structures:
        raise ProjectionBuildError(
            "structure-lifecycle-graph has no data structures for the selected languages"
        )

    nodes: dict[str, GraphNode] = {}
    edges: dict[str, GraphEdge] = {}
    shapes_by_owner = _shapes_by_owner(report)
    for structure in structures:
        owner = subjects[structure.owner_subject_id]
        _add_role_evidence(structure, subjects, nodes, edges)
        _add_shape_state_evidence(
            structure,
            owner,
            shapes_by_owner.get(structure.owner_subject_id, ()),
            nodes,
            edges,
        )

    source = _report_input(report)
    limitations = set(_limitations(report, False))
    limitations.add(
        "Recorded role directions and observed states do not establish temporal order "
        "or runtime success."
    )
    limitations.add(
        "A shared recorded owner associates structure and shape evidence; it does not "
        "establish that their representations are identical."
    )
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.STRUCTURE_LIFECYCLE_GRAPH, selection),
        title="Data-structure lifecycle evidence graph",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, False),
        record_count=len(nodes) + len(edges),
        truncated=False,
        truncation_reason=None,
        limitations=tuple(sorted(limitations)),
        direction=EdgeDirection.DIRECTED,
        nodes=tuple(sorted(nodes.values(), key=lambda item: item.node_id)),
        edges=tuple(sorted(edges.values(), key=lambda item: item.edge_id)),
    )


def _conversion_graph_projection(report: Report, selection: ProjectionSelection) -> GraphProjection:
    """Show exact saved conversion and serialization flow edges."""
    subjects = {item.subject_id: item for item in report.subjects}
    flows = tuple(
        item
        for item in report.flow_edges
        if item.kind.value in {"conversion", "serialization"}
        and (
            not selection.languages
            or subjects[item.source_id].language in selection.languages
            or subjects[item.target_id].language in selection.languages
        )
    )
    if not flows:
        raise ProjectionBuildError(
            "conversion-graph requires recorded conversion or serialization evidence"
        )

    node_ids = {identity for flow in flows for identity in (flow.source_id, flow.target_id)}
    nodes = tuple(
        GraphNode(
            node_id=subject_id,
            label=subjects[subject_id].logical_name,
            kind="analysis-subject",
            evidence_ids=_evidence_ids(subjects[subject_id].evidence),
        )
        for subject_id in sorted(node_ids)
    )
    edges = tuple(
        GraphEdge(
            edge_id=flow.flow_id,
            source_id=flow.source_id,
            target_id=flow.target_id,
            kind=flow.kind.value,
            label=flow.kind.value,
            confidence=flow.confidence,
            resolution=_edge_resolution(flow.resolution),
            evidence_ids=_evidence_ids(flow.evidence),
        )
        for flow in sorted(flows, key=lambda item: item.flow_id)
    )
    source = _report_input(report)
    limitations = set(_limitations(report, False))
    limitations.add(
        "Conversion edges are static recorded sites; they do not prove runtime execution "
        "or success."
    )
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.CONVERSION_GRAPH, selection),
        title="Conversion and serialization flow graph",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, False),
        record_count=len(nodes) + len(edges),
        truncated=False,
        truncation_reason=None,
        limitations=tuple(sorted(limitations)),
        direction=EdgeDirection.DIRECTED,
        nodes=nodes,
        edges=edges,
    )
