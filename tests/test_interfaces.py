from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.config import AnalysisConfig, load_project_config
from code_analysis.interface_reporting import _classify_nullability, integrate_interface_report
from code_analysis.models import Completeness, ProviderState, SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.report_models import (
    AnalysisReport,
    BoundaryClass,
    InterfaceMismatchKind,
    InterfaceRole,
    InterfaceTransport,
    SectionState,
    TypeExpression,
    TypeKind,
)
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.service import scan


def _report():
    project = FIXTURES / "interface_project"
    return scan(discover_context(project, load_project_config(project)))


@pytest.mark.parametrize(
    ("name", "payload", "language"),
    (
        ("app.py", b"value = 1\n", SourceLanguage.PYTHON),
        ("app.js", b"export const value = 1;\n", SourceLanguage.JAVASCRIPT),
    ),
)
def test_unavailable_interface_source_retains_partial_evidence(
    tmp_path, monkeypatch, name, payload, language
):
    target = tmp_path / name
    target.write_bytes(payload)
    context = discover_context(target, AnalysisConfig(test_roots=()))
    report = scan(context)
    original = Path.read_bytes

    def unavailable(path, *args, **kwargs):
        if path == target:
            raise PermissionError("synthetic unavailable source")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    result = integrate_interface_report(context, report)
    section = next(item for item in result.section_coverage if item.section == "interfaces")
    provider = result.providers[-1]
    assert section.state is SectionState.PARTIAL and section.unresolved == 1
    assert provider.provider_id == "native-interfaces" and provider.state is ProviderState.PARTIAL
    assert result.completeness is Completeness.PARTIAL
    assert provider.diagnostics[0].code == "interface-source-read-error"
    assert provider.diagnostics[0].language is language
    assert result.source_files == report.source_files and result.modules == report.modules


def test_optional_with_unnamed_expression_allows_null() -> None:
    expression = TypeExpression(
        kind=TypeKind.OPTIONAL,
        arguments=(TypeExpression(kind=TypeKind.SCALAR, name="str"),),
    )
    assert expression.name is None
    assert _classify_nullability(expression) is True


def test_nested_union_with_optional_member_allows_null() -> None:
    expression = TypeExpression(
        kind=TypeKind.UNION,
        arguments=(
            TypeExpression(kind=TypeKind.SCALAR, name="int"),
            TypeExpression(
                kind=TypeKind.UNION,
                arguments=(
                    TypeExpression(kind=TypeKind.NAMED, name="Record"),
                    TypeExpression(
                        kind=TypeKind.OPTIONAL,
                        arguments=(TypeExpression(kind=TypeKind.SCALAR, name="str"),),
                    ),
                ),
            ),
        ),
    )
    assert _classify_nullability(expression) is True


def test_non_null_union_does_not_allow_null() -> None:
    expression = TypeExpression(
        kind=TypeKind.UNION,
        arguments=(
            TypeExpression(kind=TypeKind.SCALAR, name="str"),
            TypeExpression(kind=TypeKind.NAMED, name="Record"),
        ),
    )
    assert _classify_nullability(expression) is False


def test_literal_union_containing_json_null_allows_null() -> None:
    expression = TypeExpression(
        kind=TypeKind.UNION,
        arguments=(
            TypeExpression(kind=TypeKind.LITERAL, literal_values=("ready", None)),
            TypeExpression(kind=TypeKind.SCALAR, name="str"),
        ),
    )
    assert _classify_nullability(expression) is True


def test_unnamed_unknown_nullability_remains_unresolved() -> None:
    expression = TypeExpression(kind=TypeKind.UNKNOWN)
    assert expression.name is None
    assert _classify_nullability(expression) is None


def test_unknown_contract_field_propagates_partial_interface_evidence() -> None:
    project = FIXTURES / "interface_unknown_project"
    report = scan(discover_context(project, load_project_config(project)))
    field = next(
        field
        for variant in report.interface_payload_variants
        for field in variant.fields
        if field.name == "payload"
    )
    assert field.allows_null is None
    assert report.completeness is Completeness.PARTIAL
    assert (
        next(
            item for item in report.interface_payload_variants if field in item.fields
        ).completeness
        is Completeness.PARTIAL
    )
    provider = next(item for item in report.providers if item.provider_id == "native-interfaces")
    assert provider.completeness is Completeness.PARTIAL


