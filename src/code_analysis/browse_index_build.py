"""Explicit one-time preparation of a disposable browsing cache from validated reports."""

from __future__ import annotations

import errno
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from code_analysis.browse_index import (
    COMPARISON_SOURCE_TABLE_SQL,
    COMPARISON_TABLE_SQL,
    EVIDENCE_TABLE_SQL,
    FINDING_FILTER_INDEX_SQL,
    INDEX_TABLE_SQL,
    NAVIGATION_TABLE_SQL,
    PACKET_TABLE_SQL,
    SCOPE_TABLE_SQL,
    SOURCE_TABLE_SQL,
    BrowseIndex,
    artifact_digest,
    builder_identity,
    index_directory,
)
from code_analysis.browse_index_models import (
    BrowseComparisonSummary,
    BrowseIndexManifest,
    BrowseTableCount,
)
from code_analysis.clone_browsing import CloneRow
from code_analysis.contract_browsing import ContractRow, ShapeRow
from code_analysis.decisions import CandidateSignatureCatalog
from code_analysis.module_navigation import module_navigation_pages
from code_analysis.projection_models import ProjectionInputRole
from code_analysis.queries import PreparedQueries
from code_analysis.query_models import QuerySelector, QuerySelectorKind
from code_analysis.runtime_browse_storage import write_runtime_records
from code_analysis.static_browse import write_static_projections
from code_analysis.static_graph_browse import write_static_graphs
from code_analysis.static_graph_navigation import write_graph_navigation
from code_analysis.test_browsing import TestImpactRow
from code_analysis.ui_models import UICandidateRow, UIFindingRow, UIModuleRow, UIStateEffectRow
from code_analysis.ui_rows import BrowseTable, participant_scopes, search_fields, table_rows
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.web_service import ReportViewService


class BrowseIndexLimitError(ValueError):
    """Preparation cannot publish a complete cache within its explicit bounds."""


def _write_tables(
    connection: sqlite3.Connection, service: ReportViewService
) -> tuple[BrowseTableCount, ...]:
    connection.execute(INDEX_TABLE_SQL)
    connection.execute(SCOPE_TABLE_SQL)
    scope_count = 0
    counts: list[BrowseTableCount] = []
    for table in BrowseTable:
        count = 0
        for row in table_rows(service.primary, table):
            if count >= 1_000_000:
                raise BrowseIndexLimitError(
                    "browse table exceeds the one-million-record preparation bound"
                )
            payload = row.model_dump_json()
            if len(payload.encode()) > 1024 * 1024:
                raise BrowseIndexLimitError("browse row exceeds the one-MiB preparation bound")
            terms = search_fields(row)
            padded = (*terms, *("" for _ in range(4 - len(terms))))
            connection.execute(
                "INSERT INTO browse_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    table.value,
                    count,
                    payload,
                    *padded,
                    row.severity if isinstance(row, UIFindingRow) else "",
                    row.category if isinstance(row, UIFindingRow) else "",
                    row.confidence.value if isinstance(row, UIFindingRow) else "",
                    (
                        row.language.value
                        if isinstance(
                            row,
                            (
                                UIFindingRow,
                                UIModuleRow,
                                UIStateEffectRow,
                                ContractRow,
                                ShapeRow,
                                TestImpactRow,
                            ),
                        )
                        else ""
                    ),
                    (
                        row.module
                        if isinstance(
                            row,
                            (
                                UIFindingRow,
                                UIModuleRow,
                                UIStateEffectRow,
                                ContractRow,
                                ShapeRow,
                                TestImpactRow,
                            ),
                        )
                        else ""
                    ),
                    row.owner if isinstance(row, (UIStateEffectRow, ContractRow, ShapeRow)) else "",
                ),
            )
            if isinstance(row, (UICandidateRow, CloneRow)):
                scopes = participant_scopes(row)
                scope_count += len(scopes)
                if scope_count > 1_000_000:
                    raise BrowseIndexLimitError("participant scopes exceed one million records")
                connection.executemany(
                    "INSERT INTO browse_scopes VALUES (?, ?, ?, ?)",
                    ((table.value, count, item.language.value, item.module) for item in scopes),
                )
            count += 1
        counts.append(BrowseTableCount(table=table, records=count))
    connection.execute(FINDING_FILTER_INDEX_SQL)
    return tuple(counts)


