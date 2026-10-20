"""Immutable internal values for future dictionary analysis.

This module defines data ownership only. It does not traverse ASTs, infer shapes,
or resolve flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DictionaryOperationKind(StrEnum):
    LITERAL = "literal"
    FACTORY = "factory"
    COMPREHENSION = "comprehension"
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    GET = "get"
    MEMBERSHIP = "membership"
    TRUTHINESS = "truthiness"
    COERCION = "coercion"
    UPDATE = "update"
    CLEAR = "clear"
    POP = "pop"
    SET_DEFAULT = "setdefault"
    MERGE = "merge"
    UNPACK = "unpack"
    ARGUMENT = "argument"
    RETURN = "return"
    VALIDATION = "validation"


class InternalConversionKind(StrEnum):
    CONVERSION = "conversion"
    SERIALIZATION = "serialization"


class DictionaryKeyKind(StrEnum):
    LITERAL_STRING = "literal-string"
    LITERAL_NON_STRING = "literal-non-string"
    DYNAMIC = "dynamic"
    UNRESOLVED = "unresolved"


class MutationTiming(StrEnum):
    NOT_MUTATION = "not-mutation"
    BEFORE_VALIDATION = "before-validation"
    AFTER_VALIDATION = "after-validation"
    UNKNOWN = "unknown"


class DictionaryPredicate(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    TRUTHY = "truthy"
    FALSY = "falsy"


class InternalDefaultKind(StrEnum):
    ABSENT = "absent"
    EXPLICIT_NULL = "explicit-null"
    JSON_LITERAL = "json-literal"
    FACTORY = "factory"
    EXPRESSION = "expression"
    UNRESOLVED = "unresolved"


class InternalRequirednessKind(StrEnum):
    ALWAYS = "always"
    OPTIONAL = "optional"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


class DictionaryDefaultRole(StrEnum):
    PRODUCER_PROVIDED = "producer-provided"
    CONSUMER_FALLBACK = "consumer-fallback"


class InternalLockKind(StrEnum):
    THREAD = "thread-lock"
    PROCESS = "process-lock"
    ASYNC_TASK = "async-task-lock"


class InternalStructuralValidationKind(StrEnum):
    VALUE_TYPE = "dictionary-value-type"
    EXACT_KEYS = "dictionary-exact-keys"
    VALUE_LITERAL = "dictionary-value-literal"


class InternalLiteralValidationRelation(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "not-equals"
    IN = "in"
    NOT_IN = "not-in"


@dataclass(frozen=True, slots=True)
class InternalSourceSpan:
    logical_path: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    def __post_init__(self) -> None:
        if not self.logical_path or self.logical_path.startswith("/"):
            raise ValueError("internal span requires a nonempty logical path")
        values = (
            self.start_line,
            self.start_column,
            self.end_line,
            self.end_column,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("internal span coordinates must be exact integers")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("internal span line range is invalid")
        if self.start_column < 0 or self.end_column < 0:
            raise ValueError("internal span columns must be nonnegative")


@dataclass(frozen=True, slots=True)
class DictionaryOperationObservation:
    owner: str
    kind: DictionaryOperationKind
    span: InternalSourceSpan
    variant: str | None = None
    key_kind: DictionaryKeyKind = DictionaryKeyKind.UNRESOLVED
    key_repr: str | None = None
    key_type_repr: str | None = None
    value_type_repr: str | None = None
    literal_value_json: str | None = None
    coercion: str | None = None
    predicate: DictionaryPredicate | None = None
    mutation_timing: MutationTiming = MutationTiming.NOT_MUTATION
    fail_open: bool = False

    def __post_init__(self) -> None:
        if not self.owner:
            raise ValueError("dictionary operation owner must be nonempty")
        if not isinstance(self.fail_open, bool):
            raise TypeError("dictionary operation fail-open state must be boolean")
        if self.variant == "":
            raise ValueError("dictionary operation variant cannot be empty")
        if self.key_repr == "" and self.key_kind is not DictionaryKeyKind.LITERAL_STRING:
            raise ValueError("dictionary operation key representation cannot be empty")
        if (
            self.key_kind
            in {
                DictionaryKeyKind.LITERAL_STRING,
                DictionaryKeyKind.LITERAL_NON_STRING,
                DictionaryKeyKind.DYNAMIC,
            }
            and self.key_repr is None
        ):
            raise ValueError(f"{self.key_kind.value} key requires a representation")
        if self.key_kind is DictionaryKeyKind.LITERAL_NON_STRING and not self.key_type_repr:
            raise ValueError("literal non-string key requires a type representation")
        if self.kind is DictionaryOperationKind.COERCION:
            if not self.coercion:
                raise ValueError("coercion operation requires coercion detail")
        elif self.coercion is not None:
            raise ValueError("only coercion operations may contain coercion detail")
        predicate_operations = {
            DictionaryOperationKind.MEMBERSHIP,
            DictionaryOperationKind.TRUTHINESS,
            DictionaryOperationKind.VALIDATION,
        }
        if self.kind in predicate_operations and self.predicate is None:
            raise ValueError(f"{self.kind.value} operation requires a predicate")
        if self.kind not in predicate_operations and self.predicate is not None:
            raise ValueError("only predicate operations may contain a predicate")
        mutation_kinds = {
            DictionaryOperationKind.WRITE,
            DictionaryOperationKind.DELETE,
            DictionaryOperationKind.UPDATE,
            DictionaryOperationKind.CLEAR,
            DictionaryOperationKind.POP,
            DictionaryOperationKind.SET_DEFAULT,
            DictionaryOperationKind.MERGE,
        }
        if self.kind in mutation_kinds and self.mutation_timing is MutationTiming.NOT_MUTATION:
            raise ValueError("dictionary mutation requires mutation timing")
        if (
            self.kind not in mutation_kinds
            and self.mutation_timing is not MutationTiming.NOT_MUTATION
        ):
            raise ValueError("non-mutation operation requires not-mutation timing")


@dataclass(frozen=True, slots=True)
class DictionaryMutationGuardObservation:
    owner: str
    guard_identity: str
    kind: InternalLockKind
    span: InternalSourceSpan

    def __post_init__(self) -> None:
        if not self.owner or not self.guard_identity:
            raise ValueError("dictionary mutation guard identities must be nonempty")


@dataclass(frozen=True, slots=True)
class DictionaryStructuralValidationObservation:
    owner: str
    kind: InternalStructuralValidationKind
    span: InternalSourceSpan
    key_kind: DictionaryKeyKind = DictionaryKeyKind.UNRESOLVED
    key_repr: str | None = None
    expected_type_repr: str | None = None
    expected_keys: tuple[str, ...] = ()
    expected_literals_json: tuple[str, ...] = ()
    literal_relation: InternalLiteralValidationRelation | None = None
    negated: bool = False
    fail_open: bool = False

    def __post_init__(self) -> None:
        if not self.owner:
            raise ValueError("structural validation owner must be nonempty")
        if not isinstance(self.fail_open, bool):
            raise TypeError("structural validation fail-open state must be boolean")
        if self.kind is InternalStructuralValidationKind.VALUE_TYPE:
            if (
                self.key_repr is None
                or not self.expected_type_repr
                or self.expected_keys
                or self.expected_literals_json
                or self.literal_relation is not None
            ):
                raise ValueError("value-type validation requires only key and type evidence")
        elif self.kind is InternalStructuralValidationKind.EXACT_KEYS:
            if (
                not self.expected_keys
                or self.key_repr is not None
                or self.expected_type_repr
                or self.expected_literals_json
                or self.literal_relation is not None
            ):
                raise ValueError("exact-key validation requires only expected keys")
            if tuple(sorted(set(self.expected_keys))) != self.expected_keys:
                raise ValueError("exact-key validation keys must be unique and sorted")
        elif (
            self.key_repr is None
            or not self.expected_literals_json
            or self.literal_relation is None
            or self.expected_type_repr
            or self.expected_keys
            or self.negated
        ):
            raise ValueError("literal-value validation requires only key, literals, and relation")
        elif tuple(sorted(set(self.expected_literals_json))) != self.expected_literals_json:
            raise ValueError("literal-value validation literals must be unique and sorted")


@dataclass(frozen=True, slots=True)
class DictionaryMutationLockOrderObservation:
    owner: str
    guard_identities: tuple[str, ...]
    span: InternalSourceSpan

    def __post_init__(self) -> None:
        if not self.owner:
            raise ValueError("dictionary lock-order owner must be nonempty")
        if len(self.guard_identities) < 2 or any(not item for item in self.guard_identities):
            raise ValueError("dictionary lock order requires at least two guard identities")


@dataclass(frozen=True, slots=True)
class DictionaryConversionObservation:
    owner: str
    target_identity: str
    kind: InternalConversionKind
    span: InternalSourceSpan
    serialization_format: str | None = None

    def __post_init__(self) -> None:
        if not self.owner or not self.target_identity:
            raise ValueError("dictionary conversion identities must be nonempty")
        if self.kind is InternalConversionKind.SERIALIZATION:
            if not self.serialization_format:
                raise ValueError("serialization conversion requires a format")
        elif self.serialization_format is not None:
            raise ValueError("only serialization conversions may define a format")


@dataclass(frozen=True, slots=True)
class DictionaryDefaultObservation:
    kind: InternalDefaultKind
    span: InternalSourceSpan
    literal_value_json: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.kind is InternalDefaultKind.JSON_LITERAL:
            if self.literal_value_json is None or self.detail is not None:
                raise ValueError("JSON-literal default requires only literal_value_json")
            if self.literal_value_json == "null":
                raise ValueError("null default must use explicit-null kind")
        elif self.kind in {
            InternalDefaultKind.FACTORY,
            InternalDefaultKind.EXPRESSION,
            InternalDefaultKind.UNRESOLVED,
        }:
            if not self.detail or self.literal_value_json is not None:
                raise ValueError(f"{self.kind.value} default requires only detail")
        elif self.literal_value_json is not None or self.detail is not None:
            raise ValueError(f"{self.kind.value} default cannot contain a payload")


@dataclass(frozen=True, slots=True)
class DictionaryFieldDefaultObservation:
    owner: str
    role: DictionaryDefaultRole
    key_kind: DictionaryKeyKind
    key_repr: str
    span: InternalSourceSpan
    default: DictionaryDefaultObservation
    variant: str | None = None

    def __post_init__(self) -> None:
        if not self.owner:
            raise ValueError("dictionary field default owner must be nonempty")
        if self.variant == "":
            raise ValueError("dictionary field default variant cannot be empty")
        if not isinstance(self.key_repr, str) or (
            not self.key_repr and self.key_kind is not DictionaryKeyKind.LITERAL_STRING
        ):
            raise ValueError("dictionary field default key must be nonempty")
        if self.key_kind is DictionaryKeyKind.UNRESOLVED:
            raise ValueError("dictionary field default requires resolved key evidence")


@dataclass(frozen=True, slots=True)
class DictionaryAliasObservation:
    source_owner: str
    alias_identity: str
    span: InternalSourceSpan
    variant: str | None = None

    def __post_init__(self) -> None:
        if not self.source_owner or not self.alias_identity:
            raise ValueError("dictionary alias identities must be nonempty")
        if self.source_owner == self.alias_identity:
            raise ValueError("dictionary alias must differ from its source owner")
        if self.variant == "":
            raise ValueError("dictionary alias variant cannot be empty")


@dataclass(frozen=True, slots=True)
class DictionaryStateTransitionObservation:
    owner: str
    field_name: str
    from_value_json: str
    to_value_json: str
    span: InternalSourceSpan
    variant: str | None = None

    def __post_init__(self) -> None:
        if not self.owner or not self.field_name:
            raise ValueError("dictionary state transition identities must be nonempty")
        if not self.from_value_json or not self.to_value_json:
            raise ValueError("dictionary state transition values must be nonempty")
        if self.from_value_json == self.to_value_json:
            raise ValueError("dictionary state transition values must differ")
        if self.variant == "":
            raise ValueError("dictionary state transition variant cannot be empty")


@dataclass(frozen=True, slots=True)
class DictionaryRequirednessObservation:
    kind: InternalRequirednessKind
    span: InternalSourceSpan
    condition: str | None = None

    def __post_init__(self) -> None:
        if self.kind is InternalRequirednessKind.CONDITIONAL:
            if not self.condition:
                raise ValueError("conditional requiredness requires a condition")
        elif self.condition is not None:
            raise ValueError("only conditional requiredness may contain a condition")


@dataclass(frozen=True, slots=True)
class DictionaryFieldFact:
    name: str
    type_reprs: tuple[str, ...]
    requiredness_observations: tuple[DictionaryRequirednessObservation, ...]
    producer_defaults: tuple[DictionaryDefaultObservation, ...] = ()
    consumer_fallbacks: tuple[DictionaryDefaultObservation, ...] = ()
    operations: tuple[DictionaryOperationObservation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError("dictionary field name must be a string")
        for label, values in (("type representations", self.type_reprs),):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"dictionary field {label} must be unique and sorted")


@dataclass(frozen=True, slots=True)
class DictionaryVariantJoinInput:
    name: str
    fields: tuple[DictionaryFieldFact, ...]
    discriminator_field: str | None = None
    discriminator_values_json: tuple[str, ...] = ()
    lifecycle_states: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("dictionary variant name must be nonempty")
        field_names = tuple(field.name for field in self.fields)
        if list(field_names) != sorted(field_names) or len(field_names) != len(set(field_names)):
            raise ValueError("dictionary variant fields must be unique and sorted")
        if self.discriminator_field is not None and self.discriminator_field not in field_names:
            raise ValueError("dictionary discriminator must reference a variant field")
        if self.discriminator_values_json and self.discriminator_field is None:
            raise ValueError("dictionary discriminator values require a discriminator field")
        for label, values in (
            ("discriminator values", self.discriminator_values_json),
            ("lifecycle states", self.lifecycle_states),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"dictionary variant {label} must be unique and sorted")


@dataclass(frozen=True, slots=True)
class DictionaryShapeJoinInput:
    owner: str
    variants: tuple[DictionaryVariantJoinInput, ...]
    producers: tuple[str, ...] = ()
    consumers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.owner:
            raise ValueError("dictionary shape owner must be nonempty")
        variant_names = tuple(variant.name for variant in self.variants)
        if not variant_names:
            raise ValueError("dictionary shape requires at least one variant")
        if list(variant_names) != sorted(variant_names) or len(variant_names) != len(
            set(variant_names)
        ):
            raise ValueError("dictionary shape variants must be unique and sorted")
        for label, values in (
            ("producers", self.producers),
            ("consumers", self.consumers),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"dictionary shape {label} must be unique and sorted")