def test_python_javascript_interfaces_correlate_without_execution() -> None:
    report = _report()
    assert report.completeness is Completeness.COMPLETE
    assert {item.language for item in report.interface_endpoints} == {
        SourceLanguage.PYTHON,
        SourceLanguage.JAVASCRIPT,
    }
    assert {item.transport for item in report.interface_endpoints} >= {
        InterfaceTransport.HTTP,
        InterfaceTransport.WEBSOCKET,
    }
    producers = [item for item in report.interface_endpoints if item.role is InterfaceRole.PRODUCER]
    consumers = [item for item in report.interface_endpoints if item.role is InterfaceRole.CONSUMER]
    assert {item.normalized_path for item in producers} & {
        item.normalized_path for item in consumers
    } == {"/api/events/{}", "/api/jobs/{}"}
    assert len(report.interface_correlations) == 2
    assert {item.kind for item in report.interface_mismatches} == {
        InterfaceMismatchKind.REQUEST_FIELD,
        InterfaceMismatchKind.MISSING_NULL,
    }
    assert {item.field_name for item in report.interface_mismatches} == {"extra", "force"}
    classes = {item.boundary_class for item in report.interface_payload_variants}
    assert BoundaryClass.HTTP_REQUEST in classes
    assert BoundaryClass.HTTP_RESPONSE in classes
    assert report.summary.interface_endpoints == len(report.interface_endpoints)
    assert {item.section: item for item in report.section_coverage}[
        "interfaces"
    ].state.value == "complete"
    for name in (
        ProjectionName.PRODUCER_CONSUMER_SCHEMA_DRIFT,
        ProjectionName.FRONTEND_BACKEND_INTERFACE_MISMATCHES,
        ProjectionName.OWNERSHIP_CONTRACT_FLOW,
    ):
        projection = build_projection(report, name)
        assert projection.record_count


def test_interface_report_is_deterministic_strict_and_portable() -> None:
    first = _report()
    second = _report()
    assert render_json(first) == render_json(second)
    retained = load_json(render_json(first))
    assert retained.interface_correlations == first.interface_correlations
    malformed = retained.model_dump()
    malformed["interface_correlations"][0]["producer_endpoint_id"] = (
        "interface-endpoint-" + "f" * 20
    )
    try:
        AnalysisReport.model_validate(malformed)
    except ValidationError as exc:
        assert "unknown IDs" in str(exc)
    else:
        raise AssertionError("dangling interface endpoint was accepted")
    for forbidden in ("/Users/", "/Volumes/", "/tmp/", "/private/var/"):
        assert forbidden not in render_json(first)


def test_javascript_wrapper_and_dynamic_query_keep_literal_endpoint_identity() -> None:
    report = _report()
    javascript = [
        item for item in report.interface_endpoints if item.language is SourceLanguage.JAVASCRIPT
    ]
    assert any(
        item.transport is InterfaceTransport.HTTP
        and item.method == "GET"
        and item.normalized_path == "/api/jobs/{}"
        for item in javascript
    )
    assert (
        sum(
            item.transport is InterfaceTransport.HTTP
            and item.method == "GET"
            and item.normalized_path == "/api/jobs/{}"
            for item in javascript
        )
        == 2
    )
    assert {
        item.normalized_path for item in javascript if item.transport is InterfaceTransport.HTTP
    } >= {
        "/api/jobs/{}/start",
        "/api/jobs/{}/plan",
        "/api/jobs/{}/fallback",
    }
    assert any(
        item.transport is InterfaceTransport.SSE and item.normalized_path == "/api/events/{}"
        for item in javascript
    )
    assert not any(item.module.endswith("web.lib.dom") for item in javascript)
    assert not any(item.code == "interface-route-unresolved" for item in report.diagnostics)
