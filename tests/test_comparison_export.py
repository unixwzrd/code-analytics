"""One saved-selection export contract for Web and indexed terminal views."""

from http.client import HTTPConnection
from urllib.parse import urlencode

import pytest
from test_web_runtime_comparison_source import TOKEN, served
from test_web_static_comparison_source import comparison  # noqa: F401

from code_analysis.browse_export import export_browse_page
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.comparison_browse import (
    ComparisonFilters,
    ComparisonPageRequest,
    comparison_page_projection,
)
from code_analysis.models import Completeness
from code_analysis.projection_models import GraphProjection, ProjectionInputRole, TableProjection
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.web_server import create_server


@pytest.mark.parametrize(
    "format_", [ProjectionFormat.JSON_COMPACT, ProjectionFormat.TSV, ProjectionFormat.MARKDOWN]
)
def test_web_and_terminal_export_same_applied_page(comparison, tmp_path, format_):  # noqa: F811
    service, _, selection = comparison
    request = ComparisonPageRequest(
        baseline_sha256=selection["baseline_sha256"],
        candidate_sha256=selection["candidate_sha256"],
        filters=ComparisonFilters(kinds=("finding",)),
        offset=0,
        limit=25,
    )
    page = service.comparison_records(offset=0, limit=25, filters=request.filters)
    projection = comparison_page_projection(page, request, service.bootstrap().reports)
    manifest = build_browse_index(service, tmp_path / "index")
    index = BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256)
    path, indexed = export_browse_page(index, request, "selected", format_)
    assert indexed == projection
    expected = render_projection(projection, format_).encode()
    assert path.read_bytes() == expected
    with pytest.raises(FileExistsError):
        export_browse_page(index, request, "selected", format_)
    params = {
        "baseline_sha256": request.baseline_sha256,
        "candidate_sha256": request.candidate_sha256,
        "kind": "finding",
        "offset": 0,
        "limit": 25,
        "format": format_.value,
    }
    with served(create_server(service, token=TOKEN)) as server:
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        try:
            connection.request(
                "GET",
                "/api/comparison-download?" + urlencode(params),
                headers={"X-Code-Analysis-Token": TOKEN},
            )
            response = connection.getresponse()
            assert response.status == 200
            assert (
                response.getheader("X-Code-Analysis-Completeness") == projection.completeness.value
            )
            assert response.read() == expected
        finally:
            connection.close()


def test_export_filters_do_not_erase_source_partiality_or_create_truncation(
    comparison,  # noqa: F811
):
    service, _, selection = comparison
    request = ComparisonPageRequest(
        baseline_sha256=selection["baseline_sha256"],
        candidate_sha256=selection["candidate_sha256"],
        filters=ComparisonFilters(search="absent"),
    )
    page = service.comparison_records(offset=0, limit=25, filters=request.filters)
    assert page.eligible == 0 and page.filtered_out > 0
    for state in Completeness:
        projection = comparison_page_projection(
            page.model_copy(update={"completeness": state}), request, service.bootstrap().reports
        )
        assert projection.completeness is state
        assert not projection.truncated and projection.record_count == 0
    with pytest.raises(ValueError, match="report pair mismatch"):
        comparison_page_projection(
            page,
            request.model_copy(update={"candidate_sha256": "0" * 64}),
            service.bootstrap().reports,
        )


def test_comparison_table_input_pair_is_closed(comparison):  # noqa: F811
    service, _, selection = comparison
    request = ComparisonPageRequest(
        baseline_sha256=selection["baseline_sha256"],
        candidate_sha256=selection["candidate_sha256"],
    )
    projection = comparison_page_projection(
        service.comparison_records(offset=0, limit=25), request, service.bootstrap().reports
    )
    assert TableProjection.model_validate_json(projection.model_dump_json()) == projection
    for inputs in (
        projection.inputs[:1],
        projection.inputs[::-1],
        (
            projection.inputs[0],
            projection.inputs[1].model_copy(update={"role": ProjectionInputRole.SOURCE}),
        ),
        (projection.inputs[0], projection.inputs[0]),
    ):
        payload = projection.model_dump()
        payload["inputs"] = inputs
        with pytest.raises(ValueError):
            TableProjection.model_validate(payload)
    with pytest.raises(ValueError, match="exactly one source"):
        GraphProjection(
            projection_id=projection.projection_id,
            title="Invalid paired graph",
            inputs=projection.inputs,
            completeness=Completeness.COMPLETE,
            record_count=0,
        )
