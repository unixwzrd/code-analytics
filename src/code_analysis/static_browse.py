"""Prepared static projection rows: bounded indexed browsing, never source scanning."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.candidate_browse import (
    decision_aware_candidate_row,
    effective_candidate_policy,
    selected_candidate_positions,
    validate_candidate_policies,
    validate_candidate_selection,
    write_candidate_policies,
)
from code_analysis.decisions import (
    CandidateSignatureCatalog,
    EffectiveDecision,
    effective_decisions_from_signatures,
    ledger_sha256,
)
from code_analysis.models import Completeness, StrictModel
from code_analysis.projection_models import (
    MatrixProjection,
    ProjectionInput,
    ProjectionScalar,
    ProjectionSelection,
    ScalarKind,
    TableColumn,
    TableProjection,
    TableRow,
    TableSort,
)
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.projections.candidates import candidate_worklist_sort
from code_analysis.report_models import AnalysisReport, CommonalityDimensionKind, SectionState
from code_analysis.reports.projection_views import _scalar_text
from code_analysis.subject_finding_browse import (
    SubjectFindingFactCounts,
    read_subject_finding_facts,
    subject_finding_fact_records,
    validate_indexed_finding_selection,
    write_subject_finding_facts,
)
from code_analysis.subject_findings import (
    SubjectFindingSelectionError,
    aggregate_subject_findings,
    unscoped_scope_limitation,
)

if TYPE_CHECKING:
    from code_analysis.ui_decisions import DecisionSnapshot

STATIC_PROJECTIONS = (
    ProjectionName.COMPLEXITY,
    ProjectionName.MODULE_RESPONSIBILITIES,
    ProjectionName.SCCS,
    ProjectionName.CYCLE_LIST,
    ProjectionName.ARCHITECTURE_ESTIMATES,
    ProjectionName.ARCHITECTURE_CONTRACT_SUGGESTIONS,
    ProjectionName.FUNCTION_COMMONALITY,
    ProjectionName.SUBJECT_FINDINGS,
)
OPTIONAL_STATIC_PROJECTIONS = (ProjectionName.CANDIDATE_WORKLIST,)
COVERAGE_STATIC_PROJECTIONS = (
    ProjectionName.COVERAGE_FILES,
    ProjectionName.COVERAGE_MATRIX,
    ProjectionName.UNCOVERED_PROPOSAL_SUBJECTS,
)
STATIC_CALL_STATIC_PROJECTIONS = (ProjectionName.STATIC_CALL_SITES,)
ALL_OPTIONAL_STATIC_PROJECTIONS = (
    *OPTIONAL_STATIC_PROJECTIONS,
    *COVERAGE_STATIC_PROJECTIONS,
    *STATIC_CALL_STATIC_PROJECTIONS,
)
_STATIC_CALL_SECTION = "static-call-occurrences"
STATIC_TABLE_SQL = (
    "CREATE TABLE static_projection_rows (name TEXT NOT NULL, position INTEGER NOT NULL, "
    "payload TEXT NOT NULL, search_text TEXT NOT NULL, "
    "PRIMARY KEY (name, position)) WITHOUT ROWID"
)


class StaticProjectionLimitError(ValueError):
    """Valid static evidence exceeds an explicit preparation or page bound."""


def _static_call_section_state(report: AnalysisReport) -> SectionState | None:
    for item in report.section_coverage:
        if item.section == _STATIC_CALL_SECTION:
            return item.state
    return None


def static_call_sites_prepared(report: AnalysisReport) -> bool:
    """True when saved static-call projections are allowed to be materialized."""
    state = _static_call_section_state(report)
    if state is None or state is SectionState.DISABLED:
        return False
    if state is SectionState.UNAVAILABLE:
        return False
    return not (state is SectionState.FAILED and not report.static_call_occurrences)


def _commonality_table(projection: MatrixProjection) -> TableProjection:
    """One named row per saved sparse cell; never construct the dense matrix."""
    metric_names = tuple(
        sorted(
            [f"shared-{kind.value}" for kind in CommonalityDimensionKind]
            + ["structural-clone-clusters"]
        )
    )
    labels = {item.item_id: item.label for item in projection.rows}
    column_labels = {item.item_id: item.label for item in projection.columns}
    identities = ("left-subject-id", "right-subject-id", "left-subject", "right-subject")
    columns = tuple(
        TableColumn(
            column_id=name,
            label=name.replace("-", " ").title(),
            position=position,
            value_kind=ScalarKind.STRING if position < 4 else ScalarKind.INTEGER,
        )
        for position, name in enumerate((*identities, *metric_names))
    )
    rows = []
    for cell in projection.cells:
        metrics = {item.name: item.value for item in cell.metrics}
        if set(metrics) - set(metric_names):
            raise ValueError("unrecognized commonality metric in prepared matrix")
        texts = (cell.row_id, cell.column_id, labels[cell.row_id], column_labels[cell.column_id])
        rows.append(
            TableRow(
                row_id=cell.cell_id,
                cells=tuple(
                    ProjectionScalar(kind=ScalarKind.STRING, string_value=text) for text in texts
                )
                + tuple(
                    metrics.get(name, ProjectionScalar(kind=ScalarKind.NULL))
                    for name in metric_names
                ),
                evidence_ids=cell.evidence_ids,
            )
        )
    identity = hashlib.sha256(
        (projection.projection_id + ":sparse-pair-table").encode()
    ).hexdigest()[:20]
    return TableProjection(
        projection_id=f"projection-{identity}",
        title="Function commonality: recorded sparse pairs",
        inputs=projection.inputs,
        selection=projection.selection,
        columns=columns,
        rows=tuple(rows),
        record_count=len(rows),
        completeness=projection.completeness,
        truncated=projection.truncated,
        truncation_reason=projection.truncation_reason,
        limitations=tuple(
            sorted(
                {
                    *projection.limitations,
                    "One row per recorded sparse pair; absent dimensions are null, not zero. "
                    "Counts are shared feature identities, not a similarity score "
                    "or refactor approval.",
                }
            )
        ),
    )


def _sparse_matrix_table(projection: MatrixProjection) -> TableProjection:
    """Preserve one typed row per retained sparse cell, including absent metrics."""
    metric_kinds: dict[str, ScalarKind] = {}
    for cell in projection.cells:
        for metric in cell.metrics:
            current = metric_kinds.setdefault(metric.name, metric.value.kind)
            if metric.value.kind is not ScalarKind.NULL and current is ScalarKind.NULL:
                metric_kinds[metric.name] = metric.value.kind
            elif (
                metric.value.kind is not ScalarKind.NULL
                and current is not ScalarKind.NULL
                and current is not metric.value.kind
            ):
                raise ValueError("sparse matrix metric changes type between cells")
    metric_names = tuple(sorted(metric_kinds))
    row_labels = {item.item_id: item.label for item in projection.rows}
    column_labels = {item.item_id: item.label for item in projection.columns}
    identity_columns = (
        ("row-id", "Row Id"),
        ("column-id", "Column Id"),
        ("row-label", "Row Label"),
        ("column-label", "Column Label"),
    )
    columns = tuple(
        TableColumn(
            column_id=column_id,
            label=label,
            position=position,
            value_kind=ScalarKind.STRING,
        )
        for position, (column_id, label) in enumerate(identity_columns)
    ) + tuple(
        TableColumn(
            column_id=name,
            label=name.replace("-", " ").title(),
            position=position + len(identity_columns),
            value_kind=metric_kinds[name],
        )
        for position, name in enumerate(metric_names)
    )
    rows = []
    for cell in projection.cells:
        metrics = {metric.name: metric.value for metric in cell.metrics}
        identities = (
            cell.row_id,
            cell.column_id,
            row_labels[cell.row_id],
            column_labels[cell.column_id],
        )
        rows.append(
            TableRow(
                row_id=cell.cell_id,
                cells=tuple(
                    ProjectionScalar(kind=ScalarKind.STRING, string_value=value)
                    for value in identities
                )
                + tuple(
                    metrics.get(name, ProjectionScalar(kind=ScalarKind.NULL))
                    for name in metric_names
                ),
                evidence_ids=cell.evidence_ids,
            )
        )
    identity = hashlib.sha256(
        (projection.projection_id + ":sparse-cell-table").encode()
    ).hexdigest()[:20]
    return TableProjection(
        projection_id=f"projection-{identity}",
        title=f"{projection.title}: recorded sparse cells",
        inputs=projection.inputs,
        selection=projection.selection,
        columns=columns,
        rows=tuple(rows),
        record_count=len(rows),
        completeness=projection.completeness,
        truncated=projection.truncated,
        truncation_reason=projection.truncation_reason,
        limitations=tuple(
            sorted(
                {
                    *projection.limitations,
                    "One row per recorded sparse cell; absent metrics are null, not zero.",
                }
            )
        ),
    )


def prepared_static_projection(report: AnalysisReport, name: ProjectionName) -> TableProjection:
    projection = build_projection(report, name, max_records=100_000)
    if name is ProjectionName.FUNCTION_COMMONALITY and isinstance(projection, MatrixProjection):
        return _commonality_table(projection)
    if name is ProjectionName.COVERAGE_MATRIX and isinstance(projection, MatrixProjection):
        return _sparse_matrix_table(projection)
    if not isinstance(projection, TableProjection):
        raise ValueError("prepared static projection must be a table or supported sparse matrix")
    return projection


class StaticProjectionRequest(StrictModel):
    name: ProjectionName
    offset: StrictInt = Field(default=0, ge=0)
    limit: StrictInt = Field(default=25, ge=1, le=500)
    query: StrictStr = Field(default="", max_length=4096)
    candidate_selection: ProjectionSelection | None = None
    finding_selection: ProjectionSelection | None = None
    decision_ledger_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    decision_report_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_name(self) -> Self:
        if self.name not in (*STATIC_PROJECTIONS, *ALL_OPTIONAL_STATIC_PROJECTIONS):
            raise ValueError("projection is not prepared for indexed static browsing")
        if self.finding_selection is not None:
            if self.name is not ProjectionName.SUBJECT_FINDINGS:
                raise ValueError("finding selection requires the subject-findings projection")
            validate_indexed_finding_selection(self.finding_selection)
        if self.candidate_selection is not None:
            if self.name is not ProjectionName.CANDIDATE_WORKLIST:
                raise ValueError("candidate policy requires the candidate worklist")
            validate_candidate_selection(self.candidate_selection)
        if (self.decision_ledger_sha256 is None) != (self.decision_report_sha256 is None):
            raise ValueError("decision ledger and report identities must be supplied together")
        if (
            self.decision_ledger_sha256 is not None
            and self.name is not ProjectionName.CANDIDATE_WORKLIST
        ):
            raise ValueError("decision ledger selection requires the candidate worklist")
        if (
            self.candidate_selection is not None
            and self.candidate_selection.candidate_decisions
            and self.decision_ledger_sha256 is None
        ):
            raise ValueError("candidate decision filtering requires an explicit decision ledger")
        return self


class StaticProjectionSummary(StrictModel):
    name: ProjectionName
    title: StrictStr
    projection_id: StrictStr = Field(pattern=r"^projection-[0-9a-f]{20}$")
    source: ProjectionInput
    columns: tuple[TableColumn, ...] = Field(min_length=1)
    sort: tuple[TableSort, ...]
    completeness: Completeness
    source_truncated: StrictBool
    truncation_reason: StrictStr | None
    limitations: tuple[StrictStr, ...]
    records: StrictInt = Field(ge=0, le=100_000)
    unscoped_finding_count: StrictInt | None = Field(default=None, ge=0, le=100_000)

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.name not in (*STATIC_PROJECTIONS, *ALL_OPTIONAL_STATIC_PROJECTIONS):
            raise ValueError("unexpected prepared static projection")
        if (self.unscoped_finding_count is None) is (self.name is ProjectionName.SUBJECT_FINDINGS):
            raise ValueError("only subject-findings carries a prepared unscoped finding count")
        if self.source_truncated and self.completeness is Completeness.COMPLETE:
            raise ValueError("truncated static projection cannot be complete")
        # Reuse the authoritative column/sort/source/limitation validators.
        self.projection((), ProjectionSelection(), self.projection_id)
        return self

    def projection(
        self, rows: tuple[TableRow, ...], selection: ProjectionSelection, identity: str
    ) -> TableProjection:
        return TableProjection(
            projection_id=identity,
            title=self.title,
            inputs=(self.source,),
            selection=selection,
            columns=self.columns,
            sort=self.sort,
            rows=rows,
            record_count=len(rows),
            completeness=self.completeness,
            truncated=self.source_truncated,
            truncation_reason=self.truncation_reason,
            limitations=self.limitations,
        )


class StaticProjectionPage(StrictModel):
    request: StaticProjectionRequest
    summary: StaticProjectionSummary
    eligible: StrictInt = Field(ge=0, le=100_000)
    retained: StrictInt = Field(ge=0, le=500)
    truncated: StrictBool
    records: tuple[TableRow, ...] = Field(max_length=500)
    unscoped_finding_count: StrictInt | None = Field(default=None, ge=0, le=100_000)

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if self.request.name is not self.summary.name or self.eligible > self.summary.records:
            raise ValueError("static projection page identity/count mismatch")
        prepared_unscoped = self.summary.unscoped_finding_count
        if (self.unscoped_finding_count is None) != (prepared_unscoped is None):
            raise ValueError("subject-findings page must recompute its unscoped finding count")
        if (
            prepared_unscoped is not None
            and self.unscoped_finding_count is not None
            and self.unscoped_finding_count > prepared_unscoped
        ):
            raise ValueError("filtering cannot increase the unscoped finding count")
        if self.retained != len(self.records) or self.retained != min(
            self.request.limit, max(0, self.eligible - self.request.offset)
        ):
            raise ValueError("static projection page retention mismatch")
        if self.truncated != (self.request.offset + self.retained < self.eligible):
            raise ValueError("static projection page continuation mismatch")
        self.summary.projection(self.records, ProjectionSelection(), self.summary.projection_id)
        if (
            self.request.decision_report_sha256 is not None
            and self.request.decision_report_sha256 != self.summary.source.report_sha256
        ):
            raise ValueError("decision ledger page report identity mismatch")
        return self


def write_static_projections(
    connection: sqlite3.Connection,
    report: AnalysisReport,
    candidate_signatures: Mapping[str, str] | None = None,
) -> tuple[tuple[StaticProjectionSummary, ...], SubjectFindingFactCounts]:
    connection.execute(STATIC_TABLE_SQL)
    facts = subject_finding_fact_records(report)
    fact_counts = write_subject_finding_facts(connection, facts)
    unscoped = sum(1 for finding in facts.findings if not finding.subject_ids)
    summaries = []
    optional_names = []
    if report.refactor_candidates:
        optional_names.append(ProjectionName.CANDIDATE_WORKLIST)
    if report.coverage_files:
        optional_names.append(ProjectionName.COVERAGE_FILES)
    if report.coverage_observations:
        optional_names.append(ProjectionName.COVERAGE_MATRIX)
    if report.refactor_proposals:
        optional_names.append(ProjectionName.UNCOVERED_PROPOSAL_SUBJECTS)
    if static_call_sites_prepared(report):
        optional_names.append(ProjectionName.STATIC_CALL_SITES)
    names = (*STATIC_PROJECTIONS, *optional_names)
    candidate_rows: tuple[TableRow, ...] = ()
    for name in names:
        projection = prepared_static_projection(report, name)
        if name is ProjectionName.CANDIDATE_WORKLIST:
            candidate_rows = projection.rows
        summaries.append(
            StaticProjectionSummary(
                name=name,
                title=projection.title,
                projection_id=projection.projection_id,
                source=projection.inputs[0],
                columns=projection.columns,
                sort=projection.sort,
                completeness=projection.completeness,
                source_truncated=projection.truncated,
                truncation_reason=projection.truncation_reason,
                limitations=tuple(
                    sorted(
                        (
                            "Preparation: " + item
                            if name is ProjectionName.CANDIDATE_WORKLIST
                            and item.startswith("Candidate selection retained")
                            else item
                        )
                        for item in projection.limitations
                    )
                ),
                records=len(projection.rows),
                unscoped_finding_count=(
                    unscoped if name is ProjectionName.SUBJECT_FINDINGS else None
                ),
            )
        )
        if name is ProjectionName.SUBJECT_FINDINGS:
            # Subject rows are rebuilt from the retained facts on every read; storing
            # them here would duplicate the aggregation and could drift from it.
            continue
        for position, row in enumerate(projection.rows):
            payload = row.model_dump_json()
            if len(payload.encode()) > 1024**2:
                raise StaticProjectionLimitError("static projection row exceeds one MiB")
            search = " ".join((row.row_id, *(_scalar_text(cell) for cell in row.cells))).casefold()
            connection.execute(
                "INSERT INTO static_projection_rows VALUES (?, ?, ?, ?)",
                (name.value, position, payload, search),
            )
    signatures = candidate_signatures
    if candidate_rows and signatures is None:
        catalog = CandidateSignatureCatalog.from_report(report)
        signatures = {row.row_id: catalog.signature(row.row_id) for row in candidate_rows}
    write_candidate_policies(connection, report, candidate_rows, signatures or {})
    return tuple(summaries), fact_counts


def validate_static_projections(
    connection: sqlite3.Connection, summaries: tuple[StaticProjectionSummary, ...]
) -> None:
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'static_projection_rows'"
    ).fetchone() != (STATIC_TABLE_SQL,):
        raise ValueError("static projection table schema mismatch")
    actual = connection.execute(
        "SELECT name, count(*), min(position), max(position) "
        "FROM static_projection_rows GROUP BY name ORDER BY name"
    ).fetchall()
    expected = sorted(
        (summary.name.value, summary.records, 0, summary.records - 1)
        for summary in summaries
        # Subject-findings rows live only as facts; see write_static_projections.
        if summary.records and summary.name is not ProjectionName.SUBJECT_FINDINGS
    )
    if actual != expected:
        raise ValueError("static projection rows disagree with receipt")
    validate_candidate_policies(
        connection,
        next(
            (item.records for item in summaries if item.name is ProjectionName.CANDIDATE_WORKLIST),
            0,
        ),
    )


def _row_search_text(row: TableRow) -> str:
    return " ".join((row.row_id, *(_scalar_text(cell) for cell in row.cells))).casefold()


def _read_subject_findings(
    connection: sqlite3.Connection,
    summary: StaticProjectionSummary,
    request: StaticProjectionRequest,
    fact_counts: SubjectFindingFactCounts | None,
    query: str,
) -> StaticProjectionPage:
    """Filter and aggregate retained facts, then search and page the resulting rows.

    Filtering happens inside the shared aggregation, before grouping, so the
    row counts and the recomputed unscoped total describe the same filtered
    record set. No report loader, projection builder or source file is read.
    """

    if fact_counts is None:
        raise ValueError("indexed subject-findings browsing requires the receipt fact counts")
    selection = request.finding_selection or ProjectionSelection()
    facts = read_subject_finding_facts(connection, fact_counts)
    try:
        aggregate = aggregate_subject_findings(facts, selection)
    except SubjectFindingSelectionError as exc:
        raise ValueError(str(exc)) from exc
    unfiltered = selection == ProjectionSelection()
    if len(aggregate.rows) > summary.records or (
        unfiltered and len(aggregate.rows) != summary.records
    ):
        raise ValueError("indexed subject rows disagree with the prepared receipt")
    matching = tuple(row for row in aggregate.rows if query in _row_search_text(row))
    records = matching[request.offset : request.offset + request.limit]
    total = 0
    for row in records:
        size = len(row.model_dump_json().encode())
        if size > 1024**2:
            raise ValueError("subject-findings row exceeds byte bound")
        total += size
        if total > 8 * 1024**2:
            raise StaticProjectionLimitError("static projection page exceeds eight MiB")
    return StaticProjectionPage(
        request=request,
        summary=summary,
        eligible=len(matching),
        retained=len(records),
        truncated=request.offset + len(records) < len(matching),
        records=records,
        unscoped_finding_count=aggregate.unscoped_finding_count,
    )


def read_static_projection(
    connection: sqlite3.Connection,
    summaries: tuple[StaticProjectionSummary, ...],
    request: StaticProjectionRequest,
    decisions: DecisionSnapshot | None = None,
    fact_counts: SubjectFindingFactCounts | None = None,
) -> StaticProjectionPage:
    summary = next((item for item in summaries if item.name is request.name), None)
    if summary is None:
        raise ValueError("projection unavailable: no matching evidence was recorded in this index")
    if request.decision_ledger_sha256 is not None:
        if decisions is None:
            raise ValueError("explicit decision ledger snapshot is required")
        if (
            decisions.ledger_sha256 != request.decision_ledger_sha256
            or decisions.report_sha256 != request.decision_report_sha256
            or request.decision_report_sha256 != summary.source.report_sha256
            or ledger_sha256(decisions.ledger) != decisions.ledger_sha256
        ):
            raise ValueError("decision ledger snapshot identity mismatch")
    elif decisions is not None:
        raise ValueError("decision ledger snapshot requires explicit request identities")
    query = request.query.casefold()
    parameters = (request.name.value, query)
    predicate = "name = ? AND instr(search_text, ?) > 0"
    if request.name is ProjectionName.SUBJECT_FINDINGS:
        return _read_subject_findings(connection, summary, request, fact_counts, query)
    if request.name is ProjectionName.CANDIDATE_WORKLIST:
        selection = request.candidate_selection or ProjectionSelection()
        columns = tuple(item.column_id for item in summary.columns)
        current_decisions: Mapping[str, EffectiveDecision] | None = None
        if decisions is not None:
            stored = dict(
                connection.execute(
                    "SELECT json_extract(payload, '$.candidate_id'), "
                    "reconsideration_signature_sha256 FROM candidate_policy_rows"
                )
            )
            if len(stored) != connection.execute(
                "SELECT count(*) FROM candidate_policy_rows"
            ).fetchone()[0] or any(
                not isinstance(candidate_id, str)
                or not isinstance(signature, str)
                or len(signature) != 64
                or any(character not in "0123456789abcdef" for character in signature)
                for candidate_id, signature in stored.items()
            ):
                raise ValueError("candidate reconsideration signatures are malformed")
            current_decisions = effective_decisions_from_signatures(decisions.ledger, stored)
        positions = selected_candidate_positions(
            connection, selection, query, current_decisions, column_ids=columns
        )
        eligible = len(positions)
        candidate_payloads: list[tuple[str]] = []
        for position in positions[request.offset : request.offset + request.limit]:
            record = connection.execute(
                "SELECT s.payload, p.payload FROM static_projection_rows s "
                "JOIN candidate_policy_rows p ON p.position = s.position "
                "WHERE s.name = ? AND s.position = ?",
                (request.name.value, position),
            ).fetchone()
            if record is None:
                raise ValueError("candidate policy row is absent from indexed selection")
            payload, policy_payload = record
            if any(
                not isinstance(value, str) or len(value.encode()) > 1024**2
                for value in (payload, policy_payload)
            ):
                raise ValueError("candidate policy row exceeds byte bound")
            row = TableRow.model_validate_json(payload)
            if current_decisions is not None:
                from code_analysis.candidate_policy import CandidatePolicyFacts

                facts = CandidatePolicyFacts.model_validate_json(policy_payload)
                row = decision_aware_candidate_row(
                    row,
                    effective_candidate_policy(facts, current_decisions.get(facts.candidate_id)),
                    columns,
                )
            candidate_payloads.append((row.model_dump_json(),))
        summary = StaticProjectionSummary.model_validate(
            {
                **summary.model_dump(),
                "sort": candidate_worklist_sort(selection.prefer_existing_owner),
            }
        )
        rows: Iterable[tuple[str]] = candidate_payloads
    else:
        eligible = connection.execute(
            f"SELECT count(*) FROM static_projection_rows WHERE {predicate}", parameters
        ).fetchone()[0]
        rows = connection.execute(
            f"SELECT payload FROM static_projection_rows WHERE {predicate} "
            "ORDER BY position LIMIT ? OFFSET ?",
            (*parameters, request.limit, request.offset),
        )
    records = []
    total = 0
    for (payload,) in rows:
        if not isinstance(payload, str) or len(payload.encode()) > 1024**2:
            raise ValueError("static projection row exceeds byte bound")
        total += len(payload.encode())
        if total > 8 * 1024**2:
            raise StaticProjectionLimitError("static projection page exceeds eight MiB")
        records.append(TableRow.model_validate_json(payload))
    return StaticProjectionPage(
        request=request,
        summary=summary,
        eligible=eligible,
        retained=len(records),
        truncated=request.offset + len(records) < eligible,
        records=tuple(records),
    )


def static_page_projection(page: StaticProjectionPage) -> TableProjection:
    identity = hashlib.sha256(
        (page.summary.projection_id + page.request.model_dump_json()).encode()
    ).hexdigest()[:20]
    requested = (
        page.request.candidate_selection or page.request.finding_selection or ProjectionSelection()
    )
    projection = page.summary.projection(
        page.records,
        requested.model_copy(update={"max_records": page.request.limit}),
        f"projection-{identity}",
    )
    if page.request.decision_ledger_sha256 is not None:
        from code_analysis.projection_models import ProjectionInputRole

        projection = projection.model_copy(
            update={
                "inputs": (
                    *projection.inputs,
                    ProjectionInput(
                        role=ProjectionInputRole.DECISION_LEDGER,
                        report_schema_name="code-analysis-decision-ledger",
                        report_schema_version="1.0.0",
                        report_sha256=page.request.decision_ledger_sha256,
                    ),
                )
            }
        )
    bounded = page.truncated or min(page.request.offset, page.eligible) > 0
    limitations = set(projection.limitations)
    if page.unscoped_finding_count is not None:
        # The receipt's disclosure describes the unfiltered facts; this page
        # discloses the count recomputed for its own filtered record set.
        limitations.discard(unscoped_scope_limitation(page.summary.unscoped_finding_count or 0))
        if page.unscoped_finding_count:
            limitations.add(unscoped_scope_limitation(page.unscoped_finding_count))
        limitations.add(
            f"Indexed finding selection matched {page.eligible} of "
            f"{page.summary.records} prepared subject rows."
        )
    limitations.add(f"Indexed selection retained {page.retained} of {page.eligible} matching rows.")
    limitations.add("Indexed selection: " + page.request.model_dump_json())
    return TableProjection.model_validate(
        {
            **projection.model_dump(),
            "limitations": tuple(sorted(limitations)),
            "completeness": (
                Completeness.PARTIAL
                if bounded and projection.completeness is Completeness.COMPLETE
                else projection.completeness
            ),
            "truncated": projection.truncated or bounded,
            "truncation_reason": projection.truncation_reason
            or ("indexed projection page bound" if bounded else None),
        }
    )
