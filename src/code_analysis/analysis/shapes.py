"""Conservative joins from dictionary observations to internal shape inputs."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace

from code_analysis.analysis.dictionary_ops import DictionaryAnalysisFacts
from code_analysis.analysis.dictionary_values import (
    DictionaryDefaultObservation,
    DictionaryDefaultRole,
    DictionaryFieldDefaultObservation,
    DictionaryFieldFact,
    DictionaryKeyKind,
    DictionaryOperationKind,
    DictionaryOperationObservation,
    DictionaryRequirednessObservation,
    DictionaryShapeJoinInput,
    DictionaryStateTransitionObservation,
    DictionaryVariantJoinInput,
    InternalRequirednessKind,
)


@dataclass(frozen=True, slots=True)
class DictionaryShapeBuildResult:
    shapes: tuple[DictionaryShapeJoinInput, ...]
    transitions: tuple[DictionaryStateTransitionObservation, ...]
    unresolved_owners: tuple[str, ...]
    unresolved_observation_count: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.unresolved_observation_count, bool)
            or not isinstance(self.unresolved_observation_count, int)
            or self.unresolved_observation_count < 0
        ):
            raise ValueError("unresolved observation count must be a nonnegative exact integer")
        if list(self.unresolved_owners) != sorted(self.unresolved_owners) or len(
            self.unresolved_owners
        ) != len(set(self.unresolved_owners)):
            raise ValueError("unresolved shape owners must be unique and sorted")


def build_dictionary_shape_inputs(
    facts: DictionaryAnalysisFacts,
) -> DictionaryShapeBuildResult:
    """Build one conservative observed variant per evidenced lexical owner."""

    operations_by_owner: dict[str, list[DictionaryOperationObservation]] = defaultdict(list)
    defaults_by_owner = defaultdict(list)
    for operation in facts.operations:
        operations_by_owner[operation.owner].append(operation)
    for default in facts.defaults:
        defaults_by_owner[default.owner].append(default)

    shapes: list[DictionaryShapeJoinInput] = []
    transitions: list[DictionaryStateTransitionObservation] = []
    unresolved_owners: set[str] = set()
    unresolved_count = 0
    all_owners = sorted(set(facts.owners) | set(operations_by_owner) | set(defaults_by_owner))
    for owner in all_owners:
        owner_operations = operations_by_owner[owner]
        field_operations = [
            item for item in owner_operations if item.kind is not DictionaryOperationKind.CLEAR
        ]
        owner_defaults = defaults_by_owner[owner]
        named_operations = [
            item
            for item in field_operations
            if item.key_kind is DictionaryKeyKind.LITERAL_STRING and item.key_repr is not None
        ]
        named_defaults = [
            item for item in owner_defaults if item.key_kind is DictionaryKeyKind.LITERAL_STRING
        ]
        unresolved_count += len(field_operations) - len(named_operations)
        unresolved_count += len(owner_defaults) - len(named_defaults)
        if not named_operations and not named_defaults:
            if owner_operations or owner_defaults:
                unresolved_owners.add(owner)
            continue
        variant_names = sorted(
            {
                item.variant
                for item in named_operations
                if item.variant is not None and _is_variant_defining(item.kind)
            }
            | {
                item.variant
                for item in named_defaults
                if item.variant is not None and item.role is DictionaryDefaultRole.PRODUCER_PROVIDED
            }
        )
        has_common_producer = any(
            item.variant is None and _is_variant_defining(item.kind) for item in named_operations
        )
        variant_specs = (
            sorted((["observed"] if has_common_producer else []) + variant_names)
            if variant_names
            else ["observed"]
        )
        variants: list[DictionaryVariantJoinInput] = []
        for variant_name in variant_specs:
            variant_operations = [
                item
                for item in named_operations
                if not variant_names or item.variant is None or item.variant == variant_name
            ]
            variant_defaults = [
                item
                for item in named_defaults
                if not variant_names or item.variant is None or item.variant == variant_name
            ]
            field_names = sorted(
                {item.key_repr for item in variant_operations if item.key_repr is not None}
                | {item.key_repr for item in variant_defaults}
            )
            fields = tuple(
                _build_field(
                    field_name,
                    [item for item in variant_operations if item.key_repr == field_name],
                    [item for item in variant_defaults if item.key_repr == field_name],
                )
                for field_name in field_names
            )
            variants.append(DictionaryVariantJoinInput(name=variant_name, fields=fields))
        joined_variants = _apply_discriminator(tuple(variants))
        owner_transitions = _state_transitions(owner, joined_variants)
        joined_variants = _apply_lifecycle_states(joined_variants, owner_transitions)
        transitions.extend(owner_transitions)
        producer = any(_is_producer(item.kind) for item in owner_operations)
        consumer = any(_is_consumer(item.kind) for item in owner_operations)
        shapes.append(
            DictionaryShapeJoinInput(
                owner=owner,
                variants=joined_variants,
                producers=(owner,) if producer else (),
                consumers=(owner,) if consumer else (),
            )
        )
    return DictionaryShapeBuildResult(
        shapes=tuple(sorted(shapes, key=lambda item: item.owner)),
        transitions=tuple(
            sorted(
                {
                    (
                        item.owner,
                        item.field_name,
                        item.from_value_json,
                        item.to_value_json,
                        item.span,
                        item.variant,
                    ): item
                    for item in transitions
                }.values(),
                key=lambda item: (
                    item.owner,
                    item.span.logical_path,
                    item.span.start_line,
                    item.span.start_column,
                    item.field_name,
                    item.from_value_json,
                    item.to_value_json,
                    item.variant or "",
                ),
            )
        ),
        unresolved_owners=tuple(sorted(unresolved_owners)),
        unresolved_observation_count=unresolved_count,
    )


def _apply_discriminator(
    variants: tuple[DictionaryVariantJoinInput, ...],
) -> tuple[DictionaryVariantJoinInput, ...]:
    if len(variants) < 2:
        return variants
    field_names = set.intersection(
        *({field.name for field in variant.fields} for variant in variants)
    )
    for candidate in sorted(field_names):
        values: list[str] = []
        for variant in variants:
            field = next(item for item in variant.fields if item.name == candidate)
            exact_values = [
                operation.literal_value_json
                for operation in field.operations
                if operation.kind
                in {DictionaryOperationKind.LITERAL, DictionaryOperationKind.WRITE}
                and operation.literal_value_json is not None
            ]
            if not exact_values:
                break
            values.append(exact_values[-1])
        if len(values) == len(variants) and len(set(values)) == len(variants):
            return tuple(
                replace(
                    variant,
                    discriminator_field=candidate,
                    discriminator_values_json=(value,),
                )
                for variant, value in zip(variants, values, strict=True)
            )
    return variants


def _state_transitions(
    owner: str,
    variants: tuple[DictionaryVariantJoinInput, ...],
) -> tuple[DictionaryStateTransitionObservation, ...]:
    transitions: list[DictionaryStateTransitionObservation] = []
    for variant in variants:
        current_values: dict[str, str] = {}
        operations = sorted(
            (operation for field in variant.fields for operation in field.operations),
            key=lambda item: (
                item.span.logical_path,
                item.span.start_line,
                item.span.start_column,
                item.kind.value,
            ),
        )
        for operation in operations:
            if operation.key_repr is None:
                continue
            field_name = operation.key_repr
            if operation.kind is DictionaryOperationKind.LITERAL:
                if operation.literal_value_json is not None:
                    current_values[field_name] = operation.literal_value_json
                else:
                    current_values.pop(field_name, None)
                continue
            if operation.kind is DictionaryOperationKind.DELETE:
                current_values.pop(field_name, None)
                continue
            if operation.kind is not DictionaryOperationKind.WRITE:
                continue
            previous = current_values.get(field_name)
            current = operation.literal_value_json
            if current is None:
                current_values.pop(field_name, None)
                continue
            if previous is not None and previous != current:
                transitions.append(
                    DictionaryStateTransitionObservation(
                        owner=owner,
                        field_name=field_name,
                        from_value_json=previous,
                        to_value_json=current,
                        span=operation.span,
                        variant=operation.variant,
                    )
                )
            current_values[field_name] = current
    return tuple(transitions)


def _apply_lifecycle_states(
    variants: tuple[DictionaryVariantJoinInput, ...],
    transitions: tuple[DictionaryStateTransitionObservation, ...],
) -> tuple[DictionaryVariantJoinInput, ...]:
    return tuple(
        replace(
            variant,
            lifecycle_states=tuple(
                sorted(
                    {
                        f"{transition.field_name}={value}"
                        for transition in transitions
                        if transition.variant is None or transition.variant == variant.name
                        for value in (
                            transition.from_value_json,
                            transition.to_value_json,
                        )
                    }
                )
            ),
        )
        for variant in variants
    )


def _build_field(
    name: str,
    operations: list[DictionaryOperationObservation],
    defaults: list[DictionaryFieldDefaultObservation],
) -> DictionaryFieldFact:
    producer_defaults = tuple(
        sorted(
            (
                item.default
                for item in defaults
                if item.role is DictionaryDefaultRole.PRODUCER_PROVIDED
            ),
            key=_default_sort_key,
        )
    )
    consumer_fallbacks = tuple(
        sorted(
            (
                item.default
                for item in defaults
                if item.role is DictionaryDefaultRole.CONSUMER_FALLBACK
            ),
            key=_default_sort_key,
        )
    )
    requiredness = tuple(
        sorted(
            (_requiredness(item) for item in operations),
            key=lambda item: (
                item.span.logical_path,
                item.span.start_line,
                item.span.start_column,
                item.kind.value,
                item.condition or "",
            ),
        )
    )
    return DictionaryFieldFact(
        name=name,
        type_reprs=tuple(
            sorted({item.value_type_repr for item in operations if item.value_type_repr})
        ),
        requiredness_observations=requiredness,
        producer_defaults=producer_defaults,
        consumer_fallbacks=consumer_fallbacks,
        operations=tuple(
            sorted(
                operations,
                key=lambda item: (
                    item.span.logical_path,
                    item.span.start_line,
                    item.span.start_column,
                    item.kind.value,
                ),
            )
        ),
    )


def _requiredness(
    operation: DictionaryOperationObservation,
) -> DictionaryRequirednessObservation:
    if operation.kind in {
        DictionaryOperationKind.GET,
        DictionaryOperationKind.SET_DEFAULT,
    }:
        kind = InternalRequirednessKind.OPTIONAL
        condition = None
    elif operation.kind is DictionaryOperationKind.MEMBERSHIP:
        kind = InternalRequirednessKind.CONDITIONAL
        condition = "membership check"
    elif operation.variant is not None and operation.kind is DictionaryOperationKind.READ:
        kind = InternalRequirednessKind.CONDITIONAL
        condition = operation.variant
    elif operation.kind in {
        DictionaryOperationKind.LITERAL,
        DictionaryOperationKind.READ,
        DictionaryOperationKind.WRITE,
    }:
        kind = InternalRequirednessKind.ALWAYS
        condition = None
    else:
        kind = InternalRequirednessKind.UNKNOWN
        condition = None
    return DictionaryRequirednessObservation(
        kind=kind,
        span=operation.span,
        condition=condition,
    )


def _default_sort_key(item: DictionaryDefaultObservation) -> tuple[object, ...]:
    return (
        item.span.logical_path,
        item.span.start_line,
        item.span.start_column,
        item.kind.value,
        item.literal_value_json or "",
        item.detail or "",
    )


def _is_producer(kind: DictionaryOperationKind) -> bool:
    return kind in {
        DictionaryOperationKind.LITERAL,
        DictionaryOperationKind.FACTORY,
        DictionaryOperationKind.COMPREHENSION,
        DictionaryOperationKind.WRITE,
        DictionaryOperationKind.UPDATE,
        DictionaryOperationKind.SET_DEFAULT,
        DictionaryOperationKind.MERGE,
        DictionaryOperationKind.UNPACK,
    }


def _is_variant_defining(kind: DictionaryOperationKind) -> bool:
    return kind in {
        DictionaryOperationKind.LITERAL,
        DictionaryOperationKind.WRITE,
        DictionaryOperationKind.SET_DEFAULT,
    }


def _is_consumer(kind: DictionaryOperationKind) -> bool:
    return kind in {
        DictionaryOperationKind.READ,
        DictionaryOperationKind.DELETE,
        DictionaryOperationKind.GET,
        DictionaryOperationKind.MEMBERSHIP,
        DictionaryOperationKind.TRUTHINESS,
        DictionaryOperationKind.COERCION,
        DictionaryOperationKind.POP,
    }
