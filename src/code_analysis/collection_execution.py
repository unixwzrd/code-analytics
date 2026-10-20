"""Owned collection worker; transport and browser lifetime do not own its process."""

from __future__ import annotations

import errno
import hashlib
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from code_analysis.collection_commands import (
    CollectionCommand,
    CollectionPlanner,
    CollectionRequest,
)
from code_analysis.config import RuntimeConfig
from code_analysis.models import Completeness, StrictModel
from code_analysis.profile_models import ProfileReport
from code_analysis.profiling import load_profile_json
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import load_json
from code_analysis.runtime_execution import controlled_environment
from code_analysis.runtime_process import OwnedRuntimeProcess


class CollectionExecutionLimits(StrictModel):
    wall_seconds: StrictFloat = Field(default=300.0, gt=0, le=86_400)
    stream_bytes: StrictInt = Field(default=64 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)
    graceful_seconds: StrictFloat = Field(default=5.0, ge=0, le=5)
    forced_seconds: StrictFloat = Field(default=5.0, gt=0, le=5)


class CollectionProcessIdentity(StrictModel):
    pid: StrictInt = Field(gt=0)
    start_time: StrictFloat = Field(gt=0)


class CollectionExecutionReceipt(StrictModel):
    """Local terminal evidence. Parsed stdout is not a verified generation receipt."""

    command: CollectionCommand
    process: CollectionProcessIdentity | None
    state: Literal["completed", "partial", "failed", "cancelled"]
    status: StrictInt = Field(ge=0)
    returncode: StrictInt | None
    forced_stop: StrictBool
    stream_truncated: StrictBool
    stdout_bytes: StrictInt = Field(ge=0)
    stderr_bytes: StrictInt = Field(ge=0)
    stdout_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    stderr_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    report_validated: StrictBool
    reason: StrictStr = Field(min_length=1)
    limitations: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def reconcile(self) -> Self:
        if self.state == "completed" and (
            self.status != 0
            or self.returncode != 0
            or not self.report_validated
            or self.forced_stop
        ):
            raise ValueError("completed collection requires successful validated report output")
        if self.state == "partial" and self.status != errno.ENODATA:
            raise ValueError("partial collection requires ENODATA")
        if self.state == "cancelled" and self.status != errno.ECANCELED:
            raise ValueError("cancelled collection requires ECANCELED")
        if self.state == "failed" and self.status == 0:
            raise ValueError("failed collection cannot have success status")
        if self.stream_truncated and self.report_validated:
            raise ValueError("truncated output cannot be a validated report")
        if self.process is None and self.returncode is not None:
            raise ValueError("returncode requires a launched process")
        if self.report_validated and (self.process is None or self.returncode is None):
            raise ValueError("validated output requires a terminated owned process")
        return self


@dataclass(frozen=True, slots=True)
class CollectionExecutionResult:
    receipt: CollectionExecutionReceipt
    stdout: bytes
    stderr: bytes
    report: AnalysisReport | ProfileReport | None


def execute_collection(
    planner: CollectionPlanner,
    request: CollectionRequest,
    *,
    reviewed_command: CollectionCommand,
    cancel: Event,
    limits: CollectionExecutionLimits | None = None,
    on_started: Callable[[CollectionProcessIdentity], None] | None = None,
) -> CollectionExecutionResult:
    """Run only a freshly validated, unchanged preview using shared process ownership.

    Exceptions before launch are request failures, not successful empty jobs. A caller
    owns scheduling and persistence; closing a browser must not set the cancel event.
    """
    limits = limits or CollectionExecutionLimits()
    command = planner.preview(request)
    if command != reviewed_command:
        raise OSError(errno.EINVAL, "collection options differ from the reviewed command")
    if request.operation == "profile":
        # Validate the declared report before any untrusted workload can start.
        assert request.source_report is not None
        load_json(
            planner.read_input(request.source_report, max_bytes=limits.stream_bytes).decode("utf-8")
        )
    environment = controlled_environment(RuntimeConfig().environment_allowlist, ())
    process: OwnedRuntimeProcess | None = None
    identity: CollectionProcessIdentity | None = None
    status = errno.ECANCELED if cancel.is_set() else 0
    reason = "Cancelled before launch." if status else "Collection command finished."
    forced = False
    deadline = time.monotonic() + limits.wall_seconds
    try:
        if status == 0:
            process = OwnedRuntimeProcess(
                command.argv,
                cwd=Path(command.cwd),
                environment=environment,
                stream_limit=limits.stream_bytes,
            )
            identity = CollectionProcessIdentity.model_validate(
                {"pid": process.process.pid, "start_time": process.start}
            )
            if on_started is not None:
                on_started(identity)
            status, reason = _observe(process, cancel, deadline)
    except OSError as exc:
        status = exc.errno or errno.EIO
        reason = "Collection process could not be started or observed."
    finally:
        if process is not None:
            try:
                forced = process.shutdown(
                    graceful=signal.SIGINT,
                    graceful_seconds=limits.graceful_seconds,
                    forced_seconds=limits.forced_seconds,
                )
                if forced and status == 0:
                    status, reason = errno.EPROTO, "Collection left owned descendants running."
            except OSError as exc:
                status, reason = exc.errno or errno.EIO, "Owned process cleanup failed."
            finally:
                process.close()
    return _result(command, process, identity, status, reason, forced)


