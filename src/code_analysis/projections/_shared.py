"""Shared deterministic projection construction helpers."""

from __future__ import annotations

import hashlib
import json

from code_analysis.models import Completeness, Evidence
from code_analysis.projection_models import (
    ColumnAlignment,
    ProjectionInput,
    ProjectionInputRole,
    ProjectionScalar,
    ProjectionSelection,
    ScalarKind,
    TableColumn,
    TableProjection,
    TableRow,
)
from code_analysis.projections._catalog import ProjectionBuildError, ProjectionName
from code_analysis.report_models import AnalysisReport, ContractDefinition
from code_analysis.reports.json_report import canonical_json

Report = AnalysisReport


def _report_input(report: Report) -> ProjectionInput:
    rendered = canonical_json(report).encode("utf-8")
    return ProjectionInput(
        role=ProjectionInputRole.SOURCE,
        report_schema_name=report.schema_name,
        report_schema_version=report.schema_version,
        report_sha256=hashlib.sha256(rendered).hexdigest(),
        run_identity_sha256=getattr(report, "run_identity_sha256", None),
    )


def _projection_id(
    report_input: ProjectionInput,
    name: ProjectionName,
    selection: ProjectionSelection,
) -> str:
    identity = {
        "name": name.value,
        "selection": selection.model_dump(mode="json"),
        "source": report_input.model_dump(mode="json"),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return "projection-" + hashlib.sha256(encoded).hexdigest()[:20]


def _edge_id(source: str, target: str, kind: str) -> str:
    encoded = json.dumps((source, target, kind), separators=(",", ":")).encode()
    return "edge-" + hashlib.sha256(encoded).hexdigest()[:20]


def _evidence_identity(evidence: Evidence) -> str:
    payload = json.dumps(
        evidence.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "evidence-" + hashlib.sha256(payload.encode()).hexdigest()[:20]


def _string(value: str) -> ProjectionScalar:
    return ProjectionScalar(kind=ScalarKind.STRING, string_value=value)


def _integer(value: int) -> ProjectionScalar:
    return ProjectionScalar(kind=ScalarKind.INTEGER, integer_value=value)


def _number(value: float) -> ProjectionScalar:
    return ProjectionScalar(kind=ScalarKind.NUMBER, number_value=value)


def _boolean(value: bool) -> ProjectionScalar:
    return ProjectionScalar(kind=ScalarKind.BOOLEAN, boolean_value=value)


def _bounded_rows(
    rows: list[TableRow], selection: ProjectionSelection
) -> tuple[tuple[TableRow, ...], bool]:
    return tuple(rows[: selection.max_records]), len(rows) > selection.max_records


def _projection_state(report: Report, truncated: bool) -> Completeness:
    if report.completeness is Completeness.FAILED:
        return Completeness.FAILED
    if truncated or report.completeness is Completeness.PARTIAL:
        return Completeness.PARTIAL
    return Completeness.COMPLETE


def _limitations(report: Report, truncated: bool) -> tuple[str, ...]:
    values = set(report.limitations)
    if truncated:
        values.add("Projection output is truncated by its declared record limit.")
    return tuple(sorted(values))


def _require_contract_evidence(report: Report, projection_name: ProjectionName) -> AnalysisReport:
    if not report.contracts:
        raise ProjectionBuildError(f"{projection_name.value} requires recorded contract evidence")
    return report


def _selected_contracts(
    report: AnalysisReport,
    selection: ProjectionSelection,
) -> tuple[ContractDefinition, ...]:
    contracts = tuple(sorted(report.contracts, key=lambda contract: contract.contract_id))
    if not selection.selected_ids:
        return contracts
    selected = set(selection.selected_ids)
    missing = selected - {contract.contract_id for contract in contracts}
    if missing:
        raise ProjectionBuildError(
            "selected contract is not present in the report: " + ", ".join(sorted(missing))
        )
    return tuple(contract for contract in contracts if contract.contract_id in selected)


def _table_base(
    report: Report,
    name: ProjectionName,
    selection: ProjectionSelection,
    title: str,
    record_count: int,
    truncated: bool,
) -> dict[str, object]:
    source = _report_input(report)
    return {
        "projection_id": _projection_id(source, name, selection),
        "title": title,
        "inputs": (source,),
        "selection": selection,
        "completeness": _projection_state(report, truncated),
        "record_count": record_count,
        "truncated": truncated,
        "truncation_reason": ("record limit reached" if truncated else None),
        "limitations": _limitations(report, truncated),
    }


def _simple_phase_three_table(
    report: Report,
    selection: ProjectionSelection,
    name: ProjectionName,
    title: str,
    definitions: tuple[tuple[str, str, ScalarKind], ...],
    rows: list[TableRow],
) -> TableProjection:
    rows.sort(key=lambda item: item.row_id)
    bounded, truncated = _bounded_rows(rows, selection)
    return TableProjection(
        **_table_base(report, name, selection, title, len(bounded), truncated),
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
        rows=bounded,
    )
