from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

from code_analysis import __version__
from code_analysis.analysis.contracts import (
    ClassLineageFact,
    ContractDeclarationFact,
    ContractMemberFact,
    ContractUseFact,
    DefaultFact,
    InternalDefaultKind,
    TypeFact,
    discover_class_lineages,
    discover_contract_declarations,
    discover_contract_uses,
    merge_inherited_contract_members,
    resolve_contract_kinds,
)
from code_analysis.cache import ContentCache
from code_analysis.candidate_qualification import integrate_candidate_qualification
from code_analysis.clone_reporting import integrate_clone_report
from code_analysis.cohesion_reporting import integrate_cohesion_report
from code_analysis.commonality_reporting import integrate_commonality_report
from code_analysis.config import AnalysisConfig, ContractBoundaryRule
from code_analysis.context import AnalysisContext
from code_analysis.dictionary_reporting import integrate_dictionary_report
from code_analysis.finding_suppressions import apply_finding_suppressions
from code_analysis.graph import canonical_cycles, strongly_connected_components
from code_analysis.interface_reporting import integrate_interface_report
from code_analysis.javascript_reporting import (
    attach_python_source_records,
    finalize_language_sections,
    integrate_language_sources,
)
from code_analysis.languages.python.imports import ResolvedEdge, resolve_edges
from code_analysis.languages.python.native_ast import (
    InternalComplexity,
    InternalSymbol,
    ParsedModule,
    exceeds_thresholds,
    is_orchestration,
    parse_module,
)
from code_analysis.models import (
    CommandIdentity,
    Completeness,
    ComplexityObservation,
    Confidence,
    ConfigurationIdentity,
    Diagnostic,
    DiagnosticLevel,
    EnvironmentProvenance,
    Evidence,
    ImportCycle,
    ImportEdge,
    Module,
    ProviderResult,
    ProviderState,
    SourceParseState,
    SourceSpan,
    StaticAnalysisResult,
    StaticFinding,
    StaticSummaryCounts,
    StrongComponent,
    Symbol,
    TargetIdentity,
    ThresholdIdentity,
)
from code_analysis.project import iter_python_files, logical_path, module_name
from code_analysis.proposal_reconciliation import reconcile_proposal_evidence
from code_analysis.report_models import (
    SCHEMA_NAME,
    AnalysisReport,
    AnalysisSubject,
    BoundaryClass,
    BoundaryDirection,
    ContractBoundary,
    ContractDefinition,
    ContractKind,
    ContractMember,
    DefaultKind,
    DefaultValue,
    FieldRequiredness,
    Finding,
    JsonLiteralValue,
    RequirednessKind,
    SectionCoverage,
    SectionState,
    SubjectKind,
    SummaryCounts,
    TypeExpression,
    TypeKind,
)
from code_analysis.ruby_reporting import integrate_ruby_sources
from code_analysis.state_reporting import integrate_state_effect_report
from code_analysis.static_call_reporting import integrate_static_call_report
from code_analysis.test_impact_reporting import integrate_test_impact

LIMITATIONS = (
    "Phase 1 uses Python AST evidence and does not perform dictionary-shape or "
    "data-contract inference.",
    "Dynamic imports, reflection, generated modules, and runtime-only edges are unresolved.",
    "Complexity and orchestration classifications are threshold heuristics, not refactor approval.",
    "External graph providers are inventoried separately and are not reconciled in Phase 1 scans.",
)

CONTRACT_INVENTORY_LIMITATIONS = (
    "Contract inventory is syntax-only and does not resolve producer/consumer call flow.",
    "Serialization evidence resolves exact declaration kinds, local annotations, and direct "
    "straight-line aliases; control-flow aliases and runtime types remain unresolved.",
    "Dictionary-shape, validation, lifecycle, and candidate analysis remain disabled.",
)


@dataclass(frozen=True, slots=True)
class _ClassifiedContractUse:
    use: ContractUseFact
    relative: str
    source_sha256: str
    boundary_class: BoundaryClass
    confidence: Confidence
    limitations: tuple[str, ...] = ()


