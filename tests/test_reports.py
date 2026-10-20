from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.config import AnalysisConfig, Strictness
from code_analysis.models import Completeness, Confidence
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.report_models import (
    AnalysisReport,
    AnalysisSubject,
    BoundaryClass,
    BoundaryDirection,
    CandidateClassification,
    CandidateKind,
    CloneFragment,
    CloneKind,
    ContractBoundary,
    ContractDefinition,
    ContractKind,
    Finding,
    FlowEdge,
    FlowKind,
    ResolutionState,
    SectionCoverage,
    SectionState,
    SubjectKind,
)
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.reports.markdown import render_markdown
from code_analysis.service import scan

GOLDEN = Path(__file__).parent / "golden"


def _section_index(payload: dict[str, object], section: str) -> int:
    coverage = payload["section_coverage"]
    for index, item in enumerate(coverage):
        section_name = item.section if isinstance(item, SectionCoverage) else item.get("section")
        if section_name == section:
            return index
    raise AssertionError(f"missing section coverage for {section}")


def _report_with_contract(ordinary_report: AnalysisReport) -> AnalysisReport:
    base = ordinary_report
    evidence = ordinary_report.findings[0].evidence[0]
    subjects = (
        AnalysisSubject(
            subject_id="subject-" + "1" * 20,
            kind=SubjectKind.SYMBOL,
            logical_name="alpha.a:orchestrate",
            module="alpha.a",
            evidence=(evidence,),
        ),
        AnalysisSubject(
            subject_id="subject-" + "2" * 20,
            kind=SubjectKind.SYMBOL,
            logical_name="alpha.b:consume",
            module="alpha.b",
            evidence=(evidence,),
        ),
    )
    contract = ContractDefinition(
        contract_id="contract-" + "1" * 20,
        kind=ContractKind.DATACLASS,
        defining_subject_id=subjects[0].subject_id,
        boundaries=(
            ContractBoundary(
                boundary_id="boundary-" + "1" * 20,
                boundary_class=BoundaryClass.CROSS_SUBSYSTEM_INTERNAL,
                direction=BoundaryDirection.PRODUCER,
                site_subject_id=subjects[0].subject_id,
                evidence=(evidence,),
                confidence=Confidence.EXACT,
            ),
            ContractBoundary(
                boundary_id="boundary-" + "2" * 20,
                boundary_class=BoundaryClass.HTTP_RESPONSE,
                direction=BoundaryDirection.CONSUMER,
                site_subject_id=subjects[1].subject_id,
                evidence=(evidence,),
                confidence=Confidence.RESOLVED,
            ),
        ),
        evidence=(evidence,),
    )
    payload = base.model_dump()
    payload["subjects"] = subjects
    payload["contracts"] = (contract,)
    for field in (
        "clone_fragments",
        "clone_clusters",
        "function_profiles",
        "function_commonalities",
        "state_resources",
        "side_effects",
        "responsibility_clusters",
        "architecture_estimates",
        "architecture_contract_suggestions",
        "refactor_proposals",
    ):
        payload[field] = ()
    payload["findings"] = tuple(
        item
        for item in base.findings
        if item.category
        not in {
            "duplication",
            "commonality",
            "state-coupling",
            "side-effect-ownership",
            "cohesion",
            "extraction-candidate",
            "module-combination",
            "architecture-contract",
        }
    )
    payload["static_call_occurrences"] = ()
    payload["static_call_coverage"] = ()
    payload["section_coverage"] = tuple(
        section.model_copy(
            update=(
                {"state": SectionState.COMPLETE, "parsed": 1}
                if section.section == "contracts"
                else (
                    {"state": SectionState.DISABLED, "parsed": 0}
                    if section.section
                    in {
                        "static-call-occurrences",
                        "clones",
                        "function-commonality",
                        "state-side-effects",
                        "cohesion-refactoring",
                    }
                    else {}
                )
            )
        )
        for section in base.section_coverage
    )
    payload["summary"] = {
        **base.summary.model_dump(),
        "subjects": 2,
        "contracts": 1,
        "clone_fragments": 0,
        "clone_clusters": 0,
        "function_profiles": 0,
        "function_commonalities": 0,
        "state_resources": 0,
        "side_effects": 0,
        "responsibility_clusters": 0,
        "architecture_estimates": 0,
        "architecture_contract_suggestions": 0,
        "refactor_proposals": 0,
        "static_call_occurrences": 0,
        "static_calls_observed": 0,
        "static_calls_omitted": 0,
        "findings": len(payload["findings"]),
    }
    payload["signal_quality"] = {}
    return AnalysisReport.model_validate(payload)


