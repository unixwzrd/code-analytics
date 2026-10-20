from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.browse_export import export_browse_page
from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.browse_index_models import BrowseIndexManifest
from code_analysis.config import AnalysisConfig
from code_analysis.models import SourceLanguage
from code_analysis.project import discover_context
from code_analysis.projection_models import (
    ProjectionFindingCategory,
    ProjectionFindingSeverity,
    ProjectionSelection,
)
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.reports.json_report import load_json
from code_analysis.reports.projection_views import ProjectionFormat
from code_analysis.service import scan
from code_analysis.static_browse import (
    StaticProjectionLimitError,
    StaticProjectionRequest,
    static_page_projection,
)
from code_analysis.subject_finding_browse import (
    SubjectFindingFactCounts,
    read_subject_finding_facts,
    subject_finding_fact_records,
    validate_subject_finding_facts,
)
from code_analysis.subject_findings import unscoped_scope_limitation
from code_analysis.web_service import ReportViewService

GOLDEN = Path(__file__).parent / "golden"
FIXTURES = Path(__file__).parent / "fixtures"

COMBINE_A = "subject-dc537fbe8077aaa3b39a"
DOMAIN_BAD = "subject-f7326b273e0da8dcf15e"

SELECTION_CASES: tuple[tuple[str, dict[str, object]], ...] = (
    ("unfiltered", {}),
    ("category", {"finding_categories": (ProjectionFindingCategory.DUPLICATION,)}),
    (
        "category-or",
        {
            "finding_categories": (
                ProjectionFindingCategory.ARCHITECTURE_CONTRACT,
                ProjectionFindingCategory.DUPLICATION,
            )
        },
    ),
    ("category-absent", {"finding_categories": (ProjectionFindingCategory.COHESION,)}),
    ("severity-warning", {"finding_severities": (ProjectionFindingSeverity.WARNING,)}),
    ("severity-info", {"finding_severities": (ProjectionFindingSeverity.INFO,)}),
    ("severity-absent", {"finding_severities": (ProjectionFindingSeverity.ERROR,)}),
    ("rule", {"finding_rule_ids": ("duplication.normalized-ast",)}),
    (
        "rule-or",
        {"finding_rule_ids": ("configured-layer-direction", "duplication.normalized-ast")},
    ),
    ("module-exact", {"module_prefixes": ("package.combine_a",)}),
    ("module-dotted-parent", {"module_prefixes": ("package",)}),
    ("module-boundary", {"module_prefixes": ("package.wor",)}),
    ("module-or", {"module_prefixes": ("package.combine_a", "package.work")}),
    (
        "category-and-severity",
        {
            "finding_categories": (ProjectionFindingCategory.EXTRACTION_CANDIDATE,),
            "finding_severities": (ProjectionFindingSeverity.INFO,),
        },
    ),
    (
        "contradictory-and",
        {
            "finding_categories": (ProjectionFindingCategory.DUPLICATION,),
            "finding_severities": (ProjectionFindingSeverity.INFO,),
        },
    ),
    (
        "module-and-rule",
        {
            "module_prefixes": ("package.work",),
            "finding_rule_ids": ("extraction-candidate",),
            "finding_severities": (ProjectionFindingSeverity.INFO,),
        },
    ),
)


@pytest.fixture
def subject_report():
    """The Phase 3 golden report plus two findings that carry no subject identity."""

    report = load_json((GOLDEN / "phase3-report.json").read_text(encoding="utf-8"))
    template = report.findings[0]
    return report.model_copy(
        update={
            "summary": report.summary.model_copy(update={"findings": report.summary.findings + 2}),
            "findings": (
                *report.findings,
                template.model_copy(
                    update={
                        "finding_id": "finding-unscoped-duplication",
                        "rule_id": "duplication.normalized-ast",
                        "category": "duplication",
                        "severity": "warning",
                        "subject_ids": (),
                    }
                ),
                template.model_copy(
                    update={
                        "finding_id": "finding-unscoped-cycle",
                        "rule_id": "layering.import-cycle",
                        "category": "import-cycle",
                        "severity": "error",
                        "subject_ids": (),
                    }
                ),
            ),
        }
    )


