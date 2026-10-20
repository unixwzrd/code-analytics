"""Conversion of immutable dictionary-analysis facts into authoritative report records."""

from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass
from fnmatch import fnmatchcase

from code_analysis.analysis.candidates import (
    DictionaryCandidateFact,
    classify_dictionary_candidates,
)
from code_analysis.analysis.dictionary_ops import DictionaryAnalysisFacts, discover_dictionary_facts
from code_analysis.analysis.dictionary_values import (
    DictionaryConversionObservation,
    DictionaryDefaultObservation,
    DictionaryFieldFact,
    DictionaryOperationObservation,
    DictionaryShapeJoinInput,
    DictionaryVariantJoinInput,
    InternalSourceSpan,
)
from code_analysis.analysis.flow import (
    DictionaryFlowFacts,
    DictionaryFlowObservation,
    DictionaryModuleSource,
    InternalResolutionState,
    discover_project_dictionary_flows,
)
from code_analysis.analysis.shapes import DictionaryShapeBuildResult, build_dictionary_shape_inputs
from code_analysis.analysis.structures import (
    DataStructureTopologyFact,
    discover_data_structure_topology,
)
from code_analysis.analysis.validation import (
    OperationalValidationPattern,
    OperationalValidationSource,
    StructuralValidationPattern,
    discover_operational_validation_patterns,
    discover_structural_validation_patterns,
)
from code_analysis.config import ContractBoundaryRule
from code_analysis.context import AnalysisContext
from code_analysis.models import (
    Completeness,
    Confidence,
    Diagnostic,
    DiagnosticLevel,
    Evidence,
    ProviderResult,
    ProviderState,
    SourceParseState,
    SourceSpan,
)
from code_analysis.project import iter_python_files, logical_path, module_name
from code_analysis.report_models import (
    AnalysisReport,
    AnalysisSubject,
    BoundaryClass,
    BoundaryDirection,
    CandidateClassification,
    CandidateKind,
    ContractBoundary,
    ContractDefinition,
    ContractKind,
    DataStructureRecord,
    DefaultKind,
    DefaultValue,
    DictionaryKeyKind,
    DictionaryObservation,
    DictionaryOperationKind,
    DictionaryPredicate,
    DictionaryShape,
    FieldRequiredness,
    Finding,
    FlowEdge,
    FlowKind,
    JsonLiteralValue,
    MutationTiming,
    RequirednessKind,
    ResolutionState,
    SectionCoverage,
    SectionState,
    ShapeDiscriminator,
    ShapeField,
    ShapeLifecycleState,
    ShapeVariant,
    StructureScope,
    SubjectKind,
    SummaryCounts,
    TypeExpression,
    TypeKind,
    UnknownFieldPolicy,
    UnresolvedEdge,
    ValidationPattern,
)

DICTIONARY_ANALYSIS_LIMITATIONS = (
    "Dictionary analysis is syntax-only and bounded; dynamic calls and keys remain unresolved.",
    "Validation-relative mutation timing and data-structure ownership remain unavailable.",
    "Dictionary evidence is descriptive and does not authorize a representation change.",
)

VALIDATION_ANALYSIS_LIMITATIONS = (
    "Structural validation currently covers proven dictionary membership, truthiness, "
    "and coercion checks.",
    "Operational filesystem, path, symlink, authorization, security, and runtime-state "
    "validation is excluded.",
)

STRUCTURE_ANALYSIS_LIMITATIONS = (
    "Data-structure topology is syntax-only and limited to evidenced dictionary operations "
    "and declarations.",
    "Runtime ownership, lock behavior and coverage, persistence success, cleanup "
    "effectiveness, and growth remain unmeasured.",
    "Growth indicators are measurement prompts, not performance findings.",
)


@dataclass(frozen=True, slots=True)
class _DictionaryUnit:
    module: str
    relative: str
    source_sha256: str
    source: str
    facts: DictionaryAnalysisFacts
    shapes: DictionaryShapeBuildResult


@dataclass(frozen=True, slots=True)
class _DictionaryPersistenceRecords:
    subjects: tuple[AnalysisSubject, ...]
    contracts: tuple[ContractDefinition, ...]
    boundary_ids_by_owner: tuple[tuple[str, tuple[str, ...]], ...]
    boundary_classes_by_owner: tuple[tuple[str, tuple[BoundaryClass, ...]], ...]
    conflict_evidence: tuple[Evidence, ...]


