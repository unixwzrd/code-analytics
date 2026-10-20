"""Explicit coverage workload collection; static scans never call this module."""

from __future__ import annotations

import errno
import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from code_analysis.runtime_execution import (
    ExecutionResult,
    controlled_environment,
    execute_owned,
)


@dataclass(frozen=True, slots=True)
class CoverageRunCapture:
    workload_identity_sha256: str
    results: tuple[ExecutionResult, ...]
    coverage_data: tuple[bytes, ...]
    coverage_json: tuple[bytes, ...]


def run_coverage_workload(
    command: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: float,
    repeat: int,
    expected_status: int,
    environment_allowlist: tuple[str, ...],
    environment_additions: tuple[tuple[str, str], ...],
) -> CoverageRunCapture:
    if repeat < 1 or repeat > 20:
        raise OSError(errno.EINVAL, "coverage repetition count must be between 1 and 20")
    environment = controlled_environment(environment_allowlist, environment_additions)
    workload_identity = hashlib.sha256(
        json.dumps(
            {"command": command, "expected_status": expected_status, "repeat": repeat},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    results: list[ExecutionResult] = []
    data_payloads: list[bytes] = []
    json_payloads: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="code-analysis-coverage-") as temporary_text:
        temporary = Path(temporary_text)
        for index in range(repeat):
            data_path = temporary / f".coverage.{index + 1}"
            per_run_config = temporary / f"coveragerc.{index + 1}"
            per_run_config.write_text(
                "[run]\nbranch = true\ndynamic_context = test_function\n"
                f"data_file = {data_path}\n",
                encoding="utf-8",
            )
            instrumented = _coverage_command(command, per_run_config)
            result = execute_owned(
                instrumented,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                environment=environment,
            )
            results.append(result)
            data_payloads.append(data_path.read_bytes() if data_path.is_file() else b"")
            json_payloads.append(
                _normalized_json(data_path, temporary / f"coverage.{index + 1}.json")
            )
            if result.timed_out:
                break
            if result.status != expected_status:
                break
    return CoverageRunCapture(
        workload_identity_sha256=workload_identity,
        results=tuple(results),
        coverage_data=tuple(data_payloads),
        coverage_json=tuple(json_payloads),
    )


def _coverage_command(command: tuple[str, ...], configuration: Path) -> tuple[str, ...]:
    if not command:
        raise OSError(errno.EINVAL, "coverage command cannot be empty")
    first = Path(command[0])
    current = Path(sys.executable).resolve()
    if first.name in {"python", "python3"} or first.resolve() == current:
        if first.resolve() != current and shutil.which(command[0]) is not None:
            resolved = Path(shutil.which(command[0]) or command[0]).resolve()
            if resolved != current:
                raise OSError(errno.EINVAL, "coverage requires the current interpreter")
        remainder = command[1:]
        if len(remainder) >= 2 and remainder[0] == "-m":
            return (
                sys.executable,
                "-m",
                "coverage",
                "run",
                "--rcfile",
                str(configuration),
                "-m",
                remainder[1],
                *remainder[2:],
            )
        if remainder and remainder[0].endswith(".py"):
            return (
                sys.executable,
                "-m",
                "coverage",
                "run",
                "--rcfile",
                str(configuration),
                *remainder,
            )
        raise OSError(errno.EINVAL, "unsupported current-interpreter coverage command")
    if first.name in {"pytest", "py.test"}:
        return (
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--rcfile",
            str(configuration),
            "-m",
            "pytest",
            *command[1:],
        )
    raise OSError(errno.EINVAL, "coverage command is not a supported Python workload")


def _normalized_json(data_path: Path, output_path: Path) -> bytes:
    if not data_path.is_file():
        return b""
    try:
        from coverage import Coverage

        coverage = Coverage(data_file=str(data_path))
        coverage.load()
        coverage.json_report(outfile=str(output_path), show_contexts=True)
    except Exception:
        return b""
    return output_path.read_bytes() if output_path.is_file() else b""
