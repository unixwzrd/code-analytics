from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path

import pytest

from code_analysis.comparisons import build_report_delta
from code_analysis.models import Completeness, Diagnostic, DiagnosticLevel
from code_analysis.queries import build_query
from code_analysis.query_models import QuerySelector, QuerySelectorKind
from code_analysis.reports.json_report import canonical_json
from code_analysis.ui_models import UIComparisonPage
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


def test_overview_identity_is_computed_once_for_concurrent_requests(
    ordinary_report, monkeypatch
) -> None:
    import code_analysis.web_service as views

    calls = []
    original = views.canonical_json

    def counted(report):
        calls.append(report)
        return original(report)

    expected = ReportViewService(ordinary_report).bootstrap().model_dump_json()
    monkeypatch.setattr(views, "canonical_json", counted)
    service = ReportViewService(ordinary_report)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _: service.bootstrap(), range(16)))
    assert calls == [ordinary_report]
    assert all(result.model_dump_json() == expected for result in results)
    assert all(result is results[0] for result in results)


def test_overview_cache_is_selection_local_and_retries_failed_construction(
    ordinary_report, monkeypatch
) -> None:
    import code_analysis.web_service as views

    original = views.canonical_json
    service = ReportViewService(ordinary_report)

    def unavailable(report):
        raise ValueError("synthetic canonicalization failure")

    monkeypatch.setattr(views, "canonical_json", unavailable)
    with pytest.raises(ValueError, match="canonicalization"):
        service.bootstrap()
    monkeypatch.setattr(views, "canonical_json", original)
    first = service.bootstrap()
    changed = ordinary_report.model_copy(update={"limitations": ("Different snapshot",)})
    second = ReportViewService(changed).bootstrap()
    assert first.reports[0].report_sha256 != second.reports[0].report_sha256
    assert service.bootstrap() is first
    comparison = ReportViewService(ordinary_report, changed).bootstrap()
    assert comparison.reports[0] == first.reports[0]
    assert comparison.reports[1].report_sha256 == second.reports[0].report_sha256


