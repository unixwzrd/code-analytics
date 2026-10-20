from __future__ import annotations

from code_analysis.analysis.dictionary_ops import discover_dictionary_facts
from code_analysis.analysis.shapes import build_dictionary_shape_inputs
from code_analysis.analysis.structures import (
    InternalStructureScope,
    discover_data_structure_topology,
)


def test_dictionary_topology_retains_scope_alias_lifecycle_and_growth_evidence() -> None:
    source = """
registry: dict[str, int] = {}
registry["first"] = 1
registry.pop("first")
registry.clear()

def build() -> dict[str, object]:
    payload = {"state": "queued"}
    alias = payload
    payload["state"] = "running"
    return alias
"""
    facts = discover_dictionary_facts(source, "package/state.py", "package.state")
    shapes = build_dictionary_shape_inputs(facts)
    records = discover_data_structure_topology((facts,), (shapes,))
    by_owner = {item.owner: item for item in records}

    registry = by_owner["package.state:<module>:registry"]
    assert registry.scope is InternalStructureScope.MODULE
    assert registry.writer_owners == (registry.owner,)
    assert registry.growth_indicators == (
        "module-scope mutable mapping receives writes; measure boundedness",
    )
    assert registry.cleanup == (
        "exact mapping clear observed",
        "exact mapping entry removal observed",
    )

    payload = by_owner["package.state:build:payload"]
    assert payload.scope is InternalStructureScope.LOCAL
    assert payload.alias_owners == ("package.state:build:alias",)
    assert payload.ordering_assumptions == ("exact state-write order observed",)
    assert payload.invariants == ()
    assert payload.evidence


def test_data_structure_topology_requires_aligned_inputs() -> None:
    facts = discover_dictionary_facts("payload = {'id': 1}\n", "module.py", "module")
    try:
        discover_data_structure_topology((facts,), ())
    except ValueError as error:
        assert "must align" in str(error)
    else:
        raise AssertionError("unaligned structure inputs must fail")


def test_invalid_clear_call_is_not_cleanup_evidence() -> None:
    source = "registry: dict[str, int] = {'first': 1}\nregistry.clear(1)\n"
    facts = discover_dictionary_facts(source, "module.py", "module")
    shapes = build_dictionary_shape_inputs(facts)
    records = discover_data_structure_topology((facts,), (shapes,))
    assert records[0].cleanup == ()


def test_topology_retains_lexical_lock_evidence_without_runtime_claims() -> None:
    source = """
from multiprocessing import Lock

registry_lock = Lock()
registry: dict[str, int] = {}
with registry_lock:
    registry["first"] = 1
"""
    facts = discover_dictionary_facts(source, "package/state.py", "package.state")
    shapes = build_dictionary_shape_inputs(facts)
    record = discover_data_structure_topology((facts,), (shapes,))[0]

    assert record.concurrency == (
        "process-lock context lexically guards mutation via "
        "package.state:<module>:registry_lock",
    )
    assert record.unresolved_behavior == (
        "Lexical lock context does not prove runtime mutual exclusion or complete " "coordination.",
    )


def test_module_mutation_without_recognized_lock_remains_explicitly_unresolved() -> None:
    facts = discover_dictionary_facts(
        "registry: dict[str, int] = {}\nregistry['first'] = 1\n",
        "package/state.py",
        "package.state",
    )
    shapes = build_dictionary_shape_inputs(facts)
    record = discover_data_structure_topology((facts,), (shapes,))[0]

    assert record.concurrency == ()
    assert record.unresolved_behavior == (
        "No recognized lexical lock guard was observed; indirect or external coordination "
        "is unresolved.",
    )


