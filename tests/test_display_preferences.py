from __future__ import annotations

import errno
import json
import os

import pytest

from code_analysis import cli
from code_analysis.artifact_viewer import ViewerCommand
from code_analysis.display_preferences import DisplayPreferences, load_display_preferences


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": True},
        {"browser_auto_open": 1},
        {"viewer_enabled": "false"},
        {"browser_command": {"arguments": ["relative"]}},
        {"viewer_command": {"arguments": ["/bin/tool", "\u0000"]}},
    ],
)
def test_invalid_preferences_fail_without_echoing_values(tmp_path, payload):
    path = tmp_path / "display.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(OSError) as exc:
        load_display_preferences(path)
    assert exc.value.errno == errno.EINVAL
    assert exc.value.strerror == "invalid display configuration"


def test_explicit_preferences_and_regular_file_bounds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "display.json"
    path.write_text('{"browser_auto_open":false,"viewer_enabled":false}')
    assert load_display_preferences(None) == DisplayPreferences()
    assert load_display_preferences(path).browser_auto_open is False
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        load_display_preferences(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(OSError, match="regular file"):
        load_display_preferences(fifo)
    path.write_bytes(b" " * 65537)
    with pytest.raises(OSError, match="64 KiB"):
        load_display_preferences(path)


@pytest.mark.parametrize(
    "auto,option,expected",
    [(False, None, False), (True, "--no-open", False), (False, "--open", True)],
)
def test_ui_display_preference_and_explicit_override(tmp_path, monkeypatch, auto, option, expected):
    config = tmp_path / "display.json"
    config.write_text(json.dumps({"browser_auto_open": auto}))
    captured = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_web", lambda args: captured.append(args) or 0)
    args = ["ui", "--collection-root", str(tmp_path), "--display-config", str(config)]
    if option:
        args.append(option)
    assert cli.main(args) == 0
    assert captured[0].open is expected
    assert captured[0].display_config == config


def test_browser_command_is_literal_and_preferences_do_not_execute(tmp_path, monkeypatch):
    from code_analysis import artifact_viewer

    calls = []
    monkeypatch.setattr(artifact_viewer, "launch_display", lambda *args: calls.append(args))
    command = ViewerCommand(arguments=("/usr/bin/open", "-a", "Browser; literal"))
    config = tmp_path / "display.json"
    config.write_text(json.dumps({"browser_command": command.model_dump(mode="json")}))
    preferences = load_display_preferences(config)
    assert calls == []
    cli._open_web_url("http://127.0.0.1:1234/?token=example", preferences.browser_command)
    assert calls == [("http://127.0.0.1:1234/?token=example", command)]


def test_failed_browser_launch_closes_server(tmp_path, monkeypatch):
    from code_analysis import web_server

    servers = []
    real_create = web_server.create_server

    def create(*args, **kwargs):
        server = real_create(*args, **kwargs)
        servers.append(server)
        return server

    def failed(*args):
        raise OSError(errno.ENOENT, "browser missing")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(web_server, "create_server", create)
    monkeypatch.setattr(cli, "_open_web_url", failed)
    assert (
        cli.main(["ui", "--foreground", "--collection-root", str(tmp_path), "--open"])
        == errno.ENOENT
    )
    assert servers[0].socket.fileno() == -1


def test_tui_preference_overrides(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from code_analysis import tui_app, tui_cli

    config = tmp_path / "display.json"
    config.write_text('{"viewer_enabled":false}')
    captured = []

    class App:
        return_code = exit_status = 0

        def __init__(self, *args, **kwargs):
            captured.append(kwargs["viewer"])

        def run(self):
            return 0

        def close_web_handoff(self):
            pass

    monkeypatch.setattr(tui_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(tui_cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(tui_cli.BrowseIndex, "open", lambda *args, **kwargs: object())
    monkeypatch.setattr(tui_app, "AnalysisTerminal", App)
    assert tui_cli.tui_command(tmp_path, "a" * 64, 25, display_config=config) == 0
    assert captured[-1] is None
    assert (
        tui_cli.tui_command(
            tmp_path, "a" * 64, 25, viewer_command='["/bin/custom"]', display_config=config
        )
        == 0
    )
    assert captured[-1].arguments == ("/bin/custom",)
    config.write_text('{"viewer_command":{"arguments":["/bin/custom"]}}')
    assert tui_cli.tui_command(tmp_path, "a" * 64, 25, no_viewer=True, display_config=config) == 0
    assert captured[-1] is None