def _scan_static(context: AnalysisContext) -> StaticAnalysisResult:
    paths = iter_python_files(context)
    parsed: list[ParsedModule] = []
    modules_by_name: dict[str, ParsedModule] = {}
    diagnostics: list[Diagnostic] = []
    failed_modules: list[Module] = []

    for path in paths:
        relative = logical_path(context, path)
        name = module_name(context, path)
        try:
            module = parse_module(path, relative, name)
        except SyntaxError as exc:
            failed_modules.append(_failed_module(path, relative, name))
            diagnostics.append(
                Diagnostic(
                    code="source-syntax-error",
                    level=DiagnosticLevel.ERROR,
                    message=f"syntax error at line {exc.lineno or 1}: {exc.msg}",
                    path=relative,
                    provider="native-ast",
                )
            )
            continue
        except (OSError, UnicodeError) as exc:
            failed_modules.append(_failed_module(path, relative, name))
            diagnostics.append(
                Diagnostic(
                    code="source-read-error",
                    level=DiagnosticLevel.ERROR,
                    message=f"source could not be read: {type(exc).__name__}",
                    path=relative,
                    provider="native-ast",
                )
            )
            continue
        if name in modules_by_name:
            diagnostics.append(
                Diagnostic(
                    code="ambiguous-module-name",
                    level=DiagnosticLevel.ERROR,
                    message=f"duplicate logical module name: {name}",
                    path=relative,
                    provider="native-ast",
                )
            )
            failed_modules.append(_failed_module(path, relative, name))
            continue
        modules_by_name[name] = module
        parsed.append(module)

    parsed_tuple = tuple(sorted(parsed, key=lambda item: (item.name, item.logical_path)))
    edges = resolve_edges(parsed_tuple, only_local=context.config.only_local_imports)
    local_pairs = tuple((edge.source, edge.target) for edge in edges if edge.local)
    component_values = strongly_connected_components(modules_by_name, local_pairs)
    cyclic_components = tuple(
        component
        for component in component_values
        if len(component) > 1 or (component[0], component[0]) in local_pairs
    )
    cycle_values = canonical_cycles(modules_by_name, local_pairs)

    fan_in: dict[str, set[str]] = {name: set() for name in modules_by_name}
    fan_out: dict[str, set[str]] = {name: set() for name in modules_by_name}
    for edge in edges:
        fan_out.setdefault(edge.source, set()).add(edge.target)
        if edge.local:
            fan_in.setdefault(edge.target, set()).add(edge.source)

    report_modules = [
        _public_module(item, fan_in.get(item.name, set()), fan_out.get(item.name, set()))
        for item in parsed_tuple
    ]
    report_modules.extend(failed_modules)
    report_modules.sort(key=lambda item: (item.name, item.path))

    findings = _complexity_findings(parsed_tuple, context.config)
    findings.extend(_cycle_findings(cycle_values, modules_by_name))
    findings.sort(key=lambda item: item.finding_id)
    completeness = Completeness.PARTIAL if diagnostics else Completeness.COMPLETE
    limitations = list(LIMITATIONS)
    if context.config.max_findings is not None and len(findings) > context.config.max_findings:
        original = len(findings)
        findings = findings[: context.config.max_findings]
        diagnostics.append(
            Diagnostic(
                code="findings-truncated",
                level=DiagnosticLevel.WARNING,
                message=f"finding limit retained {len(findings)} of {original} findings",
                provider="native-ast",
            )
        )
        limitations.append("The configured finding limit truncated report findings.")
        completeness = Completeness.PARTIAL

    provider = ProviderResult(
        provider_id="native-ast",
        state=(
            ProviderState.PARTIAL if completeness is Completeness.PARTIAL else ProviderState.SUCCESS
        ),
        executable="python",
        version=platform.python_version(),
        duration_ms=0,
        diagnostics=tuple(diagnostics),
        observations=("module-index", "symbol-index", "complexity", "import-graph"),
        completeness=completeness,
    )
    complexity = tuple(
        _public_complexity(symbol.complexity)
        for module in parsed_tuple
        for symbol in module.symbols
        if symbol.complexity is not None
    )
    counts = StaticSummaryCounts(
        discovered_files=len(paths),
        parsed_files=len(parsed_tuple),
        failed_files=len(failed_modules),
        modules=len(report_modules),
        classes=sum(
            symbol.kind in {"class", "nested_class"}
            for module in parsed_tuple
            for symbol in module.symbols
        ),
        functions=sum(
            symbol.kind in {"function", "nested_function"}
            for module in parsed_tuple
            for symbol in module.symbols
        ),
        methods=sum(
            symbol.kind == "method" for module in parsed_tuple for symbol in module.symbols
        ),
        import_edges=len(edges),
        strongly_connected_components=len(cyclic_components),
        cycles=len(cycle_values),
        findings=len(findings),
    )
    thresholds = context.config.thresholds.model_dump()
    return StaticAnalysisResult(
        tool_version=__version__,
        command=CommandIdentity(
            name="scan",
            arguments=(
                ".",
                "--strictness",
                context.config.strictness.value,
                "--only-local-imports" if context.config.only_local_imports else "--all-imports",
            ),
        ),
        target=TargetIdentity(
            logical_root=context.source.logical_root,
            source_roots=context.source.logical_source_roots,
            target_kind="file" if context.target.is_file() else "directory",
        ),
        environment=EnvironmentProvenance(
            python_implementation=sys.implementation.name,
            python_version=platform.python_version(),
            executable_identity="python",
        ),
        configuration=ConfigurationIdentity(
            digest_sha256=context.config.identity(),
            strictness=context.config.strictness.value,
            thresholds=ThresholdIdentity(**thresholds),
            only_local_imports=context.config.only_local_imports,
            max_findings=context.config.max_findings,
            symbol_filter=context.config.symbol_filter,
        ),
        modules=tuple(report_modules),
        complexity_observations=complexity,
        import_edges=tuple(_public_edge(edge) for edge in edges),
        strongly_connected_components=tuple(
            StrongComponent(modules=component) for component in cyclic_components
        ),
        cycles=tuple(ImportCycle(modules=cycle) for cycle in cycle_values),
        findings=tuple(findings),
        diagnostics=tuple(diagnostics),
        providers=(provider,),
        completeness=completeness,
        limitations=tuple(limitations),
        summary=counts,
    )