def integrate_dictionary_report(
    context: AnalysisContext,
    report: AnalysisReport,
) -> AnalysisReport:
    modules_by_identity = {(item.name, item.path): item for item in report.modules}
    units: list[_DictionaryUnit] = []
    for path in iter_python_files(context):
        relative = logical_path(context, path)
        name = module_name(context, path)
        module = modules_by_identity.get((name, relative))
        if module is None or module.parse_state not in {
            SourceParseState.COMPLETE,
            SourceParseState.PARTIAL,
        }:
            continue
        payload = path.read_bytes()
        source = payload.decode("utf-8")
        source_sha256 = hashlib.sha256(payload).hexdigest()
        if source_sha256 != module.source_sha256:
            raise OSError("source changed during dictionary analysis")
        facts = discover_dictionary_facts(source, relative, name)
        units.append(
            _DictionaryUnit(
                module=name,
                relative=relative,
                source_sha256=source_sha256,
                source=source,
                facts=facts,
                shapes=build_dictionary_shape_inputs(facts),
            )
        )
    units.sort(key=lambda item: (item.module, item.relative))
    project_flows = (
        discover_project_dictionary_flows(
            tuple(
                DictionaryModuleSource(
                    module=item.module,
                    logical_path=item.relative,
                    source=item.source,
                )
                for item in units
            )
        )
        if units
        else DictionaryFlowFacts((), 0, 0, 0)
    )
    project_flows = _without_resolved_conversion_duplicates(
        project_flows,
        tuple(conversion for item in units for conversion in item.facts.conversions),
    )
    source_hashes = {item.relative: item.source_sha256 for item in units}
    conversion_targets = {
        conversion.target_identity for item in units for conversion in item.facts.conversions
    }
    subject_evidence = _dictionary_subject_evidence(units, project_flows.flows, source_hashes)
    dictionary_subjects = tuple(
        AnalysisSubject(
            subject_id=_dictionary_subject_id(identity),
            kind=(SubjectKind.EXTERNAL if identity in conversion_targets else SubjectKind.SYMBOL),
            logical_name=identity,
            module=(
                identity.rsplit(".", 1)[0]
                if identity in conversion_targets
                else identity.split(":", 1)[0]
            ),
            evidence=evidence,
        )
        for identity, evidence in sorted(subject_evidence.items())
    )
    subjects = tuple(
        sorted((*report.subjects, *dictionary_subjects), key=lambda item: item.subject_id)
    )
    shapes = tuple(
        sorted(
            (
                _public_dictionary_shape(item.shapes, shape, source_hashes)
                for item in units
                for shape in item.shapes.shapes
            ),
            key=lambda item: item.shape_id,
        )
    )
    shape_ids = {
        shape.owner: _stable_public_id("shape", ("dictionary", shape.owner))
        for item in units
        for shape in item.shapes.shapes
    }
    variant_ids = {
        (shape.owner, variant.name): _stable_public_id(
            "variant", (shape_ids[shape.owner], variant.name)
        )
        for item in units
        for shape in item.shapes.shapes
        for variant in shape.variants
    }
    observations = tuple(
        sorted(
            (
                _public_dictionary_observation(
                    operation,
                    source_hashes,
                    shape_ids,
                    variant_ids,
                )
                for item in units
                for operation in item.facts.operations
            ),
            key=lambda item: item.observation_id,
        )
    )
    internal_validation_patterns = discover_structural_validation_patterns(
        tuple(unit.facts for unit in units)
    )
    internal_operational_patterns = discover_operational_validation_patterns(
        tuple(
            OperationalValidationSource(item.module, item.relative, item.source) for item in units
        )
    )
    operational_subjects = _operational_validation_subjects(
        internal_operational_patterns, source_hashes
    )
    subjects_by_id = {item.subject_id: item for item in (*subjects, *operational_subjects)}
    subjects = tuple(subjects_by_id[key] for key in sorted(subjects_by_id))
    validation_patterns = tuple(
        sorted(
            (
                *(
                    _public_validation_pattern(item, source_hashes)
                    for item in internal_validation_patterns
                ),
                *(
                    _public_operational_validation_pattern(item, source_hashes)
                    for item in internal_operational_patterns
                ),
            ),
            key=lambda item: item.validation_id,
        )
    )
    internal_structures = discover_data_structure_topology(
        tuple(unit.facts for unit in units),
        tuple(unit.shapes for unit in units),
    )
    internal_candidates = classify_dictionary_candidates(
        tuple(unit.facts for unit in units),
        tuple(unit.shapes for unit in units),
        internal_structures,
        project_flows.flows,
    )
    conversion_records = tuple(
        (conversion, _public_conversion_flow(conversion, source_hashes))
        for item in units
        for conversion in item.facts.conversions
    )
    conversion_flows = tuple(
        sorted((flow for _conversion, flow in conversion_records), key=lambda item: item.flow_id)
    )
    conversion_flows_by_owner = {
        item.owner: tuple(
            sorted(
                (flow for conversion, flow in conversion_records if conversion.owner == item.owner),
                key=lambda flow: flow.flow_id,
            )
        )
        for item in internal_structures
    }
    persistence = _dictionary_persistence_records(
        internal_structures,
        conversion_records,
        context.config.contract_boundaries,
        subject_evidence,
    )
    subjects = tuple(sorted((*subjects, *persistence.subjects), key=lambda item: item.subject_id))
    contracts = tuple(
        sorted((*report.contracts, *persistence.contracts), key=lambda item: item.contract_id)
    )
    persistence_ids_by_owner = dict(persistence.boundary_ids_by_owner)
    boundary_classes_by_owner = dict(persistence.boundary_classes_by_owner)
    candidate_classifications = tuple(
        sorted(
            (
                _public_candidate_classification(item, source_hashes)
                for item in internal_candidates
                if _candidate_allowed_at_boundaries(
                    item.candidate.value,
                    boundary_classes_by_owner.get(item.owner, ()),
                )
            ),
            key=lambda item: item.candidate_id,
        )
    )
    data_structures = tuple(
        sorted(
            (
                _public_data_structure(
                    item,
                    source_hashes,
                    conversion_flows_by_owner.get(item.owner, ()),
                    persistence_ids_by_owner.get(item.owner, ()),
                )
                for item in internal_structures
            ),
            key=lambda item: item.structure_id,
        )
    )
    dictionary_flows = tuple(
        sorted(
            (
                _public_dictionary_flow(flow, source_hashes)
                for flow in project_flows.flows
                if flow.resolution is InternalResolutionState.RESOLVED
            ),
            key=lambda item: item.flow_id,
        )
    )
    resolved_flows = tuple(
        sorted((*dictionary_flows, *conversion_flows), key=lambda item: item.flow_id)
    )
    unresolved_edges = tuple(
        sorted(
            (
                _public_unresolved_dictionary_flow(flow, source_hashes)
                for flow in project_flows.flows
                if flow.resolution is not InternalResolutionState.RESOLVED
            ),
            key=lambda item: item.unresolved_id,
        )
    )
    unresolved_shapes = sum(item.shapes.unresolved_observation_count for item in units)
    unresolved_total = (
        unresolved_shapes
        + project_flows.unresolved_count
        + project_flows.ambiguous_count
        + len(persistence.conflict_evidence)
    )
    failed = report.summary.failed_files
    dictionary_state = (
        SectionState.PARTIAL
        if failed or unresolved_total or project_flows.truncated_count
        else SectionState.COMPLETE
    )
    overall = (
        Completeness.FAILED
        if report.completeness is Completeness.FAILED
        else (
            Completeness.PARTIAL
            if report.completeness is Completeness.PARTIAL
            or dictionary_state is SectionState.PARTIAL
            else Completeness.COMPLETE
        )
    )
    dictionary_diagnostics = (
        (
            Diagnostic(
                code="dictionary-boundary-conflict",
                level=DiagnosticLevel.WARNING,
                message=(
                    f"{len(persistence.conflict_evidence)} dictionary serialization sites "
                    "match conflicting configured boundary classes"
                ),
                provider="native-dictionary",
            ),
        )
        if persistence.conflict_evidence
        else ()
    )
    if project_flows.truncated_count:
        dictionary_diagnostics += (
            Diagnostic(
                code="dictionary-flow-truncated",
                level=DiagnosticLevel.WARNING,
                message=(
                    "Dictionary-flow bounds omitted "
                    f"{project_flows.truncated_count} reachable observations or traversal steps; "
                    "retained evidence is incomplete"
                ),
                provider="native-dictionary",
            ),
        )
    dictionary_provider = ProviderResult(
        provider_id="native-dictionary",
        state=(
            ProviderState.PARTIAL
            if dictionary_state is SectionState.PARTIAL
            else ProviderState.SUCCESS
        ),
        executable="python",
        version=platform.python_version(),
        duration_ms=0,
        observations=(
            "dictionary-operations",
            "dictionary-shapes",
            "dictionary-flow",
            "dictionary-lifecycle-transitions",
            *(
                ("dictionary-conversion-flow", "dictionary-serialization-flow")
                if conversion_flows
                else ()
            ),
            *(("configured-dictionary-persistence-boundary",) if persistence.contracts else ()),
        ),
        diagnostics=dictionary_diagnostics,
        completeness=(
            Completeness.PARTIAL
            if dictionary_state is SectionState.PARTIAL
            else Completeness.COMPLETE
        ),
    )
    validation_state = SectionState.PARTIAL if failed else SectionState.COMPLETE
    validation_provider = ProviderResult(
        provider_id="native-validation",
        state=ProviderState.PARTIAL if failed else ProviderState.SUCCESS,
        executable="python",
        version=platform.python_version(),
        duration_ms=0,
        observations=(
            "structural-dictionary-validation",
            *(("operational-path-validation",) if internal_operational_patterns else ()),
        ),
        completeness=Completeness.PARTIAL if failed else Completeness.COMPLETE,
    )
    structure_state = (
        SectionState.PARTIAL
        if failed or unresolved_shapes or persistence.conflict_evidence
        else SectionState.COMPLETE
    )
    structure_provider = ProviderResult(
        provider_id="native-structures",
        state=(
            ProviderState.PARTIAL
            if structure_state is SectionState.PARTIAL
            else ProviderState.SUCCESS
        ),
        executable="python",
        version=platform.python_version(),
        duration_ms=0,
        observations=("dictionary-topology", "dictionary-lifecycle"),
        completeness=(
            Completeness.PARTIAL
            if structure_state is SectionState.PARTIAL
            else Completeness.COMPLETE
        ),
    )
    candidate_providers = (
        (
            ProviderResult(
                provider_id="native-candidates",
                state=(
                    ProviderState.PARTIAL
                    if structure_state is SectionState.PARTIAL
                    else ProviderState.SUCCESS
                ),
                executable="python",
                version=platform.python_version(),
                duration_ms=0,
                observations=("dictionary-representation-candidates",),
                completeness=(
                    Completeness.PARTIAL
                    if structure_state is SectionState.PARTIAL
                    else Completeness.COMPLETE
                ),
            ),
        )
        if candidate_classifications
        else ()
    )
    coverage = tuple(
        (
            SectionCoverage(
                section=item.section,
                state=dictionary_state,
                parsed=len(units),
                failed=failed,
                unresolved=unresolved_total,
                truncated=project_flows.truncated_count,
                diagnostic_codes=tuple(sorted(item.code for item in dictionary_diagnostics)),
            )
            if item.section == "dictionary-shapes"
            else (
                SectionCoverage(
                    section=item.section,
                    state=validation_state,
                    parsed=len(units),
                    failed=failed,
                )
                if item.section == "validation"
                else (
                    SectionCoverage(
                        section=item.section,
                        state=structure_state,
                        parsed=len(data_structures),
                        failed=failed,
                        unresolved=unresolved_shapes + len(persistence.conflict_evidence),
                    )
                    if item.section == "data-structures"
                    else item
                )
            )
        )
        for item in report.section_coverage
    )
    findings = tuple(
        sorted(
            (
                *(item.model_copy(update={"completeness": overall}) for item in report.findings),
                *_validation_findings(validation_patterns, subjects, overall),
                *_conversion_findings(data_structures, resolved_flows, subjects, overall),
            ),
            key=lambda item: item.finding_id,
        )
    )
    summary = SummaryCounts(
        **{
            **report.summary.model_dump(),
            "subjects": len(subjects),
            "contracts": len(contracts),
            "dictionary_shapes": len(shapes),
            "dictionary_observations": len(observations),
            "flow_edges": len(resolved_flows),
            "findings": len(findings),
            "data_structures": len(data_structures),
            "validation_patterns": len(validation_patterns),
            "candidate_classifications": len(candidate_classifications),
            "unresolved_edges": len(unresolved_edges),
        }
    )
    run_identity = _dictionary_run_identity(
        report,
        persistence.contracts,
        shapes,
        observations,
        resolved_flows,
        validation_patterns,
        data_structures,
        candidate_classifications,
        unresolved_edges,
    )
    limitations = {
        *report.limitations,
        *DICTIONARY_ANALYSIS_LIMITATIONS,
        *VALIDATION_ANALYSIS_LIMITATIONS,
        *STRUCTURE_ANALYSIS_LIMITATIONS,
    }
    limitations.discard(
        "Dictionary-shape, validation, lifecycle, and candidate analysis remain disabled."
    )
    if internal_operational_patterns:
        limitations.discard(
            "Operational filesystem, path, symlink, authorization, security, and runtime-state "
            "validation is excluded."
        )
        limitations.add(
            "Operational validation is limited to syntax-evidenced path and filesystem checks; "
            "outcomes and effectiveness remain unmeasured."
        )
    if unresolved_total:
        limitations.add("Unresolved dictionary evidence constrains report completeness.")
    if project_flows.truncated_count:
        limitations.add("Configured dictionary-flow bounds truncated reachable evidence.")
    if persistence.conflict_evidence:
        limitations.add("Conflicting dictionary-boundary rules require project-owner review.")
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "run_identity_sha256": run_identity,
            "subjects": subjects,
            "contracts": contracts,
            "dictionary_shapes": shapes,
            "dictionary_observations": observations,
            "flow_edges": resolved_flows,
            "validation_patterns": validation_patterns,
            "data_structures": data_structures,
            "candidate_classifications": candidate_classifications,
            "unresolved_edges": unresolved_edges,
            "findings": findings,
            "diagnostics": (*report.diagnostics, *dictionary_diagnostics),
            "section_coverage": coverage,
            "providers": (
                *report.providers,
                dictionary_provider,
                validation_provider,
                structure_provider,
                *candidate_providers,
            ),
            "completeness": overall,
            "limitations": tuple(sorted(limitations)),
            "summary": summary,
        }
    )


