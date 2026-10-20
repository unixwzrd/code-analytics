"""Saved static/runtime joins and deterministic, explicit activity classification."""

from __future__ import annotations

import hashlib
import json
from fnmatch import fnmatchcase

from code_analysis.models import Completeness, evidence_identity
from code_analysis.profile_models import ProfileReport
from code_analysis.report_models import AnalysisReport, SubjectKind
from code_analysis.reports.json_report import canonical_json
from code_analysis.runtime_identity import CollectionMetadata
from code_analysis.runtime_models import CategoryMatch, RuntimeCorrelation, SourceBindingState
from code_analysis.runtime_spec import RuntimeAnalysisConfig


def profile_content_identity(report: ProfileReport) -> str:
    """Content identity excludes its own digest, not any captured evidence."""
    payload = report.model_dump(mode="json", exclude={"profile_identity_sha256"})
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _related_index(report: AnalysisReport | None) -> dict[str, set[str]]:
    related: dict[str, set[str]] = {}
    if report is None:
        return related

    def link(subject_ids: tuple[str, ...], identity: str) -> None:
        for subject_id in subject_ids:
            related.setdefault(subject_id, set()).add(identity)

    for clone in report.clone_clusters:
        link(clone.subject_ids, clone.cluster_id)
    for fragment in report.clone_fragments:
        link((fragment.subject_id,), fragment.fragment_id)
    for duplicate in report.duplicate_functions:
        link((duplicate.left_subject_id, duplicate.right_subject_id), duplicate.duplicate_id)
    for responsibility in report.responsibility_clusters:
        link(responsibility.member_subject_ids, responsibility.responsibility_id)
    for candidate in report.refactor_candidates:
        link(candidate.participating_subject_ids, candidate.candidate_id)
    for contract in report.contracts:
        link((contract.defining_subject_id,), contract.contract_id)
        for boundary in contract.boundaries:
            link((boundary.site_subject_id,), boundary.boundary_id)
    return related


def correlate_runtime(
    report: ProfileReport,
    *,
    source_report: AnalysisReport | None,
    metadata: CollectionMetadata | None,
    config: RuntimeAnalysisConfig,
) -> ProfileReport:
    categories = []
    correlations = []
    captured = {item.path: item.sha256 for item in metadata.source_hashes} if metadata else {}
    modules = {item.path: item for item in source_report.modules} if source_report else {}
    related = _related_index(source_report)
    observed: set[str] = set()
    bound_paths = {
        path
        for path, digest in captured.items()
        if path in modules and modules[path].source_sha256 == digest
    }
    for frame in report.frames:
        matches = sorted(
            (
                rule
                for rule in config.category_rules
                if fnmatchcase(frame.path, rule.path_pattern)
                and fnmatchcase(frame.function, rule.function_pattern)
            ),
            key=lambda rule: (rule.priority, rule.rule_id),
        )
        categories.append(
            CategoryMatch(
                frame_id=frame.frame_id,
                primary_category=matches[0].category if matches else None,
                rule_ids=tuple(rule.rule_id for rule in matches),
                categories=tuple(sorted({rule.category for rule in matches})),
            )
        )
        candidates = []
        if frame.path in bound_paths and source_report is not None:
            for subject in source_report.subjects:
                if frame.function == "<module>":
                    name_matches = subject.kind == SubjectKind.MODULE
                else:
                    name_matches = (
                        subject.logical_name.rsplit(":", 1)[-1].rsplit(".", 1)[-1] == frame.function
                    )
                if name_matches and any(
                    item.span.path == frame.path
                    and (
                        item.span.start_line <= frame.line <= item.span.end_line
                        or (frame.line == 0 and frame.function == "<module>")
                    )
                    for item in subject.evidence
                ):
                    candidates.append(subject)
        ids = tuple(sorted(subject.subject_id for subject in candidates))
        if len(ids) == 1:
            state = SourceBindingState.MATCHED
            limitations = ()
            observed.update(ids)
        elif ids:
            state = SourceBindingState.UNRESOLVED
            limitations = ("Multiple matching static subjects; no arbitrary subject selected.",)
        else:
            state = (
                SourceBindingState.MISMATCHED
                if frame.path in captured and frame.path not in bound_paths
                else SourceBindingState.UNBOUND
            )
            limitations = (
                "No unique hash-bound static subject; runtime-only or unresolved evidence.",
            )
        correlations.append(
            RuntimeCorrelation(
                frame_id=frame.frame_id,
                state=state,
                subject_ids=ids,
                related_ids=tuple(
                    sorted(
                        {
                            identity
                            for subject_id in ids
                            for identity in related.get(subject_id, set())
                        }
                    )
                ),
                module=modules[frame.path].name if frame.path in modules else None,
                complexity=(
                    next(
                        (
                            symbol.complexity
                            for symbol in modules[frame.path].symbols
                            if symbol.name == frame.function
                            and symbol.span.start_line == frame.line
                        ),
                        None,
                    )
                    if frame.path in bound_paths and len(ids) == 1
                    else None
                ),
                evidence_ids=tuple(
                    sorted(
                        {
                            evidence_identity(item)
                            for subject in candidates
                            for item in subject.evidence
                        }
                    )
                ),
                limitations=limitations,
            )
        )
    binding = (
        SourceBindingState.MATCHED
        if captured and set(captured) == bound_paths
        else (
            SourceBindingState.MISMATCHED
            if captured and source_report
            else SourceBindingState.UNBOUND
        )
    )
    runtime = report.runtime.model_copy(
        update={
            "categories": tuple(categories),
            "category_rules": config.category_rules,
            "correlations": tuple(correlations),
            "source_binding": binding,
            "source_hashes": metadata.source_hashes if metadata else (),
            "unobserved_subject_ids": (
                tuple(
                    sorted(
                        subject.subject_id
                        for subject in source_report.subjects
                        if subject.subject_id not in observed
                    )
                )
                if source_report
                else ()
            ),
        }
    )
    limitations = tuple(
        sorted(
            set(report.limitations)
            | {
                "Static call-edge overlay is unavailable; module "
                "imports are not substituted for calls.",
                "Unobserved static subjects are not evidence of dead "
                "code or exhaustive workload coverage.",
            }
        )
    )
    subject_by_frame = {
        item.frame_id: item.subject_ids[0]
        for item in correlations
        if item.state == SourceBindingState.MATCHED
    }
    result = report.model_copy(
        update={
            "runtime": runtime,
            "limitations": limitations,
            "source_report_sha256": (
                hashlib.sha256(canonical_json(source_report).encode()).hexdigest()
                if source_report
                else None
            ),
            "frames": tuple(
                item.model_copy(update={"subject_id": subject_by_frame.get(item.frame_id)})
                for item in report.frames
            ),
            "completeness": Completeness.PARTIAL,
        }
    )
    result = result.model_copy(update={"profile_identity_sha256": profile_content_identity(result)})
    return ProfileReport.model_validate(result.model_dump())
