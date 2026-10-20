"""Deterministic responsibility clustering and bounded refactoring proposals."""

from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase

from pydantic import ValidationError

from code_analysis.analysis.assembly import AssemblyFailureFact, validation_failure
from code_analysis.behavior_reporting import evaluate_candidate_behavior
from code_analysis.config import ArchitectureLayer, PhaseThreeLimits
from code_analysis.context import AnalysisContext
from code_analysis.cost_reporting import cost_gate, estimate_candidate_cost
from code_analysis.graph import canonical_cycles, strongly_connected_components
from code_analysis.models import (
    Completeness,
    Confidence,
    Diagnostic,
    DiagnosticLevel,
    Evidence,
    ProviderResult,
    ProviderState,
    SourceLanguage,
    SourceSpan,
    evidence_identity,
)
from code_analysis.report_models import (
    AbstractionCostEstimate,
    AnalysisReport,
    AnalysisSubject,
    ArchitectureContractSuggestion,
    ArchitectureEstimate,
    ArchitectureMetrics,
    BehavioralEvent,
    BehavioralInvariant,
    CandidateBehaviorDifference,
    CloneCluster,
    CloneFragment,
    CloneKind,
    CommonalityDimensionKind,
    Finding,
    FunctionCommonality,
    OwnerAlternative,
    ProposalAdmissionFunnel,
    ProposalAdmissionRejectionReason,
    ProposalCandidateKind,
    ProposalDecisionFunnel,
    ProposalGate,
    ProposalGateDecision,
    ProposalGateOutcome,
    ProposalRejectionExample,
    RefactorCandidate,
    RefactorCandidateKind,
    RefactorCandidateObservation,
    RefactorCandidateState,
    RefactorProposal,
    RefactorProposalKind,
    RefactorProposalState,
    ResponsibilityCluster,
    SectionAssemblyFailure,
    SectionCoverage,
    SectionState,
    SignalQualitySummary,
    SummaryCounts,
)


class _CounterEvidenceKind(StrEnum):
    INCOMPLETE_CALLS = "incomplete-resolved-calls"
    DIVERGENT_EXCEPTIONS = "divergent-exceptions"
    INCOMPATIBLE_CONTRACTS = "incompatible-contracts"
    DIVERGENT_SIDE_EFFECTS = "divergent-side-effects"
    OTHER_OWNERSHIP = "other-unresolved-ownership"


_COUNTER_EVIDENCE_KINDS = {
    "one or both call sets are incomplete": _CounterEvidenceKind.INCOMPLETE_CALLS,
    "different exception behavior is syntax-evidenced": _CounterEvidenceKind.DIVERGENT_EXCEPTIONS,
    "functions reference incompatible contracts": _CounterEvidenceKind.INCOMPATIBLE_CONTRACTS,
    "different side-effect features are syntax-evidenced": (
        _CounterEvidenceKind.DIVERGENT_SIDE_EFFECTS
    ),
}


def _counter_evidence_kind(evidence: Evidence) -> _CounterEvidenceKind:
    return _COUNTER_EVIDENCE_KINDS.get(evidence.observation, _CounterEvidenceKind.OTHER_OWNERSHIP)


def _ownership_counter_reason(counter: tuple[Evidence, ...]) -> str:
    kinds = {_counter_evidence_kind(item) for item in counter}
    causes = tuple(
        label
        for kind, label in (
            (_CounterEvidenceKind.INCOMPLETE_CALLS, "resolved-call evidence is incomplete"),
            (_CounterEvidenceKind.DIVERGENT_EXCEPTIONS, "exception behavior diverges"),
            (_CounterEvidenceKind.OTHER_OWNERSHIP, "other ownership evidence remains unresolved"),
        )
        if kind in kinds
    )
    if not causes:
        return "Threshold-qualified ownership dimensions support the responsibility boundary."
    if len(causes) == 1:
        return {
            _CounterEvidenceKind.INCOMPLETE_CALLS: (
                "Incomplete resolved-call evidence prevents complete ownership evaluation."
            ),
            _CounterEvidenceKind.DIVERGENT_EXCEPTIONS: (
                "Divergent exception behavior conflicts with common ownership."
            ),
            _CounterEvidenceKind.OTHER_OWNERSHIP: (
                "Other unresolved ownership evidence prevents complete ownership evaluation."
            ),
        }[next(iter(kinds))]
    return "Ownership remains unresolved: " + "; ".join(causes) + "."


def _call_resolution_only_counter(
    responsibility: ResponsibilityCluster,
) -> tuple[Evidence, ...]:
    counter = responsibility.counter_evidence
    if counter and {_counter_evidence_kind(item) for item in counter} == {
        _CounterEvidenceKind.INCOMPLETE_CALLS
    }:
        return counter
    return ()


def _proposal_rejection_example(
    responsibility: ResponsibilityCluster,
    kind: ProposalCandidateKind,
    affected_modules: tuple[str, ...],
    evidence: tuple[Evidence, ...],
    reason: ProposalAdmissionRejectionReason = (
        ProposalAdmissionRejectionReason.INCOMPLETE_CALL_RESOLUTION_ONLY
    ),
) -> ProposalRejectionExample:
    return ProposalRejectionExample(
        candidate_id=_stable_id("proposal-candidate", kind.value, responsibility.responsibility_id),
        kind=kind,
        responsibility_id=responsibility.responsibility_id,
        affected_subject_ids=responsibility.member_subject_ids,
        affected_modules=affected_modules,
        reason=reason,
        evidence=evidence,
    )


PHASE_FOUR_LIMITATION = "Phase 4 test-impact evidence is unavailable."
PROPOSAL_LIMITATIONS = (
    PHASE_FOUR_LIMITATION,
    "Static graph simulation does not establish runtime behavior or authorize source changes.",
)

OWNERSHIP_DIMENSIONS = frozenset(
    {
        CommonalityDimensionKind.CALLEES,
        CommonalityDimensionKind.ATTRIBUTES_GLOBALS,
        CommonalityDimensionKind.EXCEPTIONS,
        CommonalityDimensionKind.SIDE_EFFECTS,
        CommonalityDimensionKind.DICTIONARY_SCHEMA,
        CommonalityDimensionKind.CONTRACTS,
    }
)
STRUCTURAL_CLONE_KINDS = frozenset(
    {
        CloneKind.EXACT_TEXT,
        CloneKind.NORMALIZED_TOKEN,
        CloneKind.NORMALIZED_AST,
        CloneKind.STATEMENT_SEQUENCE,
        CloneKind.CONTROL_CALL_SEQUENCE,
    }
)
_MAX_PROPOSAL_COMPONENT_MEMBERS = 12


@dataclass(frozen=True, slots=True)
class GraphGateFacts:
    edge_reduction: bool
    scc_safe: bool
    cycle_safe: bool
    layer_safe: bool

    @property
    def accepted(self) -> bool:
        return all((self.edge_reduction, self.scc_safe, self.cycle_safe, self.layer_safe))


def _stable_id(prefix: str, *parts: str) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def _candidate_id(
    kind: RefactorCandidateKind,
    languages: tuple[SourceLanguage, ...],
    subject_ids: tuple[str, ...],
) -> str:
    return _stable_id(
        "refactor-candidate",
        kind.value,
        *(item.value for item in languages),
        *subject_ids,
    )


def _candidate_state(proposals: tuple[RefactorProposal, ...]) -> RefactorCandidateState:
    if any(item.state is RefactorProposalState.READY_FOR_MAINTAINER_REVIEW for item in proposals):
        return RefactorCandidateState.READY_FOR_MAINTAINER_REVIEW
    if any(item.state is RefactorProposalState.REVIEWABLE_INCOMPLETE for item in proposals):
        return RefactorCandidateState.REVIEWABLE_INCOMPLETE
    return RefactorCandidateState.BLOCKED if proposals else RefactorCandidateState.OBSERVED


def _components(nodes: set[str], edges: set[tuple[str, str]]) -> tuple[tuple[str, ...], ...]:
    adjacency: dict[str, set[str]] = {node: set() for node in nodes}
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)
    found: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for start in sorted(nodes):
        if start in seen:
            continue
        pending = [start]
        component: set[str] = set()
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(sorted(adjacency[current] - component, reverse=True))
        seen.update(component)
        if len(component) >= 2:
            found.append(tuple(sorted(component)))
    return tuple(sorted(found))


