"""Bounded static comparison source bindings from typed report entities only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, StrictInt, StrictStr, model_validator

from code_analysis.comparisons import comparison_entity_index
from code_analysis.models import (
    Evidence,
    Module,
    SourceLanguage,
    StrictModel,
    Symbol,
    evidence_identity,
)
from code_analysis.projection_models import (
    ComparisonChange,
    ComparisonProjection,
    ComparisonRecord,
    ProjectionInputRole,
)
from code_analysis.report_models import AnalysisReport, SourceFileRecord, TestModuleRecord
from code_analysis.ui_source import SourcePreview, SourcePreviewIdentity, preview_source_record

type ComparisonSourceSide = Literal["baseline", "candidate"]
type SourceAuthority = Mapping[str, tuple[str, str, SourceLanguage]]

_MAX_SOURCE_OPTIONS = 200
_BINDING_TABLE_SQL = (
    "CREATE TABLE comparison_source_bindings (position INTEGER PRIMARY KEY NOT NULL, "
    "payload TEXT NOT NULL) WITHOUT ROWID"
)
_TEST_SOURCE_TABLE_SQL = (
    "CREATE TABLE comparison_test_sources (side TEXT NOT NULL, path TEXT NOT NULL, "
    "payload TEXT NOT NULL, PRIMARY KEY (side, path)) WITHOUT ROWID"
)


class ComparisonSourceSideState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MISSING_RECORD = "missing-record"
    NO_EVIDENCE = "no-evidence"


class ComparisonSourcePreviewKind(StrEnum):
    EVIDENCE_SPAN = "evidence-span"
    FILE_INVENTORY_PREVIEW = "file-inventory-preview"


class ComparisonSourceOption(StrictModel):
    option_id: StrictStr = Field(pattern=r"^source-option-[0-9a-f]{20}$")
    logical_path: StrictStr = Field(min_length=1)
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    language: SourceLanguage
    start_line: StrictInt = Field(ge=1)
    end_line: StrictInt = Field(ge=1)
    preview_kind: ComparisonSourcePreviewKind
    label: StrictStr = Field(min_length=1)

    @model_validator(mode="after")
    def reconcile(self) -> ComparisonSourceOption:
        if self.end_line < self.start_line:
            raise ValueError("source option span requires end_line >= start_line")
        path = PurePosixPath(self.logical_path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in self.logical_path
            or path.as_posix() != self.logical_path
            or self.logical_path == "."
        ):
            raise ValueError("source option requires a normalized portable path")
        expected = _option_id(
            logical_path=self.logical_path,
            source_sha256=self.source_sha256,
            language=self.language,
            start_line=self.start_line,
            end_line=self.end_line,
            preview_kind=self.preview_kind,
        )
        if self.option_id != expected:
            raise ValueError("source option identity disagrees with its content")
        return self


class StaticComparisonSourceSide(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    state: ComparisonSourceSideState
    options: tuple[ComparisonSourceOption, ...] = ()
    omitted_option_count: StrictInt = Field(default=0, ge=0)
    message: StrictStr = Field(min_length=1)

    @model_validator(mode="after")
    def reconcile(self) -> StaticComparisonSourceSide:
        if self.state is ComparisonSourceSideState.AVAILABLE:
            if not self.options:
                raise ValueError("available comparison source side requires options")
        elif self.options or self.omitted_option_count:
            raise ValueError("non-available comparison source side cannot retain options")
        if len(self.options) > _MAX_SOURCE_OPTIONS:
            raise ValueError("comparison source options exceed the retained bound")
        keys = tuple(_option_sort_key(option) for option in self.options)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("comparison source options must be unique and sorted")
        return self


class StaticComparisonSourceBinding(StrictModel):
    position: StrictInt = Field(ge=0, le=100_000)
    subject_id: StrictStr = Field(min_length=1)
    record_kind: StrictStr = Field(min_length=1)
    change: ComparisonChange
    baseline: StaticComparisonSourceSide
    candidate: StaticComparisonSourceSide

    @model_validator(mode="after")
    def reconcile(self) -> StaticComparisonSourceBinding:
        missing = ComparisonSourceSideState.MISSING_RECORD
        if (self.baseline.state is missing) != (self.change is ComparisonChange.ADDED):
            raise ValueError("comparison baseline presence disagrees with change kind")
        if (self.candidate.state is missing) != (self.change is ComparisonChange.REMOVED):
            raise ValueError("comparison candidate presence disagrees with change kind")
        return self


StaticComparisonSourceDetail = StaticComparisonSourceBinding

_SPANLESS_KINDS = frozenset(
    {
        "import-edge",
        "strong-component",
        "cycle",
        "coverage-artifact",
        "coverage-file",
        "coverage-observation",
        "git-commit",
        "git-path-change",
        "git-symbol-change",
        "change-coupling",
        "generation-change",
        "static-call-coverage",
    }
)

_EVIDENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "contract": ("evidence",),
    "boundary": ("evidence",),
    "shape": ("evidence",),
    "variant": ("evidence",),
    "validation": ("evidence",),
    "conversion": ("evidence",),
    "serialization": ("evidence",),
    "unresolved": ("evidence",),
    "candidate": ("supporting_evidence",),
    "finding": ("evidence",),
    "subject": ("evidence",),
    "data-structure": ("evidence",),
    "clone-fragment": ("evidence",),
    "clone-cluster": ("evidence",),
    "function-profile": ("evidence",),
    "function-commonality": ("evidence",),
    "state-resource": ("declaration_evidence",),
    "side-effect": ("evidence",),
    "responsibility": ("evidence",),
    "architecture-estimate": ("evidence",),
    "architecture-contract": ("evidence",),
    "refactor-proposal": ("evidence",),
    "test-case": ("evidence",),
    "test-fixture": ("evidence",),
    "test-relationship": ("evidence",),
    "static-call-occurrence": ("evidence",),
}


def comparison_source_bindings_table_sql() -> str:
    return _BINDING_TABLE_SQL


def comparison_source_record_kinds() -> frozenset[str]:
    return _SPANLESS_KINDS.union(_EVIDENCE_FIELDS).union({"module", "symbol", "test-module"})


def _option_sort_key(option: ComparisonSourceOption) -> tuple[str, int, int, str, str]:
    return (
        option.logical_path,
        option.start_line,
        option.end_line,
        option.preview_kind.value,
        option.option_id,
    )


def _option_id(
    *,
    logical_path: str,
    source_sha256: str,
    language: SourceLanguage,
    start_line: int,
    end_line: int,
    preview_kind: ComparisonSourcePreviewKind,
) -> str:
    payload = json.dumps(
        {
            "logical_path": logical_path,
            "source_sha256": source_sha256,
            "language": language.value,
            "start_line": start_line,
            "end_line": end_line,
            "preview_kind": preview_kind.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "source-option-" + hashlib.sha256(payload.encode()).hexdigest()[:20]


def _source_authorities(report: AnalysisReport) -> SourceAuthority:
    authorities: dict[str, tuple[str, str, SourceLanguage]] = {}
    for source in report.source_files:
        authorities[source.logical_path] = (
            source.logical_path,
            source.source_sha256,
            source.language,
        )
    for module in report.test_modules:
        authority = (module.path, module.source_sha256, module.language)
        if module.path in authorities and authorities[module.path] != authority:
            raise ValueError("test module disagrees with saved source authority")
        authorities[module.path] = authority
    return authorities


def _validate_option_authority(
    authorities: SourceAuthority, option: ComparisonSourceOption
) -> ComparisonSourceOption:
    authority = authorities.get(option.logical_path)
    if authority is None:
        raise ValueError("comparison source option path is absent from saved report authority")
    path, sha256, language = authority
    if option.logical_path != path or option.source_sha256 != sha256 or option.language != language:
        raise ValueError("comparison source option disagrees with saved report authority")
    return option


def _options_from_evidence(
    authorities: SourceAuthority, evidence: tuple[Evidence, ...]
) -> tuple[tuple[ComparisonSourceOption, ...], int]:
    unique: dict[str, ComparisonSourceOption] = {}
    for item in evidence:
        authority = authorities.get(item.span.path)
        if authority is None:
            continue
        path, sha256, language = authority
        if item.source_sha256 != sha256 or item.span.language != language:
            continue
        start_line = item.span.start_line
        end_line = item.span.end_line
        option = ComparisonSourceOption(
            option_id=_option_id(
                logical_path=path,
                source_sha256=sha256,
                language=language,
                start_line=start_line,
                end_line=end_line,
                preview_kind=ComparisonSourcePreviewKind.EVIDENCE_SPAN,
            ),
            logical_path=path,
            source_sha256=sha256,
            language=language,
            start_line=start_line,
            end_line=end_line,
            preview_kind=ComparisonSourcePreviewKind.EVIDENCE_SPAN,
            label=f"{path}:{start_line}-{end_line}",
        )
        unique[option.option_id] = option
    ordered = tuple(sorted(unique.values(), key=_option_sort_key))
    omitted = max(0, len(ordered) - _MAX_SOURCE_OPTIONS)
    return ordered[:_MAX_SOURCE_OPTIONS], omitted


def _module_file_preview(module: Module) -> ComparisonSourceOption:
    end_line = max(1, module.line_count)
    return ComparisonSourceOption(
        option_id=_option_id(
            logical_path=module.path,
            source_sha256=module.source_sha256,
            language=module.language,
            start_line=1,
            end_line=end_line,
            preview_kind=ComparisonSourcePreviewKind.FILE_INVENTORY_PREVIEW,
        ),
        logical_path=module.path,
        source_sha256=module.source_sha256,
        language=module.language,
        start_line=1,
        end_line=end_line,
        preview_kind=ComparisonSourcePreviewKind.FILE_INVENTORY_PREVIEW,
        label=f"file inventory preview ({module.path}, lines 1-{end_line})",
    )


def _symbol_options(
    authorities: SourceAuthority, symbol: Symbol
) -> tuple[ComparisonSourceOption, ...]:
    authority = authorities.get(symbol.span.path)
    if authority is None:
        return ()
    path, sha256, language = authority
    option = ComparisonSourceOption(
        option_id=_option_id(
            logical_path=path,
            source_sha256=sha256,
            language=language,
            start_line=symbol.span.start_line,
            end_line=symbol.span.end_line,
            preview_kind=ComparisonSourcePreviewKind.EVIDENCE_SPAN,
        ),
        logical_path=path,
        source_sha256=sha256,
        language=language,
        start_line=symbol.span.start_line,
        end_line=symbol.span.end_line,
        preview_kind=ComparisonSourcePreviewKind.EVIDENCE_SPAN,
        label=f"{path}:{symbol.span.start_line}-{symbol.span.end_line}",
    )
    return (option,)


def _test_module_preview(record: TestModuleRecord) -> ComparisonSourceOption:
    return ComparisonSourceOption(
        option_id=_option_id(
            logical_path=record.path,
            source_sha256=record.source_sha256,
            language=record.language,
            start_line=1,
            end_line=1,
            preview_kind=ComparisonSourcePreviewKind.FILE_INVENTORY_PREVIEW,
        ),
        logical_path=record.path,
        source_sha256=record.source_sha256,
        language=record.language,
        start_line=1,
        end_line=1,
        preview_kind=ComparisonSourcePreviewKind.FILE_INVENTORY_PREVIEW,
        label=f"test module file preview ({record.path}, line 1)",
    )


def _collect_evidence(kind: str, model: BaseModel) -> tuple[Evidence, ...]:
    if kind in _SPANLESS_KINDS:
        return ()
    if kind == "module":
        return ()
    if kind == "symbol":
        return ()
    if kind == "test-module":
        return ()
    fields = _EVIDENCE_FIELDS.get(kind)
    if fields is None:
        return ()
    collected: list[Evidence] = []
    for name in fields:
        value = getattr(model, name, ())
        if not isinstance(value, tuple):
            raise ValueError(f"{kind} evidence field {name} must be a tuple")
        for item in value:
            if not isinstance(item, Evidence):
                raise ValueError(f"{kind} evidence field {name} must contain Evidence values")
            collected.append(item)
    return tuple(collected)


def _side_options(
    authorities: SourceAuthority, kind: str, model: BaseModel
) -> tuple[tuple[ComparisonSourceOption, ...], int, ComparisonSourceSideState, str]:
    if kind in _SPANLESS_KINDS:
        return (
            (),
            0,
            ComparisonSourceSideState.UNAVAILABLE,
            f"{kind} records do not retain exact source spans",
        )
    if kind == "module":
        if not isinstance(model, Module):
            raise ValueError("module comparison entity has the wrong model type")
        option = _module_file_preview(model)
        _validate_option_authority(authorities, option)
        return (
            (option,),
            0,
            ComparisonSourceSideState.AVAILABLE,
            "module file inventory preview is available",
        )
    if kind == "symbol":
        if not isinstance(model, Symbol):
            raise ValueError("symbol comparison entity has the wrong model type")
        options = _symbol_options(authorities, model)
        if not options:
            return (
                (),
                0,
                ComparisonSourceSideState.NO_EVIDENCE,
                "symbol span is absent from saved source authority",
            )
        for option in options:
            _validate_option_authority(authorities, option)
        return (
            options,
            0,
            ComparisonSourceSideState.AVAILABLE,
            "symbol evidence span is available",
        )
    if kind == "test-module":
        if not isinstance(model, TestModuleRecord):
            raise ValueError("test-module comparison entity has the wrong model type")
        option = _test_module_preview(model)
        _validate_option_authority(authorities, option)
        return (
            (option,),
            0,
            ComparisonSourceSideState.AVAILABLE,
            "test module file preview is available",
        )
    evidence = _collect_evidence(kind, model)
    if not evidence:
        if kind in _EVIDENCE_FIELDS:
            return (
                (),
                0,
                ComparisonSourceSideState.NO_EVIDENCE,
                f"{kind} record has no retained source evidence",
            )
        return (
            (),
            0,
            ComparisonSourceSideState.UNAVAILABLE,
            f"{kind} records do not retain exact source spans",
        )
    options, omitted = _options_from_evidence(authorities, evidence)
    if not options:
        return (
            (),
            0,
            ComparisonSourceSideState.NO_EVIDENCE,
            f"{kind} evidence is not bound to saved source authority",
        )
    for option in options:
        _validate_option_authority(authorities, option)
    return (
        options,
        omitted,
        ComparisonSourceSideState.AVAILABLE,
        f"{kind} source evidence is available",
    )


def build_comparison_source_side(
    *,
    report: AnalysisReport,
    report_sha256: str,
    kind: str,
    model: BaseModel | None,
    present: bool,
    authorities: SourceAuthority | None = None,
) -> StaticComparisonSourceSide:
    if not present or model is None:
        return StaticComparisonSourceSide(
            report_sha256=report_sha256,
            state=ComparisonSourceSideState.MISSING_RECORD,
            message="comparison side is absent for this row",
        )
    options, omitted, state, message = _side_options(
        _source_authorities(report) if authorities is None else authorities, kind, model
    )
    return StaticComparisonSourceSide(
        report_sha256=report_sha256,
        state=state,
        options=options,
        omitted_option_count=omitted,
        message=message,
    )


def build_comparison_source_binding(
    *,
    position: int,
    record: ComparisonRecord,
    kind: str,
    baseline_report: AnalysisReport,
    candidate_report: AnalysisReport,
    baseline_sha256: str,
    candidate_sha256: str,
    baseline_model: BaseModel | None,
    candidate_model: BaseModel | None,
    baseline_authorities: SourceAuthority,
    candidate_authorities: SourceAuthority,
) -> StaticComparisonSourceBinding:
    baseline_present = record.change is not ComparisonChange.ADDED
    candidate_present = record.change is not ComparisonChange.REMOVED
    return StaticComparisonSourceBinding(
        position=position,
        subject_id=record.subject_id,
        record_kind=kind,
        change=record.change,
        baseline=build_comparison_source_side(
            report=baseline_report,
            report_sha256=baseline_sha256,
            kind=kind,
            model=baseline_model,
            present=baseline_present,
            authorities=baseline_authorities,
        ),
        candidate=build_comparison_source_side(
            report=candidate_report,
            report_sha256=candidate_sha256,
            kind=kind,
            model=candidate_model,
            present=candidate_present,
            authorities=candidate_authorities,
        ),
    )


def build_comparison_source_bindings(
    baseline: AnalysisReport,
    candidate: AnalysisReport,
    delta: ComparisonProjection,
) -> tuple[StaticComparisonSourceBinding, ...]:
    if delta.identical:
        return ()
    baseline_sha = next(
        item.report_sha256 for item in delta.inputs if item.role is ProjectionInputRole.BASELINE
    )
    candidate_sha = next(
        item.report_sha256 for item in delta.inputs if item.role is ProjectionInputRole.CANDIDATE
    )
    baseline_index = comparison_entity_index(baseline)
    candidate_index = comparison_entity_index(candidate)
    baseline_authorities = _source_authorities(baseline)
    candidate_authorities = _source_authorities(candidate)
    bindings: list[StaticComparisonSourceBinding] = []
    for position, record in enumerate(delta.records):
        before = baseline_index.get(record.subject_id)
        after = candidate_index.get(record.subject_id)
        if before is None and after is None:
            raise ValueError("comparison record is absent from both reports")
        if before is not None:
            kind = before[0]
        else:
            assert after is not None
            kind = after[0]
        if before is not None and after is not None and before[0] != after[0]:
            raise ValueError("comparison record kind disagrees between reports")
        bindings.append(
            build_comparison_source_binding(
                position=position,
                record=record,
                kind=kind,
                baseline_report=baseline,
                candidate_report=candidate,
                baseline_sha256=baseline_sha,
                candidate_sha256=candidate_sha,
                baseline_model=before[1] if before is not None else None,
                candidate_model=after[1] if after is not None else None,
                baseline_authorities=baseline_authorities,
                candidate_authorities=candidate_authorities,
            )
        )
    return tuple(bindings)


def write_comparison_source_bindings(
    connection: sqlite3.Connection,
    bindings: tuple[StaticComparisonSourceBinding, ...],
) -> None:
    connection.execute(_BINDING_TABLE_SQL)
    total = 0
    for binding in bindings:
        payload = binding.model_dump_json()
        size = len(payload.encode())
        total += size
        if binding.position >= 100_000 or size > 1024**2 or total > 2 * 1024**3:
            raise ValueError("comparison source binding exceeds preparation bounds")
        connection.execute(
            "INSERT INTO comparison_source_bindings VALUES (?, ?)",
            (binding.position, payload),
        )


def write_comparison_test_sources(
    connection: sqlite3.Connection, baseline: AnalysisReport, candidate: AnalysisReport
) -> None:
    """Retain test-owned authority, including files outside the main inventory."""
    connection.execute(_TEST_SOURCE_TABLE_SQL)
    total = 0
    for side, report in (("baseline", baseline), ("candidate", candidate)):
        for number, module in enumerate(sorted(report.test_modules, key=lambda item: item.path)):
            payload = module.model_dump_json()
            size = len(payload.encode())
            total += size
            if number >= 100_000 or size > 1024**2 or total > 2 * 1024**3:
                raise ValueError("comparison test source exceeds preparation bounds")
            connection.execute(
                "INSERT INTO comparison_test_sources VALUES (?, ?, ?)",
                (side, module.path, payload),
            )


def _indexed_source_authorities(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, tuple[str, str, SourceLanguage]]]:
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'comparison_test_sources'"
    ).fetchone() != (_TEST_SOURCE_TABLE_SQL,):
        raise ValueError("comparison test source schema is invalid")
    authorities: dict[str, dict[str, tuple[str, str, SourceLanguage]]] = {
        "baseline": {},
        "candidate": {},
    }
    total = 0
    for side, table in (("baseline", "source_files"), ("candidate", "comparison_source_files")):
        for number, (path, payload) in enumerate(
            connection.execute(f"SELECT path, payload FROM {table} ORDER BY path")
        ):
            if not isinstance(payload, str) or len(payload.encode()) > 1024**2:
                raise ValueError("comparison source authority exceeds row bound")
            total += len(payload.encode())
            if number >= 1_000_000 or total > 2 * 1024**3:
                raise ValueError("comparison source authority exceeds inventory bound")
            source = SourceFileRecord.model_validate_json(payload)
            if source.logical_path != path:
                raise ValueError("comparison source authority path mismatch")
            authorities[side][path] = (path, source.source_sha256, source.language)
    for number, (side, path, payload) in enumerate(
        connection.execute(
            "SELECT side, path, payload FROM comparison_test_sources ORDER BY side, path"
        )
    ):
        if (
            side not in authorities
            or not isinstance(payload, str)
            or len(payload.encode()) > 1024**2
        ):
            raise ValueError("comparison test source authority is malformed")
        total += len(payload.encode())
        if number >= 200_000 or total > 2 * 1024**3:
            raise ValueError("comparison test source authority exceeds inventory bound")
        module = TestModuleRecord.model_validate_json(payload)
        if module.path != path:
            raise ValueError("comparison test source authority path mismatch")
        authority = (path, module.source_sha256, module.language)
        previous = authorities[side].get(path)
        if previous is not None and previous != authority:
            raise ValueError("comparison test module disagrees with source authority")
        authorities[side][path] = authority
    return authorities


def _binding_identity(binding: StaticComparisonSourceBinding, record: ComparisonRecord) -> None:
    if binding.subject_id != record.subject_id or binding.change != record.change:
        raise ValueError("comparison source binding disagrees with comparison row")
    if binding.position < 0:
        raise ValueError("comparison source binding position is invalid")


def validate_comparison_source_bindings(
    connection: sqlite3.Connection,
    *,
    retained: int,
    baseline_sha256: str,
    candidate_sha256: str,
) -> None:
    if retained == 0:
        if connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'comparison_source_bindings'"
        ).fetchone():
            raise ValueError("identical comparison cannot retain source bindings")
        return
    if connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'comparison_source_bindings'"
    ).fetchone() != (_BINDING_TABLE_SQL,):
        raise ValueError("comparison source binding schema is invalid")
    expected = (retained, 0 if retained else None, retained - 1 if retained else None)
    if (
        connection.execute(
            "SELECT count(*), min(position), max(position) FROM comparison_source_bindings"
        ).fetchone()
        != expected
    ):
        raise ValueError("comparison source binding count disagrees with comparison receipt")
    total_bytes = 0
    authorities = _indexed_source_authorities(connection)
    for position, payload, record_payload in connection.execute(
        "SELECT binding.position, binding.payload, record.payload "
        "FROM comparison_source_bindings AS binding "
        "LEFT JOIN comparison_rows AS record ON record.position = binding.position "
        "ORDER BY binding.position"
    ):
        if not isinstance(payload, str):
            raise ValueError("comparison source binding payload is malformed")
        payload_bytes = len(payload.encode())
        total_bytes += payload_bytes
        if payload_bytes > 1024**2 or total_bytes > 2 * 1024**3:
            raise ValueError("comparison source binding exceeds byte bound")
        binding = StaticComparisonSourceBinding.model_validate_json(payload)
        if binding.position != position:
            raise ValueError("comparison source binding position mismatch")
        if binding.baseline.report_sha256 != baseline_sha256:
            raise ValueError("comparison source baseline hash disagrees with receipt")
        if binding.candidate.report_sha256 != candidate_sha256:
            raise ValueError("comparison source candidate hash disagrees with receipt")
        for side, details in (("baseline", binding.baseline), ("candidate", binding.candidate)):
            for option in details.options:
                _validate_option_authority(authorities[side], option)
        if not isinstance(record_payload, str):
            raise ValueError("comparison source binding has no comparison row")
        record = ComparisonRecord.model_validate_json(record_payload)
        _binding_identity(binding, record)
        if binding.record_kind not in _SPANLESS_KINDS.union(_EVIDENCE_FIELDS).union(
            {"module", "symbol", "test-module"}
        ):
            raise ValueError("comparison source binding record kind is unknown")


def read_comparison_source_binding(
    connection: sqlite3.Connection, position: int
) -> StaticComparisonSourceBinding:
    row = connection.execute(
        "SELECT payload FROM comparison_source_bindings WHERE position = ?", (position,)
    ).fetchone()
    if row is None or not isinstance(row[0], str) or len(row[0].encode()) > 1024**2:
        raise ValueError("comparison source binding is unavailable or outside byte bound")
    binding = StaticComparisonSourceBinding.model_validate_json(row[0])
    if binding.position != position:
        raise ValueError("comparison source binding position mismatch")
    comparison = connection.execute(
        "SELECT payload FROM comparison_rows WHERE position = ?", (position,)
    ).fetchone()
    if comparison is None or not isinstance(comparison[0], str):
        raise ValueError("comparison source binding has no comparison row")
    record = ComparisonRecord.model_validate_json(comparison[0])
    _binding_identity(binding, record)
    return binding


class StaticComparisonSelection(StrictModel):
    baseline_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    position: StrictInt = Field(ge=0, le=100_000)


class StaticComparisonSourceQuery(StaticComparisonSelection):
    side: ComparisonSourceSide
    option_id: StrictStr = Field(pattern=r"^source-option-[0-9a-f]{20}$")


def validate_static_comparison_selection(
    binding: StaticComparisonSourceBinding,
    query: StaticComparisonSelection,
) -> None:
    if (
        binding.baseline.report_sha256 != query.baseline_sha256
        or binding.candidate.report_sha256 != query.candidate_sha256
    ):
        raise ValueError("comparison source query report pair mismatch")
    if binding.position != query.position:
        raise ValueError("comparison source query position mismatch")


def resolve_static_comparison_source_option(
    binding: StaticComparisonSourceBinding,
    query: StaticComparisonSourceQuery,
) -> ComparisonSourceOption:
    validate_static_comparison_selection(binding, query)
    side_model = binding.baseline if query.side == "baseline" else binding.candidate
    if side_model.state is ComparisonSourceSideState.MISSING_RECORD:
        raise ValueError("comparison source side is absent for this row")
    if side_model.state is not ComparisonSourceSideState.AVAILABLE:
        raise ValueError("comparison source side has no selectable options")
    matches = [item for item in side_model.options if item.option_id == query.option_id]
    if not matches:
        raise ValueError("comparison source option is not available for this row and side")
    if len(matches) != 1:
        raise ValueError("comparison source option identity is ambiguous")
    return matches[0]


def preview_static_comparison_source(
    binding: StaticComparisonSourceBinding, query: StaticComparisonSourceQuery, root: Path | None
) -> SourcePreview:
    option = resolve_static_comparison_source_option(binding, query)
    return preview_source_record(
        SourcePreviewIdentity(logical_path=option.logical_path, source_sha256=option.source_sha256),
        root,
        start=option.start_line,
        end=min(option.end_line, option.start_line + 39),
    )


def stable_evidence_option_id(evidence: Evidence) -> str:
    return _option_id(
        logical_path=evidence.span.path,
        source_sha256=evidence.source_sha256,
        language=evidence.language,
        start_line=evidence.span.start_line,
        end_line=evidence.span.end_line,
        preview_kind=ComparisonSourcePreviewKind.EVIDENCE_SPAN,
    )


def evidence_catalog_option_id(evidence: Evidence) -> str:
    """Expose catalog-stable evidence identity for cross-generation regression tests."""

    _ = evidence_identity(evidence)
    return stable_evidence_option_id(evidence)
