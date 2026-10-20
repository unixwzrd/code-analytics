"""HTTP and browser coverage for bounded runtime table downloads."""

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

from code_analysis.config import AnalysisConfig
from code_analysis.profile_ingestion import ingest_profiles
from code_analysis.profile_models import ProfileReport
from code_analysis.profiling import render_profile_json
from code_analysis.project import discover_context
from code_analysis.runtime_identity import CollectionMetadata, RuntimeRoot
from code_analysis.runtime_models import CollectionIdentity
from code_analysis.runtime_queries import (
    RuntimeQueryRequest,
    RuntimeQuerySection,
    render_runtime_query_json,
    render_runtime_query_markdown,
    render_runtime_query_tsv,
)
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import GenerationKind, commit_generation, prepare_workspace

GOLDEN = Path(__file__).parent / "golden" / "phase4-profile-report.json"
TOKEN = "runtime-table-downloads"
TABLE_DRIFT = "Runtime table selection changed since this table was rendered"


@pytest.fixture(scope="module")
def profile_report(tmp_path_factory) -> ProfileReport:
    """Source-free profile snapshot with multiple frames for bounded table views."""
    root = tmp_path_factory.mktemp("workload") / "declared"
    (root / "pkg").mkdir(parents=True)
    handler = (str(root / "pkg" / "app.py"), 10, "handler")
    stats = {
        handler: (3, 3, 0.4, 1.5, {}),
        (str(root / "pkg" / "helper.py"), 4, "helper"): (
            5,
            5,
            0.3,
            0.6,
            {handler: (5, 5, 0.3, 0.6)},
        ),
        ("", 0, "len"): (7, 7, 0.2, 0.2, {handler: (7, 7, 0.2, 0.2)}),
    }
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


def expected_payload(section: RuntimeQuerySection, format_: str, profile: ProfileReport) -> bytes:
    projection = (
        RuntimeViewService(profile)
        .panel(RuntimeQueryRequest(section=section, max_records=100))
        .projection
    )
    rendered = {
        "json": render_runtime_query_json,
        "tsv": render_runtime_query_tsv,
        "markdown": render_runtime_query_markdown,
    }[format_](projection)
    return rendered.encode("utf-8")


@pytest.fixture
def runtime_server(profile_report):
    server = create_server(None, token=TOKEN, runtime_service=RuntimeViewService(profile_report))
    with served(server):
        yield server, profile_report


def test_table_download_requires_auth_and_rejects_malformed_requests(runtime_server) -> None:
    server, profile = runtime_server
    path = "/api/runtime/table-download?section=hotspots&format=json"
    assert fetch(server, path, token=None)[0] == 403
    assert fetch(server, path, token="wrong")[0] == 403
    status, body, _ = fetch(server, path)
    assert status == 200 and body == expected_payload(RuntimeQuerySection.HOTSPOTS, "json", profile)
    for invalid in (
        path + "&unexpected=yes",
        "/api/runtime/table-download?section=hotspots&format=dot",
        "/api/runtime/table-download?section=hotspots&format=json&max_records=101",
        "/api/runtime/table-download?section=hotspots&format=json&max_records=0",
        "/api/runtime/table-download?section=unknown&format=json",
        "/api/runtime/table-download?section=resources&format=json&frame=profile-frame-"
        + ("0" * 20),
    ):
        status, body, _ = fetch(server, invalid)
        assert status == 400, invalid
        assert json.loads(body)["error"]["code"] == "invalid-request"


def test_table_download_honors_completeness_and_omission_headers(runtime_server) -> None:
    server, profile = runtime_server
    status, _, headers = fetch(
        server, "/api/runtime/table-download?section=hotspots&format=tsv&max_records=1"
    )
    assert status == 200
    projection = (
        RuntimeViewService(profile)
        .panel(RuntimeQueryRequest(section=RuntimeQuerySection.HOTSPOTS, max_records=1))
        .projection
    )
    assert headers["x-analysis-completeness"] == projection.source_completeness.value
    assert headers["x-projection-truncated"] == str(projection.truncated).lower()
    assert headers["x-runtime-query-omitted"] == str(projection.omitted_count)


