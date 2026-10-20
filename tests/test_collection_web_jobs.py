from __future__ import annotations

import errno
import json
import marshal
import threading
import time
from http.client import HTTPConnection

import pytest
from browser_navigation import reveal_navigation

from code_analysis.collection_execution import CollectionExecutionLimits
from code_analysis.web_server import create_server


@pytest.fixture
def server(tmp_path):
    (tmp_path / "sample.py").write_text("def answer():\n    return 42\n")
    server = create_server(None, collection_root=tmp_path, allow_collection=True, token="jobs-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def request(server, path, body=None, **headers):
    host = f"127.0.0.1:{server.server_address[1]}"
    connection = HTTPConnection(host, timeout=15)
    defaults = {
        "X-Code-Analysis-Token": "jobs-test",
        "Origin": f"http://{host}",
        "Content-Type": "application/json",
    }
    defaults.update(headers)
    connection.request(
        "POST" if body is not None else "GET",
        path,
        json.dumps(body) if body is not None else None,
        defaults,
    )
    response = connection.getresponse()
    status, payload = response.status, json.loads(response.read())
    connection.close()
    return status, payload


def launch_body(server):
    options = {"target": "sample.py", "workspace": ".code-analysis"}
    status, command = request(server, "/api/collection/preview", options)
    assert status == 200
    return {
        "request": options,
        "command": command,
        "limits": CollectionExecutionLimits().model_dump(mode="json"),
        "confirmed": True,
    }


def wait_job(server, identity):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status, catalog = request(server, "/api/collection/jobs")
        assert status == 200
        job = next(item for item in catalog["records"] if item["job_id"] == identity)
        if job["state"] not in {"queued", "running"}:
            return job
        time.sleep(0.03)
    pytest.fail("job did not finish")


def test_http_launch_reconnect_and_open_verified_static_report(server):
    body = launch_body(server)
    status, job = request(server, "/api/collection/launch", body)
    assert status == 202
    final = wait_job(server, job["job_id"])
    assert final["state"] == "completed", final["reason"]
    assert final["command"]["argv"] == body["command"]["argv"]
    status, opened = request(server, "/api/collection/open", {"job_id": job["job_id"]})
    assert status == 200 and opened["kind"] == "analysis"
    assert request(server, "/api/bootstrap")[0] == 200
    assert request(server, "/api/collection")[1]["report_available"]
    status, workflow = request(server, "/api/workflow")
    assert status == 200 and workflow["source_connection"]["configured"]
    assert workflow["source_connection"]["source_root"] == str(server.app.collection.root)
    status, preview = request(server, "/api/source?path=sample.py")
    assert status == 200 and preview["state"] == "bound"
    assert preview["lines"][0]["text"] == "def answer():"


def test_connect_does_not_rebind_existing_report_workspace(tmp_path):
    from code_analysis.config import AnalysisConfig
    from code_analysis.project import discover_context
    from code_analysis.service import scan
    from code_analysis.web_service import ReportViewService
    from code_analysis.workspace import prepare_workspace

    cfg = AnalysisConfig(test_roots=())
    projects = []
    for name in ("view", "collection"):
        project = tmp_path / name
        project.mkdir()
        (project / "sample.py").write_text("value = 1\n")
        context = discover_context(project, cfg)
        projects.append((context, prepare_workspace(context)))
    view_context, view_workspace = projects[0]
    _, collection_workspace = projects[1]
    service = ReportViewService(scan(view_context))
    server = create_server(
        service,
        workspace=view_workspace.root,
        collection_root=tmp_path,
        allow_collection=True,
        token="jobs-test",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _ = request(
            server, "/api/collection/connect", {"workspace": "collection/.code-analysis"}
        )
        assert status == 200
        assert server.app.collection_session.selected().workspace.root == collection_workspace.root
        assert server.app.runs.workspace.root == view_workspace.root
        assert server.app.service is service
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_opening_verified_diagnostic_artifact_does_not_promote_failed_job(server):
    from code_analysis.collection_job_models import CollectionJob

    _, queued = request(server, "/api/collection/launch", launch_body(server))
    wait_job(server, queued["job_id"])
    jobs = server.app.collection_session.selected()
    original = jobs.get(queued["job_id"])
    assert original.binding is not None
    # Simulate a post-capture protocol failure with a valid bound diagnostic artifact.
    receipt = original.receipt.model_copy(
        update={"state": "failed", "status": errno.EPROTO, "reason": "Protocol failure."}
    )
    failed = CollectionJob.model_validate(
        {
            **original.model_dump(),
            "state": "failed",
            "status": errno.EPROTO,
            "receipt": receipt,
            "reason": "Protocol failure.",
        }
    )
    with jobs._lock:
        jobs._write(failed)
    status, opened = request(server, "/api/collection/open", {"job_id": failed.job_id})
    assert status == 200 and opened["kind"] == "analysis"
    retained = jobs.get(failed.job_id)
    assert retained.state == retained.receipt.state == "failed"
    assert retained.status == retained.receipt.status == errno.EPROTO
    assert retained.binding == failed.binding


def test_http_import_retains_unbound_evidence_without_inventing_source(server):
    # First create a normal owned workspace, then import rather than execute a workload.
    status, job = request(server, "/api/collection/launch", launch_body(server))
    assert status == 202
    assert wait_job(server, job["job_id"])["state"] == "completed"
    (server.app.collection.root / "capture.prof").write_bytes(
        marshal.dumps({("sample.py", 1, "answer"): (3, 3, 0.1, 0.1, {})})
    )
    options = {
        "operation": "import-profile",
        "profiles": ["capture.prof"],
        "workspace": ".code-analysis",
    }
    status, command = request(server, "/api/collection/preview", options)
    assert status == 200
    status, job = request(
        server,
        "/api/collection/launch",
        {
            "request": options,
            "command": command,
            "confirmed": True,
            "limits": CollectionExecutionLimits().model_dump(mode="json"),
        },
    )
    assert status == 202
    final = wait_job(server, job["job_id"])
    assert final["state"] == "partial", final["reason"]
    assert final["binding"]["artifact_name"] == "profile.json"
    status, opened = request(server, "/api/collection/open", {"job_id": job["job_id"]})
    assert status == 200 and opened["kind"] == "profile"
    status, overview = request(server, "/api/runtime")
    assert status == 200 and overview["available"]
    assert not overview["source_matches"] and overview["completeness"] == "partial"
    assert request(server, "/api/runtime/query")[0] == 200
    assert request(server, "/api/runtime/graph")[0] == 200
    assert request(server, "/api/bootstrap")[0] == 409
    assert request(server, "/api/collection")[1]["runtime_available"]


@pytest.mark.parametrize("width", [1280, 390])
def test_browser_import_and_open_unbound_profile(server, width):
    from code_analysis.config import AnalysisConfig
    from code_analysis.project import discover_context
    from code_analysis.workspace import prepare_workspace

    root = server.app.collection.root
    prepare_workspace(discover_context(root, AnalysisConfig()))
    (root / "capture.prof").write_bytes(
        marshal.dumps({("sample.py", 1, "answer"): (3, 3, 0.1, 0.1, {})})
    )
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 960})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=jobs-test")
            toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
            if toggle.is_visible():
                toggle.click()
            page.locator('.nav[data-view="collection"]').click()
            page.locator("#operation").select_option("import-profile")
            page.get_by_role("button", name="Browse and add profile…").click()
            page.get_by_role("option", name="capture.prof").click()
            page.locator("#browser-choose").click()
            assert page.locator("#profiles").input_value() == "capture.prof"
            assert not page.locator("#allow_execution").is_visible()
            page.get_by_role("button", name="Build command", exact=True).click()
            playwright.expect(page.locator("#preview-status")).to_contain_text("Preview ready")
            preview = page.locator("#collection-command").input_value()
            assert "runtime ingest" in preview and "--allow-execution" not in preview
            page.locator("#job-confirm").check()
            page.locator("#launch-job").click()
            page.get_by_role("button", name="Open verified result").wait_for(timeout=15000)
            page.get_by_role("button", name="Open verified result").click()
            page.get_by_role("heading", name="Runtime", exact=True).wait_for()
            playwright.expect(page.locator("#runtime-results")).to_contain_text("answer")
            playwright.expect(page.locator("#report-state")).to_contain_text("no static report")
            assert page.locator("body").evaluate("node => node.scrollWidth <= window.innerWidth")
            assert not errors
        finally:
            browser.close()


