from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import AnalysisConfig
from code_analysis.indexed_runs import IndexedRunRepository
from code_analysis.profile_runs import ProfileRunCatalog
from code_analysis.profiling import render_profile_json
from code_analysis.project import discover_context
from code_analysis.reports.json_report import canonical_json
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import GenerationKind, commit_generation, prepare_workspace


def test_standalone_profile_pair_identity_and_no_analysis_reads(profile_runs, monkeypatch):
    workspace, _, profiles = profile_runs

    def unexpected(*args, **kwargs):
        raise AssertionError("profile-only selection must not inventory analysis generations")

    monkeypatch.setattr("code_analysis.indexed_runs.list_generations", unexpected)
    repository = IndexedRunRepository(workspace.root, include_analysis=False)
    baseline, candidate = (item.generation_id for item in profiles)
    first = repository.select_runtime(baseline)
    pair = repository.select_runtime(baseline, candidate)
    reverse = repository.select_runtime(candidate, baseline)
    assert len({first.database, pair.database, reverse.database}) == 3
    assert repository.select_runtime(baseline, candidate).database == pair.database
    assert pair.manifest.report_sha256 is None
    assert pair.manifest.runtime.comparison.header.diagnostic_only
    assert not pair.manifest.runtime.comparison.header.claim_eligible
    assert (
        pair.manifest.runtime.profile_report_sha256
        != pair.manifest.runtime.comparison.candidate_report_sha256
    )
    (workspace.kind_directory(GenerationKind.PROFILE) / candidate / "profile.json").write_text("{}")
    with pytest.raises((ValueError, OSError)):
        repository.select_runtime(baseline, candidate)


def test_terminal_collection_selects_standalone_profile_pair(profile_runs):
    pytest.importorskip("textual")
    from textual.widgets import Button, Select

    from code_analysis.collection_commands import CollectionPlanner
    from code_analysis.collection_service import CollectionSession, CollectionWorkspace
    from code_analysis.tui_runs import RunPicker
    from code_analysis.tui_runtime import RuntimeBrowser
    from code_analysis.tui_runtime_comparison import RuntimeComparisonBrowser
    from code_analysis.tui_start import CollectionTerminal

    workspace, _, profiles = profile_runs
    session = CollectionSession(CollectionPlanner(workspace.root.parent))
    session.connect(CollectionWorkspace(workspace=workspace.root.name))

    async def exercise():
        app = CollectionTerminal(workspace.root.parent, session)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.screen.query_one("#jobs-profiles", Button).press()
            await pilot.pause(0.3)
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, RunPicker)
            assert not app.screen.query_one("#run-baseline", Select).display
            app.screen.query_one("#run-profile", Select).value = profiles[0].generation_id
            app.screen.query_one("#run-profile-candidate", Select).value = profiles[1].generation_id
            app.screen.query_one("#run-open", Button).press()
            await pilot.pause(0.4)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, RuntimeBrowser)
            assert app.screen.index.manifest.report_sha256 is None
            app.screen.query_one("#runtime-compare", Button).press()
            await pilot.pause(0.3)
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, RuntimeComparisonBrowser)
            assert app.screen.page is not None
            assert not app.screen.page.report.claim_eligible

    try:
        asyncio.run(exercise())
    finally:
        session.close()
        session.planner.close()


@pytest.fixture
def profile_runs(indexed_runs):
    workspace, analyses = indexed_runs
    profile = RuntimeViewService.load(
        Path(__file__).parent / "golden" / "phase4-profile-report.json"
    ).profile
    profiles = tuple(
        commit_generation(
            workspace,
            kind=GenerationKind.PROFILE,
            artifacts={
                "profile.json": render_profile_json(
                    profile.model_copy(
                        update={
                            "limitations": (*profile.limitations, f"synthetic capture {number}")
                        }
                    ),
                    compact=True,
                ).encode()
            },
            command=("profile",),
            interpreter_path="/python",
            portable_identity_sha256=profile.profile_identity_sha256,
        )
        for number in range(2)
    )
    return workspace, analyses, profiles