@pytest.mark.parametrize("format_", ("json", "tsv", "markdown"))
@pytest.mark.parametrize("section", tuple(RuntimeQuerySection))
def test_table_download_matches_shared_renderer(runtime_server, section, format_) -> None:
    server, profile = runtime_server
    status, body, _ = fetch(
        server, f"/api/runtime/table-download?section={section.value}&format={format_}"
    )
    assert status == 200 and body == expected_payload(section, format_, profile)


def test_table_download_reports_absent_runtime(ordinary_report) -> None:
    server = create_server(ReportViewService(ordinary_report), token=TOKEN)
    with served(server):
        status, body, _ = fetch(server, "/api/runtime/table-download?section=hotspots&format=json")
    assert status == 400
    assert "no saved runtime profile" in json.loads(body)["error"]["message"]


def test_table_download_preserves_null_metrics_from_saved_profile(ordinary_report) -> None:
    server = create_server(
        ReportViewService(ordinary_report),
        profile_report=GOLDEN,
        token=TOKEN,
    )
    with served(server):
        status, body, _ = fetch(server, "/api/runtime/table-download?section=resources&format=json")
    assert status == 200
    projection = (
        RuntimeViewService.load(GOLDEN)
        .panel(RuntimeQueryRequest(section=RuntimeQuerySection.RESOURCES, max_records=100))
        .projection
    )
    assert body == render_runtime_query_json(projection).encode("utf-8")
    assert b'"read_bytes":null' in body


def test_table_download_rejects_unavailable_saved_profile_run(
    ordinary_report, tmp_path, profile_report
) -> None:
    fixtures = Path(__file__).parent / "fixtures" / "ordinary"
    workspace = prepare_workspace(
        discover_context(fixtures, AnalysisConfig()),
        explicit_path=tmp_path / "workspace",
    )
    commit_generation(
        workspace,
        kind=GenerationKind.PROFILE,
        artifacts={"profile.json": render_profile_json(profile_report, compact=True).encode()},
        command=("profile",),
        interpreter_path="/python",
        portable_identity_sha256=profile_report.profile_identity_sha256,
    )
    server = create_server(
        ReportViewService(ordinary_report), workspace=workspace.root, token=TOKEN
    )
    with served(server):
        status, body, _ = fetch(
            server,
            "/api/runtime/table-download?"
            + urlencode(
                {
                    "section": "hotspots",
                    "format": "json",
                    "profile_run": "missing-profile-run",
                }
            ),
        )
    assert status == 400
    assert "profile generation is absent" in json.loads(body)["error"]["message"]


def test_download_preserves_static_and_saved_profile_selection(
    ordinary_report, tmp_path, profile_report
) -> None:
    workspace = prepare_workspace(
        discover_context(Path(__file__).parent / "fixtures" / "ordinary", AnalysisConfig()),
        explicit_path=tmp_path / "workspace",
    )
    manifest = commit_generation(
        workspace,
        kind=GenerationKind.PROFILE,
        artifacts={"profile.json": render_profile_json(profile_report, compact=True).encode()},
        command=("profile",),
        interpreter_path="/python",
        portable_identity_sha256=profile_report.profile_identity_sha256,
    )
    initial = RuntimeViewService.load(GOLDEN).profile
    assert initial.profile_identity_sha256 != profile_report.profile_identity_sha256
    server = create_server(
        ReportViewService(ordinary_report),
        workspace=workspace.root,
        profile_report=GOLDEN,
        token=TOKEN,
    )
    with served(server):
        for identity, expected in (("current", initial), (manifest.generation_id, profile_report)):
            params = {
                "section": "hotspots",
                "format": "json",
                "run": "current",
                "compare_run": "none",
                "profile_run": identity,
                "compare_profile_run": "none",
            }
            status, body, _ = fetch(server, "/api/runtime/table-download?" + urlencode(params))
            assert status == 200, body
            assert body == expected_payload(RuntimeQuerySection.HOTSPOTS, "json", expected)
        params["run"] = "missing-analysis-run"
        status, _, _ = fetch(server, "/api/runtime/table-download?" + urlencode(params))
        assert status == 400


