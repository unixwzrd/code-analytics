from __future__ import annotations

import errno
import shutil
from pathlib import Path

import pytest
from test_static_call_validation import (
    _call_span,
    _evidence,
    _occurrence,
    _synthetic_shell,
    _with_static_calls,
    _zero_coverage,
)

from code_analysis.models import Completeness, SourceLanguage
from code_analysis.projection_models import ScalarKind
from code_analysis.projections import ProjectionBuildError, ProjectionName, build_projection
from code_analysis.projections._shared import _evidence_identity
from code_analysis.report_models import SectionState
from code_analysis.reports.json_report import canonical_json, load_json, render_json
from code_analysis.reports.projection_views import (
    ProjectionFormat,
    render_projection,
    render_projection_binary,
)
from code_analysis.static_call_models import StaticCallSourceCoverageState
from code_analysis.static_call_validation import validate_static_call_occurrences


def _span_evidence(**span_changes: object) -> object:
    return _evidence(span=_call_span(**span_changes))


def _report_with_calls(*occurrences: object) -> object:
    ordered = tuple(sorted(occurrences, key=lambda item: item.occurrence_id))
    report = _with_static_calls(_synthetic_shell(), occurrences=ordered)
    validate_static_call_occurrences(report)
    return load_json(render_json(report, compact=True))


def test_table_lists_distinct_repeated_invocations() -> None:
    first = _occurrence()
    second = _occurrence(
        evidence=(_span_evidence(start_line=2, start_column=10, end_line=2, end_column=15),),
    )
    report = _report_with_calls(first, second)
    table = build_projection(report, ProjectionName.STATIC_CALL_SITES)
    assert table.record_count == 2
    assert len(table.rows) == 2
    assert {row.row_id for row in table.rows} == {first.occurrence_id, second.occurrence_id}
    callee_cells = {row.cells[8].string_value for row in table.rows}
    assert callee_cells == {"helper"}
    for row in table.rows:
        occurrence = first if row.row_id == first.occurrence_id else second
        assert row.evidence_ids == (_evidence_identity(occurrence.evidence[0]),)
        assert row.evidence_ids[0] != occurrence.occurrence_id
    graph = build_projection(report, ProjectionName.STATIC_CALL_GRAPH)
    assert len([node for node in graph.nodes if node.kind == "call-site"]) == 2
    assert len(graph.edges) == 2
    assert all(edge.kind == "source-contains-call-site" for edge in graph.edges)


def test_selection_language_bounds_and_absent_ids() -> None:
    report = _report_with_calls(
        _occurrence(),
        _occurrence(
            evidence=(_span_evidence(start_line=2, start_column=10, end_line=2, end_column=15),)
        ),
    )
    bounded = build_projection(report, ProjectionName.STATIC_CALL_SITES, max_records=1)
    assert bounded.truncated and bounded.completeness is Completeness.PARTIAL
    with pytest.raises(ProjectionBuildError, match="absent"):
        build_projection(report, ProjectionName.STATIC_CALL_SITES, selected_ids=("missing-id",))
    with pytest.raises(ProjectionBuildError, match="selected languages"):
        build_projection(
            report,
            ProjectionName.STATIC_CALL_SITES,
            languages=(SourceLanguage.JAVASCRIPT,),
        )
    module = report.modules[0].name
    filtered = build_projection(report, ProjectionName.STATIC_CALL_SITES, selected_ids=(module,))
    assert filtered.record_count == 2
    single = build_projection(
        report,
        ProjectionName.STATIC_CALL_SITES,
        selected_ids=(report.static_call_occurrences[0].occurrence_id,),
    )
    assert single.record_count == 1


def test_disabled_and_failed_zero_data_are_not_empty_success() -> None:
    with pytest.raises(ProjectionBuildError, match="disabled"):
        build_projection(_synthetic_shell(), ProjectionName.STATIC_CALL_SITES)
    failed = _with_static_calls(
        _synthetic_shell().model_copy(update={"completeness": Completeness.FAILED}),
        section_state=SectionState.FAILED,
        occurrences=(),
    )
    with pytest.raises(ProjectionBuildError, match="failed without retained"):
        build_projection(failed, ProjectionName.STATIC_CALL_SITES)


def test_complete_zero_call_source_is_valid_empty_view() -> None:
    report = _report_with_calls()
    table = build_projection(report, ProjectionName.STATIC_CALL_SITES)
    graph = build_projection(report, ProjectionName.STATIC_CALL_GRAPH)
    assert table.record_count == 0 and not table.rows
    assert graph.record_count == 0 and not graph.nodes
    assert table.completeness is Completeness.COMPLETE
    assert "Static-call-occurrences section state: complete." in table.limitations
    assert any("syntactic invocation" in item for item in table.limitations)


def test_partial_source_coverage_and_section_state_propagate() -> None:
    coverage = (
        _zero_coverage(
            state=StaticCallSourceCoverageState.PARTIAL,
            retained_count=1,
            observed_count=1,
            limitations=("bounded traversal",),
        ),
    )
    report = load_json(
        render_json(
            _with_static_calls(
                _synthetic_shell().model_copy(update={"completeness": Completeness.PARTIAL}),
                section_state=SectionState.PARTIAL,
                occurrences=(_occurrence(),),
                coverage=coverage,
            ),
            compact=True,
        )
    )
    validate_static_call_occurrences(report)
    table = build_projection(report, ProjectionName.STATIC_CALL_SITES)
    assert table.completeness is Completeness.PARTIAL
    assert "bounded traversal" in table.limitations
    assert "Static-call-occurrences section state: partial." in table.limitations