def test_profile_selection_distinguishes_snapshot_and_workload(profile_runs):
    workspace, analyses, profiles = profile_runs
    repository = IndexedRunRepository(workspace.root)
    catalog = repository.profiles()
    assert len(catalog.records) == 2
    assert catalog.records[0].profile_identity_sha256 == catalog.records[1].profile_identity_sha256
    assert catalog.records[0].artifact_sha256 != catalog.records[1].artifact_sha256
    first = repository.select(analyses[0].generation_id, profile=profiles[0].generation_id)
    second = repository.select(analyses[0].generation_id, profile=profiles[1].generation_id)
    assert first.database != second.database
    assert (
        first.manifest.runtime.profile_report_sha256
        != second.manifest.runtime.profile_report_sha256
    )
    pair = repository.select(
        analyses[0].generation_id,
        profile=profiles[0].generation_id,
        compare_profile=profiles[1].generation_id,
    )
    assert pair.manifest.runtime.comparison is not None
    assert pair.manifest.runtime.comparison.header.diagnostic_only
    assert (
        repository.select(analyses[0].generation_id, profile=profiles[0].generation_id).manifest
        == first.manifest
    )
    assert repository.select(analyses[0].generation_id).manifest.runtime is None


def test_profile_catalog_strictness_and_corruption(profile_runs):
    workspace, analyses, profiles = profile_runs
    repository = IndexedRunRepository(workspace.root)
    catalog = repository.profiles()
    payload = json.loads(catalog.model_dump_json())
    payload["records"].append(payload["records"][0])
    with pytest.raises(ValidationError):
        ProfileRunCatalog.model_validate_json(json.dumps(payload))
    with pytest.raises(ValueError, match="baseline"):
        repository.select(analyses[0].generation_id, compare_profile=profiles[0].generation_id)
    with pytest.raises(ValueError, match="absent"):
        repository.select(analyses[0].generation_id, profile="../../profile.json")
    path = (
        workspace.kind_directory(GenerationKind.PROFILE)
        / profiles[0].generation_id
        / "profile.json"
    )
    path.write_text("{}")
    with pytest.raises((OSError, ValueError)):
        repository.select(analyses[0].generation_id, profile=profiles[0].generation_id)


def test_profile_manifest_identity_mismatch_never_builds_index(profile_runs):
    workspace, analyses, profiles = profile_runs
    payload = (
        workspace.kind_directory(GenerationKind.PROFILE)
        / profiles[0].generation_id
        / "profile.json"
    ).read_bytes()
    malformed = commit_generation(
        workspace,
        kind=GenerationKind.PROFILE,
        artifacts={"profile.json": payload},
        command=("profile",),
        interpreter_path="/python",
        portable_identity_sha256="0" * 64,
    )
    repository = IndexedRunRepository(workspace.root)
    with pytest.raises(ValueError, match="manifest"):
        repository.select(analyses[0].generation_id, profile=malformed.generation_id)
    assert not tuple(workspace.cache.glob("browse-*"))


def test_profile_loader_validates_exact_payload_bytes():
    path = Path(__file__).parent / "golden" / "phase4-profile-report.json"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert RuntimeViewService.load(path, expected_sha256=digest).profile.frames
    with pytest.raises(ValueError, match="checksum"):
        RuntimeViewService.load(path, expected_sha256="0" * 64)
    with pytest.raises(ValueError, match="checksum"):
        RuntimeViewService.load(
            path, path, expected_sha256=digest, expected_comparison_sha256="0" * 64
        )


def test_terminal_selects_saved_profiles(profile_runs, ordinary_report, tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Select

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_runtime import RuntimeBrowser

    workspace, analyses, profiles = profile_runs
    root = tmp_path / "initial"
    manifest = build_browse_index(ReportViewService(ordinary_report), root)
    initial = BrowseIndex.open(root, report_sha256=manifest.report_sha256)

    async def exercise():
        app = AnalysisTerminal(initial, workspace=workspace.root)
        async with app.run_test(size=(180, 48)) as pilot:
            await pilot.press("ctrl+o")
            await app.workers.wait_for_complete()
            app.screen.query_one("#run-baseline", Select).value = analyses[0].generation_id
            app.screen.query_one("#run-profile", Select).value = profiles[0].generation_id
            app.screen.query_one("#run-profile-candidate", Select).value = profiles[1].generation_id
            await pilot.click("#run-open")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.index.manifest.runtime.comparison is not None
            await pilot.press("ctrl+r")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, RuntimeBrowser)
            assert app.screen.page.panel.projection.records

    asyncio.run(exercise())


@pytest.fixture
def indexed_runs(ordinary_report, tmp_path):
    context = discover_context(Path(__file__).parent / "fixtures" / "ordinary", AnalysisConfig())
    workspace = prepare_workspace(context, explicit_path=tmp_path / "workspace")
    payload = canonical_json(ordinary_report).encode()
    manifests = tuple(
        commit_generation(
            workspace,
            kind=GenerationKind.ANALYSIS,
            artifacts={"report.json": payload},
            command=("scan",),
            interpreter_path="/python",
            portable_identity_sha256=hashlib.sha256(payload).hexdigest(),
        )
        for _ in range(2)
    )
    return workspace, manifests


