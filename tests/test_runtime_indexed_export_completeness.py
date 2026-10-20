"""Indexed exports must retain failure, even when display rows are omitted."""

from dataclasses import replace

import pytest
from test_runtime_browse_export import runtime_export_index  # noqa: F401

from code_analysis.models import Completeness
from code_analysis.runtime_browse_export import build_runtime_browse_export
from code_analysis.runtime_browse_models import RuntimeBrowseRequest
from code_analysis.runtime_comparison_browse import RuntimeComparisonRequest
from code_analysis.runtime_queries import RuntimeQuerySection


@pytest.mark.parametrize("comparison", (False, True))
@pytest.mark.parametrize("limit", (1, 100))
@pytest.mark.parametrize("failed_side", ("baseline", "candidate"))
def test_indexed_runtime_export_preserves_failed_source(
    runtime_export_index,  # noqa: F811
    comparison,
    limit,
    failed_side,
):
    index = runtime_export_index
    summary = index.manifest.runtime
    if failed_side == "baseline":
        summary = summary.model_copy(
            update={
                "overview": summary.overview.model_copy(
                    update={"completeness": Completeness.FAILED}
                )
            }
        )
    else:
        candidate = summary.comparison
        summary = summary.model_copy(
            update={
                "comparison": candidate.model_copy(
                    update={
                        "candidate_overview": candidate.candidate_overview.model_copy(
                            update={"completeness": Completeness.FAILED}
                        )
                    }
                )
            }
        )
    # Isolate the export state combination without inventing a failed workload.
    index = replace(index, manifest=index.manifest.model_copy(update={"runtime": summary}))
    request = (
        RuntimeComparisonRequest(limit=limit)
        if comparison
        else RuntimeBrowseRequest(section=RuntimeQuerySection.HOTSPOTS, limit=limit)
    )
    result = build_runtime_browse_export(index, request)
    expected = (
        Completeness.FAILED if comparison or failed_side == "baseline" else Completeness.PARTIAL
    )
    assert result.completeness is expected
