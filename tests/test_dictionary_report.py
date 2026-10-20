from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.config import AnalysisConfig, ContractBoundaryRule, load_project_config
from code_analysis.models import Completeness, ProviderState
from code_analysis.project import discover_context
from code_analysis.report_models import (
    AnalysisReport,
    BoundaryClass,
    CandidateKind,
    ContractKind,
    DictionaryPredicate,
    SectionState,
)
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.reports.markdown import render_markdown
from code_analysis.service import scan


def test_empty_string_dictionary_keys_survive_report_round_trip(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        "def read(payload):\n"
        '    value = {"": 1}\n'
        '    payload[""] = value[""]\n'
        '    return payload.get("", 0)\n',
        encoding="utf-8",
    )
    report = scan(discover_context(tmp_path, AnalysisConfig()))
    assert any(item.key_repr == "" for item in report.dictionary_observations)
    assert load_json(render_json(report)) == report


def test_unified_scan_emits_typed_dictionary_shapes_flows_and_unresolved_evidence() -> None:
    context = discover_context(FIXTURES / "dictionary_project", AnalysisConfig())
    report = scan(context)
    assert report == scan(context)
    assert report.completeness is Completeness.PARTIAL
    assert report.summary.dictionary_shapes == 3
    assert report.summary.dictionary_observations > 0
    assert report.summary.flow_edges >= 2
    assert report.summary.unresolved_edges == 1
    assert report.summary.data_structures == report.summary.dictionary_shapes
    sections = {item.section: item for item in report.section_coverage}
    assert sections["dictionary-shapes"].state.value == "partial"
    assert sections["dictionary-shapes"].unresolved > 0
    dictionary_provider = next(
        item for item in report.providers if item.provider_id == "native-dictionary"
    )
    assert dictionary_provider.state is ProviderState.PARTIAL
    assert any(len(shape.variants) == 2 for shape in report.dictionary_shapes)
    assert any(shape.lifecycle_states for shape in report.dictionary_shapes)
    assert any(
        variant.discriminator for shape in report.dictionary_shapes for variant in shape.variants
    )
    assert all(edge.source_id != edge.target_id for edge in report.flow_edges)
    assert all(edge.effect_on_completeness for edge in report.unresolved_edges)
    assert all(item.evidence for item in report.data_structures)
    structure_provider = next(
        item for item in report.providers if item.provider_id == "native-structures"
    )
    assert structure_provider.state is ProviderState.SUCCESS


def test_dictionary_report_is_portable_deterministic_and_strictly_reloadable() -> None:
    context = discover_context(FIXTURES / "dictionary_project", AnalysisConfig())
    first = render_json(scan(context))
    second = render_json(scan(context))
    assert first == second
    assert render_json(load_json(first)) == first
    for forbidden in ("/Users/", "/Volumes/", "/private/var/", "/tmp/"):
        assert forbidden not in first


def test_dictionary_flow_bound_is_visible_in_report_provider_and_cli(capsys) -> None:
    from code_analysis.cli import ExitCode, main

    target = FIXTURES / "flow_bounds"
    context = discover_context(target, AnalysisConfig())
    report = scan(context)
    section = next(item for item in report.section_coverage if item.section == "dictionary-shapes")
    assert section.truncated > 0
    assert section.state is SectionState.PARTIAL
    assert "dictionary-flow-truncated" in section.diagnostic_codes
    diagnostic = next(
        item for item in report.diagnostics if item.code == "dictionary-flow-truncated"
    )
    assert str(section.truncated) in diagnostic.message
    provider = next(item for item in report.providers if item.provider_id == "native-dictionary")
    assert diagnostic in provider.diagnostics
    assert provider.state is ProviderState.PARTIAL
    assert report.completeness is Completeness.PARTIAL
    assert render_json(load_json(render_json(report))) == render_json(report)
    assert render_json(scan(context)) == render_json(report)
    assert main(["scan", str(target), "--json-compact", "--no-workspace"]) == ExitCode.ENODATA
    output = capsys.readouterr()
    assert "warning: dictionary-flow-truncated:" in output.err
    assert load_json(output.out).completeness is Completeness.PARTIAL


