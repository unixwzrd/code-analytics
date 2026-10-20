from __future__ import annotations

import errno
import os
import subprocess

import pytest
from pydantic import ValidationError

from code_analysis.artifact_viewer import ViewerCommand, artifact_digest, open_artifact


@pytest.mark.parametrize("command", ["not-json", '["relative"]', '["/bin/tool", false]'])
def test_invalid_viewer_cli_is_einval_before_terminal_access(tmp_path, capsys, command):
    from code_analysis.tui_cli import tui_command

    assert tui_command(tmp_path, "a" * 64, 25, viewer_command=command) == errno.EINVAL
    assert capsys.readouterr().out == ""


def test_conflicting_viewer_cli_options_are_rejected(tmp_path):
    from code_analysis.tui_cli import tui_command

    assert (
        tui_command(tmp_path, "a" * 64, 25, viewer_command='["/bin/tool"]', no_viewer=True)
        == errno.EINVAL
    )


@pytest.mark.parametrize(
    "arguments", [(), ("open",), ("/bin/tool", True), ("/bin/tool", ""), ("/bin/tool", "a\0b")]
)
def test_viewer_rejects_invalid_commands(arguments):
    with pytest.raises(ValidationError):
        ViewerCommand(arguments=arguments)


@pytest.mark.parametrize("suffix", ["json", "tsv", "md", "svg", "png", "pdf", "html"])
def test_viewer_path_only_and_hash_binding(tmp_path, monkeypatch, suffix):
    path = tmp_path / f"artifact.{suffix}"
    path.write_bytes(b"synthetic artifact")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("unexpected launch"))
    digest = artifact_digest(path)
    assert open_artifact(path, digest, None) is False
    path.write_bytes(b"changed")
    with pytest.raises(OSError) as exc:
        open_artifact(path, digest, ViewerCommand(arguments=("/bin/tool",)))
    assert exc.value.errno == errno.EBADMSG


def test_viewer_rejects_symlink_fifo_unsupported_and_oversized(tmp_path):
    path = tmp_path / "artifact.md"
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(OSError):
        artifact_digest(path)
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular"):
        artifact_digest(path)
    with pytest.raises(ValueError, match="unsupported"):
        artifact_digest(tmp_path / "script.py")
    path.unlink()
    with path.open("wb") as stream:
        stream.truncate(16 * 1024**2 + 1)
    with pytest.raises(OSError) as exc:
        artifact_digest(path)
    assert exc.value.errno == errno.ENODATA


@pytest.mark.parametrize("status", [0, 9, "timeout"])
def test_viewer_uses_argument_array_and_bounded_launcher(tmp_path, monkeypatch, status):
    path = tmp_path / "literal ; artifact.md"
    path.write_text("safe")
    calls = []
    killed = []

    class Process:
        pid = 12345

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def wait(self, timeout=None):
            if status == "timeout" and timeout is not None:
                raise subprocess.TimeoutExpired("viewer", timeout)
            return 0 if status == "timeout" else status

    def launch(args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append(pid))
    command = ViewerCommand(arguments=("/bin/viewer", "--literal;not-shell"))
    if status == 0:
        assert open_artifact(path, artifact_digest(path), command)
    else:
        with pytest.raises(OSError) as exc:
            open_artifact(path, artifact_digest(path), command)
        assert exc.value.errno == (errno.ETIMEDOUT if status == "timeout" else errno.EPROTO)
    assert calls[0][0] == ["/bin/viewer", "--literal;not-shell", str(path)]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["stdout"] == subprocess.DEVNULL
    assert killed == ([12345] if status == "timeout" else [])
