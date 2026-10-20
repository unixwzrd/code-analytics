"""Strict persisted contracts for report-only tables and diagrams."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    TypeAdapter,
    model_validator,
)

from code_analysis.models import Completeness, Confidence, SourceLanguage, StrictModel
from code_analysis.report_models import RefactorCandidateKind

PROJECTION_SCHEMA_NAME: Literal["code-analysis-projection"] = "code-analysis-projection"
PROJECTION_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"


class ProjectionInputRole(StrEnum):
    SOURCE = "source"
    BASELINE = "baseline"
    CANDIDATE = "candidate"
    DECISION_LEDGER = "decision-ledger"


class ScalarKind(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    NULL = "null"


class ColumnAlignment(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    CENTER = "center"


class SortDirection(StrEnum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


class EdgeDirection(StrEnum):
    DIRECTED = "directed"
    UNDIRECTED = "undirected"


class EdgeResolution(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    TRUNCATED = "truncated"


class ComparisonChange(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class ProjectionCollapse(StrEnum):
    NONE = "none"
    MODULE = "module"
    PACKAGE = "package"


class ProjectionFindingCategory(StrEnum):
    COMPLEXITY = "complexity"
    ORCHESTRATION = "orchestration"
    IMPORT_CYCLE = "import-cycle"
    CONTRACT_CONFLICT = "contract-conflict"
    SHAPE_CONFLICT = "shape-conflict"
    COERCION_RISK = "coercion-risk"
    VALIDATION_SPRAWL = "validation-sprawl"
    OWNERSHIP_RISK = "ownership-risk"
    LIFECYCLE_RISK = "lifecycle-risk"
    CONVERSION_CHURN = "conversion-churn"
    GROWTH_RISK = "growth-risk"
    CONTRACT_CANDIDATE = "contract-candidate"
    DUPLICATION = "duplication"
    COMMONALITY = "commonality"
    STATE_COUPLING = "state-coupling"
    SIDE_EFFECT_OWNERSHIP = "side-effect-ownership"
    COHESION = "cohesion"
    EXTRACTION_CANDIDATE = "extraction-candidate"
    MODULE_COMBINATION = "module-combination"
    ARCHITECTURE_CONTRACT = "architecture-contract"


class ProjectionFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ProjectionInput(StrictModel):
    role: ProjectionInputRole
    report_schema_name: StrictStr = Field(min_length=1)
    report_schema_version: StrictStr = Field(min_length=1)
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    run_identity_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ProjectionSelection(StrictModel):
    selected_ids: tuple[StrictStr, ...] = ()
    edge_kinds: tuple[StrictStr, ...] = ()
    module_prefixes: tuple[StrictStr, ...] = ()
    finding_categories: tuple[ProjectionFindingCategory, ...] = ()
    finding_severities: tuple[ProjectionFindingSeverity, ...] = ()
    finding_rule_ids: tuple[StrictStr, ...] = ()
    languages: tuple[SourceLanguage, ...] = ()
    candidate_module_patterns: tuple[StrictStr, ...] = ()
    candidate_exclude_patterns: tuple[StrictStr, ...] = ()
    candidate_kinds: tuple[RefactorCandidateKind, ...] = ()
    candidate_states: tuple[StrictStr, ...] = ()
    candidate_decisions: tuple[StrictStr, ...] = ()
    cross_module_only: StrictBool = False
    production_only: StrictBool = False
    min_shared_dimensions: StrictInt = Field(default=0, ge=0, le=32)
    prefer_existing_owner: StrictBool = False
    max_records: StrictInt = Field(default=500, ge=1, le=100_000)
    max_nodes: StrictInt = Field(default=100, ge=1, le=100_000)
    max_edges: StrictInt = Field(default=250, ge=0, le=250_000)
    collapse: ProjectionCollapse = ProjectionCollapse.NONE
    after_id: StrictStr | None = None
    depth: StrictInt | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        for label, values in (
            ("selected IDs", self.selected_ids),
            ("edge kinds", self.edge_kinds),
            ("module prefixes", self.module_prefixes),
            ("finding categories", tuple(item.value for item in self.finding_categories)),
            ("finding severities", tuple(item.value for item in self.finding_severities)),
            ("finding rule IDs", self.finding_rule_ids),
            ("languages", tuple(item.value for item in self.languages)),
            ("candidate module patterns", self.candidate_module_patterns),
            ("candidate exclude patterns", self.candidate_exclude_patterns),
            ("candidate kinds", tuple(item.value for item in self.candidate_kinds)),
            ("candidate states", self.candidate_states),
            ("candidate decisions", self.candidate_decisions),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"projection {label} must be unique and sorted")
        for prefix in self.module_prefixes:
            if not all(part.isidentifier() for part in prefix.split(".")):
                raise ValueError("projection module prefixes must be dotted module names")
        if any(
            not rule_id.strip() or rule_id != rule_id.strip() for rule_id in self.finding_rule_ids
        ):
            raise ValueError("projection finding rule IDs must be nonempty and trimmed")
        for pattern in (*self.candidate_module_patterns, *self.candidate_exclude_patterns):
            if (
                not pattern
                or pattern.startswith("/")
                or "\\" in pattern
                or any(part == ".." for part in pattern.split("/"))
            ):
                raise ValueError("candidate module patterns must be portable logical patterns")
        return self


class ProjectionScalar(StrictModel):
    kind: ScalarKind
    string_value: StrictStr | None = None
    integer_value: StrictInt | None = None
    number_value: StrictFloat | None = None
    boolean_value: StrictBool | None = None

    @model_validator(mode="after")
    def validate_exact_value(self) -> Self:
        present = {
            ScalarKind.STRING: self.string_value is not None,
            ScalarKind.INTEGER: self.integer_value is not None,
            ScalarKind.NUMBER: self.number_value is not None,
            ScalarKind.BOOLEAN: self.boolean_value is not None,
        }
        if self.kind is ScalarKind.NULL:
            if any(present.values()):
                raise ValueError("null projection scalar cannot contain a value")
            return self
        if not present[self.kind] or sum(present.values()) != 1:
            raise ValueError("projection scalar must contain exactly its declared value")
        return self


def _scalar_sort_value(value: ProjectionScalar) -> str | int | float | bool:
    if value.kind is ScalarKind.STRING:
        assert value.string_value is not None
        return value.string_value
    if value.kind is ScalarKind.INTEGER:
        assert value.integer_value is not None
        return value.integer_value
    if value.kind is ScalarKind.NUMBER:
        assert value.number_value is not None
        return value.number_value
    if value.kind is ScalarKind.BOOLEAN:
        assert value.boolean_value is not None
        return value.boolean_value
    raise ValueError("null projection scalar has no sort value")


class ProjectionMetric(StrictModel):
    name: StrictStr = Field(min_length=1)
    value: ProjectionScalar
    unit: StrictStr | None = None


class ProjectionBase(StrictModel):
    schema_name: Literal["code-analysis-projection"] = PROJECTION_SCHEMA_NAME
    schema_version: Literal["1.0.0"] = PROJECTION_SCHEMA_VERSION
    projection_id: StrictStr = Field(pattern=r"^projection-[0-9a-f]{20}$")
    title: StrictStr = Field(min_length=1)
    inputs: tuple[ProjectionInput, ...] = Field(min_length=1, max_length=3)
    selection: ProjectionSelection = ProjectionSelection()
    completeness: Completeness
    record_count: StrictInt = Field(ge=0)
    truncated: StrictBool = False
    truncation_reason: StrictStr | None = None
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_common_contract(self) -> Self:
        roles = [item.role for item in self.inputs]
        if len(roles) != len(set(roles)):
            raise ValueError("projection input roles must be unique")
        kind = getattr(self, "projection_kind", None)
        paired_table = kind == "table" and roles == [
            ProjectionInputRole.BASELINE,
            ProjectionInputRole.CANDIDATE,
        ]
        if (
            kind != "comparison"
            and not paired_table
            and roles
            not in (
                [ProjectionInputRole.SOURCE],
                [ProjectionInputRole.SOURCE, ProjectionInputRole.DECISION_LEDGER],
            )
        ):
            raise ValueError(
                "non-comparison projection requires exactly one source and an optional "
                "decision-ledger input"
            )
        if self.truncated != (self.truncation_reason is not None):
            raise ValueError("projection truncation requires exactly one reason state")
        if list(self.limitations) != sorted(self.limitations) or len(self.limitations) != len(
            set(self.limitations)
        ):
            raise ValueError("projection limitations must be unique and sorted")
        return self


class TableColumn(StrictModel):
    column_id: StrictStr = Field(pattern=r"^[a-z][a-z0-9-]*$")
    label: StrictStr = Field(min_length=1)
    position: StrictInt = Field(ge=0)
    value_kind: ScalarKind
    alignment: ColumnAlignment = ColumnAlignment.LEFT


class TableRow(StrictModel):
    row_id: StrictStr = Field(min_length=1)
    cells: tuple[ProjectionScalar, ...]
    evidence_ids: tuple[StrictStr, ...] = ()


class TableSort(StrictModel):
    column_id: StrictStr = Field(pattern=r"^[a-z][a-z0-9-]*$")
    direction: SortDirection = SortDirection.ASCENDING


class TableProjection(ProjectionBase):
    projection_kind: Literal["table"] = "table"
    columns: tuple[TableColumn, ...] = Field(min_length=1)
    rows: tuple[TableRow, ...] = ()
    sort: tuple[TableSort, ...] = ()

    @model_validator(mode="after")
    def validate_table(self) -> Self:
        column_ids = [column.column_id for column in self.columns]
        positions = [column.position for column in self.columns]
        if len(column_ids) != len(set(column_ids)):
            raise ValueError("table column IDs must be unique")
        if positions != list(range(len(self.columns))):
            raise ValueError("table column positions must be contiguous")
        row_ids = [row.row_id for row in self.rows]
        if len(row_ids) != len(set(row_ids)):
            raise ValueError("table row IDs must be unique")
        if any(len(row.cells) != len(self.columns) for row in self.rows):
            raise ValueError("table row cell count must match columns")
        for row in self.rows:
            for column, cell in zip(self.columns, row.cells, strict=True):
                if cell.kind is not ScalarKind.NULL and cell.kind is not column.value_kind:
                    raise ValueError("table cell kind must match its column")
        sort_ids = [item.column_id for item in self.sort]
        if len(sort_ids) != len(set(sort_ids)) or not set(sort_ids) <= set(column_ids):
            raise ValueError("table sort columns must be unique and declared")
        expected = sorted(self.rows, key=lambda row: row.row_id)
        positions_by_id = {column.column_id: column.position for column in self.columns}
        for item in reversed(self.sort):
            position = positions_by_id[item.column_id]
            if any(row.cells[position].kind is ScalarKind.NULL for row in expected):
                raise ValueError("table sort columns cannot contain null values")
            expected = sorted(
                expected,
                key=lambda row: _scalar_sort_value(row.cells[position]),
                reverse=item.direction is SortDirection.DESCENDING,
            )
        if list(self.rows) != expected:
            raise ValueError("table rows do not match the declared stable sort")
        if self.record_count != len(self.rows):
            raise ValueError("table record count must match rows")
        return self


class MatrixAxisItem(StrictModel):
    item_id: StrictStr = Field(min_length=1)
    label: StrictStr = Field(min_length=1)
    group_id: StrictStr | None = None


class MatrixCell(StrictModel):
    cell_id: StrictStr = Field(min_length=1)
    row_id: StrictStr = Field(min_length=1)
    column_id: StrictStr = Field(min_length=1)
    metrics: tuple[ProjectionMetric, ...] = Field(min_length=1)
    evidence_ids: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_metrics(self) -> Self:
        names = [metric.name for metric in self.metrics]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("matrix cell metrics must be unique and sorted")
        if list(self.evidence_ids) != sorted(self.evidence_ids) or len(self.evidence_ids) != len(
            set(self.evidence_ids)
        ):
            raise ValueError("matrix cell evidence IDs must be unique and sorted")
        return self


class MatrixProjection(ProjectionBase):
    projection_kind: Literal["matrix"] = "matrix"
    rows: tuple[MatrixAxisItem, ...] = ()
    columns: tuple[MatrixAxisItem, ...] = ()
    cells: tuple[MatrixCell, ...] = ()
    symmetric: StrictBool = False

    @model_validator(mode="after")
    def validate_matrix(self) -> Self:
        row_ids = [item.item_id for item in self.rows]
        column_ids = [item.item_id for item in self.columns]
        cell_ids = [item.cell_id for item in self.cells]
        for label, identities in (
            ("rows", row_ids),
            ("columns", column_ids),
            ("cells", cell_ids),
        ):
            if identities != sorted(identities) or len(identities) != len(set(identities)):
                raise ValueError(f"matrix {label} must be unique and sorted")
        known_rows = set(row_ids)
        known_columns = set(column_ids)
        if any(
            cell.row_id not in known_rows or cell.column_id not in known_columns
            for cell in self.cells
        ):
            raise ValueError("matrix cell axis endpoint is missing")
        coordinates = [(cell.row_id, cell.column_id) for cell in self.cells]
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("matrix cell coordinates must be unique")
        if self.symmetric and row_ids != column_ids:
            raise ValueError("symmetric matrix requires identical row and column axes")
        if self.record_count != len(self.cells):
            raise ValueError("matrix record count must match sparse cells")
        return self


class GraphNode(StrictModel):
    node_id: StrictStr = Field(min_length=1)
    label: StrictStr = Field(min_length=1)
    kind: StrictStr = Field(min_length=1)
    group_id: StrictStr | None = None
    metrics: tuple[ProjectionMetric, ...] = ()
    evidence_ids: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_metrics(self) -> Self:
        names = [metric.name for metric in self.metrics]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("graph node metrics must be unique and sorted")
        return self


class GraphEdge(StrictModel):
    edge_id: StrictStr = Field(min_length=1)
    source_id: StrictStr = Field(min_length=1)
    target_id: StrictStr = Field(min_length=1)
    kind: StrictStr = Field(min_length=1)
    label: StrictStr | None = None
    confidence: Confidence | None = None
    resolution: EdgeResolution = EdgeResolution.RESOLVED
    evidence_ids: tuple[StrictStr, ...] = ()


class GraphCluster(StrictModel):
    cluster_id: StrictStr = Field(min_length=1)
    label: StrictStr = Field(min_length=1)
    node_ids: tuple[StrictStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_nodes(self) -> Self:
        if list(self.node_ids) != sorted(self.node_ids) or len(self.node_ids) != len(
            set(self.node_ids)
        ):
            raise ValueError("cluster node IDs must be unique and sorted")
        return self


class GraphProjection(ProjectionBase):
    projection_kind: Literal["graph"] = "graph"
    direction: EdgeDirection = EdgeDirection.DIRECTED
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    clusters: tuple[GraphCluster, ...] = ()
    original_node_count: StrictInt = Field(default=0, ge=0)
    original_edge_count: StrictInt = Field(default=0, ge=0)
    omitted_node_count: StrictInt = Field(default=0, ge=0)
    omitted_edge_count: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        # Derive only omitted constructor defaults after typed JSON validation.
        # A before-model validator loses strict JSON tuple handling (Pydantic #9204).
        if "original_node_count" not in self.model_fields_set:
            object.__setattr__(self, "original_node_count", len(self.nodes))
        if "original_edge_count" not in self.model_fields_set:
            object.__setattr__(self, "original_edge_count", len(self.edges))
        node_ids = [node.node_id for node in self.nodes]
        edge_ids = [edge.edge_id for edge in self.edges]
        cluster_ids = [cluster.cluster_id for cluster in self.clusters]
        for label, identities in (
            ("nodes", node_ids),
            ("edges", edge_ids),
            ("clusters", cluster_ids),
        ):
            if identities != sorted(identities) or len(identities) != len(set(identities)):
                raise ValueError(f"graph {label} must be unique and sorted")
        known_nodes = set(node_ids)
        if any(
            edge.source_id not in known_nodes or edge.target_id not in known_nodes
            for edge in self.edges
        ):
            raise ValueError("graph edge endpoint is missing")
        if self.original_node_count != len(self.nodes) + self.omitted_node_count:
            raise ValueError("graph node omission counts must reconcile")
        if self.original_edge_count != len(self.edges) + self.omitted_edge_count:
            raise ValueError("graph edge omission counts must reconcile")
        if any(not set(cluster.node_ids) <= known_nodes for cluster in self.clusters):
            raise ValueError("graph cluster contains an unknown node")
        known_clusters = set(cluster_ids)
        if any(
            node.group_id is not None and node.group_id not in known_clusters for node in self.nodes
        ):
            raise ValueError("graph node references an unknown cluster")
        if self.record_count != len(self.nodes) + len(self.edges):
            raise ValueError("graph record count must match nodes and edges")
        return self


class HierarchyNode(StrictModel):
    node_id: StrictStr = Field(min_length=1)
    parent_id: StrictStr | None = None
    label: StrictStr = Field(min_length=1)
    kind: StrictStr = Field(min_length=1)
    evidence_ids: tuple[StrictStr, ...] = ()


class HierarchyProjection(ProjectionBase):
    projection_kind: Literal["hierarchy"] = "hierarchy"
    nodes: tuple[HierarchyNode, ...] = ()

    @model_validator(mode="after")
    def validate_hierarchy(self) -> Self:
        node_ids = [node.node_id for node in self.nodes]
        if node_ids != sorted(node_ids) or len(node_ids) != len(set(node_ids)):
            raise ValueError("hierarchy nodes must be unique and sorted")
        parents = {node.node_id: node.parent_id for node in self.nodes}
        if any(parent is not None and parent not in parents for parent in parents.values()):
            raise ValueError("hierarchy parent is missing")
        for node_id in node_ids:
            seen: set[str] = set()
            current: str | None = node_id
            while current is not None:
                if current in seen:
                    raise ValueError("hierarchy contains a cycle")
                seen.add(current)
                current = parents[current]
        if self.record_count != len(self.nodes):
            raise ValueError("hierarchy record count must match nodes")
        return self


class SequenceParticipant(StrictModel):
    participant_id: StrictStr = Field(min_length=1)
    label: StrictStr = Field(min_length=1)
    kind: StrictStr = Field(min_length=1)


class SequenceEvent(StrictModel):
    event_id: StrictStr = Field(min_length=1)
    position: StrictInt = Field(ge=0)
    source_id: StrictStr = Field(min_length=1)
    target_id: StrictStr = Field(min_length=1)
    label: StrictStr = Field(min_length=1)
    evidence_ids: tuple[StrictStr, ...] = ()


class SequenceProjection(ProjectionBase):
    projection_kind: Literal["sequence"] = "sequence"
    participants: tuple[SequenceParticipant, ...] = Field(min_length=1)
    events: tuple[SequenceEvent, ...] = ()

    @model_validator(mode="after")
    def validate_sequence(self) -> Self:
        participant_ids = [item.participant_id for item in self.participants]
        if participant_ids != sorted(participant_ids) or len(participant_ids) != len(
            set(participant_ids)
        ):
            raise ValueError("sequence participants must be unique and sorted")
        event_ids = [event.event_id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("sequence event IDs must be unique")
        if [event.position for event in self.events] != list(range(len(self.events))):
            raise ValueError("sequence event positions must be contiguous")
        known = set(participant_ids)
        if any(
            event.source_id not in known or event.target_id not in known for event in self.events
        ):
            raise ValueError("sequence event participant is missing")
        if self.record_count != len(self.events):
            raise ValueError("sequence record count must match events")
        return self


class StateNode(StrictModel):
    state_id: StrictStr = Field(min_length=1)
    label: StrictStr = Field(min_length=1)
    initial: StrictBool = False
    terminal: StrictBool = False
    error: StrictBool = False


class StateTransition(StrictModel):
    transition_id: StrictStr = Field(min_length=1)
    source_id: StrictStr = Field(min_length=1)
    target_id: StrictStr = Field(min_length=1)
    label: StrictStr = Field(min_length=1)
    guard: StrictStr | None = None
    evidence_ids: tuple[StrictStr, ...] = ()


class StateProjection(ProjectionBase):
    projection_kind: Literal["state"] = "state"
    states: tuple[StateNode, ...] = Field(min_length=1)
    transitions: tuple[StateTransition, ...] = ()

    @model_validator(mode="after")
    def validate_state_graph(self) -> Self:
        state_ids = [state.state_id for state in self.states]
        transition_ids = [item.transition_id for item in self.transitions]
        for label, identities in (
            ("states", state_ids),
            ("transitions", transition_ids),
        ):
            if identities != sorted(identities) or len(identities) != len(set(identities)):
                raise ValueError(f"state projection {label} must be unique and sorted")
        if sum(state.initial for state in self.states) != 1:
            raise ValueError("state projection requires exactly one initial state")
        known = set(state_ids)
        if any(
            item.source_id not in known or item.target_id not in known for item in self.transitions
        ):
            raise ValueError("state transition endpoint is missing")
        if self.record_count != len(self.states) + len(self.transitions):
            raise ValueError("state record count must match states and transitions")
        return self


class ComparisonRecord(StrictModel):
    subject_id: StrictStr = Field(min_length=1)
    change: ComparisonChange
    baseline_value: ProjectionScalar
    candidate_value: ProjectionScalar
    explanation: StrictStr = Field(min_length=1)
    evidence_ids: tuple[StrictStr, ...] = ()


class ComparisonChangeCounts(StrictModel):
    added: StrictInt = Field(ge=0)
    removed: StrictInt = Field(ge=0)
    changed: StrictInt = Field(ge=0)
    unchanged: StrictInt = Field(ge=0)


class ComparisonProjection(ProjectionBase):
    projection_kind: Literal["comparison"] = "comparison"
    identical: StrictBool
    includes_unchanged: StrictBool
    change_counts: ComparisonChangeCounts | None = None
    eligible_record_count: StrictInt = Field(ge=0)
    records: tuple[ComparisonRecord, ...] = ()

    @model_validator(mode="after")
    def validate_comparison(self) -> Self:
        if {item.role for item in self.inputs} != {
            ProjectionInputRole.BASELINE,
            ProjectionInputRole.CANDIDATE,
        }:
            raise ValueError("comparison requires baseline and candidate inputs")
        by_role = {item.role: item for item in self.inputs}
        hashes_match = (
            by_role[ProjectionInputRole.BASELINE].report_sha256
            == by_role[ProjectionInputRole.CANDIDATE].report_sha256
        )
        if self.identical != hashes_match:
            raise ValueError("comparison identical state must match input report identities")
        if self.identical:
            if self.records or self.truncated or self.change_counts is not None:
                raise ValueError(
                    "identical comparison cannot contain records, counts, or truncation"
                )
            if self.eligible_record_count:
                raise ValueError("identical comparison cannot contain eligible records")
        elif self.change_counts is None:
            raise ValueError("non-identical comparison requires global change counts")
        else:
            eligible = (
                self.change_counts.added
                + self.change_counts.removed
                + self.change_counts.changed
                + (self.change_counts.unchanged if self.includes_unchanged else 0)
            )
            if self.eligible_record_count != eligible:
                raise ValueError("comparison eligible record count does not reconcile")
            if not self.includes_unchanged and any(
                record.change is ComparisonChange.UNCHANGED for record in self.records
            ):
                raise ValueError("changes-only comparison cannot contain unchanged records")
            if self.truncated != (len(self.records) < self.eligible_record_count):
                raise ValueError("comparison truncation must match eligible retained records")
        identities = [record.subject_id for record in self.records]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("comparison records must be unique and sorted")
        if self.record_count != len(self.records):
            raise ValueError("comparison record count must match records")
        return self


class ReviewItem(StrictModel):
    item_id: StrictStr = Field(min_length=1)
    title: StrictStr = Field(min_length=1)
    statement: StrictStr = Field(min_length=1)
    evidence_ids: tuple[StrictStr, ...] = Field(min_length=1)
    counter_evidence_ids: tuple[StrictStr, ...] = ()
    related_ids: tuple[StrictStr, ...] = ()
    review_questions: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_review_item(self) -> Self:
        for label, values in (
            ("evidence IDs", self.evidence_ids),
            ("counter-evidence IDs", self.counter_evidence_ids),
            ("related IDs", self.related_ids),
            ("review questions", self.review_questions),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"review item {label} must be unique and sorted")
        return self


class ReviewPacketProjection(ProjectionBase):
    projection_kind: Literal["review-packet"] = "review-packet"
    items: tuple[ReviewItem, ...] = ()

    @model_validator(mode="after")
    def validate_review_packet(self) -> Self:
        identities = [item.item_id for item in self.items]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("review items must be unique and sorted")
        if self.record_count != len(self.items):
            raise ValueError("review record count must match items")
        return self


Projection = Annotated[
    TableProjection
    | MatrixProjection
    | GraphProjection
    | HierarchyProjection
    | SequenceProjection
    | StateProjection
    | ComparisonProjection
    | ReviewPacketProjection,
    Field(discriminator="projection_kind"),
]

PROJECTION_ADAPTER: TypeAdapter[Projection] = TypeAdapter(Projection)
