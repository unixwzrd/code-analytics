"""Domain-owned deterministic projection builders."""

from __future__ import annotations

from code_analysis.projection_models import (
    EdgeDirection,
    GraphEdge,
    GraphNode,
    GraphProjection,
    MatrixAxisItem,
    MatrixCell,
    MatrixProjection,
    ProjectionMetric,
    ProjectionSelection,
    ReviewItem,
    ReviewPacketProjection,
    ScalarKind,
    TableProjection,
    TableRow,
)
from code_analysis.projections._catalog import ProjectionBuildError, ProjectionName
from code_analysis.projections._shared import (
    _boolean,
    _edge_id,
    _evidence_identity,
    _integer,
    _limitations,
    _projection_id,
    _projection_state,
    _report_input,
    _simple_phase_three_table,
    _string,
)
from code_analysis.report_models import AnalysisReport, DuplicateFunctionPair

Report = AnalysisReport


def _selected_duplicates(
    report: Report, selection: ProjectionSelection
) -> list[DuplicateFunctionPair]:
    records = [
        item
        for item in report.duplicate_functions
        if not selection.languages or item.language in selection.languages
    ]
    if selection.selected_ids:
        selected = set(selection.selected_ids)
        records = [
            item
            for item in records
            if item.duplicate_id in selected
            or item.left_subject_id in selected
            or item.right_subject_id in selected
        ]
        if not records:
            raise ProjectionBuildError("selected duplicate identity is absent from the report")
    return records


def _duplicate_function_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    rows = [
        TableRow(
            row_id=item.duplicate_id,
            cells=(
                _string(item.duplicate_id),
                _string(item.language.value),
                _string(item.left_subject_id),
                _string(item.right_subject_id),
                _integer(item.matched_statements),
                _integer(item.left_statement_total),
                _integer(item.right_statement_total),
                _integer(item.insertions),
                _integer(item.deletions),
                _integer(item.substitutions),
            ),
            evidence_ids=tuple(sorted(_evidence_identity(value) for value in item.evidence)),
        )
        for item in _selected_duplicates(report, selection)
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.DUPLICATE_FUNCTIONS,
        "Duplicate and near-duplicate functions",
        (
            ("duplicate-id", "Duplicate ID", ScalarKind.STRING),
            ("language", "Language", ScalarKind.STRING),
            ("left-subject", "Left subject", ScalarKind.STRING),
            ("right-subject", "Right subject", ScalarKind.STRING),
            ("matched-statements", "Matched statements", ScalarKind.INTEGER),
            ("left-total", "Left total", ScalarKind.INTEGER),
            ("right-total", "Right total", ScalarKind.INTEGER),
            ("insertions", "Insertions", ScalarKind.INTEGER),
            ("deletions", "Deletions", ScalarKind.INTEGER),
            ("substitutions", "Substitutions", ScalarKind.INTEGER),
        ),
        rows,
    )


def _clone_membership_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    js_subjects = {
        item.subject_id
        for item in report.subjects
        if item.language.value == "javascript"
        and (not selection.languages or item.language in selection.languages)
    }
    cluster_for_fragment = {
        fragment_id: item.cluster_id
        for item in report.clone_clusters
        for fragment_id in item.fragment_ids
    }
    rows = [
        TableRow(
            row_id=item.fragment_id,
            cells=(
                _string(item.fragment_id),
                _string(cluster_for_fragment.get(item.fragment_id, "")),
                _string(item.subject_id),
                _string(item.kind.value),
                _integer(item.nonblank_lines),
                _integer(item.statement_count),
            ),
            evidence_ids=tuple(sorted(_evidence_identity(value) for value in item.evidence)),
        )
        for item in report.clone_fragments
        if item.subject_id in js_subjects
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.CLONE_MEMBERSHIP,
        "JavaScript clone membership",
        (
            ("fragment-id", "Fragment ID", ScalarKind.STRING),
            ("cluster-id", "Cluster ID", ScalarKind.STRING),
            ("subject-id", "Subject ID", ScalarKind.STRING),
            ("kind", "Kind", ScalarKind.STRING),
            ("nonblank-lines", "Nonblank lines", ScalarKind.INTEGER),
            ("statements", "Statements", ScalarKind.INTEGER),
        ),
        rows,
    )


