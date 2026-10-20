"""Transparent function-feature extraction and inverted candidate generation."""

from __future__ import annotations

import math
from dataclasses import dataclass

from code_analysis.analysis.commonality_index import indexed_candidates
from code_analysis.analysis.function_features import (
    FunctionFeatures,
    discover_function_features,
)
from code_analysis.report_models import CommonalityDimensionKind

__all__ = ("FunctionFeatures", "discover_function_features", "generate_commonality_candidates")


@dataclass(frozen=True, slots=True)
class PrevalentFeatureFact:
    kind: CommonalityDimensionKind
    identity: str
    subject_count: int
    subject_threshold: int


@dataclass(frozen=True, slots=True)
class CandidateRejectionFact:
    left: str
    right: str
    coverage_rejected: bool
    shared_dimensions: tuple[CommonalityDimensionKind, ...]


@dataclass(frozen=True, slots=True)
class CommonalityCandidates:
    """Bounded results; truncated_pairs is a known lower bound, not a tail census."""

    pairs: tuple[tuple[str, str], ...]
    truncated_pairs: int
    postings_considered: int
    prevalent_features_suppressed: int
    unique_candidates: int
    index_dimension_rejections: int
    index_dimension_rejections_exact: bool
    coverage_rejections: int
    dimension_rejections: int
    clone_admissions: int
    prevalent_examples: tuple[PrevalentFeatureFact, ...]
    rejection_examples: tuple[CandidateRejectionFact, ...]


def generate_commonality_candidates(
    features: tuple[FunctionFeatures, ...],
    clone_pairs: tuple[tuple[str, str], ...],
    *,
    minimum_ownership_dimensions: int,
    minimum_bilateral_coverage: float,
    maximum_feature_subject_fraction: float,
    maximum_pairs: int,
    maximum_examples: int = 25,
) -> CommonalityCandidates:
    by_name = {item.logical_name: item for item in features}
    inverted: dict[tuple[CommonalityDimensionKind, str], list[str]] = {}
    for item in features:
        for kind, identities in item.ownership_dimensions():
            for identity in identities:
                inverted.setdefault((kind, identity), []).append(item.logical_name)
    subject_threshold = max(2, math.ceil(len(features) * maximum_feature_subject_fraction))
    prevalent = tuple(
        PrevalentFeatureFact(kind, identity, len(set(names)), subject_threshold)
        for (kind, identity), names in sorted(
            inverted.items(), key=lambda item: (item[0][0].value, item[0][1])
        )
        if len(set(names)) > subject_threshold
    )
    allowed = {
        key: tuple(sorted(set(names)))
        for key, names in inverted.items()
        if len(set(names)) <= subject_threshold
    }
    clone_pair_set = {
        (min(pair), max(pair))
        for pair in clone_pairs
        if pair[0] in by_name and pair[1] in by_name and pair[0] != pair[1]
    }
    indexed = indexed_candidates(
        tuple(sorted(by_name)),
        allowed,
        clone_pair_set,
        minimum_dimensions=minimum_ownership_dimensions,
        maximum_pairs=maximum_pairs,
        maximum_examples=maximum_examples,
    )
    candidate_pairs = set(indexed.pairs)
    index_examples = set(indexed.excluded_examples)
    eligible: list[tuple[str, str]] = []
    rejections: list[CandidateRejectionFact] = []
    coverage_rejections = 0
    dimension_rejections = 0
    clone_admissions = 0
    allowed_identities = {
        kind: {identity for candidate_kind, identity in allowed if candidate_kind is kind}
        for kind in CommonalityDimensionKind
    }
    for left_name, right_name in sorted(candidate_pairs | index_examples):
        left = by_name[left_name]
        right = by_name[right_name]
        raw_shared = 0
        qualified: list[CommonalityDimensionKind] = []
        for (kind, left_values), (right_kind, right_values) in zip(
            left.ownership_dimensions(), right.ownership_dimensions(), strict=True
        ):
            assert kind is right_kind
            left_set = set(left_values)
            right_set = set(right_values)
            shared = left_set & right_set & allowed_identities[kind]
            if not shared:
                continue
            raw_shared += 1
            if (
                len(shared) / max(1, len(left_set)) >= minimum_bilateral_coverage
                and len(shared) / max(1, len(right_set)) >= minimum_bilateral_coverage
            ):
                qualified.append(kind)
        pair = (left_name, right_name)
        if pair in index_examples:
            if len(rejections) < maximum_examples:
                rejections.append(
                    CandidateRejectionFact(
                        left_name,
                        right_name,
                        False,
                        tuple(sorted(qualified, key=lambda item: item.value)),
                    )
                )
            continue
        if pair in clone_pair_set or len(qualified) >= minimum_ownership_dimensions:
            eligible.append(pair)
            if pair in clone_pair_set:
                clone_admissions += 1
            continue
        coverage_rejected = raw_shared >= minimum_ownership_dimensions
        if coverage_rejected:
            coverage_rejections += 1
        else:
            dimension_rejections += 1
        if len(rejections) < maximum_examples:
            rejections.append(
                CandidateRejectionFact(
                    left=left_name,
                    right=right_name,
                    coverage_rejected=coverage_rejected,
                    shared_dimensions=tuple(sorted(qualified, key=lambda item: item.value)),
                )
            )
    return CommonalityCandidates(
        pairs=tuple(eligible),
        truncated_pairs=indexed.omitted_lower_bound,
        postings_considered=sum(len(set(names)) for names in inverted.values()),
        prevalent_features_suppressed=len(prevalent),
        unique_candidates=len(candidate_pairs),
        index_dimension_rejections=indexed.dimension_exclusions,
        index_dimension_rejections_exact=indexed.dimension_exclusions_exact,
        coverage_rejections=coverage_rejections,
        dimension_rejections=dimension_rejections,
        clone_admissions=clone_admissions,
        prevalent_examples=prevalent[:maximum_examples],
        rejection_examples=tuple(rejections),
    )
