from __future__ import annotations

import asyncio
import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import AnalysisConfig
from code_analysis.models import SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projection_models import ProjectionCollapse
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.service import scan
from code_analysis.static_graph_browse import StaticGraphRequest
from code_analysis.ui_handoff import WebHandoffSelection
from code_analysis.web_server import _projection_request, create_server
from code_analysis.web_service import ReportViewService


@pytest.fixture(scope="module")
def structure_service():
    return ReportViewService(
        scan(
            discover_context(
                Path(__file__).parent / "fixtures" / "structure_project", AnalysisConfig()
            )
        )
    )


def test_graph_handoff_preserves_named_strict_selection(structure_service):
    graph = build_projection(structure_service.primary, ProjectionName.STRUCTURE_LIFECYCLE_GRAPH)
    request = StaticGraphRequest(
        name=ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
        selected_ids=(graph.nodes[0].node_id,),
        languages=(SourceLanguage.PYTHON,),
        collapse=ProjectionCollapse.MODULE,
        max_records=30,
        max_nodes=12,
        max_edges=0,
    )
    selection = WebHandoffSelection(
        report_sha256=structure_service.bootstrap().reports[0].report_sha256,
        kind="projection",
        graph_request=request,
    )
    selection.validate_report(structure_service)
    fields = parse_qs(urlsplit(selection.url("http://localhost/?token=owned")).fragment)
    assert StaticGraphRequest.model_validate_json(fields["graph_request"][0]) == request
    assert json.loads(fields["graph_request"][0])["max_edges"] == 0
    bad = selection.model_copy(
        update={"graph_request": request.model_copy(update={"selected_ids": ("missing",)})}
    )
    with pytest.raises(ValueError):
        bad.validate_report(structure_service)


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "projection"},
        {"kind": "report", "graph_request": {"name": "structure-lifecycle-graph"}},
        {
            "kind": "projection",
            "identity": "extra",
            "graph_request": {"name": "structure-lifecycle-graph"},
        },
        {
            "kind": "projection",
            "graph_request": {"name": "structure-lifecycle-graph", "max_edges": True},
        },
    ],
)
def test_inconsistent_graph_handoff_is_rejected(fields):
    with pytest.raises(ValidationError):
        WebHandoffSelection.model_validate_json(json.dumps({"report_sha256": "0" * 64, **fields}))


def test_projection_http_selection_retains_multiple_languages_and_zero_edges():
    request = _projection_request({"language": ["python", "javascript"], "max_edges": ["0"]})
    assert request.languages == (SourceLanguage.JAVASCRIPT, SourceLanguage.PYTHON)
    assert request.max_edges == 0
    with pytest.raises(ValueError):
        _projection_request({"language": ["python", "invented"]})


def test_graph_handoff_endpoint_is_authenticated_strict_and_report_bound(structure_service):
    selection = WebHandoffSelection(
        report_sha256=structure_service.bootstrap().reports[0].report_sha256,
        kind="projection",
        graph_request=StaticGraphRequest(name=ProjectionName.STRUCTURE_LIFECYCLE_GRAPH),
    )
    server = create_server(structure_service, token="graph-handoff-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = "/api/handoff?" + urlsplit(selection.url("http://localhost/")).fragment
    connection = HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        for request_path, status in (
            (path, 200),
            (path + "&unexpected=yes", 400),
            (path.replace(selection.report_sha256, "0" * 64), 400),
            (path + "&graph_request={}", 400),
        ):
            connection.request(
                "GET", request_path, headers={"X-Code-Analysis-Token": "graph-handoff-test"}
            )
            response = connection.getresponse()
            assert response.status == status
            payload = json.loads(response.read())
            if status == 200:
                assert payload == selection.model_dump(mode="json")
    finally:
        connection.close()
        server.shutdown()
        thread.join(5)
        server.server_close()


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
def test_terminal_handoff_carries_applied_graph_and_rejects_staged_controls(
    structure_service, tmp_path, size
):
    pytest.importorskip("textual")
    from textual.widgets import Input

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_handoff import WebHandoffDialog
    from code_analysis.tui_static_graphs import StaticGraphBrowser

    destination = tmp_path / "index"
    manifest = build_browse_index(structure_service, destination)
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)

    async def exercise():
        app = AnalysisTerminal(index)
        async with app.run_test(size=size) as pilot:
            screen = StaticGraphBrowser(index, app.evidence_closed)
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen.languages = (SourceLanguage.PYTHON,)
            screen.collapse = ProjectionCollapse.MODULE
            screen.query_one("#static-graph-edges", Input).value = "0"
            screen.apply()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.graph is not None
            # Choose an original identity before applying grouping.
            original = index.static_graph(StaticGraphRequest(name=screen.request.name))
            selected = original.nodes[0].node_id
            screen.query_one("#static-graph-selected", Input).value = selected
            screen.apply()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("ctrl+w")
            await pilot.pause()
            assert isinstance(app.screen, WebHandoffDialog)
            assert app.screen.selection.graph_request == screen.request
            assert app.screen.selection.graph_request.selected_ids == (selected,)
            assert app.screen.selection.report_sha256 == manifest.report_sha256
            await pilot.press("escape")
            await pilot.pause()
            screen.query_one("#static-graph-nodes", Input).value = "2"
            await pilot.pause()
            await pilot.press("ctrl+w")
            await pilot.pause()
            assert app.screen is screen

    asyncio.run(exercise())


def test_handoff_actions_recheck_input_and_reprepare_clears_old_url(
    structure_service, tmp_path, monkeypatch
):
    pytest.importorskip("textual")
    from textual.widgets import Button, Input, TextArea

    from code_analysis.artifact_viewer import ViewerCommand
    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_handoff import WebHandoffDialog

    destination = tmp_path / "index"
    manifest = build_browse_index(structure_service, destination)
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    copies, launches = [], []
    monkeypatch.setattr(
        "code_analysis.tui_handoff.launch_display", lambda *args: launches.append(args)
    )

    class Owner:
        fail = False

        def open(self, report, selection):
            if self.fail:
                raise ValueError("changed report")
            return selection.url("http://127.0.0.1:8123/?token=synthetic")

    async def exercise():
        app = AnalysisTerminal(index)
        monkeypatch.setattr(app, "copy_to_clipboard", copies.append)
        owner = Owner()
        async with app.run_test() as pilot:
            dialog = WebHandoffDialog(
                owner,
                WebHandoffSelection(report_sha256=manifest.report_sha256),
                ViewerCommand(arguments=("/bin/browser",)),
            )
            app.push_screen(dialog)
            await pilot.pause()
            field = dialog.query_one("#handoff-report", Input)
            field.value = "first.json"
            await pilot.pause()
            dialog.prepare()
            await app.workers.wait_for_complete()
            assert dialog.url is not None
            field.value = "second.json"
            # No event-loop yield: Input.Changed has not invalidated the prepared URL yet.
            dialog.on_button_pressed(Button.Pressed(dialog.query_one("#handoff-copy", Button)))
            assert not copies
            assert not dialog._prepared_input_matches()
            dialog.open_browser()
            await app.workers.wait_for_complete()
            assert not launches
            dialog.prepare()
            await app.workers.wait_for_complete()
            assert dialog.url is not None
            owner.fail = True
            dialog.prepare()
            assert dialog.url is None
            assert dialog.query_one("#handoff-url", TextArea).text == ""
            assert dialog.query_one("#handoff-copy", Button).disabled
            await app.workers.wait_for_complete()
            assert dialog.url is None
            assert dialog.query_one("#handoff-open", Button).disabled

    asyncio.run(exercise())