def scan(context: AnalysisContext, cache: ContentCache | None = None) -> AnalysisReport:
    """Run the unified static, contract, dictionary, shape, and flow pipeline."""

    contract_report = _contract_inventory(context)
    phase_two_report = integrate_dictionary_report(
        context,
        contract_report.model_copy(
            update={"command": contract_report.command.model_copy(update={"name": "scan"})}
        ),
    )
    clone_report = integrate_clone_report(context, phase_two_report, cache)
    state_report = integrate_state_effect_report(context, clone_report)
    commonality_report = integrate_commonality_report(context, state_report)
    language_report = integrate_language_sources(context, commonality_report, cache)
    language_report = integrate_ruby_sources(context, language_report)
    language_report = integrate_static_call_report(context, language_report)
    interface_report = integrate_interface_report(context, language_report)
    cohesion_report = integrate_cohesion_report(context, interface_report)
    report = reconcile_proposal_evidence(integrate_test_impact(context, cohesion_report))
    return apply_finding_suppressions(
        finalize_language_sections(integrate_candidate_qualification(report)), context.config
    )


def _phase_three_command(
    report: AnalysisReport, name: Literal["clones", "refactor"]
) -> AnalysisReport:
    identity = hashlib.sha256(
        json.dumps(
            {"command": name, "prior": report.run_identity_sha256},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "run_identity_sha256": identity,
            "command": {**report.command.model_dump(), "name": name},
        }
    )


def scan_clones(context: AnalysisContext, cache: ContentCache | None = None) -> AnalysisReport:
    """Run the unified report with clone-focused command identity."""

    return _phase_three_command(scan(context, cache), "clones")


def scan_refactor(context: AnalysisContext, cache: ContentCache | None = None) -> AnalysisReport:
    """Run the unified report with refactoring-focused command identity."""

    return _phase_three_command(scan(context, cache), "refactor")


def scan_dictionary_shapes(context: AnalysisContext) -> AnalysisReport:
    """Run the unified pipeline with dictionary-focused command identity."""

    contract_report = _contract_inventory(context)
    report = integrate_dictionary_report(
        context,
        contract_report.model_copy(
            update={"command": contract_report.command.model_copy(update={"name": "scan"})}
        ),
    )
    return apply_finding_suppressions(
        AnalysisReport.model_validate(
            {
                **report.model_dump(),
                "command": report.command.model_copy(update={"name": "dict-shapes"}),
            }
        ),
        context.config,
    )


def scan_contract_inventory(context: AnalysisContext) -> AnalysisReport:
    return apply_finding_suppressions(_contract_inventory(context), context.config)


def _contract_inventory(context: AnalysisContext) -> AnalysisReport:
    return attach_python_source_records(_scan_contract_inventory(context))