def _has_qualifying_structural_clone(
    commonality: FunctionCommonality,
    clusters: dict[str, CloneCluster],
    fragments: dict[str, CloneFragment],
    minimum_nonblank_lines: int,
) -> bool:
    pair = {commonality.left_subject_id, commonality.right_subject_id}
    for cluster_id in commonality.clone_cluster_ids:
        cluster = clusters[cluster_id]
        if cluster.kind not in STRUCTURAL_CLONE_KINDS:
            continue
        pair_fragments = tuple(
            fragments[fragment_id]
            for fragment_id in cluster.fragment_ids
            if fragments[fragment_id].subject_id in pair
        )
        if {fragment.subject_id for fragment in pair_fragments} != pair:
            continue
        if all(fragment.nonblank_lines >= minimum_nonblank_lines for fragment in pair_fragments):
            return True
    return False


def _qualifies_for_responsibility(
    commonality: FunctionCommonality,
    limits: PhaseThreeLimits,
    clusters: dict[str, CloneCluster],
    fragments: dict[str, CloneFragment],
    subjects: dict[str, AnalysisSubject],
) -> bool:
    structural_clone_qualifies = _has_qualifying_structural_clone(
        commonality,
        clusters,
        fragments,
        limits.min_responsibility_clone_nonblank_lines,
    )
    pair_subjects = (
        subjects[commonality.left_subject_id],
        subjects[commonality.right_subject_id],
    )
    both_private_helpers = all(
        subject.logical_name.rsplit(":", 1)[-1].rsplit(".", 1)[-1].startswith("_")
        for subject in pair_subjects
    )
    if commonality.clone_cluster_ids and both_private_helpers and not structural_clone_qualifies:
        return False
    ownership_count = sum(
        dimension.kind in OWNERSHIP_DIMENSIONS for dimension in commonality.dimensions
    )
    if ownership_count >= limits.min_responsibility_ownership_dimensions:
        return True
    return ownership_count >= 1 and structural_clone_qualifies


def _layer_for(module: str, layers: tuple[ArchitectureLayer, ...]) -> str | None:
    matches = [
        layer.name
        for layer in layers
        if any(fnmatchcase(module, pattern) for pattern in layer.module_patterns)
    ]
    return min(matches) if matches else None


def _layer_violations(
    edges: set[tuple[str, str]], layers: tuple[ArchitectureLayer, ...]
) -> tuple[tuple[str, str, str, str], ...]:
    by_name = {item.name: item for item in layers}
    violations = []
    for source, target in sorted(edges):
        source_layer = _layer_for(source, layers)
        target_layer = _layer_for(target, layers)
        if source_layer is None or target_layer is None or source_layer == target_layer:
            continue
        if target_layer not in by_name[source_layer].may_import_layers:
            violations.append((source, target, source_layer, target_layer))
    return tuple(violations)


def _graph_metrics(
    nodes: set[str],
    edges: set[tuple[str, str]],
    layers: tuple[ArchitectureLayer, ...],
    *,
    cross_boundary_edges: int,
    proposed_module: str | None,
) -> ArchitectureMetrics:
    components = strongly_connected_components(nodes, edges)
    cycles = canonical_cycles(nodes, edges)
    return ArchitectureMetrics(
        module_count=len(nodes),
        import_edge_count=len(edges),
        cross_boundary_edge_count=cross_boundary_edges,
        scc_count=sum(len(item) > 1 for item in components),
        cycle_count=len(cycles),
        layer_violation_count=len(_layer_violations(edges, layers)),
        proposed_module_fan_in=(
            sum(target == proposed_module for _source, target in edges)
            if proposed_module is not None
            else 0
        ),
        proposed_module_fan_out=(
            sum(source == proposed_module for source, _target in edges)
            if proposed_module is not None
            else 0
        ),
    )


def _graph_gate_facts(
    before_nodes: set[str],
    before_edges: set[tuple[str, str]],
    after_nodes: set[str],
    after_edges: set[tuple[str, str]],
    before: ArchitectureMetrics,
    after: ArchitectureMetrics,
    layers: tuple[ArchitectureLayer, ...],
) -> GraphGateFacts:
    return GraphGateFacts(
        edge_reduction=after.cross_boundary_edge_count < before.cross_boundary_edge_count,
        scc_safe=after.scc_count <= before.scc_count,
        cycle_safe=(
            after.cycle_count <= before.cycle_count
            and not (
                set(canonical_cycles(after_nodes, after_edges))
                - set(canonical_cycles(before_nodes, before_edges))
            )
        ),
        layer_safe=(
            after.layer_violation_count <= before.layer_violation_count
            and not (
                set(_layer_violations(after_edges, layers))
                - set(_layer_violations(before_edges, layers))
            )
        ),
    )


def _module_from_callee(callee: str, known_modules: set[str]) -> str | None:
    dotted = callee.split(":", 1)[0]
    candidates = [
        module for module in known_modules if dotted == module or dotted.startswith(module + ".")
    ]
    return max(candidates, key=len) if candidates else None


def _estimate_extraction(
    report: AnalysisReport,
    member_ids: tuple[str, ...],
    proposed_module: str,
    layers: tuple[ArchitectureLayer, ...],
    estimate_identity: str,
) -> tuple[ArchitectureEstimate, GraphGateFacts, tuple[str, ...], tuple[str, ...]]:
    subjects = {item.subject_id: item for item in report.subjects}
    profiles = {item.subject_id: item for item in report.function_profiles}
    known_modules = {item.name for item in report.modules}
    affected_modules = {subjects[item].module for item in member_ids}
    dependency_occurrences: list[str] = []
    for subject_id in member_ids:
        for callee in profiles[subject_id].callees:
            module = _module_from_callee(callee, known_modules)
            if module is not None and module not in affected_modules:
                dependency_occurrences.append(module)
    dependencies = tuple(sorted(set(dependency_occurrences)))
    consumers = tuple(sorted(affected_modules))
    before_cross = len(dependency_occurrences)
    after_cross = len(dependencies) + len(consumers)
    before_nodes = set(known_modules)
    before_edges = {
        (item.source, item.target)
        for item in report.import_edges
        if item.source in before_nodes and item.target in before_nodes
    }
    after_nodes = {*before_nodes, proposed_module}
    after_edges = {
        *before_edges,
        *((module, proposed_module) for module in affected_modules),
        *((proposed_module, module) for module in dependencies),
    }
    before = _graph_metrics(
        before_nodes,
        before_edges,
        layers,
        cross_boundary_edges=before_cross,
        proposed_module=None,
    )
    after = _graph_metrics(
        after_nodes,
        after_edges,
        layers,
        cross_boundary_edges=after_cross,
        proposed_module=proposed_module,
    )
    evidence = tuple(subjects[item].evidence[0] for item in member_ids)
    estimate = ArchitectureEstimate(
        estimate_id=_stable_id("architecture-estimate", "extract", estimate_identity, *member_ids),
        before=before,
        expected_after=after,
        assumptions=(
            "Affected modules become consumers of the proposed logical owner.",
            "Recorded resolved callees approximate extracted dependency edges.",
        ),
        evidence=evidence,
        completeness=Completeness.PARTIAL,
        limitations=PROPOSAL_LIMITATIONS,
    )
    gate_facts = _graph_gate_facts(
        before_nodes, before_edges, after_nodes, after_edges, before, after, layers
    )
    return estimate, gate_facts, dependencies, consumers