def _dictionary_subject_evidence(
    units: list[_DictionaryUnit],
    flows: tuple[DictionaryFlowObservation, ...],
    source_hashes: dict[str, str],
) -> dict[str, tuple[Evidence, ...]]:
    collected: dict[str, list[Evidence]] = {}

    def add(identity: str, evidence: Evidence) -> None:
        collected.setdefault(identity, []).append(evidence)

    for unit in units:
        for operation in unit.facts.operations:
            add(
                operation.owner,
                _dictionary_evidence(
                    operation.span,
                    source_hashes,
                    f"{operation.kind.value} dictionary operation",
                ),
            )
        for default in unit.facts.defaults:
            add(
                default.owner,
                _dictionary_evidence(
                    default.span,
                    source_hashes,
                    f"{default.role.value} dictionary default",
                ),
            )
        for alias in unit.facts.aliases:
            evidence = _dictionary_evidence(
                alias.span,
                source_hashes,
                "dictionary alias",
            )
            add(alias.source_owner, evidence)
            add(alias.alias_identity, evidence)
        for conversion in unit.facts.conversions:
            evidence = _dictionary_evidence(
                conversion.span,
                source_hashes,
                f"{conversion.kind.value} dictionary flow",
            )
            add(conversion.owner, evidence)
            add(conversion.target_identity, evidence)
    for flow in flows:
        evidence = _dictionary_evidence(
            flow.span,
            source_hashes,
            f"{flow.kind.value} dictionary flow",
        )
        add(flow.source_owner, evidence)
        if flow.resolution is InternalResolutionState.RESOLVED:
            add(flow.target_identity, evidence)
    return {identity: _unique_evidence(values) for identity, values in sorted(collected.items())}


