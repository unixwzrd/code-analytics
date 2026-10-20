"""Phase 3 report integration for bounded native duplication evidence."""

from __future__ import annotations

import hashlib
import json
import platform

from code_analysis.analysis.clones import (
    CloneDiscovery,
    CloneFingerprint,
    CloneSource,
    cluster_clone_fingerprints,
    fingerprint_clone_source,
)
from code_analysis.cache import ContentCache
from code_analysis.context import AnalysisContext
from code_analysis.fingerprint_cache import load_cached_fingerprints, store_cached_fingerprints
from code_analysis.models import (
    Completeness,
    Confidence,
    Diagnostic,
    DiagnosticLevel,
    Evidence,
    ProviderResult,
    ProviderState,
    SourceParseState,
    SourceSpan,
)
from code_analysis.project import iter_python_files, logical_path, module_name
from code_analysis.report_models import (
    AnalysisReport,
    AnalysisSubject,
    CloneCluster,
    CloneFragment,
    CloneKind,
    Finding,
    FlowKind,
    SectionCoverage,
    SectionState,
    SubjectKind,
    SummaryCounts,
)

CLONE_LIMITATIONS = (
    "Clone evidence is syntax-only; semantic equivalence and runtime behavior are unmeasured.",
    "Names and literals remain significant for token clones and are categorical in AST clones.",
)


def _stable_id(prefix: str, *parts: str) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:20]}"


def _evidence(item: CloneFingerprint) -> Evidence:
    return Evidence(
        span=SourceSpan(
            path=item.logical_path,
            start_line=item.start_line,
            start_column=item.start_column,
            end_line=item.end_line,
            end_column=item.end_column,
        ),
        provider="native-clones",
        observation=f"{item.kind.value} fingerprint {item.structural_signature}",
        confidence=Confidence.EXACT,
        source_sha256=item.source_sha256,
    )


def _subject_identity(item: CloneFingerprint) -> tuple[str, SubjectKind]:
    if ":" not in item.logical_name:
        return _stable_id("subject", "module", item.module), SubjectKind.MODULE
    qualified_name = item.logical_name.split(":", 1)[1]
    return _stable_id("subject", "symbol", item.module, qualified_name), SubjectKind.SYMBOL


def _fragment_id(item: CloneFingerprint, subject_id: str) -> str:
    return _stable_id(
        "clone-fragment",
        item.kind.value,
        subject_id,
        item.logical_path,
        str(item.start_line),
        str(item.start_column),
        str(item.end_line),
        str(item.end_column),
        item.fingerprint_sha256,
    )


def collect_clone_sources(
    context: AnalysisContext, report: AnalysisReport
) -> tuple[CloneSource, ...]:
    modules = {
        (item.name, item.path): item
        for item in report.modules
        if item.parse_state in {SourceParseState.COMPLETE, SourceParseState.PARTIAL}
    }
    sources: list[CloneSource] = []
    for path in iter_python_files(context):
        relative = logical_path(context, path)
        name = module_name(context, path)
        module = modules.get((name, relative))
        if module is None:
            continue
        payload = path.read_bytes()
        source = payload.decode("utf-8")
        source_sha256 = hashlib.sha256(payload).hexdigest()
        if source_sha256 != module.source_sha256:
            raise OSError("source changed during clone analysis")
        sources.append(CloneSource(name, relative, source_sha256, source))
    return tuple(sorted(sources, key=lambda item: (item.module, item.logical_path)))


def _discover(
    context: AnalysisContext,
    sources: tuple[CloneSource, ...],
    cache: ContentCache | None,
) -> CloneDiscovery:
    thresholds = context.config.clone_thresholds
    limits = context.config.phase_three_limits
    fingerprints: list[CloneFingerprint] = []
    for source in sources:
        cached = (
            load_cached_fingerprints(cache, source, context.config) if cache is not None else None
        )
        if cached is None:
            cached = fingerprint_clone_source(
                source,
                exact_min_lines=thresholds.exact_min_lines,
                token_min_tokens=thresholds.token_min_tokens,
                ast_min_nodes=thresholds.ast_min_nodes,
                statement_window=thresholds.statement_window,
                sequence_min_events=thresholds.sequence_min_events,
                max_clone_window_statements=thresholds.max_clone_window_statements,
            )
            if cache is not None:
                store_cached_fingerprints(cache, source, context.config, cached)
        fingerprints.extend(cached)
    return cluster_clone_fingerprints(
        tuple(fingerprints),
        max_candidate_pairs=limits.max_candidate_pairs,
        max_clusters=limits.max_clusters,
        max_cluster_members=limits.max_cluster_members,
    )


