from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest
from browser_navigation import reveal_navigation

from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness, SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projection_models import EdgeResolution, ProjectionCollapse
from code_analysis.projections import ProjectionBuildError, ProjectionName, build_projection
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.service import scan
from code_analysis.ui_navigation import NavigationUnavailable, navigation_page


@pytest.fixture
def saved(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(Path(__file__).parent / "fixtures" / "ordinary", source)
    report = load_json(render_json(scan(discover_context(source, AnalysisConfig())), compact=True))
    shutil.rmtree(source)
    return report


def test_exact_recorded_name_has_declaration_navigation_not_callsite(saved):
    graph = build_projection(saved, ProjectionName.LEXICAL_CALL_GRAPH)
    subjects = {item.subject_id: item for item in saved.subjects}
    edge = graph.edges[0]
    assert edge.resolution is EdgeResolution.RESOLVED
    assert subjects[edge.source_id].logical_name == "alpha.a:Outer.method"
    assert subjects[edge.target_id].logical_name == "alpha.a:Outer.method.nested"
    assert edge.evidence_ids
    assert "not call sites" in graph.title
    assert "Declaration evidence is not call-site evidence" in " ".join(graph.limitations)
    assert navigation_page(saved, edge.source_id).records
    assert graph == build_projection(saved, ProjectionName.LEXICAL_CALL_GRAPH)
    for fmt in (
        ProjectionFormat.JSON_COMPACT,
        ProjectionFormat.MARKDOWN,
        ProjectionFormat.DOT,
        ProjectionFormat.MERMAID,
        ProjectionFormat.HTML,
    ):
        assert render_projection(graph, fmt) == render_projection(
            build_projection(saved, ProjectionName.LEXICAL_CALL_GRAPH), fmt
        )


def test_unmatched_names_are_scoped_per_caller_not_fuzzy_resolved(saved):
    profiles = tuple(
        profile.model_copy(
            update={"callees": ("nested", "unknown.send"), "unresolved_call_count": 2}
        )
        for profile in saved.function_profiles[:2]
    )
    report = saved.model_copy(update={"function_profiles": profiles})
    graph = build_projection(report, ProjectionName.LEXICAL_CALL_GRAPH)
    unknown = [node for node in graph.nodes if node.kind == "unresolved-callee"]
    assert len(unknown) == 4 and len(graph.edges) == 4
    assert len({node.node_id for node in unknown}) == 4
    assert all(edge.resolution is EdgeResolution.UNRESOLVED for edge in graph.edges)
    assert all(not node.evidence_ids and node.group_id is None for node in unknown)
    assert "4 unresolved calls in aggregate" in " ".join(graph.limitations)
    with pytest.raises(NavigationUnavailable):
        navigation_page(report, unknown[0].node_id)


def test_ambiguous_and_cross_language_names_never_choose_arbitrary_owner(saved):
    profile = next(profile for profile in saved.function_profiles if profile.callees)
    target = next(
        subject for subject in saved.subjects if subject.logical_name == profile.callees[0]
    )
    duplicate = target.model_copy(update={"subject_id": "subject-" + "e" * 20})
    report = saved.model_copy(update={"subjects": (*saved.subjects, duplicate)})
    graph = build_projection(report, ProjectionName.LEXICAL_CALL_GRAPH)
    assert graph.edges[0].resolution is EdgeResolution.AMBIGUOUS
    different_language = target.model_copy(update={"language": SourceLanguage.JAVASCRIPT})
    report = saved.model_copy(
        update={
            "function_profiles": tuple(
                item for item in saved.function_profiles if item.subject_id != target.subject_id
            ),
            "subjects": tuple(
                different_language if subject.subject_id == target.subject_id else subject
                for subject in saved.subjects
            ),
        }
    )
    graph = build_projection(report, ProjectionName.LEXICAL_CALL_GRAPH)
    assert graph.edges[0].resolution is EdgeResolution.UNRESOLVED


def test_self_edge_and_multiple_names_have_no_occurrence_count(saved):
    profile = saved.function_profiles[0]
    subject = next(item for item in saved.subjects if item.subject_id == profile.subject_id)
    report = saved.model_copy(
        update={
            "function_profiles": (profile.model_copy(update={"callees": (subject.logical_name,)}),)
        }
    )
    graph = build_projection(report, ProjectionName.LEXICAL_CALL_GRAPH)
    assert len(graph.edges) == 1 and graph.edges[0].source_id == graph.edges[0].target_id
    assert "zero does not prove complete resolution" in " ".join(graph.limitations)


@pytest.mark.parametrize("collapse", tuple(ProjectionCollapse))
def test_selection_collapse_bounds_and_partiality(saved, collapse):
    graph = build_projection(saved, ProjectionName.LEXICAL_CALL_GRAPH)
    selected = graph.edges[0].source_id
    focused = build_projection(
        saved, ProjectionName.LEXICAL_CALL_GRAPH, selected_ids=(selected,), collapse=collapse
    )
    assert not focused.truncated
    assert focused.selection.selected_ids == (selected,)
    bounded = build_projection(saved, ProjectionName.LEXICAL_CALL_GRAPH, max_records=1)
    assert bounded.truncated and bounded.completeness is Completeness.PARTIAL
    with pytest.raises(ProjectionBuildError):
        build_projection(saved, ProjectionName.LEXICAL_CALL_GRAPH, selected_ids=("missing",))
    with pytest.raises(ProjectionBuildError):
        build_projection(
            saved, ProjectionName.LEXICAL_CALL_GRAPH, languages=(SourceLanguage.JAVASCRIPT,)
        )


def test_missing_evidence_is_not_successful_empty_graph(saved):
    with pytest.raises(ProjectionBuildError, match="requires recorded function profiles"):
        build_projection(
            saved.model_copy(update={"function_profiles": ()}), ProjectionName.LEXICAL_CALL_GRAPH
        )
    failed = saved.model_copy(update={"completeness": Completeness.FAILED})
    assert (
        build_projection(failed, ProjectionName.LEXICAL_CALL_GRAPH).completeness
        is Completeness.FAILED
    )


def test_browser_lexical_graph_discloses_unknown_navigation(saved):
    playwright = pytest.importorskip("playwright.sync_api")
    from code_analysis.web_server import create_server
    from code_analysis.web_service import ReportViewService

    profiles = tuple(
        profile.model_copy(update={"callees": ("unknown.send",)})
        for profile in saved.function_profiles[:1]
    )
    report = saved.model_copy(update={"function_profiles": profiles})
    server = create_server(ReportViewService(report), token="lexical-test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_port}/?token=lexical-test")
                reveal_navigation(page, "Explore evidence").click()
                page.get_by_label("Evidence view", exact=True).select_option("lexical-call-graph")
                page.get_by_role("button", name="Show evidence", exact=True).click()
                playwright.expect(page.locator("#projection-result h2")).to_have_text(
                    "Recorded lexical callee relationships (not call sites)"
                )
                page.get_by_role("button", name="Select unknown.send", exact=True).click()
                playwright.expect(page.locator("#projection-node-status")).to_contain_text(
                    "No source-navigation mapping is recorded"
                )
                assert not page.locator("#evidence-dialog").is_visible()
                assert page.get_by_role("button", name="Download DOT", exact=True).is_visible()
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()


def test_inconsistent_profile_language_cannot_be_displayed_as_valid(saved):
    profile = saved.function_profiles[0].model_copy(update={"language": SourceLanguage.JAVASCRIPT})
    with pytest.raises(ProjectionBuildError, match="language disagrees"):
        build_projection(
            saved.model_copy(update={"function_profiles": (profile,)}),
            ProjectionName.LEXICAL_CALL_GRAPH,
        )


def test_duplicate_profile_owner_cannot_silently_overwrite_evidence(saved):
    from code_analysis.report_models import AnalysisReport

    duplicate = saved.function_profiles[0].model_copy(
        update={"profile_id": "function-profile-" + "f" * 20}
    )
    profiles = tuple(
        sorted((*saved.function_profiles, duplicate), key=lambda item: item.profile_id)
    )
    admission = saved.signal_quality.commonality_admission.model_copy(
        update={"profile_count": len(profiles)}
    )
    report = saved.model_copy(
        update={
            "function_profiles": profiles,
            "summary": saved.summary.model_copy(update={"function_profiles": len(profiles)}),
            "signal_quality": saved.signal_quality.model_copy(
                update={"commonality_admission": admission}
            ),
        }
    )
    validated = AnalysisReport.model_validate(report.model_dump())
    with pytest.raises(ProjectionBuildError, match="duplicate function profile owners"):
        build_projection(validated, ProjectionName.LEXICAL_CALL_GRAPH)


def test_function_section_partiality_survives_index_preparation(saved):
    import sqlite3

    from code_analysis.report_models import SectionState
    from code_analysis.static_graph_browse import (
        StaticGraphRequest,
        read_static_graph,
        write_static_graphs,
    )

    report = saved.model_copy(
        update={
            "completeness": Completeness.PARTIAL,
            "section_coverage": tuple(
                (
                    item.model_copy(update={"state": SectionState.PARTIAL})
                    if item.section == "function-commonality"
                    else item
                )
                for item in saved.section_coverage
            ),
        }
    )
    with sqlite3.connect(":memory:") as connection:
        summaries = write_static_graphs(connection, report)
        graph = read_static_graph(
            connection, summaries, StaticGraphRequest(name=ProjectionName.LEXICAL_CALL_GRAPH)
        )
    assert graph.completeness is Completeness.PARTIAL
    assert "Function-profile section state: partial." in graph.limitations
    assert not graph.truncated
