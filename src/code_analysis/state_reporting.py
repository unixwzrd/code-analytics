"""Report integration for mutable-state and provenance-resolved side-effect evidence."""

from __future__ import annotations

import hashlib
import json
import platform

from code_analysis.analysis.state_effects import (
    FactSpan,
    MutableStateFact,
    SideEffectFact,
    discover_state_and_side_effects,
)
from code_analysis.clone_reporting import collect_clone_sources
from code_analysis.context import AnalysisContext
from code_analysis.models import (
    Completeness,
    Confidence,
    Evidence,
    ProviderResult,
    ProviderState,
    SourceSpan,
)
from code_analysis.report_models import (
    AnalysisReport,
    AnalysisSubject,
    Finding,
    SectionCoverage,
    SectionState,
    SideEffectObservation,
    StateResource,
    SubjectKind,
    SummaryCounts,
)

STATE_LIMITATIONS = (
    "Mutable-state and side-effect evidence is syntax-only; runtime ownership and outcomes "
    "are unmeasured.",
    "Shadowing, dynamic dispatch, cleanup effectiveness, and arbitrary receiver provenance "
    "remain unresolved.",
)


def _stable_id(prefix: str, *parts: str) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def _evidence(span: FactSpan, confidence: Confidence = Confidence.EXACT) -> Evidence:
    return Evidence(
        span=SourceSpan(
            path=span.logical_path,
            start_line=span.start_line,
            start_column=span.start_column,
            end_line=span.end_line,
            end_column=span.end_column,
        ),
        provider="native-state-effects",
        observation=span.observation,
        confidence=confidence,
        source_sha256=span.source_sha256,
    )


def _subject_identity(logical_name: str, module: str) -> tuple[str, SubjectKind]:
    if logical_name == module:
        return _stable_id("subject", "module", module), SubjectKind.MODULE
    qualified_name = logical_name.split(":", 1)[1]
    return _stable_id("subject", "symbol", module, qualified_name), SubjectKind.SYMBOL


def _ensure_subject(
    subjects: dict[str, AnalysisSubject],
    logical_subjects: dict[str, str],
    logical_name: str,
    module: str,
    evidence: Evidence,
) -> str:
    generated, kind = _subject_identity(logical_name, module)
    subject_id = logical_subjects.get(logical_name, generated)
    if subject_id not in subjects:
        subjects[subject_id] = AnalysisSubject(
            subject_id=subject_id,
            kind=kind,
            logical_name=logical_name,
            module=module,
            evidence=(evidence,),
        )
        logical_subjects[logical_name] = subject_id
    return subject_id


def _state_record(
    fact: MutableStateFact,
    subject_id: str,
    related_structure_ids: tuple[str, ...],
) -> StateResource:
    return StateResource(
        state_resource_id=_stable_id(
            "state-resource", fact.kind.value, fact.logical_name, fact.declaration.source_sha256
        ),
        kind=fact.kind,
        scope=fact.scope,
        owner_subject_id=subject_id,
        logical_name=fact.logical_name,
        related_structure_ids=related_structure_ids,
        declaration_evidence=(_evidence(fact.declaration),),
        read_evidence=tuple(_evidence(item) for item in fact.reads),
        write_evidence=tuple(_evidence(item) for item in fact.writes),
        cleanup_evidence=tuple(_evidence(item) for item in fact.cleanup),
        guard_evidence=tuple(_evidence(item) for item in fact.guards),
        alias_evidence=tuple(_evidence(item) for item in fact.aliases),
        confidence=Confidence.EXACT,
        completeness=Completeness.COMPLETE,
        limitations=STATE_LIMITATIONS,
    )


def _effect_record(fact: SideEffectFact, subject_id: str) -> SideEffectObservation:
    return SideEffectObservation(
        side_effect_id=_stable_id(
            "side-effect",
            fact.kind.value,
            subject_id,
            fact.target_identity,
            fact.span.logical_path,
            str(fact.span.start_line),
            str(fact.span.start_column),
        ),
        subject_id=subject_id,
        kind=fact.kind,
        target_identity=fact.target_identity,
        evidence=(_evidence(fact.span),),
        confidence=Confidence.RESOLVED,
        completeness=Completeness.COMPLETE,
        limitations=STATE_LIMITATIONS,
    )


