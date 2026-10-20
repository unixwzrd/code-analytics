from __future__ import annotations

import pytest

from code_analysis.analysis.flow import (
    DictionaryModuleSource,
    InternalFlowKind,
    InternalResolutionState,
    discover_dictionary_flows,
    discover_project_dictionary_flows,
)


def test_dictionary_flow_resolves_local_arguments_and_returns() -> None:
    source = (
        "def consume(value):\n"
        "    return None\n"
        "def forward(payload: dict[str, object]):\n"
        "    consume(payload)\n"
        "    external(payload)\n"
        "    external(42)\n"
        "    return payload\n"
    )
    facts = discover_dictionary_flows(source, "package/flow.py", "package.flow")
    assert facts == discover_dictionary_flows(source, "package/flow.py", "package.flow")
    assert len(facts.flows) == 3
    assert facts.unresolved_count == 1
    assert facts.ambiguous_count == 0
    assert facts.truncated_count == 0
    resolved_argument = next(
        item
        for item in facts.flows
        if item.kind is InternalFlowKind.ARGUMENT
        and item.resolution is InternalResolutionState.RESOLVED
    )
    assert resolved_argument.source_owner == "package.flow:forward:payload"
    assert resolved_argument.target_identity == "package.flow:consume:parameter:value"
    unresolved = next(
        item for item in facts.flows if item.resolution is InternalResolutionState.UNRESOLVED
    )
    assert unresolved.target_identity == "package.flow:call:external:argument:0"
    returned = next(item for item in facts.flows if item.kind is InternalFlowKind.RETURN)
    assert returned.target_identity == "package.flow:forward:return"
    assert all(item.span.logical_path == "package/flow.py" for item in facts.flows)
    assert "/Users/" not in repr(facts)


def test_dictionary_flow_marks_duplicate_local_targets_ambiguous() -> None:
    source = (
        "def consume(first):\n"
        "    return first\n"
        "def consume(second):\n"
        "    return second\n"
        "def forward(payload: dict):\n"
        "    consume(payload)\n"
    )
    facts = discover_dictionary_flows(source, "package/ambiguous.py", "package.ambiguous")
    assert len(facts.flows) == 1
    assert facts.flows[0].resolution is InternalResolutionState.AMBIGUOUS
    assert facts.ambiguous_count == 1
    assert facts.unresolved_count == 0


def test_dictionary_flow_does_not_resolve_shadowed_local_call_name() -> None:
    source = (
        "def consume(value):\n"
        "    return None\n"
        "def forward(payload: dict, consume):\n"
        "    consume(payload)\n"
    )
    facts = discover_dictionary_flows(source, "package/shadow.py", "package.shadow")
    assert len(facts.flows) == 1
    assert facts.flows[0].resolution is InternalResolutionState.UNRESOLVED
    assert facts.flows[0].target_identity == "package.shadow:call:consume:argument:0"


def test_builtin_set_conversion_is_not_flow_but_shadowed_set_is_unresolved() -> None:
    builtin = discover_dictionary_flows(
        "def validate(payload: dict):\n" "    return set(payload) == {'item_id'}\n",
        "package/builtin_set.py",
        "package.builtin_set",
    )
    shadowed = discover_dictionary_flows(
        "def validate(payload: dict, set):\n" "    return set(payload)\n",
        "package/shadowed_set.py",
        "package.shadowed_set",
    )

    assert builtin.flows == ()
    assert shadowed.flows[0].resolution is InternalResolutionState.UNRESOLVED
    assert shadowed.flows[0].target_identity == "package.shadowed_set:call:set:argument:0"


def test_dictionary_flow_limit_is_explicit_and_exact() -> None:
    source = (
        "def consume(value):\n"
        "    return None\n"
        "def forward(payload: dict):\n"
        "    consume(payload)\n"
        "    consume(payload)\n"
        "    return payload\n"
    )
    facts = discover_dictionary_flows(
        source,
        "package/bounded.py",
        "package.bounded",
        max_items=2,
    )
    assert len(facts.flows) == 2
    assert facts.truncated_count == 1
    with pytest.raises(ValueError, match="positive exact integer"):
        discover_dictionary_flows(source, "package/bounded.py", "package.bounded", max_items=0)
    with pytest.raises(ValueError, match="positive exact integer"):
        discover_dictionary_flows(
            source,
            "package/bounded.py",
            "package.bounded",
            max_items=True,
        )


def test_dictionary_flow_parse_failure_cannot_become_empty_success() -> None:
    with pytest.raises(SyntaxError):
        discover_dictionary_flows(
            "def broken(:\n",
            "package/broken.py",
            "package.broken",
        )


