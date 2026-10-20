from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_analysis.models import Confidence, Evidence, SourceLanguage, SourceSpan
from code_analysis.report_models import AnalysisReport, SectionState
from code_analysis.reports.json_report import canonical_json, load_json, render_json
from code_analysis.static_call_models import (
    StaticCallEvaluationContext,
    StaticCallOccurrence,
    StaticCallScope,
    StaticCallScopeKind,
    StaticCallSourceCoverage,
    StaticCallSourceCoverageState,
    StaticCallSyntaxKind,
)
from code_analysis.static_call_validation import (
    static_call_identity,
    validate_static_call_occurrences,
)

GOLDEN = Path(__file__).parent / "golden" / "minimal-report.json"


def _call_span(**changes: object) -> SourceSpan:
    payload = {
        "path": "visible.py",
        "start_line": 2,
        "start_column": 4,
        "end_line": 2,
        "end_column": 9,
        "language": SourceLanguage.PYTHON,
    }
    payload.update(changes)
    return SourceSpan.model_validate(payload)


def _evidence(**changes: object) -> Evidence:
    source_sha256 = "e36f383170cdf8112ac70983d167249fc26c6e4dfb56414de6798e4042eca0de"
    payload = {
        "span": _call_span(),
        "provider": "static-call-synthetic",
        "observation": "syntactic call site",
        "confidence": Confidence.EXACT,
        "source_sha256": source_sha256,
        "language": SourceLanguage.PYTHON,
    }
    payload.update(changes)
    return Evidence.model_validate(payload)


def _module_scope() -> StaticCallScope:
    return StaticCallScope(
        kind=StaticCallScopeKind.MODULE,
        name=None,
        definition_line=1,
        definition_column=0,
    )


def _function_scope() -> StaticCallScope:
    return StaticCallScope(
        kind=StaticCallScopeKind.FUNCTION,
        name="visible",
        definition_line=1,
        definition_column=0,
    )


def _occurrence(**changes: object) -> StaticCallOccurrence:
    payload: dict[str, object] = {
        "occurrence_id": "call-occurrence-00000000000000000000",
        "analysis_identity_sha256": "f" * 64,
        "language": SourceLanguage.PYTHON,
        "source_file_id": "source-file-333306e76beb370627f1",
        "module_name": "visible",
        "enclosing_subject_id": "subject-8b4834729f1b95435d1e",
        "callee_spelling": "helper",
        "syntax_kind": StaticCallSyntaxKind.CALL,
        "evaluation_context": StaticCallEvaluationContext.FUNCTION_BODY,
        "conditional": False,
        "deferred": False,
        "evaluation_unknown": False,
        "scope_path": (_module_scope(), _function_scope()),
        "evidence": (_evidence(),),
        "limitations": (),
    }
    payload.update(changes)
    record = StaticCallOccurrence.model_validate(payload)
    identity = static_call_identity(record)
    if payload.get("occurrence_id") == "call-occurrence-00000000000000000000":
        return record.model_copy(update={"occurrence_id": identity})
    return record


def _zero_coverage(**changes: object) -> StaticCallSourceCoverage:
    payload = {
        "source_file_id": "source-file-333306e76beb370627f1",
        "language": SourceLanguage.PYTHON,
        "state": StaticCallSourceCoverageState.COMPLETE,
        "observed_count": 0,
        "retained_count": 0,
        "omitted_count": 0,
        "limitations": (),
    }
    payload.update(changes)
    return StaticCallSourceCoverage.model_validate(payload)


def _disabled_static_call_section() -> dict[str, object]:
    return {
        "section": "static-call-occurrences",
        "state": "disabled",
        "required": True,
        "parsed": 0,
        "skipped": 0,
        "failed": 0,
        "unresolved": 0,
        "truncated": 0,
        "diagnostic_codes": [],
        "languages": ["python"],
    }


