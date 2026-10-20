"""Explicit, bounded launch of a configured viewer for a verified local artifact."""

from __future__ import annotations

import errno
import hashlib
import os
import signal
import stat
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from pydantic import Field, StrictStr, model_validator

from code_analysis.models import StrictModel


class ViewerCommand(StrictModel):
    arguments: tuple[StrictStr, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_command(self) -> ViewerCommand:
        if not Path(self.arguments[0]).is_absolute():
            raise ValueError("viewer executable must be absolute")
        if any(not arg or len(arg) > 4096 or "\0" in arg for arg in self.arguments):
            raise ValueError("viewer arguments must be nonempty bounded strings without NUL")
        return self


def default_viewer() -> ViewerCommand | None:
    if sys.platform == "darwin" and Path("/usr/bin/open").is_file():
        return ViewerCommand(arguments=("/usr/bin/open",))
    return None


def artifact_digest(path: Path) -> str:
    """Hash a non-symlink regular artifact, bounded independently of its suffix."""
    if path.suffix.lower() not in {".json", ".tsv", ".md", ".svg", ".png", ".pdf", ".html"}:
        raise ValueError("unsupported viewer artifact format")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("viewer artifact must be a regular file")
        if before.st_size > 16 * 1024**2:
            raise OSError(errno.ENODATA, "viewer artifact exceeds 16 MiB")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(16 * 1024**2 + 1)
        after = os.fstat(descriptor)
        if len(data) != before.st_size or (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise OSError(errno.EBADMSG, "viewer artifact changed while reading")
        return hashlib.sha256(data).hexdigest()
    finally:
        os.close(descriptor)


def open_artifact(path: Path, expected_sha256: str, command: ViewerCommand | None) -> bool:
    """Return whether a launch request completed, not whether rendering succeeded.

    The caller confines the selection to an owned artifact directory. External
    viewers receive a path and are not sandboxed against later local mutation.
    """
    if artifact_digest(path) != expected_sha256:
        raise OSError(errno.EBADMSG, "exported artifact changed; export again before opening")
    if command is None:
        return False
    launch_display(str(path.absolute()), command)
    return True


def launch_display(target: str, command: ViewerCommand) -> None:
    """Invoke a trusted launcher with one literal URL/path; no shell or rendering claim."""
    with subprocess.Popen(
        [*command.arguments, target],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        shell=False,
    ) as process:
        try:
            status = process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise OSError(errno.ETIMEDOUT, "viewer launcher exceeded five seconds") from exc
    if status:
        raise OSError(errno.EPROTO, "viewer launcher returned an unexpected nonzero status")
