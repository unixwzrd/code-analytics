from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FIXTURES

from code_analysis.config import AnalysisConfig, load_config_file, load_project_config
from code_analysis.models import Completeness
from code_analysis.project import discover_context
from code_analysis.projection_models import (
    GraphProjection,
    HierarchyProjection,
    MatrixProjection,
    ProjectionFindingCategory,
    ProjectionFindingSeverity,
    ReviewPacketProjection,
    TableProjection,
)
from code_analysis.projections import ProjectionBuildError, ProjectionName, build_projection
from code_analysis.projections._catalog import SelectionPolicy
from code_analysis.projections._registry import PROJECTION_REGISTRY, projection_spec
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.reports.markdown import render_markdown
from code_analysis.reports.projection_json import render_projection_json
from code_analysis.reports.projection_views import (
    ProjectionFormat,
    ProjectionRenderError,
    render_projection,
)
from code_analysis.service import scan, scan_contract_inventory, scan_refactor

GOLDEN = Path(__file__).parent / "golden"


def test_projection_registry_is_exhaustive_unique_and_policy_owned(monkeypatch) -> None:
    assert set(PROJECTION_REGISTRY) == set(ProjectionName)
    assert len(PROJECTION_REGISTRY) == len(ProjectionName)
    assert (
        PROJECTION_REGISTRY[ProjectionName.DEPENDENCY_NEIGHBORHOOD].selection_policy
        is SelectionPolicy.DEPENDENCY_NEIGHBORHOOD
    )
    bounded = {
        ProjectionName.STATIC_CALL_SITES,
        ProjectionName.CONTRACTS,
        ProjectionName.CONTRACT_BOUNDARIES,
        ProjectionName.PROPOSAL_GATES,
        ProjectionName.REFACTOR_SHORTLIST,
        ProjectionName.REVIEW_PACKET,
        ProjectionName.REFACTOR_PACKET,
        ProjectionName.DUPLICATE_FUNCTIONS,
        ProjectionName.CLONE_MEMBERSHIP,
        ProjectionName.BEHAVIOR_DIFFERENCES,
        ProjectionName.DUPLICATE_OWNERSHIP,
        ProjectionName.DUPLICATE_CALLERS_TESTS,
        ProjectionName.DIVERGENCE_REASONS,
        ProjectionName.CANONICAL_OWNER,
        ProjectionName.JAVASCRIPT_REFACTOR_PACKET,
        ProjectionName.ABSTRACTION_COST,
        ProjectionName.EXISTING_OWNER_ALTERNATIVES,
        ProjectionName.CANDIDATE_BEHAVIORAL_DIFFERENCES,
        ProjectionName.CROSS_MODULE_DUPLICATION,
        ProjectionName.INTERNAL_CONTRACT_RISKS,
        ProjectionName.PRODUCER_CONSUMER_SCHEMA_DRIFT,
        ProjectionName.FRONTEND_BACKEND_INTERFACE_MISMATCHES,
        ProjectionName.CANDIDATE_QUALIFICATION,
        ProjectionName.CANDIDATE_ARCHITECTURE_CHANGES,
    }
    assert {
        name
        for name, spec in PROJECTION_REGISTRY.items()
        if spec.selection_policy is SelectionPolicy.BOUNDED_REVIEW
    } == bounded
    assert (
        PROJECTION_REGISTRY[ProjectionName.SUBJECT_FINDINGS].selection_policy
        is SelectionPolicy.SUBJECT_FINDINGS
    )
    monkeypatch.delitem(PROJECTION_REGISTRY, ProjectionName.HEALTH)
    with pytest.raises(ProjectionBuildError, match="projection is not registered"):
        projection_spec(ProjectionName.HEALTH)


def test_all_r1_projection_json_is_deterministic_and_portable(ordinary_report) -> None:
    for name in ProjectionName:
        if name is ProjectionName.DEPENDENCY_NEIGHBORHOOD:
            first = build_projection(ordinary_report, name, selected_ids=("alpha.a",))
            second = build_projection(ordinary_report, name, selected_ids=("alpha.a",))
        elif name in {
            ProjectionName.CONTRACTS,
            ProjectionName.CONTRACT_BOUNDARIES,
            ProjectionName.CONTRACT_BOUNDARY_GRAPH,
        }:
            with pytest.raises(ProjectionBuildError, match="recorded contract evidence"):
                build_projection(ordinary_report, name)
            continue
        elif name in {
            ProjectionName.SHAPE_VARIANTS,
            ProjectionName.DICTIONARY_FLOW,
            ProjectionName.VALIDATION_DIVERGENCE,
            ProjectionName.OWNERSHIP_ALIAS_FLOW,
            ProjectionName.STRUCTURE_LIFECYCLE,
            ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
            ProjectionName.CONVERSION_GRAPH,
            ProjectionName.CONVERSION_CHURN,
            ProjectionName.REPRESENTATION_CANDIDATES,
            ProjectionName.TEST_IMPACT,
            ProjectionName.TEST_IMPACT_GRAPH,
            ProjectionName.GIT_CHURN,
            ProjectionName.CHANGE_COUPLING,
            ProjectionName.CHANGE_COUPLING_GRAPH,
            ProjectionName.GENERATION_CHANGES,
            ProjectionName.COVERAGE_FILES,
            ProjectionName.COVERAGE_MATRIX,
            ProjectionName.CANDIDATE_WORKLIST,
            ProjectionName.ABSTRACTION_COST,
            ProjectionName.EXISTING_OWNER_ALTERNATIVES,
            ProjectionName.OWNERSHIP_CONTRACT_FLOW,
        }:
            with pytest.raises(ProjectionBuildError, match="requires recorded"):
                build_projection(ordinary_report, name)
            continue
        elif name is ProjectionName.UNCOVERED_PROPOSAL_SUBJECTS:
            with pytest.raises(ProjectionBuildError, match="requires refactor proposals"):
                build_projection(ordinary_report, name)
            continue
        else:
            first = build_projection(ordinary_report, name)
            second = build_projection(ordinary_report, name)
        rendered = render_projection_json(first)
        assert first == second
        assert rendered == render_projection_json(second)
        payload = json.loads(rendered)
        assert payload["inputs"][0]["report_sha256"]
        for forbidden in ("/Users/", "/Volumes/", "/tmp/", "/private/var/"):
            assert forbidden not in rendered