def test_launch_permission_origin_confirmation_and_preview_drift(server):
    body = launch_body(server)
    assert (
        request(server, "/api/collection/launch", body, Origin="https://foreign.invalid")[0] == 403
    )
    assert (
        request(server, "/api/collection/launch", body, **{"X-Code-Analysis-Token": "wrong"})[0]
        == 403
    )
    status, payload = request(server, "/api/collection/launch", {**body, "confirmed": False})
    assert status == 403 and payload["status"] == errno.EPERM
    body["request"]["target"] = "."
    assert request(server, "/api/collection/launch", body)[0] == 400
    assert not (server.app.collection.root / ".code-analysis").exists()


def test_cancel_is_explicit_and_terminal_evidence_is_not_fabricated(server):
    status, job = request(server, "/api/collection/launch", launch_body(server))
    assert status == 202
    assert request(server, "/api/collection/cancel", {"job_id": job["job_id"]})[0] == 200
    final = wait_job(server, job["job_id"])
    assert final["state"] == "cancelled"
    assert final["status"] == errno.ECANCELED
    assert request(server, "/api/collection/open", {"job_id": job["job_id"]})[0] == 400


def test_collection_enablement_requires_explicit_root(tmp_path):
    with pytest.raises(ValueError):
        create_server(None, allow_collection=True)
    disabled = create_server(None, collection_root=tmp_path, token="jobs-test")
    thread = threading.Thread(target=disabled.serve_forever, daemon=True)
    thread.start()
    try:
        assert request(disabled, "/api/collection/jobs")[0] == 403
        assert request(disabled, "/api/collection/launch", {})[0] == 403
    finally:
        disabled.shutdown()
        thread.join()
        disabled.server_close()


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_browser_collect_reconnect_and_open(server, width, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 960})
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append((request.method, request.url)))
            page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=jobs-test")
            toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
            if toggle.is_visible():
                toggle.click()
            page.locator('.nav[data-view="collection"]').click()
            page.locator("#target").fill("sample.py")
            page.get_by_role("button", name="Build command", exact=True).click()
            playwright.expect(page.locator("#preview-status")).to_contain_text("Preview ready")
            assert page.locator("#launch-job").is_disabled()
            page.locator("#job-confirm").check()
            page.locator("#launch-job").click()
            playwright.expect(page.locator("#job-list")).to_contain_text("scan")
            page.reload()
            toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
            if toggle.is_visible():
                toggle.click()
            page.locator('.nav[data-view="collection"]').click()
            page.get_by_role("button", name="Open verified result").wait_for(timeout=15000)
            assert page.locator("body").evaluate("node => node.scrollWidth <= window.innerWidth")
            page.get_by_role("button", name="Open verified result").click()
            page.get_by_role("heading", name="Overview", exact=True).wait_for()
            playwright.expect(page.locator(".workflow-source-status")).to_contain_text(
                "Source folder:"
            )
            reveal_navigation(page, "Browse code").click()
            page.locator('.code-browser-list [data-browse-identity^="source-file-"]').click()
            page.get_by_role("button", name="Recorded relationships", exact=True).click()
            page.locator(".code-browser-map").get_by_role(
                "button", name="Inspect answer", exact=True
            ).click()
            playwright.expect(
                page.locator(".code-browser-source .code-browser-lines")
            ).to_contain_text("return 42")
            assert not page.get_by_role(
                "dialog", name="Connect source folder", exact=True
            ).is_visible()
            assert not any(
                method == "POST" and "/api/project/source" in url for method, url in requests
            )
            assert not errors
        finally:
            browser.close()