@pytest.fixture
def subject_index(subject_report, tmp_path):
    destination = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(subject_report), destination)
    return BrowseIndex.open(destination, report_sha256=manifest.report_sha256)


def authoritative(report, **options):
    return build_projection(report, ProjectionName.SUBJECT_FINDINGS, max_records=100_000, **options)


def indexed(index, *, limit: int = 500, offset: int = 0, query: str = "", **options):
    return index.static_projection(
        StaticProjectionRequest(
            name=ProjectionName.SUBJECT_FINDINGS,
            limit=limit,
            offset=offset,
            query=query,
            finding_selection=ProjectionSelection(**options) if options else None,
        )
    )


def unscoped_disclosure(projection) -> list[str]:
    return [item for item in projection.limitations if "without subject identities" in item]


def test_prepared_receipt_records_facts_without_a_second_aggregated_copy(
    subject_report, subject_index
):
    manifest = subject_index.manifest
    assert manifest.subject_finding_facts == SubjectFindingFactCounts(
        subjects=len(subject_report.subjects), findings=len(subject_report.findings)
    )
    summary = next(
        item for item in manifest.static_projections if item.name is ProjectionName.SUBJECT_FINDINGS
    )
    expected = authoritative(subject_report)
    assert summary.records == expected.record_count
    assert summary.columns == expected.columns
    assert summary.sort == expected.sort
    assert summary.unscoped_finding_count == 2
    with sqlite3.connect(subject_index.database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM static_projection_rows WHERE name = 'subject-findings'"
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM subject_facts").fetchone() == (
            len(subject_report.subjects),
        )
        assert connection.execute("SELECT count(*) FROM finding_facts").fetchone() == (
            len(subject_report.findings),
        )
        payload = connection.execute(
            "SELECT payload FROM finding_facts ORDER BY finding_id"
        ).fetchone()[0]
    assert "evidence_ids" in payload and "source" not in payload


@pytest.mark.parametrize(
    "label,options", SELECTION_CASES, ids=[case[0] for case in SELECTION_CASES]
)
def test_indexed_rows_match_the_authoritative_projection(
    subject_report, subject_index, label, options
):
    expected = authoritative(subject_report, **options)
    page = indexed(subject_index, **options)
    assert page.records == expected.rows, label
    assert [row.evidence_ids for row in page.records] == [
        row.evidence_ids for row in expected.rows
    ], label
    assert page.eligible == expected.record_count, label
    assert not page.truncated, label
    disclosed = unscoped_disclosure(static_page_projection(page))
    assert disclosed == unscoped_disclosure(expected), label


def test_filtering_precedes_aggregation_and_recomputes_every_count(subject_index):
    unfiltered = {row.row_id: row for row in indexed(subject_index).records}
    filtered = {
        row.row_id: row
        for row in indexed(
            subject_index, finding_categories=(ProjectionFindingCategory.DUPLICATION,)
        ).records
    }
    assert set(filtered) < set(unfiltered)
    before, after = unfiltered[COMBINE_A], filtered[COMBINE_A]
    # Column 4 is the finding count; 6 the warnings; 7 the info count.
    assert (before.cells[4].integer_value, after.cells[4].integer_value) == (3, 1)
    assert (before.cells[6].integer_value, after.cells[6].integer_value) == (2, 1)
    assert (before.cells[7].integer_value, after.cells[7].integer_value) == (1, 0)
    assert before.cells[8].string_value == (
        "duplication=1, extraction-candidate=1, module-combination=1"
    )
    assert after.cells[8].string_value == "duplication=1"
    assert after.cells[9].string_value == "duplication.normalized-ast=1"
    assert set(after.evidence_ids) < set(before.evidence_ids)