def test_json_is_byte_deterministic(ordinary_report) -> None:
    assert render_json(ordinary_report) == render_json(ordinary_report)
    rescanned = scan(discover_context(FIXTURES / "ordinary", AnalysisConfig()))
    assert render_json(ordinary_report) == render_json(rescanned)


def test_compact_json_round_trips(ordinary_report) -> None:
    compact = render_json(ordinary_report, compact=True)
    assert "\n  " not in compact
    assert load_json(compact) == ordinary_report


def test_compact_defaults_preserve_counts_health_identity_and_full_values(ordinary_report):
    from code_analysis.reports.json_report import canonical_json

    pretty = render_json(ordinary_report)
    compact = canonical_json(ordinary_report)
    full = json.loads(pretty)
    reduced = json.loads(compact)
    assert set(reduced) == set(full)
    for key in (
        "configuration",
        "summary",
        "environment",
        "providers",
        "section_coverage",
        "source_files",
        "signal_quality",
        "evidence_catalog",
        "source_bindings",
    ):
        assert reduced[key] == full[key]
    assert load_json(compact) == load_json(pretty) == ordinary_report
    assert json.dumps(load_json(compact).model_dump(mode="json"), sort_keys=True) == json.dumps(
        ordinary_report.model_dump(mode="json"), sort_keys=True
    )
    assert canonical_json(load_json(pretty)) == compact
    assert canonical_json(load_json(compact)) == compact
    # Default-valued optional metadata is elided, not required or nondefault data.
    omissions = []

    def compare(original, sparse):
        if isinstance(original, dict):
            omissions.extend(set(original) - set(sparse))
            for key in (
                "language",
                "languages",
                "kind",
                "state",
                "parse_state",
                "confidence",
                "completeness",
                "fan_in",
                "fan_out",
            ):
                if key in original:
                    assert sparse[key] == original[key]
            for key in sparse:
                compare(original[key], sparse[key])
        elif isinstance(original, list):
            assert len(original) == len(sparse)
            for left, right in zip(original, sparse, strict=True):
                compare(left, right)
        else:
            assert original == sparse

    compare(full, reduced)
    assert omissions
    assert len(compact) < len(
        json.dumps(full, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def test_compact_output_still_rejects_missing_required_symbol_data(ordinary_report):
    payload = json.loads(render_json(ordinary_report, compact=True))
    symbol = next(symbol for module in payload["modules"] for symbol in module.get("symbols", []))
    del symbol["name"]
    with pytest.raises(ValidationError):
        load_json(json.dumps(payload))


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "dangling", "hash", "language", "path"]
)
def test_report_source_bindings_fail_closed(ordinary_report, mutation: str) -> None:
    payload = json.loads(render_json(ordinary_report, compact=True))
    assert payload["source_bindings"]
    assert "span" not in payload["evidence_catalog"][0]
    assert "source_sha256" not in payload["evidence_catalog"][0]
    match mutation:
        case "missing":
            del payload["source_bindings"]
        case "duplicate":
            payload["source_bindings"].append(payload["source_bindings"][0])
        case "dangling":
            payload["evidence_catalog"][0]["binding_id"] = "binding-" + "f" * 20
        case "hash":
            payload["source_bindings"][0]["source_sha256"] = "a" * 64
        case "language":
            payload["source_bindings"][0]["language"] = "ruby"
        case "path":
            payload["source_bindings"][0]["logical_path"] = "/tmp/private.py"
    with pytest.raises(ValueError):
        load_json(json.dumps(payload))


def test_persisted_report_interns_and_validates_evidence(ordinary_report) -> None:
    payload = json.loads(render_json(ordinary_report, compact=True))
    catalog = payload["evidence_catalog"]
    identities = [item["evidence_id"] for item in catalog]
    assert identities == sorted(set(identities))
    assert "evidence" not in payload["subjects"][0]
    assert payload["subjects"][0]["evidence_ids"]

    dangling = json.loads(render_json(ordinary_report, compact=True))
    dangling["subjects"][0]["evidence_ids"] = ["evidence-" + "f" * 20]
    with pytest.raises(ValueError, match="dangling evidence references"):
        load_json(json.dumps(dangling))

    mismatched = json.loads(render_json(ordinary_report, compact=True))
    mismatched["evidence_catalog"][0]["observation"] += " changed"
    with pytest.raises(ValueError, match="identity mismatch"):
        load_json(json.dumps(mismatched))

    embedded = ordinary_report.model_dump(mode="json")
    embedded["evidence_catalog"] = []
    embedded["source_bindings"] = []
    with pytest.raises(ValueError, match="embedded persisted evidence is forbidden"):
        load_json(json.dumps(embedded))


