"""Artifact-bound selection transfer; never authorizes collection or source access."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from threading import Event, RLock, Thread
from typing import TYPE_CHECKING, Literal, Self
from urllib.parse import urlencode, urlsplit, urlunsplit

from pydantic import Field, StrictStr, model_validator

from code_analysis.models import StrictModel
from code_analysis.profile_models import ProfileReport
from code_analysis.profiling import load_profile_json, render_profile_json
from code_analysis.projections.runtime import build_runtime_graph
from code_analysis.reports.json_report import load_json
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_models import RuntimeSelection
from code_analysis.static_graph_browse import StaticGraphRequest
from code_analysis.ui_projections import UIProjectionRequest, project_report
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService

if TYPE_CHECKING:
    from code_analysis.web_server import ReportHTTPServer, WebApplication

_MAXIMUM_ARTIFACT_BYTES = 256 * 1024 * 1024


def canonical_profile_sha256(profile: ProfileReport) -> str:
    """The compact canonical profile identity, not the saved artifact's raw bytes."""
    return hashlib.sha256(render_profile_json(profile, compact=True).encode()).hexdigest()


def _capability_url(base_url: str, fields: dict[str, str]) -> str:
    """Extend a loopback session URL; the receiver still checks its capability."""
    base = urlsplit(base_url)
    if (
        base.scheme != "http"
        or base.hostname not in {"127.0.0.1", "localhost", "::1"}
        or base.username is not None
        or base.password is not None
        or base.path not in {"", "/"}
        or base.fragment
    ):
        raise ValueError("handoff requires an owned loopback Web UI URL")
    # Selection lives in the fragment; it is not sent in the initial HTTP request.
    return urlunsplit((base.scheme, base.netloc, base.path, base.query, urlencode(fields)))


