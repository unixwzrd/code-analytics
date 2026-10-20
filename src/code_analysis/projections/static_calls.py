"""Saved-report-only bounded static call-site table and containment graph."""

from __future__ import annotations

from collections import defaultdict

from code_analysis.models import Completeness
from code_analysis.projection_models import (
    ColumnAlignment,
    EdgeDirection,
    GraphCluster,
    GraphEdge,
    GraphNode,
    GraphProjection,
    ProjectionScalar,
    ProjectionSelection,
    ScalarKind,
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
from code_analysis.report_models import AnalysisReport, SectionState
from code_analysis.static_call_models import (
    StaticCallOccurrence,
    StaticCallScope,
    StaticCallScopeKind,
    StaticCallSourceCoverageState,
)

Report = AnalysisReport
_STATIC_CALL_SECTION = "static-call-occurrences"

_TABLE_COLUMNS: tuple[tuple[str, str, ScalarKind], ...] = (
    ("occurrence-id", "Occurrence ID", ScalarKind.STRING),
    ("language", "Language", ScalarKind.STRING),
    ("module", "Module", ScalarKind.STRING),
    ("logical-path", "Logical path", ScalarKind.STRING),
    ("start-line", "Start line", ScalarKind.INTEGER),
    ("start-column", "Start column", ScalarKind.INTEGER),
    ("end-line", "End line", ScalarKind.INTEGER),
    ("end-column", "End column", ScalarKind.INTEGER),
    ("callee-spelling", "Callee spelling", ScalarKind.STRING),
    ("syntax-kind", "Syntax kind", ScalarKind.STRING),
    ("evaluation-context", "Evaluation context", ScalarKind.STRING),
    ("scope-path", "Scope path", ScalarKind.STRING),
    ("conditional", "Conditional", ScalarKind.BOOLEAN),
    ("deferred", "Deferred", ScalarKind.BOOLEAN),
    ("evaluation-unknown", "Evaluation unknown", ScalarKind.BOOLEAN),
    ("enclosing-subject", "Enclosing subject", ScalarKind.STRING),
)


def _static_call_section(report: Report) -> SectionState | None:
    for item in report.section_coverage:
        if item.section == _STATIC_CALL_SECTION:
            return item.state
    return None


def _require_static_call_section(report: Report) -> SectionState:
    state = _static_call_section(report)
    if state is None:
        raise ProjectionBuildError(
            "static-call projections require a static-call-occurrences section entry"
        )
    if state is SectionState.DISABLED:
        raise ProjectionBuildError("static-call-occurrences section is disabled")
    if state is SectionState.UNAVAILABLE:
        raise ProjectionBuildError("static-call-occurrences section is unavailable")
    if state is SectionState.FAILED and not report.static_call_occurrences:
        raise ProjectionBuildError(
            "static-call-occurrences section failed without retained call-site rows"
        )
    return state


def _format_scope_path(scopes: tuple[StaticCallScope, ...]) -> str:
    parts: list[str] = []
    for scope in scopes:
        if scope.kind is StaticCallScopeKind.MODULE:
            parts.append("module")
        elif scope.name:
            parts.append(f"{scope.kind.value}:{scope.name}")
        else:
            parts.append(scope.kind.value)
    return " > ".join(parts)


def _known_selection_ids(report: Report) -> set[str]:
    known = {item.occurrence_id for item in report.static_call_occurrences}
    known.update(item.source_file_id for item in report.source_files)
    known.update(item.subject_id for item in report.subjects)
    known.update(item.name for item in report.modules)
    return known


def _occurrence_matches_selection(occurrence: StaticCallOccurrence, selected: set[str]) -> bool:
    return (
        occurrence.occurrence_id in selected
        or occurrence.source_file_id in selected
        or occurrence.module_name in selected
        or (
            occurrence.enclosing_subject_id is not None
            and occurrence.enclosing_subject_id in selected
        )
    )


def _validate_selected_ids(report: Report, selection: ProjectionSelection) -> None:
    if not selection.selected_ids:
        return
    missing = set(selection.selected_ids) - _known_selection_ids(report)
    if missing:
        raise ProjectionBuildError(
            "selected static call identities are absent: " + ", ".join(sorted(missing))
        )


def _reject_graph_module_subject_selection(report: Report, selection: ProjectionSelection) -> None:
    if not selection.selected_ids:
        return
    modules = {item.name for item in report.modules}
    subjects = {item.subject_id for item in report.subjects}
    for identity in selection.selected_ids:
        if identity in modules:
            raise ProjectionBuildError(
                "static-call-graph selection accepts source-file and call-occurrence node "
                f"IDs only; module identity {identity} is not supported"
            )
        if identity in subjects:
            raise ProjectionBuildError(
                "static-call-graph selection accepts source-file and call-occurrence node "
                f"IDs only; enclosing-subject identity {identity} is not supported"
            )


def _selected_occurrences(
    report: Report,
    selection: ProjectionSelection,
    *,
    graph: bool,
) -> tuple[StaticCallOccurrence, ...]:
    _validate_selected_ids(report, selection)
    if graph:
        _reject_graph_module_subject_selection(report, selection)
    occurrences = list(report.static_call_occurrences)
    if selection.languages:
        occurrences = [item for item in occurrences if item.language in selection.languages]
        if not occurrences and report.static_call_occurrences:
            raise ProjectionBuildError(
                "static-call projections have no occurrences for the selected languages"
            )
    if selection.selected_ids:
        selected = set(selection.selected_ids)
        occurrences = [
            item for item in occurrences if _occurrence_matches_selection(item, selected)
        ]
        if not occurrences:
            raise ProjectionBuildError(
                "selected static call identities are absent from the filtered view"
            )
    return tuple(occurrences)


def _static_call_limitations(
    report: Report,
    section_state: SectionState,
    truncated: bool,
) -> tuple[str, ...]:
    limitations = set(_limitations(report, truncated))
    limitations.add(f"Static-call-occurrences section state: {section_state.value}.")
    for row in report.static_call_coverage:
        limitations.update(row.limitations)
        if row.state is not StaticCallSourceCoverageState.COMPLETE:
            limitations.add(
                f"Source {row.source_file_id} static call coverage state: {row.state.value}."
            )
    for occurrence in report.static_call_occurrences:
        limitations.update(occurrence.limitations)
    limitations.update(
        {
            "Each row is one saved syntactic invocation site; callee spellings are nullable "
            "and do not prove dispatch, binding, or runtime targets.",
            "Scope paths and enclosing subjects are recorded navigation anchors only.",
            "This projection never invents call targets or merges repeated sites.",
        }
    )
    return tuple(sorted(limitations))


def _static_call_completeness(
    report: Report,
    section_state: SectionState,
    truncated: bool,
) -> Completeness:
    completeness = _projection_state(report, truncated)
    if section_state is not SectionState.COMPLETE and completeness is Completeness.COMPLETE:
        completeness = Completeness.PARTIAL
    if completeness is Completeness.COMPLETE and any(
        row.state is not StaticCallSourceCoverageState.COMPLETE
        for row in report.static_call_coverage
    ):
        completeness = Completeness.PARTIAL
    return completeness


def _occurrence_sort_key(occurrence: StaticCallOccurrence) -> tuple[object, ...]:
    span = occurrence.evidence[0].span
    return (
        occurrence.language.value,
        span.path,
        span.start_line,
        span.start_column,
        span.end_line,
        span.end_column,
        occurrence.occurrence_id,
    )


def _occurrence_row(occurrence: StaticCallOccurrence) -> TableRow:
    span = occurrence.evidence[0].span
    evidence_id = _evidence_identity(occurrence.evidence[0])
    return TableRow(
        row_id=occurrence.occurrence_id,
        cells=(
            _string(occurrence.occurrence_id),
            _string(occurrence.language.value),
            _string(occurrence.module_name),
            _string(span.path),
            _integer(span.start_line),
            _integer(span.start_column),
            _integer(span.end_line),
            _integer(span.end_column),
            (
                _string(occurrence.callee_spelling)
                if occurrence.callee_spelling is not None
                else ProjectionScalar(kind=ScalarKind.NULL)
            ),
            _string(occurrence.syntax_kind.value),
            _string(occurrence.evaluation_context.value),
            _string(_format_scope_path(occurrence.scope_path)),
            _boolean(occurrence.conditional),
            _boolean(occurrence.deferred),
            _boolean(occurrence.evaluation_unknown),
            (
                _string(occurrence.enclosing_subject_id)
                if occurrence.enclosing_subject_id is not None
                else ProjectionScalar(kind=ScalarKind.NULL)
            ),
        ),
        evidence_ids=(evidence_id,),
    )


def _static_call_sites_projection(
    report: Report, selection: ProjectionSelection
) -> TableProjection:
    section_state = _require_static_call_section(report)
    occurrences = _selected_occurrences(report, selection, graph=False)
    rows = [_occurrence_row(item) for item in sorted(occurrences, key=_occurrence_sort_key)]
    bounded, truncated = _bounded_rows(rows, selection)
    base = _table_base(
        report,
        ProjectionName.STATIC_CALL_SITES,
        selection,
        "Recorded static call sites (syntactic occurrences)",
        len(bounded),
        truncated,
    )
    base["completeness"] = _static_call_completeness(report, section_state, truncated)
    base["limitations"] = _static_call_limitations(report, section_state, truncated)
    return TableProjection(
        **base,
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
            for position, (column_id, label, value_kind) in enumerate(_TABLE_COLUMNS)
        ),
        rows=bounded,
        sort=tuple(
            TableSort(column_id=name)
            for name in (
                "language",
                "logical-path",
                "start-line",
                "start-column",
                "end-line",
                "end-column",
                "occurrence-id",
            )
        ),
    )


