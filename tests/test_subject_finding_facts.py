from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.models import SourceLanguage, evidence_identity
from code_analysis.projection_models import (
    ProjectionFindingCategory,
    ProjectionFindingSeverity,
    ProjectionSelection,
    TableProjection,
)
from code_analysis.projections import ProjectionBuildError, ProjectionName, build_projection
from code_analysis.report_models import SubjectKind
from code_analysis.reports.json_report import load_json
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.subject_findings import (
    SUBJECT_FINDING_COLUMN_DEFINITIONS,
    FindingFact,
    SubjectFact,
    SubjectFindingFacts,
    SubjectFindingSelectionError,
    aggregate_subject_findings,
    subject_finding_columns,
    subject_finding_facts,
    unscoped_scope_limitation,
)

GOLDEN = Path(__file__).parent / "golden"

POSITIONS = {
    column_id: position
    for position, (column_id, _, _) in enumerate(SUBJECT_FINDING_COLUMN_DEFINITIONS)
}


def subject_id(suffix: str) -> str:
    return "subject-" + suffix.rjust(20, "0")


def proposal_id(suffix: str) -> str:
    return "refactor-proposal-" + suffix.rjust(20, "0")


def evidence_id(suffix: str) -> str:
    return "evidence-" + suffix.rjust(20, "0")


def subject_payload(suffix: str, module: str, logical_name: str) -> dict[str, object]:
    return {
        "subject_id": subject_id(suffix),
        "kind": SubjectKind.SYMBOL,
        "module": module,
        "logical_name": logical_name,
        "language": SourceLanguage.PYTHON,
    }


def subject_fact(suffix: str, module: str, logical_name: str, **overrides: object) -> SubjectFact:
    return SubjectFact(**{**subject_payload(suffix, module, logical_name), **overrides})


def finding_payload(finding: str) -> dict[str, object]:
    return {
        "finding_id": finding,
        "category": ProjectionFindingCategory.DUPLICATION,
        "severity": ProjectionFindingSeverity.WARNING,
        "rule_id": "duplication.normalized-ast",
        "subject_ids": (),
        "evidence_ids": (evidence_id("a"),),
        "proposal_ids": (),
        "language": SourceLanguage.PYTHON,
    }


def finding_fact(
    finding: str,
    *,
    category: ProjectionFindingCategory = ProjectionFindingCategory.DUPLICATION,
    severity: ProjectionFindingSeverity = ProjectionFindingSeverity.WARNING,
    rule_id: str = "duplication.normalized-ast",
    subject_ids: tuple[str, ...] = (),
    evidence_ids: tuple[str, ...] = (evidence_id("a"),),
    proposal_ids: tuple[str, ...] = (),
    language: SourceLanguage = SourceLanguage.PYTHON,
) -> FindingFact:
    return FindingFact(
        finding_id=finding,
        category=category,
        severity=severity,
        rule_id=rule_id,
        subject_ids=subject_ids,
        evidence_ids=evidence_ids,
        proposal_ids=proposal_ids,
        language=language,
    )


@pytest.fixture
def synthetic_facts() -> SubjectFindingFacts:
    return SubjectFindingFacts(
        subjects=(
            subject_fact("a1", "package.alpha", "one", proposal_ids=(proposal_id("p1"),)),
            subject_fact("a2", "package.alpha.nested", "two"),
            subject_fact("b1", "package.alphabet", "three"),
            subject_fact("c1", "other", "four"),
        ),
        findings=(
            finding_fact(
                "finding-alpha-duplication",
                subject_ids=(subject_id("a1"), subject_id("a2")),
                evidence_ids=(evidence_id("a"), evidence_id("b")),
                proposal_ids=(proposal_id("p2"),),
            ),
            finding_fact(
                "finding-alpha-cohesion",
                category=ProjectionFindingCategory.COHESION,
                severity=ProjectionFindingSeverity.ERROR,
                rule_id="cohesion.module",
                subject_ids=(subject_id("a1"),),
                evidence_ids=(evidence_id("b"),),
            ),
            finding_fact(
                "finding-alphabet-info",
                category=ProjectionFindingCategory.COMMONALITY,
                severity=ProjectionFindingSeverity.INFO,
                rule_id="commonality.shared",
                subject_ids=(subject_id("b1"),),
            ),
            finding_fact("finding-unscoped", subject_ids=()),
        ),
    )


