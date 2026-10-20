"""Conservative typed topology facts for evidenced dictionary structures."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from code_analysis.analysis.dictionary_ops import DictionaryAnalysisFacts
from code_analysis.analysis.dictionary_values import (
    DictionaryOperationKind,
    InternalSourceSpan,
)
from code_analysis.analysis.shapes import DictionaryShapeBuildResult


class InternalStructureScope(StrEnum):
    LOCAL = "local"
    INSTANCE = "instance"
    CLASS = "class"
    MODULE = "module"


@dataclass(frozen=True, slots=True)
class DataStructureTopologyFact:
    owner: str
    scope: InternalStructureScope
    constructor_owners: tuple[str, ...]
    producer_owners: tuple[str, ...]
    reader_owners: tuple[str, ...]
    writer_owners: tuple[str, ...]
    mutator_owners: tuple[str, ...]
    alias_owners: tuple[str, ...]
    invariants: tuple[str, ...]
    ordering_assumptions: tuple[str, ...]
    lifetime: tuple[str, ...]
    cleanup: tuple[str, ...]
    concurrency: tuple[str, ...]
    growth_indicators: tuple[str, ...]
    evidence: tuple[InternalSourceSpan, ...]
    unresolved_behavior: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.owner or not self.evidence:
            raise ValueError("data-structure topology requires owner and evidence")
        for label, values in (
            ("constructors", self.constructor_owners),
            ("producers", self.producer_owners),
            ("readers", self.reader_owners),
            ("writers", self.writer_owners),
            ("mutators", self.mutator_owners),
            ("aliases", self.alias_owners),
            ("invariants", self.invariants),
            ("ordering assumptions", self.ordering_assumptions),
            ("lifetime", self.lifetime),
            ("cleanup", self.cleanup),
            ("concurrency", self.concurrency),
            ("growth indicators", self.growth_indicators),
            ("unresolved behavior", self.unresolved_behavior),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"data-structure {label} must be unique and sorted")
        if tuple(sorted(set(self.evidence), key=_span_sort_key)) != self.evidence:
            raise ValueError("data-structure evidence must be unique and sorted")


def discover_data_structure_topology(
    facts: tuple[DictionaryAnalysisFacts, ...],
    shapes: tuple[DictionaryShapeBuildResult, ...],
) -> tuple[DataStructureTopologyFact, ...]:
    """Build syntax-owned structure records without inferring runtime behavior."""

    if len(facts) != len(shapes):
        raise ValueError("dictionary facts and shape results must align")
    results: list[DataStructureTopologyFact] = []
    for bundle, shape_result in zip(facts, shapes, strict=True):
        operations_by_owner = {
            owner: tuple(item for item in bundle.operations if item.owner == owner)
            for owner in {item.owner for item in bundle.operations}
        }
        defaults_by_owner = {
            owner: tuple(item for item in bundle.defaults if item.owner == owner)
            for owner in {item.owner for item in bundle.defaults}
        }
        aliases_by_owner = {
            owner: tuple(item for item in bundle.aliases if item.source_owner == owner)
            for owner in {item.source_owner for item in bundle.aliases}
        }
        guards_by_owner = {
            owner: tuple(item for item in bundle.mutation_guards if item.owner == owner)
            for owner in {item.owner for item in bundle.mutation_guards}
        }
        lock_orders_by_owner = {
            owner: tuple(item for item in bundle.lock_orders if item.owner == owner)
            for owner in {item.owner for item in bundle.lock_orders}
        }
        shapes_by_owner = {shape.owner: shape for shape in shape_result.shapes}
        candidate_owners = {
            *operations_by_owner,
            *defaults_by_owner,
            *aliases_by_owner,
            *guards_by_owner,
            *shapes_by_owner,
        }
        for owner in sorted(candidate_owners):
            shape = shapes_by_owner.get(owner)
            operations = operations_by_owner.get(owner, ())
            defaults = defaults_by_owner.get(owner, ())
            aliases = aliases_by_owner.get(owner, ())
            guards = guards_by_owner.get(owner, ())
            lock_orders = lock_orders_by_owner.get(owner, ())
            mutation_kinds = {
                DictionaryOperationKind.WRITE,
                DictionaryOperationKind.DELETE,
                DictionaryOperationKind.UPDATE,
                DictionaryOperationKind.POP,
                DictionaryOperationKind.SET_DEFAULT,
                DictionaryOperationKind.MERGE,
            }
            read_kinds = {
                DictionaryOperationKind.READ,
                DictionaryOperationKind.GET,
                DictionaryOperationKind.MEMBERSHIP,
                DictionaryOperationKind.TRUTHINESS,
                DictionaryOperationKind.COERCION,
                DictionaryOperationKind.ARGUMENT,
                DictionaryOperationKind.RETURN,
                DictionaryOperationKind.VALIDATION,
            }
            has_constructor = any(
                item.kind
                in {
                    DictionaryOperationKind.LITERAL,
                    DictionaryOperationKind.FACTORY,
                    DictionaryOperationKind.COMPREHENSION,
                }
                for item in operations
            )
            has_reader = any(item.kind in read_kinds for item in operations)
            has_writer = any(item.kind in mutation_kinds for item in operations)
            scope = _scope(owner)
            invariants = tuple(
                sorted(
                    {
                        f"discriminator {variant.discriminator_field}: "
                        + ",".join(variant.discriminator_values_json)
                        for variant in (shape.variants if shape is not None else ())
                        if variant.discriminator_field is not None
                    }
                )
            )
            lifecycle_states = {
                state
                for variant in (shape.variants if shape is not None else ())
                for state in variant.lifecycle_states
            }
            ordering = ("exact state-write order observed",) if lifecycle_states else ()
            growth = (
                ("module-scope mutable mapping receives writes; measure boundedness",)
                if scope is InternalStructureScope.MODULE and has_writer
                else ()
            )
            cleanup = tuple(
                sorted(
                    {
                        *(
                            {"exact mapping clear observed"}
                            if any(
                                item.kind is DictionaryOperationKind.CLEAR for item in operations
                            )
                            else set()
                        ),
                        *(
                            {"exact mapping entry removal observed"}
                            if any(
                                item.kind
                                in {
                                    DictionaryOperationKind.DELETE,
                                    DictionaryOperationKind.POP,
                                }
                                for item in operations
                            )
                            else set()
                        ),
                    }
                )
            )
            concurrency = tuple(
                sorted(
                    {
                        f"{item.kind.value} context lexically guards mutation via "
                        f"{item.guard_identity}"
                        for item in guards
                    }
                    | {
                        "lexical lock nesting order: " + " -> ".join(item.guard_identities)
                        for item in lock_orders
                    }
                )
            )
            unresolved_items = set()
            if owner in shape_result.unresolved_owners:
                unresolved_items.add(
                    "Some operations could not be assigned to a literal-string field."
                )
            if shape is None:
                unresolved_items.add(
                    "Dictionary field shape remains unresolved for this evidenced structure."
                )
            if guards:
                unresolved_items.add(
                    "Lexical lock context does not prove runtime mutual exclusion or complete "
                    "coordination."
                )
            elif scope is InternalStructureScope.MODULE and has_writer:
                unresolved_items.add(
                    "No recognized lexical lock guard was observed; indirect or external "
                    "coordination is unresolved."
                )
            unresolved = tuple(sorted(unresolved_items))
            evidence = tuple(
                sorted(
                    {
                        *(item.span for item in operations),
                        *(item.span for item in defaults),
                        *(item.span for item in aliases),
                        *(item.span for item in shape_result.transitions if item.owner == owner),
                    },
                    key=_span_sort_key,
                )
            )
            results.append(
                DataStructureTopologyFact(
                    owner=owner,
                    scope=scope,
                    constructor_owners=(owner,) if has_constructor else (),
                    producer_owners=(
                        shape.producers
                        if shape is not None
                        else ((owner,) if has_constructor else ())
                    ),
                    reader_owners=(owner,) if has_reader else (),
                    writer_owners=(owner,) if has_writer else (),
                    mutator_owners=(owner,) if has_writer else (),
                    alias_owners=tuple(sorted(item.alias_identity for item in aliases)),
                    invariants=invariants,
                    ordering_assumptions=ordering,
                    lifetime=(
                        ("module-scope",)
                        if scope is InternalStructureScope.MODULE
                        else (
                            ("instance-owned",)
                            if scope is InternalStructureScope.INSTANCE
                            else (
                                ("class-owned",)
                                if scope is InternalStructureScope.CLASS
                                else ("lexical-scope",)
                            )
                        )
                    ),
                    cleanup=cleanup,
                    concurrency=concurrency,
                    growth_indicators=growth,
                    evidence=evidence,
                    unresolved_behavior=unresolved,
                )
            )
    return tuple(sorted(results, key=lambda item: item.owner))


def _scope(owner: str) -> InternalStructureScope:
    parts = owner.split(":", 2)
    if len(parts) >= 3 and parts[2].startswith("self."):
        return InternalStructureScope.INSTANCE
    if len(parts) >= 3 and parts[2].startswith("<class>."):
        return InternalStructureScope.CLASS
    return (
        InternalStructureScope.MODULE
        if len(parts) >= 2 and parts[1] == "<module>"
        else InternalStructureScope.LOCAL
    )


def _span_sort_key(span: InternalSourceSpan) -> tuple[object, ...]:
    return (
        span.logical_path,
        span.start_line,
        span.start_column,
        span.end_line,
        span.end_column,
    )
