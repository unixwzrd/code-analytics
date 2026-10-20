"""Strict local semantic-nomination evidence contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictFloat, StrictInt, StrictStr, model_validator

from code_analysis.models import Completeness, ProviderState, StrictModel


class SemanticNominationDisposition(StrEnum):
    DETERMINISTICALLY_SUPPORTED = "deterministically-supported"
    REJECTED_DETERMINISTIC_EVIDENCE = "rejected-deterministic-evidence"


class SemanticModelProvenance(StrictModel):
    model_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    configuration_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    vector_dimension: StrictInt = Field(ge=1)
    provider_version: StrictStr = Field(min_length=1)
    license_identity: StrictStr | None = None
    license_files: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_license_files(self) -> Self:
        if list(self.license_files) != sorted(self.license_files) or len(self.license_files) != len(
            set(self.license_files)
        ):
            raise ValueError("semantic license file identities must be unique and sorted")
        if bool(self.license_files) != bool(self.license_identity):
            raise ValueError("semantic license hash and file identities must be present together")
        return self


class SemanticNomination(StrictModel):
    nomination_id: StrictStr = Field(pattern=r"^semantic-nomination-[0-9a-f]{20}$")
    left_subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    right_subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    cosine_similarity: StrictFloat = Field(ge=-1.0, le=1.0)
    disposition: SemanticNominationDisposition
    deterministic_evidence_ids: tuple[StrictStr, ...] = ()
    candidate_ids: tuple[StrictStr, ...] = ()
    reason: StrictStr = Field(min_length=1)

    @model_validator(mode="after")
    def validate_nomination(self) -> Self:
        if self.left_subject_id >= self.right_subject_id:
            raise ValueError("semantic nomination subjects must use canonical order")
        for label, values in (
            ("deterministic evidence IDs", self.deterministic_evidence_ids),
            ("candidate IDs", self.candidate_ids),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"semantic {label} must be unique and sorted")
        if self.disposition is SemanticNominationDisposition.DETERMINISTICALLY_SUPPORTED and not (
            self.deterministic_evidence_ids or self.candidate_ids
        ):
            raise ValueError("supported semantic nomination requires deterministic evidence")
        return self


class SemanticNominationReport(StrictModel):
    schema_name: Literal["code-analysis-semantic-nomination-report"] = (
        "code-analysis-semantic-nomination-report"
    )
    schema_version: Literal["1.0.0"] = "1.0.0"
    source_report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    nomination_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: SemanticModelProvenance
    provider_state: Literal[ProviderState.SUCCESS] = ProviderState.SUCCESS
    considered_pairs: StrictInt = Field(ge=0)
    retained_nominations: StrictInt = Field(ge=0)
    supported_nominations: StrictInt = Field(ge=0)
    rejected_nominations: StrictInt = Field(ge=0)
    omitted_pairs: StrictInt = Field(ge=0)
    nominations: tuple[SemanticNomination, ...] = ()
    rejection_example_ids: tuple[StrictStr, ...] = Field(max_length=25)
    completeness: Completeness
    limitations: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        identities = [item.nomination_id for item in self.nominations]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("semantic nomination IDs must be unique and sorted")
        if self.retained_nominations != len(self.nominations):
            raise ValueError("semantic retained count must match nominations")
        if self.considered_pairs != self.retained_nominations + self.omitted_pairs:
            raise ValueError("semantic nomination counts must reconcile")
        supported = sum(
            item.disposition is SemanticNominationDisposition.DETERMINISTICALLY_SUPPORTED
            for item in self.nominations
        )
        if self.supported_nominations != supported:
            raise ValueError("semantic supported count must match nominations")
        if self.rejected_nominations != self.retained_nominations - supported:
            raise ValueError("semantic rejected count must match nominations")
        rejected_ids = {
            item.nomination_id
            for item in self.nominations
            if item.disposition is SemanticNominationDisposition.REJECTED_DETERMINISTIC_EVIDENCE
        }
        if (
            list(self.rejection_example_ids) != sorted(self.rejection_example_ids)
            or len(self.rejection_example_ids) != len(set(self.rejection_example_ids))
            or not set(self.rejection_example_ids) <= rejected_ids
        ):
            raise ValueError("semantic rejection examples must be unique sorted rejected IDs")
        if list(self.limitations) != sorted(self.limitations) or len(self.limitations) != len(
            set(self.limitations)
        ):
            raise ValueError("semantic limitations must be unique and sorted")
        return self
