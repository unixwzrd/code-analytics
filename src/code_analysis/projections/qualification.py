"""Domain-owned deterministic projection builders."""

from __future__ import annotations

import hashlib

from code_analysis.models import Confidence
from code_analysis.projection_models import (
    ColumnAlignment,
    EdgeDirection,
    EdgeResolution,
    GraphEdge,
    GraphNode,
    GraphProjection,
    MatrixAxisItem,
    MatrixCell,
    MatrixProjection,
    ProjectionMetric,
    ProjectionSelection,
    ScalarKind,
    TableColumn,
    TableProjection,
    TableRow,
)
from code_analysis.projections._catalog import ProjectionBuildError, ProjectionName
from code_analysis.projections._shared import (
    _boolean,
    _bounded_rows,
    _integer,
    _limitations,
    _number,
    _projection_id,
    _projection_state,
    _report_input,
    _simple_phase_three_table,
    _string,
    _table_base,
)
from code_analysis.report_models import AnalysisReport, SymbolCoverageRecord

Report = AnalysisReport


def _test_impact_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    if not (report.test_modules or report.test_cases or report.test_fixtures):
        raise ProjectionBuildError("test-impact requires recorded static test evidence")
    test_names = {
        **{item.test_module_id: item.module for item in report.test_modules},
        **{item.test_id: item.qualified_name for item in report.test_cases},
    }
    fixture_names = {item.fixture_id: item.qualified_name for item in report.test_fixtures}
    subject_names = {item.subject_id: item.logical_name for item in report.subjects}
    rows = []
    for relationship in report.test_relationships:
        source_id = relationship.source_test_id or relationship.source_test_module_id
        assert source_id is not None
        target = (
            subject_names.get(relationship.target_subject_id or "")
            or fixture_names.get(relationship.target_fixture_id or "")
            or relationship.target_text
            or relationship.target_module
            or "unresolved"
        )
        rows.append(
            TableRow(
                row_id=relationship.relationship_id,
                cells=(
                    _string(
                        test_names.get(
                            source_id,
                            fixture_names.get(
                                relationship.source_test_id or "",
                                source_id,
                            ),
                        )
                    ),
                    _string(relationship.kind.value),
                    _string(target),
                    _string(relationship.target_module or ""),
                    _string("resolved" if relationship.resolved else "unresolved"),
                    _string(relationship.evidence[0].span.path),
                    _integer(relationship.evidence[0].span.start_line),
                ),
                evidence_ids=(relationship.relationship_id,),
            )
        )
    rows.sort(key=lambda item: item.row_id)
    bounded, truncated = _bounded_rows(rows, selection)
    definitions = (
        ("test", "Test, fixture or module", ScalarKind.STRING),
        ("relationship", "Relationship", ScalarKind.STRING),
        ("target", "Target", ScalarKind.STRING),
        ("module", "Target module", ScalarKind.STRING),
        ("resolution", "Resolution", ScalarKind.STRING),
        ("path", "Evidence path", ScalarKind.STRING),
        ("line", "Evidence line", ScalarKind.INTEGER),
    )
    return TableProjection(
        **_table_base(
            report,
            ProjectionName.TEST_IMPACT,
            selection,
            "Static test-impact relationships",
            len(bounded),
            truncated,
        ),
        columns=tuple(
            TableColumn(
                column_id=column_id,
                label=label,
                position=index,
                value_kind=value_kind,
                alignment=(
                    ColumnAlignment.RIGHT
                    if value_kind is ScalarKind.INTEGER
                    else ColumnAlignment.LEFT
                ),
            )
            for index, (column_id, label, value_kind) in enumerate(definitions)
        ),
        rows=bounded,
    )


