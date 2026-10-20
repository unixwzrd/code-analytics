"""Saved-report-only actionable boundary and candidate projections."""

from __future__ import annotations

from code_analysis.models import Confidence
from code_analysis.projection_models import (
    ColumnAlignment,
    EdgeDirection,
    GraphEdge,
    GraphNode,
    GraphProjection,
    ProjectionSelection,
    ScalarKind,
    TableColumn,
    TableProjection,
    TableRow,
    TableSort,
)
from code_analysis.projections._catalog import ProjectionBuildError, ProjectionName
from code_analysis.projections._shared import (
    _bounded_rows,
    _integer,
    _limitations,
    _projection_id,
    _projection_state,
    _report_input,
    _string,
    _table_base,
)
from code_analysis.report_models import AnalysisReport, BoundaryClass


def _table(
    report: AnalysisReport,
    selection: ProjectionSelection,
    name: ProjectionName,
    title: str,
    definitions: tuple[tuple[str, str, ScalarKind], ...],
    rows: list[TableRow],
    sort: tuple[TableSort, ...],
) -> TableProjection:
    positions = {
        column_id: position for position, (column_id, _label, _kind) in enumerate(definitions)
    }

    def sort_value(row: TableRow) -> tuple[str | int, ...]:
        values = []
        for item in sort:
            cell = row.cells[positions[item.column_id]]
            values.append(
                cell.string_value
                if cell.string_value is not None
                else cell.integer_value if cell.integer_value is not None else 0
            )
        return (*values, row.row_id)

    rows.sort(key=sort_value)
    bounded, truncated = _bounded_rows(rows, selection)
    base = _table_base(report, name, selection, title, len(bounded), truncated)
    return TableProjection(
        **base,
        columns=tuple(
            TableColumn(
                column_id=column_id,
                label=label,
                position=position,
                value_kind=kind,
                alignment=(
                    ColumnAlignment.RIGHT if kind is ScalarKind.INTEGER else ColumnAlignment.LEFT
                ),
            )
            for position, (column_id, label, kind) in enumerate(definitions)
        ),
        rows=tuple(bounded),
        sort=sort,
    )


def _candidate_behavioral_difference_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    rows = [
        TableRow(
            row_id=item.difference_id,
            cells=(
                _string(item.candidate_id),
                _string(item.kind.value),
                _string(item.left_subject_id),
                _string(item.right_subject_id),
                _string(", ".join(item.left_identities)),
                _string(", ".join(item.right_identities)),
                _integer(1 if item.blocking else 0),
            ),
        )
        for item in report.candidate_behavior_differences
    ]
    rows.sort(key=lambda item: item.row_id)
    return _table(
        report,
        selection,
        ProjectionName.CANDIDATE_BEHAVIORAL_DIFFERENCES,
        "Candidate behavioral differences",
        (
            ("candidate", "Candidate", ScalarKind.STRING),
            ("kind", "Kind", ScalarKind.STRING),
            ("left-subject", "Left subject", ScalarKind.STRING),
            ("right-subject", "Right subject", ScalarKind.STRING),
            ("left-observations", "Left observations", ScalarKind.STRING),
            ("right-observations", "Right observations", ScalarKind.STRING),
            ("blocking", "Blocking", ScalarKind.INTEGER),
        ),
        rows,
        (TableSort(column_id="candidate"), TableSort(column_id="kind")),
    )


def _cross_module_duplication_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    subjects = {item.subject_id: item for item in report.subjects}
    rows = []
    for cluster in report.clone_clusters:
        modules = tuple(
            sorted({subjects[item].module for item in cluster.subject_ids if item in subjects})
        )
        if len(modules) < 2:
            continue
        rows.append(
            TableRow(
                row_id=cluster.cluster_id,
                cells=(
                    _string(cluster.kind.value),
                    _string(", ".join(modules)),
                    _integer(len(cluster.subject_ids)),
                    _integer(len(modules)),
                    _string(", ".join(cluster.subject_ids)),
                ),
            )
        )
    rows.sort(key=lambda item: item.row_id)
    return _table(
        report,
        selection,
        ProjectionName.CROSS_MODULE_DUPLICATION,
        "Cross-module duplication",
        (
            ("kind", "Kind", ScalarKind.STRING),
            ("modules", "Modules", ScalarKind.STRING),
            ("members", "Members", ScalarKind.INTEGER),
            ("module-count", "Module count", ScalarKind.INTEGER),
            ("subjects", "Subjects", ScalarKind.STRING),
        ),
        rows,
        (TableSort(column_id="module-count"), TableSort(column_id="kind")),
    )


