from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from code_analysis.analysis.contracts import (
    DeclarationKind,
    InternalDefaultKind,
    InternalRequirednessKind,
    InternalTypeKind,
    discover_class_lineages,
    discover_contract_declarations,
    discover_contract_uses,
    merge_inherited_contract_members,
    normalize_annotation,
    resolve_contract_kinds,
)
from code_analysis.config import AnalysisConfig, load_project_config
from code_analysis.models import Completeness
from code_analysis.project import discover_context
from code_analysis.report_models import AnalysisReport, BoundaryClass, SectionCoverage, SectionState
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.reports.markdown import render_markdown
from code_analysis.service import scan_contract_inventory

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


def _section_coverage(report: AnalysisReport, section: str) -> SectionCoverage:
    for item in report.section_coverage:
        if item.section == section:
            return item
    raise AssertionError(f"missing section coverage for {section}")


SOURCE = """
from dataclasses import dataclass as record
from enum import StrEnum
from typing import Literal, NamedTuple, NotRequired, Protocol, Required, TypedDict
from pydantic import BaseModel, Field

Mode = Literal["batch", "stream"]

@record(frozen=True)
class Job:
    name: str
    retries: int = 3
    metadata: dict[str, object] = Field(default_factory=dict)

class Request(TypedDict, total=False):
    optional: str
    required: Required[int]
    override: NotRequired[bool]

class Response(BaseModel):
    result: str
    detail: str | None = None

class Pair(NamedTuple):
    left: int
    right: int

class Service(Protocol):
    name: str

class State(StrEnum):
    READY = "ready"
    FAILED = "failed"
"""


def test_contract_declarations_are_typed_frozen_and_deterministic() -> None:
    declarations = discover_contract_declarations(SOURCE, "sample.contracts")
    assert declarations == discover_contract_declarations(SOURCE, "sample.contracts")
    assert [(item.qualified_name, item.kind) for item in declarations] == [
        ("Job", DeclarationKind.DATACLASS),
        ("Mode", DeclarationKind.LITERAL),
        ("Pair", DeclarationKind.NAMED_TUPLE),
        ("Request", DeclarationKind.TYPED_DICT),
        ("Response", DeclarationKind.PYDANTIC_MODEL),
        ("Service", DeclarationKind.PROTOCOL),
        ("State", DeclarationKind.ENUM),
    ]
    with pytest.raises(FrozenInstanceError):
        declarations[0].qualified_name = "Changed"  # type: ignore[misc]


def test_contract_members_preserve_requiredness_defaults_and_normalized_types() -> None:
    declarations = {
        item.qualified_name: item
        for item in discover_contract_declarations(SOURCE, "sample.contracts")
    }
    job = {item.name: item for item in declarations["Job"].members}
    assert job["name"].requiredness is InternalRequirednessKind.ALWAYS
    assert job["name"].default.kind is InternalDefaultKind.ABSENT
    assert job["retries"].requiredness is InternalRequirednessKind.OPTIONAL
    assert job["retries"].default.canonical_json == "3"
    assert job["metadata"].default.kind is InternalDefaultKind.FACTORY
    assert job["metadata"].default.detail == "dict"
    assert job["metadata"].type_fact.kind is InternalTypeKind.MAPPING

    request = {item.name: item for item in declarations["Request"].members}
    assert request["optional"].requiredness is InternalRequirednessKind.OPTIONAL
    assert request["required"].requiredness is InternalRequirednessKind.ALWAYS
    assert request["override"].requiredness is InternalRequirednessKind.OPTIONAL

    response = {item.name: item for item in declarations["Response"].members}
    assert response["detail"].type_fact.kind is InternalTypeKind.OPTIONAL
    assert response["detail"].default.kind is InternalDefaultKind.EXPLICIT_NULL


