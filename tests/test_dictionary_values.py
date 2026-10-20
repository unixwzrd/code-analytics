from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from code_analysis.analysis.dictionary_ops import (
    discover_dictionary_defaults,
    discover_dictionary_facts,
    discover_dictionary_operations,
)
from code_analysis.analysis.dictionary_values import (
    DictionaryConversionObservation,
    DictionaryDefaultObservation,
    DictionaryDefaultRole,
    DictionaryFieldFact,
    DictionaryKeyKind,
    DictionaryMutationGuardObservation,
    DictionaryMutationLockOrderObservation,
    DictionaryOperationKind,
    DictionaryOperationObservation,
    DictionaryRequirednessObservation,
    DictionaryShapeJoinInput,
    DictionaryStateTransitionObservation,
    DictionaryVariantJoinInput,
    InternalConversionKind,
    InternalDefaultKind,
    InternalLockKind,
    InternalRequirednessKind,
    InternalSourceSpan,
    MutationTiming,
)


def _span() -> InternalSourceSpan:
    return InternalSourceSpan(
        logical_path="package/module.py",
        start_line=1,
        start_column=0,
        end_line=1,
        end_column=12,
    )


def test_internal_dictionary_values_are_frozen_dataclasses() -> None:
    operation = DictionaryOperationObservation(
        owner="package.module:payload",
        kind=DictionaryOperationKind.LITERAL,
        span=_span(),
        key_kind=DictionaryKeyKind.LITERAL_STRING,
        key_repr="enabled",
        value_type_repr="bool",
        literal_value_json="false",
    )
    field = DictionaryFieldFact(
        name="enabled",
        type_reprs=("bool",),
        requiredness_observations=(
            DictionaryRequirednessObservation(
                kind=InternalRequirednessKind.ALWAYS,
                span=_span(),
            ),
        ),
        producer_defaults=(
            DictionaryDefaultObservation(
                kind=InternalDefaultKind.JSON_LITERAL,
                span=_span(),
                literal_value_json="false",
            ),
        ),
        operations=(operation,),
    )
    variant = DictionaryVariantJoinInput(name="default", fields=(field,))
    shape = DictionaryShapeJoinInput(
        owner="package.module:payload",
        variants=(variant,),
        producers=("package.module:build",),
        consumers=("package.module:consume",),
    )
    assert shape.variants[0].fields[0].operations == (operation,)
    with pytest.raises(FrozenInstanceError):
        shape.owner = "changed"

    conversion = DictionaryConversionObservation(
        owner="package.module:payload",
        target_identity="json.dumps",
        kind=InternalConversionKind.SERIALIZATION,
        serialization_format="json",
        span=_span(),
    )
    with pytest.raises(FrozenInstanceError):
        conversion.owner = "changed"

    guard = DictionaryMutationGuardObservation(
        owner="package.module:<module>:registry",
        guard_identity="package.module:<module>:registry_lock",
        kind=InternalLockKind.THREAD,
        span=_span(),
    )
    with pytest.raises(FrozenInstanceError):
        guard.guard_identity = "changed"

    order = DictionaryMutationLockOrderObservation(
        owner="package.module:<module>:registry",
        guard_identities=("package.module:<module>:outer", "package.module:<module>:inner"),
        span=_span(),
    )
    with pytest.raises(FrozenInstanceError):
        order.owner = "changed"
    with pytest.raises(ValueError, match="at least two"):
        DictionaryMutationLockOrderObservation(
            owner="package.module:<module>:registry",
            guard_identities=("package.module:<module>:outer",),
            span=_span(),
        )


def test_nested_lock_order_is_exact_lexical_evidence() -> None:
    facts = discover_dictionary_facts(
        "import threading\n"
        "outer = threading.Lock()\n"
        "inner = threading.RLock()\n"
        "registry: dict[str, int] = {}\n"
        "with outer:\n"
        "    with inner:\n"
        "        registry['first'] = 1\n",
        "package/state.py",
        "package.state",
    )
    assert facts.lock_orders == (
        DictionaryMutationLockOrderObservation(
            owner="package.state:<module>:registry",
            guard_identities=(
                "package.state:<module>:outer",
                "package.state:<module>:inner",
            ),
            span=InternalSourceSpan(
                logical_path="package/state.py",
                start_line=7,
                start_column=8,
                end_line=7,
                end_column=25,
            ),
        ),
    )


