"""Saved-report outline browsing: files, declarations, and recorded domain evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal, Self

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from code_analysis.models import (
    Completeness,
    Evidence,
    Module,
    SourceLanguage,
    SourceSpan,
    StrictModel,
    Symbol,
)
from code_analysis.report_models import (
    AnalysisReport,
    AnalysisSubject,
    SourceFileRecord,
    SubjectKind,
)
from code_analysis.reports.json_report import canonical_json

type ParentRelation = Literal["module-membership", "span-containment"]

_DOMAIN_KIND = {
    "contract": "contract-evidence",
    "shape": "dictionary-shape-evidence",
    "structure": "data-structure-evidence",
    "shape-state": "observed-state",
}


class CodeBrowseSelectionError(ValueError):
    """Requested path or identity is absent from the saved browse index."""


class CodeBrowseBindingError(ValueError):
    """Report binding does not match the prepared browse reader."""


def _normalize_browse_path(path: str) -> str:
    if path == "":
        return ""
    if "\\" in path or path.startswith("/"):
        raise ValueError("browse path must be a normalized relative POSIX path")
    normalized = PurePosixPath(path).as_posix()
    if (
        not normalized
        or normalized == "."
        or PurePosixPath(normalized).is_absolute()
        or ".." in PurePosixPath(normalized).parts
        or any(part in {"", "."} for part in normalized.split("/"))
        or normalized != path
    ):
        raise ValueError("browse path must be a normalized relative POSIX path")
    return normalized


def _span_key(span: SourceSpan) -> tuple[object, ...]:
    return (
        span.path,
        span.start_line,
        span.start_column,
        span.end_line,
        span.end_column,
        span.language.value,
    )


def _span_size(span: SourceSpan) -> tuple[int, int]:
    return (span.end_line - span.start_line, span.end_column - span.start_column)


def _strictly_contains(outer: SourceSpan, inner: SourceSpan) -> bool:
    if outer.path != inner.path:
        return False
    outer_key = (
        outer.start_line,
        outer.start_column,
        outer.end_line,
        outer.end_column,
    )
    inner_key = (
        inner.start_line,
        inner.start_column,
        inner.end_line,
        inner.end_column,
    )
    if outer_key == inner_key:
        return False
    return (outer.start_line, outer.start_column) <= (inner.start_line, inner.start_column) and (
        inner.end_line,
        inner.end_column,
    ) <= (outer.end_line, outer.end_column)


def _line_contains(span: SourceSpan, line: int) -> bool:
    return span.start_line <= line <= span.end_line


def _module_span(module_path: str, line_count: int, language: SourceLanguage) -> SourceSpan:
    return SourceSpan(
        path=module_path,
        start_line=1,
        start_column=0,
        end_line=max(1, line_count),
        end_column=0,
        language=language,
    )


def _spans_for_path(spans: tuple[SourceSpan, ...], path: str) -> tuple[SourceSpan, ...]:
    return tuple(span for span in spans if span.path == path)


def _entry_sort_key(entry: CodeBrowseEntry) -> tuple[object, ...]:
    primary = entry.spans[0] if entry.spans else (entry.logical_path, entry.identity)
    if isinstance(primary, SourceSpan):
        return (
            primary.start_line,
            primary.start_column,
            primary.end_line,
            primary.end_column,
            entry.kind,
            entry.identity,
        )
    return (0, 0, 0, 0, entry.kind, entry.identity)


class CodeBrowseRequest(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    revision: StrictInt = Field(ge=0)
    path: StrictStr = ""
    identity: StrictStr = ""
    search: StrictStr = Field(default="", max_length=256)
    offset: StrictInt = Field(default=0, ge=0)
    limit: StrictInt = Field(default=50, ge=1, le=100)
    line: StrictInt | None = Field(default=None, ge=1)

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        return _normalize_browse_path(value)

    @model_validator(mode="after")
    def validate_line_requires_path(self) -> Self:
        if self.line is not None and not self.path:
            raise ValueError("line selection requires a browse path")
        return self


class CodeBrowseEntry(StrictModel):
    identity: StrictStr
    label: StrictStr
    kind: StrictStr
    module: StrictStr
    language: SourceLanguage
    source_file_id: StrictStr = Field(pattern=r"^source-file-[0-9a-f]{20}$")
    logical_path: StrictStr = Field(min_length=1)
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    spans: tuple[SourceSpan, ...] = Field(min_length=1)
    parent_identity: StrictStr | None = None
    parent_relation: ParentRelation | None = None

    @field_validator("logical_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _normalize_browse_path(value)

    @model_validator(mode="after")
    def validate_parent_and_spans(self) -> Self:
        if (self.parent_identity is None) != (self.parent_relation is None):
            raise ValueError("parent identity and relation must be provided together")
        paths = {span.path for span in self.spans}
        if len(paths) != 1 or next(iter(paths)) != self.logical_path:
            raise ValueError("browse entry spans must belong to one logical path")
        languages = {span.language for span in self.spans}
        if len(languages) != 1 or next(iter(languages)) is not self.language:
            raise ValueError("browse entry spans must share one language")
        return self


class CodeBrowsePage(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    revision: StrictInt = Field(ge=0)
    completeness: Completeness
    logical_path: StrictStr | None = None
    records: tuple[CodeBrowseEntry, ...] = Field(max_length=100)
    eligible: StrictInt = Field(ge=0)
    offset: StrictInt = Field(ge=0)
    limit: StrictInt = Field(ge=1, le=100)
    next_offset: StrictInt | None = Field(default=None, ge=0)
    selected: CodeBrowseEntry | None = None
    line_matches: tuple[CodeBrowseEntry, ...] = Field(default=(), max_length=100)
    line_match_count: StrictInt = Field(default=0, ge=0)
    limitations: tuple[StrictStr, ...] = ()
    selection_locations: tuple[CodeBrowseEntry, ...] = Field(default=(), max_length=100)
    selection_location_count: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        if self.limitations != tuple(sorted(set(self.limitations))):
            raise ValueError("browse limitations must be unique and sorted")
        retained = min(self.limit, max(0, self.eligible - self.offset))
        if len(self.records) != retained:
            raise ValueError("browse page record count does not reconcile")
        stop = self.offset + len(self.records)
        expected_next = stop if stop < self.eligible else None
        if self.next_offset != expected_next:
            raise ValueError("browse page continuation does not reconcile")
        if self.line_match_count < len(self.line_matches):
            raise ValueError("line match count cannot be smaller than returned matches")
        identities = tuple(item.identity for item in self.records)
        if len(set(identities)) != len(identities):
            raise ValueError("browse page identities must be unique")
        if self.logical_path and any(
            item.logical_path != self.logical_path for item in self.records
        ):
            raise ValueError("browse page records must belong to the selected path")
        if self.selection_location_count < len(self.selection_locations):
            raise ValueError("selection location count does not reconcile")
        if (
            self.line_match_count > len(self.line_matches)
            or self.selection_location_count > len(self.selection_locations)
        ) and self.completeness is Completeness.COMPLETE:
            raise ValueError("omitted selection evidence cannot be complete")
        if (
            self.selected is not None
            and self.logical_path is not None
            and self.selected.logical_path != self.logical_path
        ):
            raise ValueError("selected browse entry disagrees with the requested path")
        return self


@dataclass(frozen=True, slots=True)
class _PathIndex:
    entries: tuple[CodeBrowseEntry, ...]
    by_identity: Mapping[str, CodeBrowseEntry]


@dataclass(frozen=True, slots=True)
class CodeBrowseReader:
    """Immutable saved-report outline index."""

    report_sha256: str
    completeness: Completeness
    files: tuple[CodeBrowseEntry, ...]
    paths: Mapping[str, _PathIndex]
    identity_map: Mapping[str, CodeBrowseEntry]
    identity_paths: Mapping[str, tuple[str, ...]]
    aliases: Mapping[str, str]

    @classmethod
    def from_report(cls, report: AnalysisReport, report_sha256: str) -> CodeBrowseReader:
        if len(report_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in report_sha256
        ):
            raise ValueError("report SHA-256 must be 64 lowercase hexadecimal characters")
        source_by_path = {item.logical_path: item for item in report.source_files}
        modules_by_path = {module.path: module for module in report.modules}
        _validate_source_bindings(report, source_by_path)
        aliases, suppressed_subjects = _subject_aliases(report)
        files = tuple(_file_entries(report.source_files))
        file_by_identity = {entry.identity: entry for entry in files}
        subjects_by_module: dict[str, list[AnalysisSubject]] = {}
        for subject in report.subjects:
            subjects_by_module.setdefault(subject.module, []).append(subject)
        path_indexes: dict[str, _PathIndex] = {}
        identity_map: dict[str, CodeBrowseEntry] = {}
        identity_paths: dict[str, list[str]] = {}
        for path, source in sorted(source_by_path.items()):
            module = modules_by_path.get(path)
            entries = _path_entries(
                path,
                source,
                module,
                tuple(subjects_by_module.get(module.name, ())) if module else (),
                suppressed_subjects,
            )
            by_identity = MappingProxyType({entry.identity: entry for entry in entries})
            path_indexes[path] = _PathIndex(entries=entries, by_identity=by_identity)
            for entry in entries:
                identity_map[entry.identity] = entry
                identity_paths.setdefault(entry.identity, []).append(path)
        for source in report.source_files:
            identity_map[source.source_file_id] = file_by_identity[source.source_file_id]
            identity_paths.setdefault(source.source_file_id, []).append(source.logical_path)
        for module in report.modules:
            path_index = path_indexes.get(module.path)
            if path_index is not None and module.name in path_index.by_identity:
                identity_map[module.name] = path_index.by_identity[module.name]
            for symbol in module.symbols:
                identity = f"{module.name}:{symbol.qualified_name}"
                if path_index is not None and identity in path_index.by_identity:
                    identity_map[identity] = path_index.by_identity[identity]
        for alias, canonical in aliases.items():
            if canonical in identity_map:
                identity_map[alias] = identity_map[canonical]
        _attach_domain_entries(report, source_by_path, path_indexes, identity_map, identity_paths)
        for path, index in tuple(path_indexes.items()):
            entries = tuple(sorted(_assign_span_parents(list(index.entries)), key=_entry_sort_key))
            path_indexes[path] = _PathIndex(
                entries, MappingProxyType({entry.identity: entry for entry in entries})
            )
        return cls(
            report_sha256=report_sha256,
            completeness=report.completeness,
            files=files,
            paths=MappingProxyType(path_indexes),
            identity_map=MappingProxyType(identity_map),
            identity_paths=MappingProxyType(
                {key: tuple(sorted(set(values))) for key, values in identity_paths.items()}
            ),
            aliases=MappingProxyType(aliases),
        )

    def read(self, request: CodeBrowseRequest) -> CodeBrowsePage:
        if request.report_sha256 != self.report_sha256:
            raise CodeBrowseBindingError("browse request is bound to another report")
        limitations: list[str] = []
        if request.path and request.path not in self.paths:
            raise CodeBrowseSelectionError("browse path is not present in this saved report")
        if request.identity:
            canonical = self.aliases.get(request.identity, request.identity)
            if canonical not in self.identity_map:
                raise CodeBrowseSelectionError(
                    "browse identity has no recorded outline mapping in this saved report"
                )
            if request.path:
                allowed_paths = self.identity_paths.get(canonical, ())
                if request.path not in allowed_paths:
                    raise CodeBrowseSelectionError(
                        "browse identity is not recorded for the requested path"
                    )
        path = request.path
        locations: tuple[CodeBrowseEntry, ...] = ()
        location_count = 0
        if request.identity:
            canonical = self.aliases.get(request.identity, request.identity)
            paths = self.identity_paths.get(canonical, ())
            all_locations = tuple(
                self._selected_entry(canonical, item, limitations) for item in paths
            )
            location_count = len(all_locations)
            locations = all_locations[:100]
            if not path and len(paths) == 1:
                path = paths[0]
            elif not path and len(paths) > 1:
                limitations.append(
                    "Choose a recorded source location; this identity spans multiple files."
                )
            if location_count > 100:
                limitations.append("Recorded location choices exceed the 100-location bound.")
        pool = self.files if not path else self.paths[path].entries
        filtered = _search_records(pool, request.search)
        eligible = len(filtered)
        page_records = filtered[request.offset : request.offset + request.limit]
        selected = None
        if request.identity and path:
            canonical = self.aliases.get(request.identity, request.identity)
            selected = self._selected_entry(canonical, path, limitations)
        line_matches: tuple[CodeBrowseEntry, ...] = ()
        line_match_count = 0
        if request.line is not None and path:
            line_matches, line_match_count, line_limits = _line_matches(
                self.paths[path].entries, request.line
            )
            limitations.extend(line_limits)
            if not request.identity and len(line_matches) == 1:
                selected = line_matches[0]
        bounded = line_match_count > len(line_matches) or location_count > len(locations)
        return CodeBrowsePage(
            report_sha256=self.report_sha256,
            revision=request.revision,
            completeness=(
                Completeness.PARTIAL
                if bounded and self.completeness is Completeness.COMPLETE
                else self.completeness
            ),
            logical_path=path or None,
            records=page_records,
            eligible=eligible,
            offset=request.offset,
            limit=request.limit,
            next_offset=(
                request.offset + len(page_records)
                if request.offset + len(page_records) < eligible
                else None
            ),
            selected=selected,
            line_matches=line_matches,
            line_match_count=line_match_count,
            limitations=tuple(sorted(set(limitations))),
            selection_locations=locations,
            selection_location_count=location_count,
        )

    def _selected_entry(self, canonical: str, path: str, limitations: list[str]) -> CodeBrowseEntry:
        entry = self.identity_map[canonical]
        paths = self.identity_paths.get(canonical, ())
        if path:
            indexed = self.paths[path].by_identity.get(canonical)
            if indexed is not None:
                return indexed
            if entry.logical_path == path:
                return entry
            raise CodeBrowseSelectionError("selected identity has no entry for this source path")
        if len(paths) > 1:
            limitations.append(
                "Recorded evidence spans multiple source files; narrow selection with path."
            )
        return entry


def read_code_browse(report: AnalysisReport, request: CodeBrowseRequest) -> CodeBrowsePage:
    """Convenience reader using the canonical compact report hash."""

    digest = hashlib.sha256(canonical_json(report).encode()).hexdigest()
    return CodeBrowseReader.from_report(report, digest).read(request)


def _file_entries(source_files: tuple[SourceFileRecord, ...]) -> tuple[CodeBrowseEntry, ...]:
    entries = [
        CodeBrowseEntry(
            identity=source.source_file_id,
            label=source.logical_path,
            kind="source-file",
            module=source.module_name,
            language=source.language,
            source_file_id=source.source_file_id,
            logical_path=source.logical_path,
            source_sha256=source.source_sha256,
            spans=(
                SourceSpan(
                    path=source.logical_path,
                    start_line=1,
                    start_column=0,
                    end_line=max(1, source.physical_loc),
                    end_column=0,
                    language=source.language,
                ),
            ),
        )
        for source in sorted(source_files, key=lambda item: item.logical_path)
    ]
    return tuple(entries)


def _validate_source_bindings(
    report: AnalysisReport, sources: Mapping[str, SourceFileRecord]
) -> None:
    if len(sources) != len(report.source_files) or len(
        {item.source_file_id for item in report.source_files}
    ) != len(report.source_files):
        raise ValueError("browse source identities and paths must be unique")
    if len({module.name for module in report.modules}) != len(report.modules) or len(
        {module.path for module in report.modules}
    ) != len(report.modules):
        raise ValueError("browse module identities and paths must be unique")
    for source in report.source_files:
        _normalize_browse_path(source.logical_path)
    for module in report.modules:
        source = sources.get(module.path)
        if source is None or (source.language, source.source_sha256, source.module_name) != (
            module.language,
            module.source_sha256,
            module.name,
        ):
            raise ValueError("browse module disagrees with its saved source identity")
        for symbol in module.symbols:
            if (
                symbol.language is not source.language
                or symbol.span.path != source.logical_path
                or symbol.span.language is not source.language
            ):
                raise ValueError("browse symbol disagrees with its saved source identity")
        if len({symbol.qualified_name for symbol in module.symbols}) != len(module.symbols):
            raise ValueError("browse declaration identities must be unique")
    for subject in report.subjects:
        _validate_evidence_bindings(subject.evidence, sources)
    for record in (*report.contracts, *report.dictionary_shapes, *report.data_structures):
        _validate_evidence_bindings(record.evidence, sources)
    for shape in report.dictionary_shapes:
        for state in shape.lifecycle_states:
            _validate_evidence_bindings(state.evidence, sources)


def _validate_evidence_bindings(
    evidence: tuple[Evidence, ...], sources: Mapping[str, SourceFileRecord]
) -> None:
    for item in evidence:
        source = sources.get(item.span.path)
        if (
            source is None
            or item.source_sha256 != source.source_sha256
            or item.span.language is not source.language
        ):
            raise ValueError("browse evidence disagrees with its saved source identity")


def _subject_aliases(report: AnalysisReport) -> tuple[dict[str, str], frozenset[str]]:
    aliases: dict[str, str] = {}
    suppressed: set[str] = set()
    symbols_by_module: dict[str, dict[str, Symbol]] = {}
    modules_by_name = {module.name: module for module in report.modules}
    for module in report.modules:
        symbols_by_module[module.name] = {
            symbol.qualified_name: symbol for symbol in module.symbols
        }
    for subject in report.subjects:
        if subject.module not in modules_by_name:
            continue
        symbols = symbols_by_module.get(subject.module, {})
        if subject.kind is SubjectKind.MODULE and subject.logical_name == subject.module:
            canonical = subject.module
            if canonical in modules_by_name:
                aliases[subject.subject_id] = canonical
                suppressed.add(subject.subject_id)
            continue
        if subject.kind is not SubjectKind.SYMBOL:
            continue
        parts = subject.logical_name.split(":", 1)
        if len(parts) != 2 or parts[0] != subject.module:
            continue
        symbol = symbols.get(parts[1])
        if symbol is None:
            continue
        if _span_key(subject.evidence[0].span) != _span_key(symbol.span):
            continue
        canonical = f"{subject.module}:{symbol.qualified_name}"
        aliases[subject.subject_id] = canonical
        suppressed.add(subject.subject_id)
    return aliases, frozenset(suppressed)


def _path_entries(
    path: str,
    source: SourceFileRecord,
    module: Module | None,
    subjects: tuple[AnalysisSubject, ...],
    suppressed_subjects: frozenset[str],
) -> tuple[CodeBrowseEntry, ...]:
    entries: list[CodeBrowseEntry] = []
    if module is None:
        return ()
    module_span = _module_span(module.path, module.line_count, module.language)
    entries.append(
        CodeBrowseEntry(
            identity=module.name,
            label=module.name,
            kind="module",
            module=module.name,
            language=module.language,
            source_file_id=source.source_file_id,
            logical_path=path,
            source_sha256=source.source_sha256,
            spans=(module_span,),
            parent_identity=source.source_file_id,
            parent_relation="module-membership",
        )
    )
    for symbol in sorted(
        module.symbols,
        key=lambda item: (item.span.start_line, item.span.start_column, item.qualified_name),
    ):
        identity = f"{module.name}:{symbol.qualified_name}"
        entries.append(
            CodeBrowseEntry(
                identity=identity,
                label=symbol.qualified_name,
                kind=symbol.kind,
                module=module.name,
                language=symbol.language,
                source_file_id=source.source_file_id,
                logical_path=path,
                source_sha256=source.source_sha256,
                spans=(symbol.span,),
            )
        )
    for subject in subjects:
        if subject.subject_id in suppressed_subjects:
            continue
        if subject.module != module.name:
            continue
        spans = _spans_for_path(tuple(item.span for item in subject.evidence), path)
        if not spans:
            continue
        entries.append(
            CodeBrowseEntry(
                identity=subject.subject_id,
                label=subject.logical_name,
                kind=subject.kind.value,
                module=subject.module,
                language=subject.language,
                source_file_id=source.source_file_id,
                logical_path=path,
                source_sha256=source.source_sha256,
                spans=spans,
            )
        )
    entries = _assign_span_parents(entries)
    return tuple(sorted(entries, key=_entry_sort_key))


def _assign_span_parents(entries: list[CodeBrowseEntry]) -> list[CodeBrowseEntry]:
    by_identity = {entry.identity: entry for entry in entries}
    updated: list[CodeBrowseEntry] = []
    for entry in entries:
        if entry.parent_identity is not None:
            updated.append(entry)
            continue
        primary = entry.spans[0]
        module_parent = (
            entry.module if entry.kind != "module" and entry.module in by_identity else None
        )
        containers = [
            candidate
            for candidate in entries
            if candidate.identity != entry.identity
            and _strictly_contains(candidate.spans[0], primary)
        ]
        parent_identity = None
        parent_relation: ParentRelation | None = None
        if containers:
            sizes = {_span_size(candidate.spans[0]) for candidate in containers}
            smallest = min(sizes)
            smallest_containers = [
                candidate for candidate in containers if _span_size(candidate.spans[0]) == smallest
            ]
            if len(smallest_containers) == 1:
                parent_identity = smallest_containers[0].identity
                parent_relation = "span-containment"
        elif module_parent is not None and entry.kind != "module":
            parent_identity = module_parent
            parent_relation = "module-membership"
        if parent_identity is not None:
            updated.append(
                entry.model_copy(
                    update={
                        "parent_identity": parent_identity,
                        "parent_relation": parent_relation,
                    }
                )
            )
        else:
            updated.append(entry)
    return updated


def _attach_domain_entries(
    report: AnalysisReport,
    source_by_path: Mapping[str, SourceFileRecord],
    path_indexes: dict[str, _PathIndex],
    identity_map: dict[str, CodeBrowseEntry],
    identity_paths: dict[str, list[str]],
) -> None:
    subjects = {item.subject_id: item for item in report.subjects}
    for contract in report.contracts:
        _add_domain_record(
            contract.contract_id,
            contract.contract_id,
            _DOMAIN_KIND["contract"],
            contract.defining_subject_id,
            tuple(item.span for item in contract.evidence),
            subjects,
            source_by_path,
            path_indexes,
            identity_map,
            identity_paths,
        )
    for shape in report.dictionary_shapes:
        _add_domain_record(
            shape.shape_id,
            shape.shape_id,
            _DOMAIN_KIND["shape"],
            shape.owner_subject_id,
            tuple(item.span for item in shape.evidence),
            subjects,
            source_by_path,
            path_indexes,
            identity_map,
            identity_paths,
        )
        for state in shape.lifecycle_states:
            _add_domain_record(
                state.state_id,
                state.name,
                _DOMAIN_KIND["shape-state"],
                shape.owner_subject_id,
                tuple(item.span for item in state.evidence),
                subjects,
                source_by_path,
                path_indexes,
                identity_map,
                identity_paths,
            )
    for structure in report.data_structures:
        _add_domain_record(
            structure.structure_id,
            structure.structure_id,
            _DOMAIN_KIND["structure"],
            structure.owner_subject_id,
            tuple(item.span for item in structure.evidence),
            subjects,
            source_by_path,
            path_indexes,
            identity_map,
            identity_paths,
        )


def _add_domain_record(
    identity: str,
    label: str,
    kind: str,
    owner_subject_id: str,
    spans: tuple[SourceSpan, ...],
    subjects: Mapping[str, AnalysisSubject],
    source_by_path: Mapping[str, SourceFileRecord],
    path_indexes: dict[str, _PathIndex],
    identity_map: dict[str, CodeBrowseEntry],
    identity_paths: dict[str, list[str]],
) -> None:
    owner = subjects.get(owner_subject_id)
    if owner is None or not spans:
        return
    grouped: dict[str, list[SourceSpan]] = {}
    for span in spans:
        grouped.setdefault(span.path, []).append(span)
    for path, path_spans in sorted(grouped.items()):
        source = source_by_path.get(path)
        if source is None:
            continue
        entry = CodeBrowseEntry(
            identity=identity,
            label=label,
            kind=kind,
            module=owner.module,
            language=source.language,
            source_file_id=source.source_file_id,
            logical_path=path,
            source_sha256=source.source_sha256,
            spans=tuple(sorted(path_spans, key=_span_key)),
        )
        path_index = path_indexes.setdefault(path, _PathIndex(entries=(), by_identity={}))
        merged = list(path_index.entries)
        merged.append(entry)
        by_identity = {item.identity: item for item in merged}
        path_indexes[path] = _PathIndex(
            entries=tuple(sorted(merged, key=_entry_sort_key)),
            by_identity=MappingProxyType(by_identity),
        )
        identity_map[identity] = entry
        identity_paths.setdefault(identity, []).append(path)


def _search_records(
    records: tuple[CodeBrowseEntry, ...], search: str
) -> tuple[CodeBrowseEntry, ...]:
    if not search:
        return records
    needle = search.casefold()
    return tuple(
        record
        for record in records
        if needle
        in " ".join(
            (
                record.label.casefold(),
                record.identity.casefold(),
                record.logical_path.casefold(),
                record.module.casefold(),
            )
        )
    )


def _line_matches(
    entries: tuple[CodeBrowseEntry, ...], line: int
) -> tuple[tuple[CodeBrowseEntry, ...], int, list[str]]:
    containing = [
        entry for entry in entries if any(_line_contains(span, line) for span in entry.spans)
    ]
    if not containing:
        return (), 0, []
    # Module spans have no measured end column. They are a fallback, never a
    # narrower declaration than a function spanning the same complete file.
    declarations = [entry for entry in containing if entry.kind != "module"]
    if declarations:
        containing = declarations
    innermost = [
        entry
        for entry in containing
        if not any(
            other.identity != entry.identity
            and any(
                _line_contains(outer, line)
                and _line_contains(inner, line)
                and _strictly_contains(outer, inner)
                for outer in entry.spans
                for inner in other.spans
            )
            for other in containing
        )
    ]
    innermost = sorted(innermost, key=_entry_sort_key)
    limitations: list[str] = []
    total = len(innermost)
    if total > 100:
        limitations.append(
            "Line matches exceed the browse bound; results are partial (100 of "
            f"{total} equally specific matches)."
        )
        innermost = innermost[:100]
    return tuple(innermost), total, limitations
