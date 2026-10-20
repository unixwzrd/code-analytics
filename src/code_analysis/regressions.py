"""Domain-specific regression analysis over retained reports only."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from code_analysis.comparisons import ComparisonBuildError
from code_analysis.decisions import EffectiveDecision, candidate_signature
from code_analysis.models import Completeness
from code_analysis.regression_models import (
    RegressionCounts,
    RegressionKind,
    RegressionObservation,
    RegressionReport,
)
from code_analysis.report_models import AnalysisReport, BoundaryClass, RefactorCandidateState
from code_analysis.reports.json_report import canonical_json


def _identity(prefix: str, *parts: str) -> str:
    return prefix + "-" + hashlib.sha256("\0".join(parts).encode()).hexdigest()[:20]


def _compatible(baseline: AnalysisReport, candidate: AnalysisReport) -> None:
    mismatches = []
    if (baseline.schema_name, baseline.schema_version) != (
        candidate.schema_name,
        candidate.schema_version,
    ):
        mismatches.append("report schema")
    if baseline.target != candidate.target:
        mismatches.append("target identity")
    if baseline.configuration != candidate.configuration:
        mismatches.append("configuration identity")
    if mismatches:
        raise ComparisonBuildError("incompatible " + ", ".join(mismatches))


def _observation(
    kind: RegressionKind,
    subject_id: str,
    explanation: str,
    *,
    module: str | None = None,
    source_ids: tuple[str, ...] = (),
    baseline_value: int | None = None,
    candidate_value: int | None = None,
    absolute_change: int | None = None,
    relative_change: float | None = None,
) -> RegressionObservation:
    return RegressionObservation(
        observation_id=_identity("regression-observation", kind.value, subject_id),
        kind=kind,
        subject_id=subject_id,
        module=module,
        baseline_value=baseline_value,
        candidate_value=candidate_value,
        absolute_change=absolute_change,
        relative_change=relative_change,
        explanation=explanation,
        source_ids=tuple(sorted(source_ids)),
    )


def _set_changes(
    baseline_values: Mapping[str, object],
    candidate_values: Mapping[str, object],
    added_kind: RegressionKind,
    removed_kind: RegressionKind,
    label: str,
) -> list[RegressionObservation]:
    observations = [
        _observation(added_kind, identity, f"{label} is present only in the candidate report.")
        for identity in sorted(set(candidate_values) - set(baseline_values))
    ]
    observations.extend(
        _observation(removed_kind, identity, f"{label} is present only in the baseline report.")
        for identity in sorted(set(baseline_values) - set(candidate_values))
    )
    return observations


def build_regression_report(
    baseline: AnalysisReport,
    candidate: AnalysisReport,
    *,
    max_records: int = 25,
    decisions: Mapping[str, EffectiveDecision] | None = None,
) -> RegressionReport:
    if not 1 <= max_records <= 10_000:
        raise ComparisonBuildError("regression limit must be between 1 and 10000")
    _compatible(baseline, candidate)
    observations: list[RegressionObservation] = []
    observations.extend(
        _set_changes(
            {item.cluster_id: item for item in baseline.clone_clusters},
            {item.cluster_id: item for item in candidate.clone_clusters},
            RegressionKind.DUPLICATION_ADDED,
            RegressionKind.DUPLICATION_REMOVED,
            "clone cluster",
        )
    )
    baseline_ownership = {
        item.finding_id: item
        for item in baseline.findings
        if item.category in {"ownership-risk", "state-coupling", "side-effect-ownership"}
    }
    candidate_ownership = {
        item.finding_id: item
        for item in candidate.findings
        if item.category in {"ownership-risk", "state-coupling", "side-effect-ownership"}
    }
    for identity in sorted(set(candidate_ownership) - set(baseline_ownership)):
        item = candidate_ownership[identity]
        observations.append(
            _observation(
                RegressionKind.OWNERSHIP_WORSENED,
                identity,
                "Ownership-related finding is new in the candidate report.",
                module=item.module,
                source_ids=item.subject_ids,
            )
        )
    internal = {
        BoundaryClass.INTERNAL_MUTABLE_INDEX,
        BoundaryClass.INTERNAL_RECORD,
        BoundaryClass.CONFIGURATION_MERGE,
        BoundaryClass.CROSS_SUBSYSTEM_INTERNAL,
        BoundaryClass.UNCLASSIFIED_INTERNAL,
    }
    baseline_boundaries = {
        boundary.boundary_id
        for contract in baseline.contracts
        for boundary in contract.boundaries
        if boundary.boundary_class in internal
    }
    for contract in candidate.contracts:
        for boundary in contract.boundaries:
            if (
                boundary.boundary_class in internal
                and boundary.boundary_id not in baseline_boundaries
            ):
                observations.append(
                    _observation(
                        RegressionKind.INTERNAL_CONTRACT_ADDED,
                        boundary.boundary_id,
                        f"New {boundary.boundary_class.value} contract boundary is evidenced.",
                        source_ids=(contract.contract_id, boundary.site_subject_id),
                    )
                )
    baseline_sources = {item.source_file_id: item for item in baseline.source_files}
    for item in candidate.source_files:
        before = baseline_sources.get(item.source_file_id)
        if before is None or before.physical_loc <= 0:
            continue
        absolute = item.physical_loc - before.physical_loc
        relative = absolute / before.physical_loc
        if absolute >= 25 and relative >= 0.2:
            observations.append(
                _observation(
                    RegressionKind.MODULE_GROWTH,
                    item.source_file_id,
                    "Module grew by at least 25 physical lines and 20 percent.",
                    module=item.module_name,
                    baseline_value=before.physical_loc,
                    candidate_value=item.physical_loc,
                    absolute_change=absolute,
                    relative_change=relative,
                )
            )
    baseline_states = {item.candidate_id: item.state for item in baseline.candidate_observations}
    actionable = {
        RefactorCandidateState.REVIEWABLE_INCOMPLETE,
        RefactorCandidateState.READY_FOR_MAINTAINER_REVIEW,
    }
    for item in candidate.candidate_observations:
        if item.state in actionable and baseline_states.get(item.candidate_id) not in actionable:
            observations.append(
                _observation(
                    RegressionKind.CANDIDATE_NEWLY_ACTIONABLE,
                    item.candidate_id,
                    f"Candidate state advanced to {item.state.value}.",
                    source_ids=(item.observation_id,),
                )
            )
    for candidate_id, decision in sorted((decisions or {}).items()):
        if not decision.record.decision.suppresses_unchanged:
            continue
        try:
            changed = (
                candidate_signature(candidate, candidate_id)
                != decision.record.reconsideration_signature_sha256
            )
        except OSError:
            continue
        if changed:
            observations.append(
                _observation(
                    RegressionKind.REJECTED_CANDIDATE_CHANGED,
                    candidate_id,
                    "Rejected candidate reconsideration signature changed.",
                    source_ids=(decision.record.decision_id,),
                )
            )
    observations.extend(
        _set_changes(
            {f"{item.source}|{item.target}|{item.kind}": item for item in baseline.import_edges},
            {f"{item.source}|{item.target}|{item.kind}": item for item in candidate.import_edges},
            RegressionKind.IMPORT_EDGE_ADDED,
            RegressionKind.IMPORT_EDGE_REMOVED,
            "import edge",
        )
    )
    observations.extend(
        _set_changes(
            {"|".join(item.modules): item for item in baseline.strongly_connected_components},
            {"|".join(item.modules): item for item in candidate.strongly_connected_components},
            RegressionKind.SCC_ADDED,
            RegressionKind.SCC_REMOVED,
            "strongly connected component",
        )
    )
    observations.extend(
        _set_changes(
            {"|".join(item.modules): item for item in baseline.cycles},
            {"|".join(item.modules): item for item in candidate.cycles},
            RegressionKind.CYCLE_ADDED,
            RegressionKind.CYCLE_REMOVED,
            "canonical cycle",
        )
    )
    ordered = sorted(observations, key=lambda item: (item.kind.value, item.subject_id))
    retained = tuple(sorted(ordered[:max_records], key=lambda item: item.observation_id))
    omitted = len(ordered) - len(retained)
    baseline_sha = hashlib.sha256(canonical_json(baseline).encode()).hexdigest()
    candidate_sha = hashlib.sha256(canonical_json(candidate).encode()).hexdigest()
    identity = hashlib.sha256(
        json.dumps(
            {"baseline": baseline_sha, "candidate": candidate_sha, "limit": max_records},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    source_partial = Completeness.PARTIAL in {baseline.completeness, candidate.completeness}
    completeness = Completeness.PARTIAL if source_partial or omitted else Completeness.COMPLETE
    limitations = {
        "Regression observations compare retained static evidence and do not prove runtime impact.",
    }
    if omitted:
        limitations.add("Regression observations are truncated by the declared record limit.")
    if source_partial:
        limitations.add("At least one source report is partial.")
    return RegressionReport(
        tool_version=candidate.tool_version,
        regression_identity_sha256=identity,
        baseline_report_sha256=baseline_sha,
        candidate_report_sha256=candidate_sha,
        counts=RegressionCounts(eligible=len(ordered), retained=len(retained), omitted=omitted),
        observations=retained,
        truncated=bool(omitted),
        completeness=completeness,
        limitations=tuple(sorted(limitations)),
    )


def render_regression(report: RegressionReport, output_format: str) -> str:
    if output_format in {"json", "json-compact"}:
        return (
            json.dumps(
                report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=None if output_format == "json-compact" else 2,
                sort_keys=True,
                separators=(",", ":") if output_format == "json-compact" else None,
            )
            + "\n"
        )
    columns = (
        "kind",
        "subject",
        "module",
        "baseline",
        "candidate",
        "change",
        "relative",
        "explanation",
    )
    rows = [
        (
            item.kind.value,
            item.subject_id,
            item.module or "",
            "" if item.baseline_value is None else str(item.baseline_value),
            "" if item.candidate_value is None else str(item.candidate_value),
            "" if item.absolute_change is None else str(item.absolute_change),
            "" if item.relative_change is None else f"{item.relative_change:.6f}",
            item.explanation,
        )
        for item in report.observations
    ]
    if output_format == "tsv":
        return "\n".join(("\t".join(columns), *("\t".join(row) for row in rows))) + "\n"
    if output_format != "markdown":
        raise ComparisonBuildError("unsupported regression format")
    lines = [
        "# Domain-specific regression report",
        "",
        f"- Completeness: `{report.completeness.value}`",
        f"- Eligible: {report.counts.eligible}",
        f"- Retained: {report.counts.retained}",
        f"- Omitted: {report.counts.omitted}",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    lines.extend(
        "| " + " | ".join(value.replace("|", "\\|") for value in row) + " |" for row in rows
    )
    if not rows:
        lines.append("| no regression observations |  |  |  |  |  |  |  |")
    return "\n".join(lines) + "\n"