def _estimate_combination(
    report: AnalysisReport,
    modules: tuple[str, ...],
    proposed_module: str,
    evidence: tuple[Evidence, ...],
    layers: tuple[ArchitectureLayer, ...],
    estimate_identity: str,
) -> tuple[ArchitectureEstimate, GraphGateFacts, tuple[str, ...], tuple[str, ...]]:
    before_nodes = {item.name for item in report.modules}
    before_edges = {
        (item.source, item.target)
        for item in report.import_edges
        if item.source in before_nodes and item.target in before_nodes
    }
    replacements = dict.fromkeys(modules, proposed_module)
    after_nodes = (before_nodes - set(modules)) | {proposed_module}
    after_edges = {
        (replacements.get(source, source), replacements.get(target, target))
        for source, target in before_edges
        if replacements.get(source, source) != replacements.get(target, target)
    }
    before_cross = sum(
        (source in modules and target not in modules)
        or (target in modules and source not in modules)
        for source, target in before_edges
    )
    after_cross = sum(
        source == proposed_module or target == proposed_module for source, target in after_edges
    )
    before = _graph_metrics(
        before_nodes,
        before_edges,
        layers,
        cross_boundary_edges=before_cross,
        proposed_module=None,
    )
    after = _graph_metrics(
        after_nodes,
        after_edges,
        layers,
        cross_boundary_edges=after_cross,
        proposed_module=proposed_module,
    )
    estimate = ArchitectureEstimate(
        estimate_id=_stable_id("architecture-estimate", "combine", estimate_identity, *modules),
        before=before,
        expected_after=after,
        assumptions=(
            "Imports to combined modules are redirected to one logical owner.",
            "Internal edges between combined modules are removed.",
        ),
        evidence=evidence,
        completeness=Completeness.PARTIAL,
        limitations=PROPOSAL_LIMITATIONS,
    )
    gate_facts = _graph_gate_facts(
        before_nodes, before_edges, after_nodes, after_edges, before, after, layers
    )
    dependencies = tuple(
        sorted(target for source, target in after_edges if source == proposed_module)
    )
    consumers = tuple(sorted(source for source, target in after_edges if target == proposed_module))
    return estimate, gate_facts, dependencies, consumers


def _suggestions_and_findings(
    context: AnalysisContext, report: AnalysisReport
) -> tuple[tuple[ArchitectureContractSuggestion, ...], tuple[Finding, ...]]:
    modules = {item.name: item for item in report.modules}
    edges = {
        (item.source, item.target)
        for item in report.import_edges
        if item.source in modules and item.target in modules
    }
    suggestions: list[ArchitectureContractSuggestion] = []
    findings: list[Finding] = []
    for source, target, source_layer, target_layer in _layer_violations(
        edges, context.config.architecture_layers
    ):
        module = modules[source]
        evidence = Evidence(
            span=SourceSpan(
                path=module.path,
                language=module.language,
                start_line=1,
                start_column=0,
                end_line=1,
                end_column=0,
            ),
            provider="native-cohesion",
            observation=f"configured layer edge {source_layer} -> {target_layer}",
            language=module.language,
            confidence=Confidence.RESOLVED,
            source_sha256=module.source_sha256,
        )
        suggestion_id = _stable_id(
            "architecture-contract", "configured", source_layer, target_layer
        )
        suggestions.append(
            ArchitectureContractSuggestion(
                suggestion_id=suggestion_id,
                source_layer=source_layer,
                target_layer=target_layer,
                source_module_patterns=(source,),
                target_module_patterns=(target,),
                import_linter_text=(
                    "[importlinter:contract:phase3-"
                    f"{source_layer}-{target_layer}]\nname = Review {source_layer} to "
                    f"{target_layer}\ntype = forbidden\nsource_modules =\n    {source}\n"
                    f"forbidden_modules =\n    {target}"
                ),
                configured_rule=True,
                evidence=(evidence,),
                confidence=Confidence.RESOLVED,
                limitations=("Suggestion is report-only; no configuration was written.",),
            )
        )
        findings.append(
            Finding(
                finding_id=_stable_id("finding", "architecture-layer", source, target),
                rule_id="configured-layer-direction",
                category="architecture-contract",
                severity="warning",
                title="Configured architecture layer direction is violated",
                explanation=(
                    f"{source} imports {target}, which is not allowed by configured layers."
                ),
                module=source,
                language=module.language,
                subject_ids=(
                    min(
                        subject.subject_id
                        for subject in report.subjects
                        if subject.module == source
                    ),
                ),
                evidence=(evidence,),
                confidence=Confidence.RESOLVED,
                completeness=Completeness.COMPLETE,
                limitations=("Only configured layer rules produce violation findings.",),
                likely_validation_needs=("Review the configured layer ownership and import edge.",),
            )
        )
    for component in report.strongly_connected_components:
        if len(component.modules) < 2:
            continue
        first, last = component.modules[0], component.modules[-1]
        module = modules[first]
        evidence = Evidence(
            span=SourceSpan(
                path=module.path,
                language=module.language,
                start_line=1,
                start_column=0,
                end_line=1,
                end_column=0,
            ),
            provider="native-cohesion",
            observation="existing strongly connected import component",
            language=module.language,
            confidence=Confidence.EXACT,
            source_sha256=module.source_sha256,
        )
        suggestions.append(
            ArchitectureContractSuggestion(
                suggestion_id=_stable_id("architecture-contract", "native-scc", *component.modules),
                source_layer=first,
                target_layer=last,
                source_module_patterns=(first,),
                target_module_patterns=(last,),
                import_linter_text=(
                    f"# Review-only SCC boundary suggestion for {', '.join(component.modules)}"
                ),
                configured_rule=False,
                evidence=(evidence,),
                confidence=Confidence.INFERRED,
                limitations=(
                    "Native suggestion is not a configured rule and cannot produce a violation.",
                ),
            )
        )
    unique = {item.suggestion_id: item for item in suggestions}
    return (
        tuple(unique[key] for key in sorted(unique)),
        tuple(sorted(findings, key=lambda item: item.finding_id)),
    )


def _gate_decision(
    proposal_id: str,
    gate: ProposalGate,
    outcome: ProposalGateOutcome,
    reason: str,
    *,
    blocking: bool = False,
    affected_ids: tuple[str, ...] = (),
    supporting: tuple[Evidence, ...] = (),
    counter: tuple[Evidence, ...] = (),
) -> ProposalGateDecision:
    return ProposalGateDecision(
        decision_id=_stable_id("proposal-gate", proposal_id, gate.value),
        gate=gate,
        outcome=outcome,
        blocking=blocking,
        affected_ids=tuple(sorted(set(affected_ids))),
        supporting_catalog_ids=tuple(sorted({evidence_identity(item) for item in supporting})),
        counter_catalog_ids=tuple(sorted({evidence_identity(item) for item in counter})),
        reason=reason,
    )