def _synthetic_shell() -> AnalysisReport:
    payload = json.loads(GOLDEN.read_text(encoding="utf-8"))
    payload["static_call_occurrences"] = []
    payload["static_call_coverage"] = []
    summary = payload["summary"]
    summary["static_call_occurrences"] = 0
    summary["static_calls_observed"] = 0
    summary["static_calls_omitted"] = 0
    static_section = _disabled_static_call_section()
    sections: list[dict[str, object]] = []
    replaced = False
    for section in payload["section_coverage"]:
        if section["section"] == "static-call-occurrences":
            sections.append(static_section)
            replaced = True
        else:
            sections.append(section)
    if not replaced:
        inserted: list[dict[str, object]] = []
        for section in sections:
            inserted.append(section)
            if section["section"] == "r1-static-analysis":
                inserted.append(static_section)
        sections = inserted
    payload["section_coverage"] = sections
    return load_json(json.dumps(payload))


def _with_static_calls(
    report: AnalysisReport,
    *,
    section_state: SectionState = SectionState.COMPLETE,
    occurrences: tuple[StaticCallOccurrence, ...] = (),
    coverage: tuple[StaticCallSourceCoverage, ...] | None = None,
) -> AnalysisReport:
    if coverage is None:
        coverage = (
            _zero_coverage(retained_count=len(occurrences), observed_count=len(occurrences)),
        )
    section_coverage = tuple(
        (
            item.model_copy(update={"state": section_state, "parsed": len(report.source_files)})
            if item.section == "static-call-occurrences"
            else item
        )
        for item in report.section_coverage
    )
    observed = sum(item.observed_count for item in coverage)
    omitted = sum(item.omitted_count for item in coverage)
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "static_call_occurrences": occurrences,
            "static_call_coverage": coverage,
            "section_coverage": section_coverage,
            "summary": {
                **report.summary.model_dump(),
                "static_call_occurrences": len(occurrences),
                "static_calls_observed": observed,
                "static_calls_omitted": omitted,
            },
        }
    )


