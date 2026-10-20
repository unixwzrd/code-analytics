"""Report integration for transparent function-commonality evidence."""

from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import replace

from code_analysis.analysis.commonality import (
    FunctionFeatures,
    discover_function_features,
    generate_commonality_candidates,
)
from code_analysis.clone_reporting import collect_clone_sources
from code_analysis.context import AnalysisContext
from code_analysis.models import (
    Completeness,
    Confidence,
    Diagnostic,
    DiagnosticLevel,
    Evidence,
    ProviderResult,
    ProviderState,
    SourceSpan,
)
from code_analysis.report_models import (
    AnalysisReport,
    AnalysisSubject,
    CandidateRejectionExample,
    CandidateRejectionReason,
    CommonalityAdmissionFunnel,
    CommonalityDimension,
    FunctionCommonality,
    FunctionFeatureProfile,
    PrevalentFeatureExclusion,
    SectionCoverage,
    SectionState,
    SignalQualitySummary,
    SubjectKind,
    SummaryCounts,
)

PROFILE_LIMITATIONS = (
    "Call resolution is bounded to direct local and imported syntax; dynamic dispatch "
    "is unresolved.",
    "Feature profiles describe syntax and do not establish runtime behavior or common ownership.",
)


def _stable_id(prefix: str, *parts: str) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def _evidence(item: FunctionFeatures, observation: str, confidence: Confidence) -> Evidence:
    return Evidence(
        span=SourceSpan(
            path=item.logical_path,
            start_line=item.start_line,
            start_column=item.start_column,
            end_line=item.end_line,
            end_column=item.end_column,
        ),
        provider="native-commonality",
        observation=observation,
        confidence=confidence,
        source_sha256=item.source_sha256,
    )


def _subject_id(item: FunctionFeatures) -> str:
    qualified_name = item.logical_name.split(":", 1)[1]
    return _stable_id("subject", "symbol", item.module, qualified_name)


