from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from pydantic import ValidationError

from code_analysis.profile_models import (
    CallerRepresentation,
    FrameClassification,
    ProfileReport,
)
from code_analysis.profiling import render_profile_json
from code_analysis.projections.runtime import build_runtime_graph
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_index import RuntimeIndex, build_runtime_index
from code_analysis.runtime_models import RuntimeSelection
from code_analysis.ui_handoff import (
    OwnedWebHandoff,
    RuntimeHandoffSelection,
    WebHandoffSelection,
    canonical_profile_sha256,
)
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService

GOLDEN = Path(__file__).parent / "golden" / "phase4-profile-report.json"


def mixed_profile() -> ProfileReport:
    """A profile with a non-project frame and an untimed edge, so filters and nulls show."""
    original = RuntimeViewService.load(GOLDEN).profile
    frames = tuple(
        original.frames[0].model_copy(
            update={
                "frame_id": f"profile-frame-{number:020x}",
                "function": f"function_{number}",
                "cumulative_seconds": float(number + 1),
                "classification": (
                    FrameClassification.PROJECT if number % 2 else FrameClassification.ENVIRONMENT
                ),
            }
        )
        for number in range(4)
    )
    edges = tuple(
        original.runtime_edges[0].model_copy(
            update={
                "runtime_edge_id": f"runtime-edge-{number:020x}",
                "caller_frame_id": frames[number].frame_id,
                "callee_frame_id": frames[number].frame_id,
                "total_calls": number + 1,
                # Integer callers carry no timing at all; the null must survive intact.
                "caller_representation": (
                    CallerRepresentation.COUNT if number % 2 else CallerRepresentation.TIMED
                ),
                "primitive_calls": None if number % 2 else 1,
                "total_seconds": None if number % 2 else 0.25,
                "cumulative_seconds": None if number % 2 else 0.5,
            }
        )
        for number in range(4)
    )
    return ProfileReport.model_validate(
        {**original.model_dump(), "frames": frames, "runtime_edges": edges, "statistics": ()}
    )


@pytest.fixture
def saved_profile(tmp_path):
    profile = mixed_profile()
    path = tmp_path / "profile.json"
    path.write_text(render_profile_json(profile), encoding="utf-8")
    selection = RuntimeHandoffSelection(
        profile_report_sha256=canonical_profile_sha256(profile),
        profile_identity_sha256=profile.profile_identity_sha256,
        graph_request=RuntimeGraphRequest(max_nodes=3, max_edges=2),
    )
    return path, profile, selection


def request(url, endpoint="/api/runtime"):
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        token = parse_qs(parsed.query)["token"][0]
        connection.request("GET", endpoint, headers={"X-Code-Analysis-Token": token})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.fixture
def runtime_server():
    profile = mixed_profile()
    server = create_server(
        None, token="runtime-handoff-test", runtime_service=RuntimeViewService(profile)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, profile
    server.shutdown()
    thread.join(5)
    server.server_close()


def fetch(server, path, *, token="runtime-handoff-test"):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        headers = {"X-Code-Analysis-Token": token} if token is not None else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


# --- selection contract -------------------------------------------------------------


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"graph_request": RuntimeGraphRequest(), "kind": "graph"},
        {"graph_request": RuntimeGraphRequest(), "identity": "extra"},
        {"graph_request": {"max_nodes": 101}},
        {"graph_request": {"max_edges": True}},
        {"graph_request": {"project_only": 1}},
        {"graph_request": {"unexpected": 1}},
    ],
)
def test_runtime_handoff_closed_contract(options):
    with pytest.raises(ValidationError):
        RuntimeHandoffSelection(
            profile_report_sha256="0" * 64, profile_identity_sha256="0" * 64, **options
        )


