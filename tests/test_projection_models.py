from __future__ import annotations

import pytest
from pydantic import ValidationError

from code_analysis.models import Completeness
from code_analysis.projection_models import (
    PROJECTION_ADAPTER,
    ComparisonChangeCounts,
    ComparisonProjection,
    EdgeDirection,
    GraphEdge,
    GraphNode,
    GraphProjection,
    HierarchyNode,
    HierarchyProjection,
    MatrixAxisItem,
    MatrixCell,
    MatrixProjection,
    ProjectionFindingCategory,
    ProjectionFindingSeverity,
    ProjectionInput,
    ProjectionInputRole,
    ProjectionMetric,
    ProjectionScalar,
    ProjectionSelection,
    ScalarKind,
    SequenceEvent,
    SequenceParticipant,
    SequenceProjection,
    SortDirection,
    StateNode,
    StateProjection,
    StateTransition,
    TableColumn,
    TableProjection,
    TableRow,
    TableSort,
)
from code_analysis.reports.projection_json import (
    load_projection_json,
    render_projection_json,
)


def _source(role: ProjectionInputRole = ProjectionInputRole.SOURCE) -> ProjectionInput:
    return ProjectionInput(
        role=role,
        report_schema_name="code-analysis-report",
        report_schema_version="1.0.0",
        report_sha256="1" * 64,
    )


def _base(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "projection_id": "projection-" + "1" * 20,
        "title": "Example projection",
        "inputs": (_source(),),
        "completeness": Completeness.COMPLETE,
        "record_count": 0,
    }
    values.update(overrides)
    return values


def _string(value: str) -> ProjectionScalar:
    return ProjectionScalar(kind=ScalarKind.STRING, string_value=value)


def _integer(value: int) -> ProjectionScalar:
    return ProjectionScalar(kind=ScalarKind.INTEGER, integer_value=value)


def _null() -> ProjectionScalar:
    return ProjectionScalar(kind=ScalarKind.NULL)


def test_projection_scalar_is_typed_strict_and_frozen() -> None:
    scalar = _integer(3)
    assert scalar.integer_value == 3
    with pytest.raises(ValidationError):
        ProjectionScalar(kind=ScalarKind.INTEGER, integer_value=True)
    with pytest.raises(ValidationError, match="exactly its declared value"):
        ProjectionScalar(kind=ScalarKind.STRING, integer_value=3)
    with pytest.raises(ValidationError):
        scalar.integer_value = 4


def test_projection_finding_filters_are_strict_sorted_and_portable() -> None:
    selection = ProjectionSelection(
        module_prefixes=("package", "package.module"),
        finding_categories=(ProjectionFindingCategory.DUPLICATION,),
        finding_severities=(ProjectionFindingSeverity.WARNING,),
        finding_rule_ids=("duplication.normalized-ast",),
    )
    assert selection.module_prefixes == ("package", "package.module")
    with pytest.raises(ValidationError, match="dotted module names"):
        ProjectionSelection(module_prefixes=("package.*",))
    with pytest.raises(ValidationError, match="nonempty and trimmed"):
        ProjectionSelection(finding_rule_ids=(" rule",))
    with pytest.raises(ValidationError, match="unique and sorted"):
        ProjectionSelection(module_prefixes=("package.module", "package"))
    with pytest.raises(ValidationError):
        ProjectionSelection(finding_categories=("duplication",))


def test_table_projection_validates_columns_rows_and_round_trip() -> None:
    projection = TableProjection(
        **_base(record_count=1),
        columns=(
            TableColumn(
                column_id="module",
                label="Module",
                position=0,
                value_kind=ScalarKind.STRING,
            ),
            TableColumn(
                column_id="fan-in",
                label="Fan in",
                position=1,
                value_kind=ScalarKind.INTEGER,
            ),
        ),
        rows=(TableRow(row_id="forge.api", cells=(_string("forge.api"), _integer(4))),),
        sort=(TableSort(column_id="fan-in", direction=SortDirection.DESCENDING),),
    )
    encoded = PROJECTION_ADAPTER.dump_json(projection)
    assert PROJECTION_ADAPTER.validate_json(encoded) == projection
    rendered = render_projection_json(projection)
    assert load_projection_json(rendered) == projection
    assert render_projection_json(load_projection_json(rendered)) == rendered
    assert render_projection_json(projection, compact=True).endswith("\n")
    with pytest.raises(ValidationError, match="cell kind"):
        TableProjection(
            **_base(record_count=1),
            columns=(
                TableColumn(
                    column_id="module",
                    label="Module",
                    position=0,
                    value_kind=ScalarKind.STRING,
                ),
            ),
            rows=(TableRow(row_id="forge.api", cells=(_integer(4),)),),
        )


