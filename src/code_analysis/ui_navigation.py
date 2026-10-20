"""Resolve graph/inventory identities to bounded saved-source navigation."""

from __future__ import annotations

from pydantic import Field, StrictInt, StrictStr, model_validator

from code_analysis.models import Completeness, Evidence, SourceSpan, StrictModel
from code_analysis.report_models import AnalysisReport


class NavigationItem(StrictModel):
    identity: StrictStr
    label: StrictStr
    kind: StrictStr
    span: SourceSpan


class NavigationUnavailable(ValueError):
    """The selected saved identity has no recorded source-navigation mapping."""


class NavigationPage(StrictModel):
    identity: StrictStr
    label: StrictStr
    completeness: Completeness
    offset: StrictInt = Field(ge=0)
    eligible: StrictInt = Field(ge=0)
    records: tuple[NavigationItem, ...] = Field(max_length=25)
    next_offset: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> NavigationPage:
        if len(self.records) != min(25, max(0, self.eligible - self.offset)):
            raise ValueError("navigation page does not reconcile with eligible records")
        expected = self.offset + len(self.records)
        if self.next_offset != (expected if expected < self.eligible else None):
            raise ValueError("navigation continuation does not reconcile")
        return self


def navigation_page(report: AnalysisReport, identity: str, offset: int = 0) -> NavigationPage:
    _validate_offset(offset)
    items = navigation_items(report, frozenset((identity,))).get(identity)
    if items is None:
        raise NavigationUnavailable("selection has no recorded source-navigation mapping")
    return build_navigation_page(identity, items, report.completeness, offset)


def build_navigation_page(
    identity: str,
    items: tuple[NavigationItem, ...],
    completeness: Completeness,
    offset: int = 0,
) -> NavigationPage:
    _validate_offset(offset)
    records = ordered_navigation_items(items)
    retained = records[offset : offset + 25]
    stop = offset + len(retained)
    return NavigationPage(
        identity=identity,
        label=identity,
        completeness=completeness,
        offset=offset,
        eligible=len(records),
        records=retained,
        next_offset=stop if stop < len(records) else None,
    )


def _validate_offset(offset: int) -> None:
    if type(offset) is not int or offset < 0:
        raise ValueError("navigation offset must be a nonnegative integer")


def ordered_navigation_items(
    items: tuple[NavigationItem, ...],
) -> tuple[NavigationItem, ...]:
    return tuple(
        sorted(
            items,
            key=lambda item: (
                item.span.path,
                item.span.start_line,
                item.span.start_column,
                item.span.end_line,
                item.span.end_column,
                item.identity,
            ),
        )
    )


def navigation_items(
    report: AnalysisReport, identities: frozenset[str]
) -> dict[str, tuple[NavigationItem, ...]]:
    selected: dict[str, tuple[NavigationItem, ...]] = {}
    if not identities:
        return selected
    _add_static_call_items(report, identities, selected)
    if len(selected) == len(identities):
        return selected
    for module in report.modules:
        if module.name in identities and module.name not in selected:
            selected[module.name] = tuple(
                NavigationItem(
                    identity=f"{module.name}:{symbol.qualified_name}",
                    label=symbol.qualified_name,
                    kind=symbol.kind,
                    span=symbol.span,
                )
                for symbol in module.symbols
            ) or (
                NavigationItem(
                    identity=module.name,
                    label=module.name,
                    kind="module",
                    span=SourceSpan(
                        path=module.path,
                        start_line=1,
                        start_column=0,
                        end_line=max(1, module.line_count),
                        end_column=0,
                        language=module.language,
                    ),
                ),
            )
        for symbol in module.symbols:
            identity = f"{module.name}:{symbol.qualified_name}"
            if identity in identities and identity not in selected:
                selected[identity] = (
                    NavigationItem(
                        identity=identity,
                        label=symbol.qualified_name,
                        kind=symbol.kind,
                        span=symbol.span,
                    ),
                )
    if len(selected) == len(identities):
        return selected
    for subject in report.subjects:
        identity = subject.subject_id
        if identity in identities and identity not in selected:
            selected[identity] = tuple(
                NavigationItem(
                    identity=identity,
                    label=subject.logical_name,
                    kind=subject.kind.value,
                    span=evidence.span,
                )
                for evidence in subject.evidence
            )
    if len(selected) == len(identities):
        return selected
    for resource in report.state_resources:
        identity = resource.state_resource_id
        if identity in identities and identity not in selected:
            selected[identity] = tuple(
                NavigationItem(
                    identity=resource.owner_subject_id,
                    label=resource.logical_name,
                    kind=resource.kind.value,
                    span=evidence.span,
                )
                for evidence in resource.declaration_evidence
            )
    if len(selected) == len(identities):
        return selected
    for effect in report.side_effects:
        identity = effect.side_effect_id
        if identity in identities and identity not in selected:
            selected[identity] = tuple(
                NavigationItem(
                    identity=effect.subject_id,
                    label=effect.target_identity,
                    kind=effect.kind.value,
                    span=evidence.span,
                )
                for evidence in effect.evidence
            )
    if len(selected) == len(identities):
        return selected
    _add_domain_items(report, identities, selected)
    return selected


