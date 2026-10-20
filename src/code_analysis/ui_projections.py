"""Typed saved-report projection requests shared by human interfaces."""

from __future__ import annotations

import hashlib
from typing import TypedDict

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.candidate_policy import DECISION_RANK
from code_analysis.decisions import effective_decisions, ledger_sha256
from code_analysis.models import Completeness, SourceLanguage, StrictModel
from code_analysis.projection_models import (
    GraphProjection,
    HierarchyProjection,
    Projection,
    ProjectionCollapse,
    ProjectionFindingCategory,
    ProjectionFindingSeverity,
    ProjectionSelection,
)
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.projections.candidates import build_candidate_worklist_projection
from code_analysis.report_models import (
    AnalysisReport,
    RefactorCandidateKind,
    RefactorCandidateState,
)
from code_analysis.reports.json_report import canonical_json
from code_analysis.ui_decisions import DecisionSnapshot
from code_analysis.ui_navigation import NavigationUnavailable, navigation_page


class CandidateProjectionOptions(TypedDict):
    candidate_module_patterns: tuple[str, ...]
    candidate_exclude_patterns: tuple[str, ...]
    candidate_kinds: tuple[RefactorCandidateKind, ...]
    candidate_states: tuple[str, ...]
    candidate_decisions: tuple[str, ...]
    cross_module_only: bool
    production_only: bool
    min_shared_dimensions: int
    prefer_existing_owner: bool


class FindingProjectionOptions(TypedDict):
    module_prefixes: tuple[str, ...]
    finding_categories: tuple[ProjectionFindingCategory, ...]
    finding_severities: tuple[ProjectionFindingSeverity, ...]
    finding_rule_ids: tuple[str, ...]


class UIProjectionRequest(StrictModel):
    name: ProjectionName
    selected_ids: tuple[str, ...] = ()
    languages: tuple[SourceLanguage, ...] = ()
    max_records: StrictInt = Field(default=100, ge=1, le=500)
    max_nodes: StrictInt = Field(default=50, ge=1, le=100)
    max_edges: StrictInt = Field(default=100, ge=0, le=250)
    depth: StrictInt | None = Field(default=None, ge=0, le=5)
    collapse: ProjectionCollapse = ProjectionCollapse.NONE
    module_prefixes: tuple[StrictStr, ...] = Field(default=(), max_length=100)
    finding_categories: tuple[ProjectionFindingCategory, ...] = ()
    finding_severities: tuple[ProjectionFindingSeverity, ...] = ()
    finding_rule_ids: tuple[StrictStr, ...] = Field(default=(), max_length=100)
    candidate_module_patterns: tuple[StrictStr, ...] = Field(default=(), max_length=100)
    candidate_exclude_patterns: tuple[StrictStr, ...] = Field(default=(), max_length=100)
    candidate_kinds: tuple[RefactorCandidateKind, ...] = ()
    candidate_states: tuple[StrictStr, ...] = ()
    candidate_decisions: tuple[StrictStr, ...] = ()
    decision_ledger_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    decision_report_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    cross_module_only: StrictBool = False
    production_only: StrictBool = False
    min_shared_dimensions: StrictInt = Field(default=0, ge=0, le=32)
    prefer_existing_owner: StrictBool = False

    @model_validator(mode="after")
    def validate_candidate_policy(self) -> UIProjectionRequest:
        options = self.candidate_options()
        if (self.decision_ledger_sha256 is None) != (self.decision_report_sha256 is None):
            raise ValueError("decision selection requires report and ledger snapshot identities")
        if (
            self.decision_ledger_sha256 is not None
            and self.name is not ProjectionName.CANDIDATE_WORKLIST
        ):
            raise ValueError("decision ledger selection requires the candidate worklist")
        if self.candidate_decisions and self.decision_ledger_sha256 is None:
            raise ValueError("decision filters require explicit ledger selection")
        if set(self.candidate_decisions) - set(DECISION_RANK):
            raise ValueError("unknown candidate decision")
        if self.name is not ProjectionName.CANDIDATE_WORKLIST and any(options.values()):
            raise ValueError("candidate filters require the candidate-worklist projection")
        # Reuse the public selection contract, including portable pattern validation.
        ProjectionSelection(**options)
        if set(self.candidate_states) - {state.value for state in RefactorCandidateState}:
            raise ValueError("unknown candidate state")
        if any(
            len(pattern) > 2048 or any(ord(char) < 32 for char in pattern)
            for pattern in (*self.candidate_module_patterns, *self.candidate_exclude_patterns)
        ):
            raise ValueError("candidate patterns must be bounded single-line logical patterns")
        return self

    @model_validator(mode="after")
    def validate_finding_filters(self) -> UIProjectionRequest:
        options = self.finding_options()
        if self.name is not ProjectionName.SUBJECT_FINDINGS and any(options.values()):
            raise ValueError("finding filters require the subject-findings projection")
        # The shared subject-finding aggregation never subsets records by language, so a
        # language scope here would be silently ignored rather than applied.
        if self.name is ProjectionName.SUBJECT_FINDINGS and self.languages:
            raise ValueError(
                "language selection is unsupported for the subject-findings projection: "
                "findings are aggregated across all recorded languages"
            )
        if self.selected_ids and self.module_prefixes:
            raise ValueError(
                "exact subject selection cannot be combined with module-prefix selection"
            )
        # Reuse the public selection contract for ordering, prefixes and rule IDs.
        ProjectionSelection(**options)
        if any(
            len(value) > 2048 or any(ord(char) < 32 for char in value)
            for value in (*self.module_prefixes, *self.finding_rule_ids)
        ):
            raise ValueError("finding filters must be bounded single-line values")
        return self

    def finding_options(self) -> FindingProjectionOptions:
        return {
            "module_prefixes": self.module_prefixes,
            "finding_categories": self.finding_categories,
            "finding_severities": self.finding_severities,
            "finding_rule_ids": self.finding_rule_ids,
        }

    def candidate_options(self) -> CandidateProjectionOptions:
        return {
            "candidate_module_patterns": self.candidate_module_patterns,
            "candidate_exclude_patterns": self.candidate_exclude_patterns,
            "candidate_kinds": self.candidate_kinds,
            "candidate_states": self.candidate_states,
            "candidate_decisions": self.candidate_decisions,
            "cross_module_only": self.cross_module_only,
            "production_only": self.production_only,
            "min_shared_dimensions": self.min_shared_dimensions,
            "prefer_existing_owner": self.prefer_existing_owner,
        }


