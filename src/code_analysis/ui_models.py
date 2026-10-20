"""Strict read-only contracts for the Phase 5 prototype interface."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.finding_suppression_models import (
    FindingSuppressionAnnotation,
    finding_suppression_summary,
)
from code_analysis.models import Completeness, Confidence, Diagnostic, SourceLanguage, StrictModel
from code_analysis.projection_models import ProjectionFindingCategory
from code_analysis.report_models import (
    AbstractionCostEstimate,
    AnalysisSubject,
    CandidateBehaviorDifference,
    CandidateQualificationRequirement,
    OwnerAlternative,
    RefactorCandidate,
    RefactorCandidateObservation,
    RefactorProposal,
    SectionCoverage,
    SideEffectObservation,
    StateResource,
)


class UIReportIdentity(StrictModel):
    role: Literal["primary", "comparison"]
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    run_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    target_root: StrictStr = Field(min_length=1)
    command: StrictStr = Field(min_length=1)
    completeness: Completeness


class UIEvidenceBinding(StrictModel):
    """Optional originating-view binding; never changes the static packet contract."""

    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    decision_ledger_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class UICount(StrictModel):
    label: StrictStr = Field(min_length=1)
    value: StrictInt = Field(ge=0)


class UICandidatePacket(StrictModel):
    offset: StrictInt = Field(ge=0, le=1_000_000)
    limit: StrictInt = Field(ge=1, le=50)
    totals: tuple[UICount, ...]
    next_offset: StrictInt | None = Field(ge=1, le=1_000_000)
    candidate: RefactorCandidate
    observation: RefactorCandidateObservation
    subjects: tuple[AnalysisSubject, ...] = Field(max_length=50)
    cost: AbstractionCostEstimate | None
    owners: tuple[OwnerAlternative, ...] = Field(max_length=50)
    proposals: tuple[RefactorProposal, ...] = Field(max_length=50)
    differences: tuple[CandidateBehaviorDifference, ...] = Field(max_length=50)
    qualification: tuple[CandidateQualificationRequirement, ...] = Field(max_length=50)
    omitted: tuple[UICount, ...]
    source_completeness: Completeness

    @model_validator(mode="after")
    def validate_candidate_packet(self) -> UICandidatePacket:
        records = (
            self.observation,
            *self.owners,
            *self.proposals,
            *self.differences,
            *self.qualification,
        )
        if any(item.candidate_id != self.candidate.candidate_id for item in records):
            raise ValueError("candidate packet contains evidence for a different candidate")
        if self.cost is not None and self.cost.candidate_id != self.candidate.candidate_id:
            raise ValueError("candidate packet contains cost for a different candidate")
        labels = tuple(item.label for item in self.omitted)
        if labels != ("owners", "proposals", "differences", "qualification", "subjects"):
            raise ValueError("candidate packet omission categories must be complete and ordered")
        if tuple(item.label for item in self.totals) != labels:
            raise ValueError("candidate packet totals must have complete ordered categories")
        for total, omitted in zip(self.totals, self.omitted, strict=True):
            retained = len(getattr(self, total.label))
            expected = min(self.limit, max(0, total.value - self.offset))
            if retained != expected or total.value != retained + omitted.value:
                raise ValueError("candidate packet page counts do not reconcile")
        largest = max(item.value for item in self.totals)
        expected_next = self.offset + self.limit if self.offset + self.limit < largest else None
        if self.next_offset != expected_next:
            raise ValueError("candidate packet continuation does not reconcile")
        if any(
            item.subject_id not in self.candidate.participating_subject_ids
            for item in self.subjects
        ):
            raise ValueError("candidate packet contains an unrelated subject")
        return self


class UIModuleRow(StrictModel):
    module: StrictStr = Field(min_length=1)
    path: StrictStr = Field(min_length=1)
    language: SourceLanguage
    lines: StrictInt = Field(ge=0)
    symbols: StrictInt = Field(ge=0)
    fan_in: StrictInt = Field(ge=0)
    fan_out: StrictInt = Field(ge=0)
    finding_count: StrictInt = Field(ge=0)


class UIFindingRow(StrictModel):
    finding_id: StrictStr = Field(min_length=1)
    language: SourceLanguage
    severity: Literal["info", "warning", "error"]
    category: StrictStr = Field(min_length=1)
    rule_id: StrictStr = Field(min_length=1)
    module: StrictStr = Field(min_length=1)
    symbol: StrictStr | None = None
    title: StrictStr = Field(min_length=1)
    explanation: StrictStr = Field(min_length=1)
    confidence: Confidence
    suppression: FindingSuppressionAnnotation
    suppression_summary: StrictStr = Field(min_length=1)

    @model_validator(mode="after")
    def validate_suppression_summary(self) -> Self:
        expected = finding_suppression_summary(self.suppression)
        if self.suppression_summary != expected:
            raise ValueError("suppression summary must match the typed suppression annotation")
        return self


class BrowseFacets(StrictModel):
    language: SourceLanguage | None = None
    module_prefix: StrictStr = Field(default="", max_length=2048)
    owner: StrictStr = Field(default="", max_length=2048)

    @model_validator(mode="after")
    def validate_literals(self) -> BrowseFacets:
        if any(ord(char) < 32 for char in self.module_prefix + self.owner):
            raise ValueError("facet identities must be single-line literals")
        if self.module_prefix and (
            self.module_prefix.startswith(("/", "."))
            or self.module_prefix.endswith(("/", "."))
            or "\\" in self.module_prefix
            or ".." in self.module_prefix
            or "//" in self.module_prefix
            or any(char in self.module_prefix for char in "*?[]")
        ):
            raise ValueError("module prefix must be a normalized logical identity, not a glob")
        return self

    def matches(self, language: SourceLanguage, module: str, owner: str = "") -> bool:
        separator = "." if language is SourceLanguage.PYTHON else "/"
        return (
            (self.language is None or self.language is language)
            and (not self.owner or self.owner == owner)
            and (
                not self.module_prefix
                or module == self.module_prefix
                or module.startswith(self.module_prefix + separator)
            )
        )


class FindingFilters(StrictModel):
    facets: BrowseFacets = BrowseFacets()
    severity: Literal["", "info", "warning", "error"] = ""
    category: ProjectionFindingCategory | None = None
    confidence: Confidence | None = None

    def matches(self, row: UIFindingRow) -> bool:
        return (
            (not self.severity or row.severity == self.severity)
            and (self.category is None or row.category == self.category.value)
            and (self.confidence is None or row.confidence is self.confidence)
            and self.facets.matches(row.language, row.module)
        )


class UIModuleScope(StrictModel):
    language: SourceLanguage
    module: StrictStr = Field(min_length=1)


class UICandidateRow(StrictModel):
    candidate_id: StrictStr = Field(min_length=1)
    kind: StrictStr = Field(min_length=1)
    state: StrictStr = Field(min_length=1)
    languages: tuple[SourceLanguage, ...]
    module_scopes: tuple[UIModuleScope, ...] = Field(min_length=1)
    subject_count: StrictInt = Field(ge=1)
    proposal_count: StrictInt = Field(ge=0)
    failed_gates: StrictInt = Field(ge=0)
    unresolved_gates: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_scopes(self) -> UICandidateRow:
        scopes = tuple((item.language.value, item.module) for item in self.module_scopes)
        if scopes != tuple(sorted(set(scopes))) or len(scopes) > self.subject_count:
            raise ValueError("candidate module scopes must be unique, sorted participant scopes")
        if self.languages != tuple(sorted({item.language for item in self.module_scopes})):
            raise ValueError("candidate languages must agree with participant module scopes")
        return self


class UIStateEffectRow(StrictModel):
    identity: StrictStr = Field(pattern=r"^(state-resource|side-effect)-[0-9a-f]{20}$")
    record_kind: Literal["resource", "side-effect"]
    category: StrictStr
    module: StrictStr = Field(min_length=1)
    owner: StrictStr = Field(min_length=1)
    language: SourceLanguage
    target: StrictStr
    confidence: Confidence
    completeness: Completeness
    detail: StateResource | SideEffectObservation

    @model_validator(mode="after")
    def reconcile_detail(self) -> UIStateEffectRow:
        if isinstance(self.detail, StateResource):
            expected_id = self.detail.state_resource_id
            expected_target = self.detail.logical_name
            kind = "resource"
            evidence = self.detail.declaration_evidence
        else:
            expected_id = self.detail.side_effect_id
            expected_target = self.detail.target_identity
            kind = "side-effect"
            evidence = self.detail.evidence
        if (
            self.identity != expected_id
            or self.record_kind != kind
            or self.category != self.detail.kind.value
            or self.target != expected_target
            or self.confidence is not self.detail.confidence
            or self.completeness is not self.detail.completeness
            or any(item.language is not self.language for item in evidence)
        ):
            raise ValueError("state/effect display fields disagree with typed evidence")
        return self


class UIProviderRow(StrictModel):
    provider_id: StrictStr = Field(min_length=1)
    state: StrictStr = Field(min_length=1)
    completeness: Completeness
    observations: tuple[StrictStr, ...] = ()


class UIComparisonCounts(StrictModel):
    identical: StrictBool
    added: StrictInt = Field(ge=0)
    removed: StrictInt = Field(ge=0)
    changed: StrictInt = Field(ge=0)
    unchanged: StrictInt = Field(ge=0)


class UIComparisonRow(StrictModel):
    position: StrictInt = Field(ge=0)
    record_kind: StrictStr = Field(min_length=1)
    subject_id: StrictStr = Field(min_length=1)
    change: StrictStr = Field(min_length=1)
    explanation: StrictStr = Field(min_length=1)


class UIGraphNode(StrictModel):
    node_id: StrictStr = Field(min_length=1)
    label: StrictStr = Field(min_length=1)
    language: SourceLanguage
    lines: StrictInt = Field(ge=0)
    finding_count: StrictInt = Field(ge=0)


class UIGraphEdge(StrictModel):
    source_id: StrictStr = Field(min_length=1)
    target_id: StrictStr = Field(min_length=1)
    kind: StrictStr = Field(min_length=1)


class UIPage(StrictModel):
    offset: StrictInt = Field(ge=0)
    limit: StrictInt = Field(ge=1, le=500)
    eligible: StrictInt = Field(ge=0)
    retained: StrictInt = Field(ge=0)
    truncated: StrictBool

    @model_validator(mode="after")
    def validate_page(self) -> UIPage:
        if self.retained > self.limit or self.offset + self.retained > self.eligible:
            raise ValueError("UI page counts do not reconcile")
        if self.truncated != (self.offset + self.retained < self.eligible):
            raise ValueError("UI page truncation does not reconcile")
        return self


class UIBootstrap(StrictModel):
    schema_name: Literal["code-analysis-ui"] = "code-analysis-ui"
    schema_version: Literal["1.0.0"] = "1.0.0"
    reports: tuple[UIReportIdentity, ...] = Field(min_length=1, max_length=2)
    counts: tuple[UICount, ...]
    languages: tuple[SourceLanguage, ...]
    limitations: tuple[StrictStr, ...]
    comparison: UIComparisonCounts | None = None
    finding_categories: tuple[ProjectionFindingCategory, ...]
    finding_confidences: tuple[Confidence, ...]

    @model_validator(mode="after")
    def validate_finding_facets(self) -> UIBootstrap:
        for values in (self.finding_categories, self.finding_confidences):
            if tuple(item.value for item in values) != tuple(
                sorted({item.value for item in values})
            ):
                raise ValueError("finding facets must be unique and sorted")
        return self


class UIModulePage(UIPage):
    records: tuple[UIModuleRow, ...]


class UIFindingPage(UIPage):
    records: tuple[UIFindingRow, ...]


class UICandidatePage(UIPage):
    records: tuple[UICandidateRow, ...]


class UIProviderPage(UIPage):
    records: tuple[UIProviderRow, ...]


class UIDiagnosticPage(UIPage):
    records: tuple[Diagnostic, ...]


class UIStateEffectPage(UIPage):
    records: tuple[UIStateEffectRow, ...]
    coverage: SectionCoverage

    @model_validator(mode="after")
    def validate_state_page(self) -> UIStateEffectPage:
        if self.coverage.section != "state-side-effects" or self.retained != len(self.records):
            raise ValueError("state/effect page coverage or row count mismatch")
        return self


class UIComparisonPage(UIPage):
    records: tuple[UIComparisonRow, ...]
    total_eligible: StrictInt = Field(ge=0)
    omitted: StrictInt = Field(ge=0)
    completeness: Completeness
    filtered_out: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_comparison_page(self) -> UIComparisonPage:
        if self.total_eligible != self.eligible + self.omitted + self.filtered_out:
            raise ValueError("comparison available and omitted counts do not reconcile")
        if self.retained != len(self.records):
            raise ValueError("comparison retained count must match records")
        return self


class UIArchitectureGraph(StrictModel):
    eligible_nodes: StrictInt = Field(ge=0)
    retained_nodes: StrictInt = Field(ge=0)
    omitted_nodes: StrictInt = Field(ge=0)
    eligible_edges: StrictInt = Field(ge=0)
    retained_edges: StrictInt = Field(ge=0)
    omitted_edges: StrictInt = Field(ge=0)
    truncated: StrictBool
    nodes: tuple[UIGraphNode, ...]
    edges: tuple[UIGraphEdge, ...]

    @model_validator(mode="after")
    def validate_graph(self) -> UIArchitectureGraph:
        if self.retained_nodes != len(self.nodes) or self.retained_edges != len(self.edges):
            raise ValueError("UI architecture retained counts do not reconcile")
        if self.eligible_nodes != self.retained_nodes + self.omitted_nodes:
            raise ValueError("UI architecture node counts do not reconcile")
        if self.eligible_edges != self.retained_edges + self.omitted_edges:
            raise ValueError("UI architecture edge counts do not reconcile")
        known = {item.node_id for item in self.nodes}
        if len(known) != len(self.nodes):
            raise ValueError("UI architecture node IDs must be unique")
        if any(item.source_id not in known or item.target_id not in known for item in self.edges):
            raise ValueError("UI architecture edges require retained endpoints")
        if self.truncated != bool(self.omitted_nodes or self.omitted_edges):
            raise ValueError("UI architecture truncation does not reconcile")
        return self
