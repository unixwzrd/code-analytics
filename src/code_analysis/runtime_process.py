"""Owned process sessions with bounded nonblocking streams and identity checks."""

from __future__ import annotations

import errno
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from code_analysis.profile_models import ResourceObservation


class OwnedRuntimeProcess:
    """A directly launched subject or command; no attachment to existing PIDs."""

    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        stream_limit: int,
        require_identity: bool = True,
    ) -> None:
        try:
            import psutil
        except ImportError:
            if require_identity:
                raise OSError(errno.ENODATA, "process identity provider unavailable") from None
            psutil = None
        self.psutil = psutil

        self.started = time.monotonic()
        self.exited: float | None = None
        self.process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.selector = selectors.DefaultSelector()
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.stream_limit = stream_limit
        self.overflow = False
        self.owned: dict[int, float] = {}
        self.detached_descendants: set[int] = set()
        self.last_sample = 0.0
        self.cpu_samples: dict[int, tuple[float, float]] = {}
        self.io_samples: dict[int, tuple[int, int, int, int]] = {}
        self.peak_rss: int | None = None
        self.maximum_processes: int | None = None
        identity_errors = (OSError, psutil.Error) if psutil else (OSError,)
        try:
            self.start = psutil.Process(self.process.pid).create_time() if psutil else None
            if self.start is not None:
                self.owned[self.process.pid] = self.start
            for stream, buffer in (
                (self.process.stdout, self.stdout),
                (self.process.stderr, self.stderr),
            ):
                os.set_blocking(stream.fileno(), False)
                self.selector.register(stream, selectors.EVENT_READ, buffer)
        except identity_errors:
            self.process.kill()
            try:
                self.process.wait(timeout=5)
            finally:
                for stream in (self.process.stdout, self.process.stderr):
                    if stream is not None:
                        stream.close()
                self.selector.close()
            raise OSError(errno.EPROTO, "cannot prove launched process identity") from None

    def pump(self, timeout: float = 0.05) -> None:
        if self.psutil is not None:
            self._sample()
        for key, _ in self.selector.select(timeout):
            try:
                data = os.read(key.fileobj.fileno(), 65536)
            except BlockingIOError:
                continue
            if not data:
                self.selector.unregister(key.fileobj)
                key.fileobj.close()
                continue
            room = max(0, self.stream_limit - len(key.data))
            key.data.extend(data[:room])
            self.overflow |= len(data) > room
        self.process.poll()
        if self.process.returncode is not None and self.exited is None:
            self.exited = time.monotonic()

    def _sample(self) -> None:
        psutil = self.psutil
        # Continue following proven descendants after the direct subject exits.
        for pid, start in tuple(self.owned.items()):
            try:
                root = psutil.Process(pid)
                if root.create_time() != start:
                    continue
                for child in root.children(recursive=True):
                    try:
                        self.owned[child.pid] = child.create_time()
                        try:
                            if os.getsid(child.pid) != self.process.pid:
                                self.detached_descendants.add(child.pid)
                        except ProcessLookupError:
                            pass
                    except psutil.Error:
                        continue
            except psutil.Error:
                continue
        if time.monotonic() - self.last_sample >= 0.1:
            self.last_sample = time.monotonic()
            rss = 0
            sampled = 0
            for pid in self.alive():
                try:
                    member = psutil.Process(pid)
                    if member.create_time() != self.owned[pid]:
                        continue
                    cpu = member.cpu_times()
                    rss += member.memory_info().rss
                    previous = self.cpu_samples.get(pid, (0.0, 0.0))
                    self.cpu_samples[pid] = (
                        max(previous[0], cpu.user),
                        max(previous[1], cpu.system),
                    )
                    sampled += 1
                    io_method = getattr(member, "io_counters", None)
                    if io_method is not None:
                        try:
                            counters = io_method()
                            self.io_samples[pid] = (
                                counters.read_bytes,
                                counters.write_bytes,
                                counters.read_count,
                                counters.write_count,
                            )
                        except (psutil.Error, NotImplementedError):
                            pass
                except psutil.Error:
                    continue
            if sampled:
                self.peak_rss = max(self.peak_rss or 0, rss)
                self.maximum_processes = max(self.maximum_processes or 0, sampled)

    def alive(self) -> tuple[int, ...]:
        psutil = self.psutil
        if psutil is None:
            return (self.process.pid,) if self.process.poll() is None else ()

        alive = []
        for pid, start in self.owned.items():
            try:
                process = psutil.Process(pid)
                if process.create_time() == start and process.status() != psutil.STATUS_ZOMBIE:
                    alive.append(pid)
            except psutil.Error:
                continue
        return tuple(alive)

    def send(self, number: int) -> None:
        psutil = self.psutil
        if psutil is None:
            # Popen guards the unreaped direct child; no process-tree claim or group signal.
            self.process.send_signal(number)
            return

        # Signal individually proven members; never signal a reused group ID.
        for pid in self.alive():
            try:
                process = psutil.Process(pid)
                if process.create_time() == self.owned[pid]:
                    process.send_signal(number)
            except psutil.Error:
                continue

    def wait(self, deadline: float, *, companions: tuple[OwnedRuntimeProcess, ...] = ()) -> int:
        """Drain all active streams while waiting, including after direct-child exit."""
        while True:
            for companion in companions:
                companion.pump(0.0)
                if companion.overflow:
                    raise OSError(errno.ENODATA, "subject stream exceeded configured byte bound")
                if companion.detached_descendants:
                    raise OSError(errno.EPROTO, "subject spawned an undeclared detached process")
                if companion.process.poll() is not None:
                    raise OSError(errno.EPROTO, "subject exited during runtime command")
            self.pump(min(0.05, max(0.0, deadline - time.monotonic())))
            if self.overflow:
                raise OSError(errno.ENODATA, "runtime stream exceeded configured byte bound")
            if self.detached_descendants:
                raise OSError(
                    errno.EPROTO, "runtime command spawned an undeclared detached process"
                )
            if time.monotonic() >= deadline:
                raise OSError(errno.ETIMEDOUT, "owned runtime command timed out")
            if self.process.returncode is not None and not self.selector.get_map():
                return self.process.returncode

    def shutdown(
        self,
        *,
        graceful: int,
        graceful_seconds: float,
        forced_seconds: float,
        deadline: float | None = None,
        companions: tuple[OwnedRuntimeProcess, ...] = (),
    ) -> bool:
        """Clamp graceful work to the run deadline; forced reaping has a safety budget."""
        self.pump(0.0)
        self.send(graceful)
        grace_end = time.monotonic() + graceful_seconds
        if deadline is not None:
            grace_end = min(grace_end, deadline)
        self._drain_until_stopped(grace_end, companions)
        forced = bool(self.alive())
        if forced:
            self.send(signal.SIGKILL)
            self._drain_until_stopped(time.monotonic() + forced_seconds, companions)
        self.process.poll()
        if self.alive():
            raise OSError(errno.ETIMEDOUT, "owned processes remained after bounded forced stop")
        return forced

    def _drain_until_stopped(
        self, deadline: float, companions: tuple[OwnedRuntimeProcess, ...]
    ) -> None:
        while (self.alive() or self.selector.get_map()) and time.monotonic() < deadline:
            for companion in companions:
                companion.pump(0.0)
            self.pump(min(0.02, max(0.0, deadline - time.monotonic())))

    def close(self) -> None:
        for key in list(self.selector.get_map().values()):
            key.fileobj.close()
        self.selector.close()

    def resources(self) -> ResourceObservation:
        return ResourceObservation(
            repetition=1,
            wall_seconds=self.exited - self.started if self.exited is not None else None,
            user_cpu_seconds=(
                sum(item[0] for item in self.cpu_samples.values()) if self.cpu_samples else None
            ),
            system_cpu_seconds=(
                sum(item[1] for item in self.cpu_samples.values()) if self.cpu_samples else None
            ),
            peak_rss_bytes=self.peak_rss,
            maximum_owned_processes=self.maximum_processes,
            read_bytes=(
                sum(item[0] for item in self.io_samples.values()) if self.io_samples else None
            ),
            write_bytes=(
                sum(item[1] for item in self.io_samples.values()) if self.io_samples else None
            ),
            read_count=(
                sum(item[2] for item in self.io_samples.values()) if self.io_samples else None
            ),
            write_count=(
                sum(item[3] for item in self.io_samples.values()) if self.io_samples else None
            ),
        )