def _scan_contract_inventory(context: AnalysisContext) -> AnalysisReport:
    """Run static analysis plus the bounded contract declaration inventory."""

    base = _scan_static(context)
    report_modules = {(module.name, module.path): module for module in base.modules}
    source_units: list[tuple[str, str, str, str, bool]] = []
    lineages: list[ClassLineageFact] = []
    contract_diagnostics: list[Diagnostic] = []
    parsed_count = 0
    failed_count = 0

    for path in iter_python_files(context):
        relative = logical_path(context, path)
        name = module_name(context, path)
        report_module = report_modules.get((name, relative))
        if report_module is None or report_module.parse_state not in {
            SourceParseState.COMPLETE,
            SourceParseState.PARTIAL,
        }:
            failed_count += 1
            continue
        try:
            payload = path.read_bytes()
            source = payload.decode("utf-8")
            source_sha256 = hashlib.sha256(payload).hexdigest()
            if source_sha256 != report_module.source_sha256:
                raise OSError("source changed during analysis")
            is_package = relative.endswith("/__init__.py") or relative == "__init__.py"
            module_lineages = discover_class_lineages(source, name, is_package=is_package)
        except SyntaxError as exc:
            failed_count += 1
            contract_diagnostics.append(
                Diagnostic(
                    code="contract-source-syntax-error",
                    level=DiagnosticLevel.ERROR,
                    message=f"contract inventory syntax error at line {exc.lineno or 1}",
                    path=relative,
                    provider="native-contracts",
                )
            )
            continue
        except (OSError, UnicodeError) as exc:
            failed_count += 1
            contract_diagnostics.append(
                Diagnostic(
                    code="contract-source-read-error",
                    level=DiagnosticLevel.ERROR,
                    message=(
                        "contract inventory could not read stable source: " f"{type(exc).__name__}"
                    ),
                    path=relative,
                    provider="native-contracts",
                )
            )
            continue
        parsed_count += 1
        source_units.append((name, relative, source_sha256, source, is_package))
        lineages.extend(module_lineages)

    resolved_kinds = resolve_contract_kinds(tuple(lineages))
    declarations = tuple(
        fact
        for name, _relative, _source_sha256, source, is_package in source_units
        for fact in discover_contract_declarations(
            source,
            name,
            is_package=is_package,
            resolved_kinds=resolved_kinds,
        )
    )
    declarations = merge_inherited_contract_members(declarations, tuple(lineages))
    module_evidence = {
        name: (relative, source_sha256)
        for name, relative, source_sha256, _source, _is_package in source_units
    }

    contract_kinds = tuple(
        sorted((f"{fact.module}.{fact.qualified_name}", fact.kind) for fact in declarations)
    )
    uses = tuple(
        use
        for name, _relative, _source_sha256, source, is_package in source_units
        for use in discover_contract_uses(
            source,
            name,
            contract_kinds,
            is_package=is_package,
        )
    )
    definition_modules = {
        f"{fact.module}.{fact.qualified_name}": fact.module for fact in declarations
    }
    classified_uses = tuple(
        _classify_contract_use(
            use,
            definition_modules[use.contract_identity],
            *module_evidence[use.module],
            context.config.contract_boundaries,
        )
        for use in uses
    )
    unclassified_count = sum(
        item.boundary_class is BoundaryClass.UNCLASSIFIED_INTERNAL for item in classified_uses
    )
    if unclassified_count:
        contract_diagnostics.append(
            Diagnostic(
                code="contract-boundary-unclassified",
                level=DiagnosticLevel.WARNING,
                message=(
                    f"{unclassified_count} cross-module contract uses require an explicit "
                    "boundary classification"
                ),
                provider="native-contracts",
            )
        )
    uses_by_contract: dict[str, list[_ClassifiedContractUse]] = {}
    for classified in classified_uses:
        uses_by_contract.setdefault(classified.use.contract_identity, []).append(classified)
    subjects_and_contracts = [
        _public_contract(
            fact,
            *module_evidence[fact.module],
            tuple(uses_by_contract.get(f"{fact.module}.{fact.qualified_name}", ())),
        )
        for fact in declarations
    ]
    subjects = tuple(
        sorted(
            (
                subject
                for item_subjects, _contract in subjects_and_contracts
                for subject in item_subjects
            ),
            key=lambda item: item.subject_id,
        )
    )
    contracts = tuple(
        sorted((item[1] for item in subjects_and_contracts), key=lambda item: item.contract_id)
    )
    contract_state = (
        SectionState.PARTIAL if failed_count or unclassified_count else SectionState.COMPLETE
    )
    overall = (
        Completeness.FAILED
        if base.completeness is Completeness.FAILED
        else (
            Completeness.PARTIAL
            if failed_count or unclassified_count or base.completeness is Completeness.PARTIAL
            else Completeness.COMPLETE
        )
    )
    contract_provider = ProviderResult(
        provider_id="native-contracts",
        state=(
            ProviderState.FAILED
            if overall is Completeness.FAILED
            else (
                ProviderState.PARTIAL
                if failed_count or unclassified_count
                else ProviderState.SUCCESS
            )
        ),
        executable="python",
        version=platform.python_version(),
        duration_ms=0,
        diagnostics=tuple(contract_diagnostics),
        observations=(
            "contract-declaration-inventory",
            "contract-direct-aliases",
            "contract-serialization-sites",
            "contract-use-sites",
            "normalized-type-expressions",
            "schema-version-declarations",
        ),
        completeness=overall,
    )
    findings = tuple(
        Finding.model_validate(
            {
                **finding.model_dump(),
                "confidence": finding.evidence[0].confidence,
                "completeness": base.completeness,
            }
        )
        for finding in base.findings
    )
    phase_two_configuration = base.configuration.model_copy(
        update={"digest_sha256": context.config.phase_two_identity()}
    )
    run_identity = _phase_two_run_identity(base, contracts, phase_two_configuration.digest_sha256)
    summary = SummaryCounts(
        **base.summary.model_dump(),
        subjects=len(subjects),
        contracts=len(contracts),
    )
    return AnalysisReport(
        tool_version=base.tool_version,
        run_identity_sha256=run_identity,
        command=base.command.model_copy(update={"name": "contracts"}),
        target=base.target,
        environment=base.environment,
        configuration=phase_two_configuration,
        modules=base.modules,
        complexity_observations=base.complexity_observations,
        import_edges=base.import_edges,
        strongly_connected_components=base.strongly_connected_components,
        cycles=base.cycles,
        subjects=subjects,
        contracts=contracts,
        dictionary_shapes=(),
        dictionary_observations=(),
        flow_edges=(),
        data_structures=(),
        validation_patterns=(),
        candidate_classifications=(),
        unresolved_edges=(),
        section_coverage=(
            SectionCoverage(
                section="r1-static-analysis",
                state=(
                    SectionState.COMPLETE
                    if base.completeness is Completeness.COMPLETE
                    else SectionState.PARTIAL
                ),
                parsed=base.summary.parsed_files,
                failed=base.summary.failed_files,
                diagnostic_codes=tuple(sorted({item.code for item in base.diagnostics})),
            ),
            SectionCoverage(section="static-call-occurrences", state=SectionState.DISABLED),
            SectionCoverage(
                section="contracts",
                state=contract_state,
                parsed=parsed_count,
                failed=failed_count,
                unresolved=unclassified_count,
                diagnostic_codes=tuple(sorted({item.code for item in contract_diagnostics})),
            ),
            SectionCoverage(section="interfaces", state=SectionState.DISABLED),
            SectionCoverage(section="dictionary-shapes", state=SectionState.DISABLED),
            SectionCoverage(section="validation", state=SectionState.DISABLED),
            SectionCoverage(section="data-structures", state=SectionState.DISABLED),
            SectionCoverage(section="clones", state=SectionState.DISABLED),
            SectionCoverage(section="function-commonality", state=SectionState.DISABLED),
            SectionCoverage(section="state-side-effects", state=SectionState.DISABLED),
            SectionCoverage(section="cohesion-refactoring", state=SectionState.DISABLED),
            SectionCoverage(section="test-impact", state=SectionState.DISABLED),
            SectionCoverage(section="coverage", state=SectionState.DISABLED),
            SectionCoverage(section="git-history", state=SectionState.DISABLED, required=False),
            SectionCoverage(
                section="generation-history", state=SectionState.DISABLED, required=False
            ),
        ),
        findings=findings,
        diagnostics=(*base.diagnostics, *contract_diagnostics),
        providers=(*base.providers, contract_provider),
        completeness=overall,
        limitations=tuple(
            sorted(
                {
                    *(
                        item
                        for item in base.limitations
                        if not item.startswith("Phase 1 uses Python AST evidence")
                    ),
                    *CONTRACT_INVENTORY_LIMITATIONS,
                }
            )
        ),
        summary=summary,
    )