def _observe(process: OwnedRuntimeProcess, cancel: Event, deadline: float) -> tuple[int, str]:
    while True:
        process.pump(0.05)
        if cancel.is_set():
            return errno.ECANCELED, "Cancellation requested; owned process cleanup follows."
        if time.monotonic() >= deadline:
            return errno.ETIMEDOUT, "Collection exceeded its wall-time limit."
        if process.overflow:
            return errno.ENODATA, "Captured command output exceeded its byte bound."
        # Profiling intentionally creates a child session. Ownership is established
        # by the shared PID/start ancestry tracker, not by common session membership.
        if process.process.returncode is not None and not process.selector.get_map():
            code = process.process.returncode
            return (
                code if code >= 0 else errno.EPROTO,
                "Collection command finished." if code >= 0 else "Collection ended by signal.",
            )


def _result(
    command: CollectionCommand,
    process: OwnedRuntimeProcess | None,
    identity: CollectionProcessIdentity | None,
    status: int,
    reason: str,
    forced: bool,
) -> CollectionExecutionResult:
    stdout = bytes(process.stdout) if process else b""
    stderr = bytes(process.stderr) if process else b""
    truncated = process.overflow if process else False
    report: AnalysisReport | ProfileReport | None = None
    if truncated and status == 0:
        status, reason = errno.ENODATA, "Output exceeded its bound during shutdown."
    terminated = process is not None and process.process.returncode is not None
    if not truncated and stdout and terminated:
        try:
            text = stdout.decode("utf-8")
            report = (
                load_profile_json(text)
                if command.operation in {"profile", "import-profile"}
                else load_json(text)
            )
        except (ValueError, UnicodeError):
            if status in {0, errno.ENODATA}:
                status, reason = errno.EBADMSG, "Collection emitted malformed report output."
    elif status == 0:
        status, reason = errno.EBADMSG, "Collection emitted no report output."
    if report is not None and status == 0 and report.completeness != Completeness.COMPLETE:
        status, reason = errno.ENODATA, "Validated report retains incomplete evidence."
    state: Literal["completed", "partial", "failed", "cancelled"] = (
        "cancelled"
        if status == errno.ECANCELED
        else "partial" if status == errno.ENODATA else "failed" if status else "completed"
    )
    receipt = CollectionExecutionReceipt(
        command=command,
        process=identity,
        state=state,
        status=status,
        returncode=process.process.returncode if process else None,
        forced_stop=forced,
        stream_truncated=truncated,
        stdout_bytes=len(stdout),
        stderr_bytes=len(stderr),
        stdout_sha256=hashlib.sha256(stdout).hexdigest(),
        stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        report_validated=report is not None,
        reason=reason,
        limitations=(
            "Parsed stdout is not verification of a retained generation manifest or its artifacts.",
            "Collection process ownership is not an OS sandbox or whole-program profiling proof.",
            "Cancellation or forced termination may prevent profile serialization.",
            "Captured streams are local sensitive data; do not publish them automatically.",
        ),
    )
    return CollectionExecutionResult(receipt, stdout, stderr, report)