def test_contract_boundary_matrix_uses_only_validated_report_evidence() -> None:
    project = FIXTURES / "contracts_project"
    scanned = scan_contract_inventory(discover_context(project, load_project_config(project)))
    report = load_json(render_json(scanned))
    projection = build_projection(report, ProjectionName.CONTRACT_BOUNDARIES)
    assert isinstance(projection, TableProjection)
    assert projection.record_count == 28
    assert projection.completeness is Completeness.COMPLETE
    contract_position = next(
        column.position for column in projection.columns if column.column_id == "contract"
    )
    format_position = next(
        column.position
        for column in projection.columns
        if column.column_id == "serialization-formats"
    )
    response_rows = [
        row
        for row in projection.rows
        if row.cells[contract_position].string_value == "package.contracts:Response"
    ]
    assert response_rows
    assert {row.cells[format_position].string_value for row in response_rows} == {"json, mapping"}
    for output_format in (
        ProjectionFormat.JSON,
        ProjectionFormat.JSON_COMPACT,
        ProjectionFormat.TSV,
        ProjectionFormat.MARKDOWN,
    ):
        rendered = render_projection(projection, output_format)
        assert rendered == render_projection(projection, output_format)
        assert "/Users/" not in rendered
    bounded = build_projection(report, ProjectionName.CONTRACT_BOUNDARIES, max_records=3)
    assert bounded.record_count == 3
    assert bounded.truncated
    assert bounded.completeness is Completeness.PARTIAL
    with pytest.raises(ProjectionBuildError, match="requires recorded contract evidence"):
        build_projection(
            report.model_copy(update={"contracts": ()}),
            ProjectionName.CONTRACT_BOUNDARIES,
        )
    golden_projection = build_projection(report, ProjectionName.CONTRACT_BOUNDARIES, max_records=5)
    assert render_projection(golden_projection, ProjectionFormat.TSV) == (
        GOLDEN / "projection-contract-boundaries.tsv"
    ).read_text(encoding="utf-8")
    assert render_projection(golden_projection, ProjectionFormat.MARKDOWN) == (
        GOLDEN / "projection-contract-boundaries.md"
    ).read_text(encoding="utf-8")


def test_contract_inventory_projection_preserves_structural_summary() -> None:
    project = FIXTURES / "contracts_project"
    scanned = scan_contract_inventory(discover_context(project, load_project_config(project)))
    report = load_json(render_json(scanned))
    projection = build_projection(report, ProjectionName.CONTRACTS)
    assert isinstance(projection, TableProjection)
    assert projection.record_count == 9
    positions = {column.column_id: column.position for column in projection.columns}
    response = next(
        row
        for row in projection.rows
        if row.cells[positions["contract"]].string_value == "package.contracts:Response"
    )
    assert response.cells[positions["member-count"]].integer_value == 4
    assert response.cells[positions["producer-default-count"]].integer_value == 2
    assert response.cells[positions["consumer-fallback-count"]].integer_value == 0
    assert response.cells[positions["serialization-formats"]].string_value == "json, mapping"
    assert response.cells[positions["schema-versions"]].string_value == "v1"
    bounded = build_projection(report, ProjectionName.CONTRACTS, max_records=2)
    assert bounded.record_count == 2
    assert bounded.truncated
    assert bounded.completeness is Completeness.PARTIAL
    with pytest.raises(ProjectionBuildError, match="requires recorded contract evidence"):
        build_projection(
            report.model_copy(update={"contracts": ()}),
            ProjectionName.CONTRACTS,
        )
    assert render_projection(projection, ProjectionFormat.TSV) == (
        GOLDEN / "projection-contracts.tsv"
    ).read_text(encoding="utf-8")
    assert render_projection(projection, ProjectionFormat.MARKDOWN) == (
        GOLDEN / "projection-contracts.md"
    ).read_text(encoding="utf-8")


def test_contract_boundary_graph_is_evidenced_endpoint_safe_and_bounded() -> None:
    project = FIXTURES / "contracts_project"
    scanned = scan_contract_inventory(discover_context(project, load_project_config(project)))
    report = load_json(render_json(scanned))
    projection = build_projection(report, ProjectionName.CONTRACT_BOUNDARY_GRAPH)
    assert isinstance(projection, GraphProjection)
    assert projection.record_count == 65
    assert projection.completeness is Completeness.COMPLETE
    node_ids = {node.node_id for node in projection.nodes}
    assert all(
        edge.source_id in node_ids and edge.target_id in node_ids for edge in projection.edges
    )
    assert {edge.kind for edge in projection.edges} == {
        "consumer",
        "definition",
        "producer",
    }
    bounded = build_projection(
        report,
        ProjectionName.CONTRACT_BOUNDARY_GRAPH,
        max_records=20,
    )
    assert isinstance(bounded, GraphProjection)
    assert bounded.record_count == 20
    assert bounded.truncated
    assert bounded.completeness is Completeness.PARTIAL
    bounded_node_ids = {node.node_id for node in bounded.nodes}
    assert all(
        edge.source_id in bounded_node_ids and edge.target_id in bounded_node_ids
        for edge in bounded.edges
    )
    for output_format in (
        ProjectionFormat.JSON,
        ProjectionFormat.JSON_COMPACT,
        ProjectionFormat.DOT,
        ProjectionFormat.MERMAID,
        ProjectionFormat.MARKDOWN,
    ):
        rendered = render_projection(bounded, output_format)
        assert rendered == render_projection(bounded, output_format)
        assert "/Users/" not in rendered
    assert render_projection(bounded, ProjectionFormat.DOT) == (
        GOLDEN / "projection-contract-boundary-graph.dot"
    ).read_text(encoding="utf-8")
    assert render_projection(bounded, ProjectionFormat.MERMAID) == (
        GOLDEN / "projection-contract-boundary-graph.mmd"
    ).read_text(encoding="utf-8")
    with pytest.raises(ProjectionBuildError, match="requires recorded contract evidence"):
        build_projection(
            report.model_copy(update={"contracts": ()}),
            ProjectionName.CONTRACT_BOUNDARY_GRAPH,
        )


def test_dictionary_shape_flow_and_unresolved_projections_use_saved_evidence_only() -> None:
    scanned = scan(discover_context(FIXTURES / "dictionary_project", AnalysisConfig()))
    report = load_json(render_json(scanned))

    variants = build_projection(report, ProjectionName.SHAPE_VARIANTS)
    assert isinstance(variants, TableProjection)
    assert variants.record_count == sum(len(shape.variants) for shape in report.dictionary_shapes)
    assert any(
        row.cells[6].string_value == "kind" and row.cells[7].string_value for row in variants.rows
    )
    assert render_projection(variants, ProjectionFormat.TSV) == (
        GOLDEN / "projection-shape-variants.tsv"
    ).read_text(encoding="utf-8")
    assert render_projection(variants, ProjectionFormat.MARKDOWN) == (
        GOLDEN / "projection-shape-variants.md"
    ).read_text(encoding="utf-8")

    flow = build_projection(report, ProjectionName.DICTIONARY_FLOW)
    assert isinstance(flow, GraphProjection)
    assert len(flow.edges) == report.summary.flow_edges
    node_ids = {node.node_id for node in flow.nodes}
    assert all(edge.source_id in node_ids and edge.target_id in node_ids for edge in flow.edges)
    assert render_projection(flow, ProjectionFormat.DOT) == (
        GOLDEN / "projection-dictionary-flow.dot"
    ).read_text(encoding="utf-8")
    assert render_projection(flow, ProjectionFormat.MERMAID) == (
        GOLDEN / "projection-dictionary-flow.mmd"
    ).read_text(encoding="utf-8")

    unresolved = build_projection(report, ProjectionName.UNRESOLVED_COVERAGE)
    assert isinstance(unresolved, TableProjection)
    assert unresolved.record_count == len(report.section_coverage) + len(report.unresolved_edges)
    assert render_projection(unresolved, ProjectionFormat.TSV) == (
        GOLDEN / "projection-unresolved-coverage.tsv"
    ).read_text(encoding="utf-8")
    assert render_projection(unresolved, ProjectionFormat.MARKDOWN) == (
        GOLDEN / "projection-unresolved-coverage.md"
    ).read_text(encoding="utf-8")

    for rendered in (
        render_projection(variants, ProjectionFormat.JSON),
        render_projection(flow, ProjectionFormat.JSON),
        render_projection(unresolved, ProjectionFormat.JSON),
    ):
        for forbidden in ("/Users/", "/Volumes/", "/private/var/", "/tmp/"):
            assert forbidden not in rendered


