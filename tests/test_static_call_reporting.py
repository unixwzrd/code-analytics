from __future__ import annotations

from pathlib import Path

import pytest

from code_analysis.config import AnalysisConfig, LanguageSelection
from code_analysis.context import AnalysisContext
from code_analysis.languages.python.call_occurrences import static_call_batch_from_source
from code_analysis.models import SourceLanguage
from code_analysis.project import discover_context
from code_analysis.report_models import AnalysisReport, SectionState
from code_analysis.reports.json_report import canonical_json, load_json, render_json
from code_analysis.service import scan, scan_contract_inventory, scan_dictionary_shapes
from code_analysis.static_call_normalization import (
    PythonStaticCallProviderRecord,
    StaticCallProviderRecords,
)
from code_analysis.static_call_reporting import integrate_static_call_report
from code_analysis.static_call_validation import validate_static_call_occurrences

FIXTURES = Path(__file__).parent / "fixtures"


def _context(root: Path, **config: object) -> AnalysisContext:
    return discover_context(root, AnalysisConfig(**config))


def test_scan_enables_static_call_occurrences_for_python() -> None:
    report = scan(_context(FIXTURES / "ordinary", languages=(LanguageSelection.PYTHON,)))
    section = next(
        item for item in report.section_coverage if item.section == "static-call-occurrences"
    )
    assert section.state is SectionState.COMPLETE
    assert section.languages == (SourceLanguage.PYTHON,)
    assert report.summary.static_call_occurrences > 0
    assert report.summary.static_calls_observed >= report.summary.static_call_occurrences
    validate_static_call_occurrences(report)
    AnalysisReport.model_validate(report.model_dump())


def test_focused_contract_and_dict_commands_leave_static_calls_disabled() -> None:
    context = _context(FIXTURES / "ordinary", languages=(LanguageSelection.PYTHON,))
    contract = scan_contract_inventory(context)
    shapes = scan_dictionary_shapes(context)
    for report in (contract, shapes):
        section = next(
            item for item in report.section_coverage if item.section == "static-call-occurrences"
        )
        assert section.state is SectionState.DISABLED
        assert not report.static_call_occurrences
        assert not report.static_call_coverage


def test_zero_call_source_is_complete(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    module = root / "empty.py"
    module.write_text("x = 1\n", encoding="utf-8")
    report = scan(_context(root, languages=(LanguageSelection.PYTHON,)))
    coverage = report.static_call_coverage[0]
    assert coverage.observed_count == 0
    assert coverage.retained_count == 0
    assert coverage.state.value == "complete"
    validate_static_call_occurrences(report)


def test_global_cap_truncates_across_files(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("f()\n", encoding="utf-8")
    (root / "b.py").write_text("g()\n", encoding="utf-8")
    report = scan(
        _context(
            root,
            languages=(LanguageSelection.PYTHON,),
            max_static_call_occurrences=1,
        )
    )
    assert report.summary.static_calls_observed == 2
    assert report.summary.static_call_occurrences == 1
    assert report.summary.static_calls_omitted == 1
    section = next(
        item for item in report.section_coverage if item.section == "static-call-occurrences"
    )
    assert section.state is SectionState.TRUNCATED
    validate_static_call_occurrences(report)


def test_source_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    from code_analysis.report_models import SourceFileRecord
    from code_analysis.static_call_reporting import _collect_source, _SubjectSpanIndex

    root = tmp_path / "project"
    root.mkdir()
    module = root / "sample.py"
    module.write_text("f()\n", encoding="utf-8")
    context = _context(root, languages=(LanguageSelection.PYTHON,))
    base = scan_contract_inventory(context)
    source = SourceFileRecord.model_validate(
        {
            **base.source_files[0].model_dump(),
            "source_sha256": "a" * 64,
        }
    )
    result = _collect_source(
        context,
        source,
        subject_index=_SubjectSpanIndex.empty(),
        analysis_identity="b" * 64,
        max_occurrences=100,
        retain_occurrences=True,
        provider_records=StaticCallProviderRecords(
            python=PythonStaticCallProviderRecord(
                frontend_id="python-ast",
                parser="ast",
                python_version="0.0.0",
            )
        ),
        ruby_runtime=None,
    )
    assert result.coverage.state.value == "failed"
    assert result.coverage.retained_count == 0
    assert any(item.code == "static-call-source-hash-mismatch" for item in result.diagnostics)


def test_malformed_python_source_marks_failed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    module = root / "broken.py"
    module.write_text("def (\n", encoding="utf-8")
    report = scan(_context(root, languages=(LanguageSelection.PYTHON,)))
    coverage = report.static_call_coverage[0]
    assert coverage.state.value == "failed"
    assert coverage.retained_count == 0


def test_json_round_trip_is_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "calls.py").write_text("def run():\n    helper()\n", encoding="utf-8")
    report = scan(_context(root, languages=(LanguageSelection.PYTHON,)))
    first = canonical_json(load_json(render_json(report, compact=True)))
    second = canonical_json(load_json(render_json(report, compact=True)))
    assert first == second


def test_javascript_fixture_when_parser_available() -> None:
    js_root = FIXTURES / "javascript"
    if not js_root.exists():
        pytest.skip("javascript fixture missing")
    try:
        import tree_sitter_javascript  # noqa: F401
    except ImportError:
        pytest.skip("tree-sitter javascript extra not installed")
    report = scan(_context(js_root, languages=(LanguageSelection.JAVASCRIPT,)))
    section = next(
        item for item in report.section_coverage if item.section == "static-call-occurrences"
    )
    assert section.languages == (SourceLanguage.JAVASCRIPT,)
    assert report.summary.static_calls_observed > 0
    validate_static_call_occurrences(report)


def test_ruby_fixture_when_prism_available() -> None:
    ruby_root = FIXTURES / "ruby_project"
    if not ruby_root.exists():
        pytest.skip("ruby fixture missing")
    report = scan(
        _context(
            ruby_root,
            languages=(LanguageSelection.PYTHON, LanguageSelection.RUBY),
        )
    )
    ruby_sources = [item for item in report.source_files if item.language is SourceLanguage.RUBY]
    if not ruby_sources:
        pytest.skip("no ruby sources discovered")
    section = next(
        item for item in report.section_coverage if item.section == "static-call-occurrences"
    )
    assert SourceLanguage.RUBY in section.languages
    validate_static_call_occurrences(report)


def test_function_profiles_unchanged_by_static_call_integration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "mod.py").write_text("def run():\n    helper()\n\ndef helper():\n    return 1\n")
    context = _context(root, languages=(LanguageSelection.PYTHON,))
    monkeypatch.setattr(
        "code_analysis.static_call_reporting.integrate_static_call_report",
        lambda _context, report: report,
    )
    without_static_calls = scan(context)
    monkeypatch.undo()
    with_static_calls = scan(context)
    assert len(with_static_calls.function_profiles) == len(without_static_calls.function_profiles)
    assert {item.profile_id for item in with_static_calls.function_profiles} == {
        item.profile_id for item in without_static_calls.function_profiles
    }


