"""Integrate parser-private JavaScript facts into the authoritative report."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections import defaultdict
from dataclasses import asdict, replace
from difflib import SequenceMatcher
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from code_analysis.cache import ContentCache
from code_analysis.config import JavaScriptModuleMode, LanguageSelection
from code_analysis.context import AnalysisContext
from code_analysis.errors import ConfigurationError
from code_analysis.languages.base import FrontendContext, FrontendResult, SourceUnit
from code_analysis.languages.javascript.cache import (
    load_cached_javascript_result,
    store_cached_javascript_result,
)
from code_analysis.languages.registry import frontend_for
from code_analysis.models import (
    Completeness,
    ComplexityObservation,
    Confidence,
    Diagnostic,
    DiagnosticLevel,
    Evidence,
    ImportEdge,
    JavaScriptSyntaxMode,
    Module,
    ModuleSystem,
    ProviderResult,
    ProviderState,
    SourceLanguage,
    SourceParseState,
    SourceSpan,
    Symbol,
)
from code_analysis.project import (
    JAVASCRIPT_SUFFIXES,
    iter_javascript_files,
    javascript_module_name,
    logical_path,
    selected_languages,
    source_root_for,
)
from code_analysis.report_models import (
    AdapterCapabilityState,
    AnalysisReport,
    AnalysisSubject,
    BehaviorDifference,
    BehaviorDifferenceKind,
    CanonicalOwnerState,
    CanonicalOwnerSuggestion,
    CloneCluster,
    CloneFragment,
    CloneKind,
    CommonalityDimension,
    CommonalityDimensionKind,
    DivergenceReason,
    DuplicateFunctionPair,
    FunctionCommonality,
    FunctionFeatureProfile,
    LanguageFrontendCoverage,
    ModuleExportRecord,
    SectionState,
    SideEffectKind,
    SideEffectObservation,
    SourceFileRecord,
    StateResource,
    StateResourceKind,
    StructureScope,
    SubjectKind,
    SummaryCounts,
    TestModuleRecord,
    TestRelationshipKind,
    TestRelationshipRecord,
    UnresolvedEdge,
)


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def _source_file_id(path: str, language: SourceLanguage) -> str:
    return _stable_id("source-file", language.value, ".", path)


def attach_python_source_records(report: AnalysisReport) -> AnalysisReport:
    """Attach authoritative Python source records without enabling JavaScript."""

    sources = tuple(
        sorted(
            (
                SourceFileRecord(
                    source_file_id=_source_file_id(module.path, SourceLanguage.PYTHON),
                    logical_path=module.path,
                    logical_root=".",
                    source_sha256=module.source_sha256,
                    language=SourceLanguage.PYTHON,
                    parse_state=module.parse_state,
                    module_name=module.name,
                    physical_loc=module.line_count,
                    nonblank_loc=module.nonblank_line_count,
                )
                for module in report.modules
                if module.language is SourceLanguage.PYTHON
            ),
            key=lambda item: item.source_file_id,
        )
    )
    complete = sum(item.parse_state is SourceParseState.COMPLETE for item in sources)
    partial = sum(item.parse_state is SourceParseState.PARTIAL for item in sources)
    failed = sum(item.parse_state is SourceParseState.FAILED for item in sources)
    coverage = LanguageFrontendCoverage(
        language=SourceLanguage.PYTHON,
        frontend_id="python-ast",
        state=(SectionState.PARTIAL if partial or failed else SectionState.COMPLETE),
        discovered=len(sources),
        complete=complete,
        partial=partial,
        failed=failed,
        unavailable=0,
        diagnostic_codes=tuple(
            sorted(
                {
                    item.code
                    for item in report.diagnostics
                    if item.language in {None, SourceLanguage.PYTHON}
                }
            )
        ),
        capabilities=frontend_for(SourceLanguage.PYTHON).capabilities,
    )
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "source_files": sources,
            "language_frontends": (coverage,),
            "target": report.target.model_copy(
                update={"languages": (SourceLanguage.PYTHON,) if sources else ()}
            ),
            "summary": {**report.summary.model_dump(), "source_files": len(sources)},
        }
    )


def integrate_language_sources(
    context: AnalysisContext,
    report: AnalysisReport,
    cache: ContentCache | None = None,
) -> AnalysisReport:
    """Attach source identities and JavaScript evidence to a Python-owned base report."""

    if LanguageSelection.PYTHON in selected_languages(context.config):
        report = attach_python_source_records(report)
        python_sources = report.source_files
    else:
        python_sources = ()
    if LanguageSelection.JAVASCRIPT not in selected_languages(context.config):
        updated = _replace_language_records(report, python_sources, (), None)
        return (
            updated
            if python_sources or LanguageSelection.RUBY in selected_languages(context.config)
            else _empty_source_report(updated)
        )

    paths = iter_javascript_files(context)
    if not paths:
        updated = _replace_language_records(
            report,
            python_sources,
            (),
            LanguageFrontendCoverage(
                language=SourceLanguage.JAVASCRIPT,
                frontend_id="javascript-tree-sitter",
                state=SectionState.COMPLETE,
                discovered=0,
                complete=0,
                partial=0,
                failed=0,
                unavailable=0,
                capabilities=frontend_for(SourceLanguage.JAVASCRIPT).capabilities,
            ),
        )
        return (
            updated
            if python_sources or LanguageSelection.RUBY in selected_languages(context.config)
            else _empty_source_report(updated)
        )

    frontend = frontend_for(SourceLanguage.JAVASCRIPT)
    frontend_context = FrontendContext(context.config, context.config.identity())
    availability = frontend.availability(frontend_context)
    if availability.state.value == "unavailable":
        return _unavailable_report(context, report, python_sources, paths, availability.detail)

    results: list[FrontendResult] = []
    diagnostics: list[Diagnostic] = []
    failed_sources: list[SourceFileRecord] = []
    failed_modules: list[Module] = []
    for path in paths:
        relative = logical_path(context, path)
        module_name = javascript_module_name(context, path)
        data = b""
        try:
            data = path.read_bytes()
            source = data.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            digest = hashlib.sha256(data).hexdigest()
            failed_sources.append(
                _failed_source(relative, module_name, digest, SourceParseState.FAILED)
            )
            failed_modules.append(
                _failed_module(relative, module_name, digest, SourceParseState.FAILED)
            )
            diagnostics.append(
                Diagnostic(
                    code="javascript-source-read-error",
                    level=DiagnosticLevel.ERROR,
                    message=f"JavaScript source could not be read as UTF-8: {type(exc).__name__}",
                    path=relative,
                    provider=frontend.frontend_id,
                    language=SourceLanguage.JAVASCRIPT,
                )
            )
            continue
        digest = hashlib.sha256(data).hexdigest()
        unit = SourceUnit(
            logical_path=relative,
            module_name=module_name,
            source=source,
            source_bytes=data,
            source_sha256=digest,
            language=SourceLanguage.JAVASCRIPT,
        )
        mode = _configured_module_mode(context, path, relative)
        result = (
            load_cached_javascript_result(cache, unit, context.config, mode)
            if cache is not None
            else None
        )
        if result is None:
            result = frontend.analyze(
                unit,
                FrontendContext(context.config, context.config.identity(), mode),
            )
            if cache is not None:
                store_cached_javascript_result(cache, result, context.config, mode)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            diagnostics.append(
                Diagnostic(
                    code="javascript-source-changed",
                    level=DiagnosticLevel.ERROR,
                    message="JavaScript source changed during analysis",
                    path=relative,
                    provider=frontend.frontend_id,
                    language=SourceLanguage.JAVASCRIPT,
                )
            )
            result = replace(result, completeness=Completeness.PARTIAL)
        diagnostics.extend(result.diagnostics)
        results.append(result)

    return _merge_results(
        context,
        report,
        python_sources,
        tuple(results),
        tuple(failed_sources),
        tuple(failed_modules),
        tuple(diagnostics),
    )


def finalize_language_sections(report: AnalysisReport) -> AnalysisReport:
    """Reconcile language scope after cross-language cohesion and test integration."""

    subjects = {item.subject_id: item for item in report.subjects}
    cohesion_languages = {
        subjects[subject_id].language
        for cluster in report.responsibility_clusters
        for subject_id in cluster.member_subject_ids
    }
    test_languages = {item.language for item in report.test_modules}
    sections = []
    for section in report.section_coverage:
        if section.section == "cohesion-refactoring" and cohesion_languages:
            sections.append(
                section.model_copy(
                    update={
                        "languages": tuple(sorted(cohesion_languages, key=lambda item: item.value))
                    }
                )
            )
        elif section.section == "test-impact" and test_languages:
            javascript_tests = sum(
                item.language is SourceLanguage.JAVASCRIPT for item in report.test_modules
            )
            state = (
                section.state
                if section.state
                in {SectionState.FAILED, SectionState.PARTIAL, SectionState.TRUNCATED}
                else SectionState.COMPLETE
            )
            sections.append(
                section.model_copy(
                    update={
                        "state": state,
                        "required": True,
                        "parsed": max(section.parsed, javascript_tests),
                        "languages": tuple(
                            sorted(
                                test_languages
                                | (set(section.languages) if section.failed else set()),
                                key=lambda item: item.value,
                            )
                        ),
                    }
                )
            )
        else:
            sections.append(section)
    return AnalysisReport.model_validate(
        {**report.model_dump(), "section_coverage": tuple(sections)}
    )


def _configured_module_mode(
    context: AnalysisContext, path: Path, relative: str
) -> ModuleSystem | None:
    configured = context.config.javascript
    if configured.default_module_mode is not JavaScriptModuleMode.AUTO:
        return ModuleSystem(configured.default_module_mode.value)
    for rule in configured.module_mode_rules:
        if fnmatchcase(relative, rule.pattern):
            return rule.mode
    if path.suffix == ".mjs":
        return ModuleSystem.ESM
    if path.suffix == ".cjs":
        return ModuleSystem.COMMONJS
    try:
        logical_root = source_root_for(context, path)
    except ConfigurationError:
        logical_root = context.source.physical_root
    package_mode = _nearest_package_mode(path, logical_root)
    return package_mode


def _nearest_package_mode(path: Path, root: Path) -> ModuleSystem | None:
    directory = path.parent
    while directory == root or root in directory.parents:
        metadata = directory / "package.json"
        if metadata.is_file():
            try:
                value = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return ModuleSystem.UNRESOLVED
            package_type = value.get("type") if isinstance(value, dict) else None
            if package_type == "module":
                return ModuleSystem.ESM
            if package_type == "commonjs":
                return ModuleSystem.COMMONJS
            if package_type is not None:
                return ModuleSystem.UNRESOLVED
            return None
        if directory == root:
            break
        directory = directory.parent
    return None


def _merge_results(
    context: AnalysisContext,
    report: AnalysisReport,
    python_sources: tuple[SourceFileRecord, ...],
    results: tuple[FrontendResult, ...],
    failed_sources: tuple[SourceFileRecord, ...],
    failed_modules: tuple[Module, ...],
    diagnostics: tuple[Diagnostic, ...],
) -> AnalysisReport:
    js_sources = tuple(_source_record(item) for item in results)
    modules = tuple(_module_record(item) for item in results)
    all_js_modules = tuple(sorted((*modules, *failed_modules), key=lambda item: item.path))
    subjects = tuple(
        sorted(
            (subject for result in results for subject in _subjects(result)),
            key=lambda item: item.subject_id,
        )
    )
    state_resources = _state_resources(results, subjects)
    side_effects = _side_effects(results, subjects)
    exports = tuple(
        sorted(
            (record for result in results for record in _exports(result)),
            key=lambda item: item.export_id,
        )
    )
    edges = _import_edges(results)
    all_js_modules = _modules_with_fan_counts(all_js_modules, edges)
    unresolved_imports = _unresolved_import_edges(results, subjects)
    unresolved_diagnostics = tuple(
        Diagnostic(
            code="javascript-import-unresolved",
            level=DiagnosticLevel.WARNING,
            message=item.reason,
            path=item.evidence[0].span.path,
            provider="javascript-tree-sitter",
            language=SourceLanguage.JAVASCRIPT,
        )
        for item in unresolved_imports
    )
    fragments, clusters, cluster_pairs, clone_omissions = _clone_records(context, results, subjects)
    profiles = _function_profiles(results, subjects)
    differences, duplicates, pair_omissions = _duplicates(context, results, subjects, cluster_pairs)
    bound_omissions = clone_omissions + pair_omissions
    bound_diagnostics = (
        (
            Diagnostic(
                code="javascript-analysis-truncated",
                level=DiagnosticLevel.ERROR,
                message=(
                    "JavaScript clone or duplicate candidate bounds omitted "
                    f"at least {bound_omissions} records"
                ),
                provider="javascript-tree-sitter",
                language=SourceLanguage.JAVASCRIPT,
            ),
        )
        if bound_omissions
        else ()
    )
    all_diagnostics = (*diagnostics, *unresolved_diagnostics, *bound_diagnostics)
    commonalities = _javascript_commonalities(duplicates, profiles, clusters)
    js_test_modules, js_test_relationships = _javascript_test_impact(context, results, subjects)
    parsed_count = sum(item.completeness is Completeness.COMPLETE for item in results)
    partial_count = sum(item.completeness is Completeness.PARTIAL for item in results)
    failed_count = len(failed_sources)
    coverage = LanguageFrontendCoverage(
        language=SourceLanguage.JAVASCRIPT,
        frontend_id="javascript-tree-sitter",
        state=(
            SectionState.PARTIAL
            if partial_count or failed_count or unresolved_imports or bound_omissions
            else SectionState.COMPLETE
        ),
        discovered=len(results) + failed_count,
        complete=parsed_count,
        partial=partial_count,
        failed=failed_count,
        unavailable=0,
        diagnostic_codes=tuple(sorted({item.code for item in all_diagnostics})),
        capabilities=frontend_for(SourceLanguage.JAVASCRIPT).capabilities,
    )
    source_files = tuple(
        sorted(
            (*python_sources, *js_sources, *failed_sources),
            key=lambda item: item.source_file_id,
        )
    )
    provider = ProviderResult(
        provider_id="javascript-tree-sitter",
        state=(
            ProviderState.PARTIAL
            if coverage.state is SectionState.PARTIAL
            else ProviderState.SUCCESS
        ),
        executable="python-extension",
        version=results[0].provenance.grammar_version if results else "0.25.0",
        duration_ms=0,
        observations=(
            "javascript-module-inventory",
            "javascript-symbol-inventory",
            "javascript-import-export-graph",
            "javascript-complexity",
            "javascript-duplicate-functions",
        ),
        diagnostics=all_diagnostics,
        completeness=(
            Completeness.PARTIAL
            if coverage.state is SectionState.PARTIAL
            else Completeness.COMPLETE
        ),
    )
    summary = SummaryCounts.model_validate(
        {
            **report.summary.model_dump(),
            "source_files": len(source_files),
            "discovered_files": report.summary.discovered_files + len(all_js_modules),
            "parsed_files": report.summary.parsed_files + len(results),
            "failed_files": report.summary.failed_files + failed_count,
            "modules": report.summary.modules + len(all_js_modules),
            "classes": report.summary.classes
            + sum(
                symbol.kind in {"class", "nested_class"}
                for module in modules
                for symbol in module.symbols
            ),
            "functions": report.summary.functions
            + sum(
                symbol.kind in {"function", "nested_function"}
                for module in modules
                for symbol in module.symbols
            ),
            "methods": report.summary.methods
            + sum(symbol.kind == "method" for module in modules for symbol in module.symbols),
            "import_edges": report.summary.import_edges + len(edges),
            "subjects": report.summary.subjects + len(subjects),
            "module_exports": len(exports),
            "duplicate_functions": len(duplicates),
            "behavior_differences": len(differences),
            "state_resources": report.summary.state_resources + len(state_resources),
            "side_effects": report.summary.side_effects + len(side_effects),
            "clone_fragments": report.summary.clone_fragments + len(fragments),
            "clone_clusters": report.summary.clone_clusters + len(clusters),
            "function_profiles": report.summary.function_profiles + len(profiles),
            "function_commonalities": report.summary.function_commonalities + len(commonalities),
            "test_modules": report.summary.test_modules + len(js_test_modules),
            "test_relationships": report.summary.test_relationships + len(js_test_relationships),
            "unresolved_edges": report.summary.unresolved_edges + len(unresolved_imports),
        }
    )
    completeness = (
        Completeness.PARTIAL
        if coverage.state is SectionState.PARTIAL or report.completeness is Completeness.PARTIAL
        else report.completeness
    )
    payload = {
        **report.model_dump(),
        "source_files": source_files,
        "target": report.target.model_copy(
            update={
                "languages": tuple(
                    sorted({item.language for item in source_files}, key=lambda item: item.value)
                )
            }
        ),
        "language_frontends": tuple(
            sorted(
                (*report.language_frontends, coverage),
                key=lambda item: item.language.value,
            )
        ),
        "parser_provenance": tuple(
            sorted(
                (item.provenance for item in results),
                key=lambda item: (
                    item.language.value,
                    item.logical_path,
                    item.frontend_id,
                    item.syntax_mode.value if item.syntax_mode else "",
                ),
            )
        ),
        "modules": tuple(
            sorted((*report.modules, *all_js_modules), key=lambda item: (item.name, item.path))
        ),
        "module_exports": exports,
        "complexity_observations": (
            *report.complexity_observations,
            *(
                symbol.complexity
                for module in modules
                for symbol in module.symbols
                if symbol.complexity
            ),
        ),
        "import_edges": tuple(
            sorted(
                (*report.import_edges, *edges),
                key=lambda item: (item.source, item.target, item.kind),
            )
        ),
        "subjects": tuple(sorted((*report.subjects, *subjects), key=lambda item: item.subject_id)),
        "clone_fragments": tuple(
            sorted((*report.clone_fragments, *fragments), key=lambda item: item.fragment_id)
        ),
        "clone_clusters": tuple(
            sorted((*report.clone_clusters, *clusters), key=lambda item: item.cluster_id)
        ),
        "function_profiles": tuple(
            sorted((*report.function_profiles, *profiles), key=lambda item: item.profile_id)
        ),
        "function_commonalities": tuple(
            sorted(
                (*report.function_commonalities, *commonalities),
                key=lambda item: item.commonality_id,
            )
        ),
        "duplicate_functions": duplicates,
        "behavior_differences": differences,
        "state_resources": tuple(
            sorted(
                (*report.state_resources, *state_resources),
                key=lambda item: item.state_resource_id,
            )
        ),
        "side_effects": tuple(
            sorted((*report.side_effects, *side_effects), key=lambda item: item.side_effect_id)
        ),
        "test_modules": tuple(
            sorted((*report.test_modules, *js_test_modules), key=lambda item: item.test_module_id)
        ),
        "test_relationships": tuple(
            sorted(
                (*report.test_relationships, *js_test_relationships),
                key=lambda item: item.relationship_id,
            )
        ),
        "unresolved_edges": tuple(
            sorted(
                (*report.unresolved_edges, *unresolved_imports),
                key=lambda item: item.unresolved_id,
            )
        ),
        "diagnostics": tuple(
            sorted(
                (*report.diagnostics, *all_diagnostics),
                key=lambda item: (item.path or "", item.code),
            )
        ),
        "providers": (*report.providers, provider),
        "completeness": completeness,
        "limitations": tuple(
            sorted(
                {
                    *report.limitations,
                    "JavaScript analysis is syntax-only; dynamic dispatch, package export "
                    "maps, bundler aliases, and framework semantics remain unresolved.",
                }
            )
        ),
        "summary": summary,
    }
    language_sections = {
        "r1-static-analysis": (len(results), failed_count + partial_count),
        "clones": (len(fragments), 0),
        "function-commonality": (len(profiles), 0),
        "state-side-effects": (len(state_resources) + len(side_effects), 0),
    }
    payload["section_coverage"] = tuple(
        (
            section.model_copy(
                update={
                    "languages": tuple(
                        sorted(
                            {*section.languages, SourceLanguage.JAVASCRIPT},
                            key=lambda item: item.value,
                        )
                    ),
                    "parsed": section.parsed + language_sections[section.section][0],
                    "failed": section.failed + language_sections[section.section][1],
                    "state": (
                        SectionState.PARTIAL
                        if language_sections[section.section][1]
                        else section.state
                    ),
                    "diagnostic_codes": tuple(
                        sorted(
                            {
                                *section.diagnostic_codes,
                                *(
                                    item.code
                                    for item in all_diagnostics
                                    if language_sections[section.section][1]
                                ),
                            }
                        )
                    ),
                }
            )
            if section.section in language_sections
            else section
        )
        for section in report.section_coverage
    )
    if unresolved_imports:
        payload["section_coverage"] = tuple(
            (
                section.model_copy(
                    update={
                        "state": SectionState.PARTIAL,
                        "unresolved": section.unresolved + len(unresolved_imports),
                        "diagnostic_codes": tuple(
                            sorted({*section.diagnostic_codes, "javascript-import-unresolved"})
                        ),
                    }
                )
                if section.section == "r1-static-analysis"
                else section
            )
            for section in payload["section_coverage"]
        )
    if bound_omissions:
        payload["section_coverage"] = tuple(
            (
                section.model_copy(
                    update={
                        "state": SectionState.TRUNCATED,
                        "truncated": section.truncated
                        + (clone_omissions if section.section == "clones" else pair_omissions),
                        "diagnostic_codes": tuple(
                            sorted(
                                {
                                    *section.diagnostic_codes,
                                    "javascript-analysis-truncated",
                                }
                            )
                        ),
                    }
                )
                if section.section in {"clones", "function-commonality"}
                and (
                    (section.section == "clones" and clone_omissions)
                    or (section.section == "function-commonality" and pair_omissions)
                )
                else section
            )
            for section in payload["section_coverage"]
        )
    admission = report.signal_quality.commonality_admission
    if admission is not None:
        payload["signal_quality"] = report.signal_quality.model_copy(
            update={
                "commonality_admission": admission.model_copy(
                    update={
                        "profile_count": admission.profile_count + len(profiles),
                        "unique_candidates": admission.unique_candidates + len(commonalities),
                        "clone_admissions": admission.clone_admissions + len(commonalities),
                        "admitted_pairs": admission.admitted_pairs + len(commonalities),
                        "candidate_bound_omissions": (
                            admission.candidate_bound_omissions + pair_omissions
                        ),
                        "candidate_bound_omissions_exact": (
                            admission.candidate_bound_omissions_exact and not pair_omissions
                        ),
                    }
                )
            }
        )
    if js_test_modules:
        payload["section_coverage"] = tuple(
            (
                section.model_copy(
                    update={
                        "state": SectionState.COMPLETE,
                        "required": True,
                        "parsed": section.parsed + len(js_test_modules),
                        "languages": tuple(
                            sorted(
                                {*section.languages, SourceLanguage.JAVASCRIPT},
                                key=lambda item: item.value,
                            )
                        ),
                    }
                )
                if section.section == "test-impact"
                else section
            )
            for section in payload["section_coverage"]
        )
    payload["run_identity_sha256"] = hashlib.sha256(
        json.dumps(
            {
                "prior": report.run_identity_sha256,
                "sources": tuple(
                    (item.logical_path, item.source_sha256, item.parse_state.value)
                    for item in source_files
                ),
                "frontend": tuple(item.provenance.model_dump(mode="json") for item in results),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return AnalysisReport.model_validate(payload)


def _replace_language_records(
    report: AnalysisReport,
    python_sources: tuple[SourceFileRecord, ...],
    js_sources: tuple[SourceFileRecord, ...],
    coverage: LanguageFrontendCoverage | None,
) -> AnalysisReport:
    source_files = tuple(
        sorted((*python_sources, *js_sources), key=lambda item: item.source_file_id)
    )
    language_frontends = tuple(
        item
        for item in report.language_frontends
        if item.language is not SourceLanguage.JAVASCRIPT
        and (python_sources or item.language is not SourceLanguage.PYTHON)
    )
    if coverage is not None:
        language_frontends = (*language_frontends, coverage)
    language_frontends = tuple(sorted(language_frontends, key=lambda item: item.language.value))
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "source_files": source_files,
            "target": report.target.model_copy(
                update={
                    "languages": tuple(
                        sorted(
                            {item.language for item in source_files},
                            key=lambda item: item.value,
                        )
                    )
                }
            ),
            "language_frontends": language_frontends,
            "summary": {**report.summary.model_dump(), "source_files": len(source_files)},
        }
    )


def _unavailable_report(
    context: AnalysisContext,
    report: AnalysisReport,
    python_sources: tuple[SourceFileRecord, ...],
    paths: tuple[Path, ...],
    detail: str | None,
) -> AnalysisReport:
    sources: list[SourceFileRecord] = []
    modules: list[Module] = []
    diagnostics: list[Diagnostic] = []
    for path in paths:
        data = path.read_bytes()
        relative = logical_path(context, path)
        name = javascript_module_name(context, path)
        digest = hashlib.sha256(data).hexdigest()
        sources.append(_failed_source(relative, name, digest, SourceParseState.UNAVAILABLE))
        modules.append(_failed_module(relative, name, digest, SourceParseState.UNAVAILABLE))
        diagnostics.append(
            Diagnostic(
                code="javascript-parser-unavailable",
                level=DiagnosticLevel.ERROR,
                message=detail or "optional JavaScript parser is unavailable",
                path=relative,
                provider="javascript-tree-sitter",
                language=SourceLanguage.JAVASCRIPT,
            )
        )
    coverage = LanguageFrontendCoverage(
        language=SourceLanguage.JAVASCRIPT,
        frontend_id="javascript-tree-sitter",
        state=SectionState.UNAVAILABLE,
        discovered=len(paths),
        complete=0,
        partial=0,
        failed=0,
        unavailable=len(paths),
        diagnostic_codes=("javascript-parser-unavailable",),
        capabilities=tuple(
            item.model_copy(
                update={
                    "state": AdapterCapabilityState.UNAVAILABLE,
                    "limitations": (detail or "optional JavaScript parser is unavailable",),
                }
            )
            for item in frontend_for(SourceLanguage.JAVASCRIPT).capabilities
        ),
    )
    source_files = tuple(sorted((*python_sources, *sources), key=lambda item: item.source_file_id))
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "source_files": source_files,
            "target": report.target.model_copy(
                update={
                    "languages": tuple(
                        sorted(
                            {item.language for item in source_files},
                            key=lambda item: item.value,
                        )
                    )
                }
            ),
            "language_frontends": tuple(
                sorted(
                    (*report.language_frontends, coverage),
                    key=lambda item: item.language.value,
                )
            ),
            "modules": tuple(
                sorted((*report.modules, *modules), key=lambda item: (item.name, item.path))
            ),
            "diagnostics": tuple(
                sorted(
                    (*report.diagnostics, *diagnostics),
                    key=lambda item: (item.path or "", item.code),
                )
            ),
            "providers": (
                *report.providers,
                ProviderResult(
                    provider_id="javascript-tree-sitter",
                    state=ProviderState.UNAVAILABLE,
                    version=None,
                    duration_ms=0,
                    diagnostics=tuple(diagnostics),
                    completeness=Completeness.PARTIAL,
                ),
            ),
            "completeness": Completeness.PARTIAL,
            "summary": {
                **report.summary.model_dump(),
                "source_files": len(source_files),
                "discovered_files": report.summary.discovered_files + len(paths),
                "failed_files": report.summary.failed_files + len(paths),
                "modules": report.summary.modules + len(paths),
            },
        }
    )


def _empty_source_report(report: AnalysisReport) -> AnalysisReport:
    diagnostic = Diagnostic(
        code="no-selected-source",
        level=DiagnosticLevel.ERROR,
        message="target contains no source for the selected languages",
        provider="language-discovery",
    )
    sections = tuple(
        (
            section.model_copy(
                update={
                    "state": SectionState.PARTIAL,
                    "unresolved": max(1, section.unresolved),
                    "diagnostic_codes": tuple(sorted({*section.diagnostic_codes, diagnostic.code})),
                }
            )
            if section.section == "r1-static-analysis"
            else section
        )
        for section in report.section_coverage
    )
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "section_coverage": sections,
            "diagnostics": (*report.diagnostics, diagnostic),
            "completeness": Completeness.PARTIAL,
            "limitations": tuple(
                sorted({*report.limitations, "No selected supported source was discovered."})
            ),
        }
    )


def _source_record(result: FrontendResult) -> SourceFileRecord:
    return SourceFileRecord(
        source_file_id=_source_file_id(result.unit.logical_path, SourceLanguage.JAVASCRIPT),
        logical_path=result.unit.logical_path,
        logical_root=".",
        source_sha256=result.unit.source_sha256,
        language=SourceLanguage.JAVASCRIPT,
        parse_state=(
            SourceParseState.COMPLETE
            if result.completeness is Completeness.COMPLETE
            else SourceParseState.PARTIAL
        ),
        syntax_mode=JavaScriptSyntaxMode(result.syntax_mode),
        module_system=result.module_system,
        module_name=result.unit.module_name,
        physical_loc=len(result.unit.source_bytes.splitlines()),
        nonblank_loc=sum(bool(line.strip()) for line in result.unit.source_bytes.splitlines()),
    )


def _failed_source(
    path: str, module: str, digest: str, state: SourceParseState
) -> SourceFileRecord:
    return SourceFileRecord(
        source_file_id=_source_file_id(path, SourceLanguage.JAVASCRIPT),
        logical_path=path,
        logical_root=".",
        source_sha256=digest,
        language=SourceLanguage.JAVASCRIPT,
        parse_state=state,
        syntax_mode=(
            JavaScriptSyntaxMode.JSX if path.endswith(".jsx") else JavaScriptSyntaxMode.JAVASCRIPT
        ),
        module_system=ModuleSystem.UNRESOLVED,
        module_name=module,
        physical_loc=0,
        nonblank_loc=0,
    )


def _failed_module(path: str, name: str, digest: str, state: SourceParseState) -> Module:
    return Module(
        name=name,
        path=path,
        source_sha256=digest,
        line_count=0,
        language=SourceLanguage.JAVASCRIPT,
        parse_state=state,
        syntax_mode=(
            JavaScriptSyntaxMode.JSX if path.endswith(".jsx") else JavaScriptSyntaxMode.JAVASCRIPT
        ),
        module_system=ModuleSystem.UNRESOLVED,
    )


def _module_record(result: FrontendResult) -> Module:
    symbols = tuple(_symbol_record(result, item) for item in result.symbols)
    return Module(
        name=result.unit.module_name,
        path=result.unit.logical_path,
        source_sha256=result.unit.source_sha256,
        line_count=len(result.unit.source_bytes.splitlines()),
        nonblank_line_count=sum(
            bool(line.strip()) for line in result.unit.source_bytes.splitlines()
        ),
        symbols=symbols,
        language=SourceLanguage.JAVASCRIPT,
        parse_state=(
            SourceParseState.COMPLETE
            if result.completeness is Completeness.COMPLETE
            else SourceParseState.PARTIAL
        ),
        syntax_mode=JavaScriptSyntaxMode(result.syntax_mode),
        module_system=result.module_system,
    )


def _symbol_record(result: FrontendResult, fact: object) -> Symbol:
    from code_analysis.languages.base import NormalizedSymbolFact

    assert isinstance(fact, NormalizedSymbolFact)
    complexity = fact.complexity
    return Symbol(
        qualified_name=fact.qualified_name,
        name=fact.name,
        kind=fact.kind,  # type: ignore[arg-type]
        span=SourceSpan(
            path=result.unit.logical_path,
            start_line=fact.start_line,
            start_column=fact.start_column,
            end_line=fact.end_line,
            end_column=fact.end_column,
            language=SourceLanguage.JAVASCRIPT,
        ),
        parameters=fact.parameters,
        returns=fact.returns,
        complexity=(
            ComplexityObservation(**asdict(complexity), language=SourceLanguage.JAVASCRIPT)
            if complexity
            else None
        ),
        language=SourceLanguage.JAVASCRIPT,
        async_=fact.async_,
        generator=fact.generator,
    )


def _evidence(
    result: FrontendResult, start: int, start_col: int, end: int, end_col: int, observation: str
) -> Evidence:
    return Evidence(
        span=SourceSpan(
            path=result.unit.logical_path,
            start_line=start,
            start_column=start_col,
            end_line=end,
            end_column=end_col,
            language=SourceLanguage.JAVASCRIPT,
        ),
        provider="javascript-tree-sitter",
        observation=observation,
        confidence=Confidence.EXACT,
        source_sha256=result.unit.source_sha256,
        language=SourceLanguage.JAVASCRIPT,
    )


def _subjects(result: FrontendResult) -> tuple[AnalysisSubject, ...]:
    end_line = max(1, len(result.unit.source_bytes.splitlines()))
    module_evidence = _evidence(
        result,
        1,
        0,
        end_line,
        0,
        "JavaScript module or script",
    )
    records = [
        AnalysisSubject(
            subject_id=_stable_id(
                "subject",
                SourceLanguage.JAVASCRIPT.value,
                ".",
                result.unit.logical_path,
                "module",
                result.unit.module_name,
            ),
            kind=SubjectKind.MODULE,
            logical_name=result.unit.module_name,
            module=result.unit.module_name,
            evidence=(module_evidence,),
            language=SourceLanguage.JAVASCRIPT,
        )
    ]
    for item in result.symbols:
        evidence = _evidence(
            result,
            item.start_line,
            item.start_column,
            item.end_line,
            item.end_column,
            f"JavaScript {item.kind} declaration",
        )
        records.append(
            AnalysisSubject(
                subject_id=_stable_id(
                    "subject",
                    SourceLanguage.JAVASCRIPT.value,
                    ".",
                    result.unit.logical_path,
                    item.kind,
                    item.qualified_name,
                ),
                kind=SubjectKind.SYMBOL,
                logical_name=f"{result.unit.module_name}:{item.qualified_name}",
                module=result.unit.module_name,
                evidence=(evidence,),
                language=SourceLanguage.JAVASCRIPT,
            )
        )
    return tuple(records)


def _state_resources(
    results: tuple[FrontendResult, ...], subjects: tuple[AnalysisSubject, ...]
) -> tuple[StateResource, ...]:
    by_logical = {item.logical_name: item for item in subjects}
    records = []
    for result in results:
        for fact in result.states:
            owner_name = (
                f"{result.unit.module_name}:{fact.owner_qualified_name}"
                if fact.owner_qualified_name
                else result.unit.module_name
            )
            owner = by_logical[owner_name]
            evidence = _evidence(
                result,
                fact.start_line,
                fact.start_column,
                fact.end_line,
                fact.end_column,
                (
                    "JavaScript mutable class field"
                    if fact.class_state
                    else "JavaScript mutable module binding"
                ),
            )
            logical_name = f"{owner_name}:{fact.name}"
            records.append(
                StateResource(
                    state_resource_id=_stable_id(
                        "state-resource",
                        SourceLanguage.JAVASCRIPT.value,
                        logical_name,
                    ),
                    kind=(
                        StateResourceKind.MUTABLE_CLASS_STATE
                        if fact.class_state
                        else StateResourceKind.MUTABLE_GLOBAL
                    ),
                    scope=StructureScope.CLASS if fact.class_state else StructureScope.MODULE,
                    owner_subject_id=owner.subject_id,
                    logical_name=logical_name,
                    declaration_evidence=(evidence,),
                    write_evidence=(evidence,),
                    confidence=Confidence.EXACT,
                    completeness=Completeness.COMPLETE,
                    limitations=(
                        "syntax evidence does not establish runtime ownership or mutation",
                    ),
                )
            )
    return tuple(sorted(records, key=lambda item: item.state_resource_id))


_SIDE_EFFECT_KINDS = {
    "network-client": SideEffectKind.NETWORK_CLIENT,
    "storage": SideEffectKind.STORAGE,
    "dom": SideEffectKind.DOM_MUTATION,
    "timer": SideEffectKind.TIMER,
    "filesystem": SideEffectKind.FILESYSTEM_WRITE,
    "subprocess": SideEffectKind.SUBPROCESS,
    "event-listener": SideEffectKind.EVENT_LISTENER,
    "promise-chain": SideEffectKind.PROMISE_CHAIN,
}


def _side_effects(
    results: tuple[FrontendResult, ...], subjects: tuple[AnalysisSubject, ...]
) -> tuple[SideEffectObservation, ...]:
    by_logical = {item.logical_name: item for item in subjects}
    records = []
    for result in results:
        for function in result.functions:
            subject = by_logical[f"{result.unit.module_name}:{function.qualified_name}"]
            for effect in sorted(set(function.side_effects)):
                kind = _SIDE_EFFECT_KINDS.get(effect)
                if kind is None:
                    continue
                evidence = _evidence(
                    result,
                    function.start_line,
                    function.start_column,
                    function.end_line,
                    function.end_column,
                    f"JavaScript provenance-resolved {effect} syntax",
                )
                records.append(
                    SideEffectObservation(
                        side_effect_id=_stable_id(
                            "side-effect",
                            SourceLanguage.JAVASCRIPT.value,
                            subject.subject_id,
                            kind.value,
                        ),
                        subject_id=subject.subject_id,
                        kind=kind,
                        target_identity=effect,
                        evidence=(evidence,),
                        confidence=Confidence.RESOLVED,
                        completeness=Completeness.COMPLETE,
                        limitations=(
                            "syntax evidence does not establish side-effect success or "
                            "runtime outcome",
                        ),
                    )
                )
    return tuple(sorted(records, key=lambda item: item.side_effect_id))


def _exports(result: FrontendResult) -> tuple[ModuleExportRecord, ...]:
    source_id = _source_file_id(result.unit.logical_path, SourceLanguage.JAVASCRIPT)
    return tuple(
        ModuleExportRecord(
            export_id=_stable_id(
                "module-export",
                SourceLanguage.JAVASCRIPT.value,
                result.unit.logical_path,
                item.kind,
                item.name,
                item.local_name,
                item.source_specifier,
                item.start_line,
                item.start_column,
            ),
            module=result.unit.module_name,
            source_file_id=source_id,
            name=item.name,
            kind=item.kind,
            local_name=item.local_name,
            source_specifier=item.source_specifier,
            evidence=(
                _evidence(
                    result,
                    item.start_line,
                    item.start_column,
                    item.end_line,
                    item.end_column,
                    f"JavaScript {item.kind} export",
                ),
            ),
        )
        for item in result.exports
    )


def _import_edges(results: tuple[FrontendResult, ...]) -> tuple[ImportEdge, ...]:
    known = {item.unit.module_name for item in results}
    edges: set[tuple[str, str, bool, str]] = set()
    for result in results:
        for item in result.imports:
            target, local = _resolve_specifier(result.unit.module_name, item.specifier, known)
            if item.kind == "dynamic-unresolved" or (item.specifier.startswith(".") and not local):
                continue
            edges.add((result.unit.module_name, target, local, item.kind))
    return tuple(
        ImportEdge(
            source=source,
            target=target,
            local=local,
            kind=kind,
            source_language=SourceLanguage.JAVASCRIPT,
            target_language=SourceLanguage.JAVASCRIPT if local else None,
        )
        for source, target, local, kind in sorted(edges)
    )


def _modules_with_fan_counts(
    modules: tuple[Module, ...], edges: tuple[ImportEdge, ...]
) -> tuple[Module, ...]:
    known = {item.name for item in modules}
    incoming: dict[str, set[str]] = {name: set() for name in known}
    outgoing: dict[str, set[str]] = {name: set() for name in known}
    for edge in edges:
        outgoing.setdefault(edge.source, set()).add(edge.target)
        if edge.local and edge.target in known:
            incoming.setdefault(edge.target, set()).add(edge.source)
    return tuple(
        item.model_copy(
            update={
                "fan_in": len(incoming.get(item.name, ())),
                "fan_out": len(outgoing.get(item.name, ())),
            }
        )
        for item in modules
    )


def _unresolved_import_edges(
    results: tuple[FrontendResult, ...],
    subjects: tuple[AnalysisSubject, ...],
) -> tuple[UnresolvedEdge, ...]:
    known = {item.unit.module_name for item in results}
    module_subjects = {item.module: item for item in subjects if item.kind is SubjectKind.MODULE}
    records: list[UnresolvedEdge] = []
    for result in results:
        source = module_subjects[result.unit.module_name]
        for item in result.imports:
            _target, local = _resolve_specifier(
                result.unit.module_name,
                item.specifier,
                known,
            )
            if item.kind != "dynamic-unresolved" and (not item.specifier.startswith(".") or local):
                continue
            reason = (
                "computed JavaScript import target cannot be resolved statically"
                if item.kind == "dynamic-unresolved"
                else "relative JavaScript import target is missing or ambiguous"
            )
            evidence = _evidence(
                result,
                item.start_line,
                item.start_column,
                item.end_line,
                item.end_column,
                reason,
            )
            records.append(
                UnresolvedEdge(
                    unresolved_id=_stable_id(
                        "unresolved",
                        source.subject_id,
                        item.specifier,
                        item.start_line,
                        item.start_column,
                    ),
                    source_id=source.subject_id,
                    attempted_target=(
                        None if item.kind == "dynamic-unresolved" else item.specifier
                    ),
                    operation="javascript-import-resolution",
                    reason=reason,
                    effect_on_completeness=True,
                    evidence=(evidence,),
                )
            )
    return tuple(sorted(records, key=lambda item: item.unresolved_id))


def _resolve_specifier(source: str, specifier: str, known: set[str]) -> tuple[str, bool]:
    if not specifier.startswith("."):
        return specifier, False
    base = posixpath.normpath(posixpath.join(posixpath.dirname(source), specifier))
    candidates = (
        [base]
        if PurePosixPath(base).suffix in JAVASCRIPT_SUFFIXES
        else [f"{base}{suffix}" for suffix in sorted(JAVASCRIPT_SUFFIXES)]
        + [f"{base}/index{suffix}" for suffix in sorted(JAVASCRIPT_SUFFIXES)]
    )
    matches = [item for item in candidates if item in known]
    return (matches[0], True) if len(matches) == 1 else (specifier, False)


def _duplicates(
    context: AnalysisContext,
    results: tuple[FrontendResult, ...],
    subjects: tuple[AnalysisSubject, ...],
    cluster_pairs: dict[tuple[str, str], tuple[str, ...]],
) -> tuple[tuple[BehaviorDifference, ...], tuple[DuplicateFunctionPair, ...], int]:
    subject_by_name = {subject.logical_name: subject for subject in subjects}
    functions = [(result, item) for result in results for item in result.functions]
    postings: dict[str, list[int]] = defaultdict(list)
    for index, (_result, item) in enumerate(functions):
        for signature in sorted(set(item.statement_signatures)):
            postings[signature].append(index)
    pair_limit = context.config.phase_three_limits.max_candidate_pairs
    pair_set: set[tuple[int, int]] = set()
    pair_omissions = 0
    for signature in sorted(postings):
        values = postings[signature]
        for offset, left in enumerate(values):
            for right in values[offset + 1 :]:
                pair = (left, right)
                if pair in pair_set:
                    continue
                if len(pair_set) >= pair_limit:
                    pair_omissions = 1
                    break
                pair_set.add(pair)
            if pair_omissions:
                break
        if pair_omissions:
            break
    pairs = sorted(pair_set)
    differences: list[BehaviorDifference] = []
    duplicates: list[DuplicateFunctionPair] = []
    threshold = context.config.clone_thresholds
    known_modules = {item.unit.module_name for item in results}
    for left_index, right_index in pairs:
        left_result, left = functions[left_index]
        right_result, right = functions[right_index]
        matcher = SequenceMatcher(
            a=left.statement_signatures, b=right.statement_signatures, autojunk=False
        )
        matched = sum(block.size for block in matcher.get_matching_blocks())
        if (
            matched < threshold.near_clone_min_statements
            or matched / len(left.statement_signatures)
            < threshold.near_clone_min_bilateral_coverage
            or matched / len(right.statement_signatures)
            < threshold.near_clone_min_bilateral_coverage
        ):
            continue
        opcodes = matcher.get_opcodes()
        insertions = sum(j2 - j1 for tag, _i1, _i2, j1, j2 in opcodes if tag == "insert")
        deletions = sum(i2 - i1 for tag, i1, i2, _j1, _j2 in opcodes if tag == "delete")
        substitutions = sum(
            max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in opcodes if tag == "replace"
        )
        if insertions + deletions + substitutions > threshold.near_clone_max_changed_statements:
            continue
        left_subject = subject_by_name[f"{left_result.unit.module_name}:{left.qualified_name}"]
        right_subject = subject_by_name[f"{right_result.unit.module_name}:{right.qualified_name}"]
        left_id, right_id = sorted((left_subject.subject_id, right_subject.subject_id))
        left_first = left_subject.subject_id == left_id
        canonical_left_subject = left_subject if left_first else right_subject
        canonical_right_subject = right_subject if left_first else left_subject
        pair_differences: list[BehaviorDifference] = []
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == "equal":
                for offset in range(i2 - i1):
                    left_detail = left.detailed_statement_signatures[i1 + offset]
                    right_detail = right.detailed_statement_signatures[j1 + offset]
                    if left_detail == right_detail:
                        continue
                    canonical_left = left_detail if left_first else right_detail
                    canonical_right = right_detail if left_first else left_detail
                    pair_differences.append(
                        _behavior_difference(
                            left_id,
                            right_id,
                            i1 + offset,
                            canonical_left,
                            canonical_right,
                            canonical_left_subject,
                            canonical_right_subject,
                        )
                    )
            elif tag == "replace":
                physical_left = " | ".join(left.detailed_statement_signatures[i1:i2])
                physical_right = " | ".join(right.detailed_statement_signatures[j1:j2])
                pair_differences.append(
                    _behavior_difference(
                        left_id,
                        right_id,
                        i1,
                        physical_left if left_first else physical_right,
                        physical_right if left_first else physical_left,
                        canonical_left_subject,
                        canonical_right_subject,
                    )
                )
        differences.extend(pair_differences)
        divergence = []
        if left.exceptions != right.exceptions:
            divergence.append(DivergenceReason.EXCEPTION)
        if left.side_effects != right.side_effects:
            divergence.append(DivergenceReason.SIDE_EFFECT)
        evidence = (left_subject.evidence[0], right_subject.evidence[0])
        canonical_owner = _canonical_owner(
            left_result,
            left,
            left_subject,
            right_result,
            right,
            right_subject,
            known_modules,
        )
        duplicates.append(
            DuplicateFunctionPair(
                duplicate_id=_stable_id(
                    "duplicate", SourceLanguage.JAVASCRIPT.value, left_id, right_id
                ),
                language=SourceLanguage.JAVASCRIPT,
                left_subject_id=left_id,
                right_subject_id=right_id,
                clone_cluster_ids=cluster_pairs.get((left_id, right_id), ()),
                matched_statements=matched,
                left_statement_total=len(left.statement_signatures),
                right_statement_total=len(right.statement_signatures),
                insertions=insertions,
                deletions=deletions,
                substitutions=substitutions,
                behavior_difference_ids=tuple(
                    sorted(item.difference_id for item in pair_differences)
                ),
                divergence_reasons=tuple(sorted(divergence, key=lambda item: item.value)),
                canonical_owner=canonical_owner,
                evidence=evidence,
                limitations=("syntax evidence cannot establish semantic equivalence",),
            )
        )
    return (
        tuple(sorted(differences, key=lambda item: item.difference_id)),
        tuple(sorted(duplicates, key=lambda item: item.duplicate_id)),
        pair_omissions,
    )


def _canonical_owner(
    left_result: FrontendResult,
    left: object,
    left_subject: AnalysisSubject,
    right_result: FrontendResult,
    right: object,
    right_subject: AnalysisSubject,
    known_modules: set[str],
) -> CanonicalOwnerSuggestion:
    from code_analysis.languages.base import NormalizedFunctionFact

    assert isinstance(left, NormalizedFunctionFact)
    assert isinstance(right, NormalizedFunctionFact)
    compatible = (
        left.exceptions == right.exceptions
        and left.side_effects == right.side_effects
        and left.schema_operations == right.schema_operations
        and left.attributes_globals == right.attributes_globals
    )
    if not compatible:
        return CanonicalOwnerSuggestion(
            state=CanonicalOwnerState.UNRESOLVED,
            reason="contract, state, exception, or side-effect evidence is incompatible",
        )
    left_imports_right = _imports_module(left_result, right_result.unit.module_name, known_modules)
    right_imports_left = _imports_module(right_result, left_result.unit.module_name, known_modules)
    if left_imports_right != right_imports_left:
        owner = right_subject if left_imports_right else left_subject
        return CanonicalOwnerSuggestion(
            state=CanonicalOwnerState.SUGGESTED,
            subject_id=owner.subject_id,
            reason="one-way resolved local import direction supports the canonical owner",
        )
    return CanonicalOwnerSuggestion(
        state=CanonicalOwnerState.UNRESOLVED,
        reason="a unique resolved local caller or import direction is not proven",
    )


def _imports_module(
    source: FrontendResult,
    target_module: str,
    known_modules: set[str],
) -> bool:
    return any(
        _resolve_specifier(source.unit.module_name, item.specifier, known_modules)
        == (target_module, True)
        for item in source.imports
        if item.kind != "dynamic-unresolved"
    )


def _behavior_difference(
    left_subject_id: str,
    right_subject_id: str,
    statement_index: int,
    left_identity: str,
    right_identity: str,
    left_subject: AnalysisSubject,
    right_subject: AnalysisSubject,
) -> BehaviorDifference:
    lowered = f"{left_identity} {right_identity}".lower()
    if "call_expression" in lowered or "new_expression" in lowered:
        kind = BehaviorDifferenceKind.CALL
    elif any(item in lowered for item in ("if_statement", "switch_statement", "ternary")):
        kind = BehaviorDifferenceKind.BRANCH
    elif any(item in lowered for item in ("throw_statement", "catch_clause")):
        kind = BehaviorDifferenceKind.EXCEPTION
    elif any(item in lowered for item in ("assignment", "update_expression", "delete")):
        kind = BehaviorDifferenceKind.STATE
    elif "default" in lowered:
        kind = BehaviorDifferenceKind.DEFAULT
    else:
        kind = BehaviorDifferenceKind.CONSTANT
    return BehaviorDifference(
        difference_id=_stable_id(
            "behavior-difference",
            left_subject_id,
            right_subject_id,
            statement_index,
            kind.value,
            left_identity,
            right_identity,
        ),
        kind=kind,
        left_identity=left_identity,
        right_identity=right_identity,
        statement_index=statement_index,
        evidence=(left_subject.evidence[0], right_subject.evidence[0]),
    )


def _function_subjects(
    results: tuple[FrontendResult, ...], subjects: tuple[AnalysisSubject, ...]
) -> dict[tuple[str, str], AnalysisSubject]:
    by_name = {item.logical_name: item for item in subjects}
    return {
        (result.unit.module_name, function.qualified_name): by_name[
            f"{result.unit.module_name}:{function.qualified_name}"
        ]
        for result in results
        for function in result.functions
    }


def _function_profiles(
    results: tuple[FrontendResult, ...], subjects: tuple[AnalysisSubject, ...]
) -> tuple[FunctionFeatureProfile, ...]:
    subject_map = _function_subjects(results, subjects)
    records = []
    for result in results:
        for item in result.functions:
            subject = subject_map[(result.unit.module_name, item.qualified_name)]
            records.append(
                FunctionFeatureProfile(
                    profile_id=_stable_id("function-profile", subject.subject_id),
                    subject_id=subject.subject_id,
                    callees=item.callees,
                    signature_shapes=(f"parameters:{len(item.parameters)}",),
                    attributes_globals=item.attributes_globals,
                    exceptions=item.exceptions,
                    side_effects=item.side_effects,
                    control_flow=tuple(sorted(set(item.control_events))),
                    dictionary_schema_operations=item.schema_operations,
                    unresolved_call_count=len(item.unresolved_calls),
                    evidence=subject.evidence,
                    limitations=(
                        "JavaScript feature resolution is lexical and dynamic dispatch "
                        "remains unresolved",
                    ),
                    language=SourceLanguage.JAVASCRIPT,
                )
            )
    return tuple(sorted(records, key=lambda item: item.profile_id))


def _clone_records(
    context: AnalysisContext,
    results: tuple[FrontendResult, ...],
    subjects: tuple[AnalysisSubject, ...],
) -> tuple[
    tuple[CloneFragment, ...],
    tuple[CloneCluster, ...],
    dict[tuple[str, str], tuple[str, ...]],
    int,
]:
    subject_map = _function_subjects(results, subjects)
    candidates: list[tuple[FrontendResult, object, AnalysisSubject, str, str, int, int]] = []
    for result in results:
        for item in result.functions:
            body = result.unit.source_bytes[item.body_start_byte : item.body_end_byte]
            text = body.decode("utf-8")
            nonblank = sum(bool(line.strip()) for line in text.splitlines())
            normalized = "\0".join(item.statement_signatures)
            candidates.append(
                (
                    result,
                    item,
                    subject_map[(result.unit.module_name, item.qualified_name)],
                    hashlib.sha256(body.replace(b"\r\n", b"\n")).hexdigest(),
                    hashlib.sha256(normalized.encode()).hexdigest(),
                    nonblank,
                    len(re.findall(r"[A-Za-z_$][\w$]*|\S", text)),
                )
            )
    fragments: list[CloneFragment] = []
    clusters: list[CloneCluster] = []
    pair_clusters: dict[tuple[str, str], list[str]] = defaultdict(list)
    omissions = 0
    for kind, fingerprint_index, minimum in (
        (CloneKind.EXACT_TEXT, 3, context.config.clone_thresholds.exact_min_lines),
        (CloneKind.NORMALIZED_AST, 4, context.config.clone_thresholds.near_clone_min_statements),
    ):
        groups: dict[
            str, list[tuple[FrontendResult, object, AnalysisSubject, str, str, int, int]]
        ] = defaultdict(list)
        for candidate in candidates:
            if candidate[5] >= minimum:
                groups[candidate[fingerprint_index]].append(candidate)
        for fingerprint, members in sorted(groups.items()):
            if len(members) < 2:
                continue
            if len(clusters) >= context.config.phase_three_limits.max_clusters:
                omissions += 1
                continue
            members = sorted(members, key=lambda item: item[2].subject_id)
            member_limit = context.config.phase_three_limits.max_cluster_members
            if len(members) > member_limit:
                omissions += len(members) - member_limit
                members = members[:member_limit]
            member_fragments = []
            for _result, function, subject, _exact, _normalized, nonblank, tokens in members:
                fragment = CloneFragment(
                    fragment_id=_stable_id("clone-fragment", kind.value, subject.subject_id),
                    kind=kind,
                    subject_id=subject.subject_id,
                    fingerprint_sha256=fingerprint,
                    structural_signature=f"javascript:{kind.value}:v1",
                    nonblank_lines=nonblank,
                    token_count=tokens,
                    ast_node_count=len(function.statement_signatures),
                    statement_count=len(function.statement_signatures),
                    evidence=subject.evidence,
                    limitations=("JavaScript normalization is syntax-only",),
                    language=SourceLanguage.JAVASCRIPT,
                )
                fragments.append(fragment)
                member_fragments.append(fragment)
            subject_ids = tuple(sorted(item.subject_id for item in member_fragments))
            cluster_id = _stable_id("clone-cluster", kind.value, fingerprint, *subject_ids)
            clusters.append(
                CloneCluster(
                    cluster_id=cluster_id,
                    kind=kind,
                    fragment_ids=tuple(sorted(item.fragment_id for item in member_fragments)),
                    subject_ids=subject_ids,
                    evidence=tuple(item.evidence[0] for item in member_fragments),
                    confidence=Confidence.EXACT,
                    completeness=Completeness.COMPLETE,
                    limitations=(
                        "JavaScript clone evidence does not establish semantic equivalence",
                    ),
                )
            )
            for left_offset, left in enumerate(subject_ids):
                for right in subject_ids[left_offset + 1 :]:
                    pair_clusters[(left, right)].append(cluster_id)
    return (
        tuple(sorted(fragments, key=lambda item: item.fragment_id)),
        tuple(sorted(clusters, key=lambda item: item.cluster_id)),
        {key: tuple(sorted(value)) for key, value in pair_clusters.items()},
        omissions,
    )


def _javascript_commonalities(
    duplicates: tuple[DuplicateFunctionPair, ...],
    profiles: tuple[FunctionFeatureProfile, ...],
    clusters: tuple[CloneCluster, ...],
) -> tuple[FunctionCommonality, ...]:
    by_subject = {item.subject_id: item for item in profiles}
    cluster_by_id = {item.cluster_id: item for item in clusters}
    records = []
    dimensions = (
        (CommonalityDimensionKind.CALLEES, "callees"),
        (CommonalityDimensionKind.ATTRIBUTES_GLOBALS, "attributes_globals"),
        (CommonalityDimensionKind.EXCEPTIONS, "exceptions"),
        (CommonalityDimensionKind.SIDE_EFFECTS, "side_effects"),
        (CommonalityDimensionKind.CONTROL_FLOW, "control_flow"),
        (CommonalityDimensionKind.DICTIONARY_SCHEMA, "dictionary_schema_operations"),
    )
    for duplicate in duplicates:
        left = by_subject[duplicate.left_subject_id]
        right = by_subject[duplicate.right_subject_id]
        shared = []
        for kind, field in dimensions:
            left_values = set(getattr(left, field))
            right_values = set(getattr(right, field))
            common = tuple(sorted(left_values & right_values))
            if common:
                shared.append(
                    CommonalityDimension(
                        kind=kind,
                        shared_identities=common,
                        left_total=len(left_values),
                        right_total=len(right_values),
                    )
                )
        evidence = (
            tuple(cluster_by_id[item].evidence[0] for item in duplicate.clone_cluster_ids)
            or duplicate.evidence
        )
        records.append(
            FunctionCommonality(
                commonality_id=_stable_id(
                    "commonality", duplicate.left_subject_id, duplicate.right_subject_id
                ),
                left_subject_id=duplicate.left_subject_id,
                right_subject_id=duplicate.right_subject_id,
                dimensions=tuple(sorted(shared, key=lambda item: item.kind.value)),
                clone_cluster_ids=duplicate.clone_cluster_ids,
                evidence=duplicate.evidence if len(evidence) < 2 else evidence,
                confidence=Confidence.RESOLVED,
                completeness=Completeness.COMPLETE,
                limitations=("JavaScript commonality is syntax-only",),
            )
        )
    return tuple(sorted(records, key=lambda item: item.commonality_id))


def _javascript_test_impact(
    context: AnalysisContext,
    source_results: tuple[FrontendResult, ...],
    subjects: tuple[AnalysisSubject, ...],
) -> tuple[tuple[TestModuleRecord, ...], tuple[TestRelationshipRecord, ...]]:
    known_results = {item.unit.logical_path: item for item in source_results}
    paths: set[Path] = set()
    for root_name in context.config.test_roots:
        root = context.source.physical_root / root_name
        if root.is_dir():
            paths.update(
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix in JAVASCRIPT_SUFFIXES and not path.is_symlink()
            )
    paths.update(
        path
        for path in iter_javascript_files(context)
        if ".test." in path.name or ".spec." in path.name
    )
    if not paths:
        return (), ()

    frontend = frontend_for(SourceLanguage.JAVASCRIPT)
    parsed: list[FrontendResult] = []
    for path in sorted(paths, key=lambda item: logical_path(context, item)):
        relative = logical_path(context, path)
        existing = known_results.get(relative)
        if existing is not None:
            parsed.append(existing)
            continue
        data = path.read_bytes()
        unit = SourceUnit(
            logical_path=relative,
            module_name=relative,
            source=data.decode("utf-8"),
            source_bytes=data,
            source_sha256=hashlib.sha256(data).hexdigest(),
            language=SourceLanguage.JAVASCRIPT,
        )
        parsed.append(
            frontend.analyze(
                unit,
                FrontendContext(
                    context.config,
                    context.config.identity(),
                    _configured_module_mode(context, path, relative),
                ),
            )
        )

    modules = tuple(
        sorted(
            (
                TestModuleRecord(
                    test_module_id=_stable_id(
                        "test-module",
                        SourceLanguage.JAVASCRIPT.value,
                        item.unit.logical_path,
                    ),
                    module=item.unit.module_name,
                    path=item.unit.logical_path,
                    source_sha256=item.unit.source_sha256,
                    framework="javascript-generic",
                    language=SourceLanguage.JAVASCRIPT,
                )
                for item in parsed
            ),
            key=lambda item: item.test_module_id,
        )
    )
    module_ids = {item.path: item.test_module_id for item in modules}
    known_subjects = {item.logical_name.rsplit(":", 1)[-1]: item for item in subjects}
    relationships: list[TestRelationshipRecord] = []
    known_application_modules = {item.unit.module_name for item in source_results}
    for result in parsed:
        source_id = module_ids[result.unit.logical_path]
        for item in result.imports:
            target, local = _resolve_specifier(
                result.unit.module_name,
                item.specifier,
                known_application_modules,
            )
            if not local and not item.dynamic and not item.specifier.startswith("."):
                continue
            relationships.append(
                TestRelationshipRecord(
                    relationship_id=_stable_id(
                        "test-relationship",
                        source_id,
                        "module-import",
                        item.specifier,
                        item.start_line,
                        item.start_column,
                    ),
                    source_test_module_id=source_id,
                    kind=TestRelationshipKind.MODULE_IMPORT,
                    target_module=target if local else None,
                    target_text=None if local else item.specifier,
                    resolved=local,
                    evidence=(
                        _evidence(
                            result,
                            item.start_line,
                            item.start_column,
                            item.end_line,
                            item.end_column,
                            "generic JavaScript test module import",
                        ),
                    ),
                    limitations=(
                        "module-level import is likely test impact, not behavioral coverage",
                    ),
                )
            )
        for function in result.functions:
            for callee in function.callees:
                target = known_subjects.get(callee.rsplit(".", 1)[-1])
                if target is None:
                    continue
                relationships.append(
                    TestRelationshipRecord(
                        relationship_id=_stable_id(
                            "test-relationship",
                            source_id,
                            "direct-symbol",
                            target.subject_id,
                            function.start_line,
                        ),
                        source_test_module_id=source_id,
                        kind=TestRelationshipKind.DIRECT_SYMBOL,
                        target_subject_id=target.subject_id,
                        resolved=True,
                        evidence=(
                            _evidence(
                                result,
                                function.start_line,
                                function.start_column,
                                function.end_line,
                                function.end_column,
                                "generic JavaScript direct symbol reference",
                            ),
                        ),
                        limitations=(
                            "static direct reference indicates likely impact, not execution",
                        ),
                    )
                )
    return modules, tuple(sorted(relationships, key=lambda item: item.relationship_id))
