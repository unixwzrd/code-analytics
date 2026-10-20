from __future__ import annotations

import errno
import json
import os
import threading
from http.client import HTTPConnection
from urllib.parse import parse_qs, urlsplit

import pytest

from code_analysis.reports.json_report import canonical_json
from code_analysis.ui_handoff import OwnedWebHandoff, WebHandoffSelection
from code_analysis.web_service import ReportViewService


@pytest.fixture
def saved(tmp_path, ordinary_report):
    path = tmp_path / "saved.json"
    path.write_text(canonical_json(ordinary_report))
    identity = ReportViewService(ordinary_report).bootstrap().reports[0].report_sha256
    return path, WebHandoffSelection(report_sha256=identity)


def request(url, endpoint="/api/bootstrap"):
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=2)
    try:
        token = parse_qs(parsed.query)["token"][0]
        connection.request("GET", endpoint, headers={"X-Code-Analysis-Token": token})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_owned_handoff_retains_exact_snapshot_and_closes(saved):
    path, selection = saved
    owner = OwnedWebHandoff()
    try:
        url = owner.open(path, selection)
        status, bootstrap = request(url)
        assert status == 200
        assert bootstrap["reports"][0]["report_sha256"] == selection.report_sha256
        assert owner.open(path, selection) == url
        status, context = request(url, "/api/collection")
        assert status == 200
        assert not context["enabled"] and not context["execution_available"]
        path.unlink()
        assert request(url)[0] == 200  # loaded immutable snapshot, not re-reading target
        with pytest.raises(FileNotFoundError):
            owner.open(path, selection)
        assert request(url)[0] == 200  # failed replacement preserves existing session
    finally:
        owner.close()
    owner.close()
    with pytest.raises(OSError):
        request(url)
    with pytest.raises(OSError) as error:
        owner.open(path, selection)
    assert error.value.errno == errno.ECANCELED


def test_wrong_report_and_malformed_replacement_do_not_publish(saved):
    path, selection = saved
    owner = OwnedWebHandoff()
    try:
        with pytest.raises(ValueError, match="identity does not match"):
            owner.open(path, WebHandoffSelection(report_sha256="0" * 64))
        assert owner._server is None
        url = owner.open(path, selection)
        path.write_text("{}")
        with pytest.raises(ValueError):
            owner.open(path, selection)
        assert request(url)[0] == 200
    finally:
        owner.close()


def test_replacement_switches_exact_report_and_closes_previous(saved, ordinary_report):
    path, selection = saved
    owner = OwnedWebHandoff()
    try:
        previous = owner.open(path, selection)
        changed = ordinary_report.model_copy(
            update={
                "limitations": tuple(
                    sorted((*ordinary_report.limitations, "Synthetic handoff variant."))
                )
            }
        )
        path.write_text(canonical_json(changed))
        identity = ReportViewService(changed).bootstrap().reports[0].report_sha256
        current = owner.open(path, WebHandoffSelection(report_sha256=identity))
        assert request(current)[1]["reports"][0]["report_sha256"] == identity
        with pytest.raises(OSError):
            request(previous)
    finally:
        owner.close()


def test_handoff_rejects_nonregular_and_symlink_inputs(saved, tmp_path):
    path, selection = saved
    link = tmp_path / "link.json"
    link.symlink_to(path)
    fifo = tmp_path / "pipe.json"
    os.mkfifo(fifo)
    owner = OwnedWebHandoff()
    try:
        for invalid in (link, fifo, tmp_path):
            with pytest.raises(OSError):
                owner.open(invalid, selection)
        assert owner._server is None
    finally:
        owner.close()


def test_close_during_load_prevents_late_server(saved, monkeypatch):
    import code_analysis.ui_handoff as module

    path, selection = saved
    entered, release = threading.Event(), threading.Event()
    original = module._saved_service

    def delayed(path):
        entered.set()
        assert release.wait(5)
        return original(path)

    monkeypatch.setattr(module, "_saved_service", delayed)
    owner = OwnedWebHandoff()
    errors = []

    def opening():
        try:
            owner.open(path, selection)
        except OSError as error:
            errors.append(error.errno)

    opening_thread = threading.Thread(target=opening)
    opening_thread.start()
    assert entered.wait(5)
    closing_thread = threading.Thread(target=owner.close)
    closing_thread.start()
    assert owner._closed.wait(5)
    release.set()
    opening_thread.join(5)
    closing_thread.join(5)
    assert not opening_thread.is_alive() and not closing_thread.is_alive()
    assert errors == [errno.ECANCELED]
    assert owner._server is None