def _write_packets(connection: sqlite3.Connection, service: ReportViewService) -> None:
    connection.execute(PACKET_TABLE_SQL)
    pages = 0
    total_bytes = 0
    for candidate in sorted(
        service.primary.refactor_candidates, key=lambda item: item.candidate_id
    ):
        offset = 0
        while True:
            if pages >= 1_000_000:
                raise BrowseIndexLimitError(
                    "candidate packet preparation exceeds one million pages"
                )
            packet = service.candidate_packet(candidate.candidate_id, offset=offset)
            payload = packet.model_dump_json()
            size = len(payload.encode())
            total_bytes += size
            if total_bytes > 2 * 1024**3:
                raise BrowseIndexLimitError("candidate packet payloads exceed two GiB")
            if size > 1024 * 1024:
                raise BrowseIndexLimitError(
                    "candidate packet exceeds the one-MiB preparation bound"
                )
            connection.execute(
                "INSERT INTO candidate_packets VALUES (?, ?, ?)",
                (candidate.candidate_id, offset, payload),
            )
            pages += 1
            if packet.next_offset is None:
                break
            offset = packet.next_offset


def _write_evidence(connection: sqlite3.Connection, service: ReportViewService) -> None:
    connection.execute(EVIDENCE_TABLE_SQL)
    if not service.primary.findings:
        return
    prepared = PreparedQueries.prepare(service.primary)
    pages = 0
    total_bytes = 0
    for finding in sorted(service.primary.findings, key=lambda item: item.finding_id):
        selector = QuerySelector(kind=QuerySelectorKind.FINDING, identity=finding.finding_id)
        after = None
        while True:
            page = prepared.build(selector, after=after)
            payload = page.model_dump_json()
            size = len(payload.encode())
            pages += 1
            total_bytes += size
            if pages > 1_000_000 or size > 1024**2 or total_bytes > 2 * 1024**3:
                raise BrowseIndexLimitError("finding evidence exceeds preparation bounds")
            connection.execute(
                "INSERT INTO finding_pages VALUES (?, ?, ?)",
                (finding.finding_id, after or "", payload),
            )
            if page.next_identity is None:
                break
            if after is not None and page.next_identity <= after:
                raise ValueError("finding evidence continuation did not advance")
            after = page.next_identity


def _write_navigation(connection: sqlite3.Connection, service: ReportViewService) -> None:
    connection.execute(NAVIGATION_TABLE_SQL)
    total = 0
    for number, page in enumerate(module_navigation_pages(service.primary)):
        payload = page.model_dump_json()
        size = len(payload.encode())
        total += size
        if number >= 1_000_000 or size > 1024**2 or total > 2 * 1024**3:
            raise BrowseIndexLimitError("module navigation exceeds preparation bounds")
        connection.execute(
            "INSERT INTO module_pages VALUES (?, ?, ?, ?)",
            (page.module.module, page.view, page.offset, payload),
        )


def _write_sources(connection: sqlite3.Connection, service: ReportViewService) -> int:
    connection.execute(SOURCE_TABLE_SQL)
    for number, source in enumerate(
        sorted(service.primary.source_files, key=lambda item: item.logical_path)
    ):
        payload = source.model_dump_json()
        if number >= 1_000_000 or len(payload.encode()) > 1024**2:
            raise BrowseIndexLimitError("source identity exceeds preparation bounds")
        connection.execute("INSERT INTO source_files VALUES (?, ?)", (source.logical_path, payload))
    candidate_count = 0
    if service.comparison is not None:
        connection.execute(COMPARISON_SOURCE_TABLE_SQL)
        for number, source in enumerate(
            sorted(service.comparison.source_files, key=lambda item: item.logical_path)
        ):
            payload = source.model_dump_json()
            if number >= 1_000_000 or len(payload.encode()) > 1024**2:
                raise BrowseIndexLimitError("candidate source identity exceeds preparation bounds")
            connection.execute(
                "INSERT INTO comparison_source_files VALUES (?, ?)",
                (source.logical_path, payload),
            )
            candidate_count += 1
    return candidate_count


def _write_comparison(
    connection: sqlite3.Connection, service: ReportViewService
) -> BrowseComparisonSummary | None:
    from code_analysis.static_comparison_source import (
        StaticComparisonSourceBinding,
        write_comparison_source_bindings,
        write_comparison_test_sources,
    )

    connection.execute(COMPARISON_TABLE_SQL)
    delta = service.delta
    if delta is None:
        return None
    bindings: tuple[StaticComparisonSourceBinding, ...] = ()
    if service.comparison is not None and delta.records:
        bindings = service._comparison_source_bindings
        write_comparison_source_bindings(connection, bindings)
        write_comparison_test_sources(connection, service.primary, service.comparison)
    total = 0
    for position, record in enumerate(delta.records):
        payload = record.model_dump_json()
        size = len(payload.encode())
        total += size
        if position >= 100_000 or size > 1024**2 or total > 2 * 1024**3:
            raise BrowseIndexLimitError("comparison exceeds index preparation bounds")
        connection.execute("INSERT INTO comparison_rows VALUES (?, ?)", (position, payload))
    hashes = {item.role: item.report_sha256 for item in delta.inputs}
    return BrowseComparisonSummary(
        baseline_sha256=hashes[ProjectionInputRole.BASELINE],
        candidate_sha256=hashes[ProjectionInputRole.CANDIDATE],
        identical=delta.identical,
        includes_unchanged=delta.includes_unchanged,
        counts=delta.change_counts,
        eligible=delta.eligible_record_count,
        retained=len(delta.records),
        omitted=delta.eligible_record_count - len(delta.records),
        completeness=delta.completeness,
        source_binding_count=len(bindings),
    )


