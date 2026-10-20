from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.cli import _report_exit
from code_analysis.config import AnalysisConfig
from code_analysis.finding_suppression_models import AcceptedFindingSuppression
from code_analysis.finding_suppressions import apply_finding_suppressions
from code_analysis.project import discover_context
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json, load_json
from code_analysis.service import (
    scan,
    scan_clones,
    scan_contract_inventory,
    scan_dictionary_shapes,
    scan_refactor,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ordinary"


def config_for(identity: str, *, evaluated: str = "2026-09-22") -> AnalysisConfig:
    return AnalysisConfig(
        accepted_suppressions=(
            AcceptedFindingSuppression(
                finding_id=identity, rationale="Reviewed exact finding", expires_on="2026-09-23"
            ),
        ),
        suppression_evaluation_date=evaluated,
    )


def test_annotation_preserves_findings_evidence_counts_and_exit_policy(ordinary_report):
    identity = ordinary_report.findings[0].finding_id
    config = config_for(identity)
    annotated = apply_finding_suppressions(ordinary_report, config)
    assert annotated.summary == ordinary_report.summary
    assert annotated.completeness == ordinary_report.completeness
    assert annotated.diagnostics == ordinary_report.diagnostics
    assert annotated.providers == ordinary_report.providers
    for before, after in zip(ordinary_report.findings, annotated.findings, strict=True):
        assert before.model_dump(exclude={"suppression"}) == after.model_dump(
            exclude={"suppression"}
        )
    assert annotated.findings[0].suppression.status.value == "accepted"
    assert _report_exit(annotated, fail_on_findings=True) == _report_exit(
        ordinary_report, fail_on_findings=True
    )
    assert canonical_json(annotated) == canonical_json(
        apply_finding_suppressions(ordinary_report, config)
    )
    assert apply_finding_suppressions(annotated, config) == annotated
    assert load_json(canonical_json(annotated)) == annotated


def test_expiry_is_bound_to_declared_date_not_view_time(ordinary_report):
    identity = ordinary_report.findings[0].finding_id
    accepted = apply_finding_suppressions(ordinary_report, config_for(identity))
    expired = apply_finding_suppressions(
        ordinary_report, config_for(identity, evaluated="2026-09-23")
    )
    assert expired.findings[0].suppression.status.value == "expired"
    assert expired.run_identity_sha256 != accepted.run_identity_sha256
    assert expired.summary == accepted.summary


def test_unmatched_rule_remains_inspectable_without_fabricating_finding(ordinary_report):
    annotated = apply_finding_suppressions(ordinary_report, config_for("not-in-this-report"))
    assert annotated.findings == ordinary_report.findings
    assert annotated.finding_suppression_evaluation.rules[0].finding_id == "not-in-this-report"


def test_dangling_and_contradictory_annotations_fail_closed(ordinary_report):
    annotated = apply_finding_suppressions(
        ordinary_report, config_for(ordinary_report.findings[0].finding_id)
    )
    raw = annotated.model_dump()
    raw["finding_suppression_evaluation"] = None
    with pytest.raises(ValidationError, match="saved evaluation policy"):
        AnalysisReport.model_validate(raw)
    raw = annotated.model_dump()
    raw["findings"][0]["suppression"] = None
    with pytest.raises(ValidationError, match="saved evaluation policy"):
        AnalysisReport.model_validate(raw)
    raw = annotated.model_dump()
    raw["findings"][0]["suppression"]["finding_id"] = "another-finding"
    with pytest.raises(ValidationError, match="different finding"):
        AnalysisReport.model_validate(raw)


@pytest.mark.parametrize(
    "analyze", [scan, scan_clones, scan_refactor, scan_contract_inventory, scan_dictionary_shapes]
)
def test_public_scan_facades_apply_only_after_assembly(analyze):
    baseline = analyze(discover_context(FIXTURE, AnalysisConfig()))
    assert baseline.findings
    config = config_for(baseline.findings[0].finding_id)
    report = analyze(discover_context(FIXTURE, config))
    assert report.findings[0].suppression is not None
    assert report.summary == baseline.summary
    assert report.completeness == baseline.completeness
    assert canonical_json(report) == canonical_json(analyze(discover_context(FIXTURE, config)))
