"""Saved-report-only signal-quality and proposal-decision projections."""

from __future__ import annotations

from code_analysis.projection_models import (
    ColumnAlignment,
    ProjectionSelection,
    ScalarKind,
    TableColumn,
    TableProjection,
    TableRow,
    TableSort,
)
from code_analysis.projections._catalog import ProjectionBuildError, ProjectionName
from code_analysis.projections._shared import (
    _bounded_rows,
    _evidence_identity,
    _integer,
    _simple_phase_three_table,
    _string,
    _table_base,
)
from code_analysis.report_models import AnalysisReport, RefactorProposalState


def _admission(report: AnalysisReport):
    value = report.signal_quality.commonality_admission
    if value is None:
        raise ProjectionBuildError("signal-quality projection requires admission evidence")
    return value


def _decisions(report: AnalysisReport):
    value = report.signal_quality.proposal_decisions
    if value is None:
        raise ProjectionBuildError("proposal-decision projection requires proposal evidence")
    return value


def _proposal_admission(report: AnalysisReport):
    value = report.signal_quality.proposal_admission
    if value is None:
        raise ProjectionBuildError("proposal-admission projection requires proposal evidence")
    return value


def _signal_quality_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    admission = _admission(report)
    proposal_admission = report.signal_quality.proposal_admission
    decisions = report.signal_quality.proposal_decisions
    rows = [
        TableRow(
            row_id="commonality-admission",
            cells=(
                _string("commonality-admission"),
                _integer(admission.admitted_pairs),
                _integer(
                    admission.coverage_rejections
                    + admission.dimension_rejections
                    + admission.index_dimension_rejections
                ),
                _integer(admission.candidate_bound_omissions),
                _string(
                    "ownership dimensions and structural clones; rejections include index "
                    "preselection; index exclusions are "
                    + (
                        "exact; "
                        if admission.index_dimension_rejections_exact
                        else "a lower bound; "
                    )
                    + (
                        "omissions are exact"
                        if admission.candidate_bound_omissions_exact
                        else "omissions are a lower bound; remaining total unknown"
                    )
                ),
            ),
        )
    ]
    if decisions is not None:
        rows.append(
            TableRow(
                row_id="proposal-decisions",
                cells=(
                    _string("proposal-decisions"),
                    _integer(decisions.total_proposals),
                    _integer(
                        proposal_admission.call_only_rejections
                        if proposal_admission is not None
                        else 0
                    ),
                    _integer(decisions.unresolved_gates),
                    _string("explicit proposal gates"),
                ),
            )
        )
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.SIGNAL_QUALITY,
        "Signal quality summary",
        (
            ("section", "Section", ScalarKind.STRING),
            ("retained", "Retained", ScalarKind.INTEGER),
            ("rejected", "Rejected", ScalarKind.INTEGER),
            ("unresolved", "Unresolved", ScalarKind.INTEGER),
            ("policy", "Policy", ScalarKind.STRING),
        ),
        rows,
    )


def _proposal_admission_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    admission = _proposal_admission(report)
    values = (
        ("responsibility-clusters", admission.responsibility_clusters_considered),
        ("proposal-candidates", admission.proposal_candidates_considered),
        ("admitted-candidates", admission.admitted_candidates),
        ("call-only-rejections", admission.call_only_rejections),
        ("proposal-bound-omissions", admission.proposal_bound_omissions),
        ("retained-proposals", admission.retained_proposals),
    )
    rows = [TableRow(row_id=name, cells=(_string(name), _integer(value))) for name, value in values]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.PROPOSAL_ADMISSION,
        "Proposal admission funnel",
        (("stage", "Stage", ScalarKind.STRING), ("count", "Count", ScalarKind.INTEGER)),
        rows,
    )


def _commonality_admission_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    admission = _admission(report)
    values = (
        ("profiles", admission.profile_count),
        ("postings-considered", admission.postings_considered),
        ("prevalent-features-suppressed", admission.prevalent_features_suppressed),
        ("unique-candidates", admission.unique_candidates),
        ("index-dimension-rejections", admission.index_dimension_rejections),
        ("candidate-bound-omissions", admission.candidate_bound_omissions),
        ("coverage-rejections", admission.coverage_rejections),
        ("dimension-rejections", admission.dimension_rejections),
        ("clone-admissions", admission.clone_admissions),
        ("admitted-pairs", admission.admitted_pairs),
    )
    rows = [
        TableRow(
            row_id=name,
            cells=(
                _string(name),
                _integer(value),
                _string(
                    "lower-bound; remaining total unknown"
                    if (
                        name == "candidate-bound-omissions"
                        and not admission.candidate_bound_omissions_exact
                    )
                    or (
                        name == "index-dimension-rejections"
                        and not admission.index_dimension_rejections_exact
                    )
                    else "exact"
                ),
            ),
        )
        for name, value in values
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.COMMONALITY_ADMISSION,
        "Commonality admission funnel",
        (
            ("stage", "Stage", ScalarKind.STRING),
            ("count", "Count", ScalarKind.INTEGER),
            ("count-kind", "Count kind", ScalarKind.STRING),
        ),
        rows,
    )


def _prevalent_feature_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    admission = _admission(report)
    rows = [
        TableRow(
            row_id=item.exclusion_id,
            cells=(
                _string(item.exclusion_id),
                _string(item.dimension.value),
                _string(item.feature_identity),
                _integer(item.subject_count),
                _integer(item.subject_threshold),
            ),
        )
        for item in admission.prevalent_feature_examples
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.PREVALENT_FEATURES,
        "Prevalent candidate-generating features",
        (
            ("exclusion-id", "Exclusion ID", ScalarKind.STRING),
            ("dimension", "Dimension", ScalarKind.STRING),
            ("feature", "Feature", ScalarKind.STRING),
            ("subjects", "Subjects", ScalarKind.INTEGER),
            ("threshold", "Threshold", ScalarKind.INTEGER),
        ),
        rows,
    )