def integrate_commonality_report(
    context: AnalysisContext, report: AnalysisReport
) -> AnalysisReport:
    sources = collect_clone_sources(context, report)
    features = discover_function_features(sources)
    subjects = {item.subject_id: item for item in report.subjects}
    logical_subjects = {item.logical_name: item.subject_id for item in report.subjects}
    effects_by_logical: dict[str, set[str]] = {}
    for effect in report.side_effects:
        subject = subjects[effect.subject_id]
        effects_by_logical.setdefault(subject.logical_name, set()).add(
            f"{effect.kind.value}:{effect.target_identity}"
        )
    features = tuple(
        replace(
            item,
            side_effects=tuple(
                sorted(
                    {
                        *item.side_effects,
                        *effects_by_logical.get(item.logical_name, set()),
                    }
                )
            ),
        )
        for item in features
    )
    features_by_logical = {item.logical_name: item for item in features}
    feature_subjects: dict[str, str] = {}
    for item in features:
        subject_id = logical_subjects.get(item.logical_name, _subject_id(item))
        feature_subjects[item.logical_name] = subject_id
        if subject_id not in subjects:
            subjects[subject_id] = AnalysisSubject(
                subject_id=subject_id,
                kind=SubjectKind.SYMBOL,
                logical_name=item.logical_name,
                module=item.module,
                evidence=(_evidence(item, "function declaration", Confidence.EXACT),),
            )
    contracts_by_subject: dict[str, list[str]] = {}
    for contract in report.contracts:
        contracts_by_subject.setdefault(contract.defining_subject_id, []).append(
            contract.contract_id
        )
    features = tuple(
        replace(
            item,
            contract_ids=tuple(
                sorted(contracts_by_subject.get(feature_subjects[item.logical_name], ()))
            ),
        )
        for item in features
    )
    features_by_logical = {item.logical_name: item for item in features}
    profiles = tuple(
        sorted(
            (
                FunctionFeatureProfile(
                    profile_id=_stable_id("function-profile", feature_subjects[item.logical_name]),
                    subject_id=feature_subjects[item.logical_name],
                    callees=item.callees,
                    signature_shapes=item.signature_shapes,
                    attributes_globals=item.attributes_globals,
                    exceptions=item.exceptions,
                    side_effects=item.side_effects,
                    control_flow=item.control_flow,
                    dictionary_schema_operations=item.dictionary_schema_operations,
                    contract_ids=item.contract_ids,
                    weak_semantic_signals=item.weak_semantic_signals,
                    unresolved_call_count=item.unresolved_call_count,
                    evidence=(
                        _evidence(item, "transparent function feature profile", Confidence.EXACT),
                    ),
                    limitations=PROFILE_LIMITATIONS,
                )
                for item in features
            ),
            key=lambda item: item.profile_id,
        )
    )
    subjects_by_id = {item.subject_id: item for item in subjects.values()}
    clone_pairs: set[tuple[str, str]] = set()
    for cluster in report.clone_clusters:
        logical_names = sorted(
            subjects_by_id[subject_id].logical_name
            for subject_id in cluster.subject_ids
            if subject_id in subjects_by_id
            and subjects_by_id[subject_id].logical_name in features_by_logical
        )
        for left_index, left in enumerate(logical_names):
            for right in logical_names[left_index + 1 :]:
                clone_pairs.add((left, right))
    candidates = generate_commonality_candidates(
        features,
        tuple(sorted(clone_pairs)),
        minimum_ownership_dimensions=(
            context.config.clone_thresholds.commonality_min_ownership_dimensions
        ),
        minimum_bilateral_coverage=(
            context.config.clone_thresholds.commonality_min_bilateral_coverage
        ),
        maximum_feature_subject_fraction=(
            context.config.clone_thresholds.commonality_max_feature_subject_fraction
        ),
        maximum_pairs=context.config.phase_three_limits.max_candidate_pairs,
    )
    clusters_by_pair: dict[tuple[str, str], list[str]] = {}
    for cluster in report.clone_clusters:
        logical_names = sorted(
            subjects_by_id[item].logical_name
            for item in cluster.subject_ids
            if item in subjects_by_id and subjects_by_id[item].logical_name in features_by_logical
        )
        for left_index, left in enumerate(logical_names):
            for right in logical_names[left_index + 1 :]:
                clusters_by_pair.setdefault((left, right), []).append(cluster.cluster_id)
    commonalities: list[FunctionCommonality] = []
    for left_name, right_name in candidates.pairs:
        # Persisted left/right identities are canonical subject-ID order, not
        # lexical-name order. Orient metrics and evidence before constructing them.
        left, right = sorted(
            (features_by_logical[left_name], features_by_logical[right_name]),
            key=lambda feature: feature_subjects[feature.logical_name],
        )
        dimensions = []
        for (kind, left_values), (right_kind, right_values) in zip(
            left.dimensions(), right.dimensions(), strict=True
        ):
            assert kind is right_kind
            shared = tuple(sorted(set(left_values) & set(right_values)))
            if shared:
                dimensions.append(
                    CommonalityDimension(
                        kind=kind,
                        shared_identities=shared,
                        left_total=len(left_values),
                        right_total=len(right_values),
                    )
                )
        pair_clusters = tuple(sorted(clusters_by_pair.get((left_name, right_name), ())))
        if not dimensions and not pair_clusters:
            continue
        counter: list[Evidence] = []
        if left.module.split(".", 1)[0] != right.module.split(".", 1)[0]:
            counter.append(
                _evidence(left, "different top-level module domains", Confidence.INFERRED)
            )
        if left.exceptions != right.exceptions and (left.exceptions or right.exceptions):
            counter.append(
                _evidence(
                    left, "different exception behavior is syntax-evidenced", Confidence.EXACT
                )
            )
        if left.side_effects != right.side_effects and (left.side_effects or right.side_effects):
            counter.append(
                _evidence(
                    left, "different side-effect features are syntax-evidenced", Confidence.EXACT
                )
            )
        if left.unresolved_call_count or right.unresolved_call_count:
            counter.append(
                _evidence(left, "one or both call sets are incomplete", Confidence.INFERRED)
            )
        left_contracts = set(contracts_by_subject.get(feature_subjects[left.logical_name], ()))
        right_contracts = set(contracts_by_subject.get(feature_subjects[right.logical_name], ()))
        if left_contracts and right_contracts and left_contracts.isdisjoint(right_contracts):
            counter.append(
                _evidence(left, "functions reference incompatible contracts", Confidence.RESOLVED)
            )
        left_id, right_id = (
            feature_subjects[left.logical_name],
            feature_subjects[right.logical_name],
        )
        commonalities.append(
            FunctionCommonality(
                commonality_id=_stable_id("commonality", left_id, right_id),
                left_subject_id=left_id,
                right_subject_id=right_id,
                dimensions=tuple(sorted(dimensions, key=lambda item: item.kind.value)),
                clone_cluster_ids=pair_clusters,
                evidence=(
                    _evidence(left, "left function feature evidence", Confidence.EXACT),
                    _evidence(right, "right function feature evidence", Confidence.EXACT),
                ),
                counter_evidence=tuple(counter),
                confidence=Confidence.RESOLVED,
                completeness=(
                    Completeness.PARTIAL
                    if left.unresolved_call_count or right.unresolved_call_count
                    else Completeness.COMPLETE
                ),
                limitations=PROFILE_LIMITATIONS,
            )
        )
    commonality_records = tuple(sorted(commonalities, key=lambda item: item.commonality_id))
    admission = CommonalityAdmissionFunnel(
        profile_count=len(profiles),
        postings_considered=candidates.postings_considered,
        prevalent_features_suppressed=candidates.prevalent_features_suppressed,
        unique_candidates=candidates.unique_candidates,
        index_dimension_rejections=candidates.index_dimension_rejections,
        index_dimension_rejections_exact=candidates.index_dimension_rejections_exact,
        candidate_bound_omissions=candidates.truncated_pairs,
        candidate_bound_omissions_exact=not bool(candidates.truncated_pairs),
        coverage_rejections=candidates.coverage_rejections,
        dimension_rejections=candidates.dimension_rejections,
        clone_admissions=candidates.clone_admissions,
        admitted_pairs=len(commonality_records),
        prevalent_feature_examples=tuple(
            sorted(
                (
                    PrevalentFeatureExclusion(
                        exclusion_id=_stable_id(
                            "prevalent-feature", item.kind.value, item.identity
                        ),
                        dimension=item.kind,
                        feature_identity=item.identity,
                        subject_count=item.subject_count,
                        subject_threshold=item.subject_threshold,
                    )
                    for item in candidates.prevalent_examples
                ),
                key=lambda item: item.exclusion_id,
            )
        ),
        rejection_examples=tuple(
            sorted(
                (
                    CandidateRejectionExample(
                        example_id=_stable_id("candidate-rejection", item.left, item.right),
                        reason=(
                            CandidateRejectionReason.INSUFFICIENT_BILATERAL_COVERAGE
                            if item.coverage_rejected
                            else CandidateRejectionReason.INSUFFICIENT_OWNERSHIP_DIMENSIONS
                        ),
                        left_subject_id=min(
                            feature_subjects[item.left], feature_subjects[item.right]
                        ),
                        right_subject_id=max(
                            feature_subjects[item.left], feature_subjects[item.right]
                        ),
                        shared_ownership_dimensions=item.shared_dimensions,
                    )
                    for item in candidates.rejection_examples
                ),
                key=lambda item: item.example_id,
            )
        ),
    )
    diagnostic = (
        (
            Diagnostic(
                code="commonality-candidate-bound-truncated",
                level=DiagnosticLevel.WARNING,
                message=(
                    "commonality candidate generation omitted at least "
                    f"{candidates.truncated_pairs} pair(s); the remaining total is unknown "
                    "because generation stopped at the configured bound"
                ),
                provider="native-commonality",
            ),
        )
        if candidates.truncated_pairs
        else ()
    )
    state = (
        SectionState.PARTIAL
        if candidates.truncated_pairs or report.summary.failed_files
        else SectionState.COMPLETE
    )
    overall = (
        Completeness.FAILED
        if report.completeness is Completeness.FAILED
        else (
            Completeness.PARTIAL
            if report.completeness is Completeness.PARTIAL or state is SectionState.PARTIAL
            else Completeness.COMPLETE
        )
    )
    coverage = tuple(
        (
            SectionCoverage(
                section="function-commonality",
                state=state,
                parsed=len(profiles),
                failed=report.summary.failed_files,
                truncated=candidates.truncated_pairs,
                diagnostic_codes=tuple(item.code for item in diagnostic),
            )
            if item.section == "function-commonality"
            else item
        )
        for item in report.section_coverage
    )
    provider = ProviderResult(
        provider_id="native-commonality",
        state=ProviderState.PARTIAL if state is SectionState.PARTIAL else ProviderState.SUCCESS,
        executable="python",
        version=platform.python_version(),
        duration_ms=0,
        observations=("function-feature-profiles", "inverted-feature-candidates"),
        diagnostics=diagnostic,
        completeness=(
            Completeness.PARTIAL if state is SectionState.PARTIAL else Completeness.COMPLETE
        ),
    )
    summary = SummaryCounts(
        **{
            **report.summary.model_dump(),
            "subjects": len(subjects),
            "function_profiles": len(profiles),
            "function_commonalities": len(commonality_records),
        }
    )
    identity_payload = json.dumps(
        {
            "prior": report.run_identity_sha256,
            "profiles": [item.model_dump(mode="json") for item in profiles],
            "commonalities": [item.model_dump(mode="json") for item in commonality_records],
            "admission": admission.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    limitations = {*report.limitations, *PROFILE_LIMITATIONS}
    if candidates.truncated_pairs:
        limitations.add("Configured Phase 3 bounds truncated commonality coverage.")
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "run_identity_sha256": hashlib.sha256(identity_payload).hexdigest(),
            "subjects": tuple(subjects[key] for key in sorted(subjects)),
            "function_profiles": profiles,
            "function_commonalities": commonality_records,
            "signal_quality": SignalQualitySummary(commonality_admission=admission),
            "section_coverage": coverage,
            "diagnostics": (*report.diagnostics, *diagnostic),
            "providers": (*report.providers, provider),
            "completeness": overall,
            "limitations": tuple(sorted(limitations)),
            "summary": summary,
        }
    )
