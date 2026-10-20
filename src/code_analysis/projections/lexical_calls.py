"""Unique recorded callee names, not inferred call sites or runtime edges."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from code_analysis.models import Completeness, SourceLanguage
from code_analysis.projection_models import (
    EdgeDirection,
    EdgeResolution,
    GraphCluster,
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
from code_analysis.report_models import AnalysisReport, AnalysisSubject, SectionState


def _subject_node(subject: AnalysisSubject) -> GraphNode:
    return GraphNode(
        node_id=subject.subject_id,
        label=subject.logical_name,
        kind="analysis-subject",
        group_id=subject.module,
        evidence_ids=tuple(sorted({_evidence_identity(item) for item in subject.evidence})),
    )


def _unknown_node(caller_id: str, callee: str, ambiguous: bool) -> GraphNode:
    digest = hashlib.sha256(
        json.dumps((caller_id, callee), separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return GraphNode(
        node_id="lexical-target-" + digest,
        label=callee,
        kind="ambiguous-callee" if ambiguous else "unresolved-callee",
    )


def _lexical_call_graph_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> GraphProjection:
    profiles = tuple(
        item
        for item in report.function_profiles
        if not selection.languages or item.language in selection.languages
    )
    if not profiles:
        raise ProjectionBuildError(
            "lexical-call-graph requires recorded function profiles for the selected languages"
        )
    subjects = {item.subject_id: item for item in report.subjects}
    if len({item.subject_id for item in profiles}) != len(profiles):
        raise ProjectionBuildError("lexical-call-graph has duplicate function profile owners")
    by_name: dict[tuple[SourceLanguage, str], list[AnalysisSubject]] = defaultdict(list)
    for subject in report.subjects:
        by_name[(subject.language, subject.logical_name)].append(subject)
    nodes: dict[str, GraphNode] = {}
    edges: dict[str, GraphEdge] = {}
    limitations = set(_limitations(report, False))
    unresolved_total = 0
    for profile in profiles:
        caller = subjects[profile.subject_id]
        if profile.language is not caller.language:
            raise ProjectionBuildError(
                "function profile language disagrees with its caller subject"
            )
        nodes[caller.subject_id] = _subject_node(caller)
        unresolved_total += profile.unresolved_call_count
        limitations.update(profile.limitations)
        for callee in profile.callees:
            matches = by_name.get((profile.language, callee), ())
            if len(matches) == 1:
                target = _subject_node(matches[0])
                resolution = EdgeResolution.RESOLVED
            else:
                target = _unknown_node(caller.subject_id, callee, bool(matches))
                resolution = EdgeResolution.AMBIGUOUS if matches else EdgeResolution.UNRESOLVED
            nodes[target.node_id] = target
            identity = _edge_id(caller.subject_id, target.node_id, "recorded-callee-name")
            edges[identity] = GraphEdge(
                edge_id=identity,
                source_id=caller.subject_id,
                target_id=target.node_id,
                kind="recorded-callee-name",
                label="unique recorded callee name",
                resolution=resolution,
                evidence_ids=tuple(sorted({_evidence_identity(item) for item in profile.evidence})),
            )
    if missing := set(selection.selected_ids) - nodes.keys():
        raise ProjectionBuildError(
            "selected lexical-call identities are absent: " + ", ".join(sorted(missing))
        )
    limitations.update(
        {
            "Each edge is one unique recorded callee name per function; call-site spans, "
            "occurrence counts and invocation order are not recorded.",
            "Resolved means a unique exact same-language saved subject-name match, not proof "
            "of runtime dispatch or behavioral coverage. No suffix or fuzzy matching is used.",
            "Unmatched and ambiguous names remain caller-scoped targets; they do not prove "
            "an external function exists. Declaration evidence is not call-site evidence.",
            f"Language-selected profiles before neighborhood/display bounds report "
            f"{unresolved_total} unresolved calls in aggregate. "
            "Provider semantics differ; zero does not prove complete resolution, and "
            "unnamed unresolved calls cannot become invented edges.",
        }
    )
    completeness = _projection_state(report, False)
    coverage = next(
        item for item in report.section_coverage if item.section == "function-commonality"
    )
    limitations.add(f"Function-profile section state: {coverage.state.value}.")
    if coverage.state is not SectionState.COMPLETE and completeness is Completeness.COMPLETE:
        completeness = Completeness.PARTIAL
    source = _report_input(report)
    groups: dict[str, list[str]] = defaultdict(list)
    for node in nodes.values():
        if node.group_id is not None:
            groups[node.group_id].append(node.node_id)
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.LEXICAL_CALL_GRAPH, selection),
        title="Recorded lexical callee relationships (not call sites)",
        inputs=(source,),
        selection=selection,
        completeness=completeness,
        record_count=len(nodes) + len(edges),
        truncated=False,
        truncation_reason=None,
        limitations=tuple(sorted(limitations)),
        direction=EdgeDirection.DIRECTED,
        nodes=tuple(sorted(nodes.values(), key=lambda item: item.node_id)),
        edges=tuple(sorted(edges.values(), key=lambda item: item.edge_id)),
        clusters=tuple(
            GraphCluster(cluster_id=module, label=module, node_ids=tuple(sorted(identities)))
            for module, identities in sorted(groups.items())
        ),
    )