def test_web_service_builds_bounded_source_free_views(ordinary_report, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(canonical_json(ordinary_report), encoding="utf-8")
    service = ReportViewService.load(report_path)

    bootstrap = service.bootstrap()
    assert bootstrap.reports[0].report_sha256
    assert bootstrap.comparison is None
    modules = service.modules(offset=0, limit=2)
    assert modules.retained == 2
    assert modules.eligible == len(ordinary_report.modules)
    assert modules.truncated == (len(ordinary_report.modules) > 2)
    assert service.findings(offset=0, limit=100).eligible == len(ordinary_report.findings)
    assert service.comparison_records(offset=0, limit=10).records == ()
    architecture = service.architecture(max_nodes=2, max_edges=2)
    assert architecture.retained_nodes == 2
    assert architecture.eligible_nodes == len(ordinary_report.modules)
    assert {edge.source_id for edge in architecture.edges} <= {
        node.node_id for node in architecture.nodes
    }


def test_web_service_compares_validated_reports(ordinary_report, tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(canonical_json(ordinary_report), encoding="utf-8")
    second.write_text(canonical_json(ordinary_report), encoding="utf-8")

    bootstrap = ReportViewService.load(first, second).bootstrap()
    assert bootstrap.comparison is not None
    assert bootstrap.comparison.identical is True


def test_web_service_rejects_incompatible_comparison(ordinary_report) -> None:
    incompatible = ordinary_report.model_copy(
        update={"target": ordinary_report.target.model_copy(update={"logical_root": "different"})}
    )
    with pytest.raises(ValueError, match="incompatible target identity"):
        ReportViewService(ordinary_report, incompatible)


def test_web_service_rejects_invalid_page_bounds(ordinary_report) -> None:
    service = ReportViewService(ordinary_report)
    with pytest.raises(ValueError, match="offset"):
        service.modules(offset=-1, limit=10)
    with pytest.raises(ValueError, match="limit"):
        service.modules(offset=0, limit=501)


def test_comparison_pages_stop_at_retained_bound(ordinary_report) -> None:
    # A non-identical comparison bounded to one retained record still has a
    # larger global count. Use real comparison assembly to establish the bound.
    candidate = ordinary_report.model_copy(update={"limitations": ("Changed limitation",)})
    delta = build_report_delta(ordinary_report, candidate, max_records=1, include_unchanged=True)
    assert delta.eligible_record_count > len(delta.records)
    service = ReportViewService(ordinary_report, candidate, delta)
    page = service.comparison_records(offset=0, limit=1)
    assert page.eligible == 1
    assert page.retained == 1
    assert not page.truncated
    assert page.omitted == delta.eligible_record_count - 1
    assert page.total_eligible == delta.eligible_record_count
    assert page.completeness is Completeness.PARTIAL
    exhausted = service.comparison_records(offset=1, limit=1)
    assert not exhausted.records
    assert not exhausted.truncated


def test_comparison_page_rejects_false_omission_counts() -> None:
    with pytest.raises(ValueError, match="available and omitted"):
        UIComparisonPage(
            offset=0,
            limit=1,
            eligible=0,
            retained=0,
            truncated=False,
            records=(),
            total_eligible=3,
            omitted=2,
            completeness=Completeness.PARTIAL,
        )


def test_web_evidence_uses_the_saved_query_contract(ordinary_report, tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(canonical_json(ordinary_report), encoding="utf-8")
    service = ReportViewService.load(path)
    module = next(item for item in ordinary_report.modules if item.symbols)
    identity = f"{module.name}:{module.symbols[0].qualified_name}"
    response = service.evidence(kind="symbol", identity=identity, limit=1)
    assert response == build_query(
        ordinary_report,
        QuerySelector(kind=QuerySelectorKind.SYMBOL, identity=identity),
        limit=1,
    )
    assert response.items
    with pytest.raises(ValueError):
        service.evidence(kind="finding", identity="nonexistent")
    with pytest.raises(ValueError):
        service.evidence(kind="arbitrary-path", identity="/etc/passwd")


def test_diagnostics_keep_partial_evidence_visible(ordinary_report) -> None:
    diagnostic = Diagnostic(
        code="unresolved-boundary",
        level=DiagnosticLevel.WARNING,
        message="Boundary classification is unresolved",
        provider="contracts",
        path="app.py",
    )
    report = ordinary_report.model_copy(update={"diagnostics": (diagnostic,)})
    service = ReportViewService(report)
    assert service.diagnostics(offset=0, limit=1, query="CONTRACTS").records == (diagnostic,)
    assert service.diagnostics(offset=0, limit=1, query="app.py").eligible == 1
    assert service.diagnostics(offset=0, limit=1, query="absent").eligible == 0


def test_web_server_requires_loopback_host_and_session_token(ordinary_report) -> None:
    with pytest.raises(ValueError, match="loopback"):
        create_server(ReportViewService(ordinary_report), host="0.0.0.0")

    server = create_server(ReportViewService(ordinary_report), token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request("GET", "/api/bootstrap", headers={"Host": "127.0.0.1"})
        assert connection.getresponse().status == 403
        connection.request(
            "GET",
            "/api/bootstrap",
            headers={"Host": "127.0.0.1", "X-Code-Analysis-Token": "test-token"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Security-Policy")
        payload = json.loads(response.read())
        assert payload["schema_name"] == "code-analysis-ui"

        connection.request("GET", "/icon.svg", headers={"Host": "127.0.0.1"})
        icon = connection.getresponse()
        assert icon.status == 200
        assert icon.getheader("Content-Type") == "image/svg+xml"
        assert b"dependency graph" in icon.read()

        connection.request(
            "GET",
            "/api/architecture?max_nodes=2&max_edges=2",
            headers={"Host": "127.0.0.1", "X-Code-Analysis-Token": "test-token"},
        )
        graph = connection.getresponse()
        assert graph.status == 200
        assert json.loads(graph.read())["retained_nodes"] == 2

        connection.request(
            "GET",
            "/api/modules?limit=1",
            headers={"Host": "example.test", "X-Code-Analysis-Token": "test-token"},
        )
        assert connection.getresponse().status == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
