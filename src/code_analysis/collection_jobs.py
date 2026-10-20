"""Single-workspace owned jobs, durable local receipts and explicit cancellation."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import stat
from contextlib import suppress
from datetime import UTC, datetime
from threading import Event, RLock, Thread
from uuid import uuid4

from code_analysis.collection_artifacts import (
    bind_collection_generation,
    discover_collection_generation,
)
from code_analysis.collection_commands import (
    CollectionCommand,
    CollectionPlanner,
    CollectionRequest,
)
from code_analysis.collection_execution import (
    CollectionExecutionLimits,
    CollectionExecutionResult,
    CollectionProcessIdentity,
    execute_collection,
)
from code_analysis.collection_job_models import CollectionJob, CollectionJobLog
from code_analysis.profiling import load_profile_json
from code_analysis.reports.json_report import load_json
from code_analysis.workspace import WorkspacePaths, validate_workspace_ownership


class CollectionJobs:
    """Browser lifetime is independent of this backend-owned worker.

    The caller supplies an already-owned workspace, explicitly selected in the
    reviewed command. No source scanning or workspace claiming occurs here.
    """

    def __init__(self, planner: CollectionPlanner, workspace: WorkspacePaths):
        validate_workspace_ownership(workspace)
        self.planner, self.workspace = planner, workspace
        self._lock = RLock()
        self._cancel = Event()
        self._thread: Thread | None = None
        self._active: str | None = None
        self._closed = False
        self._closing = False
        self._write_error: OSError | None = None
        self.directory = workspace.root / "collection-jobs"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self._fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._lease = -1
        try:
            self._lease = os.open(
                ".lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=self._fd,
            )
            if not stat.S_ISREG(os.fstat(self._lease).st_mode):
                raise OSError(errno.EBADMSG, "job lease must be a regular file")
            try:
                fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OSError(errno.EBUSY, "workspace already has a job controller") from exc
            self._recover()
        except BaseException:
            if self._lease >= 0:
                os.close(self._lease)
            os.close(self._fd)
            raise

    def _recover(self) -> None:
        for job in self.list():
            if job.state not in {"queued", "running"}:
                continue
            if job.process is None:
                raise OSError(
                    errno.ENODATA, "interrupted job lacks durable launch identity; review required"
                )
            if job.process is not None:
                try:
                    import psutil
                except ImportError as exc:
                    raise OSError(errno.ENODATA, "cannot verify interrupted job process") from exc
                try:
                    process = psutil.Process(job.process.pid)
                    if (
                        process.create_time() == job.process.start_time
                        and process.status() != psutil.STATUS_ZOMBIE
                    ):
                        raise OSError(
                            errno.EBUSY, "recorded collection remains live; refusing adoption"
                        )
                except psutil.NoSuchProcess:
                    pass
                except psutil.Error as exc:
                    raise OSError(
                        errno.ENODATA, "interrupted job process state is unresolved"
                    ) from exc
            self._write(
                self._updated(
                    job,
                    state="failed",
                    status=errno.ENODATA,
                    reason="Backend stopped before a terminal receipt; outcome is unverified.",
                )
            )

    def _check(self) -> None:
        if self._closed:
            raise OSError(errno.EBADF, "job controller is closed")
        if self._write_error is not None:
            raise OSError(
                errno.EIO, "job persistence failed; retained state needs inspection"
            ) from self._write_error

    def list(self) -> tuple[CollectionJob, ...]:
        with self._lock:
            self._check()
            names = []
            with os.scandir(self._fd) as entries:
                for count, entry in enumerate(entries):
                    if count >= 64:
                        raise OSError(errno.ENODATA, "job directory inventory exceeds its bound")
                    if not entry.name.startswith("."):
                        names.append(entry.name)
                    if len(names) > 32:
                        raise OSError(errno.ENODATA, "job catalog exceeds 32 retained records")
            return tuple(
                sorted(
                    (self.get(name) for name in names),
                    key=lambda item: (item.created_at, item.job_id),
                    reverse=True,
                )
            )

    def _job_fd(self, identity: str) -> int:
        if re.fullmatch(r"[a-f0-9]{32}", identity) is None:
            raise OSError(errno.EINVAL, "invalid job identity")
        return os.open(identity, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._fd)

    @staticmethod
    def _read(fd: int, name: str, limit: int) -> bytes:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(source, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OSError(errno.EBADMSG, "job artifact must be a regular file")
            payload = handle.read(limit + 1)
        if len(payload) > limit:
            raise OSError(errno.ENODATA, "job artifact exceeds byte bound")
        return payload

    def get(self, identity: str) -> CollectionJob:
        with self._lock:
            self._check()
            fd = self._job_fd(identity)
            try:
                job = CollectionJob.model_validate_json(self._read(fd, "job.json", 128 * 1024))
            except ValueError as exc:
                raise OSError(errno.EBADMSG, "invalid retained job") from exc
            finally:
                os.close(fd)
            if (
                job.job_id != identity
                or job.workspace_path != str(self.workspace.root)
                or job.collection_root != str(self.planner.root)
            ):
                raise OSError(errno.EBADMSG, "retained job belongs to another workspace/root")
            return job

    @staticmethod
    def _atomic(fd: int, name: str, payload: bytes) -> None:
        temporary = f".{name}-{uuid4().hex}.tmp"
        target = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        try:
            with os.fdopen(target, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=fd)

    def _write(self, job: CollectionJob) -> None:
        payload = job.model_dump_json().encode()
        if len(payload) > 128 * 1024:
            raise OSError(errno.ENODATA, "job metadata exceeds byte bound")
        fd = self._job_fd(job.job_id)
        try:
            self._atomic(fd, "job.json", payload)
        finally:
            os.close(fd)

    @staticmethod
    def _updated(job: CollectionJob, **changes: object) -> CollectionJob:
        return CollectionJob.model_validate(
            {**job.model_dump(), **changes, "updated_at": datetime.now(UTC).isoformat()}
        )

    def submit(
        self,
        request: CollectionRequest,
        reviewed_command: CollectionCommand,
        limits: CollectionExecutionLimits,
    ) -> CollectionJob:
        with self._lock:
            self._check()
            if self._closing:
                raise OSError(errno.EBUSY, "job controller is stopping")
            if self._active is not None:
                raise OSError(errno.EBUSY, "a collection is already active")
            if self.planner.preview(request) != reviewed_command:
                raise OSError(errno.EINVAL, "reviewed command changed")
            if (
                request.workspace is None
                or (self.planner.root / request.workspace).resolve()
                != self.workspace.root.resolve()
            ):
                raise OSError(errno.EINVAL, "select this owned workspace explicitly in the command")
            jobs = self.list()
            if len(jobs) >= 32:
                raise OSError(errno.ENOSPC, "retained job count limit reached")
            used = self._retained_bytes(jobs)
            if used + 2 * limits.stream_bytes + 128 * 1024 > 1024 * 1024 * 1024:
                raise OSError(errno.ENOSPC, "retained job stream budget reached")
            instant = datetime.now(UTC).isoformat()
            job = CollectionJob(
                job_id=uuid4().hex,
                created_at=instant,
                updated_at=instant,
                collection_root=str(self.planner.root),
                workspace_path=str(self.workspace.root),
                request=request,
                command=reviewed_command,
                limits=limits,
                state="queued",
                reason="Collection accepted by backend.",
            )
            if 2 * len(job.model_dump_json().encode()) + 8192 > 128 * 1024:
                raise OSError(errno.ENODATA, "job options exceed durable metadata budget")
            os.mkdir(job.job_id, mode=0o700, dir_fd=self._fd)
            os.fsync(self._fd)
            self._write(job)
            self._active = job.job_id
            self._cancel.clear()
            self._thread = Thread(
                target=self._run, args=(job,), name=f"collection-{job.job_id}", daemon=False
            )
            try:
                self._thread.start()
            except RuntimeError:
                self._active = None
                self._thread = None
                self._write(
                    self._updated(
                        job,
                        state="failed",
                        status=errno.EAGAIN,
                        reason="Worker could not start; no collection launched.",
                    )
                )
                raise
            return job

    def _retained_bytes(self, jobs: tuple[CollectionJob, ...]) -> int:
        total = 0
        for job in jobs:
            fd = self._job_fd(job.job_id)
            try:
                with os.scandir(fd) as entries:
                    for count, entry in enumerate(entries):
                        if count >= 8:
                            raise OSError(errno.ENODATA, "job directory exceeds artifact bound")
                        metadata = entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(metadata.st_mode):
                            raise OSError(errno.EBADMSG, "job artifact is not a regular file")
                        total += metadata.st_size
            finally:
                os.close(fd)
        return total

    def cancel(self, identity: str) -> CollectionJob:
        with self._lock:
            job = self.get(identity)
            if job.state in {"queued", "running"}:
                if identity != self._active:
                    raise OSError(errno.EPERM, "this backend does not own the active job")
                self._cancel.set()
            return job

    def _started(self, job: CollectionJob, process: CollectionProcessIdentity) -> None:
        with self._lock:
            self._write(
                self._updated(
                    job,
                    state="running",
                    process=process,
                    reason="Owned collection process is running.",
                )
            )

    def _run(self, job: CollectionJob) -> None:
        try:
            result = execute_collection(
                self.planner,
                job.request,
                reviewed_command=job.command,
                limits=job.limits,
                cancel=self._cancel,
                on_started=lambda process: self._started(job, process),
            )
            self._finish(job, result)
        except Exception as exc:
            try:
                with self._lock:
                    current = self.get(job.job_id)
                    self._write(
                        self._updated(
                            current,
                            state="failed",
                            status=(
                                exc.errno
                                if isinstance(exc, OSError) and exc.errno
                                else errno.EBADMSG if isinstance(exc, ValueError) else errno.EFAULT
                            ),
                            reason="Collection/persistence failed; success is not claimed.",
                        )
                    )
            except OSError as write_error:
                self._write_error = write_error
        finally:
            with self._lock:
                self._active = None

    def _finish(self, job: CollectionJob, result: CollectionExecutionResult) -> None:
        receipt = result.receipt
        binding = None
        state, status, reason = receipt.state, receipt.status, receipt.reason
        if receipt.report_validated:
            try:
                binding = discover_collection_generation(result, self.workspace.root)
            except (OSError, ValueError) as exc:
                if state in {"completed", "partial"}:
                    status = exc.errno if isinstance(exc, OSError) and exc.errno else errno.EBADMSG
                    state = "partial" if status == errno.ENODATA else "failed"
                    reason = "Captured report exists, but retained generation verification failed."
        terminal = self._updated(
            job,
            state=state,
            status=status,
            reason=reason,
            process=receipt.process,
            receipt=receipt,
            binding=binding,
        )
        with self._lock:
            fd = self._job_fd(job.job_id)
            try:
                self._atomic(fd, "stdout", result.stdout)
                self._atomic(fd, "stderr", result.stderr)
            finally:
                os.close(fd)
            self._write(terminal)

    def result(self, identity: str) -> CollectionExecutionResult:
        with self._lock:
            job = self.get(identity)
            if job.receipt is None:
                raise OSError(errno.ENODATA, "job has no terminal capture")
            fd = self._job_fd(identity)
            try:
                stdout = self._read(fd, "stdout", job.limits.stream_bytes)
                stderr = self._read(fd, "stderr", job.limits.stream_bytes)
            finally:
                os.close(fd)
            for payload, size, digest in (
                (stdout, job.receipt.stdout_bytes, job.receipt.stdout_sha256),
                (stderr, job.receipt.stderr_bytes, job.receipt.stderr_sha256),
            ):
                if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
                    raise OSError(errno.EBADMSG, "retained job capture changed")
            report = None
            if job.receipt.report_validated:
                report = (
                    load_profile_json(stdout.decode())
                    if job.request.operation in {"profile", "import-profile"}
                    else load_json(stdout.decode())
                )
            result = CollectionExecutionResult(job.receipt, stdout, stderr, report)
            if (
                job.binding is not None
                and bind_collection_generation(
                    result, self.workspace.root, job.binding.generation_id
                )
                != job.binding
            ):
                raise OSError(errno.EBADMSG, "retained generation binding changed")
            return result

    def log(self, identity: str) -> CollectionJobLog:
        with self._lock:
            job = self.get(identity)
            if job.receipt is None:
                raise OSError(errno.ENODATA, "CLI logs are available after capture finishes")
            fd = self._job_fd(identity)
            try:
                payload = self._read(fd, "stderr", job.limits.stream_bytes)
            finally:
                os.close(fd)
            if (
                len(payload) != job.receipt.stderr_bytes
                or hashlib.sha256(payload).hexdigest() != job.receipt.stderr_sha256
            ):
                raise OSError(errno.EBADMSG, "retained CLI stderr changed")
            shown = min(len(payload), 16_384)
            return CollectionJobLog(
                job_id=identity,
                text=payload[-shown:].decode(errors="replace") if shown else "",
                total_bytes=len(payload),
                shown_bytes=shown,
                omitted_bytes=len(payload) - shown,
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closing = True
            self._cancel.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=15)
            if thread.is_alive():
                raise OSError(errno.EBUSY, "collection worker has not stopped; lease remains held")
        with self._lock:
            self._closed = True
            os.close(self._lease)
            os.close(self._fd)