def _internal_contract_risk_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    subjects = {item.subject_id: item for item in report.subjects}
    eligible = {
        BoundaryClass.INTERNAL_MUTABLE_INDEX,
        BoundaryClass.INTERNAL_RECORD,
        BoundaryClass.CONFIGURATION_MERGE,
        BoundaryClass.CROSS_SUBSYSTEM_INTERNAL,
        BoundaryClass.UNCLASSIFIED_INTERNAL,
    }
    rows = []
    for contract in report.contracts:
        for boundary in contract.boundaries:
            if boundary.boundary_class not in eligible:
                continue
            subject = subjects[boundary.site_subject_id]
            rows.append(
                TableRow(
                    row_id=boundary.boundary_id,
                    cells=(
                        _string(contract.contract_id),
                        _string(boundary.boundary_class.value),
                        _string(boundary.direction.value),
                        _string(subject.module),
                        _string(subject.logical_name),
                        _string("; ".join(boundary.limitations)),
                    ),
                )
            )
    rows.sort(key=lambda item: item.row_id)
    return _table(
        report,
        selection,
        ProjectionName.INTERNAL_CONTRACT_RISKS,
        "Internal contract risks",
        (
            ("contract", "Contract", ScalarKind.STRING),
            ("boundary-class", "Boundary class", ScalarKind.STRING),
            ("direction", "Direction", ScalarKind.STRING),
            ("module", "Module", ScalarKind.STRING),
            ("logical-name", "Logical name", ScalarKind.STRING),
            ("limitations", "Limitations", ScalarKind.STRING),
        ),
        rows,
        (TableSort(column_id="boundary-class"), TableSort(column_id="module")),
    )


def _schema_drift_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    endpoints = {item.endpoint_id: item for item in report.interface_endpoints}
    rows = []
    for item in report.interface_correlations:
        producer = endpoints[item.producer_endpoint_id]
        consumer = endpoints[item.consumer_endpoint_id]
        rows.append(
            TableRow(
                row_id=item.correlation_id,
                cells=(
                    _string(producer.normalized_path),
                    _string(producer.method or producer.transport.value),
                    _string(producer.module),
                    _string(consumer.module),
                    _string(", ".join(item.matched_field_names)),
                    _integer(len(item.mismatch_ids)),
                    _string(", ".join(item.mismatch_ids)),
                ),
            )
        )
    rows.sort(key=lambda item: item.row_id)
    return _table(
        report,
        selection,
        ProjectionName.PRODUCER_CONSUMER_SCHEMA_DRIFT,
        "Producer and consumer schema drift",
        (
            ("path", "Path", ScalarKind.STRING),
            ("method-transport", "Method/transport", ScalarKind.STRING),
            ("producer", "Producer", ScalarKind.STRING),
            ("consumer", "Consumer", ScalarKind.STRING),
            ("matched-fields", "Matched fields", ScalarKind.STRING),
            ("mismatch-count", "Mismatches", ScalarKind.INTEGER),
            ("mismatch-ids", "Mismatch IDs", ScalarKind.STRING),
        ),
        rows,
        (TableSort(column_id="mismatch-count"), TableSort(column_id="path")),
    )


def _interface_mismatch_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    endpoints = {item.endpoint_id: item for item in report.interface_endpoints}
    rows = [
        TableRow(
            row_id=item.mismatch_id,
            cells=(
                _string(item.kind.value),
                _string(endpoints[item.producer_endpoint_id].normalized_path),
                _string(endpoints[item.producer_endpoint_id].module),
                _string(endpoints[item.consumer_endpoint_id].module),
                _string(item.field_name or ""),
                _string(item.explanation),
                _string(item.completeness.value),
            ),
        )
        for item in report.interface_mismatches
    ]
    rows.sort(key=lambda item: item.row_id)
    return _table(
        report,
        selection,
        ProjectionName.FRONTEND_BACKEND_INTERFACE_MISMATCHES,
        "Frontend/backend interface mismatches",
        (
            ("kind", "Kind", ScalarKind.STRING),
            ("path", "Path", ScalarKind.STRING),
            ("producer", "Producer", ScalarKind.STRING),
            ("consumer", "Consumer", ScalarKind.STRING),
            ("field", "Field", ScalarKind.STRING),
            ("explanation", "Explanation", ScalarKind.STRING),
            ("completeness", "Completeness", ScalarKind.STRING),
        ),
        rows,
        (TableSort(column_id="kind"), TableSort(column_id="path")),
    )


