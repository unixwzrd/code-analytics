from __future__ import annotations

import asyncio

import pytest

from code_analysis.browse_export import export_browse_page, render_browse_artifact
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import AnalysisConfig
from code_analysis.dependency_graph import DependencyGraphRequest
from code_analysis.dependency_navigation import DependencyRequest
from code_analysis.project import discover_context
from code_analysis.reports.projection_views import ProjectionFormat
from code_analysis.service import scan
from code_analysis.web_service import ReportViewService


@pytest.fixture
def exported_dependency_index(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    sources = {
        "__init__.py": "",
        "a.py": "from . import b\n",
        "b.py": "from . import c\n",
        "c.py": "value = 1\n",
    }
    for name, content in sources.items():
        (package / name).write_text(content)
    report = scan(discover_context(tmp_path, AnalysisConfig()))
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), destination)
    for name in sources:
        (package / name).unlink()
    package.rmdir()
    return BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def test_static_dependency_graph_export_is_source_free_and_partial(exported_dependency_index):
    request = DependencyGraphRequest(
        investigation=DependencyRequest(source="pkg.a", target="pkg.c"), max_nodes=2
    )
    source = exported_dependency_index.bootstrap().reports[0]
    path, projection = export_browse_page(
        exported_dependency_index, request, "dependency-path", ProjectionFormat.JSON_COMPACT
    )
    assert projection.inputs[0].report_sha256 == source.report_sha256
    assert projection.truncated and projection.completeness.value == "partial"
    assert projection.original_node_count == 3 and projection.omitted_node_count == 1
    assert path.read_bytes() == render_browse_artifact(projection, ProjectionFormat.JSON_COMPACT)
    with pytest.raises(FileExistsError):
        export_browse_page(
            exported_dependency_index, request, "dependency-path", ProjectionFormat.JSON_COMPACT
        )


@pytest.mark.parametrize(
    "output_format",
    (
        ProjectionFormat.JSON_COMPACT,
        ProjectionFormat.MARKDOWN,
        ProjectionFormat.DOT,
        ProjectionFormat.MERMAID,
        ProjectionFormat.SVG,
        ProjectionFormat.PNG,
        ProjectionFormat.HTML,
    ),
)
def test_static_dependency_graph_export_uses_shared_graph_renderers(
    exported_dependency_index, output_format
):
    request = DependencyGraphRequest(
        investigation=DependencyRequest(source="pkg.a", target="pkg.c")
    )
    path, projection = export_browse_page(
        exported_dependency_index, request, f"dependency-{output_format.value}", output_format
    )
    assert path.read_bytes() == render_browse_artifact(projection, output_format)


def test_dependency_browser_exposes_graph_export_at_80x24(exported_dependency_index):
    pytest.importorskip("textual")
    from textual.widgets import Button, Checkbox, Input, Static

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_dependencies import DependencyBrowser
    from code_analysis.tui_export import ExportPage
    from code_analysis.tui_modules import ModuleBrowser

    async def exercise():
        app = AnalysisTerminal(exported_dependency_index)
        async with app.run_test(size=(80, 24)) as pilot:
            browser = ModuleBrowser(exported_dependency_index, "pkg.a", app.evidence_closed)
            app.push_screen(browser)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.click("#module-dependencies")
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, DependencyBrowser)
            screen = app.screen
            assert not screen.query_one("#dependency-export", Button).disabled
            screen.query_one("#dependency-nodes", Input).value = "2"
            await pilot.pause()
            assert screen.query_one("#dependency-export", Button).disabled
            assert "press Apply" in str(screen.query_one("#dependency-status", Static).content)
            await pilot.click("#dependency-apply")
            await app.workers.wait_for_complete()
            assert not screen.query_one("#dependency-export", Button).disabled
            await pilot.click("#dependency-export")
            assert isinstance(app.screen, ExportPage)
            export = app.screen
            assert export.query_one("#export-filtered", Checkbox).disabled
            assert "100 nodes and 250 edges" in str(
                export.query_one("#export-status", Static).content
            )
            export.query_one("#export-name", Input).value = "terminal-dependency"
            await pilot.click("#export-write")
            await app.workers.wait_for_complete()
            assert export.exported_path is not None
            await pilot.press("escape", "ctrl+c")

    asyncio.run(exercise())