def test_runtime_handoff_requires_both_lowercase_identities():
    common = {"graph_request": RuntimeGraphRequest()}
    for fields in (
        {"profile_report_sha256": "0" * 64},
        {"profile_identity_sha256": "0" * 64},
        {"profile_report_sha256": "A" * 64, "profile_identity_sha256": "0" * 64},
        {"profile_report_sha256": "0" * 63, "profile_identity_sha256": "0" * 64},
    ):
        with pytest.raises(ValidationError):
            RuntimeHandoffSelection(**common, **fields)


def test_runtime_handoff_url_rejects_nonlocal_or_ambiguous_targets(saved_profile):
    _, _, selection = saved_profile
    for url in (
        "https://example.com",
        "http://user@localhost",
        "file:///tmp/profile",
        "http://localhost/other",
        "http://localhost/#old",
        "http://example.com:8000/",
    ):
        with pytest.raises(ValueError, match="loopback"):
            selection.url(url)
    url = selection.url("http://127.0.0.1:8000/?token=owned")
    assert parse_qs(urlsplit(url).query) == {"token": ["owned"]}
    fields = parse_qs(urlsplit(url).fragment)
    assert fields["profile_report_sha256"] == [selection.profile_report_sha256]
    assert fields["profile_identity_sha256"] == [selection.profile_identity_sha256]
    assert "report_sha256" not in fields and "kind" not in fields
    assert (
        RuntimeGraphRequest.model_validate_json(fields["graph_request"][0])
        == selection.graph_request
    )


def test_canonical_digest_is_compact_rendering_not_raw_bytes_or_static_identity(
    saved_profile, tmp_path
):
    path, profile, selection = saved_profile
    compact = tmp_path / "compact.json"
    compact.write_text(render_profile_json(profile, compact=True), encoding="utf-8")
    raw_pretty = hashlib.sha256(path.read_bytes()).hexdigest()
    raw_compact = hashlib.sha256(compact.read_bytes()).hexdigest()
    assert selection.profile_report_sha256 == raw_compact != raw_pretty
    assert selection.profile_report_sha256 != profile.source_report_sha256
    assert selection.profile_report_sha256 != profile.profile_identity_sha256
    # The same canonical identity regardless of how the artifact was rendered on disk.
    for saved in (path, compact):
        selection.validate_profile(RuntimeViewService.load(saved))


def test_runtime_handoff_validation_rejects_each_wrong_identity(saved_profile):
    _, profile, selection = saved_profile
    runtime = RuntimeViewService(profile)
    with pytest.raises(ValueError, match="profile identity does not match"):
        selection.model_copy(update={"profile_report_sha256": "0" * 64}).validate_profile(runtime)
    with pytest.raises(ValueError, match="profile identity does not match"):
        selection.model_copy(
            update={"profile_report_sha256": profile.source_report_sha256}
        ).validate_profile(runtime)
    with pytest.raises(ValueError, match="workload identity does not match"):
        selection.model_copy(update={"profile_identity_sha256": "0" * 64}).validate_profile(runtime)
    other = profile.model_copy(update={"repetitions": profile.repetitions + 1})
    with pytest.raises(ValueError, match="profile identity does not match"):
        selection.validate_profile(RuntimeViewService(other))


# --- owned session ------------------------------------------------------------------


def test_open_runtime_publishes_a_runtime_only_session(saved_profile):
    path, _, selection = saved_profile
    owner = OwnedWebHandoff()
    try:
        url = owner.open_runtime(path, selection)
        status, overview = request(url)
        assert status == 200 and overview["available"]
        assert not overview["source_matches"] and not overview["comparison_available"]
        assert owner.open_runtime(path, selection) == url  # idempotent, no replacement
        assert request(url, "/api/bootstrap")[0] == 409
        assert request(url, "/api/source?path=sample.py")[0] == 409
        assert request(url, "/api/runtime/graph")[0] == 200
        status, context = request(url, "/api/collection")
        assert status == 200
        assert context["runtime_available"]
        assert not context["enabled"] and not context["execution_available"]
        assert not context["report_available"]
        path.unlink()
        assert request(url)[0] == 200  # loaded immutable snapshot, not re-reading the target
        with pytest.raises(FileNotFoundError):
            owner.open_runtime(path, selection)
        assert request(url)[0] == 200  # failed replacement preserves the existing session
    finally:
        owner.close()
    with pytest.raises(OSError):
        request(url)
    with pytest.raises(OSError) as error:
        owner.open_runtime(path, selection)
    assert error.value.errno == errno.ECANCELED