def _proposal_decision_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    decisions = _decisions(report)
    values = (
        ("total", decisions.total_proposals),
        ("blocked", decisions.blocked_proposals),
        ("reviewable-incomplete", decisions.reviewable_incomplete_proposals),
        ("ready-for-maintainer-review", decisions.ready_for_maintainer_review_proposals),
        ("failed-gates", decisions.failed_gates),
        ("unresolved-gates", decisions.unresolved_gates),
    )
    rows = [TableRow(row_id=name, cells=(_string(name), _integer(value))) for name, value in values]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.PROPOSAL_DECISIONS,
        "Proposal decision funnel",
        (("stage", "Stage", ScalarKind.STRING), ("count", "Count", ScalarKind.INTEGER)),
        rows,
    )


def _selected_proposals(report: AnalysisReport, selection: ProjectionSelection):
    proposals = report.refactor_proposals
    if not selection.selected_ids:
        return proposals
    selected = set(selection.selected_ids)
    missing = selected - {item.proposal_id for item in proposals}
    if missing:
        raise ProjectionBuildError(
            "selected proposal is absent from the report: " + ", ".join(sorted(missing))
        )
    return tuple(item for item in proposals if item.proposal_id in selected)


def _proposal_gate_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    _decisions(report)
    rows = [
        TableRow(
            row_id=decision.decision_id,
            cells=(
                _string(proposal.proposal_id),
                _string(decision.gate.value),
                _string(decision.outcome.value),
                _string("yes" if decision.blocking else "no"),
                _string(decision.reason),
                _string(", ".join(decision.affected_ids)),
            ),
            evidence_ids=tuple(
                sorted({*decision.supporting_catalog_ids, *decision.counter_catalog_ids})
            ),
        )
        for proposal in _selected_proposals(report, selection)
        for decision in proposal.gate_decisions
    ]
    return _simple_phase_three_table(
        report,
        selection,
        ProjectionName.PROPOSAL_GATES,
        "Proposal gate decisions",
        (
            ("proposal", "Proposal", ScalarKind.STRING),
            ("gate", "Gate", ScalarKind.STRING),
            ("outcome", "Outcome", ScalarKind.STRING),
            ("blocking", "Blocking", ScalarKind.STRING),
            ("reason", "Reason", ScalarKind.STRING),
            ("affected", "Affected identities", ScalarKind.STRING),
        ),
        rows,
    )


def _refactor_shortlist_projection(
    report: AnalysisReport, selection: ProjectionSelection
) -> TableProjection:
    _decisions(report)
    state_rank = {
        RefactorProposalState.READY_FOR_MAINTAINER_REVIEW: 0,
        RefactorProposalState.REVIEWABLE_INCOMPLETE: 1,
        RefactorProposalState.BLOCKED_INSUFFICIENT_EVIDENCE: 2,
    }
    rows = []
    for proposal in _selected_proposals(report, selection):
        outcomes = [item.outcome.value for item in proposal.gate_decisions]
        rows.append(
            TableRow(
                row_id=proposal.proposal_id,
                cells=(
                    _string(proposal.proposal_id),
                    _string(proposal.kind.value),
                    _string(proposal.state.value),
                    _integer(state_rank[proposal.state]),
                    _integer(outcomes.count("passed")),
                    _integer(outcomes.count("failed")),
                    _integer(outcomes.count("unresolved")),
                    _integer(len(proposal.affected_subject_ids)),
                    _integer(len(proposal.affected_modules)),
                    _string(proposal.primary_decision_reason),
                ),
                evidence_ids=tuple(sorted(_evidence_identity(item) for item in proposal.evidence)),
            )
        )
    definitions = (
        ("proposal", "Proposal", ScalarKind.STRING),
        ("kind", "Kind", ScalarKind.STRING),
        ("state", "State", ScalarKind.STRING),
        ("state-rank", "State rank", ScalarKind.INTEGER),
        ("passed-gates", "Passed gates", ScalarKind.INTEGER),
        ("failed-gates", "Failed gates", ScalarKind.INTEGER),
        ("unresolved-gates", "Unresolved gates", ScalarKind.INTEGER),
        ("affected-subjects", "Affected subjects", ScalarKind.INTEGER),
        ("affected-modules", "Affected modules", ScalarKind.INTEGER),
        ("primary-reason", "Primary reason", ScalarKind.STRING),
    )
    positions = {column_id: position for position, (column_id, _, _) in enumerate(definitions)}
    rows.sort(
        key=lambda row: (
            row.cells[positions["state-rank"]].integer_value,
            row.cells[positions["failed-gates"]].integer_value,
            row.cells[positions["unresolved-gates"]].integer_value,
            row.cells[positions["affected-subjects"]].integer_value,
            row.cells[positions["affected-modules"]].integer_value,
            row.row_id,
        )
    )
    bounded, truncated = _bounded_rows(rows, selection)
    sort = (
        TableSort(column_id="state-rank"),
        TableSort(column_id="failed-gates"),
        TableSort(column_id="unresolved-gates"),
        TableSort(column_id="affected-subjects"),
        TableSort(column_id="affected-modules"),
        TableSort(column_id="proposal"),
    )
    return TableProjection(
        **_table_base(
            report,
            ProjectionName.REFACTOR_SHORTLIST,
            selection,
            "Bounded refactoring shortlist",
            len(bounded),
            truncated,
        ),
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
        rows=bounded,
        sort=sort,
    )