def test_module_prefixes_match_exactly_or_on_a_dotted_boundary(subject_index):
    assert {row.row_id for row in indexed(subject_index, module_prefixes=("package.work",)).records}
    assert [
        row.cells[2].string_value
        for row in indexed(subject_index, module_prefixes=("package.work",)).records
    ] == ["package.work"] * 3
    assert indexed(subject_index, module_prefixes=("package.wor",)).records == ()
    assert [
        row.row_id for row in indexed(subject_index, module_prefixes=("package.combine_a",)).records
    ] == [COMBINE_A]
    assert indexed(subject_index, module_prefixes=("package",)).eligible == 6


def test_corrected_unscoped_count_survives_nonempty_and_empty_results(subject_index):
    unfiltered = indexed(subject_index)
    assert unfiltered.unscoped_finding_count == 2
    assert unscoped_disclosure(static_page_projection(unfiltered)) == [unscoped_scope_limitation(2)]

    narrowed = indexed(subject_index, finding_categories=(ProjectionFindingCategory.DUPLICATION,))
    assert narrowed.records and narrowed.unscoped_finding_count == 1
    assert unscoped_disclosure(static_page_projection(narrowed)) == [unscoped_scope_limitation(1)]

    empty = indexed(subject_index, finding_categories=(ProjectionFindingCategory.IMPORT_CYCLE,))
    assert empty.records == () and empty.unscoped_finding_count == 1
    assert unscoped_disclosure(static_page_projection(empty)) == [unscoped_scope_limitation(1)]

    silent = indexed(subject_index, finding_severities=(ProjectionFindingSeverity.INFO,))
    assert silent.records and silent.unscoped_finding_count == 0
    assert unscoped_disclosure(static_page_projection(silent)) == []

    both_empty = indexed(subject_index, finding_categories=(ProjectionFindingCategory.COHESION,))
    assert both_empty.records == () and both_empty.unscoped_finding_count == 0
    assert unscoped_disclosure(static_page_projection(both_empty)) == []


def test_receipt_disclosure_is_never_mutated_by_a_request(subject_index):
    prepared = tuple(
        item
        for item in subject_index.manifest.static_projections
        if item.name is ProjectionName.SUBJECT_FINDINGS
    )
    page = indexed(subject_index, finding_categories=(ProjectionFindingCategory.DUPLICATION,))
    assert page.summary == prepared[0]
    assert unscoped_scope_limitation(2) in page.summary.limitations
    assert (
        subject_index.manifest.static_projections
        == BrowseIndex.open(
            subject_index.database.parent, report_sha256=subject_index.manifest.report_sha256
        ).manifest.static_projections
    )


def test_paging_and_search_apply_after_filtering(subject_report, subject_index):
    expected = authoritative(subject_report)
    ordered = []
    for offset in (0, 2, 4):
        page = indexed(subject_index, limit=2, offset=offset)
        assert page.retained == 2 and page.eligible == 6
        assert page.truncated is (offset < 4)
        ordered.extend(row.row_id for row in page.records)
    assert ordered == [row.row_id for row in expected.rows]
    beyond = indexed(subject_index, limit=2, offset=6)
    assert beyond.records == () and not beyond.truncated

    searched = indexed(subject_index, query="COMBINE_A")
    assert [row.row_id for row in searched.records] == [COMBINE_A]
    assert searched.eligible == 1 and searched.unscoped_finding_count == 2
    combined = indexed(
        subject_index,
        query="package.work",
        finding_severities=(ProjectionFindingSeverity.INFO,),
    )
    assert combined.eligible == 3 and combined.unscoped_finding_count == 0
    assert indexed(subject_index, query="no-such-subject").records == ()