def test_dictionary_mutation_guards_require_recognized_bound_locks() -> None:
    facts = discover_dictionary_facts(
        "from threading import RLock as Guard\n"
        "import asyncio\n"
        "registry_lock = Guard()\n"
        "async_lock = asyncio.Lock()\n"
        "registry: dict[str, int] = {}\n"
        "with registry_lock:\n"
        "    registry['thread'] = 1\n"
        "async def update():\n"
        "    async with async_lock:\n"
        "        registry.update({'async': 2})\n",
        "package/state.py",
        "package.state",
    )
    assert [item.kind for item in facts.mutation_guards] == [
        InternalLockKind.THREAD,
        InternalLockKind.ASYNC_TASK,
    ]
    assert [item.guard_identity for item in facts.mutation_guards] == [
        "package.state:<module>:registry_lock",
        "package.state:<module>:async_lock",
    ]

    shadowed = discover_dictionary_facts(
        "from threading import Lock\n"
        "def update(Lock):\n"
        "    guard = Lock()\n"
        "    payload = {}\n"
        "    with guard:\n"
        "        payload['value'] = 1\n",
        "package/shadow.py",
        "package.shadow",
    )
    assert shadowed.mutation_guards == ()

    deferred_body = discover_dictionary_facts(
        "from threading import Lock\n"
        "guard = Lock()\n"
        "with guard:\n"
        "    def update():\n"
        "        payload = {}\n"
        "        payload['value'] = 1\n",
        "package/deferred.py",
        "package.deferred",
    )
    assert deferred_body.mutation_guards == ()


def test_instance_dictionary_mutations_link_to_exact_instance_lock() -> None:
    facts = discover_dictionary_facts(
        "import threading\n"
        "class Registry:\n"
        "    def __init__(this):\n"
        "        this._entries: dict[str, int] = {}\n"
        "        this._lock = threading.RLock()\n"
        "    def add(this, key: str):\n"
        "        with this._lock:\n"
        "            this._entries[key] = 1\n",
        "package/registry.py",
        "package.registry",
    )
    assert "package.registry:Registry:self._entries" in facts.owners
    assert facts.mutation_guards == (
        DictionaryMutationGuardObservation(
            owner="package.registry:Registry:self._entries",
            guard_identity="package.registry:Registry:self._lock",
            kind=InternalLockKind.THREAD,
            span=InternalSourceSpan(
                logical_path="package/registry.py",
                start_line=8,
                start_column=12,
                end_line=8,
                end_column=30,
            ),
        ),
    )


def test_dataclass_default_factories_define_instance_mapping_and_lock() -> None:
    facts = discover_dictionary_facts(
        "from dataclasses import dataclass, field as dc_field\n"
        "import threading\n"
        "@dataclass(slots=True)\n"
        "class Registry:\n"
        "    entries: dict[str, int] = dc_field(default_factory=dict)\n"
        "    lock: threading.Lock = dc_field(default_factory=threading.Lock)\n"
        "    def add(self, key: str):\n"
        "        with self.lock:\n"
        "            self.entries[key] = 1\n",
        "package/dataclass_registry.py",
        "package.dataclass_registry",
    )
    owner = "package.dataclass_registry:Registry:self.entries"
    assert facts.owners == (owner,)
    factory = next(
        item for item in facts.operations if item.kind is DictionaryOperationKind.FACTORY
    )
    assert factory.owner == owner
    assert factory.value_type_repr == "dict"
    assert facts.mutation_guards[0].guard_identity == (
        "package.dataclass_registry:Registry:self.lock"
    )


