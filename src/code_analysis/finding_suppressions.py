"""Deterministic finding acceptance annotations; never delete analytical evidence."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Self

from pydantic import Field, StrictStr, field_validator, model_validator

from code_analysis.finding_suppression_models import (
    AcceptedFindingSuppression,
    validate_canonical_date_string,
)
from code_analysis.models import StrictModel

if TYPE_CHECKING:
    from code_analysis.config import AnalysisConfig
    from code_analysis.report_models import AnalysisReport


class FindingSuppressionEvaluation(StrictModel):
    evaluated_on: StrictStr
    rules: tuple[AcceptedFindingSuppression, ...] = Field(min_length=1, max_length=10000)

    @field_validator("evaluated_on")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return validate_canonical_date_string(value, label="evaluated_on")

    @model_validator(mode="after")
    def validate_rules(self) -> Self:
        identities = tuple(rule.finding_id for rule in self.rules)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("finding suppression rules must have unique sorted finding IDs")
        return self


def validate_finding_suppressions(report: AnalysisReport) -> None:
    evaluation = report.finding_suppression_evaluation
    expected = (
        {rule.finding_id: rule.evaluated(evaluation.evaluated_on) for rule in evaluation.rules}
        if evaluation is not None
        else {}
    )
    for finding in report.findings:
        if finding.suppression != expected.get(finding.finding_id):
            raise ValueError("finding suppression disagrees with saved evaluation policy")


def apply_finding_suppressions(report: AnalysisReport, config: AnalysisConfig) -> AnalysisReport:
    from code_analysis.report_models import AnalysisReport

    if not config.accepted_suppressions:
        return report
    if config.suppression_evaluation_date is None:
        raise ValueError("finding suppression requires an explicit evaluation date")
    evaluation = FindingSuppressionEvaluation(
        evaluated_on=config.suppression_evaluation_date,
        rules=config.accepted_suppressions,
    )
    if report.finding_suppression_evaluation == evaluation:
        validate_finding_suppressions(report)
        return report
    annotations = {
        rule.finding_id: rule.evaluated(evaluation.evaluated_on) for rule in evaluation.rules
    }
    findings = tuple(
        finding.model_copy(update={"suppression": annotations.get(finding.finding_id)})
        for finding in report.findings
    )
    identity = hashlib.sha256(
        json.dumps(
            {
                "prior": report.run_identity_sha256,
                "suppression": evaluation.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "findings": findings,
            "finding_suppression_evaluation": evaluation,
            "run_identity_sha256": identity,
        }
    )
