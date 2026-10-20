"""Real detached processes and authenticated lifecycle boundaries; synthetic inputs only."""

from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import ValidationError

from code_analysis.cli import main
from code_analysis.web_launcher import (
    UISession,
    launch_options,
    read_session,
    registry_lock,
    session_request,
    start_session,
    state_directory,
    stop_session,
    write_session,
)
from code_analysis.web_lifecycle import UIShutdownRequest
from code_analysis.web_server import create_server


def options(tmp_path, **changes):
    args = argparse.Namespace(
        report=None,
        compare=None,
        profile_report=None,
        compare_profile=None,
        workspace=None,
        source_root=None,
        collection_root=tmp_path,
        collection_python=(),
        display_config=None,
        host="127.0.0.1",
        port=0,
        allow_collection=False,
    )
    args.__dict__.update(changes)
    return launch_options(args)


@pytest.fixture
def private_state(tmp_path, monkeypatch):
    root = tmp_path / "ui-state"
    monkeypatch.setenv("CODE_ANALYSIS_UI_STATE_DIR", str(root))
    monkeypatch.chdir(tmp_path)
    return root


def test_strict_shutdown_and_launch_identities(tmp_path):
    for data in (
        {"confirmed": 1},
        {"confirmed": True, "extra": True},
        {"confirmed": True, "cancel_jobs": "yes"},
    ):
        with pytest.raises(ValidationError):
            UIShutdownRequest.model_validate(data)
    normal = options(tmp_path)
    assert normal.session_id == options(tmp_path).session_id
    assert normal.session_id != options(tmp_path, allow_collection=True).session_id
    assert normal.session_id != options(tmp_path, source_root=tmp_path).session_id
    assert normal.session_id != options(tmp_path / "other").session_id
    with pytest.raises(ValidationError):
        normal.model_copy(update={"port": True}).model_validate(
            {**normal.model_dump(), "port": True}
        )


def test_upgrade_does_not_reuse_previous_version_server(tmp_path, monkeypatch):
    before = options(tmp_path)
    monkeypatch.setattr("code_analysis.web_launcher.__version__", "different-test-build")
    after = options(tmp_path)
    assert before.interpreter == after.interpreter
    assert before.implementation_root == after.implementation_root
    assert before.session_id != after.session_id


def test_private_records_reject_symlinks_permissions_and_corruption(private_state):
    root = state_directory()
    target = root / "target"
    target.write_text("secret")
    alias = root / "link.json"
    alias.symlink_to(target)
    with pytest.raises(OSError) as failure:
        read_session(alias)
    assert failure.value.errno == errno.ELOOP
    target.chmod(0o644)
    with pytest.raises(OSError) as failure:
        read_session(target)
    assert failure.value.errno == errno.EACCES
    target.chmod(0o600)
    with pytest.raises(OSError) as failure:
        read_session(target)
    assert failure.value.errno == errno.EBADMSG
    root.chmod(0o755)
    with pytest.raises(OSError) as failure:
        state_directory()
    assert failure.value.errno == errno.EACCES


def test_detached_start_reopen_scope_isolation_and_stop(private_state, tmp_path):
    first = second = None
    try:
        first, started = start_session(options(tmp_path))
        assert started and first.pid != os.getpid()
        assert os.getsid(first.pid) == first.pid
        same, started = start_session(options(tmp_path))
        assert not started and same == first
        second, started = start_session(options(tmp_path, allow_collection=True))
        assert started and second.pid != first.pid
        with pytest.raises(OSError) as failure:
            stop_session(None, cancel_jobs=False)
        assert failure.value.errno == errno.EINVAL
        forged = first.model_copy(update={"instance_id": "0" * 32})
        with pytest.raises(OSError) as failure:
            session_request(forged)
        assert failure.value.errno == errno.EBADMSG
        assert session_request(first).stopping is False
        assert stop_session(first.session_id, cancel_jobs=False) == first
        assert not (private_state / f"{first.session_id}.json").exists()
        first = None
        assert not (tmp_path / ".code-analysis").exists()
        assert not (private_state / "0.json").exists()
    finally:
        for record in (first, second):
            if record is not None:
                stop_session(record.session_id, cancel_jobs=True)


