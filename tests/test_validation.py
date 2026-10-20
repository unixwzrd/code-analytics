from __future__ import annotations

from code_analysis.analysis.dictionary_ops import discover_dictionary_facts
from code_analysis.analysis.validation import (
    OperationalValidationKind,
    OperationalValidationSource,
    StructuralValidationKind,
    discover_operational_validation_patterns,
    discover_structural_validation_patterns,
)


def test_structural_validation_groups_exact_dictionary_checks() -> None:
    source = """
def first(payload: dict[str, object]) -> None:
    if "item_id" not in payload:
        raise ValueError("missing")
    if payload:
        dict(payload)

def second(payload: dict[str, object]) -> bool:
    return "item_id" in payload
"""
    facts = discover_dictionary_facts(source, "package/checks.py", "package.checks")
    patterns = discover_structural_validation_patterns((facts,))

    assert [item.rule for item in patterns] == [
        "dictionary coercion: builtins.dict",
        "dictionary key absent: literal-string:item_id",
        "dictionary key present: literal-string:item_id",
        "dictionary truthy",
    ]
    assert all(len(item.sites) == 1 for item in patterns)


def test_presence_and_value_truthiness_are_evidenced_as_divergent() -> None:
    source = """
def validate(payload: dict[str, object]) -> None:
    if "item_id" in payload:
        consume(payload["item_id"])
    if payload.get("item_id"):
        consume(payload["item_id"])
"""
    facts = discover_dictionary_facts(source, "package/checks.py", "package.checks")
    patterns = discover_structural_validation_patterns((facts,))
    divergent = [item for item in patterns if item.divergent_enforcement]

    assert [item.kind for item in divergent] == [
        StructuralValidationKind.KEY_MEMBERSHIP,
        StructuralValidationKind.KEY_VALUE_TRUTHINESS,
    ]
    assert divergent[1].limitations == (
        "Truthy/falsy value checks do not distinguish absent, null, zero, false, or empty values.",
    )


def test_operational_path_checks_are_not_structural_dictionary_validation() -> None:
    source = """
from pathlib import Path

def validate(path: Path) -> bool:
    return path.exists() and path.is_file()
"""
    facts = discover_dictionary_facts(source, "package/paths.py", "package.paths")
    assert discover_structural_validation_patterns((facts,)) == ()


def test_operational_path_checks_are_classified_separately() -> None:
    source = """
from pathlib import Path
import os

def validate(path: Path, root: Path) -> bool:
    if not path.exists() or path.is_symlink():
        return False
    if path.stat().st_uid != os.getuid() or not os.access(path, os.R_OK):
        return False
    return path.resolve().is_relative_to(root.resolve())
"""
    patterns = discover_operational_validation_patterns(
        (OperationalValidationSource("package.paths", "package/paths.py", source),)
    )

    assert {item.kind for item in patterns} == {
        OperationalValidationKind.FILESYSTEM_EXISTENCE,
        OperationalValidationKind.FILESYSTEM_METADATA,
        OperationalValidationKind.PERMISSION,
        OperationalValidationKind.PATH_CANONICALIZATION,
        OperationalValidationKind.SYMLINK_STATE,
    }
    assert all(item.limitations for item in patterns)
    assert all(
        site.owner == "package.paths:validate:operational-validation"
        for item in patterns
        for site in item.sites
    )


def test_operational_checks_require_path_provenance_and_validation_context() -> None:
    source = """
import os

def inspect(candidate, path_factory, os):
    candidate.exists()
    value = path_factory().resolve()
    if os.path.exists(candidate):
        return True
    if candidate.st_uid == 0:
        return False
    return value
"""
    patterns = discover_operational_validation_patterns(
        (OperationalValidationSource("package.paths", "package/paths.py", source),)
    )
    assert patterns == ()


def test_exact_value_type_and_key_set_checks_are_structural_validation() -> None:
    source = """
from package.types import ItemId

def validate(payload: dict[str, object]) -> None:
    if not isinstance(payload["item_id"], (str, ItemId)):
        raise TypeError("item_id")
    if set(payload) != {"item_id", "kind"}:
        raise ValueError("keys")
"""
    facts = discover_dictionary_facts(source, "package/checks.py", "package.checks")
    patterns = discover_structural_validation_patterns((facts,))

    assert [item.kind for item in patterns] == [
        StructuralValidationKind.EXACT_KEYS,
        StructuralValidationKind.VALUE_TYPE,
    ]
    assert [item.rule for item in patterns] == [
        "dictionary keys not equal to: [item_id,kind]",
        "dictionary key value is not instance of: "
        "literal-string:item_id:(str, package.types.ItemId)",
    ]


