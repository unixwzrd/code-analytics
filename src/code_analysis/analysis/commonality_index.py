"""Bounded tuple generation after transparent shared-dimension preselection."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import islice

from code_analysis.report_models import CommonalityDimensionKind


@dataclass(frozen=True, slots=True)
class IndexedCandidates:
    pairs: tuple[tuple[str, str], ...]
    omitted_lower_bound: int
    dimension_exclusions: int
    dimension_exclusions_exact: bool
    excluded_examples: tuple[tuple[str, str], ...]


def _positions(mask: int) -> Iterator[int]:
    while mask:
        bit = mask & -mask
        yield bit.bit_length() - 1
        mask ^= bit


def _qualified_mask(masks: tuple[int, ...], minimum: int) -> tuple[int, int]:
    """Return union and peers sharing at least minimum distinct dimensions."""
    matches = [0] * minimum
    for mask in masks:
        for level in range(minimum - 1, 0, -1):
            matches[level] |= matches[level - 1] & mask
        matches[0] |= mask
    return matches[0], matches[-1]


def indexed_candidates(
    names: tuple[str, ...],
    allowed: Mapping[tuple[CommonalityDimensionKind, str], tuple[str, ...]],
    clone_pairs: set[tuple[str, str]],
    *,
    minimum_dimensions: int,
    maximum_pairs: int,
    maximum_examples: int,
) -> IndexedCandidates:
    if not 1 <= minimum_dimensions <= 6:
        raise ValueError("ownership dimension minimum must be between one and six")
    pairs = set(sorted(clone_pairs)[:maximum_pairs])
    if len(clone_pairs) > maximum_pairs:
        return IndexedCandidates(tuple(sorted(pairs)), 1, 0, False, ())
    positions = {name: index for index, name in enumerate(names)}
    shared: list[dict[CommonalityDimensionKind, int]] = [{} for _ in names]
    for (kind, _identity), members in allowed.items():
        if len(members) < 2:
            continue
        mask = sum(1 << positions[name] for name in members)
        for name in members:
            dimensions = shared[positions[name]]
            dimensions[kind] = dimensions.get(kind, 0) | mask
    clones: list[int] = [0] * len(names)
    for left, right in clone_pairs:
        clones[positions[left]] |= 1 << positions[right]
    exclusions = 0
    examples: list[tuple[str, str]] = []
    for index, left in enumerate(names):
        union, qualified = _qualified_mask(tuple(shared[index].values()), minimum_dimensions)
        higher = ~((1 << (index + 1)) - 1)
        rejected = union & higher & ~qualified & ~clones[index]
        exclusions += rejected.bit_count()
        for position in islice(_positions(rejected), maximum_examples - len(examples)):
            examples.append((left, names[position]))
        for position in _positions(qualified & higher & ~clones[index]):
            if len(pairs) >= maximum_pairs:
                return IndexedCandidates(
                    tuple(sorted(pairs)), 1, exclusions, False, tuple(examples)
                )
            pairs.add((left, names[position]))
    return IndexedCandidates(tuple(sorted(pairs)), 0, exclusions, True, tuple(examples))
