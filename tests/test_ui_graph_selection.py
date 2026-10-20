from __future__ import annotations

import json
import shutil
import threading
from http.client import HTTPConnection
from urllib.parse import urlencode

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName
from code_analysis.service import scan
from code_analysis.ui_navigation import navigation_page
from code_analysis.ui_projections import (
    UIGraphNodeSelection,
    UIProjectionRequest,
    graph_node_selection,
    project_report,
)
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


@pytest.fixture(
    params=[
        ("ordinary", ProjectionName.SYMBOL_GRAPH, "alpha.a:"),
        ("structure_project", ProjectionName.OWNERSHIP_ALIAS_FLOW, "structure-"),
        ("structure_project", ProjectionName.STRUCTURE_LIFECYCLE_GRAPH, "state-"),
        ("structure_project", ProjectionName.CONVERSION_GRAPH, "subject-"),
        ("contract_golden", ProjectionName.CONTRACT_BOUNDARY_GRAPH, "contract-"),
        ("test_impact_project", ProjectionName.TEST_IMPACT_GRAPH, "test-"),
        ("interface_project", ProjectionName.OWNERSHIP_CONTRACT_FLOW, "interface-endpoint-"),
    ]
)
def graph_case(request, tmp_path):
    fixture, name, prefix = request.param
    source = tmp_path / "source"
    shutil.copytree(FIXTURES / fixture, source)
    report = scan(discover_context(source, AnalysisConfig()))
    shutil.rmtree(source)
    selection = UIProjectionRequest(name=name)
    graph = project_report(report, selection)
    return report, selection, graph, prefix


def test_domain_graph_selection_is_report_bound_and_source_free(graph_case):
    report, request, graph, prefix = graph_case
    nodes = [node for node in graph.nodes if node.node_id.startswith(prefix)]
    assert nodes
    for node in nodes:
        result = graph_node_selection(report, request, graph.projection_id, node.node_id)
        assert result.node_kind == node.kind and result.label == node.label
        assert result.completeness == graph.completeness
        assert result.report_sha256 == graph.inputs[0].report_sha256
        if node.kind == "unresolved-target":
            assert result.navigation_identity is None and result.unavailable_reason
        else:
            assert result.navigation_identity == node.node_id
            page = navigation_page(report, result.navigation_identity)
            assert page.records
            assert page.completeness == report.completeness
            assert result == graph_node_selection(
                report, request, graph.projection_id, node.node_id
            )
        with pytest.raises(ValidationError):
            UIGraphNodeSelection.model_validate({**result.model_dump(), "unexpected": True})
    with pytest.raises(ValueError, match="does not match"):
        graph_node_selection(report, request, "projection-" + "0" * 20, nodes[0].node_id)
    with pytest.raises(ValueError, match="not retained"):
        graph_node_selection(report, request, graph.projection_id, "missing-node")


def test_selection_closed_availability_states(graph_case):
    report, request, graph, _ = graph_case
    result = graph_node_selection(report, request, graph.projection_id, graph.nodes[0].node_id)
    for updates in (
        {"navigation_identity": None, "unavailable_reason": None},
        {"navigation_identity": graph.nodes[0].node_id, "unavailable_reason": "unavailable"},
        {"navigation_identity": "wrong", "unavailable_reason": None},
        {"navigation_identity": None, "unavailable_reason": ""},
    ):
        with pytest.raises(ValidationError):
            UIGraphNodeSelection.model_validate({**result.model_dump(), **updates})


def test_graph_selection_http_validates_auth_projection_and_node(graph_case):
    report, request, graph, prefix = graph_case
    node = next(node for node in graph.nodes if node.node_id.startswith(prefix))
    server = create_server(ReportViewService(report), token="graph-node")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port)
    params = {
        "name": request.name.value,
        "projection_id": graph.projection_id,
        "node": node.node_id,
    }
    try:
        for changes, token, status in (
            ({}, True, 200),
            ({"node": "missing"}, True, 400),
            ({"projection_id": "projection-" + "0" * 20}, True, 400),
            ({}, False, 403),
        ):
            connection.request(
                "GET",
                "/api/projection-node?" + urlencode(params | changes),
                headers={"X-Code-Analysis-Token": "graph-node"} if token else {},
            )
            response = connection.getresponse()
            assert response.status == status
            payload = json.loads(response.read())
            if status == 200:
                assert payload == graph_node_selection(
                    report, request, graph.projection_id, node.node_id
                ).model_dump(mode="json")
    finally:
        connection.close()
        server.shutdown()
        thread.join(5)
        server.server_close()