def _without_resolved_conversion_duplicates(
    flows: DictionaryFlowFacts,
    conversions: tuple[DictionaryConversionObservation, ...],
) -> DictionaryFlowFacts:
    conversion_keys = {
        (
            item.owner,
            item.target_identity,
            item.span.logical_path,
            item.span.start_line,
            item.span.start_column,
        )
        for item in conversions
    }
    retained = tuple(
        item
        for item in flows.flows
        if not any(
            item.source_owner == owner
            and f":call:{target}:argument:0" in item.target_identity
            and item.span.logical_path == path
            and item.span.start_line == line
            and item.span.start_column == column
            for owner, target, path, line, column in conversion_keys
        )
    )
    return DictionaryFlowFacts(
        flows=retained,
        unresolved_count=sum(
            item.resolution is InternalResolutionState.UNRESOLVED for item in retained
        ),
        ambiguous_count=sum(
            item.resolution is InternalResolutionState.AMBIGUOUS for item in retained
        ),
        truncated_count=flows.truncated_count,
    )


def _dictionary_persistence_records(
    structures: tuple[DataStructureTopologyFact, ...],
    conversions: tuple[tuple[DictionaryConversionObservation, FlowEdge], ...],
    rules: tuple[ContractBoundaryRule, ...],
    subject_evidence: dict[str, tuple[Evidence, ...]],
) -> _DictionaryPersistenceRecords:
    subjects: list[AnalysisSubject] = []
    contracts: list[ContractDefinition] = []
    boundary_ids_by_owner: list[tuple[str, tuple[str, ...]]] = []
    boundary_classes_by_owner: list[tuple[str, tuple[BoundaryClass, ...]]] = []
    conflict_evidence: list[Evidence] = []
    for owner in sorted(item.owner for item in structures):
        classified_items: list[tuple[DictionaryConversionObservation, FlowEdge, BoundaryClass]] = []
        for conversion, flow in conversions:
            if conversion.owner != owner or conversion.serialization_format is None:
                continue
            classification, conflict = _persistence_classification(
                owner, conversion.target_identity, rules
            )
            if conflict:
                conflict_evidence.extend(flow.evidence)
            elif classification is not None:
                classified_items.append((conversion, flow, classification))
        classified = tuple(classified_items)
        if not classified:
            continue
        contract_id = _stable_public_id("contract", ("dictionary", owner))
        defining_subject_id = _dictionary_subject_id(owner)
        definition_boundary = ContractBoundary(
            boundary_id=_stable_public_id(
                "boundary", (contract_id, BoundaryClass.MODULE_LOCAL.value, "definition")
            ),
            boundary_class=BoundaryClass.MODULE_LOCAL,
            direction=BoundaryDirection.DEFINITION,
            site_subject_id=defining_subject_id,
            evidence=subject_evidence[owner],
            confidence=Confidence.EXACT,
        )
        classified_boundaries: list[ContractBoundary] = []
        persistence_boundary_ids: list[str] = []
        serialization_sites: list[Evidence] = []
        formats: set[str] = set()
        classified_classes: set[BoundaryClass] = set()
        for conversion, flow, boundary_class in classified:
            evidence = flow.evidence[0]
            site_logical_name = (
                f"{owner}:{conversion.target_identity}@{conversion.span.logical_path}:"
                f"{conversion.span.start_line}:{conversion.span.start_column}"
            )
            site_subject_id = _stable_public_id(
                "subject", ("dictionary-boundary", boundary_class.value, site_logical_name)
            )
            subjects.append(
                AnalysisSubject(
                    subject_id=site_subject_id,
                    kind=SubjectKind.BOUNDARY,
                    logical_name=site_logical_name,
                    module=owner.split(":", 1)[0],
                    evidence=(evidence,),
                )
            )
            boundary = ContractBoundary(
                boundary_id=_stable_public_id(
                    "boundary",
                    (contract_id, boundary_class.value, site_logical_name),
                ),
                boundary_class=boundary_class,
                direction=BoundaryDirection.PRODUCER,
                site_subject_id=site_subject_id,
                evidence=(evidence,),
                confidence=Confidence.RESOLVED,
                limitations=(
                    (
                        "Configured classification identifies a persistence boundary but does "
                        "not prove successful runtime I/O."
                        if boundary_class is BoundaryClass.PERSISTED_DOCUMENT
                        else "Configured classification establishes static boundary intent but "
                        "does not prove runtime behavior."
                    ),
                ),
            )
            classified_boundaries.append(boundary)
            classified_classes.add(boundary_class)
            if boundary_class is BoundaryClass.PERSISTED_DOCUMENT:
                persistence_boundary_ids.append(boundary.boundary_id)
            serialization_sites.append(evidence)
            assert conversion.serialization_format is not None
            formats.add(conversion.serialization_format)
        ordered_boundaries = tuple(sorted(classified_boundaries, key=lambda item: item.boundary_id))
        contracts.append(
            ContractDefinition(
                contract_id=contract_id,
                kind=ContractKind.DICTIONARY,
                defining_subject_id=defining_subject_id,
                boundaries=tuple(
                    sorted(
                        (definition_boundary, *ordered_boundaries),
                        key=lambda item: item.boundary_id,
                    )
                ),
                serialization_formats=tuple(sorted(formats)),
                serialization_sites=_unique_evidence(serialization_sites),
                evidence=subject_evidence[owner],
                unresolved_behavior=(
                    "Runtime persistence success, durability, and read-back behavior are "
                    "unmeasured.",
                ),
            )
        )
        boundary_ids_by_owner.append((owner, tuple(sorted(persistence_boundary_ids))))
        boundary_classes_by_owner.append(
            (owner, tuple(sorted(classified_classes, key=lambda item: item.value)))
        )
    return _DictionaryPersistenceRecords(
        subjects=tuple(sorted(subjects, key=lambda item: item.subject_id)),
        contracts=tuple(sorted(contracts, key=lambda item: item.contract_id)),
        boundary_ids_by_owner=tuple(boundary_ids_by_owner),
        boundary_classes_by_owner=tuple(boundary_classes_by_owner),
        conflict_evidence=_unique_evidence(conflict_evidence),
    )


