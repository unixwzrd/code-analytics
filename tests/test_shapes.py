from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from code_analysis.analysis.dictionary_ops import discover_dictionary_facts
from code_analysis.analysis.dictionary_values import InternalRequirednessKind
from code_analysis.analysis.shapes import build_dictionary_shape_inputs


def test_single_owner_shape_preserves_fields_requiredness_and_default_roles() -> None:
    source = (
        "def process(field):\n"
        "    payload = {'name': 'job', 'retries': 3, field: False}\n"
        "    payload['name']\n"
        "    payload.get('mode', 'safe')\n"
        "    payload.setdefault('enabled', True)\n"
        "    if 'name' in payload:\n"
        "        return payload\n"
    )
    facts = discover_dictionary_facts(source, "package/shape.py", "package.shape")
    result = build_dictionary_shape_inputs(facts)
    assert result == build_dictionary_shape_inputs(facts)
    assert len(result.shapes) == 1
    shape = result.shapes[0]
    assert shape.owner == "package.shape:process:payload"
    assert shape.producers == (shape.owner,)
    assert shape.consumers == (shape.owner,)
    fields = {field.name: field for field in shape.variants[0].fields}
    assert list(fields) == ["enabled", "mode", "name", "retries"]
    assert fields["retries"].type_reprs == ("int",)
    assert fields["mode"].consumer_fallbacks[0].literal_value_json == '"safe"'
    assert fields["enabled"].producer_defaults[0].literal_value_json == "true"
    assert {item.kind for item in fields["name"].requiredness_observations} == {
        InternalRequirednessKind.ALWAYS,
        InternalRequirednessKind.CONDITIONAL,
    }
    assert result.unresolved_observation_count == 1
    assert result.unresolved_owners == ()
    with pytest.raises(FrozenInstanceError):
        result.shapes = ()


def test_shape_builder_does_not_flatten_dynamic_or_non_string_keys() -> None:
    source = "def build(field):\n" "    payload = {field: 1, 7: 'value'}\n" "    return payload\n"
    facts = discover_dictionary_facts(source, "package/dynamic.py", "package.dynamic")
    result = build_dictionary_shape_inputs(facts)
    assert result.shapes == ()
    assert result.unresolved_owners == ("package.dynamic:build:payload",)
    assert result.unresolved_observation_count == 2


def test_conditional_literals_remain_variants_with_exact_discriminator() -> None:
    source = (
        "def build(queued):\n"
        "    if queued:\n"
        "        payload = {'kind': 'queued', 'job_id': 'j1'}\n"
        "    else:\n"
        "        payload = {'kind': 'done', 'result': 1}\n"
        "    payload.get('trace_id')\n"
        "    return payload\n"
    )
    facts = discover_dictionary_facts(source, "package/variants.py", "package.variants")
    shape = build_dictionary_shape_inputs(facts).shapes[0]
    assert [variant.name for variant in shape.variants] == [
        "if@2:4:body",
        "if@2:4:else",
    ]
    fields = [{field.name for field in variant.fields} for variant in shape.variants]
    assert fields == [
        {"job_id", "kind", "trace_id"},
        {"kind", "result", "trace_id"},
    ]
    assert [variant.discriminator_field for variant in shape.variants] == ["kind", "kind"]
    assert [variant.discriminator_values_json for variant in shape.variants] == [
        ('"queued"',),
        ('"done"',),
    ]
    assert all(variant.lifecycle_states == () for variant in shape.variants)
    assert all("job_id" not in values or "result" not in values for values in fields)


def test_variants_do_not_invent_discriminator_without_distinct_exact_literals() -> None:
    source = (
        "def build(flag, kind):\n"
        "    if flag:\n"
        "        payload = {'kind': kind, 'left': 1}\n"
        "    else:\n"
        "        payload = {'kind': kind, 'right': 2}\n"
        "    return payload\n"
    )
    facts = discover_dictionary_facts(source, "package/no_tag.py", "package.no_tag")
    shape = build_dictionary_shape_inputs(facts).shapes[0]
    assert len(shape.variants) == 2
    assert all(variant.discriminator_field is None for variant in shape.variants)


def test_consumer_only_branches_do_not_manufacture_shape_variants() -> None:
    source = (
        "def consume(payload: dict, flag):\n"
        "    if flag:\n"
        "        payload['left']\n"
        "    else:\n"
        "        payload['right']\n"
    )
    facts = discover_dictionary_facts(source, "package/read_paths.py", "package.read_paths")
    shape = build_dictionary_shape_inputs(facts).shapes[0]
    assert [variant.name for variant in shape.variants] == ["observed"]
    fields = {field.name: field for field in shape.variants[0].fields}
    assert set(fields) == {"left", "right"}
    assert all(
        observation.kind is InternalRequirednessKind.CONDITIONAL
        for field in fields.values()
        for observation in field.requiredness_observations
    )


def test_exact_literal_writes_create_separate_lifecycle_transition_evidence() -> None:
    source = (
        "def run(next_state):\n"
        "    payload = {'state': 'queued', 'job_id': 'j1'}\n"
        "    payload['state'] = 'running'\n"
        "    payload['state'] = next_state\n"
        "    payload['state'] = 'done'\n"
        "    return payload\n"
    )
    facts = discover_dictionary_facts(source, "package/lifecycle.py", "package.lifecycle")
    result = build_dictionary_shape_inputs(facts)
    assert len(result.transitions) == 1
    transition = result.transitions[0]
    assert transition.owner == "package.lifecycle:run:payload"
    assert transition.field_name == "state"
    assert transition.from_value_json == '"queued"'
    assert transition.to_value_json == '"running"'
    assert transition.span.start_line == 3
    assert result.shapes[0].variants[0].lifecycle_states == (
        'state="queued"',
        'state="running"',
    )


def test_branch_lifecycle_transitions_remain_variant_specific() -> None:
    source = (
        "def run(failed):\n"
        "    payload = {'state': 'queued'}\n"
        "    if failed:\n"
        "        payload['state'] = 'failed'\n"
        "    else:\n"
        "        payload['state'] = 'running'\n"
        "    return payload\n"
    )
    facts = discover_dictionary_facts(source, "package/branches.py", "package.branches")
    result = build_dictionary_shape_inputs(facts)
    assert [variant.name for variant in result.shapes[0].variants] == [
        "if@3:4:body",
        "if@3:4:else",
        "observed",
    ]
    assert [
        (item.variant, item.from_value_json, item.to_value_json) for item in result.transitions
    ] == [
        ("if@3:4:body", '"queued"', '"failed"'),
        ("if@3:4:else", '"queued"', '"running"'),
    ]
    variants = {item.name: item.lifecycle_states for item in result.shapes[0].variants}
    assert variants == {
        "if@3:4:body": ('state="failed"', 'state="queued"'),
        "if@3:4:else": ('state="queued"', 'state="running"'),
        "observed": (),
    }