def mixed_report(report):
    """A golden report extended with unscoped findings in two categories."""

    template = report.findings[0]
    return report.model_copy(
        update={
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
            )
        }
    )


def test_subject_facts_reject_extras_coercion_and_unsorted_proposals() -> None:
    valid = subject_fact("a1", "package.alpha", "one", proposal_ids=(proposal_id("p1"),))
    assert valid.language is SourceLanguage.PYTHON
    with pytest.raises(ValidationError):
        valid.module = "other"
    for override in (
        {"nope": True},
        {"kind": "symbol"},
        {"language": "python"},
        {"module": 123},
        {"module": ""},
        {"subject_id": "subject-not-a-digest"},
        {"proposal_ids": [proposal_id("p1")]},
        {"proposal_ids": (proposal_id("p2"), proposal_id("p1"))},
        {"proposal_ids": (proposal_id("p1"), proposal_id("p1"))},
        {"proposal_ids": (subject_id("a1"),)},
    ):
        with pytest.raises(ValidationError):
            SubjectFact(**{**subject_payload("a1", "package.alpha", "one"), **override})


def test_finding_facts_reject_coercion_duplicates_and_foreign_identities() -> None:
    assert FindingFact(**finding_payload("finding-one")).rule_id == "duplication.normalized-ast"
    for override in (
        {"surprise": "yes"},
        {"category": "duplication"},
        {"severity": "warning"},
        {"language": "python"},
        {"rule_id": ""},
        {"finding_id": ""},
        {"subject_ids": [subject_id("a1")]},
        {"subject_ids": (subject_id("a2"), subject_id("a1"))},
        {"subject_ids": (subject_id("a1"), subject_id("a1"))},
        {"evidence_ids": (evidence_id("b"), evidence_id("a"))},
        {"evidence_ids": (evidence_id("a"), evidence_id("a"))},
        {"evidence_ids": (proposal_id("p1"),)},
        {"proposal_ids": (evidence_id("a"),)},
    ):
        with pytest.raises(ValidationError):
            FindingFact(**{**finding_payload("finding-one"), **override})


def test_fact_collection_rejects_duplicate_and_dangling_identities() -> None:
    subject = subject_fact("a1", "package.alpha", "one")
    with pytest.raises(ValidationError, match="unique subject identities"):
        SubjectFindingFacts(subjects=(subject, subject))
    with pytest.raises(ValidationError, match="unique finding identities"):
        SubjectFindingFacts(
            subjects=(subject,),
            findings=(
                finding_fact("finding-one", subject_ids=(subject.subject_id,)),
                finding_fact("finding-one", subject_ids=(subject.subject_id,)),
            ),
        )
    with pytest.raises(ValidationError, match="unknown subject identities"):
        SubjectFindingFacts(
            subjects=(subject,),
            findings=(finding_fact("finding-one", subject_ids=(subject_id("zz"),)),),
        )


def test_report_conversion_preserves_identity_evidence_and_proposals() -> None:
    report = load_json((GOLDEN / "phase3-report.json").read_text(encoding="utf-8"))
    facts = subject_finding_facts(report)
    assert {item.subject_id for item in facts.subjects} == {
        item.subject_id for item in report.subjects
    }
    assert [item.finding_id for item in facts.findings] == [
        item.finding_id for item in report.findings
    ]
    subjects = facts.subjects_by_id()
    for subject in report.subjects:
        fact = subjects[subject.subject_id]
        assert (fact.kind, fact.module, fact.logical_name, fact.language) == (
            subject.kind,
            subject.module,
            subject.logical_name,
            subject.language,
        )
        assert fact.proposal_ids == tuple(
            sorted(
                proposal.proposal_id
                for proposal in report.refactor_proposals
                if subject.subject_id in proposal.affected_subject_ids
            )
        )
    findings = {item.finding_id: item for item in facts.findings}
    for finding in report.findings:
        fact = findings[finding.finding_id]
        assert fact.evidence_ids == tuple(
            sorted(
                {
                    evidence_identity(evidence)
                    for evidence in (*finding.evidence, *finding.counter_evidence)
                }
            )
        )
        assert fact.proposal_ids == finding.affected_proposal_ids
        assert fact.subject_ids == finding.subject_ids
        assert fact.category.value == finding.category
        assert fact.severity.value == finding.severity
        assert fact.language is finding.language
    donor = next(
        item
        for item in report.findings
        if not set(item.evidence) & set(report.findings[0].evidence)
    )
    with_counter = report.model_copy(
        update={
            "findings": (
                report.findings[0].model_copy(update={"counter_evidence": donor.evidence}),
                *report.findings[1:],
            )
        }
    )
    counter_fact = subject_finding_facts(with_counter).findings[0]
    assert set(counter_fact.evidence_ids) == {
        evidence_identity(evidence) for evidence in (*report.findings[0].evidence, *donor.evidence)
    }
    assert set(counter_fact.evidence_ids) > set(
        findings[report.findings[0].finding_id].evidence_ids
    )