def _behavior_difference_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    allowed = {
        value
        for item in _selected_duplicates(report, selection)
        for value in item.behavior_difference_ids
    }
    rows = [
        TableRow(
            row_id=item.difference_id,
            cells=(
                _string(item.difference_id),
                _string(item.kind.value),
                _string(item.left_identity),
                _string(item.right_identity),
            ),
            evidence_ids=tuple(sorted(_evidence_identity(value) for value in item.evidence)),
        )
        for item in report.behavior_differences
        if item.difference_id in allowed
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.BEHAVIOR_DIFFERENCES,
        "JavaScript behavior differences",
        (
            ("difference-id", "Difference ID", ScalarKind.STRING),
            ("kind", "Kind", ScalarKind.STRING),
            ("left", "Left", ScalarKind.STRING),
            ("right", "Right", ScalarKind.STRING),
        ),
        rows,
    )


def _duplicate_ownership_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    rows = [
        TableRow(
            row_id=item.duplicate_id,
            cells=(
                _string(item.duplicate_id),
                _string(item.canonical_owner.state.value),
                _string(item.canonical_owner.subject_id or ""),
                _string(item.canonical_owner.reason),
            ),
        )
        for item in _selected_duplicates(report, selection)
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.DUPLICATE_OWNERSHIP,
        "Duplicate ownership review",
        (
            ("duplicate-id", "Duplicate ID", ScalarKind.STRING),
            ("state", "State", ScalarKind.STRING),
            ("subject-id", "Canonical subject", ScalarKind.STRING),
            ("reason", "Reason", ScalarKind.STRING),
        ),
        rows,
    )


def _duplicate_callers_tests_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    subject_to_duplicates: dict[str, list[str]] = {}
    for item in _selected_duplicates(report, selection):
        for subject_id in (item.left_subject_id, item.right_subject_id):
            subject_to_duplicates.setdefault(subject_id, []).append(item.duplicate_id)
    rows = [
        TableRow(
            row_id=f"{duplicate_id}:{relationship.relationship_id}",
            cells=(
                _string(duplicate_id),
                _string(relationship.relationship_id),
                _string(relationship.source_test_id or relationship.source_test_module_id or ""),
                _string(relationship.kind.value),
            ),
        )
        for relationship in report.test_relationships
        for duplicate_id in subject_to_duplicates.get(relationship.target_subject_id or "", ())
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.DUPLICATE_CALLERS_TESTS,
        "Duplicate callers and tests",
        (
            ("duplicate-id", "Duplicate ID", ScalarKind.STRING),
            ("relationship-id", "Relationship ID", ScalarKind.STRING),
            ("test-source", "Test source", ScalarKind.STRING),
            ("kind", "Kind", ScalarKind.STRING),
        ),
        rows,
    )


def _divergence_reason_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    rows = [
        TableRow(
            row_id=f"{item.duplicate_id}:{reason.value}",
            cells=(_string(item.duplicate_id), _string(reason.value)),
        )
        for item in _selected_duplicates(report, selection)
        for reason in item.divergence_reasons
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.DIVERGENCE_REASONS,
        "Duplicate divergence reasons",
        (
            ("duplicate-id", "Duplicate ID", ScalarKind.STRING),
            ("reason", "Reason", ScalarKind.STRING),
        ),
        rows,
    )


def _canonical_owner_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    result = _duplicate_ownership_projection(report, selection)
    return result.model_copy(
        update={
            "projection_id": _projection_id(
                _report_input(report), ProjectionName.CANONICAL_OWNER, selection
            ),
            "title": "Canonical owner suggestions",
        }
    )


