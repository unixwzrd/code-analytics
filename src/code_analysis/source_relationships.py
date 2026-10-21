"""Bounded relationship inspection over validated saved reports, never source scans."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.code_browse import (
    CodeBrowseBindingError,
    CodeBrowseEntry,
    CodeBrowseReader,
    CodeBrowseRequest,
    CodeBrowseSelectionError,
)
from code_analysis.models import Completeness, Evidence, SourceLanguage, SourceSpan, StrictModel
from code_analysis.report_models import AnalysisReport, AnalysisSubject, FlowKind
from code_analysis.reports.json_report import canonical_json

type RelationshipDirection = Literal["caller", "callee", "structure"]
type RelationshipBasis = Literal[
    "lexical-call-occurrence", "recorded-callee-name", "saved-flow", "structure-role"
]
type RelationshipResolution = Literal[
    "name-matched", "resolved", "unresolved", "ambiguous", "truncated"
]


class SourceRelationshipRequest(CodeBrowseRequest):
    """Use the outline's exact saved identity/path contract, with a display bound."""

    max_records: StrictInt = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def require_identity(self) -> Self:
        if not self.identity:
            raise ValueError("relationship inspection requires a saved identity")
        return self


class SourceRelationship(StrictModel):
    relationship_id: StrictStr = Field(pattern=r"^source-relationship-[0-9a-f]{20}$")
    direction: RelationshipDirection
    kind: StrictStr = Field(min_length=1)
    basis: RelationshipBasis
    resolution: RelationshipResolution
    label: StrictStr = Field(min_length=1)
    record_id: StrictStr = Field(min_length=1)
    target: CodeBrowseEntry | None = None
    spans: tuple[SourceSpan, ...] = ()