def _public_contract(
    fact: ContractDeclarationFact,
    relative: str,
    source_sha256: str,
    use_sites: tuple[_ClassifiedContractUse, ...],
) -> tuple[tuple[AnalysisSubject, ...], ContractDefinition]:
    logical_name = f"{fact.module}:{fact.qualified_name}"
    subject_id = _stable_public_id("subject", ("symbol", fact.module, fact.qualified_name))
    contract_id = _stable_public_id(
        "contract",
        (SCHEMA_NAME, fact.kind.value, fact.module, fact.qualified_name),
    )
    evidence = _contract_evidence(
        relative,
        source_sha256,
        fact.span.start_line,
        fact.span.start_column,
        fact.span.end_line,
        fact.span.end_column,
        f"{fact.kind.value} contract declaration",
    )
    boundary_class = (
        BoundaryClass.TEST_QUALIFICATION_EVIDENCE
        if relative.startswith("tests/") or "/tests/" in relative
        else BoundaryClass.MODULE_LOCAL
    )
    definition_boundary = ContractBoundary(
        boundary_id=_stable_public_id(
            "boundary", (contract_id, boundary_class.value, "definition", logical_name)
        ),
        boundary_class=boundary_class,
        direction=BoundaryDirection.DEFINITION,
        site_subject_id=subject_id,
        evidence=(evidence,),
        confidence=Confidence.EXACT,
    )
    members = tuple(
        _public_contract_member(contract_id, member, relative, source_sha256)
        for member in fact.members
    )
    subject = AnalysisSubject(
        subject_id=subject_id,
        kind=SubjectKind.SYMBOL,
        logical_name=logical_name,
        module=fact.module,
        evidence=(evidence,),
    )
    boundary_subjects: list[AnalysisSubject] = []
    use_boundaries: list[ContractBoundary] = []
    serialization_sites: list[Evidence] = []
    serialization_formats: set[str] = set()
    for classified in use_sites:
        use = classified.use
        use_evidence = _contract_evidence(
            classified.relative,
            classified.source_sha256,
            use.span.start_line,
            use.span.start_column,
            use.span.end_line,
            use.span.end_column,
            use.observation,
        )
        use_subject_id = _stable_public_id("subject", ("boundary", use.module, use.logical_name))
        use_boundary_class = classified.boundary_class
        boundary_subjects.append(
            AnalysisSubject(
                subject_id=use_subject_id,
                kind=SubjectKind.BOUNDARY,
                logical_name=use.logical_name,
                module=use.module,
                evidence=(use_evidence,),
            )
        )
        use_boundaries.append(
            ContractBoundary(
                boundary_id=_stable_public_id(
                    "boundary",
                    (
                        contract_id,
                        use_boundary_class.value,
                        use.direction.value,
                        use.logical_name,
                    ),
                ),
                boundary_class=use_boundary_class,
                direction=BoundaryDirection(use.direction.value),
                site_subject_id=use_subject_id,
                evidence=(use_evidence,),
                confidence=classified.confidence,
                limitations=classified.limitations,
            )
        )
        if use.serialization_format is not None:
            serialization_formats.add(use.serialization_format)
            serialization_sites.append(use_evidence)
    contract = ContractDefinition(
        contract_id=contract_id,
        kind=ContractKind(fact.kind.value),
        defining_subject_id=subject_id,
        boundaries=tuple(
            sorted(
                (definition_boundary, *use_boundaries),
                key=lambda item: item.boundary_id,
            )
        ),
        members=members,
        serialization_formats=tuple(sorted(serialization_formats)),
        serialization_sites=tuple(
            sorted(
                serialization_sites,
                key=lambda item: (
                    item.span.path,
                    item.span.start_line,
                    item.span.start_column,
                    item.observation,
                ),
            )
        ),
        schema_versions=_contract_schema_versions(fact),
        evidence=(evidence,),
    )
    subjects = tuple(sorted((subject, *boundary_subjects), key=lambda item: item.subject_id))
    return subjects, contract