def test_saved_rendering_is_deterministic_and_source_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report_with_calls(
        _occurrence(),
        _occurrence(
            evidence=(_span_evidence(start_line=2, start_column=10, end_line=2, end_column=15),),
        ),
    )
    table = build_projection(report, ProjectionName.STATIC_CALL_SITES)
    graph = build_projection(report, ProjectionName.STATIC_CALL_GRAPH)

    def forbid_source(*args: object, **kwargs: object) -> str:
        pytest.fail("projection rendering must not read source files")

    monkeypatch.setattr(Path, "read_text", forbid_source)
    monkeypatch.setattr(Path, "read_bytes", forbid_source)
    table = build_projection(report, ProjectionName.STATIC_CALL_SITES)
    graph = build_projection(report, ProjectionName.STATIC_CALL_GRAPH)

    for projection, formats in (
        (
            table,
            (
                ProjectionFormat.JSON,
                ProjectionFormat.JSON_COMPACT,
                ProjectionFormat.TSV,
                ProjectionFormat.MARKDOWN,
            ),
        ),
        (
            graph,
            (
                ProjectionFormat.JSON,
                ProjectionFormat.MARKDOWN,
                ProjectionFormat.DOT,
                ProjectionFormat.MERMAID,
            ),
        ),
    ):
        for fmt in formats:
            rendered = render_projection(projection, fmt)
            assert rendered == render_projection(projection, fmt)
            assert "/Users/" not in rendered and "/private/" not in rendered
    assert canonical_json(table) == canonical_json(
        build_projection(report, ProjectionName.STATIC_CALL_SITES)
    )


def test_unknown_callee_and_anchor_remain_null() -> None:
    report = _report_with_calls(_occurrence(callee_spelling=None, enclosing_subject_id=None))
    table = build_projection(report, ProjectionName.STATIC_CALL_SITES)
    assert table.rows[0].cells[8].kind is ScalarKind.NULL
    assert table.rows[0].cells[15].kind is ScalarKind.NULL


def test_source_span_order_is_declared_not_hash_id_order() -> None:
    calls = tuple(
        _occurrence(evidence=(_span_evidence(start_column=column, end_column=column + 3),))
        for column in range(4, 28, 4)
    )
    assert [item.occurrence_id for item in calls] != sorted(item.occurrence_id for item in calls)
    table = build_projection(_report_with_calls(*calls), ProjectionName.STATIC_CALL_SITES)
    assert [row.row_id for row in table.rows] == [item.occurrence_id for item in calls]
    assert [item.column_id for item in table.sort] == [
        "language",
        "logical-path",
        "start-line",
        "start-column",
        "end-line",
        "end-column",
        "occurrence-id",
    ]


def test_graph_containment_limits_selection_and_bounds() -> None:
    report = _report_with_calls(
        _occurrence(),
        _occurrence(
            evidence=(_span_evidence(start_line=2, start_column=10, end_line=2, end_column=15),)
        ),
    )
    graph = build_projection(report, ProjectionName.STATIC_CALL_GRAPH)
    assert "not dispatch" in graph.title.lower()
    joined = " ".join(graph.limitations)
    assert "not caller-to-callee" in joined
    assert "dynamic callee" in joined.lower()
    subject = report.subjects[0].subject_id
    with pytest.raises(ProjectionBuildError, match="enclosing-subject identity"):
        build_projection(report, ProjectionName.STATIC_CALL_GRAPH, selected_ids=(subject,))
    with pytest.raises(ProjectionBuildError, match="module identity"):
        build_projection(
            report, ProjectionName.STATIC_CALL_GRAPH, selected_ids=(report.modules[0].name,)
        )
    bounded = build_projection(report, ProjectionName.STATIC_CALL_GRAPH, max_nodes=1, max_edges=1)
    assert bounded.truncated and bounded.completeness is Completeness.PARTIAL
    assert bounded.original_node_count == len(graph.nodes)
    assert bounded.omitted_node_count == len(graph.nodes) - len(bounded.nodes)
    focused = build_projection(
        report,
        ProjectionName.STATIC_CALL_GRAPH,
        selected_ids=(graph.nodes[0].node_id,),
    )
    assert focused.nodes and not focused.truncated


def test_truncated_table_maps_to_enodata_exit(tmp_path: Path) -> None:
    from code_analysis.cli import main

    report = _report_with_calls(
        _occurrence(),
        _occurrence(
            evidence=(_span_evidence(start_line=2, start_column=10, end_line=2, end_column=15),)
        ),
    )
    path = tmp_path / "report.json"
    path.write_text(canonical_json(report))
    out = tmp_path / "sites.json"
    status = main(
        [
            "export",
            str(path),
            "--projection",
            "static-call-sites",
            "--max-records",
            "1",
            "-o",
            str(out),
        ]
    )
    assert status == errno.ENODATA
    assert out.exists()


@pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz unavailable")
def test_graph_diagram_binary_rendering() -> None:
    report = _report_with_calls(_occurrence())
    graph = build_projection(report, ProjectionName.STATIC_CALL_GRAPH)
    assert b"<svg" in render_projection_binary(graph, ProjectionFormat.SVG)
    assert render_projection_binary(graph, ProjectionFormat.PNG).startswith(b"\x89PNG")
