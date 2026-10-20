"""Bounded read-only table access without loading authoritative report JSON.

Indexes are disposable derivatives. The caller supplies the expected report hash;
the receipt binds a complete immutable database, not a replacement report schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from code_analysis.browse_index_models import BrowseIndexManifest, BrowseRequest
from code_analysis.clone_browsing import ClonePage, CloneRow
from code_analysis.comparison_browse import ComparisonFilters
from code_analysis.contract_browsing import ContractPage, ShapePage
from code_analysis.module_navigation import ModuleNavigationPage, ModuleSelectionError, ModuleView
from code_analysis.projection_models import ComparisonRecord, GraphProjection
from code_analysis.query_models import QueryResponse, QuerySelectorKind
from code_analysis.report_models import SourceFileRecord
from code_analysis.runtime_browse_models import RuntimeBrowsePage, RuntimeBrowseRequest
from code_analysis.runtime_browse_storage import read_runtime_page, validate_runtime_records
from code_analysis.runtime_comparison_browse import (
    RuntimeComparisonPage,
    RuntimeComparisonRequest,
    read_comparison_page,
)
from code_analysis.runtime_graph_browse import RuntimeGraphRequest, read_runtime_graph
from code_analysis.runtime_models import SourceBindingState
from code_analysis.runtime_source_browse import (
    CANDIDATE_HOTSPOTS_SECTION,
    CANDIDATE_SOURCE_HASHES_SECTION,
    RuntimeSourcePreview,
    RuntimeSourceUnavailable,
    read_runtime_comparison_frame_binding,
    read_runtime_source,
    require_comparison_source_side,
)
from code_analysis.static_browse import (
    StaticProjectionPage,
    StaticProjectionRequest,
    read_static_projection,
    validate_static_projections,
)
from code_analysis.static_comparison_source import (
    StaticComparisonSourceBinding,
    read_comparison_source_binding,
    validate_comparison_source_bindings,
)
from code_analysis.static_graph_browse import (
    StaticGraphRequest,
    StaticGraphSelectionError,
    read_static_graph,
    validate_static_graphs,
)
from code_analysis.static_graph_navigation import read_graph_navigation, validate_graph_navigation
from code_analysis.subject_finding_browse import validate_subject_finding_facts
from code_analysis.test_browsing import TestImpactPage
from code_analysis.ui_models import (
    UIBootstrap,
    UICandidatePacket,
    UICandidatePage,
    UICandidateRow,
    UIComparisonPage,
    UIComparisonRow,
    UIDiagnosticPage,
    UIFindingPage,
    UIModulePage,
    UIProviderPage,
    UIStateEffectPage,
)
from code_analysis.ui_navigation import NavigationPage
from code_analysis.ui_rows import BrowseTable, participant_matches

if TYPE_CHECKING:
    from code_analysis.ui_decisions import DecisionSnapshot

type BrowsePage = (
    UIModulePage
    | UIFindingPage
    | UICandidatePage
    | UIProviderPage
    | UIDiagnosticPage
    | UIStateEffectPage
    | ClonePage
    | ContractPage
    | ShapePage
    | TestImpactPage
)

PAGE_MODELS: dict[BrowseTable, type[BrowsePage]] = {
    BrowseTable.MODULES: UIModulePage,
    BrowseTable.FINDINGS: UIFindingPage,
    BrowseTable.CANDIDATES: UICandidatePage,
    BrowseTable.PROVIDERS: UIProviderPage,
    BrowseTable.DIAGNOSTICS: UIDiagnosticPage,
    BrowseTable.STATE_EFFECTS: UIStateEffectPage,
    BrowseTable.CLONES: ClonePage,
    BrowseTable.CONTRACTS: ContractPage,
    BrowseTable.SHAPES: ShapePage,
    BrowseTable.TEST_IMPACT: TestImpactPage,
}
_MAX_DATABASE_BYTES = 2 * 1024**3
_MAX_ROW_BYTES = 1024 * 1024
INDEX_TABLE_SQL = (
    "CREATE TABLE browse_rows (table_name TEXT NOT NULL, position INTEGER NOT NULL, "
    "payload TEXT NOT NULL, term0 TEXT NOT NULL, term1 TEXT NOT NULL, "
    "term2 TEXT NOT NULL, term3 TEXT NOT NULL, severity TEXT NOT NULL, "
    "category TEXT NOT NULL, confidence TEXT NOT NULL, "
    "language TEXT NOT NULL, module TEXT NOT NULL, owner TEXT NOT NULL, "
    "PRIMARY KEY (table_name, position)) WITHOUT ROWID"
)
FINDING_FILTER_INDEX_SQL = (
    "CREATE INDEX finding_filters ON browse_rows (table_name, severity, category, confidence)"
)
SCOPE_TABLE_SQL = (
    "CREATE TABLE browse_scopes (table_name TEXT NOT NULL, position INTEGER NOT NULL, "
    "language TEXT NOT NULL, module TEXT NOT NULL, "
    "PRIMARY KEY (table_name, position, language, module)) WITHOUT ROWID"
)
PACKET_TABLE_SQL = (
    "CREATE TABLE candidate_packets (identity TEXT NOT NULL, offset INTEGER NOT NULL, "
    "payload TEXT NOT NULL, PRIMARY KEY (identity, offset)) WITHOUT ROWID"
)
EVIDENCE_TABLE_SQL = (
    "CREATE TABLE finding_pages (identity TEXT NOT NULL, cursor TEXT NOT NULL, "
    "payload TEXT NOT NULL, PRIMARY KEY (identity, cursor)) WITHOUT ROWID"
)
NAVIGATION_TABLE_SQL = (
    "CREATE TABLE module_pages (module TEXT NOT NULL, view TEXT NOT NULL, "
    "offset INTEGER NOT NULL, payload TEXT NOT NULL, "
    "PRIMARY KEY (module, view, offset)) WITHOUT ROWID"
)
SOURCE_TABLE_SQL = (
    "CREATE TABLE source_files (path TEXT PRIMARY KEY NOT NULL, "
    "payload TEXT NOT NULL) WITHOUT ROWID"
)
COMPARISON_SOURCE_TABLE_SQL = (
    "CREATE TABLE comparison_source_files (path TEXT PRIMARY KEY NOT NULL, "
    "payload TEXT NOT NULL) WITHOUT ROWID"
)
COMPARISON_TABLE_SQL = (
    "CREATE TABLE comparison_rows (position INTEGER PRIMARY KEY NOT NULL, "
    "payload TEXT NOT NULL) WITHOUT ROWID"
)


def builder_identity() -> str:
    """Invalidate derivatives when the installed Python view implementation changes."""

    digest = hashlib.sha256(f"{sys.implementation.cache_tag}:{sqlite3.sqlite_version}".encode())
    package = Path(__file__).parent
    for path in sorted(package.rglob("*.py")):
        if path.is_symlink():
            raise ValueError("browse index builder source must not be symlinked")
        digest.update(path.relative_to(package).as_posix().encode() + b"\0")
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class FileStamp:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


def file_stamp(path: Path) -> FileStamp:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("browse index artifact must be a regular non-symlink file")
    return FileStamp(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def index_directory(path: Path) -> Path:
    absolute = path.absolute()
    if not absolute.is_dir() or any(item.is_symlink() for item in (absolute, *absolute.parents)):
        raise ValueError("browse index requires an existing non-symlink directory chain")
    return absolute


def artifact_digest(path: Path, maximum: int) -> tuple[str, FileStamp]:
    before = file_stamp(path)
    if before.size > maximum:
        raise ValueError("browse index artifact exceeds its byte bound")
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (
            before.device,
            before.inode,
        ):
            raise ValueError("browse index artifact changed while opening")
        total = 0
        while block := stream.read(1024 * 1024):
            total += len(block)
            if total > maximum:
                raise ValueError("browse index artifact exceeds its byte bound")
            digest.update(block)
    if total != before.size or file_stamp(path) != before:
        raise ValueError("browse index artifact changed while hashing")
    return digest.hexdigest(), before


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    connection.execute("PRAGMA cache_size = -2048")
    connection.execute("PRAGMA temp_store = FILE")
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_ROW_BYTES * 2)
    return connection


def _validate_scopes(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'browse_scopes'"
    ).fetchone() != (SCOPE_TABLE_SQL,):
        raise ValueError("browse participant scope schema mismatch")
    if connection.execute("SELECT count(*) FROM browse_scopes").fetchone()[0] > 1_000_000:
        raise ValueError("browse participant scopes exceed preparation bound")
    expected = (
        "SELECT table_name, position, json_extract(value, '$.language') AS language, "
        "json_extract(value, '$.module') AS module FROM browse_rows, "
        "json_each(payload, '$.module_scopes') WHERE table_name = 'candidates' UNION "
        "SELECT table_name, position, json_extract(payload, '$.language'), value "
        "FROM browse_rows, json_each(payload, '$.modules') WHERE table_name = 'clones'"
    )
    actual = "SELECT table_name, position, language, module FROM browse_scopes"
    for left, right in (("expected", "actual"), ("actual", "expected")):
        if (
            connection.execute(
                f"WITH expected AS ({expected}), actual AS ({actual}) "
                f"SELECT * FROM {left} EXCEPT SELECT * FROM {right} LIMIT 1"
            ).fetchone()
            is not None
        ):
            raise ValueError("browse participant scopes disagree with saved rows")


def _expected_schema_objects(manifest: BrowseIndexManifest) -> list[tuple[str, str]]:
    objects = [
        ("table", "browse_rows"),
        ("table", "browse_scopes"),
        ("table", "candidate_packets"),
        ("table", "candidate_policy_rows"),
        ("table", "comparison_rows"),
    ]
    if manifest.comparison is not None:
        objects.append(("table", "comparison_source_files"))
        if manifest.comparison.retained:
            objects.append(("table", "comparison_source_bindings"))
            objects.append(("table", "comparison_test_sources"))
    objects.extend(
        [
            ("table", "finding_facts"),
            ("index", "finding_filters"),
            ("table", "finding_pages"),
            ("table", "module_pages"),
            ("table", "runtime_records"),
            ("table", "source_files"),
            ("table", "static_graph_clusters"),
            ("table", "static_graph_edges"),
            ("table", "static_graph_navigation_items"),
            ("table", "static_graph_navigation_subjects"),
            ("table", "static_graph_nodes"),
            ("table", "static_projection_rows"),
            ("table", "subject_facts"),
        ]
    )
    return sorted(objects, key=lambda item: item[1])


def _validate_comparison_source_files(
    connection: sqlite3.Connection, manifest: BrowseIndexManifest
) -> None:
    comparison = manifest.comparison
    if comparison is None:
        if manifest.candidate_source_file_count:
            raise ValueError("candidate source inventory requires comparison metadata")
        if connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'comparison_source_files'"
        ).fetchone():
            raise ValueError("non-comparison index cannot retain candidate source inventory")
        return
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'comparison_source_files'"
    ).fetchone() != (COMPARISON_SOURCE_TABLE_SQL,):
        raise ValueError("browse comparison source identity schema is invalid")
    if connection.execute("SELECT count(*) FROM comparison_source_files").fetchone() != (
        manifest.candidate_source_file_count,
    ):
        raise ValueError("candidate source identity count disagrees with receipt")
    seen_paths: list[str] = []
    seen_source_ids: set[str] = set()
    total_bytes = 0
    for row_count, (path, payload) in enumerate(
        connection.execute("SELECT path, payload FROM comparison_source_files ORDER BY path"),
        start=1,
    ):
        if row_count > 1_000_000:
            raise ValueError("candidate source inventory exceeds preparation bound")
        if not isinstance(path, str) or not isinstance(payload, str):
            raise ValueError("candidate source identity row is malformed")
        payload_bytes = len(payload.encode())
        total_bytes += payload_bytes
        if payload_bytes > _MAX_ROW_BYTES or total_bytes > 2 * 1024**3:
            raise ValueError("candidate source identity exceeds byte bound")
        source = SourceFileRecord.model_validate_json(payload)
        if source.logical_path != path:
            raise ValueError("candidate source identity path mismatch")
        if source.logical_root != manifest.target.logical_root:
            raise ValueError("candidate source identity belongs to another target root")
        if source.source_file_id in seen_source_ids:
            raise ValueError("candidate source inventory contains duplicate source identity")
        seen_source_ids.add(source.source_file_id)
        if seen_paths and path <= seen_paths[-1]:
            raise ValueError("candidate source inventory must be sorted by path")
        seen_paths.append(path)


def _validate_database(connection: sqlite3.Connection, manifest: BrowseIndexManifest) -> None:
    objects = connection.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    if objects != _expected_schema_objects(manifest):
        raise ValueError("browse index contains an unexpected database schema")
    validate_runtime_records(connection, manifest.runtime)
    validate_static_projections(connection, manifest.static_projections)
    validate_subject_finding_facts(connection, manifest.subject_finding_facts)
    validate_static_graphs(connection, manifest.static_graphs)
    validate_graph_navigation(connection, manifest.graph_navigation)
    _validate_scopes(connection)
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'finding_filters'"
    ).fetchone() != (FINDING_FILTER_INDEX_SQL,):
        raise ValueError("browse finding filter index is invalid")
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'comparison_rows'"
    ).fetchone() != (COMPARISON_TABLE_SQL,):
        raise ValueError("browse comparison schema is invalid")
    count = manifest.comparison.retained if manifest.comparison is not None else 0
    expected_comparison = (count, 0 if count else None, count - 1 if count else None)
    if (
        connection.execute(
            "SELECT count(*), min(position), max(position) FROM comparison_rows"
        ).fetchone()
        != expected_comparison
    ):
        raise ValueError("browse comparison records disagree with receipt")
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'source_files'"
    ).fetchone() != (SOURCE_TABLE_SQL,):
        raise ValueError("browse source identity schema is invalid")
    if connection.execute("SELECT count(*) FROM source_files").fetchone() != (
        manifest.source_file_count,
    ):
        raise ValueError("browse source identity count disagrees with receipt")
    _validate_comparison_source_files(connection, manifest)
    if manifest.comparison is not None:
        validate_comparison_source_bindings(
            connection,
            retained=manifest.comparison.retained,
            baseline_sha256=manifest.comparison.baseline_sha256,
            candidate_sha256=manifest.comparison.candidate_sha256,
        )
        if manifest.comparison.source_binding_count != manifest.comparison.retained:
            raise ValueError("comparison source binding receipt disagrees with retained rows")
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'module_pages'"
    ).fetchone() != (NAVIGATION_TABLE_SQL,):
        raise ValueError("browse module navigation schema is invalid")
    module_count = next(
        item.records for item in manifest.tables if item.table is BrowseTable.MODULES
    )
    if connection.execute("SELECT count(*) FROM module_pages WHERE offset = 0").fetchone() != (
        module_count * 3,
    ):
        raise ValueError("browse module navigation count disagrees with receipt")
    if connection.execute("SELECT count(*) FROM module_pages").fetchone()[0] > 1_000_000:
        raise ValueError("browse module navigation exceeds page bound")
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'finding_pages'"
    ).fetchone() != (EVIDENCE_TABLE_SQL,):
        raise ValueError("browse finding page schema is invalid")
    finding_count = next(
        item.records for item in manifest.tables if item.table is BrowseTable.FINDINGS
    )
    if connection.execute("SELECT count(*) FROM finding_pages WHERE cursor = ''").fetchone() != (
        finding_count,
    ):
        raise ValueError("browse finding page count disagrees with receipt")
    if connection.execute("SELECT count(*) FROM finding_pages").fetchone()[0] > 1_000_000:
        raise ValueError("browse finding page count exceeds its bound")
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'candidate_packets'"
    ).fetchone() != (PACKET_TABLE_SQL,):
        raise ValueError("browse candidate packet schema is invalid")
    candidate_count = next(
        item.records for item in manifest.tables if item.table is BrowseTable.CANDIDATES
    )
    if connection.execute("SELECT count(*) FROM candidate_packets WHERE offset = 0").fetchone() != (
        candidate_count,
    ):
        raise ValueError("browse candidate packet count disagrees with receipt")
    if connection.execute("SELECT count(*) FROM candidate_packets").fetchone()[0] > 1_000_000:
        raise ValueError("browse candidate packet page count exceeds its bound")
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'browse_rows'"
    ).fetchone() != (INDEX_TABLE_SQL,):
        raise ValueError("browse index table definition does not match the declared schema")
    columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(browse_rows)"))
    if columns != (
        "table_name",
        "position",
        "payload",
        "term0",
        "term1",
        "term2",
        "term3",
        "severity",
        "category",
        "confidence",
        "language",
        "module",
        "owner",
    ):
        raise ValueError("browse index columns do not match the declared schema")
    if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise ValueError("browse index database integrity check failed")
    found = {
        row[0]: (row[1], row[2], row[3])
        for row in connection.execute(
            "SELECT table_name, count(*), min(position), max(position) "
            "FROM browse_rows GROUP BY table_name"
        )
    }
    expected = {
        item.table.value: (item.records, 0, item.records - 1)
        for item in manifest.tables
        if item.records
    }
    if found != expected:
        raise ValueError("browse index row counts/order disagree with its receipt")


@dataclass(frozen=True, slots=True)
class BrowseIndex:
    database: Path
    manifest: BrowseIndexManifest
    stamp: FileStamp

    @classmethod
    def open(cls, directory: Path, *, report_sha256: str) -> BrowseIndex:
        directory = index_directory(directory)
        receipt = directory / "manifest.json"
        receipt_digest, receipt_stamp = artifact_digest(receipt, 1024 * 1024)
        descriptor = os.open(receipt, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if hashlib.sha256(raw).hexdigest() != receipt_digest:
            raise ValueError("browse index receipt identity changed while reading")
        manifest = BrowseIndexManifest.model_validate_json(raw)
        if file_stamp(receipt) != receipt_stamp:
            raise ValueError("browse index receipt changed while loading")
        if manifest.report_sha256 != report_sha256:
            raise ValueError("browse index belongs to a different report")
        if manifest.builder_identity_sha256 != builder_identity():
            raise ValueError(
                "browse index builder identity changed; rebuild from authoritative JSON"
            )
        database = directory / "index.sqlite3"
        digest, stamp_ = artifact_digest(database, _MAX_DATABASE_BYTES)
        if digest != manifest.database_sha256 or stamp_.size != manifest.database_bytes:
            raise ValueError("browse index database identity mismatch")
        with closing(_connect(database)) as connection:
            _validate_database(connection, manifest)
        if file_stamp(database) != stamp_:
            raise ValueError("browse index database changed while opening")
        return cls(database, manifest, stamp_)

    def bootstrap(self) -> UIBootstrap:
        return self.manifest.overview

    def runtime_source(self, frame_id: str) -> RuntimeSourcePreview:
        summary = self.manifest.runtime
        if (
            summary is None
            or not summary.overview.source_matches
            or summary.source_binding is not SourceBindingState.MATCHED
        ):
            raise RuntimeSourceUnavailable(
                "runtime source preview unavailable: source binding is not matched"
            )
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        with closing(_connect(self.database)) as connection:
            preview = read_runtime_source(connection, frame_id)
        source = self.source_file(
            preview.source.logical_path, static_report_sha256=summary.static_report_sha256
        )
        if source.source_sha256 != preview.source.source_sha256:
            raise ValueError("runtime captured hash does not match static source identity")
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index changed while reading runtime source binding")
        return preview

    def runtime_comparison_source(
        self, frame_identity: str, side: Literal["baseline", "candidate"]
    ) -> RuntimeSourcePreview:
        summary = self.manifest.runtime
        if summary is None or summary.comparison is None:
            raise RuntimeSourceUnavailable(
                "runtime comparison source preview unavailable: no comparison was indexed"
            )
        comparison = summary.comparison
        side = require_comparison_source_side(side)
        if side == "baseline":
            if (
                not summary.overview.source_matches
                or summary.source_binding is not SourceBindingState.MATCHED
            ):
                raise RuntimeSourceUnavailable(
                    "runtime comparison source preview unavailable: baseline source binding "
                    "is not matched"
                )
            hotspots_section = "hotspots"
            hashes_section = "source-hashes"
        elif side == "candidate":
            if (
                not comparison.candidate_overview.source_matches
                or comparison.candidate_source_binding is not SourceBindingState.MATCHED
            ):
                raise RuntimeSourceUnavailable(
                    "runtime comparison source preview unavailable: candidate source binding "
                    "is not matched"
                )
            hotspots_section = CANDIDATE_HOTSPOTS_SECTION
            hashes_section = CANDIDATE_SOURCE_HASHES_SECTION
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        with closing(_connect(self.database)) as connection:
            binding = read_runtime_comparison_frame_binding(connection, frame_identity)
            frame_id = (
                binding.baseline_frame_id if side == "baseline" else binding.candidate_frame_id
            )
            if frame_id is None:
                raise RuntimeSourceUnavailable(
                    f"runtime comparison has no {side} frame at the selected identity"
                )
            preview = read_runtime_source(
                connection,
                frame_id,
                hotspots_section=hotspots_section,
                hashes_section=hashes_section,
            )
        static_sha = (
            summary.static_report_sha256
            if side == "baseline"
            else comparison.candidate_static_sha256
        )
        if static_sha is None:
            raise RuntimeSourceUnavailable(
                "runtime comparison source preview unavailable: candidate static report "
                "identity is absent"
            )
        source = self.source_file(preview.source.logical_path, static_report_sha256=static_sha)
        if source.source_sha256 != preview.source.source_sha256:
            raise ValueError("runtime captured hash does not match static source identity")
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index changed while reading runtime comparison source")
        return preview

    def runtime_graph(self, request: RuntimeGraphRequest) -> GraphProjection:
        summary = self.manifest.runtime
        if summary is None:
            raise ValueError("runtime graph unavailable; no saved profile was indexed")
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        with closing(_connect(self.database)) as connection:
            graph = read_runtime_graph(connection, summary, request)
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index changed while reading runtime graph")
        return graph

    def static_graph(self, request: StaticGraphRequest) -> GraphProjection:
        """Select bounded prepared structure evidence without reading the report."""
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        with closing(_connect(self.database)) as connection:
            graph = read_static_graph(connection, self.manifest.static_graphs, request)
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index changed while reading static graph")
        return graph

    def graph_navigation(
        self, request: StaticGraphRequest, identity: str, offset: int = 0
    ) -> NavigationPage:
        """Resolve an exact retained graph node to saved spans, never target source."""
        if type(offset) is not int or offset < 0:
            raise StaticGraphSelectionError("navigation offset must be a nonnegative integer")
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        with closing(_connect(self.database)) as connection:
            graph = read_static_graph(connection, self.manifest.static_graphs, request)
            if identity not in {node.node_id for node in graph.nodes}:
                raise StaticGraphSelectionError("selection is not a retained graph node")
            page = read_graph_navigation(
                connection,
                self.manifest.graph_navigation,
                identity,
                offset,
                self.manifest.overview.reports[0].completeness,
            )
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index changed while reading graph navigation")
        return page

    def runtime_comparison(self, request: RuntimeComparisonRequest) -> RuntimeComparisonPage:
        summary = self.manifest.runtime
        if summary is None or summary.comparison is None:
            raise ValueError("runtime comparison unavailable; prepare with --compare-profile")
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        with closing(_connect(self.database)) as connection:
            page = read_comparison_page(connection, summary.comparison, request)
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index changed while reading runtime comparison")
        return page

    def runtime_page(self, request: RuntimeBrowseRequest) -> RuntimeBrowsePage:
        summary = self.manifest.runtime
        if summary is None:
            raise ValueError("runtime unavailable; prepare an index with --profile-report")
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        with closing(_connect(self.database)) as connection:
            page = read_runtime_page(connection, summary, request)
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index changed while reading runtime evidence")
        return page

    def comparison_records(
        self, *, offset: int = 0, limit: int = 25, filters: ComparisonFilters | None = None
    ) -> UIComparisonPage:
        summary = self.manifest.comparison
        if summary is None:
            raise ValueError("comparison unavailable; prepare an index with --compare")
        if (
            type(offset) is not int
            or type(limit) is not int
            or not 0 <= offset <= summary.retained
            or not 1 <= limit <= 500
        ):
            raise ValueError("invalid comparison page selection")
        selection = ComparisonFilters() if filters is None else filters
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        records: list[UIComparisonRow] = []
        eligible = 0
        if summary.retained:
            predicate, parameters = selection.sql()
            joined = (
                " FROM comparison_rows AS record JOIN comparison_source_bindings AS binding "
                "ON record.position = binding.position WHERE " + predicate
            )
            with closing(_connect(self.database)) as connection:
                eligible = connection.execute("SELECT count(*)" + joined, parameters).fetchone()[0]
                total = 0
                for position, payload, kind in connection.execute(
                    "SELECT record.position, record.payload, "
                    "json_extract(binding.payload, '$.record_kind')"
                    + joined
                    + " ORDER BY record.position LIMIT ? OFFSET ?",
                    (*parameters, limit, offset),
                ):
                    if not isinstance(payload, str) or len(payload.encode()) > _MAX_ROW_BYTES:
                        raise ValueError("comparison record exceeds byte bound")
                    total += len(payload.encode())
                    if total > 8 * 1024**2:
                        raise ValueError("comparison page exceeds byte bound")
                    item = ComparisonRecord.model_validate_json(payload)
                    records.append(
                        UIComparisonRow(
                            position=position,
                            record_kind=kind,
                            subject_id=item.subject_id,
                            change=item.change.value,
                            explanation=item.explanation,
                        )
                    )
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index changed while reading comparison")
        return UIComparisonPage(
            offset=offset,
            limit=limit,
            eligible=eligible,
            retained=len(records),
            truncated=offset + len(records) < eligible,
            records=tuple(records),
            total_eligible=summary.eligible,
            omitted=summary.omitted,
            completeness=summary.completeness,
            filtered_out=summary.retained - eligible,
        )

    def static_comparison_source_detail(self, position: int) -> StaticComparisonSourceBinding:
        summary = self.manifest.comparison
        if summary is None or type(position) is not int or not 0 <= position < summary.retained:
            raise ValueError("static comparison source binding is unavailable")
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        with closing(_connect(self.database)) as connection:
            binding = read_comparison_source_binding(connection, position)
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index changed while reading comparison source binding")
        return binding

    def comparison_record(self, position: int) -> ComparisonRecord:
        if (
            self.manifest.comparison is None
            or type(position) is not int
            or not 0 <= position < self.manifest.comparison.retained
        ):
            raise ValueError("comparison record position is unavailable")
        return self._comparison_records(position, 1)[0]

    def _comparison_records(self, offset: int, limit: int) -> tuple[ComparisonRecord, ...]:
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        total = 0
        records: list[ComparisonRecord] = []
        with closing(_connect(self.database)) as connection:
            rows = connection.execute(
                "SELECT payload FROM comparison_rows WHERE position >= ? ORDER BY position LIMIT ?",
                (offset, limit),
            )
            for row in rows:
                if not isinstance(row[0], str) or len(row[0].encode()) > _MAX_ROW_BYTES:
                    raise ValueError("comparison record exceeds byte bound")
                total += len(row[0].encode())
                if total > 8 * 1024**2:
                    raise ValueError("comparison page exceeds byte bound")
                records.append(ComparisonRecord.model_validate_json(row[0]))
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index changed while reading comparison")
        return tuple(records)

    def source_file(
        self, path: str, *, static_report_sha256: str | None = None
    ) -> SourceFileRecord:
        if not path or len(path) > 4096:
            raise ValueError("invalid source identity")
        table = self._source_inventory_table(static_report_sha256)
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        with closing(_connect(self.database)) as connection:
            row = connection.execute(
                f"SELECT payload FROM {table} WHERE path = ?", (path,)
            ).fetchone()
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index changed while reading source identity")
        if row is None:
            raise ValueError("source path is absent from this index")
        if not isinstance(row[0], str) or len(row[0].encode()) > _MAX_ROW_BYTES:
            raise ValueError("source identity exceeds byte bound")
        source = SourceFileRecord.model_validate_json(row[0])
        if source.logical_path != path:
            raise ValueError("source identity path mismatch")
        return source

    def _source_inventory_table(self, static_report_sha256: str | None) -> str:
        primary = self.manifest.report_sha256
        if static_report_sha256 is None or static_report_sha256 == primary:
            return "source_files"
        if self.manifest.candidate_source_file_count and len(self.manifest.overview.reports) > 1:
            secondary = self.manifest.overview.reports[1].report_sha256
            if static_report_sha256 == secondary:
                return "comparison_source_files"
        raise ValueError("static report identity is not indexed")

    def module_navigation(
        self, module: str, view: ModuleView = "outgoing", offset: int = 0
    ) -> ModuleNavigationPage:
        if (
            not module
            or len(module) > 4096
            or view not in ("outgoing", "incoming", "symbols")
            or type(offset) is not int
            or offset < 0
            or offset % 25
        ):
            raise ValueError("invalid module navigation selection")
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        with closing(_connect(self.database)) as connection:
            row = connection.execute(
                "SELECT payload FROM module_pages WHERE module = ? AND view = ? AND offset = ?",
                (module, view, offset),
            ).fetchone()
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed while reading navigation")
        if row is None:
            raise ModuleSelectionError("module navigation selection is not present in this index")
        if not isinstance(row[0], str) or len(row[0].encode()) > _MAX_ROW_BYTES:
            raise ValueError("module navigation page exceeds byte bound")
        page = ModuleNavigationPage.model_validate_json(row[0])
        if (
            page.module.module != module
            or page.view != view
            or page.offset != offset
            or page.source_completeness != self.manifest.overview.reports[0].completeness
        ):
            raise ValueError("module navigation selection identity mismatch")
        return page

    def static_projection(
        self, request: StaticProjectionRequest, *, decisions: DecisionSnapshot | None = None
    ) -> StaticProjectionPage:
        if (
            request.decision_report_sha256 is not None
            and request.decision_report_sha256 != self.manifest.report_sha256
        ):
            raise ValueError("decision ledger request does not match the indexed report")
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        with closing(_connect(self.database)) as connection:
            page = read_static_projection(
                connection,
                self.manifest.static_projections,
                request,
                decisions,
                self.manifest.subject_finding_facts,
            )
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index changed while reading static projection")
        return page

    def finding_evidence(self, identity: str, after: str | None = None) -> QueryResponse:
        """Read a prepared 50-item query page without report or source access."""
        if (
            not identity
            or len(identity) > 4096
            or (after is not None and (not after or len(after) > 4096))
        ):
            raise ValueError("invalid finding identity or continuation")
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        with closing(_connect(self.database)) as connection:
            row = connection.execute(
                "SELECT payload FROM finding_pages WHERE identity = ? AND cursor = ?",
                (identity, after or ""),
            ).fetchone()
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed while reading evidence")
        if row is None:
            raise ValueError("finding identity or continuation is not present in this index")
        if not isinstance(row[0], str) or len(row[0].encode()) > _MAX_ROW_BYTES:
            raise ValueError("finding page exceeds the one-MiB bound")
        page = QueryResponse.model_validate_json(row[0])
        source = self.manifest.overview.reports[0]
        if (
            page.selector.kind is not QuerySelectorKind.FINDING
            or page.selector.identity != identity
            or page.after_identity != after
            or page.source_report_sha256 != source.report_sha256
            or page.source_run_identity_sha256 != source.run_identity_sha256
            or page.source_completeness != source.completeness
            or page.limit != 50
            or len(page.items) > 50
            or not page.items
            or (page.next_identity is not None and page.next_identity != page.items[-1].item_id)
        ):
            raise ValueError("finding page identity or continuation mismatch")
        return page

    def candidate_packet(self, identity: str, *, offset: int = 0) -> UICandidatePacket:
        """Read a prepared continuation, with the same bounded sections as the Web UI."""
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ValueError("invalid candidate packet offset")
        if not identity or len(identity) > 4096:
            raise ValueError("invalid candidate identity")
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed")
        with closing(_connect(self.database)) as connection:
            row = connection.execute(
                "SELECT payload FROM candidate_packets WHERE identity = ? AND offset = ?",
                (identity, offset),
            ).fetchone()
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed while reading packet")
        if row is None:
            raise ValueError("candidate identity is not present in this index")
        if not isinstance(row[0], str) or len(row[0].encode()) > _MAX_ROW_BYTES:
            raise ValueError("candidate packet exceeds the one-MiB bound")
        packet = UICandidatePacket.model_validate_json(row[0])
        if packet.candidate.candidate_id != identity:
            raise ValueError("candidate packet identity mismatch")
        if packet.offset != offset or packet.limit != 25:
            raise ValueError("candidate packet selection mismatch")
        if packet.source_completeness != self.manifest.overview.reports[0].completeness:
            raise ValueError("candidate packet source completeness mismatch")
        return packet

    def page(self, request: BrowseRequest) -> BrowsePage:
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed; rebuild from authoritative JSON")
        needle = request.query.casefold().strip()
        predicate = "table_name = ?"
        parameters: list[str | int] = [request.table.value]
        if needle:
            predicate += (
                " AND (instr(term0, ?) OR instr(term1, ?) OR instr(term2, ?) OR instr(term3, ?))"
            )
            parameters.extend([needle] * 4)
        if request.severity:
            predicate += " AND severity = ?"
            parameters.append(request.severity)
        multi_scope = request.table in {BrowseTable.CANDIDATES, BrowseTable.CLONES}
        scoped = multi_scope and (
            request.facets.language is not None or bool(request.facets.module_prefix)
        )
        if scoped:
            # Set membership retains one outer row per candidate/cluster. Avoid
            # EXISTS-to-join OFFSET duplication on SQLite 3.51.2 (forum 2c43f36255a630e3).
            predicate += (
                " AND position IN (SELECT position FROM browse_scopes AS scope WHERE "
                "scope.table_name = ?"
            )
            parameters.append(request.table.value)
        qualifier = "scope." if scoped else ""
        if request.facets.language is not None:
            predicate += f" AND {qualifier}language = ?"
            parameters.append(request.facets.language.value)
        if request.facets.owner:
            predicate += " AND owner = ?"
            parameters.append(request.facets.owner)
        if request.facets.module_prefix:
            prefix = request.facets.module_prefix
            predicate += (
                f" AND ({qualifier}module = ? OR substr({qualifier}module, 1, ?) = "
                f"? || CASE WHEN {qualifier}language = 'python' THEN '.' ELSE '/' END)"
            )
            parameters.extend((prefix, len(prefix) + 1, prefix))
        if scoped:
            predicate += ")"
        for column, value in (("category", request.category), ("confidence", request.confidence)):
            if value is not None:
                predicate += f" AND {column} = ?"
                parameters.append(value.value)
        with closing(_connect(self.database)) as connection:
            eligible = connection.execute(
                f"SELECT count(*) FROM browse_rows WHERE {predicate}", parameters
            ).fetchone()[0]
            if request.offset > eligible:
                raise ValueError("browse offset exceeds the eligible record count")
            cursor = connection.execute(
                f"SELECT payload FROM browse_rows WHERE {predicate} "
                "ORDER BY position LIMIT ? OFFSET ?",
                [*parameters, request.limit, request.offset],
            )
            rows: list[str] = []
            total_bytes = 0
            for (value,) in cursor:
                if not isinstance(value, str) or len(value.encode()) > _MAX_ROW_BYTES:
                    raise ValueError("browse index contains malformed or oversized row data")
                total_bytes += len(value.encode())
                if total_bytes > 8 * 1024 * 1024:
                    raise ValueError("browse page exceeds its eight-MiB byte bound")
                rows.append(value)
        if file_stamp(self.database) != self.stamp:
            raise ValueError("browse index database changed while reading")
        payload = {
            "offset": request.offset,
            "limit": request.limit,
            "eligible": eligible,
            "retained": len(rows),
            "truncated": request.offset + len(rows) < eligible,
            "records": [json.loads(row) for row in rows],
        }
        if request.table is BrowseTable.STATE_EFFECTS:
            payload["coverage"] = self.manifest.state_effect_coverage.model_dump(mode="json")
        if request.table is BrowseTable.CLONES:
            payload["coverage"] = self.manifest.clone_coverage.model_dump(mode="json")
        if request.table is BrowseTable.CONTRACTS:
            payload["coverage"] = self.manifest.contract_coverage.model_dump(mode="json")
        if request.table is BrowseTable.SHAPES:
            payload["coverage"] = self.manifest.shape_coverage.model_dump(mode="json")
        if request.table is BrowseTable.TEST_IMPACT:
            payload["coverage"] = self.manifest.test_coverage.model_dump(mode="json")
        page = PAGE_MODELS[request.table].model_validate_json(json.dumps(payload))
        if multi_scope and any(
            isinstance(row, (UICandidateRow, CloneRow))
            and not participant_matches(row, request.facets)
            for row in page.records
        ):
            raise ValueError("indexed participant scope disagrees with the requested filters")
        if isinstance(page, UIFindingPage) and any(
            not request.matches(row) for row in page.records
        ):
            raise ValueError("indexed finding disagrees with the requested filters")
        return page
