"""Private, capability-verified UI launch/reopen/stop; never signal registry PIDs."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, Self
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import Field, StrictBool, StrictInt, StrictStr, ValidationError, model_validator

from code_analysis import __version__
from code_analysis.models import StrictModel
from code_analysis.web_lifecycle import UILifecycleStatus, UIShutdownRequest


class UILaunchOptions(StrictModel):
    report: StrictStr | None = None
    compare: StrictStr | None = None
    profile_report: StrictStr | None = None
    compare_profile: StrictStr | None = None
    workspace: StrictStr | None = None
    source_root: StrictStr | None = None
    collection_root: StrictStr
    collection_python: tuple[StrictStr, ...] = ()
    display_config: StrictStr | None = None
    host: Literal["127.0.0.1", "localhost", "::1"]
    port: StrictInt = Field(ge=0, le=65535)
    allow_collection: StrictBool
    interpreter: StrictStr
    cwd: StrictStr
    input_hashes: tuple[tuple[StrictStr, StrictStr], ...] = ()
    environment_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    implementation_root: StrictStr

    @property
    def session_id(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:24]


class UISession(StrictModel):
    session_id: StrictStr = Field(pattern=r"^[a-f0-9]{24}$")
    instance_id: StrictStr = Field(pattern=r"^[a-f0-9]{32}$")
    token: StrictStr = Field(min_length=32, max_length=128)
    options: UILaunchOptions
    port: StrictInt = Field(ge=0, le=65535, default=0)
    pid: StrictInt = Field(ge=0, default=0)

    @model_validator(mode="after")
    def reconcile(self) -> Self:
        if self.session_id != self.options.session_id or bool(self.port) != bool(self.pid):
            raise ValueError("session identity or readiness fields disagree")
        return self

    @property
    def base_url(self) -> str:
        host = f"[{self.options.host}]" if ":" in self.options.host else self.options.host
        return f"http://{host}:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.base_url}/?token={self.token}"


class UIStartupFailure(StrictModel):
    instance_id: StrictStr = Field(pattern=r"^[a-f0-9]{32}$")
    status: StrictInt = Field(gt=0, le=255)


def launch_options(args: argparse.Namespace) -> UILaunchOptions:
    paths: dict[str, str | None] = {}
    for name in (
        "report",
        "compare",
        "profile_report",
        "compare_profile",
        "workspace",
        "source_root",
        "collection_root",
        "display_config",
    ):
        value = getattr(args, name, None)
        paths[name] = str(value.expanduser().absolute()) if value is not None else None
    hashes = []
    for name in ("report", "compare", "profile_report", "compare_profile", "display_config"):
        selected_path = paths[name]
        if selected_path is not None:
            path = Path(selected_path)
            if path.is_dir():
                path = path / "profile.json"
            hashes.append((name, hashlib.sha256(path.read_bytes()).hexdigest()))
    return UILaunchOptions(
        report=paths["report"],
        compare=paths["compare"],
        profile_report=paths["profile_report"],
        compare_profile=paths["compare_profile"],
        workspace=paths["workspace"],
        source_root=paths["source_root"],
        collection_root=paths["collection_root"] or str(Path.cwd()),
        display_config=paths["display_config"],
        host=args.host,
        port=args.port,
        allow_collection=args.allow_collection,
        collection_python=tuple(
            str(path.expanduser().absolute()) for path in args.collection_python
        ),
        interpreter=sys.executable,
        cwd=str(Path.cwd()),
        input_hashes=tuple(hashes),
        implementation_root=str(Path(__file__).resolve().parent),
        environment_sha256=hashlib.sha256(
            json.dumps(
                {
                    "tool_version": __version__,
                    "environment": sorted(
                        (name, value)
                        for name, value in os.environ.items()
                        if name not in {"COLUMNS", "LINES"}
                    ),
                }
            ).encode()
        ).hexdigest(),
    )


def state_directory() -> Path:
    root = Path(
        os.environ.get("CODE_ANALYSIS_UI_STATE_DIR", Path.home() / ".cache/code-analysis/ui")
    )
    root = root.expanduser().absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError(errno.EACCES, "UI state must be a private owned directory", str(root))
    return root


def _open_private(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise OSError(errno.EACCES, "UI state must be an owned private regular file", str(path))
    return fd


@contextmanager
def registry_lock(root: Path, name: str = "registry.lock") -> Iterator[None]:
    fd = _open_private(root / name, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def read_session(path: Path) -> UISession:
    fd = _open_private(path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as stream:
        payload = stream.read(65537)
    try:
        if len(payload) > 65536:
            raise ValueError("oversized UI session")
        result = UISession.model_validate_json(payload)
        if result.session_id != result.options.session_id:
            raise ValueError("UI session/options identity mismatch")
        return result
    except (ValueError, ValidationError) as exc:
        raise OSError(errno.EBADMSG, "invalid UI session record", str(path)) from exc


def write_session(path: Path, record: StrictModel) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        fd = _open_private(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "w") as stream:
            stream.write(record.model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # Capability tokens must never leave the original loopback endpoint.


def session_request(
    record: UISession, shutdown: UIShutdownRequest | None = None
) -> UILifecycleStatus:
    path = "/api/session/stop" if shutdown is not None else "/api/session"
    data = shutdown.model_dump_json().encode() if shutdown is not None else None
    request = Request(
        record.base_url + path,
        data=data,
        headers={
            "X-Code-Analysis-Token": record.token,
            "Origin": record.base_url,
            "Content-Type": "application/json",
        },
    )
    try:
        with build_opener(ProxyHandler({}), _NoRedirects()).open(request, timeout=2) as response:
            result = UILifecycleStatus.model_validate_json(response.read(65537))
    except HTTPError as exc:
        raise OSError(
            errno.EBUSY if exc.code == 409 else errno.EPROTO,
            (
                "active jobs require --cancel-jobs"
                if exc.code == 409
                else "UI session rejected the request"
            ),
        ) from exc
    except ValidationError as exc:
        raise OSError(errno.EBADMSG, "invalid UI session response") from exc
    if result.instance_id != record.instance_id or result.pid != record.pid:
        raise OSError(errno.EBADMSG, "UI session process/instance identity mismatch")
    return result


def _probe(record: UISession) -> UILifecycleStatus | None:
    if not record.port or not record.pid:
        return None
    try:
        return session_request(record)
    except (URLError, TimeoutError, ConnectionError):
        return None


def _reachable(record: UISession) -> bool:
    status = _probe(record)
    return status is not None and not status.stopping


def _lease_busy(root: Path, session_id: str) -> bool:
    fd = _open_private(root / f"{session_id}.lease", os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def start_session(options: UILaunchOptions, *, timeout: float = 20) -> tuple[UISession, bool]:
    if options.host not in {"localhost", "127.0.0.1", "::1"}:
        raise OSError(errno.EINVAL, "UI is loopback-only")
    root = state_directory()
    path = root / f"{options.session_id}.json"
    with registry_lock(root):
        if path.exists() or path.is_symlink():
            existing = read_session(path)
            status = _probe(existing)
            if status is not None and not status.stopping:
                return existing, False
        if _lease_busy(root, options.session_id):
            raise OSError(
                errno.EBUSY, "UI session is starting, stopping or not responding; inspect its log"
            )
        record = UISession(
            session_id=options.session_id,
            instance_id=secrets.token_hex(16),
            token=secrets.token_urlsafe(32),
            options=options,
        )
        request_path = root / f"{options.session_id}.request"
        write_session(request_path, record)
        log_path = root / f"{options.session_id}.log"
        fd = _open_private(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            with os.fdopen(fd, "ab") as log:
                child = subprocess.Popen(
                    [sys.executable, "-I", "-m", "code_analysis.web_launcher", str(request_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    close_fds=True,
                    cwd=options.cwd,
                )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    failure_path = root / f"{record.session_id}.error"
                    startup_status = errno.EPROTO
                    if failure_path.exists():
                        with os.fdopen(_open_private(failure_path, os.O_RDONLY), "rb") as error:
                            failure = UIStartupFailure.model_validate_json(error.read(65537))
                        if (
                            failure.instance_id == record.instance_id
                            and failure.status in errno.errorcode
                        ):
                            startup_status = failure.status
                    raise OSError(
                        startup_status,
                        f"UI startup failed; inspect private log: {log_path}",
                    )
                if path.exists():
                    ready = read_session(path)
                    if ready.instance_id == record.instance_id and _reachable(ready):
                        return ready, True
                time.sleep(0.1)
            raise OSError(errno.ETIMEDOUT, f"UI readiness timed out; inspect: {log_path}")
        except BaseException:
            if "child" in locals() and child.poll() is None:
                child.terminate()  # Only the direct child created by this launch.
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            raise
        finally:
            request_path.unlink(missing_ok=True)


def stop_session(session_id: str | None, *, cancel_jobs: bool) -> UISession:
    root = state_directory()
    with registry_lock(root):
        records = []
        for path in sorted(root.glob("*.json")):
            record = read_session(path)
            if session_id is not None and record.session_id != session_id:
                continue
            if session_id is None and record.options.collection_root != str(Path.cwd()):
                continue
            if _probe(record) is not None:
                records.append(record)
        if not records:
            raise OSError(errno.ENOENT, "no matching running Code Analysis UI")
        if len(records) != 1:
            raise OSError(
                errno.EINVAL,
                "multiple sessions; specify --session: "
                + ", ".join(record.session_id for record in records),
            )
        record = records[0]
        session_request(record, UIShutdownRequest(confirmed=True, cancel_jobs=cancel_jobs))
    # The child must acquire this lock to remove its exact receipt after cleanup.
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        with registry_lock(root):
            path = root / f"{record.session_id}.json"
            if not path.exists() or read_session(path).instance_id != record.instance_id:
                return record
        time.sleep(0.1)
    raise OSError(errno.ETIMEDOUT, "UI shutdown has not completed; inspect its private log")


def run_child(request_path: Path) -> int:
    from code_analysis.cli import _web

    root = state_directory()
    if request_path.parent != root or request_path.suffix != ".request":
        raise OSError(errno.EINVAL, "invalid UI launch request location")
    record = read_session(request_path)
    if request_path.name != f"{record.session_id}.request":
        raise OSError(errno.EBADMSG, "UI launch request identity mismatch")
    args = record.options.model_dump(
        exclude={"interpreter", "cwd", "input_hashes", "environment_sha256", "implementation_root"}
    )
    for name in (
        "report",
        "compare",
        "profile_report",
        "compare_profile",
        "workspace",
        "source_root",
        "collection_root",
        "display_config",
    ):
        args[name] = Path(args[name]) if args[name] is not None else None
    args["collection_python"] = tuple(Path(item) for item in record.options.collection_python)
    actual = launch_options(argparse.Namespace(**args))
    if actual != record.options:
        changed = ", ".join(
            name
            for name in type(actual).model_fields
            if getattr(actual, name) != getattr(record.options, name)
        )
        raise OSError(errno.EBADMSG, f"UI launch inputs changed before startup: {changed}")
    args.update(open=False, managed_session=record)
    with registry_lock(root, f"{record.session_id}.lease"):
        return _web(argparse.Namespace(**args))


def publish_session(record: UISession, port: int) -> UISession:
    ready = record.model_copy(update={"port": port, "pid": os.getpid()})
    write_session(state_directory() / f"{ready.session_id}.json", ready)
    return ready


def remove_session(record: UISession) -> None:
    root = state_directory()
    path = root / f"{record.session_id}.json"
    with registry_lock(root):
        if path.exists() and read_session(path).instance_id == record.instance_id:
            path.unlink()


if __name__ == "__main__":
    record = None
    try:
        record = read_session(Path(sys.argv[1]))
        sys.exit(run_child(Path(sys.argv[1])))
    except Exception as exc:
        # The outer process boundary preserves declared errno, never a raw Python exit 1.
        status = (
            (exc.errno or errno.EIO)
            if isinstance(exc, OSError)
            else errno.EBADMSG if isinstance(exc, ValueError) else errno.EFAULT
        )
        print(str(exc), file=sys.stderr)
        if record is not None:
            write_session(
                state_directory() / f"{record.session_id}.error",
                UIStartupFailure(instance_id=record.instance_id, status=status),
            )
        sys.exit(status)