class SourceRelationshipPage(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    revision: StrictInt = Field(ge=0)
    selected: CodeBrowseEntry
    completeness: Completeness
    records: tuple[SourceRelationship, ...] = Field(max_length=500)
    eligible: StrictInt = Field(ge=0)
    omitted: StrictInt = Field(ge=0)
    truncated: StrictBool
    limitations: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def reconcile(self) -> Self:
        ids = tuple(row.relationship_id for row in self.records)
        if len(ids) != len(set(ids)):
            raise ValueError("relationship identities must be unique")
        if self.eligible != len(self.records) + self.omitted:
            raise ValueError("relationship counts must reconcile")
        if self.truncated != bool(self.omitted):
            raise ValueError("relationship truncation disagrees with omitted count")
        if self.truncated and self.completeness is Completeness.COMPLETE:
            raise ValueError("bounded relationship omissions cannot be complete")
        if self.limitations != tuple(sorted(set(self.limitations))):
            raise ValueError("relationship limitations must be sorted and unique")
        return self


@dataclass(frozen=True, slots=True)
class _Context:
    reader: CodeBrowseReader
    selected: CodeBrowseEntry
    subject_ids: frozenset[str]
    by_name: dict[tuple[SourceLanguage, str], list[AnalysisSubject]]

    def entry(self, identity: str) -> CodeBrowseEntry | None:
        return self.reader.identity_map.get(self.reader.aliases.get(identity, identity))


def _row(
    direction: RelationshipDirection,
    kind: str,
    basis: RelationshipBasis,
    resolution: RelationshipResolution,
    label: str,
    record_id: str,
    target: CodeBrowseEntry | None,
    evidence: tuple[Evidence, ...],
) -> SourceRelationship:
    digest = hashlib.sha256(
        repr((direction, kind, basis, record_id, target.identity if target else label)).encode()
    ).hexdigest()[:20]
    return SourceRelationship(
        relationship_id="source-relationship-" + digest,
        direction=direction,
        kind=kind,
        basis=basis,
        resolution=resolution,
        label=label,
        record_id=record_id,
        target=target,
        spans=tuple(dict.fromkeys(item.span for item in evidence)),
    )


def _name_target(
    context: _Context, language: SourceLanguage, name: str
) -> tuple[RelationshipResolution, CodeBrowseEntry | None]:
    matches = context.by_name.get((language, name), ())
    if len(matches) != 1:
        return ("ambiguous" if matches else "unresolved"), None
    return "name-matched", context.entry(matches[0].subject_id)


def _calls(report: AnalysisReport, context: _Context) -> list[SourceRelationship]:
    rows: list[SourceRelationship] = []
    for profile in report.function_profiles:
        for name in profile.callees:
            resolution, target = _name_target(context, profile.language, name)
            if profile.subject_id in context.subject_ids:
                rows.append(
                    _row(
                        "callee",
                        "callee-name",
                        "recorded-callee-name",
                        resolution,
                        name,
                        profile.profile_id,
                        target,
                        profile.evidence,
                    )
                )
            if target and target.identity == context.selected.identity:
                caller = context.entry(profile.subject_id)
                rows.append(
                    _row(
                        "caller",
                        "callee-name",
                        "recorded-callee-name",
                        resolution,
                        caller.label if caller else profile.subject_id,
                        profile.profile_id,
                        caller,
                        profile.evidence,
                    )
                )
    for occurrence in report.static_call_occurrences:
        callee_name = occurrence.callee_spelling
        resolution, target = (
            _name_target(context, occurrence.language, callee_name)
            if callee_name
            else ("unresolved", None)
        )
        if occurrence.enclosing_subject_id in context.subject_ids:
            rows.append(
                _row(
                    "callee",
                    "call-site",
                    "lexical-call-occurrence",
                    resolution,
                    callee_name or "Computed/unnamed call",
                    occurrence.occurrence_id,
                    target,
                    occurrence.evidence,
                )
            )
        if target and target.identity == context.selected.identity:
            caller = context.entry(occurrence.enclosing_subject_id or "")
            rows.append(
                _row(
                    "caller",
                    "call-site",
                    "lexical-call-occurrence",
                    resolution,
                    caller.label if caller else "Unmapped lexical caller",
                    occurrence.occurrence_id,
                    caller,
                    occurrence.evidence,
                )
            )
    return rows


def _structures(report: AnalysisReport, context: _Context) -> list[SourceRelationship]:
    rows: list[SourceRelationship] = []
    conversion_ids: set[str] = set()
    for structure in report.data_structures:
        if structure.structure_id != context.selected.identity:
            continue
        conversion_ids.update(structure.conversion_flow_ids)
        for role, identities in (
            ("constructor", structure.constructor_subject_ids),
            ("producer", structure.producer_subject_ids),
            ("reader", structure.reader_subject_ids),
            ("writer", structure.writer_subject_ids),
            ("mutator", structure.mutator_subject_ids),
            ("serializer", structure.serializer_subject_ids),
            ("alias", structure.alias_subject_ids),
        ):
            for identity in identities:
                target = context.entry(identity)
                rows.append(
                    _row(
                        "structure",
                        role,
                        "structure-role",
                        "resolved" if target else "unresolved",
                        target.label if target else identity,
                        structure.structure_id,
                        target,
                        structure.evidence,
                    )
                )
    selected_ids = context.subject_ids | {context.selected.identity}
    for flow in report.flow_edges:
        if flow.source_id not in selected_ids and flow.target_id not in selected_ids:
            if flow.flow_id in conversion_ids:
                for role, identity in (("source", flow.source_id), ("target", flow.target_id)):
                    target = context.entry(identity)
                    rows.append(
                        _row(
                            "structure",
                            f"conversion-{role}",
                            "saved-flow",
                            flow.resolution.value,
                            target.label if target else identity,
                            flow.flow_id,
                            target,
                            flow.evidence,
                        )
                    )
            continue
        incoming = flow.target_id in selected_ids
        target_id = flow.source_id if incoming else flow.target_id
        target = context.entry(target_id)
        direction: RelationshipDirection = (
            ("caller" if incoming else "callee") if flow.kind is FlowKind.CALL else "structure"
        )
        rows.append(
            _row(
                direction,
                flow.kind.value,
                "saved-flow",
                flow.resolution.value,
                target.label if target else target_id,
                flow.flow_id,
                target,
                flow.evidence,
            )
        )
    return rows


def read_source_relationships(
    report: AnalysisReport,
    request: SourceRelationshipRequest,
    *,
    reader: CodeBrowseReader | None = None,
) -> SourceRelationshipPage:
    """Inspect retained records, optionally using the owning service's prepared reader.

    A prepared reader must belong to this report's validated service state. Standalone
    callers omit it to compute and verify the canonical report binding here.
    """
    digest = (
        reader.report_sha256
        if reader is not None
        else hashlib.sha256(canonical_json(report).encode()).hexdigest()
    )
    if request.report_sha256 != digest:
        raise CodeBrowseBindingError("relationship request is bound to another report")
    reader = reader or CodeBrowseReader.from_report(report, digest)
    page = reader.read(request)
    if page.selected is None:
        raise CodeBrowseSelectionError("choose one recorded source location for this identity")
    subject_ids = frozenset(
        item.subject_id
        for item in report.subjects
        if reader.aliases.get(item.subject_id, item.subject_id) == page.selected.identity
    )
    names: dict[tuple[SourceLanguage, str], list[AnalysisSubject]] = defaultdict(list)
    for subject in report.subjects:
        names[(subject.language, subject.logical_name)].append(subject)
    context = _Context(reader, page.selected, subject_ids, names)
    rows = _calls(report, context) + _structures(report, context)
    rows = sorted(
        {row.relationship_id: row for row in rows}.values(),
        key=lambda row: (row.direction, row.basis, row.kind, row.label, row.relationship_id),
    )
    omitted = max(0, len(rows) - request.max_records)
    limitations = {
        "Name-matched targets are exact same-language saved names, not proven runtime dispatch.",
        "Lexical call sites and unique callee-name summaries are separate evidence, "
        "not invocation counts.",
        "No runtime profile, variable values, complete def/use tracing "
        "or new source analysis is included.",
        "Zero relationships means no retained matches, not proof "
        "that no calls or data relationships exist.",
        "Validation is shown only through explicit saved flow endpoints; "
        "sharing a containing function does not prove validation of a selected structure.",
    }
    relevant_sections = {"function-commonality", "data-structures", "static-call-occurrences"}
    for section in report.section_coverage:
        if section.section in relevant_sections:
            limitations.add(f"Saved {section.section} section: {section.state.value}.")
    for coverage in report.static_call_coverage:
        if coverage.source_file_id == page.selected.source_file_id:
            limitations.add(f"Selected file lexical-call coverage: {coverage.state.value}.")
            limitations.update(coverage.limitations)
    for profile in report.function_profiles:
        if profile.subject_id in subject_ids:
            limitations.add(
                f"Selected function profile records {profile.unresolved_call_count} "
                "unresolved calls; these are not invented as named relationships."
            )
            limitations.update(profile.limitations)
    if omitted:
        limitations.add(f"Display bound omitted {omitted} of {len(rows)} saved relationships.")
    return SourceRelationshipPage(
        report_sha256=digest,
        revision=request.revision,
        selected=page.selected,
        completeness=(
            Completeness.PARTIAL
            if omitted and report.completeness is Completeness.COMPLETE
            else report.completeness
        ),
        records=tuple(rows[: request.max_records]),
        eligible=len(rows),
        omitted=omitted,
        truncated=bool(omitted),
        limitations=tuple(sorted(limitations)),
    )