def test_non_dataclass_and_shadowed_factories_are_not_instance_evidence() -> None:
    non_dataclass = discover_dictionary_facts(
        "from pydantic import Field\n"
        "class Model:\n"
        "    values: dict[str, int] = Field(default_factory=dict)\n",
        "package/model.py",
        "package.model",
    )
    assert not any(":self." in owner for owner in non_dataclass.owners)
    assert all(
        item.kind is not DictionaryOperationKind.FACTORY for item in non_dataclass.operations
    )

    shadowed = discover_dictionary_facts(
        "def dataclass(value):\n"
        "    return value\n"
        "def field(**kwargs):\n"
        "    return kwargs\n"
        "@dataclass\n"
        "class Record:\n"
        "    values: dict[str, int] = field(default_factory=dict)\n",
        "package/shadowed.py",
        "package.shadowed",
    )
    assert not any(":self." in owner for owner in shadowed.owners)
    assert all(item.kind is not DictionaryOperationKind.FACTORY for item in shadowed.operations)


def test_classmethod_receiver_links_class_mapping_and_lock() -> None:
    facts = discover_dictionary_facts(
        "import threading\n"
        "class Registry:\n"
        "    @classmethod\n"
        "    def add(cls):\n"
        "        with cls.lock:\n"
        "            cls.entries['first'] = 1\n"
        "    entries: dict[str, int] = {}\n"
        "    lock = threading.Lock()\n",
        "package/class_registry.py",
        "package.class_registry",
    )
    owner = "package.class_registry:Registry:<class>.entries"
    writes = [
        item
        for item in facts.operations
        if item.owner == owner and item.kind is DictionaryOperationKind.WRITE
    ]
    assert len(writes) == 1
    assert facts.mutation_guards[0].guard_identity == (
        "package.class_registry:Registry:<class>.lock"
    )


def test_method_bare_name_does_not_inherit_class_namespace() -> None:
    facts = discover_dictionary_facts(
        "class Registry:\n"
        "    entries: dict[str, int] = {}\n"
        "    def invalid(self):\n"
        "        entries['first'] = 1\n",
        "package/class_registry.py",
        "package.class_registry",
    )
    owner = "package.class_registry:Registry:<class>.entries"
    assert not any(
        item.owner == owner and item.kind is DictionaryOperationKind.WRITE
        for item in facts.operations
    )


def test_direct_instance_mapping_and_lock_aliases_retain_canonical_owners() -> None:
    facts = discover_dictionary_facts(
        "import threading\n"
        "class Registry:\n"
        "    def __init__(self):\n"
        "        self.entries = {}\n"
        "        self.lock = threading.Lock()\n"
        "    def add(self):\n"
        "        entries = self.entries\n"
        "        guard: object = self.lock\n"
        "        with guard:\n"
        "            entries['first'] = 1\n",
        "package/instance_registry.py",
        "package.instance_registry",
    )
    owner = "package.instance_registry:Registry:self.entries"
    writes = [
        item
        for item in facts.operations
        if item.owner == owner and item.kind is DictionaryOperationKind.WRITE
    ]
    assert len(writes) == 1
    assert [(item.source_owner, item.alias_identity) for item in facts.aliases] == [
        (owner, "package.instance_registry:Registry.add:entries")
    ]
    assert facts.mutation_guards[0].guard_identity == (
        "package.instance_registry:Registry:self.lock"
    )


def test_reassignment_stops_direct_lock_alias_flow() -> None:
    facts = discover_dictionary_facts(
        "import threading\n"
        "def update(payload: dict[str, int]):\n"
        "    lock = threading.Lock()\n"
        "    guard = lock\n"
        "    guard = object()\n"
        "    with guard:\n"
        "        payload['first'] = 1\n",
        "package/lock_alias.py",
        "package.lock_alias",
    )
    assert facts.mutation_guards == ()


def test_lock_alias_if_join_requires_same_canonical_guard_in_both_branches() -> None:
    joined = discover_dictionary_facts(
        "import threading\n"
        "def update(payload: dict[str, int], flag):\n"
        "    lock = threading.Lock()\n"
        "    if flag:\n"
        "        guard = lock\n"
        "    else:\n"
        "        guard: object = lock\n"
        "    with guard:\n"
        "        payload['first'] = 1\n",
        "package/joined_lock.py",
        "package.joined_lock",
    )
    partial = discover_dictionary_facts(
        "import threading\n"
        "def update(payload: dict[str, int], flag):\n"
        "    lock = threading.Lock()\n"
        "    if flag:\n"
        "        guard = lock\n"
        "    with guard:\n"
        "        payload['first'] = 1\n",
        "package/partial_lock.py",
        "package.partial_lock",
    )
    assert joined.mutation_guards[0].guard_identity == "package.joined_lock:update:lock"
    assert partial.mutation_guards == ()