def _javascript_refactor_packet_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    relationship_counts: dict[str, int] = {}
    for duplicate in _selected_duplicates(report, selection):
        subjects = {duplicate.left_subject_id, duplicate.right_subject_id}
        relationship_counts[duplicate.duplicate_id] = sum(
            item.target_subject_id in subjects for item in report.test_relationships
        )
    rows = [
        TableRow(
            row_id=item.duplicate_id,
            cells=(
                _string(item.duplicate_id),
                _string(item.left_subject_id),
                _string(item.right_subject_id),
                _integer(item.matched_statements),
                _string(", ".join(item.behavior_difference_ids)),
                _string(", ".join(value.value for value in item.divergence_reasons)),
                _string(item.canonical_owner.state.value),
                _string(item.canonical_owner.subject_id or ""),
                _string(item.canonical_owner.reason),
                _integer(relationship_counts[item.duplicate_id]),
                _string("; ".join(item.limitations)),
            ),
            evidence_ids=tuple(sorted(_evidence_identity(value) for value in item.evidence)),
        )
        for item in _selected_duplicates(report, selection)
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.JAVASCRIPT_REFACTOR_PACKET,
        "JavaScript refactoring review packet",
        (
            ("duplicate-id", "Duplicate ID", ScalarKind.STRING),
            ("left-subject", "Left subject", ScalarKind.STRING),
            ("right-subject", "Right subject", ScalarKind.STRING),
            ("matched-statements", "Matched statements", ScalarKind.INTEGER),
            ("behavior-differences", "Behavior differences", ScalarKind.STRING),
            ("divergence-reasons", "Divergence reasons", ScalarKind.STRING),
            ("owner-state", "Owner state", ScalarKind.STRING),
            ("owner-subject", "Owner subject", ScalarKind.STRING),
            ("owner-reason", "Owner reason", ScalarKind.STRING),
            ("caller-test-links", "Caller/test links", ScalarKind.INTEGER),
            ("limitations", "Limitations", ScalarKind.STRING),
        ),
        rows,
    )


def _clone_cluster_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    eligible_subjects = {
        item.subject_id: item.logical_name
        for item in report.subjects
        if not selection.languages or item.language in selection.languages
    }
    subjects = {item.subject_id: item.logical_name for item in report.subjects}
    rows = [
        TableRow(
            row_id=item.cluster_id,
            cells=(
                _string(item.cluster_id),
                _string(item.kind.value),
                _integer(len(item.fragment_ids)),
                _string(", ".join(subjects[value] for value in item.subject_ids)),
                _string(", ".join(item.related_cluster_ids)),
                _string(item.completeness.value),
            ),
            evidence_ids=tuple(sorted(_evidence_identity(value) for value in item.evidence)),
        )
        for item in report.clone_clusters
        if set(item.subject_ids) <= set(eligible_subjects)
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.CLONE_CLUSTERS,
        "Canonical clone clusters",
        (
            ("cluster-id", "Cluster ID", ScalarKind.STRING),
            ("kind", "Kind", ScalarKind.STRING),
            ("fragments", "Fragments", ScalarKind.INTEGER),
            ("subjects", "Subjects", ScalarKind.STRING),
            ("related-clusters", "Related clusters", ScalarKind.STRING),
            ("completeness", "Completeness", ScalarKind.STRING),
        ),
        rows,
    )