def test_export_discloses_filters_counts_partiality_and_source(subject_index):
    request = StaticProjectionRequest(
        name=ProjectionName.SUBJECT_FINDINGS,
        limit=2,
        finding_selection=ProjectionSelection(
            finding_categories=(ProjectionFindingCategory.DUPLICATION,)
        ),
    )
    path, projection = export_browse_page(
        subject_index, request, "subject-findings", ProjectionFormat.JSON_COMPACT
    )
    assert path.exists()
    assert projection.inputs[0].report_sha256 == subject_index.manifest.report_sha256
    assert projection.selection.finding_categories == (ProjectionFindingCategory.DUPLICATION,)
    assert "Indexed selection: " + request.model_dump_json() in projection.limitations
    assert "Indexed selection retained 2 of 5 matching rows." in projection.limitations
    assert "Indexed finding selection matched 5 of 6 prepared subject rows." in (
        projection.limitations
    )
    assert unscoped_scope_limitation(1) in projection.limitations
    assert unscoped_scope_limitation(2) not in projection.limitations
    assert projection.truncated and projection.completeness.value == "partial"
    unfiltered = static_page_projection(
        subject_index.static_projection(
            StaticProjectionRequest(name=ProjectionName.SUBJECT_FINDINGS, limit=2)
        )
    )
    assert unfiltered.projection_id != projection.projection_id


def test_browsing_reads_no_source_or_report_after_preparation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    shutil.copytree(FIXTURES / "ordinary", source)
    report = scan(discover_context(source, AnalysisConfig()))
    manifest = build_browse_index(ReportViewService(report), tmp_path / "index")
    shutil.rmtree(source)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("indexed browsing must not rebuild from the report or source")

    for target in (
        "code_analysis.static_browse.build_projection",
        "code_analysis.static_browse.prepared_static_projection",
        "code_analysis.static_browse.subject_finding_fact_records",
        "code_analysis.subject_finding_browse.subject_finding_facts",
        "code_analysis.reports.json_report.load_json",
    ):
        monkeypatch.setattr(target, forbidden)

    index = BrowseIndex.open(tmp_path / "index", report_sha256=manifest.report_sha256)
    page = indexed(index)
    assert page.records == () and page.eligible == 0
    assert page.unscoped_finding_count == 1
    assert unscoped_disclosure(static_page_projection(page)) == [unscoped_scope_limitation(1)]
    assert not source.exists()


def test_malformed_counts_rows_identities_and_dangling_references_fail_closed(
    subject_report, subject_index
):
    counts = subject_index.manifest.subject_finding_facts
    database = subject_index.database
    dangling_counts = SubjectFindingFactCounts(
        subjects=counts.subjects - 1, findings=counts.findings
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM subject_facts WHERE subject_id = ?", (COMBINE_A,))
        with pytest.raises(ValueError, match="disagree with the index receipt"):
            validate_subject_finding_facts(connection, counts)
        with pytest.raises(ValidationError, match="unknown subject identities"):
            validate_subject_finding_facts(connection, dangling_counts)
        connection.rollback()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE subject_facts SET payload = "
            "(SELECT payload FROM subject_facts WHERE subject_id = ?) WHERE subject_id = ?",
            (DOMAIN_BAD, COMBINE_A),
        )
        with pytest.raises(ValueError, match="identity disagrees with its stored key"):
            validate_subject_finding_facts(connection, counts)
        connection.rollback()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE finding_facts SET payload = '{}'")
        with pytest.raises(ValidationError):
            validate_subject_finding_facts(connection, counts)
        connection.rollback()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE finding_facts SET payload = ?", ("x" * (1024**2 + 1),))
        with pytest.raises(StaticProjectionLimitError, match="one-MiB"):
            validate_subject_finding_facts(connection, counts)
        connection.rollback()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE finding_facts")
        with pytest.raises(ValueError, match="fact table schema mismatch"):
            validate_subject_finding_facts(connection, counts)
        connection.rollback()


