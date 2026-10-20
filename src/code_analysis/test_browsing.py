"""Typed static test inventories and relationships for saved-report browsing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.models import Completeness, SourceLanguage, StrictModel
from code_analysis.report_models import (
    AnalysisReport,
    AnalysisSubject,
    CoverageBindingState,
    SectionCoverage,
    SectionState,
    SymbolCoverageRecord,
    TestCaseRecord,
    TestFixtureRecord,
    TestModuleRecord,
    TestRelationshipKind,
    TestRelationshipRecord,
)
from code_analysis.ui_models import UIPage


class TestCoverageObservation(StrictModel):
    observation_id: StrictStr = Field(pattern=r"^coverage-observation-[0-9a-f]{20}$")
    coverage_id: StrictStr = Field(pattern=r"^coverage-[0-9a-f]{20}$")
    binding: CoverageBindingState
    completeness: Completeness
    covered_lines: StrictInt = Field(ge=0)
    missing_lines: StrictInt = Field(ge=0)
    contexts: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def reconcile(self) -> TestCoverageObservation:
        if (
            self.binding is not CoverageBindingState.BOUND
            and self.completeness is Completeness.COMPLETE
        ):
            raise ValueError("unbound test target coverage cannot be complete")
        return self


class TestTargetCoverage(StrictModel):
    subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    section_state: SectionState
    observations: tuple[TestCoverageObservation, ...] = Field(default=(), max_length=25)
    omitted: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="after")
    def reconcile(self) -> TestTargetCoverage:
        ids = tuple(item.observation_id for item in self.observations)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("test target coverage observations must be sorted and unique")
        if self.omitted and len(self.observations) != 25:
            raise ValueError("test target coverage omission requires a full retained page")
        return self


class TestImpactDetail(StrictModel):
    module: TestModuleRecord
    case: TestCaseRecord | None = None
    fixture: TestFixtureRecord | None = None
    relationship: TestRelationshipRecord | None = None
    target_subject: AnalysisSubject | None = None
    target_fixture: TestFixtureRecord | None = None
    target_coverage: TestTargetCoverage | None = None

    @model_validator(mode="after")
    def reconcile(self) -> TestImpactDetail:
        if self.case is not None and self.fixture is not None:
            raise ValueError("test context cannot be both a case and fixture")
        entity = self.case or self.fixture
        if entity is not None and entity.test_module_id != self.module.test_module_id:
            raise ValueError("test context module mismatch")
        relationship = self.relationship
        if self.target_coverage is not None and (
            self.target_subject is None
            or self.target_coverage.subject_id != self.target_subject.subject_id
        ):
            raise ValueError("test target coverage requires the exact application subject")
        if relationship is None:
            if self.target_subject is not None or self.target_fixture is not None:
                raise ValueError("test inventory cannot invent a relationship target")
        else:
            source = (
                self.case.test_id
                if self.case
                else self.fixture.fixture_id if self.fixture else None
            )
            if relationship.source_test_id != source:
                raise ValueError("test relationship source mismatch")
            if relationship.source_test_module_id != (
                self.module.test_module_id if source is None else None
            ):
                raise ValueError("test relationship module source mismatch")
            if relationship.target_subject_id != (
                self.target_subject.subject_id if self.target_subject else None
            ):
                raise ValueError("test relationship subject target mismatch")
            if relationship.target_fixture_id != (
                self.target_fixture.fixture_id if self.target_fixture else None
            ):
                raise ValueError("test relationship fixture target mismatch")
        return self


class TestImpactRow(StrictModel):
    identity: StrictStr
    record_kind: Literal["module", "test", "fixture", "relationship"]
    module: StrictStr
    name: StrictStr
    language: SourceLanguage
    relationship_kind: TestRelationshipKind | None
    target: StrictStr | None
    resolved: StrictBool | None
    detail: TestImpactDetail

    @model_validator(mode="after")
    def reconcile(self) -> TestImpactRow:
        detail = self.detail
        entity = detail.case or detail.fixture
        relationship = detail.relationship
        identity = (
            relationship.relationship_id
            if relationship
            else (
                detail.case.test_id
                if detail.case
                else detail.fixture.fixture_id if detail.fixture else detail.module.test_module_id
            )
        )
        kind = (
            "relationship"
            if relationship
            else "test" if detail.case else "fixture" if detail.fixture else "module"
        )
        expected = (
            identity,
            kind,
            detail.module.module,
            entity.qualified_name if entity else detail.module.module,
            detail.module.language,
            relationship.kind if relationship else None,
            _target(detail),
            relationship.resolved if relationship else None,
        )
        actual = (
            self.identity,
            self.record_kind,
            self.module,
            self.name,
            self.language,
            self.relationship_kind,
            self.target,
            self.resolved,
        )
        if actual != expected:
            raise ValueError("test-impact display fields disagree with evidence")
        return self


class TestImpactPage(UIPage):
    records: tuple[TestImpactRow, ...]
    coverage: SectionCoverage

    @model_validator(mode="after")
    def reconcile(self) -> TestImpactPage:
        if self.coverage.section != "test-impact" or self.retained != len(self.records):
            raise ValueError("test-impact page coverage or count mismatch")
        return self


class TestSourceChoice(StrictModel):
    choice_id: StrictStr = Field(pattern=r"^test-source-[0-9a-f]{20}$")
    role: Literal["test", "application", "fixture"]
    path: StrictStr
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    start: StrictInt = Field(ge=1)
    end: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def reconcile(self) -> TestSourceChoice:
        path = PurePosixPath(self.path)
        if (
            self.end < self.start
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in self.path
            or path.as_posix() != self.path
            or self.path == "."
        ):
            raise ValueError("test source requires a portable path and ordered span")
        location = (self.role, self.path, self.source_sha256, self.start, self.end)
        expected = (
            "test-source-"
            + hashlib.sha256(json.dumps(location, separators=(",", ":")).encode()).hexdigest()[:20]
        )
        if expected != self.choice_id:
            raise ValueError("test source identity disagrees with its content")
        return self


class TestInvestigation(StrictModel):
    row: TestImpactRow
    sources: tuple[TestSourceChoice, ...] = Field(max_length=25)
    omitted_sources: StrictInt = Field(ge=0)


def investigate_test(row: TestImpactRow) -> TestInvestigation:
    detail = row.detail
    module = detail.module
    entity = detail.relationship or detail.case or detail.fixture
    test_evidence = (
        tuple(
            item
            for item in entity.evidence
            if item.span.path == module.path and item.source_sha256 == module.source_sha256
        )
        if entity
        else ()
    )
    locations: list[tuple[Literal["test", "application", "fixture"], str, str, int, int]] = [
        (
            "test",
            module.path,
            module.source_sha256,
            test_evidence[0].span.start_line if test_evidence else 1,
            test_evidence[0].span.end_line if test_evidence else 40,
        )
    ]
    targets: tuple[
        tuple[Literal["application", "fixture"], AnalysisSubject | TestFixtureRecord | None], ...
    ] = (
        ("application", detail.target_subject),
        ("fixture", detail.target_fixture),
    )
    for role, target in targets:
        if target is not None:
            locations.extend(
                (role, item.span.path, item.source_sha256, item.span.start_line, item.span.end_line)
                for item in target.evidence
            )
    ordered = sorted(set(locations))
    sources = tuple(
        TestSourceChoice(
            choice_id="test-source-"
            + hashlib.sha256(json.dumps(location, separators=(",", ":")).encode()).hexdigest()[:20],
            role=location[0],
            path=location[1],
            source_sha256=location[2],
            start=location[3],
            end=location[4],
        )
        for location in ordered[:25]
    )
    return TestInvestigation(row=row, sources=sources, omitted_sources=max(0, len(ordered) - 25))


def _target(detail: TestImpactDetail) -> str | None:
    relationship = detail.relationship
    if relationship is None:
        return None
    return (
        detail.target_subject.logical_name
        if detail.target_subject
        else (
            detail.target_fixture.qualified_name
            if detail.target_fixture
            else relationship.target_text or relationship.target_module
        )
    )


def _row(detail: TestImpactDetail) -> TestImpactRow:
    relationship = detail.relationship
    entity = detail.case or detail.fixture
    return TestImpactRow(
        identity=(
            relationship.relationship_id
            if relationship
            else (
                detail.case.test_id
                if detail.case
                else detail.fixture.fixture_id if detail.fixture else detail.module.test_module_id
            )
        ),
        record_kind=(
            "relationship"
            if relationship
            else "test" if detail.case else "fixture" if detail.fixture else "module"
        ),
        module=detail.module.module,
        name=entity.qualified_name if entity else detail.module.module,
        language=detail.module.language,
        relationship_kind=relationship.kind if relationship else None,
        target=_target(detail),
        resolved=relationship.resolved if relationship else None,
        detail=detail,
    )


def test_impact_rows(report: AnalysisReport) -> Iterator[TestImpactRow]:
    modules = {item.test_module_id: item for item in report.test_modules}
    cases = {item.test_id: item for item in report.test_cases}
    fixtures = {item.fixture_id: item for item in report.test_fixtures}
    subjects = {item.subject_id: item for item in report.subjects}
    coverage = _target_coverage(report)
    for module in sorted(modules.values(), key=lambda item: item.test_module_id):
        yield _row(TestImpactDetail(module=module))
    for test_case in sorted(cases.values(), key=lambda item: item.test_id):
        yield _row(TestImpactDetail(module=modules[test_case.test_module_id], case=test_case))
    for test_fixture in sorted(fixtures.values(), key=lambda item: item.fixture_id):
        yield _row(
            TestImpactDetail(module=modules[test_fixture.test_module_id], fixture=test_fixture)
        )
    for relationship in sorted(report.test_relationships, key=lambda item: item.relationship_id):
        case = cases.get(relationship.source_test_id or "")
        fixture = fixtures.get(relationship.source_test_id or "")
        entity = case or fixture
        module_id = entity.test_module_id if entity else relationship.source_test_module_id
        if module_id is None:
            raise ValueError("test relationship has no source module")
        yield _row(
            TestImpactDetail(
                module=modules[module_id],
                case=case,
                fixture=fixture,
                relationship=relationship,
                target_subject=subjects.get(relationship.target_subject_id or ""),
                target_fixture=fixtures.get(relationship.target_fixture_id or ""),
                target_coverage=coverage.get(relationship.target_subject_id or ""),
            )
        )


def _target_coverage(report: AnalysisReport) -> dict[str, TestTargetCoverage]:
    artifacts = {item.coverage_id: item for item in report.coverage_artifacts}
    targets = {
        item.target_subject_id for item in report.test_relationships if item.target_subject_id
    }
    observations: dict[str, list[SymbolCoverageRecord]] = {}
    for item in report.coverage_observations:
        if item.subject_id in targets:
            observations.setdefault(item.subject_id, []).append(item)
    state = next(item.state for item in report.section_coverage if item.section == "coverage")
    return {
        subject_id: TestTargetCoverage(
            subject_id=subject_id,
            section_state=state,
            observations=tuple(
                TestCoverageObservation(
                    observation_id=item.coverage_observation_id,
                    coverage_id=item.coverage_id,
                    binding=artifacts[item.coverage_id].binding,
                    completeness=artifacts[item.coverage_id].completeness,
                    covered_lines=len(item.covered_lines),
                    missing_lines=len(item.missing_lines),
                    contexts=len(item.contexts),
                )
                for item in sorted(
                    observations.get(subject_id, ()), key=lambda item: item.coverage_observation_id
                )[:25]
            ),
            omitted=max(0, len(observations.get(subject_id, ())) - 25),
        )
        for subject_id in sorted(targets)
    }