def test_validation_divergence_projection_uses_saved_evidence_only() -> None:
    scanned = scan(discover_context(FIXTURES / "validation_project", AnalysisConfig()))
    report = load_json(render_json(scanned))

    projection = build_projection(report, ProjectionName.VALIDATION_DIVERGENCE)
    assert isinstance(projection, TableProjection)
    assert projection.record_count == 10
    assert sum(bool(row.cells[6].boolean_value) for row in projection.rows) == 2
    assert sum(bool(row.cells[7].boolean_value) for row in projection.rows) == 1
    assert render_projection(projection, ProjectionFormat.TSV) == (
        GOLDEN / "projection-validation-divergence.tsv"
    ).read_text(encoding="utf-8")
    assert render_projection(projection, ProjectionFormat.MARKDOWN) == (
        GOLDEN / "projection-validation-divergence.md"
    ).read_text(encoding="utf-8")

    for rendered in (
        render_projection(projection, ProjectionFormat.JSON),
        render_projection(projection, ProjectionFormat.TSV),
        render_projection(projection, ProjectionFormat.MARKDOWN),
    ):
        for forbidden in ("/Users/", "/Volumes/", "/private/var/", "/tmp/"):
            assert forbidden not in rendered


def test_structure_projections_use_saved_evidence_only() -> None:
    scanned = scan(discover_context(FIXTURES / "structure_project", AnalysisConfig()))
    report = load_json(render_json(scanned))

    ownership = build_projection(report, ProjectionName.OWNERSHIP_ALIAS_FLOW)
    assert isinstance(ownership, GraphProjection)
    node_ids = {node.node_id for node in ownership.nodes}
    assert all(
        edge.source_id in node_ids and edge.target_id in node_ids for edge in ownership.edges
    )
    assert any(edge.kind == "alias" for edge in ownership.edges)
    assert render_projection(ownership, ProjectionFormat.DOT) == (
        GOLDEN / "projection-ownership-alias-flow.dot"
    ).read_text(encoding="utf-8")
    assert render_projection(ownership, ProjectionFormat.MERMAID) == (
        GOLDEN / "projection-ownership-alias-flow.mmd"
    ).read_text(encoding="utf-8")

    lifecycle = build_projection(report, ProjectionName.STRUCTURE_LIFECYCLE)
    assert isinstance(lifecycle, TableProjection)
    assert lifecycle.record_count == 2
    concurrency_position = next(
        item.position for item in lifecycle.columns if item.column_id == "concurrency"
    )
    assert any(
        "thread-lock context lexically guards mutation"
        in row.cells[concurrency_position].string_value
        for row in lifecycle.rows
    )
    assert render_projection(lifecycle, ProjectionFormat.TSV) == (
        GOLDEN / "projection-structure-lifecycle.tsv"
    ).read_text(encoding="utf-8")
    assert render_projection(lifecycle, ProjectionFormat.MARKDOWN) == (
        GOLDEN / "projection-structure-lifecycle.md"
    ).read_text(encoding="utf-8")

    conversions = build_projection(report, ProjectionName.CONVERSION_CHURN)
    assert isinstance(conversions, TableProjection)
    assert conversions.record_count == 2
    assert {row.cells[3].string_value for row in conversions.rows} == {"serialization"}
    assert render_projection(conversions, ProjectionFormat.TSV) == (
        GOLDEN / "projection-conversion-churn.tsv"
    ).read_text(encoding="utf-8")
    assert render_projection(conversions, ProjectionFormat.MARKDOWN) == (
        GOLDEN / "projection-conversion-churn.md"
    ).read_text(encoding="utf-8")

    for rendered in (
        render_projection(ownership, ProjectionFormat.JSON),
        render_projection(lifecycle, ProjectionFormat.JSON),
        render_projection(conversions, ProjectionFormat.JSON),
    ):
        for forbidden in ("/Users/", "/Volumes/", "/private/var/", "/tmp/"):
            assert forbidden not in rendered


def test_structure_lifecycle_retains_configured_persistence_from_saved_report() -> None:
    project = FIXTURES / "persistence_project"
    scanned = scan(discover_context(project, load_project_config(project)))
    report = load_json(render_json(scanned))
    projection = build_projection(report, ProjectionName.STRUCTURE_LIFECYCLE)
    assert isinstance(projection, TableProjection)
    persistence_position = next(
        item.position for item in projection.columns if item.column_id == "persistence-boundaries"
    )
    conversion_position = next(
        item.position for item in projection.columns if item.column_id == "conversions"
    )
    assert projection.rows[0].cells[persistence_position].string_value.startswith("boundary-")
    assert projection.rows[0].cells[conversion_position].integer_value == 2


def test_structure_lifecycle_retains_exact_cleanup_evidence() -> None:
    report = scan(discover_context(FIXTURES / "cleanup_project", AnalysisConfig()))
    projection = build_projection(report, ProjectionName.STRUCTURE_LIFECYCLE)
    assert isinstance(projection, TableProjection)
    cleanup_position = next(
        item.position for item in projection.columns if item.column_id == "cleanup"
    )
    assert any(
        row.cells[cleanup_position].string_value == "exact mapping clear observed"
        for row in projection.rows
    )
    assert report.completeness is Completeness.COMPLETE


def test_representation_candidate_projection_uses_saved_evidence_only() -> None:
    scanned = scan(discover_context(FIXTURES / "dictionary_project", AnalysisConfig()))
    report = load_json(render_json(scanned))
    projection = build_projection(report, ProjectionName.REPRESENTATION_CANDIDATES)
    assert isinstance(projection, TableProjection)
    assert projection.record_count == 2
    assert {row.cells[2].string_value for row in projection.rows} == {
        "typed-dict",
    }
    assert render_projection(projection, ProjectionFormat.TSV) == (
        GOLDEN / "projection-representation-candidates.tsv"
    ).read_text(encoding="utf-8")
    assert render_projection(projection, ProjectionFormat.MARKDOWN) == (
        GOLDEN / "projection-representation-candidates.md"
    ).read_text(encoding="utf-8")


