"""Source-free navigation storage for prepared static-graph node identities."""

from __future__ import annotations

import sqlite3
from typing import Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.models import Completeness, StrictModel
from code_analysis.report_models import AnalysisReport, SourceFileRecord
from code_analysis.ui_navigation import (
    NavigationItem,
    NavigationPage,
    NavigationUnavailable,
    navigation_items,
    ordered_navigation_items,
)

SUBJECT_TABLE_SQL = (
    "CREATE TABLE static_graph_navigation_subjects (node_id TEXT PRIMARY KEY NOT NULL, "
    "payload TEXT NOT NULL) WITHOUT ROWID"
)
ITEM_TABLE_SQL = (
    "CREATE TABLE static_graph_navigation_items (node_id TEXT NOT NULL, "
    "position INTEGER NOT NULL, payload TEXT NOT NULL, "
    "PRIMARY KEY (node_id, position)) WITHOUT ROWID"
)

_MAX_RECORDS = 1_000_000
_MAX_PAYLOAD_BYTES = 1024**2
_MAX_TOTAL_PAYLOAD_BYTES = 2 * 1024**3
_MAX_READ_BYTES = 8 * 1024**2
_UNAVAILABLE_REASON = "no recorded source-navigation mapping"


class GraphNavigationSubject(StrictModel):
    id: StrictStr = Field(min_length=1)
    available: StrictBool
    reason: StrictStr | None = None
    record_count: StrictInt = Field(ge=0, le=_MAX_RECORDS)

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.available == (self.reason is not None):
            raise ValueError("graph navigation availability requires exactly one reason state")
        if not self.available and self.reason != _UNAVAILABLE_REASON:
            raise ValueError("unavailable graph navigation reason is not canonical")
        if not self.available and self.record_count:
            raise ValueError("unavailable graph navigation cannot have records")
        return self


class GraphNavigationReceipt(StrictModel):
    identity_count: StrictInt = Field(ge=0, le=_MAX_RECORDS)
    record_count: StrictInt = Field(ge=0, le=_MAX_RECORDS)


def _checked_payload(payload: str) -> tuple[str, int]:
    if type(payload) is not str:
        raise ValueError("graph navigation payload must be text")
    size = len(payload.encode())
    if size > _MAX_PAYLOAD_BYTES:
        raise ValueError("graph navigation record exceeds one-MiB bound")
    return payload, size


def write_graph_navigation(
    connection: sqlite3.Connection, report: AnalysisReport
) -> GraphNavigationReceipt:
    """Materialize navigation once for the union of prepared graph node IDs."""

    connection.execute(SUBJECT_TABLE_SQL)
    connection.execute(ITEM_TABLE_SQL)
    node_ids = tuple(
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT node_id FROM static_graph_nodes ORDER BY node_id"
        )
    )
    if len(node_ids) > _MAX_RECORDS:
        raise ValueError("graph navigation identities exceed preparation bounds")
    selected = navigation_items(report, frozenset(node_ids))
    record_count = 0
    total_bytes = 0
    for node_id in node_ids:
        items = selected.get(node_id)
        ordered = ordered_navigation_items(items or ())
        if record_count + len(ordered) > _MAX_RECORDS:
            raise ValueError("graph navigation records exceed preparation bounds")
        subject = GraphNavigationSubject(
            id=node_id,
            available=items is not None,
            reason=None if items is not None else _UNAVAILABLE_REASON,
            record_count=len(ordered),
        )
        payload, size = _checked_payload(subject.model_dump_json())
        total_bytes += size
        if total_bytes > _MAX_TOTAL_PAYLOAD_BYTES:
            raise ValueError("graph navigation payload exceeds two-GiB preparation bound")
        connection.execute(
            "INSERT INTO static_graph_navigation_subjects VALUES (?, ?)",
            (node_id, payload),
        )
        for position, item in enumerate(ordered):
            payload, size = _checked_payload(item.model_dump_json())
            total_bytes += size
            if total_bytes > _MAX_TOTAL_PAYLOAD_BYTES:
                raise ValueError("graph navigation payload exceeds two-GiB preparation bound")
            connection.execute(
                "INSERT INTO static_graph_navigation_items VALUES (?, ?, ?)",
                (node_id, position, payload),
            )
        record_count += len(ordered)
    return GraphNavigationReceipt(identity_count=len(node_ids), record_count=record_count)


def _validate_item_source(
    connection: sqlite3.Connection,
    item: NavigationItem,
    sources: dict[str, SourceFileRecord],
) -> None:
    source = sources.get(item.span.path)
    if source is None:
        row = connection.execute(
            "SELECT payload FROM source_files WHERE path = ?", (item.span.path,)
        ).fetchone()
        if row is None:
            raise ValueError("graph navigation span has no recorded source file")
        payload, _size = _checked_payload(row[0])
        source = SourceFileRecord.model_validate_json(payload)
        sources[item.span.path] = source
    if source.logical_path != item.span.path or source.language is not item.span.language:
        raise ValueError("graph navigation span disagrees with recorded source file")
    if (item.span.end_line, item.span.end_column) < (
        item.span.start_line,
        item.span.start_column,
    ):
        raise ValueError("graph navigation span has reversed bounds")
    if source.physical_loc and (
        item.span.start_line > source.physical_loc or item.span.end_line > source.physical_loc
    ):
        raise ValueError("graph navigation span exceeds recorded source lines")


