"""Domain-owned deterministic projection builders."""

from __future__ import annotations

from code_analysis.projection_models import (
    ColumnAlignment,
    EdgeDirection,
    EdgeResolution,
    GraphEdge,
    GraphNode,
    GraphProjection,
    ProjectionSelection,
    ReviewItem,
    ReviewPacketProjection,
    ScalarKind,
    SortDirection,
    TableColumn,
    TableProjection,
    TableRow,
    TableSort,
)
from code_analysis.projections._catalog import ProjectionBuildError, ProjectionName
from code_analysis.projections._shared import (
    _boolean,
    _bounded_rows,
    _edge_id,
    _evidence_identity,
    _integer,
    _limitations,
    _projection_id,
    _projection_state,
    _report_input,
    _string,
    _table_base,
)
from code_analysis.report_models import AnalysisReport

Report = AnalysisReport


def _require_shape_evidence(report: Report) -> AnalysisReport:
    if not report.dictionary_shapes:
        raise ProjectionBuildError("shape-variants requires recorded dictionary-shape evidence")
    return report


def _shape_variant_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    shape_report = _require_shape_evidence(report)
    subjects = {subject.subject_id: subject for subject in shape_report.subjects}
    rows: list[TableRow] = []
    for shape in shape_report.dictionary_shapes:
        owner = subjects[shape.owner_subject_id]
        states = {state.state_id: state.name for state in shape.lifecycle_states}
        for variant in shape.variants:
            evidence = variant.evidence[0]
            requiredness = sorted(
                {
                    item.kind.value
                    for field in variant.fields
                    for item in field.requiredness_observations
                }
            )
            rows.append(
                TableRow(
                    row_id=variant.variant_id,
                    cells=(
                        _string(owner.logical_name),
                        _string(shape.shape_id),
                        _string(variant.name),
                        _string(variant.variant_id),
                        _integer(len(variant.fields)),
                        _string(", ".join(field.name for field in variant.fields)),
                        _string(
                            variant.discriminator.field_name
                            if variant.discriminator is not None
                            else ""
                        ),
                        _string(
                            ", ".join(
                                value.canonical_json()
                                for value in (
                                    variant.discriminator.values
                                    if variant.discriminator is not None
                                    else ()
                                )
                            )
                        ),
                        _string(", ".join(requiredness)),
                        _string(", ".join(states[item] for item in variant.lifecycle_state_ids)),
                        _integer(sum(len(field.producer_defaults) for field in variant.fields)),
                        _integer(sum(len(field.consumer_fallbacks) for field in variant.fields)),
                        _string(variant.confidence.value),
                        _string(evidence.span.path),
                        _integer(evidence.span.start_line),
                    ),
                )
            )
    rows.sort(
        key=lambda row: (
            row.cells[0].string_value,
            row.cells[2].string_value,
            row.row_id,
        )
    )
    bounded, truncated = _bounded_rows(rows, selection)
    definitions = (
        ("owner", "Owner", ScalarKind.STRING),
        ("shape-id", "Shape ID", ScalarKind.STRING),
        ("variant", "Variant", ScalarKind.STRING),
        ("variant-id", "Variant ID", ScalarKind.STRING),
        ("field-count", "Fields", ScalarKind.INTEGER),
        ("field-names", "Field names", ScalarKind.STRING),
        ("discriminator", "Discriminator", ScalarKind.STRING),
        ("discriminator-values", "Discriminator values", ScalarKind.STRING),
        ("requiredness", "Requiredness states", ScalarKind.STRING),
        ("lifecycle-states", "Lifecycle states", ScalarKind.STRING),
        ("producer-defaults", "Producer defaults", ScalarKind.INTEGER),
        ("consumer-fallbacks", "Consumer fallbacks", ScalarKind.INTEGER),
        ("confidence", "Confidence", ScalarKind.STRING),
        ("evidence-path", "Evidence path", ScalarKind.STRING),
        ("evidence-line", "Evidence line", ScalarKind.INTEGER),
    )
    return TableProjection(
        **_table_base(
            shape_report,
            ProjectionName.SHAPE_VARIANTS,
            selection,
            "Dictionary shape variants",
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
        sort=(TableSort(column_id="owner"), TableSort(column_id="variant")),
    )


def _dictionary_flow_projection(report: Report, selection: ProjectionSelection) -> GraphProjection:
    if not report.flow_edges:
        raise ProjectionBuildError("dictionary-flow requires recorded resolved flow evidence")
    subjects = {subject.subject_id: subject for subject in report.subjects}
    flows = tuple(
        flow
        for flow in report.flow_edges
        if not selection.languages
        or subjects[flow.source_id].language in selection.languages
        or subjects[flow.target_id].language in selection.languages
    )
    if not flows:
        raise ProjectionBuildError("dictionary-flow has no flows for the selected languages")
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for flow in sorted(flows, key=lambda item: item.flow_id):
        source = subjects[flow.source_id]
        target = subjects[flow.target_id]
        cost = int(flow.source_id not in nodes) + int(flow.target_id not in nodes) + 1
        if len(nodes) + len(edges) + cost > selection.max_records:
            break
        nodes.setdefault(
            flow.source_id,
            GraphNode(
                node_id=flow.source_id,
                label=source.logical_name,
                kind="dictionary-subject",
            ),
        )
        nodes.setdefault(
            flow.target_id,
            GraphNode(
                node_id=flow.target_id,
                label=target.logical_name,
                kind="dictionary-subject",
            ),
        )
        edges.append(
            GraphEdge(
                edge_id=flow.flow_id,
                source_id=flow.source_id,
                target_id=flow.target_id,
                kind=flow.kind.value,
                label=flow.kind.value,
                confidence=flow.confidence,
                resolution=EdgeResolution.RESOLVED,
            )
        )
    full_node_ids = {identity for flow in flows for identity in (flow.source_id, flow.target_id)}
    truncated = len(nodes) + len(edges) < len(full_node_ids) + len(flows)
    source_input = _report_input(report)
    return GraphProjection(
        projection_id=_projection_id(source_input, ProjectionName.DICTIONARY_FLOW, selection),
        title="Dictionary producer and consumer flow",
        inputs=(source_input,),
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


def _unresolved_coverage_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    rows = [
        TableRow(
            row_id=f"section:{section.section}",
            cells=(
                _string("section"),
                _string(section.section),
                _string(section.state.value),
                _string(""),
                _string(""),
                _integer(section.unresolved),
                _integer(section.truncated),
                _string(""),
                _integer(0),
            ),
        )
        for section in report.section_coverage
    ]
    rows.extend(
        TableRow(
            row_id=f"edge:{edge.unresolved_id}",
            cells=(
                _string("unresolved-edge"),
                _string(edge.unresolved_id),
                _string("partial" if edge.effect_on_completeness else "recorded"),
                _string(edge.operation),
                _string(edge.reason),
                _integer(1),
                _integer(0),
                _string(edge.evidence[0].span.path),
                _integer(edge.evidence[0].span.start_line),
            ),
        )
        for edge in report.unresolved_edges
    )
    rows.sort(key=lambda row: (row.cells[0].string_value, row.cells[1].string_value, row.row_id))
    bounded, truncated = _bounded_rows(rows, selection)
    definitions = (
        ("record-type", "Record type", ScalarKind.STRING),
        ("identity", "Identity", ScalarKind.STRING),
        ("state", "State", ScalarKind.STRING),
        ("operation", "Operation", ScalarKind.STRING),
        ("reason", "Reason", ScalarKind.STRING),
        ("unresolved", "Unresolved", ScalarKind.INTEGER),
        ("truncated", "Truncated", ScalarKind.INTEGER),
        ("evidence-path", "Evidence path", ScalarKind.STRING),
        ("evidence-line", "Evidence line", ScalarKind.INTEGER),
    )
    return TableProjection(
        **_table_base(
            report,
            ProjectionName.UNRESOLVED_COVERAGE,
            selection,
            "Unresolved evidence coverage",
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
        sort=(TableSort(column_id="record-type"), TableSort(column_id="identity")),
    )


def _validation_divergence_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    if not report.validation_patterns:
        raise ProjectionBuildError(
            "validation-divergence requires recorded structural-validation evidence"
        )
    subjects = {subject.subject_id: subject for subject in report.subjects}
    rows = [
        TableRow(
            row_id=pattern.validation_id,
            cells=(
                _string(pattern.validation_id),
                _string(pattern.classification),
                _string(pattern.rule),
                _string(pattern.field_identity or ""),
                _integer(len(pattern.sites)),
                _string(", ".join(subjects[item].logical_name for item in pattern.subject_ids)),
                _boolean(pattern.divergent_enforcement),
                _boolean(pattern.fail_open),
                _string("; ".join(pattern.limitations)),
                _string(pattern.sites[0].span.path),
                _integer(pattern.sites[0].span.start_line),
            ),
        )
        for pattern in report.validation_patterns
    ]
    rows.sort(
        key=lambda row: (
            not bool(row.cells[6].boolean_value),
            row.cells[3].string_value,
            row.cells[2].string_value,
            row.row_id,
        )
    )
    bounded, truncated = _bounded_rows(rows, selection)
    definitions = (
        ("validation-id", "Validation ID", ScalarKind.STRING),
        ("classification", "Classification", ScalarKind.STRING),
        ("rule", "Rule", ScalarKind.STRING),
        ("field-identity", "Field identity", ScalarKind.STRING),
        ("site-count", "Sites", ScalarKind.INTEGER),
        ("subjects", "Subjects", ScalarKind.STRING),
        ("divergent", "Divergent", ScalarKind.BOOLEAN),
        ("fail-open", "Fail open", ScalarKind.BOOLEAN),
        ("limitations", "Limitations", ScalarKind.STRING),
        ("evidence-path", "Evidence path", ScalarKind.STRING),
        ("evidence-line", "Evidence line", ScalarKind.INTEGER),
    )
    return TableProjection(
        **_table_base(
            report,
            ProjectionName.VALIDATION_DIVERGENCE,
            selection,
            "Structural validation divergence",
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
            TableSort(column_id="divergent", direction=SortDirection.DESCENDING),
            TableSort(column_id="field-identity"),
            TableSort(column_id="rule"),
        ),
    )


def _require_structure_evidence(report: Report, name: ProjectionName) -> AnalysisReport:
    if not report.data_structures:
        raise ProjectionBuildError(f"{name.value} requires recorded data-structure evidence")
    return report


def _ownership_alias_flow_projection(
    report: Report, selection: ProjectionSelection
) -> GraphProjection:
    structure_report = _require_structure_evidence(report, ProjectionName.OWNERSHIP_ALIAS_FLOW)
    subjects = {item.subject_id: item for item in structure_report.subjects}
    structures = tuple(
        item
        for item in structure_report.data_structures
        if not selection.languages
        or subjects[item.owner_subject_id].language in selection.languages
    )
    if not structures:
        raise ProjectionBuildError(
            "ownership-alias-flow has no data structures for the selected languages"
        )
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    relation_specs = (
        ("owner", "subject-to-structure", lambda item: (item.owner_subject_id,)),
        ("producer", "subject-to-structure", lambda item: item.producer_subject_ids),
        ("writer", "subject-to-structure", lambda item: item.writer_subject_ids),
        ("mutator", "subject-to-structure", lambda item: item.mutator_subject_ids),
        ("reader", "structure-to-subject", lambda item: item.reader_subject_ids),
        ("alias", "structure-to-subject", lambda item: item.alias_subject_ids),
    )
    full_node_ids: set[str] = set()
    full_edge_ids: set[str] = set()
    for structure in structures:
        relations = [
            (kind, direction, subject_id)
            for kind, direction, accessor in relation_specs
            for subject_id in accessor(structure)
        ]
        subject_ids = {subject_id for _kind, _direction, subject_id in relations}
        full_node_ids.add(structure.structure_id)
        full_node_ids.update(subject_ids)
        for kind, direction, subject_id in relations:
            source_id, target_id = (
                (subject_id, structure.structure_id)
                if direction == "subject-to-structure"
                else (structure.structure_id, subject_id)
            )
            full_edge_ids.add(_edge_id(source_id, target_id, kind))
        cost = int(structure.structure_id not in nodes)
        cost += sum(subject_id not in nodes for subject_id in subject_ids)
        cost += len(relations)
        if len(nodes) + len(edges) + cost > selection.max_records:
            continue
        owner = subjects[structure.owner_subject_id]
        nodes.setdefault(
            structure.structure_id,
            GraphNode(
                node_id=structure.structure_id,
                label=owner.logical_name,
                kind="data-structure",
            ),
        )
        for subject_id in sorted(subject_ids):
            subject = subjects[subject_id]
            nodes.setdefault(
                subject_id,
                GraphNode(
                    node_id=subject_id,
                    label=subject.logical_name,
                    kind="analysis-subject",
                ),
            )
        for kind, direction, subject_id in relations:
            source_id, target_id = (
                (subject_id, structure.structure_id)
                if direction == "subject-to-structure"
                else (structure.structure_id, subject_id)
            )
            edges.append(
                GraphEdge(
                    edge_id=_edge_id(source_id, target_id, kind),
                    source_id=source_id,
                    target_id=target_id,
                    kind=kind,
                    label=kind,
                    confidence=structure.confidence,
                    resolution=EdgeResolution.RESOLVED,
                )
            )
    truncated = len(nodes) + len(edges) < len(full_node_ids) + len(full_edge_ids)
    source_input = _report_input(structure_report)
    return GraphProjection(
        projection_id=_projection_id(
            source_input,
            ProjectionName.OWNERSHIP_ALIAS_FLOW,
            selection,
        ),
        title="Data-structure ownership and alias flow",
        inputs=(source_input,),
        selection=selection,
        completeness=_projection_state(structure_report, truncated),
        record_count=len(nodes) + len(edges),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=_limitations(structure_report, truncated),
        direction=EdgeDirection.DIRECTED,
        nodes=tuple(sorted(nodes.values(), key=lambda item: item.node_id)),
        edges=tuple(sorted(edges, key=lambda item: item.edge_id)),
    )


def _structure_lifecycle_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    structure_report = _require_structure_evidence(report, ProjectionName.STRUCTURE_LIFECYCLE)
    subjects = {item.subject_id: item for item in structure_report.subjects}
    rows = [
        TableRow(
            row_id=structure.structure_id,
            cells=(
                _string(structure.structure_id),
                _string(subjects[structure.owner_subject_id].logical_name),
                _string(structure.scope.value),
                _string(structure.canonical_type.name or structure.canonical_type.kind.value),
                _integer(len(structure.producer_subject_ids)),
                _integer(len(structure.reader_subject_ids)),
                _integer(len(structure.writer_subject_ids)),
                _integer(len(structure.mutator_subject_ids)),
                _integer(len(structure.serializer_subject_ids)),
                _string(", ".join(structure.persistence_boundary_ids)),
                _integer(len(structure.conversion_flow_ids)),
                _integer(len(structure.alias_subject_ids)),
                _string("; ".join(structure.invariants)),
                _string("; ".join(structure.ordering_assumptions)),
                _string("; ".join(structure.lifetime)),
                _string("; ".join(structure.cleanup)),
                _string("; ".join(structure.concurrency)),
                _string("; ".join(structure.growth_indicators)),
                _string("; ".join(structure.unresolved_behavior)),
                _string(structure.confidence.value),
                _string(structure.evidence[0].span.path),
                _integer(structure.evidence[0].span.start_line),
            ),
        )
        for structure in structure_report.data_structures
    ]
    rows.sort(key=lambda row: (row.cells[1].string_value, row.row_id))
    bounded, truncated = _bounded_rows(rows, selection)
    definitions = (
        ("structure-id", "Structure ID", ScalarKind.STRING),
        ("owner", "Owner", ScalarKind.STRING),
        ("scope", "Scope", ScalarKind.STRING),
        ("type", "Type", ScalarKind.STRING),
        ("producers", "Producers", ScalarKind.INTEGER),
        ("readers", "Readers", ScalarKind.INTEGER),
        ("writers", "Writers", ScalarKind.INTEGER),
        ("mutators", "Mutators", ScalarKind.INTEGER),
        ("serializers", "Serializers", ScalarKind.INTEGER),
        ("persistence-boundaries", "Persistence boundaries", ScalarKind.STRING),
        ("conversions", "Conversions", ScalarKind.INTEGER),
        ("aliases", "Aliases", ScalarKind.INTEGER),
        ("invariants", "Invariants", ScalarKind.STRING),
        ("ordering", "Ordering", ScalarKind.STRING),
        ("lifetime", "Lifetime", ScalarKind.STRING),
        ("cleanup", "Cleanup", ScalarKind.STRING),
        ("concurrency", "Concurrency", ScalarKind.STRING),
        ("growth", "Growth indicators", ScalarKind.STRING),
        ("unresolved", "Unresolved", ScalarKind.STRING),
        ("confidence", "Confidence", ScalarKind.STRING),
        ("evidence-path", "Evidence path", ScalarKind.STRING),
        ("evidence-line", "Evidence line", ScalarKind.INTEGER),
    )
    return TableProjection(
        **_table_base(
            structure_report,
            ProjectionName.STRUCTURE_LIFECYCLE,
            selection,
            "Data-structure lifecycle",
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
        sort=(TableSort(column_id="owner"),),
    )


def _representation_candidate_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    if not report.candidate_classifications:
        raise ProjectionBuildError("representation-candidates requires recorded candidate evidence")
    subjects = {item.subject_id: item for item in report.subjects}
    rows = [
        TableRow(
            row_id=item.candidate_id,
            cells=(
                _string(item.candidate_id),
                _string(subjects[item.subject_id].logical_name),
                _string(item.candidate.value),
                _string(item.confidence.value),
                _integer(len(item.supporting_evidence)),
                _integer(len(item.counter_evidence)),
                _string("; ".join(item.limitations)),
                _string(item.supporting_evidence[0].span.path),
                _integer(item.supporting_evidence[0].span.start_line),
            ),
        )
        for item in report.candidate_classifications
    ]
    rows.sort(
        key=lambda row: (
            row.cells[2].string_value,
            row.cells[1].string_value,
            row.row_id,
        )
    )
    bounded, truncated = _bounded_rows(rows, selection)
    definitions = (
        ("candidate-id", "Candidate ID", ScalarKind.STRING),
        ("subject", "Subject", ScalarKind.STRING),
        ("classification", "Classification", ScalarKind.STRING),
        ("confidence", "Confidence", ScalarKind.STRING),
        ("supporting-sites", "Supporting sites", ScalarKind.INTEGER),
        ("counter-sites", "Counter sites", ScalarKind.INTEGER),
        ("limitations", "Limitations", ScalarKind.STRING),
        ("evidence-path", "Evidence path", ScalarKind.STRING),
        ("evidence-line", "Evidence line", ScalarKind.INTEGER),
    )
    return TableProjection(
        **_table_base(
            report,
            ProjectionName.REPRESENTATION_CANDIDATES,
            selection,
            "Dictionary representation candidates",
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
            TableSort(column_id="classification"),
            TableSort(column_id="subject"),
        ),
    )


def _conversion_churn_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    structure_report = _require_structure_evidence(report, ProjectionName.CONVERSION_CHURN)
    flows = {
        item.flow_id: item
        for item in structure_report.flow_edges
        if item.kind.value in {"conversion", "serialization"}
    }
    if not flows:
        raise ProjectionBuildError(
            "conversion-churn requires recorded conversion or serialization evidence"
        )
    subjects = {item.subject_id: item for item in structure_report.subjects}
    rows: list[TableRow] = []
    for structure in structure_report.data_structures:
        owner = subjects[structure.owner_subject_id]
        for flow_id in structure.conversion_flow_ids:
            flow = flows.get(flow_id)
            if flow is None:
                continue
            target = subjects[flow.target_id]
            rows.append(
                TableRow(
                    row_id=f"{structure.structure_id}:{flow.flow_id}",
                    cells=(
                        _string(structure.structure_id),
                        _string(owner.logical_name),
                        _string(flow.flow_id),
                        _string(flow.kind.value),
                        _string(target.logical_name),
                        _string(flow.confidence.value),
                        _string(flow.evidence[0].span.path),
                        _integer(flow.evidence[0].span.start_line),
                    ),
                )
            )
    rows.sort(
        key=lambda row: (
            row.cells[1].string_value,
            row.cells[3].string_value,
            row.cells[4].string_value,
            row.cells[6].string_value,
            row.cells[7].integer_value,
            row.row_id,
        )
    )
    bounded, truncated = _bounded_rows(rows, selection)
    definitions = (
        ("structure-id", "Structure ID", ScalarKind.STRING),
        ("owner", "Owner", ScalarKind.STRING),
        ("flow-id", "Flow ID", ScalarKind.STRING),
        ("kind", "Kind", ScalarKind.STRING),
        ("target", "Target", ScalarKind.STRING),
        ("confidence", "Confidence", ScalarKind.STRING),
        ("evidence-path", "Evidence path", ScalarKind.STRING),
        ("evidence-line", "Evidence line", ScalarKind.INTEGER),
    )
    return TableProjection(
        **_table_base(
            structure_report,
            ProjectionName.CONVERSION_CHURN,
            selection,
            "Evidenced conversion and serialization sites",
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
            TableSort(column_id="owner"),
            TableSort(column_id="kind"),
            TableSort(column_id="target"),
            TableSort(column_id="evidence-path"),
            TableSort(column_id="evidence-line"),
        ),
    )


def _review_packet_projection(
    report: Report, selection: ProjectionSelection
) -> ReviewPacketProjection:
    items: list[ReviewItem] = []
    for finding in report.findings:
        related_ids = tuple(
            sorted(
                {
                    *finding.subject_ids,
                    *finding.affected_contract_ids,
                    *finding.affected_shape_ids,
                    *finding.affected_structure_ids,
                }
            )
        )
        items.append(
            ReviewItem(
                item_id=finding.finding_id,
                title=finding.title,
                statement=finding.explanation,
                evidence_ids=tuple(sorted({_evidence_identity(item) for item in finding.evidence})),
                counter_evidence_ids=tuple(
                    sorted({_evidence_identity(item) for item in finding.counter_evidence})
                ),
                related_ids=related_ids,
                review_questions=finding.likely_validation_needs,
            )
        )
    for candidate in report.candidate_classifications:
        items.append(
            ReviewItem(
                item_id=candidate.candidate_id,
                title=f"Review {candidate.candidate.value} classification",
                statement=(
                    "The retained static evidence supports this representation classification; "
                    "it does not authorize a source change."
                ),
                evidence_ids=tuple(
                    sorted({_evidence_identity(item) for item in candidate.supporting_evidence})
                ),
                counter_evidence_ids=tuple(
                    sorted({_evidence_identity(item) for item in candidate.counter_evidence})
                ),
                related_ids=(candidate.subject_id,),
                review_questions=(
                    "Does evidence outside the static proof boundary contradict "
                    "this classification?",
                ),
            )
        )
    items.sort(key=lambda item: item.item_id)
    if not items:
        raise ProjectionBuildError("review-packet requires recorded findings or candidate evidence")
    if selection.selected_ids:
        selectable = {identity for item in items for identity in (item.item_id, *item.related_ids)}
        missing = sorted(set(selection.selected_ids) - selectable)
        if missing:
            raise ProjectionBuildError("unknown review identity: " + ", ".join(missing))
        selected = set(selection.selected_ids)
        items = [
            item
            for item in items
            if item.item_id in selected or selected.intersection(item.related_ids)
        ]
    truncated = len(items) > selection.max_records
    bounded = tuple(items[: selection.max_records])
    source_input = _report_input(report)
    limitations = set(_limitations(report, truncated))
    limitations.add("review items are evidence summaries, not refactoring instructions")
    return ReviewPacketProjection(
        projection_id=_projection_id(source_input, ProjectionName.REVIEW_PACKET, selection),
        title="Bounded refactoring evidence review",
        inputs=(source_input,),
        selection=selection,
        completeness=_projection_state(report, truncated),
        record_count=len(bounded),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=tuple(sorted(limitations)),
        items=bounded,
    )
