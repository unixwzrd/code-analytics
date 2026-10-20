from __future__ import annotations

import shutil
import sqlite3

import pytest
from conftest import FIXTURES
from pydantic import ValidationError

from code_analysis.browse_export import export_browse_page, render_browse_artifact
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseIndexManifest
from code_analysis.candidate_browse import validate_candidate_policies
from code_analysis.config import load_config_file
from code_analysis.models import Completeness
from code_analysis.project import discover_context
from code_analysis.projection_models import ProjectionSelection
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.report_models import RefactorCandidateKind
from code_analysis.reports.projection_views import ProjectionFormat
from code_analysis.service import scan_refactor
from code_analysis.static_browse import StaticProjectionRequest, static_page_projection
from code_analysis.web_service import ReportViewService


@pytest.fixture
def saved(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(FIXTURES / "proposal_project", source)
    report = scan_refactor(
        discover_context(source, load_config_file(FIXTURES / "proposal-config.toml"))
    )
    manifest = build_browse_index(ReportViewService(report), tmp_path / "index")
    shutil.rmtree(source)
    return report, BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"prefer_existing_owner": True},
        {"cross_module_only": True},
        {"production_only": True},
        {"min_shared_dimensions": 32},
        {"candidate_module_patterns": ("package.combine_*",)},
        {"candidate_exclude_patterns": ("*",)},
        {"candidate_states": ("blocked",)},
        {"candidate_kinds": (RefactorCandidateKind.MODULE_COMBINATION,)},
        {
            "candidate_module_patterns": ("*",),
            "candidate_exclude_patterns": ("*.missing",),
            "cross_module_only": True,
            "prefer_existing_owner": True,
        },
    ],
)
def test_indexed_policy_matches_authoritative_builder_before_paging(saved, options):
    report, index = saved
    expected = build_projection(report, ProjectionName.CANDIDATE_WORKLIST, **options)
    request = StaticProjectionRequest(
        name=ProjectionName.CANDIDATE_WORKLIST,
        limit=1,
        candidate_selection=ProjectionSelection(**options),
    )
    page = index.static_projection(request)
    assert page.eligible == len(expected.rows)
    assert page.records == expected.rows[:1]
    assert page.summary.sort == expected.sort
    assert page.summary.source == expected.inputs[0]
    if len(expected.rows) > 1:
        later = index.static_projection(request.model_copy(update={"offset": 1}))
        assert later.records == expected.rows[1:2]
        assert static_page_projection(later).truncated
    projected = static_page_projection(page)
    assert projected.selection == expected.selection.model_copy(update={"max_records": 1})
    assert projected.completeness is not Completeness.COMPLETE or not projected.truncated


def test_search_and_exports_keep_applied_policy(saved):
    report, index = saved
    full = build_projection(report, ProjectionName.CANDIDATE_WORKLIST, prefer_existing_owner=True)
    identity = full.rows[-1].row_id
    request = StaticProjectionRequest(
        name=ProjectionName.CANDIDATE_WORKLIST,
        query=identity,
        candidate_selection=ProjectionSelection(prefer_existing_owner=True),
    )
    page = index.static_projection(request)
    assert page.eligible == 1 and page.records == (full.rows[-1],)
    for format in (ProjectionFormat.JSON_COMPACT, ProjectionFormat.TSV, ProjectionFormat.MARKDOWN):
        path, projection = export_browse_page(index, request, "selected-candidate", format)
        assert path.read_bytes() == render_browse_artifact(projection, format)
        assert projection.selection.prefer_existing_owner
        assert projection.inputs[0].report_sha256 == index.manifest.report_sha256
        with pytest.raises(FileExistsError):
            export_browse_page(index, request, "selected-candidate", format)


def test_no_candidate_evidence_is_unavailable_not_empty_worklist(ordinary_report, tmp_path):
    manifest = build_browse_index(ReportViewService(ordinary_report), tmp_path / "index")
    index = BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256)
    assert ProjectionName.CANDIDATE_WORKLIST not in {
        item.name for item in manifest.static_projections
    }
    with pytest.raises(ValueError, match="unavailable"):
        index.static_projection(StaticProjectionRequest(name=ProjectionName.CANDIDATE_WORKLIST))


def test_policy_rows_are_bound_to_receipt(saved):
    _, index = saved
    with sqlite3.connect(index.database) as connection:
        count = connection.execute("SELECT count(*) FROM candidate_policy_rows").fetchone()[0]
        assert count
        connection.execute("DELETE FROM candidate_policy_rows WHERE position = 0")
        with pytest.raises(ValueError, match="receipt"):
            validate_candidate_policies(connection, count)


def test_swapped_policy_associations_are_rejected(saved):
    _, index = saved
    with sqlite3.connect(index.database) as connection:
        rows = connection.execute(
            "SELECT position, payload FROM candidate_policy_rows ORDER BY position"
        ).fetchall()
        assert len(rows) >= 2
        connection.execute(
            "UPDATE candidate_policy_rows SET payload = ? WHERE position = ?",
            (rows[1][1], rows[0][0]),
        )
        connection.execute(
            "UPDATE candidate_policy_rows SET payload = ? WHERE position = ?",
            (rows[0][1], rows[1][0]),
        )
        with pytest.raises(ValueError, match="association"):
            validate_candidate_policies(connection, len(rows))


def test_manifest_cannot_hide_candidates_or_soften_failed_sources(saved):
    _, index = saved
    payload = index.manifest.model_dump()
    payload["static_projections"] = tuple(
        item
        for item in payload["static_projections"]
        if item["name"] is not ProjectionName.CANDIDATE_WORKLIST
    )
    with pytest.raises(ValidationError, match="availability"):
        BrowseIndexManifest.model_validate(payload)
    payload = index.manifest.model_dump()
    for table in payload["tables"]:
        if table["table"].value == "candidates":
            table["records"] = 0
    with pytest.raises(ValidationError, match="availability"):
        BrowseIndexManifest.model_validate(payload)
    payload = index.manifest.model_dump()
    payload["overview"]["reports"][0]["completeness"] = Completeness.FAILED
    for item in payload["static_projections"]:
        item["completeness"] = Completeness.PARTIAL
    with pytest.raises(ValidationError, match="completeness"):
        BrowseIndexManifest.model_validate(payload)


@pytest.mark.parametrize(
    "selection",
    [
        ProjectionSelection(candidate_states=("approved",)),
        ProjectionSelection(candidate_decisions=("accepted",)),
        ProjectionSelection(selected_ids=("some-id",)),
        ProjectionSelection(max_records=1),
        ProjectionSelection(depth=1),
    ],
)
def test_indexed_policy_rejects_inapplicable_options(selection):
    with pytest.raises(ValidationError):
        StaticProjectionRequest(
            name=ProjectionName.CANDIDATE_WORKLIST, candidate_selection=selection
        )


def test_policy_cannot_be_applied_to_other_static_views():
    with pytest.raises(ValidationError):
        StaticProjectionRequest(
            name=ProjectionName.COMPLEXITY, candidate_selection=ProjectionSelection()
        )