def test_unified_scan_separates_structural_and_operational_validation() -> None:
    context = discover_context(FIXTURES / "validation_project", AnalysisConfig())
    report = scan(context)

    assert report.summary.validation_patterns == 10
    validation_findings = [item for item in report.findings if item.category == "validation-sprawl"]
    assert len(validation_findings) == 2
    pattern = next(item for item in report.validation_patterns if len(item.sites) == 2)
    assert pattern.classification == "structural"
    assert pattern.rule == "dictionary key absent: literal-string:item_id"
    assert len(pattern.subject_ids) == 2
    assert len(pattern.sites) == 2
    assert all("path" not in item.observation for item in pattern.sites)
    assert {item.section: item for item in report.section_coverage}[
        "validation"
    ].state is SectionState.COMPLETE
    validation_provider = next(
        item for item in report.providers if item.provider_id == "native-validation"
    )
    assert validation_provider.state is ProviderState.SUCCESS
    assert "operational-path-validation" in validation_provider.observations
    structure_provider = next(
        item for item in report.providers if item.provider_id == "native-structures"
    )
    assert structure_provider.state is ProviderState.SUCCESS
    candidate_provider = next(
        item for item in report.providers if item.provider_id == "native-candidates"
    )
    assert candidate_provider.state is ProviderState.SUCCESS
    assert "## Validation patterns" in render_markdown(report)
    assert "2 sites across 2 subjects" in render_markdown(report)
    assert {
        "duplication.validation-idiom",
        "validation-divergence",
        "validation-sprawl-repeated",
    } <= {item.rule_id for item in report.findings}
    assert {item.predicate for item in report.dictionary_observations if item.predicate} == {
        DictionaryPredicate.ABSENT,
        DictionaryPredicate.PRESENT,
        DictionaryPredicate.TRUTHY,
    }
    assert {item.rule for item in report.validation_patterns} >= {
        "dictionary keys not equal to: [enabled,item_id]",
        "dictionary key value is not instance of: literal-string:item_id:str",
        "dictionary key value not-equals: literal-string:schema_version:[2]",
        'dictionary key value not-in: literal-string:state:["complete","ready"]',
    }
    operational = [
        item for item in report.validation_patterns if item.classification == "operational"
    ]
    assert {item.rule for item in operational} == {
        "filesystem path exists",
        "filesystem path type checked: file",
    }
    assert all(item.limitations for item in operational)
    exists_pattern = next(item for item in operational if item.rule == "filesystem path exists")
    assert len(exists_pattern.sites) == 2
    known_subjects = {item.subject_id for item in report.subjects}
    assert all(set(item.subject_ids) <= known_subjects for item in operational)
    fail_open = [item for item in report.validation_patterns if item.fail_open]
    assert len(fail_open) == 1
    assert fail_open[0].rule == "dictionary key absent: literal-string:warning"
    assert fail_open[0].classification == "structural"
    assert "runtime acceptance remains unmeasured" in fail_open[0].limitations[0]

    dangling = report.model_dump()
    dangling["validation_patterns"][0]["subject_ids"] = ("subject-" + "f" * 20,)
    with pytest.raises(ValidationError, match=r"validation .* references unknown IDs"):
        AnalysisReport.model_validate(dangling)

    disabled = report.model_dump()
    validation_section = next(
        item for item in disabled["section_coverage"] if item["section"] == "validation"
    )
    validation_section["state"] = SectionState.DISABLED
    validation_section["parsed"] = 0
    with pytest.raises(
        ValidationError,
        match="disabled validation section cannot contain records",
    ):
        AnalysisReport.model_validate(disabled)


def test_unified_scan_emits_referentially_valid_data_structure_topology() -> None:
    context = discover_context(FIXTURES / "structure_project", AnalysisConfig())
    report = scan(context)
    subjects = {item.subject_id: item for item in report.subjects}

    assert report.summary.data_structures == 2
    assert {item.section: item for item in report.section_coverage}[
        "data-structures"
    ].state is SectionState.COMPLETE
    module_record = next(item for item in report.data_structures if item.scope.value == "module")
    local_record = next(item for item in report.data_structures if item.scope.value == "local")
    assert module_record.growth_indicators == (
        "module-scope mutable mapping receives writes; measure boundedness",
    )
    assert local_record.ordering_assumptions == ("exact state-write order observed",)
    assert len(local_record.alias_subject_ids) == 1
    assert len(local_record.conversion_flow_ids) == 2
    assert len(local_record.serializer_subject_ids) == 1
    conversion_flows = {
        item.flow_id: item
        for item in report.flow_edges
        if item.flow_id in local_record.conversion_flow_ids
    }
    assert {item.kind.value for item in conversion_flows.values()} == {"serialization"}
    assert {subjects[item.target_id].logical_name for item in conversion_flows.values()} == {
        "json.dumps"
    }
    assert local_record.persistence_boundary_ids == ()
    conversion_finding = next(
        item for item in report.findings if item.rule_id == "conversion-churn-repeated"
    )
    assert conversion_finding.affected_structure_ids == (local_record.structure_id,)
    assert "runtime frequency" in conversion_finding.limitations[0]
    assert subjects[local_record.alias_subject_ids[0]].logical_name.endswith(":alias")
    assert all(item.owner_subject_id in subjects for item in report.data_structures)
    structure_provider = next(
        item for item in report.providers if item.provider_id == "native-structures"
    )
    assert structure_provider.state is ProviderState.SUCCESS
    assert "## Data structures" in render_markdown(report)

    rendered = render_json(report)
    assert render_json(load_json(rendered)) == rendered
    for forbidden in ("/Users/", "/Volumes/", "/private/var/", "/tmp/"):
        assert forbidden not in rendered

    dangling = report.model_dump()
    local_index = next(
        index for index, item in enumerate(report.data_structures) if item.scope.value == "local"
    )
    dangling["data_structures"][local_index]["alias_subject_ids"] = ("subject-" + "f" * 20,)
    with pytest.raises(ValidationError, match=r"structure .* references unknown IDs"):
        AnalysisReport.model_validate(dangling)