def test_review_packet_is_bounded_evidence_not_refactoring_instruction() -> None:
    report = scan(discover_context(FIXTURES / "dictionary_project", AnalysisConfig()))
    projection = build_projection(report, ProjectionName.REVIEW_PACKET)
    assert isinstance(projection, ReviewPacketProjection)
    assert projection.items
    assert all(item.evidence_ids for item in projection.items)
    assert any("not refactoring instructions" in item for item in projection.limitations)
    assert all("replace" not in item.statement.lower() for item in projection.items)
    rendered = render_projection(projection, ProjectionFormat.MARKDOWN)
    assert rendered.startswith("# Bounded refactoring evidence review\n")
    assert "Review questions:" in rendered
    assert "/Users/" not in rendered

    selected_identity = projection.items[0].related_ids[0]
    selected = build_projection(
        report,
        ProjectionName.REVIEW_PACKET,
        selected_ids=(selected_identity,),
    )
    assert isinstance(selected, ReviewPacketProjection)
    assert all(
        item.item_id == selected_identity or selected_identity in item.related_ids
        for item in selected.items
    )

    bounded = build_projection(report, ProjectionName.REVIEW_PACKET, max_records=1)
    assert bounded.truncated
    assert bounded.completeness is Completeness.PARTIAL
    with pytest.raises(ProjectionBuildError, match="unknown review identity"):
        build_projection(
            report,
            ProjectionName.REVIEW_PACKET,
            selected_ids=("subject-missing",),
        )


def test_dictionary_projection_absence_and_bounds_fail_closed(ordinary_report) -> None:
    with pytest.raises(ProjectionBuildError, match="dictionary-shape evidence"):
        build_projection(ordinary_report, ProjectionName.SHAPE_VARIANTS)
    with pytest.raises(ProjectionBuildError, match="resolved flow evidence"):
        build_projection(ordinary_report, ProjectionName.DICTIONARY_FLOW)

    report = scan(discover_context(FIXTURES / "dictionary_project", AnalysisConfig()))
    bounded = build_projection(report, ProjectionName.DICTIONARY_FLOW, max_records=3)
    assert isinstance(bounded, GraphProjection)
    assert bounded.truncated
    assert bounded.completeness is Completeness.PARTIAL
    node_ids = {node.node_id for node in bounded.nodes}
    assert all(edge.source_id in node_ids and edge.target_id in node_ids for edge in bounded.edges)


def test_contract_projections_select_stable_contract_ids() -> None:
    project = FIXTURES / "contracts_project"
    scanned = scan_contract_inventory(discover_context(project, load_project_config(project)))
    report = load_json(render_json(scanned))
    subjects = {subject.subject_id: subject for subject in report.subjects}
    response = next(
        contract
        for contract in report.contracts
        if subjects[contract.defining_subject_id].logical_name == "package.contracts:Response"
    )
    selected_ids = (response.contract_id, response.contract_id)

    inventory = build_projection(
        report,
        ProjectionName.CONTRACTS,
        selected_ids=selected_ids,
    )
    boundaries = build_projection(
        report,
        ProjectionName.CONTRACT_BOUNDARIES,
        selected_ids=selected_ids,
    )
    graph = build_projection(
        report,
        ProjectionName.CONTRACT_BOUNDARY_GRAPH,
        selected_ids=selected_ids,
    )
    assert isinstance(inventory, TableProjection)
    assert isinstance(boundaries, TableProjection)
    assert isinstance(graph, GraphProjection)
    assert inventory.selection.selected_ids == (response.contract_id,)
    assert inventory.record_count == 1
    assert boundaries.record_count == len(response.boundaries)
    assert {row.cells[1].string_value for row in boundaries.rows} == {response.contract_id}
    assert len(graph.edges) == len(response.boundaries)
    assert {edge.source_id for edge in graph.edges} == {response.contract_id}
    expected_node_ids = {response.contract_id} | {
        boundary.site_subject_id for boundary in response.boundaries
    }
    assert {node.node_id for node in graph.nodes} == expected_node_ids
    assert graph.record_count == len(expected_node_ids) + len(response.boundaries)
    assert (
        build_projection(
            report,
            ProjectionName.CONTRACTS,
            selected_ids=selected_ids,
        )
        == inventory
    )

    for name in (
        ProjectionName.CONTRACTS,
        ProjectionName.CONTRACT_BOUNDARIES,
        ProjectionName.CONTRACT_BOUNDARY_GRAPH,
    ):
        with pytest.raises(ProjectionBuildError, match="selected contract is not present"):
            build_projection(report, name, selected_ids=("contract-missing",))
        with pytest.raises(ProjectionBuildError, match="require dependency-neighborhood"):
            build_projection(report, name, selected_ids=(response.contract_id,), depth=1)


def test_module_and_complexity_tables_are_ranked_and_bounded(ordinary_report) -> None:
    modules = build_projection(ordinary_report, ProjectionName.MODULES)
    complexity = build_projection(ordinary_report, ProjectionName.COMPLEXITY)
    assert isinstance(modules, TableProjection)
    assert isinstance(complexity, TableProjection)
    fan_in_position = next(
        column.position for column in modules.columns if column.column_id == "fan-in"
    )
    module_fan_in = [row.cells[fan_in_position].integer_value for row in modules.rows]
    assert module_fan_in == sorted(module_fan_in, reverse=True)
    score_position = next(
        column.position for column in complexity.columns if column.column_id == "weighted-score"
    )
    scores = [row.cells[score_position].integer_value for row in complexity.rows]
    assert scores == sorted(scores, reverse=True)
    bounded = build_projection(ordinary_report, ProjectionName.COMPLEXITY, max_records=1)
    assert bounded.truncated
    assert bounded.completeness is Completeness.PARTIAL
    assert bounded.record_count == 1


def test_symbol_and_finding_tables_have_stable_review_order(ordinary_report) -> None:
    symbols = build_projection(ordinary_report, ProjectionName.SYMBOLS)
    findings = build_projection(ordinary_report, ProjectionName.FINDINGS)
    assert isinstance(symbols, TableProjection)
    assert isinstance(findings, TableProjection)
    assert [row.row_id for row in symbols.rows] == sorted(row.row_id for row in symbols.rows)
    severity_position = next(
        column.position for column in findings.columns if column.column_id == "severity-rank"
    )
    severity_ranks = [row.cells[severity_position].integer_value for row in findings.rows]
    assert severity_ranks == sorted(severity_ranks, reverse=True)


