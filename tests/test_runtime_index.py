from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from code_analysis.browse_export import export_browse_page
from code_analysis.profiling import render_profile_json
from code_analysis.reports.projection_views import ProjectionFormat
from code_analysis.runtime_browse_models import RuntimeBrowseRequest
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_index import RuntimeIndex, RuntimeIndexManifest, build_runtime_index
from code_analysis.runtime_queries import RuntimeQuerySection
from code_analysis.runtime_source_browse import RuntimeSourceUnavailable
from code_analysis.ui_runtime import RuntimeViewService

GOLDEN = Path(__file__).parent / "golden/phase4-profile-report.json"


def test_standalone_index_pages_graphs_exports_and_integrity(tmp_path):
    service = RuntimeViewService.load(GOLDEN)
    digest = hashlib.sha256(render_profile_json(service.profile, compact=True).encode()).hexdigest()
    directory = tmp_path / "runtime"
    manifest = build_runtime_index(service, directory)
    assert manifest.report_sha256 is None and not manifest.runtime.overview.source_matches
    index = RuntimeIndex.open(directory, profile_sha256=digest)
    request = RuntimeBrowseRequest(section=RuntimeQuerySection.HOTSPOTS)
    assert index.runtime_page(request).panel.projection.records
    assert index.runtime_graph(RuntimeGraphRequest()).nodes
    for format_ in (ProjectionFormat.JSON_COMPACT, ProjectionFormat.TSV, ProjectionFormat.MARKDOWN):
        path, projection = export_browse_page(index, request, format_.value, format_)
        assert path.is_file() and projection.rows
    with pytest.raises(RuntimeSourceUnavailable):
        index.runtime_source(service.profile.frames[0].frame_id)
    with pytest.raises(ValueError, match="different profile"):
        RuntimeIndex.open(directory, profile_sha256="0" * 64)
    with pytest.raises(FileExistsError):
        build_runtime_index(service, directory)
    index.database.write_bytes(b"broken")
    with pytest.raises(ValueError):
        index.runtime_page(request)
    with pytest.raises(ValueError):
        RuntimeIndex.open(directory, profile_sha256=digest)


def test_standalone_manifest_cannot_claim_static_binding(tmp_path):
    from pydantic import ValidationError

    manifest = build_runtime_index(RuntimeViewService.load(GOLDEN), tmp_path / "runtime")
    raw = manifest.model_dump(mode="json")
    raw["runtime"]["overview"]["source_matches"] = True
    with pytest.raises(ValidationError):
        RuntimeIndexManifest.model_validate_json(json.dumps(raw))


def test_standalone_comparison_remains_diagnostic(tmp_path):
    from code_analysis.runtime_comparison_browse import RuntimeComparisonRequest

    service = RuntimeViewService.load(GOLDEN, GOLDEN)
    manifest = build_runtime_index(service, tmp_path / "runtime")
    index = RuntimeIndex.open(
        tmp_path / "runtime", profile_sha256=manifest.runtime.profile_report_sha256
    )
    page = index.runtime_comparison(RuntimeComparisonRequest())
    assert not page.report.claim_eligible
    assert not index.manifest.runtime.comparison.candidate_overview.source_matches


def test_standalone_index_rejects_symlink_receipt(tmp_path):
    service = RuntimeViewService.load(GOLDEN)
    directory = tmp_path / "runtime"
    manifest = build_runtime_index(service, directory)
    receipt = directory / "manifest.json"
    receipt.rename(tmp_path / "receipt.json")
    receipt.symlink_to(tmp_path / "receipt.json")
    with pytest.raises((ValueError, OSError)):
        RuntimeIndex.open(directory, profile_sha256=manifest.runtime.profile_report_sha256)
