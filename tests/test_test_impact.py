from __future__ import annotations

import builtins
import hashlib
import shutil
from pathlib import Path

import pytest

from code_analysis.config import AnalysisConfig, PhaseFourLimits
from code_analysis.models import Completeness, ProviderState, SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.report_models import SectionState
from code_analysis.report_models import TestRelationshipKind as RelationKind
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.reports.projection_views import (
    ProjectionFormat,
    render_projection,
    render_projection_binary,
)
from code_analysis.service import scan, scan_contract_inventory

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


def test_crlf_test_modules_bind_original_source_bytes(tmp_path):
    (tmp_path / "app.py").write_bytes(b"def calculate():\r\n    return 1\r\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    payload = (
        b"from app import calculate\r\ndef test_calculate():\r\n    assert calculate() == 1\r\n"
    )
    (tests / "test_app.py").write_bytes(payload)
    report = scan(discover_context(tmp_path, AnalysisConfig()))
    observed = next(item for item in report.test_modules if item.path == "tests/test_app.py")
    assert observed.source_sha256 == hashlib.sha256(payload).hexdigest()
    assert report.test_cases and report.test_relationships


def test_static_test_impact_inventory_and_relationships_are_bound() -> None:
    project = FIXTURES / "test_impact_project"
    report = load_json(render_json(scan(discover_context(project, AnalysisConfig()))))
    assert len(report.test_modules) == 2
    assert {item.qualified_name.rsplit(":", 1)[-1] for item in report.test_cases} == {
        "test_calculate",
        "test_dynamic_patch",
        "test_qualification_path",
    }
    assert len(report.test_fixtures) == 2
    client = next(item for item in report.test_fixtures if item.qualified_name.endswith(":client"))
    assert len(client.dependency_fixture_ids) == 1
    assert any(item.qualification for item in report.test_cases)
    kinds = {item.kind for item in report.test_relationships}
    assert {
        RelationKind.MODULE_IMPORT,
        RelationKind.DIRECT_SYMBOL,
        RelationKind.FIXTURE_USE,
        RelationKind.PATCH,
    } <= kinds
    assert any(
        item.target_subject_id is not None and item.resolved
        for item in report.test_relationships
        if item.kind is RelationKind.DIRECT_SYMBOL
    )
    assert any(
        item.target_fixture_id == client.fixture_id and item.resolved
        for item in report.test_relationships
    )
    assert any(
        not item.resolved and item.target_text == "target_name"
        for item in report.test_relationships
        if item.kind is RelationKind.PATCH
    )
    coverage = next(item for item in report.section_coverage if item.section == "test-impact")
    assert coverage.state is SectionState.PARTIAL
    assert coverage.unresolved > 0
    assert report.completeness is Completeness.PARTIAL


