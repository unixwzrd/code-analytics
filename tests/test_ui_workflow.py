"""Workflow catalog and readiness operate only on retained evidence snapshots."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.collection_commands import CollectionContext
from code_analysis.models import Completeness, ProviderResult, ProviderState
from code_analysis.report_models import SectionState
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.ui_workflow import (
    WORKFLOW_GROUPS,
    WORKFLOW_VIEWS,
    WorkflowOverview,
    WorkflowReadiness,
    build_workflow,
)
from code_analysis.web_service import ReportViewService


def context(*, enabled=False):
    return CollectionContext(
        enabled=enabled,
        root="/not-inspected" if enabled else None,
        host="localhost",
        interpreter="python",
        report_available=False,
    )


def views(workflow):
    return {view.view_id: view for view in workflow.views}


def with_section(report, name, state, **updates):
    sections = tuple(
        (
            section.model_copy(update={"state": state, **updates})
            if section.section == name
            else section
        )
        for section in report.section_coverage
    )
    completeness = Completeness.FAILED if state is SectionState.FAILED else Completeness.PARTIAL
    # Keep intentional evidence-state fixtures local; model_copy allows independent
    # section readiness cases without rerunning collection or fabricating a report.
    return report.model_copy(update={"section_coverage": sections, "completeness": completeness})


def test_empty_catalog_and_workspace_connection():
    workflow = build_workflow(context(), None, None)
    assert workflow.groups == WORKFLOW_GROUPS
    assert len(workflow.views) == len(WORKFLOW_VIEWS) == 18
    assert len({view.view_id for view in workflow.views}) == 18
    assert {view.view_id for view in workflow.views} == {
        view for group in workflow.groups for view in group.view_ids
    }
    result = views(workflow)
    assert result["overview"].readiness.state == "available"
    assert result["runs"].readiness.state == "available"
    assert result["collection"].readiness.state == "missing"
    assert result["modules"].readiness.state == "missing"
    assert result["runtime"].readiness.state == "missing"
    assert workflow.overall_readiness.state == "missing"
    assert workflow.overall_readiness.reason == (
        "Collect or open static evidence or a saved Python profile."
    )
    assert workflow.source_access == "not-configured"
    assert workflow.actions[0].view_id == "runs"


def test_static_readiness_is_deterministic_and_does_not_mutate(ordinary_report):
    service = ReportViewService(ordinary_report)
    before = ordinary_report.model_dump_json()
    workflow = build_workflow(context(enabled=True), service, None, source_configured=True)
    assert workflow == build_workflow(context(enabled=True), service, None, source_configured=True)
    assert ordinary_report.model_dump_json() == before
    result = views(workflow)
    assert result["modules"].readiness.state == "available"
    assert result["browse"].readiness.state == "available"
    assert result["collection"].readiness.state == "available"
    assert result["comparison"].readiness.state == "missing"
    assert workflow.source_access == "configured-unverified"
    assert workflow.reports[0].completeness == ordinary_report.completeness
    assert WorkflowOverview.model_validate_json(workflow.model_dump_json()) == workflow
    assert views(build_workflow(context(), service, None))["browse"].readiness.state == "available"


@pytest.mark.parametrize(
    "state,expected",
    [
        (SectionState.DISABLED, "missing"),
        (SectionState.UNAVAILABLE, "missing"),
        (SectionState.PARTIAL, "partial"),
        (SectionState.TRUNCATED, "partial"),
        (SectionState.FAILED, "failed"),
        (SectionState.COMPLETE, "available"),
    ],
)
def test_required_section_states(ordinary_report, state, expected):
    report = with_section(
        ordinary_report, "contracts", state, truncated=1 if state is SectionState.TRUNCATED else 0
    )
    workflow = build_workflow(context(), ReportViewService(report), None)
    assert views(workflow)["contracts"].readiness.state == expected
    # Unrelated usable core evidence remains available.
    assert views(workflow)["modules"].readiness.state == "available"


def test_complete_empty_results_are_available(ordinary_report):
    report = ordinary_report.model_copy(update={"findings": (), "contracts": ()})
    workflow = build_workflow(context(), ReportViewService(report), None)
    assert views(workflow)["findings"].readiness.state == "available"
    assert views(workflow)["contracts"].readiness.state == "available"


def test_optional_failures_do_not_degrade_core(ordinary_report):
    provider = ProviderResult(
        provider_id="optional-tool",
        state=ProviderState.FAILED,
        duration_ms=0,
        completeness=Completeness.FAILED,
    )
    report = with_section(ordinary_report, "coverage", SectionState.FAILED, required=False)
    report = report.model_copy(update={"providers": (provider,)})
    workflow = build_workflow(context(), ReportViewService(report), None)
    readiness = views(workflow)["modules"].readiness
    assert readiness.state == "available"
    assert any("optional-tool" in issue for issue in readiness.optional_issues)
    assert any("coverage" in issue for issue in readiness.optional_issues)
    assert workflow.reports[0].completeness is Completeness.FAILED


def test_disabled_sections_do_not_degrade_overall_and_actions_are_usable(ordinary_report):
    report = ordinary_report.model_copy(
        update={
            "section_coverage": tuple(
                (
                    section.model_copy(
                        update={
                            "state": SectionState.DISABLED,
                            "parsed": 0,
                            "failed": 0,
                            "unresolved": 0,
                            "truncated": 0,
                            "diagnostic_codes": (),
                        }
                    )
                    if section.section != "r1-static-analysis"
                    else section
                )
                for section in ordinary_report.section_coverage
            )
        }
    )
    workflow = build_workflow(context(enabled=True), ReportViewService(report), None)
    assert workflow.overall_readiness.state == "available"
    assert views(workflow)["contracts"].readiness.state == "missing"
    assert views(workflow)["comparison"].next_view == "runs"
    assert {action.operation for action in workflow.actions} >= {
        "scan",
        "profile",
        "import-profile",
    }
    assert {action.view_id for action in workflow.actions} >= {"modules", "findings", "diagnostics"}
    failed = with_section(report, "r1-static-analysis", SectionState.FAILED)
    failed_workflow = build_workflow(context(), ReportViewService(failed), None)
    assert failed_workflow.overall_readiness.state == "failed"
    assert not any(action.view_id in ("modules", "findings") for action in failed_workflow.actions)


def test_absent_section_is_missing(ordinary_report):
    report = ordinary_report.model_copy(
        update={
            "section_coverage": tuple(
                section
                for section in ordinary_report.section_coverage
                if section.section != "contracts"
            )
        }
    )
    workflow = build_workflow(context(), ReportViewService(report), None)
    assert views(workflow)["contracts"].readiness.state == "missing"


def test_runtime_only_and_combined_evidence(ordinary_report):
    runtime = RuntimeViewService.load(
        Path(__file__).parent / "golden" / "phase4-profile-report.json"
    )
    only = build_workflow(context(), None, runtime)
    assert only.runtime.available
    assert views(only)["runtime"].readiness.state == runtime.profile.completeness.value.replace(
        "complete", "available"
    )
    assert views(only)["modules"].readiness.state == "missing"
    assert views(only)["providers"].readiness.state == "missing"
    assert views(only)["providers"].next_view == "runtime"
    assert views(only)["diagnostics"].next_view == "runtime"
    both = build_workflow(context(), ReportViewService(ordinary_report), runtime)
    assert both.reports and both.runtime.available
    assert views(both)["modules"].readiness.state == "available"


def test_profile_comparison_identity_is_explicit():
    from code_analysis.ui_handoff import canonical_profile_sha256

    path = Path(__file__).parent / "golden" / "phase4-profile-report.json"
    runtime = RuntimeViewService.load(path, path)
    workflow = build_workflow(context(), None, runtime)
    assert workflow.runtime.profile_report_sha256 == canonical_profile_sha256(runtime.profile)
    assert workflow.comparison_profile_report_sha256 == canonical_profile_sha256(runtime.comparison)


@pytest.mark.parametrize(
    "state,expected",
    [
        (SectionState.COMPLETE, "available"),
        (SectionState.PARTIAL, "partial"),
        (SectionState.FAILED, "failed"),
    ],
)
def test_comparison_requires_both_reports(ordinary_report, state, expected):
    candidate = with_section(ordinary_report, "r1-static-analysis", state)
    workflow = build_workflow(context(), ReportViewService(ordinary_report, candidate), None)
    assert len(workflow.reports) == 2
    assert views(workflow)["comparison"].readiness.state == expected


def test_models_are_strict_frozen_and_catalog_is_validated():
    workflow = build_workflow(context(), None, None)
    with pytest.raises(ValidationError):
        workflow.source_access = "configured-unverified"
    with pytest.raises(ValidationError):
        WorkflowReadiness(state="ready", reason="No such state")
    with pytest.raises(ValidationError):
        WorkflowReadiness(state="available", reason=42)
    with pytest.raises(ValidationError):
        WorkflowReadiness(state="available", reason="Valid", surprise=True)
    with pytest.raises(ValidationError):
        WorkflowOverview.model_validate(
            {**workflow.model_dump(), "views": (*workflow.views[:-1], workflow.views[0])}
        )
    with pytest.raises(ValidationError):
        WorkflowOverview.model_validate(
            {**workflow.model_dump(), "groups": (*workflow.groups[:4], workflow.groups[0])}
        )


@pytest.mark.parametrize("enabled,destination", [(False, "runs"), (True, "collection")])
def test_missing_views_route_to_available_corrective_destination(enabled, destination):
    result = views(build_workflow(context(enabled=enabled), None, None))
    assert result["modules"].next_view == destination
    assert result[destination].readiness.state == "available"
    assert result["comparison"].next_view == "runs"


def test_partial_and_failed_views_route_to_health(ordinary_report):
    for state in (SectionState.PARTIAL, SectionState.FAILED):
        report = with_section(ordinary_report, "contracts", state)
        result = views(build_workflow(context(), ReportViewService(report), None))
        assert result["contracts"].next_view == "diagnostics"
        assert result["diagnostics"].readiness.state == "available"


def test_comparison_retains_required_noncore_incompleteness(ordinary_report):
    candidate = with_section(ordinary_report, "contracts", SectionState.PARTIAL)
    workflow = build_workflow(context(), ReportViewService(ordinary_report, candidate), None)
    assert views(workflow)["comparison"].readiness.state == "partial"


@pytest.mark.parametrize(
    "completeness,expected", [(Completeness.PARTIAL, "partial"), (Completeness.FAILED, "failed")]
)
def test_runtime_incomplete_and_failed(completeness, expected):
    runtime = RuntimeViewService.load(
        Path(__file__).parent / "golden" / "phase4-profile-report.json"
    )
    runtime = RuntimeViewService(runtime.profile.model_copy(update={"completeness": completeness}))
    workflow = build_workflow(context(), None, runtime)
    assert views(workflow)["runtime"].readiness.state == expected
    assert workflow.overall_readiness.state == expected


def test_help_references_and_no_filesystem_side_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("workflow must not read source or help assets")

    with monkeypatch.context() as guarded:
        guarded.setattr(Path, "open", forbidden)
        guarded.setattr(Path, "stat", forbidden)
        workflow = build_workflow(context(enabled=True), None, None, source_configured=True)
    result = views(workflow)
    assert result["overview"].help_anchor == "your-first-investigation"
    assert result["explore"].help_document_id == "reports"
    assert result["explore"].help_anchor == "projection-families"
    assert result["browse"].help_anchor == "code-browsing-and-session-draft-editing"