def _test_impact_graph_projection(
    report: Report, selection: ProjectionSelection
) -> GraphProjection:
    if not (report.test_modules or report.test_cases or report.test_fixtures):
        raise ProjectionBuildError("test-impact-graph requires recorded static test evidence")
    labels = {
        **{item.test_module_id: (item.module, "test-module") for item in report.test_modules},
        **{item.test_id: (item.qualified_name, "test") for item in report.test_cases},
        **{item.fixture_id: (item.qualified_name, "fixture") for item in report.test_fixtures},
        **{item.subject_id: (item.logical_name, "application-symbol") for item in report.subjects},
    }
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for relationship in report.test_relationships:
        source_id = relationship.source_test_id or relationship.source_test_module_id
        if source_id is None:
            raise ProjectionBuildError("test-impact relationship has no validated source")
        target_id = relationship.target_subject_id or relationship.target_fixture_id
        if target_id is None:
            target_label = relationship.target_text or relationship.target_module
            if target_label is None:
                continue
            target_id = "test-target-" + hashlib.sha256(target_label.encode()).hexdigest()[:20]
            labels[target_id] = (target_label, "unresolved-target")
        cost = int(source_id not in nodes) + int(target_id not in nodes) + 1
        if len(nodes) + len(edges) + cost > selection.max_records:
            break
        for node_id in (source_id, target_id):
            label, kind = labels[node_id]
            nodes.setdefault(node_id, GraphNode(node_id=node_id, label=label, kind=kind))
        edges.append(
            GraphEdge(
                edge_id=relationship.relationship_id,
                source_id=source_id,
                target_id=target_id,
                kind=relationship.kind.value,
                label=relationship.kind.value,
                confidence=relationship.evidence[0].confidence,
                resolution=(
                    EdgeResolution.RESOLVED if relationship.resolved else EdgeResolution.UNRESOLVED
                ),
                evidence_ids=(relationship.relationship_id,),
            )
        )
    full_nodes = {
        identity
        for relationship in report.test_relationships
        for identity in (
            relationship.source_test_id or relationship.source_test_module_id,
            relationship.target_subject_id
            or relationship.target_fixture_id
            or relationship.target_text
            or relationship.target_module,
        )
        if identity is not None
    }
    truncated = len(nodes) + len(edges) < len(full_nodes) + len(report.test_relationships)
    source = _report_input(report)
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.TEST_IMPACT_GRAPH, selection),
        title="Static test-impact map",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, truncated),
        record_count=len(nodes) + len(edges),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=_limitations(report, truncated),
        direction=EdgeDirection.DIRECTED,
        nodes=tuple(sorted(nodes.values(), key=lambda item: item.node_id)),
        edges=tuple(sorted(edges, key=lambda item: item.edge_id)),
    )


def _git_churn_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    if not report.git_path_changes:
        raise ProjectionBuildError("git-churn requires recorded Git path-change evidence")
    rows = [
        TableRow(
            row_id=item.path_change_id,
            cells=(
                _string(item.path),
                _string(item.kind.value),
                _string(item.commit_id),
                _integer(item.additions or 0),
                _integer(item.deletions or 0),
                _boolean(item.formatting_only),
            ),
            evidence_ids=(item.path_change_id,),
        )
        for item in report.git_path_changes
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.GIT_CHURN,
        "Git path churn",
        (
            ("path", "Path", ScalarKind.STRING),
            ("change", "Change", ScalarKind.STRING),
            ("commit", "Commit", ScalarKind.STRING),
            ("additions", "Additions", ScalarKind.INTEGER),
            ("deletions", "Deletions", ScalarKind.INTEGER),
            ("formatting-only", "Formatting only", ScalarKind.BOOLEAN),
        ),
        rows,
    )


def _change_coupling_projection(report: Report, selection: ProjectionSelection) -> MatrixProjection:
    if not report.change_couplings:
        raise ProjectionBuildError("change-coupling requires recorded sparse coupling evidence")
    retained = report.change_couplings[: selection.max_records]
    paths = sorted({path for item in retained for path in (item.left_path, item.right_path)})
    axes = tuple(MatrixAxisItem(item_id=path, label=path) for path in paths)
    cells = tuple(
        MatrixCell(
            cell_id=item.coupling_id,
            row_id=item.left_path,
            column_id=item.right_path,
            metrics=(
                ProjectionMetric(name="jaccard", value=_number(item.jaccard)),
                ProjectionMetric(name="left-total", value=_integer(item.left_total)),
                ProjectionMetric(name="right-total", value=_integer(item.right_total)),
                ProjectionMetric(name="shared-changes", value=_integer(item.shared_changes)),
            ),
            evidence_ids=tuple(sorted((item.coupling_id, *item.commit_ids))),
        )
        for item in retained
    )
    truncated = len(cells) < len(report.change_couplings)
    source = _report_input(report)
    return MatrixProjection(
        projection_id=_projection_id(source, ProjectionName.CHANGE_COUPLING, selection),
        title="Sparse change-coupling matrix",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, truncated),
        record_count=len(cells),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=_limitations(report, truncated),
        rows=axes,
        columns=axes,
        cells=cells,
        symmetric=True,
    )