def _clone_graph_projection(report: Report, selection: ProjectionSelection) -> GraphProjection:
    subjects = {item.subject_id: item for item in report.subjects}
    eligible = {
        identity
        for identity, subject in subjects.items()
        if not selection.languages or subject.language in selection.languages
    }
    clusters = [item for item in report.clone_clusters if set(item.subject_ids) <= eligible]
    used_subjects = {value for item in clusters for value in item.subject_ids}
    nodes = [
        GraphNode(
            node_id=subject_id,
            label=subjects[subject_id].logical_name,
            kind="clone-subject",
        )
        for subject_id in sorted(used_subjects)
    ]
    edges: list[GraphEdge] = []
    for cluster in clusters:
        for left_index, left in enumerate(cluster.subject_ids):
            for right in cluster.subject_ids[left_index + 1 :]:
                edges.append(
                    GraphEdge(
                        edge_id=_edge_id(left, right, cluster.cluster_id),
                        source_id=left,
                        target_id=right,
                        kind=cluster.kind.value,
                        label=cluster.cluster_id,
                        confidence=cluster.confidence,
                        evidence_ids=tuple(
                            sorted(_evidence_identity(value) for value in cluster.evidence)
                        ),
                    )
                )
    edges.sort(key=lambda item: item.edge_id)
    retained_nodes = nodes[: selection.max_records]
    known = {item.node_id for item in retained_nodes}
    edge_limit = max(0, selection.max_records - len(retained_nodes))
    retained_edges = [
        item for item in edges if item.source_id in known and item.target_id in known
    ][:edge_limit]
    truncated = len(retained_nodes) < len(nodes) or len(retained_edges) < len(edges)
    source = _report_input(report)
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.CLONE_GRAPH, selection),
        title="Clone relationship graph",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, truncated),
        record_count=len(retained_nodes) + len(retained_edges),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=_limitations(report, truncated),
        direction=EdgeDirection.UNDIRECTED,
        nodes=tuple(retained_nodes),
        edges=tuple(retained_edges),
    )


def _function_commonality_projection(
    report: Report, selection: ProjectionSelection
) -> MatrixProjection:
    subjects = {item.subject_id: item for item in report.subjects}
    cells: list[MatrixCell] = []
    eligible = {
        identity
        for identity, subject in subjects.items()
        if not selection.languages or subject.language in selection.languages
    }
    for item in report.function_commonalities:
        if {item.left_subject_id, item.right_subject_id} - eligible:
            continue
        metrics = [
            ProjectionMetric(
                name=f"shared-{dimension.kind.value}",
                value=_integer(len(dimension.shared_identities)),
            )
            for dimension in item.dimensions
        ]
        metrics.append(
            ProjectionMetric(
                name="structural-clone-clusters",
                value=_integer(len(item.clone_cluster_ids)),
            )
        )
        metrics.sort(key=lambda value: value.name)
        cells.append(
            MatrixCell(
                cell_id=item.commonality_id,
                row_id=item.left_subject_id,
                column_id=item.right_subject_id,
                metrics=tuple(metrics),
                evidence_ids=tuple(sorted({_evidence_identity(value) for value in item.evidence})),
            )
        )
    cells.sort(key=lambda item: item.cell_id)
    truncated = len(cells) > selection.max_records
    bounded = tuple(cells[: selection.max_records])
    retained_identities = sorted(
        {value for item in bounded for value in (item.row_id, item.column_id)}
    )
    axes = tuple(
        MatrixAxisItem(item_id=value, label=subjects[value].logical_name)
        for value in retained_identities
    )
    source = _report_input(report)
    return MatrixProjection(
        projection_id=_projection_id(source, ProjectionName.FUNCTION_COMMONALITY, selection),
        title="Sparse function commonality matrix",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, truncated),
        record_count=len(bounded),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=_limitations(report, truncated),
        rows=axes,
        columns=axes,
        cells=bounded,
        symmetric=True,
    )