def test_subject_findings_aggregate_and_disclose_scope(ordinary_report) -> None:
    report = load_json((GOLDEN / "phase3-report.json").read_text(encoding="utf-8"))
    projection = build_projection(report, ProjectionName.SUBJECT_FINDINGS)
    assert isinstance(projection, TableProjection)
    positions = {column.column_id: column.position for column in projection.columns}
    counts = [row.cells[positions["finding-count"]].integer_value for row in projection.rows]
    assert counts == sorted(counts, reverse=True)
    shared = next(
        row for row in projection.rows if row.cells[positions["finding-count"]].integer_value == 3
    )
    assert shared.cells[positions["category-counts"]].string_value == (
        "duplication=1, extraction-candidate=1, module-combination=1"
    )
    assert shared.cells[positions["affected-proposals"]].integer_value >= 1
    assert shared.evidence_ids == tuple(sorted(set(shared.evidence_ids)))

    zero_finding_subject = next(
        subject.subject_id
        for subject in report.subjects
        if all(subject.subject_id not in finding.subject_ids for finding in report.findings)
    )
    selected = build_projection(
        report,
        ProjectionName.SUBJECT_FINDINGS,
        selected_ids=(zero_finding_subject,),
    )
    assert selected.record_count == 1
    assert selected.rows[0].cells[positions["finding-count"]].integer_value == 0
    with pytest.raises(ProjectionBuildError, match="selected subject is absent"):
        build_projection(
            report,
            ProjectionName.SUBJECT_FINDINGS,
            selected_ids=("subject-00000000000000000000",),
        )

    bounded = build_projection(report, ProjectionName.SUBJECT_FINDINGS, max_records=1)
    assert bounded.truncated
    assert bounded.rows[0].row_id == projection.rows[0].row_id

    unscoped = build_projection(ordinary_report, ProjectionName.SUBJECT_FINDINGS)
    assert unscoped.record_count == 0
    assert any(
        "1 findings without subject identities" in limitation for limitation in unscoped.limitations
    )
    assert unscoped.completeness is ordinary_report.completeness


def test_subject_findings_support_typed_focused_filters(ordinary_report) -> None:
    report = load_json((GOLDEN / "phase3-report.json").read_text(encoding="utf-8"))
    duplication = build_projection(
        report,
        ProjectionName.SUBJECT_FINDINGS,
        finding_categories=(ProjectionFindingCategory.DUPLICATION,),
        finding_severities=(ProjectionFindingSeverity.WARNING,),
        finding_rule_ids=("duplication.normalized-ast",),
    )
    positions = {column.column_id: column.position for column in duplication.columns}
    assert duplication.rows
    assert all(
        row.cells[positions["category-counts"]].string_value == "duplication=1"
        for row in duplication.rows
    )
    assert all(
        row.cells[positions["rule-counts"]].string_value == "duplication.normalized-ast=1"
        for row in duplication.rows
    )
    filtered_golden = build_projection(
        report,
        ProjectionName.SUBJECT_FINDINGS,
        module_prefixes=("package.combine_a",),
        finding_categories=(ProjectionFindingCategory.DUPLICATION,),
        finding_severities=(ProjectionFindingSeverity.WARNING,),
        finding_rule_ids=("duplication.normalized-ast",),
    )
    for output_format, filename in (
        (ProjectionFormat.JSON, "projection-subject-findings-filtered.json"),
        (ProjectionFormat.TSV, "projection-subject-findings-filtered.tsv"),
        (ProjectionFormat.MARKDOWN, "projection-subject-findings-filtered.md"),
    ):
        assert render_projection(filtered_golden, output_format) == (GOLDEN / filename).read_text(
            encoding="utf-8"
        )

    category_or = build_projection(
        report,
        ProjectionName.SUBJECT_FINDINGS,
        finding_categories=(
            ProjectionFindingCategory.DUPLICATION,
            ProjectionFindingCategory.MODULE_COMBINATION,
        ),
    )
    assert all(row.cells[positions["finding-count"]].integer_value == 2 for row in category_or.rows)
    severity_only = build_projection(
        report,
        ProjectionName.SUBJECT_FINDINGS,
        finding_severities=(ProjectionFindingSeverity.INFO,),
    )
    assert severity_only.rows
    assert all(row.cells[positions["info"]].integer_value == 1 for row in severity_only.rows)
    rule_or = build_projection(
        report,
        ProjectionName.SUBJECT_FINDINGS,
        finding_rule_ids=("configured-layer-direction", "duplication.normalized-ast"),
    )
    assert rule_or.rows
    assert rule_or.projection_id != category_or.projection_id

    exact_module = build_projection(
        report,
        ProjectionName.SUBJECT_FINDINGS,
        module_prefixes=("package.combine_a",),
    )
    assert exact_module.rows
    assert all(
        row.cells[positions["module"]].string_value == "package.combine_a"
        for row in exact_module.rows
    )
    arbitrary_prefix = build_projection(
        report,
        ProjectionName.SUBJECT_FINDINGS,
        module_prefixes=("package.combine",),
    )
    assert arbitrary_prefix.record_count == 0
    filtered_bound = build_projection(
        report,
        ProjectionName.SUBJECT_FINDINGS,
        finding_categories=(ProjectionFindingCategory.DUPLICATION,),
        max_records=1,
    )
    assert filtered_bound.truncated
    assert filtered_bound.record_count == 1

    zero_finding_subject = next(
        subject.subject_id
        for subject in report.subjects
        if all(subject.subject_id not in finding.subject_ids for finding in report.findings)
    )
    selected = build_projection(
        report,
        ProjectionName.SUBJECT_FINDINGS,
        selected_ids=(zero_finding_subject,),
        finding_categories=(ProjectionFindingCategory.DUPLICATION,),
    )
    assert selected.record_count == 1
    assert selected.rows[0].cells[positions["finding-count"]].integer_value == 0

    with pytest.raises(ProjectionBuildError, match="cannot be combined"):
        build_projection(
            report,
            ProjectionName.SUBJECT_FINDINGS,
            selected_ids=(zero_finding_subject,),
            module_prefixes=("package",),
        )
    with pytest.raises(ProjectionBuildError, match="require the subject-findings"):
        build_projection(
            report,
            ProjectionName.FINDINGS,
            finding_severities=(ProjectionFindingSeverity.WARNING,),
        )

    filtered_unscoped = build_projection(
        ordinary_report,
        ProjectionName.SUBJECT_FINDINGS,
        finding_categories=(ProjectionFindingCategory.DUPLICATION,),
    )
    assert not any("without subject identities" in item for item in filtered_unscoped.limitations)
    matching_unscoped = build_projection(
        ordinary_report,
        ProjectionName.SUBJECT_FINDINGS,
        finding_categories=(ProjectionFindingCategory.IMPORT_CYCLE,),
    )
    assert any(
        "1 findings without subject identities" in item for item in matching_unscoped.limitations
    )


def _unscoped_limitation_counts(projection: TableProjection) -> list[str]:
    return [item for item in projection.limitations if "without subject identities" in item]