def test_topology_distinguishes_instance_owned_dictionary_scope() -> None:
    source = """
import threading

class Registry:
    def __init__(self):
        self.entries: dict[str, int] = {}
        self.lock = threading.Lock()

    def add(self) -> None:
        with self.lock:
            self.entries["first"] = 1
"""
    facts = discover_dictionary_facts(source, "package/registry.py", "package.registry")
    shapes = build_dictionary_shape_inputs(facts)
    record = discover_data_structure_topology((facts,), (shapes,))[0]

    assert record.owner == "package.registry:Registry:self.entries"
    assert record.scope is InternalStructureScope.INSTANCE
    assert record.lifetime == ("instance-owned",)
    assert record.concurrency == (
        "thread-lock context lexically guards mutation via package.registry:Registry:self.lock",
    )


def test_topology_retains_evidenced_mapping_when_field_shape_is_unresolved() -> None:
    facts = discover_dictionary_facts(
        "def build(key: str):\n"
        "    payload = {}\n"
        "    payload[key] = 1\n"
        "    return payload\n",
        "package/dynamic.py",
        "package.dynamic",
    )
    shapes = build_dictionary_shape_inputs(facts)
    assert shapes.shapes == ()

    record = discover_data_structure_topology((facts,), (shapes,))[0]
    assert record.owner == "package.dynamic:build:payload"
    assert record.producer_owners == (record.owner,)
    assert "Dictionary field shape remains unresolved" in " ".join(record.unresolved_behavior)


def test_dataclass_factory_is_an_instance_structure_constructor() -> None:
    source = """
from dataclasses import dataclass, field
import threading

@dataclass
class Registry:
    entries: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self) -> None:
        with self.lock:
            self.entries["first"] = 1
"""
    facts = discover_dictionary_facts(source, "package/registry.py", "package.registry")
    shapes = build_dictionary_shape_inputs(facts)
    record = discover_data_structure_topology((facts,), (shapes,))[0]

    assert record.scope is InternalStructureScope.INSTANCE
    assert record.constructor_owners == (record.owner,)
    assert record.concurrency == (
        "thread-lock context lexically guards mutation via package.registry:Registry:self.lock",
    )


def test_class_body_mapping_has_distinct_class_ownership() -> None:
    facts = discover_dictionary_facts(
        "class Registry:\n" "    entries: dict[str, int] = {'first': 1}\n",
        "package/registry.py",
        "package.registry",
    )
    shapes = build_dictionary_shape_inputs(facts)
    record = discover_data_structure_topology((facts,), (shapes,))[0]

    assert record.owner == "package.registry:Registry:<class>.entries"
    assert record.scope is InternalStructureScope.CLASS
    assert record.lifetime == ("class-owned",)


def test_topology_retains_nested_lock_order_without_deadlock_claim() -> None:
    source = """
import threading
outer = threading.Lock()
inner = threading.RLock()
registry: dict[str, int] = {}
with outer:
    with inner:
        registry["first"] = 1
"""
    facts = discover_dictionary_facts(source, "package/state.py", "package.state")
    shapes = build_dictionary_shape_inputs(facts)
    record = discover_data_structure_topology((facts,), (shapes,))[0]

    assert (
        "lexical lock nesting order: package.state:<module>:outer -> "
        "package.state:<module>:inner"
    ) in record.concurrency
    assert all("deadlock" not in item.lower() for item in record.concurrency)


def test_classmethod_mutation_retains_class_lock_guard() -> None:
    source = """
import threading
class Registry:
    entries: dict[str, int] = {}
    lock = threading.Lock()

    @classmethod
    def add(cls) -> None:
        with cls.lock:
            cls.entries["first"] = 1
"""
    facts = discover_dictionary_facts(source, "package/registry.py", "package.registry")
    shapes = build_dictionary_shape_inputs(facts)
    record = discover_data_structure_topology((facts,), (shapes,))[0]

    assert record.scope is InternalStructureScope.CLASS
    assert record.writer_owners == (record.owner,)
    assert record.concurrency == (
        "thread-lock context lexically guards mutation via "
        "package.registry:Registry:<class>.lock",
    )