def _static_call_graph_projection(
    report: Report, selection: ProjectionSelection
) -> GraphProjection:
    section_state = _require_static_call_section(report)
    occurrences = _selected_occurrences(report, selection, graph=True)
    sources = {item.source_file_id: item for item in report.source_files}
    nodes: dict[str, GraphNode] = {}
    edges: dict[str, GraphEdge] = {}
    for occurrence in occurrences:
        source = sources[occurrence.source_file_id]
        evidence_id = _evidence_identity(occurrence.evidence[0])
        if occurrence.source_file_id not in nodes:
            file_evidence = sorted(
                {
                    _evidence_identity(item.evidence[0])
                    for item in occurrences
                    if item.source_file_id == occurrence.source_file_id
                }
            )
            nodes[occurrence.source_file_id] = GraphNode(
                node_id=occurrence.source_file_id,
                label=source.logical_path,
                kind="source-file",
                group_id=occurrence.module_name,
                evidence_ids=tuple(file_evidence),
            )
        label = (
            occurrence.callee_spelling
            if occurrence.callee_spelling is not None
            else "dynamic callee"
        )
        nodes[occurrence.occurrence_id] = GraphNode(
            node_id=occurrence.occurrence_id,
            label=label,
            kind="call-site",
            group_id=occurrence.module_name,
            evidence_ids=(evidence_id,),
        )
        identity = _edge_id(
            occurrence.source_file_id, occurrence.occurrence_id, "source-contains-call-site"
        )
        edges[identity] = GraphEdge(
            edge_id=identity,
            source_id=occurrence.source_file_id,
            target_id=occurrence.occurrence_id,
            kind="source-contains-call-site",
            label="source contains call site",
            evidence_ids=(evidence_id,),
        )
    if selection.selected_ids:
        known_nodes = set(nodes)
        missing = set(selection.selected_ids) - known_nodes
        if missing:
            raise ProjectionBuildError(
                "selected static-call-graph identities are absent: " + ", ".join(sorted(missing))
            )
    groups: dict[str, list[str]] = defaultdict(list)
    for node in nodes.values():
        if node.group_id is not None:
            groups[node.group_id].append(node.node_id)
    source = _report_input(report)
    limitations = set(_static_call_limitations(report, section_state, False))
    limitations.update(
        {
            "Edges are syntactic source-file containment only; they are not caller-to-callee "
            "dispatch or runtime invocation relationships.",
            "Repeated invocations remain distinct occurrence nodes; no name matching or target "
            "invention is applied.",
            "Nullable callee labels use the recorded spelling when present and otherwise read "
            "'dynamic callee' without implying a resolved target.",
        }
    )
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.STATIC_CALL_GRAPH, selection),
        title="Static call-site containment graph (not dispatch)",
        inputs=(source,),
        selection=selection,
        completeness=_static_call_completeness(report, section_state, False),
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
