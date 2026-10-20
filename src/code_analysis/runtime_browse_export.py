"""Saved indexed runtime pages projected through the shared table renderers."""

from __future__ import annotations

import hashlib
import json

from code_analysis.browse_index import BrowseIndex
from code_analysis.models import Completeness
from code_analysis.profile_models import ProfileDiffRecord
from code_analysis.projection_models import (
    ProjectionInput,
    ProjectionInputRole,
    ProjectionScalar,
    ScalarKind,
    TableColumn,
    TableProjection,
    TableRow,
)
from code_analysis.runtime_browse_models import RuntimeBrowseRequest
from code_analysis.runtime_comparison_browse import RuntimeComparisonRequest
from code_analysis.runtime_index import RuntimeIndex
from code_analysis.runtime_queries import runtime_record_type


def build_runtime_browse_export(
    index: BrowseIndex | RuntimeIndex,
    request: RuntimeBrowseRequest | RuntimeComparisonRequest,
) -> TableProjection:
    summary = index.manifest.runtime
    if summary is None:
        raise ValueError("runtime unavailable; no profile was indexed")
    inputs = [
        ProjectionInput(
            role=ProjectionInputRole.SOURCE,
            report_schema_name="code-analysis-profile-report",
            report_schema_version="1.0.0",
            report_sha256=summary.profile_report_sha256,
            run_identity_sha256=summary.overview.profile_identity,
        )
    ]
    overview = summary.overview
    failed = overview.completeness is Completeness.FAILED
    incomplete = overview.completeness is Completeness.PARTIAL or not overview.source_matches
    if isinstance(request, RuntimeComparisonRequest):
        page = index.runtime_comparison(request)
        records, eligible = page.report.records, page.eligible
        comparison = summary.comparison
        if comparison is None:
            raise ValueError("runtime comparison unavailable")
        inputs = [
            ProjectionInput(
                role=ProjectionInputRole.SOURCE,
                report_schema_name=page.report.schema_name,
                report_schema_version="1.0.0",
                report_sha256=hashlib.sha256(
                    json.dumps(
                        page.report.model_dump(mode="json"),
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            )
        ]
        candidate_overview = comparison.candidate_overview
        failed |= candidate_overview.completeness is Completeness.FAILED
        incomplete |= (
            candidate_overview.completeness is Completeness.PARTIAL
            or not candidate_overview.source_matches
            or any(item.outcome != "matched" for item in page.report.decisions)
        )
        label = "runtime-comparison"
    else:
        runtime_page = index.runtime_page(request)
        records = runtime_page.panel.projection.records
        eligible = runtime_page.panel.projection.eligible_count
        label = f"runtime-{request.section.value}"
    selection = request.model_dump_json()
    health = summary.model_dump_json()
    identity = hashlib.sha256(
        (
            (index.manifest.report_sha256 or summary.profile_report_sha256)
            + label
            + selection
            + health
        ).encode()
    ).hexdigest()[:20]
    model = (
        ProfileDiffRecord
        if isinstance(request, RuntimeComparisonRequest)
        else runtime_record_type(request.section)
    )
    fields = tuple(model.model_fields)
    rows = tuple(
        TableRow(
            row_id=f"{label}:{request.offset + number:012d}",
            cells=tuple(
                ProjectionScalar(
                    kind=ScalarKind.STRING,
                    string_value=(
                        value
                        if isinstance(value, str)
                        else json.dumps(
                            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        )
                    ),
                )
                for value in record.model_dump(mode="json").values()
            ),
        )
        for number, record in enumerate(records)
    )
    omitted = eligible - len(records)
    return TableProjection(
        projection_id=f"projection-{identity}",
        title=f"Indexed {label} at offset {request.offset}",
        inputs=tuple(inputs),
        completeness=(
            Completeness.FAILED
            if failed
            else Completeness.PARTIAL if incomplete or omitted else Completeness.COMPLETE
        ),
        record_count=len(rows),
        truncated=bool(omitted),
        truncation_reason=f"{omitted} records outside selected page" if omitted else None,
        limitations=tuple(
            sorted(
                (
                    "Saved Python workload evidence only; "
                    "not JavaScript profiling or a performance claim.",
                    "Only this selected page is exported; "
                    "other records remain in the saved profile/index.",
                    "Null measurements are unavailable, not zero. "
                    "TSV cells must be imported as text.",
                    f"Exact selection: {selection}",
                    f"Indexed static report: {index.manifest.report_sha256 or 'unavailable'}",
                    f"Runtime provenance and compatibility: {health}",
                    f"Eligible: {eligible}; retained: {len(rows)}; outside page: {omitted}.",
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
        rows=rows,
    )