def test_cli_background_returns_without_inheriting_terminal_pipes(private_state, tmp_path):
    session = None
    try:
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, "-m", "code_analysis", "ui", "--no-open"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout == ""
            assert "Press Ctrl-C" not in result.stderr
            assert "code-analysis ui-stop --session" in result.stderr
            records = list(private_state.glob("*.json"))
            assert len(records) == 1
            current = read_session(records[0])
            if session is not None:
                assert session == current
                assert "reopened" in result.stderr
            session = current
        assert main(["ui-stop", "--session", session.session_id]) == 0
        session = None
        assert not (tmp_path / ".code-analysis").exists()
    finally:
        if session is not None:
            stop_session(session.session_id, cancel_jobs=True)


def test_startup_failure_preserves_errno_and_private_log(private_state, tmp_path):
    missing = tmp_path / "no-workspace"
    with pytest.raises(OSError) as failure:
        start_session(options(tmp_path, workspace=missing))
    assert failure.value.errno != 0
    assert list(private_state.glob("*.log"))
    assert not list(private_state.glob("*.json"))
    assert not list(private_state.glob("*.request"))


def test_readiness_timeout_cleans_only_launched_child(private_state, tmp_path):
    with pytest.raises(OSError) as failure:
        start_session(options(tmp_path), timeout=0)
    assert failure.value.errno == errno.ETIMEDOUT
    assert not list(private_state.glob("*.json"))


def test_terminal_dimensions_do_not_change_session_identity(tmp_path, monkeypatch):
    before = options(tmp_path)
    monkeypatch.setenv("COLUMNS", "192")
    monkeypatch.setenv("LINES", "60")
    assert options(tmp_path).session_id == before.session_id
    monkeypatch.setenv("PYTHONPATH", "different-runtime-configuration")
    assert options(tmp_path).session_id != before.session_id


def test_saved_input_changes_do_not_reuse_session(tmp_path):
    saved = tmp_path / "report.json"
    saved.write_text("{}")
    before = options(tmp_path, report=saved)
    saved.write_text('{"changed":true}')
    assert options(tmp_path, report=saved).session_id != before.session_id


def test_concurrent_launches_reuse_one_ready_server(private_state, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    session = None
    try:
        launch = options(tmp_path)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(start_session, launch) for _ in range(2)]
            results = [future.result(timeout=25) for future in futures]
        session = results[0][0]
        assert results[0][0] == results[1][0]
        assert sum(started for _, started in results) == 1
    finally:
        if session is not None:
            stop_session(session.session_id, cancel_jobs=True)


def test_malformed_receipt_never_launches_or_signals(private_state, tmp_path):
    launch = options(tmp_path)
    path = state_directory() / f"{launch.session_id}.json"
    path.write_text("not-json")
    path.chmod(0o600)
    with pytest.raises(OSError) as failure:
        start_session(launch)
    assert failure.value.errno == errno.EBADMSG
    assert not list(private_state.glob("*.log"))


def test_ipv6_loopback_session(private_state, tmp_path):
    session = None
    try:
        session, started = start_session(options(tmp_path, host="::1"))
        assert started and session_request(session).pid == session.pid
    finally:
        if session is not None:
            stop_session(session.session_id, cancel_jobs=True)


def test_live_lease_prevents_replacement_even_without_receipt(private_state, tmp_path):
    launch = options(tmp_path)
    with (
        registry_lock(state_directory(), f"{launch.session_id}.lease"),
        pytest.raises(OSError) as failure,
    ):
        start_session(launch)
    assert failure.value.errno == errno.EBUSY
    assert not list(private_state.glob("*.log"))


def test_target_package_cannot_shadow_analyzer(private_state, tmp_path):
    package = tmp_path / "code_analysis"
    package.mkdir()
    marker = tmp_path / "TARGET_WAS_EXECUTED"
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "raise RuntimeError('target executed')\n"
    )
    session = None
    try:
        session, started = start_session(options(tmp_path))
        assert started and not marker.exists()
    finally:
        if session is not None:
            stop_session(session.session_id, cancel_jobs=True)


