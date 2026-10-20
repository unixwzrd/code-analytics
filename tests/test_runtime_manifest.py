"""Strict runtime publication-manifest invariants using synthetic profiles only."""

from __future__ import annotations

import hashlib
import marshal
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.models import Completeness
from code_analysis.profile_ingestion import ingest_profiles
from code_analysis.runtime_artifacts import publish_runtime_report
from code_analysis.runtime_models import RuntimeArtifactEntry, RuntimeManifest, RuntimeSelection


def _entry(
    path: str = "runtime-summary.json", *, status: str = "complete", digest: str | None = None
) -> RuntimeArtifactEntry:
    digest = digest if digest is not None else "a" * 64
    if status in {"failed", "unavailable"}:
        return RuntimeArtifactEntry(path=path, status=status, reason="synthetic failure")
    return RuntimeArtifactEntry(path=path, sha256=digest, byte_count=1, status=status)


def _manifest(*artifacts: RuntimeArtifactEntry, source: str = "a" * 64) -> RuntimeManifest:
    return RuntimeManifest(
        source_json_sha256=source,
        selection=RuntimeSelection(),
        original_nodes=1,
        retained_nodes=1,
        original_edges=0,
        retained_edges=0,
        completeness=Completeness.COMPLETE,
        artifacts=artifacts or (_entry(),),
        limitations=(),
    )


@pytest.mark.parametrize("status", ["failed", "unavailable"])
def test_nonmaterialized_manifest_entries_cannot_claim_file_digests(status: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeArtifactEntry(
            path="runtime-graph.svg",
            sha256="a" * 64,
            byte_count=1,
            status=status,
            reason="synthetic failure",
        )


@pytest.mark.parametrize("status", ["complete", "partial"])
def test_materialized_manifest_entries_require_file_digests(status: str) -> None:
    with pytest.raises(ValidationError):
        RuntimeArtifactEntry(path="runtime-graph.svg", status=status)


def test_manifest_rejects_duplicate_paths_missing_summary_and_digest_mismatch() -> None:
    with pytest.raises(ValidationError):
        _manifest(_entry(), _entry())
    with pytest.raises(ValidationError):
        _manifest(_entry("runtime-graph.svg"))
    with pytest.raises(ValidationError):
        _manifest(_entry(digest="b" * 64))


def test_manifest_rejects_complete_summary_with_reason() -> None:
    with pytest.raises(ValidationError):
        RuntimeArtifactEntry(
            path="runtime-summary.json",
            sha256="a" * 64,
            byte_count=1,
            status="complete",
            reason="contradictory",
        )


def test_failed_profile_report_publishes_failed_manifest(tmp_path: Path, monkeypatch) -> None:
    profile = tmp_path / "failed.prof"
    profile.write_bytes(marshal.dumps({("module.py", 1, "work"): (1, 1, 0.1, 0.1, {})}))
    report, _ = ingest_profiles((profile,))
    failed = report.model_copy(update={"completeness": Completeness.FAILED})
    monkeypatch.setattr("code_analysis.runtime_artifacts.shutil.which", lambda _: None)

    manifest = publish_runtime_report(failed, tmp_path / "published")

    assert manifest.completeness == Completeness.FAILED
    summary = next(item for item in manifest.artifacts if item.path == "runtime-summary.json")
    assert summary.status == "complete"
    assert summary.sha256 == manifest.source_json_sha256
    assert (
        summary.sha256
        == hashlib.sha256((tmp_path / "published" / summary.path).read_bytes()).hexdigest()
    )
