"""Strict contracts for configured finding suppressions (exact finding IDs only)."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictStr, field_validator, model_validator

from code_analysis.models import StrictModel

FINDING_ID_MAX_LENGTH = 512
RATIONALE_MAX_LENGTH = 4096


class FindingSuppressionStatus(StrEnum):
    ACCEPTED = "accepted"
    EXPIRED = "expired"


def validate_canonical_date_string(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a canonical ISO date string")
    if value != value.strip():
        raise ValueError(f"{label} must be trimmed")
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError(f"{label} must use canonical ISO YYYY-MM-DD form")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must use canonical ISO YYYY-MM-DD form")
    return value


def suppression_status_for_dates(evaluated_on: str, expires_on: str) -> FindingSuppressionStatus:
    evaluated = validate_canonical_date_string(evaluated_on, label="evaluated_on")
    expiry = validate_canonical_date_string(expires_on, label="expires_on")
    if evaluated >= expiry:
        return FindingSuppressionStatus.EXPIRED
    return FindingSuppressionStatus.ACCEPTED


class AcceptedFindingSuppression(StrictModel):
    finding_id: StrictStr = Field(min_length=1, max_length=FINDING_ID_MAX_LENGTH)
    rationale: StrictStr = Field(min_length=1, max_length=RATIONALE_MAX_LENGTH)
    expires_on: StrictStr

    @field_validator("finding_id")
    @classmethod
    def validate_finding_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("finding_id must be trimmed")
        if not value:
            raise ValueError("finding_id must be nonempty")
        return value

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("rationale must be trimmed")
        if not value.strip():
            raise ValueError("rationale must be nonblank")
        return value

    @field_validator("expires_on")
    @classmethod
    def validate_expires_on(cls, value: str) -> str:
        return validate_canonical_date_string(value, label="expires_on")

    def evaluated(self, evaluated_on: str) -> FindingSuppression:
        return FindingSuppression(
            finding_id=self.finding_id,
            rationale=self.rationale,
            expires_on=self.expires_on,
            evaluated_on=evaluated_on,
            status=suppression_status_for_dates(evaluated_on, self.expires_on),
        )


class FindingSuppression(StrictModel):
    finding_id: StrictStr = Field(min_length=1, max_length=FINDING_ID_MAX_LENGTH)
    rationale: StrictStr = Field(min_length=1, max_length=RATIONALE_MAX_LENGTH)
    expires_on: StrictStr
    evaluated_on: StrictStr
    status: FindingSuppressionStatus

    @field_validator("finding_id")
    @classmethod
    def validate_finding_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("finding_id must be trimmed")
        if not value:
            raise ValueError("finding_id must be nonempty")
        return value

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("rationale must be trimmed")
        if not value.strip():
            raise ValueError("rationale must be nonblank")
        return value

    @field_validator("expires_on", "evaluated_on")
    @classmethod
    def validate_dates(cls, value: str, info) -> str:
        label = "expires_on" if info.field_name == "expires_on" else "evaluated_on"
        return validate_canonical_date_string(value, label=label)

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        expected = suppression_status_for_dates(self.evaluated_on, self.expires_on)
        if self.status is not expected:
            raise ValueError("suppression status must be expired iff evaluated_on >= expires_on")
        return self


class FindingSuppressionAnnotation(StrictModel):
    """Shared saved-query and table annotation, independent of display adapters."""

    status: Literal["not-suppressed", "accepted", "expired"]
    rationale: StrictStr = Field(default="", max_length=RATIONALE_MAX_LENGTH)
    evaluated_on: StrictStr = ""
    expires_on: StrictStr = ""

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        if self.status == "not-suppressed":
            if self.rationale or self.evaluated_on or self.expires_on:
                raise ValueError("not-suppressed findings must not carry suppression details")
            return self
        if not self.rationale.strip() or self.rationale != self.rationale.strip():
            raise ValueError("suppression rationale must be nonblank and trimmed")
        expected = suppression_status_for_dates(self.evaluated_on, self.expires_on)
        if self.status != expected.value:
            raise ValueError("suppression annotation contradicts its saved evaluation date")
        return self


def finding_suppression_summary(annotation: FindingSuppressionAnnotation) -> str:
    if annotation.status == "not-suppressed":
        return "not suppressed"
    return (
        f"{annotation.status}; expires {annotation.expires_on} "
        f"(evaluated {annotation.evaluated_on})"
    )


def finding_suppression_annotation(
    suppression: FindingSuppression | None,
) -> FindingSuppressionAnnotation:
    if suppression is None:
        return FindingSuppressionAnnotation(status="not-suppressed")
    return FindingSuppressionAnnotation(
        status=suppression.status.value,
        rationale=suppression.rationale,
        evaluated_on=suppression.evaluated_on,
        expires_on=suppression.expires_on,
    )
