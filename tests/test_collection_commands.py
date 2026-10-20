from __future__ import annotations

import errno
import json
import os
import shlex
import sys
import threading
from http.client import HTTPConnection

import pytest
from pydantic import ValidationError

from code_analysis.cli import build_parser, main
from code_analysis.collection_commands import CollectionPlanner, CollectionRequest
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


@pytest.fixture
def planner(tmp_path):
    (tmp_path / "source space.py").write_text("raise RuntimeError('must not run')\n")
    (tmp_path / "saved.json").write_text("{}")
    instance = CollectionPlanner(tmp_path)
    yield instance
    instance.close()


@pytest.mark.parametrize("operation", ["scan", "clones", "refactor"])
def test_static_command_is_cli_compatible_and_never_executes(planner, operation):
    request = CollectionRequest(
        operation=operation,
        target="source space.py",
        exclude=("--option", "a b;$(false)"),
        languages=("python", "javascript"),
        workspace="new-workspace",
    )
    first = planner.preview(request)
    assert first == planner.preview(request)
    shell = shlex.split(first.command)
    assert shell[:3] == ["cd", first.cwd, "&&"]
    assert shell[3:] == list(first.argv)
    parsed = build_parser().parse_args(first.argv[4:])
    assert parsed.command == operation
    assert parsed.exclude_dir == ["--option", "a b;$(false)"]
    assert first.execution_available is False
    assert not (planner.root / "new-workspace").exists()
    assert first.required_inputs == (str(planner.root / "source space.py"),)


def test_import_preview_is_nonexecuting_and_cli_compatible(planner):
    (planner.root / "saved profile.prof").write_bytes(b"not parsed during preview")
    request = CollectionRequest(operation="import-profile", profiles=("saved profile.prof",))
    preview = planner.preview(request)
    parsed = build_parser().parse_args(preview.argv[4:])
    assert parsed.command == "runtime" and parsed.runtime_command == "ingest"
    assert parsed.profile == [planner.root / "saved profile.prof"]
    assert "--allow-execution" not in preview.argv
    assert "--json-compact" not in preview.argv
    assert any(entry.kind == "profile" for entry in planner.browse(".").entries)
    assert not (planner.root / ".code-analysis").exists()


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"profiles": ("../escape.prof",)},
        {"profiles": ("a.prof", "a.prof")},
        {"profiles": ("a.prof",), "allow_execution": True},
        {"profiles": ("a.prof",), "workload": "app.py"},
    ],
)
def test_invalid_import_requests(options):
    with pytest.raises(ValidationError):
        CollectionRequest(operation="import-profile", **options)


@pytest.mark.parametrize(
    "kind,workload", [("script", "source space.py"), ("module", "package.main")]
)
def test_profile_preview_preserves_workload_tokens(planner, kind, workload):
    request = CollectionRequest(
        operation="profile",
        source_report="saved.json",
        workload_kind=kind,
        workload=workload,
        allow_execution=True,
        workspace="profile-output",
        arguments=("--flag", " two words ", "", "$(touch BAD);'"),
        repeat=3,
        expected_status="ENODATA",
    )
    preview = planner.preview(request)
    divider = preview.argv.index("--")
    parsed = build_parser().parse_args(preview.argv[4:divider])
    assert parsed.command == "profile" and parsed.allow_execution and parsed.repeat == 3
    assert parsed.workspace == planner.root / "profile-output"
    assert preview.argv[divider + 1] == sys.executable
    assert preview.argv[-4:] == request.arguments
    assert tuple(shlex.split(preview.command)[3:]) == preview.argv
    assert not (planner.root / "BAD").exists()


@pytest.mark.parametrize(
    "values",
    [
        {"unknown": 1},
        {"repeat": True},
        {"languages": ["auto", "python"]},
        {"languages": []},
        {"operation": "profile"},
        {"allow_execution": True},
        {
            "operation": "profile",
            "source_report": "saved.json",
            "workload": "x",
            "allow_execution": True,
            "repeat": 21,
        },
        {
            "operation": "profile",
            "source_report": "saved.json",
            "workload": "x",
            "allow_execution": True,
            "timeout": 0,
        },
        {
            "operation": "profile",
            "source_report": "saved.json",
            "workload": "x",
            "allow_execution": True,
            "expected_status": "invented",
        },
        {"exclude": ["a\nb"]},
        {"arguments": ["\u0000"]},
    ],
)
def test_strict_collection_requests_reject_inapplicable_and_coercive_values(values):
    with pytest.raises(ValidationError):
        CollectionRequest.model_validate_json(json.dumps(values))


