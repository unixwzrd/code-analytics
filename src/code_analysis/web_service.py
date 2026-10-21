"""Bounded, source-free report views shared by the prototype Web UI."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Literal

from code_analysis.clone_browsing import ClonePage, clone_rows
from code_analysis.code_browse import CodeBrowsePage, CodeBrowseReader, CodeBrowseRequest
from code_analysis.code_browse_graph import CodeGraphRequest, CodeGraphResult, build_code_graph
from code_analysis.code_browse_runtime import (
    CodeRuntimeSourceRequest,
    CodeRuntimeSourceResult,
    read_code_runtime_source,
)
from code_analysis.comparison_browse import ComparisonFilters
from code_analysis.comparisons import build_report_delta
from code_analysis.contract_browsing import ContractPage, ShapePage, contract_rows, shape_rows
from code_analysis.dependency_graph import DependencyGraphRequest, build_dependency_graph
from code_analysis.dependency_navigation import (
    DependencyRequest,
    DependencyResult,
    investigate_dependencies,
)
from code_analysis.models import Confidence
from code_analysis.module_navigation import ReportModuleReader
from code_analysis.profile_models import ProfileReport
from code_analysis.project_browsing import LocalSourceLocation, retained_source_location
from code_analysis.projection_models import (
    ComparisonProjection,
    GraphProjection,
    ProjectionFindingCategory,
    ProjectionInput,
    ProjectionInputRole,
)
from code_analysis.queries import build_query
from code_analysis.query_models import QueryResponse, QuerySelector, QuerySelectorKind
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json, load_json
from code_analysis.source_findings import (
    SourceFindingPage,
    SourceFindingRequest,
    read_source_findings,
)
from code_analysis.source_relationships import (
    SourceRelationshipPage,
    SourceRelationshipRequest,
    read_source_relationships,
)
from code_analysis.static_comparison_source import (
    StaticComparisonSourceBinding,
    build_comparison_source_bindings,
)
from code_analysis.test_browsing import (
    TestImpactPage,
    TestImpactRow,
    TestInvestigation,
    investigate_test,
    test_impact_rows,
)
from code_analysis.ui_models import (
    BrowseFacets,
    FindingFilters,
    UIArchitectureGraph,
    UIBootstrap,
    UICandidatePacket,
    UICandidatePage,
    UIComparisonCounts,
    UIComparisonPage,
    UIComparisonRow,
    UICount,
    UIDiagnosticPage,
    UIFindingPage,
    UIGraphEdge,
    UIGraphNode,
    UIModulePage,
    UIProviderPage,
    UIReportIdentity,
    UIStateEffectPage,
)
from code_analysis.ui_rows import (
    candidate_rows,
    finding_rows,
    module_rows,
    participant_matches,
    provider_rows,
    row_matches,
    state_effect_rows,
)

_MAX_REPORT_BYTES = 256 * 1024 * 1024
_EMPTY_FACETS = BrowseFacets()


@dataclass(frozen=True, slots=True)
class ReportViewService:
    primary: AnalysisReport
    comparison: AnalysisReport | None = None
    delta: ComparisonProjection | None = None
    source_location: LocalSourceLocation | None = field(default=None, repr=False, compare=False)
    _overview: UIBootstrap | None = field(default=None, init=False, repr=False, compare=False)
    _overview_lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)
    _module_reader: ReportModuleReader | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _module_lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)
    _comparison_source_bindings: tuple[StaticComparisonSourceBinding, ...] = field(
        default=(), init=False, repr=False, compare=False
    )
    _test_rows: dict[str, TestImpactRow] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _test_lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)
    _code_reader: CodeBrowseReader | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _code_lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.comparison is None and self.delta is not None:
            raise ValueError("comparison projection requires a comparison report")
        if self.comparison is not None and self.delta is None:
            object.__setattr__(
                self,
                "delta",
                build_report_delta(self.primary, self.comparison, max_records=100_000),
            )
        delta = self.delta
        if self.comparison is not None and delta is not None and not delta.identical:
            object.__setattr__(
                self,
                "_comparison_source_bindings",
                build_comparison_source_bindings(self.primary, self.comparison, delta),
            )

    @classmethod
    def load(cls, primary: Path, comparison: Path | None = None) -> ReportViewService:
        report = _load_bounded(primary)
        return cls(
            report,
            _load_bounded(comparison) if comparison else None,
            source_location=retained_source_location(primary, report),
        )

    def bootstrap(self) -> UIBootstrap:
        # Report selections are immutable snapshots. Cache only the small
        # validated overview, never an additional serialized report copy.
        # Publish under the lock so concurrent first requests do not each
        # rebuild the complete canonical evidence catalog for its identity.
        with self._overview_lock:
            if self._overview is None:
                object.__setattr__(self, "_overview", self._build_bootstrap())
            assert self._overview is not None
            return self._overview

    def _dependency_reader(self) -> ReportModuleReader:
        with self._module_lock:
            if self._module_reader is None:
                object.__setattr__(
                    self, "_module_reader", ReportModuleReader.from_report(self.primary)
                )
            assert self._module_reader is not None
            return self._module_reader

    def code_browse(self, request: CodeBrowseRequest) -> CodeBrowsePage:
        """Serve bounded saved outlines; source disclosure is a separate service."""
        return self._code_browse_reader().read(request)

    def source_findings(self, request: SourceFindingRequest) -> SourceFindingPage:
        return read_source_findings(self.primary, request, reader=self._code_browse_reader())

    def source_relationships(self, request: SourceRelationshipRequest) -> SourceRelationshipPage:
        return read_source_relationships(self.primary, request, reader=self._code_browse_reader())

    def _code_browse_reader(self) -> CodeBrowseReader:
        with self._code_lock:
            if self._code_reader is None:
                object.__setattr__(
                    self,
                    "_code_reader",
                    CodeBrowseReader.from_report(
                        self.primary, self.bootstrap().reports[0].report_sha256
                    ),
                )
            assert self._code_reader is not None
            return self._code_reader

    def code_graph(
        self, request: CodeGraphRequest, profile: ProfileReport | None = None
    ) -> CodeGraphResult:
        """Build an explicitly focused relationship layer from saved evidence."""
        return build_code_graph(self.primary, self._code_browse_reader(), request, profile)

    def code_runtime_source(
        self, request: CodeRuntimeSourceRequest, profile: ProfileReport | None = None
    ) -> CodeRuntimeSourceResult:
        return read_code_runtime_source(self.primary, self._code_browse_reader(), request, profile)

    def dependencies(self, request: DependencyRequest) -> DependencyResult:
        return investigate_dependencies(self._dependency_reader(), request)

    def dependency_graph(self, request: DependencyGraphRequest) -> GraphProjection:
        return build_dependency_graph(
            self._dependency_reader(),
            request,
            ProjectionInput(
                role=ProjectionInputRole.SOURCE,
                report_schema_name=self.primary.schema_name,
                report_schema_version=self.primary.schema_version,
                report_sha256=self.bootstrap().reports[0].report_sha256,
            ),
        )

    def _build_bootstrap(self) -> UIBootstrap:
        reports: tuple[UIReportIdentity, ...] = (self._identity(self.primary, "primary"),)
        delta_counts = None
        if self.comparison is not None:
            reports += (self._identity(self.comparison, "comparison"),)
            assert self.delta is not None
            delta = self.delta
            change_counts = delta.change_counts
            delta_counts = UIComparisonCounts(
                identical=delta.identical,
                added=change_counts.added if change_counts else 0,
                removed=change_counts.removed if change_counts else 0,
                changed=change_counts.changed if change_counts else 0,
                unchanged=change_counts.unchanged if change_counts else 0,
            )
        summary = self.primary.summary
        counts = (
            UICount(label="Modules", value=summary.modules),
            UICount(label="Symbols", value=summary.classes + summary.functions + summary.methods),
            UICount(label="Findings", value=summary.findings),
            UICount(label="Candidates", value=summary.refactor_candidates),
            UICount(label="Cycles", value=summary.cycles),
            UICount(label="Unresolved", value=summary.unresolved_edges),
        )
        return UIBootstrap(
            reports=reports,
            counts=counts,
            languages=self.primary.target.languages,
            limitations=self.primary.limitations,
            comparison=delta_counts,
            finding_categories=tuple(
                ProjectionFindingCategory(value)
                for value in sorted({item.category for item in self.primary.findings})
            ),
            finding_confidences=tuple(
                sorted(
                    {item.confidence for item in self.primary.findings},
                    key=lambda value: value.value,
                )
            ),
        )

    def modules(
        self, *, offset: int, limit: int, query: str = "", facets: BrowseFacets = _EMPTY_FACETS
    ) -> UIModulePage:
        if facets.owner:
            raise ValueError("owner filtering requires saved state/effect ownership")
        records = tuple(
            item
            for item in module_rows(self.primary)
            if row_matches(item, query) and facets.matches(item.language, item.module)
        )
        return _page(records, offset, limit, UIModulePage)

    def findings(
        self,
        *,
        offset: int,
        limit: int,
        query: str = "",
        severity: str = "",
        category: str = "",
        confidence: str = "",
        facets: BrowseFacets = _EMPTY_FACETS,
    ) -> UIFindingPage:
        if facets.owner:
            raise ValueError("owner filtering requires saved state/effect ownership")
        filters = FindingFilters.model_validate(
            {
                "severity": severity,
                "facets": facets,
                "category": ProjectionFindingCategory(category) if category else None,
                "confidence": Confidence(confidence) if confidence else None,
            }
        )
        records = tuple(
            item
            for item in finding_rows(self.primary)
            if filters.matches(item) and row_matches(item, query)
        )
        return _page(records, offset, limit, UIFindingPage)

    def candidates(
        self, *, offset: int, limit: int, query: str = "", facets: BrowseFacets = _EMPTY_FACETS
    ) -> UICandidatePage:
        if facets.owner:
            raise ValueError("candidate participant scopes do not establish ownership")
        records = tuple(
            item
            for item in candidate_rows(self.primary)
            if row_matches(item, query) and participant_matches(item, facets)
        )
        return _page(records, offset, limit, UICandidatePage)

    def providers(self, *, offset: int, limit: int) -> UIProviderPage:
        records = tuple(provider_rows(self.primary))
        return _page(records, offset, limit, UIProviderPage)

    def contracts(
        self, *, offset: int, limit: int, query: str = "", facets: BrowseFacets = _EMPTY_FACETS
    ) -> ContractPage:
        coverage = next(
            item for item in self.primary.section_coverage if item.section == "contracts"
        )
        records = tuple(
            item
            for item in contract_rows(self.primary)
            if row_matches(item, query) and facets.matches(item.language, item.module, item.owner)
        )
        return _page(
            records, offset, limit, lambda **values: ContractPage(coverage=coverage, **values)
        )

    def dictionary_shapes(
        self, *, offset: int, limit: int, query: str = "", facets: BrowseFacets = _EMPTY_FACETS
    ) -> ShapePage:
        coverage = next(
            item for item in self.primary.section_coverage if item.section == "dictionary-shapes"
        )
        records = tuple(
            item
            for item in shape_rows(self.primary)
            if row_matches(item, query) and facets.matches(item.language, item.module, item.owner)
        )
        return _page(
            records, offset, limit, lambda **values: ShapePage(coverage=coverage, **values)
        )

    def test_impact(
        self, *, offset: int, limit: int, query: str = "", facets: BrowseFacets = _EMPTY_FACETS
    ) -> TestImpactPage:
        if facets.owner:
            raise ValueError("test source scopes do not establish ownership")
        coverage = next(
            item for item in self.primary.section_coverage if item.section == "test-impact"
        )
        records = tuple(
            item
            for item in self._test_inventory().values()
            if row_matches(item, query) and facets.matches(item.language, item.module)
        )
        return _page(
            records, offset, limit, lambda **values: TestImpactPage(coverage=coverage, **values)
        )

    def _test_inventory(self) -> dict[str, TestImpactRow]:
        with self._test_lock:
            if self._test_rows is None:
                object.__setattr__(
                    self,
                    "_test_rows",
                    {row.identity: row for row in test_impact_rows(self.primary)},
                )
            assert self._test_rows is not None
            return self._test_rows

    def test_detail(self, identity: str) -> TestInvestigation:
        row = self._test_inventory().get(identity)
        if row is None:
            raise ValueError("test identity is absent from the selected report")
        return investigate_test(row)

    def clones(
        self, *, offset: int, limit: int, query: str = "", facets: BrowseFacets = _EMPTY_FACETS
    ) -> ClonePage:
        if facets.owner:
            raise ValueError("clone participant scopes do not establish ownership")
        coverage = next(item for item in self.primary.section_coverage if item.section == "clones")
        records = tuple(
            item
            for item in clone_rows(self.primary)
            if row_matches(item, query) and participant_matches(item, facets)
        )
        return _page(
            records, offset, limit, lambda **values: ClonePage(coverage=coverage, **values)
        )

    def state_effects(
        self, *, offset: int, limit: int, query: str = "", facets: BrowseFacets = _EMPTY_FACETS
    ) -> UIStateEffectPage:
        coverage = next(
            item for item in self.primary.section_coverage if item.section == "state-side-effects"
        )
        records = tuple(
            item
            for item in state_effect_rows(self.primary)
            if row_matches(item, query) and facets.matches(item.language, item.module, item.owner)
        )
        return _page(
            records, offset, limit, lambda **values: UIStateEffectPage(coverage=coverage, **values)
        )

    def candidate_packet(
        self, identity: str, *, limit: int = 25, offset: int = 0
    ) -> UICandidatePacket:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("candidate packet limit must be between 1 and 50")
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ValueError("candidate packet offset must be between zero and one million")
        candidate = next(
            (item for item in self.primary.refactor_candidates if item.candidate_id == identity),
            None,
        )
        if candidate is None:
            raise ValueError("candidate identity is not present in this report")
        observation = next(
            item for item in self.primary.candidate_observations if item.candidate_id == identity
        )
        subjects = tuple(
            item
            for item in self.primary.subjects
            if item.subject_id in candidate.participating_subject_ids
        )
        owners = tuple(
            item for item in self.primary.owner_alternatives if item.candidate_id == identity
        )
        proposals = tuple(
            item for item in self.primary.refactor_proposals if item.candidate_id == identity
        )
        differences = tuple(
            item
            for item in self.primary.candidate_behavior_differences
            if item.candidate_id == identity
        )
        qualification = tuple(
            item
            for item in self.primary.candidate_qualification_requirements
            if item.candidate_id == identity
        )
        sections = (
            ("owners", owners),
            ("proposals", proposals),
            ("differences", differences),
            ("qualification", qualification),
            ("subjects", subjects),
        )
        largest = max(len(records) for _, records in sections)
        end = offset + limit
        return UICandidatePacket(
            offset=offset,
            limit=limit,
            totals=tuple(UICount(label=label, value=len(records)) for label, records in sections),
            next_offset=end if end < largest else None,
            candidate=candidate,
            observation=observation,
            subjects=subjects[offset:end],
            cost=next(
                (
                    item
                    for item in self.primary.abstraction_cost_estimates
                    if item.candidate_id == identity
                ),
                None,
            ),
            owners=owners[offset:end],
            proposals=proposals[offset:end],
            differences=differences[offset:end],
            qualification=qualification[offset:end],
            omitted=tuple(
                UICount(label=label, value=len(records) - len(records[offset:end]))
                for label, records in sections
            ),
            source_completeness=self.primary.completeness,
        )

    def diagnostics(self, *, offset: int, limit: int, query: str = "") -> UIDiagnosticPage:
        records = tuple(item for item in self.primary.diagnostics if row_matches(item, query))
        return _page(records, offset, limit, UIDiagnosticPage)

    def evidence(
        self, *, kind: str, identity: str, limit: int = 50, after: str | None = None
    ) -> QueryResponse:
        return build_query(
            self.primary,
            QuerySelector(kind=QuerySelectorKind(kind), identity=identity),
            limit=limit,
            after=after,
        )

    def architecture(self, *, max_nodes: int = 60, max_edges: int = 120) -> UIArchitectureGraph:
        if not 1 <= max_nodes <= 200 or not 1 <= max_edges <= 500:
            raise ValueError("architecture bounds require 1-200 nodes and 1-500 edges")
        finding_counts: dict[str, int] = {}
        for finding in self.primary.findings:
            finding_counts[finding.module] = finding_counts.get(finding.module, 0) + 1
        modules = tuple(
            sorted(
                self.primary.modules,
                key=lambda item: (
                    -(item.fan_in + item.fan_out),
                    -finding_counts.get(item.name, 0),
                    item.name,
                ),
            )
        )
        selected = modules[:max_nodes]
        known_names = {item.name for item in modules}
        selected_names = {item.name for item in selected}
        nodes = tuple(
            sorted(
                (
                    UIGraphNode(
                        node_id=item.name,
                        label=item.name,
                        language=item.language,
                        lines=item.line_count,
                        finding_count=finding_counts.get(item.name, 0),
                    )
                    for item in selected
                ),
                key=lambda item: item.node_id,
            )
        )
        eligible_edges = tuple(
            item
            for item in self.primary.import_edges
            if item.local and item.source in known_names and item.target in known_names
        )
        retained_edges = tuple(
            sorted(
                (
                    UIGraphEdge(source_id=item.source, target_id=item.target, kind=item.kind)
                    for item in eligible_edges
                    if item.source in selected_names and item.target in selected_names
                ),
                key=lambda item: (item.source_id, item.target_id, item.kind),
            )
        )[:max_edges]
        omitted_nodes = len(modules) - len(nodes)
        omitted_edges = len(eligible_edges) - len(retained_edges)
        return UIArchitectureGraph(
            eligible_nodes=len(modules),
            retained_nodes=len(nodes),
            omitted_nodes=omitted_nodes,
            eligible_edges=len(eligible_edges),
            retained_edges=len(retained_edges),
            omitted_edges=omitted_edges,
            truncated=bool(omitted_nodes or omitted_edges),
            nodes=nodes,
            edges=retained_edges,
        )

    def static_comparison_source_detail(self, position: int) -> StaticComparisonSourceBinding:
        if self.comparison is None or self.delta is None or self.delta.identical:
            raise ValueError("static comparison source binding is unavailable")
        bindings = self._comparison_source_bindings
        if type(position) is not int or not 0 <= position < len(bindings):
            raise ValueError("static comparison source binding is unavailable")
        return bindings[position]

    def comparison_records(
        self, *, offset: int, limit: int, filters: ComparisonFilters | None = None
    ) -> UIComparisonPage:
        selection = ComparisonFilters() if filters is None else filters
        if self.comparison is None:
            return UIComparisonPage(
                offset=offset,
                limit=limit,
                eligible=0,
                retained=0,
                truncated=False,
                records=(),
                total_eligible=0,
                omitted=0,
                completeness=self.primary.completeness,
            )
        assert self.delta is not None
        delta = self.delta
        rows = tuple(
            UIComparisonRow(
                position=position,
                record_kind=self._comparison_source_bindings[position].record_kind,
                subject_id=item.subject_id,
                change=item.change.value,
                explanation=item.explanation,
            )
            for position, item in enumerate(delta.records)
            if selection.matches(item, self._comparison_source_bindings[position].record_kind)
        )
        if offset < 0 or not 1 <= limit <= 500:
            raise ValueError("UI page requires offset >= 0 and limit between 1 and 500")
        selected = rows[offset : offset + limit]
        return UIComparisonPage(
            offset=offset,
            limit=limit,
            eligible=len(rows),
            retained=len(selected),
            truncated=offset + len(selected) < len(rows),
            records=selected,
            total_eligible=delta.eligible_record_count,
            omitted=delta.eligible_record_count - len(delta.records),
            filtered_out=len(delta.records) - len(rows),
            completeness=delta.completeness,
        )

    @staticmethod
    def _identity(
        report: AnalysisReport, role: Literal["primary", "comparison"]
    ) -> UIReportIdentity:
        rendered = canonical_json(report).encode()
        return UIReportIdentity(
            role=role,
            report_sha256=hashlib.sha256(rendered).hexdigest(),
            run_identity_sha256=report.run_identity_sha256,
            target_root=report.target.logical_root,
            command=report.command.name,
            completeness=report.completeness,
        )


def _load_bounded(path: Path) -> AnalysisReport:
    size = path.stat().st_size
    if size > _MAX_REPORT_BYTES:
        raise ValueError(f"report exceeds {_MAX_REPORT_BYTES} byte UI input bound")
    return load_json(path.read_text(encoding="utf-8"))


def _page[PageT](
    records: Sequence[object],
    offset: int,
    limit: int,
    model: Callable[..., PageT],
) -> PageT:
    if offset < 0 or not 1 <= limit <= 500:
        raise ValueError("UI page requires offset >= 0 and limit between 1 and 500")
    selected = tuple(records[offset : offset + limit])
    return model(
        offset=offset,
        limit=limit,
        eligible=len(records),
        retained=len(selected),
        truncated=offset + len(selected) < len(records),
        records=selected,
    )
