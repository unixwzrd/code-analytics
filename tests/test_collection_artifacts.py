from __future__ import annotations

import errno
import json
import os
import threading
from dataclasses import replace

import pytest
from pydantic import ValidationError

from code_analysis.collection_artifacts import CollectionArtifactBinding, bind_collection_generation
from code_analysis.collection_commands import CollectionPlanner, CollectionRequest
from code_analysis.collection_execution import execute_collection
from code_analysis.workspace import (
    GenerationKind,
    GenerationManifest,
    list_generations,
    open_workspace,
    read_generation,
)


@pytest.fixture
def captured(tmp_path):
    (tmp_path / "sample.py").write_text("def answer():\n    return 42\n")
    planner = CollectionPlanner(tmp_path)
    request = CollectionRequest(target="sample.py")
    result = execute_collection(
        planner,
        request,
        reviewed_command=planner.preview(request),
        cancel=threading.Event(),
    )
    workspace = open_workspace(tmp_path / ".code-analysis")
    generation = list_generations(workspace, kind=GenerationKind.ANALYSIS)[0]
    yield planner, result, workspace, generation
    planner.close()


def test_exact_generation_binding_does_not_follow_latest(captured):
    planner, result, workspace, generation = captured
    request = CollectionRequest(target="sample.py")
    later = execute_collection(
        planner,
        request,
        reviewed_command=planner.preview(request),
        cancel=threading.Event(),
    )
    assert later.receipt.process.pid != result.receipt.process.pid
    binding = bind_collection_generation(result, workspace.root, generation.generation_id)
    assert binding.report_sha256 == result.receipt.stdout_sha256
    assert binding.artifact_name == "report.json"
    with pytest.raises(OSError) as caught:
        bind_collection_generation(result, workspace.root, "latest")
    assert caught.value.errno == errno.EINVAL


@pytest.mark.parametrize(
    "field,value",
    [
        ("process_id", 1),
        ("process_start_time", 1.0),
        ("process_start_time", None),
        ("created_at", "2000-01-01T00:00:00+00:00"),
        ("created_at", "2999-01-01T00:00:00+00:00"),
        ("interpreter_path", "/wrong/python"),
        ("project_root", "/wrong/project"),
        ("workspace_path", "/wrong/workspace"),
        ("portable_identity_sha256", "f" * 64),
        ("command", ["code-analysis", "refactor"]),
    ],
)
def test_unrelated_or_changed_generation_identity_fails(captured, field, value):
    _, result, workspace, generation = captured
    path = workspace.kind_directory(generation.kind) / generation.generation_id / "manifest.json"
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(OSError) as caught:
        bind_collection_generation(result, workspace.root, generation.generation_id)
    assert caught.value.errno == errno.EBADMSG


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversized"])
def test_manifest_unsafe_reads_fail_without_following_or_blocking(captured, kind):
    _, _, workspace, generation = captured
    path = workspace.kind_directory(generation.kind) / generation.generation_id / "manifest.json"
    if kind == "oversized":
        path.write_bytes(b" " * (1024 * 1024 + 1))
    else:
        saved = path.with_name("original.json")
        path.rename(saved)
        if kind == "symlink":
            path.symlink_to(saved)
        else:
            os.mkfifo(path)
    with pytest.raises(OSError) as caught:
        read_generation(workspace, generation.kind, generation.generation_id)
    assert caught.value.errno == errno.EBADMSG


def test_artifact_tampering_and_stream_receipt_mismatch_fail(captured):
    _, result, workspace, generation = captured
    with pytest.raises(OSError) as caught:
        bind_collection_generation(
            replace(result, stdout=result.stdout + b" "), workspace.root, generation.generation_id
        )
    assert caught.value.errno == errno.EBADMSG
    path = workspace.kind_directory(generation.kind) / generation.generation_id / "report.json"
    path.write_bytes(b"{}")
    with pytest.raises(OSError) as caught:
        bind_collection_generation(result, workspace.root, generation.generation_id)
    assert caught.value.errno == errno.EBADMSG


def test_profile_pretty_artifact_binds_to_compact_capture(captured):
    planner, _, workspace, generation = captured
    source = workspace.kind_directory(generation.kind) / generation.generation_id / "report.json"
    request = CollectionRequest(
        operation="profile",
        source_report=str(source.relative_to(planner.root)),
        workload="sample.py",
        allow_execution=True,
    )
    result = execute_collection(
        planner,
        request,
        reviewed_command=planner.preview(request),
        cancel=threading.Event(),
    )
    retained = list_generations(workspace, kind=GenerationKind.PROFILE)[0]
    binding = bind_collection_generation(result, workspace.root, retained.generation_id)
    assert binding.kind == GenerationKind.PROFILE
    assert binding.artifact_name == "profile.json"
    assert binding.artifact_sha256 != binding.report_sha256


@pytest.mark.parametrize("kind", ["fifo", "oversized"])
def test_owned_workspace_marker_is_bounded_regular_input(captured, kind):
    _, _, workspace, _ = captured
    if kind == "fifo":
        workspace.marker.unlink()
        os.mkfifo(workspace.marker)
    else:
        workspace.marker.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(OSError) as caught:
        open_workspace(workspace.root)
    assert caught.value.errno == errno.EBADMSG


def test_binding_model_rejects_contradictory_kind_and_relative_workspace(captured):
    _, result, workspace, generation = captured
    binding = bind_collection_generation(result, workspace.root, generation.generation_id)
    for change in (
        {"artifact_name": "profile.json"},
        {"workspace_path": "relative"},
        {"extra": True},
    ):
        with pytest.raises(ValidationError):
            CollectionArtifactBinding.model_validate({**binding.model_dump(), **change})


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_report_artifact_must_be_regular_and_not_followed(captured, kind):
    _, result, workspace, generation = captured
    path = workspace.kind_directory(generation.kind) / generation.generation_id / "report.json"
    payload = path.read_bytes()
    path.unlink()
    if kind == "fifo":
        os.mkfifo(path)
    else:
        outside = workspace.root.parent / "external.json"
        outside.write_bytes(payload)
        path.symlink_to(outside)
    with pytest.raises(OSError):
        bind_collection_generation(result, workspace.root, generation.generation_id)


def test_process_provenance_missing_and_nonfinite_states(captured, monkeypatch):
    import sys

    from code_analysis.workspace import _current_process_start

    _, _, _, generation = captured
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert _current_process_start() is None
    for invalid in (True, float("inf"), float("nan"), -1.0):
        with pytest.raises(ValidationError):
            GenerationManifest.model_validate(
                {**generation.model_dump(), "process_start_time": invalid}
            )
    historical = generation.model_dump(exclude={"process_start_time"})
    assert GenerationManifest.model_validate(historical).process_start_time is None
