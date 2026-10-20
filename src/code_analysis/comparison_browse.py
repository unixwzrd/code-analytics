"""Shared strict selection for saved static comparison views."""

import hashlib
import json
from collections.abc import Mapping

from pydantic import Field, StrictInt, StrictStr, model_validator

from code_analysis.models import Completeness, StrictModel
from code_analysis.projection_models import (
    ComparisonChange,
    ComparisonRecord,
    ProjectionInput,
    ProjectionInputRole,
    ProjectionScalar,
    ScalarKind,
    TableColumn,
    TableProjection,
    TableRow,
)
from code_analysis.ui_models import UIComparisonPage, UIReportIdentity


def comparison_filters_from_params(params: dict[str, list[str]]) -> "ComparisonFilters":
    searches = params.get("q", [""])
    if len(searches) != 1:
        raise ValueError("comparison search must be supplied once")
    return ComparisonFilters(
        changes=tuple(ComparisonChange(value) for value in params.get("change", [])),
        kinds=tuple(params.get("kind", [])),
        search=searches[0],
    )


class ComparisonFilters(StrictModel):
    changes: tuple[ComparisonChange, ...] = ()
    kinds: tuple[StrictStr, ...] = ()
    search: StrictStr = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_filters(self) -> "ComparisonFilters":
        from code_analysis.static_comparison_source import comparison_source_record_kinds

        if len(set(self.changes)) != len(self.changes) or len(set(self.kinds)) != len(self.kinds):
            raise ValueError("comparison filters must not contain duplicate values")
        if set(self.kinds) - comparison_source_record_kinds():
            raise ValueError("unknown comparison record kind")
        return self

    def matches(self, record: ComparisonRecord, kind: str) -> bool:
        return (
            (not self.changes or record.change in self.changes)
            and (not self.kinds or kind in self.kinds)
            and (self.search in record.subject_id or self.search in record.explanation)
        )

    def sql(self) -> tuple[str, tuple[str, ...]]:
        clauses: list[str] = []
        values: list[str] = []
        for field, selected in (
            (
                "json_extract(record.payload, '$.change')",
                tuple(item.value for item in self.changes),
            ),
            ("json_extract(binding.payload, '$.record_kind')", self.kinds),
        ):
            if selected:
                clauses.append(f"{field} IN ({','.join('?' for _ in selected)})")
                values.extend(selected)
        if self.search:
            clauses.append(
                "(instr(json_extract(record.payload, '$.subject_id'), ?) > 0 OR "
                "instr(json_extract(record.payload, '$.explanation'), ?) > 0)"
            )
            values.extend((self.search, self.search))
        return " AND ".join(clauses) or "1", tuple(values)


class ComparisonPageRequest(StrictModel):
    baseline_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    filters: ComparisonFilters = Field(default_factory=ComparisonFilters)
    offset: StrictInt = Field(default=0, ge=0, le=100_000)
    limit: StrictInt = Field(default=25, ge=1, le=500)


def comparison_page_projection(
    page: UIComparisonPage, request: ComparisonPageRequest, reports: tuple[UIReportIdentity, ...]
) -> TableProjection:
    by_role: Mapping[str, UIReportIdentity] = {report.role: report for report in reports}
    if (
        set(by_role) != {"primary", "comparison"}
        or len(reports) != 2
        or by_role["primary"].report_sha256 != request.baseline_sha256
        or by_role["comparison"].report_sha256 != request.candidate_sha256
    ):
        raise ValueError("comparison export report pair mismatch")
    if (page.offset, page.limit) != (request.offset, request.limit):
        raise ValueError("comparison export page selection mismatch")
    selection = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    omitted = page.eligible - page.retained
    completeness = page.completeness
    if (omitted or page.omitted) and completeness is Completeness.COMPLETE:
        completeness = Completeness.PARTIAL
    fields = ("position", "record_kind", "subject_id", "change", "explanation")
    return TableProjection(
        projection_id="projection-" + hashlib.sha256(selection.encode()).hexdigest()[:20],
        title="Static comparison — selected filtered page",
        inputs=tuple(
            ProjectionInput(
                role=role,
                report_schema_name="code-analysis-report",
                report_schema_version="1.0.0",
                report_sha256=by_role[key].report_sha256,
                run_identity_sha256=by_role[key].run_identity_sha256,
            )
            for key, role in (
                ("primary", ProjectionInputRole.BASELINE),
                ("comparison", ProjectionInputRole.CANDIDATE),
            )
        ),
        completeness=completeness,
        record_count=page.retained,
        truncated=bool(omitted or page.omitted),
        truncation_reason=(
            f"{omitted} matching records outside this page; {page.omitted} analytically omitted"
            if omitted or page.omitted
            else None
        ),
        limitations=tuple(
            sorted(
                (
                    f"Exact selection: {selection}",
                    f"Global eligible: {page.total_eligible}; filtered out: {page.filtered_out}; "
                    f"matching retained: {page.eligible}; exported: {page.retained}; "
                    f"analytically omitted: {page.omitted}.",
                    "Only the selected filtered page is exported. "
                    "Filtering is not analytical truncation.",
                    "This is a comparison summary; authoritative baseline/candidate evidence "
                    "remains in saved JSON.",
                    "TSV is plain text, not a spreadsheet-safe document; import cells as text.",
                )
            )
        ),
        columns=tuple(
            TableColumn(
                column_id=field.replace("_", "-"),
                label=field.replace("_", " ").title(),
                position=number,
                value_kind=ScalarKind.STRING,
            )
            for number, field in enumerate(fields)
        ),
        rows=tuple(
            TableRow(
                row_id=f"comparison:{record.position:012d}",
                cells=tuple(
                    ProjectionScalar(
                        kind=ScalarKind.STRING, string_value=str(getattr(record, field))
                    )
                    for field in fields
                ),
            )
            for record in page.records
        ),
    )