def test_dynamic_type_and_nonliteral_exact_keys_are_not_inferred() -> None:
    source = """
def validate(payload: dict[str, object], expected_type, expected_keys) -> None:
    isinstance(payload["item_id"], expected_type)
    set(payload) == expected_keys
    payload.keys() == ["item_id"]
"""
    facts = discover_dictionary_facts(source, "package/checks.py", "package.checks")
    assert discover_structural_validation_patterns((facts,)) == ()


def test_schema_version_and_state_literals_retain_exact_relations() -> None:
    source = """
def validate(payload: dict[str, object]) -> None:
    if payload["schema_version"] != 2:
        raise ValueError("version")
    if payload.get("state") not in {"ready", "complete"}:
        raise ValueError("state")
"""
    facts = discover_dictionary_facts(source, "package/checks.py", "package.checks")
    patterns = discover_structural_validation_patterns((facts,))

    assert [item.kind for item in patterns] == [
        StructuralValidationKind.VALUE_LITERAL,
        StructuralValidationKind.VALUE_LITERAL,
    ]
    assert [item.rule for item in patterns] == [
        "dictionary key value not-equals: literal-string:schema_version:[2]",
        'dictionary key value not-in: literal-string:state:["complete","ready"]',
    ]


def test_dynamic_literal_constraints_and_chained_comparisons_are_not_inferred() -> None:
    source = """
def validate(payload: dict[str, object], expected, low, high) -> None:
    payload["schema_version"] == expected
    low < payload["count"] < high
"""
    facts = discover_dictionary_facts(source, "package/checks.py", "package.checks")
    assert discover_structural_validation_patterns((facts,)) == ()


def test_negative_validation_branch_that_continues_is_retained_as_fail_open() -> None:
    source = """
import logging

def missing(payload: dict[str, object]) -> None:
    if "item_id" not in payload:
        logging.warning("missing item_id")
    consume(payload)

def wrong_type(payload: dict[str, object]) -> None:
    if not isinstance(payload["item_id"], str):
        pass
    consume(payload)
"""
    facts = discover_dictionary_facts(source, "package/checks.py", "package.checks")
    patterns = discover_structural_validation_patterns((facts,))

    fail_open = [item for item in patterns if item.fail_open]
    assert [item.kind for item in fail_open] == [
        StructuralValidationKind.KEY_MEMBERSHIP,
        StructuralValidationKind.VALUE_TYPE,
    ]
    assert all(
        item.limitations
        == (
            "A negative literal-field or exact-key-set branch contains only pass or "
            "standard-library logging; runtime acceptance remains unmeasured.",
        )
        for item in fail_open
    )


def test_terminating_and_positive_validation_branches_are_not_fail_open() -> None:
    source = """
def raises(payload: dict[str, object]) -> None:
    if "item_id" not in payload:
        raise ValueError("missing")

def returns(payload: dict[str, object]) -> bool:
    if payload["schema_version"] != 2:
        return False
    return True

def positive(payload: dict[str, object]) -> None:
    if "item_id" in payload:
        consume(payload["item_id"])
"""
    facts = discover_dictionary_facts(source, "package/checks.py", "package.checks")
    patterns = discover_structural_validation_patterns((facts,))

    assert patterns
    assert not any(item.fail_open for item in patterns)


def test_mixed_condition_marks_only_its_negative_validation_rule_fail_open() -> None:
    source = """
def validate(payload: dict[str, object]) -> None:
    if "missing" not in payload or "present" in payload:
        pass
"""
    facts = discover_dictionary_facts(source, "package/checks.py", "package.checks")
    patterns = discover_structural_validation_patterns((facts,))

    states = {item.rule: item.fail_open for item in patterns}
    assert states == {
        "dictionary key absent: literal-string:missing": True,
        "dictionary key present: literal-string:present": False,
    }


def test_dynamic_key_and_whole_mapping_checks_are_not_fail_open_candidates() -> None:
    source = """
def inspect(payload: dict[str, object], key: str) -> None:
    if key not in payload:
        visit(key)
    if not payload:
        initialize()
"""
    facts = discover_dictionary_facts(source, "package/checks.py", "package.checks")
    patterns = discover_structural_validation_patterns((facts,))

    assert patterns
    assert not any(item.fail_open for item in patterns)


def test_error_accumulation_and_fallback_are_not_fail_open_candidates() -> None:
    source = """
def validate(payload: dict[str, object], errors: list[str]) -> None:
    if payload.get("schema_version") != 2:
        errors.append("wrong version")
    if "mode" not in payload:
        payload["mode"] = "safe"
"""
    facts = discover_dictionary_facts(source, "package/checks.py", "package.checks")
    patterns = discover_structural_validation_patterns((facts,))

    assert patterns
    assert not any(item.fail_open for item in patterns)
