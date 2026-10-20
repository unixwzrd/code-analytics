"""Fault-injection tests for static comparison source binding validation."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest
from test_static_comparison_source_browsing import _build_index

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.models import Evidence, SourceSpan
from code_analysis.projection_models import ComparisonChange
from code_analysis.runtime_source_browse import require_comparison_source_side
from code_analysis.static_comparison_source import (
    ComparisonSourceSideState,
    StaticComparisonSourceQuery,
    validate_comparison_source_bindings,
)
from code_analysis.web_service import ReportViewService


def test_require_comparison_source_side_rejects_unknown_name() -> None:
    assert require_comparison_source_side("baseline") == "baseline"
    with pytest.raises(ValueError, match="baseline or candidate"):
        require_comparison_source_side("typo")


def test_binding_preparation_is_once_per_report_and_validation_is_joined(
    ordinary_report, tmp_path, monkeypatch
) -> None:
    from code_analysis import static_comparison_source as source

    original = source._source_authorities
    prepared = []

    def prepare(report):
        prepared.append(report)
        return original(report)

    monkeypatch.setattr(source, "_source_authorities", prepare)
    finding = ordinary_report.findings[0]
    candidate = ordinary_report.model_copy(
        update={"findings": (finding.model_copy(update={"title": "changed"}),)}
    )
    _, index, manifest = _build_index(ordinary_report, candidate, tmp_path)
    assert len(prepared) == 2
    assert prepared[0] is ordinary_report and prepared[1] is candidate
    statements = []
    with closing(sqlite3.connect(index.database)) as connection:
        connection.set_trace_callback(statements.append)
        source.validate_comparison_source_bindings(
            connection,
            retained=manifest.comparison.retained,
            baseline_sha256=manifest.report_sha256,
            candidate_sha256=manifest.comparison.candidate_sha256,
        )
    assert len(statements) == 7
    assert "LEFT JOIN comparison_rows" in statements[-1]


def test_index_rejects_tampered_binding_payload(ordinary_report, tmp_path) -> None:
    finding = ordinary_report.findings[0]
    changed = finding.model_copy(update={"title": finding.title + " changed"})
    candidate = ordinary_report.model_copy(update={"findings": (changed,)})
    _, index, manifest = _build_index(ordinary_report, candidate, tmp_path)
    database = index.database
    with closing(sqlite3.connect(database)) as connection, connection:
        row = connection.execute(
            "SELECT payload FROM comparison_source_bindings WHERE position = 0"
        ).fetchone()
        payload = json.loads(row[0])
        payload["subject_id"] = "foreign-subject"
        connection.execute(
            "UPDATE comparison_source_bindings SET payload = ? WHERE position = 0",
            (json.dumps(payload, separators=(",", ":"), sort_keys=True),),
        )
        with pytest.raises(ValueError, match="disagrees with comparison row"):
            validate_comparison_source_bindings(
                connection,
                retained=manifest.comparison.retained,
                baseline_sha256=manifest.report_sha256,
                candidate_sha256=manifest.comparison.candidate_sha256,
            )
    with pytest.raises(ValueError, match="database identity mismatch"):
        BrowseIndex.open(index.database.parent, report_sha256=manifest.report_sha256)


@pytest.mark.parametrize("side", ["baseline", "candidate"])
@pytest.mark.parametrize(
    "field,value",
    [("source_sha256", "0" * 64), ("logical_path", "foreign.py"), ("language", "javascript")],
)
def test_index_validates_option_against_its_own_source_inventory(
    ordinary_report, tmp_path, side, field, value
):
    from code_analysis.models import SourceLanguage
    from code_analysis.static_comparison_source import ComparisonSourcePreviewKind, _option_id

    finding = ordinary_report.findings[0]
    candidate = ordinary_report.model_copy(
        update={"findings": (finding.model_copy(update={"title": "changed"}),)}
    )
    service, index, manifest = _build_index(ordinary_report, candidate, tmp_path)
    position = next(
        number
        for number, row in enumerate(service.delta.records)
        if row.subject_id == finding.finding_id
    )
    with closing(sqlite3.connect(index.database)) as connection, connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM comparison_source_bindings WHERE position = ?", (position,)
            ).fetchone()[0]
        )
        option = payload[side]["options"][0]
        option[field] = value
        option["option_id"] = _option_id(
            logical_path=option["logical_path"],
            source_sha256=option["source_sha256"],
            language=SourceLanguage(option["language"]),
            start_line=option["start_line"],
            end_line=option["end_line"],
            preview_kind=ComparisonSourcePreviewKind(option["preview_kind"]),
        )
        # Keep exactly one well-formed option: the fault is authority, not ordering or ID syntax.
        payload[side]["options"] = [option]
        connection.execute(
            "UPDATE comparison_source_bindings SET payload = ? WHERE position = ?",
            (json.dumps(payload), position),
        )
        with pytest.raises(ValueError, match="saved report authority"):
            validate_comparison_source_bindings(
                connection,
                retained=manifest.comparison.retained,
                baseline_sha256=manifest.report_sha256,
                candidate_sha256=manifest.comparison.candidate_sha256,
            )


def test_index_rejects_dangling_binding_position(ordinary_report, tmp_path) -> None:
    finding = ordinary_report.findings[0]
    changed = finding.model_copy(update={"title": finding.title + " changed"})
    candidate = ordinary_report.model_copy(update={"findings": (changed,)})
    service = ReportViewService(ordinary_report, candidate)
    root = tmp_path / "dangling"
    manifest = build_browse_index(service, root)
    database = root / "index.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM comparison_source_bindings WHERE position = 0"
            ).fetchone()[0]
        )
        payload["position"] = 999
        connection.execute(
            "INSERT INTO comparison_source_bindings VALUES (?, ?)",
            (999, json.dumps(payload, separators=(",", ":"), sort_keys=True)),
        )
    with pytest.raises(ValueError):
        BrowseIndex.open(root, report_sha256=manifest.report_sha256)


def test_validate_rejects_wrong_report_hash(ordinary_report, tmp_path) -> None:
    finding = ordinary_report.findings[0]
    changed = finding.model_copy(update={"title": finding.title + " changed"})
    candidate = ordinary_report.model_copy(update={"findings": (changed,)})
    _, index, manifest = _build_index(ordinary_report, candidate, tmp_path)
    database = index.database
    with (
        closing(sqlite3.connect(database)) as connection,
        connection,
        pytest.raises(ValueError, match="baseline hash"),
    ):
        validate_comparison_source_bindings(
            connection,
            retained=manifest.comparison.retained,
            baseline_sha256="0" * 64,
            candidate_sha256=manifest.comparison.candidate_sha256,
        )


def test_option_hash_must_match_saved_authority(ordinary_report, tmp_path) -> None:
    finding = ordinary_report.findings[0]
    wrong_hash = finding.model_copy(
        update={
            "title": finding.title + " changed",
            "evidence": (
                finding.evidence[0].model_copy(update={"source_sha256": "0" * 64}),
                *finding.evidence[1:],
            ),
        }
    )
    candidate = ordinary_report.model_copy(update={"findings": (wrong_hash,)})
    service, _, _ = _build_index(ordinary_report, candidate, tmp_path)
    position = next(
        number
        for number, record in enumerate(service.delta.records)
        if record.subject_id == finding.finding_id
    )
    detail = service.static_comparison_source_detail(position)
    assert detail.candidate.state is ComparisonSourceSideState.NO_EVIDENCE


def test_omitted_option_count_when_more_than_two_hundred_spans(ordinary_report, tmp_path) -> None:
    finding = ordinary_report.findings[0]
    base = finding.evidence[0]
    evidence = tuple(
        Evidence(
            span=SourceSpan(
                path=base.span.path,
                start_line=10 + index,
                start_column=0,
                end_line=11 + index,
                end_column=0,
                language=base.span.language,
            ),
            provider="test",
            observation=f"span-{index}",
            confidence=base.confidence,
            source_sha256=base.source_sha256,
            language=base.language,
        )
        for index in range(205)
    )
    rich = finding.model_copy(update={"evidence": evidence})
    baseline = ordinary_report.model_copy(
        update={"findings": (rich, *ordinary_report.findings[1:])}
    )
    candidate = baseline.model_copy(
        update={
            "findings": (
                rich.model_copy(update={"title": rich.title + " changed"}),
                *ordinary_report.findings[1:],
            )
        }
    )
    service, index, _ = _build_index(baseline, candidate, tmp_path)
    position = next(
        number
        for number, record in enumerate(service.delta.records)
        if record.subject_id == finding.finding_id
    )
    detail = service.static_comparison_source_detail(position)
    assert len(detail.baseline.options) == 200
    assert detail.baseline.omitted_option_count == 5
    assert index.static_comparison_source_detail(position) == detail


def test_missing_side_never_borrows_other_side_options(ordinary_report, tmp_path) -> None:
    removed = ordinary_report.model_copy(update={"findings": ()})
    service, _, _ = _build_index(ordinary_report, removed, tmp_path)
    position = next(
        index
        for index, record in enumerate(service.delta.records)
        if record.change is ComparisonChange.REMOVED
    )
    detail = service.static_comparison_source_detail(position)
    assert detail.candidate.options == ()
    assert detail.candidate.state is ComparisonSourceSideState.MISSING_RECORD
    with pytest.raises(ValueError, match="side is absent"):
        from code_analysis.static_comparison_source import resolve_static_comparison_source_option

        resolve_static_comparison_source_option(
            detail,
            StaticComparisonSourceQuery(
                baseline_sha256=detail.baseline.report_sha256,
                candidate_sha256=detail.candidate.report_sha256,
                position=position,
                side="candidate",
                option_id=detail.baseline.options[0].option_id,
            ),
        )


def test_static_comparison_source_query_validates_option_pattern() -> None:
    with pytest.raises(ValueError):
        StaticComparisonSourceQuery(
            baseline_sha256="a" * 64,
            candidate_sha256="b" * 64,
            position=0,
            side="baseline",
            option_id="bad",
        )


@pytest.mark.parametrize("changed_side", ["baseline_sha256", "candidate_sha256"])
def test_preview_rejects_stale_report_pair(ordinary_report, tmp_path, changed_side):
    from code_analysis.static_comparison_source import resolve_static_comparison_source_option

    finding = ordinary_report.findings[0]
    candidate = ordinary_report.model_copy(
        update={"findings": (finding.model_copy(update={"title": "changed"}),)}
    )
    service, _, _ = _build_index(ordinary_report, candidate, tmp_path)
    position = next(
        number
        for number, record in enumerate(service.delta.records)
        if record.subject_id == finding.finding_id
    )
    detail = service.static_comparison_source_detail(position)
    query = StaticComparisonSourceQuery(
        baseline_sha256=detail.baseline.report_sha256,
        candidate_sha256=detail.candidate.report_sha256,
        position=position,
        side="baseline",
        option_id=detail.baseline.options[0].option_id,
    )
    with pytest.raises(ValueError, match="report pair mismatch"):
        resolve_static_comparison_source_option(
            detail, query.model_copy(update={changed_side: "0" * 64})
        )