def test_midscan_hash_change_marks_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    module = root / "flip.py"
    module.write_text("f()\n", encoding="utf-8")
    context = _context(root, languages=(LanguageSelection.PYTHON,))
    base = scan_contract_inventory(context)

    def mutating_batch(source: str, *, max_occurrences: int):
        module.write_text("g()\n", encoding="utf-8")
        return static_call_batch_from_source(source, max_occurrences=max_occurrences)

    monkeypatch.setattr(
        "code_analysis.static_call_reporting.static_call_batch_from_source",
        mutating_batch,
    )
    report = integrate_static_call_report(context, base)
    coverage = report.static_call_coverage[0]
    assert coverage.state.value == "failed"
    assert any(item.code == "static-call-source-hash-changed" for item in report.diagnostics)


def test_javascript_unavailable_marks_source_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from code_analysis.languages.javascript.call_occurrences import (
        JavascriptStaticCallProviderIdentity,
    )

    root = tmp_path / "project"
    root.mkdir()
    (root / "app.js").write_text("f();\n", encoding="utf-8")
    context = _context(root, languages=(LanguageSelection.JAVASCRIPT,))
    monkeypatch.setattr(
        "code_analysis.static_call_reporting.javascript_static_call_provider_identity",
        lambda: JavascriptStaticCallProviderIdentity(False, None, None, None),
    )
    report = scan(context)
    if not report.static_call_coverage:
        pytest.fail("expected javascript static call coverage row")
    coverage = report.static_call_coverage[0]
    assert coverage.state.value == "unavailable"
    assert not report.static_call_occurrences


def test_ruby_unavailable_marks_source_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from code_analysis.models import AvailabilityState, ProviderAvailability

    root = tmp_path / "project"
    root.mkdir()
    (root / "app.rb").write_text("f()\n", encoding="utf-8")
    context = _context(root, languages=(LanguageSelection.RUBY,))

    def unavailable(_self, _ctx):
        return ProviderAvailability(
            provider_id="ruby-prism",
            state=AvailabilityState.UNAVAILABLE,
            detail="Ruby executable was not found",
        )

    monkeypatch.setattr(
        "code_analysis.static_call_reporting.frontend_for",
        lambda _language: type(
            "StubFrontend",
            (),
            {"frontend_id": "ruby-prism", "availability": unavailable},
        )(),
    )
    report = scan(context)
    if not report.static_call_coverage:
        pytest.fail("expected ruby static call coverage row")
    coverage = report.static_call_coverage[0]
    assert coverage.state.value == "unavailable"
    assert not report.static_call_occurrences


def test_empty_source_reread_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    module = root / "empty.py"
    module.write_text("", encoding="utf-8")
    context = _context(root, languages=(LanguageSelection.PYTHON,))
    base = scan_contract_inventory(context)
    reads = 0

    def alternating_read(root_path, logical_path):
        nonlocal reads
        reads += 1
        if reads == 1:
            from code_analysis.static_call_reporting import _read_source_bytes as original

            return original(root_path, logical_path)
        from code_analysis.static_call_reporting import _ReadResult

        return _ReadResult(b"", "static-call-source-unavailable", "source became unavailable")

    monkeypatch.setattr("code_analysis.static_call_reporting._read_source_bytes", alternating_read)
    report = integrate_static_call_report(context, base)
    coverage = report.static_call_coverage[0]
    assert coverage.state.value == "failed"
    assert any(item.code == "static-call-source-unavailable" for item in report.diagnostics)


