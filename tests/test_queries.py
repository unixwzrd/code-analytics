from __future__ import annotations

import json

import pytest
from conftest import FIXTURES

from code_analysis.config import AnalysisConfig, load_project_config
from code_analysis.profiling import load_profile_json
from code_analysis.project import discover_context
from code_analysis.queries import (
    QueryBuildError,
    build_profile_query,
    build_query,
    render_query_json,
    render_query_markdown,
)
from code_analysis.query_models import QuerySelector, QuerySelectorKind
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.service import scan


def test_symbol_query_is_deterministic_portable_and_report_only(ordinary_report) -> None:
    report = load_json(render_json(ordinary_report))
    qualified_name = next(
        f"{module.name}:{symbol.qualified_name}"
        for module in report.modules
        for symbol in module.symbols
    )
    selector = QuerySelector(kind=QuerySelectorKind.SYMBOL, identity=qualified_name)
    first = build_query(report, selector)
    second = build_query(report, selector)
    rendered = render_query_json(first)
    assert first == second
    assert rendered == render_query_json(second)
    assert json.loads(rendered)["source_report_sha256"]
    assert any(item.identity == qualified_name for item in first.items)
    assert render_query_markdown(first).startswith("# Code Analysis Query\n")
    for forbidden in ("/Users/", "/Volumes/", "/tmp/", "/private/var/"):
        assert forbidden not in rendered


def test_contract_shape_structure_and_finding_queries_include_neighbors(
    ordinary_report,
) -> None:
    report = scan(discover_context(FIXTURES / "dictionary_project", AnalysisConfig()))
    contract_project = FIXTURES / "contracts_project"
    contract_report = scan(
        discover_context(contract_project, load_project_config(contract_project))
    )
    selections = (
        (
            contract_report,
            QuerySelectorKind.CONTRACT,
            contract_report.contracts[0].contract_id,
        ),
        (QuerySelectorKind.SHAPE, report.dictionary_shapes[0].shape_id),
        (QuerySelectorKind.STRUCTURE, report.data_structures[0].structure_id),
        (
            ordinary_report,
            QuerySelectorKind.FINDING,
            ordinary_report.findings[0].finding_id,
        ),
    )
    normalized = (
        selections[0],
        *((report, kind, identity) for kind, identity in selections[1:3]),
        selections[3],
    )
    for selected_report, kind, identity in normalized:
        response = build_query(selected_report, QuerySelector(kind=kind, identity=identity))
        selected = next(item for item in response.items if item.item_id == identity)
        assert selected.evidence
        assert response.returned_count >= 1
        if selected.related_ids:
            assert response.returned_count > 1


def test_query_limit_and_exact_continuation_are_fail_closed() -> None:
    report = scan(discover_context(FIXTURES / "dictionary_project", AnalysisConfig()))
    selector = QuerySelector(
        kind=QuerySelectorKind.SHAPE,
        identity=report.dictionary_shapes[0].shape_id,
    )
    first = build_query(report, selector, limit=2)
    assert first.truncated
    assert first.next_identity == first.items[-1].item_id
    second = build_query(report, selector, limit=2, after=first.next_identity)
    assert second.items[0].item_id > first.next_identity
    with pytest.raises(QueryBuildError, match="continuation identity"):
        build_query(report, selector, after="subject-not-from-this-query")
    with pytest.raises(QueryBuildError, match="between 1 and 500"):
        build_query(report, selector, limit=501)


def test_dependency_path_is_deterministic_and_missing_path_is_not_empty_success(
    ordinary_report,
) -> None:
    edge = next(item for item in ordinary_report.import_edges if item.local)
    selector = QuerySelector(
        kind=QuerySelectorKind.DEPENDENCY_PATH,
        identity=edge.source,
        target_identity=edge.target,
    )
    response = build_query(ordinary_report, selector)
    assert any(item.kind.value == "import-edge" for item in response.items)
    with pytest.raises(QueryBuildError, match="unknown module identity"):
        build_query(
            ordinary_report,
            QuerySelector(
                kind=QuerySelectorKind.DEPENDENCY_PATH,
                identity=edge.target,
                target_identity="not.a.module",
            ),
        )


def test_unknown_query_identity_cannot_be_successful_empty_report(ordinary_report) -> None:
    with pytest.raises(QueryBuildError, match="unknown contract identity"):
        build_query(
            ordinary_report,
            QuerySelector(
                kind=QuerySelectorKind.CONTRACT, identity="contract-00000000000000000000"
            ),
        )


def test_bare_symbol_identity_fails_when_multiple_modules_match(ordinary_report) -> None:
    source_module = next(module for module in ordinary_report.modules if module.symbols)
    duplicate = source_module.model_copy(update={"name": "duplicate", "path": "duplicate.py"})
    ambiguous = ordinary_report.model_copy(
        update={"modules": (*ordinary_report.modules, duplicate)}
    )
    with pytest.raises(QueryBuildError, match="ambiguous symbol identity"):
        build_query(
            ambiguous,
            QuerySelector(
                kind=QuerySelectorKind.SYMBOL,
                identity=source_module.symbols[0].qualified_name,
            ),
        )


def test_test_coverage_and_profile_queries_use_saved_evidence_only() -> None:
    report = scan(discover_context(FIXTURES / "test_impact_project", AnalysisConfig()))
    test = report.test_cases[0]
    response = build_query(
        report,
        QuerySelector(kind=QuerySelectorKind.TEST, identity=test.test_id),
    )
    assert any(item.item_id == test.test_id for item in response.items)

    golden = FIXTURES.parent / "golden" / "phase4-profile-report.json"
    profile = load_profile_json(golden.read_text(encoding="utf-8"))
    frame = profile.frames[0]
    profile_response = build_profile_query(
        profile,
        QuerySelector(kind=QuerySelectorKind.PROFILE_FRAME, identity=frame.frame_id),
    )
    assert any(item.item_id == frame.frame_id for item in profile_response.items)
    assert any(item.kind.value == "runtime-edge" for item in profile_response.items)