def test_open_runtime_does_not_publish_on_mismatch_or_malformed_profile(saved_profile):
    path, _, selection = saved_profile
    owner = OwnedWebHandoff()
    try:
        with pytest.raises(ValueError, match="profile identity does not match"):
            owner.open_runtime(
                path, selection.model_copy(update={"profile_report_sha256": "0" * 64})
            )
        assert owner._server is None
        with pytest.raises(ValueError, match="workload identity does not match"):
            owner.open_runtime(
                path, selection.model_copy(update={"profile_identity_sha256": "0" * 64})
            )
        assert owner._server is None
        url = owner.open_runtime(path, selection)
        path.write_text("{}", encoding="utf-8")
        with pytest.raises(ValidationError):
            owner.open_runtime(path, selection)
        assert request(url)[0] == 200
    finally:
        owner.close()


def test_static_and_runtime_sessions_replace_each_other(saved_profile, tmp_path, ordinary_report):
    from code_analysis.reports.json_report import canonical_json

    path, _, selection = saved_profile
    report = tmp_path / "report.json"
    report.write_text(canonical_json(ordinary_report), encoding="utf-8")
    static = WebHandoffSelection(
        report_sha256=ReportViewService(ordinary_report).bootstrap().reports[0].report_sha256
    )
    owner = OwnedWebHandoff()
    served = []
    try:
        first = owner.open(report, static)
        assert request(first, "/api/bootstrap")[0] == 200
        served.append(owner._thread)
        second = owner.open_runtime(path, selection)
        assert request(second)[1]["available"]
        assert request(second, "/api/bootstrap")[0] == 409
        with pytest.raises(OSError):
            request(first, "/api/bootstrap")
        served.append(owner._thread)
        third = owner.open(report, static)
        assert request(third, "/api/bootstrap")[0] == 200
        with pytest.raises(OSError):
            request(second)
        served.append(owner._thread)
    finally:
        owner.close()
    assert owner._server is None and owner._thread is None
    # Three distinct serving threads, every one of them joined rather than leaked.
    assert len(set(served)) == 3
    assert not any(thread.is_alive() for thread in served)


def test_open_runtime_rejects_nonregular_symlink_and_oversized_inputs(
    saved_profile, tmp_path, monkeypatch
):
    path, _, selection = saved_profile
    link = tmp_path / "link.json"
    link.symlink_to(path)
    fifo = tmp_path / "pipe.json"
    os.mkfifo(fifo)
    owner = OwnedWebHandoff()
    try:
        for invalid, expected in (
            (link, {errno.ELOOP}),
            (fifo, {errno.EINVAL}),
            (tmp_path, {errno.EINVAL, errno.EISDIR}),
            (tmp_path / "absent.json", {errno.ENOENT}),
        ):
            with pytest.raises(OSError) as error:
                owner.open_runtime(invalid, selection)
            assert error.value.errno in expected
        monkeypatch.setattr("code_analysis.ui_handoff._MAXIMUM_ARTIFACT_BYTES", 16)
        with pytest.raises(OSError) as error:
            owner.open_runtime(path, selection)
        assert error.value.errno == errno.EINVAL
        assert owner._server is None
    finally:
        owner.close()


def test_close_during_runtime_load_cancels_without_leaking_a_server(saved_profile, monkeypatch):
    import code_analysis.ui_handoff as module

    path, _, selection = saved_profile
    entered, release = threading.Event(), threading.Event()
    original = module._saved_profile

    def delayed(target):
        entered.set()
        assert release.wait(5)
        return original(target)

    monkeypatch.setattr(module, "_saved_profile", delayed)
    owner = OwnedWebHandoff()
    errors: list[int | None] = []

    def opening():
        try:
            owner.open_runtime(path, selection)
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
    assert owner._server is None and owner._thread is None


