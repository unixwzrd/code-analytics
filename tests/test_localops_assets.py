"""The shared toolkit is packaged and served unchanged with a closed URL set."""

from __future__ import annotations

import hashlib
import json
import threading
from http.client import HTTPConnection
from importlib.resources import files

from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService


def test_bundled_localops_provenance_and_license() -> None:
    root = files("code_analysis").joinpath("web_static/localops")
    provenance = json.loads(root.joinpath("provenance.json").read_text())
    assert provenance["version"] == "0.1.5"
    assert provenance["wheel_sha256"] == (
        "89800189b2c3177eefff40d4bc518c3eb0060ca68f97d375be76fb9d94f20929"
    )
    assert len(provenance["assets_sha256"]) == 18
    for name, digest in provenance["assets_sha256"].items():
        assert hashlib.sha256(root.joinpath(name).read_bytes()).hexdigest() == digest
    assert "LocalOps UI Kit contributors" in root.joinpath("LICENSE").read_text()


def test_localops_served_bytes_and_closed_asset_routes(ordinary_report) -> None:
    root = files("code_analysis").joinpath("web_static/localops")
    provenance = json.loads(root.joinpath("provenance.json").read_text())
    server = create_server(ReportViewService(ordinary_report), token="localops-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for name, digest in provenance["assets_sha256"].items():
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            connection.request("GET", f"/localops/{name}")
            response = connection.getresponse()
            assert response.status == 200
            assert hashlib.sha256(response.read()).hexdigest() == digest
            assert response.getheader("X-Content-Type-Options") == "nosniff"
            connection.close()
        for path in ("/localops/LICENSE", "/localops/../app.js", "/localops/not-present.js"):
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            connection.request("GET", path, headers={"X-Code-Analysis-Token": "localops-test"})
            response = connection.getresponse()
            assert response.status == 404
            response.read()
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