def _candidate_qualification_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    rows = [
        TableRow(
            row_id=item.requirement_id,
            cells=(
                _string(item.candidate_id),
                _string(item.kind.value),
                _string(item.state.value),
                _integer(len(item.test_ids)),
                _integer(len(item.coverage_observation_ids)),
                _string(item.reason),
            ),
        )
        for item in report.candidate_qualification_requirements
    ]
    rows.sort(key=lambda item: item.row_id)
    return _table(
        report,
        selection,
        ProjectionName.CANDIDATE_QUALIFICATION,
        "Candidate qualification requirements",
        (
            ("candidate", "Candidate", ScalarKind.STRING),
            ("kind", "Kind", ScalarKind.STRING),
            ("state", "State", ScalarKind.STRING),
            ("tests", "Tests", ScalarKind.INTEGER),
            ("coverage", "Coverage", ScalarKind.INTEGER),
            ("reason", "Reason", ScalarKind.STRING),
        ),
        rows,
        (TableSort(column_id="candidate"), TableSort(column_id="kind")),
    )


def _candidate_architecture_change_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    proposals = {item.architecture_estimate_id: item for item in report.refactor_proposals}
    rows = []
    for item in report.architecture_estimates:
        proposal = proposals.get(item.estimate_id)
        rows.append(
            TableRow(
                row_id=item.estimate_id,
                cells=(
                    _string(proposal.candidate_id if proposal and proposal.candidate_id else ""),
                    _integer(item.before.import_edge_count),
                    _integer(item.expected_after.import_edge_count),
                    _integer(item.before.scc_count),
                    _integer(item.expected_after.scc_count),
                    _integer(item.before.cycle_count),
                    _integer(item.expected_after.cycle_count),
                ),
            )
        )
    rows.sort(key=lambda item: item.row_id)
    return _table(
        report,
        selection,
        ProjectionName.CANDIDATE_ARCHITECTURE_CHANGES,
        "Candidate before/after architecture changes",
        (
            ("candidate", "Candidate", ScalarKind.STRING),
            ("before-edges", "Before edges", ScalarKind.INTEGER),
            ("after-edges", "After edges", ScalarKind.INTEGER),
            ("before-sccs", "Before SCCs", ScalarKind.INTEGER),
            ("after-sccs", "After SCCs", ScalarKind.INTEGER),
            ("before-cycles", "Before cycles", ScalarKind.INTEGER),
            ("after-cycles", "After cycles", ScalarKind.INTEGER),
        ),
        rows,
        (TableSort(column_id="candidate"),),
    )


def _ownership_contract_flow_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> GraphProjection:
    if not report.interface_endpoints:
        raise ProjectionBuildError("ownership contract flow requires recorded interface evidence")
    nodes = tuple(
        GraphNode(
            node_id=item.endpoint_id,
            label=f"{item.module} {item.normalized_path}",
            kind=f"interface-{item.role.value}",
            metrics=(),
        )
        for item in report.interface_endpoints
    )
    edges = tuple(
        GraphEdge(
            edge_id=item.correlation_id,
            source_id=item.producer_endpoint_id,
            target_id=item.consumer_endpoint_id,
            kind="producer-consumer",
            label=f"{len(item.mismatch_ids)} mismatches",
            confidence=Confidence.EXACT,
            evidence_ids=(),
        )
        for item in report.interface_correlations
    )
    source = _report_input(report)
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.OWNERSHIP_CONTRACT_FLOW, selection),
        title="Ownership and contract flow",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, False),
        record_count=len(nodes) + len(edges),
        limitations=_limitations(report, False),
        nodes=nodes,
        edges=edges,
        direction=EdgeDirection.DIRECTED,
    )