@pytest.mark.parametrize("mixed", [False, True])
def test_javascript_module_test_sources_project_without_framework_cases(
    tmp_path, monkeypatch, mixed
):
    root = tmp_path / "source"
    if mixed:
        shutil.copytree(FIXTURES / "test_impact_project", root)
        shutil.copytree(FIXTURES / "javascript", root, dirs_exist_ok=True)
    else:
        shutil.copytree(FIXTURES / "javascript", root)
    report = load_json(render_json(scan(discover_context(root, AnalysisConfig()))))
    module_sources = {
        item.test_module_id: item.module
        for item in report.test_modules
        if item.framework == "javascript-generic"
    }
    assert module_sources
    relationships = {item.relationship_id: item for item in report.test_relationships}
    assert any(item.source_test_module_id in module_sources for item in relationships.values())
    if not mixed:
        assert not report.test_cases
    else:
        assert report.test_cases and report.test_fixtures

    # The temporary source is no longer available during projection/rendering.
    shutil.rmtree(root)
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name.startswith("tree_sitter"):
            pytest.fail("saved-report projections must not import the JavaScript parser")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    table = build_projection(report, ProjectionName.TEST_IMPACT)
    graph = build_projection(report, ProjectionName.TEST_IMPACT_GRAPH)
    assert {row.row_id for row in table.rows} == set(relationships)
    assert {edge.edge_id for edge in graph.edges} == set(relationships)
    nodes = {node.node_id: node for node in graph.nodes}
    for row in table.rows:
        relationship = relationships[row.row_id]
        if relationship.source_test_module_id is not None:
            assert row.cells[0].string_value == module_sources[relationship.source_test_module_id]
    for edge in graph.edges:
        relationship = relationships[edge.edge_id]
        assert edge.source_id == (relationship.source_test_id or relationship.source_test_module_id)
        if relationship.source_test_module_id is not None:
            assert nodes[edge.source_id].kind == "test-module"
            assert nodes[edge.source_id].label == module_sources[edge.source_id]
    for projection, formats in (
        (table, (ProjectionFormat.JSON, ProjectionFormat.TSV, ProjectionFormat.MARKDOWN)),
        (graph, (ProjectionFormat.JSON, ProjectionFormat.DOT, ProjectionFormat.MERMAID)),
    ):
        for output_format in formats:
            rendered = render_projection(projection, output_format)
            assert rendered == render_projection(projection, output_format)
            assert str(tmp_path) not in rendered
    bounded = build_projection(report, ProjectionName.TEST_IMPACT_GRAPH, max_records=1)
    assert bounded.truncated
    assert bounded.completeness is not Completeness.COMPLETE


def test_mixed_test_relationship_limit_covers_both_languages(tmp_path):
    root = tmp_path / "source"
    shutil.copytree(FIXTURES / "test_impact_project", root)
    shutil.copytree(FIXTURES / "javascript", root, dirs_exist_ok=True)
    config = AnalysisConfig(phase_four_limits=PhaseFourLimits(max_test_relationships=2))
    report = load_json(render_json(scan(discover_context(root, config))))
    assert {item.language for item in report.test_modules} == {
        SourceLanguage.PYTHON,
        SourceLanguage.JAVASCRIPT,
    }
    assert len(report.test_relationships) == 2
    section = next(item for item in report.section_coverage if item.section == "test-impact")
    assert section.state is SectionState.TRUNCATED
    assert section.truncated > 0
    assert report.completeness is Completeness.PARTIAL
    assert any(item.code == "test-impact-bound-truncated" for item in report.diagnostics)


def test_python_test_failure_retains_javascript_evidence(tmp_path, monkeypatch):
    import code_analysis.test_impact_reporting as reporting

    root = tmp_path / "source"
    shutil.copytree(FIXTURES / "test_impact_project", root)
    shutil.copytree(FIXTURES / "javascript", root, dirs_exist_ok=True)

    def failed_python_test(*args):
        raise SyntaxError("synthetic Python test parser failure")

    monkeypatch.setattr(reporting, "parse_test_module", failed_python_test)
    report = load_json(render_json(scan(discover_context(root, AnalysisConfig()))))
    assert report.test_modules
    assert all(item.language is SourceLanguage.JAVASCRIPT for item in report.test_modules)
    assert report.test_relationships
    section = next(item for item in report.section_coverage if item.section == "test-impact")
    assert section.state is SectionState.PARTIAL
    assert section.failed == 2
    assert set(section.languages) == {SourceLanguage.PYTHON, SourceLanguage.JAVASCRIPT}
    assert report.completeness is Completeness.PARTIAL
    assert build_projection(report, ProjectionName.TEST_IMPACT_GRAPH).edges


def test_missing_test_roots_are_optional_unavailable() -> None:
    report = scan(discover_context(FIXTURES / "excluded", AnalysisConfig()))
    coverage = next(item for item in report.section_coverage if item.section == "test-impact")
    provider = next(item for item in report.providers if item.provider_id == "native-test-impact")
    assert coverage.state is SectionState.UNAVAILABLE
    assert not coverage.required
    assert provider.state is ProviderState.UNAVAILABLE
    assert report.completeness is Completeness.COMPLETE