def test_dictionary_flow_resolves_straight_line_alias_arguments() -> None:
    source = (
        "def consume(value):\n"
        "    return None\n"
        "def forward(payload: dict, other):\n"
        "    alias = payload\n"
        "    consume(alias)\n"
        "    alias = other\n"
        "    consume(alias)\n"
    )
    facts = discover_dictionary_flows(source, "package/alias_flow.py", "package.alias_flow")
    assert len(facts.flows) == 1
    assert facts.flows[0].source_owner == "package.alias_flow:forward:payload"
    assert facts.flows[0].target_identity == "package.alias_flow:consume:parameter:value"


def test_dictionary_flow_reaches_untyped_parameters_through_fixed_point() -> None:
    source = (
        "def consume(value):\n"
        "    external(value)\n"
        "def relay(item):\n"
        "    consume(item)\n"
        "def produce():\n"
        "    payload = {'name': 'job'}\n"
        "    relay(payload)\n"
    )
    facts = discover_dictionary_flows(source, "package/chain.py", "package.chain")
    assert [(item.source_owner, item.target_identity, item.resolution) for item in facts.flows] == [
        (
            "package.chain:consume:parameter:value",
            "package.chain:call:external:argument:0",
            InternalResolutionState.UNRESOLVED,
        ),
        (
            "package.chain:produce:payload",
            "package.chain:relay:parameter:item",
            InternalResolutionState.RESOLVED,
        ),
        (
            "package.chain:relay:parameter:item",
            "package.chain:consume:parameter:value",
            InternalResolutionState.RESOLVED,
        ),
    ]
    assert facts.unresolved_count == 1
    assert facts.truncated_count == 0


def test_dictionary_flow_propagates_exact_call_returns() -> None:
    source = (
        "def relay(item):\n"
        "    return item\n"
        "def consume(value):\n"
        "    return None\n"
        "def produce():\n"
        "    payload = {'name': 'job'}\n"
        "    forwarded = relay(payload)\n"
        "    consume(forwarded)\n"
    )
    facts = discover_dictionary_flows(source, "package/returns.py", "package.returns")
    endpoints = {(item.source_owner, item.target_identity) for item in facts.flows}
    assert endpoints == {
        (
            "package.returns:produce:payload",
            "package.returns:relay:parameter:item",
        ),
        (
            "package.returns:relay:parameter:item",
            "package.returns:relay:return",
        ),
        (
            "package.returns:relay:return",
            "package.returns:consume:parameter:value",
        ),
    }


def test_dictionary_flow_depth_and_path_bounds_are_explicit() -> None:
    source = (
        "def third(value):\n"
        "    external(value)\n"
        "def second(value):\n"
        "    third(value)\n"
        "def first(value):\n"
        "    second(value)\n"
        "def produce():\n"
        "    payload = {}\n"
        "    first(payload)\n"
    )
    depth_limited = discover_dictionary_flows(
        source,
        "package/bounds.py",
        "package.bounds",
        max_depth=2,
    )
    assert len(depth_limited.flows) == 2
    assert depth_limited.truncated_count == 1
    path_limited = discover_dictionary_flows(
        source,
        "package/bounds.py",
        "package.bounds",
        max_paths_per_owner=1,
    )
    assert len(path_limited.flows) == 1
    assert path_limited.truncated_count == 1
    with pytest.raises(ValueError, match="max_depth"):
        discover_dictionary_flows(
            source,
            "package/bounds.py",
            "package.bounds",
            max_depth=True,
        )
    with pytest.raises(ValueError, match="max_paths_per_owner"):
        discover_dictionary_flows(
            source,
            "package/bounds.py",
            "package.bounds",
            max_paths_per_owner=0,
        )


def test_dictionary_flow_recursion_converges_without_duplicate_paths() -> None:
    source = (
        "def recurse(value):\n"
        "    recurse(value)\n"
        "def produce():\n"
        "    payload = {}\n"
        "    recurse(payload)\n"
    )
    facts = discover_dictionary_flows(source, "package/recursive.py", "package.recursive")
    assert len(facts.flows) == 2
    assert facts.truncated_count == 0
    assert (
        facts.flows
        == discover_dictionary_flows(
            source,
            "package/recursive.py",
            "package.recursive",
        ).flows
    )