def test_subject_findings_report_unscoped_counts_alongside_subject_rows() -> None:
    report = load_json((GOLDEN / "phase3-report.json").read_text(encoding="utf-8"))
    assert not _unscoped_limitation_counts(
        build_projection(report, ProjectionName.SUBJECT_FINDINGS)
    )

    template = report.findings[0]
    mixed = report.model_copy(
        update={
            "findings": (
                *report.findings,
                template.model_copy(
                    update={
                        "finding_id": "finding-unscoped-duplication",
                        "rule_id": "duplication.normalized-ast",
                        "category": "duplication",
                        "severity": "warning",
                        "subject_ids": (),
                    }
                ),
                template.model_copy(
                    update={
                        "finding_id": "finding-unscoped-cycle",
                        "rule_id": "layering.import-cycle",
                        "category": "import-cycle",
                        "severity": "error",
                        "subject_ids": (),
                    }
                ),
            )
        }
    )

    baseline = build_projection(report, ProjectionName.SUBJECT_FINDINGS)
    combined = build_projection(mixed, ProjectionName.SUBJECT_FINDINGS)
    assert combined.rows
    # Unscoped findings must not alter subject rows, only the disclosed scope limitation.
    assert [row.row_id for row in combined.rows] == [row.row_id for row in baseline.rows]
    assert [row.cells for row in combined.rows] == [row.cells for row in baseline.rows]
    assert combined.record_count == baseline.record_count
    assert _unscoped_limitation_counts(combined) == [
        "2 findings without subject identities are omitted from this scoped view."
    ]

    category_filtered = build_projection(
        mixed,
        ProjectionName.SUBJECT_FINDINGS,
        finding_categories=(ProjectionFindingCategory.DUPLICATION,),
    )
    assert category_filtered.rows
    assert _unscoped_limitation_counts(category_filtered) == [
        "1 findings without subject identities are omitted from this scoped view."
    ]

    severity_filtered = build_projection(
        mixed,
        ProjectionName.SUBJECT_FINDINGS,
        finding_severities=(ProjectionFindingSeverity.WARNING,),
    )
    assert severity_filtered.rows
    assert _unscoped_limitation_counts(severity_filtered) == [
        "1 findings without subject identities are omitted from this scoped view."
    ]

    rule_filtered = build_projection(
        mixed,
        ProjectionName.SUBJECT_FINDINGS,
        finding_rule_ids=("duplication.normalized-ast",),
    )
    assert rule_filtered.rows
    assert _unscoped_limitation_counts(rule_filtered) == [
        "1 findings without subject identities are omitted from this scoped view."
    ]

    excluded = build_projection(
        mixed,
        ProjectionName.SUBJECT_FINDINGS,
        finding_categories=(ProjectionFindingCategory.EXTRACTION_CANDIDATE,),
    )
    assert excluded.rows
    assert not _unscoped_limitation_counts(excluded)

    empty = build_projection(
        mixed,
        ProjectionName.SUBJECT_FINDINGS,
        module_prefixes=("package.combine",),
    )
    assert empty.record_count == 0
    assert _unscoped_limitation_counts(empty) == [
        "2 findings without subject identities are omitted from this scoped view."
    ]

    zero_match_subject = next(
        subject.subject_id
        for subject in mixed.subjects
        if all(subject.subject_id not in finding.subject_ids for finding in mixed.findings)
    )
    positions = {column.column_id: column.position for column in combined.columns}
    selected = build_projection(
        mixed,
        ProjectionName.SUBJECT_FINDINGS,
        selected_ids=(zero_match_subject,),
        finding_categories=(ProjectionFindingCategory.DUPLICATION,),
    )
    assert selected.record_count == 1
    assert selected.rows[0].cells[positions["finding-count"]].integer_value == 0
    assert _unscoped_limitation_counts(selected) == [
        "1 findings without subject identities are omitted from this scoped view."
    ]

    for output_format in (
        ProjectionFormat.JSON,
        ProjectionFormat.TSV,
        ProjectionFormat.MARKDOWN,
    ):
        first = render_projection(category_filtered, output_format)
        second = render_projection(
            build_projection(
                mixed,
                ProjectionName.SUBJECT_FINDINGS,
                finding_categories=(ProjectionFindingCategory.DUPLICATION,),
            ),
            output_format,
        )
        assert first == second


def test_package_module_hierarchy_has_known_parents_and_safe_truncation(ordinary_report) -> None:
    hierarchy = build_projection(ordinary_report, ProjectionName.HIERARCHY)
    assert isinstance(hierarchy, HierarchyProjection)
    node_ids = {node.node_id for node in hierarchy.nodes}
    assert all(node.parent_id is None or node.parent_id in node_ids for node in hierarchy.nodes)
    assert {node.node_id: node.kind for node in hierarchy.nodes} == {
        "alpha": "module",
        "alpha.a": "module",
        "alpha.b": "module",
    }
    bounded = build_projection(ordinary_report, ProjectionName.HIERARCHY, max_records=2)
    assert isinstance(bounded, HierarchyProjection)
    bounded_ids = {node.node_id for node in bounded.nodes}
    assert bounded.truncated
    assert all(node.parent_id is None or node.parent_id in bounded_ids for node in bounded.nodes)


def test_namespace_package_hierarchy_materializes_logical_packages() -> None:
    report = scan(discover_context(FIXTURES / "namespace", AnalysisConfig()))
    hierarchy = build_projection(report, ProjectionName.HIERARCHY)
    assert isinstance(hierarchy, HierarchyProjection)
    assert {node.node_id: node.kind for node in hierarchy.nodes} == {
        "acme": "package",
        "acme.tools": "package",
        "acme.tools.worker": "module",
    }


def test_dependency_and_cycle_graphs_have_known_endpoints(ordinary_report) -> None:
    for name in (ProjectionName.DEPENDENCIES, ProjectionName.CYCLES):
        projection = build_projection(ordinary_report, name)
        assert isinstance(projection, GraphProjection)
        node_ids = {node.node_id for node in projection.nodes}
        assert all(
            edge.source_id in node_ids and edge.target_id in node_ids for edge in projection.edges
        )