def _proposal_gate_decisions(
    proposal_id: str,
    responsibility: ResponsibilityCluster,
    affected_modules: tuple[str, ...],
    graph: GraphGateFacts,
    estimate_id: str,
    abstraction_cost: AbstractionCostEstimate,
    behavior_outcome: ProposalGateOutcome,
    behavior_reason: str,
    behavior_difference_ids: tuple[str, ...],
) -> tuple[ProposalGateDecision, ...]:
    counter = responsibility.counter_evidence
    classified = tuple((_counter_evidence_kind(item), item) for item in counter)
    contract_counter = tuple(
        item for kind, item in classified if kind is _CounterEvidenceKind.INCOMPATIBLE_CONTRACTS
    )
    side_effect_counter = tuple(
        item for kind, item in classified if kind is _CounterEvidenceKind.DIVERGENT_SIDE_EFFECTS
    )
    ownership_counter = tuple(
        item
        for kind, item in classified
        if kind
        in {
            _CounterEvidenceKind.INCOMPLETE_CALLS,
            _CounterEvidenceKind.DIVERGENT_EXCEPTIONS,
            _CounterEvidenceKind.OTHER_OWNERSHIP,
        }
    )
    domains = {item.split(".", 1)[0] for item in affected_modules}
    support = responsibility.evidence
    cost_outcome, cost_blocking, cost_reason = cost_gate(abstraction_cost)
    return (
        _gate_decision(
            proposal_id,
            ProposalGate.OWNERSHIP,
            ProposalGateOutcome.UNRESOLVED if ownership_counter else ProposalGateOutcome.PASSED,
            _ownership_counter_reason(ownership_counter),
            blocking=bool(ownership_counter),
            affected_ids=responsibility.member_subject_ids,
            supporting=support,
            counter=ownership_counter,
        ),
        _gate_decision(
            proposal_id,
            ProposalGate.DOMAIN_BOUNDARY,
            ProposalGateOutcome.FAILED if len(domains) > 1 else ProposalGateOutcome.PASSED,
            (
                "Affected modules cross top-level domains."
                if len(domains) > 1
                else "Affected modules remain within one top-level domain."
            ),
            blocking=len(domains) > 1,
            affected_ids=affected_modules,
            supporting=support,
        ),
        _gate_decision(
            proposal_id,
            ProposalGate.CONTRACT_COMPATIBILITY,
            ProposalGateOutcome.FAILED if contract_counter else ProposalGateOutcome.PASSED,
            (
                "Referenced contracts are incompatible."
                if contract_counter
                else "No incompatible referenced contracts were evidenced."
            ),
            blocking=bool(contract_counter),
            affected_ids=responsibility.member_subject_ids,
            supporting=support,
            counter=contract_counter,
        ),
        _gate_decision(
            proposal_id,
            ProposalGate.MUTABLE_STATE,
            (
                ProposalGateOutcome.PASSED
                if responsibility.state_resource_ids
                else ProposalGateOutcome.NOT_APPLICABLE
            ),
            (
                "Referenced mutable-state resources retain compatible ownership."
                if responsibility.state_resource_ids
                else "No mutable-state resource is attached to this cluster."
            ),
            affected_ids=responsibility.state_resource_ids,
            supporting=support if responsibility.state_resource_ids else (),
        ),
        _gate_decision(
            proposal_id,
            ProposalGate.SIDE_EFFECTS,
            ProposalGateOutcome.FAILED if side_effect_counter else ProposalGateOutcome.PASSED,
            (
                "Divergent side-effect ownership blocks the proposal."
                if side_effect_counter
                else "No conflicting side-effect ownership was evidenced."
            ),
            blocking=bool(side_effect_counter),
            affected_ids=responsibility.side_effect_ids,
            supporting=support,
            counter=side_effect_counter,
        ),
        _gate_decision(
            proposal_id,
            ProposalGate.GRAPH_EDGE_REDUCTION,
            ProposalGateOutcome.PASSED if graph.edge_reduction else ProposalGateOutcome.FAILED,
            (
                "The simulated graph reduces cross-boundary edges."
                if graph.edge_reduction
                else "The simulated graph does not reduce cross-boundary edges."
            ),
            blocking=not graph.edge_reduction,
            affected_ids=(estimate_id,),
            supporting=support,
        ),
        _gate_decision(
            proposal_id,
            ProposalGate.SCC,
            ProposalGateOutcome.PASSED if graph.scc_safe else ProposalGateOutcome.FAILED,
            (
                "The simulated graph does not add an SCC."
                if graph.scc_safe
                else "The simulated graph adds an SCC."
            ),
            blocking=not graph.scc_safe,
            affected_ids=(estimate_id,),
            supporting=support,
        ),
        _gate_decision(
            proposal_id,
            ProposalGate.CYCLE,
            ProposalGateOutcome.PASSED if graph.cycle_safe else ProposalGateOutcome.FAILED,
            (
                "The simulated graph does not add a canonical cycle."
                if graph.cycle_safe
                else "The simulated graph adds a canonical cycle."
            ),
            blocking=not graph.cycle_safe,
            affected_ids=(estimate_id,),
            supporting=support,
        ),
        _gate_decision(
            proposal_id,
            ProposalGate.CONFIGURED_LAYER,
            ProposalGateOutcome.PASSED if graph.layer_safe else ProposalGateOutcome.FAILED,
            (
                "The simulated graph does not add a configured layer violation."
                if graph.layer_safe
                else "The simulated graph adds a configured layer violation."
            ),
            blocking=not graph.layer_safe,
            affected_ids=(estimate_id,),
            supporting=support,
        ),
        _gate_decision(
            proposal_id,
            ProposalGate.ABSTRACTION_COST,
            ProposalGateOutcome(cost_outcome),
            cost_reason,
            blocking=cost_blocking,
            affected_ids=(abstraction_cost.cost_estimate_id,),
            supporting=support,
        ),
        _gate_decision(
            proposal_id,
            ProposalGate.BEHAVIOR_PRESERVATION,
            behavior_outcome,
            behavior_reason,
            blocking=behavior_outcome
            in {ProposalGateOutcome.FAILED, ProposalGateOutcome.UNRESOLVED},
            affected_ids=behavior_difference_ids or responsibility.member_subject_ids,
            supporting=support,
        ),
        _gate_decision(
            proposal_id,
            ProposalGate.TEST_IMPACT,
            ProposalGateOutcome.UNRESOLVED,
            "Phase 4 affected-test evidence is unavailable.",
            affected_ids=responsibility.member_subject_ids,
        ),
        _gate_decision(
            proposal_id,
            ProposalGate.COVERAGE_BINDING,
            ProposalGateOutcome.UNRESOLVED,
            "Source-bound executed coverage evidence is unavailable.",
            affected_ids=responsibility.member_subject_ids,
        ),
        _gate_decision(
            proposal_id,
            ProposalGate.RUNTIME_CLAIM,
            ProposalGateOutcome.NOT_APPLICABLE,
            "The proposal makes no runtime-performance claim.",
        ),
    )


def _proposal_state(
    decisions: tuple[ProposalGateDecision, ...],
) -> tuple[RefactorProposalState, str]:
    blocking = next((item for item in decisions if item.blocking), None)
    if blocking is not None:
        return RefactorProposalState.BLOCKED_INSUFFICIENT_EVIDENCE, blocking.reason
    return (
        RefactorProposalState.REVIEWABLE_INCOMPLETE,
        "Static proposal gates passed; Phase 4 qualification evidence is required.",
    )


