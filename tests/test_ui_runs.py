from __future__ import annotations

import hashlib
import json
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest
from browser_navigation import reveal_navigation
from conftest import FIXTURES

from code_analysis.config import AnalysisConfig
from code_analysis.profiling import render_profile_json
from code_analysis.project import discover_context
from code_analysis.reports.json_report import canonical_json
from code_analysis.ui_runs import RunRepository
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_server import create_server
from code_analysis.web_service import ReportViewService
from code_analysis.workspace import (
    GenerationKind,
    commit_generation,
    prepare_workspace,
    prune_generations,
)


@pytest.fixture
def saved_runs(ordinary_report, tmp_path: Path):
    workspace = prepare_workspace(
        discover_context(FIXTURES / "ordinary", AnalysisConfig()),
        explicit_path=tmp_path / "workspace",
    )
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


@pytest.fixture
def saved_profiles(tmp_path):
    workspace = prepare_workspace(
        discover_context(FIXTURES / "ordinary", AnalysisConfig()),
        explicit_path=tmp_path / "workspace",
    )
    profile = RuntimeViewService.load(
        Path(__file__).parent / "golden" / "phase4-profile-report.json"
    ).profile
    manifests = tuple(
        commit_generation(
            workspace,
            kind=GenerationKind.PROFILE,
            artifacts={
                "profile.json": render_profile_json(
                    profile.model_copy(
                        update={"limitations": (*profile.limitations, f"capture {number}")}
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
    return workspace, manifests


def test_profile_only_repository_has_no_fabricated_static_report(saved_profiles, monkeypatch):
    workspace, manifests = saved_profiles

    def unexpected(*args, **kwargs):
        raise AssertionError("profile-only browsing must not read analysis generations")

    monkeypatch.setattr("code_analysis.ui_runs.list_generations", unexpected)
    repository = RunRepository(workspace.root, None)
    assert not repository.catalog().records and not repository.refresh().records
    runtime = repository.select_profiles(*(item.generation_id for item in manifests))
    assert runtime is not None and runtime.delta.diagnostic_only
    assert not runtime.overview(None).source_matches
    with pytest.raises(ValueError, match="no static report"):
        repository.select("current", "none")


@pytest.mark.parametrize("width", [1280, 390])
def test_web_profile_only_picker_after_workspace_connection(saved_profiles, width):
    workspace, manifests = saved_profiles
    playwright = pytest.importorskip("playwright.sync_api")
    server = create_server(
        None, collection_root=workspace.root.parent, allow_collection=True, token="profile-picker"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": width, "height": 960})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=profile-picker")
                toggle = page.locator(".lui-shell__menu")
                if toggle.is_visible():
                    toggle.click()
                page.get_by_role("button", name="Collect / plan", exact=True).click()
                page.locator('fieldset[data-mode="static"] summary').click()
                page.locator("#workspace").fill(workspace.root.name)
                page.locator("#connect-jobs").click()
                playwright.expect(page.locator("#job-status")).to_contain_text("retained jobs")

                def navigate(name):
                    toggle = page.locator(".lui-shell__menu")
                    if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
                        toggle.click()
                    reveal_navigation(page, name).click()

                navigate("Saved runs")
                page.locator("#baseline-profile").select_option(manifests[0].generation_id)
                page.locator("#candidate-profile").select_option(manifests[1].generation_id)
                assert not page.locator("#baseline-run").is_visible()
                page.locator("#apply-runs").click()
                page.get_by_role("heading", name="Runtime", exact=True).wait_for()
                page.get_by_label("Runtime view", exact=True).select_option("comparison")
                page.get_by_role("button", name="Show runtime evidence", exact=True).click()
                playwright.expect(page.locator("#runtime-results")).to_contain_text(
                    "Diagnostic comparison only"
                )
                playwright.expect(page.locator("#report-state")).to_contain_text("no static report")
                navigate("Saved runs")
                page.locator("#baseline-profile").select_option("none")
                page.locator("#apply-runs").click()
                playwright.expect(page.locator("#content")).to_contain_text("requires a baseline")
                navigate("Runtime")
                playwright.expect(page.locator("#runtime-controls")).to_be_visible()
                navigate("Saved runs")
                page.locator("#baseline-profile").select_option("none")
                page.locator("#candidate-profile").select_option("none")
                page.locator("#apply-runs").click()
                playwright.expect(page.locator("#report-state")).to_contain_text(
                    "No runtime profile selected"
                )
                playwright.expect(page.locator('.nav[data-view="runtime"]')).to_have_attribute(
                    "data-readiness", "missing"
                )
                assert not page.get_by_role(
                    "button", name="Overview", exact=True, include_hidden=True
                ).is_disabled()
                page.get_by_role("heading", name="What would you like to investigate?").wait_for()
                assert page.locator("body").evaluate(
                    "node => node.scrollWidth <= window.innerWidth"
                )
                assert not errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_web_profile_snapshots_are_bounded_and_not_workload_aliases(
    ordinary_report, saved_profiles
):
    workspace, manifests = saved_profiles
    repository = RunRepository(workspace.root, ReportViewService(ordinary_report))
    catalog = repository.profiles()
    assert catalog.records[0].profile_identity_sha256 == catalog.records[1].profile_identity_sha256
    assert catalog.records[0].artifact_sha256 != catalog.records[1].artifact_sha256
    left, right = (item.generation_id for item in manifests)
    first = repository.select_profiles(left, "none")
    second = repository.select_profiles(right, "none")
    assert first.profile.limitations != second.profile.limitations
    assert repository.select_profiles(left, "none") is first
    paired = repository.select_profiles(left, right)
    assert paired.delta.diagnostic_only and not paired.delta.claim_eligible
    assert len(repository._profiles) == 2
    assert repository.select_profiles("none", "none") is None
    with pytest.raises(ValueError, match="baseline"):
        repository.select_profiles("none", left)
    with pytest.raises(ValueError, match="absent"):
        repository.select_profiles("../../profile.json", "none")
    # Already loaded snapshots remain usable; a new pairing revalidates disk.
    path = workspace.kind_directory(GenerationKind.PROFILE) / right / "profile.json"
    path.write_text("{}")
    assert repository.select_profiles(left, right) is paired
    with pytest.raises(OSError):
        repository.select_profiles(right, left)
    with pytest.raises(OSError):
        repository.profiles()


def test_web_profile_catalog_selection_and_download_share_snapshot(ordinary_report, saved_profiles):
    from code_analysis.projections.runtime import build_runtime_graph
    from code_analysis.reports.projection_views import ProjectionFormat, render_projection
    from code_analysis.runtime_models import RuntimeSelection

    workspace, manifests = saved_profiles
    server = create_server(
        ReportViewService(ordinary_report), workspace=workspace.root, token="profiles-test"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)

    def get(path, authenticated=True):
        connection.request(
            "GET",
            path,
            headers={"X-Code-Analysis-Token": "profiles-test"} if authenticated else {},
        )
        response = connection.getresponse()
        return response.status, response.read()

    try:
        assert get("/api/profile-runs", False)[0] == 403
        status, payload = get("/api/profile-runs")
        assert status == 200 and len(json.loads(payload)["records"]) == 2
        left, right = (item.generation_id for item in manifests)
        params = f"profile_run={left}&compare_profile_run={right}"
        status, payload = get(f"/api/runtime?{params}")
        overview = json.loads(payload)
        assert status == 200 and overview["available"] and overview["comparison_available"]
        assert not overview["source_matches"]
        status, payload = get(f"/api/runtime/comparison?{params}")
        assert status == 200 and json.loads(payload)["report"]["diagnostic_only"]
        runtime = server.app.runs.select_profiles(left, right)
        graph = build_runtime_graph(runtime.profile, RuntimeSelection(max_nodes=1, max_edges=1))
        status, payload = get(f"/api/runtime/download?{params}&format=dot&max_nodes=1&max_edges=1")
        assert status == 200
        assert payload == render_projection(graph, ProjectionFormat.DOT).encode()
        assert get(f"/api/runtime?compare_profile_run={right}")[0] == 400
        assert get(f"/api/runtime?profile_run=current&compare_profile_run={right}")[0] == 400
        assert get(f"/api/runtime?profile_run={left}&profile_run={right}")[0] == 400
        assert not json.loads(get("/api/runtime?profile_run=none")[1])["available"]
        assert not json.loads(get("/api/runtime")[1])["available"]  # no global mutation
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_web_profile_picker_preserves_selection_on_failure_and_clears_explicitly(
    ordinary_report, saved_profiles
):
    playwright = pytest.importorskip("playwright.sync_api")
    workspace, manifests = saved_profiles
    # Profile-only workspaces must expose the picker too.
    server = create_server(
        ReportViewService(ordinary_report), workspace=workspace.root, token="profiles-test"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=profiles-test")
                page.locator(".cards").wait_for()
                page.get_by_role("button", name="Saved runs", exact=True).click()
                page.locator("#baseline-profile").select_option(manifests[0].generation_id)
                page.locator("#candidate-profile").select_option(manifests[1].generation_id)
                page.get_by_role("button", name="Open selected runs", exact=True).click()
                page.locator(".cards").wait_for()
                reveal_navigation(page, "Runtime").click()
                page.get_by_text("capture 0", exact=True).wait_for()
                page.get_by_label("Runtime view", exact=True).select_option("comparison")
                page.get_by_role("button", name="Show runtime evidence", exact=True).click()
                page.get_by_text("Diagnostic comparison only; claim eligible: false.").wait_for()
                page.get_by_role("button", name="Saved runs", exact=True).click()
                page.locator("#baseline-profile").select_option("none")
                page.get_by_role("button", name="Open selected runs", exact=True).click()
                page.get_by_text("a comparison profile requires a baseline profile").wait_for()
                reveal_navigation(page, "Runtime").click()
                page.get_by_text("capture 0", exact=True).wait_for()
                page.get_by_role("button", name="Saved runs", exact=True).click()
                page.locator("#baseline-profile").select_option("none")
                page.locator("#candidate-profile").select_option("none")
                page.get_by_role("button", name="Open selected runs", exact=True).click()
                page.locator(".cards").wait_for()
                reveal_navigation(page, "Runtime").click()
                playwright.expect(page.locator("#workflow-notice")).to_contain_text(
                    "Load saved runtime evidence"
                )
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_run_selection_is_source_free_and_identity_bound(ordinary_report, saved_runs) -> None:
    workspace, manifests = saved_runs
    repository = RunRepository(workspace.root, ReportViewService(ordinary_report))
    assert len(repository.catalog().records) == 2
    identities = [item.generation_id for item in manifests]
    service = repository.select(*identities)
    assert service.delta.identical
    assert repository.select(*identities) is service
    assert repository.select(identities[0], "none").comparison is None
    with pytest.raises(ValueError, match="identity"):
        repository.select("../../report.json", "none")
    path = workspace.kind_directory(GenerationKind.ANALYSIS) / identities[0] / "report.json"
    path.write_text("{}")
    with pytest.raises(OSError):
        repository.select(identities[0], "current")


def test_workspace_run_picker_in_browser(ordinary_report, saved_runs) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    workspace, manifests = saved_runs
    server = create_server(
        ReportViewService(ordinary_report), workspace=workspace.root, token="runs-test"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=runs-test")
                page.locator(".cards").wait_for()
                page.get_by_role("button", name="Saved runs", exact=True).click()
                payload = canonical_json(ordinary_report).encode()
                added = commit_generation(
                    workspace,
                    kind=GenerationKind.ANALYSIS,
                    artifacts={"report.json": payload},
                    command=("scan",),
                    interpreter_path="/python",
                    portable_identity_sha256=hashlib.sha256(payload).hexdigest(),
                )
                assert (
                    page.locator(f'#baseline-run option[value="{added.generation_id}"]').count()
                    == 0
                )
                page.get_by_role("button", name="Refresh saved runs", exact=True).click()
                page.locator(f'#baseline-run option[value="{added.generation_id}"]').wait_for(
                    state="attached"
                )
                page.locator("#baseline-run").select_option(manifests[0].generation_id)
                page.locator("#candidate-run").select_option(manifests[1].generation_id)
                page.get_by_role("button", name="Open selected runs", exact=True).click()
                page.get_by_role("heading", name="No canonical changes", exact=True).wait_for()
                reveal_navigation(page, "Comparison").click()
                page.get_by_text("Evidence completeness:", exact=False).wait_for()
                assert page.get_by_role("button", name="Next", exact=True).is_disabled()
                reveal_navigation(page, "Modules").click()
                page.get_by_role("columnheader", name="Module", exact=True).wait_for()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_catalog_refresh_is_atomic_and_preserves_loaded_snapshots(
    ordinary_report, saved_runs
) -> None:
    workspace, manifests = saved_runs
    repository = RunRepository(workspace.root, ReportViewService(ordinary_report))
    loaded = repository.select(manifests[0].generation_id, "none")
    payload = canonical_json(ordinary_report).encode()
    added = commit_generation(
        workspace,
        kind=GenerationKind.ANALYSIS,
        artifacts={"report.json": payload},
        command=("scan",),
        interpreter_path="/python",
        portable_identity_sha256=hashlib.sha256(payload).hexdigest(),
    )
    assert len(repository.catalog().records) == 2
    assert len(repository.refresh().records) == 3
    assert repository.select(manifests[0].generation_id, "none") is loaded
    before = repository.catalog()
    path = workspace.kind_directory(GenerationKind.ANALYSIS) / added.generation_id / "report.json"
    path.write_text("{}")
    with pytest.raises(OSError):
        repository.refresh()
    assert repository.catalog() == before
    assert repository.select(manifests[0].generation_id, "none") is loaded


def test_refresh_excludes_pruned_runs_but_keeps_loaded_snapshot(
    ordinary_report, saved_runs
) -> None:
    workspace, manifests = saved_runs
    repository = RunRepository(workspace.root, ReportViewService(ordinary_report))
    loaded = repository.select(manifests[0].generation_id, "none")
    prune_generations(workspace, kind=GenerationKind.ANALYSIS, keep_older=0)
    catalog = repository.refresh()
    assert len(catalog.records) == 1
    assert catalog.records[0].generation_id == manifests[1].generation_id
    assert repository.select(manifests[0].generation_id, "none") is loaded
    with pytest.raises(ValueError):
        repository.select(manifests[0].generation_id, "current")