def _candidate_allowed_at_boundaries(
    candidate: str, boundary_classes: tuple[BoundaryClass, ...]
) -> bool:
    if candidate == "local-implementation-dictionary" or not boundary_classes:
        return True
    eligible = {
        BoundaryClass.INTERNAL_RECORD,
        BoundaryClass.INTERNAL_MUTABLE_INDEX,
        BoundaryClass.CONFIGURATION_MERGE,
        BoundaryClass.CROSS_SUBSYSTEM_INTERNAL,
        BoundaryClass.UNCLASSIFIED_INTERNAL,
    }
    return bool(set(boundary_classes) & eligible)


def _persistence_classification(
    owner: str,
    target_identity: str,
    rules: tuple[ContractBoundaryRule, ...],
) -> tuple[BoundaryClass | None, bool]:
    matches = {
        rule.boundary_class
        for rule in rules
        if fnmatchcase(owner, rule.contract_pattern)
        and fnmatchcase(target_identity, rule.site_pattern)
    }
    if len(matches) > 1:
        return None, True
    return (next(iter(matches)) if matches else None), False


def _public_dictionary_shape(
    result: DictionaryShapeBuildResult,
    shape: DictionaryShapeJoinInput,
    source_hashes: dict[str, str],
) -> DictionaryShape:
    shape_id = _stable_public_id("shape", ("dictionary", shape.owner))
    transitions = tuple(item for item in result.transitions if item.owner == shape.owner)
    state_names = tuple(
        sorted({state for variant in shape.variants for state in variant.lifecycle_states})
    )
    lifecycle_states = tuple(
        sorted(
            (
                ShapeLifecycleState(
                    state_id=_stable_public_id("state", (shape_id, state)),
                    name=state,
                    evidence=_unique_evidence(
                        [
                            _dictionary_evidence(
                                transition.span,
                                source_hashes,
                                "exact dictionary lifecycle transition",
                            )
                            for transition in transitions
                            if state
                            in {
                                f"{transition.field_name}={transition.from_value_json}",
                                f"{transition.field_name}={transition.to_value_json}",
                            }
                        ]
                    ),
                )
                for state in state_names
            ),
            key=lambda item: item.state_id,
        )
    )
    variants = tuple(
        sorted(
            (
                _public_dictionary_variant(
                    shape_id,
                    variant,
                    lifecycle_states,
                    source_hashes,
                )
                for variant in shape.variants
            ),
            key=lambda item: item.variant_id,
        )
    )
    evidence = _unique_evidence(
        [evidence for variant in variants for evidence in variant.evidence]
        + [evidence for state in lifecycle_states for evidence in state.evidence]
    )
    limitations = (
        ("Some dictionary observations could not be assigned to a named field.",)
        if shape.owner in set(result.unresolved_owners)
        else ()
    )
    return DictionaryShape(
        shape_id=shape_id,
        owner_subject_id=_dictionary_subject_id(shape.owner),
        lifecycle_states=lifecycle_states,
        variants=variants,
        unknown_field_policy=UnknownFieldPolicy.OBSERVED,
        producer_subject_ids=tuple(
            sorted(_dictionary_subject_id(identity) for identity in shape.producers)
        ),
        consumer_subject_ids=tuple(
            sorted(_dictionary_subject_id(identity) for identity in shape.consumers)
        ),
        evidence=evidence,
        confidence=Confidence.RESOLVED,
        limitations=limitations,
    )


def _public_dictionary_variant(
    shape_id: str,
    variant: DictionaryVariantJoinInput,
    lifecycle_states: tuple[ShapeLifecycleState, ...],
    source_hashes: dict[str, str],
) -> ShapeVariant:
    fields = tuple(
        _public_dictionary_field(shape_id, variant.name, field, source_hashes)
        for field in variant.fields
    )
    evidence = _unique_evidence([item for field in fields for item in field.evidence])
    discriminator = None
    if variant.discriminator_field is not None:
        discriminator_evidence = _unique_evidence(
            [
                item
                for field in fields
                if field.name == variant.discriminator_field
                for item in field.evidence
            ]
        )
        discriminator = ShapeDiscriminator(
            field_name=variant.discriminator_field,
            values=tuple(
                sorted(
                    (
                        JsonLiteralValue(value=json.loads(value))
                        for value in variant.discriminator_values_json
                    ),
                    key=lambda item: item.canonical_json(),
                )
            ),
            evidence=discriminator_evidence,
        )
    states_by_name = {state.name: state.state_id for state in lifecycle_states}
    return ShapeVariant(
        variant_id=_stable_public_id("variant", (shape_id, variant.name)),
        name=variant.name,
        discriminator=discriminator,
        lifecycle_state_ids=tuple(
            sorted(states_by_name[name] for name in variant.lifecycle_states)
        ),
        fields=fields,
        evidence=evidence,
        confidence=Confidence.RESOLVED,
    )


def _public_dictionary_field(
    shape_id: str,
    variant_name: str,
    field: DictionaryFieldFact,
    source_hashes: dict[str, str],
) -> ShapeField:
    requiredness = tuple(
        sorted(
            (
                FieldRequiredness(
                    kind=RequirednessKind(item.kind.value),
                    condition=item.condition,
                    evidence=(
                        _dictionary_evidence(
                            item.span,
                            source_hashes,
                            "dictionary field requiredness",
                        ),
                    ),
                )
                for item in field.requiredness_observations
            ),
            key=lambda item: (
                item.kind.value,
                item.condition or "",
                item.evidence[0].span.path,
                item.evidence[0].span.start_line,
                item.evidence[0].span.start_column,
            ),
        )
    )
    producer_defaults = tuple(
        sorted(
            (
                _public_dictionary_default(
                    shape_id,
                    variant_name,
                    field.name,
                    "producer",
                    position,
                    item,
                    source_hashes,
                )
                for position, item in enumerate(field.producer_defaults)
            ),
            key=lambda item: item.default_id,
        )
    )
    consumer_fallbacks = tuple(
        sorted(
            (
                _public_dictionary_default(
                    shape_id,
                    variant_name,
                    field.name,
                    "consumer",
                    position,
                    item,
                    source_hashes,
                )
                for position, item in enumerate(field.consumer_fallbacks)
            ),
            key=lambda item: item.default_id,
        )
    )
    operation_evidence = [
        _dictionary_evidence(
            item.span,
            source_hashes,
            f"{item.kind.value} dictionary field operation",
        )
        for item in field.operations
    ]
    evidence = _unique_evidence(
        operation_evidence
        + [item for value in requiredness for item in value.evidence]
        + [item for value in (*producer_defaults, *consumer_fallbacks) for item in value.evidence]
    )
    mutations = _unique_evidence(
        [
            evidence_item
            for operation, evidence_item in zip(field.operations, operation_evidence, strict=True)
            if operation.mutation_timing.value != "not-mutation"
        ]
    )
    coercions = tuple(
        sorted({item.coercion for item in field.operations if item.coercion is not None})
    )
    return ShapeField(
        name=field.name,
        type_expression=_type_expression(field.type_reprs),
        requiredness_observations=requiredness,
        producer_defaults=producer_defaults,
        consumer_fallbacks=consumer_fallbacks,
        coercions=coercions,
        mutations=mutations,
        evidence=evidence,
        confidence=(Confidence.EXACT if field.type_reprs else Confidence.INFERRED),
    )


