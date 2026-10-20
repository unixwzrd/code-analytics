"""Explicit no-overwrite export of a bounded indexed table selection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from code_analysis.browse_index import BrowseIndex, index_directory
from code_analysis.browse_index_models import BrowseRequest
from code_analysis.clone_browsing import CloneRow
from code_analysis.comparison_browse import ComparisonPageRequest, comparison_page_projection
from code_analysis.contract_browsing import ContractRow, ShapeRow
from code_analysis.dependency_graph import DependencyGraphRequest, build_dependency_graph
from code_analysis.models import Completeness, Diagnostic
from code_analysis.projection_models import (
    GraphProjection,
    ProjectionInput,
    ProjectionInputRole,
    ProjectionScalar,
    ScalarKind,
    TableColumn,
    TableProjection,
    TableRow,
)
from code_analysis.report_models import SectionState
from code_analysis.reports.projection_views import (
    ProjectionFormat,
    render_projection,
    render_projection_binary,
)
from code_analysis.runtime_browse_models import RuntimeBrowseRequest
from code_analysis.runtime_comparison_browse import RuntimeComparisonRequest
from code_analysis.runtime_graph_browse import RuntimeGraphRequest
from code_analysis.runtime_index import RuntimeIndex
from code_analysis.static_browse import StaticProjectionRequest, static_page_projection
from code_analysis.static_graph_browse import (
    StaticGraphReadLimitError,
    StaticGraphRequest,
    StaticGraphSelectionError,
)
from code_analysis.test_browsing import TestImpactRow
from code_analysis.ui_models import (
    UICandidateRow,
    UIFindingRow,
    UIModuleRow,
    UIProviderRow,
    UIStateEffectRow,
)
from code_analysis.ui_rows import BrowseTable

if TYPE_CHECKING:
    from code_analysis.ui_decisions import DecisionSnapshot


class BrowseExportSelectionError(ValueError):
    """Invalid user-supplied export options, not a malformed analytical artifact."""


class BrowseExportLimitError(ValueError):
    """An explicit presentation bound prevented artifact publication."""


def build_browse_export(index: BrowseIndex, request: BrowseRequest) -> TableProjection:
    page = index.page(request)
    source = index.bootstrap().reports[0]
    models: dict[BrowseTable, type[BaseModel]] = {
        BrowseTable.MODULES: UIModuleRow,
        BrowseTable.FINDINGS: UIFindingRow,
        BrowseTable.CANDIDATES: UICandidateRow,
        BrowseTable.PROVIDERS: UIProviderRow,
        BrowseTable.DIAGNOSTICS: Diagnostic,
        BrowseTable.STATE_EFFECTS: UIStateEffectRow,
        BrowseTable.CLONES: CloneRow,
        BrowseTable.CONTRACTS: ContractRow,
        BrowseTable.SHAPES: ShapeRow,
        BrowseTable.TEST_IMPACT: TestImpactRow,
    }
    fields = tuple(models[request.table].model_fields)
    selection = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    identity = hashlib.sha256((source.report_sha256 + "\0" + selection).encode()).hexdigest()[:20]
    omitted = page.eligible - page.retained
    limitations: tuple[str, ...] = (
        "Display cells are strings; nested values retain JSON notation. "
        "JSON source evidence remains authoritative.",
        f"Exact indexed selection: {selection}",
        f"Filtered records: {page.eligible}; exported: {page.retained}; "
        f"outside this page: {omitted}.",
        "Only the selected page is exported; this is not the complete analysis report.",
        "TSV is plain tabular data, not a spreadsheet-safe document; import all cells as text.",
    )
    section_incomplete = False
    if request.table is BrowseTable.STATE_EFFECTS:
        coverage = index.manifest.state_effect_coverage
        section_incomplete = coverage.state is not SectionState.COMPLETE
        limitations += (
            f"State/side-effect section: {coverage.state.value}; unresolved {coverage.unresolved}; "
            f"truncated {coverage.truncated}; failed {coverage.failed}.",
            "Syntax evidence does not establish runtime ownership, effect success, "
            "or cleanup effectiveness.",
        )
    if request.table is BrowseTable.CLONES:
        coverage = index.manifest.clone_coverage
        section_incomplete = coverage.state is not SectionState.COMPLETE
        limitations += (
            f"Clone section: {coverage.state.value}; unresolved {coverage.unresolved}; "
            f"truncated {coverage.truncated}; failed {coverage.failed}.",
            "Matched nonblank lines sum fragment observations, "
            "not net deletion or distinct project LOC.",
            "Clone similarity is syntax evidence, "
            "not semantic equivalence or refactor authorization.",
        )
    if request.table in (BrowseTable.CONTRACTS, BrowseTable.SHAPES):
        coverage = (
            index.manifest.contract_coverage
            if request.table is BrowseTable.CONTRACTS
            else index.manifest.shape_coverage
        )
        section_incomplete = coverage.state is not SectionState.COMPLETE
        limitations += (
            f"{coverage.section} section: {coverage.state.value}; "
            f"unresolved {coverage.unresolved}; "
            f"truncated {coverage.truncated}; failed {coverage.failed}.",
            "Variants and conditional requiredness remain separate; "
            "producer defaults do not establish consumer fallback behavior.",
        )
    if request.table is BrowseTable.TEST_IMPACT:
        coverage = index.manifest.test_coverage
        section_incomplete = coverage.state is not SectionState.COMPLETE
        limitations += (
            f"Test-impact section: {coverage.state.value}; unresolved {coverage.unresolved}; "
            f"truncated {coverage.truncated}; failed {coverage.failed}.",
            "Static test references indicate likely impact, not execution or behavioral coverage.",
            "Module import protection is distinct from direct symbol evidence; "
            "generic JavaScript test modules do not imply framework test cases.",
        )
    rows: list[TableRow] = []
    for number, record in enumerate(page.records, start=request.offset):
        payload = record.model_dump(mode="json")
        cells = tuple(
            ProjectionScalar(
                kind=ScalarKind.STRING,
                string_value=(
                    value
                    if isinstance(value, str)
                    else json.dumps(
                        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                ),
            )
            for value in (payload[field] for field in fields)
        )
        rows.append(TableRow(row_id=f"{request.table.value}:{number:012d}", cells=cells))
    return TableProjection(
        projection_id=f"projection-{identity}",
        title=f"Indexed {request.table.value} — selected page at offset {request.offset}",
        inputs=(
            ProjectionInput(
                role=ProjectionInputRole.SOURCE,
                report_schema_name="code-analysis-report",
                report_schema_version="1.0.0",
                report_sha256=source.report_sha256,
                run_identity_sha256=source.run_identity_sha256,
            ),
        ),
        completeness=(
            Completeness.PARTIAL
            if (omitted or section_incomplete) and source.completeness is Completeness.COMPLETE
            else source.completeness
        ),
        record_count=len(rows),
        truncated=bool(omitted),
        truncation_reason=(
            f"{omitted} filtered records are outside the selected page" if omitted else None
        ),
        limitations=tuple(sorted(limitations)),
        columns=tuple(
            TableColumn(
                column_id=field.replace("_", "-"),
                label=field.replace("_", " ").title(),
                position=number,
                value_kind=ScalarKind.STRING,
            )
            for number, field in enumerate(fields)
        ),
        rows=tuple(rows),
    )


def _static_projection_input(index: BrowseIndex) -> ProjectionInput:
    """Bind a derived static graph to the retained report, never to source files."""
    source = index.bootstrap().reports[0]
    return ProjectionInput(
        role=ProjectionInputRole.SOURCE,
        report_schema_name="code-analysis-report",
        report_schema_version="1.0.0",
        report_sha256=source.report_sha256,
    )


def export_browse_page(
    index: BrowseIndex | RuntimeIndex,
    request: (
        BrowseRequest
        | RuntimeBrowseRequest
        | RuntimeComparisonRequest
        | RuntimeGraphRequest
        | DependencyGraphRequest
        | StaticProjectionRequest
        | StaticGraphRequest
        | ComparisonPageRequest
    ),
    name: str,
    output_format: ProjectionFormat,
    *,
    decisions: DecisionSnapshot | None = None,
) -> tuple[Path, TableProjection | GraphProjection]:
    """Write one new artifact only inside the validated index's self-ignored exports."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
        raise BrowseExportSelectionError(
            "export name must be 1-80 letters, digits, hyphens, or underscores"
        )
    extensions = {
        ProjectionFormat.JSON_COMPACT: "json",
        ProjectionFormat.TSV: "tsv",
        ProjectionFormat.MARKDOWN: "md",
    }
    if isinstance(request, (RuntimeGraphRequest, DependencyGraphRequest, StaticGraphRequest)):
        extensions = {
            ProjectionFormat.JSON_COMPACT: "json",
            ProjectionFormat.MARKDOWN: "md",
            ProjectionFormat.DOT: "dot",
            ProjectionFormat.MERMAID: "mmd",
            ProjectionFormat.SVG: "svg",
            ProjectionFormat.PNG: "png",
            ProjectionFormat.HTML: "html",
        }
    if output_format not in extensions:
        raise BrowseExportSelectionError("unsupported format for the indexed selection")
    projection: TableProjection | GraphProjection
    if isinstance(request, RuntimeGraphRequest):
        projection = index.runtime_graph(request)
    elif isinstance(request, DependencyGraphRequest):
        if not isinstance(index, BrowseIndex):
            raise BrowseExportSelectionError(
                "static dependency graph requires a static report index"
            )
        projection = build_dependency_graph(index, request, _static_projection_input(index))
    elif isinstance(request, StaticProjectionRequest):
        if not isinstance(index, BrowseIndex):
            raise BrowseExportSelectionError("static projection requires a static report index")
        projection = static_page_projection(index.static_projection(request, decisions=decisions))
    elif isinstance(request, StaticGraphRequest):
        if not isinstance(index, BrowseIndex):
            raise BrowseExportSelectionError("static graph requires a static report index")
        try:
            projection = index.static_graph(request)
        except StaticGraphSelectionError as exc:
            raise BrowseExportSelectionError(str(exc)) from exc
        except StaticGraphReadLimitError as exc:
            raise BrowseExportLimitError(str(exc)) from exc
    elif isinstance(request, ComparisonPageRequest):
        if not isinstance(index, BrowseIndex):
            raise BrowseExportSelectionError("static comparison requires a static report index")
        projection = comparison_page_projection(
            index.comparison_records(
                offset=request.offset, limit=request.limit, filters=request.filters
            ),
            request,
            index.bootstrap().reports,
        )
    elif isinstance(request, BrowseRequest):
        if not isinstance(index, BrowseIndex):
            raise BrowseExportSelectionError("static export requires a static report index")
        projection = build_browse_export(index, request)
    else:
        from code_analysis.runtime_browse_export import build_runtime_browse_export

        projection = build_runtime_browse_export(index, request)
    payload = render_browse_artifact(projection, output_format)
    if len(payload) > 16 * 1024**2:
        raise BrowseExportLimitError("rendered page exceeds the 16-MiB export bound")
    root = index_directory(index.database.parent)
    filename = f"{name}.{extensions[output_format]}"
    with_directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with suppress(FileExistsError):
            os.mkdir("exports", mode=0o700, dir_fd=with_directory)
        directory = os.open(
            "exports", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=with_directory
        )
        try:
            temporary = f".export-{secrets.token_hex(16)}"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Link publication is atomic and fails on every existing destination.
                os.link(
                    temporary,
                    filename,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            finally:
                os.unlink(temporary, dir_fd=directory)
        finally:
            os.close(directory)
    finally:
        os.close(with_directory)
    return root / "exports" / filename, projection


def render_browse_artifact(
    projection: TableProjection | GraphProjection, output_format: ProjectionFormat
) -> bytes:
    if output_format in {ProjectionFormat.SVG, ProjectionFormat.PNG}:
        return render_projection_binary(projection, output_format)
    return render_projection(projection, output_format).encode("utf-8")
