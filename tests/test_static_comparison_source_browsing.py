"""Focused backend tests for static comparison source bindings."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import AnalysisConfig
from code_analysis.models import Evidence, SourceSpan
from code_analysis.project import discover_context
from code_analysis.projection_models import ComparisonChange
from code_analysis.report_models import AnalysisReport
from code_analysis.service import scan
from code_analysis.static_comparison_source import (
    ComparisonSourcePreviewKind,
    ComparisonSourceSideState,
    StaticComparisonSourceQuery,
    resolve_static_comparison_source_option,
)
from code_analysis.web_service import ReportViewService

FIXTURES = Path(__file__).parent / "fixtures"


def _build_index(baseline: AnalysisReport, candidate: AnalysisReport, tmp_path: Path):
    service = ReportViewService(baseline, candidate)
    root = tmp_path / "static-comparison"
    manifest = build_browse_index(service, root)
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    return service, index, manifest


def test_identical_fastpath_has_no_bindings(ordinary_report, tmp_path) -> None:
    service = ReportViewService(ordinary_report, ordinary_report)
    assert service.delta is not None and service.delta.identical
    assert service._comparison_source_bindings == ()
    with pytest.raises(ValueError, match="unavailable"):
        service.static_comparison_source_detail(0)
    root = tmp_path / "identical"
    manifest = build_browse_index(service, root)
    assert manifest.comparison is not None
    assert manifest.comparison.source_binding_count == 0
    index = BrowseIndex.open(root, report_sha256=manifest.report_sha256)
    with pytest.raises(ValueError, match="unavailable"):
        index.static_comparison_source_detail(0)


def test_external_test_inventory_retains_each_generation_authority(tmp_path):
    root = tmp_path / "project"
    shutil.copytree(FIXTURES / "test_impact_project", root)
    baseline = scan(discover_context(root, AnalysisConfig()))
    source = root / "tests/test_core.py"
    source.write_text(source.read_text() + "\n# second captured generation\n")
    candidate = scan(discover_context(root, AnalysisConfig()))
    path = "tests/test_core.py"
    assert path not in {item.logical_path for item in baseline.source_files}
    assert path not in {item.logical_path for item in candidate.source_files}
    before = next(item for item in baseline.test_modules if item.path == path)
    after = next(item for item in candidate.test_modules if item.path == path)
    assert before.test_module_id == after.test_module_id
    assert before.source_sha256 != after.source_sha256
    root.rename(tmp_path / "unavailable-source")
    service, index, _ = _build_index(baseline, candidate, tmp_path)
    position = next(
        number
        for number, row in enumerate(service.delta.records)
        if row.subject_id == before.test_module_id
    )
    detail = index.static_comparison_source_detail(position)
    assert detail == service.static_comparison_source_detail(position)
    assert detail.baseline.options[0].source_sha256 == before.source_sha256
    assert detail.candidate.options[0].source_sha256 == after.source_sha256
    assert (
        detail.baseline.options[0].preview_kind
        is ComparisonSourcePreviewKind.FILE_INVENTORY_PREVIEW
    )


def test_service_and_index_parity_for_changed_finding(ordinary_report, tmp_path) -> None:
    finding = ordinary_report.findings[0]
    changed = finding.model_copy(update={"title": finding.title + " changed"})
    candidate = ordinary_report.model_copy(update={"findings": (changed,)})
    service, index, _ = _build_index(ordinary_report, candidate, tmp_path)
    position = next(
        index
        for index, record in enumerate(service.delta.records)
        if record.subject_id == finding.finding_id
    )
    service_detail = service.static_comparison_source_detail(position)
    index_detail = index.static_comparison_source_detail(position)
    assert service_detail == index_detail
    assert service_detail.record_kind == "finding"
    assert service_detail.change is ComparisonChange.CHANGED
    assert service_detail.baseline.state is ComparisonSourceSideState.AVAILABLE
    assert service_detail.candidate.state is ComparisonSourceSideState.AVAILABLE
    assert service_detail.baseline.options
    assert (
        service_detail.baseline.report_sha256 != service_detail.candidate.report_sha256
        or service_detail.baseline.options[0].source_sha256
        == service_detail.candidate.options[0].source_sha256
    )


def test_same_path_changed_hash_retains_stable_option_ids(ordinary_report, tmp_path) -> None:
    module = ordinary_report.modules[0]
    alpha = next(item for item in ordinary_report.source_files if item.logical_path == module.path)
    alternate = alpha.model_copy(update={"source_sha256": "f" * 64})
    source_files = tuple(
        alternate if item.logical_path == module.path else item
        for item in ordinary_report.source_files
    )
    updated_module = module.model_copy(update={"source_sha256": alternate.source_sha256})
    candidate = ordinary_report.model_copy(
        update={
            "source_files": source_files,
            "modules": (updated_module, *ordinary_report.modules[1:]),
        }
    )
    service, index, _ = _build_index(ordinary_report, candidate, tmp_path)
    subject_id = f"module:{module.name}"
    position = next(
        number
        for number, record in enumerate(service.delta.records)
        if record.subject_id == subject_id
    )
    baseline_detail = service.static_comparison_source_detail(position)
    assert baseline_detail.baseline.options[0].source_sha256 == alpha.source_sha256
    assert baseline_detail.candidate.options[0].source_sha256 == alternate.source_sha256
    assert (
        baseline_detail.baseline.options[0].option_id
        != baseline_detail.candidate.options[0].option_id
    )
    assert index.static_comparison_source_detail(position) == baseline_detail


def test_added_and_removed_rows_mark_missing_side(ordinary_report, tmp_path) -> None:
    finding = ordinary_report.findings[0]
    removed = ordinary_report.model_copy(
        update={
            "findings": (),
            "summary": ordinary_report.summary.model_copy(update={"findings": 0}),
        }
    )
    service_removed, _, _ = _build_index(ordinary_report, removed, tmp_path)
    position = next(
        index
        for index, record in enumerate(service_removed.delta.records)
        if record.change is ComparisonChange.REMOVED
    )
    detail = service_removed.static_comparison_source_detail(position)
    assert detail.baseline.state is ComparisonSourceSideState.AVAILABLE
    assert detail.candidate.state is ComparisonSourceSideState.MISSING_RECORD

    added_only = removed.model_copy(
        update={
            "findings": (finding,),
            "summary": removed.summary.model_copy(update={"findings": 1}),
        }
    )
    baseline_empty = removed
    added_root = tmp_path / "added"
    added_root.mkdir()
    service_added, _, _ = _build_index(baseline_empty, added_only, added_root)
    position = next(
        index
        for index, record in enumerate(service_added.delta.records)
        if record.change is ComparisonChange.ADDED
    )
    detail = service_added.static_comparison_source_detail(position)
    assert detail.baseline.state is ComparisonSourceSideState.MISSING_RECORD
    assert detail.candidate.state is ComparisonSourceSideState.AVAILABLE


def test_symbol_span_binding(ordinary_report, tmp_path) -> None:
    module = next(item for item in ordinary_report.modules if item.symbols)
    symbol = module.symbols[0]
    changed = symbol.model_copy(
        update={"span": symbol.span.model_copy(update={"end_line": symbol.span.end_line + 1})}
    )
    modules = tuple(
        (
            item.model_copy(update={"symbols": (changed, *item.symbols[1:])})
            if item.name == module.name
            else item
        )
        for item in ordinary_report.modules
    )
    candidate = ordinary_report.model_copy(update={"modules": modules})
    service, index, _ = _build_index(ordinary_report, candidate, tmp_path)
    subject_id = f"symbol:{module.name}:{symbol.qualified_name}"
    position = next(
        index
        for index, record in enumerate(service.delta.records)
        if record.subject_id == subject_id
    )
    detail = service.static_comparison_source_detail(position)
    assert detail.record_kind == "symbol"
    assert index.static_comparison_source_detail(position) == detail
    assert detail.baseline.options[0].start_line == symbol.span.start_line


def test_module_file_inventory_preview(ordinary_report, tmp_path) -> None:
    module = ordinary_report.modules[0]
    renamed = module.model_copy(update={"line_count": module.line_count + 3})
    modules = (renamed, *ordinary_report.modules[1:])
    candidate = ordinary_report.model_copy(update={"modules": modules})
    service, _, _ = _build_index(ordinary_report, candidate, tmp_path)
    subject_id = f"module:{module.name}"
    position = next(
        index
        for index, record in enumerate(service.delta.records)
        if record.subject_id == subject_id
    )
    detail = service.static_comparison_source_detail(position)
    assert detail.record_kind == "module"
    option = detail.baseline.options[0]
    assert option.preview_kind is ComparisonSourcePreviewKind.FILE_INVENTORY_PREVIEW
    assert option.start_line == 1
    assert "file inventory preview" in option.label


def test_spanless_kind_is_unavailable_not_partial(ordinary_report) -> None:
    if not ordinary_report.import_edges:
        pytest.skip("fixture has no import edges")
    from code_analysis.static_comparison_source import build_comparison_source_side

    edge = ordinary_report.import_edges[0]
    side = build_comparison_source_side(
        report=ordinary_report,
        report_sha256="a" * 64,
        kind="import-edge",
        model=edge,
        present=True,
    )
    assert side.state is ComparisonSourceSideState.UNAVAILABLE


def test_multi_evidence_on_changed_row(ordinary_report, tmp_path) -> None:
    finding = ordinary_report.findings[0]
    extra = Evidence(
        span=SourceSpan(
            path=finding.evidence[0].span.path,
            start_line=finding.evidence[0].span.end_line + 5,
            start_column=0,
            end_line=finding.evidence[0].span.end_line + 7,
            end_column=0,
            language=finding.evidence[0].span.language,
        ),
        provider="test",
        observation="extra span",
        confidence=finding.evidence[0].confidence,
        source_sha256=finding.evidence[0].source_sha256,
        language=finding.evidence[0].language,
    )
    rich = finding.model_copy(update={"evidence": (*finding.evidence, extra)})
    baseline = ordinary_report.model_copy(
        update={"findings": (rich, *ordinary_report.findings[1:])}
    )
    candidate = baseline.model_copy(
        update={
            "findings": (
                rich.model_copy(update={"title": rich.title + " changed"}),
                *ordinary_report.findings[1:],
            )
        }
    )
    service, _, _ = _build_index(baseline, candidate, tmp_path)
    position = next(
        index
        for index, record in enumerate(service.delta.records)
        if record.subject_id == finding.finding_id
    )
    detail = service.static_comparison_source_detail(position)
    assert len(detail.baseline.options) >= 2
    option_id = detail.baseline.options[1].option_id
    resolved = resolve_static_comparison_source_option(
        detail,
        StaticComparisonSourceQuery(
            baseline_sha256=detail.baseline.report_sha256,
            candidate_sha256=detail.candidate.report_sha256,
            position=position,
            side="baseline",
            option_id=option_id,
        ),
    )
    assert resolved.option_id == option_id


def test_resolve_rejects_foreign_option(ordinary_report, tmp_path) -> None:
    finding = ordinary_report.findings[0]
    changed = finding.model_copy(update={"title": finding.title + " changed"})
    candidate = ordinary_report.model_copy(update={"findings": (changed,)})
    service, _, _ = _build_index(ordinary_report, candidate, tmp_path)
    position = next(
        number
        for number, record in enumerate(service.delta.records)
        if record.subject_id == finding.finding_id
    )
    detail = service.static_comparison_source_detail(position)
    with pytest.raises(ValueError, match="not available"):
        resolve_static_comparison_source_option(
            detail,
            StaticComparisonSourceQuery(
                baseline_sha256=detail.baseline.report_sha256,
                candidate_sha256=detail.candidate.report_sha256,
                position=position,
                side="baseline",
                option_id="source-option-" + "a" * 20,
            ),
        )


def test_structure_project_scan_bindings_are_source_free(tmp_path) -> None:
    report = scan(discover_context(FIXTURES / "structure_project", AnalysisConfig()))
    candidate = report
    service = ReportViewService(report, candidate)
    assert service.delta is not None and service.delta.identical
    assert service._comparison_source_bindings == ()