def test_dictionary_conversion_extraction_is_exact_and_shadow_aware() -> None:
    facts = discover_dictionary_facts(
        "import json as codec\n"
        "from json import dumps\n"
        "def emit(payload: dict[str, object]):\n"
        "    dict(payload)\n"
        "    codec.dumps(payload)\n"
        "    dumps(payload)\n"
        "def shadow(payload: dict[str, object], json):\n"
        "    json.dumps(payload)\n",
        "package/emit.py",
        "package.emit",
    )
    assert [item.kind for item in facts.conversions] == [
        InternalConversionKind.CONVERSION,
        InternalConversionKind.SERIALIZATION,
        InternalConversionKind.SERIALIZATION,
    ]
    assert [item.target_identity for item in facts.conversions] == [
        "builtins.dict",
        "json.dumps",
        "json.dumps",
    ]
    assert facts.conversions[1].serialization_format == "json"


def test_internal_dictionary_values_reject_unstable_or_coercive_values() -> None:
    with pytest.raises(TypeError):
        InternalSourceSpan(
            logical_path="package/module.py",
            start_line=True,
            start_column=0,
            end_line=1,
            end_column=0,
        )
    with pytest.raises(ValueError, match="requires mutation timing"):
        DictionaryOperationObservation(
            owner="package.module:payload",
            kind=DictionaryOperationKind.WRITE,
            span=_span(),
            key_kind=DictionaryKeyKind.LITERAL_STRING,
            key_repr="enabled",
            mutation_timing=MutationTiming.NOT_MUTATION,
        )
    with pytest.raises(ValueError, match="unique and sorted"):
        DictionaryVariantJoinInput(
            name="bad-order",
            fields=(
                DictionaryFieldFact("z", (), ()),
                DictionaryFieldFact("a", (), ()),
            ),
        )
    with pytest.raises(ValueError, match="must differ"):
        DictionaryStateTransitionObservation(
            owner="package.module:payload",
            field_name="state",
            from_value_json='"queued"',
            to_value_json='"queued"',
            span=_span(),
        )


def test_internal_dictionary_values_keep_variants_defaults_and_validation_timing() -> None:
    field = DictionaryFieldFact(
        name="kind",
        type_reprs=("str",),
        requiredness_observations=(
            DictionaryRequirednessObservation(
                kind=InternalRequirednessKind.CONDITIONAL,
                condition="state == 'queued'",
                span=_span(),
            ),
        ),
        consumer_fallbacks=(
            DictionaryDefaultObservation(
                kind=InternalDefaultKind.FACTORY,
                detail="package.module:default_kind",
                span=_span(),
            ),
        ),
        operations=(
            DictionaryOperationObservation(
                owner="package.module:payload",
                kind=DictionaryOperationKind.WRITE,
                span=_span(),
                key_kind=DictionaryKeyKind.DYNAMIC,
                key_repr="field_name",
                mutation_timing=MutationTiming.AFTER_VALIDATION,
            ),
        ),
    )
    shape = DictionaryShapeJoinInput(
        owner="package.module:payload",
        variants=(
            DictionaryVariantJoinInput(
                name="queued",
                fields=(field,),
                discriminator_field="kind",
                discriminator_values_json=('"queued"',),
                lifecycle_states=("queued",),
            ),
        ),
    )
    assert (
        shape.variants[0].fields[0].operations[0].mutation_timing is MutationTiming.AFTER_VALIDATION
    )