def _contract_schema_versions(fact: ContractDeclarationFact) -> tuple[str, ...]:
    versions: set[str] = set()
    for member in fact.members:
        if member.name not in {"schemaVersion", "schema_version"}:
            continue
        for canonical in member.type_fact.literal_json:
            value = json.loads(canonical)
            if isinstance(value, str):
                versions.add(value)
            elif value is not None and not isinstance(value, (dict, list)):
                versions.add(canonical)
        if member.default.kind is InternalDefaultKind.JSON_LITERAL:
            assert member.default.canonical_json is not None
            value = json.loads(member.default.canonical_json)
            if isinstance(value, str):
                versions.add(value)
            elif value is not None and not isinstance(value, (dict, list)):
                versions.add(member.default.canonical_json)
    return tuple(sorted(versions))


def _classify_contract_use(
    use: ContractUseFact,
    definition_module: str,
    relative: str,
    source_sha256: str,
    rules: tuple[ContractBoundaryRule, ...],
) -> _ClassifiedContractUse:
    if relative.startswith("tests/") or "/tests/" in relative:
        return _ClassifiedContractUse(
            use=use,
            relative=relative,
            source_sha256=source_sha256,
            boundary_class=BoundaryClass.TEST_QUALIFICATION_EVIDENCE,
            confidence=Confidence.EXACT,
        )
    if use.module == definition_module:
        return _ClassifiedContractUse(
            use=use,
            relative=relative,
            source_sha256=source_sha256,
            boundary_class=BoundaryClass.MODULE_LOCAL,
            confidence=Confidence.EXACT,
        )
    matches = {
        rule.boundary_class
        for rule in rules
        if fnmatchcase(use.contract_identity, rule.contract_pattern)
        and fnmatchcase(use.logical_name, rule.site_pattern)
    }
    if len(matches) == 1:
        return _ClassifiedContractUse(
            use=use,
            relative=relative,
            source_sha256=source_sha256,
            boundary_class=matches.pop(),
            confidence=Confidence.RESOLVED,
        )
    limitation = (
        "Conflicting configured boundary rules require review."
        if matches
        else "Cross-module subsystem ownership is not configured."
    )
    return _ClassifiedContractUse(
        use=use,
        relative=relative,
        source_sha256=source_sha256,
        boundary_class=BoundaryClass.UNCLASSIFIED_INTERNAL,
        confidence=Confidence.EXACT,
        limitations=(limitation,),
    )