def test_annotation_normalization_retains_variants_and_rejects_execution() -> None:
    annotation = normalize_annotation(__import__("ast").parse("str | int | None", mode="eval").body)
    assert annotation.kind is InternalTypeKind.UNION
    assert [item.name for item in annotation.arguments] == ["None", "int", "str"]

    malicious = (
        "from pydantic import BaseModel\n"
        "class Bad(BaseModel):\n    value: __import__('os').system('false')\n"
    )
    declarations = discover_contract_declarations(malicious, "sample.bad")
    assert declarations[0].members[0].type_fact.kind is InternalTypeKind.NAMED

    unresolved_literal = normalize_annotation(
        __import__("ast").parse("Literal[UNKNOWN]", mode="eval").body,
        {"Literal": "typing.Literal"},
    )
    assert unresolved_literal.kind is InternalTypeKind.UNKNOWN
    assert unresolved_literal.literal_json == ()


def test_unowned_lookalike_bases_and_decorators_are_not_contracts() -> None:
    declarations = discover_contract_declarations(
        "def dataclass(value):\n    return value\n"
        "class BaseModel:\n    pass\n"
        "@dataclass\nclass LocalRecord:\n    value: str\n"
        "class LocalModel(BaseModel):\n    value: str\n",
        "sample.lookalikes",
    )
    assert declarations == ()


def test_malformed_source_is_not_a_successful_empty_inventory() -> None:
    with pytest.raises(SyntaxError):
        discover_contract_declarations("class Broken(:\n", "sample.broken")


def test_duplicate_declarations_remain_visible_for_report_validation() -> None:
    source = (
        "from dataclasses import dataclass\n"
        "@dataclass\nclass Item:\n    first: str\n"
        "@dataclass\nclass Item:\n    second: int\n"
    )
    declarations = discover_contract_declarations(source, "sample.duplicate")
    assert [item.qualified_name for item in declarations] == ["Item", "Item"]
    lineages = discover_class_lineages(source, "sample.duplicate")
    assert len(merge_inherited_contract_members(declarations, lineages)) == 2


def test_local_inherited_contract_kinds_resolve_without_importing_code() -> None:
    base_source = "from pydantic import BaseModel\nclass ContractBase(BaseModel):\n    key: str\n"
    child_source = "from .base import ContractBase\nclass Request(ContractBase):\n    value: int\n"
    lineages = (
        *discover_class_lineages(base_source, "package.base"),
        *discover_class_lineages(child_source, "package.child"),
    )
    resolved = resolve_contract_kinds(lineages)
    assert dict(resolved)["package.child.Request"] is DeclarationKind.PYDANTIC_MODEL
    declarations = discover_contract_declarations(
        child_source,
        "package.child",
        resolved_kinds=resolved,
    )
    assert declarations[0].kind is DeclarationKind.PYDANTIC_MODEL
    assert declarations[0].base_names == ("package.base.ContractBase",)


def test_functional_named_tuple_and_typed_dict_forms_are_retained() -> None:
    declarations = discover_contract_declarations(
        "from typing import NamedTuple, TypedDict\n"
        'Point = NamedTuple("Point", [("x", int), ("y", int)])\n'
        'Payload = TypedDict("Payload", {"name": str, "count": int}, total=False)\n',
        "sample.functional",
    )
    by_name = {item.qualified_name: item for item in declarations}
    assert by_name["Point"].kind is DeclarationKind.NAMED_TUPLE
    assert [item.name for item in by_name["Point"].members] == ["x", "y"]
    assert by_name["Payload"].kind is DeclarationKind.TYPED_DICT
    assert all(
        item.requiredness is InternalRequirednessKind.OPTIONAL
        for item in by_name["Payload"].members
    )


