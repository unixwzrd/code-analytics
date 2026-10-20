from __future__ import annotations

import asyncio
import shutil
import sqlite3
from pathlib import Path

import pytest

import code_analysis.static_graph_browse as static_graph_browse
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import AnalysisConfig
from code_analysis.models import SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.service import scan
from code_analysis.static_graph_browse import StaticGraphRequest
from code_analysis.ui_navigation import NavigationUnavailable, navigation_page
from code_analysis.web_service import ReportViewService


@pytest.fixture
def indexed_lexical_calls(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "calls.py").write_text(
        "def target():\n    return 1\n\ndef caller():\n    return target()\n"
    )
    report = scan(discover_context(source, AnalysisConfig()))
    caller = next(item for item in report.subjects if item.logical_name == "calls:caller")
    target = next(item for item in report.subjects if item.logical_name == "calls:target")
    profiles = tuple(
        (
            item.model_copy(update={"callees": ("calls:not_recorded",)})
            if item.subject_id == target.subject_id
            else item
        )
        for item in report.function_profiles
    )
    report = report.model_copy(update={"function_profiles": profiles})
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), destination)
    shutil.rmtree(source)
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    return report, caller.subject_id, index


def test_indexed_lexical_calls_match_saved_evidence_focus_language_and_bounds(
    indexed_lexical_calls,
):
    report, caller_id, index = indexed_lexical_calls
    name = ProjectionName.LEXICAL_CALL_GRAPH
    request = StaticGraphRequest(name=name)
    actual = index.static_graph(request)
    expected = build_projection(report, name, max_records=500, max_nodes=60, max_edges=120)
    assert actual.nodes == expected.nodes
    assert actual.edges == expected.edges

    unknown = next(item for item in actual.nodes if item.node_id.startswith("lexical-target-"))
    assert unknown.kind == "unresolved-callee"
    focused = index.static_graph(StaticGraphRequest(name=name, selected_ids=(caller_id,)))
    expected_focus = build_projection(
        report,
        name,
        selected_ids=(caller_id,),
        max_records=500,
        max_nodes=60,
        max_edges=120,
    )
    assert focused.nodes == expected_focus.nodes
    assert focused.edges == expected_focus.edges
    assert focused.clusters == expected_focus.clusters

    bounded = index.static_graph(
        StaticGraphRequest(name=name, languages=(SourceLanguage.PYTHON,), max_nodes=1, max_edges=0)
    )
    expected_bounded = build_projection(
        report,
        name,
        languages=(SourceLanguage.PYTHON,),
        max_records=500,
        max_nodes=1,
        max_edges=0,
    )
    assert bounded.nodes == expected_bounded.nodes
    assert bounded.edges == expected_bounded.edges
    assert bounded.clusters == expected_bounded.clusters

    connection = sqlite3.connect(index.database)
    try:
        memberships = dict(
            connection.execute(
                "SELECT node_id, languages FROM static_graph_nodes WHERE name = ?",
                (name.value,),
            )
        )
    finally:
        connection.close()
    assert memberships[caller_id] == '["python"]'
    assert memberships[unknown.node_id] == '["python"]'


def test_indexed_lexical_calls_navigation_is_source_free_for_subjects_only(
    indexed_lexical_calls, monkeypatch
):
    report, caller_id, index = indexed_lexical_calls
    request = StaticGraphRequest(name=ProjectionName.LEXICAL_CALL_GRAPH)
    graph = index.static_graph(request)
    unknown = next(item for item in graph.nodes if item.node_id.startswith("lexical-target-"))

    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError),
    )
    assert index.graph_navigation(request, caller_id) == navigation_page(report, caller_id)
    with pytest.raises(NavigationUnavailable, match="no recorded source-navigation mapping"):
        index.graph_navigation(request, unknown.node_id)


def test_lexical_memberships_cover_targets_when_preparation_omits_every_edge(
    indexed_lexical_calls,
):
    report, caller_id, _index = indexed_lexical_calls
    target = next(item for item in report.subjects if item.logical_name == "calls:target")
    caller_profile = next(item for item in report.function_profiles if item.subject_id == caller_id)
    reduced = report.model_copy(update={"function_profiles": (caller_profile,)})
    projection = build_projection(
        reduced,
        ProjectionName.LEXICAL_CALL_GRAPH,
        max_edges=0,
    )
    assert target.subject_id in {item.node_id for item in projection.nodes}
    assert not projection.edges
    node_languages, edge_languages = static_graph_browse._language_memberships(
        reduced,
        ProjectionName.LEXICAL_CALL_GRAPH,
        projection,
    )
    assert node_languages[target.subject_id] == {SourceLanguage.PYTHON}
    assert not edge_languages


def test_lexical_calls_without_profiles_are_an_explicit_unavailable_index_section(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "empty.py").write_text("value = 1\n")
    report = scan(discover_context(source, AnalysisConfig()))
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), destination)
    index = BrowseIndex.open(destination, report_sha256=manifest.report_sha256)
    summary = next(
        item
        for item in index.manifest.static_graphs
        if item.name is ProjectionName.LEXICAL_CALL_GRAPH
    )
    assert not summary.available
    assert summary.unavailable_reason == (
        "lexical-call-graph requires recorded function-profile evidence"
    )
    with pytest.raises(ValueError, match="requires recorded function-profile evidence"):
        index.static_graph(StaticGraphRequest(name=ProjectionName.LEXICAL_CALL_GRAPH))


def test_tui_lexical_calls_uses_the_indexed_graph_and_unavailable_state(indexed_lexical_calls):
    pytest.importorskip("textual")

    from code_analysis.tui_app import AnalysisTerminal
    from code_analysis.tui_static_graphs import StaticGraphBrowser

    _report, _caller_id, index = indexed_lexical_calls

    async def exercise() -> None:
        app = AnalysisTerminal(index)
        async with app.run_test(size=(100, 30)) as pilot:
            screen = StaticGraphBrowser(
                index, app.evidence_closed, initial_name=ProjectionName.LEXICAL_CALL_GRAPH
            )
            app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.graph == index.static_graph(screen.request)

    asyncio.run(exercise())
