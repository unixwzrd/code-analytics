from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.profile_models import FrameClassification, ProfileReport
from code_analysis.profiling import build_profile_diff
from code_analysis.runtime_comparison_browse import RuntimeComparisonFrameBinding
from code_analysis.runtime_index import RuntimeIndex, build_runtime_index
from code_analysis.runtime_models import RuntimeArtifact, RuntimeSourceHash, SourceBindingState
from code_analysis.runtime_source_browse import (
    RuntimeSourceUnavailable,
    read_runtime_comparison_frame_binding,
)
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService

GOLDEN = Path(__file__).parent / "golden" / "phase4-profile-report.json"


def _source_hash(ordinary_report, path: str = "alpha/a.py") -> RuntimeSourceHash:
    source = next(item for item in ordinary_report.source_files if item.logical_path == path)
    return RuntimeSourceHash(path=source.logical_path, sha256=source.source_sha256)


def _project_frame(original, *, path: str, line: int, function: str, frame_id: str, artifact=None):
    return original.frames[0].model_copy(
        update={
            "frame_id": frame_id,
            "path": path,
            "line": line,
            "function": function,
            "classification": FrameClassification.PROJECT,
            "artifact_sha256": artifact,
        }
    )


def build_comparison_index(
    ordinary_report,
    tmp_path,
    *,
    candidate_hash: str | None = None,
    candidate_binding: SourceBindingState = SourceBindingState.MATCHED,
    baseline_frames=None,
    candidate_frames=None,
    multi_artifact: bool = False,
):
    service = ReportViewService(ordinary_report)
    static_sha = service.bootstrap().reports[0].report_sha256
    original = RuntimeViewService.load(GOLDEN).profile
    source = _source_hash(ordinary_report)
    baseline_frame = _project_frame(
        original,
        path=source.path,
        line=1,
        function="baseline_fn",
        frame_id="profile-frame-aaaaaaaaaaaaaaaaaaaa",
    )
    candidate_frame = baseline_frame.model_copy(
        update={"frame_id": "profile-frame-bbbbbbbbbbbbbbbbbbbb"}
    )
    artifact_a = "a" * 64
    artifact_b = "b" * 64
    if multi_artifact:
        baseline_frame = baseline_frame.model_copy(
            update={"artifact_sha256": artifact_a, "function": "shared_fn"}
        )
        candidate_frame = candidate_frame.model_copy(
            update={"artifact_sha256": artifact_a, "function": "shared_fn"}
        )
        artifacts = (
            RuntimeArtifact(sha256=artifact_a, byte_count=1, kind="profile"),
            RuntimeArtifact(sha256=artifact_b, byte_count=1, kind="profile"),
        )
    else:
        artifacts = ()
    baseline_frames = baseline_frames if baseline_frames is not None else (baseline_frame,)
    candidate_frames = candidate_frames if candidate_frames is not None else (candidate_frame,)
    candidate_hash_value = candidate_hash if candidate_hash is not None else source.sha256
    runtime_kwargs = {
        "source_binding": SourceBindingState.MATCHED,
        "source_hashes": (source,),
        "artifacts": artifacts,
    }
    baseline = ProfileReport.model_validate(
        {
            **original.model_dump(),
            "frames": baseline_frames,
            "runtime_edges": (),
            "statistics": (),
            "source_report_sha256": static_sha,
            "runtime": original.runtime.model_copy(update=runtime_kwargs),
        }
    )
    candidate = ProfileReport.model_validate(
        {
            **baseline.model_dump(),
            "profile_identity_sha256": "c" * 64,
            "frames": candidate_frames,
            "runtime": baseline.runtime.model_copy(
                update={
                    "source_binding": candidate_binding,
                    "source_hashes": (
                        RuntimeSourceHash(path=source.path, sha256=candidate_hash_value),
                    ),
                }
            ),
        }
    )
    delta = build_profile_diff(baseline, candidate, diagnostic_only=True)
    runtime_service = RuntimeViewService(baseline, candidate, delta)
    root = tmp_path / "index"
    manifest = build_browse_index(service, root, runtime=runtime_service)
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    return runtime_service, index, delta


def test_same_path_different_captured_hashes(ordinary_report, tmp_path):
    _, index, delta = build_comparison_index(ordinary_report, tmp_path, candidate_hash="0" * 64)
    identity = delta.records[0].frame_identity
    preview = index.runtime_comparison_source(identity, "baseline")
    assert preview.source.logical_path == "alpha/a.py"
    with pytest.raises(ValueError, match="static source identity"):
        index.runtime_comparison_source(identity, "candidate")


