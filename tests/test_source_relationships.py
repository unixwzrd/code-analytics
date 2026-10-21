"""Saved relationship inspection remains source-free and honest about resolution."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.code_browse import (
    CodeBrowseBindingError,
    CodeBrowseReader,
    CodeBrowseSelectionError,
)
from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness, Confidence
from code_analysis.project import discover_context
from code_analysis.report_models import AnalysisReport, FlowEdge, FlowKind, ResolutionState
from code_analysis.reports.json_report import canonical_json
from code_analysis.service import scan
from code_analysis.source_relationships import (
    SourceRelationshipPage,
    SourceRelationshipRequest,
    read_source_relationships,
)
from code_analysis.static_call_models import StaticCallSourceCoverageState


@pytest.fixture
def related_report(tmp_path: Path) -> AnalysisReport:
    path = tmp_path / "relations.py"
    path.write_text(
        "def helper(value):\n"
        "    return value + 1\n\n"
        "def caller(value):\n"
        "    return helper(value) + missing(value)\n\n"
        "def records():\n"
        "    row = {'name': 'Basil'}\n"
        "    row['name'] = 'Manuel'\n"
        "    return row['name']\n"
    )
    report = scan(discover_context(path, AnalysisConfig()))
    path.unlink()
    return AnalysisReport.model_validate_json(report.model_dump_json())


def request(report: AnalysisReport, name: str, **kwargs: object) -> SourceRelationshipRequest:
    subject = next(item for item in report.subjects if item.logical_name.endswith(":" + name))
    values = {
        "report_sha256": hashlib.sha256(canonical_json(report).encode()).hexdigest(),
        "revision": 3,
        "identity": subject.subject_id,
    }
    values.update(kwargs)
    return SourceRelationshipRequest.model_validate(values)


def test_callers_and_callees_preserve_evidence_basis(related_report):
    outgoing = read_source_relationships(related_report, request(related_report, "caller"))
    lexical = [row for row in outgoing.records if row.basis == "lexical-call-occurrence"]
    assert {row.label for row in lexical} == {"helper", "missing"}
    assert all(row.resolution == "unresolved" for row in lexical)
    name_matches = [row for row in outgoing.records if row.resolution == "name-matched"]
    assert any(row.target and row.target.label == "helper" for row in name_matches)
    assert not any(row.resolution == "resolved" for row in outgoing.records)
    incoming = read_source_relationships(related_report, request(related_report, "helper"))
    assert any(
        row.direction == "caller" and row.target.label == "caller"
        for row in incoming.records
        if row.target
    )
    assert incoming.revision == 3


def test_source_free_and_deterministic(related_report, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("saved relationship inspection must not read source")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    selection = request(related_report, "caller")
    first = read_source_relationships(related_report, selection)
    assert (
        first.model_dump_json()
        == read_source_relationships(related_report, selection).model_dump_json()
    )
    assert SourceRelationshipPage.model_validate_json(first.model_dump_json()) == first


def test_prepared_service_reader_does_not_reserialize_report(related_report, monkeypatch):
    selection = request(related_report, "caller")
    reader = CodeBrowseReader.from_report(related_report, selection.report_sha256)

    def forbidden(*args, **kwargs):
        raise AssertionError("prepared service state must not reserialize the report")

    monkeypatch.setattr("code_analysis.source_relationships.canonical_json", forbidden)
    result = read_source_relationships(related_report, selection, reader=reader)
    assert result.records


def test_empty_is_not_a_claim_of_complete_call_coverage(related_report):
    result = read_source_relationships(related_report, request(related_report, "records"))
    assert not any(row.direction in {"caller", "callee"} for row in result.records)
    assert any("not proof" in text for text in result.limitations)
    assert any("static-call-occurrences section:" in text for text in result.limitations)


def test_bounds_are_partial_and_counts_reconcile(related_report):
    result = read_source_relationships(
        related_report, request(related_report, "caller", max_records=1)
    )
    assert len(result.records) == 1
    assert result.omitted == result.eligible - 1 > 0
    assert result.truncated
    assert result.completeness is Completeness.PARTIAL
    with pytest.raises(ValidationError):
        SourceRelationshipPage.model_validate(
            {**result.model_dump(), "completeness": Completeness.COMPLETE}
        )
    with pytest.raises(ValidationError):
        SourceRelationshipPage.model_validate({**result.model_dump(), "omitted": 0})


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_records", True),
        ("max_records", 0),
        ("max_records", 501),
        ("revision", True),
        ("path", "../outside.py"),
        ("identity", ""),
        ("extra", "no"),
    ],
)
def test_strict_request(related_report, field, value):
    with pytest.raises(ValidationError):
        request(related_report, "caller", **{field: value})


def test_report_path_and_identity_binding(related_report):
    with pytest.raises(CodeBrowseBindingError):
        read_source_relationships(
            related_report, request(related_report, "caller", report_sha256="0" * 64)
        )
    with pytest.raises(CodeBrowseSelectionError):
        read_source_relationships(
            related_report, request(related_report, "caller", identity="not-recorded")
        )
    with pytest.raises(CodeBrowseSelectionError):
        read_source_relationships(
            related_report, request(related_report, "caller", path="another.py")
        )


def test_structure_roles_are_saved_and_navigable(related_report):
    assert related_report.data_structures
    structure = related_report.data_structures[0]
    selection = request(related_report, "records", identity=structure.structure_id)
    result = read_source_relationships(related_report, selection)
    assert result.selected.identity == structure.structure_id
    assert any(row.basis == "structure-role" for row in result.records)
    for row in result.records:
        if row.target:
            assert row.target.logical_path == result.selected.logical_path
        assert row.spans


def test_incoming_calls_do_not_use_suffix_or_fuzzy_matching(related_report):
    profiles = tuple(
        (
            item.model_copy(update={"callees": ("helper",)})
            if item.subject_id == request(related_report, "caller").identity
            else item
        )
        for item in related_report.function_profiles
    )
    report = related_report.model_copy(update={"function_profiles": profiles})
    result = read_source_relationships(report, request(report, "helper"))
    assert not any(row.direction == "caller" for row in result.records)


def test_ambiguous_names_are_not_given_an_invented_target(related_report):
    helper = next(
        item for item in related_report.subjects if item.logical_name == "relations:helper"
    )
    # An ambiguity fixture uses two independently recorded subject identities;
    # no altered source or additional inference is consulted by this view.
    alternative = helper.model_copy(update={"subject_id": "subject-" + "f" * 20})
    report = related_report.model_copy(
        update={
            "subjects": (*related_report.subjects, alternative),
        }
    )
    result = read_source_relationships(report, request(report, "caller"))
    rows = [row for row in result.records if row.label == "relations:helper"]
    assert rows and all(row.resolution == "ambiguous" and row.target is None for row in rows)


def test_unsupported_call_scope_is_disclosed_without_dropping_valid_rows(related_report):
    coverage = tuple(
        item.model_copy(
            update={
                "state": StaticCallSourceCoverageState.UNSUPPORTED,
                "observed_count": 0,
                "retained_count": 0,
                "omitted_count": 0,
                "limitations": ("Synthetic unsupported lexical scope.",),
            }
        )
        for item in related_report.static_call_coverage
    )
    report = related_report.model_copy(
        update={
            "static_call_coverage": coverage,
            "static_call_occurrences": (),
        }
    )
    result = read_source_relationships(report, request(report, "caller"))
    assert result.records
    assert "Selected file lexical-call coverage: unsupported." in result.limitations
    assert "Synthetic unsupported lexical scope." in result.limitations


def test_duplicate_row_identity_is_invalid(related_report):
    result = read_source_relationships(related_report, request(related_report, "caller"))
    with pytest.raises(ValidationError, match="identities must be unique"):
        SourceRelationshipPage.model_validate(
            {
                **result.model_dump(),
                "records": (result.records[0], result.records[0]),
                "eligible": 2,
                "omitted": 0,
                "truncated": False,
            }
        )


def test_explicit_structure_conversion_links_retain_both_saved_endpoints(related_report):
    structure = related_report.data_structures[0]
    source = request(related_report, "helper").identity
    target = request(related_report, "caller").identity
    flow = FlowEdge(
        flow_id="flow-" + "d" * 20,
        source_id=source,
        target_id=target,
        kind=FlowKind.CONVERSION,
        resolution=ResolutionState.RESOLVED,
        evidence=structure.evidence,
        confidence=Confidence.EXACT,
    )
    structures = tuple(
        (
            item.model_copy(update={"conversion_flow_ids": (flow.flow_id,)})
            if item.structure_id == structure.structure_id
            else item
        )
        for item in related_report.data_structures
    )
    report = related_report.model_copy(
        update={
            "flow_edges": (*related_report.flow_edges, flow),
            "data_structures": structures,
        }
    )
    result = read_source_relationships(
        report, request(report, "records", identity=structure.structure_id)
    )
    conversions = [row for row in result.records if row.record_id == flow.flow_id]
    assert {row.kind for row in conversions} == {"conversion-source", "conversion-target"}
    assert {row.target.label for row in conversions} == {"helper", "caller"}
