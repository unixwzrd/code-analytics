"""Independent fail-closed checks for comparison source ownership."""

import json
import sqlite3
from contextlib import closing

import pytest
from test_runtime_comparison_source_browsing import build_comparison_index

from code_analysis.runtime_browse_storage import validate_runtime_records


@pytest.mark.parametrize("side", ("typo", "Candidate", "", None, True))
def test_comparison_source_rejects_unknown_side(ordinary_report, tmp_path, side):
    _, index, delta = build_comparison_index(ordinary_report, tmp_path)
    with pytest.raises(ValueError, match="side"):
        index.runtime_comparison_source(delta.records[0].frame_identity, side)


def test_frame_map_cannot_hide_an_existing_candidate(ordinary_report, tmp_path):
    _, index, _ = build_comparison_index(ordinary_report, tmp_path)
    with (
        closing(sqlite3.connect(index.database)) as original,
        closing(sqlite3.connect(":memory:")) as altered,
    ):
        original.backup(altered)
        row = altered.execute(
            "SELECT section, position, payload FROM runtime_records "
            "WHERE section = 'comparison-frames'"
        ).fetchone()
        assert row is not None
        value = json.loads(row[2])
        assert value["candidate_frame_id"] is not None
        value["candidate_frame_id"] = None
        altered.execute(
            "UPDATE runtime_records SET payload = ? WHERE section = ? AND position = ?",
            (json.dumps(value), row[0], row[1]),
        )
        with pytest.raises(ValueError):
            validate_runtime_records(altered, index.manifest.runtime)


@pytest.mark.parametrize("damage", ("empty-map", "lookup-id"))
def test_comparison_inventory_cannot_be_bypassed(ordinary_report, tmp_path, damage):
    _, index, _ = build_comparison_index(ordinary_report, tmp_path)
    summary = index.manifest.runtime
    with (
        closing(sqlite3.connect(index.database)) as original,
        closing(sqlite3.connect(":memory:")) as altered,
    ):
        original.backup(altered)
        if damage == "empty-map":
            altered.execute(
                "DELETE FROM runtime_records WHERE section IN ('comparison', 'comparison-frames')"
            )
            summary = summary.model_copy(
                update={
                    "comparison": summary.comparison.model_copy(
                        update={"records": 0, "comparison_frame_count": 0}
                    )
                }
            )
        else:
            altered.execute(
                "UPDATE runtime_records SET frame_a = 'incorrect' "
                "WHERE section = 'candidate-hotspots'"
            )
        with pytest.raises(ValueError):
            validate_runtime_records(altered, summary)


def test_comparison_validation_uses_bulk_inventory_reads(ordinary_report, tmp_path):
    _, index, _ = build_comparison_index(ordinary_report, tmp_path)
    queries = []
    with closing(sqlite3.connect(index.database)) as connection:
        connection.set_trace_callback(queries.append)
        validate_runtime_records(connection, index.manifest.runtime)
    assert not any("AND frame_a =" in query for query in queries)
