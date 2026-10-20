"""Strict contracts for a disposable, report-bound browsing index."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.models import Completeness, StrictModel, TargetIdentity
from code_analysis.projection_models import ComparisonChangeCounts, ProjectionInputRole
from code_analysis.projections import ProjectionName
from code_analysis.report_models import SectionCoverage
from code_analysis.runtime_browse_models import RuntimeIndexSummary
from code_analysis.static_browse import (
    ALL_OPTIONAL_STATIC_PROJECTIONS,
    STATIC_PROJECTIONS,
    StaticProjectionSummary,
)
from code_analysis.static_graph_browse import GRAPH_PROJECTIONS, StaticGraphSummary
from code_analysis.static_graph_navigation import GraphNavigationReceipt
from code_analysis.subject_finding_browse import SubjectFindingFactCounts
from code_analysis.ui_models import FindingFilters, UIBootstrap
from code_analysis.ui_rows import BrowseTable


class BrowseTableCount(StrictModel):
    table: BrowseTable
    records: StrictInt = Field(ge=0, le=1_000_000)


class BrowseComparisonSummary(StrictModel):
    baseline_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    identical: StrictBool
    includes_unchanged: StrictBool
    counts: ComparisonChangeCounts | None
    eligible: StrictInt = Field(ge=0)
    retained: StrictInt = Field(ge=0, le=100_000)
    omitted: StrictInt = Field(ge=0)
    completeness: Completeness
    source_binding_count: StrictInt = Field(default=0, ge=0, le=100_000)

    @model_validator(mode="after")
    def reconcile(self) -> Self:
        if self.identical != (self.baseline_sha256 == self.candidate_sha256):
            raise ValueError("comparison equality must match report hashes")
        if self.eligible != self.retained + self.omitted:
            raise ValueError("comparison retention counts do not reconcile")
        if self.omitted and self.completeness is Completeness.COMPLETE:
            raise ValueError("omitted comparison records cannot be complete")
        if self.identical:
            if self.counts is not None or self.eligible or self.source_binding_count:
                raise ValueError("identical comparison cannot expand records or counts")
        else:
            if self.counts is None:
                raise ValueError("non-identical comparison requires global counts")
            eligible = self.counts.added + self.counts.removed + self.counts.changed
            if self.includes_unchanged:
                eligible += self.counts.unchanged
            if eligible != self.eligible:
                raise ValueError("comparison eligible records disagree with global counts")
        if self.source_binding_count != self.retained:
            raise ValueError("comparison source binding count must match retained rows")
        return self


class BrowseIndexManifest(StrictModel):
    schema_name: Literal["code-analysis-browse-index"] = "code-analysis-browse-index"
    schema_version: Literal["1.0.0"] = "1.0.0"
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    target: TargetIdentity
    builder_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    database_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    database_bytes: StrictInt = Field(ge=1, le=2 * 1024**3)
    source_file_count: StrictInt = Field(ge=0, le=1_000_000)
    candidate_source_file_count: StrictInt = Field(default=0, ge=0, le=1_000_000)
    coverage_files_count: StrictInt = Field(ge=0, le=1_000_000_000)
    coverage_cells_count: StrictInt = Field(ge=0, le=1_000_000_000)
    proposal_coverage_subject_count: StrictInt = Field(ge=0, le=1_000_000_000)
    state_effect_coverage: SectionCoverage
    clone_coverage: SectionCoverage
    contract_coverage: SectionCoverage
    shape_coverage: SectionCoverage
    test_coverage: SectionCoverage
    runtime: RuntimeIndexSummary | None = None
    overview: UIBootstrap
    tables: tuple[BrowseTableCount, ...]
    subject_finding_facts: SubjectFindingFactCounts
    static_projections: tuple[StaticProjectionSummary, ...]
    static_graphs: tuple[StaticGraphSummary, ...]
    graph_navigation: GraphNavigationReceipt
    comparison: BrowseComparisonSummary | None = None

    @model_validator(mode="after")
    def validate_identity_and_tables(self) -> Self:
        if tuple(item.name for item in self.static_graphs) != GRAPH_PROJECTIONS:
            raise ValueError("index requires each static graph summary exactly once")
        for graph in self.static_graphs:
            if (
                graph.source.report_sha256 != self.report_sha256
                or graph.source.role is not ProjectionInputRole.SOURCE
                or graph.source.report_schema_name != "code-analysis-report"
                or graph.source.report_schema_version != "1.0.0"
                or graph.source.run_identity_sha256 != self.overview.reports[0].run_identity_sha256
            ):
                raise ValueError("static graph belongs to another report")
            source_state = self.overview.reports[0].completeness
            if (
                source_state is Completeness.FAILED
                and graph.completeness is not Completeness.FAILED
            ) or (
                source_state is Completeness.PARTIAL and graph.completeness is Completeness.COMPLETE
            ):
                raise ValueError("static graph softens source completeness")
        names = tuple(item.name for item in self.static_projections)
        optional = names[len(STATIC_PROJECTIONS) :]
        canonical_optional = tuple(
            name for name in ALL_OPTIONAL_STATIC_PROJECTIONS if name in optional
        )
        if (
            names[: len(STATIC_PROJECTIONS)] != STATIC_PROJECTIONS
            or optional != canonical_optional
            or len(optional) != len(set(optional))
        ):
            raise ValueError("index requires each prepared static projection exactly once")
        candidate_count = next(
            (item.records for item in self.tables if item.table is BrowseTable.CANDIDATES), 0
        )
        worklist = next(
            (
                item
                for item in self.static_projections
                if item.name is ProjectionName.CANDIDATE_WORKLIST
            ),
            None,
        )
        if bool(candidate_count) != (worklist is not None):
            raise ValueError("candidate worklist availability must match saved candidate count")
        if worklist is not None and worklist.records != min(candidate_count, 100_000):
            raise ValueError("candidate worklist count must match preparation bounds")
        for name, source_count in (
            (ProjectionName.COVERAGE_FILES, self.coverage_files_count),
            (ProjectionName.COVERAGE_MATRIX, self.coverage_cells_count),
            (
                ProjectionName.UNCOVERED_PROPOSAL_SUBJECTS,
                self.proposal_coverage_subject_count,
            ),
        ):
            prepared = next((item for item in self.static_projections if item.name is name), None)
            if bool(source_count) != (prepared is not None):
                raise ValueError(f"{name.value} availability must match its source count")
            if prepared is not None and prepared.records != min(source_count, 100_000):
                raise ValueError(f"{name.value} count must match preparation bounds")
        static_call_graph = next(
            item for item in self.static_graphs if item.name is ProjectionName.STATIC_CALL_GRAPH
        )
        static_call_sites = next(
            (
                item
                for item in self.static_projections
                if item.name is ProjectionName.STATIC_CALL_SITES
            ),
            None,
        )
        if static_call_graph.available != (static_call_sites is not None):
            raise ValueError("static-call-sites availability must match static-call-graph section")
        if static_call_sites is not None and static_call_sites.records > 100_000:
            raise ValueError("static-call-sites count must match preparation bounds")
        subject_findings = next(
            item for item in self.static_projections if item.name is ProjectionName.SUBJECT_FINDINGS
        )
        if (
            subject_findings.records > self.subject_finding_facts.subjects
            or (subject_findings.unscoped_finding_count or 0) > self.subject_finding_facts.findings
        ):
            raise ValueError("subject-findings receipt disagrees with the retained fact counts")
        if any(item.source.report_sha256 != self.report_sha256 for item in self.static_projections):
            raise ValueError("prepared static projection belongs to another report")
        if any(
            item.source.report_schema_name != "code-analysis-report"
            or item.source.report_schema_version != "1.0.0"
            or item.completeness
            is not (
                self.overview.reports[0].completeness
                if self.overview.reports[0].completeness is not Completeness.COMPLETE
                else Completeness.PARTIAL if item.source_truncated else Completeness.COMPLETE
            )
            for item in self.static_projections
        ):
            raise ValueError("prepared static projection schema/completeness mismatch")
        if self.runtime is not None and self.runtime.overview.source_matches != (
            self.runtime.static_report_sha256 == self.report_sha256
        ):
            raise ValueError("runtime/static source binding disagrees with receipt")
        if self.comparison is None and self.candidate_source_file_count:
            raise ValueError("candidate source inventory requires comparison metadata")
        if self.runtime is not None and self.runtime.comparison is not None:
            candidate = self.runtime.comparison
            indexed_static_sha256s = {self.report_sha256}
            if self.comparison is not None:
                indexed_static_sha256s.add(self.comparison.candidate_sha256)
            if candidate.candidate_overview.source_matches != (
                candidate.candidate_static_sha256 in indexed_static_sha256s
            ):
                raise ValueError("runtime candidate/static binding disagrees with receipt")
        if self.state_effect_coverage.section != "state-side-effects":
            raise ValueError("browse state/effect coverage has the wrong section")
        if self.clone_coverage.section != "clones":
            raise ValueError("browse clone coverage has the wrong section")
        if self.contract_coverage.section != "contracts":
            raise ValueError("browse contract coverage has the wrong section")
        if self.shape_coverage.section != "dictionary-shapes":
            raise ValueError("browse dictionary coverage has the wrong section")
        if self.test_coverage.section != "test-impact":
            raise ValueError("browse test coverage has the wrong section")
        if tuple(item.table for item in self.tables) != tuple(BrowseTable):
            raise ValueError("browse index requires every table exactly once in declared order")
        if self.comparison is None:
            if len(self.overview.reports) != 1 or self.overview.comparison is not None:
                raise ValueError("single-report index cannot contain comparison metadata")
        else:
            if len(self.overview.reports) != 2 or self.overview.comparison is None:
                raise ValueError("comparison index requires both report identities")
            baseline, candidate = self.overview.reports
            if (
                any(
                    item.completeness is not Completeness.COMPLETE for item in (baseline, candidate)
                )
                and self.comparison.completeness is Completeness.COMPLETE
            ):
                raise ValueError("comparison cannot erase source partiality")
            if (
                candidate.role != "comparison"
                or self.comparison.baseline_sha256 != baseline.report_sha256
                or self.comparison.candidate_sha256 != candidate.report_sha256
            ):
                raise ValueError("comparison index report identities disagree")
            counts = self.comparison.counts
            expected = (
                self.comparison.identical,
                counts.added if counts else 0,
                counts.removed if counts else 0,
                counts.changed if counts else 0,
                counts.unchanged if counts else 0,
            )
            found = self.overview.comparison
            if expected != (
                found.identical,
                found.added,
                found.removed,
                found.changed,
                found.unchanged,
            ):
                raise ValueError("comparison overview global counts disagree")
        source = self.overview.reports[0]
        if source.role != "primary" or source.report_sha256 != self.report_sha256:
            raise ValueError("browse index overview must match the report identity")
        if self.target.logical_root != source.target_root:
            raise ValueError("browse index target must match the primary report root")
        counts = {item.label: item.value for item in self.overview.counts}
        for table, label in (
            (BrowseTable.MODULES, "Modules"),
            (BrowseTable.FINDINGS, "Findings"),
            (BrowseTable.CANDIDATES, "Candidates"),
        ):
            actual = next(item.records for item in self.tables if item.table is table)
            if counts.get(label) != actual:
                raise ValueError("browse index table counts must match the overview")
        return self


class BrowseRequest(FindingFilters):
    table: BrowseTable
    offset: StrictInt = Field(default=0, ge=0)
    limit: StrictInt = Field(default=25, ge=1, le=500)
    query: StrictStr = Field(default="", max_length=4096)

    @model_validator(mode="after")
    def validate_filter_scope(self) -> Self:
        if (
            self.facets.language is not None or self.facets.module_prefix or self.facets.owner
        ) and self.table not in {
            BrowseTable.MODULES,
            BrowseTable.FINDINGS,
            BrowseTable.STATE_EFFECTS,
            BrowseTable.CONTRACTS,
            BrowseTable.SHAPES,
            BrowseTable.CANDIDATES,
            BrowseTable.CLONES,
            BrowseTable.TEST_IMPACT,
        }:
            raise ValueError("facets require a source-scoped evidence table")
        if self.facets.owner and self.table not in {
            BrowseTable.STATE_EFFECTS,
            BrowseTable.CONTRACTS,
            BrowseTable.SHAPES,
        }:
            raise ValueError("owner filtering requires saved state/effect or contract ownership")
        if (
            self.severity or self.category is not None or self.confidence is not None
        ) and self.table is not BrowseTable.FINDINGS:
            raise ValueError("severity, category and confidence filtering require findings")
        if self.query and self.table is BrowseTable.PROVIDERS:
            raise ValueError("provider search is not supported")
        return self