def build_browse_index(
    service: ReportViewService, destination: Path, *, runtime: RuntimeViewService | None = None
) -> BrowseIndexManifest:
    """Publish a complete new directory, never overwrite a cache or analyzed source.

    Preparation uses the existing validated full report. Browsing the resulting
    cache is bounded and source-free; this function is not a streaming report loader.
    """

    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(errno.EEXIST, "browse index destination already exists")
    index_directory(destination.parent)
    overview = service.bootstrap()
    identity = builder_identity()
    temporary = Path(tempfile.mkdtemp(prefix=".browse-index-", dir=destination.parent))
    try:
        (temporary / ".gitignore").write_text("*\n", encoding="utf-8")
        database = temporary / "index.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA cache_size = -2048")
            counts = _write_tables(connection, service)
            _write_packets(connection, service)
            _write_evidence(connection, service)
            _write_navigation(connection, service)
            candidate_source_file_count = _write_sources(connection, service)
            catalog = CandidateSignatureCatalog.from_report(service.primary)
            signatures = {
                item.candidate_id: catalog.signature(item.candidate_id)
                for item in service.primary.refactor_candidates
            }
            static_projections, subject_finding_facts = write_static_projections(
                connection, service.primary, signatures
            )
            static_graphs = write_static_graphs(connection, service.primary)
            graph_navigation = write_graph_navigation(connection, service.primary)
            comparison = _write_comparison(connection, service)
            candidate_static_identity = (
                overview.reports[1].report_sha256 if len(overview.reports) > 1 else None
            )
            runtime_summary = write_runtime_records(
                connection,
                runtime,
                overview.reports[0].report_sha256,
                candidate_static_identity,
            )
        if database.stat().st_size > 2 * 1024**3:
            raise BrowseIndexLimitError("browse database exceeds its two-GiB preparation bound")
        digest, stamp = artifact_digest(database, 2 * 1024**3)
        manifest = BrowseIndexManifest(
            report_sha256=overview.reports[0].report_sha256,
            target=service.primary.target,
            builder_identity_sha256=identity,
            database_sha256=digest,
            database_bytes=stamp.size,
            source_file_count=len(service.primary.source_files),
            candidate_source_file_count=candidate_source_file_count,
            coverage_files_count=len(service.primary.coverage_files),
            coverage_cells_count=sum(
                max(1, len(item.contexts)) for item in service.primary.coverage_observations
            ),
            proposal_coverage_subject_count=sum(
                len(item.affected_subject_ids) for item in service.primary.refactor_proposals
            ),
            runtime=runtime_summary,
            test_coverage=next(
                item for item in service.primary.section_coverage if item.section == "test-impact"
            ),
            contract_coverage=next(
                item for item in service.primary.section_coverage if item.section == "contracts"
            ),
            shape_coverage=next(
                item
                for item in service.primary.section_coverage
                if item.section == "dictionary-shapes"
            ),
            clone_coverage=next(
                item for item in service.primary.section_coverage if item.section == "clones"
            ),
            state_effect_coverage=next(
                item
                for item in service.primary.section_coverage
                if item.section == "state-side-effects"
            ),
            overview=overview,
            tables=counts,
            subject_finding_facts=subject_finding_facts,
            static_projections=static_projections,
            static_graphs=static_graphs,
            graph_navigation=graph_navigation,
            comparison=comparison,
        )
        rendered = manifest.model_dump_json().encode()
        if len(rendered) > 1024 * 1024:
            raise BrowseIndexLimitError("browse index receipt exceeds its one-MiB bound")
        (temporary / "manifest.json").write_bytes(rendered)
        BrowseIndex.open(temporary, report_sha256=manifest.report_sha256)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(
                errno.EEXIST, "browse index destination appeared during preparation"
            )
        os.rename(temporary, destination)
        return manifest
    finally:
        # Only this invocation's freshly allocated private sibling may be removed.
        if temporary.exists():
            shutil.rmtree(temporary)
