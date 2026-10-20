from __future__ import annotations

import asyncio
import errno
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.browse_export import export_browse_page, render_browse_artifact
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.profile_models import FrameClassification, ProfileReport
from code_analysis.projections.runtime import build_runtime_graph
from code_analysis.reports.projection_views import ProjectionFormat
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_models import RuntimeSelection
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService


@pytest.fixture
def graph_index(ordinary_report, tmp_path):
    profile = RuntimeViewService.load(
        Path(__file__).parent / "golden" / "phase4-profile-report.json"
    ).profile
    root = tmp_path / "index"
    manifest = build_browse_index(
        ReportViewService(ordinary_report), root, runtime=RuntimeViewService(profile)
    )
    return profile, BrowseIndex.open(root, report_sha256=manifest.report_sha256)


@pytest.mark.parametrize(
    "nodes,edges,project", [(1, 1, False), (2, 1, False), (100, 250, False), (100, 250, True)]
)
def test_indexed_graph_equals_saved_selector(graph_index, monkeypatch, nodes, edges, project):
    profile, index = graph_index
    request = RuntimeGraphRequest(max_nodes=nodes, max_edges=edges, project_only=project)
    expected = build_runtime_graph(profile, RuntimeSelection(**request.model_dump()))
    monkeypatch.setattr(RuntimeViewService, "load", lambda *a: pytest.fail("profile reload"))
    graph = index.runtime_graph(request)
    assert graph == expected
    assert graph.original_node_count == len(profile.frames)
    assert graph.omitted_node_count == len(profile.frames) - len(graph.nodes)
    assert graph.omitted_edge_count == len(profile.runtime_edges) - len(graph.edges)


@pytest.mark.parametrize(
    "format_",
    [
        ProjectionFormat.JSON_COMPACT,
        ProjectionFormat.MARKDOWN,
        ProjectionFormat.DOT,
        ProjectionFormat.MERMAID,
        ProjectionFormat.SVG,
        ProjectionFormat.PNG,
        ProjectionFormat.HTML,
    ],
)
def test_graph_artifacts_reuse_renderers(graph_index, format_):
    _, index = graph_index
    request = RuntimeGraphRequest()
    graph = index.runtime_graph(request)
    path, exported = export_browse_page(index, request, "calls", format_)
    assert exported == graph
    assert path.read_bytes() == render_browse_artifact(graph, format_)
    if format_ is ProjectionFormat.PNG:
        assert path.read_bytes().startswith(b"\x89PNG")
    else:
        assert "/Users/" not in path.read_text()
    with pytest.raises(FileExistsError):
        export_browse_page(index, request, "calls", format_)


def test_graph_bounds_missing_renderer_and_changed_index(graph_index, monkeypatch):
    from code_analysis.reports import projection_views

    _, index = graph_index
    for payload in (
        {"max_nodes": True},
        {"max_nodes": 101},
        {"max_edges": 251},
        {"project_only": 1},
        {"extra": 1},
    ):
        with pytest.raises(ValidationError):
            RuntimeGraphRequest.model_validate(payload)
    monkeypatch.setattr(projection_views.shutil, "which", lambda *a: None)
    with pytest.raises(OSError) as error:
        export_browse_page(index, RuntimeGraphRequest(), "absent", ProjectionFormat.SVG)
    assert error.value.errno == errno.ENOENT
    assert not (index.database.parent / "exports").exists()
    index.database.touch()
    with pytest.raises(ValueError, match="changed"):
        index.runtime_graph(RuntimeGraphRequest())


def test_graph_renderer_timeout_does_not_publish(graph_index, monkeypatch):
    from code_analysis.reports import projection_views

    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(projection_views.subprocess, "run", timeout)
    with pytest.raises(OSError) as error:
        export_browse_page(graph_index[1], RuntimeGraphRequest(), "timeout", ProjectionFormat.PNG)
    assert error.value.errno == errno.ETIMEDOUT
    assert not (graph_index[1].database.parent / "exports").exists()


def test_sparse_graph_ranking_ties_recursion_and_project_filter(ordinary_report, tmp_path):
    original = RuntimeViewService.load(
        Path(__file__).parent / "golden" / "phase4-profile-report.json"
    ).profile
    frames = tuple(
        original.frames[0].model_copy(
            update={
                "frame_id": f"profile-frame-{number:020x}",
                "function": f"function_{number}",
                "cumulative_seconds": float(number % 5 + 1),
                "classification": (
                    FrameClassification.PROJECT if number % 2 else FrameClassification.ENVIRONMENT
                ),
            }
        )
        for number in range(201)
    )
    edges = tuple(
        original.runtime_edges[0].model_copy(
            update={
                "runtime_edge_id": f"runtime-edge-{number:020x}",
                "caller_frame_id": frame.frame_id,
                "callee_frame_id": frame.frame_id,
                "total_calls": number % 3 + 1,
                "primitive_calls": 1,
            }
        )
        for number, frame in enumerate(frames)
    )
    profile = ProfileReport.model_validate(
        {
            **original.model_dump(),
            "frames": frames,
            "runtime_edges": edges,
            "statistics": (),
        }
    )
    root = tmp_path / "index"
    manifest = build_browse_index(
        ReportViewService(ordinary_report), root, runtime=RuntimeViewService(profile)
    )
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    for project in (False, True):
        request = RuntimeGraphRequest(max_nodes=100, max_edges=7, project_only=project)
        graph = index.runtime_graph(request)
        assert graph == build_runtime_graph(profile, RuntimeSelection(**request.model_dump()))
        assert graph.truncated and graph.omitted_node_count == 101
        assert graph.omitted_edge_count == 194
        assert all(edge.source_id == edge.target_id for edge in graph.edges)


def test_graph_terminal_selection_export_and_bounds(graph_index):
    pytest.importorskip("textual")
    from textual.widgets import Input, Select

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_export import ExportPage
    from code_analysis.tui_runtime import RuntimeBrowser
    from code_analysis.tui_runtime_graph import RuntimeGraphBrowser

    _, index = graph_index

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=(180, 48)) as pilot:
            await pilot.press("ctrl+r")
            await app.workers.wait_for_complete()
            await pilot.click("#runtime-graph")
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, RuntimeGraphBrowser)
            assert len(screen.graph.nodes) == 2
            screen.query_one("#runtime-graph-nodes", Input).value = "1"
            await pilot.click("#runtime-graph-apply")
            await app.workers.wait_for_complete()
            assert screen.graph.truncated and len(screen.graph.nodes) == 1
            node = screen.graph.nodes[0].node_id
            await pilot.press("ctrl+e")
            assert isinstance(app.screen, ExportPage)
            export = app.screen
            export.query_one("#export-name", Input).value = "runtime-calls"
            export.query_one("#export-format", Select).value = "svg"
            await pilot.click("#export-write")
            await app.workers.wait_for_complete()
            assert export.exported_path is not None
            assert "<svg" in export.exported_path.read_text()
            await pilot.press("escape")
            screen.query_one("#runtime-graph-table").focus()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, RuntimeBrowser)
            assert app.screen.request.frame_id == node
            assert app.screen.request.section.value == "calls"

    asyncio.run(exercise())