def _public_contract_member(
    contract_id: str,
    fact: ContractMemberFact,
    relative: str,
    source_sha256: str,
) -> ContractMember:
    evidence = _contract_evidence(
        relative,
        source_sha256,
        fact.span.start_line,
        fact.span.start_column,
        fact.span.end_line,
        fact.span.end_column,
        f"contract member {fact.name}",
    )
    default = _public_default(contract_id, fact.name, fact.default, evidence)
    return ContractMember(
        name=fact.name,
        type_expression=_public_type(fact.type_fact),
        requiredness=FieldRequiredness(
            kind=RequirednessKind(fact.requiredness.value),
            evidence=(evidence,),
        ),
        producer_defaults=(default,),
        evidence=(evidence,),
    )


def _public_type(fact: TypeFact) -> TypeExpression:
    return TypeExpression(
        kind=TypeKind(fact.kind.value),
        name=fact.name,
        arguments=tuple(_public_type(argument) for argument in fact.arguments),
        literal_values=tuple(json.loads(value) for value in fact.literal_json),
    )


def _public_default(
    contract_id: str, member_name: str, fact: DefaultFact, evidence: Evidence
) -> DefaultValue:
    kind = DefaultKind(fact.kind.value)
    default_id = _stable_public_id(
        "default", (contract_id, member_name, "producer", fact.kind.value)
    )
    if fact.kind is InternalDefaultKind.JSON_LITERAL:
        return DefaultValue(
            default_id=default_id,
            kind=kind,
            json_literal=JsonLiteralValue(value=json.loads(fact.canonical_json or "null")),
            evidence=(evidence,),
        )
    if fact.kind in {
        InternalDefaultKind.FACTORY,
        InternalDefaultKind.EXPRESSION,
        InternalDefaultKind.UNRESOLVED,
    }:
        return DefaultValue(
            default_id=default_id,
            kind=kind,
            detail=fact.detail,
            evidence=(evidence,),
        )
    return DefaultValue(default_id=default_id, kind=kind, evidence=(evidence,))


def _contract_evidence(
    relative: str,
    source_sha256: str,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
    observation: str,
) -> Evidence:
    return Evidence(
        span=SourceSpan(
            path=relative,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
        ),
        provider="native-contracts",
        observation=observation,
        confidence=Confidence.EXACT,
        source_sha256=source_sha256,
    )


def _stable_public_id(prefix: str, parts: tuple[str, ...]) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:20]}"