@pytest.mark.parametrize("path", ["../", "/tmp", "a/../b", "a//b", "a/", "a\\b", "x\n", "missing"])
def test_paths_cannot_escape_or_silently_disappear(planner, path):
    with pytest.raises((OSError, ValueError)):
        planner.preview(CollectionRequest(target=path))
    with pytest.raises((OSError, ValueError)):
        planner.browse(path)


def test_symlinks_special_files_and_directory_bounds(planner, tmp_path):
    (tmp_path / "escape").symlink_to(tmp_path.parent, target_is_directory=True)
    os.mkfifo(tmp_path / "pipe.py")
    for target in ("escape", "escape/other.py", "pipe.py"):
        with pytest.raises((OSError, ValueError)):
            planner.preview(CollectionRequest(target=target))
    (tmp_path / "folder").mkdir()
    for index in range(205):
        (tmp_path / f"z{index:03}.py").touch()
    listing = planner.browse(".")
    assert listing.parent is None and len(listing.entries) == 200 and listing.omitted == 8
    assert listing.entries[0].name == "folder"
    assert listing.skipped == 2
    assert listing == planner.browse(".")
    assert planner.browse("folder").parent == "."


@pytest.fixture
def collection_server(tmp_path):
    (tmp_path / "sample.py").write_text("print('must not run during preview')\n")
    (tmp_path / "saved.json").write_text("{}")
    alternate = tmp_path / "trusted-python"
    alternate.symlink_to(sys.executable)
    server = create_server(
        None,
        collection_root=tmp_path,
        collection_pythons=(alternate,),
        token="collection-test",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def _request(server, *, body=None, path="/api/collection/preview", overrides=None):
    host = f"127.0.0.1:{server.server_address[1]}"
    headers = {
        "X-Code-Analysis-Token": "collection-test",
        "Origin": f"http://{host}",
        "Content-Type": "application/json",
    }
    headers.update(overrides or {})
    connection = HTTPConnection(host, timeout=10)
    try:
        connection.request("POST" if body is not None else "GET", path, body, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_http_preview_and_empty_startup_are_not_successful_empty_reports(collection_server):
    status, context = _request(collection_server, path="/api/collection")
    assert status == 200 and context["enabled"] and not context["report_available"]
    assert context["interpreters"][0] == {"id": "current", "path": sys.executable}
    assert context["interpreters"][1]["path"].endswith("/trusted-python")
    assert _request(collection_server, path="/api/bootstrap")[0] == 409
    status, command = _request(collection_server, body='{"target":"sample.py"}')
    assert status == 200 and command["execution_available"] is False
    assert command["argv"][4] == "scan"
    assert _request(collection_server, path="/api/collection/files?path=..")[0] == 400


@pytest.mark.parametrize(
    "overrides,status",
    [
        ({"Origin": "http://evil.example"}, 403),
        ({"Origin": "null"}, 403),
        ({"X-Code-Analysis-Token": "wrong"}, 403),
        ({"Host": "evil.example"}, 400),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Content-Type": "text/plain"}, 400),
        ({"Transfer-Encoding": "chunked"}, 400),
        ({"Content-Length": "65537"}, 400),
    ],
)
def test_http_preview_security(collection_server, overrides, status):
    assert _request(collection_server, body="{}", overrides=overrides)[0] == status


def test_preview_rejects_bad_json_and_unknown_actions(collection_server):
    assert _request(collection_server, body="{")[0] == 400
    assert _request(collection_server, body='{"operation":"profile"}')[0] == 400
    assert _request(collection_server, body="{}", path="/api/collection/run")[0] == 404


def test_planning_is_opt_in_and_bad_root_does_not_open_server(ordinary_report, tmp_path):
    server = create_server(ReportViewService(ordinary_report))
    assert server.app.collection is None
    server.server_close()
    with pytest.raises(FileNotFoundError):
        create_server(None, collection_root=tmp_path / "missing")
    with pytest.raises(ValueError):
        create_server(None)


def test_ui_defaults_to_collection_planning_without_workspace(tmp_path, monkeypatch):
    import code_analysis.cli as cli

    captured = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli, "_web", lambda args: captured.append(args) or 0)
    for argv in ([], ["ui", "--no-open"]):
        captured.clear()
        assert main(argv) == 0
        assert captured[0].report is None
        assert captured[0].collection_root == tmp_path.resolve()
        assert captured[0].allow_collection is True
        assert not captured[0].open
        assert not (tmp_path / ".code-analysis").exists()


def test_ui_can_start_without_saved_report_with_explicit_root(tmp_path, monkeypatch):
    import code_analysis.cli as cli

    captured = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_web", lambda args: captured.append(args) or 0)
    assert main(["ui", "--collection-root", str(tmp_path), "--no-open"]) == 0
    assert captured[0].report is None and captured[0].collection_root == tmp_path
    assert not captured[0].open
    assert not (tmp_path / ".code-analysis").exists()


def test_ui_forwards_only_explicit_collection_interpreters(tmp_path, monkeypatch):
    import code_analysis.cli as cli

    alternate = tmp_path / "trusted-python"
    alternate.symlink_to(sys.executable)
    captured = []
    monkeypatch.setattr(cli, "_web", lambda args: captured.append(args) or 0)
    assert (
        main(
            [
                "ui",
                "--collection-root",
                str(tmp_path),
                "--collection-python",
                str(alternate),
                "--no-open",
            ]
        )
        == 0
    )
    assert captured[0].collection_python == (alternate,)
    assert main(["ui", "--collection-python", str(alternate), "--no-open"]) == errno.EINVAL


def test_cli_directory_exclusions_apply_to_real_synthetic_scan(tmp_path, capsys):
    (tmp_path / "kept.py").write_text("answer = 42\n")
    (tmp_path / "omit").mkdir()
    (tmp_path / "omit" / "ignored.py").write_text("raise RuntimeError('not executed')\n")
    result = main(
        ["scan", str(tmp_path), "--no-workspace", "--json-compact", "--exclude-dir", "omit"]
    )
    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert [item["logical_path"] for item in report["source_files"]] == ["kept.py"]


@pytest.mark.parametrize("width,scheme", [(1440, "dark"), (768, "light"), (390, "dark")])
def test_collection_browser_preview_form_and_path_selection(collection_server, width, scheme):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme=scheme)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(
                f"http://127.0.0.1:{collection_server.server_address[1]}/?token=collection-test"
            )
            page.get_by_role("heading", name="What would you like to investigate?").wait_for()
            toggle = page.get_by_role("button", name="Toggle navigation", exact=True)
            if toggle.is_visible():
                toggle.click()
            page.locator('.nav[data-view="collection"]').click()
            page.get_by_role("heading", name="Collection setup").wait_for()
            interpreter = page.locator("#interpreter_id")
            playwright.expect(interpreter.locator("option")).to_have_count(2)
            assert not page.locator('.nav[data-view="overview"]').is_disabled()
            page.get_by_role("button", name="Browse for Target path", exact=True).click()
            page.get_by_role("option", name="sample.py (source)", exact=True).click()
            page.get_by_role("button", name="Choose selected entry").click()
            assert page.locator("#target").input_value() == "sample.py"
            page.get_by_role("button", name="Build command", exact=True).click()
            playwright.expect(page.locator("#preview-status")).to_contain_text("Preview ready")
            command = page.locator("#collection-command").input_value()
            assert "sample.py" in command and "--allow-execution" not in command
            trusted = interpreter.locator("option").nth(1).get_attribute("value")
            interpreter.select_option(trusted)
            assert page.locator("#collection-command").input_value() == ""
            page.get_by_role("button", name="Build command", exact=True).click()
            playwright.expect(page.locator("#preview-status")).to_contain_text("Preview ready")
            assert shlex.split(page.locator("#collection-command").input_value())[3].endswith(
                "/trusted-python"
            )
            page.locator("#target").fill("../escape.py")
            assert page.locator("#collection-command").input_value() == ""
            assert page.get_by_role("button", name="Copy command").is_disabled()
            page.get_by_role("button", name="Build command", exact=True).click()
            playwright.expect(page.locator("#collection-error")).to_be_visible()
            assert page.locator("#target").input_value() == "../escape.py"
            assert page.locator("body").evaluate("node => node.scrollWidth <= window.innerWidth")
            assert errors == []
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.ui_smoke
def test_picker_up_navigation_and_visible_collection_boundary(collection_server, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    root = collection_server.app.collection.root
    (root / "project" / "source").mkdir(parents=True)
    with playwright.sync_playwright() as runtime:
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(
                f"http://127.0.0.1:{collection_server.server_address[1]}/?token=collection-test"
            )
            page.locator('.nav[data-view="collection"]').click()
            page.get_by_role("button", name="Browse for Target path", exact=True).click()
            up = page.get_by_role("button", name="Up", exact=True)
            location = page.locator("#browser-path")
            boundary = page.locator("#browser-boundary")
            playwright.expect(location).to_have_text(f"Current directory: {root}")
            playwright.expect(up).to_be_disabled()
            playwright.expect(boundary).to_contain_text("At collection root")
            playwright.expect(boundary).to_contain_text("--collection-root")

            page.get_by_role("option", name="project (directory)", exact=True).press("Enter")
            playwright.expect(location).to_have_text(f"Current directory: {root}/project")
            playwright.expect(up).to_be_enabled()
            page.get_by_role("option", name="source (directory)", exact=True).dblclick()
            playwright.expect(location).to_have_text(f"Current directory: {root}/project/source")
            up.click()
            playwright.expect(location).to_have_text(f"Current directory: {root}/project")
            up.click()
            playwright.expect(location).to_have_text(f"Current directory: {root}")
            playwright.expect(up).to_be_disabled()
            assert page.locator("#target").input_value() == "."

            page.get_by_role("option", name="project (directory)", exact=True).press("Enter")
            playwright.expect(up).to_be_enabled()
            page.get_by_role("button", name="Collection root", exact=True).click()
            playwright.expect(location).to_have_text(f"Current directory: {root}")
            playwright.expect(up).to_be_disabled()
            page.get_by_role("button", name="Choose this directory", exact=True).click()
            assert page.locator("#target").input_value() == "."
            assert not (root / ".code-analysis").exists()
        finally:
            browser.close()


def test_profile_browser_authorization_arguments_and_invalid_bounds(collection_server):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(
                f"http://127.0.0.1:{collection_server.server_address[1]}/?token=collection-test"
            )
            page.locator('.nav[data-view="collection"]').click()
            page.locator("#operation").select_option("profile")
            page.locator("#source_report").fill("saved.json")
            page.locator("#workload").fill("sample.py")
            page.locator("#arguments").fill(" two words \n\n$(false)")
            page.get_by_role("button", name="Build command", exact=True).click()
            playwright.expect(page.locator("#collection-error")).to_contain_text("acknowledge")
            page.locator("#allow_execution").check()
            with page.expect_request("**/api/collection/preview") as requested:
                page.get_by_role("button", name="Build command", exact=True).click()
            assert requested.value.post_data_json["arguments"] == [" two words ", "", "$(false)"]
            playwright.expect(page.locator("#preview-status")).to_contain_text("Preview ready")
            assert shlex.split(page.locator("#collection-command").input_value())[-3:] == [
                " two words ",
                "",
                "$(false)",
            ]
            page.locator('fieldset[data-mode="runtime"] summary').click()
            page.locator("#repeat").fill("21")
            page.get_by_role("button", name="Build command", exact=True).click()
            playwright.expect(page.locator("#collection-error")).to_be_visible()
            assert page.locator("#collection-command").input_value() == ""
            page.locator("#operation").select_option("scan")
            page.locator("#operation").select_option("profile")
            assert not page.locator("#allow_execution").is_checked()
        finally:
            browser.close()
