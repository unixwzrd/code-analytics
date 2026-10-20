from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

import code_analysis.static_graph_navigation as graph_navigation
from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness, SourceLanguage
from code_analysis.project import discover_context
from code_analysis.service import scan
from code_analysis.static_graph_browse import write_static_graphs
from code_analysis.static_graph_navigation import (
    GraphNavigationReceipt,
    GraphNavigationSubject,
    read_graph_navigation,
    validate_graph_navigation,
    write_graph_navigation,
)
from code_analysis.ui_navigation import NavigationItem, NavigationUnavailable, navigation_page

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_TABLE_SQL = (
    "CREATE TABLE source_files (path TEXT PRIMARY KEY NOT NULL, "
    "payload TEXT NOT NULL) WITHOUT ROWID"
)


def _prepared_connection(report):
    connection = sqlite3.connect(":memory:")
    connection.execute(SOURCE_TABLE_SQL)
    for source in report.source_files:
        connection.execute(
            "INSERT INTO source_files VALUES (?, ?)",
            (source.logical_path, source.model_dump_json()),
        )
    write_static_graphs(connection, report)
    return connection


@pytest.fixture(scope="module")
def structure_report():
    return scan(discover_context(FIXTURES / "structure_project", AnalysisConfig()))


@pytest.fixture
def navigation_index(structure_report):
    connection = _prepared_connection(structure_report)
    receipt = write_graph_navigation(connection, structure_report)
    yield connection, receipt
    connection.close()


