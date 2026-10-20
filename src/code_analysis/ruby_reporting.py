"""Ruby/Prism source discovery and authoritative report assembly."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from code_analysis.config import LanguageSelection
from code_analysis.context import AnalysisContext
from code_analysis.graph import canonical_cycles, strongly_connected_components
from code_analysis.languages import FrontendContext, FrontendResult, SourceUnit, frontend_for
from code_analysis.models import (
    AvailabilityState,
    Completeness,
    ComplexityObservation,
    Confidence,
    Diagnostic,
    DiagnosticLevel,
    Evidence,
    ImportCycle,
    ImportEdge,
    Module,
    ProviderResult,
    ProviderState,
    SourceLanguage,
    SourceParseState,
    SourceSpan,
    StrongComponent,
    Symbol,
)
from code_analysis.project import (
    iter_ruby_files,
    logical_path,
    ruby_module_name,
    selected_languages,
)
from code_analysis.report_models import (
    AnalysisReport,
    AnalysisSubject,
    CanonicalOwnerState,
    CanonicalOwnerSuggestion,
    CloneCluster,
    CloneFragment,
    CloneKind,
    DuplicateFunctionPair,
    FunctionFeatureProfile,
    LanguageFrontendCoverage,
    SectionState,
    SourceFileRecord,
    SubjectKind,
    SummaryCounts,
)


def _stable_id(prefix: str, *parts: str) -> str:
    return prefix + "-" + hashlib.sha256("\0".join(parts).encode()).hexdigest()[:20]


def _evidence(result: FrontendResult, symbol, observation: str) -> Evidence:
    return Evidence(
        span=SourceSpan(
            path=result.unit.logical_path,
            start_line=symbol.start_line,
            start_column=symbol.start_column,
            end_line=symbol.end_line,
            end_column=symbol.end_column,
            language=SourceLanguage.RUBY,
        ),
        provider="ruby-prism",
        observation=observation,
        confidence=Confidence.EXACT,
        source_sha256=result.unit.source_sha256,
        language=SourceLanguage.RUBY,
    )


def _source(result: FrontendResult) -> SourceFileRecord:
    lines = result.unit.source_bytes.splitlines()
    return SourceFileRecord(
        source_file_id=_stable_id(
            "source-file", SourceLanguage.RUBY.value, ".", result.unit.logical_path
        ),
        logical_path=result.unit.logical_path,
        logical_root=".",
        source_sha256=result.unit.source_sha256,
        language=SourceLanguage.RUBY,
        parse_state=(
            SourceParseState.COMPLETE
            if result.completeness is Completeness.COMPLETE
            else SourceParseState.PARTIAL
        ),
        module_name=result.unit.module_name,
        physical_loc=len(lines),
        nonblank_loc=sum(bool(line.strip()) for line in lines),
    )


def _module(result: FrontendResult) -> Module:
    lines = result.unit.source_bytes.splitlines()
    return Module(
        name=result.unit.module_name,
        path=result.unit.logical_path,
        source_sha256=result.unit.source_sha256,
        line_count=len(lines),
        nonblank_line_count=sum(bool(line.strip()) for line in lines),
        symbols=tuple(
            Symbol(
                qualified_name=item.qualified_name,
                name=item.name,
                kind=item.kind,  # type: ignore[arg-type]
                span=SourceSpan(
                    path=result.unit.logical_path,
                    start_line=item.start_line,
                    start_column=item.start_column,
                    end_line=item.end_line,
                    end_column=item.end_column,
                    language=SourceLanguage.RUBY,
                ),
                parameters=item.parameters,
                returns=item.returns,
                complexity=(
                    ComplexityObservation(**asdict(item.complexity), language=SourceLanguage.RUBY)
                    if item.complexity
                    else None
                ),
                language=SourceLanguage.RUBY,
            )
            for item in result.symbols
        ),
        language=SourceLanguage.RUBY,
        parse_state=(
            SourceParseState.COMPLETE
            if result.completeness is Completeness.COMPLETE
            else SourceParseState.PARTIAL
        ),
    )


def _subjects(result: FrontendResult) -> tuple[AnalysisSubject, ...]:
    module_span = type(
        "ModuleSpan",
        (),
        {
            "start_line": 1,
            "start_column": 0,
            "end_line": max(1, len(result.unit.source_bytes.splitlines())),
            "end_column": 0,
        },
    )()
    records = [
        AnalysisSubject(
            subject_id=_stable_id(
                "subject", "ruby", ".", result.unit.logical_path, "module", result.unit.module_name
            ),
            kind=SubjectKind.MODULE,
            logical_name=result.unit.module_name,
            module=result.unit.module_name,
            evidence=(_evidence(result, module_span, "Ruby source module"),),
            language=SourceLanguage.RUBY,
        )
    ]
    records.extend(
        AnalysisSubject(
            subject_id=_stable_id(
                "subject",
                "ruby",
                ".",
                result.unit.logical_path,
                item.kind,
                item.qualified_name,
            ),
            kind=SubjectKind.SYMBOL,
            logical_name=f"{result.unit.module_name}:{item.qualified_name}",
            module=result.unit.module_name,
            evidence=(_evidence(result, item, f"Ruby {item.kind} declaration"),),
            language=SourceLanguage.RUBY,
        )
        for item in result.symbols
    )
    return tuple(records)


def _profiles(
    results: tuple[FrontendResult, ...], subjects: tuple[AnalysisSubject, ...]
) -> tuple[FunctionFeatureProfile, ...]:
    by_name = {item.logical_name: item for item in subjects}
    records = []
    for result in results:
        for item in result.functions:
            subject = by_name[f"{result.unit.module_name}:{item.qualified_name}"]
            records.append(
                FunctionFeatureProfile(
                    profile_id=_stable_id("function-profile", subject.subject_id),
                    subject_id=subject.subject_id,
                    callees=item.callees,
                    signature_shapes=(f"parameters:{len(item.parameters)}",),
                    exceptions=item.exceptions,
                    side_effects=item.side_effects,
                    control_flow=tuple(sorted(set(item.control_events))),
                    dictionary_schema_operations=item.schema_operations,
                    evidence=subject.evidence,
                    limitations=(
                        "Ruby resolution is lexical; metaprogramming and runtime mixins "
                        "remain unresolved.",
                    ),
                    language=SourceLanguage.RUBY,
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
    tuple[DuplicateFunctionPair, ...],
    int,
]:
    by_name = {item.logical_name: item for item in subjects}
    groups: dict[str, list[tuple[object, AnalysisSubject]]] = {}
    minimum = context.config.clone_thresholds.near_clone_min_statements
    for result in results:
        for function in result.functions:
            if len(function.detailed_statement_signatures) < minimum:
                continue
            signature = json.dumps(function.detailed_statement_signatures, separators=(",", ":"))
            groups.setdefault(signature, []).append(
                (
                    function,
                    by_name[f"{result.unit.module_name}:{function.qualified_name}"],
                )
            )
    fragments: list[CloneFragment] = []
    clusters: list[CloneCluster] = []
    duplicates: list[DuplicateFunctionPair] = []
    omissions = 0
    pair_count = 0
    for signature, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        fingerprint = hashlib.sha256(signature.encode()).hexdigest()
        member_fragments = []
        for function, subject in sorted(members, key=lambda item: item[1].subject_id):
            fragment = CloneFragment(
                fragment_id=_stable_id("clone-fragment", "ruby", subject.subject_id, fingerprint),
                kind=CloneKind.NORMALIZED_AST,
                subject_id=subject.subject_id,
                fingerprint_sha256=fingerprint,
                structural_signature="ruby-prism-call-sequence:v1",
                nonblank_lines=function.end_line - function.start_line + 1,
                token_count=len(function.detailed_statement_signatures),
                ast_node_count=len(function.detailed_statement_signatures),
                statement_count=len(function.detailed_statement_signatures),
                evidence=subject.evidence,
                limitations=(
                    "Ruby normalized clone evidence is syntax-only and call-sequence bounded.",
                ),
                language=SourceLanguage.RUBY,
            )
            fragments.append(fragment)
            member_fragments.append(fragment)
        cluster_id = _stable_id(
            "clone-cluster",
            "ruby",
            fingerprint,
            *(item.subject_id for item in member_fragments),
        )
        clusters.append(
            CloneCluster(
                cluster_id=cluster_id,
                kind=CloneKind.NORMALIZED_AST,
                fragment_ids=tuple(sorted(item.fragment_id for item in member_fragments)),
                subject_ids=tuple(sorted(item.subject_id for item in member_fragments)),
                evidence=tuple(item.evidence[0] for item in member_fragments),
                confidence=Confidence.RESOLVED,
                completeness=Completeness.COMPLETE,
                limitations=("Ruby clone evidence does not prove semantic equivalence.",),
            )
        )
        ordered = sorted(members, key=lambda item: item[1].subject_id)
        for left_position, (left, left_subject) in enumerate(ordered):
            for right, right_subject in ordered[left_position + 1 :]:
                if pair_count >= context.config.phase_three_limits.max_candidate_pairs:
                    omissions += 1
                    continue
                pair_count += 1
                duplicates.append(
                    DuplicateFunctionPair(
                        duplicate_id=_stable_id(
                            "duplicate",
                            "ruby",
                            left_subject.subject_id,
                            right_subject.subject_id,
                        ),
                        language=SourceLanguage.RUBY,
                        left_subject_id=left_subject.subject_id,
                        right_subject_id=right_subject.subject_id,
                        clone_cluster_ids=(cluster_id,),
                        matched_statements=len(left.statement_signatures),
                        left_statement_total=len(left.statement_signatures),
                        right_statement_total=len(right.statement_signatures),
                        insertions=0,
                        deletions=0,
                        substitutions=0,
                        canonical_owner=CanonicalOwnerSuggestion(
                            state=CanonicalOwnerState.UNRESOLVED,
                            reason="Ruby import and caller direction does not prove one owner.",
                        ),
                        evidence=(left_subject.evidence[0], right_subject.evidence[0]),
                        limitations=("Ruby duplicate evidence is syntax-only.",),
                    )
                )
    return (
        tuple(sorted(fragments, key=lambda item: item.fragment_id)),
        tuple(sorted(clusters, key=lambda item: item.cluster_id)),
        tuple(sorted(duplicates, key=lambda item: item.duplicate_id)),
        omissions,
    )


def _import_edges(results: tuple[FrontendResult, ...]) -> tuple[ImportEdge, ...]:
    known = {item.unit.module_name for item in results}
    edges = set()
    for result in results:
        parent = result.unit.module_name.rsplit("/", 1)[0] if "/" in result.unit.module_name else ""
        for item in result.imports:
            specifier = item.specifier
            candidate = (
                f"{parent}/{specifier}.rb"
                if item.kind == "require_relative" and parent
                else f"{specifier}.rb"
            )
            if candidate in known:
                edges.add((result.unit.module_name, candidate))
    return tuple(
        ImportEdge(
            source=source,
            target=target,
            local=True,
            kind="runtime",
            source_language=SourceLanguage.RUBY,
            target_language=SourceLanguage.RUBY,
        )
        for source, target in sorted(edges)
    )


def integrate_ruby_sources(context: AnalysisContext, report: AnalysisReport) -> AnalysisReport:
    if LanguageSelection.RUBY not in selected_languages(context.config):
        return report
    paths = iter_ruby_files(context)
    if not paths:
        return report
    frontend = frontend_for(SourceLanguage.RUBY)
    frontend_context = FrontendContext(context.config, context.config.identity())
    availability = frontend.availability(frontend_context)
    results: list[FrontendResult] = []
    diagnostics: list[Diagnostic] = []
    for path in paths:
        data = path.read_bytes()
        relative = logical_path(context, path)
        unit = SourceUnit(
            logical_path=relative,
            module_name=ruby_module_name(context, path),
            source=data.decode("utf-8"),
            source_bytes=data,
            source_sha256=hashlib.sha256(data).hexdigest(),
            language=SourceLanguage.RUBY,
        )
        if availability.state is AvailabilityState.UNAVAILABLE:
            result = frontend.analyze(unit, frontend_context)
        else:
            result = frontend.analyze(unit, frontend_context)
        if hashlib.sha256(path.read_bytes()).hexdigest() != unit.source_sha256:
            diagnostics.append(
                Diagnostic(
                    code="ruby-source-changed",
                    level=DiagnosticLevel.ERROR,
                    message="Ruby source changed during analysis",
                    path=relative,
                    provider="ruby-prism",
                    language=SourceLanguage.RUBY,
                )
            )
        diagnostics.extend(result.diagnostics)
        results.append(result)
    sources = tuple(_source(item) for item in results)
    modules = tuple(_module(item) for item in results)
    subjects = tuple(
        sorted(
            (subject for result in results for subject in _subjects(result)),
            key=lambda item: item.subject_id,
        )
    )
    profiles = _profiles(tuple(results), subjects)
    fragments, clusters, duplicates, clone_omissions = _clone_records(
        context, tuple(results), subjects
    )
    edges = _import_edges(tuple(results))
    all_edges = tuple(
        sorted(
            (*report.import_edges, *edges), key=lambda item: (item.source, item.target, item.kind)
        )
    )
    module_names = {item.name for item in (*report.modules, *modules)}
    local_pairs = {(item.source, item.target) for item in all_edges if item.local}
    components = strongly_connected_components(module_names, local_pairs)
    cycles = canonical_cycles(module_names, local_pairs)
    failed = sum(item.completeness is not Completeness.COMPLETE for item in results)
    if clone_omissions:
        diagnostics.append(
            Diagnostic(
                code="ruby-clone-truncated",
                level=DiagnosticLevel.ERROR,
                message=f"Ruby clone bounds omitted {clone_omissions} duplicate pairs",
                provider="ruby-prism",
                language=SourceLanguage.RUBY,
            )
        )
    coverage = LanguageFrontendCoverage(
        language=SourceLanguage.RUBY,
        frontend_id="ruby-prism",
        state=SectionState.PARTIAL if failed else SectionState.COMPLETE,
        discovered=len(paths),
        complete=len(paths) - failed,
        partial=failed,
        failed=0,
        unavailable=(len(paths) if availability.state is AvailabilityState.UNAVAILABLE else 0),
        diagnostic_codes=tuple(sorted({item.code for item in diagnostics})),
        capabilities=frontend.capabilities,
    )
    provider_state = (
        ProviderState.UNAVAILABLE
        if availability.state is AvailabilityState.UNAVAILABLE
        else ProviderState.PARTIAL if failed else ProviderState.SUCCESS
    )
    merged_sources = tuple(
        sorted((*report.source_files, *sources), key=lambda item: item.source_file_id)
    )
    merged_modules = tuple(
        sorted((*report.modules, *modules), key=lambda item: (item.name, item.path))
    )
    summary = SummaryCounts.model_validate(
        {
            **report.summary.model_dump(),
            "source_files": len(merged_sources),
            "discovered_files": report.summary.discovered_files + len(paths),
            "parsed_files": report.summary.parsed_files + len(paths) - failed,
            "failed_files": report.summary.failed_files + failed,
            "modules": len(merged_modules),
            "classes": report.summary.classes
            + sum(
                symbol.kind in {"class", "module"}
                for module in modules
                for symbol in module.symbols
            ),
            "methods": report.summary.methods
            + sum(symbol.kind == "method" for module in modules for symbol in module.symbols),
            "import_edges": len(all_edges),
            "strongly_connected_components": len([item for item in components if len(item) > 1]),
            "cycles": len(cycles),
            "subjects": report.summary.subjects + len(subjects),
            "function_profiles": report.summary.function_profiles + len(profiles),
            "clone_fragments": report.summary.clone_fragments + len(fragments),
            "clone_clusters": report.summary.clone_clusters + len(clusters),
            "duplicate_functions": report.summary.duplicate_functions + len(duplicates),
        }
    )
    payload = {
        **report.model_dump(),
        "source_files": merged_sources,
        "target": report.target.model_copy(
            update={
                "languages": tuple(
                    sorted({item.language for item in merged_sources}, key=lambda item: item.value)
                )
            }
        ),
        "language_frontends": tuple(
            sorted((*report.language_frontends, coverage), key=lambda item: item.language.value)
        ),
        "parser_provenance": tuple(
            sorted(
                (*report.parser_provenance, *(item.provenance for item in results)),
                key=lambda item: (item.language.value, item.logical_path),
            )
        ),
        "modules": merged_modules,
        "complexity_observations": (
            *report.complexity_observations,
            *(
                symbol.complexity
                for module in modules
                for symbol in module.symbols
                if symbol.complexity
            ),
        ),
        "import_edges": all_edges,
        "strongly_connected_components": tuple(
            StrongComponent(modules=item) for item in components if len(item) > 1
        ),
        "cycles": tuple(ImportCycle(modules=item) for item in cycles),
        "subjects": tuple(sorted((*report.subjects, *subjects), key=lambda item: item.subject_id)),
        "function_profiles": tuple(
            sorted((*report.function_profiles, *profiles), key=lambda item: item.profile_id)
        ),
        "clone_fragments": tuple(
            sorted((*report.clone_fragments, *fragments), key=lambda item: item.fragment_id)
        ),
        "clone_clusters": tuple(
            sorted((*report.clone_clusters, *clusters), key=lambda item: item.cluster_id)
        ),
        "duplicate_functions": tuple(
            sorted((*report.duplicate_functions, *duplicates), key=lambda item: item.duplicate_id)
        ),
        "diagnostics": tuple(
            sorted(
                (*report.diagnostics, *diagnostics), key=lambda item: (item.path or "", item.code)
            )
        ),
        "providers": (
            *report.providers,
            ProviderResult(
                provider_id="ruby-prism",
                state=provider_state,
                executable="ruby",
                version=availability.detail,
                duration_ms=0,
                diagnostics=tuple(diagnostics),
                observations=("ruby-module-symbol-call-import-hash-effect-inventory",),
                completeness=Completeness.PARTIAL if failed else Completeness.COMPLETE,
            ),
        ),
        "completeness": (
            Completeness.PARTIAL
            if failed or report.completeness is Completeness.PARTIAL
            else report.completeness
        ),
        "limitations": tuple(
            sorted(
                {
                    *report.limitations,
                    "Ruby analysis is syntax-only; metaprogramming, runtime mixins, dynamic "
                    "constants, and generated methods remain unresolved.",
                }
            )
        ),
        "summary": summary,
    }
    affected = {
        "r1-static-analysis",
        "clones",
        "function-commonality",
        "state-side-effects",
    }
    payload["section_coverage"] = tuple(
        (
            section.model_copy(
                update={
                    "languages": tuple(
                        sorted(
                            {*section.languages, SourceLanguage.RUBY}, key=lambda item: item.value
                        )
                    ),
                    "parsed": section.parsed
                    + (
                        len(paths) - failed
                        if section.section == "r1-static-analysis"
                        else (
                            len(fragments)
                            if section.section == "clones"
                            else len(profiles) if section.section == "function-commonality" else 0
                        )
                    ),
                    "failed": section.failed
                    + (failed if section.section == "r1-static-analysis" else 0),
                    "state": (
                        SectionState.TRUNCATED
                        if clone_omissions and section.section == "clones"
                        else SectionState.PARTIAL if failed else section.state
                    ),
                    "truncated": section.truncated
                    + (clone_omissions if section.section == "clones" else 0),
                    "diagnostic_codes": tuple(
                        sorted({*section.diagnostic_codes, *(item.code for item in diagnostics)})
                    ),
                }
            )
            if section.section in affected
            else section
        )
        for section in report.section_coverage
    )
    payload["run_identity_sha256"] = hashlib.sha256(
        json.dumps(
            {
                "prior": report.run_identity_sha256,
                "ruby": tuple((item.logical_path, item.source_sha256) for item in sources),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    admission = report.signal_quality.commonality_admission
    if admission is not None:
        payload["signal_quality"] = report.signal_quality.model_copy(
            update={
                "commonality_admission": admission.model_copy(
                    update={"profile_count": admission.profile_count + len(profiles)}
                )
            }
        )
    return AnalysisReport.model_validate(payload)