def test_profile_job_opens_runtime_and_checked_cli_log(server):
    _, first = request(server, "/api/collection/launch", launch_body(server))
    source = wait_job(server, first["job_id"])
    source_path = (
        f".code-analysis/generations/analysis/{source['binding']['generation_id']}/report.json"
    )
    options = {
        "operation": "profile",
        "source_report": source_path,
        "workload": "sample.py",
        "workspace": ".code-analysis",
        "allow_execution": True,
    }
    _, preview = request(server, "/api/collection/preview", options)
    status, job = request(
        server,
        "/api/collection/launch",
        {
            "request": options,
            "command": preview,
            "limits": CollectionExecutionLimits().model_dump(mode="json"),
            "confirmed": True,
        },
    )
    assert status == 202
    final = wait_job(server, job["job_id"])
    assert final["binding"]["kind"] == "profile", final["reason"]
    status, log = request(server, "/api/collection/logs", {"job_id": job["job_id"]})
    assert status == 200 and "untrusted project code" in log["text"]
    assert log["shown_bytes"] + log["omitted_bytes"] == log["total_bytes"]
    status, opened = request(server, "/api/collection/open", {"job_id": job["job_id"]})
    assert status == 200 and opened["kind"] == "profile"
    assert request(server, "/api/runtime")[0] == 200


