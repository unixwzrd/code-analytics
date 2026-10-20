"""Client disconnect handling for the loopback report HTTP server."""

from __future__ import annotations

import errno
import json
import socket
import struct
import sys
import threading
from http import HTTPStatus
from http.client import HTTPConnection
from io import BytesIO
from unittest.mock import Mock

import pytest

from code_analysis.web_server import ReportRequestHandler, WebApplication, create_server
from code_analysis.web_service import ReportViewService


def _get_handler(
    service: ReportViewService,
    *,
    token: str = "disconnect-test",
    path: str = "/api/bootstrap",
) -> ReportRequestHandler:
    server = Mock()
    server.app = WebApplication(service=service, token=token)
    handler = ReportRequestHandler.__new__(ReportRequestHandler)
    handler.server = server
    handler.client_address = ("127.0.0.1", 0)
    handler.request = b""
    handler.path = path
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.protocol_version = "HTTP/1.1"
    handler.command = "GET"
    handler.wfile = BytesIO()
    handler.rfile = BytesIO()
    handler.headers = type("H", (), {})()
    handler.headers.get = lambda name, default="": {
        "Host": "127.0.0.1",
        "X-Code-Analysis-Token": token,
    }.get(name, default)
    handler.headers.get_all = lambda name, default=None: []
    return handler


def test_emit_response_body_succeeds(ordinary_report) -> None:
    handler = _get_handler(ReportViewService(ordinary_report))
    handler._send_headers(HTTPStatus.OK, "application/json", 2)
    handler._emit_response_body(b"{}")
    assert handler.wfile.getvalue().endswith(b"{}")


@pytest.mark.parametrize("error", (BrokenPipeError, ConnectionResetError))
def test_emit_handles_disconnect_on_end_headers(ordinary_report, error) -> None:
    handler = _get_handler(ReportViewService(ordinary_report))
    handler.wfile = Mock()
    handler.end_headers = Mock(side_effect=error())
    handler._emit_response_body(b"payload")
    assert handler.close_connection
    handler.wfile.write.assert_not_called()


@pytest.mark.parametrize("error", (BrokenPipeError, ConnectionResetError))
def test_emit_handles_disconnect_on_write(ordinary_report, error) -> None:
    handler = _get_handler(ReportViewService(ordinary_report))
    handler.end_headers = Mock()
    handler.wfile = Mock()
    handler.wfile.write.side_effect = error()
    handler._emit_response_body(b"payload")
    assert handler.close_connection


def test_emit_reraises_unexpected_oserror_on_write(ordinary_report) -> None:
    handler = _get_handler(ReportViewService(ordinary_report))
    handler.end_headers = Mock()
    handler.wfile = Mock()
    handler.wfile.write.side_effect = OSError(errno.ENOSPC, "No space left on device")
    with pytest.raises(OSError, match="No space"):
        handler._emit_response_body(b"payload")


def test_do_get_json_response(ordinary_report) -> None:
    handler = _get_handler(ReportViewService(ordinary_report))
    handler.do_GET()
    payload = json.loads(handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1])
    assert payload["schema_name"] == "code-analysis-ui"


@pytest.mark.parametrize(
    "error",
    (
        OSError(errno.ENOENT, "missing"),
        BrokenPipeError("provider"),
        ConnectionResetError("provider"),
    ),
)
def test_provider_oserror_returns_invalid_artifact(ordinary_report, monkeypatch, error) -> None:
    def failing_bootstrap(self: ReportViewService) -> object:
        raise error

    monkeypatch.setattr(ReportViewService, "bootstrap", failing_bootstrap)
    handler = _get_handler(ReportViewService(ordinary_report))
    handler.do_GET()
    body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1]
    payload = json.loads(body)
    assert payload["error"]["code"] == "invalid-artifact"


def test_disconnect_during_json_emission_is_not_artifact_error(ordinary_report) -> None:
    handler = _get_handler(ReportViewService(ordinary_report))
    error_calls = 0
    original_error = handler._error

    def counting_error(*args: object, **kwargs: object) -> None:
        nonlocal error_calls
        error_calls += 1
        original_error(*args, **kwargs)

    handler._error = counting_error

    def disconnect_only(_body: bytes) -> None:
        handler.close_connection = True

    handler._emit_response_body = disconnect_only
    handler.do_GET()
    assert error_calls == 0
    assert handler.close_connection


def test_aborted_projection_then_bootstrap_still_works(ordinary_report, monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    original_emit = ReportRequestHandler._emit_response_body
    failures = []

    def delayed_emit(handler, body):
        if handler.path.startswith("/api/projection?"):
            started.set()
            assert release.wait(timeout=5)
            try:
                original_emit(handler, body)
            finally:
                completed.set()
        else:
            original_emit(handler, body)

    monkeypatch.setattr(ReportRequestHandler, "_emit_response_body", delayed_emit)
    server = create_server(ReportViewService(ordinary_report), token="abort-test")
    monkeypatch.setattr(server, "handle_error", lambda *_: failures.append(sys.exc_info()[1]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        headers = (
            b"GET /api/projection?name=modules HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"X-Code-Analysis-Token: abort-test\r\n"
            b"Connection: close\r\n\r\n"
        )
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            sock.sendall(headers)
            assert started.wait(timeout=5)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        finally:
            sock.close()
            release.set()
        assert completed.wait(timeout=5)

        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "GET",
            "/api/bootstrap",
            headers={"Host": "127.0.0.1", "X-Code-Analysis-Token": "abort-test"},
        )
        response = connection.getresponse()
        assert response.status == 200
        payload = json.loads(response.read())
        assert payload["schema_name"] == "code-analysis-ui"
        connection.close()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not failures
