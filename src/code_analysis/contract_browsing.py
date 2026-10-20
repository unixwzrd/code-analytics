"""Saved contract and dictionary inventories without flattening variants or defaults."""

from __future__ import annotations

from collections.abc import Iterator

from pydantic import Field, StrictInt, StrictStr, model_validator

from code_analysis.models import SourceLanguage, StrictModel
from code_analysis.report_models import (
    AnalysisReport,
    AnalysisSubject,
    ContractDefinition,
    DictionaryShape,
    SectionCoverage,
)
from code_analysis.ui_models import UIPage


class ContractDetail(StrictModel):
    owner: AnalysisSubject
    contract: ContractDefinition

    @model_validator(mode="after")
    def reconcile(self) -> ContractDetail:
        if self.owner.subject_id != self.contract.defining_subject_id:
            raise ValueError("contract owner identity mismatch")
        return self


class ShapeDetail(StrictModel):
    owner: AnalysisSubject
    shape: DictionaryShape

    @model_validator(mode="after")
    def reconcile(self) -> ShapeDetail:
        if self.owner.subject_id != self.shape.owner_subject_id:
            raise ValueError("dictionary owner identity mismatch")
        return self


class ContractRow(StrictModel):
    identity: StrictStr
    module: StrictStr
    owner: StrictStr
    language: SourceLanguage
    kind: StrictStr
    member_count: StrictInt = Field(ge=0)
    boundary_classes: tuple[StrictStr, ...]
    unresolved_count: StrictInt = Field(ge=0)
    detail: ContractDetail

    @model_validator(mode="after")
    def reconcile(self) -> ContractRow:
        owner, contract = self.detail.owner, self.detail.contract
        expected = (
            contract.contract_id,
            owner.module,
            owner.logical_name,
            owner.language,
            contract.kind.value,
            len(contract.members),
            tuple(sorted({item.boundary_class.value for item in contract.boundaries})),
            len(contract.unresolved_behavior),
        )
        actual = (
            self.identity,
            self.module,
            self.owner,
            self.language,
            self.kind,
            self.member_count,
            self.boundary_classes,
            self.unresolved_count,
        )
        if actual != expected:
            raise ValueError("contract display fields disagree with evidence")
        return self


class ShapeRow(StrictModel):
    identity: StrictStr
    module: StrictStr
    owner: StrictStr
    language: SourceLanguage
    variant_count: StrictInt = Field(ge=1)
    field_occurrences: StrictInt = Field(ge=0)
    lifecycle_state_count: StrictInt = Field(ge=0)
    unknown_field_policy: StrictStr
    detail: ShapeDetail

    @model_validator(mode="after")
    def reconcile(self) -> ShapeRow:
        owner, shape = self.detail.owner, self.detail.shape
        expected = (
            shape.shape_id,
            owner.module,
            owner.logical_name,
            owner.language,
            len(shape.variants),
            sum(len(item.fields) for item in shape.variants),
            len(shape.lifecycle_states),
            shape.unknown_field_policy.value,
        )
        actual = (
            self.identity,
            self.module,
            self.owner,
            self.language,
            self.variant_count,
            self.field_occurrences,
            self.lifecycle_state_count,
            self.unknown_field_policy,
        )
        if actual != expected:
            raise ValueError("dictionary display fields disagree with evidence")
        return self


class ContractPage(UIPage):
    records: tuple[ContractRow, ...]
    coverage: SectionCoverage

    @model_validator(mode="after")
    def reconcile(self) -> ContractPage:
        if self.coverage.section != "contracts" or self.retained != len(self.records):
            raise ValueError("contract page coverage or count mismatch")
        return self


class ShapePage(UIPage):
    records: tuple[ShapeRow, ...]
    coverage: SectionCoverage

    @model_validator(mode="after")
    def reconcile(self) -> ShapePage:
        if self.coverage.section != "dictionary-shapes" or self.retained != len(self.records):
            raise ValueError("dictionary page coverage or count mismatch")
        return self


def contract_rows(report: AnalysisReport) -> Iterator[ContractRow]:
    subjects = {item.subject_id: item for item in report.subjects}
    for contract in sorted(report.contracts, key=lambda item: item.contract_id):
        owner = subjects[contract.defining_subject_id]
        yield ContractRow(
            identity=contract.contract_id,
            module=owner.module,
            owner=owner.logical_name,
            language=owner.language,
            kind=contract.kind.value,
            member_count=len(contract.members),
            boundary_classes=tuple(sorted({x.boundary_class.value for x in contract.boundaries})),
            unresolved_count=len(contract.unresolved_behavior),
            detail=ContractDetail(owner=owner, contract=contract),
        )


def shape_rows(report: AnalysisReport) -> Iterator[ShapeRow]:
    subjects = {item.subject_id: item for item in report.subjects}
    for shape in sorted(report.dictionary_shapes, key=lambda item: item.shape_id):
        owner = subjects[shape.owner_subject_id]
        yield ShapeRow(
            identity=shape.shape_id,
            module=owner.module,
            owner=owner.logical_name,
            language=owner.language,
            variant_count=len(shape.variants),
            field_occurrences=sum(len(item.fields) for item in shape.variants),
            lifecycle_state_count=len(shape.lifecycle_states),
            unknown_field_policy=shape.unknown_field_policy.value,
            detail=ShapeDetail(owner=owner, shape=shape),
        )
