"""Conservative representation-candidate facts from retained syntax evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from code_analysis.analysis.dictionary_ops import DictionaryAnalysisFacts
from code_analysis.analysis.dictionary_values import (
    DictionaryOperationKind,
    InternalSourceSpan,
)
from code_analysis.analysis.flow import DictionaryFlowObservation, InternalResolutionState
from code_analysis.analysis.shapes import DictionaryShapeBuildResult
from code_analysis.analysis.structures import DataStructureTopologyFact


class InternalCandidateKind(StrEnum):
    PYDANTIC_MODEL = "pydantic-model"
    TYPED_DICT = "typed-dict"
    LOCAL_DICTIONARY = "local-implementation-dictionary"


class InternalCandidateConfidence(StrEnum):
    EXACT = "exact"
    RESOLVED = "resolved"
    INFERRED = "inferred"


@dataclass(frozen=True, slots=True)
class DictionaryCandidateFact:
    owner: str
    candidate: InternalCandidateKind
    supporting_evidence: tuple[InternalSourceSpan, ...]
    counter_evidence: tuple[InternalSourceSpan, ...]
    confidence: InternalCandidateConfidence
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.owner or not self.supporting_evidence:
            raise ValueError("dictionary candidate requires owner and supporting evidence")
        if tuple(sorted(set(self.limitations))) != self.limitations:
            raise ValueError("dictionary candidate limitations must be unique and sorted")
        for label, values in (
            ("supporting evidence", self.supporting_evidence),
            ("counter evidence", self.counter_evidence),
        ):
            if tuple(sorted(set(values), key=_span_sort_key)) != values:
                raise ValueError(f"dictionary candidate {label} must be unique and sorted")


def classify_dictionary_candidates(
    facts: tuple[DictionaryAnalysisFacts, ...],
    shapes: tuple[DictionaryShapeBuildResult, ...],
    structures: tuple[DataStructureTopologyFact, ...],
    flows: tuple[DictionaryFlowObservation, ...],
) -> tuple[DictionaryCandidateFact, ...]:
    """Classify evidenced dictionary representations without recommending a change."""

    if len(facts) != len(shapes):
        raise ValueError("dictionary facts and shape results must align")
    facts_by_owner = {
        owner: bundle for bundle in facts for owner in {item.owner for item in bundle.operations}
    }
    shapes_by_owner = {shape.owner: (result, shape) for result in shapes for shape in result.shapes}
    results: list[DictionaryCandidateFact] = []
    for structure in structures:
        resolved_shape = shapes_by_owner.get(structure.owner)
        if resolved_shape is None:
            continue
        result, shape = resolved_shape
        bundle = facts_by_owner.get(structure.owner)
        operations = (
            tuple(item for item in bundle.operations if item.owner == structure.owner)
            if bundle is not None
            else ()
        )
        owner_flows = tuple(item for item in flows if item.source_owner == structure.owner)
        if any(item.resolution is not InternalResolutionState.RESOLVED for item in owner_flows):
            continue
        resolved_flows = tuple(
            item
            for item in owner_flows
            if item.resolution is InternalResolutionState.RESOLVED
            and item.target_identity != structure.owner
        )
        validations = tuple(
            item for item in operations if item.kind is DictionaryOperationKind.VALIDATION
        )
        discriminated = any(variant.discriminator_field is not None for variant in shape.variants)
        if discriminated and validations:
            candidate = InternalCandidateKind.PYDANTIC_MODEL
            confidence = InternalCandidateConfidence.INFERRED
            limitations = (
                "Static shape and validation evidence does not establish runtime model ownership.",
                "The classification is review evidence, not a migration instruction.",
            )
        elif resolved_flows:
            candidate = InternalCandidateKind.TYPED_DICT
            confidence = InternalCandidateConfidence.RESOLVED
            limitations = (
                "Resolved call flow does not establish that all producers and consumers agree.",
                "The classification is review evidence, not a migration instruction.",
            )
        else:
            candidate = InternalCandidateKind.LOCAL_DICTIONARY
            confidence = InternalCandidateConfidence.EXACT
            limitations = (
                "Local syntax evidence does not establish runtime escape behavior.",
                "The classification is review evidence, not a refactoring instruction.",
            )
        counter_spans = {
            item.span
            for item in operations
            if item.key_repr is None or item.owner in result.unresolved_owners
        }
        results.append(
            DictionaryCandidateFact(
                owner=structure.owner,
                candidate=candidate,
                supporting_evidence=structure.evidence,
                counter_evidence=tuple(sorted(counter_spans, key=_span_sort_key)),
                confidence=confidence,
                limitations=tuple(sorted(limitations)),
            )
        )
    return tuple(sorted(results, key=lambda item: item.owner))


def _span_sort_key(span: InternalSourceSpan) -> tuple[object, ...]:
    return (
        span.logical_path,
        span.start_line,
        span.start_column,
        span.end_line,
        span.end_column,
    )