def test_dependency_neighborhood_is_selected_bounded_and_edge_filtered(ordinary_report) -> None:
    neighborhood = build_projection(
        ordinary_report,
        ProjectionName.DEPENDENCY_NEIGHBORHOOD,
        selected_ids=("alpha.a",),
    )
    assert isinstance(neighborhood, GraphProjection)
    assert {node.node_id for node in neighborhood.nodes} == {"alpha.a", "alpha.b"}
    assert {(edge.source_id, edge.target_id) for edge in neighborhood.edges} == {
        ("alpha.a", "alpha.b"),
        ("alpha.b", "alpha.a"),
    }
    assert neighborhood.selection.depth == 1

    root_only = build_projection(
        ordinary_report,
        ProjectionName.DEPENDENCY_NEIGHBORHOOD,
        selected_ids=("alpha.a",),
        depth=0,
    )
    assert isinstance(root_only, GraphProjection)
    assert [node.node_id for node in root_only.nodes] == ["alpha.a"]
    assert root_only.edges == ()
    assert not root_only.truncated

    filtered = build_projection(
        ordinary_report,
        ProjectionName.DEPENDENCY_NEIGHBORHOOD,
        selected_ids=("alpha.a",),
        edge_kinds=("optional",),
    )
    assert isinstance(filtered, GraphProjection)
    assert [node.node_id for node in filtered.nodes] == ["alpha.a"]

    bounded = build_projection(
        ordinary_report,
        ProjectionName.DEPENDENCY_NEIGHBORHOOD,
        selected_ids=("alpha.a",),
        max_records=1,
    )
    assert bounded.truncated
    assert bounded.completeness is Completeness.PARTIAL
    with pytest.raises(ProjectionBuildError, match="exactly one selected module"):
        build_projection(ordinary_report, ProjectionName.DEPENDENCY_NEIGHBORHOOD)
    with pytest.raises(ProjectionBuildError, match="not present"):
        build_projection(
            ordinary_report,
            ProjectionName.DEPENDENCY_NEIGHBORHOOD,
            selected_ids=("missing",),
        )


def test_scc_and_canonical_cycle_tables_preserve_review_evidence(ordinary_report) -> None:
    sccs = build_projection(ordinary_report, ProjectionName.SCCS)
    cycles = build_projection(ordinary_report, ProjectionName.CYCLE_LIST)
    assert isinstance(sccs, TableProjection)
    assert isinstance(cycles, TableProjection)
    assert sccs.record_count == len(ordinary_report.strongly_connected_components)
    assert cycles.record_count == len(ordinary_report.cycles)
    assert sccs.rows[0].cells[1].integer_value == 2
    assert sccs.rows[0].cells[3].integer_value == 2
    assert sccs.rows[0].cells[4].integer_value == 1
    assert cycles.rows[0].cells[3].string_value == "alpha.a -> alpha.b -> alpha.a"
    assert cycles.rows[0].cells[4].string_value == sccs.rows[0].row_id


def test_projection_renderers_are_deterministic_and_type_checked(ordinary_report) -> None:
    table = build_projection(ordinary_report, ProjectionName.MODULES)
    graph = build_projection(ordinary_report, ProjectionName.DEPENDENCIES)
    for output_format in (
        ProjectionFormat.JSON,
        ProjectionFormat.JSON_COMPACT,
        ProjectionFormat.TSV,
        ProjectionFormat.MARKDOWN,
    ):
        assert render_projection(table, output_format) == render_projection(table, output_format)
    for output_format in (
        ProjectionFormat.JSON,
        ProjectionFormat.JSON_COMPACT,
        ProjectionFormat.MARKDOWN,
        ProjectionFormat.DOT,
        ProjectionFormat.MERMAID,
    ):
        rendered = render_projection(graph, output_format)
        assert rendered == render_projection(graph, output_format)
        assert rendered.endswith("\n")
    with pytest.raises(ProjectionRenderError, match="TSV requires"):
        render_projection(graph, ProjectionFormat.TSV)
    with pytest.raises(ProjectionRenderError, match="requires a graph"):
        render_projection(table, ProjectionFormat.DOT)

    hierarchy = build_projection(ordinary_report, ProjectionName.HIERARCHY)
    for output_format in (
        ProjectionFormat.JSON,
        ProjectionFormat.JSON_COMPACT,
        ProjectionFormat.MARKDOWN,
        ProjectionFormat.DOT,
        ProjectionFormat.MERMAID,
    ):
        rendered = render_projection(hierarchy, output_format)
        assert rendered == render_projection(hierarchy, output_format)
        assert rendered.endswith("\n")
    with pytest.raises(ProjectionRenderError, match="TSV requires"):
        render_projection(hierarchy, ProjectionFormat.TSV)


def test_tsv_uses_declared_scalar_columns_and_escaping(ordinary_report) -> None:
    health = build_projection(ordinary_report, ProjectionName.HEALTH)
    assert isinstance(health, TableProjection)
    rendered = render_projection(health, ProjectionFormat.TSV)
    assert rendered.splitlines()[0] == ("record-type\tidentity\tstate\tcompleteness\tdetail")
    assert len(rendered.splitlines()) == health.record_count + 1


def test_saved_report_projection_goldens_need_no_source_scan() -> None:
    report = load_json((GOLDEN / "minimal-report.json").read_text(encoding="utf-8"))
    cases = (
        (
            ProjectionName.MODULES,
            ProjectionFormat.JSON,
            "projection-modules.json",
        ),
        (
            ProjectionName.MODULES,
            ProjectionFormat.TSV,
            "projection-modules.tsv",
        ),
        (
            ProjectionName.HEALTH,
            ProjectionFormat.MARKDOWN,
            "projection-health.md",
        ),
        (
            ProjectionName.SYMBOLS,
            ProjectionFormat.TSV,
            "projection-symbols.tsv",
        ),
        (
            ProjectionName.FINDINGS,
            ProjectionFormat.TSV,
            "projection-findings.tsv",
        ),
        (
            ProjectionName.HIERARCHY,
            ProjectionFormat.MERMAID,
            "projection-hierarchy.mmd",
        ),
        (
            ProjectionName.DEPENDENCIES,
            ProjectionFormat.DOT,
            "projection-dependencies.dot",
        ),
        (
            ProjectionName.DEPENDENCIES,
            ProjectionFormat.MERMAID,
            "projection-dependencies.mmd",
        ),
    )
    for name, output_format, filename in cases:
        projection = build_projection(report, name)
        assert render_projection(projection, output_format) == (GOLDEN / filename).read_text(
            encoding="utf-8"
        )


def test_scc_and_cycle_list_goldens(ordinary_report) -> None:
    cases = (
        (ProjectionName.SCCS, "projection-sccs.tsv"),
        (ProjectionName.CYCLE_LIST, "projection-cycle-list.tsv"),
    )
    for name, filename in cases:
        projection = build_projection(ordinary_report, name)
        assert render_projection(projection, ProjectionFormat.TSV) == (GOLDEN / filename).read_text(
            encoding="utf-8"
        )


def test_dependency_neighborhood_goldens(ordinary_report) -> None:
    projection = build_projection(
        ordinary_report,
        ProjectionName.DEPENDENCY_NEIGHBORHOOD,
        selected_ids=("alpha.a",),
    )
    for output_format, filename in (
        (ProjectionFormat.DOT, "projection-dependency-neighborhood.dot"),
        (ProjectionFormat.MERMAID, "projection-dependency-neighborhood.mmd"),
    ):
        assert render_projection(projection, output_format) == (GOLDEN / filename).read_text(
            encoding="utf-8"
        )