def test_table_projection_enforces_declared_rank_order() -> None:
    columns = (
        TableColumn(
            column_id="module",
            label="Module",
            position=0,
            value_kind=ScalarKind.STRING,
        ),
        TableColumn(
            column_id="score",
            label="Score",
            position=1,
            value_kind=ScalarKind.INTEGER,
        ),
    )
    rows = (
        TableRow(row_id="b", cells=(_string("b"), _integer(9))),
        TableRow(row_id="a", cells=(_string("a"), _integer(3))),
    )
    projection = TableProjection(
        **_base(record_count=2),
        columns=columns,
        rows=rows,
        sort=(TableSort(column_id="score", direction=SortDirection.DESCENDING),),
    )
    assert projection.rows == rows
    with pytest.raises(ValidationError, match="stable sort"):
        TableProjection(
            **_base(record_count=2),
            columns=columns,
            rows=tuple(reversed(rows)),
            sort=(TableSort(column_id="score", direction=SortDirection.DESCENDING),),
        )


def test_sparse_matrix_projection_validates_axes_cells_and_round_trip() -> None:
    axes = (
        MatrixAxisItem(item_id="a", label="A"),
        MatrixAxisItem(item_id="b", label="B"),
    )
    cell = MatrixCell(
        cell_id="a:b",
        row_id="a",
        column_id="b",
        metrics=(ProjectionMetric(name="shared-calls", value=_integer(2)),),
        evidence_ids=("evidence-a",),
    )
    projection = MatrixProjection(
        **_base(record_count=1),
        rows=axes,
        columns=axes,
        cells=(cell,),
        symmetric=True,
    )
    encoded = PROJECTION_ADAPTER.dump_json(projection)
    assert PROJECTION_ADAPTER.validate_json(encoded) == projection
    with pytest.raises(ValidationError, match="endpoint"):
        MatrixProjection(
            **_base(record_count=1),
            rows=(axes[0],),
            columns=(axes[0],),
            cells=(cell,),
        )
    with pytest.raises(ValidationError, match="identical"):
        MatrixProjection(
            **_base(),
            rows=(axes[0],),
            columns=(axes[1],),
            symmetric=True,
        )


def test_graph_projection_rejects_missing_endpoints_and_unstable_order() -> None:
    nodes = (
        GraphNode(node_id="a", label="A", kind="module"),
        GraphNode(node_id="b", label="B", kind="module"),
    )
    edge = GraphEdge(edge_id="a-b", source_id="a", target_id="b", kind="import")
    graph = GraphProjection(
        **_base(record_count=3),
        direction=EdgeDirection.DIRECTED,
        nodes=nodes,
        edges=(edge,),
    )
    assert graph.record_count == 3
    with pytest.raises(ValidationError, match="unique and sorted"):
        GraphProjection(**_base(record_count=2), nodes=tuple(reversed(nodes)))
    with pytest.raises(ValidationError, match="endpoint"):
        GraphProjection(
            **_base(record_count=2),
            nodes=(nodes[0],),
            edges=(edge,),
        )
    with pytest.raises(ValidationError, match="unknown cluster"):
        GraphProjection(
            **_base(record_count=1),
            nodes=(
                GraphNode(
                    node_id="a",
                    label="A",
                    kind="module",
                    group_id="missing",
                ),
            ),
        )


def test_hierarchy_projection_rejects_cycles() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        HierarchyProjection(
            **_base(record_count=2),
            nodes=(
                HierarchyNode(node_id="a", parent_id="b", label="A", kind="module"),
                HierarchyNode(node_id="b", parent_id="a", label="B", kind="module"),
            ),
        )