def test_aggregation_filters_findings_before_grouping(synthetic_facts) -> None:
    unfiltered = aggregate_subject_findings(synthetic_facts, ProjectionSelection())
    # Two findings first, then the warning ahead of the info at equal counts.
    assert [row.row_id for row in unfiltered.rows] == [
        subject_id("a1"),
        subject_id("a2"),
        subject_id("b1"),
    ]
    assert unfiltered.unscoped_finding_count == 1
    first = unfiltered.rows[0]
    assert first.cells[POSITIONS["finding-count"]].integer_value == 2
    assert first.cells[POSITIONS["errors"]].integer_value == 1
    assert first.cells[POSITIONS["warnings"]].integer_value == 1
    assert first.cells[POSITIONS["category-counts"]].string_value == "cohesion=1, duplication=1"
    assert first.cells[POSITIONS["rule-counts"]].string_value == (
        "cohesion.module=1, duplication.normalized-ast=1"
    )
    assert first.cells[POSITIONS["evidence-count"]].integer_value == 2
    assert first.evidence_ids == (evidence_id("a"), evidence_id("b"))
    # One proposal from the subject association, one carried by the finding.
    assert first.cells[POSITIONS["affected-proposals"]].integer_value == 2

    cohesion = aggregate_subject_findings(
        synthetic_facts,
        ProjectionSelection(finding_categories=(ProjectionFindingCategory.COHESION,)),
    )
    assert [row.row_id for row in cohesion.rows] == [subject_id("a1")]
    assert cohesion.rows[0].cells[POSITIONS["finding-count"]].integer_value == 1
    assert cohesion.unscoped_finding_count == 0

    duplication = aggregate_subject_findings(
        synthetic_facts,
        ProjectionSelection(finding_categories=(ProjectionFindingCategory.DUPLICATION,)),
    )
    assert duplication.unscoped_finding_count == 1


def test_aggregation_ors_within_and_ands_across_filters(synthetic_facts) -> None:
    category_or = aggregate_subject_findings(
        synthetic_facts,
        ProjectionSelection(
            finding_categories=(
                ProjectionFindingCategory.COHESION,
                ProjectionFindingCategory.COMMONALITY,
            )
        ),
    )
    assert {row.row_id for row in category_or.rows} == {subject_id("a1"), subject_id("b1")}
    rule_or = aggregate_subject_findings(
        synthetic_facts,
        ProjectionSelection(finding_rule_ids=("cohesion.module", "commonality.shared")),
    )
    assert {row.row_id for row in rule_or.rows} == {subject_id("a1"), subject_id("b1")}
    contradiction = aggregate_subject_findings(
        synthetic_facts,
        ProjectionSelection(
            finding_categories=(ProjectionFindingCategory.COHESION,),
            finding_severities=(ProjectionFindingSeverity.INFO,),
        ),
    )
    assert contradiction.rows == ()
    conjunction = aggregate_subject_findings(
        synthetic_facts,
        ProjectionSelection(
            finding_categories=(ProjectionFindingCategory.COHESION,),
            finding_severities=(ProjectionFindingSeverity.ERROR,),
            finding_rule_ids=("cohesion.module",),
        ),
    )
    assert [row.row_id for row in conjunction.rows] == [subject_id("a1")]