def test_dictionary_literal_extraction_distinguishes_key_evidence() -> None:
    observations = discover_dictionary_operations(
        "def build(field, extra):\n"
        "    return {'name': 'job', 7: False, field: value, **extra}\n",
        "package/builders.py",
        "package.builders",
    )
    assert [item.kind for item in observations] == [
        DictionaryOperationKind.LITERAL,
        DictionaryOperationKind.LITERAL,
        DictionaryOperationKind.LITERAL,
        DictionaryOperationKind.UNPACK,
    ]
    assert [item.key_kind for item in observations] == [
        DictionaryKeyKind.LITERAL_STRING,
        DictionaryKeyKind.LITERAL_NON_STRING,
        DictionaryKeyKind.DYNAMIC,
        DictionaryKeyKind.UNRESOLVED,
    ]
    assert observations[0].key_repr == "name"
    assert observations[0].literal_value_json == '"job"'
    assert observations[1].key_repr == "7"
    assert observations[1].key_type_repr == "int"
    assert observations[1].literal_value_json == "false"
    assert observations[2].key_repr == "field"
    assert all(item.owner == "package.builders:build:dict@2:11" for item in observations)


def test_dictionary_comprehension_and_empty_literal_are_explicit() -> None:
    observations = discover_dictionary_operations(
        "empty = {}\nvalues = {item.key: item.value for item in source}\n",
        "package/data.py",
        "package.data",
    )
    assert len(observations) == 2
    assert observations[0].kind is DictionaryOperationKind.LITERAL
    assert observations[0].key_kind is DictionaryKeyKind.UNRESOLVED
    assert observations[1].kind is DictionaryOperationKind.COMPREHENSION
    assert observations[1].key_kind is DictionaryKeyKind.DYNAMIC
    assert observations[1].key_repr == "item.key"
    assert all(item.span.logical_path == "package/data.py" for item in observations)


def test_dictionary_access_and_mutation_operations_require_lexical_evidence() -> None:
    observations = discover_dictionary_operations(
        "def process(payload: dict[str, object], field: str, other):\n"
        "    payload['name']\n"
        "    payload[field] = 1\n"
        "    del payload[7]\n"
        "    payload.get('missing', False)\n"
        "    payload.update(other)\n"
        "    payload.pop('old', None)\n"
        "    payload.setdefault('mode', 'safe')\n"
        "    if 'name' in payload:\n"
        "        pass\n"
        "    if payload:\n"
        "        return dict(payload)\n"
        "    other['not-proven']\n",
        "package/consumer.py",
        "package.consumer",
    )
    assert [item.kind for item in observations] == [
        DictionaryOperationKind.READ,
        DictionaryOperationKind.WRITE,
        DictionaryOperationKind.DELETE,
        DictionaryOperationKind.GET,
        DictionaryOperationKind.UPDATE,
        DictionaryOperationKind.POP,
        DictionaryOperationKind.SET_DEFAULT,
        DictionaryOperationKind.MEMBERSHIP,
        DictionaryOperationKind.TRUTHINESS,
        DictionaryOperationKind.COERCION,
    ]
    assert all(item.owner == "package.consumer:process:payload" for item in observations)
    mutations = {
        DictionaryOperationKind.WRITE,
        DictionaryOperationKind.DELETE,
        DictionaryOperationKind.UPDATE,
        DictionaryOperationKind.POP,
        DictionaryOperationKind.SET_DEFAULT,
    }
    assert all(
        item.mutation_timing
        is (MutationTiming.UNKNOWN if item.kind in mutations else MutationTiming.NOT_MUTATION)
        for item in observations
    )
    assert observations[1].key_kind is DictionaryKeyKind.DYNAMIC
    assert observations[2].key_kind is DictionaryKeyKind.LITERAL_NON_STRING
    assert observations[-1].coercion == "builtins.dict"


def test_dictionary_literal_assignment_owns_following_accesses_and_rebinding_stops_them() -> None:
    observations = discover_dictionary_operations(
        "def build(other):\n"
        "    payload = {'enabled': True}\n"
        "    payload['enabled']\n"
        "    payload = other\n"
        "    payload['not-proven']\n",
        "package/build.py",
        "package.build",
    )
    assert [item.kind for item in observations] == [
        DictionaryOperationKind.LITERAL,
        DictionaryOperationKind.READ,
    ]
    assert all(item.owner == "package.build:build:payload" for item in observations)