def test_receipt_and_subject_models_are_strict_and_bounded() -> None:
    assert GraphNavigationReceipt(identity_count=0, record_count=0).record_count == 0
    for model, payload in (
        (GraphNavigationReceipt, {"identity_count": True, "record_count": 0}),
        (GraphNavigationReceipt, {"identity_count": 0, "record_count": 1_000_001}),
        (
            GraphNavigationSubject,
            {"id": "x", "available": False, "reason": "invented", "record_count": 0},
        ),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_writer_matches_public_navigation_and_validator(structure_report, navigation_index) -> None:
    connection, receipt = navigation_index
    validate_graph_navigation(connection, receipt)
    assert (
        receipt.identity_count
        == connection.execute("SELECT count(DISTINCT node_id) FROM static_graph_nodes").fetchone()[
            0
        ]
    )
    available = connection.execute(
        "SELECT node_id FROM static_graph_navigation_subjects "
        "WHERE json_extract(payload, '$.available') = 1 ORDER BY node_id"
    ).fetchall()
    assert available
    for (identity,) in available:
        indexed = read_graph_navigation(
            connection, receipt, identity, 0, structure_report.completeness
        )
        assert indexed == navigation_page(structure_report, identity)


def test_unknown_and_recorded_unavailable_are_distinct(navigation_index) -> None:
    connection, receipt = navigation_index
    with pytest.raises(NavigationUnavailable):
        read_graph_navigation(connection, receipt, "not-prepared", 0, Completeness.COMPLETE)
    row = connection.execute(
        "SELECT node_id FROM static_graph_navigation_subjects "
        "WHERE json_extract(payload, '$.available') = 0 LIMIT 1"
    ).fetchone()
    if row is not None:
        with pytest.raises(NavigationUnavailable, match="no recorded"):
            read_graph_navigation(connection, receipt, row[0], 0, Completeness.COMPLETE)


def test_validation_rejects_noncontiguous_positions(navigation_index) -> None:
    connection, receipt = navigation_index
    node_id, position = connection.execute(
        "SELECT node_id, position FROM static_graph_navigation_items LIMIT 1"
    ).fetchone()
    connection.execute(
        "UPDATE static_graph_navigation_items SET position = 999 "
        "WHERE node_id = ? AND position = ?",
        (node_id, position),
    )
    with pytest.raises(ValueError, match="positions"):
        validate_graph_navigation(connection, receipt)


@pytest.mark.parametrize("corruption", ("missing-path", "language", "end-line"))
def test_validation_rejects_invalid_source_binding(navigation_index, corruption) -> None:
    connection, receipt = navigation_index
    node_id, position, payload = connection.execute(
        "SELECT node_id, position, payload FROM static_graph_navigation_items LIMIT 1"
    ).fetchone()
    item = NavigationItem.model_validate_json(payload)
    source_payload = connection.execute(
        "SELECT payload FROM source_files WHERE path = ?", (item.span.path,)
    ).fetchone()[0]
    source = graph_navigation.SourceFileRecord.model_validate_json(source_payload)
    if corruption == "missing-path":
        span = item.span.model_copy(update={"path": "absent.py"})
        match = "no recorded source"
    elif corruption == "language":
        language = (
            SourceLanguage.JAVASCRIPT
            if item.span.language is SourceLanguage.PYTHON
            else SourceLanguage.PYTHON
        )
        span = item.span.model_copy(update={"language": language})
        match = "disagrees with recorded"
    else:
        assert source.physical_loc > 0
        span = item.span.model_copy(update={"end_line": source.physical_loc + 1})
        match = "exceeds recorded source lines"
    connection.execute(
        "UPDATE static_graph_navigation_items SET payload = ? "
        "WHERE node_id = ? AND position = ?",
        (item.model_copy(update={"span": span}).model_dump_json(), node_id, position),
    )
    with pytest.raises(ValueError, match=match):
        validate_graph_navigation(connection, receipt)


def test_validation_rejects_noncanonical_payload_order(navigation_index) -> None:
    connection, receipt = navigation_index
    node_id = connection.execute(
        "SELECT node_id FROM static_graph_navigation_items "
        "GROUP BY node_id HAVING count(*) >= 2 LIMIT 1"
    ).fetchone()[0]
    rows = connection.execute(
        "SELECT position, payload FROM static_graph_navigation_items "
        "WHERE node_id = ? ORDER BY position LIMIT 2",
        (node_id,),
    ).fetchall()
    connection.execute(
        "UPDATE static_graph_navigation_items SET payload = ? WHERE node_id = ? AND position = ?",
        (rows[1][1], node_id, rows[0][0]),
    )
    connection.execute(
        "UPDATE static_graph_navigation_items SET payload = ? WHERE node_id = ? AND position = ?",
        (rows[0][1], node_id, rows[1][0]),
    )
    with pytest.raises(ValueError, match="canonically ordered"):
        validate_graph_navigation(connection, receipt)


@pytest.mark.parametrize("corruption", ("missing", "extra"))
def test_validation_rejects_graph_subject_union_mismatch(navigation_index, corruption) -> None:
    connection, receipt = navigation_index
    if corruption == "missing":
        node_id = connection.execute(
            "SELECT node_id FROM static_graph_navigation_subjects LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM static_graph_navigation_items WHERE node_id = ?", (node_id,)
        )
        connection.execute(
            "DELETE FROM static_graph_navigation_subjects WHERE node_id = ?", (node_id,)
        )
        adjusted = receipt.model_copy(
            update={
                "identity_count": receipt.identity_count - 1,
                "record_count": connection.execute(
                    "SELECT count(*) FROM static_graph_navigation_items"
                ).fetchone()[0],
            }
        )
    else:
        subject = GraphNavigationSubject(
            id="extra",
            available=False,
            reason="no recorded source-navigation mapping",
            record_count=0,
        )
        connection.execute(
            "INSERT INTO static_graph_navigation_subjects VALUES (?, ?)",
            (subject.id, subject.model_dump_json()),
        )
        adjusted = receipt.model_copy(update={"identity_count": receipt.identity_count + 1})
    with pytest.raises(ValueError, match="prepared graph nodes"):
        validate_graph_navigation(connection, adjusted)


def test_validation_rejects_receipt_count_mismatch(navigation_index) -> None:
    connection, receipt = navigation_index
    forged = receipt.model_copy(update={"record_count": receipt.record_count + 1})
    with pytest.raises(ValueError, match="counts disagree"):
        validate_graph_navigation(connection, forged)


def test_reader_rejects_malformed_payload_before_returning(navigation_index) -> None:
    connection, receipt = navigation_index
    identity = connection.execute(
        "SELECT node_id FROM static_graph_navigation_subjects LIMIT 1"
    ).fetchone()[0]
    connection.execute(
        "UPDATE static_graph_navigation_subjects SET payload = '{}' WHERE node_id = ?",
        (identity,),
    )
    with pytest.raises(ValidationError):
        read_graph_navigation(connection, receipt, identity, 0, Completeness.COMPLETE)


@pytest.mark.parametrize("table", ("subject", "item"))
def test_reader_rejects_non_text_sqlite_payload(navigation_index, table) -> None:
    connection, receipt = navigation_index
    if table == "subject":
        identity = connection.execute(
            "SELECT node_id FROM static_graph_navigation_subjects LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "UPDATE static_graph_navigation_subjects SET payload = ? WHERE node_id = ?",
            (sqlite3.Binary(b"{}"), identity),
        )
    else:
        identity, position = connection.execute(
            "SELECT node_id, position FROM static_graph_navigation_items LIMIT 1"
        ).fetchone()
        connection.execute(
            "UPDATE static_graph_navigation_items SET payload = ? "
            "WHERE node_id = ? AND position = ?",
            (sqlite3.Binary(b"{}"), identity, position),
        )
    with pytest.raises(ValueError, match="payload must be text"):
        read_graph_navigation(connection, receipt, identity, 0, Completeness.COMPLETE)


def test_huge_offset_matches_public_empty_page(structure_report, navigation_index) -> None:
    connection, receipt = navigation_index
    identity = connection.execute(
        "SELECT node_id FROM static_graph_navigation_subjects "
        "WHERE json_extract(payload, '$.available') = 1 LIMIT 1"
    ).fetchone()[0]
    offset = 10**100
    assert read_graph_navigation(
        connection, receipt, identity, offset, structure_report.completeness
    ) == navigation_page(structure_report, identity, offset)


def test_reader_pages_at_25_without_reading_target_source(navigation_index, monkeypatch) -> None:
    connection, _receipt = navigation_index
    item = NavigationItem.model_validate_json(
        connection.execute("SELECT payload FROM static_graph_navigation_items LIMIT 1").fetchone()[
            0
        ]
    )
    identity = "synthetic-many"
    subject = GraphNavigationSubject(id=identity, available=True, reason=None, record_count=30)
    connection.execute(
        "INSERT INTO static_graph_navigation_subjects VALUES (?, ?)",
        (identity, subject.model_dump_json()),
    )
    for position in range(30):
        connection.execute(
            "INSERT INTO static_graph_navigation_items VALUES (?, ?, ?)",
            (identity, position, item.model_dump_json()),
        )
    receipt = GraphNavigationReceipt(identity_count=2, record_count=30)

    def fail_source_read(*_args, **_kwargs):
        raise AssertionError("target source read attempted")

    monkeypatch.setattr(Path, "read_text", fail_source_read)
    first = read_graph_navigation(connection, receipt, identity, 0, Completeness.PARTIAL)
    second = read_graph_navigation(connection, receipt, identity, 25, Completeness.PARTIAL)
    assert len(first.records) == 25 and first.next_offset == 25
    assert len(second.records) == 5 and second.next_offset is None
    assert first.completeness is Completeness.PARTIAL


def test_reader_charges_aggregate_budget_before_parsing_next_item(
    navigation_index, monkeypatch
) -> None:
    connection, receipt = navigation_index
    identity = connection.execute(
        "SELECT node_id FROM static_graph_navigation_items "
        "GROUP BY node_id HAVING count(*) >= 2 LIMIT 1"
    ).fetchone()[0]
    subject_payload = connection.execute(
        "SELECT payload FROM static_graph_navigation_subjects WHERE node_id = ?",
        (identity,),
    ).fetchone()[0]
    item_payloads = [
        row[0]
        for row in connection.execute(
            "SELECT payload FROM static_graph_navigation_items "
            "WHERE node_id = ? ORDER BY position LIMIT 2",
            (identity,),
        )
    ]
    parsed = 0
    validate = NavigationItem.model_validate_json

    def count_validation(payload):
        nonlocal parsed
        parsed += 1
        return validate(payload)

    monkeypatch.setattr(
        graph_navigation,
        "_MAX_READ_BYTES",
        len(subject_payload.encode()) + len(item_payloads[0].encode()),
    )
    monkeypatch.setattr(NavigationItem, "model_validate_json", count_validation)
    with pytest.raises(ValueError, match="eight-MiB byte budget"):
        read_graph_navigation(connection, receipt, identity, 0, Completeness.COMPLETE)
    assert parsed == 1


def test_validation_rejects_individual_payload_bound(navigation_index) -> None:
    connection, receipt = navigation_index
    identity = connection.execute(
        "SELECT node_id FROM static_graph_navigation_subjects LIMIT 1"
    ).fetchone()[0]
    connection.execute(
        "UPDATE static_graph_navigation_subjects SET payload = ? WHERE node_id = ?",
        ("x" * (1024**2 + 1), identity),
    )
    with pytest.raises(ValueError, match="one-MiB"):
        validate_graph_navigation(connection, receipt)


def test_preparation_rejects_identity_and_total_payload_limits(
    structure_report, monkeypatch
) -> None:
    connection = _prepared_connection(structure_report)
    monkeypatch.setattr(graph_navigation, "_MAX_RECORDS", 0)
    with pytest.raises(ValueError, match="identities exceed preparation bounds"):
        write_graph_navigation(connection, structure_report)
    connection.close()

    connection = _prepared_connection(structure_report)
    monkeypatch.setattr(graph_navigation, "_MAX_RECORDS", 1_000_000)
    monkeypatch.setattr(graph_navigation, "_MAX_TOTAL_PAYLOAD_BYTES", 0)
    with pytest.raises(ValueError, match="two-GiB preparation bound"):
        write_graph_navigation(connection, structure_report)
    connection.close()
