"""Framework-independent workflow catalog and read-only saved-evidence readiness.

No filesystem capabilities are needed here: configuration is not source verification,
and an empty, complete evidence section is still valid evidence.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, StrictStr, model_validator

from code_analysis.collection_commands import CollectionContext, Operation
from code_analysis.models import Completeness, ProviderState, StrictModel
from code_analysis.project_browsing import SourceConnection
from code_analysis.report_models import AnalysisReport, SectionCoverage, SectionState
from code_analysis.ui_models import UIReportIdentity
from code_analysis.ui_runtime import RuntimeOverview, RuntimeViewService
from code_analysis.web_service import ReportViewService

ViewId = Literal[
    "overview",
    "collection",
    "runs",
    "providers",
    "diagnostics",
    "modules",
    "browse",
    "architecture",
    "contracts",
    "dictionary-shapes",
    "findings",
    "clones",
    "state-effects",
    "runtime",
    "explore",
    "candidates",
    "test-impact",
    "comparison",
]
ReadinessState = Literal["available", "missing", "partial", "failed"]
GroupId = Literal["project", "understand", "investigate", "review", "compare"]


class WorkflowReadiness(StrictModel):
    state: ReadinessState
    reason: StrictStr = Field(min_length=1, max_length=2048)
    optional_issues: tuple[StrictStr, ...] = Field(default=(), max_length=100)


class WorkflowGroup(StrictModel):
    group_id: GroupId
    label: StrictStr = Field(min_length=1, max_length=100)
    view_ids: tuple[ViewId, ...] = Field(min_length=1, max_length=18)


class WorkflowViewDefinition(StrictModel):
    view_id: ViewId
    label: StrictStr = Field(min_length=1, max_length=100)
    purpose: StrictStr = Field(min_length=1, max_length=1024)
    usage: StrictStr = Field(min_length=1, max_length=1024)
    evidence_required: tuple[StrictStr, ...] = Field(max_length=15)
    next_view: ViewId | None
    help_document_id: Literal["user-guide", "reports"]
    help_anchor: StrictStr = Field(min_length=1, max_length=100)


class WorkflowView(WorkflowViewDefinition):
    readiness: WorkflowReadiness


class WorkflowAction(StrictModel):
    action_id: StrictStr = Field(min_length=1, max_length=100)
    label: StrictStr = Field(min_length=1, max_length=100)
    view_id: ViewId
    reason: StrictStr = Field(min_length=1, max_length=2048)
    operation: Operation | None = None


class WorkflowOverview(StrictModel):
    groups: tuple[WorkflowGroup, ...] = Field(min_length=5, max_length=5)
    views: tuple[WorkflowView, ...] = Field(min_length=18, max_length=18)
    collection: CollectionContext
    reports: tuple[UIReportIdentity, ...] = Field(max_length=2)
    runtime: RuntimeOverview
    comparison_profile_report_sha256: StrictStr | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    source_access: Literal["not-configured", "configured-unverified"]
    source_connection: SourceConnection | None = None
    actions: tuple[WorkflowAction, ...] = Field(max_length=20)
    overall_readiness: WorkflowReadiness

    @model_validator(mode="after")
    def validate_catalog(self) -> Self:
        _validate_catalog(self.groups, self.views)
        if len({action.action_id for action in self.actions}) != len(self.actions):
            raise ValueError("workflow action identities must be unique")
        if self.comparison_profile_report_sha256 is not None and not self.runtime.available:
            raise ValueError("profile comparison identity requires loaded runtime evidence")
        return self


WORKFLOW_GROUPS = (
    WorkflowGroup(
        group_id="project",
        label="Project & collection",
        view_ids=("overview", "collection", "runs", "providers", "diagnostics"),
    ),
    WorkflowGroup(
        group_id="understand",
        label="Understand code",
        view_ids=("modules", "browse", "architecture", "contracts", "dictionary-shapes"),
    ),
    WorkflowGroup(
        group_id="investigate",
        label="Investigate issues",
        view_ids=("findings", "clones", "state-effects", "runtime", "explore"),
    ),
    WorkflowGroup(
        group_id="review", label="Review & validate", view_ids=("candidates", "test-impact")
    ),
    WorkflowGroup(group_id="compare", label="Compare runs", view_ids=("comparison",)),
)


def _view(
    view_id: ViewId,
    label: str,
    purpose: str,
    usage: str,
    evidence: tuple[str, ...],
    next_view: ViewId | None,
    anchor: str,
) -> WorkflowViewDefinition:
    return WorkflowViewDefinition(
        view_id=view_id,
        label=label,
        purpose=purpose,
        usage=usage,
        evidence_required=evidence,
        next_view=next_view,
        help_document_id="reports" if view_id == "explore" else "user-guide",
        help_anchor=anchor,
    )


WORKFLOW_VIEWS = (
    _view(
        "overview",
        "Overview",
        "Choose a workflow from the evidence currently loaded.",
        "Start here; readiness describes saved evidence, not live source verification.",
        (),
        "collection",
        "your-first-investigation",
    ),
    _view(
        "collection",
        "Collect / plan",
        "Plan explicit evidence collection commands.",
        "Select inputs and review the command before running collection.",
        ("collection-context",),
        "runs",
        "collection-command-planning",
    ),
    _view(
        "runs",
        "Saved runs",
        "Connect a workspace and select retained runs.",
        "Use the workspace picker or refresh and select saved analysis runs.",
        (),
        "modules",
        "refreshing-retained-analysis-runs",
    ),
    _view(
        "providers",
        "Providers",
        "Inspect saved provider outcomes.",
        "Check provider availability and failures without rerunning providers.",
        ("saved-evidence",),
        "diagnostics",
        "check-evidence-health",
    ),
    _view(
        "diagnostics",
        "Diagnostics",
        "Inspect evidence limitations and diagnostics.",
        "Review saved diagnostics before interpreting analysis results.",
        ("saved-evidence",),
        "collection",
        "check-evidence-health",
    ),
    _view(
        "modules",
        "Modules",
        "Understand package and module structure.",
        "Browse the saved hierarchy and select a module to investigate.",
        ("r1-static-analysis",),
        "browse",
        "browse-package-and-module-hierarchy",
    ),
    _view(
        "browse",
        "Browse code",
        "Browse saved entities and outlines.",
        "Inspect saved entities; source preview and editing need separate configured capabilities.",
        ("r1-static-analysis",),
        "architecture",
        "code-browsing-and-session-draft-editing",
    ),
    _view(
        "architecture",
        "Architecture",
        "Investigate dependency relationships.",
        "Focus a module and follow saved imports and dependency evidence.",
        ("r1-static-analysis",),
        "findings",
        "dependency-investigation",
    ),
    _view(
        "contracts",
        "Contracts",
        "Inspect observed contracts and interfaces.",
        "Select a contract and inspect its evidence and limitations.",
        ("contracts",),
        "dictionary-shapes",
        "contracts-and-dictionary-shapes",
    ),
    _view(
        "dictionary-shapes",
        "Dictionary shapes",
        "Inspect observed dictionary structures.",
        "Review shapes and their saved observations, not runtime guarantees.",
        ("dictionary-shapes",),
        "state-effects",
        "contracts-and-dictionary-shapes",
    ),
    _view(
        "findings",
        "Findings",
        "Review retained findings, including an empty result.",
        "Filter findings and inspect the originating saved evidence.",
        ("r1-static-analysis",),
        "candidates",
        "review-findings",
    ),
    _view(
        "clones",
        "Clones",
        "Inspect duplicate and common code evidence.",
        "Select clone clusters; commonality is supplementary evidence.",
        ("clones",),
        "candidates",
        "inspect-clones-and-commonality",
    ),
    _view(
        "state-effects",
        "State / effects",
        "Understand saved state and side-effect observations.",
        "Trace resources and effects while respecting evidence completeness.",
        ("state-side-effects",),
        "candidates",
        "understand-state-and-effects",
    ),
    _view(
        "runtime",
        "Runtime",
        "Inspect saved runtime profiles.",
        "Review observed execution costs; this view does not launch workloads.",
        ("runtime-profile",),
        "explore",
        "saved-runtime-evidence-in-the-ui",
    ),
    _view(
        "explore",
        "Explore evidence",
        "Explore saved evidence projections.",
        "Choose a projection family and bound the evidence you inspect.",
        ("r1-static-analysis",),
        "candidates",
        "projection-families",
    ),
    _view(
        "candidates",
        "Candidates",
        "Review evidence-backed refactoring candidates.",
        "Inspect candidate rationale and qualification before making changes.",
        ("cohesion-refactoring",),
        "test-impact",
        "review-refactoring-candidates",
    ),
    _view(
        "test-impact",
        "Test impact",
        "Understand saved test relationships.",
        "Inspect impacted tests; saved relationships do not guarantee complete coverage.",
        ("test-impact",),
        "comparison",
        "understand-test-impact",
    ),
    _view(
        "comparison",
        "Comparison",
        "Compare two saved static reports.",
        "Select baseline and candidate reports and inspect their retained differences.",
        ("two-static-reports",),
        None,
        "compare-saved-runs",
    ),
)


def _validate_catalog(
    groups: tuple[WorkflowGroup, ...], views: tuple[WorkflowViewDefinition, ...]
) -> None:
    from typing import get_args

    ids = tuple(view.view_id for view in views)
    grouped = tuple(view for group in groups for view in group.view_ids)
    if len(ids) != 18 or len(set(ids)) != 18 or set(ids) != set(get_args(ViewId)):
        raise ValueError("workflow must register all 18 views exactly once")
    if len(grouped) != 18 or set(grouped) != set(ids):
        raise ValueError("workflow groups must contain all views exactly once")
    if len({group.group_id for group in groups}) != 5:
        raise ValueError("workflow groups must have unique identities")


_validate_catalog(WORKFLOW_GROUPS, WORKFLOW_VIEWS)


def _ready(state: ReadinessState, reason: str, issues: tuple[str, ...] = ()) -> WorkflowReadiness:
    return WorkflowReadiness(state=state, reason=reason, optional_issues=issues[:100])


def _optional_issues(report: AnalysisReport) -> tuple[str, ...]:
    sections = tuple(
        f"Optional {s.section}: {s.state.value}."
        for s in report.section_coverage
        if not s.required and s.state not in (SectionState.COMPLETE, SectionState.DISABLED)
    )
    providers = tuple(
        f"Provider {p.provider_id}: {p.state.value}."
        for p in report.providers
        if p.state is not ProviderState.SUCCESS
    )
    return (sections + providers)[:100]


def _static_readiness(
    report: AnalysisReport | None, sections: tuple[str, ...]
) -> WorkflowReadiness:
    if report is None:
        return _ready("missing", "Load a saved static analysis report.")
    issues = _optional_issues(report)
    coverage: dict[str, SectionCoverage] = {
        section.section: section for section in report.section_coverage
    }
    selected = tuple(coverage.get(name) for name in sections)
    if any(s is not None and s.state is SectionState.FAILED for s in selected):
        return _ready(
            "failed", "Required saved analyzer evidence failed validation or analysis.", issues
        )
    if any(
        s is None or s.state in (SectionState.DISABLED, SectionState.UNAVAILABLE) for s in selected
    ):
        return _ready(
            "missing", "Required analyzer evidence is absent, disabled, or unavailable.", issues
        )
    if any(
        s is not None and s.state in (SectionState.PARTIAL, SectionState.TRUNCATED)
        for s in selected
    ):
        return _ready(
            "partial", "Required saved analyzer evidence is incomplete or truncated.", issues
        )
    return _ready(
        "available", "Required saved evidence is complete; empty results are valid.", issues
    )


def _core_sections(report: AnalysisReport) -> tuple[str, ...]:
    return tuple(
        section.section
        for section in report.section_coverage
        if section.section == "r1-static-analysis"
        or (section.required and section.state is not SectionState.DISABLED)
    )


def build_workflow(
    context: CollectionContext,
    service: ReportViewService | None,
    runtime: RuntimeViewService | None,
    *,
    source_configured: bool = False,
    run_catalog_available: bool = False,
    source_connection: SourceConnection | None = None,
) -> WorkflowOverview:
    """Describe existing immutable evidence; never collect, read source, or run jobs."""
    from code_analysis.ui_handoff import canonical_profile_sha256

    reports = service.bootstrap().reports if service is not None else ()
    runtime_overview = (
        runtime.overview(reports[0].report_sha256 if reports else None)
        if runtime
        else RuntimeOverview(available=False, message="Load saved runtime evidence.")
    )
    report = service.primary if service else None
    runtime_ready = _ready(
        (
            "missing"
            if not runtime_overview.available
            else (
                "failed"
                if runtime_overview.completeness is Completeness.FAILED
                else (
                    "partial"
                    if runtime_overview.completeness is Completeness.PARTIAL
                    else "available"
                )
            )
        ),
        runtime_overview.message,
        tuple(
            f"Provider {p.provider_id}: {p.state.value}."
            for p in runtime_overview.providers
            if p.state is not ProviderState.SUCCESS
        ),
    )
    views = []
    for definition in WORKFLOW_VIEWS:
        view_id = definition.view_id
        if view_id == "overview":
            readiness = _ready("available", "Choose a workflow or load saved evidence.")
        elif view_id == "collection":
            readiness = _ready(
                "available" if context.enabled else "missing",
                (
                    "Collection command planning is configured."
                    if context.enabled
                    else "Configure a collection root to plan commands."
                ),
            )
        elif view_id == "runs":
            readiness = _ready(
                "available",
                (
                    "Select or refresh retained runs."
                    if run_catalog_available
                    else "Connect a workspace using the workspace picker."
                ),
            )
        elif view_id in ("providers", "diagnostics"):
            readiness = (
                _ready("available", "Inspect saved evidence health, including empty diagnostics.")
                if report is not None
                else _ready(
                    "missing",
                    "Load static evidence for this panel; profile health is shown in Runtime.",
                )
            )
        elif view_id == "runtime":
            readiness = runtime_ready
        elif view_id == "comparison":
            if service is None or service.comparison is None:
                readiness = _ready("missing", "Load two saved static reports for comparison.")
            else:
                sides = (
                    _static_readiness(service.primary, _core_sections(service.primary)),
                    _static_readiness(service.comparison, _core_sections(service.comparison)),
                )
                priority_states: tuple[ReadinessState, ...] = ("failed", "missing", "partial")
                state: ReadinessState = next(
                    (
                        state
                        for state in priority_states
                        if any(side.state == state for side in sides)
                    ),
                    "available",
                )
                readiness = _ready(
                    state,
                    "Comparison uses both saved static reports and their completeness.",
                    tuple(issue for side in sides for issue in side.optional_issues),
                )
        else:
            readiness = _static_readiness(report, definition.evidence_required)
        payload = definition.model_dump()
        if readiness.state == "missing":
            payload["next_view"] = "collection" if context.enabled else "runs"
        elif readiness.state in ("failed", "partial"):
            payload["next_view"] = "diagnostics" if report is not None else "runs"
        if (
            view_id in ("providers", "diagnostics")
            and report is None
            and runtime_overview.available
        ):
            payload["next_view"] = "runtime"
        if view_id == "comparison" and readiness.state == "missing":
            payload["next_view"] = "runs"
        views.append(WorkflowView(**payload, readiness=readiness))
    overall = _static_readiness(report, _core_sections(report)) if report else runtime_ready
    if report is None and not runtime_overview.available:
        overall = _ready("missing", "Collect or open static evidence or a saved Python profile.")
    actions = []
    if not reports:
        actions.append(
            WorkflowAction(
                action_id="load-saved-evidence",
                label="Load saved evidence",
                view_id="runs",
                reason="Connect a workspace or select retained evidence.",
            )
        )
    if context.enabled:
        actions.append(
            WorkflowAction(
                action_id="plan-scan",
                label="Plan static collection",
                view_id="collection",
                reason="Review an explicit collection command.",
                operation="scan",
            )
        )
        collection_actions: tuple[tuple[Operation, str], ...] = (
            ("profile", "Plan runtime collection"),
            ("import-profile", "Plan profile import"),
        )
        for operation, label in collection_actions:
            actions.append(
                WorkflowAction(
                    action_id=f"plan-{operation}",
                    label=label,
                    view_id="collection",
                    reason="Review an explicit command and its required inputs.",
                    operation=operation,
                )
            )
    if runtime_overview.available:
        actions.append(
            WorkflowAction(
                action_id="inspect-runtime",
                label="Inspect saved runtime",
                view_id="runtime",
                reason=runtime_ready.reason,
            )
        )
    by_id = {view.view_id: view for view in views}
    for view_id, label in (
        ("modules", "Inspect saved modules"),
        ("findings", "Review findings"),
        ("candidates", "Review candidates"),
        ("diagnostics", "Check evidence health"),
        ("comparison", "Compare saved reports"),
    ):
        if by_id[view_id].readiness.state in ("available", "partial"):
            actions.append(
                WorkflowAction(
                    action_id=f"inspect-{view_id}",
                    label=label,
                    view_id=view_id,
                    reason=by_id[view_id].readiness.reason,
                )
            )
    return WorkflowOverview(
        groups=WORKFLOW_GROUPS,
        views=tuple(views),
        collection=context,
        reports=reports,
        runtime=runtime_overview,
        comparison_profile_report_sha256=(
            canonical_profile_sha256(runtime.comparison)
            if runtime is not None and runtime.comparison is not None
            else None
        ),
        source_access="configured-unverified" if source_configured else "not-configured",
        source_connection=source_connection,
        actions=tuple(actions),
        overall_readiness=overall,
    )