def test_static_call_identity_rejects_stale_occurrence_id() -> None:
    occurrence = _occurrence()
    assert occurrence.occurrence_id == static_call_identity(occurrence)
    tampered = occurrence.model_copy(
        update={"evidence": (occurrence.evidence[0].model_copy(update={"observation": "changed"}),)}
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        AnalysisReport.model_validate(
            _with_static_calls(
                _synthetic_shell(),
                occurrences=(
                    tampered.model_copy(update={"occurrence_id": occurrence.occurrence_id}),
                ),
            ).model_dump()
        )


def test_zero_call_complete_source_is_valid() -> None:
    report = _with_static_calls(_synthetic_shell())
    validate_static_call_occurrences(report)
    AnalysisReport.model_validate(report.model_dump())


def test_absent_section_with_empty_tuples_is_valid() -> None:
    from types import SimpleNamespace

    report = SimpleNamespace(
        static_call_occurrences=(),
        static_call_coverage=(),
        section_coverage=(),
        source_files=(),
        subjects=(),
        summary=SimpleNamespace(
            static_call_occurrences=0,
            static_calls_observed=0,
            static_calls_omitted=0,
        ),
    )
    validate_static_call_occurrences(report)


def test_evidence_catalog_round_trips_static_call_occurrences() -> None:
    occurrence = _occurrence()
    report = _with_static_calls(_synthetic_shell(), occurrences=(occurrence,))
    rendered = render_json(report, compact=True)
    retained = load_json(rendered)
    assert retained.static_call_occurrences == report.static_call_occurrences
    assert canonical_json(retained) == canonical_json(report)


def test_dangling_source_rejected() -> None:
    occurrence = _occurrence(source_file_id="source-file-aaaaaaaaaaaaaaaaaaaa")
    occurrence = occurrence.model_copy(update={"occurrence_id": static_call_identity(occurrence)})
    with pytest.raises(ValueError, match="unknown source"):
        AnalysisReport.model_validate(
            _with_static_calls(_synthetic_shell(), occurrences=(occurrence,)).model_dump()
        )


def test_wrong_language_module_path_or_hash_rejected() -> None:
    occurrence = _occurrence(
        module_name="other.module",
    )
    occurrence = occurrence.model_copy(update={"occurrence_id": static_call_identity(occurrence)})
    with pytest.raises(ValueError, match="disagrees with its source file"):
        AnalysisReport.model_validate(
            _with_static_calls(_synthetic_shell(), occurrences=(occurrence,)).model_dump()
        )

    occurrence = _occurrence(
        evidence=(
            _evidence(
                span=_call_span(path="missing.py"),
            ),
        ),
    )
    occurrence = occurrence.model_copy(update={"occurrence_id": static_call_identity(occurrence)})
    with pytest.raises(ValueError, match="disagrees with its source file"):
        AnalysisReport.model_validate(
            _with_static_calls(_synthetic_shell(), occurrences=(occurrence,)).model_dump()
        )

    occurrence = _occurrence(
        evidence=(_evidence(source_sha256="b" * 64),),
    )
    occurrence = occurrence.model_copy(update={"occurrence_id": static_call_identity(occurrence)})
    with pytest.raises(ValueError, match="disagrees with its source file"):
        AnalysisReport.model_validate(
            _with_static_calls(_synthetic_shell(), occurrences=(occurrence,)).model_dump()
        )


def test_out_of_range_and_reversed_span_rejected() -> None:
    occurrence = _occurrence(
        evidence=(
            _evidence(span=_call_span(start_line=9, end_line=9, start_column=0, end_column=1)),
        ),
    )
    occurrence = occurrence.model_copy(update={"occurrence_id": static_call_identity(occurrence)})
    with pytest.raises(ValueError, match="outside source bounds"):
        AnalysisReport.model_validate(
            _with_static_calls(_synthetic_shell(), occurrences=(occurrence,)).model_dump()
        )

    with pytest.raises(ValueError, match="forward"):
        _occurrence(
            evidence=(
                _evidence(span=_call_span(start_line=2, end_line=2, start_column=8, end_column=4)),
            ),
        )


def test_dangling_enclosing_subject_rejected() -> None:
    occurrence = _occurrence(enclosing_subject_id="subject-aaaaaaaaaaaaaaaaaaaa")
    occurrence = occurrence.model_copy(update={"occurrence_id": static_call_identity(occurrence)})
    with pytest.raises(ValueError, match="unknown enclosing subject"):
        AnalysisReport.model_validate(
            _with_static_calls(_synthetic_shell(), occurrences=(occurrence,)).model_dump()
        )


def test_duplicate_and_unsorted_occurrence_ids_rejected() -> None:
    first = _occurrence()
    second = _occurrence(
        callee_spelling="other",
        evidence=(_evidence(span=_call_span(start_column=10, end_column=15)),),
    )
    with pytest.raises(ValueError, match="occurrence_id"):
        AnalysisReport.model_validate(
            _with_static_calls(_synthetic_shell(), occurrences=(second, first)).model_dump()
        )

    with pytest.raises(ValueError, match="occurrence_id"):
        AnalysisReport.model_validate(
            _with_static_calls(_synthetic_shell(), occurrences=(first, first)).model_dump()
        )


def test_source_coverage_omission_and_summary_contradictions_rejected() -> None:
    with pytest.raises(ValueError, match="account for every selected source"):
        AnalysisReport.model_validate(
            _with_static_calls(_synthetic_shell(), coverage=()).model_dump()
        )

    occurrence = _occurrence()
    report = _with_static_calls(_synthetic_shell(), occurrences=(occurrence,))
    bad_summary = report.model_copy(
        update={"summary": report.summary.model_copy(update={"static_calls_observed": 0})}
    )
    with pytest.raises(ValueError, match="static_calls_observed"):
        validate_static_call_occurrences(bad_summary)

    coverage_payload = _zero_coverage(retained_count=1, observed_count=1).model_dump()
    coverage_payload["retained_count"] = 0
    bad_coverage = StaticCallSourceCoverage.model_construct(**coverage_payload)
    bad_report = report.model_copy(update={"static_call_coverage": (bad_coverage,)})
    with pytest.raises(ValueError, match="retained_count disagrees"):
        validate_static_call_occurrences(bad_report)


def test_disabled_section_cannot_retain_rows() -> None:
    occurrence = _occurrence()
    shell = _synthetic_shell()
    section_coverage = tuple(
        (
            item.model_copy(update={"state": SectionState.DISABLED, "parsed": 0})
            if item.section == "static-call-occurrences"
            else item
        )
        for item in shell.section_coverage
    )
    payload = shell.model_dump()
    payload.update(
        {
            "static_call_occurrences": (occurrence,),
            "static_call_coverage": (),
            "section_coverage": section_coverage,
            "summary": shell.summary.model_copy(
                update={
                    "static_call_occurrences": 1,
                    "static_calls_observed": 0,
                    "static_calls_omitted": 0,
                }
            ),
        }
    )
    report = AnalysisReport.model_construct(**payload)
    with pytest.raises(ValueError, match="cannot contain rows"):
        validate_static_call_occurrences(report)


def test_complete_section_cannot_hide_incomplete_source_coverage() -> None:
    coverage = (
        _zero_coverage(
            state=StaticCallSourceCoverageState.PARTIAL,
            limitations=("bounded traversal",),
        ),
    )
    with pytest.raises(ValueError, match="cannot hide incomplete"):
        AnalysisReport.model_validate(
            _with_static_calls(_synthetic_shell(), coverage=coverage).model_dump()
        )


def test_section_languages_must_cover_contributions() -> None:
    occurrence = _occurrence()
    report = _with_static_calls(_synthetic_shell(), occurrences=(occurrence,))
    report = report.model_copy(
        update={
            "section_coverage": tuple(
                (
                    item.model_copy(
                        update={
                            "state": SectionState.COMPLETE,
                            "languages": (SourceLanguage.JAVASCRIPT,),
                        }
                    )
                    if item.section == "static-call-occurrences"
                    else item
                )
                for item in report.section_coverage
            )
        }
    )
    with pytest.raises(ValueError, match="must cover contributing languages"):
        validate_static_call_occurrences(report)


def test_validate_static_call_occurrences_regression_messages() -> None:
    validate_static_call_occurrences(_with_static_calls(_synthetic_shell()))

    shell = _synthetic_shell()
    section_coverage = tuple(
        (
            item.model_copy(
                update={"state": SectionState.COMPLETE, "parsed": len(shell.source_files)}
            )
            if item.section == "static-call-occurrences"
            else item
        )
        for item in shell.section_coverage
    )
    missing_coverage = shell.model_copy(
        update={
            "static_call_occurrences": (),
            "static_call_coverage": (),
            "section_coverage": section_coverage,
            "summary": shell.summary.model_copy(
                update={
                    "static_call_occurrences": 0,
                    "static_calls_observed": 0,
                    "static_calls_omitted": 0,
                }
            ),
        }
    )
    with pytest.raises(ValueError) as exc_info:
        validate_static_call_occurrences(missing_coverage)
    assert str(exc_info.value) == (
        "static call coverage must account for every selected source: "
        "source-file-333306e76beb370627f1"
    )

    occurrence = _occurrence()
    report = _with_static_calls(_synthetic_shell(), occurrences=(occurrence,))
    with pytest.raises(ValueError) as exc_info:
        validate_static_call_occurrences(
            report.model_copy(
                update={"summary": report.summary.model_copy(update={"static_call_occurrences": 0})}
            )
        )
    assert str(exc_info.value) == ("summary static_call_occurrences disagrees with occurrence rows")


def test_unsorted_coverage_ids_rejected() -> None:
    shell = _synthetic_shell()
    second_source = shell.source_files[0].model_copy(
        update={
            "source_file_id": "source-file-ffffffffffffffffffff",
            "logical_path": "second.py",
            "module_name": "second",
        }
    )
    shell = shell.model_copy(
        update={
            "source_files": (*shell.source_files, second_source),
            "summary": shell.summary.model_copy(update={"source_files": 2}),
        }
    )
    second = _zero_coverage(source_file_id="source-file-ffffffffffffffffffff")
    first = _zero_coverage()
    with pytest.raises(ValueError, match="source_file_id records must be unique and sorted"):
        AnalysisReport.model_validate(
            _with_static_calls(shell, coverage=(second, first)).model_dump()
        )