def test_browser_downloads_exact_table_and_refuses_stale_controls(profile_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    server = create_server(None, token=TOKEN, runtime_service=RuntimeViewService(profile_report))
    base = f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}#runtime"
    with served(server), playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(base)
            page.get_by_role("heading", name="Runtime", exact=True).wait_for()
            page.get_by_role("button", name="Show runtime evidence", exact=True).click()
            page.get_by_role("columnheader", name="function", exact=True).wait_for()
            with page.expect_download() as transfer:
                page.get_by_role("button", name="Download JSON", exact=True).click()
            assert Path(transfer.value.path()).read_bytes() == expected_payload(
                RuntimeQuerySection.HOTSPOTS, "json", profile_report
            )

            page.get_by_label("Runtime view", exact=True).select_option("calls")
            download = page.get_by_role("button", name="Download JSON", exact=True)
            status = page.locator(".runtime-table-download-status")
            playwright.expect(status).to_contain_text(TABLE_DRIFT)
            assert download.is_disabled()

            page.get_by_role("button", name="Show runtime evidence", exact=True).click()
            page.get_by_role("columnheader", name="caller frame id", exact=True).wait_for()
            with page.expect_download() as transfer:
                page.get_by_role("button", name="Download TSV", exact=True).click()
            assert Path(transfer.value.path()).read_bytes() == expected_payload(
                RuntimeQuerySection.CALLS, "tsv", profile_report
            )
        finally:
            browser.close()


def test_comparison_table_uses_comparison_download_controls(profile_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    baseline = profile_report
    candidate = ProfileReport.model_validate(
        {
            **baseline.model_dump(),
            "profile_identity_sha256": "b" * 64,
            "source_report_sha256": None,
        }
    )
    from code_analysis.profiling import build_profile_diff

    delta = build_profile_diff(baseline, candidate, diagnostic_only=True)
    server = create_server(
        None,
        token=TOKEN,
        runtime_service=RuntimeViewService(baseline, candidate, delta),
    )
    base = f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}#runtime"
    with served(server), playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(base)
            page.get_by_role("heading", name="Runtime", exact=True).wait_for()
            page.get_by_label("Runtime view", exact=True).select_option("comparison")
            page.get_by_role("button", name="Show runtime evidence", exact=True).click()
            page.get_by_text("Diagnostic comparison only", exact=False).wait_for()
            assert page.locator("[data-runtime-table-format]").count() == 0
            assert page.locator("[data-runtime-comparison-format]").count() == 3
        finally:
            browser.close()


def test_controls_changed_during_table_load_leave_download_disabled(profile_report) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    server = create_server(None, token=TOKEN, runtime_service=RuntimeViewService(profile_report))
    base = f"http://127.0.0.1:{server.server_address[1]}/?token={TOKEN}#runtime"
    with served(server), playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            held = []
            page.route("**/api/runtime/query?*", lambda route: held.append(route))
            page.goto(base)
            page.get_by_role("heading", name="Runtime", exact=True).wait_for()
            playwright.expect(page.locator("#runtime-results")).not_to_have_text(
                "Loading runtime evidence", timeout=5000
            )
            if len(held) == 1:
                held[0].fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        RuntimeViewService(profile_report)
                        .panel(
                            RuntimeQueryRequest(
                                section=RuntimeQuerySection.HOTSPOTS, max_records=100
                            )
                        )
                        .model_dump(mode="json")
                    ),
                )
            page.get_by_label("Runtime view", exact=True).select_option("calls")
            page.get_by_role("button", name="Show runtime evidence", exact=True).click()
            for _ in range(100):
                if len(held) >= 2:
                    break
                page.wait_for_timeout(50)
            assert len(held) >= 2
            assert "section=calls" in held[-1].request.url
            page.get_by_label("Runtime view", exact=True).select_option("hotspots")
            held[-1].fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    RuntimeViewService(profile_report)
                    .panel(RuntimeQueryRequest(section=RuntimeQuerySection.CALLS, max_records=100))
                    .model_dump(mode="json")
                ),
            )
            page.get_by_role("columnheader", name="caller frame id", exact=True).wait_for()
            assert page.get_by_label("Runtime view", exact=True).input_value() == "hotspots"
            assert page.get_by_role("button", name="Download JSON", exact=True).is_disabled()
            playwright.expect(page.locator(".runtime-table-download-status")).to_contain_text(
                TABLE_DRIFT
            )
        finally:
            browser.close()