def test_serialization_uses_require_exact_contract_kind_and_annotation_binding() -> None:
    source = (
        "from dataclasses import asdict, dataclass\n"
        "from pydantic import BaseModel\n"
        "from typing import NamedTuple\n"
        "class Model(BaseModel):\n    value: str\n"
        "@dataclass\nclass Record:\n    value: str\n"
        "class Pair(NamedTuple):\n    left: int\n    right: int\n"
        "class Lookalike:\n    def model_dump(self): ...\n"
        "def encode(model: Model, record: Record, pair: Pair, other: Lookalike):\n"
        "    model.model_dump_json()\n"
        "    asdict(record)\n"
        "    pair._asdict()\n"
        "    other.model_dump()\n"
        "Model.model_validate_json('{}')\n"
    )
    declarations = discover_contract_declarations(source, "sample.serialization")
    contract_kinds = tuple(
        (f"{item.module}.{item.qualified_name}", item.kind) for item in declarations
    )
    uses = discover_contract_uses(source, "sample.serialization", contract_kinds)
    serialization = [item for item in uses if item.serialization_format is not None]
    assert [
        (item.contract_identity, item.direction.value, item.serialization_format)
        for item in serialization
    ] == [
        ("sample.serialization.Model", "producer", "json"),
        ("sample.serialization.Model", "consumer", "json"),
        ("sample.serialization.Pair", "consumer", "mapping"),
        ("sample.serialization.Record", "consumer", "mapping"),
    ]
    assert all("Lookalike" not in item.contract_identity for item in uses)


def test_contract_receiver_aliases_are_straight_line_and_fail_closed() -> None:
    source = (
        "from pydantic import BaseModel\n"
        "class Model(BaseModel):\n    value: str\n"
        "def encode(model: Model, other: object, flag: bool):\n"
        "    alias = model\n"
        "    second = alias\n"
        "    second.model_dump()\n"
        "    alias = other\n"
        "    alias.model_dump()\n"
        "    unpacked = model\n"
        "    unpacked, ignored = other, None\n"
        "    unpacked.model_dump()\n"
        "    created = Model(value='x')\n"
        "    created.model_dump()\n"
        "    validated = Model.model_validate({})\n"
        "    validated.model_dump_json()\n"
        "    stable = model\n"
        "    if flag:\n"
        "        stable.model_dump_json()\n"
        "    maybe = model\n"
        "    if flag:\n"
        "        maybe = other\n"
        "    maybe.model_dump()\n"
        "    if flag:\n"
        "        branch = model\n"
        "        branch.model_dump()\n"
        "    if flag:\n"
        "        joined = model\n"
        "    else:\n"
        "        joined = Model(value='x')\n"
        "    joined.model_dump_json()\n"
        "    if flag:\n"
        "        divergent = model\n"
        "    else:\n"
        "        divergent = other\n"
        "    divergent.model_dump()\n"
    )
    declarations = discover_contract_declarations(source, "sample.aliases")
    contract_kinds = tuple(
        (f"{item.module}.{item.qualified_name}", item.kind) for item in declarations
    )
    uses = discover_contract_uses(source, "sample.aliases", contract_kinds)
    serialization = [item for item in uses if item.serialization_format is not None]
    assert len(serialization) == 7
    assert sum(item.direction.value == "producer" for item in serialization) == 1
    assert sum(item.direction.value == "consumer" for item in serialization) == 6
    assert sum(item.serialization_format == "json" for item in serialization) == 3
    assert sum(item.serialization_format == "mapping" for item in serialization) == 4