def test_aggregation_honours_dotted_prefix_boundaries(synthetic_facts) -> None:
    exact = aggregate_subject_findings(
        synthetic_facts, ProjectionSelection(module_prefixes=("package.alpha",))
    )
    assert {row.row_id for row in exact.rows} == {subject_id("a1"), subject_id("a2")}
    partial = aggregate_subject_findings(
        synthetic_facts, ProjectionSelection(module_prefixes=("package.alph",))
    )
    assert partial.rows == ()
    assert partial.unscoped_finding_count == 1
    parent = aggregate_subject_findings(
        synthetic_facts, ProjectionSelection(module_prefixes=("package",))
    )
    assert {row.row_id for row in parent.rows} == {
        subject_id("a1"),
        subject_id("a2"),
        subject_id("b1"),
    }


def test_aggregation_preserves_selected_zero_match_subjects(synthetic_facts) -> None:
    selected = aggregate_subject_findings(
        synthetic_facts,
        ProjectionSelection(
            selected_ids=(subject_id("c1"),),
            finding_categories=(ProjectionFindingCategory.DUPLICATION,),
        ),
    )
    assert [row.row_id for row in selected.rows] == [subject_id("c1")]
    assert selected.rows[0].cells[POSITIONS["finding-count"]].integer_value == 0
    assert selected.rows[0].cells[POSITIONS["category-counts"]].string_value == ""
    assert selected.rows[0].evidence_ids == ()
    assert selected.unscoped_finding_count == 1
    with pytest.raises(SubjectFindingSelectionError, match="selected subject is absent"):
        aggregate_subject_findings(
            synthetic_facts, ProjectionSelection(selected_ids=(subject_id("zz"),))
        )


def test_aggregation_rejects_unsupported_language_selection(synthetic_facts) -> None:
    """Unsupported selection must never look like successfully filtered evidence."""
    for languages in (
        (SourceLanguage.PYTHON,),
        (SourceLanguage.JAVASCRIPT,),
        (SourceLanguage.JAVASCRIPT, SourceLanguage.PYTHON),
    ):
        with pytest.raises(SubjectFindingSelectionError, match="language filtering is unsupported"):
            aggregate_subject_findings(synthetic_facts, ProjectionSelection(languages=languages))


def test_aggregation_order_is_independent_of_fact_order(synthetic_facts) -> None:
    reversed_facts = SubjectFindingFacts(
        subjects=tuple(reversed(synthetic_facts.subjects)),
        findings=tuple(reversed(synthetic_facts.findings)),
    )
    selection = ProjectionSelection()
    assert aggregate_subject_findings(reversed_facts, selection) == aggregate_subject_findings(
        synthetic_facts, selection
    )


def test_projection_rejects_unsupported_language_selection(ordinary_report) -> None:
    with pytest.raises(ProjectionBuildError, match="language filtering is unsupported"):
        build_projection(
            ordinary_report,
            ProjectionName.SUBJECT_FINDINGS,
            languages=(SourceLanguage.PYTHON,),
        )


def test_aggregation_rejects_untyped_inputs(synthetic_facts) -> None:
    with pytest.raises(TypeError, match="facts must be"):
        aggregate_subject_findings({"subjects": ()}, ProjectionSelection())
    with pytest.raises(TypeError, match="validated ProjectionSelection"):
        aggregate_subject_findings(synthetic_facts, {"max_records": 10})


def parity_cases(report) -> list[tuple[str, dict[str, object]]]:
    zero_match = [
        subject.subject_id
        for subject in report.subjects
        if all(subject.subject_id not in item.subject_ids for item in report.findings)
    ]
    scoped = sorted({sid for item in report.findings for sid in item.subject_ids})
    modules = sorted({subject.module for subject in report.subjects})
    rules = sorted({item.rule_id for item in report.findings})
    categories = sorted({item.category for item in report.findings})
    cases: list[tuple[str, dict[str, object]]] = [
        ("default", {}),
        ("bounded", {"max_records": 1}),
        ("bounded-two", {"max_records": 2}),
    ]
    for value in categories:
        cases.append(
            (f"category={value}", {"finding_categories": (ProjectionFindingCategory(value),)})
        )
    if len(categories) >= 2:
        cases.append(
            (
                "category-or",
                {
                    "finding_categories": tuple(
                        sorted(
                            (
                                ProjectionFindingCategory(categories[0]),
                                ProjectionFindingCategory(categories[1]),
                            ),
                            key=lambda item: item.value,
                        )
                    )
                },
            )
        )
    for value in ("info", "warning", "error"):
        cases.append(
            (f"severity={value}", {"finding_severities": (ProjectionFindingSeverity(value),)})
        )
    for value in rules[:6]:
        cases.append((f"rule={value}", {"finding_rule_ids": (value,)}))
    if len(rules) >= 2:
        cases.append(("rule-or", {"finding_rule_ids": tuple(sorted(rules[:2]))}))
    prefixes: set[str] = set()
    for module in modules:
        parts = module.split(".")
        for index in range(1, len(parts) + 1):
            prefixes.add(".".join(parts[:index]))
        prefixes.add(module[:-1] or module)
    for value in sorted(prefixes):
        if all(part.isidentifier() for part in value.split(".")):
            cases.append((f"prefix={value}", {"module_prefixes": (value,)}))
    for value in (*zero_match[:2], *scoped[:2]):
        cases.append((f"selected={value}", {"selected_ids": (value,)}))
        cases.append(
            (
                f"selected-filtered={value}",
                {
                    "selected_ids": (value,),
                    "finding_categories": (ProjectionFindingCategory.DUPLICATION,),
                },
            )
        )
    if len(scoped) >= 2:
        cases.append(("selected-multi", {"selected_ids": tuple(sorted(scoped[:2]))}))
    return cases


