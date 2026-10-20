"""Domain-owned deterministic projection builders."""

from __future__ import annotations

from code_analysis.projection_models import (
    ColumnAlignment,
    EdgeDirection,
    GraphEdge,
    GraphNode,
    GraphProjection,
    ProjectionMetric,
    ProjectionSelection,
    ScalarKind,
    TableColumn,
    TableProjection,
    TableRow,
    TableSort,
)
from code_analysis.projections._catalog import ProjectionName
from code_analysis.projections._shared import (
    _bounded_rows,
    _integer,
    _limitations,
    _projection_id,
    _projection_state,
    _report_input,
    _require_contract_evidence,
    _selected_contracts,
    _string,
    _table_base,
)
from code_analysis.report_models import AnalysisReport, DefaultKind

Report = AnalysisReport


def _contract_inventory_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    contract_report = _require_contract_evidence(report, ProjectionName.CONTRACTS)
    subjects = {subject.subject_id: subject for subject in contract_report.subjects}
    rows = []
    for contract in _selected_contracts(contract_report, selection):
        subject = subjects[contract.defining_subject_id]
        evidence = contract.evidence[0]
        rows.append(
            TableRow(
                row_id=contract.contract_id,
                cells=(
                    _string(subject.logical_name),
                    _string(contract.contract_id),
                    _string(contract.kind.value),
                    _string(subject.module),
                    _integer(len(contract.members)),
                    _integer(
                        sum(
                            default.kind is not DefaultKind.ABSENT
                            for member in contract.members
                            for default in member.producer_defaults
                        )
                    ),
                    _integer(sum(len(member.consumer_fallbacks) for member in contract.members)),
                    _integer(len(contract.boundaries)),
                    _string(
                        ", ".join(
                            sorted(
                                {boundary.boundary_class.value for boundary in contract.boundaries}
                            )
                        )
                    ),
                    _string(
                        ", ".join(
                            sorted({boundary.direction.value for boundary in contract.boundaries})
                        )
                    ),
                    _string(", ".join(contract.serialization_formats)),
                    _string(", ".join(contract.schema_versions)),
                    _string("; ".join(contract.unresolved_behavior)),
                    _string(evidence.span.path),
                    _integer(evidence.span.start_line),
                ),
            )
        )
    rows.sort(
        key=lambda row: (
            row.cells[2].string_value,
            row.cells[3].string_value,
            row.cells[0].string_value,
            row.row_id,
        )
    )
    bounded, truncated = _bounded_rows(rows, selection)
    integer_columns = {
        "member-count",
        "producer-default-count",
        "consumer-fallback-count",
        "boundary-count",
        "evidence-line",
    }
    definitions = (
        ("contract", "Contract"),
        ("contract-id", "Contract ID"),
        ("contract-kind", "Contract kind"),
        ("module", "Module"),
        ("member-count", "Members"),
        ("producer-default-count", "Producer defaults"),
        ("consumer-fallback-count", "Consumer fallbacks"),
        ("boundary-count", "Boundaries"),
        ("boundary-classes", "Boundary classes"),
        ("directions", "Directions"),
        ("serialization-formats", "Serialization formats"),
        ("schema-versions", "Schema versions"),
        ("unresolved-behavior", "Unresolved behavior"),
        ("evidence-path", "Evidence path"),
        ("evidence-line", "Evidence line"),
    )
    return TableProjection(
        **_table_base(
            contract_report,
            ProjectionName.CONTRACTS,
            selection,
            "Contract inventory",
            len(bounded),
            truncated,
        ),
        columns=tuple(
            TableColumn(
                column_id=column_id,
                label=label,
                position=position,
                value_kind=(
                    ScalarKind.INTEGER if column_id in integer_columns else ScalarKind.STRING
                ),
                alignment=(
                    ColumnAlignment.RIGHT if column_id in integer_columns else ColumnAlignment.LEFT
                ),
            )
            for position, (column_id, label) in enumerate(definitions)
        ),
        rows=bounded,
        sort=(
            TableSort(column_id="contract-kind"),
            TableSort(column_id="module"),
            TableSort(column_id="contract"),
        ),
    )


