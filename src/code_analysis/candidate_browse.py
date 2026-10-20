"""Disposable candidate policy facts; selection never reloads source or a report."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from code_analysis.candidate_policy import (
    DECISION_RANK,
    CandidatePolicyFacts,
    suppresses_unchanged_candidate,
)
from code_analysis.decisions import EffectiveDecision
from code_analysis.projection_models import (
    ProjectionScalar,
    ProjectionSelection,
    ScalarKind,
    TableRow,
)
from code_analysis.projections.candidates import ranked_candidates
from code_analysis.report_models import AnalysisReport, RefactorCandidateState
from code_analysis.reports.projection_views import _scalar_text

CANDIDATE_POLICY_SQL = (
    "CREATE TABLE candidate_policy_rows (position INTEGER PRIMARY KEY, "
    "payload TEXT NOT NULL, reconsideration_signature_sha256 TEXT NOT NULL) WITHOUT ROWID"
)


def validate_candidate_selection(selection: ProjectionSelection) -> None:
    allowed = {
        "languages",
        "candidate_module_patterns",
        "candidate_exclude_patterns",
        "candidate_states",
        "candidate_decisions",
        "candidate_kinds",
        "cross_module_only",
        "production_only",
        "min_shared_dimensions",
        "prefer_existing_owner",
    }
    defaults = ProjectionSelection().model_dump()
    if any(
        value != defaults[name]
        for name, value in selection.model_dump().items()
        if name not in allowed
    ):
        raise ValueError(
            "indexed candidate worklist requires its dedicated filters and "
            "explicit ledger selection"
        )
    if set(selection.candidate_states) - {item.value for item in RefactorCandidateState}:
        raise ValueError("unknown candidate state")
    if set(selection.candidate_decisions) - set(DECISION_RANK):
        raise ValueError("unknown candidate decision")
    if any(
        len(pattern) > 2048 or any(ord(char) < 32 for char in pattern)
        for pattern in (*selection.candidate_module_patterns, *selection.candidate_exclude_patterns)
    ):
        raise ValueError("candidate patterns must be bounded single-line logical patterns")
    if len(selection.candidate_module_patterns) + len(selection.candidate_exclude_patterns) > 200:
        raise ValueError("too many candidate module patterns")


def write_candidate_policies(
    connection: sqlite3.Connection,
    report: AnalysisReport,
    rows: tuple[TableRow, ...],
    signatures: Mapping[str, str],
) -> None:
    connection.execute(CANDIDATE_POLICY_SQL)
    if not rows:
        return
    facts = {
        item.candidate.candidate_id: item.policy
        for item in ranked_candidates(report, ProjectionSelection(), {}, None)
    }
    for position, row in enumerate(rows):
        payload = facts[row.row_id].model_dump_json()
        signature = signatures.get(row.row_id)
        if (
            len(payload.encode()) > 1024**2
            or not isinstance(signature, str)
            or len(signature) != 64
            or any(character not in "0123456789abcdef" for character in signature)
        ):
            raise ValueError("candidate policy exceeds one MiB")
        connection.execute(
            "INSERT INTO candidate_policy_rows VALUES (?, ?, ?)", (position, payload, signature)
        )


def validate_candidate_policies(connection: sqlite3.Connection, count: int) -> None:
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'candidate_policy_rows'"
    ).fetchone() != (CANDIDATE_POLICY_SQL,):
        raise ValueError("candidate policy table schema mismatch")
    expected = (count, 0 if count else None, count - 1 if count else None)
    if (
        connection.execute(
            "SELECT count(*), min(position), max(position) FROM candidate_policy_rows"
        ).fetchone()
        != expected
    ):
        raise ValueError("candidate policy rows disagree with receipt")
    associations = connection.execute(
        "SELECT p.payload, p.reconsideration_signature_sha256, s.payload "
        "FROM candidate_policy_rows p "
        "LEFT JOIN static_projection_rows s ON s.position = p.position "
        "AND s.name = 'candidate-worklist' ORDER BY p.position"
    )
    seen = set()
    for payload, signature, row_payload in associations:
        if any(
            not isinstance(value, str) or len(value.encode()) > 1024**2
            for value in (payload, row_payload)
        ):
            raise ValueError("candidate policy association exceeds byte bound or is absent")
        if (
            not isinstance(signature, str)
            or len(signature) != 64
            or any(character not in "0123456789abcdef" for character in signature)
        ):
            raise ValueError("candidate policy signature is invalid")
        facts = CandidatePolicyFacts.model_validate_json(payload)
        row = TableRow.model_validate_json(row_payload)
        if (
            facts.candidate_id != row.row_id
            or facts.candidate_id in seen
            or facts.decision_name != "undecided"
        ):
            raise ValueError("candidate policy identity/decision association mismatch")
        seen.add(facts.candidate_id)


def effective_candidate_policy(
    facts: CandidatePolicyFacts, effective: EffectiveDecision | None
) -> CandidatePolicyFacts:
    decision_name = (
        "changed-since-decision"
        if effective is not None and effective.changed_since_decision
        else effective.record.decision.value if effective is not None else "undecided"
    )
    return facts.model_copy(update={"decision_name": decision_name})


def _suppressed(effective: EffectiveDecision | None, selection: ProjectionSelection) -> bool:
    return suppresses_unchanged_candidate(
        effective.record.decision if effective is not None else None,
        changed_since_decision=effective.changed_since_decision if effective is not None else False,
        requested_decisions=selection.candidate_decisions,
    )


def decision_aware_candidate_row(
    row: TableRow, policy: CandidatePolicyFacts, column_ids: tuple[str, ...]
) -> TableRow:
    updates = {"decision": policy.decision_name}
    updates["decision-rank"] = DECISION_RANK[policy.decision_name]
    cells = list(row.cells)
    for column, value in updates.items():
        try:
            position = column_ids.index(column)
        except ValueError as exc:
            raise ValueError("candidate worklist has no decision column") from exc
        cells[position] = ProjectionScalar(
            kind=ScalarKind.STRING if isinstance(value, str) else ScalarKind.INTEGER,
            string_value=value if isinstance(value, str) else None,
            integer_value=value if isinstance(value, int) else None,
        )
    return row.model_copy(update={"cells": tuple(cells)})


def selected_candidate_positions(
    connection: sqlite3.Connection,
    selection: ProjectionSelection,
    query: str,
    decisions: Mapping[str, EffectiveDecision] | None = None,
    *,
    column_ids: tuple[str, ...] = (),
) -> tuple[int, ...]:
    validate_candidate_selection(selection)
    positions = []
    statement = (
        "SELECT p.position, p.payload, s.payload, s.search_text FROM candidate_policy_rows p "
        "JOIN static_projection_rows s ON s.position = p.position "
        "AND s.name = 'candidate-worklist'"
    )
    rows = connection.execute(
        statement if decisions is not None else statement + " WHERE instr(s.search_text, ?) > 0",
        () if decisions is not None else (query.casefold(),),
    )
    for position, payload, row_payload, saved_search_text in rows:
        if any(
            not isinstance(value, str) or len(value.encode()) > 1024**2
            for value in (payload, row_payload)
        ):
            raise ValueError("candidate policy exceeds byte bound")
        facts = CandidatePolicyFacts.model_validate_json(payload)
        if facts.decision_name != "undecided":
            raise ValueError("report-only candidate index cannot imply a selected ledger")
        effective = decisions.get(facts.candidate_id) if decisions is not None else None
        policy = effective_candidate_policy(facts, effective)
        if _suppressed(effective, selection):
            continue
        if decisions is None:
            if not isinstance(saved_search_text, str):
                raise ValueError("candidate search index is malformed")
            search_text = saved_search_text
        else:
            row = decision_aware_candidate_row(
                TableRow.model_validate_json(row_payload), policy, column_ids
            )
            search_text = " ".join(
                (row.row_id, *(_scalar_text(cell) for cell in row.cells))
            ).casefold()
        if query.casefold() in search_text and policy.matches(selection):
            positions.append((policy.ordering(selection.prefer_existing_owner), position))
        if len(positions) > 100_000:
            raise ValueError("candidate policy inventory exceeds preparation bound")
    positions.sort()
    return tuple(position for _, position in positions)