class WebHandoffSelection(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["report", "finding", "symbol", "graph", "projection"] = "report"
    identity: StrictStr | None = Field(default=None, min_length=1, max_length=2048)
    graph_request: StaticGraphRequest | None = None

    @model_validator(mode="after")
    def reconcile(self) -> Self:
        if (self.kind == "projection") != (self.graph_request is not None):
            raise ValueError("projection handoff requires an exact graph request only")
        if self.kind == "projection" and self.identity is not None:
            raise ValueError("projection handoff uses graph selected IDs, not identity")
        if self.kind == "report" and self.identity is not None:
            raise ValueError("report handoff has no subject selection")
        if self.kind in {"finding", "symbol"} and self.identity is None:
            raise ValueError("finding and symbol handoffs require an exact identity")
        if self.identity is not None and any(ord(char) < 32 for char in self.identity):
            raise ValueError("handoff identities must not contain control characters")
        return self

    def validate_report(self, service: ReportViewService) -> None:
        if service.bootstrap().reports[0].report_sha256 != self.report_sha256:
            raise ValueError("handoff report identity does not match the selected saved report")
        report = service.primary
        if self.graph_request is not None:
            project_report(report, UIProjectionRequest(**self.graph_request.model_dump()))
            return
        identities = (
            {item.finding_id for item in report.findings}
            if self.kind == "finding"
            else (
                {
                    f"{module.name}:{symbol.qualified_name}"
                    for module in report.modules
                    for symbol in module.symbols
                }
                if self.kind == "symbol"
                else {item.name for item in report.modules}
            )
        )
        if self.identity is not None and self.identity not in identities:
            raise ValueError("handoff subject does not exist in the selected saved report")

    def url(self, base_url: str) -> str:
        return _capability_url(base_url, self._url_fields())

    def _url_fields(self) -> dict[str, str]:
        fields = {"report_sha256": self.report_sha256, "kind": self.kind}
        if self.identity is not None:
            fields["identity"] = self.identity
        if self.graph_request is not None:
            fields["graph_request"] = self.graph_request.model_dump_json()
        return fields


class RuntimeHandoffSelection(StrictModel):
    """Baseline runtime graph selection; carries no static report or source authority."""

    profile_report_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    profile_identity_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    graph_request: RuntimeGraphRequest

    def validate_profile(self, runtime: RuntimeViewService) -> None:
        if canonical_profile_sha256(runtime.profile) != self.profile_report_sha256:
            raise ValueError("handoff profile identity does not match the selected saved profile")
        if runtime.profile.profile_identity_sha256 != self.profile_identity_sha256:
            raise ValueError("handoff workload identity does not match the selected saved profile")
        build_runtime_graph(runtime.profile, RuntimeSelection(**self.graph_request.model_dump()))

    def url(self, base_url: str) -> str:
        return _capability_url(base_url, self._url_fields())

    def _url_fields(self) -> dict[str, str]:
        return {
            "profile_report_sha256": self.profile_report_sha256,
            "profile_identity_sha256": self.profile_identity_sha256,
            "graph_request": self.graph_request.model_dump_json(),
        }


def _saved_bytes(path: Path) -> bytes:
    """Read a bounded regular saved artifact, not a target or an index derivative."""
    maximum = _MAXIMUM_ARTIFACT_BYTES
    fd = os.open(path.expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise OSError(errno.EINVAL, "handoff requires a regular saved artifact <= 256 MiB")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            payload = stream.read(maximum + 1)
        after = os.fstat(fd)
        if len(payload) != before.st_size or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise OSError(errno.EBADMSG, "saved artifact changed during handoff")
        return payload
    finally:
        os.close(fd)


def _saved_service(path: Path) -> ReportViewService:
    return ReportViewService(load_json(_saved_bytes(path).decode("utf-8")))


def _saved_profile(path: Path) -> RuntimeViewService:
    """Baseline only; a handed-off session never claims a comparison it cannot show."""
    return RuntimeViewService(load_profile_json(_saved_bytes(path).decode("utf-8")))


class OwnedWebHandoff:
    """One read-only loopback session; terminal shutdown prevents late publication."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._closed = Event()
        self._server: ReportHTTPServer | None = None
        self._thread: Thread | None = None

    def open(self, report: Path, selection: WebHandoffSelection) -> str:
        def prepare() -> ReportViewService:
            service = _saved_service(report)
            selection.validate_report(service)
            return service

        def published(app: WebApplication) -> bool:
            service = app.service
            return (
                service is not None
                and service.bootstrap().reports[0].report_sha256 == selection.report_sha256
            )

        def start(service: ReportViewService) -> ReportHTTPServer:
            from code_analysis.web_server import create_server

            return create_server(service)

        return self._publish(prepare, published, start, selection)

    def open_runtime(self, profile: Path, selection: RuntimeHandoffSelection) -> str:
        """Start a runtime-only session: no saved report, no source, no collection."""

        def prepare() -> RuntimeViewService:
            runtime = _saved_profile(profile)
            selection.validate_profile(runtime)
            return runtime

        def published(app: WebApplication) -> bool:
            runtime = app.runtime
            return (
                app.service is None
                and runtime is not None
                and canonical_profile_sha256(runtime.profile) == selection.profile_report_sha256
                and runtime.profile.profile_identity_sha256 == selection.profile_identity_sha256
            )

        def start(runtime: RuntimeViewService) -> ReportHTTPServer:
            from code_analysis.web_server import create_server

            return create_server(None, runtime_service=runtime)

        return self._publish(prepare, published, start, selection)

    def _publish[T](
        self,
        prepare: Callable[[], T],
        published: Callable[[WebApplication], bool],
        start: Callable[[T], ReportHTTPServer],
        selection: WebHandoffSelection | RuntimeHandoffSelection,
    ) -> str:
        with self._lock:
            self._require_open()
            loaded = prepare()
            self._require_open()
            if self._server is not None and published(self._server.app):
                return self._url(selection)
            # Validate the replacement before disturbing a usable existing session.
            server = start(loaded)
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            try:
                self._require_open()
                thread.start()
            except BaseException:
                server.server_close()
                raise
            try:
                self._stop()
            except BaseException:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                raise
            self._server, self._thread = server, thread
            if self._closed.is_set():
                self._stop()
                self._require_open()
            return self._url(selection)

    def _require_open(self) -> None:
        if self._closed.is_set():
            raise OSError(errno.ECANCELED, "terminal Web handoff session is closed")

    def _url(self, selection: WebHandoffSelection | RuntimeHandoffSelection) -> str:
        assert self._server is not None
        return selection.url(
            f"http://127.0.0.1:{self._server.server_port}/?token={self._server.app.token}"
        )

    def _stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = self._thread = None

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            self._stop()
