"""HTTP and browser coverage for bounded runtime comparison downloads."""

from __future__ import annotations

import json
import marshal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlencode

import pytest

from code_analysis.profile_ingestion import ingest_profiles
from code_analysis.profile_models import ProfileReport
from code_analysis.profiling import build_profile_diff
from code_analysis.runtime_identity import CollectionMetadata, RuntimeRoot
from code_analysis.runtime_models import CollectionIdentity
from code_analysis.ui_runtime import (
    RuntimeViewService,
    render_runtime_comparison_json,
    render_runtime_comparison_markdown,
    render_runtime_comparison_tsv,
)
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

GOLDEN = Path(__file__).parent / "golden" / "phase4-profile-report.json"
TOKEN = "runtime-comparison-downloads"
COMPARISON_DRIFT = "Runtime comparison selection changed since this comparison was rendered"


@contextmanager
def served(server) -> Iterator:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def fetch(
    server,
    path: str,
    *,
    token: str | None = TOKEN,
) -> tuple[int, bytes, dict[str, str]]:
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        headers = {"X-Code-Analysis-Token": token} if token is not None else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, body, {key.lower(): value for key, value in response.getheaders()}
    finally:
        connection.close()


@pytest.fixture
def profile_report(tmp_path_factory) -> ProfileReport:
    root = tmp_path_factory.mktemp("workload") / "declared"
    (root / "pkg").mkdir(parents=True)
    handler = (str(root / "pkg" / "app.py"), 10, "handler")
    stats = {handler: (3, 3, 0.4, 1.5, {})}
    capture = root / "capture.prof"
    capture.write_bytes(marshal.dumps(stats))
    report, _ = ingest_profiles(
        (capture,),
        metadata=CollectionMetadata(
            collection=CollectionIdentity(),
            roots=(RuntimeRoot(physical=root, logical="project", classification="project"),),
        ),
    )
    return report


def comparison_service() -> RuntimeViewService:
    original = RuntimeViewService.load(GOLDEN).profile
    frames = tuple(
        original.frames[0].model_copy(
            update={
                "frame_id": f"profile-frame-{number:020x}",
                "function": f"function_{number:03d}",
            }
        )
        for number in range(131)
    )
    baseline = ProfileReport.model_validate(
        {
            **original.model_dump(),
            "frames": frames,
            "runtime_edges": (),
            "statistics": (),
        }
    )
    candidate = ProfileReport.model_validate(
        {
            **baseline.model_dump(),
            "profile_identity_sha256": "b" * 64,
            "source_report_sha256": None,
        }
    )
    delta = build_profile_diff(baseline, candidate, diagnostic_only=True)
    return RuntimeViewService(baseline, candidate, delta)


def expected_payload(service: RuntimeViewService, format_: str) -> bytes:
    comparison = service.comparison_view()
    rendered = {
        "json": render_runtime_comparison_json,
        "tsv": render_runtime_comparison_tsv,
        "markdown": lambda value: render_runtime_comparison_markdown(
            value, service, static_identity=None
        ),
    }[format_](comparison)
    return rendered.encode("utf-8")


@pytest.fixture
def comparison_server():
    service = comparison_service()
    server = create_server(None, token=TOKEN, runtime_service=service)
    with served(server):
        yield server, service


def test_comparison_download_requires_auth_and_rejects_malformed_requests(
    comparison_server,
) -> None:
    server, service = comparison_server
    path = "/api/runtime/comparison-download?format=json"
    assert fetch(server, path, token=None)[0] == 403
    assert fetch(server, path, token="wrong")[0] == 403
    status, body, _ = fetch(server, path)
    assert status == 200 and body == expected_payload(service, "json")
    for invalid in (
        path + "&unexpected=yes",
        "/api/runtime/comparison-download?format=dot",
        "/api/runtime/comparison-download?format=json&section=comparison",
        "/api/runtime/comparison-download?format=json&max_records=50",
        "/api/runtime/comparison-download?format=json&frame=profile-frame-" + ("0" * 20),
    ):
        status, body, _ = fetch(server, invalid)
        assert status == 400, invalid
        assert json.loads(body)["error"]["code"] == "invalid-request"