def assert_projection_parity(report, label: str, kwargs: dict[str, object]) -> None:
    projection = build_projection(report, ProjectionName.SUBJECT_FINDINGS, **kwargs)
    assert isinstance(projection, TableProjection)
    aggregate = aggregate_subject_findings(subject_finding_facts(report), projection.selection)
    limit = projection.selection.max_records
    assert projection.rows == aggregate.rows[:limit], label
    assert projection.record_count == len(aggregate.rows[:limit]), label
    assert projection.truncated == (len(aggregate.rows) > limit), label
    assert projection.columns == subject_finding_columns(), label
    disclosed = [item for item in projection.limitations if "without subject identities" in item]
    expected = (
        [unscoped_scope_limitation(aggregate.unscoped_finding_count)]
        if aggregate.unscoped_finding_count
        else []
    )
    assert disclosed == expected, label


@pytest.mark.parametrize("golden", ["phase3-report.json", "javascript-report.json"])
def test_shared_aggregation_matches_projection_rows_for_golden_reports(golden: str) -> None:
    report = load_json((GOLDEN / golden).read_text(encoding="utf-8"))
    for source in (report, mixed_report(report)):
        for label, kwargs in parity_cases(source):
            assert_projection_parity(source, f"{golden}:{label}", kwargs)


def test_shared_aggregation_matches_projection_rows_for_scanned_report(ordinary_report) -> None:
    for label, kwargs in parity_cases(ordinary_report):
        assert_projection_parity(ordinary_report, f"ordinary:{label}", kwargs)


def test_golden_renders_are_reproduced_through_the_shared_aggregation() -> None:
    report = load_json((GOLDEN / "phase3-report.json").read_text(encoding="utf-8"))
    for kwargs, filenames in (
        (
            {},
            (
                (ProjectionFormat.JSON, "projection-subject-findings.json"),
                (ProjectionFormat.TSV, "projection-subject-findings.tsv"),
                (ProjectionFormat.MARKDOWN, "projection-subject-findings.md"),
            ),
        ),
        (
            {
                "module_prefixes": ("package.combine_a",),
                "finding_categories": (ProjectionFindingCategory.DUPLICATION,),
                "finding_severities": (ProjectionFindingSeverity.WARNING,),
                "finding_rule_ids": ("duplication.normalized-ast",),
            },
            (
                (ProjectionFormat.JSON, "projection-subject-findings-filtered.json"),
                (ProjectionFormat.TSV, "projection-subject-findings-filtered.tsv"),
                (ProjectionFormat.MARKDOWN, "projection-subject-findings-filtered.md"),
            ),
        ),
    ):
        projection = build_projection(report, ProjectionName.SUBJECT_FINDINGS, **kwargs)
        for output_format, filename in filenames:
            assert render_projection(projection, output_format) == (GOLDEN / filename).read_text(
                encoding="utf-8"
            )


def test_facade_translates_selection_errors_into_projection_build_errors() -> None:
    report = load_json((GOLDEN / "phase3-report.json").read_text(encoding="utf-8"))
    with pytest.raises(ProjectionBuildError, match="selected subject is absent"):
        build_projection(
            report,
            ProjectionName.SUBJECT_FINDINGS,
            selected_ids=("subject-00000000000000000000",),
        )
