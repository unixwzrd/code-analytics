"""Typed saved-cluster comparison rows shared by indexed and live browsing."""

from __future__ import annotations

from collections.abc import Iterator

from pydantic import Field, StrictInt, StrictStr, model_validator

from code_analysis.models import Completeness, Confidence, SourceLanguage, StrictModel
from code_analysis.report_models import (
    AnalysisReport,
    AnalysisSubject,
    CloneCluster,
    CloneFragment,
    CloneKind,
    SectionCoverage,
)
from code_analysis.ui_models import UIPage


class CloneMember(StrictModel):
    subject: AnalysisSubject
    fragment: CloneFragment

    @model_validator(mode="after")
    def reconcile(self) -> CloneMember:
        if (
            self.subject.subject_id != self.fragment.subject_id
            or self.subject.language is not self.fragment.language
        ):
            raise ValueError("clone member subject or language mismatch")
        return self


class CloneDetail(StrictModel):
    cluster: CloneCluster
    members: tuple[CloneMember, ...]

    @model_validator(mode="after")
    def reconcile(self) -> CloneDetail:
        if tuple(item.fragment.fragment_id for item in self.members) != self.cluster.fragment_ids:
            raise ValueError("clone detail must retain all ordered fragments")
        if (
            tuple(sorted({item.subject.subject_id for item in self.members}))
            != self.cluster.subject_ids
        ):
            raise ValueError("clone detail subjects disagree with cluster")
        if any(item.fragment.kind is not self.cluster.kind for item in self.members):
            raise ValueError("clone fragment kind mismatch")
        if len({item.fragment.language for item in self.members}) != 1:
            raise ValueError("clone comparison must remain within one language")
        return self


class CloneRow(StrictModel):
    cluster_id: StrictStr
    kind: CloneKind
    language: SourceLanguage
    modules: tuple[StrictStr, ...]
    subject_count: StrictInt = Field(ge=2)
    fragment_count: StrictInt = Field(ge=2)
    matched_nonblank_lines: StrictInt = Field(ge=0)
    confidence: Confidence
    completeness: Completeness
    detail: CloneDetail

    @model_validator(mode="after")
    def reconcile(self) -> CloneRow:
        cluster, members = self.detail.cluster, self.detail.members
        if (
            self.cluster_id != cluster.cluster_id
            or self.kind is not cluster.kind
            or self.language is not members[0].fragment.language
            or self.modules != tuple(sorted({item.subject.module for item in members}))
            or self.subject_count != len(cluster.subject_ids)
            or self.fragment_count != len(cluster.fragment_ids)
            or self.matched_nonblank_lines != sum(item.fragment.nonblank_lines for item in members)
            or self.confidence is not cluster.confidence
            or self.completeness is not cluster.completeness
        ):
            raise ValueError("clone display fields disagree with retained evidence")
        return self


class ClonePage(UIPage):
    records: tuple[CloneRow, ...]
    coverage: SectionCoverage

    @model_validator(mode="after")
    def reconcile(self) -> ClonePage:
        if self.coverage.section != "clones" or self.retained != len(self.records):
            raise ValueError("clone page coverage or row count mismatch")
        return self


def clone_rows(report: AnalysisReport) -> Iterator[CloneRow]:
    subjects = {item.subject_id: item for item in report.subjects}
    fragments = {item.fragment_id: item for item in report.clone_fragments}
    for cluster in sorted(
        report.clone_clusters, key=lambda item: (item.kind.value, item.cluster_id)
    ):
        members = tuple(
            CloneMember(
                subject=subjects[fragments[identity].subject_id], fragment=fragments[identity]
            )
            for identity in cluster.fragment_ids
        )
        yield CloneRow(
            cluster_id=cluster.cluster_id,
            kind=cluster.kind,
            language=members[0].fragment.language,
            modules=tuple(sorted({item.subject.module for item in members})),
            subject_count=len(cluster.subject_ids),
            fragment_count=len(cluster.fragment_ids),
            matched_nonblank_lines=sum(item.fragment.nonblank_lines for item in members),
            confidence=cluster.confidence,
            completeness=cluster.completeness,
            detail=CloneDetail(cluster=cluster, members=members),
        )