def validate_graph_navigation(
    connection: sqlite3.Connection, receipt: GraphNavigationReceipt
) -> None:
    for table, sql in (
        ("static_graph_navigation_subjects", SUBJECT_TABLE_SQL),
        ("static_graph_navigation_items", ITEM_TABLE_SQL),
    ):
        if connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
        ).fetchone() != (sql,):
            raise ValueError("graph navigation index schema mismatch")
    subject_count = connection.execute(
        "SELECT count(*) FROM static_graph_navigation_subjects"
    ).fetchone()[0]
    item_count = connection.execute(
        "SELECT count(*) FROM static_graph_navigation_items"
    ).fetchone()[0]
    if (subject_count, item_count) != (receipt.identity_count, receipt.record_count):
        raise ValueError("graph navigation counts disagree with receipt")
    graph_ids = {
        row[0] for row in connection.execute("SELECT DISTINCT node_id FROM static_graph_nodes")
    }
    subject_ids = {
        row[0] for row in connection.execute("SELECT node_id FROM static_graph_navigation_subjects")
    }
    if graph_ids != subject_ids:
        raise ValueError("graph navigation subjects disagree with prepared graph nodes")

    total_bytes = 0
    total_records = 0
    sources: dict[str, SourceFileRecord] = {}
    for node_id, subject_payload in connection.execute(
        "SELECT node_id, payload FROM static_graph_navigation_subjects ORDER BY node_id"
    ):
        subject_payload, size = _checked_payload(subject_payload)
        total_bytes += size
        if total_bytes > _MAX_TOTAL_PAYLOAD_BYTES:
            raise ValueError("graph navigation payload exceeds two-GiB preparation bound")
        subject = GraphNavigationSubject.model_validate_json(subject_payload)
        if subject.id != node_id:
            raise ValueError("graph navigation subject identity mismatch")
        rows = connection.execute(
            "SELECT position, payload FROM static_graph_navigation_items "
            "WHERE node_id = ? ORDER BY position",
            (node_id,),
        )
        previous: NavigationItem | None = None
        seen = 0
        for position, payload in rows:
            if position != seen:
                raise ValueError("graph navigation positions disagree with subject")
            payload, size = _checked_payload(payload)
            total_bytes += size
            if total_bytes > _MAX_TOTAL_PAYLOAD_BYTES:
                raise ValueError("graph navigation payload exceeds two-GiB preparation bound")
            item = NavigationItem.model_validate_json(payload)
            _validate_item_source(connection, item, sources)
            if previous is not None and ordered_navigation_items((previous, item)) != (
                previous,
                item,
            ):
                raise ValueError("graph navigation items are not canonically ordered")
            previous = item
            seen += 1
        if seen != subject.record_count:
            raise ValueError("graph navigation positions disagree with subject")
        total_records += seen
    if total_records != receipt.record_count:
        raise ValueError("graph navigation item total disagrees with receipt")


def read_graph_navigation(
    connection: sqlite3.Connection,
    receipt: GraphNavigationReceipt,
    identity: str,
    offset: int,
    completeness: Completeness,
) -> NavigationPage:
    if type(identity) is not str or not identity:
        raise NavigationUnavailable("selection has no recorded source-navigation mapping")
    if type(offset) is not int or offset < 0:
        raise ValueError("navigation offset must be a nonnegative integer")
    row = connection.execute(
        "SELECT payload FROM static_graph_navigation_subjects WHERE node_id = ?", (identity,)
    ).fetchone()
    if row is None:
        raise NavigationUnavailable("selection has no recorded source-navigation mapping")
    payload, total_bytes = _checked_payload(row[0])
    subject = GraphNavigationSubject.model_validate_json(payload)
    if subject.id != identity:
        raise ValueError("graph navigation subject identity mismatch")
    if receipt.identity_count == 0 or subject.record_count > receipt.record_count:
        raise ValueError("graph navigation subject disagrees with receipt")
    if not subject.available:
        raise NavigationUnavailable(subject.reason or _UNAVAILABLE_REASON)
    if offset >= subject.record_count:
        return NavigationPage(
            identity=identity,
            label=identity,
            completeness=completeness,
            offset=offset,
            eligible=subject.record_count,
            records=(),
            next_offset=None,
        )
    rows = connection.execute(
        "SELECT position, payload FROM static_graph_navigation_items "
        "WHERE node_id = ? AND position >= ? ORDER BY position LIMIT 25",
        (identity, offset),
    )
    items: list[NavigationItem] = []
    for expected, (position, item_payload) in enumerate(rows, start=offset):
        if position != expected:
            raise ValueError("graph navigation page positions are not contiguous")
        item_payload, size = _checked_payload(item_payload)
        total_bytes += size
        if total_bytes > _MAX_READ_BYTES:
            raise ValueError("graph navigation read exceeds eight-MiB byte budget")
        items.append(NavigationItem.model_validate_json(item_payload))
    expected_count = min(25, max(0, subject.record_count - offset))
    if len(items) != expected_count:
        raise ValueError("graph navigation page count disagrees with subject")
    if tuple(items) != ordered_navigation_items(tuple(items)):
        raise ValueError("graph navigation page is not canonically ordered")
    return NavigationPage(
        identity=identity,
        label=identity,
        completeness=completeness,
        offset=offset,
        eligible=subject.record_count,
        records=tuple(items),
        next_offset=(offset + len(items) if offset + len(items) < subject.record_count else None),
    )
