"""Full source is transient, byte-verified, bounded and read-only."""

from __future__ import annotations

import hashlib
import json
import os
from http.client import HTTPConnection
from urllib.parse import urlencode

import pytest
from pydantic import ValidationError
from test_graph_handoff_browser import served

from code_analysis.config import AnalysisConfig
from code_analysis.models import SourceLanguage
from code_analysis.project import discover_context
from code_analysis.report_models import SourceFileRecord, SourceParseState
from code_analysis.reports.json_report import canonical_json
from code_analysis.service import scan
from code_analysis.ui_source import SourceBrowseDocument, SourceDocument, read_source_document
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

DOCUMENT_ROUTE = "/api/code-browser/document"
TOKEN = "full-document-test"


def _saved_source(tmp_path, payload):
    target = tmp_path / "demo.py"
    target.write_bytes(payload)
    service = ReportViewService(scan(discover_context(target, AnalysisConfig(test_roots=()))))
    params = {
        "report_sha256": service.bootstrap().reports[0].report_sha256,
        "revision": 8,
        "path": "demo.py",
    }
    return target, service, params


def _get_document(connection, params, *, token=TOKEN):
    connection.request(
        "GET",
        DOCUMENT_ROUTE + "?" + urlencode(params),
        headers={"X-Code-Analysis-Token": token},
    )
    response = connection.getresponse()
    return response.status, response.getheader("Cache-Control"), response.read()


def _no_source_access(*args, **kwargs):
    pytest.fail("Rejected or disabled document requests must not read source")


@pytest.mark.parametrize(
    ("payload", "style", "final", "editable"),
    (
        (b"", "none", False, True),
        (b"value = 1", "none", False, True),
        (b"# line\n" * 300, "lf", True, True),
        ("# Unicode \u2014 \U0001f44b\r\nvalue = 1".encode(), "crlf", False, True),
        (b"value = 1\r\n", "crlf", True, True),
        (b"value = 1\r\nnext = 2\n", "mixed", True, False),
        (b"value = 1\rnext = 2", "mixed", False, False),
        (b"# <script>window.pwned = true</script>\nvalue = 1\n", "lf", True, True),
    ),
)
def test_document_http_exact_full_text_no_cache_write_or_rescan(
    tmp_path, monkeypatch, payload, style, final, editable
):
    target, service, params = _saved_source(tmp_path, payload)
    before = canonical_json(service.primary)
    monkeypatch.setattr("code_analysis.service.scan", _no_source_access)
    with served(create_server(service, source_root=tmp_path, token=TOKEN)) as server:
        contents = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            status, cache_control, body = _get_document(connection, params)
            assert status == 200 and cache_control == "no-store"
            result = SourceBrowseDocument.model_validate_json(body)
            assert result.report_sha256 == params["report_sha256"] and result.revision == 8
            assert result.document.state == "bound"
            assert result.document.text.encode() == payload
            assert (
                result.document.newline_style,
                result.document.has_final_newline,
                result.document.editable,
            ) == (style, final, editable)
            assert _get_document(connection, params)[2] == body
            assert canonical_json(service.primary) == before
            assert target.read_bytes() == payload
            assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == contents
        finally:
            connection.close()


def test_document_http_authorization_identity_and_closed_parameters(tmp_path, monkeypatch):
    _, service, params = _saved_source(tmp_path, b"def runner():\n    return 1\n")
    monkeypatch.setattr("code_analysis.ui_source._read_confined", _no_source_access)
    with served(create_server(service, source_root=tmp_path, token=TOKEN)) as server:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            assert _get_document(connection, params, token="wrong")[0] == 403
            empty = {key: value for key, value in params.items() if key != "path"}
            assert _get_document(connection, empty)[0] == 400
            for update in (
                {"report_sha256": "0" * 64},
                {"revision": "-1"},
                {"path": "../demo.py"},
                {"path": "/demo.py"},
                {"path": "demo.py/../demo.py"},
                {"path": "unknown.py"},
                {"identity": "unknown"},
                {"identity": "demo:runner", "path": "other.py"},
                {"path": ""},
                {"root": str(tmp_path)},
                {"source_root": str(tmp_path)},
                {"search": "value"},
                {"offset": "1"},
                {"limit": "10"},
                {"line": "1"},
                {"unknown": "value"},
            ):
                assert _get_document(connection, {**params, **update})[0] == 400, update
            status, _, body = _get_document(connection, (*params.items(), ("path", "demo.py")))
            assert status == 400 and "document" not in json.loads(body)
        finally:
            connection.close()


def test_document_http_source_root_is_not_guessed(tmp_path, monkeypatch):
    _, service, params = _saved_source(tmp_path, b"value = 1\n")
    monkeypatch.setattr("code_analysis.ui_source._read_confined", _no_source_access)
    with served(create_server(service, token=TOKEN)) as server:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            status, _, body = _get_document(connection, params)
            result = SourceBrowseDocument.model_validate_json(body)
            assert status == 200 and result.document.state == "disabled"
            assert result.document.text is None
        finally:
            connection.close()


def test_document_http_entity_selection_and_stale_source(tmp_path):
    payload = b"def runner():\n    return 1\n"
    target, service, params = _saved_source(tmp_path, payload)
    selection = {**params, "identity": "demo:runner"}
    selection.pop("path")
    with served(create_server(service, source_root=tmp_path, token=TOKEN)) as server:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            status, _, body = _get_document(connection, selection)
            result = SourceBrowseDocument.model_validate_json(body)
            assert status == 200 and result.document.text.encode() == payload
            target.write_bytes(b"# changed\n")
            status, _, body = _get_document(connection, selection)
            result = SourceBrowseDocument.model_validate_json(body)
            assert status == 200 and result.document.state == "mismatch"
            assert result.document.text is None and result.document.byte_count is None
        finally:
            connection.close()


