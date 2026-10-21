"""Saved source finding markers preserve binding, evidence and visible bounds."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from code_analysis.code_browse import (
    CodeBrowseBindingError,
    CodeBrowseReader,
    CodeBrowseSelectionError,
)
from code_analysis.models import Completeness, Confidence, Evidence, SourceLanguage, SourceSpan
from code_analysis.report_models import Finding
from code_analysis.reports.json_report import canonical_json
from code_analysis.source_findings import (
    MAX_PAGE_BYTES,
    SourceFindingPage,
    SourceFindingRequest,
    read_source_findings,
)


def _report(report, *, count=1, duplicates=False, long=False):
    source = report.source_files[0]
    evidence = Evidence(
        span=SourceSpan(
            path=source.logical_path,
            start_line=1,
            end_line=2,
            start_column=0,
            end_column=4,
            language=source.language,
        ),
        provider="synthetic",
        observation="function-level complexity",
        confidence=Confidence.EXACT,
        source_sha256=source.source_sha256,
        language=source.language,
    )
    findings = tuple(
        Finding(
            finding_id=f"finding-{index:04}",
            rule_id="CA100",
            category="complexity",
            severity="warning",
            title="Recorded function complexity" + (" title" * 100 if long else ""),
            explanation="Review the affected function, not an inferred faulty line."
            + (" detail" * 1000 if long else ""),
            module=source.module_name,
            evidence=(evidence, evidence) if duplicates else (evidence,),
            confidence=Confidence.EXACT,
            completeness=Completeness.COMPLETE,
        )
        for index in range(count)
    )
    return report.model_copy(update={"findings": findings, "completeness": Completeness.COMPLETE})


def _request(report, **updates):
    payload = {
        "report_sha256": hashlib.sha256(canonical_json(report).encode()).hexdigest(),
        "revision": 2,
        "path": report.source_files[0].logical_path,
    }
    payload.update(updates)
    return SourceFindingRequest.model_validate(payload)


def test_saved_only_deterministic_ranges_and_deduplication(ordinary_report):
    report = _report(ordinary_report, duplicates=True)
    page = read_source_findings(report, _request(report))
    assert page == read_source_findings(report, _request(report))
    assert page.eligible == 1 and page.omitted == 0 and not page.truncated
    assert page.completeness is Completeness.COMPLETE
    assert page.source_sha256 == report.source_files[0].source_sha256
    marker = page.records[0]
    assert (marker.start_line, marker.end_line) == (1, 2)
    assert len(marker.evidence_ids) == 1
    assert marker.label == "Recorded function complexity"
    assert "faulty statement" in page.limitations[0]
    assert (
        page.model_dump_json() == read_source_findings(report, _request(report)).model_dump_json()
    )


def test_empty_complete_and_partial_are_distinct(ordinary_report):
    complete = _report(ordinary_report, count=0)
    assert read_source_findings(complete, _request(complete)).completeness is Completeness.COMPLETE
    partial = complete.model_copy(update={"completeness": Completeness.PARTIAL})
    page = read_source_findings(partial, _request(partial))
    assert page.records == () and page.completeness is Completeness.PARTIAL
    assert not page.truncated
    failed = complete.model_copy(update={"completeness": Completeness.FAILED})
    assert read_source_findings(failed, _request(failed)).completeness is Completeness.FAILED


def test_prepared_reader_uses_prevalidated_canonical_binding(ordinary_report, monkeypatch):
    report = _report(ordinary_report)
    request = _request(report)
    reader = CodeBrowseReader.from_report(report, request.report_sha256)

    def unexpected_expansion(*args):
        raise AssertionError("prepared reads must not expand the full report")

    monkeypatch.setattr("code_analysis.source_findings.canonical_json", unexpected_expansion)
    assert read_source_findings(report, request, reader=reader).eligible == 1


def test_saved_projection_does_not_read_source(ordinary_report, monkeypatch):
    from pathlib import Path

    report = _report(ordinary_report)
    request = _request(report)

    def forbidden_read(*args, **kwargs):
        raise AssertionError("finding markers must use saved evidence only")

    monkeypatch.setattr(Path, "open", forbidden_read)
    assert read_source_findings(report, request).records


def test_record_bound_and_byte_bound_preserve_eligible_counts(ordinary_report):
    report = _report(ordinary_report, count=150, long=True)
    page = read_source_findings(report, _request(report, max_records=2))
    assert len(page.records) == 2 and page.eligible == 150 and page.omitted == 148
    assert page.completeness is Completeness.PARTIAL and page.truncated
    byte_page = read_source_findings(report, _request(report, max_records=1000))
    assert 2 < len(byte_page.records) < 150
    assert len(byte_page.model_dump_json().encode()) <= MAX_PAGE_BYTES
    assert byte_page.eligible == len(byte_page.records) + byte_page.omitted
    assert len(byte_page.records[0].label) <= 256
    assert len(byte_page.records[0].description) <= 2048


@pytest.mark.parametrize(
    "updates",
    [
        {"max_records": True},
        {"revision": True},
        {"max_records": 0},
        {"max_records": 1001},
        {"extra": "x"},
        {"path": "../secret.py"},
        {"path": "/absolute.py"},
        {"path": "a\\b.py"},
        {"path": "a//b.py"},
        {"path": "C:/x.py"},
        {"path": ""},
        {"report_sha256": "bad"},
    ],
)
def test_request_strict_bounds_and_paths(ordinary_report, updates):
    with pytest.raises(ValidationError):
        _request(ordinary_report, **updates)


def test_report_binding_and_missing_path_fail_closed(ordinary_report):
    report = _report(ordinary_report)
    with pytest.raises(CodeBrowseBindingError):
        read_source_findings(report, _request(report, report_sha256="0" * 64))
    with pytest.raises(CodeBrowseSelectionError):
        read_source_findings(report, _request(report, path="absent.py"))
    reader = CodeBrowseReader.from_report(report, _request(report).report_sha256)
    assert read_source_findings(report, _request(report), reader=reader).eligible == 1
    with pytest.raises(CodeBrowseBindingError):
        read_source_findings(report, _request(report, report_sha256="0" * 64), reader=reader)


@pytest.mark.parametrize("mismatch", ["hash", "language", "range"])
def test_evidence_binding_mismatch_fails(ordinary_report, mismatch):
    report = _report(ordinary_report)
    finding = report.findings[0]
    evidence = finding.evidence[0]
    if mismatch == "hash":
        evidence = evidence.model_copy(update={"source_sha256": "0" * 64})
    elif mismatch == "language":
        evidence = evidence.model_copy(
            update={
                "language": SourceLanguage.JAVASCRIPT,
                "span": evidence.span.model_copy(update={"language": SourceLanguage.JAVASCRIPT}),
            }
        )
    else:
        evidence = evidence.model_copy(
            update={"span": evidence.span.model_copy(update={"end_line": 999999})}
        )
    report = report.model_copy(
        update={"findings": (finding.model_copy(update={"evidence": (evidence,)}),)}
    )
    with pytest.raises(CodeBrowseBindingError):
        read_source_findings(report, _request(report))


@pytest.mark.parametrize(
    "change", ["counts", "duplicate", "truncated", "complete", "boolean", "identity"]
)
def test_page_rejects_inconsistent_contracts(ordinary_report, change):
    report = _report(ordinary_report)
    payload = read_source_findings(report, _request(report)).model_dump()
    if change == "counts":
        payload["eligible"] = 9
    elif change == "duplicate":
        payload["records"] *= 2
        payload["eligible"] = 2
    elif change == "truncated":
        payload["truncated"] = True
    elif change == "complete":
        payload.update(eligible=2, omitted=1, truncated=True)
    elif change == "boolean":
        payload["eligible"] = True
    else:
        payload["records"] = (
            payload["records"][0] | {"annotation_id": "finding-marker-" + "0" * 32},
        )
    with pytest.raises(ValidationError):
        SourceFindingPage.model_validate(payload)
