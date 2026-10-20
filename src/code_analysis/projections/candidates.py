"""Saved-report-only actionable refactor candidate worklist."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import NamedTuple

from code_analysis.candidate_policy import (
    DECISION_RANK,
    STATE_RANK,
    CandidatePolicyFacts,
    suppresses_unchanged_candidate,
)
from code_analysis.decisions import EffectiveDecision
from code_analysis.projection_models import (
    ColumnAlignment,
    ProjectionInput,
    ProjectionInputRole,
    ProjectionSelection,
    ScalarKind,
    SortDirection,
    TableColumn,
    TableProjection,
    TableRow,
    TableSort,
)
from code_analysis.projections._catalog import ProjectionBuildError, ProjectionName
from code_analysis.projections._shared import (
    _evidence_identity,
    _integer,
    _string,
    _table_base,
)
from code_analysis.report_models import (
    AnalysisReport,
    OwnerAlternativeClass,
    RefactorCandidate,
    RefactorCandidateObservation,
    RefactorCandidateState,
)

_OWNER_RANK = {
    OwnerAlternativeClass.EXISTING_SEMANTIC_OWNER: 0,
    OwnerAlternativeClass.EXISTING_PRIMITIVE: 1,
    OwnerAlternativeClass.EXISTING_DOMAIN_MODULE: 2,
    OwnerAlternativeClass.NEW_OWNER: 3,
}


def _net_class(estimate) -> tuple[str, int]:
    value = estimate.net_production_loc
    if value.lower is None or value.upper is None:
        return "unresolved", 3
    if value.upper < 0:
        return "favorable", 0
    if value.lower >= 0:
        return "unfavorable", 2
    return "uncertain", 1


def _candidate_worklist_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    return build_candidate_worklist_projection(report, selection, {}, None)


class RankedCandidate(NamedTuple):
    candidate: RefactorCandidate
    observation: RefactorCandidateObservation
    modules: tuple[str, ...]
    shared_dimensions: int
    owner_rank: int
    net_name: str
    net_rank: int
    deleted_loc: int
    consumer_count: int
    graph_change: int
    decision_name: str
    policy: CandidatePolicyFacts


def ranked_candidates(
    report: AnalysisReport,
    selection: ProjectionSelection,
    decisions: Mapping[str, EffectiveDecision],
    decision_ledger_sha256: str | None,
) -> tuple[RankedCandidate, ...]:
    if not report.refactor_candidates:
        raise ProjectionBuildError("candidate worklist requires recorded candidate evidence")
    requested_states = set(selection.candidate_states)
    allowed_states = {item.value for item in RefactorCandidateState}
    invalid_states = requested_states - allowed_states
    if invalid_states:
        raise ProjectionBuildError("unknown candidate state: " + ", ".join(sorted(invalid_states)))
    if selection.candidate_decisions and decision_ledger_sha256 is None:
        raise ProjectionBuildError("decision filters require a selected decision ledger")
    allowed_decisions = set(DECISION_RANK)
    invalid_decisions = set(selection.candidate_decisions) - allowed_decisions
    if invalid_decisions:
        raise ProjectionBuildError(
            "unknown candidate decision: " + ", ".join(sorted(invalid_decisions))
        )
    subjects = {item.subject_id: item for item in report.subjects}
    observations = {item.candidate_id: item for item in report.candidate_observations}
    responsibilities = {item.responsibility_id: item for item in report.responsibility_clusters}
    costs = {item.cost_estimate_id: item for item in report.abstraction_cost_estimates}
    owners: dict[str, list] = {}
    for item in report.owner_alternatives:
        owners.setdefault(item.candidate_id, []).append(item)
    proposals = {item.proposal_id: item for item in report.refactor_proposals}
    test_modules = {item.module for item in report.test_modules}
    candidates = []
    for candidate in report.refactor_candidates:
        observation = observations[candidate.candidate_id]
        effective = decisions.get(candidate.candidate_id)
        decision_name = (
            "changed-since-decision"
            if effective is not None and effective.changed_since_decision
            else effective.record.decision.value if effective is not None else "undecided"
        )
        if suppresses_unchanged_candidate(
            effective.record.decision if effective is not None else None,
            changed_since_decision=(
                effective.changed_since_decision if effective is not None else False
            ),
            requested_decisions=selection.candidate_decisions,
        ):
            continue
        modules = tuple(
            sorted({subjects[item].module for item in candidate.participating_subject_ids})
        )
        shared_dimension_count = max(
            (
                len(responsibilities[item].shared_dimensions)
                for item in observation.responsibility_ids
            ),
            default=0,
        )
        estimate = costs.get(observation.cost_estimate_id)
        alternatives = tuple(item for item in owners.get(candidate.candidate_id, ()) if item.viable)
        owner_rank = min((_OWNER_RANK[item.owner_class] for item in alternatives), default=4)
        net_name, net_rank = _net_class(estimate) if estimate is not None else ("unresolved", 3)
        proposal_records = tuple(proposals[item] for item in observation.proposal_ids)
        consumer_count = len(
            {identity for item in proposal_records for identity in item.consumer_identities}
        )
        deleted_loc = (
            estimate.expected_deleted_loc.likely
            if estimate is not None and estimate.expected_deleted_loc.likely is not None
            else -1
        )
        graph_change = (
            estimate.graph_edge_change.likely
            if estimate is not None and estimate.graph_edge_change.likely is not None
            else 0
        )
        policy = CandidatePolicyFacts(
            candidate_id=candidate.candidate_id,
            kind=candidate.kind,
            state=observation.state,
            languages=candidate.languages,
            modules=modules,
            has_test_subjects=any(module in test_modules for module in modules),
            shared_dimensions=shared_dimension_count,
            failed_gates=observation.failed_gate_count,
            unresolved_gates=observation.unresolved_gate_count,
            owner_rank=owner_rank,
            net_rank=net_rank,
            deleted_loc=deleted_loc,
            consumer_count=consumer_count,
            graph_change=graph_change,
            subject_count=len(candidate.participating_subject_ids),
            decision_name=decision_name,
        )
        if not policy.matches(selection):
            continue
        candidates.append(
            RankedCandidate(
                candidate,
                observation,
                modules,
                shared_dimension_count,
                owner_rank,
                net_name,
                net_rank,
                deleted_loc,
                consumer_count,
                graph_change,
                decision_name,
                policy,
            )
        )
    candidates.sort(key=lambda item: item.policy.ordering(selection.prefer_existing_owner))
    return tuple(candidates)


def build_candidate_worklist_projection(
    report: AnalysisReport,
    selection: ProjectionSelection,
    decisions: Mapping[str, EffectiveDecision],
    decision_ledger_sha256: str | None,
) -> TableProjection:
    candidates = ranked_candidates(report, selection, decisions, decision_ledger_sha256)
    eligible_count = len(candidates)
    retained = candidates[: selection.max_records]
    omitted = eligible_count - len(retained)
    rows = tuple(
        TableRow(
            row_id=candidate.candidate_id,
            cells=(
                _string(candidate.candidate_id),
                _string(candidate.kind.value),
                _string(decision_name),
                _integer(DECISION_RANK[decision_name]),
                _string(observation.state.value),
                _integer(STATE_RANK[observation.state]),
                _string(", ".join(item.value for item in candidate.languages)),
                _string(", ".join(modules)),
                _integer(len(candidate.participating_subject_ids)),
                _integer(len(modules)),
                _integer(shared_dimensions),
                _integer(observation.failed_gate_count),
                _integer(observation.unresolved_gate_count),
                _integer(owner_rank),
                _string(net_name),
                _integer(net_rank),
                _integer(deleted_loc),
                _integer(consumer_count),
                _integer(graph_change),
                _integer(len(observation.proposal_ids)),
            ),
            evidence_ids=tuple(sorted(_evidence_identity(item) for item in observation.evidence)),
        )
        for (
            candidate,
            observation,
            modules,
            shared_dimensions,
            owner_rank,
            net_name,
            net_rank,
            deleted_loc,
            consumer_count,
            graph_change,
            decision_name,
            _policy,
        ) in retained
    )
    definitions = (
        ("candidate", "Candidate", ScalarKind.STRING),
        ("kind", "Kind", ScalarKind.STRING),
        ("decision", "Decision", ScalarKind.STRING),
        ("decision-rank", "Decision rank", ScalarKind.INTEGER),
        ("state", "State", ScalarKind.STRING),
        ("state-rank", "State rank", ScalarKind.INTEGER),
        ("languages", "Languages", ScalarKind.STRING),
        ("modules", "Modules", ScalarKind.STRING),
        ("subjects", "Subjects", ScalarKind.INTEGER),
        ("module-count", "Module count", ScalarKind.INTEGER),
        ("shared-dimensions", "Shared dimensions", ScalarKind.INTEGER),
        ("failed-gates", "Failed gates", ScalarKind.INTEGER),
        ("unresolved-gates", "Unresolved gates", ScalarKind.INTEGER),
        ("existing-owner-class", "Existing owner class", ScalarKind.INTEGER),
        ("net-loc-class", "Net LOC class", ScalarKind.STRING),
        ("net-loc-rank", "Net LOC rank", ScalarKind.INTEGER),
        ("expected-deleted-loc", "Expected deleted LOC", ScalarKind.INTEGER),
        ("consumers", "Consumers", ScalarKind.INTEGER),
        ("graph-edge-change", "Graph edge change", ScalarKind.INTEGER),
        ("proposals", "Proposals", ScalarKind.INTEGER),
    )
    truncated = omitted > 0
    base = _table_base(
        report,
        ProjectionName.CANDIDATE_WORKLIST,
        selection,
        "Actionable refactor candidate worklist",
        len(rows),
        truncated,
    )
    if decision_ledger_sha256 is not None:
        ledger_input = ProjectionInput(
            role=ProjectionInputRole.DECISION_LEDGER,
            report_schema_name="code-analysis-decision-ledger",
            report_schema_version="1.0.0",
            report_sha256=decision_ledger_sha256,
        )
        base["inputs"] = (*base["inputs"], ledger_input)
        base["projection_id"] = (
            "projection-"
            + hashlib.sha256(
                (base["projection_id"] + "\0" + decision_ledger_sha256).encode("utf-8")
            ).hexdigest()[:20]
        )
    limitations = set(base["limitations"])
    limitations.add(
        f"Candidate selection retained {len(rows)} of {eligible_count} eligible records; "
        f"{omitted} omitted."
    )
    return TableProjection(
        **{**base, "limitations": tuple(sorted(limitations))},
        columns=tuple(
            TableColumn(
                column_id=column_id,
                label=label,
                position=position,
                value_kind=kind,
                alignment=(
                    ColumnAlignment.RIGHT if kind is ScalarKind.INTEGER else ColumnAlignment.LEFT
                ),
            )
            for position, (column_id, label, kind) in enumerate(definitions)
        ),
        rows=rows,
        sort=candidate_worklist_sort(selection.prefer_existing_owner),
    )


def candidate_worklist_sort(prefer_existing_owner: bool) -> tuple[TableSort, ...]:
    return (
        TableSort(column_id="decision-rank"),
        TableSort(column_id="state-rank"),
        TableSort(column_id="failed-gates"),
        TableSort(column_id="unresolved-gates"),
        *((TableSort(column_id="existing-owner-class"),) if prefer_existing_owner else ()),
        TableSort(column_id="net-loc-rank"),
        TableSort(column_id="expected-deleted-loc", direction=SortDirection.DESCENDING),
        TableSort(column_id="consumers", direction=SortDirection.DESCENDING),
        TableSort(column_id="graph-edge-change"),
        TableSort(column_id="subjects"),
        TableSort(column_id="module-count"),
        TableSort(column_id="candidate", direction=SortDirection.ASCENDING),
    )


def _abstraction_cost_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    from code_analysis.projections._shared import _simple_phase_three_table

    if not report.abstraction_cost_estimates:
        raise ProjectionBuildError("abstraction cost requires recorded candidate evidence")
    rows = [
        TableRow(
            row_id=item.cost_estimate_id,
            cells=(
                _string(item.candidate_id),
                _integer(item.existing_duplicate_loc or 0),
                _integer(item.expected_deleted_loc.lower or 0),
                _integer(item.expected_deleted_loc.likely or 0),
                _integer(item.expected_deleted_loc.upper or 0),
                _integer(item.net_production_loc.lower or 0),
                _integer(item.net_production_loc.likely or 0),
                _integer(item.net_production_loc.upper or 0),
                _string(_net_class(item)[0]),
                _integer(item.expected_test_requirement_categories),
            ),
        )
        for item in report.abstraction_cost_estimates
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.ABSTRACTION_COST,
        "Abstraction cost ranges",
        (
            ("candidate", "Candidate", ScalarKind.STRING),
            ("duplicate-loc", "Duplicate LOC", ScalarKind.INTEGER),
            ("deleted-lower", "Deleted lower", ScalarKind.INTEGER),
            ("deleted-likely", "Deleted likely", ScalarKind.INTEGER),
            ("deleted-upper", "Deleted upper", ScalarKind.INTEGER),
            ("net-lower", "Net lower", ScalarKind.INTEGER),
            ("net-likely", "Net likely", ScalarKind.INTEGER),
            ("net-upper", "Net upper", ScalarKind.INTEGER),
            ("net-class", "Net class", ScalarKind.STRING),
            ("test-categories", "Test categories", ScalarKind.INTEGER),
        ),
        rows,
    )


def _owner_alternative_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    from code_analysis.projections._shared import _simple_phase_three_table

    if not report.owner_alternatives:
        raise ProjectionBuildError("owner alternatives requires recorded candidate evidence")
    rows = [
        TableRow(
            row_id=item.alternative_id,
            cells=(
                _string(item.candidate_id),
                _string(item.owner_class.value),
                _string(item.logical_owner),
                _string("yes" if item.existing else "no"),
                _string("yes" if item.viable else "no"),
                _string(", ".join(value.value for value in item.warnings)),
                _string(", ".join(item.rejection_reasons)),
            ),
            evidence_ids=tuple(sorted(_evidence_identity(value) for value in item.evidence)),
        )
        for item in report.owner_alternatives
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.EXISTING_OWNER_ALTERNATIVES,
        "Existing and new owner alternatives",
        (
            ("candidate", "Candidate", ScalarKind.STRING),
            ("owner-class", "Owner class", ScalarKind.STRING),
            ("logical-owner", "Logical owner", ScalarKind.STRING),
            ("existing", "Existing", ScalarKind.STRING),
            ("viable", "Viable", ScalarKind.STRING),
            ("warnings", "Warnings", ScalarKind.STRING),
            ("rejections", "Rejections", ScalarKind.STRING),
        ),
        rows,
    )