# --- server surface -----------------------------------------------------------------


def test_runtime_handoff_endpoint_is_authenticated_strict_and_profile_bound(runtime_server):
    server, profile = runtime_server
    selection = RuntimeHandoffSelection(
        profile_report_sha256=canonical_profile_sha256(profile),
        profile_identity_sha256=profile.profile_identity_sha256,
        graph_request=RuntimeGraphRequest(max_nodes=3, max_edges=2, project_only=True),
    )
    fragment = urlsplit(selection.url("http://localhost/")).fragment
    path = "/api/runtime/handoff?" + fragment
    assert fetch(server, path, token=None)[0] == 403
    assert fetch(server, path, token="wrong")[0] == 403
    status, body, _ = fetch(server, path)
    assert status == 200 and json.loads(body) == selection.model_dump(mode="json")
    for invalid in (
        path + "&unexpected=yes",
        path + "&profile_run=current",
        path + f"&profile_report_sha256={selection.profile_report_sha256}",
        path + "&graph_request=%7B%7D",
        path.replace(selection.profile_report_sha256, "0" * 64),
        path.replace(selection.profile_identity_sha256, "0" * 64),
        "/api/runtime/handoff?"
        + urlencode(
            {
                "profile_report_sha256": selection.profile_report_sha256,
                "profile_identity_sha256": selection.profile_identity_sha256,
            }
        ),
        "/api/runtime/handoff?"
        + urlencode(
            {
                "profile_report_sha256": selection.profile_report_sha256,
                "profile_identity_sha256": selection.profile_identity_sha256,
                "graph_request": json.dumps({"max_nodes": 0, "max_edges": 2, "project_only": True}),
            }
        ),
    ):
        status, body, _ = fetch(server, invalid)
        assert status == 400, invalid
        assert json.loads(body)["error"]["code"] == "invalid-request"