def test_owned_run_selection_reuses_index_without_loading(indexed_runs, monkeypatch):
    workspace, manifests = indexed_runs
    repository = IndexedRunRepository(workspace.root)
    assert len(repository.refresh().records) == 2
    selected = tuple(item.generation_id for item in manifests)
    index = repository.select(*selected)
    assert index.manifest.comparison.identical
    assert index.manifest.runtime is None
    monkeypatch.setattr(ReportViewService, "load", lambda *a: pytest.fail("cached report reload"))
    assert repository.select(*selected).manifest == index.manifest
    path = workspace.kind_directory(GenerationKind.ANALYSIS) / selected[0] / "report.json"
    path.write_text("{}")
    with pytest.raises((OSError, ValueError)):
        repository.select(*selected)


def test_run_catalog_refresh_and_invalid_selection(indexed_runs):
    workspace, manifests = indexed_runs
    repository = IndexedRunRepository(workspace.root)
    with pytest.raises(ValueError, match="absent"):
        repository.select("../../report.json")
    index = repository.select(manifests[0].generation_id)
    assert index.manifest.comparison is None
    assert len(repository.refresh().records) == 2


def test_run_refresh_adds_saved_generation_without_source(indexed_runs):
    workspace, manifests = indexed_runs
    repository = IndexedRunRepository(workspace.root)
    path = (
        workspace.kind_directory(GenerationKind.ANALYSIS)
        / manifests[0].generation_id
        / "report.json"
    )
    incoming = commit_generation(
        workspace,
        kind=GenerationKind.ANALYSIS,
        artifacts={"report.json": path.read_bytes()},
        command=("scan",),
        interpreter_path="/python",
        portable_identity_sha256=manifests[0].portable_identity_sha256,
    )
    assert repository.refresh().records[0].generation_id == incoming.generation_id
    assert (
        repository.select(incoming.generation_id).manifest.report_sha256
        == incoming.portable_identity_sha256
    )


def test_cached_index_wrong_pair_is_rejected(indexed_runs):
    workspace, manifests = indexed_runs
    repository = IndexedRunRepository(workspace.root)
    single = repository.select(manifests[0].generation_id)
    pair = repository.select(*(item.generation_id for item in manifests))
    # Preserve both caches while putting a valid, but wrong, index at the selected pair path.
    moved = pair.database.parent.with_name("preserved-pair")
    pair.database.parent.rename(moved)
    shutil.copytree(single.database.parent, pair.database.parent)
    with pytest.raises(ValueError, match="run pair"):
        repository.select(*(item.generation_id for item in manifests))


def test_run_cache_symlink_is_rejected(indexed_runs):
    workspace, manifests = indexed_runs
    repository = IndexedRunRepository(workspace.root)
    index = repository.select(manifests[0].generation_id)
    path = index.database.parent
    moved = path.with_name("preserved-index")
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        repository.select(manifests[0].generation_id)


def test_terminal_run_picker_recomposes_selection(indexed_runs, ordinary_report, tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Select

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_runs import RunPicker

    workspace, manifests = indexed_runs
    initial = tmp_path / "initial"
    receipt = build_browse_index(ReportViewService(ordinary_report), initial)
    index = BrowseIndex.open(initial, report_sha256=receipt.report_sha256)

    async def exercise():
        app = AnalysisTerminal(index, workspace=workspace.root)
        async with app.run_test(size=(180, 48)) as pilot:
            await pilot.press("ctrl+o")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, RunPicker)
            app.screen.query_one("#run-baseline", Select).value = manifests[0].generation_id
            app.screen.query_one("#run-candidate", Select).value = manifests[1].generation_id
            await pilot.click("#run-open")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not isinstance(app.screen, RunPicker)
            assert app.index.database.parent != initial
            assert app.index.manifest.comparison.identical
            assert app.index.manifest.runtime is None
            await pilot.press("f2")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.current_page is not None, (
                app.showing_overview,
                app.selection,
                app.query_one("#page-status").render(),
                app.screen_stack,
            )
            assert app.current_page.records
            await pilot.press("ctrl+o")
            await app.workers.wait_for_complete()
            await pilot.click("#run-refresh")
            await app.workers.wait_for_complete()
            await pilot.press("escape")

    asyncio.run(exercise())
