"""Focused tests for saved-report code browser relationship graphs."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.code_browse import CodeBrowseBindingError, CodeBrowseReader
from code_analysis.code_browse_graph import (
    CodeGraphLayer,
    CodeGraphRequest,
    CodeGraphResult,
    build_code_graph,
)
from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness
from code_analysis.project import discover_context
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json
from code_analysis.service import scan

FIXTURES = Path(__file__).parent / "fixtures"


def _digest(report: AnalysisReport) -> str:
    return hashlib.sha256(canonical_json(report).encode()).hexdigest()


def _reader(report: AnalysisReport) -> CodeBrowseReader:
    return CodeBrowseReader.from_report(report, _digest(report))


def _graph_request(report: AnalysisReport, **updates: object) -> CodeGraphRequest:
    payload = {
        "report_sha256": _digest(report),
        "revision": 0,
        "layer": CodeGraphLayer.CONTAINMENT,
        "path": "",
        "identity": "",
        "depth": 1,
        "max_nodes": 100,
        "max_edges": 250,
    }
    payload.update(updates)
    return CodeGraphRequest.model_validate(payload)


@pytest.fixture
def saved_ordinary(tmp_path: Path) -> AnalysisReport:
    source = tmp_path / "source"
    shutil.copytree(FIXTURES / "ordinary", source)
    report = scan(discover_context(source, AnalysisConfig()))
    shutil.rmtree(source)
    return AnalysisReport.model_validate_json(report.model_dump_json())


@pytest.fixture
def dictionary_report() -> AnalysisReport:
    return scan(discover_context(FIXTURES / "dictionary_project", AnalysisConfig()))


@pytest.fixture
def structure_report() -> AnalysisReport:
    return scan(discover_context(FIXTURES / "structure_project", AnalysisConfig()))


def test_request_rejects_bools_extras_and_missing_focus(ordinary_report) -> None:
    digest = _digest(ordinary_report)
    with pytest.raises(ValidationError):
        CodeGraphRequest.model_validate(
            {
                "report_sha256": digest,
                "revision": 0,
                "layer": CodeGraphLayer.CONTAINMENT,
                "max_nodes": True,
            }
        )
    with pytest.raises(ValidationError):
        _graph_request(ordinary_report, extra="x")
    with pytest.raises(ValidationError):
        _graph_request(ordinary_report, path="../secret.py")
    with pytest.raises(ValidationError):
        CodeGraphRequest.model_validate(
            {
                "report_sha256": digest,
                "revision": 0,
                "layer": CodeGraphLayer.IMPORTS,
            }
        )


def test_stale_hash_raises_binding_error(ordinary_report) -> None:
    reader = _reader(ordinary_report)
    request = _graph_request(
        ordinary_report,
        report_sha256="0" * 64,
        path=ordinary_report.modules[0].path,
    )
    with pytest.raises(CodeBrowseBindingError):
        build_code_graph(ordinary_report, reader, request)


def test_containment_uses_prefixed_nodes_and_parent_edges(saved_ordinary) -> None:
    report = saved_ordinary
    reader = _reader(report)
    module = next(item for item in report.modules if item.symbols)
    request = _graph_request(report, path=module.path)
    result = build_code_graph(report, reader, request)
    assert result.state == "available"
    assert result.graph is not None
    assert all(node.node_id.startswith("code-browse-contained:") for node in result.graph.nodes)
    symbol = next(
        identity
        for identity in (node.node_id for node in result.graph.nodes)
        if ":" in identity.removeprefix("code-browse-contained:")
    )
    assert any(edge.kind == "span-containment" for edge in result.graph.edges)
    assert symbol not in {node.node_id for node in result.graph.nodes if node.kind == "module"}


def test_containment_bounds_and_partiality(saved_ordinary) -> None:
    report = saved_ordinary
    reader = _reader(report)
    module = next(item for item in report.modules if item.symbols)
    tight = _graph_request(report, path=module.path, max_nodes=2, max_edges=1)
    result = build_code_graph(report, reader, tight)
    assert result.state == "available"
    graph = result.graph
    assert graph is not None
    assert graph.truncated
    assert graph.omitted_node_count > 0 or graph.omitted_edge_count > 0
    assert graph.original_node_count >= len(graph.nodes)
    assert result.completeness is Completeness.PARTIAL


def test_containment_canonical_json_is_stable(saved_ordinary) -> None:
    report = saved_ordinary
    reader = _reader(report)
    path = report.modules[0].path
    request = _graph_request(report, path=path)
    first = build_code_graph(report, reader, request)
    second = build_code_graph(report, reader, request)
    assert canonical_json(first) == canonical_json(second)


def test_containment_survives_deleted_source_tree(saved_ordinary) -> None:
    report = saved_ordinary
    reader = _reader(report)
    path = report.modules[0].path
    result = build_code_graph(report, reader, _graph_request(report, path=path))
    assert result.state == "available"


def test_imports_focus_module_neighborhood(saved_ordinary) -> None:
    report = saved_ordinary
    reader = _reader(report)
    module = report.modules[0]
    request = _graph_request(
        report,
        layer=CodeGraphLayer.IMPORTS,
        identity=module.name,
        depth=1,
    )
    result = build_code_graph(report, reader, request)
    assert result.state == "available"
    assert result.graph is not None
    assert module.name in {node.node_id for node in result.graph.nodes}


def test_static_calls_symbol_alias_focus(saved_ordinary) -> None:
    report = saved_ordinary
    reader = _reader(report)
    profile = next(item for item in report.function_profiles if item.callees)
    subject = next(item for item in report.subjects if item.subject_id == profile.subject_id)
    path = next(module.path for module in report.modules if module.name == subject.module)
    request = _graph_request(
        report,
        layer=CodeGraphLayer.STATIC_CALLS,
        path=path,
        identity=subject.subject_id,
    )
    result = build_code_graph(report, reader, request)
    assert result.state == "available"
    graph = result.graph
    assert graph is not None
    assert graph.edges
    assert all(edge.kind == "recorded-callee-name" for edge in graph.edges)


def test_static_calls_unresolved_targets_have_no_containment_parents(saved_ordinary) -> None:
    report = saved_ordinary
    profiles = tuple(
        profile.model_copy(update={"callees": ("missing.callee",), "unresolved_call_count": 1})
        for profile in report.function_profiles[:1]
    )
    report = report.model_copy(update={"function_profiles": profiles})
    reader = _reader(report)
    profile = report.function_profiles[0]
    canonical = reader.aliases.get(profile.subject_id, profile.subject_id)
    subject = next(item for item in report.subjects if item.subject_id == profile.subject_id)
    path = next(module.path for module in report.modules if module.name == subject.module)
    request = _graph_request(
        report,
        layer=CodeGraphLayer.STATIC_CALLS,
        path=path,
        identity=canonical,
    )
    result = build_code_graph(report, reader, request)
    assert result.state == "available"
    graph = result.graph
    assert graph is not None
    unknown = next(node for node in graph.nodes if node.kind == "unresolved-callee")
    assert unknown.node_id not in {link.node_id for link in result.links}
    assert all(
        edge.target_id != unknown.node_id or edge.kind == "recorded-callee-name"
        for edge in graph.edges
    )
    assert not any(edge.kind in {"module-membership", "span-containment"} for edge in graph.edges)


def test_selected_non_function_never_falls_back_to_module_profiles(saved_ordinary) -> None:
    reader = _reader(saved_ordinary)
    entry = next(item for item in reader.identity_map.values() if item.kind == "class")
    result = build_code_graph(
        saved_ordinary,
        reader,
        _graph_request(
            saved_ordinary,
            layer=CodeGraphLayer.STATIC_CALLS,
            identity=entry.identity,
            path=entry.logical_path,
        ),
    )
    assert result.state == "unavailable" and result.graph is None


def test_graph_result_rejects_foreign_source_and_dangling_links(saved_ordinary) -> None:
    reader = _reader(saved_ordinary)
    result = build_code_graph(
        saved_ordinary, reader, _graph_request(saved_ordinary, path=saved_ordinary.modules[0].path)
    )
    payload = result.model_dump(mode="json")
    assert payload["links"]
    payload["report_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="source identity"):
        CodeGraphResult.model_validate_json(json.dumps(payload))
    payload = result.model_dump(mode="json")
    payload["links"] = [{"node_id": "absent-node", "identity": "unproven"}]
    with pytest.raises(ValidationError, match="absent node"):
        CodeGraphResult.model_validate_json(json.dumps(payload))


def test_graph_and_nested_result_strict_json_roundtrip(saved_ordinary) -> None:
    from code_analysis.projection_models import GraphProjection

    reader = _reader(saved_ordinary)
    result = build_code_graph(
        saved_ordinary, reader, _graph_request(saved_ordinary, path=saved_ordinary.modules[0].path)
    )
    assert CodeGraphResult.model_validate_json(result.model_dump_json()) == result
    assert GraphProjection.model_validate_json(result.graph.model_dump_json()) == result.graph
    payload = result.graph.model_dump(mode="json")
    payload["original_node_count"] = True
    with pytest.raises(ValidationError):
        GraphProjection.model_validate_json(json.dumps(payload))
    payload["original_node_count"] = 0
    with pytest.raises(ValidationError):
        GraphProjection.model_validate_json(json.dumps(payload))


def test_static_calls_without_profiles_is_unavailable(saved_ordinary) -> None:
    report = saved_ordinary.model_copy(update={"function_profiles": ()})
    reader = _reader(report)
    module = next(item for item in report.modules if item.symbols)
    request = _graph_request(
        report,
        layer=CodeGraphLayer.STATIC_CALLS,
        path=module.path,
    )
    result = build_code_graph(report, reader, request)
    assert result.state == "unavailable"
    assert result.graph is None
    assert result.completeness is not Completeness.COMPLETE


def test_static_call_sites_file_focus(structure_report) -> None:
    report = structure_report
    reader = _reader(report)
    source = report.source_files[0]
    request = _graph_request(
        report,
        layer=CodeGraphLayer.STATIC_CALL_SITES,
        path=source.logical_path,
    )
    result = build_code_graph(report, reader, request)
    assert result.state == "available"
    graph = result.graph
    assert graph is not None
    assert any(edge.kind == "source-contains-call-site" for edge in graph.edges)
    assert source.source_file_id in {node.node_id for node in graph.nodes}


def test_static_call_sites_rejects_module_identity(structure_report) -> None:
    report = structure_report
    reader = _reader(report)
    module = report.modules[0]
    request = _graph_request(
        report,
        layer=CodeGraphLayer.STATIC_CALL_SITES,
        identity=module.name,
    )
    result = build_code_graph(report, reader, request)
    assert result.state == "unavailable"


def test_data_flow_shape_owner_focus(dictionary_report) -> None:
    report = dictionary_report
    reader = _reader(report)
    shape = report.dictionary_shapes[0]
    request = _graph_request(
        report,
        layer=CodeGraphLayer.DATA_FLOW,
        identity=shape.shape_id,
    )
    result = build_code_graph(report, reader, request)
    assert result.state == "available"
    graph = result.graph
    assert graph is not None
    assert shape.owner_subject_id in {node.node_id for node in graph.nodes}


def test_data_flow_without_edges_is_unavailable(saved_ordinary) -> None:
    report = saved_ordinary
    reader = _reader(report)
    request = _graph_request(
        report,
        layer=CodeGraphLayer.DATA_FLOW,
        path=report.modules[0].path,
    )
    result = build_code_graph(report, reader, request)
    assert result.state == "unavailable"
    assert result.completeness is not Completeness.COMPLETE


def test_ambiguous_multi_file_identity_is_unavailable(ordinary_report) -> None:
    from code_analysis.models import Confidence
    from code_analysis.report_models import (
        DataStructureRecord,
        StructureScope,
        SubjectKind,
        TypeExpression,
        TypeKind,
    )

    report = ordinary_report
    subject = next(item for item in report.subjects if item.kind is SubjectKind.SYMBOL)
    span_a = subject.evidence[0].span
    extra_path = "extra/other.py"
    span_b = span_a.model_copy(update={"path": extra_path, "start_line": 4, "end_line": 4})
    evidence_b = subject.evidence[0].model_copy(update={"span": span_b})
    structure = report.data_structures[0] if report.data_structures else None
    if structure is None:
        structure = DataStructureRecord(
            structure_id="structure-aaaaaaaaaaaaaaaaaaaa",
            owner_subject_id=subject.subject_id,
            scope=StructureScope.LOCAL,
            canonical_type=TypeExpression(kind=TypeKind.NAMED, name="dict"),
            evidence=subject.evidence,
            confidence=Confidence.EXACT,
        )
    patched = report.model_copy(
        update={
            "source_files": (
                *report.source_files,
                report.source_files[0].model_copy(
                    update={
                        "source_file_id": "source-file-cccccccccccccccccccc",
                        "logical_path": extra_path,
                        "module_name": "extra.other",
                    }
                ),
            ),
            "data_structures": (
                structure.model_copy(update={"evidence": (*structure.evidence, evidence_b)}),
            ),
        }
    )
    reader = _reader(patched)
    request = _graph_request(
        patched,
        layer=CodeGraphLayer.CONTAINMENT,
        identity=structure.structure_id,
    )
    result = build_code_graph(patched, reader, request)
    assert result.state == "unavailable"
    assert any("multiple" in item.casefold() for item in result.limitations)


def test_malformed_result_rejects_complete_unavailable() -> None:
    with pytest.raises(ValidationError):
        CodeGraphResult.model_validate(
            {
                "report_sha256": "a" * 64,
                "revision": 0,
                "layer": CodeGraphLayer.CONTAINMENT,
                "state": "unavailable",
                "completeness": Completeness.COMPLETE,
                "message": "missing graph",
            }
        )