def _change_coupling_graph_projection(
    report: Report, selection: ProjectionSelection
) -> GraphProjection:
    if not report.change_couplings:
        raise ProjectionBuildError(
            "change-coupling-graph requires recorded sparse coupling evidence"
        )
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for item in report.change_couplings:
        cost = int(item.left_path not in nodes) + int(item.right_path not in nodes) + 1
        if len(nodes) + len(edges) + cost > selection.max_records:
            break
        nodes.setdefault(
            item.left_path,
            GraphNode(node_id=item.left_path, label=item.left_path, kind="source-path"),
        )
        nodes.setdefault(
            item.right_path,
            GraphNode(node_id=item.right_path, label=item.right_path, kind="source-path"),
        )
        edges.append(
            GraphEdge(
                edge_id=item.coupling_id,
                source_id=item.left_path,
                target_id=item.right_path,
                kind="change-coupling",
                label=f"{item.shared_changes} shared",
                confidence=Confidence.HEURISTIC,
                evidence_ids=(item.coupling_id,),
            )
        )
    truncated = len(edges) < len(report.change_couplings)
    source = _report_input(report)
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.CHANGE_COUPLING_GRAPH, selection),
        title="Change-coupling graph",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, truncated),
        record_count=len(nodes) + len(edges),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=_limitations(report, truncated),
        direction=EdgeDirection.UNDIRECTED,
        nodes=tuple(sorted(nodes.values(), key=lambda item: item.node_id)),
        edges=tuple(sorted(edges, key=lambda item: item.edge_id)),
    )


def _generation_change_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    if not report.generation_changes:
        raise ProjectionBuildError(
            "generation-changes requires recorded generation comparison evidence"
        )
    rows = [
        TableRow(
            row_id=item.generation_change_id,
            cells=(
                _string(item.generation_change_id),
                _integer(len(item.added_paths)),
                _integer(len(item.changed_paths)),
                _integer(len(item.removed_paths)),
            ),
            evidence_ids=(item.generation_change_id,),
        )
        for item in report.generation_changes
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.GENERATION_CHANGES,
        "Retained-generation change sets",
        (
            ("interval", "Interval", ScalarKind.STRING),
            ("added", "Added", ScalarKind.INTEGER),
            ("changed", "Changed", ScalarKind.INTEGER),
            ("removed", "Removed", ScalarKind.INTEGER),
        ),
        rows,
    )


def _coverage_file_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    if not report.coverage_files:
        raise ProjectionBuildError("coverage-files requires recorded coverage evidence")
    rows = [
        TableRow(
            row_id=item.coverage_file_id,
            cells=(
                _string(item.path),
                _string(item.binding.value),
                _integer(len(item.covered_lines)),
                _integer(len(item.missing_lines)),
                _integer(len(item.arcs)),
                _integer(len(item.missing_arcs)),
                _integer(len(item.contexts)),
            ),
            evidence_ids=(item.coverage_file_id, item.coverage_id),
        )
        for item in report.coverage_files
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.COVERAGE_FILES,
        "Coverage files, lines, branches, and contexts",
        (
            ("path", "Path", ScalarKind.STRING),
            ("binding", "Binding", ScalarKind.STRING),
            ("covered-lines", "Covered lines", ScalarKind.INTEGER),
            ("missing-lines", "Missing lines", ScalarKind.INTEGER),
            ("arcs", "Arcs", ScalarKind.INTEGER),
            ("missing-arcs", "Missing arcs", ScalarKind.INTEGER),
            ("contexts", "Contexts", ScalarKind.INTEGER),
        ),
        rows,
    )


