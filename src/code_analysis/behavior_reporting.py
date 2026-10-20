"""Conservative behavior-preservation evidence derived from language-front-end facts."""

from __future__ import annotations

import hashlib

from code_analysis.models import Completeness, Confidence
from code_analysis.report_models import (
    AnalysisReport,
    BehavioralEvent,
    BehavioralEventKind,
    BehavioralInvariant,
    CandidateBehaviorDifference,
    FunctionFeatureProfile,
    ProposalGateOutcome,
)


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}-{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:20]}"


def _effect_kind(value: str) -> BehavioralEventKind:
    lowered = value.lower()
    if "read" in lowered and any(item in lowered for item in ("file", "path", "filesystem")):
        return BehavioralEventKind.FILE_READ
    if "fsync" in lowered:
        return BehavioralEventKind.FSYNC
    if "replace" in lowered or "rename" in lowered:
        return BehavioralEventKind.REPLACE
    if "cleanup" in lowered or "delete" in lowered or "unlink" in lowered:
        return BehavioralEventKind.CLEANUP
    if any(item in lowered for item in ("write", "mutation", "storage", "dom")):
        return BehavioralEventKind.MUTATION
    return BehavioralEventKind.EXTERNAL_CALL


def _dictionary_kind(value: str) -> BehavioralEventKind | None:
    lowered = value.lower()
    if "default" in lowered or ".get" in lowered or "setdefault" in lowered:
        return BehavioralEventKind.DEFAULT_HANDLING
    if "null" in lowered or "none" in lowered:
        return BehavioralEventKind.NULL_HANDLING
    if "missing" in lowered or "membership" in lowered:
        return BehavioralEventKind.MISSING_HANDLING
    if "serialize" in lowered or "json" in lowered:
        return BehavioralEventKind.SERIALIZATION
    return None


def _profile_events(profile: FunctionFeatureProfile) -> tuple[tuple[BehavioralEventKind, str], ...]:
    values: set[tuple[BehavioralEventKind, str]] = set()
    values.update((BehavioralEventKind.EXCEPTION, item) for item in profile.exceptions)
    values.update((_effect_kind(item), item) for item in profile.side_effects)
    values.update((BehavioralEventKind.BRANCH_PREDICATE, item) for item in profile.control_flow)
    for item in profile.dictionary_schema_operations:
        kind = _dictionary_kind(item)
        if kind is not None:
            values.add((kind, item))
    return tuple(sorted(values, key=lambda item: (item[0].value, item[1])))


def evaluate_candidate_behavior(
    report: AnalysisReport,
    *,
    candidate_id: str,
    subject_ids: tuple[str, ...],
) -> tuple[
    tuple[BehavioralEvent, ...],
    tuple[BehavioralInvariant, ...],
    tuple[CandidateBehaviorDifference, ...],
    ProposalGateOutcome,
    str,
]:
    profiles = {item.subject_id: item for item in report.function_profiles}
    subjects = {item.subject_id: item for item in report.subjects}
    selected = tuple(profiles[item] for item in subject_ids if item in profiles)
    if len(selected) != len(subject_ids):
        return (
            (),
            (),
            (),
            ProposalGateOutcome.UNRESOLVED,
            ("One or more candidate subjects lack behavior-capable function profiles."),
        )
    events = tuple(
        sorted(
            (
                BehavioralEvent(
                    event_id=_stable_id("behavior-event", profile.subject_id, kind.value, identity),
                    subject_id=profile.subject_id,
                    kind=kind,
                    identity=identity,
                    evidence=profile.evidence,
                    confidence=Confidence.EXACT,
                )
                for profile in selected
                for kind, identity in _profile_events(profile)
            ),
            key=lambda item: item.event_id,
        )
    )
    by_subject_kind: dict[tuple[str, BehavioralEventKind], set[str]] = {}
    events_by_subject_kind: dict[tuple[str, BehavioralEventKind], list[BehavioralEvent]] = {}
    for event in events:
        key = (event.subject_id, event.kind)
        by_subject_kind.setdefault(key, set()).add(event.identity)
        events_by_subject_kind.setdefault(key, []).append(event)
    invariants: list[BehavioralInvariant] = []
    differences: list[CandidateBehaviorDifference] = []
    compared_kinds = tuple(
        sorted(
            {kind for _subject_id, kind in by_subject_kind},
            key=lambda item: item.value,
        )
    )
    for kind in compared_kinds:
        sets = [by_subject_kind.get((subject_id, kind), set()) for subject_id in subject_ids]
        if all(value == sets[0] for value in sets) and sets[0]:
            invariant_events = tuple(
                sorted(
                    (
                        event
                        for subject_id in subject_ids
                        for event in events_by_subject_kind.get((subject_id, kind), ())
                    ),
                    key=lambda item: item.event_id,
                )
            )
            invariants.append(
                BehavioralInvariant(
                    invariant_id=_stable_id("behavior-invariant", candidate_id, kind.value),
                    candidate_id=candidate_id,
                    kind=kind,
                    participating_subject_ids=subject_ids,
                    event_ids=tuple(item.event_id for item in invariant_events),
                    evidence=tuple(item for event in invariant_events for item in event.evidence),
                    completeness=Completeness.COMPLETE,
                )
            )
            continue
        if kind in {
            BehavioralEventKind.EXCEPTION,
            BehavioralEventKind.FILE_READ,
            BehavioralEventKind.MUTATION,
            BehavioralEventKind.REPLACE,
            BehavioralEventKind.CLEANUP,
            BehavioralEventKind.EXTERNAL_CALL,
        }:
            left_subject_id = subject_ids[0]
            left = sets[0]
            for right_subject_id, right in zip(subject_ids[1:], sets[1:], strict=True):
                if left == right:
                    continue
                differences.append(
                    CandidateBehaviorDifference(
                        difference_id=_stable_id(
                            "candidate-behavior-difference",
                            candidate_id,
                            kind.value,
                            left_subject_id,
                            right_subject_id,
                        ),
                        candidate_id=candidate_id,
                        kind=kind,
                        left_subject_id=left_subject_id,
                        right_subject_id=right_subject_id,
                        left_identities=tuple(sorted(left)),
                        right_identities=tuple(sorted(right)),
                        blocking=True,
                        evidence=(
                            subjects[left_subject_id].evidence[0],
                            subjects[right_subject_id].evidence[0],
                        ),
                        limitations=(
                            "Difference is syntax-evidenced and does not prove runtime outcome.",
                        ),
                    )
                )
    if differences:
        return (
            events,
            tuple(sorted(invariants, key=lambda item: item.invariant_id)),
            tuple(sorted(differences, key=lambda item: item.difference_id)),
            ProposalGateOutcome.FAILED,
            "Syntax evidence contains incompatible exception or side-effect behavior.",
        )
    if any(item.unresolved_call_count for item in selected):
        return (
            events,
            tuple(sorted(invariants, key=lambda item: item.invariant_id)),
            (),
            ProposalGateOutcome.UNRESOLVED,
            "Dynamic or unresolved calls prevent complete behavior comparison.",
        )
    return (
        events,
        tuple(sorted(invariants, key=lambda item: item.invariant_id)),
        (),
        ProposalGateOutcome.PASSED,
        "Supported exception and side-effect behavior has no evidenced incompatibility.",
    )