def _contract_boundary_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    contract_report = _require_contract_evidence(report, ProjectionName.CONTRACT_BOUNDARIES)
    subjects = {subject.subject_id: subject for subject in contract_report.subjects}
    rows = []
    for contract in _selected_contracts(contract_report, selection):
        defining_subject = subjects[contract.defining_subject_id]
        for boundary in contract.boundaries:
            site_subject = subjects[boundary.site_subject_id]
            evidence = boundary.evidence[0]
            rows.append(
                TableRow(
                    row_id=boundary.boundary_id,
                    cells=(
                        _string(defining_subject.logical_name),
                        _string(contract.contract_id),
                        _string(contract.kind.value),
                        _string(boundary.boundary_class.value),
                        _string(boundary.direction.value),
                        _string(site_subject.logical_name),
                        _string(site_subject.module),
                        _string(boundary.confidence.value),
                        _string(", ".join(contract.serialization_formats)),
                        _string(", ".join(contract.schema_versions)),
                        _string(evidence.span.path),
                        _integer(evidence.span.start_line),
                    ),
                )
            )
    rows.sort(
        key=lambda row: (
            row.cells[0].string_value,
            row.cells[3].string_value,
            row.cells[4].string_value,
            row.cells[5].string_value,
            row.row_id,
        )
    )
    bounded, truncated = _bounded_rows(rows, selection)
    definitions = (
        ("contract", "Contract", ScalarKind.STRING),
        ("contract-id", "Contract ID", ScalarKind.STRING),
        ("contract-kind", "Contract kind", ScalarKind.STRING),
        ("boundary-class", "Boundary class", ScalarKind.STRING),
        ("direction", "Direction", ScalarKind.STRING),
        ("site", "Site", ScalarKind.STRING),
        ("site-module", "Site module", ScalarKind.STRING),
        ("confidence", "Confidence", ScalarKind.STRING),
        ("serialization-formats", "Serialization formats", ScalarKind.STRING),
        ("schema-versions", "Schema versions", ScalarKind.STRING),
        ("evidence-path", "Evidence path", ScalarKind.STRING),
        ("evidence-line", "Evidence line", ScalarKind.INTEGER),
    )
    return TableProjection(
        **_table_base(
            contract_report,
            ProjectionName.CONTRACT_BOUNDARIES,
            selection,
            "Contract boundary matrix",
            len(bounded),
            truncated,
        ),
        columns=tuple(
            TableColumn(
                column_id=column_id,
                label=label,
                position=position,
                value_kind=value_kind,
                alignment=(
                    ColumnAlignment.RIGHT
                    if value_kind is ScalarKind.INTEGER
                    else ColumnAlignment.LEFT
                ),
            )
            for position, (column_id, label, value_kind) in enumerate(definitions)
        ),
        rows=bounded,
        sort=(
            TableSort(column_id="contract"),
            TableSort(column_id="boundary-class"),
            TableSort(column_id="direction"),
            TableSort(column_id="site"),
        ),
    )


def _contract_boundary_graph_projection(
    report: Report, selection: ProjectionSelection
) -> GraphProjection:
    contract_report = _require_contract_evidence(report, ProjectionName.CONTRACT_BOUNDARY_GRAPH)
    subjects = {subject.subject_id: subject for subject in contract_report.subjects}
    contracts = sorted(
        _selected_contracts(contract_report, selection),
        key=lambda contract: (
            subjects[contract.defining_subject_id].logical_name,
            contract.contract_id,
        ),
    )
    full_site_ids = {
        boundary.site_subject_id for contract in contracts for boundary in contract.boundaries
    }
    full_record_count = (
        len(contracts)
        + len(full_site_ids)
        + sum(len(contract.boundaries) for contract in contracts)
    )
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    exhausted = False
    for contract in contracts:
        if len(nodes) + len(edges) >= selection.max_records:
            break
        subject = subjects[contract.defining_subject_id]
        nodes[contract.contract_id] = GraphNode(
            node_id=contract.contract_id,
            label=subject.logical_name,
            kind=f"contract:{contract.kind.value}",
            metrics=(
                ProjectionMetric(name="boundaries", value=_integer(len(contract.boundaries))),
                ProjectionMetric(name="members", value=_integer(len(contract.members))),
                ProjectionMetric(
                    name="schema versions", value=_integer(len(contract.schema_versions))
                ),
            ),
        )
        for boundary in contract.boundaries:
            site = subjects[boundary.site_subject_id]
            site_cost = 0 if site.subject_id in nodes else 1
            if len(nodes) + len(edges) + site_cost + 1 > selection.max_records:
                exhausted = True
                break
            nodes.setdefault(
                site.subject_id,
                GraphNode(
                    node_id=site.subject_id,
                    label=site.logical_name,
                    kind=f"boundary-site:{boundary.boundary_class.value}",
                ),
            )
            edges.append(
                GraphEdge(
                    edge_id=boundary.boundary_id,
                    source_id=contract.contract_id,
                    target_id=site.subject_id,
                    kind=boundary.direction.value,
                    label=boundary.boundary_class.value,
                    confidence=boundary.confidence,
                )
            )
        if exhausted:
            break
    truncated = len(nodes) + len(edges) < full_record_count
    source = _report_input(contract_report)
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.CONTRACT_BOUNDARY_GRAPH, selection),
        title="Contract boundary graph",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(contract_report, truncated),
        record_count=len(nodes) + len(edges),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=_limitations(contract_report, truncated),
        direction=EdgeDirection.DIRECTED,
        nodes=tuple(sorted(nodes.values(), key=lambda node: node.node_id)),
        edges=tuple(sorted(edges, key=lambda edge: edge.edge_id)),
    )