def _phase_two_run_identity(
    base: StaticAnalysisResult,
    contracts: tuple[ContractDefinition, ...],
    configuration_digest: str,
) -> str:
    payload = {
        "analyzers": ("native-ast@1", "native-contracts@1"),
        "configuration": configuration_digest,
        "contracts": tuple(contract.contract_id for contract in contracts),
        "python": base.environment.python_version,
        "sources": tuple(
            (module.name, module.path, module.source_sha256) for module in base.modules
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _failed_module(path: Path, relative: str, name: str) -> Module:
    try:
        data = path.read_bytes()
    except OSError:
        data = b""
    return Module(
        name=name,
        path=relative,
        source_sha256=hashlib.sha256(data).hexdigest(),
        line_count=len(data.splitlines()),
        nonblank_line_count=sum(bool(line.strip()) for line in data.splitlines()),
        parse_state=SourceParseState.FAILED,
    )


def _public_module(module: ParsedModule, incoming: set[str], outgoing: set[str]) -> Module:
    return Module(
        name=module.name,
        path=module.logical_path,
        source_sha256=module.source_sha256,
        line_count=module.line_count,
        nonblank_line_count=module.nonblank_line_count,
        symbols=tuple(_public_symbol(symbol, module.logical_path) for symbol in module.symbols),
        fan_in=len(incoming),
        fan_out=len(outgoing),
        parse_state=SourceParseState.COMPLETE,
    )


def _public_symbol(symbol: InternalSymbol, path: str) -> Symbol:
    return Symbol(
        qualified_name=symbol.qualified_name,
        name=symbol.name,
        kind=symbol.kind,  # type: ignore[arg-type]
        span=SourceSpan(
            path=path,
            start_line=symbol.start_line,
            start_column=symbol.start_column,
            end_line=symbol.end_line,
            end_column=symbol.end_column,
        ),
        decorators=symbol.decorators,
        parameters=symbol.parameters,
        returns=symbol.returns,
        complexity=_public_complexity(symbol.complexity) if symbol.complexity else None,
    )


def _public_complexity(metric: InternalComplexity) -> ComplexityObservation:
    return ComplexityObservation(
        physical_loc=metric.physical_loc,
        logical_loc=metric.logical_loc,
        branches=metric.branches,
        nesting_depth=metric.nesting_depth,
        argument_count=metric.argument_count,
        cyclomatic_complexity=metric.cyclomatic_complexity,
        weighted_score=metric.weighted_score,
        returns=metric.returns,
        raises=metric.raises,
    )


def _public_edge(edge: ResolvedEdge) -> ImportEdge:
    return ImportEdge(
        source=edge.source,
        target=edge.target,
        local=edge.local,
        kind=edge.kind,  # type: ignore[arg-type]
    )


def _complexity_findings(
    modules: tuple[ParsedModule, ...], config: AnalysisConfig
) -> list[StaticFinding]:
    findings: list[StaticFinding] = []
    for module in modules:
        for symbol in module.symbols:
            if symbol.complexity is None or not exceeds_thresholds(symbol, config):
                continue
            if config.symbol_filter and config.symbol_filter not in symbol.qualified_name:
                continue
            orchestration = is_orchestration(symbol)
            rule = "CA101" if orchestration else "CA100"
            category = "orchestration" if orchestration else "complexity"
            metric = symbol.complexity
            observation = (
                f"loc={metric.physical_loc}, branches={metric.branches}, "
                f"nesting={metric.nesting_depth}, args={metric.argument_count}, "
                f"weighted_score={metric.weighted_score}"
            )
            finding_id = _finding_id(rule, module.name, symbol.qualified_name)
            findings.append(
                StaticFinding(
                    finding_id=finding_id,
                    rule_id=rule,
                    category=category,  # type: ignore[arg-type]
                    severity="warning",
                    title=(
                        "Orchestration candidate exceeds configured thresholds"
                        if orchestration
                        else "Function exceeds configured complexity thresholds"
                    ),
                    explanation=(
                        "Review ownership boundaries and evidence before proposing extraction."
                    ),
                    module=module.name,
                    symbol=symbol.qualified_name,
                    evidence=(
                        Evidence(
                            span=SourceSpan(
                                path=module.logical_path,
                                start_line=symbol.start_line,
                                start_column=symbol.start_column,
                                end_line=symbol.end_line,
                                end_column=symbol.end_column,
                            ),
                            provider="native-ast",
                            observation=observation,
                            confidence=Confidence.EXACT,
                            source_sha256=module.source_sha256,
                        ),
                    ),
                )
            )
    return findings


def _cycle_findings(
    cycles: tuple[tuple[str, ...], ...], modules: dict[str, ParsedModule]
) -> list[StaticFinding]:
    findings: list[StaticFinding] = []
    for cycle in cycles:
        module = modules[cycle[0]]
        observation = " -> ".join(cycle)
        findings.append(
            StaticFinding(
                finding_id=_finding_id("CA200", *cycle[:-1]),
                rule_id="CA200",
                category="import-cycle",
                severity="warning",
                title="Local module import cycle",
                explanation=(
                    "The canonical directed cycle is direct syntax evidence; assess ownership "
                    "before changing dependencies."
                ),
                module=module.name,
                evidence=(
                    Evidence(
                        span=SourceSpan(
                            path=module.logical_path,
                            start_line=1,
                            start_column=0,
                            end_line=1,
                            end_column=0,
                        ),
                        provider="native-ast",
                        observation=observation,
                        confidence=Confidence.RESOLVED,
                        source_sha256=module.source_sha256,
                    ),
                ),
            )
        )
    return findings


def _finding_id(rule: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join((rule, *parts)).encode()).hexdigest()[:16]
    return f"{rule}-{digest}"