def _public_dictionary_default(
    shape_id: str,
    variant_name: str,
    field_name: str,
    role: str,
    position: int,
    item: DictionaryDefaultObservation,
    source_hashes: dict[str, str],
) -> DefaultValue:
    evidence = _dictionary_evidence(
        item.span,
        source_hashes,
        f"{role} dictionary default",
    )
    return DefaultValue(
        default_id=_stable_public_id(
            "default",
            (
                shape_id,
                variant_name,
                field_name,
                role,
                str(position),
                item.kind.value,
                item.literal_value_json or "",
                item.detail or "",
            ),
        ),
        kind=DefaultKind(item.kind.value),
        json_literal=(
            JsonLiteralValue(value=json.loads(item.literal_value_json))
            if item.literal_value_json is not None
            else None
        ),
        detail=item.detail,
        evidence=(evidence,),
    )


def _public_dictionary_observation(
    item: DictionaryOperationObservation,
    source_hashes: dict[str, str],
    shape_ids: dict[str, str],
    variant_ids: dict[tuple[str, str], str],
) -> DictionaryObservation:
    shape_id = shape_ids.get(item.owner)
    variant_id = variant_ids.get((item.owner, item.variant)) if item.variant is not None else None
    owner_variants = [value for (owner, _name), value in variant_ids.items() if owner == item.owner]
    if variant_id is None and shape_id is not None and len(owner_variants) == 1:
        variant_id = owner_variants[0]
    evidence = _dictionary_evidence(
        item.span,
        source_hashes,
        f"{item.kind.value} dictionary operation",
    )
    return DictionaryObservation(
        observation_id=_stable_public_id(
            "dict-observation",
            (
                item.owner,
                item.kind.value,
                item.span.logical_path,
                str(item.span.start_line),
                str(item.span.start_column),
                item.variant or "",
                item.key_repr or "",
            ),
        ),
        subject_id=_dictionary_subject_id(item.owner),
        shape_id=shape_id,
        variant_id=variant_id,
        key_kind=DictionaryKeyKind(item.key_kind.value),
        key_repr=item.key_repr,
        key_type=(_type_expression((item.key_type_repr,)) if item.key_type_repr else None),
        operation=DictionaryOperationKind(item.kind.value),
        mutation_timing=MutationTiming(item.mutation_timing.value),
        coercion=item.coercion,
        predicate=(DictionaryPredicate(item.predicate.value) if item.predicate else None),
        evidence=(evidence,),
        confidence=Confidence.EXACT,
    )


def _public_dictionary_flow(
    item: DictionaryFlowObservation,
    source_hashes: dict[str, str],
) -> FlowEdge:
    return FlowEdge(
        flow_id=_stable_public_id(
            "flow",
            (
                item.source_owner,
                item.target_identity,
                item.kind.value,
                item.span.logical_path,
                str(item.span.start_line),
                str(item.span.start_column),
            ),
        ),
        source_id=_dictionary_subject_id(item.source_owner),
        target_id=_dictionary_subject_id(item.target_identity),
        kind=FlowKind(item.kind.value),
        resolution=ResolutionState.RESOLVED,
        evidence=(
            _dictionary_evidence(
                item.span,
                source_hashes,
                f"resolved {item.kind.value} dictionary flow",
            ),
        ),
        confidence=Confidence.RESOLVED,
    )


def _public_conversion_flow(
    item: DictionaryConversionObservation,
    source_hashes: dict[str, str],
) -> FlowEdge:
    return FlowEdge(
        flow_id=_stable_public_id(
            "flow",
            (
                item.owner,
                item.target_identity,
                item.kind.value,
                item.serialization_format or "",
                item.span.logical_path,
                str(item.span.start_line),
                str(item.span.start_column),
            ),
        ),
        source_id=_dictionary_subject_id(item.owner),
        target_id=_dictionary_subject_id(item.target_identity),
        kind=FlowKind(item.kind.value),
        resolution=ResolutionState.RESOLVED,
        evidence=(
            _dictionary_evidence(
                item.span,
                source_hashes,
                (
                    f"exact {item.serialization_format} serialization call"
                    if item.serialization_format
                    else "exact dictionary conversion call"
                ),
            ),
        ),
        confidence=Confidence.EXACT,
    )


def _public_unresolved_dictionary_flow(
    item: DictionaryFlowObservation,
    source_hashes: dict[str, str],
) -> UnresolvedEdge:
    return UnresolvedEdge(
        unresolved_id=_stable_public_id(
            "unresolved",
            (
                item.source_owner,
                item.target_identity,
                item.kind.value,
                item.resolution.value,
                item.span.logical_path,
                str(item.span.start_line),
                str(item.span.start_column),
            ),
        ),
        source_id=_dictionary_subject_id(item.source_owner),
        attempted_target=item.target_identity,
        operation=f"dictionary-{item.kind.value}",
        reason=item.reason or item.resolution.value,
        effect_on_completeness=True,
        evidence=(
            _dictionary_evidence(
                item.span,
                source_hashes,
                f"{item.resolution.value} {item.kind.value} dictionary flow",
            ),
        ),
    )


def _public_validation_pattern(
    item: StructuralValidationPattern,
    source_hashes: dict[str, str],
) -> ValidationPattern:
    subject_ids = tuple(sorted({_dictionary_subject_id(site.owner) for site in item.sites}))
    sites = _unique_evidence(
        [
            _dictionary_evidence(
                site.span,
                source_hashes,
                f"{item.kind.value} structural validation",
            )
            for site in item.sites
        ]
    )
    return ValidationPattern(
        validation_id=_stable_public_id(
            "validation",
            (
                "structural",
                item.kind.value,
                item.rule,
                *subject_ids,
            ),
        ),
        classification="structural",
        rule=item.rule,
        field_identity=item.field_identity,
        subject_ids=subject_ids,
        sites=sites,
        divergent_enforcement=item.divergent_enforcement,
        fail_open=item.fail_open,
        limitations=item.limitations,
    )