def test_comparison_download_honors_completeness_and_omission_headers(comparison_server) -> None:
    server, service = comparison_server
    status, _, headers = fetch(server, "/api/runtime/comparison-download?format=tsv")
    comparison = service.comparison_view()
    assert status == 200
    assert headers["x-analysis-completeness"] == "partial"
    assert headers["x-projection-truncated"] == "true"
    assert headers["x-runtime-comparison-omitted"] == str(comparison.omitted)
    assert headers["x-runtime-comparison-eligible"] == str(comparison.eligible)
    assert comparison.omitted == 31
    assert len(comparison.report.records) == 100


@pytest.mark.parametrize("format_", ("json", "tsv", "markdown"))
def test_comparison_download_matches_shared_renderer(comparison_server, format_) -> None:
    server, service = comparison_server
    status, body, _ = fetch(server, f"/api/runtime/comparison-download?format={format_}")
    assert status == 200 and body == expected_payload(service, format_)


def test_comparison_download_reports_absent_comparison(profile_report) -> None:
    server = create_server(None, token=TOKEN, runtime_service=RuntimeViewService(profile_report))
    with served(server):
        status, body, _ = fetch(server, "/api/runtime/comparison-download?format=json")
    assert status == 400
    assert "no comparison profile" in json.loads(body)["error"]["message"]


def test_comparison_download_json_retains_envelope_counts(comparison_server) -> None:
    server, service = comparison_server
    status, body, _ = fetch(server, "/api/runtime/comparison-download?format=json")
    assert status == 200
    payload = json.loads(body)
    assert payload["eligible"] == 131
    assert payload["omitted"] == 31
    assert len(payload["report"]["records"]) == 100
    assert payload["report"]["claim_eligible"] is False
    assert payload["report"]["diagnostic_only"] is True
    assert payload["report"]["decisions"]
    assert payload["report"]["baseline_profile_identity_sha256"] == (
        service.profile.profile_identity_sha256
    )


def test_comparison_download_tsv_quotes_and_nulls() -> None:
    service = comparison_service()
    assert service.delta is not None
    quoted = service.delta.records[0].model_copy(
        update={
            "frame_identity": "package/module.py:10:function\t000",
            "baseline_total_seconds": None,
        }
    )
    records = (quoted, *service.delta.records[1:])
    delta = service.delta.model_copy(update={"records": records})
    service = RuntimeViewService(service.profile, service.comparison, delta)
    server = create_server(None, token=TOKEN, runtime_service=service)
    with served(server):
        status, body, _ = fetch(server, "/api/runtime/comparison-download?format=tsv")
    assert status == 200
    text = body.decode("utf-8")
    assert text.startswith("frame_identity\t")
    assert "unavailable" in text
    assert "function\t000" in text or "function\\t000" in text
    assert any('"' in line for line in text.splitlines()[1:])


def test_browser_downloads_exact_comparison_and_refuses_stale_controls(comparison_server) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    server, service = comparison_server
    base = f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}#runtime"
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(base)
            page.get_by_role("heading", name="Runtime", exact=True).wait_for()
            page.get_by_label("Runtime view", exact=True).select_option("comparison")
            page.get_by_role("button", name="Show runtime evidence", exact=True).click()
            page.get_by_text("Diagnostic comparison only", exact=False).wait_for()
            with page.expect_download() as transfer:
                page.get_by_role("button", name="Download JSON", exact=True).click()
            assert Path(transfer.value.path()).read_bytes() == expected_payload(service, "json")

            page.get_by_label("Runtime view", exact=True).select_option("hotspots")
            download = page.get_by_role("button", name="Download JSON", exact=True)
            status = page.locator(".runtime-comparison-download-status")
            playwright.expect(status).to_contain_text(COMPARISON_DRIFT)
            assert download.is_disabled()

            page.get_by_label("Runtime view", exact=True).select_option("comparison")
            page.get_by_role("button", name="Show runtime evidence", exact=True).click()
            page.get_by_text("Diagnostic comparison only", exact=False).wait_for()
            with page.expect_download() as transfer:
                page.get_by_role("button", name="Download TSV", exact=True).click()
            assert Path(transfer.value.path()).read_bytes() == expected_payload(service, "tsv")
        finally:
            browser.close()