def test_dictionary_annotation_binding_rejects_unowned_mapping_lookalikes() -> None:
    observations = discover_dictionary_operations(
        "from collections.abc import Mapping as RealMapping\n"
        "class Mapping: pass\n"
        "def accepted(value: RealMapping[str, object]):\n"
        "    value['known']\n"
        "def rejected(value: Mapping):\n"
        "    value['lookalike']\n",
        "package/types.py",
        "package.types",
    )
    assert len(observations) == 1
    assert observations[0].owner == "package.types:accepted:value"
    assert observations[0].key_repr == "known"


def test_shadowed_builtin_dict_does_not_create_annotation_or_coercion_evidence() -> None:
    observations = discover_dictionary_operations(
        "def dict(value):\n"
        "    return value\n"
        "def process(payload: dict):\n"
        "    payload['not-proven']\n"
        "    return dict(payload)\n",
        "package/shadowed.py",
        "package.shadowed",
    )
    assert observations == ()


def test_dictionary_defaults_preserve_role_and_structural_kind() -> None:
    source = (
        "def consume(payload: dict[str, object], fallback):\n"
        "    payload.get('absent')\n"
        "    payload.get('null', None)\n"
        "    payload.get('literal', {'enabled': False})\n"
        "    payload.pop('expression', fallback())\n"
        "    payload.setdefault('provided', 7)\n"
    )
    facts = discover_dictionary_facts(
        source,
        "package/defaults.py",
        "package.defaults",
    )
    defaults = facts.defaults
    assert defaults == discover_dictionary_defaults(
        source,
        "package/defaults.py",
        "package.defaults",
    )
    assert [item.role for item in defaults] == [
        DictionaryDefaultRole.CONSUMER_FALLBACK,
        DictionaryDefaultRole.CONSUMER_FALLBACK,
        DictionaryDefaultRole.CONSUMER_FALLBACK,
        DictionaryDefaultRole.CONSUMER_FALLBACK,
        DictionaryDefaultRole.PRODUCER_PROVIDED,
    ]
    assert [item.default.kind for item in defaults] == [
        InternalDefaultKind.ABSENT,
        InternalDefaultKind.EXPLICIT_NULL,
        InternalDefaultKind.JSON_LITERAL,
        InternalDefaultKind.EXPRESSION,
        InternalDefaultKind.JSON_LITERAL,
    ]
    assert defaults[2].default.literal_value_json == '{"enabled":false}'
    assert defaults[3].default.detail == "fallback()"
    assert defaults[4].default.literal_value_json == "7"
    assert len(facts.operations) == 6


def test_dictionary_owner_survives_if_only_when_both_branches_bind_it() -> None:
    joined = discover_dictionary_operations(
        "def build(flag):\n"
        "    if flag:\n"
        "        payload = {'kind': 'left'}\n"
        "    else:\n"
        "        payload = {'kind': 'right'}\n"
        "    payload.get('kind')\n",
        "package/joined.py",
        "package.joined",
    )
    partial = discover_dictionary_operations(
        "def build(flag):\n"
        "    if flag:\n"
        "        payload = {'kind': 'left'}\n"
        "    payload.get('kind')\n",
        "package/partial.py",
        "package.partial",
    )
    assert [item.kind for item in joined].count(DictionaryOperationKind.GET) == 1
    assert all(
        item.variant is not None for item in joined if item.kind is DictionaryOperationKind.LITERAL
    )
    assert all(item.kind is not DictionaryOperationKind.GET for item in partial)


def test_dictionary_aliases_are_explicit_straight_line_and_reassignment_stops_flow() -> None:
    facts = discover_dictionary_facts(
        "def consume(payload: dict, other):\n"
        "    alias = payload\n"
        "    second = alias\n"
        "    second['known']\n"
        "    alias = other\n"
        "    alias['not-proven']\n"
        "    left = right = payload\n"
        "    left['ambiguous']\n",
        "package/aliases.py",
        "package.aliases",
    )
    assert [(item.source_owner, item.alias_identity) for item in facts.aliases] == [
        ("package.aliases:consume:payload", "package.aliases:consume:alias"),
        ("package.aliases:consume:payload", "package.aliases:consume:second"),
    ]
    reads = [item for item in facts.operations if item.kind is DictionaryOperationKind.READ]
    assert len(reads) == 1
    assert reads[0].owner == "package.aliases:consume:payload"
    assert reads[0].key_repr == "known"