def test_receipt_rejects_fact_counts_that_cannot_carry_the_prepared_rows(subject_index):
    receipt = subject_index.manifest.model_dump()
    receipt["subject_finding_facts"]["subjects"] = 1
    with pytest.raises(ValidationError, match="retained fact counts"):
        BrowseIndexManifest.model_validate(receipt)
    receipt = subject_index.manifest.model_dump()
    receipt["subject_finding_facts"]["findings"] = 0
    with pytest.raises(ValidationError, match="retained fact counts"):
        BrowseIndexManifest.model_validate(receipt)
    receipt = subject_index.manifest.model_dump()
    for item in receipt["static_projections"]:
        item["unscoped_finding_count"] = 0 if item["unscoped_finding_count"] is None else None
    with pytest.raises(ValidationError, match="prepared unscoped finding count"):
        BrowseIndexManifest.model_validate(receipt)


@pytest.mark.parametrize(
    "options,message",
    [
        ({"name": ProjectionName.COMPLEXITY}, "requires the subject-findings projection"),
        ({"selected_ids": ("subject-00000000000000000000",)}, "exact subject selection"),
        ({"languages": (SourceLanguage.PYTHON,)}, "language selection"),
        ({"max_records": 10}, "remove: max_records"),
        ({"cross_module_only": True}, "remove: cross_module_only"),
        ({"candidate_states": ("open",)}, "remove: candidate_states"),
        ({"depth": 2}, "remove: depth"),
    ],
)
def test_finding_selection_rejects_inapplicable_or_unsupported_options(options, message):
    name = options.pop("name", ProjectionName.SUBJECT_FINDINGS)
    with pytest.raises(ValidationError, match=message):
        StaticProjectionRequest(name=name, finding_selection=ProjectionSelection(**options))


@pytest.mark.parametrize(
    "options",
    [
        {"finding_selection": True},
        {"finding_selection": {"unknown": 1}},
        {"finding_selection": {"module_prefixes": ["package"]}},
        {"finding_selection": {"module_prefixes": ("package/work",)}},
        {"candidate_selection": {}},
        {"decision_ledger_sha256": "0" * 64, "decision_report_sha256": "0" * 64},
        {"limit": True},
        {"limit": 0},
        {"limit": 501},
        {"offset": -1},
        {"query": True},
        {"surprise": 1},
    ],
)
def test_strict_indexed_subject_finding_requests(options):
    with pytest.raises(ValidationError):
        StaticProjectionRequest.model_validate({"name": ProjectionName.SUBJECT_FINDINGS, **options})


def test_default_and_explicit_empty_selections_are_accepted(subject_index):
    default = indexed(subject_index)
    explicit = subject_index.static_projection(
        StaticProjectionRequest(
            name=ProjectionName.SUBJECT_FINDINGS,
            limit=500,
            finding_selection=ProjectionSelection(),
        )
    )
    assert default.records == explicit.records
    assert default.unscoped_finding_count == explicit.unscoped_finding_count


def test_fact_bounds_fail_preparation_and_reads_instead_of_omitting_facts(
    subject_report, subject_index, monkeypatch
):
    monkeypatch.setattr("code_analysis.subject_finding_browse.MAX_SUBJECT_FINDING_FACTS", 2)
    with pytest.raises(StaticProjectionLimitError, match="preparation bound"):
        subject_finding_fact_records(subject_report)
    with (
        sqlite3.connect(subject_index.database) as connection,
        pytest.raises(StaticProjectionLimitError, match="read bound"),
    ):
        read_subject_finding_facts(connection, subject_index.manifest.subject_finding_facts)


def test_fact_preparation_checks_bounds_before_allocating_facts(subject_report, monkeypatch):
    def forbidden_conversion(report):
        raise AssertionError("oversized reports must not allocate derived facts")

    monkeypatch.setattr("code_analysis.subject_finding_browse.MAX_SUBJECT_FINDING_FACTS", 2)
    monkeypatch.setattr(
        "code_analysis.subject_finding_browse.subject_finding_facts", forbidden_conversion
    )
    with pytest.raises(StaticProjectionLimitError, match="preparation bound"):
        subject_finding_fact_records(subject_report)