def _operational_validation_subjects(
    patterns: tuple[OperationalValidationPattern, ...],
    source_hashes: dict[str, str],
) -> tuple[AnalysisSubject, ...]:
    evidence_by_owner: dict[str, list[Evidence]] = {}
    for pattern in patterns:
        for site in pattern.sites:
            evidence_by_owner.setdefault(site.owner, []).append(
                _dictionary_evidence(
                    site.span,
                    source_hashes,
                    f"{site.kind.value} operational validation",
                )
            )
    return tuple(
        AnalysisSubject(
            subject_id=_dictionary_subject_id(owner),
            kind=SubjectKind.SYMBOL,
            logical_name=owner,
            module=owner.split(":", 1)[0],
            evidence=_unique_evidence(evidence),
        )
        for owner, evidence in sorted(evidence_by_owner.items())
    )


def _public_operational_validation_pattern(
    item: OperationalValidationPattern,
    source_hashes: dict[str, str],
) -> ValidationPattern:
    subject_ids = tuple(sorted({_dictionary_subject_id(site.owner) for site in item.sites}))
    sites = _unique_evidence(
        [
            _dictionary_evidence(
                site.span,
                source_hashes,
                f"{item.kind.value} operational validation",
            )
            for site in item.sites
        ]
    )
    return ValidationPattern(
        validation_id=_stable_public_id(
            "validation",
            (
                "operational",
                item.kind.value,
                item.rule,
                *subject_ids,
            ),
        ),
        classification="operational",
        rule=item.rule,
        subject_ids=subject_ids,
        sites=sites,
        limitations=item.limitations,
    )


def _public_data_structure(
    item: DataStructureTopologyFact,
    source_hashes: dict[str, str],
    conversion_flows: tuple[FlowEdge, ...],
    persistence_boundary_ids: tuple[str, ...],
) -> DataStructureRecord:
    evidence = _unique_evidence(
        [
            Evidence(
                span=SourceSpan(
                    path=span.logical_path,
                    start_line=span.start_line,
                    start_column=span.start_column,
                    end_line=span.end_line,
                    end_column=span.end_column,
                ),
                provider="native-structures",
                observation="dictionary data-structure topology",
                confidence=Confidence.RESOLVED,
                source_sha256=source_hashes[span.logical_path],
            )
            for span in item.evidence
        ]
    )

    def subject_ids(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(_dictionary_subject_id(value) for value in values))

    return DataStructureRecord(
        structure_id=_stable_public_id(
            "structure",
            ("dictionary", item.owner, item.scope.value),
        ),
        owner_subject_id=_dictionary_subject_id(item.owner),
        scope=StructureScope(item.scope.value),
        canonical_type=TypeExpression(kind=TypeKind.MAPPING, name="dict"),
        constructor_subject_ids=subject_ids(item.constructor_owners),
        producer_subject_ids=subject_ids(item.producer_owners),
        reader_subject_ids=subject_ids(item.reader_owners),
        writer_subject_ids=subject_ids(item.writer_owners),
        mutator_subject_ids=subject_ids(item.mutator_owners),
        serializer_subject_ids=tuple(
            sorted(
                {flow.target_id for flow in conversion_flows if flow.kind is FlowKind.SERIALIZATION}
            )
        ),
        persistence_boundary_ids=persistence_boundary_ids,
        alias_subject_ids=subject_ids(item.alias_owners),
        conversion_flow_ids=tuple(sorted(flow.flow_id for flow in conversion_flows)),
        invariants=item.invariants,
        ordering_assumptions=item.ordering_assumptions,
        concurrency=item.concurrency,
        lifetime=item.lifetime,
        cleanup=item.cleanup,
        growth_indicators=item.growth_indicators,
        evidence=evidence,
        confidence=Confidence.RESOLVED,
        unresolved_behavior=item.unresolved_behavior,
    )


def _public_candidate_classification(
    item: DictionaryCandidateFact,
    source_hashes: dict[str, str],
) -> CandidateClassification:
    def evidence(spans: tuple[InternalSourceSpan, ...], observation: str) -> tuple[Evidence, ...]:
        return _unique_evidence(
            [
                Evidence(
                    span=SourceSpan(
                        path=span.logical_path,
                        start_line=span.start_line,
                        start_column=span.start_column,
                        end_line=span.end_line,
                        end_column=span.end_column,
                    ),
                    provider="native-candidates",
                    observation=observation,
                    confidence=Confidence(item.confidence.value),
                    source_sha256=source_hashes[span.logical_path],
                )
                for span in spans
            ]
        )

    return CandidateClassification(
        candidate_id=_stable_public_id(
            "candidate", ("dictionary", item.owner, item.candidate.value)
        ),
        subject_id=_dictionary_subject_id(item.owner),
        candidate=CandidateKind(item.candidate.value),
        supporting_evidence=evidence(
            item.supporting_evidence,
            f"{item.candidate.value} representation candidate",
        ),
        counter_evidence=evidence(item.counter_evidence, "candidate counter-evidence"),
        confidence=Confidence(item.confidence.value),
        limitations=item.limitations,
    )


