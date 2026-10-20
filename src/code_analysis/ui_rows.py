"""Report-owned table rows and search fields shared by live and indexed views."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from enum import StrEnum

from code_analysis.clone_browsing import CloneRow, clone_rows
from code_analysis.contract_browsing import ContractRow, ShapeRow, contract_rows, shape_rows
from code_analysis.finding_suppression_models import (
    finding_suppression_annotation,
    finding_suppression_summary,
)
from code_analysis.models import Diagnostic
from code_analysis.projection_models import ProjectionSelection
from code_analysis.projections.candidates import ranked_candidates
from code_analysis.report_models import AnalysisReport
from code_analysis.test_browsing import TestImpactRow, test_impact_rows
from code_analysis.ui_models import (
    BrowseFacets,
    UICandidateRow,
    UIFindingRow,
    UIModuleRow,
    UIModuleScope,
    UIProviderRow,
    UIStateEffectRow,
)


class BrowseTable(StrEnum):
    MODULES = "modules"
    FINDINGS = "findings"
    CANDIDATES = "candidates"
    PROVIDERS = "providers"
    DIAGNOSTICS = "diagnostics"
    STATE_EFFECTS = "state-effects"
    CLONES = "clones"
    CONTRACTS = "contracts"
    SHAPES = "dictionary-shapes"
    TEST_IMPACT = "test-impact"


type BrowseRow = (
    UIModuleRow
    | UIFindingRow
    | UICandidateRow
    | UIProviderRow
    | Diagnostic
    | UIStateEffectRow
    | CloneRow
    | ContractRow
    | ShapeRow
    | TestImpactRow
)


def state_effect_rows(report: AnalysisReport) -> Iterator[UIStateEffectRow]:
    subjects = {item.subject_id: item for item in report.subjects}
    for item in sorted(report.state_resources, key=lambda value: value.state_resource_id):
        owner = subjects[item.owner_subject_id]
        yield UIStateEffectRow(
            identity=item.state_resource_id,
            record_kind="resource",
            category=item.kind.value,
            module=owner.module,
            owner=owner.logical_name,
            language=owner.language,
            target=item.logical_name,
            confidence=item.confidence,
            completeness=item.completeness,
            detail=item,
        )
    for effect in sorted(report.side_effects, key=lambda value: value.side_effect_id):
        owner = subjects[effect.subject_id]
        yield UIStateEffectRow(
            identity=effect.side_effect_id,
            record_kind="side-effect",
            category=effect.kind.value,
            module=owner.module,
            owner=owner.logical_name,
            language=owner.language,
            target=effect.target_identity,
            confidence=effect.confidence,
            completeness=effect.completeness,
            detail=effect,
        )


def module_rows(report: AnalysisReport) -> Iterator[UIModuleRow]:
    counts = Counter(item.module for item in report.findings)
    for item in report.modules:
        yield UIModuleRow(
            module=item.name,
            path=item.path,
            language=item.language,
            lines=item.line_count,
            symbols=len(item.symbols),
            fan_in=item.fan_in,
            fan_out=item.fan_out,
            finding_count=counts[item.name],
        )


def finding_rows(report: AnalysisReport) -> Iterator[UIFindingRow]:
    for item in sorted(
        report.findings,
        key=lambda item: (
            {"error": 0, "warning": 1, "info": 2}[item.severity],
            item.module,
            item.finding_id,
        ),
    ):
        suppression = finding_suppression_annotation(item.suppression)
        yield UIFindingRow(
            finding_id=item.finding_id,
            language=item.language,
            severity=item.severity,
            category=item.category,
            rule_id=item.rule_id,
            module=item.module,
            symbol=item.symbol,
            title=item.title,
            explanation=item.explanation,
            confidence=item.confidence,
            suppression=suppression,
            suppression_summary=finding_suppression_summary(suppression),
        )


def candidate_rows(report: AnalysisReport) -> Iterator[UICandidateRow]:
    subjects = {item.subject_id: item for item in report.subjects}
    observations = {item.candidate_id: item for item in report.candidate_observations}
    ranked = (
        ranked_candidates(report, ProjectionSelection(), {}, None)
        if report.refactor_candidates
        else ()
    )
    for item in ranked:
        candidate = item.candidate
        observation = observations.get(candidate.candidate_id)
        if observation is not None:
            yield UICandidateRow(
                candidate_id=candidate.candidate_id,
                kind=candidate.kind.value,
                state=observation.state.value,
                languages=candidate.languages,
                module_scopes=tuple(
                    UIModuleScope(language=language, module=module)
                    for language, module in sorted(
                        {
                            (subjects[identity].language, subjects[identity].module)
                            for identity in candidate.participating_subject_ids
                        }
                    )
                ),
                subject_count=len(candidate.participating_subject_ids),
                proposal_count=len(observation.proposal_ids),
                failed_gates=observation.failed_gate_count,
                unresolved_gates=observation.unresolved_gate_count,
            )


def participant_scopes(row: UICandidateRow | CloneRow) -> tuple[UIModuleScope, ...]:
    if isinstance(row, UICandidateRow):
        return row.module_scopes
    return tuple(UIModuleScope(language=row.language, module=module) for module in row.modules)


def participant_matches(row: UICandidateRow | CloneRow, facets: BrowseFacets) -> bool:
    if facets.owner:
        raise ValueError("participant filtering does not establish ownership")
    return any(facets.matches(item.language, item.module) for item in participant_scopes(row))


def provider_rows(report: AnalysisReport) -> Iterator[UIProviderRow]:
    for item in report.providers:
        yield UIProviderRow(
            provider_id=item.provider_id,
            state=item.state.value,
            completeness=item.completeness,
            observations=item.observations,
        )


def table_rows(report: AnalysisReport, table: BrowseTable) -> Iterator[BrowseRow]:
    match table:
        case BrowseTable.MODULES:
            yield from module_rows(report)
        case BrowseTable.FINDINGS:
            yield from finding_rows(report)
        case BrowseTable.CANDIDATES:
            yield from candidate_rows(report)
        case BrowseTable.PROVIDERS:
            yield from provider_rows(report)
        case BrowseTable.DIAGNOSTICS:
            yield from report.diagnostics
        case BrowseTable.STATE_EFFECTS:
            yield from state_effect_rows(report)
        case BrowseTable.CLONES:
            yield from clone_rows(report)
        case BrowseTable.CONTRACTS:
            yield from contract_rows(report)
        case BrowseTable.SHAPES:
            yield from shape_rows(report)
        case BrowseTable.TEST_IMPACT:
            yield from test_impact_rows(report)
        case _:
            raise ValueError("unknown browse table")


def search_fields(row: BrowseRow) -> tuple[str, ...]:
    values: tuple[str, ...]
    if isinstance(row, UIModuleRow):
        values = (row.module, row.path)
    elif isinstance(row, UIFindingRow):
        values = (row.title, row.module, row.rule_id)
    elif isinstance(row, UICandidateRow):
        values = (row.candidate_id, row.kind)
    elif isinstance(row, Diagnostic):
        values = (row.code, row.message, row.provider or "", row.path or "")
    elif isinstance(row, UIStateEffectRow):
        values = (
            row.identity,
            row.module,
            row.owner,
            f"{row.record_kind} {row.category} {row.target}",
        )
    elif isinstance(row, CloneRow):
        values = (
            row.cluster_id,
            row.kind.value,
            " ".join(row.modules),
            " ".join(item.subject.logical_name for item in row.detail.members),
        )
    elif isinstance(row, ContractRow):
        values = (
            row.identity,
            row.module,
            row.owner,
            " ".join(
                (row.kind, *row.boundary_classes, *(x.name for x in row.detail.contract.members))
            ),
        )
    elif isinstance(row, ShapeRow):
        values = (
            row.identity,
            row.module,
            row.owner,
            " ".join(
                (
                    row.unknown_field_policy,
                    *(x.name for v in row.detail.shape.variants for x in v.fields),
                )
            ),
        )
    elif isinstance(row, TestImpactRow):
        values = (
            row.identity,
            row.module,
            row.name,
            " ".join(
                (
                    row.record_kind,
                    row.relationship_kind.value if row.relationship_kind else "",
                    row.target or "",
                    (
                        "unresolved"
                        if row.resolved is False
                        else "resolved" if row.resolved is True else "inventory"
                    ),
                )
            ),
        )
    else:
        values = ()
    return tuple(value.casefold() for value in values)


def row_matches(row: BrowseRow, query: str) -> bool:
    needle = query.casefold().strip()
    return not needle or any(needle in value for value in search_fields(row))