def integrate_clone_report(
    context: AnalysisContext,
    report: AnalysisReport,
    cache: ContentCache | None = None,
) -> AnalysisReport:
    """Append bounded clone evidence to a validated unified report."""

    sources = collect_clone_sources(context, report)
    discovery = _discover(context, sources, cache)
    existing_subjects = {item.subject_id: item for item in report.subjects}
    subjects_by_logical = {item.logical_name: item.subject_id for item in report.subjects}
    subject_ids: dict[CloneFingerprint, str] = {}
    for item in discovery.fingerprints:
        identity, kind = _subject_identity(item)
        subject_id = subjects_by_logical.get(item.logical_name, identity)
        subject_ids[item] = subject_id
        if subject_id not in existing_subjects:
            existing_subjects[subject_id] = AnalysisSubject(
                subject_id=subject_id,
                kind=kind,
                logical_name=item.logical_name,
                module=item.module,
                evidence=(_evidence(item),),
            )
            subjects_by_logical[item.logical_name] = subject_id
    native_fragments = tuple(
        sorted(
            (
                CloneFragment(
                    fragment_id=_fragment_id(item, subject_ids[item]),
                    kind=item.kind,
                    subject_id=subject_ids[item],
                    fingerprint_sha256=item.fingerprint_sha256,
                    structural_signature=item.structural_signature,
                    nonblank_lines=item.nonblank_lines,
                    token_count=item.token_count,
                    ast_node_count=item.ast_node_count,
                    statement_count=item.statement_count,
                    matched_event_count=(
                        int(item.structural_signature.rsplit(":", 1)[1])
                        if item.kind is CloneKind.CONTROL_CALL_SEQUENCE
                        else 0
                    ),
                    sequence_coverage=(
                        1.0 if item.kind is CloneKind.CONTROL_CALL_SEQUENCE else 0.0
                    ),
                    evidence=(_evidence(item),),
                    limitations=CLONE_LIMITATIONS,
                )
                for item in discovery.fingerprints
            ),
            key=lambda item: item.fragment_id,
        )
    )
    idiom_fragments: list[CloneFragment] = []
    idiom_groups: list[tuple[CloneKind, str, tuple[CloneFragment, ...]]] = []
    for pattern in report.validation_patterns:
        if len(pattern.subject_ids) < 2:
            continue
        signature = ":".join(
            (
                pattern.classification,
                pattern.rule,
                pattern.field_identity or "<no-field>",
            )
        )
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
        group_members: list[CloneFragment] = []
        for index, subject_id in enumerate(pattern.subject_ids):
            evidence = pattern.sites[min(index, len(pattern.sites) - 1)]
            fragment = CloneFragment(
                fragment_id=_stable_id(
                    "clone-fragment",
                    CloneKind.VALIDATION_IDIOM.value,
                    subject_id,
                    pattern.validation_id,
                ),
                kind=CloneKind.VALIDATION_IDIOM,
                subject_id=subject_id,
                fingerprint_sha256=digest,
                structural_signature=f"validation-idiom:v1:{signature}",
                nonblank_lines=0,
                token_count=0,
                ast_node_count=0,
                statement_count=1,
                evidence=(evidence,),
                limitations=(
                    "Validation idioms reuse Phase 2 syntax evidence; runtime enforcement "
                    "is unmeasured.",
                ),
            )
            idiom_fragments.append(fragment)
            group_members.append(fragment)
        idiom_groups.append((CloneKind.VALIDATION_IDIOM, digest, tuple(group_members)))
    serialization_groups: dict[tuple[str, str], list[tuple[str, Evidence]]] = {}
    known_subject_ids = set(existing_subjects)
    for flow in report.flow_edges:
        if flow.kind not in {
            FlowKind.SERIALIZATION,
            FlowKind.DESERIALIZATION,
            FlowKind.CONVERSION,
        }:
            continue
        subject_id = (
            flow.source_id
            if flow.source_id in known_subject_ids
            else flow.target_id if flow.target_id in known_subject_ids else None
        )
        if subject_id is None:
            continue
        evidence = flow.evidence[0]
        signature = (flow.kind.value, evidence.observation)
        serialization_groups.setdefault(signature, []).append((subject_id, evidence))
    for (flow_kind, observation), raw_members in sorted(serialization_groups.items()):
        members_by_subject = dict(raw_members)
        if len(members_by_subject) < 2:
            continue
        signature = f"{flow_kind}:{observation}"
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
        group_members = []
        for subject_id, evidence in sorted(members_by_subject.items()):
            fragment = CloneFragment(
                fragment_id=_stable_id(
                    "clone-fragment",
                    CloneKind.SERIALIZATION_IDIOM.value,
                    subject_id,
                    digest,
                ),
                kind=CloneKind.SERIALIZATION_IDIOM,
                subject_id=subject_id,
                fingerprint_sha256=digest,
                structural_signature=f"serialization-idiom:v1:{signature}",
                nonblank_lines=0,
                token_count=0,
                ast_node_count=0,
                statement_count=1,
                evidence=(evidence,),
                limitations=(
                    "Serialization idioms reuse Phase 2 syntax evidence; runtime formats "
                    "are unmeasured.",
                ),
            )
            idiom_fragments.append(fragment)
            group_members.append(fragment)
        idiom_groups.append((CloneKind.SERIALIZATION_IDIOM, digest, tuple(group_members)))
    fragments = tuple(
        sorted((*native_fragments, *idiom_fragments), key=lambda item: item.fragment_id)
    )
    fragments_by_key = {
        (
            item.kind,
            item.subject_id,
            item.evidence[0].span.path,
            item.evidence[0].span.start_line,
            item.evidence[0].span.end_line,
            item.fingerprint_sha256,
        ): item.fragment_id
        for item in fragments
    }
    provisional: list[
        tuple[str, object, tuple[str, ...], tuple[str, ...], tuple[Evidence, ...]]
    ] = []
    for group in discovery.groups:
        group_fragment_ids = tuple(
            sorted(
                fragments_by_key[
                    (
                        member.kind,
                        subject_ids[member],
                        member.logical_path,
                        member.start_line,
                        member.end_line,
                        member.fingerprint_sha256,
                    )
                ]
                for member in group.members
            )
        )
        group_subject_ids = tuple(sorted({subject_ids[item] for item in group.members}))
        cluster_id = _stable_id(
            "clone-cluster", group.kind.value, group.fingerprint_sha256, *group_fragment_ids
        )
        provisional.append(
            (
                cluster_id,
                group.kind,
                group_fragment_ids,
                group_subject_ids,
                tuple(_evidence(item) for item in group.members),
            )
        )
    candidate_pairs = discovery.candidate_pairs
    truncated_pairs = discovery.truncated_pairs
    truncated_clusters = discovery.truncated_clusters
    truncated_members = discovery.truncated_members
    limits = context.config.phase_three_limits
    for kind, digest, raw_members in idiom_groups:
        subject_count = len({item.subject_id for item in raw_members})
        prospective_pairs = subject_count * (subject_count - 1) // 2
        if candidate_pairs + prospective_pairs > limits.max_candidate_pairs:
            truncated_pairs += prospective_pairs
            continue
        candidate_pairs += prospective_pairs
        if len(provisional) >= limits.max_clusters:
            truncated_clusters += 1
            continue
        members = raw_members
        if len(members) > limits.max_cluster_members:
            truncated_members += len(members) - limits.max_cluster_members
            members = members[: limits.max_cluster_members]
        fragment_ids = tuple(sorted(item.fragment_id for item in members))
        group_subject_ids = tuple(sorted({item.subject_id for item in members}))
        cluster_id = _stable_id("clone-cluster", kind.value, digest, *fragment_ids)
        provisional.append(
            (
                cluster_id,
                kind,
                fragment_ids,
                group_subject_ids,
                tuple(item.evidence[0] for item in members),
            )
        )
    retained_fragment_ids = {
        fragment_id
        for _cluster_id, _kind, values, _subjects, _evidence in provisional
        for fragment_id in values
    }
    fragments = tuple(item for item in fragments if item.fragment_id in retained_fragment_ids)
    cluster_subjects = {item[0]: set(item[3]) for item in provisional}
    clusters = tuple(
        sorted(
            (
                CloneCluster(
                    cluster_id=cluster_id,
                    kind=kind,
                    fragment_ids=fragment_ids,
                    subject_ids=group_subject_ids,
                    related_cluster_ids=tuple(
                        sorted(
                            other_id
                            for other_id, other_subjects in cluster_subjects.items()
                            if other_id != cluster_id
                            and len(set(group_subject_ids) & other_subjects) >= 2
                        )
                    ),
                    evidence=evidence,
                    confidence=Confidence.EXACT,
                    completeness=Completeness.COMPLETE,
                    limitations=CLONE_LIMITATIONS,
                )
                for cluster_id, kind, fragment_ids, group_subject_ids, evidence in provisional
            ),
            key=lambda item: item.cluster_id,
        )
    )
    truncated = truncated_pairs + truncated_clusters + truncated_members
    diagnostic = (
        (
            Diagnostic(
                code="clone-candidate-bound-truncated",
                level=DiagnosticLevel.WARNING,
                message=(
                    "clone bounds omitted "
                    f"{truncated_pairs} candidate pairs, "
                    f"{truncated_clusters} clusters, and "
                    f"{truncated_members} cluster members"
                ),
                provider="native-clones",
            ),
        )
        if truncated
        else ()
    )
    state = (
        SectionState.PARTIAL if truncated or report.summary.failed_files else SectionState.COMPLETE
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
    findings = tuple(
        sorted(
            (
                *(item.model_copy(update={"completeness": overall}) for item in report.findings),
                *(
                    Finding(
                        finding_id=_stable_id("finding", "duplication", cluster.cluster_id),
                        rule_id=f"duplication.{cluster.kind.value}",
                        category="duplication",
                        severity="warning",
                        title=f"{cluster.kind.value} clone cluster",
                        explanation=(
                            f"{len(cluster.subject_ids)} subjects share the recorded static "
                            "fingerprint; ownership and extraction require review."
                        ),
                        module=existing_subjects[cluster.subject_ids[0]].module,
                        subject_ids=cluster.subject_ids,
                        affected_clone_cluster_ids=(cluster.cluster_id,),
                        evidence=cluster.evidence,
                        counter_evidence=cluster.counter_evidence,
                        confidence=cluster.confidence,
                        completeness=overall,
                        limitations=cluster.limitations,
                        likely_validation_needs=(
                            "Phase 4 test-impact mapping before an extraction decision.",
                        ),
                    )
                    for cluster in clusters
                ),
            ),
            key=lambda item: item.finding_id,
        )
    )
    coverage = tuple(
        (
            SectionCoverage(
                section="clones",
                state=state,
                parsed=len(sources),
                failed=report.summary.failed_files,
                truncated=truncated,
                diagnostic_codes=tuple(item.code for item in diagnostic),
            )
            if item.section == "clones"
            else item
        )
        for item in report.section_coverage
    )
    provider = ProviderResult(
        provider_id="native-clones",
        state=ProviderState.PARTIAL if state is SectionState.PARTIAL else ProviderState.SUCCESS,
        executable="python",
        version=platform.python_version(),
        duration_ms=0,
        observations=tuple(sorted({item.kind.value for item in fragments})),
        diagnostics=diagnostic,
        completeness=(
            Completeness.PARTIAL if state is SectionState.PARTIAL else Completeness.COMPLETE
        ),
    )
    summary = SummaryCounts(
        **{
            **report.summary.model_dump(),
            "subjects": len(existing_subjects),
            "clone_fragments": len(fragments),
            "clone_clusters": len(clusters),
            "findings": len(findings),
        }
    )
    phase_three_configuration = report.configuration.model_copy(
        update={"digest_sha256": context.config.phase_three_identity()}
    )
    identity_payload = json.dumps(
        {
            "configuration": phase_three_configuration.digest_sha256,
            "prior": report.run_identity_sha256,
            "fragments": [item.model_dump(mode="json") for item in fragments],
            "clusters": [item.model_dump(mode="json") for item in clusters],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    limitations = {*report.limitations, *CLONE_LIMITATIONS}
    if truncated:
        limitations.add("Configured Phase 3 bounds truncated clone coverage.")
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "run_identity_sha256": hashlib.sha256(identity_payload).hexdigest(),
            "configuration": phase_three_configuration,
            "subjects": tuple(existing_subjects[key] for key in sorted(existing_subjects)),
            "clone_fragments": fragments,
            "clone_clusters": clusters,
            "section_coverage": coverage,
            "findings": findings,
            "diagnostics": (*report.diagnostics, *diagnostic),
            "providers": (*report.providers, provider),
            "completeness": overall,
            "limitations": tuple(sorted(limitations)),
            "summary": summary,
        }
    )
