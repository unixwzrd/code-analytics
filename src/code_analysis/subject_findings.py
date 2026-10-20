"""Subject/finding boundary facts and the shared subject-findings aggregation.

The subject-findings projection must produce identical rows whether its inputs
come from a freshly parsed :class:`AnalysisReport` or from a persisted index
that stores only these minimal records.  Conversion from a report happens once
in :func:`subject_finding_facts`; every consumer then calls
:func:`aggregate_subject_findings`, so there is exactly one implementation of
the filtering, grouping and ordering rules.

The facts deliberately carry identities and counts only: no raw source, no
embedded evidence payloads and no untyped dictionaries.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Self

from pydantic import Field, StrictStr, model_validator

from code_analysis.models import SourceLanguage, StrictModel, evidence_identity
from code_analysis.projection_models import (
    ColumnAlignment,
    ProjectionFindingCategory,
    ProjectionFindingSeverity,
    ProjectionScalar,
    ProjectionSelection,
    ScalarKind,
    SortDirection,
    TableColumn,
    TableRow,
    TableSort,
)
from code_analysis.report_models import AnalysisReport, SubjectKind

__all__ = (
    "SUBJECT_FINDING_COLUMN_DEFINITIONS",
    "SUBJECT_FINDING_SORT",
    "FindingFact",
    "SubjectFact",
    "SubjectFindingAggregate",
    "SubjectFindingFacts",
    "SubjectFindingSelectionError",
    "aggregate_subject_findings",
    "subject_finding_columns",
    "subject_finding_facts",
    "unscoped_scope_limitation",
)

SUBJECT_ID_PATTERN = r"^subject-[0-9a-f]{20}$"
PROPOSAL_ID_PATTERN = r"^refactor-proposal-[0-9a-f]{20}$"
EVIDENCE_ID_PATTERN = r"^evidence-[0-9a-f]{20}$"

SUBJECT_FINDING_COLUMN_DEFINITIONS: tuple[tuple[str, str, ScalarKind], ...] = (
    ("subject-id", "Subject ID", ScalarKind.STRING),
    ("kind", "Kind", ScalarKind.STRING),
    ("module", "Module", ScalarKind.STRING),
    ("logical-name", "Logical name", ScalarKind.STRING),
    ("finding-count", "Findings", ScalarKind.INTEGER),
    ("errors", "Errors", ScalarKind.INTEGER),
    ("warnings", "Warnings", ScalarKind.INTEGER),
    ("info", "Info", ScalarKind.INTEGER),
    ("category-counts", "Category counts", ScalarKind.STRING),
    ("rule-counts", "Rule counts", ScalarKind.STRING),
    ("evidence-count", "Unique evidence", ScalarKind.INTEGER),
    ("affected-proposals", "Affected proposals", ScalarKind.INTEGER),
)

SUBJECT_FINDING_SORT: tuple[TableSort, ...] = (
    TableSort(column_id="finding-count", direction=SortDirection.DESCENDING),
    TableSort(column_id="errors", direction=SortDirection.DESCENDING),
    TableSort(column_id="warnings", direction=SortDirection.DESCENDING),
    TableSort(column_id="module"),
    TableSort(column_id="logical-name"),
    TableSort(column_id="subject-id"),
)


class SubjectFindingSelectionError(ValueError):
    """A subject-findings selection cannot be resolved against the given facts."""


def _require_sorted_unique(label: str, values: tuple[str, ...]) -> None:
    if list(values) != sorted(values) or len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique and sorted")


class SubjectFact(StrictModel):
    """Identity of one analysis subject plus its proposal associations."""

    subject_id: StrictStr = Field(pattern=SUBJECT_ID_PATTERN)
    kind: SubjectKind
    module: StrictStr = Field(min_length=1)
    logical_name: StrictStr = Field(min_length=1)
    language: SourceLanguage
    proposal_ids: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        for value in self.proposal_ids:
            if not value.startswith("refactor-proposal-"):
                raise ValueError("subject proposal IDs must be proposal identities")
        _require_sorted_unique("subject proposal IDs", self.proposal_ids)
        return self


class FindingFact(StrictModel):
    """Identity, classification and associations of one finding."""

    finding_id: StrictStr = Field(min_length=1)
    category: ProjectionFindingCategory
    severity: ProjectionFindingSeverity
    rule_id: StrictStr = Field(min_length=1)
    subject_ids: tuple[StrictStr, ...] = ()
    evidence_ids: tuple[StrictStr, ...] = ()
    proposal_ids: tuple[StrictStr, ...] = ()
    language: SourceLanguage

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        _require_sorted_unique("finding subject IDs", self.subject_ids)
        _require_sorted_unique("finding evidence IDs", self.evidence_ids)
        _require_sorted_unique("finding proposal IDs", self.proposal_ids)
        for value in self.proposal_ids:
            if not value.startswith("refactor-proposal-"):
                raise ValueError("finding proposal IDs must be proposal identities")
        for value in self.evidence_ids:
            if not value.startswith("evidence-"):
                raise ValueError("finding evidence IDs must be evidence identities")
        return self


class SubjectFindingFacts(StrictModel):
    """The complete minimal input the subject-findings aggregation consumes."""

    subjects: tuple[SubjectFact, ...] = ()
    findings: tuple[FindingFact, ...] = ()

    @model_validator(mode="after")
    def validate_identities(self) -> Self:
        subject_ids = [subject.subject_id for subject in self.subjects]
        if len(subject_ids) != len(set(subject_ids)):
            raise ValueError("subject facts must have unique subject identities")
        finding_ids = [finding.finding_id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding facts must have unique finding identities")
        known = set(subject_ids)
        dangling = sorted(
            {
                subject_id
                for finding in self.findings
                for subject_id in finding.subject_ids
                if subject_id not in known
            }
        )
        if dangling:
            raise ValueError(
                "finding facts reference unknown subject identities: " + ", ".join(dangling)
            )
        return self

    def subjects_by_id(self) -> dict[str, SubjectFact]:
        return {subject.subject_id: subject for subject in self.subjects}


@dataclass(frozen=True, slots=True)
class SubjectFindingAggregate:
    """Ordered subject rows plus the disclosed unscoped finding count."""

    rows: tuple[TableRow, ...]
    unscoped_finding_count: int


def subject_finding_facts(report: AnalysisReport) -> SubjectFindingFacts:
    """Convert one report into subject-findings boundary facts exactly once."""

    proposals_by_subject: dict[str, set[str]] = {}
    for proposal in report.refactor_proposals:
        for subject_id in proposal.affected_subject_ids:
            proposals_by_subject.setdefault(subject_id, set()).add(proposal.proposal_id)
    subjects = tuple(
        SubjectFact(
            subject_id=subject.subject_id,
            kind=subject.kind,
            module=subject.module,
            logical_name=subject.logical_name,
            language=subject.language,
            proposal_ids=tuple(sorted(proposals_by_subject.get(subject.subject_id, ()))),
        )
        for subject in report.subjects
    )
    findings = tuple(
        FindingFact(
            finding_id=finding.finding_id,
            category=ProjectionFindingCategory(finding.category),
            severity=ProjectionFindingSeverity(finding.severity),
            rule_id=finding.rule_id,
            subject_ids=tuple(sorted(set(finding.subject_ids))),
            evidence_ids=tuple(
                sorted(
                    {
                        evidence_identity(evidence)
                        for evidence in (*finding.evidence, *finding.counter_evidence)
                    }
                )
            ),
            proposal_ids=tuple(sorted(set(finding.affected_proposal_ids))),
            language=finding.language,
        )
        for finding in report.findings
    )
    return SubjectFindingFacts(subjects=subjects, findings=findings)


def subject_finding_columns() -> tuple[TableColumn, ...]:
    """Return the shared subject-findings table columns."""

    return tuple(
        TableColumn(
            column_id=column_id,
            label=label,
            position=position,
            value_kind=kind,
            alignment=(
                ColumnAlignment.RIGHT if kind is ScalarKind.INTEGER else ColumnAlignment.LEFT
            ),
        )
        for position, (column_id, label, kind) in enumerate(SUBJECT_FINDING_COLUMN_DEFINITIONS)
    )


def unscoped_scope_limitation(count: int) -> str:
    """Return the disclosure for findings that carry no subject identity."""

    return f"{count} findings without subject identities are omitted from this scoped view."


def _string(value: str) -> ProjectionScalar:
    return ProjectionScalar(kind=ScalarKind.STRING, string_value=value)


def _integer(value: int) -> ProjectionScalar:
    return ProjectionScalar(kind=ScalarKind.INTEGER, integer_value=value)


def _selected_findings(
    facts: SubjectFindingFacts, selection: ProjectionSelection
) -> tuple[FindingFact, ...]:
    categories = {item.value for item in selection.finding_categories}
    severities = {item.value for item in selection.finding_severities}
    rule_ids = set(selection.finding_rule_ids)
    return tuple(
        finding
        for finding in facts.findings
        if (not categories or finding.category.value in categories)
        and (not severities or finding.severity.value in severities)
        and (not rule_ids or finding.rule_id in rule_ids)
    )


def _matches_prefix(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def _selected_subject_ids(
    facts: SubjectFindingFacts,
    selection: ProjectionSelection,
    subjects: dict[str, SubjectFact],
    findings: tuple[FindingFact, ...],
) -> set[str]:
    if selection.selected_ids:
        missing = set(selection.selected_ids) - set(subjects)
        if missing:
            raise SubjectFindingSelectionError(
                "selected subject is absent from the report: " + ", ".join(sorted(missing))
            )
        return set(selection.selected_ids)
    subject_ids = {subject_id for finding in findings for subject_id in finding.subject_ids}
    if selection.module_prefixes:
        subject_ids = {
            subject_id
            for subject_id in subject_ids
            if _matches_prefix(subjects[subject_id].module, selection.module_prefixes)
        }
    return subject_ids


def aggregate_subject_findings(
    facts: SubjectFindingFacts,
    selection: ProjectionSelection,
) -> SubjectFindingAggregate:
    """Aggregate findings per subject into deterministically ordered table rows.

    Finding-level filtering happens before grouping, so counts and the
    disclosed unscoped total always describe the same filtered record set.
    Categories, severities and rule IDs are OR within one filter and AND
    across filters.  ``selected_ids`` keeps explicitly requested subjects even
    when they match no finding; ``module_prefixes`` matches a module exactly or
    on a dotted prefix boundary. Language scope is not implemented for this
    projection and is rejected rather than silently returning unfiltered rows.
    """

    if not isinstance(facts, SubjectFindingFacts):
        raise TypeError("facts must be SubjectFindingFacts")
    if not isinstance(selection, ProjectionSelection):
        raise TypeError("selection must be a validated ProjectionSelection")
    if selection.languages:
        raise SubjectFindingSelectionError(
            "language filtering is unsupported for the subject-findings projection"
        )
    subjects = facts.subjects_by_id()
    findings = _selected_findings(facts, selection)
    subject_ids = _selected_subject_ids(facts, selection, subjects, findings)
    findings_by_subject: dict[str, list[FindingFact]] = {
        subject_id: [] for subject_id in subject_ids
    }
    for finding in findings:
        for subject_id in finding.subject_ids:
            records = findings_by_subject.get(subject_id)
            if records is not None:
                records.append(finding)
    rows: list[TableRow] = []
    order: list[tuple[int, int, int, str, str, str]] = []
    for subject_id in sorted(subject_ids):
        subject = subjects[subject_id]
        subject_findings = findings_by_subject[subject_id]
        severity_counts = Counter(finding.severity.value for finding in subject_findings)
        category_counts = Counter(finding.category.value for finding in subject_findings)
        rule_counts = Counter(finding.rule_id for finding in subject_findings)
        evidence_ids = {
            evidence_id for finding in subject_findings for evidence_id in finding.evidence_ids
        }
        affected_proposals = set(subject.proposal_ids) | {
            proposal_id for finding in subject_findings for proposal_id in finding.proposal_ids
        }
        order.append(
            (
                -len(subject_findings),
                -severity_counts["error"],
                -severity_counts["warning"],
                subject.module,
                subject.logical_name,
                subject_id,
            )
        )
        rows.append(
            TableRow(
                row_id=subject_id,
                cells=(
                    _string(subject_id),
                    _string(subject.kind.value),
                    _string(subject.module),
                    _string(subject.logical_name),
                    _integer(len(subject_findings)),
                    _integer(severity_counts["error"]),
                    _integer(severity_counts["warning"]),
                    _integer(severity_counts["info"]),
                    _string(
                        ", ".join(
                            f"{key}={category_counts[key]}" for key in sorted(category_counts)
                        )
                    ),
                    _string(", ".join(f"{key}={rule_counts[key]}" for key in sorted(rule_counts))),
                    _integer(len(evidence_ids)),
                    _integer(len(affected_proposals)),
                ),
                evidence_ids=tuple(sorted(evidence_ids)),
            )
        )
    ordered = tuple(row for _, row in sorted(zip(order, rows, strict=True), key=lambda p: p[0]))
    unscoped = sum(1 for finding in findings if not finding.subject_ids)
    return SubjectFindingAggregate(rows=ordered, unscoped_finding_count=unscoped)
