from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from http.client import HTTPConnection
from urllib.parse import urlencode

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.config import load_config_file
from code_analysis.decision_models import MaintainerDecision
from code_analysis.decisions import set_decision
from code_analysis.project import discover_context
from code_analysis.service import scan_refactor
from code_analysis.ui_decisions import read_decision_snapshot
from code_analysis.ui_models import UIEvidenceBinding
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import prepare_workspace


@pytest.fixture
def saved(tmp_path):
    context = discover_context(
        FIXTURES / "proposal_project", load_config_file(FIXTURES / "proposal-config.toml")
    )
    report = scan_refactor(context)
    workspace = prepare_workspace(context, explicit_path=tmp_path / "workspace")
    return report, workspace


@contextmanager
def serving(report, workspace=None):
    service = ReportViewService(report)
    server = create_server(
        service, token="evidence-binding", workspace=workspace.root if workspace else None
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], service.bootstrap().reports[0].report_sha256
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get(port, route, params):
    connection = HTTPConnection("127.0.0.1", port)
    try:
        connection.request(
            "GET",
            route + "?" + urlencode(params),
            headers={"X-Code-Analysis-Token": "evidence-binding"},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize(
    "route", ["/api/candidate", "/api/evidence", "/api/navigation", "/api/source"]
)
def test_originating_report_binding_precedes_evidence_or_source_read(saved, route):
    report, _workspace = saved
    identity = report.refactor_candidates[0].candidate_id
    with serving(report) as (port, digest):
        params = {"identity": identity, "kind": "refactor-candidate", "report_sha256": "0" * 64}
        status, body = get(port, route, params)
        assert status == 400
        assert "does not match the selected report" in body["error"]["message"]
        if route == "/api/candidate":
            params = {"identity": identity, "report_sha256": digest, "limit": 1}
            status, first = get(port, route, params)
            assert status == 200 and first["candidate"]["candidate_id"] == identity
            assert first["next_offset"] is not None
            status, later = get(port, route, {**params, "offset": first["next_offset"]})
            assert status == 200 and later["offset"] > first["offset"]
            # The ordinary unbound inventory remains usable without a ledger.
            assert get(port, route, {"identity": identity})[0] == 200


def record(report, workspace, decision):
    set_decision(
        workspace,
        report,
        candidate_id=report.refactor_candidates[0].candidate_id,
        proposal_id=None,
        decision=decision,
        reason="Synthetic decision for bound packet review.",
        recorded_at=datetime(2026, 9, 19, tzinfo=UTC),
    )


def test_changed_ledger_blocks_packet_and_continuation_without_mutating_it(saved):
    report, workspace = saved
    record(report, workspace, MaintainerDecision.DEFERRED_PREREQUISITE)
    snapshot = read_decision_snapshot(report, workspace)
    assert snapshot is not None
    path = workspace.decisions / "ledger.json"
    original = path.read_bytes()
    with serving(report, workspace) as (port, digest):
        params = {
            "identity": report.refactor_candidates[0].candidate_id,
            "report_sha256": digest,
            "decision_ledger_sha256": snapshot.ledger_sha256,
            "limit": 1,
        }
        status, first = get(port, "/api/candidate", params)
        assert status == 200 and path.read_bytes() == original
        record(report, workspace, MaintainerDecision.ACCEPTED)
        changed = path.read_bytes()
        for route in ("/api/candidate", "/api/evidence", "/api/source"):
            status, body = get(port, route, {**params, "offset": first["next_offset"]})
            assert status == 400 and "snapshot changed" in body["error"]["message"]
        assert path.read_bytes() == changed


def test_binding_rejects_malformed_unpaired_and_unowned_requests(saved):
    report, _workspace = saved
    with serving(report) as (port, digest):
        identity = report.refactor_candidates[0].candidate_id
        for binding in (
            {"report_sha256": "bad"},
            {"decision_ledger_sha256": "0" * 64},
            {"report_sha256": digest, "decision_ledger_sha256": ""},
            {"report_sha256": digest, "decision_ledger_sha256": "0" * 64},
        ):
            assert get(port, "/api/candidate", {"identity": identity, **binding})[0] == 400
    for values in (
        {"report_sha256": True},
        {"report_sha256": "0" * 64, "unexpected": "value"},
        {"report_sha256": "0" * 64, "decision_ledger_sha256": False},
    ):
        with pytest.raises(ValidationError):
            UIEvidenceBinding.model_validate(values)