def test_source_record_validation_isolated(tmp_path: Path) -> None:
    from code_analysis.report_models import SourceFileRecord
    from code_analysis.static_call_normalization import (
        PythonStaticCallProviderRecord,
        StaticCallProviderRecords,
    )
    from code_analysis.static_call_reporting import _collect_source, _SubjectSpanIndex

    root = tmp_path / "project"
    root.mkdir()
    (root / "sample.py").write_text("f()\n", encoding="utf-8")
    context = _context(root, languages=(LanguageSelection.PYTHON,))
    base = scan_contract_inventory(context)
    payload = base.source_files[0].model_dump()
    payload["nonblank_loc"] = payload["physical_loc"] + 1
    source = SourceFileRecord.model_construct(**payload)
    result = _collect_source(
        context,
        source,
        subject_index=_SubjectSpanIndex.empty(),
        analysis_identity="b" * 64,
        max_occurrences=10,
        retain_occurrences=True,
        provider_records=StaticCallProviderRecords(
            python=PythonStaticCallProviderRecord(
                frontend_id="python-ast",
                parser="ast",
                python_version="0.0.0",
            )
        ),
        ruby_runtime=None,
    )
    assert result.coverage.state.value == "failed"
    assert any(item.code == "static-call-source-record-invalid" for item in result.diagnostics)


def test_integrate_preserves_prior_inventory_fields(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "mod.py").write_text("def run():\n    helper()\n", encoding="utf-8")
    context = _context(root, languages=(LanguageSelection.PYTHON,))
    base = scan_contract_inventory(context)
    integrated = integrate_static_call_report(context, base)
    assert integrated.function_profiles == base.function_profiles
    assert integrated.subjects == base.subjects
    assert integrated.modules == base.modules


def test_two_integrations_produce_identical_static_bytes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "calls.py").write_text("f()\ng()\n", encoding="utf-8")
    context = _context(root, languages=(LanguageSelection.PYTHON,), max_static_call_occurrences=5)
    base = scan_contract_inventory(context)
    first = integrate_static_call_report(context, base)
    second = integrate_static_call_report(context, base)
    first_payload = {
        "coverage": [item.model_dump(mode="json") for item in first.static_call_coverage],
        "occurrences": [item.model_dump(mode="json") for item in first.static_call_occurrences],
    }
    second_payload = {
        "coverage": [item.model_dump(mode="json") for item in second.static_call_coverage],
        "occurrences": [item.model_dump(mode="json") for item in second.static_call_occurrences],
    }
    assert first_payload == second_payload


def test_portable_analysis_identity_ignores_ruby_executable_relocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from code_analysis import static_call_reporting as reporting
    from code_analysis.models import AvailabilityState, ProviderAvailability
    from code_analysis.static_call_normalization import compute_analysis_identity_sha256

    root = tmp_path / "project"
    root.mkdir()
    (root / "app.rb").write_text("f()\n", encoding="utf-8")
    context = _context(root, languages=(LanguageSelection.RUBY,))
    base = scan(context)
    if not any(item.language is SourceLanguage.RUBY for item in base.source_files):
        pytest.skip("ruby source missing")

    def first_availability(_self, _context):
        return ProviderAvailability(
            provider_id="ruby-prism",
            state=AvailabilityState.AVAILABLE,
            executable="/opt/ruby-a/bin/ruby",
            version="ruby 3.3.0",
            detail="Prism 1.0.0",
        )

    def second_availability(_self, _context):
        return ProviderAvailability(
            provider_id="ruby-prism",
            state=AvailabilityState.AVAILABLE,
            executable="/opt/ruby-b/bin/ruby",
            version="ruby 3.3.0",
            detail="Prism 1.0.0",
        )

    monkeypatch.setattr(
        "code_analysis.static_call_reporting.frontend_for",
        lambda _language: type(
            "StubFrontend",
            (),
            {
                "frontend_id": "ruby-prism",
                "availability": first_availability,
            },
        )(),
    )
    records_one, _ = reporting._resolve_provider_records(context, (SourceLanguage.RUBY,))
    monkeypatch.setattr(
        "code_analysis.static_call_reporting.frontend_for",
        lambda _language: type(
            "StubFrontend",
            (),
            {
                "frontend_id": "ruby-prism",
                "availability": second_availability,
            },
        )(),
    )
    records_two, _ = reporting._resolve_provider_records(context, (SourceLanguage.RUBY,))
    identity_one = compute_analysis_identity_sha256(
        tool_version=base.tool_version,
        configuration_digest_sha256=context.config.identity(),
        providers=records_one,
    )
    identity_two = compute_analysis_identity_sha256(
        tool_version=base.tool_version,
        configuration_digest_sha256=context.config.identity(),
        providers=records_two,
    )
    assert identity_one == identity_two