@pytest.mark.parametrize(
    ("payload", "state"),
    (
        (b"#" + b"x" * (1024 * 1024), "too-large"),
        (b"# line\n" * 20_001, "too-large"),
        (b"\xff", "invalid-encoding"),
    ),
)
def test_document_http_bounds_and_invalid_utf8(tmp_path, payload, state):
    _, service, params = _saved_source(tmp_path, payload)
    with served(create_server(service, source_root=tmp_path, token=TOKEN)) as server:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            status, _, body = _get_document(connection, params)
            result = SourceBrowseDocument.model_validate_json(body)
            assert status == 200 and result.document.state == state
            assert result.document.text is None and not result.document.editable
        finally:
            connection.close()


@pytest.mark.parametrize("replacement", ("missing", "symlink", "fifo"))
def test_document_http_unavailable_and_confined(tmp_path, replacement):
    payload = b"value = 1\n"
    target, service, params = _saved_source(tmp_path, payload)
    target.unlink()
    if replacement == "symlink":
        other = tmp_path / "other.py"
        other.write_bytes(payload)
        target.symlink_to(other)
    elif replacement == "fifo":
        os.mkfifo(target)
    with served(create_server(service, source_root=tmp_path, token=TOKEN)) as server:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            status, _, body = _get_document(connection, params)
            result = SourceBrowseDocument.model_validate_json(body)
            assert status == 200 and result.document.state == "unavailable"
            assert result.document.text is None
        finally:
            connection.close()


def source_record(payload: bytes) -> SourceFileRecord:
    return SourceFileRecord(
        source_file_id="source-file-" + "a" * 20,
        logical_path="demo.py",
        logical_root="source",
        source_sha256=hashlib.sha256(payload).hexdigest(),
        language=SourceLanguage.PYTHON,
        parse_state=SourceParseState.COMPLETE,
        module_name="demo",
        physical_loc=0,
        nonblank_loc=0,
    )


@pytest.mark.parametrize(
    ("payload", "style", "final", "editable"),
    (
        (b"", "none", False, True),
        (b"value = 1", "none", False, True),
        (b"value = 1\nnext = 2", "lf", False, True),
        (b"value = 1\n", "lf", True, True),
        (b"value = 1\r\nnext = 2", "crlf", False, True),
        (b"value = 1\r\n", "crlf", True, True),
        (b"value = 1\r\nnext = 2\n", "mixed", True, False),
        (b"value = 1\rnext = 2", "mixed", False, False),
    ),
)
def test_original_bytes_and_newlines_are_preserved(tmp_path, payload, style, final, editable):
    target = tmp_path / "demo.py"
    target.write_bytes(payload)
    result = read_source_document(source_record(payload), tmp_path)
    assert result.state == "bound" and result.text.encode("utf-8") == payload
    assert (result.newline_style, result.has_final_newline, result.editable) == (
        style,
        final,
        editable,
    )
    assert SourceDocument.model_validate_json(result.model_dump_json()) == result
    assert target.read_bytes() == payload


def test_unavailable_hash_mismatch_and_encoding_never_disclose_source(tmp_path):
    payload = b"value = 1\n"
    source = source_record(payload)
    target = tmp_path / "demo.py"
    target.write_bytes(payload)
    assert read_source_document(source, None).state == "disabled"
    target.write_bytes(b"changed\n")
    mismatch = read_source_document(source, tmp_path)
    assert mismatch.state == "mismatch" and mismatch.text is None
    target.write_bytes(b"\xff")
    invalid = read_source_document(source_record(b"\xff"), tmp_path)
    assert invalid.state == "invalid-encoding" and invalid.text is None
    target.unlink()
    assert read_source_document(source, tmp_path).state == "unavailable"


def test_symlink_special_file_and_analytical_bounds(tmp_path):
    payload = b"value = 1\n"
    source = source_record(payload)
    target = tmp_path / "demo.py"
    real = tmp_path / "other.py"
    real.write_bytes(payload)
    target.symlink_to(real)
    assert read_source_document(source, tmp_path).state == "unavailable"
    target.unlink()
    os.mkfifo(target)
    assert read_source_document(source, tmp_path).state == "unavailable"
    target.unlink()
    target.write_bytes(b"x" * (1024 * 1024 + 1))
    assert read_source_document(source, tmp_path).state == "too-large"
    payload = b"# line\n" * 20_001
    target.write_bytes(payload)
    assert read_source_document(source_record(payload), tmp_path).state == "too-large"


def test_strict_document_hash_and_state_reconciliation(tmp_path):
    payload = "# Unicode — 👋\r\n".encode()
    target = tmp_path / "demo.py"
    target.write_bytes(payload)
    bound = read_source_document(source_record(payload), tmp_path)
    fields = bound.model_dump()
    for update in (
        {"text": "different"},
        {"byte_count": True},
        {"editable": 1},
        {"state": "disabled"},
        {"newline_style": "lf"},
        {"has_final_newline": False},
        {"logical_path": "../demo.py"},
        {"extra": "no"},
    ):
        with pytest.raises(ValidationError):
            SourceDocument.model_validate({**fields, **update})
