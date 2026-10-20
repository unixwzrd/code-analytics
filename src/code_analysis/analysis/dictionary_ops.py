"""Assembly façades for immutable dictionary analysis facts."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from code_analysis.analysis.dictionary_traversal import (
    DictionaryFactAccumulator,
    traverse_dictionary_facts,
)
from code_analysis.analysis.dictionary_values import (
    DictionaryAliasObservation,
    DictionaryConversionObservation,
    DictionaryFieldDefaultObservation,
    DictionaryMutationGuardObservation,
    DictionaryMutationLockOrderObservation,
    DictionaryOperationObservation,
    DictionaryStructuralValidationObservation,
)


@dataclass(frozen=True, slots=True)
class DictionaryAnalysisFacts:
    owners: tuple[str, ...]
    aliases: tuple[DictionaryAliasObservation, ...]
    operations: tuple[DictionaryOperationObservation, ...]
    defaults: tuple[DictionaryFieldDefaultObservation, ...]
    conversions: tuple[DictionaryConversionObservation, ...]
    mutation_guards: tuple[DictionaryMutationGuardObservation, ...]
    lock_orders: tuple[DictionaryMutationLockOrderObservation, ...]
    structural_validations: tuple[DictionaryStructuralValidationObservation, ...] = ()


def discover_dictionary_operations(
    source: str, logical_path: str, module: str
) -> tuple[DictionaryOperationObservation, ...]:
    return discover_dictionary_facts(source, logical_path, module).operations


def discover_dictionary_defaults(
    source: str, logical_path: str, module: str
) -> tuple[DictionaryFieldDefaultObservation, ...]:
    return discover_dictionary_facts(source, logical_path, module).defaults


def discover_dictionary_facts(
    source: str, logical_path: str, module: str
) -> DictionaryAnalysisFacts:
    facts = traverse_dictionary_facts(ast.parse(source), logical_path, module)
    return _assemble_dictionary_facts(facts)


def _assemble_dictionary_facts(
    facts: DictionaryFactAccumulator,
) -> DictionaryAnalysisFacts:
    return DictionaryAnalysisFacts(
        owners=tuple(sorted(facts.owners)),
        aliases=tuple(sorted(facts.aliases, key=_alias_sort_key)),
        operations=tuple(sorted(facts.operations, key=_operation_sort_key)),
        defaults=tuple(sorted(facts.defaults, key=_default_sort_key)),
        conversions=tuple(sorted(facts.conversions, key=_conversion_sort_key)),
        mutation_guards=tuple(sorted(set(facts.mutation_guards), key=_guard_sort_key)),
        lock_orders=tuple(sorted(set(facts.lock_orders), key=_lock_order_sort_key)),
        structural_validations=tuple(
            sorted(set(facts.structural_validations), key=_structural_validation_sort_key)
        ),
    )


def _structural_validation_sort_key(
    item: DictionaryStructuralValidationObservation,
) -> tuple[object, ...]:
    return (
        item.owner,
        item.kind.value,
        item.span.logical_path,
        item.span.start_line,
        item.span.start_column,
        item.key_repr or "",
        item.expected_type_repr or "",
        item.expected_keys,
        item.expected_literals_json,
        item.literal_relation.value if item.literal_relation is not None else "",
        item.negated,
        item.fail_open,
    )


def _operation_sort_key(item: DictionaryOperationObservation) -> tuple[object, ...]:
    return (
        item.owner,
        item.span.start_line,
        item.span.start_column,
        item.variant or "",
        item.kind.value,
        item.key_kind.value,
        item.key_repr or "",
        item.fail_open,
    )


def _alias_sort_key(item: DictionaryAliasObservation) -> tuple[object, ...]:
    return (
        item.source_owner,
        item.alias_identity,
        item.span.logical_path,
        item.span.start_line,
        item.span.start_column,
        item.variant or "",
    )


def _default_sort_key(item: DictionaryFieldDefaultObservation) -> tuple[object, ...]:
    return (
        item.owner,
        item.span.start_line,
        item.span.start_column,
        item.variant or "",
        item.role.value,
        item.key_kind.value,
        item.key_repr,
    )


def _conversion_sort_key(item: DictionaryConversionObservation) -> tuple[object, ...]:
    return (
        item.owner,
        item.span.logical_path,
        item.span.start_line,
        item.span.start_column,
        item.kind.value,
        item.target_identity,
    )


def _guard_sort_key(item: DictionaryMutationGuardObservation) -> tuple[object, ...]:
    return (
        item.owner,
        item.span.logical_path,
        item.span.start_line,
        item.span.start_column,
        item.kind.value,
        item.guard_identity,
    )


def _lock_order_sort_key(item: DictionaryMutationLockOrderObservation) -> tuple[object, ...]:
    return (
        item.owner,
        item.span.logical_path,
        item.span.start_line,
        item.span.start_column,
        item.guard_identities,
    )
