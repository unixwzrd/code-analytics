"""Transparent static abstraction-cost and logical-owner estimates."""

from __future__ import annotations

import hashlib
import statistics

from code_analysis.models import Completeness, Confidence, Evidence
from code_analysis.report_models import (
    AbstractionCostEstimate,
    AbstractionWarningKind,
    AnalysisReport,
    CloneKind,
    EstimateRange,
    OwnerAlternative,
    OwnerAlternativeClass,
    SignedEstimateRange,
)


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}-{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:20]}"


def _range(lower: int, likely: int, upper: int) -> EstimateRange:
    return EstimateRange(lower=max(0, lower), likely=max(0, likely), upper=max(0, upper))


def _signed_range(lower: int, likely: int, upper: int) -> SignedEstimateRange:
    return SignedEstimateRange(lower=lower, likely=likely, upper=upper)


def _subject_loc(report: AnalysisReport, subject_id: str) -> int:
    subject = next(item for item in report.subjects if item.subject_id == subject_id)
    spans = [item.span for item in subject.evidence]
    return max((span.end_line - span.start_line + 1 for span in spans), default=0)


def _duplicate_loc(report: AnalysisReport, subject_ids: tuple[str, ...]) -> tuple[int, int]:
    selected = set(subject_ids)
    fragments = [
        item
        for item in report.clone_fragments
        if item.subject_id in selected
        and item.kind
        in {
            CloneKind.EXACT_TEXT,
            CloneKind.NORMALIZED_TOKEN,
            CloneKind.NORMALIZED_AST,
        }
    ]
    by_subject: dict[str, int] = {}
    for fragment in fragments:
        by_subject[fragment.subject_id] = max(
            by_subject.get(fragment.subject_id, 0), fragment.nonblank_lines
        )
    return sum(by_subject.values()), min(by_subject.values(), default=0)


def _warning_for_owner(owner: str) -> tuple[AbstractionWarningKind, ...]:
    leaf = owner.rsplit(".", 1)[-1].lower()
    warnings = []
    if leaf in {"common", "shared", "utils", "utilities", "helpers"} or leaf.startswith("shared_"):
        warnings.append(AbstractionWarningKind.GENERIC_COMMON_MODULE)
    if "compat" in leaf or "shim" in leaf:
        warnings.append(AbstractionWarningKind.COMPATIBILITY_SHIM)
    return tuple(sorted(warnings, key=lambda item: item.value))


def estimate_candidate_cost(
    report: AnalysisReport,
    *,
    candidate_id: str,
    subject_ids: tuple[str, ...],
    proposed_owner: str,
    consumer_ids: tuple[str, ...],
    evidence: tuple[Evidence, ...],
    graph_edge_delta: int,
) -> tuple[AbstractionCostEstimate, tuple[OwnerAlternative, ...]]:
    modules = tuple(
        sorted({item.module for item in report.subjects if item.subject_id in set(subject_ids)})
    )
    member_locs = sorted(_subject_loc(report, item) for item in subject_ids)
    duplicate_loc, common_loc = _duplicate_loc(report, subject_ids)
    likely_member = int(statistics.median(member_locs)) if member_locs else 0
    maximum_member = max(member_locs, default=0)
    viable_existing = bool(modules)
    owner_specs = []
    if modules:
        owner_specs.append((OwnerAlternativeClass.EXISTING_PRIMITIVE, modules[0], True, True))
        owner_specs.extend(
            (OwnerAlternativeClass.EXISTING_DOMAIN_MODULE, module, True, True) for module in modules
        )
    owner_specs.append((OwnerAlternativeClass.NEW_OWNER, proposed_owner, False, True))
    alternatives = tuple(
        sorted(
            (
                OwnerAlternative(
                    alternative_id=_stable_id(
                        "owner-alternative", candidate_id, owner_class.value, logical_owner
                    ),
                    candidate_id=candidate_id,
                    owner_class=owner_class,
                    logical_owner=logical_owner,
                    existing=existing,
                    viable=viable,
                    warnings=_warning_for_owner(logical_owner),
                    evidence=evidence,
                )
                for owner_class, logical_owner, existing, viable in owner_specs
            ),
            key=lambda item: item.alternative_id,
        )
    )
    retained_observations = (
        common_loc if common_loc else min(member_locs, default=0),
        likely_member,
        maximum_member,
    )
    retained_lower = min(retained_observations)
    retained_upper = max(retained_observations)
    retained_likely = min(max(likely_member, retained_lower), retained_upper)
    deleted = _range(
        max(0, duplicate_loc - retained_upper),
        max(0, duplicate_loc - retained_likely),
        max(0, duplicate_loc - retained_lower),
    )
    added_owner = _range(
        0 if viable_existing else retained_lower,
        0 if viable_existing else retained_likely,
        0 if viable_existing else retained_upper,
    )
    migration = _range(0, len(consumer_ids), len(consumer_ids) * 2)
    assert deleted.lower is not None and deleted.likely is not None and deleted.upper is not None
    assert added_owner.lower is not None and added_owner.likely is not None
    assert added_owner.upper is not None and migration.lower is not None
    assert migration.likely is not None and migration.upper is not None
    net = _signed_range(
        added_owner.lower + migration.lower - deleted.upper,
        added_owner.likely + migration.likely - deleted.likely,
        added_owner.upper + migration.upper - deleted.lower,
    )
    estimate = AbstractionCostEstimate(
        cost_estimate_id=_stable_id("abstraction-cost", candidate_id),
        candidate_id=candidate_id,
        existing_duplicate_loc=duplicate_loc,
        expected_deleted_loc=deleted,
        added_owner_loc=added_owner,
        migration_adapter_loc=migration,
        net_production_loc=net,
        expected_test_requirement_categories=4,
        new_files=_range(0, 0 if viable_existing else 1, 1),
        new_types=_range(0, 0, 1),
        new_callbacks=_range(0, 0, 0),
        new_parameters=_range(0, 0, 2),
        new_options=_range(0, 0, 2),
        new_imports=_range(0, len(modules), len(modules) + len(consumer_ids)),
        graph_edge_change=SignedEstimateRange(
            lower=graph_edge_delta,
            likely=graph_edge_delta,
            upper=graph_edge_delta,
        ),
        derivation=tuple(
            sorted(
                (
                    "Duplicate LOC is the union of maximal exact-clone member spans.",
                    "Migration LOC is bounded by direct recorded consumers.",
                    "Net LOC uses interval addition for owner and migration LOC minus deletion.",
                    "Owner alternatives are evaluated independently of naming preferences.",
                )
            )
        ),
        confidence=Confidence.INFERRED,
        completeness=(Completeness.COMPLETE if duplicate_loc else Completeness.PARTIAL),
        limitations=(
            "Static LOC ranges do not measure readability, semantic value, or maintenance cost.",
        ),
    )
    return estimate, alternatives


def cost_gate(estimate: AbstractionCostEstimate) -> tuple[str, bool, str]:
    values = estimate.net_production_loc
    if values.lower is None or values.upper is None:
        return "unresolved", True, "Net production LOC is unresolved."
    if values.upper < 0:
        return "passed", False, "The upper net production LOC estimate is negative."
    if values.lower >= 0:
        return "failed", True, "The lower net production LOC estimate is nonnegative."
    return (
        "unresolved",
        True,
        "The net production LOC estimate spans favorable and unfavorable outcomes.",
    )
