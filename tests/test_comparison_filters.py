"""Saved comparison selection parity and honest omission accounting."""

import pytest
from pydantic import ValidationError

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.comparison_browse import ComparisonFilters
from code_analysis.comparisons import build_report_delta
from code_analysis.projection_models import ComparisonChange
from code_analysis.web_service import ReportViewService


def test_comparison_filters_match_index_and_keep_original_positions(ordinary_report, tmp_path):
    finding = ordinary_report.findings[0]
    candidate = ordinary_report.model_copy(
        update={
            "findings": (
                finding.model_copy(update={"title": "changed"}),
                *ordinary_report.findings[1:],
            )
        }
    )
    delta = build_report_delta(
        ordinary_report, candidate, include_unchanged=True, max_records=100_000
    )
    service = ReportViewService(ordinary_report, candidate, delta)
    manifest = build_browse_index(service, tmp_path / "index")
    index = BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256)
    for filters in (
        ComparisonFilters(),
        ComparisonFilters(changes=(ComparisonChange.CHANGED,)),
        ComparisonFilters(kinds=("finding",)),
        ComparisonFilters(search=finding.finding_id),
        ComparisonFilters(changes=(ComparisonChange.UNCHANGED,), kinds=("module", "symbol")),
        ComparisonFilters(search="not-present"),
        ComparisonFilters(search="%' OR 1=1 --"),
    ):
        page = service.comparison_records(offset=0, limit=2, filters=filters)
        assert index.comparison_records(offset=0, limit=2, filters=filters) == page
        assert page.total_eligible == page.eligible + page.filtered_out + page.omitted
        assert page.completeness == delta.completeness
        for row in page.records:
            assert index.comparison_record(row.position).subject_id == row.subject_id
            assert (
                index.static_comparison_source_detail(row.position).record_kind == row.record_kind
            )
        if page.truncated:
            assert index.comparison_records(
                offset=2, limit=2, filters=filters
            ) == service.comparison_records(offset=2, limit=2, filters=filters)


@pytest.mark.parametrize(
    "values",
    [
        {"kinds": ("unknown",)},
        {"kinds": ("module", "module")},
        {"search": True},
        {"search": "x" * 501},
        {"unexpected": "field"},
    ],
)
def test_comparison_filters_are_strict(values):
    with pytest.raises(ValidationError):
        ComparisonFilters(**values)
