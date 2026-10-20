"""Focused tests for saved runtime code-browser source mapping and call graphs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.code_browse import CodeBrowseBindingError, CodeBrowseReader
from code_analysis.code_browse_graph import (
    CodeGraphLayer,
    CodeGraphRequest,
    CodeGraphResult,
    build_code_graph,
)
from code_analysis.code_browse_runtime import (
    CodeRuntimeSourceRequest,
    CodeRuntimeSourceResult,
    build_code_runtime_graph,
    read_code_runtime_source,
)
from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness
from code_analysis.profile_models import (
    CallerRepresentation,
    FrameClassification,
    ProfileFrame,
    ProfileReport,
)
from code_analysis.profiling import render_profile_json
from code_analysis.project import discover_context
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json
from code_analysis.runtime_models import RuntimeSourceHash, SourceBindingState
from code_analysis.service import scan
from code_analysis.ui_runtime import RuntimeViewService

GOLDEN = Path(__file__).parent / "golden" / "phase4-profile-report.json"


def _digest(report: AnalysisReport) -> str:
    return hashlib.sha256(canonical_json(report).encode()).hexdigest()


def _reader(report: AnalysisReport) -> CodeBrowseReader:
    return CodeBrowseReader.from_report(report, _digest(report))


def _profile_digest(profile: ProfileReport) -> str:
    return hashlib.sha256(render_profile_json(profile, compact=True).encode()).hexdigest()


def _source_hash(report: AnalysisReport, path: str = "alpha/a.py") -> RuntimeSourceHash:
    source = next(item for item in report.source_files if item.logical_path == path)
    return RuntimeSourceHash(path=source.logical_path, sha256=source.source_sha256)


def _project_frame(
    template: ProfileReport,
    *,
    frame_id: str,
    path: str,
    line: int,
    function: str,
    classification: FrameClassification = FrameClassification.PROJECT,
    subject_id: str | None = None,
) -> ProfileFrame:
    return template.frames[0].model_copy(
        update={
            "frame_id": frame_id,
            "path": path,
            "line": line,
            "function": function,
            "classification": classification,
            "subject_id": subject_id,
        }
    )


def _build_profile(
    report: AnalysisReport,
    *,
    frames,
    runtime_edges=(),
    source_hashes: tuple[RuntimeSourceHash, ...] | None = None,
    source_binding: SourceBindingState = SourceBindingState.MATCHED,
    source_report_sha256: str | None = None,
    profile_identity_sha256: str | None = None,
) -> ProfileReport:
    original = RuntimeViewService.load(GOLDEN).profile
    static_sha = source_report_sha256 if source_report_sha256 is not None else _digest(report)
    hashes = source_hashes if source_hashes is not None else (_source_hash(report),)
    ordered_frames = tuple(sorted(frames, key=lambda item: item.frame_id))
    ordered_edges = tuple(sorted(runtime_edges, key=lambda item: item.runtime_edge_id))
    profile = ProfileReport.model_validate(
        {
            **original.model_dump(),
            "frames": ordered_frames,
            "runtime_edges": ordered_edges,
            "statistics": (),
            "source_report_sha256": static_sha,
            "profile_identity_sha256": profile_identity_sha256 or original.profile_identity_sha256,
            "runtime": original.runtime.model_copy(
                update={
                    "source_binding": source_binding,
                    "source_hashes": hashes,
                }
            ),
        }
    )
    return profile


def _source_request(
    report: AnalysisReport,
    profile: ProfileReport,
    frame_id: str,
    *,
    revision: int = 0,
) -> CodeRuntimeSourceRequest:
    return CodeRuntimeSourceRequest.model_validate(
        {
            "report_sha256": _digest(report),
            "profile_report_sha256": _profile_digest(profile),
            "profile_identity_sha256": profile.profile_identity_sha256,
            "revision": revision,
            "frame_id": frame_id,
        }
    )


def _graph_request(
    report: AnalysisReport,
    profile: ProfileReport,
    *,
    path: str = "",
    identity: str = "",
    depth: int = 1,
    max_nodes: int = 100,
    max_edges: int = 250,
) -> CodeGraphRequest:
    return CodeGraphRequest.model_validate(
        {
            "report_sha256": _digest(report),
            "revision": 0,
            "layer": CodeGraphLayer.RUNTIME_CALLS,
            "path": path,
            "identity": identity,
            "depth": depth,
            "max_nodes": max_nodes,
            "max_edges": max_edges,
            "profile_report_sha256": _profile_digest(profile),
            "profile_identity_sha256": profile.profile_identity_sha256,
        }
    )


@pytest.fixture
def ordinary_report() -> AnalysisReport:
    context = discover_context(Path(__file__).parent / "fixtures" / "ordinary", AnalysisConfig())
    return AnalysisReport.model_validate_json(scan(context).model_dump_json())


def test_runtime_source_request_rejects_extras_and_bools(ordinary_report) -> None:
    digest = _digest(ordinary_report)
    with pytest.raises(ValidationError):
        CodeRuntimeSourceRequest.model_validate(
            {
                "report_sha256": digest,
                "profile_report_sha256": "1" * 64,
                "profile_identity_sha256": "2" * 64,
                "revision": 0,
                "frame_id": "profile-frame-aaaaaaaaaaaaaaaaaaaa",
                "extra": "x",
            }
        )
    with pytest.raises(ValidationError):
        CodeRuntimeSourceRequest.model_validate(
            {
                "report_sha256": digest,
                "profile_report_sha256": "1" * 64,
                "profile_identity_sha256": "2" * 64,
                "revision": True,
                "frame_id": "profile-frame-aaaaaaaaaaaaaaaaaaaa",
            }
        )


def test_runtime_source_available_binding(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-aaaaaaaaaaaaaaaaaaaa",
        path="alpha/a.py",
        line=20,
        function="calculate",
        subject_id="alpha.a:calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _source_request(ordinary_report, profile, frame.frame_id)
    result = read_code_runtime_source(ordinary_report, _reader(ordinary_report), request, profile)
    assert result.state == "available"
    assert result.selector is not None
    assert result.selector.path == "alpha/a.py"
    assert result.selector.line == 20
    assert result.selector.identity == "alpha.a:calculate"


def test_runtime_source_without_subject_uses_empty_identity(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-bbbbbbbbbbbbbbbbbbbb",
        path="alpha/a.py",
        line=21,
        function="calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _source_request(ordinary_report, profile, frame.frame_id)
    result = read_code_runtime_source(ordinary_report, _reader(ordinary_report), request, profile)
    assert result.state == "available"
    assert result.selector is not None
    assert result.selector.identity == ""


def test_runtime_source_no_profile_is_unavailable(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-cccccccccccccccccccc",
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _source_request(ordinary_report, profile, frame.frame_id)
    result = read_code_runtime_source(ordinary_report, _reader(ordinary_report), request, None)
    assert result.state == "unavailable"
    assert result.selector is None


def test_runtime_source_stale_static_hash_raises(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-dddddddddddddddddddd",
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _source_request(ordinary_report, profile, frame.frame_id)
    stale = request.model_copy(update={"report_sha256": "0" * 64})
    with pytest.raises(CodeBrowseBindingError):
        read_code_runtime_source(ordinary_report, _reader(ordinary_report), stale, profile)


def test_runtime_source_stale_profile_hash_raises(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-eeeeeeeeeeeeeeeeeeee",
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _source_request(ordinary_report, profile, frame.frame_id)
    stale = request.model_copy(update={"profile_report_sha256": "0" * 64})
    with pytest.raises(CodeBrowseBindingError):
        read_code_runtime_source(ordinary_report, _reader(ordinary_report), stale, profile)


def test_runtime_source_stale_identity_raises(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-ffffffffffffffffffff",
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _source_request(ordinary_report, profile, frame.frame_id)
    stale = request.model_copy(update={"profile_identity_sha256": "0" * 64})
    with pytest.raises(CodeBrowseBindingError):
        read_code_runtime_source(ordinary_report, _reader(ordinary_report), stale, profile)


def test_runtime_source_unknown_frame_raises(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-11111111111111111111",
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _source_request(ordinary_report, profile, "profile-frame-99999999999999999999")
    with pytest.raises(ValueError, match="absent"):
        read_code_runtime_source(ordinary_report, _reader(ordinary_report), request, profile)


def test_runtime_source_hash_mismatch_unavailable(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-22222222222222222222",
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    wrong_hash = RuntimeSourceHash(path="alpha/a.py", sha256="0" * 64)
    profile = _build_profile(
        ordinary_report,
        frames=(frame,),
        source_hashes=(wrong_hash,),
    )
    request = _source_request(ordinary_report, profile, frame.frame_id)
    result = read_code_runtime_source(ordinary_report, _reader(ordinary_report), request, profile)
    assert result.state == "unavailable"


def test_runtime_source_missing_hash_unavailable(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-33333333333333333333",
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    other = _source_hash(ordinary_report, path="alpha/b.py")
    profile = _build_profile(ordinary_report, frames=(frame,), source_hashes=(other,))
    request = _source_request(ordinary_report, profile, frame.frame_id)
    result = read_code_runtime_source(ordinary_report, _reader(ordinary_report), request, profile)
    assert result.state == "unavailable"


def test_runtime_source_missing_logical_path_unavailable(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-43434343434343434343",
        path="missing/module.py",
        line=1,
        function="orphan",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _source_request(ordinary_report, profile, frame.frame_id)
    result = read_code_runtime_source(ordinary_report, _reader(ordinary_report), request, profile)
    assert result.state == "unavailable"


def test_runtime_source_nonproject_unavailable(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-44444444444444444444",
        path="alpha/a.py",
        line=20,
        function="native",
        classification=FrameClassification.NATIVE,
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _source_request(ordinary_report, profile, frame.frame_id)
    result = read_code_runtime_source(ordinary_report, _reader(ordinary_report), request, profile)
    assert result.state == "unavailable"


def test_runtime_source_line_out_of_range_unavailable(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    source = next(
        item for item in ordinary_report.source_files if item.logical_path == "alpha/a.py"
    )
    frame = _project_frame(
        original,
        frame_id="profile-frame-55555555555555555555",
        path="alpha/a.py",
        line=source.physical_loc + 5,
        function="calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _source_request(ordinary_report, profile, frame.frame_id)
    result = read_code_runtime_source(ordinary_report, _reader(ordinary_report), request, profile)
    assert result.state == "unavailable"


def test_runtime_source_wrong_explicit_subject_unavailable(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-66666666666666666666",
        path="alpha/a.py",
        line=20,
        function="calculate",
        subject_id="alpha.a:method",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _source_request(ordinary_report, profile, frame.frame_id)
    result = read_code_runtime_source(ordinary_report, _reader(ordinary_report), request, profile)
    assert result.state == "unavailable"


def test_runtime_source_unavailable_result_rejects_selector() -> None:
    with pytest.raises(ValidationError):
        CodeRuntimeSourceResult.model_validate(
            {
                "report_sha256": "1" * 64,
                "profile_report_sha256": "2" * 64,
                "profile_identity_sha256": "3" * 64,
                "revision": 0,
                "frame_id": "profile-frame-aaaaaaaaaaaaaaaaaaaa",
                "state": "unavailable",
                "completeness": Completeness.PARTIAL,
                "message": "x",
                "selector": {
                    "report_sha256": "1" * 64,
                    "revision": 0,
                    "path": "alpha/a.py",
                    "line": 1,
                },
            }
        )


def test_runtime_graph_depth_and_neighbors(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    caller = _project_frame(
        original,
        frame_id="profile-frame-aaaaaaaaaaaaaaaaaaaa",
        path="alpha/a.py",
        line=20,
        function="caller",
    )
    callee = _project_frame(
        original,
        frame_id="profile-frame-bbbbbbbbbbbbbbbbbbbb",
        path="alpha/a.py",
        line=11,
        function="callee",
    )
    native = _project_frame(
        original,
        frame_id="profile-frame-cccccccccccccccccccc",
        path="<native>",
        line=0,
        function="native",
        classification=FrameClassification.NATIVE,
    )
    edge_project = original.runtime_edges[0].model_copy(
        update={
            "runtime_edge_id": "runtime-edge-aaaaaaaaaaaaaaaaaaaa",
            "caller_frame_id": caller.frame_id,
            "callee_frame_id": callee.frame_id,
        }
    )
    edge_native = original.runtime_edges[0].model_copy(
        update={
            "runtime_edge_id": "runtime-edge-bbbbbbbbbbbbbbbbbbbb",
            "caller_frame_id": callee.frame_id,
            "callee_frame_id": native.frame_id,
        }
    )
    profile = _build_profile(
        ordinary_report,
        frames=(caller, callee, native),
        runtime_edges=(edge_project, edge_native),
    )
    reader = _reader(ordinary_report)
    shallow = build_code_runtime_graph(
        ordinary_report,
        reader,
        _graph_request(ordinary_report, profile, identity="alpha.a:calculate", depth=0),
        profile,
    )
    assert shallow.state == "available"
    assert shallow.graph is not None
    assert len(shallow.graph.nodes) == 1
    one_hop = build_code_runtime_graph(
        ordinary_report,
        reader,
        _graph_request(ordinary_report, profile, identity="alpha.a:calculate", depth=1),
        profile,
    )
    assert {node.node_id for node in one_hop.graph.nodes} == {caller.frame_id, callee.frame_id}
    assert {edge.edge_id for edge in one_hop.graph.edges} == {edge_project.runtime_edge_id}
    deep = build_code_runtime_graph(
        ordinary_report,
        reader,
        _graph_request(ordinary_report, profile, identity="alpha.a:calculate", depth=2),
        profile,
    )
    assert deep.graph is not None
    assert len(deep.graph.nodes) == 3
    assert "profile-frame-cccccccccccccccccccc" not in {link.node_id for link in deep.links}


def test_runtime_graph_zero_edge_budget(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    caller = _project_frame(
        original,
        frame_id="profile-frame-dddddddddddddddddddd",
        path="alpha/a.py",
        line=20,
        function="caller",
    )
    callee = _project_frame(
        original,
        frame_id="profile-frame-eeeeeeeeeeeeeeeeeeee",
        path="alpha/a.py",
        line=11,
        function="callee",
    )
    edge = original.runtime_edges[0].model_copy(
        update={
            "caller_frame_id": caller.frame_id,
            "callee_frame_id": callee.frame_id,
        }
    )
    profile = _build_profile(
        ordinary_report,
        frames=(caller, callee),
        runtime_edges=(edge,),
    )
    result = build_code_runtime_graph(
        ordinary_report,
        _reader(ordinary_report),
        _graph_request(
            ordinary_report,
            profile,
            identity="alpha.a:calculate",
            depth=1,
            max_edges=0,
        ),
        profile,
    )
    assert result.graph is not None
    assert result.graph.edges == ()


def test_runtime_graph_edge_labels_and_null_timing(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    caller = _project_frame(
        original,
        frame_id="profile-frame-ffffffffffffffffffff",
        path="alpha/a.py",
        line=20,
        function="caller",
    )
    callee = _project_frame(
        original,
        frame_id="profile-frame-10101010101010101010",
        path="alpha/a.py",
        line=11,
        function="callee",
    )
    edge = original.runtime_edges[0].model_copy(
        update={
            "runtime_edge_id": "runtime-edge-cccccccccccccccccccc",
            "caller_frame_id": caller.frame_id,
            "callee_frame_id": callee.frame_id,
            "caller_representation": CallerRepresentation.COUNT,
            "primitive_calls": None,
            "total_seconds": None,
            "cumulative_seconds": None,
            "total_calls": 7,
        }
    )
    profile = _build_profile(
        ordinary_report,
        frames=(caller, callee),
        runtime_edges=(edge,),
    )
    result = build_code_runtime_graph(
        ordinary_report,
        _reader(ordinary_report),
        _graph_request(ordinary_report, profile, identity="alpha.a:calculate", depth=1),
        profile,
    )
    assert result.graph is not None
    assert result.graph.edges[0].label == "7 calls; edge timing unavailable"


def test_runtime_graph_no_false_parents(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    lone = _project_frame(
        original,
        frame_id="profile-frame-12121212121212121212",
        path="alpha/a.py",
        line=20,
        function="lone",
    )
    profile = _build_profile(ordinary_report, frames=(lone,), runtime_edges=())
    result = build_code_runtime_graph(
        ordinary_report,
        _reader(ordinary_report),
        _graph_request(ordinary_report, profile, identity="alpha.a:calculate", depth=2),
        profile,
    )
    assert result.graph is not None
    assert result.graph.edges == ()


def test_runtime_graph_unavailable_without_profile(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-13131313131313131313",
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    result = build_code_runtime_graph(
        ordinary_report,
        _reader(ordinary_report),
        _graph_request(ordinary_report, profile, identity="alpha.a:calculate"),
        None,
    )
    assert result.state == "unavailable"
    assert result.graph is None


def test_runtime_graph_mixed_focus_partial(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    bound = _project_frame(
        original,
        frame_id="profile-frame-14141414141414141414",
        path="alpha/a.py",
        line=20,
        function="bound",
    )
    unbound = _project_frame(
        original,
        frame_id="profile-frame-15151515151515151515",
        path="alpha/a.py",
        line=21,
        function="unbound",
        classification=FrameClassification.NATIVE,
    )
    edge = original.runtime_edges[0].model_copy(
        update={
            "caller_frame_id": bound.frame_id,
            "callee_frame_id": unbound.frame_id,
        }
    )
    profile = _build_profile(
        ordinary_report,
        frames=(bound, unbound),
        runtime_edges=(edge,),
    )
    result = build_code_runtime_graph(
        ordinary_report,
        _reader(ordinary_report),
        _graph_request(ordinary_report, profile, identity="alpha.a:calculate", depth=1),
        profile,
    )
    assert result.state == "available"
    assert result.completeness is Completeness.PARTIAL
    assert any("retained runtime frames" in item for item in result.limitations)


def test_runtime_graph_canonical_json_stable(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-16161616161616161616",
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _graph_request(ordinary_report, profile, identity="alpha.a:calculate")
    reader = _reader(ordinary_report)
    first = build_code_runtime_graph(ordinary_report, reader, request, profile)
    second = build_code_runtime_graph(ordinary_report, reader, request, profile)
    assert canonical_json(first) == canonical_json(second)


def test_runtime_graph_delegates_from_build_code_graph(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-17171717171717171717",
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _graph_request(ordinary_report, profile, identity="alpha.a:calculate")
    result = build_code_graph(ordinary_report, _reader(ordinary_report), request, profile)
    assert result.layer is CodeGraphLayer.RUNTIME_CALLS
    assert result.profile_report_sha256 == _profile_digest(profile)


def test_unbound_focus_never_creates_available_graph(ordinary_report):
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-" + "a" * 20,
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    profile = _build_profile(
        ordinary_report,
        frames=(frame,),
        source_hashes=(RuntimeSourceHash(path="alpha/a.py", sha256="0" * 64),),
    )
    result = build_code_runtime_graph(
        ordinary_report,
        _reader(ordinary_report),
        _graph_request(ordinary_report, profile, path="alpha/a.py"),
        profile,
    )
    assert result.state == "unavailable" and result.graph is None
    assert "No source-bound" in result.message


def test_report_identity_is_not_rehashed_for_every_frame(ordinary_report, monkeypatch):
    from code_analysis import code_browse_runtime as module

    original = RuntimeViewService.load(GOLDEN).profile
    frames = tuple(
        _project_frame(
            original,
            frame_id=f"profile-frame-{index:020x}",
            path="alpha/a.py",
            line=20,
            function="recorded",
        )
        for index in range(20)
    )
    profile = _build_profile(ordinary_report, frames=frames)
    request = _graph_request(ordinary_report, profile, path="alpha/a.py")
    reader = _reader(ordinary_report)
    real = module._static_report_digest
    calls = []

    def digest(report):
        calls.append(report)
        return real(report)

    monkeypatch.setattr(module, "_static_report_digest", digest)
    result = build_code_runtime_graph(ordinary_report, reader, request, profile)
    assert len(result.links) == 20 and len(calls) == 1


def test_failed_evidence_remains_failed_and_zero_edge_identity_is_exact(ordinary_report):
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-" + "a" * 20,
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,)).model_copy(
        update={"completeness": Completeness.FAILED}
    )
    request = _graph_request(ordinary_report, profile, path="alpha/a.py", max_edges=0)
    reader = _reader(ordinary_report)
    graph = build_code_runtime_graph(ordinary_report, reader, request, profile)
    source = read_code_runtime_source(
        ordinary_report, reader, _source_request(ordinary_report, profile, frame.frame_id), profile
    )
    assert graph.completeness is Completeness.FAILED
    assert graph.graph.selection.max_edges == 0
    assert source.completeness is Completeness.FAILED


def test_runtime_source_deterministic_json(ordinary_report) -> None:
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-18181818181818181818",
        path="alpha/a.py",
        line=20,
        function="calculate",
        subject_id="alpha.a:calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    request = _source_request(ordinary_report, profile, frame.frame_id)
    reader = _reader(ordinary_report)
    first = read_code_runtime_source(ordinary_report, reader, request, profile)
    second = read_code_runtime_source(ordinary_report, reader, request, profile)
    assert json.dumps(json.loads(first.model_dump_json()), sort_keys=True) == json.dumps(
        json.loads(second.model_dump_json()),
        sort_keys=True,
    )


@pytest.mark.parametrize("unavailable", ("report-mismatch", "source-unbound", "hash-missing"))
def test_failed_unavailable_profile_graph_stays_failed(ordinary_report, unavailable):
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-" + "a" * 20,
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    kwargs = {}
    if unavailable == "report-mismatch":
        kwargs["source_report_sha256"] = "0" * 64
    elif unavailable == "source-unbound":
        kwargs["source_binding"] = SourceBindingState.UNBOUND
    else:
        kwargs["source_hashes"] = (RuntimeSourceHash(path="unrelated.py", sha256="0" * 64),)
    profile = _build_profile(ordinary_report, frames=(frame,), **kwargs).model_copy(
        update={"completeness": Completeness.FAILED}
    )
    result = build_code_runtime_graph(
        ordinary_report,
        _reader(ordinary_report),
        _graph_request(ordinary_report, profile, path="alpha/a.py"),
        profile,
    )
    assert result.state == "unavailable" and result.graph is None
    assert result.completeness is Completeness.FAILED
    assert CodeGraphResult.model_validate_json(result.model_dump_json()) == result


def test_runtime_browser_records_strict_json_round_trip(ordinary_report):
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-" + "a" * 20,
        path="alpha/a.py",
        line=20,
        function="calculate",
    )
    profile = _build_profile(ordinary_report, frames=(frame,))
    reader = _reader(ordinary_report)
    result = build_code_runtime_graph(
        ordinary_report,
        reader,
        _graph_request(ordinary_report, profile, path="alpha/a.py"),
        profile,
    )
    source = read_code_runtime_source(
        ordinary_report, reader, _source_request(ordinary_report, profile, frame.frame_id), profile
    )
    assert CodeGraphResult.model_validate_json(result.model_dump_json()) == result
    assert CodeRuntimeSourceResult.model_validate_json(source.model_dump_json()) == source


def test_python_profile_cannot_bind_to_javascript_source():
    from code_analysis.models import SourceLanguage

    target = Path(__file__).parent / "fixtures" / "javascript" / "src" / "widget.jsx"
    report = AnalysisReport.model_validate_json(
        scan(discover_context(target, AnalysisConfig())).model_dump_json()
    )
    source = report.source_files[0]
    assert source.language is SourceLanguage.JAVASCRIPT
    original = RuntimeViewService.load(GOLDEN).profile
    frame = _project_frame(
        original,
        frame_id="profile-frame-" + "a" * 20,
        path=source.logical_path,
        line=1,
        function="not-a-python-frame",
    )
    profile = _build_profile(
        report,
        frames=(frame,),
        source_hashes=(RuntimeSourceHash(path=source.logical_path, sha256=source.source_sha256),),
    )
    reader = _reader(report)
    mapping = read_code_runtime_source(
        report, reader, _source_request(report, profile, frame.frame_id), profile
    )
    graph = build_code_runtime_graph(
        report, reader, _graph_request(report, profile, path=source.logical_path), profile
    )
    assert mapping.state == graph.state == "unavailable"
    assert mapping.selector is None and graph.graph is None
