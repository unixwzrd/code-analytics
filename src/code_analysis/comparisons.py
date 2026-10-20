"""Compatible full-report evidence comparison over two retained reports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from pydantic import BaseModel

from code_analysis.models import Completeness
from code_analysis.projection_models import (
    ComparisonChange,
    ComparisonChangeCounts,
    ComparisonProjection,
    ComparisonRecord,
    ProjectionInput,
    ProjectionInputRole,
    ProjectionScalar,
    ProjectionSelection,
    ScalarKind,
)
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json


class ComparisonBuildError(ValueError):
    """The reports are incompatible or the requested comparison is invalid."""


def _input(report: AnalysisReport, role: ProjectionInputRole) -> ProjectionInput:
    rendered = canonical_json(report).encode()
    return ProjectionInput(
        role=role,
        report_schema_name=report.schema_name,
        report_schema_version=report.schema_version,
        report_sha256=hashlib.sha256(rendered).hexdigest(),
        run_identity_sha256=report.run_identity_sha256,
    )


def _null() -> ProjectionScalar:
    return ProjectionScalar(kind=ScalarKind.NULL)


def _digest(value: BaseModel) -> ProjectionScalar:
    payload = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ProjectionScalar(
        kind=ScalarKind.STRING,
        string_value=hashlib.sha256(payload.encode()).hexdigest(),
    )


def iter_comparison_entities(report: AnalysisReport) -> Iterable[tuple[str, str, BaseModel]]:
    """Yield comparison subject identity, record kind, and typed model for one report."""

    for module in report.modules:
        yield f"module:{module.name}", "module", module
        for symbol in module.symbols:
            yield f"symbol:{module.name}:{symbol.qualified_name}", "symbol", symbol
    for edge in report.import_edges:
        yield f"import:{edge.source}:{edge.target}:{edge.kind}", "import-edge", edge
    for index, component in enumerate(report.strongly_connected_components):
        yield f"scc:{index}:{':'.join(component.modules)}", "strong-component", component
    for index, cycle in enumerate(report.cycles):
        yield f"cycle:{index}:{':'.join(cycle.modules)}", "cycle", cycle
    for contract in report.contracts:
        yield contract.contract_id, "contract", contract
        for boundary in contract.boundaries:
            yield boundary.boundary_id, "boundary", boundary
    for shape in report.dictionary_shapes:
        yield shape.shape_id, "shape", shape
        for variant in shape.variants:
            yield variant.variant_id, "variant", variant
    for validation in report.validation_patterns:
        yield validation.validation_id, "validation", validation
    for flow in report.flow_edges:
        if flow.kind.value in {"conversion", "serialization"}:
            yield flow.flow_id, flow.kind.value, flow
    for unresolved in report.unresolved_edges:
        yield unresolved.unresolved_id, "unresolved", unresolved
    for candidate in report.candidate_classifications:
        yield candidate.candidate_id, "candidate", candidate
    for finding in report.findings:
        yield finding.finding_id, "finding", finding
    collections = (
        (report.subjects, "subject_id", "subject"),
        (report.data_structures, "structure_id", "data-structure"),
        (report.clone_fragments, "fragment_id", "clone-fragment"),
        (report.clone_clusters, "cluster_id", "clone-cluster"),
        (report.function_profiles, "profile_id", "function-profile"),
        (report.function_commonalities, "commonality_id", "function-commonality"),
        (report.state_resources, "state_resource_id", "state-resource"),
        (report.side_effects, "side_effect_id", "side-effect"),
        (report.responsibility_clusters, "responsibility_id", "responsibility"),
        (report.architecture_estimates, "estimate_id", "architecture-estimate"),
        (
            report.architecture_contract_suggestions,
            "suggestion_id",
            "architecture-contract",
        ),
        (report.refactor_proposals, "proposal_id", "refactor-proposal"),
        (report.test_modules, "test_module_id", "test-module"),
        (report.test_cases, "test_id", "test-case"),
        (report.test_fixtures, "fixture_id", "test-fixture"),
        (report.test_relationships, "relationship_id", "test-relationship"),
        (report.coverage_artifacts, "coverage_id", "coverage-artifact"),
        (report.coverage_files, "coverage_file_id", "coverage-file"),
        (
            report.coverage_observations,
            "coverage_observation_id",
            "coverage-observation",
        ),
        (report.git_commits, "commit_id", "git-commit"),
        (report.git_path_changes, "path_change_id", "git-path-change"),
        (report.git_symbol_changes, "symbol_change_id", "git-symbol-change"),
        (report.change_couplings, "coupling_id", "change-coupling"),
        (report.generation_changes, "generation_change_id", "generation-change"),
    )
    for values, identity_attribute, kind in collections:
        for value in values:
            yield str(getattr(value, identity_attribute)), kind, value
    for occurrence in report.static_call_occurrences:
        yield occurrence.occurrence_id, "static-call-occurrence", occurrence
    for coverage in report.static_call_coverage:
        yield (
            f"static-call-coverage:{coverage.source_file_id}",
            "static-call-coverage",
            coverage,
        )


def _records(report: AnalysisReport) -> Iterable[tuple[str, str, BaseModel]]:
    return iter_comparison_entities(report)


def comparison_entity_index(report: AnalysisReport) -> dict[str, tuple[str, BaseModel]]:
    return {identity: (kind, model) for identity, kind, model in iter_comparison_entities(report)}


def _validate_compatibility(baseline: AnalysisReport, candidate: AnalysisReport) -> None:
    checks = (
        (
            "report schema",
            (baseline.schema_name, baseline.schema_version),
            (candidate.schema_name, candidate.schema_version),
        ),
        ("target identity", baseline.target, candidate.target),
        ("configuration identity", baseline.configuration, candidate.configuration),
    )
    mismatches = [label for label, left, right in checks if left != right]
    if mismatches:
        raise ComparisonBuildError("incompatible " + ", ".join(mismatches))


def build_report_delta(
    baseline: AnalysisReport,
    candidate: AnalysisReport,
    *,
    max_records: int = 500,
    include_unchanged: bool = False,
) -> ComparisonProjection:
    if not 1 <= max_records <= 100_000:
        raise ComparisonBuildError("comparison limit must be between 1 and 100000")
    _validate_compatibility(baseline, candidate)
    baseline_input = _input(baseline, ProjectionInputRole.BASELINE)
    candidate_input = _input(candidate, ProjectionInputRole.CANDIDATE)
    identical = baseline_input.report_sha256 == candidate_input.report_sha256
    identity_payload = json.dumps(
        {
            "baseline": baseline_input.report_sha256,
            "candidate": candidate_input.report_sha256,
            "include_unchanged": include_unchanged,
            "max_records": max_records,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    projection_id = "projection-" + hashlib.sha256(identity_payload.encode()).hexdigest()[:20]
    limitations = set(baseline.limitations) | set(candidate.limitations)
    limitations.add("comparison reports evidence changes, not refactoring instructions")
    if Completeness.FAILED in {baseline.completeness, candidate.completeness}:
        completeness = Completeness.FAILED
    elif Completeness.PARTIAL in {baseline.completeness, candidate.completeness}:
        completeness = Completeness.PARTIAL
    else:
        completeness = Completeness.COMPLETE
    if identical:
        limitations.add("baseline and candidate reports are canonically identical")
        return ComparisonProjection(
            projection_id=projection_id,
            title="Compatible full-report evidence delta",
            inputs=(baseline_input, candidate_input),
            selection=ProjectionSelection(max_records=max_records),
            completeness=completeness,
            record_count=0,
            truncated=False,
            limitations=tuple(sorted(limitations)),
            identical=True,
            includes_unchanged=include_unchanged,
            change_counts=None,
            eligible_record_count=0,
            records=(),
        )
    baseline_records = {identity: (kind, value) for identity, kind, value in _records(baseline)}
    candidate_records = {identity: (kind, value) for identity, kind, value in _records(candidate)}
    all_records: list[ComparisonRecord] = []
    for identity in sorted(set(baseline_records) | set(candidate_records)):
        before = baseline_records.get(identity)
        after = candidate_records.get(identity)
        if before is None:
            assert after is not None
            change = ComparisonChange.ADDED
            kind = after[0]
        elif after is None:
            change = ComparisonChange.REMOVED
            kind = before[0]
        else:
            kind = before[0]
            if kind != after[0]:
                raise ComparisonBuildError(f"identity {identity} changed record kind")
            change = (
                ComparisonChange.UNCHANGED
                if _digest(before[1]) == _digest(after[1])
                else ComparisonChange.CHANGED
            )
        all_records.append(
            ComparisonRecord(
                subject_id=identity,
                change=change,
                baseline_value=_null() if before is None else _digest(before[1]),
                candidate_value=_null() if after is None else _digest(after[1]),
                explanation=f"{kind} {change.value}",
            )
        )
    if not all_records:
        raise ComparisonBuildError("compatible reports contain no evidence records")
    change_counts = ComparisonChangeCounts(
        added=sum(item.change is ComparisonChange.ADDED for item in all_records),
        removed=sum(item.change is ComparisonChange.REMOVED for item in all_records),
        changed=sum(item.change is ComparisonChange.CHANGED for item in all_records),
        unchanged=sum(item.change is ComparisonChange.UNCHANGED for item in all_records),
    )
    eligible = (
        all_records
        if include_unchanged
        else [item for item in all_records if item.change is not ComparisonChange.UNCHANGED]
    )
    truncated = len(eligible) > max_records
    bounded = tuple(eligible[:max_records])
    if truncated:
        limitations.add("projection record limit reached")
    if truncated and completeness is Completeness.COMPLETE:
        completeness = Completeness.PARTIAL
    return ComparisonProjection(
        projection_id=projection_id,
        title="Compatible full-report evidence delta",
        inputs=(baseline_input, candidate_input),
        selection=ProjectionSelection(max_records=max_records),
        completeness=completeness,
        record_count=len(bounded),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=tuple(sorted(limitations)),
        identical=False,
        includes_unchanged=include_unchanged,
        change_counts=change_counts,
        eligible_record_count=len(eligible),
        records=bounded,
    )