def test_contract_inventory_builds_referentially_valid_deterministic_report() -> None:
    project = FIXTURES / "contracts_project"
    context = discover_context(project, load_project_config(project))
    first = scan_contract_inventory(context)
    second = scan_contract_inventory(context)
    assert first.schema_version == "1.0.0"
    assert first.command.name == "contracts"
    assert first.completeness is Completeness.COMPLETE
    contracts_section = _section_coverage(first, "contracts")
    assert contracts_section.state is SectionState.COMPLETE
    assert first.summary.contracts == 9
    assert first.summary.subjects == 28
    assert {item.kind.value for item in first.contracts} == {
        "dataclass",
        "named-tuple",
        "pydantic-model",
        "typed-dict",
    }
    assert all(
        any(item.direction.value == "definition" for item in contract.boundaries)
        for contract in first.contracts
    )
    assert render_json(first) == render_json(second)
    assert "/Users/" not in render_json(first)
    contracts_by_symbol = {
        subject.logical_name: contract
        for subject in first.subjects
        for contract in first.contracts
        if contract.defining_subject_id == subject.subject_id
    }
    assert [item.name for item in contracts_by_symbol["package.contracts:Response"].members] == [
        "accepted",
        "message",
        "request_id",
        "schema_version",
    ]
    response_contract = contracts_by_symbol["package.contracts:Response"]
    assert response_contract.schema_versions == ("v1",)
    assert response_contract.serialization_formats == ("json", "mapping")
    assert len(response_contract.serialization_sites) == 4
    assert contracts_by_symbol["package.contracts:ArchivedJob"].serialization_formats == (
        "mapping",
    )
    assert contracts_by_symbol["package.functional:Point"].serialization_formats == ("mapping",)
    assert "serialization: json, mapping; schema versions: v1" in render_markdown(first)
    assert [item.name for item in contracts_by_symbol["package.contracts:Request"].members] == [
        "operation",
        "source",
        "timeout",
    ]
    assert [item.name for item in contracts_by_symbol["package.contracts:ArchivedJob"].members] == [
        "archive",
        "created",
    ]
    response_boundaries = contracts_by_symbol["package.contracts:Response"].boundaries
    assert {item.boundary_class for item in response_boundaries} == {
        BoundaryClass.MODULE_LOCAL,
        BoundaryClass.CROSS_SUBSYSTEM_INTERNAL,
        BoundaryClass.TEST_QUALIFICATION_EVIDENCE,
    }
    assert {item.direction.value for item in response_boundaries} == {
        "consumer",
        "definition",
        "producer",
    }
    request_boundaries = contracts_by_symbol["package.contracts:Request"].boundaries
    assert any(
        item.boundary_class is BoundaryClass.CROSS_SUBSYSTEM_INTERNAL
        and item.direction.value == "consumer"
        for item in request_boundaries
    )


def test_authoritative_contract_json_and_markdown_goldens() -> None:
    project = FIXTURES / "contract_golden"
    report = scan_contract_inventory(discover_context(project, AnalysisConfig()))
    golden_json = (GOLDEN / "contract-report.json").read_text(encoding="utf-8")
    golden_report = load_json(golden_json)
    assert isinstance(golden_report, AnalysisReport)

    def stable_payload(value: AnalysisReport) -> dict[str, object]:
        payload = value.model_dump(mode="json")
        payload["run_identity_sha256"] = "<environment-bound>"
        environment = payload["environment"]
        assert isinstance(environment, dict)
        environment["python_version"] = "<python-version>"
        providers = payload["providers"]
        assert isinstance(providers, list)
        for provider in providers:
            assert isinstance(provider, dict)
            if provider["provider_id"] in {"native-ast", "native-contracts"}:
                provider["version"] = "<python-version>"
        return payload

    assert stable_payload(report) == stable_payload(golden_report)
    if report.environment == golden_report.environment:
        assert render_json(report) == golden_json
    assert render_markdown(report) == (GOLDEN / "contract-report.md").read_text(encoding="utf-8")
    for forbidden in ("/Users/", "/Volumes/", "/private/var/", "/tmp/"):
        assert forbidden not in golden_json


def test_unclassified_cross_module_boundary_is_explicit_partial_evidence() -> None:
    context = discover_context(FIXTURES / "contracts_project", AnalysisConfig())
    report = scan_contract_inventory(context)
    assert report.completeness is Completeness.PARTIAL
    contracts_section = _section_coverage(report, "contracts")
    assert contracts_section.state is SectionState.PARTIAL
    assert contracts_section.unresolved > 0
    assert any(
        boundary.boundary_class is BoundaryClass.UNCLASSIFIED_INTERNAL
        for contract in report.contracts
        for boundary in contract.boundaries
    )
    assert any(item.code == "contract-boundary-unclassified" for item in report.diagnostics)


def test_contract_inventory_preserves_partial_source_failure() -> None:
    context = discover_context(FIXTURES / "malformed", AnalysisConfig())
    report = scan_contract_inventory(context)
    assert report.completeness is Completeness.PARTIAL
    contracts_section = _section_coverage(report, "contracts")
    assert contracts_section.state is SectionState.PARTIAL
    assert contracts_section.failed == 1
    assert report.providers[-1].state.value == "partial"