def test_phase_three_projections_are_bounded_and_derive_only_from_saved_json() -> None:
    project = FIXTURES / "proposal_project"
    scanned = scan(discover_context(project, AnalysisConfig()))
    report = load_json(render_json(scanned))
    matrix = build_projection(report, ProjectionName.FUNCTION_COMMONALITY, max_records=10)
    assert isinstance(matrix, MatrixProjection)
    assert matrix.record_count <= 10
    assert render_projection(matrix, ProjectionFormat.TSV).startswith("row-id\tcolumn-id")

    for name in (ProjectionName.CLONE_GRAPH, ProjectionName.STATE_SIDE_EFFECT_GRAPH):
        graph = build_projection(report, name, max_records=20)
        assert isinstance(graph, GraphProjection)
        assert graph.record_count <= 20
        assert render_projection(graph, ProjectionFormat.DOT).startswith(
            ("digraph projection", "graph projection")
        )
        assert render_projection(graph, ProjectionFormat.MERMAID).startswith(("flowchart", "graph"))

    packet = build_projection(report, ProjectionName.REFACTOR_PACKET, max_records=10)
    assert isinstance(packet, ReviewPacketProjection)
    assert "Phase 4" in render_projection(packet, ProjectionFormat.MARKDOWN)


def test_signal_quality_projections_are_saved_report_only_and_bounded() -> None:
    config = load_config_file(FIXTURES / "proposal-config.toml")
    report = load_json(
        render_json(scan_refactor(discover_context(FIXTURES / "proposal_project", config)))
    )
    expected_kinds = {
        ProjectionName.SIGNAL_QUALITY: "table",
        ProjectionName.COMMONALITY_ADMISSION: "table",
        ProjectionName.PREVALENT_FEATURES: "table",
        ProjectionName.PROPOSAL_ADMISSION: "table",
        ProjectionName.PROPOSAL_DECISIONS: "table",
        ProjectionName.PROPOSAL_GATES: "table",
        ProjectionName.REFACTOR_SHORTLIST: "table",
    }
    for name, kind in expected_kinds.items():
        projection = build_projection(report, name, max_records=5)
        assert projection.projection_kind == kind
        assert projection.record_count <= 5
        assert render_projection(projection, ProjectionFormat.JSON) == render_projection(
            projection, ProjectionFormat.JSON
        )
    admission = build_projection(report, ProjectionName.PROPOSAL_ADMISSION)
    for output_format, filename in (
        (ProjectionFormat.JSON, "projection-proposal-admission.json"),
        (ProjectionFormat.TSV, "projection-proposal-admission.tsv"),
        (ProjectionFormat.MARKDOWN, "projection-proposal-admission.md"),
    ):
        assert render_projection(admission, output_format) == (GOLDEN / filename).read_text(
            encoding="utf-8"
        )
    gates = build_projection(report, ProjectionName.PROPOSAL_GATES, max_records=5)
    assert gates.truncated
    assert gates.completeness is Completeness.PARTIAL
    proposal = report.refactor_proposals[0]
    selected = build_projection(
        report,
        ProjectionName.PROPOSAL_GATES,
        selected_ids=(proposal.proposal_id,),
    )
    assert selected.record_count == len(proposal.gate_decisions)

    shortlist = build_projection(report, ProjectionName.REFACTOR_SHORTLIST)
    positions = {column.column_id: column.position for column in shortlist.columns}
    keys = [
        (
            row.cells[positions["state-rank"]].integer_value,
            row.cells[positions["failed-gates"]].integer_value,
            row.cells[positions["unresolved-gates"]].integer_value,
            row.cells[positions["affected-subjects"]].integer_value,
            row.cells[positions["affected-modules"]].integer_value,
            row.row_id,
        )
        for row in shortlist.rows
    ]
    assert keys == sorted(keys)
    bounded = build_projection(report, ProjectionName.REFACTOR_SHORTLIST, max_records=1)
    assert bounded.rows[0].row_id == shortlist.rows[0].row_id
    selected_shortlist = build_projection(
        report,
        ProjectionName.REFACTOR_SHORTLIST,
        selected_ids=(proposal.proposal_id,),
    )
    assert [row.row_id for row in selected_shortlist.rows] == [proposal.proposal_id]


def test_phase_three_authoritative_report_and_projection_goldens() -> None:
    golden_json = (GOLDEN / "phase3-report.json").read_text(encoding="utf-8")
    retained = load_json(golden_json)
    project = FIXTURES / "proposal_project"
    config = load_config_file(FIXTURES / "proposal-config.toml")
    rescanned = scan_refactor(discover_context(project, config))
    assert render_json(rescanned) == golden_json
    scan_report = scan(discover_context(project, config))
    assert render_markdown(scan_report) == (GOLDEN / "phase3-report.md").read_text(encoding="utf-8")
    for forbidden in ("/Users/", "/Volumes/", "/tmp/", "/private/var/"):
        assert forbidden not in golden_json

    cases = (
        (
            ProjectionName.REFACTOR_PACKET,
            ProjectionFormat.MARKDOWN,
            "phase3-refactor-packet.md",
        ),
        (
            ProjectionName.FUNCTION_COMMONALITY,
            ProjectionFormat.TSV,
            "projection-function-commonality.tsv",
        ),
        (
            ProjectionName.STATE_SIDE_EFFECTS,
            ProjectionFormat.TSV,
            "projection-state-side-effects.tsv",
        ),
        (
            ProjectionName.ARCHITECTURE_ESTIMATES,
            ProjectionFormat.TSV,
            "projection-architecture-estimates.tsv",
        ),
        (
            ProjectionName.STATE_SIDE_EFFECT_GRAPH,
            ProjectionFormat.DOT,
            "projection-state-side-effect-graph.dot",
        ),
        (
            ProjectionName.STATE_SIDE_EFFECT_GRAPH,
            ProjectionFormat.MERMAID,
            "projection-state-side-effect-graph.mmd",
        ),
        (
            ProjectionName.UNCOVERED_PROPOSAL_SUBJECTS,
            ProjectionFormat.TSV,
            "phase4-uncovered-proposal-subjects.tsv",
        ),
        (
            ProjectionName.UNCOVERED_PROPOSAL_SUBJECTS,
            ProjectionFormat.MARKDOWN,
            "phase4-uncovered-proposal-subjects.md",
        ),
        (
            ProjectionName.SUBJECT_FINDINGS,
            ProjectionFormat.JSON,
            "projection-subject-findings.json",
        ),
        (
            ProjectionName.SUBJECT_FINDINGS,
            ProjectionFormat.TSV,
            "projection-subject-findings.tsv",
        ),
        (
            ProjectionName.SUBJECT_FINDINGS,
            ProjectionFormat.MARKDOWN,
            "projection-subject-findings.md",
        ),
    )
    for name, output_format, filename in cases:
        rendered = render_projection(build_projection(retained, name), output_format)
        assert rendered == (GOLDEN / filename).read_text(encoding="utf-8")