def test_candidate_only_and_baseline_only_frames(ordinary_report, tmp_path):
    original = RuntimeViewService.load(GOLDEN).profile
    source = _source_hash(ordinary_report)
    only_baseline = _project_frame(
        original,
        path=source.path,
        line=1,
        function="only_baseline",
        frame_id="profile-frame-cccccccccccccccccccc",
    )
    only_candidate = only_baseline.model_copy(
        update={
            "frame_id": "profile-frame-dddddddddddddddddddd",
            "function": "only_candidate",
        }
    )
    _, index, delta = build_comparison_index(
        ordinary_report,
        tmp_path,
        baseline_frames=(only_baseline,),
        candidate_frames=(only_candidate,),
    )
    baseline_only = next(
        record for record in delta.records if record.baseline_total_seconds is not None
    )
    candidate_only = next(
        record for record in delta.records if record.candidate_total_seconds is not None
    )
    assert index.runtime_comparison_source(baseline_only.frame_identity, "baseline").frame_id
    with pytest.raises(RuntimeSourceUnavailable):
        index.runtime_comparison_source(baseline_only.frame_identity, "candidate")
    assert index.runtime_comparison_source(candidate_only.frame_identity, "candidate").frame_id
    with pytest.raises(RuntimeSourceUnavailable):
        index.runtime_comparison_source(candidate_only.frame_identity, "baseline")


def test_baseline_bound_candidate_unbound(ordinary_report, tmp_path):
    _, index, delta = build_comparison_index(
        ordinary_report,
        tmp_path,
        candidate_binding=SourceBindingState.UNBOUND,
    )
    identity = delta.records[0].frame_identity
    assert index.manifest.runtime.comparison.candidate_source_binding is SourceBindingState.UNBOUND
    index.runtime_comparison_source(identity, "baseline")
    with pytest.raises(RuntimeSourceUnavailable, match="candidate source binding"):
        index.runtime_comparison_source(identity, "candidate")


def test_baseline_matched_does_not_qualify_candidate(ordinary_report, tmp_path):
    service, index, delta = build_comparison_index(
        ordinary_report,
        tmp_path,
        candidate_binding=SourceBindingState.MATCHED,
    )
    candidate = service.comparison
    candidate = ProfileReport.model_validate(
        {
            **candidate.model_dump(),
            "source_report_sha256": "f" * 64,
            "runtime": candidate.runtime.model_copy(
                update={"source_binding": SourceBindingState.MATCHED}
            ),
        }
    )
    delta = build_profile_diff(service.profile, candidate, diagnostic_only=True)
    runtime_service = RuntimeViewService(service.profile, candidate, delta)
    root = tmp_path / "mismatch"
    manifest = build_browse_index(ReportViewService(ordinary_report), root, runtime=runtime_service)
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    identity = delta.records[0].frame_identity
    assert index.manifest.runtime.overview.source_matches
    assert not index.manifest.runtime.comparison.candidate_overview.source_matches
    index.runtime_comparison_source(identity, "baseline")
    with pytest.raises(RuntimeSourceUnavailable, match="candidate source binding"):
        index.runtime_comparison_source(identity, "candidate")


def test_multi_artifact_join_key(ordinary_report, tmp_path):
    service, index, delta = build_comparison_index(ordinary_report, tmp_path, multi_artifact=True)
    identity = delta.records[0].frame_identity
    assert identity.startswith("a" * 64 + ":")
    preview = index.runtime_comparison_source(identity, "baseline")
    assert preview.frame_id == service.profile.frames[0].frame_id
    assert index.runtime_comparison_source(identity, "candidate").frame_id == (
        service.comparison.frames[0].frame_id
    )


def test_missing_and_malformed_comparison_frame_map(ordinary_report, tmp_path):
    _, index, delta = build_comparison_index(ordinary_report, tmp_path)
    identity = delta.records[0].frame_identity
    with closing(sqlite3.connect(index.database)) as connection:
        connection.execute(
            "DELETE FROM runtime_records WHERE section = 'comparison-frames' AND frame_a = ?",
            (identity,),
        )
        connection.commit()
    with (
        pytest.raises(ValueError, match="binding unavailable"),
        closing(sqlite3.connect(index.database)) as connection,
    ):
        read_runtime_comparison_frame_binding(connection, identity)
    with pytest.raises(ValueError):
        with closing(sqlite3.connect(index.database)) as connection:
            connection.execute(
                "INSERT INTO runtime_records VALUES ('comparison-frames', 999, ?, NULL, ?)",
                (
                    identity,
                    RuntimeComparisonFrameBinding(
                        frame_identity=identity,
                        baseline_frame_id="profile-frame-aaaaaaaaaaaaaaaaaaaa",
                        candidate_frame_id="profile-frame-bbbbbbbbbbbbbbbbbbbb",
                    ).model_dump_json(),
                ),
            )
            connection.commit()
        BrowseIndex.open(tmp_path / "index", report_sha256=index.manifest.report_sha256)


def test_source_free_runtime_index_after_comparison_json(tmp_path, ordinary_report):
    browse_root = tmp_path / "browse"
    browse_root.mkdir()
    service, browse_index, delta = build_comparison_index(ordinary_report, browse_root)
    identity = delta.records[0].frame_identity
    browse_index.runtime_comparison_source(identity, "baseline")
    runtime_service = RuntimeViewService(service.profile, service.comparison, service.delta)
    directory = tmp_path / "runtime"
    manifest = build_runtime_index(runtime_service, directory)
    index = RuntimeIndex.open(directory, profile_sha256=manifest.runtime.profile_report_sha256)
    with pytest.raises(RuntimeSourceUnavailable):
        index.runtime_source(service.profile.frames[0].frame_id)
    with pytest.raises(RuntimeSourceUnavailable):
        index.runtime_comparison_source(identity, "baseline")