def test_evidence_expansion_reuses_catalog_without_mutation(ordinary_report) -> None:
    from code_analysis.reports.json_report import _expand_payload

    persisted = json.loads(render_json(ordinary_report, compact=True))
    entry = persisted["evidence_catalog"][0]
    identity = entry.pop("evidence_id")
    before = json.dumps(entry, sort_keys=True)
    expanded = _expand_payload(
        {"evidence_ids": [identity, identity], "nested": {"counter_evidence_ids": [identity]}},
        {identity: entry},
    )
    assert expanded["evidence"][0] is expanded["evidence"][1] is entry
    assert expanded["nested"]["counter_evidence"][0] is entry
    assert json.dumps(entry, sort_keys=True) == before
    compact = render_json(ordinary_report, compact=True)
    assert render_json(load_json(compact), compact=True) == compact


def test_portable_report_has_no_host_or_external_absolute_paths(ordinary_report) -> None:
    output = render_json(ordinary_report)
    for forbidden in ("/Users/", "/Volumes/", "/tmp/", "/private/var/"):
        assert forbidden not in output


def test_golden_json_and_markdown() -> None:
    report = scan(discover_context(FIXTURES / "excluded", AnalysisConfig()))
    assert render_json(report) == (GOLDEN / "minimal-report.json").read_text(encoding="utf-8")
    assert render_markdown(report) == (GOLDEN / "minimal-report.md").read_text(encoding="utf-8")


def test_finding_limit_is_explicit_partial_state() -> None:
    config = AnalysisConfig.with_overrides(
        strictness=Strictness.STRICT,
        branches=0,
        function_loc=999,
        arguments=999,
        nesting=999,
        weighted_score=999,
        max_findings=1,
    )
    report = scan(discover_context(FIXTURES / "ordinary", config))
    assert report.completeness is Completeness.PARTIAL
    assert len(report.findings) == 1
    assert any(item.code == "findings-truncated" for item in report.diagnostics)


def test_authoritative_report_loads_without_rewriting(ordinary_report) -> None:
    rendered = render_json(ordinary_report)
    assert load_json(rendered) == ordinary_report
    assert render_json(load_json(rendered)) == rendered


def test_authoritative_report_supports_report_only_projection(ordinary_report) -> None:
    projection = build_projection(ordinary_report, ProjectionName.DEPENDENCIES)
    assert projection.inputs[0].report_schema_version == "1.0.0"
    assert projection.inputs[0].run_identity_sha256 == ordinary_report.run_identity_sha256


def test_report_is_strict_and_summary_counts_are_validated(ordinary_report) -> None:
    report = ordinary_report
    with pytest.raises(ValidationError):
        AnalysisReport.model_validate({**report.model_dump(), "unknown": True})
    with pytest.raises(ValidationError, match="summary counts"):
        AnalysisReport.model_validate(
            {
                **report.model_dump(),
                "summary": {**report.summary.model_dump(), "contracts": 1},
            }
        )
    with pytest.raises(ValidationError):
        SectionCoverage(section="contracts", state=SectionState.DISABLED, parsed=True)
    finding = ordinary_report.findings[0].model_dump()
    finding["category"] = "contract-conflict"
    contract_finding = Finding.model_validate(
        {
            **finding,
            "confidence": finding["evidence"][0]["confidence"],
            "completeness": ordinary_report.completeness,
            "subject_ids": ("subject-" + "1" * 20,),
            "limitations": ("Review boundary compatibility.",),
            "likely_validation_needs": ("Run contract compatibility tests.",),
        }
    )
    assert contract_finding.category == "contract-conflict"
    with pytest.raises(ValidationError, match="affected identity"):
        Finding.model_validate(
            {
                **contract_finding.model_dump(),
                "subject_ids": (),
            }
        )
    with pytest.raises(ValidationError, match="likely validation needs"):
        Finding.model_validate(
            {
                **contract_finding.model_dump(),
                "likely_validation_needs": (),
            }
        )
    with pytest.raises(ValidationError, match="heuristic evidence"):
        Finding.model_validate(
            {
                **contract_finding.model_dump(),
                "severity": "error",
                "confidence": Confidence.HEURISTIC,
            }
        )
    with pytest.raises(ValidationError):
        Finding.model_validate(finding)


def test_phase_three_report_references_fail_closed(ordinary_report) -> None:
    payload = ordinary_report.model_dump()
    evidence = ordinary_report.findings[0].evidence[0]
    payload["clone_fragments"] = (
        CloneFragment(
            fragment_id="clone-fragment-" + "1" * 20,
            kind=CloneKind.EXACT_TEXT,
            subject_id="subject-" + "f" * 20,
            fingerprint_sha256="1" * 64,
            structural_signature="body-lines:v1",
            nonblank_lines=6,
            token_count=20,
            ast_node_count=12,
            statement_count=4,
            evidence=(evidence,),
        ),
    )
    payload["summary"] = {**ordinary_report.summary.model_dump(), "clone_fragments": 1}
    payload["section_coverage"] = tuple(
        section.model_copy(
            update=(
                {"state": SectionState.COMPLETE, "parsed": 1} if section.section == "clones" else {}
            )
        )
        for section in ordinary_report.section_coverage
    )
    with pytest.raises(ValidationError, match="unknown IDs"):
        AnalysisReport.model_validate(payload)


