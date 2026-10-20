"""Controlled argument-array execution shared by coverage and profiling providers."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    command: tuple[str, ...]
    status: int
    timed_out: bool
    wall_seconds: float
    stdout: bytes
    stderr: bytes


def controlled_environment(
    allowlist: tuple[str, ...], additions: tuple[tuple[str, str], ...]
) -> dict[str, str]:
    allowed = set(allowlist)
    unknown = sorted(name for name, _value in additions if name not in allowed)
    if unknown:
        raise OSError(
            errno.EPERM,
            "environment variable is not allowlisted: " + ", ".join(unknown),
        )
    values = {name: os.environ[name] for name in allowlist if name in os.environ}
    for name, value in additions:
        values[name] = value
    return values


def execute_owned(
    command: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: float,
    environment: dict[str, str],
) -> ExecutionResult:
    if not command or any(not item for item in command):
        raise OSError(errno.EINVAL, "execution command cannot be empty")
    if not cwd.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "execution cwd is not a directory", str(cwd))
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise OSError(errno.EINVAL, "execution command is unavailable", command[0]) from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return ExecutionResult(
            command=command,
            status=process.returncode,
            timed_out=False,
            wall_seconds=time.monotonic() - started,
            stdout=stdout,
            stderr=stderr,
        )
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return ExecutionResult(
            command=command,
            status=errno.ETIMEDOUT,
            timed_out=True,
            wall_seconds=time.monotonic() - started,
            stdout=stdout,
            stderr=stderr,
        )


def resolve_expected_status(symbol: str) -> int:
    if symbol == "SUCCESS":
        return 0
    value = getattr(errno, symbol, None)
    if not isinstance(value, int) or value <= 0:
        raise OSError(errno.EINVAL, "unknown symbolic expected status", symbol)
    return value