def _responsibility_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    subject_records = {item.subject_id: item for item in report.subjects}
    subjects = {identity: item.logical_name for identity, item in subject_records.items()}
    rows = [
        TableRow(
            row_id=item.responsibility_id,
            cells=(
                _string(item.responsibility_id),
                _integer(len(item.member_subject_ids)),
                _string(", ".join(value.value for value in item.shared_dimensions)),
                _string(", ".join(subjects[value] for value in item.member_subject_ids)),
                _integer(len(item.counter_evidence)),
                _string(item.completeness.value),
            ),
            evidence_ids=tuple(sorted(_evidence_identity(value) for value in item.evidence)),
        )
        for item in report.responsibility_clusters
        if all(
            not selection.languages or subject_records[value].language in selection.languages
            for value in item.member_subject_ids
        )
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.MODULE_RESPONSIBILITIES,
        "Responsibility and cohesion clusters",
        (
            ("responsibility-id", "Responsibility ID", ScalarKind.STRING),
            ("members", "Members", ScalarKind.INTEGER),
            ("strong-dimensions", "Strong dimensions", ScalarKind.STRING),
            ("subjects", "Subjects", ScalarKind.STRING),
            ("counter-evidence", "Counter-evidence", ScalarKind.INTEGER),
            ("completeness", "Completeness", ScalarKind.STRING),
        ),
        rows,
    )


def _state_side_effect_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    subject_records = {item.subject_id: item for item in report.subjects}
    subjects = {identity: item.logical_name for identity, item in subject_records.items()}
    state_resources = [
        item
        for item in report.state_resources
        if not selection.languages
        or subject_records[item.owner_subject_id].language in selection.languages
    ]
    side_effects = [
        item
        for item in report.side_effects
        if not selection.languages
        or subject_records[item.subject_id].language in selection.languages
    ]
    rows = [
        TableRow(
            row_id=item.state_resource_id,
            cells=(
                _string("state-resource"),
                _string(item.state_resource_id),
                _string(subjects[item.owner_subject_id]),
                _string(item.kind.value),
                _string(item.logical_name),
                _integer(len(item.read_evidence)),
                _integer(len(item.write_evidence)),
                _integer(len(item.cleanup_evidence)),
            ),
            evidence_ids=tuple(
                sorted(
                    _evidence_identity(value)
                    for value in (
                        *item.declaration_evidence,
                        *item.read_evidence,
                        *item.write_evidence,
                    )
                )
            ),
        )
        for item in state_resources
    ]
    rows.extend(
        TableRow(
            row_id=item.side_effect_id,
            cells=(
                _string("side-effect"),
                _string(item.side_effect_id),
                _string(subjects[item.subject_id]),
                _string(item.kind.value),
                _string(item.target_identity),
                _integer(0),
                _integer(1),
                _integer(0),
            ),
            evidence_ids=tuple(sorted(_evidence_identity(value) for value in item.evidence)),
        )
        for item in side_effects
    )
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.STATE_SIDE_EFFECTS,
        "Mutable state and side effects",
        (
            ("record-type", "Record type", ScalarKind.STRING),
            ("identity", "Identity", ScalarKind.STRING),
            ("owner", "Owner", ScalarKind.STRING),
            ("kind", "Kind", ScalarKind.STRING),
            ("target", "Target", ScalarKind.STRING),
            ("reads", "Reads", ScalarKind.INTEGER),
            ("writes", "Writes", ScalarKind.INTEGER),
            ("cleanup", "Cleanup", ScalarKind.INTEGER),
        ),
        rows,
    )