def test_sequence_projection_requires_ordered_events_and_known_participants() -> None:
    participants = (
        SequenceParticipant(participant_id="consumer", label="Consumer", kind="symbol"),
        SequenceParticipant(participant_id="producer", label="Producer", kind="symbol"),
    )
    event = SequenceEvent(
        event_id="produce-consume",
        position=0,
        source_id="producer",
        target_id="consumer",
        label="payload",
    )
    sequence = SequenceProjection(
        **_base(record_count=1), participants=participants, events=(event,)
    )
    assert sequence.events == (event,)
    with pytest.raises(ValidationError, match="participant"):
        SequenceProjection(
            **_base(record_count=1),
            participants=participants,
            events=(
                SequenceEvent(
                    event_id="missing",
                    position=0,
                    source_id="unknown",
                    target_id="consumer",
                    label="payload",
                ),
            ),
        )


def test_state_projection_requires_one_initial_state_and_known_transitions() -> None:
    states = (
        StateNode(state_id="created", label="Created", initial=True),
        StateNode(state_id="ready", label="Ready", terminal=True),
    )
    transition = StateTransition(
        transition_id="created-ready",
        source_id="created",
        target_id="ready",
        label="validate",
    )
    projection = StateProjection(**_base(record_count=3), states=states, transitions=(transition,))
    assert projection.states[0].initial
    with pytest.raises(ValidationError, match="exactly one"):
        StateProjection(
            **_base(record_count=2),
            states=(
                StateNode(state_id="a", label="A"),
                StateNode(state_id="b", label="B"),
            ),
        )


def test_comparison_projection_requires_bound_baseline_and_candidate() -> None:
    with pytest.raises(ValidationError, match="baseline and candidate"):
        ComparisonProjection(
            **_base(),
            identical=False,
            includes_unchanged=False,
            change_counts=ComparisonChangeCounts(added=0, removed=0, changed=0, unchanged=0),
            eligible_record_count=0,
            records=(),
        )
    comparison = ComparisonProjection(
        **_base(
            inputs=(
                _source(ProjectionInputRole.BASELINE),
                ProjectionInput(
                    role=ProjectionInputRole.CANDIDATE,
                    report_schema_name="code-analysis-report",
                    report_schema_version="1.0.0",
                    report_sha256="2" * 64,
                ),
            )
        ),
        identical=False,
        includes_unchanged=False,
        change_counts=ComparisonChangeCounts(added=0, removed=0, changed=0, unchanged=0),
        eligible_record_count=0,
        records=(),
    )
    assert comparison.inputs[1].role is ProjectionInputRole.CANDIDATE
    with pytest.raises(ValidationError, match="identical state"):
        comparison.model_copy(update={"identical": True}).model_validate(
            comparison.model_copy(update={"identical": True}).model_dump()
        )
    assert _null().kind is ScalarKind.NULL


def test_projection_models_forbid_unknown_fields_and_bool_counts() -> None:
    with pytest.raises(ValidationError):
        TableProjection(**_base(record_count=True), columns=())
    with pytest.raises(ValidationError):
        ProjectionInput.model_validate({**_source().model_dump(), "unknown": True})
    with pytest.raises(ValidationError, match="exactly one source"):
        TableProjection(
            **_base(inputs=(_source(ProjectionInputRole.BASELINE),)),
            columns=(
                TableColumn(
                    column_id="module",
                    label="Module",
                    position=0,
                    value_kind=ScalarKind.STRING,
                ),
            ),
        )


def test_projection_loader_rejects_unknown_schema_or_kind() -> None:
    projection = TableProjection(
        **_base(),
        columns=(
            TableColumn(
                column_id="module",
                label="Module",
                position=0,
                value_kind=ScalarKind.STRING,
            ),
        ),
    )
    payload = projection.model_dump_json()
    with pytest.raises(ValidationError):
        load_projection_json(payload.replace('"1.0.0"', '"99.0.0"', 1))
    with pytest.raises(ValidationError):
        load_projection_json(payload.replace('"table"', '"unknown"', 1))
