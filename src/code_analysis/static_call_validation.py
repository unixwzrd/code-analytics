"""Referential validation for persisted static call occurrence evidence."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from code_analysis.models import Evidence, SourceSpan
from code_analysis.static_call_models import (
    StaticCallOccurrence,
    StaticCallSourceCoverage,
    StaticCallSourceCoverageState,
)

if TYPE_CHECKING:
    from code_analysis.models import SourceLanguage
    from code_analysis.report_models import (
        AnalysisReport,
        AnalysisSubject,
        SectionCoverage,
        SourceFileRecord,
        SummaryCounts,
    )

_INCOMPLETE_COVERAGE_STATES = frozenset(
    {
        StaticCallSourceCoverageState.PARTIAL,
        StaticCallSourceCoverageState.FAILED,
        StaticCallSourceCoverageState.UNAVAILABLE,
        StaticCallSourceCoverageState.UNSUPPORTED,
        StaticCallSourceCoverageState.TRUNCATED,
    }
)


def static_call_identity(record: StaticCallOccurrence) -> str:
    """Return the canonical occurrence identity for one normalized observation."""

    payload = json.dumps(
        record.model_dump(mode="json", exclude={"occurrence_id"}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"call-occurrence-{hashlib.sha256(payload).hexdigest()[:20]}"


def _require_sorted_unique(label: str, values: tuple[str, ...]) -> None:
    if list(values) != sorted(values) or len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique and sorted")


def _span_within_source(source: SourceFileRecord, span: SourceSpan) -> bool:
    return not (
        span.path != source.logical_path
        or span.start_line < 1
        or span.end_line > source.physical_loc
    )


def _subject_evidences_source(
    evidence_items: tuple[Evidence, ...],
    *,
    logical_path: str,
    source_sha256: str,
) -> bool:
    return any(
        item.span.path == logical_path and item.source_sha256 == source_sha256
        for item in evidence_items
    )


def _validate_occurrence_against_source(
    occurrence: StaticCallOccurrence,
    source: SourceFileRecord,
) -> None:
    if occurrence.occurrence_id != static_call_identity(occurrence):
        raise ValueError(f"static call occurrence identity mismatch: {occurrence.occurrence_id}")
    evidence = occurrence.evidence[0]
    if (
        occurrence.language is not source.language
        or occurrence.module_name != source.module_name
        or evidence.span.path != source.logical_path
        or evidence.source_sha256 != source.source_sha256
    ):
        raise ValueError(
            f"static call occurrence {occurrence.occurrence_id} disagrees with its source file"
        )
    if not _span_within_source(source, evidence.span):
        raise ValueError(
            f"static call occurrence {occurrence.occurrence_id} span is outside source bounds"
        )
    for scope in occurrence.scope_path:
        if scope.definition_line < 1 or scope.definition_line > source.physical_loc:
            raise ValueError(
                f"static call occurrence {occurrence.occurrence_id} scope is outside source"
            )


def _validate_occurrence_enclosing_subject(
    occurrence: StaticCallOccurrence,
    source: SourceFileRecord,
    subject_by_id: dict[str, AnalysisSubject],
) -> None:
    if occurrence.enclosing_subject_id is None:
        return
    subject = subject_by_id.get(occurrence.enclosing_subject_id)
    if subject is None:
        raise ValueError(
            f"static call occurrence {occurrence.occurrence_id} "
            "references unknown enclosing subject"
        )
    if subject.language is not occurrence.language or subject.module != occurrence.module_name:
        raise ValueError(
            f"static call occurrence {occurrence.occurrence_id} "
            "enclosing subject language or module disagrees"
        )
    if not _subject_evidences_source(
        subject.evidence,
        logical_path=source.logical_path,
        source_sha256=source.source_sha256,
    ):
        raise ValueError(
            f"static call occurrence {occurrence.occurrence_id} "
            "enclosing subject lacks evidence for the source"
        )


def _index_occurrence_contributions(
    occurrences: tuple[StaticCallOccurrence, ...],
    source_by_id: dict[str, SourceFileRecord],
    subject_by_id: dict[str, AnalysisSubject],
) -> tuple[dict[str, int], set[SourceLanguage]]:
    retained_by_source: dict[str, int] = {}
    contribution_languages: set[SourceLanguage] = set()
    for occurrence in occurrences:
        source = source_by_id.get(occurrence.source_file_id)
        if source is None:
            raise ValueError(
                f"static call occurrence {occurrence.occurrence_id} references unknown source"
            )
        _validate_occurrence_against_source(occurrence, source)
        _validate_occurrence_enclosing_subject(occurrence, source, subject_by_id)
        retained_by_source[occurrence.source_file_id] = (
            retained_by_source.get(occurrence.source_file_id, 0) + 1
        )
        contribution_languages.add(occurrence.language)
    return retained_by_source, contribution_languages


def _validate_coverage_row(
    row: StaticCallSourceCoverage,
    source_by_id: dict[str, SourceFileRecord],
    retained_by_source: dict[str, int],
    *,
    section_state_complete: bool,
) -> SourceLanguage:
    source = source_by_id.get(row.source_file_id)
    if source is None:
        raise ValueError(f"static call coverage references unknown source: {row.source_file_id}")
    if row.language is not source.language:
        raise ValueError(f"static call coverage language disagrees for {row.source_file_id}")
    retained = retained_by_source.get(row.source_file_id, 0)
    if row.retained_count != retained:
        raise ValueError(f"static call coverage retained_count disagrees for {row.source_file_id}")
    if section_state_complete and row.state in _INCOMPLETE_COVERAGE_STATES:
        raise ValueError(
            "complete static-call-occurrences section cannot hide incomplete source coverage"
        )
    return row.language


def _summarize_coverage_rows(
    coverage_rows: tuple[StaticCallSourceCoverage, ...],
    source_by_id: dict[str, SourceFileRecord],
    retained_by_source: dict[str, int],
    *,
    section_state_complete: bool,
    contribution_languages: set[SourceLanguage],
) -> tuple[int, int]:
    observed_total = 0
    omitted_total = 0
    for row in coverage_rows:
        language = _validate_coverage_row(
            row,
            source_by_id,
            retained_by_source,
            section_state_complete=section_state_complete,
        )
        observed_total += row.observed_count
        omitted_total += row.omitted_count
        contribution_languages.add(language)
    return observed_total, omitted_total


def _require_section_languages_cover_contributions(
    contribution_languages: set[SourceLanguage],
    section_languages: set[SourceLanguage],
) -> None:
    if contribution_languages and not contribution_languages.issubset(section_languages):
        raise ValueError(
            "static-call-occurrences section languages must cover contributing languages"
        )


def _require_coverage_for_selected_sources(
    source_files: tuple[SourceFileRecord, ...],
    section_languages: set[SourceLanguage],
    coverage_rows: tuple[StaticCallSourceCoverage, ...],
) -> None:
    selected_sources = tuple(item for item in source_files if item.language in section_languages)
    covered_ids = {row.source_file_id for row in coverage_rows}
    missing = sorted(
        item.source_file_id for item in selected_sources if item.source_file_id not in covered_ids
    )
    if missing:
        raise ValueError(
            "static call coverage must account for every selected source: " + ", ".join(missing)
        )


def _validate_static_call_summary(
    summary: SummaryCounts,
    occurrence_count: int,
    observed_total: int,
    omitted_total: int,
) -> None:
    if summary.static_call_occurrences != occurrence_count:
        raise ValueError("summary static_call_occurrences disagrees with occurrence rows")
    if summary.static_calls_observed != observed_total:
        raise ValueError("summary static_calls_observed disagrees with source coverage")
    if summary.static_calls_omitted != omitted_total:
        raise ValueError("summary static_calls_omitted disagrees with source coverage")


def validate_static_call_occurrences(report: AnalysisReport) -> None:
    """Validate static call tuples against report sources, subjects, and section coverage."""

    from code_analysis.report_models import SectionState

    occurrences = report.static_call_occurrences
    coverage_rows = report.static_call_coverage
    coverage_by_name = {item.section: item for item in report.section_coverage}
    section: SectionCoverage | None = coverage_by_name.get("static-call-occurrences")
    if section is None:
        if occurrences or coverage_rows:
            raise ValueError(
                "static call occurrences require a static-call-occurrences section entry"
            )
        return

    _require_sorted_unique(
        "static call occurrence_id", tuple(item.occurrence_id for item in occurrences)
    )
    _require_sorted_unique(
        "static call source coverage",
        tuple(item.source_file_id for item in coverage_rows),
    )

    if section.state in {SectionState.DISABLED, SectionState.UNAVAILABLE}:
        if occurrences or coverage_rows:
            raise ValueError(
                "disabled or unavailable static-call-occurrences section cannot contain rows"
            )
        return

    source_by_id = {item.source_file_id: item for item in report.source_files}
    subject_by_id = {item.subject_id: item for item in report.subjects}
    section_languages = set(section.languages)

    retained_by_source, contribution_languages = _index_occurrence_contributions(
        occurrences, source_by_id, subject_by_id
    )
    observed_total, omitted_total = _summarize_coverage_rows(
        coverage_rows,
        source_by_id,
        retained_by_source,
        section_state_complete=section.state is SectionState.COMPLETE,
        contribution_languages=contribution_languages,
    )
    _require_section_languages_cover_contributions(contribution_languages, section_languages)
    _require_coverage_for_selected_sources(report.source_files, section_languages, coverage_rows)
    _validate_static_call_summary(report.summary, len(occurrences), observed_total, omitted_total)


__all__ = ["static_call_identity", "validate_static_call_occurrences"]