def test_project_flow_resolves_relative_import_and_downstream_unresolved_call() -> None:
    modules = (
        DictionaryModuleSource(
            module="package.consumer",
            logical_path="src/package/consumer.py",
            source="def consume(value: dict[str, object]):\n    external(value)\n",
        ),
        DictionaryModuleSource(
            module="package.producer",
            logical_path="src/package/producer.py",
            source=(
                "from .consumer import consume\n"
                "def produce():\n"
                "    payload = {'name': 'job'}\n"
                "    consume(payload)\n"
            ),
        ),
    )
    facts = discover_project_dictionary_flows(modules)
    assert [(item.source_owner, item.target_identity, item.resolution) for item in facts.flows] == [
        (
            "package.consumer:consume:value",
            "package.consumer:call:external:argument:0",
            InternalResolutionState.UNRESOLVED,
        ),
        (
            "package.producer:produce:payload",
            "package.consumer:consume:value",
            InternalResolutionState.RESOLVED,
        ),
    ]
    assert facts.unresolved_count == 1
    assert facts.ambiguous_count == 0
    assert all("/Users/" not in repr(item) for item in facts.flows)
    depth_limited = discover_project_dictionary_flows(modules, max_depth=1)
    assert len(depth_limited.flows) == 2
    assert depth_limited.truncated_count == 1


def test_project_flow_resolves_namespace_module_alias_and_call_return() -> None:
    modules = (
        DictionaryModuleSource(
            module="acme.tools.consumer",
            logical_path="src/acme/tools/consumer.py",
            source="def relay(value):\n    return value\n",
        ),
        DictionaryModuleSource(
            module="acme.tools.producer",
            logical_path="src/acme/tools/producer.py",
            source=(
                "import acme.tools.consumer as sink\n"
                "def consume(value):\n"
                "    return None\n"
                "def produce():\n"
                "    payload = {}\n"
                "    forwarded = sink.relay(payload)\n"
                "    consume(forwarded)\n"
            ),
        ),
    )
    facts = discover_project_dictionary_flows(modules)
    assert {(item.source_owner, item.target_identity) for item in facts.flows} == {
        (
            "acme.tools.producer:produce:payload",
            "acme.tools.consumer:relay:parameter:value",
        ),
        (
            "acme.tools.consumer:relay:parameter:value",
            "acme.tools.consumer:relay:return",
        ),
        (
            "acme.tools.consumer:relay:return",
            "acme.tools.producer:consume:parameter:value",
        ),
    }


def test_project_flow_preserves_duplicate_import_target_ambiguity() -> None:
    modules = (
        DictionaryModuleSource(
            module="package.consumer",
            logical_path="package/consumer.py",
            source=(
                "def consume(first):\n"
                "    return None\n"
                "def consume(second):\n"
                "    return None\n"
            ),
        ),
        DictionaryModuleSource(
            module="package.producer",
            logical_path="package/producer.py",
            source=(
                "from .consumer import consume\n"
                "def produce():\n"
                "    payload = {}\n"
                "    consume(payload)\n"
            ),
        ),
    )
    facts = discover_project_dictionary_flows(modules)
    assert len(facts.flows) == 1
    assert facts.flows[0].resolution is InternalResolutionState.AMBIGUOUS
    assert facts.ambiguous_count == 1


def test_project_flow_cross_module_cycle_converges_deterministically() -> None:
    modules = (
        DictionaryModuleSource(
            module="package.a",
            logical_path="package/a.py",
            source=(
                "from .b import forward\n"
                "def start(value):\n"
                "    forward(value)\n"
                "def produce():\n"
                "    payload = {}\n"
                "    start(payload)\n"
            ),
        ),
        DictionaryModuleSource(
            module="package.b",
            logical_path="package/b.py",
            source=("from .a import start\n" "def forward(value):\n" "    start(value)\n"),
        ),
    )
    facts = discover_project_dictionary_flows(modules)
    assert len(facts.flows) == 3
    assert facts.truncated_count == 0
    assert facts == discover_project_dictionary_flows(modules)


def test_project_flow_requires_sorted_unique_modules_and_valid_source() -> None:
    good = DictionaryModuleSource("package.a", "package/a.py", "value = {}\n")
    with pytest.raises(ValueError, match="unique and sorted"):
        discover_project_dictionary_flows((good, good))
    with pytest.raises(ValueError, match="portable identities"):
        DictionaryModuleSource("package.a", "/Users/example/package/a.py", "")
    with pytest.raises(ValueError, match="portable identities"):
        DictionaryModuleSource("package.a", "../package/a.py", "")
    with pytest.raises(ValueError, match="at least one module"):
        discover_project_dictionary_flows(())
    with pytest.raises(SyntaxError):
        discover_project_dictionary_flows(
            (DictionaryModuleSource("package.a", "package/a.py", "def broken(:\n"),)
        )