def test_controls_changed_during_comparison_load_leave_download_disabled(comparison_server) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    server, _ = comparison_server
    base = f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}#runtime"
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            held = []
            page.route("**/api/runtime/comparison", lambda route: held.append(route))
            page.goto(base)
            page.get_by_role("heading", name="Runtime", exact=True).wait_for()
            page.get_by_label("Runtime view", exact=True).select_option("comparison")
            page.get_by_role("button", name="Show runtime evidence", exact=True).click()
            for _ in range(100):
                if held:
                    break
                page.wait_for_timeout(50)
            assert held
            page.get_by_label("Runtime view", exact=True).select_option("hotspots")
            held[0].fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(comparison_service().comparison_view().model_dump(mode="json")),
            )
            page.get_by_role("columnheader", name="frame identity", exact=True).wait_for()
            assert page.get_by_label("Runtime view", exact=True).input_value() == "hotspots"
            assert page.get_by_role("button", name="Download JSON", exact=True).is_disabled()
            playwright.expect(page.locator(".runtime-comparison-download-status")).to_contain_text(
                COMPARISON_DRIFT
            )
        finally:
            browser.close()


def test_comparison_download_preserves_distinct_profile_selection(
    ordinary_report, tmp_path
) -> None:
    from code_analysis.config import AnalysisConfig
    from code_analysis.profiling import render_profile_json
    from code_analysis.project import discover_context
    from code_analysis.workspace import GenerationKind, commit_generation, prepare_workspace

    service = comparison_service()
    workspace = prepare_workspace(
        discover_context(Path(__file__).parent / "fixtures" / "ordinary", AnalysisConfig()),
        explicit_path=tmp_path / "workspace",
    )
    manifest = commit_generation(
        workspace,
        kind=GenerationKind.PROFILE,
        artifacts={"profile.json": render_profile_json(service.profile, compact=True).encode()},
        command=("profile",),
        interpreter_path="/python",
        portable_identity_sha256=service.profile.profile_identity_sha256,
    )
    assert service.comparison is not None
    candidate_manifest = commit_generation(
        workspace,
        kind=GenerationKind.PROFILE,
        artifacts={"profile.json": render_profile_json(service.comparison, compact=True).encode()},
        command=("profile",),
        interpreter_path="/python",
        portable_identity_sha256=service.comparison.profile_identity_sha256,
    )
    server = create_server(
        ReportViewService(ordinary_report),
        workspace=workspace.root,
        token=TOKEN,
    )
    params = {
        "format": "json",
        "run": "current",
        "compare_run": "none",
        "profile_run": manifest.generation_id,
        "compare_profile_run": "none",
    }
    with served(server):
        status, body, _ = fetch(server, "/api/runtime/comparison-download?" + urlencode(params))
        assert status == 400
        assert "no comparison profile" in json.loads(body)["error"]["message"]
        params["compare_profile_run"] = candidate_manifest.generation_id
        status, body, _ = fetch(server, "/api/runtime/comparison-download?" + urlencode(params))
        assert status == 200, body
        assert body == expected_payload(service, "json")
        params["profile_run"], params["compare_profile_run"] = (
            params["compare_profile_run"],
            params["profile_run"],
        )
        status, body, _ = fetch(server, "/api/runtime/comparison-download?" + urlencode(params))
        reversed_service = RuntimeViewService(
            service.comparison,
            service.profile,
            build_profile_diff(service.comparison, service.profile, diagnostic_only=True),
        )
        assert status == 200, body
        assert body == expected_payload(reversed_service, "json")