def test_contract_supports_multiple_evidenced_boundaries(ordinary_report) -> None:
    report = _report_with_contract(ordinary_report)
    rendered = render_json(report)
    assert render_json(load_json(rendered)) == rendered
    assert "/Users/" not in rendered
    assert [boundary.boundary_class for boundary in report.contracts[0].boundaries] == [
        BoundaryClass.CROSS_SUBSYSTEM_INTERNAL,
        BoundaryClass.HTTP_RESPONSE,
    ]
    assert all(boundary.evidence for boundary in report.contracts[0].boundaries)
    payload = report.model_dump()
    payload["contracts"][0]["boundaries"][1]["site_subject_id"] = "subject-" + "f" * 20
    with pytest.raises(ValidationError, match="boundary sites references unknown IDs"):
        AnalysisReport.model_validate(payload)

    disabled = report.model_dump()
    contracts_index = _section_index(disabled, "contracts")
    disabled["section_coverage"][contracts_index]["state"] = SectionState.DISABLED
    disabled["section_coverage"][contracts_index]["parsed"] = 0
    with pytest.raises(ValidationError, match="disabled contracts section cannot contain records"):
        AnalysisReport.model_validate(disabled)


def test_report_enforces_flow_candidate_finding_and_subject_references(ordinary_report) -> None:
    report = _report_with_contract(ordinary_report)
    evidence = ordinary_report.findings[0].evidence[0]
    flow = FlowEdge(
        flow_id="flow-" + "1" * 20,
        source_id=report.subjects[0].subject_id,
        target_id=report.contracts[0].contract_id,
        kind=FlowKind.PRODUCER,
        resolution=ResolutionState.RESOLVED,
        evidence=(evidence,),
        confidence=Confidence.RESOLVED,
    )
    candidate = CandidateClassification(
        candidate_id="candidate-" + "1" * 20,
        subject_id=report.contracts[0].contract_id,
        candidate=CandidateKind.DATACLASS,
        supporting_evidence=(evidence,),
        counter_evidence=(evidence,),
        confidence=Confidence.INFERRED,
        limitations=("Review API compatibility.",),
    )
    finding = report.findings[0].model_copy(
        update={
            "subject_ids": (report.subjects[0].subject_id,),
            "affected_contract_ids": (report.contracts[0].contract_id,),
            "counter_evidence": (evidence,),
            "limitations": ("Static evidence only.",),
            "likely_validation_needs": ("Run contract compatibility tests.",),
        }
    )
    payload = report.model_dump()
    payload["flow_edges"] = (flow,)
    payload["candidate_classifications"] = (candidate,)
    payload["findings"] = (finding,)
    payload["summary"] = {
        **report.summary.model_dump(),
        "flow_edges": 1,
        "candidate_classifications": 1,
    }
    validated = AnalysisReport.model_validate(payload)
    assert validated.findings[0].counter_evidence
    assert validated.findings[0].likely_validation_needs

    broken_flow = validated.model_dump()
    broken_flow["flow_edges"][0]["target_id"] = "contract-" + "f" * 20
    with pytest.raises(ValidationError, match=r"flow .* references unknown IDs"):
        AnalysisReport.model_validate(broken_flow)

    broken_candidate = validated.model_dump()
    broken_candidate["candidate_classifications"][0]["subject_id"] = "shape-" + "f" * 20
    with pytest.raises(ValidationError, match=r"candidate .* references unknown IDs"):
        AnalysisReport.model_validate(broken_candidate)

    broken_finding = validated.model_dump()
    broken_finding["findings"][0]["affected_contract_ids"] = ("contract-" + "f" * 20,)
    with pytest.raises(ValidationError, match=r"finding .* references unknown IDs"):
        AnalysisReport.model_validate(broken_finding)

    duplicate_subject = validated.model_dump()
    duplicate_subject["subjects"] = (validated.subjects[0], validated.subjects[0])
    duplicate_subject["summary"]["subjects"] = 2
    with pytest.raises(ValidationError, match="subject_id records must be unique and sorted"):
        AnalysisReport.model_validate(duplicate_subject)


def test_unsupported_report_version_fails_closed(ordinary_report) -> None:
    payload = json.loads(render_json(ordinary_report, compact=True))
    payload["schema_version"] = "99.0.0"
    with pytest.raises(ValidationError):
        load_json(json.dumps(payload))