def test_stale_receipt_replaced_without_signalling_recorded_pid(private_state, tmp_path):
    session = None
    try:
        launch = options(tmp_path)
        stale = UISession(
            session_id=launch.session_id, instance_id="a" * 32, token="x" * 40, options=launch
        )
        write_session(state_directory() / f"{launch.session_id}.json", stale)
        session, started = start_session(launch)
        assert started and session.instance_id != stale.instance_id
    finally:
        if session is not None:
            stop_session(session.session_id, cancel_jobs=True)


@pytest.fixture
def running_server():
    server = create_server(None, token="lifecycle-test", collection_root=Path.cwd())
    server.shutdown_enabled = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post(server, payload, **headers):
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    request = Request(
        origin + "/api/session/stop",
        data=json.dumps(payload).encode(),
        headers={
            "X-Code-Analysis-Token": "lifecycle-test",
            "Origin": origin,
            "Content-Type": "application/json",
            **headers,
        },
    )
    return urlopen(request, timeout=3)


@pytest.mark.parametrize(
    "headers,code",
    [
        ({"X-Code-Analysis-Token": "wrong"}, 403),
        ({"Origin": "http://attacker.invalid"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
    ],
)
def test_shutdown_requires_token_and_same_origin(running_server, headers, code):
    with pytest.raises(HTTPError) as failure:
        post(running_server, {"confirmed": True}, **headers)
    assert failure.value.code == code
    assert not running_server.stopping


@pytest.mark.parametrize(
    "payload", [{"confirmed": False}, {"confirmed": 1}, {"confirmed": True, "extra": 0}]
)
def test_shutdown_strict_confirmation(running_server, payload):
    with pytest.raises(HTTPError):
        post(running_server, payload)
    assert not running_server.stopping


def test_active_job_shutdown_needs_explicit_consent(running_server):
    from types import SimpleNamespace

    class Session:
        def catalog(self):
            return SimpleNamespace(records=(SimpleNamespace(state="running"),))

        def close(self):
            pass

    running_server.app = replace(running_server.app, collection_session=Session())
    with pytest.raises(HTTPError) as failure:
        post(running_server, {"confirmed": True})
    assert failure.value.code == 409
    assert not running_server.stopping
    with post(running_server, {"confirmed": True, "cancel_jobs": True}) as response:
        assert json.load(response)["stopping"] is True


def test_library_handoff_owner_can_disallow_browser_shutdown(running_server):
    running_server.shutdown_enabled = False
    assert running_server.lifecycle_status().shutdown_available is False
    with pytest.raises(HTTPError) as failure:
        post(running_server, {"confirmed": True, "cancel_jobs": True})
    assert failure.value.code == 403
    assert not running_server.stopping


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_browser_close_preserves_server_and_explicit_stop_uses_toolkit(running_server, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    url = f"http://127.0.0.1:{running_server.server_address[1]}/?token=lifecycle-test"
    with playwright.sync_playwright() as runtime:
        browser = getattr(runtime, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(url)
            page.get_by_role("button", name="Stop Code Analysis", exact=True).wait_for()
            page.reload()
            page.get_by_role("button", name="Stop Code Analysis", exact=True).wait_for()
            page.close()
            assert not running_server.stopping
            page = browser.new_page()
            page.goto(url)
            page.get_by_role("button", name="Stop Code Analysis", exact=True).click()
            dialog = page.get_by_role("dialog", name="Stop Code Analysis?", exact=True)
            dialog.wait_for()
            page.get_by_role("button", name="Keep running", exact=True).click()
            assert not running_server.stopping
            page.get_by_role("button", name="Stop Code Analysis", exact=True).click()
            page.get_by_role("button", name="Stop server", exact=True).click()
            playwright.expect(
                page.get_by_role("status").filter(has_text="Shutdown requested")
            ).to_be_visible()
            assert running_server.stopping
        finally:
            browser.close()