def _state_side_effect_graph_projection(
    report: Report, selection: ProjectionSelection
) -> GraphProjection:
    subjects = {item.subject_id: item for item in report.subjects}
    state_resources = [
        item
        for item in report.state_resources
        if not selection.languages
        or subjects[item.owner_subject_id].language in selection.languages
    ]
    side_effects = [
        item
        for item in report.side_effects
        if not selection.languages or subjects[item.subject_id].language in selection.languages
    ]
    used_subjects = {
        *(item.owner_subject_id for item in state_resources),
        *(item.subject_id for item in side_effects),
    }
    nodes = [
        GraphNode(
            node_id=value,
            label=subjects[value].logical_name,
            kind="subject",
        )
        for value in sorted(used_subjects)
    ]
    nodes.extend(
        GraphNode(
            node_id=item.state_resource_id,
            label=item.logical_name,
            kind=item.kind.value,
        )
        for item in state_resources
    )
    nodes.extend(
        GraphNode(
            node_id=item.side_effect_id,
            label=item.target_identity,
            kind=item.kind.value,
        )
        for item in side_effects
    )
    nodes.sort(key=lambda item: item.node_id)
    edges = [
        GraphEdge(
            edge_id=_edge_id(item.owner_subject_id, item.state_resource_id, "owns-state"),
            source_id=item.owner_subject_id,
            target_id=item.state_resource_id,
            kind="owns-state",
        )
        for item in state_resources
    ]
    edges.extend(
        GraphEdge(
            edge_id=_edge_id(item.subject_id, item.side_effect_id, "contains-effect"),
            source_id=item.subject_id,
            target_id=item.side_effect_id,
            kind="contains-effect",
        )
        for item in side_effects
    )
    edges.sort(key=lambda item: item.edge_id)
    retained_nodes = nodes[: selection.max_records]
    known = {item.node_id for item in retained_nodes}
    edge_limit = max(0, selection.max_records - len(retained_nodes))
    retained_edges = [
        item for item in edges if item.source_id in known and item.target_id in known
    ][:edge_limit]
    truncated = len(retained_nodes) < len(nodes) or len(retained_edges) < len(edges)
    source = _report_input(report)
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.STATE_SIDE_EFFECT_GRAPH, selection),
        title="State and side-effect ownership graph",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, truncated),
        record_count=len(retained_nodes) + len(retained_edges),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=_limitations(report, truncated),
        nodes=tuple(retained_nodes),
        edges=tuple(retained_edges),
    )


def _proposal_projection(
    report: Report,
    selection: ProjectionSelection,
    name: ProjectionName,
    kind: str,
) -> TableProjection:
    rows = [
        TableRow(
            row_id=item.proposal_id,
            cells=(
                _string(item.proposal_id),
                _string(item.state.value),
                _string(", ".join(item.affected_modules)),
                _string(item.proposed_logical_ownership),
                _integer(len(item.evidence)),
                _integer(len(item.counter_evidence)),
                _string(item.architecture_estimate_id),
            ),
            evidence_ids=tuple(sorted(_evidence_identity(value) for value in item.evidence)),
        )
        for item in report.refactor_proposals
        if item.kind.value == kind
    ]
    return _simple_phase_three_table(
        report,
        selection,
        name,
        "Extraction candidates" if kind == "extract-responsibility" else "Module combinations",
        (
            ("proposal-id", "Proposal ID", ScalarKind.STRING),
            ("state", "State", ScalarKind.STRING),
            ("modules", "Modules", ScalarKind.STRING),
            ("proposed-owner", "Proposed owner", ScalarKind.STRING),
            ("evidence", "Evidence", ScalarKind.INTEGER),
            ("counter-evidence", "Counter-evidence", ScalarKind.INTEGER),
            ("estimate-id", "Estimate ID", ScalarKind.STRING),
        ),
        rows,
    )


def _extraction_candidate_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    return _proposal_projection(
        report, selection, ProjectionName.EXTRACTION_CANDIDATES, "extract-responsibility"
    )


def _module_combination_candidate_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    return _proposal_projection(
        report, selection, ProjectionName.MODULE_COMBINATION_CANDIDATES, "combine-modules"
    )


