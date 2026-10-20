"""Indexed subject/finding boundary facts: stored once, aggregated on demand.

The index retains one strict :class:`SubjectFact` and one :class:`FindingFact`
record per analysis subject and finding in two named tables. Indexed browsing
rebuilds rows through the shared aggregation instead of saving a second
aggregated copy, so an indexed page cannot drift from the authoritative
projection. Facts carry identities, classifications and associations only: no
embedded evidence payloads and no raw source.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from pydantic import Field, StrictInt

from code_analysis.models import StrictModel
from code_analysis.projection_models import ProjectionSelection
from code_analysis.report_models import AnalysisReport
from code_analysis.subject_findings import (
    FindingFact,
    SubjectFact,
    SubjectFindingFacts,
    subject_finding_facts,
)

__all__ = (
    "FINDING_FACT_TABLE_SQL",
    "INDEXED_FINDING_SELECTION_FIELDS",
    "MAX_FACT_ROW_BYTES",
    "MAX_SUBJECT_FINDING_FACTS",
    "SUBJECT_FACT_TABLE_SQL",
    "SubjectFindingFactCounts",
    "read_subject_finding_facts",
    "subject_finding_fact_records",
    "validate_indexed_finding_selection",
    "validate_subject_finding_facts",
    "write_subject_finding_facts",
)

SUBJECT_FACT_TABLE_SQL = (
    "CREATE TABLE subject_facts (subject_id TEXT PRIMARY KEY NOT NULL, "
    "payload TEXT NOT NULL) WITHOUT ROWID"
)
FINDING_FACT_TABLE_SQL = (
    "CREATE TABLE finding_facts (finding_id TEXT PRIMARY KEY NOT NULL, "
    "payload TEXT NOT NULL) WITHOUT ROWID"
)

MAX_SUBJECT_FINDING_FACTS = 100_000
MAX_FACT_ROW_BYTES = 1024**2

INDEXED_FINDING_SELECTION_FIELDS = (
    "finding_categories",
    "finding_rule_ids",
    "finding_severities",
    "module_prefixes",
)


def _limit_error(message: str) -> ValueError:
    """Reuse the existing static limit error so callers keep their ENODATA mapping.

    The import is local because :mod:`code_analysis.static_browse` imports this
    module to read facts; a module-level import would be circular.
    """

    from code_analysis.static_browse import StaticProjectionLimitError

    return StaticProjectionLimitError(message)


class SubjectFindingFactCounts(StrictModel):
    """Receipt counts for the two retained fact tables."""

    subjects: StrictInt = Field(ge=0, le=MAX_SUBJECT_FINDING_FACTS)
    findings: StrictInt = Field(ge=0, le=MAX_SUBJECT_FINDING_FACTS)


def subject_finding_fact_records(report: AnalysisReport) -> SubjectFindingFacts:
    """Convert one report into facts, refusing to omit any beyond the explicit bound."""

    for label, records in (("subject", report.subjects), ("finding", report.findings)):
        if len(records) > MAX_SUBJECT_FINDING_FACTS:
            raise _limit_error(
                f"{label} facts exceed the {MAX_SUBJECT_FINDING_FACTS}-record preparation bound"
            )
    return subject_finding_facts(report)


def _checked_payload(label: str, payload: str) -> str:
    if not isinstance(payload, str):
        raise ValueError(f"{label} fact payload must be text")
    if len(payload.encode()) > MAX_FACT_ROW_BYTES:
        raise _limit_error(f"{label} fact record exceeds the one-MiB bound")
    return payload


def write_subject_finding_facts(
    connection: sqlite3.Connection, facts: SubjectFindingFacts
) -> SubjectFindingFactCounts:
    """Persist one record per fact; no aggregation and no evidence payloads are stored."""

    connection.execute(SUBJECT_FACT_TABLE_SQL)
    connection.execute(FINDING_FACT_TABLE_SQL)
    for subject in facts.subjects:
        connection.execute(
            "INSERT INTO subject_facts VALUES (?, ?)",
            (subject.subject_id, _checked_payload("subject", subject.model_dump_json())),
        )
    for finding in facts.findings:
        connection.execute(
            "INSERT INTO finding_facts VALUES (?, ?)",
            (finding.finding_id, _checked_payload("finding", finding.model_dump_json())),
        )
    return SubjectFindingFactCounts(subjects=len(facts.subjects), findings=len(facts.findings))


def _stored_facts[Fact: (SubjectFact, FindingFact)](
    connection: sqlite3.Connection,
    table: str,
    key_column: str,
    model: type[Fact],
    identity: Callable[[Fact], str],
    label: str,
) -> tuple[Fact, ...]:
    records: list[Fact] = []
    for key, payload in connection.execute(
        f"SELECT {key_column}, payload FROM {table} ORDER BY {key_column}"
    ):
        if len(records) >= MAX_SUBJECT_FINDING_FACTS:
            raise _limit_error(
                f"{label} facts exceed the {MAX_SUBJECT_FINDING_FACTS}-record read bound"
            )
        record = model.model_validate_json(_checked_payload(label, payload))
        if identity(record) != key:
            raise ValueError(f"{label} fact payload identity disagrees with its stored key")
        records.append(record)
    return tuple(records)


def read_subject_finding_facts(
    connection: sqlite3.Connection, counts: SubjectFindingFactCounts
) -> SubjectFindingFacts:
    """Rebuild the shared aggregation input from retained facts alone.

    Strict record validation, the receipt counts and the collection's own
    identity/dangling-reference rules are all enforced here, so a tampered or
    partially written index fails instead of producing plausible rows.
    """

    subjects = _stored_facts(
        connection,
        "subject_facts",
        "subject_id",
        SubjectFact,
        lambda item: item.subject_id,
        "subject",
    )
    findings = _stored_facts(
        connection,
        "finding_facts",
        "finding_id",
        FindingFact,
        lambda item: item.finding_id,
        "finding",
    )
    if (len(subjects), len(findings)) != (counts.subjects, counts.findings):
        raise ValueError("retained subject/finding facts disagree with the index receipt")
    return SubjectFindingFacts(subjects=subjects, findings=findings)


def validate_subject_finding_facts(
    connection: sqlite3.Connection, counts: SubjectFindingFactCounts
) -> None:
    for table, sql in (
        ("subject_facts", SUBJECT_FACT_TABLE_SQL),
        ("finding_facts", FINDING_FACT_TABLE_SQL),
    ):
        if connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
        ).fetchone() != (sql,):
            raise ValueError("subject/finding fact table schema mismatch")
    read_subject_finding_facts(connection, counts)


def validate_indexed_finding_selection(selection: ProjectionSelection) -> None:
    """Accept only the finding filters this index can resolve from retained facts."""

    if not isinstance(selection, ProjectionSelection):
        raise TypeError("finding selection must be a validated ProjectionSelection")
    if selection.selected_ids:
        raise ValueError(
            "indexed subject-findings browsing does not support exact subject selection"
        )
    if selection.languages:
        raise ValueError("indexed subject-findings browsing does not support language selection")
    default = ProjectionSelection()
    unsupported = sorted(
        name
        for name in ProjectionSelection.model_fields
        if name not in INDEXED_FINDING_SELECTION_FIELDS
        and getattr(selection, name) != getattr(default, name)
    )
    if unsupported:
        raise ValueError(
            "indexed finding selection supports module prefixes, categories, severities and "
            "rule IDs only; remove: " + ", ".join(unsupported)
        )