def _add_static_call_items(
    report: AnalysisReport,
    identities: frozenset[str],
    selected: dict[str, tuple[NavigationItem, ...]],
) -> None:
    """Use exact saved invocation spans, never declaration or callee-name matches."""
    source_items: dict[str, list[NavigationItem]] = {}
    for occurrence in report.static_call_occurrences:
        if (
            occurrence.occurrence_id not in identities
            and occurrence.source_file_id not in identities
        ):
            continue
        item = NavigationItem(
            identity=occurrence.occurrence_id,
            label=occurrence.callee_spelling or "Dynamic callee",
            kind="static-call-site",
            span=occurrence.evidence[0].span,
        )
        if occurrence.occurrence_id in identities:
            selected[occurrence.occurrence_id] = (item,)
        if occurrence.source_file_id in identities:
            source_items.setdefault(occurrence.source_file_id, []).append(item)
    selected.update(
        (identity, ordered_navigation_items(tuple(items)))
        for identity, items in source_items.items()
    )


def _evidence_items(
    identity: str, label: str, kind: str, evidence: tuple[Evidence, ...]
) -> tuple[NavigationItem, ...]:
    return tuple(
        NavigationItem(identity=identity, label=label, kind=kind, span=span)
        for span in dict.fromkeys(item.span for item in evidence)
    )


def _add_domain_items(
    report: AnalysisReport,
    identities: frozenset[str],
    selected: dict[str, tuple[NavigationItem, ...]],
) -> None:
    for endpoint in report.interface_endpoints:
        identity = endpoint.endpoint_id
        if identity in identities and identity not in selected:
            selected[identity] = _evidence_items(
                identity, endpoint.logical_name, "interface-endpoint", endpoint.evidence
            )
    if len(selected) == len(identities):
        return
    for contract in report.contracts:
        identity = contract.contract_id
        if identity in identities and identity not in selected:
            selected[identity] = _evidence_items(
                identity, identity, "contract-evidence", contract.evidence
            )
    if len(selected) == len(identities):
        return
    for shape in report.dictionary_shapes:
        identity = shape.shape_id
        if identity in identities and identity not in selected:
            selected[identity] = _evidence_items(
                identity, identity, "dictionary-shape-evidence", shape.evidence
            )
        for state in shape.lifecycle_states:
            identity = state.state_id
            if identity in identities and identity not in selected:
                selected[identity] = _evidence_items(
                    identity, state.name, "observed-state", state.evidence
                )
    if len(selected) == len(identities):
        return
    for structure in report.data_structures:
        identity = structure.structure_id
        if identity in identities and identity not in selected:
            selected[identity] = _evidence_items(
                identity, identity, "data-structure-evidence", structure.evidence
            )
    if len(selected) == len(identities):
        return
    test_module_evidence: dict[str, list[Evidence]] = {}
    for case in report.test_cases:
        identity = case.test_id
        if identity in identities and identity not in selected:
            selected[identity] = _evidence_items(
                identity, case.qualified_name, "test", case.evidence
            )
        test_module_evidence.setdefault(case.test_module_id, []).extend(case.evidence)
    if len(selected) == len(identities):
        return
    for fixture in report.test_fixtures:
        identity = fixture.fixture_id
        if identity in identities and identity not in selected:
            selected[identity] = _evidence_items(
                identity, fixture.qualified_name, "fixture", fixture.evidence
            )
        test_module_evidence.setdefault(fixture.test_module_id, []).extend(fixture.evidence)
    if len(selected) == len(identities):
        return
    for relation in report.test_relationships:
        if relation.source_test_module_id is None:
            continue
        test_module_evidence.setdefault(relation.source_test_module_id, []).extend(
            relation.evidence
        )
    if len(selected) == len(identities):
        return
    for module in report.test_modules:
        identity = module.test_module_id
        if identity in identities and identity not in selected:
            selected[identity] = _evidence_items(
                identity,
                module.module,
                "test-module-evidence",
                tuple(test_module_evidence.get(identity, ())),
            )