def _coverage_matrix_projection(report: Report, selection: ProjectionSelection) -> MatrixProjection:
    if not report.coverage_observations:
        raise ProjectionBuildError("coverage-matrix requires recorded symbol coverage evidence")
    subjects = {item.subject_id: item.logical_name for item in report.subjects}
    candidates = []
    for observation in report.coverage_observations:
        for context in observation.contexts or ("<no-dynamic-context>",):
            cell_id = (
                "coverage-cell-"
                + hashlib.sha256(
                    f"{observation.coverage_observation_id}\x1f{context}".encode()
                ).hexdigest()[:20]
            )
            candidates.append(
                MatrixCell(
                    cell_id=cell_id,
                    row_id=observation.subject_id,
                    column_id=context,
                    metrics=(
                        ProjectionMetric(
                            name="covered-lines", value=_integer(len(observation.covered_lines))
                        ),
                        ProjectionMetric(
                            name="missing-arcs", value=_integer(len(observation.missing_arcs))
                        ),
                        ProjectionMetric(
                            name="missing-lines", value=_integer(len(observation.missing_lines))
                        ),
                    ),
                    evidence_ids=(observation.coverage_observation_id,),
                )
            )
    candidates.sort(key=lambda item: item.cell_id)
    bounded = tuple(candidates[: selection.max_records])
    truncated = len(bounded) < len(candidates)
    row_ids = sorted({item.row_id for item in bounded})
    column_ids = sorted({item.column_id for item in bounded})
    source = _report_input(report)
    return MatrixProjection(
        projection_id=_projection_id(source, ProjectionName.COVERAGE_MATRIX, selection),
        title="Sparse coverage context and symbol matrix",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, truncated),
        record_count=len(bounded),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=tuple(
            sorted(
                {
                    *_limitations(report, truncated),
                    "Cell counts are saved symbol observation totals associated with recorded "
                    "context labels, not independently measured per-context execution counts.",
                }
            )
        ),
        rows=tuple(MatrixAxisItem(item_id=item, label=subjects[item]) for item in row_ids),
        columns=tuple(MatrixAxisItem(item_id=item, label=item) for item in column_ids),
        cells=bounded,
    )


def _proposal_coverage_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    if not report.refactor_proposals:
        raise ProjectionBuildError("uncovered-proposal-subjects requires refactor proposals")
    subject_names = {item.subject_id: item.logical_name for item in report.subjects}
    observations: dict[str, list[SymbolCoverageRecord]] = {}
    for item in report.coverage_observations:
        observations.setdefault(item.subject_id, []).append(item)
    rows = []
    for proposal in report.refactor_proposals:
        linked = set(proposal.coverage_observation_ids)
        for subject_id in proposal.affected_subject_ids:
            subject_observations = observations.get(subject_id, [])
            linked_observations = [
                item for item in subject_observations if item.coverage_observation_id in linked
            ]
            row_id = (
                "proposal-coverage-"
                + hashlib.sha256(f"{proposal.proposal_id}\x1f{subject_id}".encode()).hexdigest()[
                    :20
                ]
            )
            rows.append(
                TableRow(
                    row_id=row_id,
                    cells=(
                        _string(proposal.proposal_id),
                        _string(subject_names[subject_id]),
                        _string(
                            "covered-and-test-bound"
                            if linked_observations
                            else "qualification-unresolved"
                        ),
                        _integer(len(linked_observations)),
                        _integer(
                            len(
                                {
                                    context
                                    for item in linked_observations
                                    for context in item.contexts
                                }
                            )
                        ),
                    ),
                    evidence_ids=tuple(
                        sorted(item.coverage_observation_id for item in linked_observations)
                    ),
                )
            )
    rows.sort(key=lambda item: item.row_id)
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.UNCOVERED_PROPOSAL_SUBJECTS,
        "Coverage and exact-test binding for proposal subjects",
        (
            ("proposal", "Proposal", ScalarKind.STRING),
            ("subject", "Affected subject", ScalarKind.STRING),
            ("state", "Evidence state", ScalarKind.STRING),
            ("observations", "Coverage observations", ScalarKind.INTEGER),
            ("contexts", "Executed test contexts", ScalarKind.INTEGER),
        ),
        rows,
    )
