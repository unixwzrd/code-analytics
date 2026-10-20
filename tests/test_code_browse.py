"""Focused tests for saved-report code outline browsing."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.code_browse import (
    CodeBrowseBindingError,
    CodeBrowseReader,
    CodeBrowseRequest,
    CodeBrowseSelectionError,
    read_code_browse,
)
from code_analysis.config import AnalysisConfig
from code_analysis.models import Confidence, SourceLanguage, SourceSpan, Symbol
from code_analysis.project import discover_context
from code_analysis.report_models import AnalysisReport, SubjectKind
from code_analysis.reports.json_report import canonical_json
from code_analysis.service import scan

FIXTURES = Path(__file__).parent / "fixtures"


def _digest(report: AnalysisReport) -> str:
    return hashlib.sha256(canonical_json(report).encode()).hexdigest()


def _request(report: AnalysisReport, **updates: object) -> CodeBrowseRequest:
    payload = {
        "report_sha256": _digest(report),
        "revision": 0,
        "path": "",
        "identity": "",
        "search": "",
        "offset": 0,
        "limit": 50,
        "line": None,
    }
    payload.update(updates)
    return CodeBrowseRequest.model_validate(payload)


def _reader(report: AnalysisReport) -> CodeBrowseReader:
    return CodeBrowseReader.from_report(report, _digest(report))


@pytest.fixture
def nested_report(tmp_path: Path) -> AnalysisReport:
    source = tmp_path / "nested.py"
    source.write_text(
        "class Outer:\n"
        "    def method(self):\n"
        "        def inner():\n"
        "            return 1\n"
        "        return inner()\n"
    )
    report = scan(discover_context(source, AnalysisConfig()))
    shutil.rmtree(tmp_path)
    return AnalysisReport.model_validate_json(report.model_dump_json())


def test_request_rejects_bools_extras_and_invalid_paths(ordinary_report) -> None:
    with pytest.raises(ValidationError):
        CodeBrowseRequest.model_validate(
            {
                "report_sha256": _digest(ordinary_report),
                "revision": 0,
                "limit": True,
            }
        )
    with pytest.raises(ValidationError):
        _request(ordinary_report, extra="x")
    with pytest.raises(ValidationError):
        _request(ordinary_report, path="../secret.py")
    with pytest.raises(ValidationError):
        _request(ordinary_report, path="alpha\\a.py")
    with pytest.raises(ValidationError):
        _request(ordinary_report, line=3)


def test_file_outline_is_source_free_and_stable(ordinary_report, tmp_path: Path) -> None:
    source = tmp_path / "mirror"
    shutil.copytree(FIXTURES / "ordinary", source)
    report = scan(discover_context(source, AnalysisConfig()))
    shutil.rmtree(source)
    reader = _reader(report)
    first = reader.read(_request(report, limit=1))
    second = reader.read(_request(report, offset=1, limit=1))
    assert first.eligible == len(report.source_files)
    assert {row.identity for row in first.records} <= {
        item.source_file_id for item in report.source_files
    }
    assert first.records == reader.read(_request(report, limit=1)).records
    assert second.records[0] == reader.read(_request(report, offset=1, limit=1)).records[0]
    assert first.next_offset == 1
    assert second.next_offset == 2


def test_path_view_lists_symbols_and_subject_alias_resolution(ordinary_report) -> None:
    reader = _reader(ordinary_report)
    module = next(item for item in ordinary_report.modules if item.symbols)
    path = module.path
    page = reader.read(_request(ordinary_report, path=path))
    identities = {row.identity for row in page.records}
    assert module.name in identities
    assert any(identity.startswith(f"{module.name}:") for identity in identities)
    subject = next(
        item
        for item in ordinary_report.subjects
        if item.kind is SubjectKind.SYMBOL and item.module == module.name
    )
    assert subject.subject_id not in identities
    selected = reader.read(
        _request(ordinary_report, path=path, identity=subject.subject_id)
    ).selected
    assert selected is not None
    assert selected.identity == f"{module.name}:{subject.logical_name.split(':', 1)[1]}"


def test_search_casefold_and_paging(ordinary_report) -> None:
    reader = _reader(ordinary_report)
    target = ordinary_report.source_files[0].logical_path
    filtered = reader.read(_request(ordinary_report, search=target.split("/")[-1].upper()))
    assert filtered.eligible == 1
    assert filtered.records[0].logical_path == target
    wide = reader.read(_request(ordinary_report, limit=1))
    tail = reader.read(_request(ordinary_report, offset=1, limit=1))
    assert wide.records[0] != tail.records[0]
    assert wide.next_offset == 1


def test_stale_hash_and_missing_identity_fail(ordinary_report) -> None:
    reader = _reader(ordinary_report)
    with pytest.raises(CodeBrowseBindingError):
        reader.read(_request(ordinary_report, report_sha256="0" * 64))
    with pytest.raises(CodeBrowseSelectionError):
        reader.read(_request(ordinary_report, identity="missing-identity"))


def test_nested_span_containment_and_equal_ambiguity(nested_report: AnalysisReport) -> None:
    reader = _reader(nested_report)
    path = nested_report.modules[0].path
    page = reader.read(_request(nested_report, path=path))
    by_id = {row.identity: row for row in page.records}
    module = nested_report.modules[0].name
    outer = f"{module}:Outer"
    method = f"{module}:Outer.method"
    inner = f"{module}:Outer.method.inner"
    assert by_id[method].parent_identity == outer
    assert by_id[method].parent_relation == "span-containment"
    assert by_id[inner].parent_identity == method
    duplicate = nested_report.model_copy(
        update={
            "modules": (
                nested_report.modules[0].model_copy(
                    update={
                        "symbols": (
                            *nested_report.modules[0].symbols,
                            Symbol(
                                qualified_name="Outer.method.shadow",
                                name="shadow",
                                kind="method",
                                span=nested_report.modules[0].symbols[1].span,
                                language=SourceLanguage.PYTHON,
                            ),
                        )
                    }
                ),
            )
        }
    )
    ambiguous = _reader(duplicate).read(_request(duplicate, path=path))
    by_id = {row.identity: row for row in ambiguous.records}
    assert by_id[inner].parent_identity is None


def test_structure_locations_and_missing_path_identity(ordinary_report) -> None:
    subject = next(item for item in ordinary_report.subjects if item.kind is SubjectKind.SYMBOL)
    span_a = subject.evidence[0].span
    extra_path = "extra/other.py"
    span_b = span_a.model_copy(update={"path": extra_path, "start_line": 4, "end_line": 4})
    evidence_b = subject.evidence[0].model_copy(update={"span": span_b})
    structure = ordinary_report.data_structures[0] if ordinary_report.data_structures else None
    if structure is None:
        from code_analysis.report_models import (
            DataStructureRecord,
            StructureScope,
            TypeExpression,
            TypeKind,
        )

        structure = DataStructureRecord(
            structure_id="structure-aaaaaaaaaaaaaaaaaaaa",
            owner_subject_id=subject.subject_id,
            scope=StructureScope.LOCAL,
            canonical_type=TypeExpression(kind=TypeKind.NAMED, name="dict"),
            evidence=subject.evidence,
            confidence=Confidence.EXACT,
        )
    patched = ordinary_report.model_copy(
        update={
            "source_files": (
                *ordinary_report.source_files,
                ordinary_report.source_files[0].model_copy(
                    update={
                        "source_file_id": "source-file-cccccccccccccccccccc",
                        "logical_path": extra_path,
                        "module_name": "extra.other",
                    }
                ),
            ),
            "data_structures": (
                structure.model_copy(update={"evidence": (*structure.evidence, evidence_b)}),
            ),
        }
    )
    reader = _reader(patched)
    first = reader.read(_request(patched, path=span_a.path))
    second = reader.read(_request(patched, path=extra_path))
    assert structure.structure_id in {row.identity for row in first.records}
    assert structure.structure_id in {row.identity for row in second.records}
    first_row = next(row for row in first.records if row.identity == structure.structure_id)
    assert all(span.path == span_a.path for span in first_row.spans)
    selected_first = reader.read(
        _request(patched, path=span_a.path, identity=structure.structure_id)
    )
    selected_second = reader.read(
        _request(patched, path=extra_path, identity=structure.structure_id)
    )
    assert selected_first.selected.logical_path == span_a.path
    assert selected_second.selected.logical_path == extra_path
    assert selected_second.selected.spans == (span_b,)
    chooser = reader.read(_request(patched, identity=structure.structure_id))
    assert chooser.selected is None and chooser.logical_path is None
    assert {entry.logical_path for entry in chooser.selection_locations} == {
        span_a.path,
        extra_path,
    }
    symbol_identity = f"{subject.module}:{subject.logical_name.split(':', 1)[-1]}"
    with pytest.raises(CodeBrowseSelectionError):
        reader.read(_request(patched, path=extra_path, identity=symbol_identity))
    with pytest.raises(CodeBrowseBindingError):
        reader.read(_request(patched, report_sha256=_digest(ordinary_report)))


def test_line_matches_keep_equal_alternatives_and_bound(tmp_path: Path) -> None:
    source = tmp_path / "lines.py"
    lines = ["def a():\n", "    x = 1\n", "    y = 2\n"]
    source.write_text("".join(lines))
    report = scan(discover_context(source, AnalysisConfig()))
    shutil.rmtree(tmp_path)
    reader = _reader(report)
    path = report.modules[0].path
    matches = reader.read(_request(report, path=path, line=2))
    assert matches.line_match_count >= 1
    assert matches.selected is not None
    many = report.model_copy(
        update={
            "modules": (
                report.modules[0].model_copy(
                    update={
                        "symbols": tuple(
                            Symbol(
                                qualified_name=f"s{i}",
                                name=f"s{i}",
                                kind="function",
                                span=SourceSpan(
                                    path=path,
                                    start_line=2,
                                    start_column=0,
                                    end_line=2,
                                    end_column=1,
                                    language=SourceLanguage.PYTHON,
                                ),
                                language=SourceLanguage.PYTHON,
                            )
                            for i in range(105)
                        )
                    }
                ),
            )
        }
    )
    bounded = _reader(many).read(_request(many, path=path, line=2))
    assert bounded.line_match_count == 105
    assert len(bounded.line_matches) == 100
    assert any("partial" in item for item in bounded.limitations)


def test_javascript_and_ruby_saved_records(ordinary_report) -> None:
    js_path = "ui/view.jsx"
    rb_path = "lib/parser.rb"
    js_source = ordinary_report.source_files[0].model_copy(
        update={
            "source_file_id": "source-file-aaaaaaaaaaaaaaaaaaaa",
            "logical_path": js_path,
            "language": SourceLanguage.JAVASCRIPT,
            "module_name": "ui.view",
        }
    )
    rb_source = ordinary_report.source_files[0].model_copy(
        update={
            "source_file_id": "source-file-bbbbbbbbbbbbbbbbbbbb",
            "logical_path": rb_path,
            "language": SourceLanguage.RUBY,
            "module_name": "lib.parser",
        }
    )
    js_module = ordinary_report.modules[0].model_copy(
        update={
            "name": "ui.view",
            "path": js_path,
            "language": SourceLanguage.JAVASCRIPT,
            "source_sha256": js_source.source_sha256,
            "symbols": (
                Symbol(
                    qualified_name="Component",
                    name="Component",
                    kind="class",
                    span=SourceSpan(
                        path=js_path,
                        start_line=1,
                        start_column=0,
                        end_line=3,
                        end_column=1,
                        language=SourceLanguage.JAVASCRIPT,
                    ),
                    language=SourceLanguage.JAVASCRIPT,
                ),
            ),
        }
    )
    rb_module = ordinary_report.modules[0].model_copy(
        update={
            "name": "lib.parser",
            "path": rb_path,
            "language": SourceLanguage.RUBY,
            "source_sha256": rb_source.source_sha256,
            "symbols": (
                Symbol(
                    qualified_name="Parser",
                    name="Parser",
                    kind="class",
                    span=SourceSpan(
                        path=rb_path,
                        start_line=1,
                        start_column=0,
                        end_line=2,
                        end_column=3,
                        language=SourceLanguage.RUBY,
                    ),
                    language=SourceLanguage.RUBY,
                ),
            ),
        }
    )
    patched = ordinary_report.model_copy(
        update={
            "source_files": (js_source, rb_source),
            "modules": (js_module, rb_module),
            "subjects": (),
        }
    )
    reader = _reader(patched)
    jsx = reader.read(_request(patched, path=js_path))
    ruby = reader.read(_request(patched, path=rb_path))
    assert jsx.records[0].language is SourceLanguage.JAVASCRIPT
    assert ruby.records[0].language is SourceLanguage.RUBY
    assert jsx.records[1].kind == "class"


def test_read_code_browse_convenience_matches_reader(ordinary_report) -> None:
    request = _request(ordinary_report, path=ordinary_report.modules[0].path)
    assert read_code_browse(ordinary_report, request) == _reader(ordinary_report).read(request)


def test_malformed_entry_language_rejected(ordinary_report) -> None:
    module = next(item for item in ordinary_report.modules if item.symbols)
    symbol = module.symbols[0]
    bad_span = symbol.span.model_copy(
        update={"language": SourceLanguage.JAVASCRIPT},
    )
    with pytest.raises(ValueError, match="symbol disagrees"):
        _reader(
            ordinary_report.model_copy(
                update={
                    "modules": (
                        module.model_copy(
                            update={"symbols": (symbol.model_copy(update={"span": bad_span}),)}
                        ),
                    )
                }
            )
        )


@pytest.mark.parametrize("value", (True, False, 42, None, [], {}))
def test_path_never_coerces_non_strings(ordinary_report, value):
    with pytest.raises(ValidationError):
        _request(ordinary_report, path=value)


def test_paging_retains_off_page_parent_identity(nested_report):
    reader = _reader(nested_report)
    path = nested_report.modules[0].path
    complete = reader.read(_request(nested_report, path=path))
    nested = next(entry for entry in complete.records if entry.label.endswith("inner"))
    offset = next(
        index for index, entry in enumerate(complete.records) if entry.identity == nested.identity
    )
    page = reader.read(_request(nested_report, path=path, offset=offset, limit=1))
    assert page.records[0].parent_identity == nested.parent_identity
    assert page.records[0].parent_relation == "span-containment"
    assert page.completeness == nested_report.completeness


def test_hash_binding_and_duplicate_declarations_fail_closed(ordinary_report):
    module = next(item for item in ordinary_report.modules if item.symbols)
    mismatched = ordinary_report.model_copy(
        update={"modules": (module.model_copy(update={"source_sha256": "0" * 64}),)}
    )
    with pytest.raises(ValueError, match="saved source identity"):
        _reader(mismatched)
    duplicated = ordinary_report.model_copy(
        update={
            "modules": (
                module.model_copy(update={"symbols": (*module.symbols, module.symbols[0])}),
            )
        }
    )
    with pytest.raises(ValueError, match="declaration identities"):
        _reader(duplicated)
    subject = ordinary_report.subjects[0]
    bad_evidence = subject.evidence[0].model_copy(update={"source_sha256": "0" * 64})
    malformed = ordinary_report.model_copy(
        update={"subjects": (subject.model_copy(update={"evidence": (bad_evidence,)}),)}
    )
    with pytest.raises(ValueError, match="evidence disagrees"):
        _reader(malformed)