def test_configured_dictionary_persistence_is_referential_and_fail_closed() -> None:
    project = FIXTURES / "persistence_project"
    report = scan(discover_context(project, load_project_config(project)))
    dictionary_contract = next(
        item for item in report.contracts if item.kind is ContractKind.DICTIONARY
    )
    persisted = tuple(
        boundary
        for boundary in dictionary_contract.boundaries
        if boundary.boundary_class is BoundaryClass.PERSISTED_DOCUMENT
    )
    structure = report.data_structures[0]

    assert len(persisted) == 1
    assert structure.persistence_boundary_ids == (persisted[0].boundary_id,)
    assert len(structure.conversion_flow_ids) == 2
    assert dictionary_contract.serialization_formats == ("json",)
    assert "successful runtime I/O" in persisted[0].limitations[0]
    assert report.completeness is Completeness.COMPLETE

    conflicting = AnalysisConfig(
        contract_boundaries=tuple(
            sorted(
                (
                    ContractBoundaryRule(
                        contract_pattern="package.store:save:payload",
                        site_pattern="json.dump",
                        boundary_class=BoundaryClass.PERSISTED_DOCUMENT,
                    ),
                    ContractBoundaryRule(
                        contract_pattern="package.store:save:payload",
                        site_pattern="json.dump",
                        boundary_class=BoundaryClass.HTTP_RESPONSE,
                    ),
                ),
                key=lambda item: item.identity(),
            )
        )
    )
    conflict_report = scan(discover_context(project, conflicting))
    assert not any(item.kind is ContractKind.DICTIONARY for item in conflict_report.contracts)
    assert conflict_report.data_structures[0].persistence_boundary_ids == ()
    assert conflict_report.completeness is Completeness.PARTIAL
    assert any(item.code == "dictionary-boundary-conflict" for item in conflict_report.diagnostics)
    conflict_provider = next(
        item for item in conflict_report.providers if item.provider_id == "native-dictionary"
    )
    assert conflict_provider.state is ProviderState.PARTIAL


def test_representation_candidates_remain_evidenced_review_labels() -> None:
    dictionary_report = scan(discover_context(FIXTURES / "dictionary_project", AnalysisConfig()))
    assert dictionary_report.summary.candidate_classifications == 2
    assert {item.candidate for item in dictionary_report.candidate_classifications} == {
        CandidateKind.TYPED_DICT,
    }
    assert all(item.supporting_evidence for item in dictionary_report.candidate_classifications)
    assert all(item.limitations for item in dictionary_report.candidate_classifications)

    model_report = scan(discover_context(FIXTURES / "candidates_project", AnalysisConfig()))
    candidate = model_report.candidate_classifications[0]
    assert candidate.candidate is CandidateKind.PYDANTIC_MODEL
    assert candidate.confidence.value == "inferred"
    assert "not a migration instruction" in " ".join(candidate.limitations)
    assert "## Representation candidates" in render_markdown(model_report)


def test_unresolved_mapping_topology_is_not_a_representation_candidate(tmp_path: Path) -> None:
    (tmp_path / "dynamic.py").write_text(
        "def build(key: str):\n"
        "    payload = {}\n"
        "    payload[key] = 1\n"
        "    return payload\n",
        encoding="utf-8",
    )
    report = scan(discover_context(tmp_path, AnalysisConfig()))

    assert len(report.data_structures) == 1
    assert report.dictionary_shapes == ()
    assert report.candidate_classifications == ()
    assert report.completeness is Completeness.PARTIAL


def test_repeated_consumer_fallbacks_have_stable_sorted_identities() -> None:
    report = scan(discover_context(FIXTURES / "multiple_defaults_project", AnalysisConfig()))
    tools_fields = [
        field
        for shape in report.dictionary_shapes
        for variant in shape.variants
        for field in variant.fields
        if field.name == "tools" and len(field.consumer_fallbacks) > 1
    ]
    assert tools_fields
    identities = [item.default_id for item in tools_fields[0].consumer_fallbacks]
    assert identities == sorted(identities)
    assert len(identities) == len(set(identities))