def test_focused_contract_command_keeps_test_impact_disabled() -> None:
    report = scan_contract_inventory(
        discover_context(FIXTURES / "test_impact_project", AnalysisConfig())
    )
    coverage = next(item for item in report.section_coverage if item.section == "test-impact")
    assert coverage.state is SectionState.DISABLED
    assert not report.test_cases


def test_test_impact_bounds_are_truncated_and_never_empty_success() -> None:
    config = AnalysisConfig(
        phase_four_limits=PhaseFourLimits(
            max_tests=1,
            max_test_relationships=2,
            max_unresolved_test_references=1,
        )
    )
    report = scan(discover_context(FIXTURES / "test_impact_project", config))
    coverage = next(item for item in report.section_coverage if item.section == "test-impact")
    assert coverage.state is SectionState.TRUNCATED
    assert coverage.truncated > 0
    assert report.completeness is Completeness.PARTIAL
    assert report.test_cases


def test_test_impact_projections_are_saved_report_only_and_golden() -> None:
    report = load_json(
        render_json(scan(discover_context(FIXTURES / "test_impact_project", AnalysisConfig())))
    )
    cases = (
        (ProjectionName.TEST_IMPACT, ProjectionFormat.JSON, "phase4-test-impact.json"),
        (ProjectionName.TEST_IMPACT, ProjectionFormat.MARKDOWN, "phase4-test-impact.md"),
        (ProjectionName.TEST_IMPACT, ProjectionFormat.TSV, "phase4-test-impact.tsv"),
        (ProjectionName.TEST_IMPACT_GRAPH, ProjectionFormat.DOT, "phase4-test-impact.dot"),
        (
            ProjectionName.TEST_IMPACT_GRAPH,
            ProjectionFormat.MERMAID,
            "phase4-test-impact.mmd",
        ),
    )
    for name, output_format, filename in cases:
        rendered = render_projection(build_projection(report, name), output_format)
        assert rendered == (GOLDEN / filename).read_text(encoding="utf-8")
        assert "/Users/" not in rendered
    if shutil.which("dot") is not None:
        graph = build_projection(report, ProjectionName.TEST_IMPACT_GRAPH)
        for output_format, filename in (
            (ProjectionFormat.SVG, "phase4-test-impact.svg"),
            (ProjectionFormat.PNG, "phase4-test-impact.png"),
        ):
            rendered = render_projection_binary(graph, output_format)
            assert rendered == render_projection_binary(graph, output_format)
            assert rendered == (GOLDEN / filename).read_bytes()


def test_malformed_test_source_is_failed_evidence_not_empty_success(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src" / "package").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n', encoding="utf-8"
    )
    (project / "src" / "package" / "module.py").write_text("value = 1\n", encoding="utf-8")
    (project / "tests" / "test_bad.py").write_text("def test_bad(:\n", encoding="utf-8")
    report = scan(discover_context(project, AnalysisConfig()))
    coverage = next(item for item in report.section_coverage if item.section == "test-impact")
    provider = next(item for item in report.providers if item.provider_id == "native-test-impact")
    assert coverage.state is SectionState.FAILED
    assert provider.state is ProviderState.FAILED
    assert report.completeness is Completeness.FAILED
    assert any(item.code == "test-source-unavailable" for item in report.diagnostics)


def test_fixture_sources_are_not_treated_as_test_modules(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src" / "package").mkdir(parents=True)
    (project / "tests" / "fixtures").mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n', encoding="utf-8"
    )
    (project / "src" / "package" / "module.py").write_text("value = 1\n", encoding="utf-8")
    (project / "tests" / "test_module.py").write_text(
        "def test_value():\n    assert True\n", encoding="utf-8"
    )
    (project / "tests" / "fixtures" / "malformed.py").write_text("def broken(:\n", encoding="utf-8")

    report = scan(discover_context(project, AnalysisConfig()))

    assert len(report.test_modules) == 1
    assert report.test_modules[0].path == "tests/test_module.py"
    assert not any(item.code == "test-source-unavailable" for item in report.diagnostics)