def test_no_execution_provider_no_workspace_claim(server, monkeypatch):
    import sys

    body = launch_body(server)
    monkeypatch.setitem(sys.modules, "psutil", None)
    status, payload = request(server, "/api/collection/launch", body)
    assert status == 400 and payload["status"] == errno.ENODATA
    assert not (server.app.collection.root / ".code-analysis").exists()


def test_web_requires_collection_root_for_execution(tmp_path, monkeypatch, capsys):
    from code_analysis.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["web", "report.json", "--allow-collection"]) == errno.EINVAL
    assert "requires --collection-root" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["ui", "web"])
def test_cli_launch_path_enables_real_collection(tmp_path, monkeypatch, command, ordinary_report):
    from code_analysis import web_server
    from code_analysis.cli import main

    (tmp_path / "sample.py").write_text("def answer():\n    return 42\n")
    monkeypatch.chdir(tmp_path)
    observed = []

    def factory(*args, **kwargs):
        result = create_server(*args, **kwargs, token="jobs-test")
        serve = result.serve_forever

        def exercise(**options):
            thread = threading.Thread(target=serve, kwargs=options, daemon=True)
            thread.start()
            try:
                assert request(result, "/api/collection")[1]["execution_available"]
                status, job = request(result, "/api/collection/launch", launch_body(result))
                assert status == 202
                final = wait_job(result, job["job_id"])
                assert final["state"] == "completed", final["reason"]
                observed.append(final["binding"]["generation_id"])
            finally:
                result.shutdown()
                thread.join(timeout=5)

        result.serve_forever = exercise
        return result

    monkeypatch.setattr(web_server, "create_server", factory)
    if command == "ui":
        arguments = [command, "--foreground", "--no-open"]
    else:
        from code_analysis.reports.json_report import canonical_json

        report = tmp_path / "report.json"
        report.write_text(canonical_json(ordinary_report))
        arguments = [
            command,
            str(report),
            "--collection-root",
            str(tmp_path),
            "--allow-collection",
        ]
    assert main(arguments) == 0
    assert len(observed) == 1


def test_web_collection_still_requires_explicit_flags(ordinary_report, tmp_path, monkeypatch):
    from code_analysis import cli as cli_module
    from code_analysis.cli import main
    from code_analysis.reports.json_report import canonical_json

    report = tmp_path / "report.json"
    report.write_text(canonical_json(ordinary_report))
    captured = []
    monkeypatch.setattr(cli_module, "_web", lambda args: captured.append(args) or 0)
    assert main(["web", str(report), "--host", "127.0.0.1", "--port", "0"]) == 0
    assert captured[0].allow_collection is False
    assert captured[0].collection_root is None


def test_connect_rejects_escape_and_invalid_confirmation_values(server):
    assert request(server, "/api/collection/connect", {"workspace": "../outside"})[0] == 400
    body = launch_body(server)
    assert request(server, "/api/collection/launch", {**body, "confirmed": "true"})[0] == 400
    body["limits"]["wall_seconds"] = True
    assert request(server, "/api/collection/launch", body)[0] == 400
    assert not (server.app.collection.root / ".code-analysis").exists()


def test_open_rejects_tampered_generation(server):
    _, queued = request(server, "/api/collection/launch", launch_body(server))
    job = wait_job(server, queued["job_id"])
    controller = server.app.collection_session.selected()
    path = (
        controller.workspace.root
        / "generations/analysis"
        / job["binding"]["generation_id"]
        / "report.json"
    )
    path.write_text("{}")
    status, response = request(server, "/api/collection/open", {"job_id": queued["job_id"]})
    assert status == 400 and response["status"] == errno.EBADMSG
    assert server.app.service is None