def test_runtime_handoff_endpoint_reports_an_absent_runtime(ordinary_report):
    server = create_server(ReportViewService(ordinary_report), token="runtime-handoff-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body, _ = fetch(server, "/api/runtime/handoff?profile_report_sha256=" + "0" * 64)
        assert status == 400
        assert "no saved runtime profile" in json.loads(body)["error"]["message"]
        assert fetch(server, "/api/bootstrap")[0] == 200
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()


def test_runtime_only_startup_rejects_every_untrusted_combination(
    saved_profile, tmp_path, ordinary_report
):
    path, profile, _ = saved_profile
    runtime = RuntimeViewService(profile)
    for options in (
        {"service": ReportViewService(ordinary_report)},
        {"profile_report": path},
        {"compare_profile": path},
        {"workspace": tmp_path},
        {"source_root": tmp_path},
        {"collection_root": tmp_path},
        {"allow_collection": True},
        {"collection_pythons": (Path("/usr/bin/python3"),)},
    ):
        service_argument = options.pop("service", None)
        with pytest.raises(ValueError):
            create_server(service_argument, runtime_service=runtime, **options)
    with pytest.raises(ValueError):
        create_server(None)  # no report, no runtime, no collection root
    collection_only = create_server(None, collection_root=tmp_path)
    try:
        assert collection_only.app.runtime is None and collection_only.app.service is None
    finally:
        collection_only.server_close()


def test_runtime_only_server_denies_source_and_collection_execution(runtime_server):
    server, _ = runtime_server
    assert server.app.service is None and server.app.source_root is None
    assert server.app.collection is None and server.app.collection_session is None
    assert server.app.runs is None
    for path in ("/api/bootstrap", "/api/source?path=sample.py", "/api/modules", "/api/findings"):
        assert fetch(server, path)[0] == 409, path
    assert fetch(server, "/api/collection/jobs")[0] == 403
    assert fetch(server, "/api/collection/files")[0] == 403
    status, body, _ = fetch(server, "/api/collection")
    assert status == 200
    context = json.loads(body)
    assert context["runtime_available"] and not context["execution_available"]


# --- projection parity --------------------------------------------------------------


@pytest.mark.parametrize("project_only", [False, True])
def test_indexed_and_http_runtime_graphs_are_the_same_projection(
    runtime_server, tmp_path, project_only
):
    server, profile = runtime_server
    index_root = tmp_path / f"runtime-{project_only}"
    build_runtime_index(RuntimeViewService(profile), index_root)
    index = RuntimeIndex.open(index_root, profile_sha256=canonical_profile_sha256(profile))
    graph_request = RuntimeGraphRequest(max_nodes=3, max_edges=2, project_only=project_only)
    expected = index.runtime_graph(graph_request)
    assert expected == build_runtime_graph(profile, RuntimeSelection(**graph_request.model_dump()))
    query = urlencode(
        {
            "max_nodes": graph_request.max_nodes,
            "max_edges": graph_request.max_edges,
            "project_only": "true" if project_only else "false",
        }
    )
    status, body, _ = fetch(server, "/api/runtime/graph?" + query)
    assert status == 200
    assert json.loads(body) == expected.model_dump(mode="json")
    assert expected.inputs[0].report_sha256 == index.manifest.runtime.profile_report_sha256
    assert expected.inputs[0].run_identity_sha256 == profile.profile_identity_sha256
    # Truncation, null edge timings and the project-filter limitation all survive.
    assert expected.truncated and expected.completeness.value == "partial"
    assert any("edge timing unavailable" in edge.label for edge in expected.edges)
    assert project_only == any("project-classified frames" in item for item in expected.limitations)
    assert project_only != any(node.kind != "project" for node in expected.nodes)
    status, payload, headers = fetch(server, "/api/runtime/download?format=json&" + query)
    assert status == 200
    assert payload == render_projection(expected, ProjectionFormat.JSON).encode()
    assert headers["X-Analysis-Completeness"] == expected.completeness.value
    assert headers["X-Projection-Truncated"] == str(expected.truncated).lower()


def test_project_only_changes_the_projection_identity_on_both_paths(runtime_server, tmp_path):
    server, profile = runtime_server
    build_runtime_index(RuntimeViewService(profile), tmp_path / "runtime")
    index = RuntimeIndex.open(
        tmp_path / "runtime", profile_sha256=canonical_profile_sha256(profile)
    )
    identities = set()
    for project_only in (False, True):
        graph_request = RuntimeGraphRequest(max_nodes=3, max_edges=2, project_only=project_only)
        indexed = index.runtime_graph(graph_request)
        query = urlencode(
            {
                "max_nodes": 3,
                "max_edges": 2,
                "project_only": "true" if project_only else "false",
            }
        )
        served = json.loads(fetch(server, "/api/runtime/graph?" + query)[1])
        assert served["projection_id"] == indexed.projection_id
        identities.add(indexed.projection_id)
    assert len(identities) == 2


@pytest.mark.parametrize(
    "query",
    [
        "project_only=1",
        "project_only=True",
        "project_only=yes",
        "project_only=",
        "project_only=true&project_only=false",
        "max_nodes=0",
        "max_nodes=101",
        "max_edges=251",
        "max_nodes=6.0",
    ],
)
def test_runtime_graph_rejects_noncanonical_bounds_and_booleans(runtime_server, query):
    server, _ = runtime_server
    assert fetch(server, "/api/runtime/graph?" + query)[0] == 400
    assert fetch(server, "/api/runtime/download?format=json&" + query)[0] == 400


def test_runtime_graph_defaults_are_unchanged(runtime_server):
    server, profile = runtime_server
    expected = build_runtime_graph(profile, RuntimeSelection(**RuntimeGraphRequest().model_dump()))
    status, body, _ = fetch(server, "/api/runtime/graph")
    assert status == 200 and json.loads(body) == expected.model_dump(mode="json")