def _validation_findings(
    patterns: tuple[ValidationPattern, ...],
    subjects: tuple[AnalysisSubject, ...],
    completeness: Completeness,
) -> tuple[Finding, ...]:
    subjects_by_id = {item.subject_id: item for item in subjects}
    findings: list[Finding] = []
    divergent_by_field: dict[str, list[ValidationPattern]] = {}
    for pattern in patterns:
        if pattern.classification != "structural":
            continue
        if pattern.divergent_enforcement and pattern.field_identity is not None:
            divergent_by_field.setdefault(pattern.field_identity, []).append(pattern)
            continue
        if len(pattern.sites) < 2:
            continue
        primary = subjects_by_id[pattern.subject_ids[0]]
        findings.append(
            Finding(
                finding_id=_stable_public_id(
                    "finding",
                    ("validation-sprawl-repeated", pattern.validation_id),
                ),
                rule_id="validation-sprawl-repeated",
                category="validation-sprawl",
                severity="info",
                title="Repeated structural validation",
                explanation=(
                    f"The same exact structural rule is checked at {len(pattern.sites)} sites."
                ),
                module=primary.module,
                symbol=primary.logical_name,
                subject_ids=pattern.subject_ids,
                evidence=pattern.sites,
                confidence=Confidence.EXACT,
                completeness=completeness,
                limitations=(
                    "Repeated checks may be intentional at independently owned boundaries.",
                ),
                likely_validation_needs=(
                    "Confirm whether each boundary must enforce the rule independently.",
                ),
            )
        )
    for field_identity, field_patterns in sorted(divergent_by_field.items()):
        if len(field_patterns) < 2:
            continue
        subject_ids = tuple(
            sorted({subject_id for item in field_patterns for subject_id in item.subject_ids})
        )
        evidence = _unique_evidence([site for item in field_patterns for site in item.sites])
        primary = subjects_by_id[subject_ids[0]]
        findings.append(
            Finding(
                finding_id=_stable_public_id(
                    "finding",
                    ("validation-divergence", field_identity),
                ),
                rule_id="validation-divergence",
                category="validation-sprawl",
                severity="warning",
                title="Divergent structural validation semantics",
                explanation=(
                    "The same dictionary field is checked by both key presence and "
                    "value truthiness semantics."
                ),
                module=primary.module,
                symbol=primary.logical_name,
                subject_ids=subject_ids,
                evidence=evidence,
                confidence=Confidence.INFERRED,
                completeness=completeness,
                limitations=(
                    "Presence and value-truthiness checks may intentionally enforce "
                    "different invariants.",
                ),
                likely_validation_needs=(
                    "Confirm accepted absent, null, false, zero, and empty-value behavior.",
                ),
            )
        )
    return tuple(sorted(findings, key=lambda item: item.finding_id))


def _conversion_findings(
    structures: tuple[DataStructureRecord, ...],
    flows: tuple[FlowEdge, ...],
    subjects: tuple[AnalysisSubject, ...],
    completeness: Completeness,
) -> tuple[Finding, ...]:
    flows_by_id = {item.flow_id: item for item in flows}
    subjects_by_id = {item.subject_id: item for item in subjects}
    findings: list[Finding] = []
    for structure in structures:
        conversion_flows = tuple(
            flows_by_id[flow_id]
            for flow_id in structure.conversion_flow_ids
            if flow_id in flows_by_id
        )
        if len(conversion_flows) < 2:
            continue
        owner = subjects_by_id[structure.owner_subject_id]
        findings.append(
            Finding(
                finding_id=_stable_public_id(
                    "finding",
                    ("conversion-churn", structure.structure_id),
                ),
                rule_id="conversion-churn-repeated",
                category="conversion-churn",
                severity="info",
                title="Repeated conversion or serialization sites",
                explanation=(
                    f"The same evidenced dictionary structure reaches "
                    f"{len(conversion_flows)} conversion or serialization calls."
                ),
                module=owner.module,
                symbol=owner.logical_name,
                subject_ids=tuple(
                    sorted(
                        {
                            structure.owner_subject_id,
                            *(flow.target_id for flow in conversion_flows),
                        }
                    )
                ),
                affected_structure_ids=(structure.structure_id,),
                evidence=_unique_evidence(
                    [evidence for flow in conversion_flows for evidence in flow.evidence]
                ),
                confidence=Confidence.INFERRED,
                completeness=completeness,
                limitations=(
                    "Static call counts do not establish runtime frequency or measurable cost.",
                ),
                likely_validation_needs=(
                    "Measure runtime frequency and confirm representation ownership before "
                    "refactoring.",
                ),
            )
        )
    return tuple(sorted(findings, key=lambda item: item.finding_id))


def _dictionary_subject_id(identity: str) -> str:
    return _stable_public_id("subject", ("dictionary", identity))


def _dictionary_evidence(
    span: InternalSourceSpan,
    source_hashes: dict[str, str],
    observation: str,
) -> Evidence:
    return Evidence(
        span=SourceSpan(
            path=span.logical_path,
            start_line=span.start_line,
            start_column=span.start_column,
            end_line=span.end_line,
            end_column=span.end_column,
        ),
        provider="native-dictionary",
        observation=observation,
        confidence=Confidence.EXACT,
        source_sha256=source_hashes[span.logical_path],
    )


def _unique_evidence(values: list[Evidence]) -> tuple[Evidence, ...]:
    unique = {
        (
            item.span.path,
            item.span.start_line,
            item.span.start_column,
            item.span.end_line,
            item.span.end_column,
            item.provider,
            item.observation,
            item.confidence.value,
            item.source_sha256,
        ): item
        for item in values
    }
    return tuple(unique[key] for key in sorted(unique))


def _type_expression(values: tuple[str, ...]) -> TypeExpression:
    if not values:
        return TypeExpression(kind=TypeKind.UNKNOWN)
    expressions = tuple(_single_type_expression(value) for value in sorted(set(values)))
    if len(expressions) == 1:
        return expressions[0]
    return TypeExpression(kind=TypeKind.UNION, arguments=expressions)


def _single_type_expression(value: str) -> TypeExpression:
    if value in {"bool", "bytes", "float", "int", "None", "str"}:
        return TypeExpression(kind=TypeKind.SCALAR, name=value)
    if value in {"dict", "mapping"}:
        return TypeExpression(kind=TypeKind.MAPPING, name=value)
    if value in {"list", "set", "tuple"}:
        return TypeExpression(kind=TypeKind.CONTAINER, name=value)
    return TypeExpression(kind=TypeKind.NAMED, name=value)


def _dictionary_run_identity(
    report: AnalysisReport,
    persistence_contracts: tuple[ContractDefinition, ...],
    shapes: tuple[DictionaryShape, ...],
    observations: tuple[DictionaryObservation, ...],
    flows: tuple[FlowEdge, ...],
    validation_patterns: tuple[ValidationPattern, ...],
    data_structures: tuple[DataStructureRecord, ...],
    candidate_classifications: tuple[CandidateClassification, ...],
    unresolved: tuple[UnresolvedEdge, ...],
) -> str:
    payload = {
        "analyzer": "native-dictionary@1",
        "base": report.run_identity_sha256,
        "flows": tuple(item.flow_id for item in flows),
        "observations": tuple(item.observation_id for item in observations),
        "shapes": tuple(item.shape_id for item in shapes),
        "unresolved": tuple(item.unresolved_id for item in unresolved),
        "validations": tuple(item.validation_id for item in validation_patterns),
        "structures": tuple(item.structure_id for item in data_structures),
    }
    if persistence_contracts:
        payload["persistence_contracts"] = tuple(item.contract_id for item in persistence_contracts)
    if candidate_classifications:
        payload["candidates"] = tuple(item.candidate_id for item in candidate_classifications)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_public_id(prefix: str, parts: tuple[str, ...]) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:20]}"
