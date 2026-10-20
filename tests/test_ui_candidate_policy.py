from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from urllib.parse import urlencode

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.config import load_config_file
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.report_models import RefactorCandidateKind
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.service import scan_refactor
from code_analysis.ui_projections import UIProjectionRequest, project_report
from code_analysis.web_server import _projection_request, create_server
from code_analysis.web_service import ReportViewService


@pytest.fixture(scope="module")
def report():
    return scan_refactor(
        discover_context(
            FIXTURES / "proposal_project", load_config_file(FIXTURES / "proposal-config.toml")
        )
    )


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"candidate_module_patterns": ("package.*",)},
        {"candidate_exclude_patterns": ("*",)},
        {"candidate_kinds": (RefactorCandidateKind.MODULE_COMBINATION,)},
        {"candidate_states": ("reviewable-incomplete",)},
        {"cross_module_only": True},
        {"production_only": True},
        {"min_shared_dimensions": 32},
        {"prefer_existing_owner": True},
        {
            "candidate_module_patterns": ("*",),
            "candidate_exclude_patterns": ("*.missing",),
            "production_only": True,
            "cross_module_only": True,
            "prefer_existing_owner": True,
        },
    ],
)
def test_ui_worklist_is_authoritative_selection_and_ranking(report, options):
    request = UIProjectionRequest(name=ProjectionName.CANDIDATE_WORKLIST, max_records=1, **options)
    result = project_report(report, request)
    expected = build_projection(
        report,
        ProjectionName.CANDIDATE_WORKLIST,
        max_records=1,
        max_nodes=50,
        max_edges=100,
        **options,
    )
    assert result == expected
    assert result.inputs == expected.inputs


@pytest.mark.parametrize(
    "options",
    [
        {"production_only": 1},
        {"cross_module_only": "true"},
        {"min_shared_dimensions": True},
        {"min_shared_dimensions": 33},
        {"candidate_states": ("approved",)},
        {"candidate_kinds": ("approved",)},
        {"candidate_module_patterns": ("/tmp/*",)},
        {"candidate_exclude_patterns": ("../*",)},
        {"candidate_module_patterns": ("a\nb",)},
        {"candidate_module_patterns": ("z", "a")},
    ],
)
def test_strict_worklist_policy(options):
    with pytest.raises(ValidationError):
        UIProjectionRequest(name=ProjectionName.CANDIDATE_WORKLIST, **options)


def test_worklist_only_and_unavailable_decisions_rejected():
    with pytest.raises(ValidationError, match="candidate-worklist"):
        UIProjectionRequest(name=ProjectionName.MODULES, production_only=True)
    with pytest.raises(ValueError, match="explicit ledger selection"):
        _projection_request({"name": ["candidate-worklist"], "candidate_decisions": ["accepted"]})
    with pytest.raises(ValueError, match="true or false"):
        _projection_request({"name": ["candidate-worklist"], "production_only": ["1"]})
    with pytest.raises(ValueError, match="supplied once"):
        _projection_request({"production_only": ["true", "false"]})
    request = _projection_request(
        {
            "name": ["candidate-worklist"],
            "candidate_module_patterns": ["z", "a", "a"],
            "candidate_kinds": ["module-combination", "module-combination"],
        }
    )
    assert request.candidate_module_patterns == ("a", "z")
    assert request.candidate_kinds == (RefactorCandidateKind.MODULE_COMBINATION,)


def test_filtered_projection_and_download_share_identity(report):
    server = create_server(ReportViewService(report), token="candidate-policy")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        params = {
            "name": "candidate-worklist",
            "cross_module_only": "true",
            "prefer_existing_owner": "true",
            "max_records": "1",
        }
        expected = project_report(report, _projection_request({k: [v] for k, v in params.items()}))
        for endpoint, suffix in (("projection", ""), ("download", "&format=tsv")):
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            connection.request(
                "GET",
                f"/api/{endpoint}?{urlencode(params)}{suffix}",
                headers={"X-Code-Analysis-Token": "candidate-policy"},
            )
            response = connection.getresponse()
            assert response.status == 200
            payload = response.read()
            connection.close()
            if endpoint == "projection":
                assert json.loads(payload) == expected.model_dump(mode="json")
            else:
                assert payload == render_projection(expected, ProjectionFormat.TSV).encode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