def integrate_cohesion_report(context: AnalysisContext, report: AnalysisReport) -> AnalysisReport:
    subjects = {item.subject_id: item for item in report.subjects}
    clusters = {item.cluster_id: item for item in report.clone_clusters}
    fragments = {item.fragment_id: item for item in report.clone_fragments}
    commonality_by_pair = {
        (item.left_subject_id, item.right_subject_id): item
        for item in report.function_commonalities
        if _qualifies_for_responsibility(
            item,
            context.config.phase_three_limits,
            clusters,
            fragments,
            subjects,
        )
    }
    edges = set(commonality_by_pair)
    components = _components({item for edge in edges for item in edge}, edges)
    responsibility_records: list[ResponsibilityCluster] = []
    assembly_failure_facts: list[AssemblyFailureFact] = []
    for component in components:
        if context.config.symbol_filter is not None and not all(
            context.config.symbol_filter in subjects[item].logical_name for item in component
        ):
            continue
        component_pairs = [
            item
            for pair, item in commonality_by_pair.items()
            if pair[0] in component and pair[1] in component
        ]
        dimensions = tuple(
            sorted(
                {dimension.kind for item in component_pairs for dimension in item.dimensions},
                key=lambda item: item.value,
            )
        )
        if len(dimensions) < 2:
            continue
        clone_ids = tuple(
            sorted({value for item in component_pairs for value in item.clone_cluster_ids})
        )
        effect_ids = tuple(
            sorted(
                item.side_effect_id for item in report.side_effects if item.subject_id in component
            )
        )
        evidence = tuple(
            sorted(
                {value for item in component_pairs for value in item.evidence},
                key=lambda item: (
                    item.span.path,
                    item.span.start_line,
                    item.span.start_column,
                    item.observation,
                ),
            )
        )
        counter = tuple(
            sorted(
                {value for item in component_pairs for value in item.counter_evidence},
                key=lambda item: (
                    item.span.path,
                    item.span.start_line,
                    item.span.start_column,
                    item.observation,
                ),
            )
        )
        try:
            record = ResponsibilityCluster(
                responsibility_id=_stable_id("responsibility", *component),
                member_subject_ids=component,
                shared_dimensions=dimensions,
                clone_cluster_ids=clone_ids,
                side_effect_ids=effect_ids,
                evidence=evidence,
                counter_evidence=counter,
                confidence=Confidence.RESOLVED,
                completeness=Completeness.PARTIAL if counter else Completeness.COMPLETE,
                limitations=tuple(
                    sorted(
                        {
                            "Responsibility clusters are static review evidence, not ownership "
                            "decisions.",
                            *(
                                {
                                    "This component exceeds the bounded proposal member limit; its "
                                    "identity and members are retained, but no extraction proposal "
                                    "is generated."
                                }
                                if len(component) > _MAX_PROPOSAL_COMPONENT_MEMBERS
                                else set()
                            ),
                        }
                    )
                ),
            )
        except ValidationError as exc:
            component_languages = tuple(
                sorted(
                    {subjects[item].language for item in component},
                    key=lambda item: item.value,
                )
            )
            for language in component_languages:
                assembly_failure_facts.append(
                    validation_failure(
                        exc,
                        section="cohesion-refactoring",
                        record_kind="responsibility-cluster",
                        language=language,
                        affected_ids=component,
                        diagnostic_code="cohesion-record-invalid",
                    )
                )
            continue
        responsibility_records.append(record)
    responsibilities = tuple(
        sorted(responsibility_records, key=lambda item: item.responsibility_id)
    )
    proposals: list[RefactorProposal] = []
    estimates: list[ArchitectureEstimate] = []
    rejection_examples: list[ProposalRejectionExample] = []
    proposal_candidates_considered = 0
    call_only_rejections = 0
    too_broad_rejections = 0
    omitted_proposals = 0
    profiles = {item.subject_id: item for item in report.function_profiles}
    modules = {item.name: item for item in report.modules}
    cost_estimates: list[AbstractionCostEstimate] = []
    owner_alternatives: list[OwnerAlternative] = []
    behavioral_events: list[BehavioralEvent] = []
    behavioral_invariants: list[BehavioralInvariant] = []
    behavioral_differences: list[CandidateBehaviorDifference] = []
    for responsibility in responsibilities:
        affected_modules = tuple(
            sorted({subjects[item].module for item in responsibility.member_subject_ids})
        )
        combination_candidate = len(affected_modules) >= 2 and any(
            edge.source in affected_modules and edge.target in affected_modules
            for edge in report.import_edges
        )
        candidate_kinds = (
            ProposalCandidateKind.EXTRACT_RESPONSIBILITY,
            *((ProposalCandidateKind.COMBINE_MODULES,) if combination_candidate else ()),
        )
        proposal_candidates_considered += len(candidate_kinds)
        if len(responsibility.member_subject_ids) > _MAX_PROPOSAL_COMPONENT_MEMBERS:
            too_broad_rejections += len(candidate_kinds)
            for candidate_kind in candidate_kinds:
                if len(rejection_examples) < 25:
                    rejection_examples.append(
                        _proposal_rejection_example(
                            responsibility,
                            candidate_kind,
                            affected_modules,
                            responsibility.evidence,
                            ProposalAdmissionRejectionReason.RESPONSIBILITY_COMPONENT_TOO_BROAD,
                        )
                    )
            continue
        call_only_counter = _call_resolution_only_counter(responsibility)
        if call_only_counter:
            call_only_rejections += len(candidate_kinds)
            for candidate_kind in candidate_kinds:
                if len(rejection_examples) < 25:
                    rejection_examples.append(
                        _proposal_rejection_example(
                            responsibility,
                            candidate_kind,
                            affected_modules,
                            call_only_counter,
                        )
                    )
            continue
        domains = {item.split(".", 1)[0] for item in affected_modules}
        proposed_module = (
            f"{affected_modules[0]}.extracted_{responsibility.responsibility_id[-8:]}"
            if len(affected_modules) == 1
            else f"{min(domains)}.shared_{responsibility.responsibility_id[-8:]}"
        )
        estimate, graph_facts, dependencies, consumers = _estimate_extraction(
            report,
            responsibility.member_subject_ids,
            proposed_module,
            context.config.architecture_layers,
            responsibility.responsibility_id,
        )
        candidate_languages = tuple(
            sorted(
                {subjects[item].language for item in responsibility.member_subject_ids},
                key=lambda item: item.value,
            )
        )
        extraction_candidate_id = _candidate_id(
            RefactorCandidateKind.RESPONSIBILITY_EXTRACTION,
            candidate_languages,
            responsibility.member_subject_ids,
        )
        extraction_observation_id = _stable_id(
            "candidate-observation",
            extraction_candidate_id,
            report.run_identity_sha256,
            report.configuration.digest_sha256,
        )
        extraction_cost, extraction_owners = estimate_candidate_cost(
            report,
            candidate_id=extraction_candidate_id,
            subject_ids=responsibility.member_subject_ids,
            proposed_owner=proposed_module,
            consumer_ids=consumers,
            evidence=responsibility.evidence,
            graph_edge_delta=(
                estimate.expected_after.cross_boundary_edge_count
                - estimate.before.cross_boundary_edge_count
            ),
        )
        cost_estimates.append(extraction_cost)
        owner_alternatives.extend(extraction_owners)
        (
            extraction_events,
            extraction_invariants,
            extraction_differences,
            extraction_behavior_outcome,
            extraction_behavior_reason,
        ) = evaluate_candidate_behavior(
            report,
            candidate_id=extraction_candidate_id,
            subject_ids=responsibility.member_subject_ids,
        )
        behavioral_events.extend(extraction_events)
        behavioral_invariants.extend(extraction_invariants)
        behavioral_differences.extend(extraction_differences)
        proposal_id = _stable_id(
            "refactor-proposal",
            extraction_candidate_id,
            RefactorProposalKind.EXTRACT_RESPONSIBILITY.value,
            proposed_module,
        )
        gate_decisions = _proposal_gate_decisions(
            proposal_id,
            responsibility,
            affected_modules,
            graph_facts,
            estimate.estimate_id,
            extraction_cost,
            extraction_behavior_outcome,
            extraction_behavior_reason,
            tuple(item.difference_id for item in extraction_differences),
        )
        state, primary_reason = _proposal_state(gate_decisions)
        contract_ids = tuple(
            sorted(
                {
                    value
                    for subject_id in responsibility.member_subject_ids
                    for value in profiles[subject_id].contract_ids
                }
            )
        )
        proposal = RefactorProposal(
            proposal_id=proposal_id,
            candidate_id=extraction_candidate_id,
            candidate_observation_id=extraction_observation_id,
            kind=RefactorProposalKind.EXTRACT_RESPONSIBILITY,
            state=state,
            primary_decision_reason=primary_reason,
            gate_decisions=gate_decisions,
            problem_statement=(
                "A threshold-qualified responsibility cluster repeats strong structural "
                "and dependency features across its members."
            ),
            responsibility_id=responsibility.responsibility_id,
            clone_cluster_ids=responsibility.clone_cluster_ids,
            affected_subject_ids=responsibility.member_subject_ids,
            affected_modules=affected_modules,
            affected_contract_ids=contract_ids,
            side_effect_ids=responsibility.side_effect_ids,
            architecture_estimate_id=estimate.estimate_id,
            current_ownership=affected_modules,
            proposed_logical_ownership=proposed_module,
            allowed_paths=tuple(sorted(modules[item].path for item in affected_modules)),
            dependency_identities=dependencies,
            consumer_identities=consumers,
            data_contract_impact=(
                "Recorded contract references must retain their defining and boundary semantics.",
            ),
            compatibility_concerns=(
                "Imports and public symbol locations may require compatibility review.",
            ),
            rollback_criteria=(
                "Retain current ownership if graph or Phase 4 validation contradicts the estimate.",
            ),
            validation_needs=("Phase 4 test-impact mapping and focused behavioral tests.",),
            unresolved_maintainer_decisions=(
                "Confirm the proposed logical owner and public compatibility policy.",
            ),
            evidence=responsibility.evidence,
            counter_evidence=responsibility.counter_evidence,
            confidence=Confidence.INFERRED,
            completeness=Completeness.PARTIAL,
            limitations=PROPOSAL_LIMITATIONS,
        )
        if len(proposals) < context.config.phase_three_limits.max_proposals:
            proposals.append(proposal)
            estimates.append(estimate)
        else:
            omitted_proposals += 1
        if combination_candidate:
            combined_module = affected_modules[0]
            (
                combination_estimate,
                combination_graph_facts,
                combination_dependencies,
                combination_consumers,
            ) = _estimate_combination(
                report,
                affected_modules,
                combined_module,
                responsibility.evidence,
                context.config.architecture_layers,
                responsibility.responsibility_id,
            )
            combination_candidate_id = _candidate_id(
                RefactorCandidateKind.MODULE_COMBINATION,
                candidate_languages,
                responsibility.member_subject_ids,
            )
            combination_observation_id = _stable_id(
                "candidate-observation",
                combination_candidate_id,
                report.run_identity_sha256,
                report.configuration.digest_sha256,
            )
            combination_cost, combination_owners = estimate_candidate_cost(
                report,
                candidate_id=combination_candidate_id,
                subject_ids=responsibility.member_subject_ids,
                proposed_owner=combined_module,
                consumer_ids=combination_consumers,
                evidence=responsibility.evidence,
                graph_edge_delta=(
                    combination_estimate.expected_after.cross_boundary_edge_count
                    - combination_estimate.before.cross_boundary_edge_count
                ),
            )
            cost_estimates.append(combination_cost)
            owner_alternatives.extend(combination_owners)
            (
                combination_events,
                combination_invariants,
                combination_differences,
                combination_behavior_outcome,
                combination_behavior_reason,
            ) = evaluate_candidate_behavior(
                report,
                candidate_id=combination_candidate_id,
                subject_ids=responsibility.member_subject_ids,
            )
            behavioral_events.extend(combination_events)
            behavioral_invariants.extend(combination_invariants)
            behavioral_differences.extend(combination_differences)
            combination_proposal_id = _stable_id(
                "refactor-proposal",
                combination_candidate_id,
                RefactorProposalKind.COMBINE_MODULES.value,
                combined_module,
            )
            combination_decisions = _proposal_gate_decisions(
                combination_proposal_id,
                responsibility,
                affected_modules,
                combination_graph_facts,
                combination_estimate.estimate_id,
                combination_cost,
                combination_behavior_outcome,
                combination_behavior_reason,
                tuple(item.difference_id for item in combination_differences),
            )
            combination_state, combination_reason = _proposal_state(combination_decisions)
            combination_proposal = proposal.model_copy(
                update={
                    "proposal_id": combination_proposal_id,
                    "candidate_id": combination_candidate_id,
                    "candidate_observation_id": combination_observation_id,
                    "kind": RefactorProposalKind.COMBINE_MODULES,
                    "state": combination_state,
                    "primary_decision_reason": combination_reason,
                    "gate_decisions": combination_decisions,
                    "problem_statement": (
                        "Dependency and responsibility evidence indicate overlapping "
                        "logical ownership across coupled modules."
                    ),
                    "architecture_estimate_id": combination_estimate.estimate_id,
                    "proposed_logical_ownership": combined_module,
                    "dependency_identities": combination_dependencies,
                    "consumer_identities": combination_consumers,
                    "counter_evidence": responsibility.counter_evidence,
                }
            )
            if len(proposals) < context.config.phase_three_limits.max_proposals:
                proposals.append(combination_proposal)
                estimates.append(combination_estimate)
            else:
                omitted_proposals += 1
    proposals.sort(key=lambda item: item.proposal_id)
    truncated = omitted_proposals
    retained_estimate_ids = {item.architecture_estimate_id for item in proposals}
    estimate_records = tuple(
        sorted(
            (item for item in estimates if item.estimate_id in retained_estimate_ids),
            key=lambda item: item.estimate_id,
        )
    )
    proposal_records = tuple(proposals)
    target_identity_sha256 = hashlib.sha256(
        json.dumps(
            report.target.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    source_hash_by_module = {item.name: item.source_sha256 for item in report.modules}
    candidate_records: list[RefactorCandidate] = []
    observation_records: list[RefactorCandidateObservation] = []
    proposals_by_candidate: dict[str, list[RefactorProposal]] = {}
    for proposal in proposal_records:
        proposals_by_candidate.setdefault(proposal.candidate_id, []).append(proposal)
    responsibility_by_id = {item.responsibility_id: item for item in responsibilities}
    cost_by_candidate = {item.candidate_id: item for item in cost_estimates}
    owners_by_candidate: dict[str, list[OwnerAlternative]] = {}
    for item in owner_alternatives:
        owners_by_candidate.setdefault(item.candidate_id, []).append(item)
    unique_behavioral_events = {item.event_id: item for item in behavioral_events}
    invariants_by_candidate: dict[str, list[BehavioralInvariant]] = {}
    for invariant in behavioral_invariants:
        invariants_by_candidate.setdefault(invariant.candidate_id, []).append(invariant)
    differences_by_candidate: dict[str, list[CandidateBehaviorDifference]] = {}
    for difference in behavioral_differences:
        differences_by_candidate.setdefault(difference.candidate_id, []).append(difference)
    for candidate_id, candidate_proposals in sorted(proposals_by_candidate.items()):
        first = candidate_proposals[0]
        candidate_kind = (
            RefactorCandidateKind.RESPONSIBILITY_EXTRACTION
            if first.kind is RefactorProposalKind.EXTRACT_RESPONSIBILITY
            else RefactorCandidateKind.MODULE_COMBINATION
        )
        candidate_subject_ids = first.affected_subject_ids
        candidate_languages = tuple(
            sorted(
                {subjects[item].language for item in candidate_subject_ids},
                key=lambda item: item.value,
            )
        )
        candidate_records.append(
            RefactorCandidate(
                candidate_id=candidate_id,
                kind=candidate_kind,
                languages=candidate_languages,
                participating_subject_ids=candidate_subject_ids,
            )
        )
        candidate_responsibility = (
            responsibility_by_id[first.responsibility_id]
            if first.responsibility_id is not None
            else None
        )
        candidate_proposal_tuple = tuple(
            sorted(candidate_proposals, key=lambda item: item.proposal_id)
        )
        source_hashes = tuple(
            sorted({source_hash_by_module[subjects[item].module] for item in candidate_subject_ids})
        )
        observation_records.append(
            RefactorCandidateObservation(
                observation_id=first.candidate_observation_id,
                candidate_id=candidate_id,
                state=_candidate_state(candidate_proposal_tuple),
                run_identity_sha256=report.run_identity_sha256,
                target_identity_sha256=target_identity_sha256,
                configuration_sha256=report.configuration.digest_sha256,
                source_sha256s=source_hashes,
                responsibility_ids=(
                    (candidate_responsibility.responsibility_id,)
                    if candidate_responsibility is not None
                    else ()
                ),
                proposal_ids=tuple(item.proposal_id for item in candidate_proposal_tuple),
                cost_estimate_id=cost_by_candidate[candidate_id].cost_estimate_id,
                owner_alternative_ids=tuple(
                    sorted(item.alternative_id for item in owners_by_candidate[candidate_id])
                ),
                behavioral_event_ids=tuple(
                    sorted(
                        item.event_id
                        for item in unique_behavioral_events.values()
                        if item.subject_id in candidate_subject_ids
                    )
                ),
                behavioral_invariant_ids=tuple(
                    sorted(
                        item.invariant_id for item in invariants_by_candidate.get(candidate_id, ())
                    )
                ),
                behavioral_difference_ids=tuple(
                    sorted(
                        item.difference_id
                        for item in differences_by_candidate.get(candidate_id, ())
                    )
                ),
                failed_gate_count=sum(
                    decision.outcome is ProposalGateOutcome.FAILED
                    for item in candidate_proposal_tuple
                    for decision in item.gate_decisions
                ),
                unresolved_gate_count=sum(
                    decision.outcome is ProposalGateOutcome.UNRESOLVED
                    for item in candidate_proposal_tuple
                    for decision in item.gate_decisions
                ),
                evidence=(
                    candidate_responsibility.evidence
                    if candidate_responsibility is not None
                    else first.evidence
                ),
                counter_evidence=(
                    candidate_responsibility.counter_evidence
                    if candidate_responsibility is not None
                    else first.counter_evidence
                ),
                completeness=first.completeness,
                limitations=first.limitations,
            )
        )
    for duplicate in report.duplicate_functions:
        subject_ids = (duplicate.left_subject_id, duplicate.right_subject_id)
        languages = (duplicate.language,)
        candidate_id = _candidate_id(
            RefactorCandidateKind.DUPLICATE_FUNCTIONS, languages, subject_ids
        )
        observation_id = _stable_id(
            "candidate-observation",
            candidate_id,
            report.run_identity_sha256,
            report.configuration.digest_sha256,
        )
        duplicate_owner = (
            subjects[duplicate.canonical_owner.subject_id].module
            if duplicate.canonical_owner.subject_id is not None
            else f"{subjects[duplicate.left_subject_id].module}.shared_{candidate_id[-8:]}"
        )
        duplicate_cost, duplicate_owners = estimate_candidate_cost(
            report,
            candidate_id=candidate_id,
            subject_ids=subject_ids,
            proposed_owner=duplicate_owner,
            consumer_ids=(),
            evidence=duplicate.evidence,
            graph_edge_delta=0,
        )
        cost_estimates.append(duplicate_cost)
        owner_alternatives.extend(duplicate_owners)
        (
            duplicate_events,
            duplicate_invariants,
            duplicate_differences,
            _duplicate_behavior_outcome,
            _duplicate_behavior_reason,
        ) = evaluate_candidate_behavior(
            report,
            candidate_id=candidate_id,
            subject_ids=subject_ids,
        )
        behavioral_events.extend(duplicate_events)
        behavioral_invariants.extend(duplicate_invariants)
        behavioral_differences.extend(duplicate_differences)
        candidate_records.append(
            RefactorCandidate(
                candidate_id=candidate_id,
                kind=RefactorCandidateKind.DUPLICATE_FUNCTIONS,
                languages=languages,
                participating_subject_ids=subject_ids,
            )
        )
        observation_records.append(
            RefactorCandidateObservation(
                observation_id=observation_id,
                candidate_id=candidate_id,
                state=RefactorCandidateState.OBSERVED,
                run_identity_sha256=report.run_identity_sha256,
                target_identity_sha256=target_identity_sha256,
                configuration_sha256=report.configuration.digest_sha256,
                source_sha256s=tuple(
                    sorted({source_hash_by_module[subjects[item].module] for item in subject_ids})
                ),
                duplicate_ids=(duplicate.duplicate_id,),
                cost_estimate_id=duplicate_cost.cost_estimate_id,
                owner_alternative_ids=tuple(
                    sorted(item.alternative_id for item in duplicate_owners)
                ),
                behavioral_event_ids=tuple(sorted(item.event_id for item in duplicate_events)),
                behavioral_invariant_ids=tuple(
                    sorted(item.invariant_id for item in duplicate_invariants)
                ),
                behavioral_difference_ids=tuple(
                    sorted(item.difference_id for item in duplicate_differences)
                ),
                evidence=duplicate.evidence,
                counter_evidence=(),
                completeness=Completeness.PARTIAL,
                limitations=tuple(
                    sorted(
                        {
                            *duplicate.limitations,
                            "Duplicate evidence is not refactoring authorization.",
                        }
                    )
                ),
            )
        )
    candidate_records.sort(key=lambda item: item.candidate_id)
    observation_records.sort(key=lambda item: item.observation_id)
    candidates = tuple(candidate_records)
    candidate_observations = tuple(observation_records)
    retained_candidate_ids = {item.candidate_id for item in candidates}
    cost_estimate_records = tuple(
        sorted(
            (item for item in cost_estimates if item.candidate_id in retained_candidate_ids),
            key=lambda item: item.cost_estimate_id,
        )
    )
    owner_alternative_records = tuple(
        sorted(
            (item for item in owner_alternatives if item.candidate_id in retained_candidate_ids),
            key=lambda item: item.alternative_id,
        )
    )
    referenced_behavioral_event_ids = {
        value for item in candidate_observations for value in item.behavioral_event_ids
    }
    referenced_behavioral_invariant_ids = {
        value for item in candidate_observations for value in item.behavioral_invariant_ids
    }
    referenced_behavioral_difference_ids = {
        value for item in candidate_observations for value in item.behavioral_difference_ids
    }
    behavioral_event_records = tuple(
        sorted(
            {
                item.event_id: item
                for item in behavioral_events
                if item.event_id in referenced_behavioral_event_ids
            }.values(),
            key=lambda item: item.event_id,
        )
    )
    behavioral_invariant_records = tuple(
        sorted(
            {
                item.invariant_id: item
                for item in behavioral_invariants
                if item.invariant_id in referenced_behavioral_invariant_ids
            }.values(),
            key=lambda item: item.invariant_id,
        )
    )
    behavioral_difference_records = tuple(
        sorted(
            {
                item.difference_id: item
                for item in behavioral_differences
                if item.difference_id in referenced_behavioral_difference_ids
            }.values(),
            key=lambda item: item.difference_id,
        )
    )
    admission_funnel = ProposalAdmissionFunnel(
        responsibility_clusters_considered=len(responsibilities),
        proposal_candidates_considered=proposal_candidates_considered,
        admitted_candidates=len(proposal_records) + omitted_proposals,
        call_only_rejections=call_only_rejections,
        too_broad_rejections=too_broad_rejections,
        proposal_bound_omissions=omitted_proposals,
        retained_proposals=len(proposal_records),
        rejection_examples=tuple(sorted(rejection_examples, key=lambda item: item.candidate_id)),
    )
    decision_funnel = ProposalDecisionFunnel(
        total_proposals=len(proposal_records),
        blocked_proposals=sum(
            item.state is RefactorProposalState.BLOCKED_INSUFFICIENT_EVIDENCE
            for item in proposal_records
        ),
        reviewable_incomplete_proposals=sum(
            item.state is RefactorProposalState.REVIEWABLE_INCOMPLETE for item in proposal_records
        ),
        ready_for_maintainer_review_proposals=sum(
            item.state is RefactorProposalState.READY_FOR_MAINTAINER_REVIEW
            for item in proposal_records
        ),
        failed_gates=sum(
            decision.outcome is ProposalGateOutcome.FAILED
            for item in proposal_records
            for decision in item.gate_decisions
        ),
        unresolved_gates=sum(
            decision.outcome is ProposalGateOutcome.UNRESOLVED
            for item in proposal_records
            for decision in item.gate_decisions
        ),
    )
    suggestions, architecture_findings = _suggestions_and_findings(context, report)
    proposal_findings = tuple(
        Finding(
            finding_id=_stable_id("finding", item.kind.value, item.proposal_id),
            rule_id=(
                "extraction-candidate"
                if item.kind is RefactorProposalKind.EXTRACT_RESPONSIBILITY
                else "module-combination-candidate"
            ),
            category=(
                "extraction-candidate"
                if item.kind is RefactorProposalKind.EXTRACT_RESPONSIBILITY
                else "module-combination"
            ),
            severity=(
                "info" if item.state is RefactorProposalState.REVIEWABLE_INCOMPLETE else "warning"
            ),
            title=f"{item.kind.value} proposal is {item.state.value}",
            explanation=item.problem_statement,
            module=item.affected_modules[0],
            language=modules[item.affected_modules[0]].language,
            subject_ids=item.affected_subject_ids,
            affected_contract_ids=item.affected_contract_ids,
            affected_shape_ids=item.affected_shape_ids,
            affected_structure_ids=item.affected_structure_ids,
            affected_clone_cluster_ids=item.clone_cluster_ids,
            affected_proposal_ids=(item.proposal_id,),
            evidence=item.evidence,
            counter_evidence=item.counter_evidence,
            confidence=item.confidence,
            completeness=item.completeness,
            limitations=item.limitations,
            likely_validation_needs=item.validation_needs,
        )
        for item in proposal_records
    )
    utility_findings = tuple(
        Finding(
            finding_id=_stable_id("finding", "utility-risk", module.name),
            rule_id="high-coupling-utility-risk",
            category="cohesion",
            severity="warning",
            title="High-coupling utility-module risk",
            explanation=(
                f"{module.name} has fan-in {module.fan_in}; similar code across domains should "
                "retain its boundary unless ownership evidence supports consolidation."
            ),
            module=module.name,
            language=module.language,
            subject_ids=tuple(
                sorted(
                    subject.subject_id
                    for subject in report.subjects
                    if subject.module == module.name
                )
            ),
            evidence=next(
                subject.evidence for subject in report.subjects if subject.module == module.name
            ),
            confidence=Confidence.HEURISTIC,
            completeness=Completeness.COMPLETE,
            limitations=("Fan-in is a review signal, not an extraction instruction.",),
            likely_validation_needs=("Review consumers and domain ownership.",),
        )
        for module in report.modules
        if module.fan_in >= 5 and any(subject.module == module.name for subject in report.subjects)
    )
    all_findings = tuple(
        sorted(
            (*report.findings, *architecture_findings, *proposal_findings, *utility_findings),
            key=lambda item: item.finding_id,
        )
    )
    finding_truncated = 0
    if context.config.max_findings is not None and len(all_findings) > context.config.max_findings:
        finding_truncated = len(all_findings) - context.config.max_findings
        findings = all_findings[: context.config.max_findings]
    else:
        findings = all_findings
    diagnostic = (
        (
            Diagnostic(
                code="refactor-proposal-bound-truncated",
                level=DiagnosticLevel.WARNING,
                message=f"proposal bound omitted {truncated} proposals",
                provider="native-cohesion",
            ),
        )
        if truncated
        else ()
    )
    if finding_truncated and not any(
        item.code == "findings-truncated" for item in (*report.diagnostics, *diagnostic)
    ):
        diagnostic = (
            *diagnostic,
            Diagnostic(
                code="findings-truncated",
                level=DiagnosticLevel.WARNING,
                message=(
                    f"finding limit omitted {finding_truncated} findings from the unified report"
                ),
                provider="native-cohesion",
            ),
        )
    public_assembly_failures = tuple(
        SectionAssemblyFailure(
            failure_id=item.identity,
            section=item.section,
            record_kind=item.record_kind,
            language=item.language,
            affected_ids=item.affected_ids,
            field_paths=item.field_paths,
            diagnostic_code=item.diagnostic_code,
        )
        for item in sorted(assembly_failure_facts, key=lambda value: value.identity)
    )
    assembly_diagnostics = tuple(
        Diagnostic(
            code=item.diagnostic_code,
            level=DiagnosticLevel.ERROR,
            message=(
                f"Invalid {item.record_kind} evidence was excluded from "
                f"{item.section}: {', '.join(item.field_paths)}"
            ),
            provider="native-cohesion",
            language=item.language,
        )
        for item in public_assembly_failures
    )
    diagnostic = (*diagnostic, *assembly_diagnostics)
    section_state = (
        SectionState.PARTIAL
        if truncated or finding_truncated or public_assembly_failures
        else SectionState.COMPLETE
    )
    overall = (
        Completeness.FAILED
        if report.completeness is Completeness.FAILED
        else (
            Completeness.PARTIAL
            if report.completeness is Completeness.PARTIAL or section_state is SectionState.PARTIAL
            else Completeness.COMPLETE
        )
    )
    coverage = tuple(
        (
            SectionCoverage(
                section="cohesion-refactoring",
                state=section_state,
                parsed=len(responsibilities),
                failed=len(public_assembly_failures),
                truncated=truncated + finding_truncated,
                diagnostic_codes=tuple(sorted({item.code for item in diagnostic})),
                languages=tuple(sorted(report.target.languages, key=lambda item: item.value)),
            )
            if item.section == "cohesion-refactoring"
            else item
        )
        for item in report.section_coverage
    )
    provider = ProviderResult(
        provider_id="native-cohesion",
        state=(
            ProviderState.PARTIAL
            if section_state is SectionState.PARTIAL
            else ProviderState.SUCCESS
        ),
        executable="python",
        version=platform.python_version(),
        duration_ms=0,
        observations=(
            "architecture-contract-suggestions",
            "bounded-refactor-proposals",
            "responsibility-clusters",
            "simulated-module-graphs",
        ),
        diagnostics=diagnostic,
        completeness=(
            Completeness.PARTIAL if section_state is SectionState.PARTIAL else Completeness.COMPLETE
        ),
    )
    summary = SummaryCounts(
        **{
            **report.summary.model_dump(),
            "responsibility_clusters": len(responsibilities),
            "architecture_estimates": len(estimate_records),
            "architecture_contract_suggestions": len(suggestions),
            "refactor_proposals": len(proposal_records),
            "refactor_candidates": len(candidates),
            "candidate_observations": len(candidate_observations),
            "abstraction_cost_estimates": len(cost_estimate_records),
            "owner_alternatives": len(owner_alternative_records),
            "behavioral_events": len(behavioral_event_records),
            "behavioral_ordering_edges": 0,
            "behavioral_invariants": len(behavioral_invariant_records),
            "candidate_behavior_differences": len(behavioral_difference_records),
            "findings": len(findings),
        }
    )
    identity = hashlib.sha256(
        json.dumps(
            {
                "prior": report.run_identity_sha256,
                "responsibilities": [item.model_dump(mode="json") for item in responsibilities],
                "estimates": [item.model_dump(mode="json") for item in estimate_records],
                "proposals": [item.model_dump(mode="json") for item in proposal_records],
                "candidates": [item.model_dump(mode="json") for item in candidates],
                "candidate_observations": [
                    item.model_dump(mode="json") for item in candidate_observations
                ],
                "abstraction_cost_estimates": [
                    item.model_dump(mode="json") for item in cost_estimate_records
                ],
                "owner_alternatives": [
                    item.model_dump(mode="json") for item in owner_alternative_records
                ],
                "behavioral_events": [
                    item.model_dump(mode="json") for item in behavioral_event_records
                ],
                "behavioral_invariants": [
                    item.model_dump(mode="json") for item in behavioral_invariant_records
                ],
                "candidate_behavior_differences": [
                    item.model_dump(mode="json") for item in behavioral_difference_records
                ],
                "proposal_admission": admission_funnel.model_dump(mode="json"),
                "proposal_decisions": decision_funnel.model_dump(mode="json"),
                "suggestions": [item.model_dump(mode="json") for item in suggestions],
                "assembly_failures": [
                    item.model_dump(mode="json") for item in public_assembly_failures
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    limitations = {*report.limitations, *PROPOSAL_LIMITATIONS}
    if truncated:
        limitations.add("Configured Phase 3 bounds truncated cohesion or proposal coverage.")
    if public_assembly_failures:
        limitations.add(
            "Invalid language-owned cohesion records were excluded while valid evidence "
            "was retained."
        )
    failure_by_language = {
        language: tuple(item for item in public_assembly_failures if item.language is language)
        for language in report.target.languages
    }
    language_frontends = tuple(
        (
            item.model_copy(
                update={
                    "state": SectionState.PARTIAL,
                    "diagnostic_codes": tuple(
                        sorted({*item.diagnostic_codes, "cohesion-record-invalid"})
                    ),
                    "assembly_failures": tuple(
                        sorted(
                            (*item.assembly_failures, *failure_by_language[item.language]),
                            key=lambda failure: failure.failure_id,
                        )
                    ),
                }
            )
            if failure_by_language.get(item.language)
            else item
        )
        for item in report.language_frontends
    )
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "run_identity_sha256": identity,
            "responsibility_clusters": responsibilities,
            "architecture_estimates": estimate_records,
            "architecture_contract_suggestions": suggestions,
            "refactor_proposals": proposal_records,
            "refactor_candidates": candidates,
            "candidate_observations": candidate_observations,
            "abstraction_cost_estimates": cost_estimate_records,
            "owner_alternatives": owner_alternative_records,
            "behavioral_events": behavioral_event_records,
            "behavioral_invariants": behavioral_invariant_records,
            "candidate_behavior_differences": behavioral_difference_records,
            "signal_quality": SignalQualitySummary(
                commonality_admission=report.signal_quality.commonality_admission,
                proposal_admission=admission_funnel,
                proposal_decisions=decision_funnel,
            ),
            "section_coverage": coverage,
            "language_frontends": language_frontends,
            "findings": findings,
            "diagnostics": (*report.diagnostics, *diagnostic),
            "providers": (*report.providers, provider),
            "completeness": overall,
            "limitations": tuple(sorted(limitations)),
            "summary": summary,
        }
    )