def integrate_state_effect_report(
    context: AnalysisContext, report: AnalysisReport
) -> AnalysisReport:
    sources = collect_clone_sources(context, report)
    state_facts, effect_facts = discover_state_and_side_effects(sources)
    subjects = {item.subject_id: item for item in report.subjects}
    logical_subjects = {item.logical_name: item.subject_id for item in report.subjects}
    state_subject_ids: dict[MutableStateFact, str] = {}
    for fact in state_facts:
        state_subject_ids[fact] = _ensure_subject(
            subjects,
            logical_subjects,
            fact.owner_logical_name,
            fact.module,
            _evidence(fact.declaration),
        )
    effect_subject_ids: dict[SideEffectFact, str] = {}
    for effect_fact in effect_facts:
        effect_subject_ids[effect_fact] = _ensure_subject(
            subjects,
            logical_subjects,
            effect_fact.subject_logical_name,
            effect_fact.module,
            _evidence(effect_fact.span),
        )
    structures_by_logical: dict[str, list[str]] = {}
    for structure in report.data_structures:
        owner = subjects.get(structure.owner_subject_id)
        if owner is not None:
            structures_by_logical.setdefault(owner.logical_name, []).append(structure.structure_id)
    state_resources = tuple(
        sorted(
            (
                _state_record(
                    fact,
                    state_subject_ids[fact],
                    tuple(sorted(structures_by_logical.get(fact.logical_name, ()))),
                )
                for fact in state_facts
            ),
            key=lambda item: item.state_resource_id,
        )
    )
    side_effects = tuple(
        sorted(
            (_effect_record(fact, effect_subject_ids[fact]) for fact in effect_facts),
            key=lambda item: item.side_effect_id,
        )
    )
    effects_by_target: dict[tuple[str, str], list[SideEffectObservation]] = {}
    for effect in side_effects:
        effects_by_target.setdefault((effect.kind.value, effect.target_identity), []).append(effect)
    new_findings = []
    for (kind, target), effects in sorted(effects_by_target.items()):
        affected_subjects = tuple(sorted({item.subject_id for item in effects}))
        if len(affected_subjects) < 2:
            continue
        new_findings.append(
            Finding(
                finding_id=_stable_id("finding", "side-effect-ownership", kind, target),
                rule_id="side-effect-ownership-shared-target",
                category="side-effect-ownership",
                severity="warning",
                title="Side-effect target has multiple syntax owners",
                explanation=(
                    f"{len(affected_subjects)} subjects contain resolved {kind} calls to "
                    f"{target}; runtime ownership remains unmeasured."
                ),
                module=subjects[affected_subjects[0]].module,
                subject_ids=affected_subjects,
                evidence=tuple(item.evidence[0] for item in effects),
                confidence=Confidence.RESOLVED,
                completeness=Completeness.COMPLETE,
                limitations=STATE_LIMITATIONS,
                likely_validation_needs=(
                    "Review operational ownership and cleanup paths before refactoring.",
                ),
            )
        )
    findings = tuple(sorted((*report.findings, *new_findings), key=lambda item: item.finding_id))
    state = SectionState.PARTIAL if report.summary.failed_files else SectionState.COMPLETE
    overall = (
        Completeness.FAILED
        if report.completeness is Completeness.FAILED
        else (
            Completeness.PARTIAL
            if report.completeness is Completeness.PARTIAL or state is SectionState.PARTIAL
            else Completeness.COMPLETE
        )
    )
    coverage = tuple(
        (
            SectionCoverage(
                section="state-side-effects",
                state=state,
                parsed=len(sources),
                failed=report.summary.failed_files,
            )
            if item.section == "state-side-effects"
            else item
        )
        for item in report.section_coverage
    )
    provider = ProviderResult(
        provider_id="native-state-effects",
        state=ProviderState.PARTIAL if state is SectionState.PARTIAL else ProviderState.SUCCESS,
        executable="python",
        version=platform.python_version(),
        duration_ms=0,
        observations=(
            "mutable-state-resources",
            "provenance-resolved-side-effects",
        ),
        completeness=(
            Completeness.PARTIAL if state is SectionState.PARTIAL else Completeness.COMPLETE
        ),
    )
    summary = SummaryCounts(
        **{
            **report.summary.model_dump(),
            "subjects": len(subjects),
            "state_resources": len(state_resources),
            "side_effects": len(side_effects),
            "findings": len(findings),
        }
    )
    identity_payload = json.dumps(
        {
            "prior": report.run_identity_sha256,
            "state": [item.model_dump(mode="json") for item in state_resources],
            "effects": [item.model_dump(mode="json") for item in side_effects],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "run_identity_sha256": hashlib.sha256(identity_payload).hexdigest(),
            "subjects": tuple(subjects[key] for key in sorted(subjects)),
            "state_resources": state_resources,
            "side_effects": side_effects,
            "section_coverage": coverage,
            "findings": findings,
            "providers": (*report.providers, provider),
            "completeness": overall,
            "limitations": tuple(sorted({*report.limitations, *STATE_LIMITATIONS})),
            "summary": summary,
        }
    )
