"""Bounded backend tests for indexed comparison static source inventory."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_runtime_comparison_source_browsing import (
    GOLDEN,
    _project_frame,
    _source_hash,
    build_comparison_index,
)

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.profile_models import ProfileReport
from code_analysis.profiling import build_profile_diff
from code_analysis.reports.json_report import load_json
from code_analysis.runtime_index import RuntimeIndex, build_runtime_index
from code_analysis.runtime_models import RuntimeSourceHash, SourceBindingState
from code_analysis.runtime_source_browse import RuntimeSourceUnavailable
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService

MINIMAL = Path(__file__).parent / "golden" / "minimal-report.json"


def _comparison_static_with_shared_source_id(ordinary_report, *, alternate_sha256: str):
    alpha = next(item for item in ordinary_report.source_files if item.logical_path == "alpha/a.py")
    alternate = alpha.model_copy(update={"source_sha256": alternate_sha256})
    source_files = tuple(
        alternate if item.logical_path == "alpha/a.py" else item
        for item in ordinary_report.source_files
    )
    return ordinary_report.model_copy(update={"source_files": source_files})


def _build_distinct_static_comparison_index(ordinary_report, minimal_report, tmp_path):
    static_service = ReportViewService(ordinary_report, minimal_report)
    overview = static_service.bootstrap()
    primary_sha = overview.reports[0].report_sha256
    candidate_sha = overview.reports[1].report_sha256
    minimal_source = minimal_report.source_files[0]
    ordinary_source = _source_hash(ordinary_report)
    original = RuntimeViewService.load(GOLDEN).profile
    baseline_frame = _project_frame(
        original,
        path=ordinary_source.path,
        line=1,
        function="baseline_fn",
        frame_id="profile-frame-aaaaaaaaaaaaaaaaaaaa",
    )
    candidate_frame = baseline_frame.model_copy(
        update={
            "frame_id": "profile-frame-bbbbbbbbbbbbbbbbbbbb",
            "path": minimal_source.logical_path,
            "line": 1,
            "function": "visible",
        }
    )
    baseline = ProfileReport.model_validate(
        {
            **original.model_dump(),
            "frames": (baseline_frame,),
            "runtime_edges": (),
            "statistics": (),
            "source_report_sha256": primary_sha,
            "runtime": original.runtime.model_copy(
                update={
                    "source_binding": SourceBindingState.MATCHED,
                    "source_hashes": (ordinary_source,),
                }
            ),
        }
    )
    candidate = ProfileReport.model_validate(
        {
            **baseline.model_dump(),
            "profile_identity_sha256": "c" * 64,
            "frames": (candidate_frame,),
            "source_report_sha256": candidate_sha,
            "runtime": baseline.runtime.model_copy(
                update={
                    "source_binding": SourceBindingState.MATCHED,
                    "source_hashes": (
                        RuntimeSourceHash(
                            path=minimal_source.logical_path,
                            sha256=minimal_source.source_sha256,
                        ),
                    ),
                }
            ),
        }
    )
    delta = build_profile_diff(baseline, candidate, diagnostic_only=True)
    runtime_service = RuntimeViewService(baseline, candidate, delta)
    root = tmp_path / "distinct-static"
    manifest = build_browse_index(static_service, root, runtime=runtime_service)
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    return static_service, index, runtime_service, delta, minimal_source, ordinary_source


def _build_primary_bound_profiles_with_distinct_static(ordinary_report, tmp_path):
    alternate_sha = "d" * 64
    comparison_static = _comparison_static_with_shared_source_id(
        ordinary_report, alternate_sha256=alternate_sha
    )
    static_service = ReportViewService(ordinary_report, comparison_static)
    overview = static_service.bootstrap()
    primary_sha = overview.reports[0].report_sha256
    secondary_sha = overview.reports[1].report_sha256
    ordinary_source = _source_hash(ordinary_report)
    original = RuntimeViewService.load(GOLDEN).profile
    baseline_frame = _project_frame(
        original,
        path=ordinary_source.path,
        line=1,
        function="baseline_fn",
        frame_id="profile-frame-aaaaaaaaaaaaaaaaaaaa",
    )
    candidate_frame = baseline_frame.model_copy(
        update={"frame_id": "profile-frame-bbbbbbbbbbbbbbbbbbbb"}
    )
    runtime_kwargs = {
        "source_binding": SourceBindingState.MATCHED,
        "source_hashes": (ordinary_source,),
    }
    baseline = ProfileReport.model_validate(
        {
            **original.model_dump(),
            "frames": (baseline_frame,),
            "runtime_edges": (),
            "statistics": (),
            "source_report_sha256": primary_sha,
            "runtime": original.runtime.model_copy(update=runtime_kwargs),
        }
    )
    candidate = ProfileReport.model_validate(
        {
            **baseline.model_dump(),
            "profile_identity_sha256": "c" * 64,
            "frames": (candidate_frame,),
            "source_report_sha256": primary_sha,
            "runtime": baseline.runtime.model_copy(update=runtime_kwargs),
        }
    )
    delta = build_profile_diff(baseline, candidate, diagnostic_only=True)
    runtime_service = RuntimeViewService(baseline, candidate, delta)
    root = tmp_path / "primary-bound-distinct-static"
    manifest = build_browse_index(static_service, root, runtime=runtime_service)
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    alpha_primary = next(
        item for item in ordinary_report.source_files if item.logical_path == "alpha/a.py"
    )
    alpha_secondary = next(
        item for item in comparison_static.source_files if item.logical_path == "alpha/a.py"
    )
    return (
        index,
        delta,
        primary_sha,
        secondary_sha,
        alpha_primary,
        alpha_secondary,
        ordinary_source,
    )


def test_distinct_candidate_static_runtime_preview(ordinary_report, tmp_path):
    minimal = load_json(MINIMAL.read_text(encoding="utf-8"))
    _, index, _, delta, minimal_source, ordinary_source = _build_distinct_static_comparison_index(
        ordinary_report, minimal, tmp_path
    )
    runtime = index.manifest.runtime
    assert runtime is not None and runtime.comparison is not None
    assert runtime.comparison.candidate_overview.source_matches
    assert index.manifest.candidate_source_file_count == len(minimal.source_files)
    baseline_identity = next(
        record.frame_identity
        for record in delta.records
        if record.baseline_total_seconds is not None
    )
    candidate_identity = next(
        record.frame_identity
        for record in delta.records
        if record.candidate_total_seconds is not None
    )
    baseline_preview = index.runtime_comparison_source(baseline_identity, "baseline")
    assert baseline_preview.source.source_sha256 == ordinary_source.sha256
    candidate_preview = index.runtime_comparison_source(candidate_identity, "candidate")
    assert candidate_preview.source.source_sha256 == minimal_source.source_sha256


def test_same_path_different_hashes_indexed_tables(ordinary_report, tmp_path):
    _, index, delta = build_comparison_index(ordinary_report, tmp_path, candidate_hash="0" * 64)
    identity = delta.records[0].frame_identity
    index.runtime_comparison_source(identity, "baseline")
    with pytest.raises(ValueError, match="static source identity"):
        index.runtime_comparison_source(identity, "candidate")


def test_candidate_only_path_uses_comparison_inventory(ordinary_report, tmp_path):
    minimal = load_json(MINIMAL.read_text(encoding="utf-8"))
    _, index, _, delta, minimal_source, _ = _build_distinct_static_comparison_index(
        ordinary_report, minimal, tmp_path
    )
    candidate_only = next(
        record for record in delta.records if record.candidate_total_seconds is not None
    )
    preview = index.runtime_comparison_source(candidate_only.frame_identity, "candidate")
    assert preview.source.logical_path == minimal_source.logical_path
    secondary_sha = index.manifest.overview.reports[1].report_sha256
    with pytest.raises(ValueError, match="absent from this index"):
        index.source_file(
            minimal_source.logical_path,
            static_report_sha256=index.manifest.report_sha256,
        )
    assert (
        index.source_file(
            minimal_source.logical_path, static_report_sha256=secondary_sha
        ).logical_path
        == minimal_source.logical_path
    )


def test_profiles_bound_to_primary_use_primary_inventory(ordinary_report, tmp_path):
    index, delta, primary_sha, secondary_sha, _, _, _ = (
        _build_primary_bound_profiles_with_distinct_static(ordinary_report, tmp_path)
    )
    runtime = index.manifest.runtime
    assert runtime is not None and runtime.comparison is not None
    assert secondary_sha != primary_sha
    assert runtime.comparison.candidate_static_sha256 == primary_sha
    assert runtime.comparison.candidate_overview.source_matches
    assert index.manifest.candidate_source_file_count > 0
    identity = delta.records[0].frame_identity
    index.runtime_comparison_source(identity, "baseline")
    index.runtime_comparison_source(identity, "candidate")


def test_shared_source_file_id_across_inventories(ordinary_report, tmp_path):
    index, _, _, _, alpha_primary, alpha_secondary, _ = (
        _build_primary_bound_profiles_with_distinct_static(ordinary_report, tmp_path)
    )
    assert alpha_primary.source_file_id == alpha_secondary.source_file_id
    assert alpha_primary.source_sha256 != alpha_secondary.source_sha256
    secondary_sha = index.manifest.overview.reports[1].report_sha256
    primary_row = index.source_file("alpha/a.py", static_report_sha256=index.manifest.report_sha256)
    secondary_row = index.source_file("alpha/a.py", static_report_sha256=secondary_sha)
    assert primary_row.source_file_id == secondary_row.source_file_id
    assert primary_row.source_sha256 != secondary_row.source_sha256


def test_foreign_static_report_identity_rejected(ordinary_report, tmp_path):
    _, index, _ = build_comparison_index(ordinary_report, tmp_path)
    with pytest.raises(ValueError, match="not indexed"):
        index.source_file("alpha/a.py", static_report_sha256="f" * 64)


def test_missing_path_in_candidate_inventory(ordinary_report, tmp_path):
    minimal = load_json(MINIMAL.read_text(encoding="utf-8"))
    _, index, _, _, _, _ = _build_distinct_static_comparison_index(
        ordinary_report, minimal, tmp_path
    )
    secondary_sha = index.manifest.overview.reports[1].report_sha256
    with pytest.raises(ValueError, match="absent"):
        index.source_file("no/such/path.py", static_report_sha256=secondary_sha)


def test_source_free_reads_without_report_reload(ordinary_report, tmp_path, monkeypatch):
    minimal = load_json(MINIMAL.read_text(encoding="utf-8"))
    _, index, _, delta, _, _ = _build_distinct_static_comparison_index(
        ordinary_report, minimal, tmp_path
    )

    def forbid_load(*_args, **_kwargs):
        raise AssertionError("indexed browse must not reload authoritative reports")

    monkeypatch.setattr(ReportViewService, "load", forbid_load)
    index.comparison_records(offset=0, limit=5)
    candidate_identity = next(
        record.frame_identity
        for record in delta.records
        if record.candidate_total_seconds is not None
    )
    index.runtime_comparison_source(candidate_identity, "candidate")


def test_tampered_candidate_source_row_rejected(ordinary_report, tmp_path):
    minimal = load_json(MINIMAL.read_text(encoding="utf-8"))
    _, index, _, _, minimal_source, _ = _build_distinct_static_comparison_index(
        ordinary_report, minimal, tmp_path
    )
    path = minimal_source.logical_path
    with closing(sqlite3.connect(index.database)) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload FROM comparison_source_files WHERE path = ?", (path,)
            ).fetchone()[0]
        )
        payload["source_sha256"] = "0" * 64
        connection.execute(
            "UPDATE comparison_source_files SET payload = ? WHERE path = ?",
            (json.dumps(payload, separators=(",", ":"), sort_keys=True), path),
        )
        connection.commit()
    with pytest.raises(ValueError, match="database identity mismatch"):
        BrowseIndex.open(tmp_path / "distinct-static", report_sha256=index.manifest.report_sha256)


def test_malformed_candidate_source_receipt_count(ordinary_report, tmp_path):
    minimal = load_json(MINIMAL.read_text(encoding="utf-8"))
    _, index, _, _, _, _ = _build_distinct_static_comparison_index(
        ordinary_report, minimal, tmp_path
    )
    receipt = tmp_path / "distinct-static" / "manifest.json"
    manifest = json.loads(receipt.read_text(encoding="utf-8"))
    manifest["candidate_source_file_count"] = manifest["candidate_source_file_count"] + 1
    receipt.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="candidate source identity count"):
        BrowseIndex.open(tmp_path / "distinct-static", report_sha256=index.manifest.report_sha256)


def test_standalone_runtime_index_still_has_no_static_inventory(tmp_path, ordinary_report):
    runtime_service, _, delta = build_comparison_index(ordinary_report, tmp_path)
    directory = tmp_path / "runtime-only"
    manifest = build_runtime_index(runtime_service, directory)
    runtime_index = RuntimeIndex.open(
        directory, profile_sha256=manifest.runtime.profile_report_sha256
    )
    with pytest.raises(RuntimeSourceUnavailable):
        runtime_index.runtime_comparison_source(delta.records[0].frame_identity, "baseline")