class UIProjectionCatalog(StrictModel):
    names: tuple[ProjectionName, ...] = tuple(ProjectionName)
    candidate_kinds: tuple[RefactorCandidateKind, ...] = tuple(RefactorCandidateKind)
    candidate_decisions: tuple[StrictStr, ...] = tuple(DECISION_RANK)
    finding_categories: tuple[ProjectionFindingCategory, ...] = tuple(ProjectionFindingCategory)
    finding_severities: tuple[ProjectionFindingSeverity, ...] = tuple(ProjectionFindingSeverity)


class UIGraphNodeSelection(StrictModel):
    projection_id: StrictStr = Field(pattern=r"^projection-[0-9a-f]{20}$")
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    node_id: StrictStr = Field(min_length=1)
    label: StrictStr
    node_kind: StrictStr
    completeness: Completeness
    navigation_identity: StrictStr | None = None
    unavailable_reason: StrictStr | None = None

    @model_validator(mode="after")
    def reconcile(self) -> UIGraphNodeSelection:
        if (self.navigation_identity is None) == (self.unavailable_reason is None):
            raise ValueError(
                "graph selection requires navigation or an explicit unavailable reason"
            )
        if self.navigation_identity is not None and self.navigation_identity != self.node_id:
            raise ValueError("graph navigation must retain the selected saved identity")
        if self.unavailable_reason is not None and not self.unavailable_reason.strip():
            raise ValueError("unavailable navigation requires a nonempty reason")
        return self


def graph_node_selection(
    report: AnalysisReport, request: UIProjectionRequest, projection_id: str, node_id: str
) -> UIGraphNodeSelection:
    projection = project_report(report, request)
    if (
        not isinstance(projection, (GraphProjection, HierarchyProjection))
        or projection.projection_id != projection_id
    ):
        raise ValueError("graph selection does not match the current saved projection")
    node = next((node for node in projection.nodes if node.node_id == node_id), None)
    if node is None:
        raise ValueError("selected node is not retained in this graph")
    reason = None
    if node.kind in {"package", "unresolved-target"}:
        reason = "This logical group or unresolved target has no proven source declaration."
    else:
        try:
            page = navigation_page(report, node.node_id)
        except NavigationUnavailable:
            reason = "No source-navigation mapping is recorded for this graph node."
        else:
            if not page.eligible:
                reason = "This saved identity has no recorded source spans."
    return UIGraphNodeSelection(
        projection_id=projection.projection_id,
        report_sha256=projection.inputs[0].report_sha256,
        node_id=node.node_id,
        label=node.label,
        node_kind=node.kind,
        completeness=projection.completeness,
        navigation_identity=node.node_id if reason is None else None,
        unavailable_reason=reason,
    )


def project_report(
    report: AnalysisReport, request: UIProjectionRequest, decisions: DecisionSnapshot | None = None
) -> Projection:
    if request.decision_ledger_sha256 is not None:
        if (
            decisions is None
            or decisions.ledger_sha256 != request.decision_ledger_sha256
            or decisions.report_sha256 != request.decision_report_sha256
            or ledger_sha256(decisions.ledger) != decisions.ledger_sha256
            or hashlib.sha256(canonical_json(report).encode()).hexdigest()
            != decisions.report_sha256
        ):
            raise ValueError("decision snapshot changed or does not match the selected report")
        if (
            request.selected_ids
            or request.depth is not None
            or request.collapse is not ProjectionCollapse.NONE
        ):
            raise ValueError("candidate worklist uses its dedicated candidate filters")
        return build_candidate_worklist_projection(
            report,
            ProjectionSelection(
                **request.candidate_options(),
                languages=request.languages,
                max_records=request.max_records,
                max_nodes=request.max_nodes,
                max_edges=request.max_edges,
            ),
            effective_decisions(decisions.ledger, report),
            decisions.ledger_sha256,
        )
    if decisions is not None:
        raise ValueError("decision snapshot requires explicit selection")
    return build_projection(
        report,
        request.name,
        selected_ids=request.selected_ids,
        languages=request.languages,
        max_records=request.max_records,
        max_nodes=request.max_nodes,
        max_edges=request.max_edges,
        depth=request.depth,
        collapse=request.collapse,
        **request.finding_options(),
        **request.candidate_options(),
    )