def _architecture_estimate_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    rows = [
        TableRow(
            row_id=item.estimate_id,
            cells=(
                _string(item.estimate_id),
                _integer(item.before.import_edge_count),
                _integer(item.expected_after.import_edge_count),
                _integer(item.before.cross_boundary_edge_count),
                _integer(item.expected_after.cross_boundary_edge_count),
                _integer(item.before.scc_count),
                _integer(item.expected_after.scc_count),
                _integer(item.before.layer_violation_count),
                _integer(item.expected_after.layer_violation_count),
            ),
            evidence_ids=tuple(sorted(_evidence_identity(value) for value in item.evidence)),
        )
        for item in report.architecture_estimates
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.ARCHITECTURE_ESTIMATES,
        "Before and expected-after architecture estimates",
        (
            ("estimate-id", "Estimate ID", ScalarKind.STRING),
            ("before-edges", "Before edges", ScalarKind.INTEGER),
            ("after-edges", "After edges", ScalarKind.INTEGER),
            ("before-cross-boundary", "Before cross-boundary", ScalarKind.INTEGER),
            ("after-cross-boundary", "After cross-boundary", ScalarKind.INTEGER),
            ("before-sccs", "Before SCCs", ScalarKind.INTEGER),
            ("after-sccs", "After SCCs", ScalarKind.INTEGER),
            ("before-layer-violations", "Before layer violations", ScalarKind.INTEGER),
            ("after-layer-violations", "After layer violations", ScalarKind.INTEGER),
        ),
        rows,
    )


def _architecture_contract_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    rows = [
        TableRow(
            row_id=item.suggestion_id,
            cells=(
                _string(item.suggestion_id),
                _string(item.source_layer),
                _string(item.target_layer),
                _boolean(item.configured_rule),
                _string(item.import_linter_text),
                _string(item.confidence.value),
            ),
            evidence_ids=tuple(sorted(_evidence_identity(value) for value in item.evidence)),
        )
        for item in report.architecture_contract_suggestions
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.ARCHITECTURE_CONTRACT_SUGGESTIONS,
        "Architecture contract suggestions",
        (
            ("suggestion-id", "Suggestion ID", ScalarKind.STRING),
            ("source-layer", "Source layer", ScalarKind.STRING),
            ("target-layer", "Target layer", ScalarKind.STRING),
            ("configured", "Configured", ScalarKind.BOOLEAN),
            ("import-linter-text", "Import Linter text", ScalarKind.STRING),
            ("confidence", "Confidence", ScalarKind.STRING),
        ),
        rows,
    )


def _refactor_packet_projection(
    report: Report, selection: ProjectionSelection
) -> ReviewPacketProjection:
    items = [
        ReviewItem(
            item_id=item.proposal_id,
            title=f"Review {item.kind.value}: {item.proposed_logical_ownership}",
            statement=f"{item.problem_statement} State: {item.state.value}.",
            evidence_ids=tuple(sorted(_evidence_identity(value) for value in item.evidence)),
            counter_evidence_ids=tuple(
                sorted(_evidence_identity(value) for value in item.counter_evidence)
            ),
            related_ids=tuple(
                sorted(
                    {
                        *item.affected_subject_ids,
                        *item.affected_contract_ids,
                        *item.affected_shape_ids,
                        *item.affected_structure_ids,
                        *item.affected_test_ids,
                        *item.coverage_observation_ids,
                        *item.change_coupling_ids,
                        *item.clone_cluster_ids,
                        item.architecture_estimate_id,
                    }
                )
            ),
            review_questions=tuple(
                sorted(
                    {
                        *item.validation_needs,
                        *item.unresolved_maintainer_decisions,
                    }
                )
            ),
        )
        for item in report.refactor_proposals
    ]
    if selection.selected_ids:
        selected = set(selection.selected_ids)
        known = {item.item_id for item in items}
        missing = selected - known
        if missing:
            raise ProjectionBuildError(
                "unknown refactor proposal identity: " + ", ".join(sorted(missing))
            )
        items = [item for item in items if item.item_id in selected]
    items.sort(key=lambda item: item.item_id)
    truncated = len(items) > selection.max_records
    bounded = tuple(items[: selection.max_records])
    source = _report_input(report)
    limitations = {*_limitations(report, truncated), "review packet does not authorize changes"}
    return ReviewPacketProjection(
        projection_id=_projection_id(source, ProjectionName.REFACTOR_PACKET, selection),
        title="Bounded Phase 4 enriched refactoring review packet",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, truncated),
        record_count=len(bounded),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=tuple(sorted(limitations)),
        items=bounded,
    )
