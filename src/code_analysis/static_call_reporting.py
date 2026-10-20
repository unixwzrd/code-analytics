"""Assemble bounded static call occurrence evidence into authoritative reports."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from code_analysis import __version__
from code_analysis.config import LanguageSelection
from code_analysis.context import AnalysisContext
from code_analysis.languages.base import FrontendContext
from code_analysis.languages.javascript.call_occurrences import (
    javascript_static_call_provider_identity,
)
from code_analysis.languages.javascript.call_occurrences import (
    static_call_batch_from_source as javascript_static_call_batch_from_source,
)
from code_analysis.languages.python.call_occurrences import static_call_batch_from_source
from code_analysis.languages.registry import frontend_for
from code_analysis.languages.ruby.call_occurrences import static_call_batch_from_driver
from code_analysis.models import (
    AvailabilityState,
    Completeness,
    Confidence,
    Diagnostic,
    DiagnosticLevel,
    Evidence,
    ProviderAvailability,
    ProviderResult,
    ProviderState,
    SourceLanguage,
    SourceParseState,
    SourceSpan,
)
from code_analysis.project import selected_languages
from code_analysis.report_models import (
    AnalysisReport,
    AnalysisSubject,
    SectionCoverage,
    SectionState,
    SourceFileRecord,
    SummaryCounts,
)
from code_analysis.static_call_models import (
    StaticCallOccurrence,
    StaticCallSourceCoverage,
    StaticCallSourceCoverageState,
)
from code_analysis.static_call_normalization import (
    JavascriptStaticCallProviderRecord,
    NormalizedStaticCallFact,
    PythonStaticCallProviderRecord,
    RubyStaticCallProviderRecord,
    StaticCallNormalizationError,
    StaticCallProviderRecords,
    compute_analysis_identity_sha256,
    normalize_javascript_call_fact,
    normalize_python_call_fact,
    normalize_ruby_call_fact,
)
from code_analysis.static_call_validation import static_call_identity

_MAX_SOURCE_BYTES = 4 * 1024 * 1024
_PROVIDER_ID = "static-call-occurrences"
_EVIDENCE_PROVIDER = "static-call-occurrences"
_RUNTIME_BINDING_LIMITATION = "runtime-binding-is-not-established-by-static-call-evidence"
_GLOBAL_RETENTION_LIMITATION = "static-call-global-retention-limit-exceeded"
_INCOMPLETE_BATCH_LIMITATIONS = frozenset(
    {
        "javascript-parse-failed",
        "parse-contains-errors",
        "parse-tree-contains-errors-or-missing-nodes",
        "python-syntax-error",
    }
)


@dataclass(frozen=True, slots=True)
class _RubyCollectionRuntime:
    executable: str
    expected_prism_version: str


@dataclass(frozen=True, slots=True)
class _SubjectSpanIndex:
    entries: tuple[tuple[SourceSpan, str], ...]

    @classmethod
    def empty(cls) -> _SubjectSpanIndex:
        return cls(())


@dataclass(frozen=True, slots=True)
class _SourceCollection:
    coverage: StaticCallSourceCoverage
    occurrences: tuple[StaticCallOccurrence, ...]
    diagnostics: tuple[Diagnostic, ...]


def integrate_static_call_report(
    context: AnalysisContext, report: AnalysisReport
) -> AnalysisReport:
    """Attach static call occurrence evidence for full-scan commands."""

    selected = _section_languages(context)
    if not selected:
        return report
    collection_languages = _collection_languages(report, selected)
    if not collection_languages:
        return report
    return _assemble_static_call_report(context, report, collection_languages=collection_languages)


def _assemble_static_call_report(
    context: AnalysisContext,
    report: AnalysisReport,
    *,
    collection_languages: tuple[SourceLanguage, ...],
) -> AnalysisReport:
    provider_records, ruby_runtime = _resolve_provider_records(context, collection_languages)
    analysis_identity = compute_analysis_identity_sha256(
        tool_version=report.tool_version,
        configuration_digest_sha256=context.config.identity(),
        providers=provider_records,
    )
    subject_index_by_source = _subject_index_by_source(report.subjects)
    sources = _ordered_collection_sources(report, collection_languages)
    collected, global_diagnostics = _collect_all_sources(
        context,
        sources,
        subject_index_by_source=subject_index_by_source,
        analysis_identity=analysis_identity,
        provider_records=provider_records,
        ruby_runtime=ruby_runtime,
        global_cap=context.config.max_static_call_occurrences,
    )
    return _attach_static_call_payload(
        context,
        report,
        collected=collected,
        global_diagnostics=global_diagnostics,
        languages=collection_languages,
        parsed=len(sources),
    )


def _section_languages(context: AnalysisContext) -> tuple[SourceLanguage, ...]:
    mapping = {
        LanguageSelection.PYTHON: SourceLanguage.PYTHON,
        LanguageSelection.JAVASCRIPT: SourceLanguage.JAVASCRIPT,
        LanguageSelection.RUBY: SourceLanguage.RUBY,
    }
    return tuple(
        sorted(
            {mapping[item] for item in selected_languages(context.config) if item in mapping},
            key=lambda item: item.value,
        )
    )


def _collection_languages(
    report: AnalysisReport,
    selected: tuple[SourceLanguage, ...],
) -> tuple[SourceLanguage, ...]:
    discovered = {item.language for item in report.source_files}
    allowed = set(selected)
    return tuple(
        sorted(
            (language for language in discovered if language in allowed),
            key=lambda item: item.value,
        )
    )


def _ordered_collection_sources(
    report: AnalysisReport,
    collection_languages: tuple[SourceLanguage, ...],
) -> tuple[SourceFileRecord, ...]:
    allowed = set(collection_languages)
    return tuple(
        sorted(
            (item for item in report.source_files if item.language in allowed),
            key=lambda item: (item.logical_path, item.language.value),
        )
    )


def _subject_index_by_source(
    subjects: tuple[AnalysisSubject, ...],
) -> dict[tuple[SourceLanguage, str, str, str], _SubjectSpanIndex]:
    grouped: dict[tuple[SourceLanguage, str, str, str], list[tuple[SourceSpan, str]]] = {}
    for subject in subjects:
        for evidence in subject.evidence:
            key = (
                subject.language,
                subject.module,
                evidence.source_sha256,
                evidence.span.path,
            )
            grouped.setdefault(key, []).append((evidence.span, subject.subject_id))
    return {key: _SubjectSpanIndex(tuple(entries)) for key, entries in grouped.items()}


def _resolve_provider_records(
    context: AnalysisContext,
    languages: tuple[SourceLanguage, ...],
) -> tuple[StaticCallProviderRecords, _RubyCollectionRuntime | None]:
    python: PythonStaticCallProviderRecord | None = None
    javascript: JavascriptStaticCallProviderRecord | None = None
    ruby: RubyStaticCallProviderRecord | None = None
    ruby_runtime: _RubyCollectionRuntime | None = None
    if SourceLanguage.PYTHON in languages:
        python = PythonStaticCallProviderRecord(
            frontend_id="python-ast",
            parser="ast",
            python_version=platform.python_version(),
        )
    if SourceLanguage.JAVASCRIPT in languages:
        identity = javascript_static_call_provider_identity()
        javascript = JavascriptStaticCallProviderRecord(
            available=identity.available,
            grammar_abi=identity.grammar_abi,
            grammar_version=identity.grammar_version,
            parser_version=identity.parser_version,
        )
    if SourceLanguage.RUBY in languages:
        frontend = frontend_for(SourceLanguage.RUBY)
        availability = frontend.availability(
            FrontendContext(context.config, context.config.identity())
        )
        prism_version = _prism_version_from_availability(availability)
        ruby = RubyStaticCallProviderRecord(
            provider_id=frontend.frontend_id,
            availability_state=availability.state.value,
            ruby_version=availability.version,
            prism_version=prism_version,
        )
        if (
            availability.state is AvailabilityState.AVAILABLE
            and availability.executable is not None
            and prism_version is not None
        ):
            ruby_runtime = _RubyCollectionRuntime(availability.executable, prism_version)
    return StaticCallProviderRecords(python=python, javascript=javascript, ruby=ruby), ruby_runtime


def _prism_version_from_availability(availability: ProviderAvailability) -> str | None:
    detail = availability.detail or ""
    prefix = "Prism "
    if detail.startswith(prefix):
        value = detail[len(prefix) :].strip()
        return value or None
    return None


def _collect_all_sources(
    context: AnalysisContext,
    sources: tuple[SourceFileRecord, ...],
    *,
    subject_index_by_source: dict[tuple[SourceLanguage, str, str, str], _SubjectSpanIndex],
    analysis_identity: str,
    provider_records: StaticCallProviderRecords,
    ruby_runtime: _RubyCollectionRuntime | None,
    global_cap: int,
) -> tuple[list[_SourceCollection], tuple[Diagnostic, ...]]:
    global_remaining = global_cap
    collected: list[_SourceCollection] = []
    global_diagnostics: list[Diagnostic] = []
    retention_exhausted = False
    for source in sources:
        per_source_cap = max(1, global_remaining)
        result = _collect_source(
            context,
            source,
            subject_index=subject_index_by_source.get(
                (source.language, source.module_name, source.source_sha256, source.logical_path),
                _SubjectSpanIndex.empty(),
            ),
            analysis_identity=analysis_identity,
            max_occurrences=per_source_cap,
            retain_occurrences=global_remaining > 0,
            provider_records=provider_records,
            ruby_runtime=ruby_runtime,
        )
        collected.append(result)
        if global_remaining > 0:
            global_remaining = max(0, global_remaining - result.coverage.retained_count)
        if global_remaining == 0 and _global_retention_limit_active(collected):
            retention_exhausted = True
    if retention_exhausted:
        global_diagnostics.append(
            Diagnostic(
                code="static-call-global-retention-limit",
                level=DiagnosticLevel.WARNING,
                message="Static call retention reached the configured global limit",
                provider=_PROVIDER_ID,
            )
        )
    return collected, tuple(global_diagnostics)


def _global_retention_limit_active(collected: list[_SourceCollection]) -> bool:
    return any(
        item.coverage.state is StaticCallSourceCoverageState.TRUNCATED
        and item.coverage.omitted_count > 0
        for item in collected
    )


def _global_cap_retention_hit(coverage_rows: tuple[StaticCallSourceCoverage, ...]) -> bool:
    return any(
        row.state is StaticCallSourceCoverageState.TRUNCATED and row.omitted_count > 0
        for row in coverage_rows
    )


def _aggregate_collected_batches(
    collected: list[_SourceCollection],
    global_diagnostics: tuple[Diagnostic, ...],
) -> tuple[
    tuple[StaticCallOccurrence, ...],
    tuple[StaticCallSourceCoverage, ...],
    tuple[Diagnostic, ...],
]:
    occurrences = tuple(
        sorted(
            (item for batch in collected for item in batch.occurrences),
            key=lambda item: item.occurrence_id,
        )
    )
    coverage_rows = tuple(
        sorted((item.coverage for item in collected), key=lambda row: row.source_file_id)
    )
    diagnostics = tuple(
        sorted(
            (
                *(item for batch in collected for item in batch.diagnostics),
                *global_diagnostics,
            ),
            key=lambda item: (item.path or "", item.code, item.message),
        )
    )
    return occurrences, coverage_rows, diagnostics


def _static_call_run_identity_sha256(
    context: AnalysisContext,
    report: AnalysisReport,
    coverage_rows: tuple[StaticCallSourceCoverage, ...],
    occurrences: tuple[StaticCallOccurrence, ...],
) -> str:
    identity_payload = json.dumps(
        {
            "configuration": context.config.identity(),
            "coverage": [item.model_dump(mode="json") for item in coverage_rows],
            "occurrences": [item.model_dump(mode="json") for item in occurrences],
            "prior": report.run_identity_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity_payload).hexdigest()


def _static_call_report_limitations(
    report: AnalysisReport,
    *,
    global_cap_hit: bool,
    section_state: SectionState,
) -> tuple[str, ...]:
    limitation_values = {
        *report.limitations,
        _RUNTIME_BINDING_LIMITATION,
        "static-call-evidence-is-syntactic-and-does-not-establish-runtime-dispatch",
    }
    if global_cap_hit or section_state is SectionState.TRUNCATED:
        limitation_values.add(_GLOBAL_RETENTION_LIMITATION)
    return tuple(sorted(limitation_values))


def _static_call_provider_result(
    *,
    section_state: SectionState,
    languages: tuple[SourceLanguage, ...],
    diagnostics: tuple[Diagnostic, ...],
) -> ProviderResult:
    return ProviderResult(
        provider_id=_PROVIDER_ID,
        state=_provider_state(section_state),
        version=__version__,
        duration_ms=0,
        observations=tuple(language.value for language in languages),
        diagnostics=diagnostics,
        completeness=(
            Completeness.PARTIAL
            if section_state is not SectionState.COMPLETE
            else Completeness.COMPLETE
        ),
    )


def _attach_static_call_payload(
    context: AnalysisContext,
    report: AnalysisReport,
    *,
    collected: list[_SourceCollection],
    global_diagnostics: tuple[Diagnostic, ...],
    languages: tuple[SourceLanguage, ...],
    parsed: int,
) -> AnalysisReport:
    occurrences, coverage_rows, diagnostics = _aggregate_collected_batches(
        collected, global_diagnostics
    )
    global_cap_hit = _global_cap_retention_hit(coverage_rows)
    section_state, truncated_sources, failed_sources = _section_state(
        collected,
        global_cap_hit=global_cap_hit,
    )
    if section_state is SectionState.TRUNCATED and truncated_sources == 0:
        truncated_sources = sum(
            1
            for row in coverage_rows
            if row.state is StaticCallSourceCoverageState.TRUNCATED and row.omitted_count > 0
        )
    section = _replace_static_section(
        report.section_coverage,
        state=section_state,
        languages=languages,
        parsed=parsed,
        failed=failed_sources,
        truncated=truncated_sources,
        diagnostic_codes=tuple(sorted({item.code for item in diagnostics})),
    )
    completeness = _report_completeness(report.completeness, section_state)
    summary = SummaryCounts.model_validate(
        {
            **report.summary.model_dump(),
            "static_call_occurrences": len(occurrences),
            "static_calls_observed": sum(item.observed_count for item in coverage_rows),
            "static_calls_omitted": sum(item.omitted_count for item in coverage_rows),
        }
    )
    limitations = _static_call_report_limitations(
        report,
        global_cap_hit=global_cap_hit,
        section_state=section_state,
    )
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "run_identity_sha256": _static_call_run_identity_sha256(
                context, report, coverage_rows, occurrences
            ),
            "static_call_occurrences": occurrences,
            "static_call_coverage": coverage_rows,
            "section_coverage": section,
            "diagnostics": (*report.diagnostics, *diagnostics),
            "providers": (
                *report.providers,
                _static_call_provider_result(
                    section_state=section_state,
                    languages=languages,
                    diagnostics=diagnostics,
                ),
            ),
            "completeness": completeness,
            "limitations": limitations,
            "summary": summary,
        }
    )


def _collect_source(
    context: AnalysisContext,
    source: SourceFileRecord,
    *,
    subject_index: _SubjectSpanIndex,
    analysis_identity: str,
    max_occurrences: int,
    retain_occurrences: bool,
    provider_records: StaticCallProviderRecords,
    ruby_runtime: _RubyCollectionRuntime | None,
) -> _SourceCollection:
    preflight = _preflight_source_collection(source)
    if preflight is not None:
        return preflight
    read = _read_source_bytes(context.source.physical_root, source.logical_path)
    read_failure = _source_collection_for_read_failure(source, read)
    if read_failure is not None:
        return read_failure
    hash_failure = _source_collection_for_hash_mismatch(source, read.payload, phase="before")
    if hash_failure is not None:
        return hash_failure
    try:
        batch_result = _language_batch(
            context,
            source,
            payload=read.payload,
            max_occurrences=max_occurrences,
            provider_records=provider_records,
            ruby_runtime=ruby_runtime,
        )
    except StaticCallNormalizationError as exc:
        return _source_collection_for_normalization_error(source, exc)
    diagnostics = list(batch_result.diagnostics)
    observed = batch_result.observed
    retained_facts = batch_result.normalized if retain_occurrences else ()
    retained_count, omitted = _retention_counts(
        observed=observed,
        retained_facts=retained_facts,
        retain_occurrences=retain_occurrences,
    )
    integrity_failure = _source_collection_for_post_batch_integrity(
        context,
        source,
        read.payload,
        observed=observed,
    )
    if integrity_failure is not None:
        return integrity_failure
    return _source_collection_from_batch(
        source,
        batch_result=batch_result,
        retained_facts=retained_facts,
        observed=observed,
        retained_count=retained_count,
        omitted=omitted,
        diagnostics=diagnostics,
        subject_index=subject_index,
        analysis_identity=analysis_identity,
        retain_occurrences=retain_occurrences,
    )


def _preflight_source_collection(source: SourceFileRecord) -> _SourceCollection | None:
    record_error = _source_record_validation_error(source)
    if record_error is not None:
        return _SourceCollection(
            _coverage_row(
                source,
                state=StaticCallSourceCoverageState.FAILED,
                observed=0,
                retained=0,
                limitations=("static-call-source-record-invalid",),
            ),
            (),
            (record_error,),
        )
    blocked = _blocked_parse_state(source.parse_state)
    if blocked is None:
        return None
    return _SourceCollection(
        _coverage_row(
            source,
            state=blocked,
            observed=0,
            retained=0,
            limitations=_parse_state_limitations(source.parse_state),
        ),
        (),
        (),
    )


def _source_collection_for_read_failure(
    source: SourceFileRecord, read: _ReadResult
) -> _SourceCollection | None:
    if read.error is None:
        return None
    diagnostic = Diagnostic(
        code=read.error,
        level=DiagnosticLevel.ERROR,
        message=read.message,
        path=source.logical_path,
        provider=_PROVIDER_ID,
        language=source.language,
    )
    return _SourceCollection(
        _coverage_row(
            source,
            state=StaticCallSourceCoverageState.FAILED,
            observed=0,
            retained=0,
            limitations=(read.message,),
        ),
        (),
        (diagnostic,),
    )


def _source_collection_for_hash_mismatch(
    source: SourceFileRecord,
    payload: bytes,
    *,
    phase: str,
) -> _SourceCollection | None:
    if hashlib.sha256(payload).hexdigest() == source.source_sha256:
        return None
    if phase == "before":
        code = "static-call-source-hash-mismatch"
        message = "Source bytes disagree with the recorded source_sha256 before analysis"
        limitation = "source-hash-mismatch"
    else:
        code = "static-call-source-hash-changed"
        message = "Source bytes changed during static call collection"
        limitation = "source-hash-changed-midscan"
    diagnostic = Diagnostic(
        code=code,
        level=DiagnosticLevel.ERROR,
        message=message,
        path=source.logical_path,
        provider=_PROVIDER_ID,
        language=source.language,
    )
    return _SourceCollection(
        _coverage_row(
            source,
            state=StaticCallSourceCoverageState.FAILED,
            observed=0,
            retained=0,
            limitations=(limitation,),
        ),
        (),
        (diagnostic,),
    )


def _source_collection_for_normalization_error(
    source: SourceFileRecord, exc: StaticCallNormalizationError
) -> _SourceCollection:
    diagnostic = Diagnostic(
        code="static-call-normalization-failed",
        level=DiagnosticLevel.ERROR,
        message=str(exc),
        path=source.logical_path,
        provider=_PROVIDER_ID,
        language=source.language,
    )
    return _SourceCollection(
        _coverage_row(
            source,
            state=StaticCallSourceCoverageState.FAILED,
            observed=0,
            retained=0,
            limitations=("normalization-failed",),
        ),
        (),
        (diagnostic,),
    )


def _retention_counts(
    *,
    observed: int,
    retained_facts: tuple[NormalizedStaticCallFact, ...],
    retain_occurrences: bool,
) -> tuple[int, int]:
    if not retain_occurrences and observed > 0:
        return 0, observed
    retained_count = len(retained_facts)
    return retained_count, observed - retained_count


def _source_collection_for_post_batch_integrity(
    context: AnalysisContext,
    source: SourceFileRecord,
    before_payload: bytes,
    *,
    observed: int,
) -> _SourceCollection | None:
    after_read = _read_source_bytes(context.source.physical_root, source.logical_path)
    if after_read.error is not None:
        diagnostic = Diagnostic(
            code=after_read.error,
            level=DiagnosticLevel.ERROR,
            message=after_read.message,
            path=source.logical_path,
            provider=_PROVIDER_ID,
            language=source.language,
        )
        return _SourceCollection(
            _coverage_row(
                source,
                state=StaticCallSourceCoverageState.FAILED,
                observed=observed,
                retained=0,
                limitations=("source-unavailable-after-scan",),
            ),
            (),
            (diagnostic,),
        )
    after_digest = hashlib.sha256(after_read.payload).hexdigest()
    if after_digest == source.source_sha256 and after_read.payload == before_payload:
        return None
    diagnostic = Diagnostic(
        code="static-call-source-hash-changed",
        level=DiagnosticLevel.ERROR,
        message="Source bytes changed during static call collection",
        path=source.logical_path,
        provider=_PROVIDER_ID,
        language=source.language,
    )
    return _SourceCollection(
        _coverage_row(
            source,
            state=StaticCallSourceCoverageState.FAILED,
            observed=observed,
            retained=0,
            limitations=("source-hash-changed-midscan",),
        ),
        (),
        (diagnostic,),
    )


def _source_collection_from_batch(
    source: SourceFileRecord,
    *,
    batch_result: _LanguageBatchResult,
    retained_facts: tuple[NormalizedStaticCallFact, ...],
    observed: int,
    retained_count: int,
    omitted: int,
    diagnostics: list[Diagnostic],
    subject_index: _SubjectSpanIndex,
    analysis_identity: str,
    retain_occurrences: bool,
) -> _SourceCollection:
    assembly = _assemble_validated_occurrences(
        source,
        retained_facts,
        analysis_identity=analysis_identity,
        subject_index=subject_index,
        batch_limitations=batch_result.limitations,
    )
    diagnostics.extend(assembly.diagnostics)
    occurrences = assembly.occurrences
    retained_count = len(occurrences)
    omitted = observed - retained_count
    contribution_failed = (
        retain_occurrences and observed > 0 and retained_count == 0 and assembly.invalid_count > 0
    )
    coverage_state = _source_coverage_state(
        source,
        observed=observed,
        retained=retained_count,
        omitted=omitted,
        batch_limitations=batch_result.limitations,
        provider_failed=batch_result.provider_failed,
        provider_unavailable=batch_result.provider_unavailable,
        diagnostics=tuple(diagnostics),
        contribution_failed=contribution_failed,
    )
    coverage_limitations = _coverage_limitations(
        coverage_state,
        batch_result.limitations,
        source.parse_state,
        contribution_invalid=assembly.invalid_count > 0,
    )
    return _SourceCollection(
        _coverage_row(
            source,
            state=coverage_state,
            observed=observed,
            retained=retained_count,
            limitations=coverage_limitations,
        ),
        occurrences,
        tuple(diagnostics),
    )


def _source_record_validation_error(source: SourceFileRecord) -> Diagnostic | None:
    try:
        SourceFileRecord.model_validate(source.model_dump())
    except ValidationError:
        return Diagnostic(
            code="static-call-source-record-invalid",
            level=DiagnosticLevel.ERROR,
            message="Source file record failed static call preflight validation",
            path=source.logical_path,
            provider=_PROVIDER_ID,
            language=source.language,
        )
    return None


@dataclass(frozen=True, slots=True)
class _ReadResult:
    payload: bytes
    error: str | None
    message: str


@dataclass(frozen=True, slots=True)
class _LanguageBatchResult:
    normalized: tuple[NormalizedStaticCallFact, ...]
    observed: int
    limitations: tuple[str, ...]
    diagnostics: tuple[Diagnostic, ...]
    provider_failed: bool
    provider_unavailable: bool = False


@dataclass(frozen=True, slots=True)
class _OccurrenceAssembly:
    occurrences: tuple[StaticCallOccurrence, ...]
    diagnostics: tuple[Diagnostic, ...]
    invalid_count: int


def _span_within_source(source: SourceFileRecord, span: SourceSpan) -> bool:
    return not (
        span.path != source.logical_path
        or span.start_line < 1
        or span.end_line > source.physical_loc
    )


def _contribution_invalid_diagnostic(source: SourceFileRecord, message: str) -> Diagnostic:
    return Diagnostic(
        code="static-call-record-invalid",
        level=DiagnosticLevel.ERROR,
        message=message,
        path=source.logical_path,
        provider=_PROVIDER_ID,
        language=source.language,
    )


def _occurrence_contribution_issue(
    occurrence: StaticCallOccurrence,
    source: SourceFileRecord,
) -> Diagnostic | None:
    if occurrence.occurrence_id != static_call_identity(occurrence):
        return _contribution_invalid_diagnostic(
            source,
            f"static call occurrence identity mismatch: {occurrence.occurrence_id}",
        )
    evidence = occurrence.evidence[0]
    if (
        occurrence.language is not source.language
        or occurrence.module_name != source.module_name
        or evidence.span.path != source.logical_path
        or evidence.source_sha256 != source.source_sha256
    ):
        return _contribution_invalid_diagnostic(
            source,
            f"static call occurrence {occurrence.occurrence_id} disagrees with its source file",
        )
    if not _span_within_source(source, evidence.span):
        return _contribution_invalid_diagnostic(
            source,
            f"static call occurrence {occurrence.occurrence_id} span is outside source bounds",
        )
    for scope in occurrence.scope_path:
        if scope.definition_line < 1 or scope.definition_line > source.physical_loc:
            return _contribution_invalid_diagnostic(
                source,
                f"static call occurrence {occurrence.occurrence_id} scope is outside source",
            )
    return None


def _assemble_validated_occurrences(
    source: SourceFileRecord,
    facts: tuple[NormalizedStaticCallFact, ...],
    *,
    analysis_identity: str,
    subject_index: _SubjectSpanIndex,
    batch_limitations: tuple[str, ...],
) -> _OccurrenceAssembly:
    diagnostics: list[Diagnostic] = []
    occurrences: list[StaticCallOccurrence] = []
    invalid_count = 0
    for fact in facts:
        try:
            record = _occurrence_record(
                source=source,
                fact=fact,
                analysis_identity=analysis_identity,
                subject_index=subject_index,
                batch_limitations=batch_limitations,
            )
        except ValidationError as exc:
            invalid_count += 1
            diagnostics.append(_contribution_invalid_diagnostic(source, exc.errors()[0]["msg"]))
            continue
        issue = _occurrence_contribution_issue(record, source)
        if issue is not None:
            invalid_count += 1
            diagnostics.append(issue)
            continue
        occurrences.append(record)
    return _OccurrenceAssembly(tuple(occurrences), tuple(diagnostics), invalid_count)


def _read_source_bytes(root: Path, logical_path: str) -> _ReadResult:
    path = PurePosixPath(logical_path)
    if (
        not logical_path
        or path.is_absolute()
        or "\\" in logical_path
        or any(part in {"", ".", ".."} for part in logical_path.split("/"))
    ):
        return _ReadResult(b"", "static-call-invalid-path", "logical path is not confined")
    try:
        payload = _read_confined(root, path.parts)
    except OSError:
        return _ReadResult(
            b"",
            "static-call-source-unavailable",
            "source is unavailable or violates path confinement",
        )
    if len(payload) > _MAX_SOURCE_BYTES:
        return _ReadResult(b"", "static-call-source-too-large", "source exceeds bounded read size")
    return _ReadResult(payload, None, "")


def _read_confined(root: Path, parts: tuple[str, ...]) -> bytes:
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("source is not a regular file")
            return stream.read(_MAX_SOURCE_BYTES + 1)
    finally:
        os.close(directory)


def _language_batch(
    context: AnalysisContext,
    source: SourceFileRecord,
    *,
    payload: bytes,
    max_occurrences: int,
    provider_records: StaticCallProviderRecords,
    ruby_runtime: _RubyCollectionRuntime | None,
) -> _LanguageBatchResult:
    if source.language is SourceLanguage.PYTHON:
        text, decode_failure = _utf8_text_from_payload(source, payload)
        if decode_failure is not None:
            return decode_failure
        return _python_batch(source, text, max_occurrences=max_occurrences)
    if source.language is SourceLanguage.JAVASCRIPT:
        return _javascript_batch(
            source,
            payload,
            max_occurrences=max_occurrences,
            provider_records=provider_records,
        )
    if source.language is SourceLanguage.RUBY:
        return _ruby_batch(
            context,
            source,
            payload,
            max_occurrences=max_occurrences,
            provider_records=provider_records,
            ruby_runtime=ruby_runtime,
        )
    raise StaticCallNormalizationError(f"unsupported source language: {source.language.value}")


def _utf8_text_from_payload(
    source: SourceFileRecord, payload: bytes
) -> tuple[str, _LanguageBatchResult | None]:
    try:
        return payload.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return "", _LanguageBatchResult(
            (),
            0,
            ("utf-8-decode-failed",),
            (
                Diagnostic(
                    code="static-call-source-decode-failed",
                    level=DiagnosticLevel.ERROR,
                    message=str(exc),
                    path=source.logical_path,
                    provider=_PROVIDER_ID,
                    language=source.language,
                ),
            ),
            True,
        )


def _python_batch(
    source: SourceFileRecord,
    text: str,
    *,
    max_occurrences: int,
) -> _LanguageBatchResult:
    try:
        batch = static_call_batch_from_source(text, max_occurrences=max_occurrences)
    except SyntaxError as exc:
        return _LanguageBatchResult(
            (),
            0,
            ("python-syntax-error",),
            (
                Diagnostic(
                    code="static-call-python-syntax-error",
                    level=DiagnosticLevel.ERROR,
                    message=str(exc.msg or "syntax error"),
                    path=source.logical_path,
                    provider=_PROVIDER_ID,
                    language=source.language,
                ),
            ),
            True,
        )
    normalized = tuple(normalize_python_call_fact(item) for item in batch.occurrences)
    limitations = tuple(
        item for item in batch.limitations if item != "truncated-at-max-occurrences"
    )
    return _LanguageBatchResult(normalized, batch.total_observed, limitations, (), False)


def _javascript_batch(
    source: SourceFileRecord,
    payload: bytes,
    *,
    max_occurrences: int,
    provider_records: StaticCallProviderRecords,
) -> _LanguageBatchResult:
    javascript = provider_records.javascript
    if javascript is None or not javascript.available:
        return _LanguageBatchResult(
            (),
            0,
            ("javascript-parser-unavailable",),
            (
                Diagnostic(
                    code="static-call-javascript-unavailable",
                    level=DiagnosticLevel.ERROR,
                    message="optional JavaScript parser extra is not installed",
                    path=source.logical_path,
                    provider=_PROVIDER_ID,
                    language=source.language,
                ),
            ),
            False,
            True,
        )
    try:
        batch = javascript_static_call_batch_from_source(payload, max_occurrences=max_occurrences)
    except ValueError as exc:
        return _LanguageBatchResult(
            (),
            0,
            ("javascript-parse-failed",),
            (
                Diagnostic(
                    code="static-call-javascript-parse-failed",
                    level=DiagnosticLevel.ERROR,
                    message=str(exc),
                    path=source.logical_path,
                    provider=_PROVIDER_ID,
                    language=source.language,
                ),
            ),
            True,
        )
    normalized = tuple(normalize_javascript_call_fact(item) for item in batch.occurrences)
    limitations = tuple(
        item
        for item in batch.limitations
        if item
        not in {"truncated-at-max-occurrences", "parse-tree-contains-errors-or-missing-nodes"}
    )
    diagnostics: tuple[Diagnostic, ...] = ()
    if not batch.tree_complete:
        diagnostics = (
            Diagnostic(
                code="static-call-javascript-partial-tree",
                level=DiagnosticLevel.ERROR,
                message="JavaScript parse tree is incomplete",
                path=source.logical_path,
                provider=_PROVIDER_ID,
                language=source.language,
            ),
        )
    return _LanguageBatchResult(normalized, batch.total_observed, limitations, diagnostics, False)


def _ruby_unavailable_batch(source: SourceFileRecord) -> _LanguageBatchResult:
    return _LanguageBatchResult(
        (),
        0,
        ("ruby-prism-unavailable",),
        (
            Diagnostic(
                code="static-call-ruby-unavailable",
                level=DiagnosticLevel.ERROR,
                message="Ruby/Prism is unavailable",
                path=source.logical_path,
                provider=_PROVIDER_ID,
                language=source.language,
            ),
        ),
        False,
        True,
    )


def _ruby_batch(
    context: AnalysisContext,
    source: SourceFileRecord,
    payload: bytes,
    *,
    max_occurrences: int,
    provider_records: StaticCallProviderRecords,
    ruby_runtime: _RubyCollectionRuntime | None,
) -> _LanguageBatchResult:
    ruby = provider_records.ruby
    if ruby is None or ruby.availability_state != AvailabilityState.AVAILABLE.value:
        return _ruby_unavailable_batch(source)
    if ruby_runtime is None:
        return _ruby_unavailable_batch(source)
    return _ruby_driver_batch(
        context,
        source,
        payload,
        max_occurrences=max_occurrences,
        ruby_runtime=ruby_runtime,
    )


def _ruby_driver_batch(
    context: AnalysisContext,
    source: SourceFileRecord,
    payload: bytes,
    *,
    max_occurrences: int,
    ruby_runtime: _RubyCollectionRuntime,
) -> _LanguageBatchResult:
    driver = static_call_batch_from_driver(
        ruby_runtime.executable,
        payload,
        max_occurrences=max_occurrences,
        timeout_seconds=min(context.config.runtime.timeout_seconds, 30.0),
    )
    if driver.failure_code is not None:
        unavailable = driver.failure_code.endswith("-unavailable")
        return _LanguageBatchResult(
            (),
            0,
            (driver.failure_code,),
            (
                Diagnostic(
                    code=driver.failure_code,
                    level=DiagnosticLevel.ERROR,
                    message=driver.failure_detail or driver.failure_code,
                    path=source.logical_path,
                    provider=_PROVIDER_ID,
                    language=source.language,
                ),
            ),
            not unavailable,
            unavailable,
        )
    assert driver.batch is not None
    if driver.prism_version != ruby_runtime.expected_prism_version:
        return _LanguageBatchResult(
            (),
            0,
            ("ruby-prism-version-mismatch",),
            (
                Diagnostic(
                    code="static-call-ruby-prism-version-mismatch",
                    level=DiagnosticLevel.ERROR,
                    message="Driver-reported Prism version disagrees with probed provider identity",
                    path=source.logical_path,
                    provider=_PROVIDER_ID,
                    language=source.language,
                ),
            ),
            True,
        )
    normalized = tuple(normalize_ruby_call_fact(item) for item in driver.batch.occurrences)
    limitations = tuple(
        item
        for item in driver.batch.limitations
        if item not in {"truncated-at-max-occurrences", "parse-contains-errors"}
    )
    diagnostics: tuple[Diagnostic, ...] = ()
    if not driver.batch.parse_complete:
        diagnostics = (
            Diagnostic(
                code="static-call-ruby-partial-parse",
                level=DiagnosticLevel.ERROR,
                message="Ruby parse is incomplete",
                path=source.logical_path,
                provider=_PROVIDER_ID,
                language=source.language,
            ),
        )
    return _LanguageBatchResult(
        normalized, driver.batch.total_observed, limitations, diagnostics, False
    )


def _occurrence_record(
    *,
    source: SourceFileRecord,
    fact: NormalizedStaticCallFact,
    analysis_identity: str,
    subject_index: _SubjectSpanIndex,
    batch_limitations: tuple[str, ...],
) -> StaticCallOccurrence:
    span = SourceSpan(
        path=source.logical_path,
        start_line=fact.start_line,
        start_column=fact.start_column,
        end_line=fact.end_line,
        end_column=fact.end_column,
        language=source.language,
    )
    evidence = Evidence(
        span=span,
        provider=_EVIDENCE_PROVIDER,
        observation="syntactic invocation site",
        confidence=Confidence.EXACT,
        source_sha256=source.source_sha256,
        language=source.language,
    )
    limitations = tuple(sorted({*batch_limitations, *fact.limitations}))
    record = StaticCallOccurrence(
        occurrence_id="call-occurrence-00000000000000000000",
        analysis_identity_sha256=analysis_identity,
        language=source.language,
        source_file_id=source.source_file_id,
        module_name=source.module_name,
        enclosing_subject_id=_nearest_enclosing_subject_id(
            call_span=span,
            subject_index=subject_index,
        ),
        callee_spelling=fact.callee_spelling,
        syntax_kind=fact.syntax_kind,
        evaluation_context=fact.evaluation_context,
        conditional=fact.conditional,
        deferred=fact.deferred,
        evaluation_unknown=fact.evaluation_unknown,
        scope_path=fact.scope_path,
        evidence=(evidence,),
        limitations=limitations,
    )
    return record.model_copy(update={"occurrence_id": static_call_identity(record)})


def _nearest_enclosing_subject_id(
    *,
    call_span: SourceSpan,
    subject_index: _SubjectSpanIndex,
) -> str | None:
    candidates: list[tuple[tuple[int, int, int, int], str]] = []
    for outer, subject_id in subject_index.entries:
        if not _span_contains(outer, call_span):
            continue
        key = (
            outer.start_line,
            outer.start_column,
            -(outer.end_line - outer.start_line),
            -(outer.end_column - outer.start_column),
        )
        candidates.append((key, subject_id))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    best = candidates[0][0]
    matches = sorted({subject_id for key, subject_id in candidates if key == best})
    if len(matches) != 1:
        return None
    return matches[0]


def _span_contains(outer: SourceSpan, inner: SourceSpan) -> bool:
    outer_start = (outer.start_line, outer.start_column)
    inner_start = (inner.start_line, inner.start_column)
    outer_end = (outer.end_line, outer.end_column)
    inner_end = (inner.end_line, inner.end_column)
    return outer_start <= inner_start and inner_end <= outer_end


def _coverage_row(
    source: SourceFileRecord,
    *,
    state: StaticCallSourceCoverageState,
    observed: int,
    retained: int,
    limitations: tuple[str, ...],
) -> StaticCallSourceCoverage:
    omitted = observed - retained
    return StaticCallSourceCoverage(
        source_file_id=source.source_file_id,
        language=source.language,
        state=state,
        observed_count=observed,
        retained_count=retained,
        omitted_count=omitted,
        limitations=limitations,
    )


def _blocked_parse_state(
    parse_state: SourceParseState,
) -> StaticCallSourceCoverageState | None:
    mapping = {
        SourceParseState.FAILED: StaticCallSourceCoverageState.FAILED,
        SourceParseState.UNAVAILABLE: StaticCallSourceCoverageState.UNAVAILABLE,
        SourceParseState.UNSUPPORTED: StaticCallSourceCoverageState.UNSUPPORTED,
    }
    return mapping.get(parse_state)


def _parse_state_limitations(parse_state: SourceParseState) -> tuple[str, ...]:
    return (f"source-parse-state-{parse_state.value}",)


def _source_coverage_state(
    source: SourceFileRecord,
    *,
    observed: int,
    retained: int,
    omitted: int,
    batch_limitations: tuple[str, ...],
    provider_failed: bool,
    provider_unavailable: bool,
    diagnostics: tuple[Diagnostic, ...],
    contribution_failed: bool = False,
) -> StaticCallSourceCoverageState:
    if provider_unavailable:
        return StaticCallSourceCoverageState.UNAVAILABLE
    if provider_failed or contribution_failed:
        return StaticCallSourceCoverageState.FAILED
    if diagnostics or _has_incomplete_batch_limitations(batch_limitations):
        return StaticCallSourceCoverageState.PARTIAL
    if source.parse_state is SourceParseState.PARTIAL:
        return StaticCallSourceCoverageState.PARTIAL
    if omitted > 0:
        return StaticCallSourceCoverageState.TRUNCATED
    return StaticCallSourceCoverageState.COMPLETE


def _has_incomplete_batch_limitations(batch_limitations: tuple[str, ...]) -> bool:
    return any(item in _INCOMPLETE_BATCH_LIMITATIONS for item in batch_limitations)


def _coverage_limitations(
    state: StaticCallSourceCoverageState,
    batch_limitations: tuple[str, ...],
    parse_state: SourceParseState,
    *,
    contribution_invalid: bool = False,
) -> tuple[str, ...]:
    if contribution_invalid and state is StaticCallSourceCoverageState.FAILED:
        return ("static-call-record-invalid",)
    if state is StaticCallSourceCoverageState.PARTIAL:
        values = {item for item in batch_limitations if item in _INCOMPLETE_BATCH_LIMITATIONS}
        if parse_state is SourceParseState.PARTIAL:
            values.add(f"source-parse-state-{parse_state.value}")
        if not values:
            values.add("incomplete-static-call-evidence")
        return tuple(sorted(values))
    if state in {
        StaticCallSourceCoverageState.FAILED,
        StaticCallSourceCoverageState.UNAVAILABLE,
        StaticCallSourceCoverageState.UNSUPPORTED,
    }:
        return batch_limitations or ("static-call-collection-failed",)
    return batch_limitations if state is StaticCallSourceCoverageState.TRUNCATED else ()


def _section_state(
    collected: list[_SourceCollection],
    *,
    global_cap_hit: bool,
) -> tuple[SectionState, int, int]:
    truncated = sum(
        1 for item in collected if item.coverage.state is StaticCallSourceCoverageState.TRUNCATED
    )
    failed = sum(
        1
        for item in collected
        if item.coverage.state
        in {
            StaticCallSourceCoverageState.FAILED,
            StaticCallSourceCoverageState.UNAVAILABLE,
            StaticCallSourceCoverageState.UNSUPPORTED,
        }
    )
    partial = any(
        item.coverage.state is StaticCallSourceCoverageState.PARTIAL for item in collected
    )
    if failed:
        return SectionState.PARTIAL, truncated, failed
    if global_cap_hit or truncated:
        return SectionState.TRUNCATED, truncated, failed
    if partial:
        return SectionState.PARTIAL, truncated, failed
    return SectionState.COMPLETE, truncated, failed


def _replace_static_section(
    sections: tuple[SectionCoverage, ...],
    *,
    state: SectionState,
    languages: tuple[SourceLanguage, ...],
    parsed: int,
    failed: int,
    truncated: int,
    diagnostic_codes: tuple[str, ...],
) -> tuple[SectionCoverage, ...]:
    return tuple(
        (
            SectionCoverage(
                section="static-call-occurrences",
                state=state,
                parsed=parsed,
                failed=failed,
                truncated=truncated,
                diagnostic_codes=diagnostic_codes,
                languages=languages,
            )
            if item.section == "static-call-occurrences"
            else item
        )
        for item in sections
    )


def _provider_state(section_state: SectionState) -> ProviderState:
    if section_state is SectionState.COMPLETE:
        return ProviderState.SUCCESS
    if section_state is SectionState.TRUNCATED:
        return ProviderState.PARTIAL
    return ProviderState.PARTIAL


def _report_completeness(current: Completeness, section_state: SectionState) -> Completeness:
    if current is Completeness.FAILED:
        return Completeness.FAILED
    if section_state is SectionState.COMPLETE:
        return current
    return Completeness.PARTIAL


__all__ = ["integrate_static_call_report"]
