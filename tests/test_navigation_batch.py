from __future__ import annotations

from pathlib import Path

import pytest

from code_analysis.config import AnalysisConfig
from code_analysis.models import SourceSpan
from code_analysis.project import discover_context
from code_analysis.service import scan
from code_analysis.ui_navigation import (
    NavigationItem,
    NavigationUnavailable,
    build_navigation_page,
    navigation_items,
    navigation_page,
    ordered_navigation_items,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _item(identity, label, kind, span):
    return NavigationItem(identity=identity, label=label, kind=kind, span=span)


def _expected(report):
    expected = {}
    for module in report.modules:
        expected.setdefault(
            module.name,
            tuple(
                _item(
                    f"{module.name}:{symbol.qualified_name}",
                    symbol.qualified_name,
                    symbol.kind,
                    symbol.span,
                )
                for symbol in module.symbols
            )
            or (
                _item(
                    module.name,
                    module.name,
                    "module",
                    SourceSpan(
                        path=module.path,
                        start_line=1,
                        start_column=0,
                        end_line=max(1, module.line_count),
                        end_column=0,
                        language=module.language,
                    ),
                ),
            ),
        )
        for symbol in module.symbols:
            identity = f"{module.name}:{symbol.qualified_name}"
            expected.setdefault(
                identity, (_item(identity, symbol.qualified_name, symbol.kind, symbol.span),)
            )
    direct = (
        (
            report.subjects,
            "subject_id",
            "logical_name",
            lambda item: item.kind.value,
            "evidence",
            lambda item: item.subject_id,
        ),
        (
            report.state_resources,
            "state_resource_id",
            "logical_name",
            lambda item: item.kind.value,
            "declaration_evidence",
            lambda item: item.owner_subject_id,
        ),
        (
            report.side_effects,
            "side_effect_id",
            "target_identity",
            lambda item: item.kind.value,
            "evidence",
            lambda item: item.subject_id,
        ),
    )
    for records, id_field, label_field, kind, evidence_field, item_identity in direct:
        for record in records:
            identity = getattr(record, id_field)
            expected.setdefault(
                identity,
                tuple(
                    _item(
                        item_identity(record),
                        getattr(record, label_field),
                        kind(record),
                        evidence.span,
                    )
                    for evidence in getattr(record, evidence_field)
                ),
            )

    def evidence(identity, label, kind, records):
        return tuple(
            _item(identity, label, kind, span)
            for span in dict.fromkeys(item.span for item in records)
        )

    for record in report.interface_endpoints:
        expected.setdefault(
            record.endpoint_id,
            evidence(
                record.endpoint_id, record.logical_name, "interface-endpoint", record.evidence
            ),
        )
    for record in report.contracts:
        expected.setdefault(
            record.contract_id,
            evidence(record.contract_id, record.contract_id, "contract-evidence", record.evidence),
        )
    for record in report.dictionary_shapes:
        expected.setdefault(
            record.shape_id,
            evidence(
                record.shape_id, record.shape_id, "dictionary-shape-evidence", record.evidence
            ),
        )
        for state in record.lifecycle_states:
            expected.setdefault(
                state.state_id,
                evidence(state.state_id, state.name, "observed-state", state.evidence),
            )
    for record in report.data_structures:
        expected.setdefault(
            record.structure_id,
            evidence(
                record.structure_id, record.structure_id, "data-structure-evidence", record.evidence
            ),
        )
    for record in report.test_cases:
        expected.setdefault(
            record.test_id, evidence(record.test_id, record.qualified_name, "test", record.evidence)
        )
    for record in report.test_fixtures:
        expected.setdefault(
            record.fixture_id,
            evidence(record.fixture_id, record.qualified_name, "fixture", record.evidence),
        )
    for module in report.test_modules:
        records = tuple(
            evidence_item
            for collection in (report.test_cases, report.test_fixtures, report.test_relationships)
            for record in collection
            if getattr(record, "test_module_id", getattr(record, "source_test_module_id", None))
            == module.test_module_id
            for evidence_item in record.evidence
        )
        expected.setdefault(
            module.test_module_id,
            evidence(module.test_module_id, module.module, "test-module-evidence", records),
        )
    return expected


@pytest.mark.parametrize(
    "fixture",
    (
        "ordinary",
        "state_effect_project",
        "interface_project",
        "contracts_project",
        "dictionary_project",
        "test_impact_project",
    ),
)
def test_batch_navigation_matches_independent_saved_report_rules(fixture) -> None:
    report = scan(discover_context(FIXTURES / fixture, AnalysisConfig()))
    expected = _expected(report)
    requested = frozenset((*expected, "unknown-navigation-id"))
    actual = navigation_items(report, requested)
    assert actual == expected
    assert "unknown-navigation-id" not in actual
    for identity, items in expected.items():
        assert navigation_page(report, identity) == build_navigation_page(
            identity, items, report.completeness
        )
        for offset in (0, 25, len(items) + 1):
            page = build_navigation_page(identity, items, report.completeness, offset)
            assert page.records == ordered_navigation_items(items)[offset : offset + 25]


def test_batch_navigation_is_subset_only_source_free_and_validates_offsets(tmp_path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("def second():\n    return 2\n\ndef first():\n    return 1\n")
    report = scan(discover_context(tmp_path, AnalysisConfig(test_roots=())))
    source.unlink()
    module = report.modules[0]
    symbol_id = f"{module.name}:{module.symbols[0].qualified_name}"
    assert set(navigation_items(report, frozenset((symbol_id, "missing")))) == {symbol_id}
    with pytest.raises(NavigationUnavailable):
        navigation_page(report, "missing")
    for offset in (-1, True):
        with pytest.raises(ValueError, match="nonnegative integer"):
            build_navigation_page(module.name, (), report.completeness, offset)
        with pytest.raises(ValueError, match="nonnegative integer"):
            navigation_page(report, "missing", offset)


def test_test_module_evidence_collections_are_each_scanned_once() -> None:
    report = scan(discover_context(FIXTURES / "test_impact_project", AnalysisConfig()))

    class CountedTuple(tuple):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    counted = {
        name: CountedTuple(getattr(report, name))
        for name in (
            "test_modules",
            "test_cases",
            "test_fixtures",
            "test_relationships",
        )
    }
    instrumented = type(report).model_construct(**(report.__dict__ | counted))
    identities = frozenset(module.test_module_id for module in report.test_modules)
    assert navigation_items(instrumented, identities)
    assert {name: records.iterations for name, records in counted.items()} == {
        "test_modules": 1,
        "test_cases": 1,
        "test_fixtures": 1,
        "test_relationships": 1,
    }


@pytest.mark.parametrize("selection", ("module", "symbol"))
def test_singleton_module_navigation_skips_unrelated_collections(
    ordinary_report, selection
) -> None:
    class CountedTuple(tuple):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    names = (
        "subjects",
        "state_resources",
        "side_effects",
        "interface_endpoints",
        "contracts",
        "dictionary_shapes",
        "data_structures",
        "test_cases",
        "test_fixtures",
        "test_relationships",
        "test_modules",
    )
    counted = {name: CountedTuple(getattr(ordinary_report, name)) for name in names}
    instrumented = type(ordinary_report).model_construct(**(ordinary_report.__dict__ | counted))
    module = next(module for module in ordinary_report.modules if module.symbols)
    identity = (
        module.name
        if selection == "module"
        else f"{module.name}:{module.symbols[0].qualified_name}"
    )
    assert navigation_items(instrumented, frozenset((identity,)))
    assert all(records.iterations == 0 for records in counted.values())
    assert navigation_items(instrumented, frozenset()) == {}


def test_navigation_sort_key_and_page_boundary_are_explicit(ordinary_report) -> None:
    prototype = next(symbol.span for module in ordinary_report.modules for symbol in module.symbols)

    def item(identity, path, start_line, start_column, end_line, end_column):
        return NavigationItem(
            identity=identity,
            label=identity,
            kind="test",
            span=prototype.model_copy(
                update={
                    "path": path,
                    "start_line": start_line,
                    "start_column": start_column,
                    "end_line": end_line,
                    "end_column": end_column,
                }
            ),
        )

    items = (
        item("path-b", "b.py", 1, 0, 1, 1),
        *(item(f"fill-{line:02}", "a.py", line, 0, line, 1) for line in range(3, 24)),
        item("tie-z", "a.py", 1, 1, 1, 3),
        item("end-line", "a.py", 1, 1, 2, 0),
        item("tie-a", "a.py", 1, 1, 1, 3),
        item("end-column", "a.py", 1, 1, 1, 2),
        item("start-column", "a.py", 1, 0, 1, 1),
    )
    expected = (
        "start-column",
        "end-column",
        "tie-a",
        "tie-z",
        "end-line",
        *(f"fill-{line:02}" for line in range(3, 24)),
        "path-b",
    )
    assert tuple(record.identity for record in ordered_navigation_items(items)) == expected
    first = build_navigation_page("selection", items, ordinary_report.completeness)
    assert tuple(record.identity for record in first.records) == expected[:25]
    assert first.eligible == 27
    assert first.next_offset == 25
    last = build_navigation_page("selection", items, ordinary_report.completeness, 25)
    assert tuple(record.identity for record in last.records) == ("fill-23", "path-b")
    assert last.next_offset is None
