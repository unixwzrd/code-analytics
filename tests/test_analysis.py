from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import FIXTURES

from code_analysis.config import AnalysisConfig, Strictness, load_config_file
from code_analysis.errors import ConfigurationError
from code_analysis.models import Completeness, ProviderState
from code_analysis.project import discover_context, iter_selected_source_files
from code_analysis.service import scan


def test_external_config_source_roots_override_project_discovery(tmp_path: Path) -> None:
    for name in ("src", "tests", "ignored"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "sample.py").write_text("value = 1\n")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["ignored"]\n'
    )
    config_path = tmp_path / "analysis.toml"
    config_path.write_text('[tool.code-analysis]\nsource-roots = ["src", "tests"]\n')
    config = load_config_file(config_path)
    context = discover_context(tmp_path, config)
    assert context.source.logical_source_roots == ("src", "tests")
    assert {
        path.relative_to(tmp_path).as_posix() for path in iter_selected_source_files(context)
    } == {"src/sample.py", "tests/sample.py"}


@pytest.mark.parametrize("roots", [("missing",), ("../outside",), ("",), ("src/",), ("src", "src")])
def test_invalid_explicit_source_roots_fail_closed(tmp_path: Path, roots) -> None:
    (tmp_path / "src").mkdir()
    with pytest.raises(ConfigurationError):
        discover_context(tmp_path, AnalysisConfig(source_roots=roots))


def test_explicit_source_root_rejects_symlink_component(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "nested").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(ConfigurationError, match="symlinks"):
        discover_context(tmp_path, AnalysisConfig(source_roots=("alias/nested",)))


def test_ordinary_package_inventory(ordinary_report) -> None:
    assert [module.name for module in ordinary_report.modules] == ["alpha", "alpha.a", "alpha.b"]
    module = next(item for item in ordinary_report.modules if item.name == "alpha.a")
    assert [symbol.qualified_name for symbol in module.symbols] == [
        "Outer",
        "Outer.Inner",
        "Outer.method",
        "Outer.method.nested",
        "calculate",
    ]
    assert ordinary_report.summary.classes == 2
    assert ordinary_report.summary.functions == 3
    assert ordinary_report.summary.methods == 1


def test_nested_complexity_is_not_charged_to_parent(ordinary_report) -> None:
    module = next(item for item in ordinary_report.modules if item.name == "alpha.a")
    method = next(item for item in module.symbols if item.qualified_name == "Outer.method")
    nested = next(item for item in module.symbols if item.qualified_name.endswith("nested"))
    assert method.complexity is not None and method.complexity.branches == 0
    assert nested.complexity is not None and nested.complexity.branches == 1


def test_src_layout_discovery() -> None:
    report = scan(discover_context(FIXTURES / "src_layout", AnalysisConfig()))
    assert report.target.source_roots == ("src",)
    assert [module.name for module in report.modules] == ["widget", "widget.core", "widget.helper"]
    assert all(module.path.startswith("src/") for module in report.modules)


def test_namespace_package_discovery() -> None:
    report = scan(discover_context(FIXTURES / "namespace", AnalysisConfig()))
    assert [module.name for module in report.modules] == ["acme.tools.worker"]
    assert report.modules[0].path == "src/acme/tools/worker.py"


def test_syntax_error_is_partial_not_empty_success() -> None:
    report = scan(discover_context(FIXTURES / "malformed", AnalysisConfig()))
    assert report.completeness is Completeness.PARTIAL
    assert report.summary.discovered_files == 2
    assert report.summary.parsed_files == 1
    assert report.summary.failed_files == 1
    assert any(item.code == "source-syntax-error" for item in report.diagnostics)
    assert report.providers[0].state.value == "partial"


def test_excluded_directory_is_not_scanned() -> None:
    report = scan(discover_context(FIXTURES / "excluded", AnalysisConfig()))
    assert report.summary.discovered_files == 1
    assert report.modules[0].path == "visible.py"


def test_unreadable_source_is_partial(monkeypatch) -> None:
    context = discover_context(FIXTURES / "excluded", AnalysisConfig())
    original = Path.read_bytes

    def unreadable(path: Path, *args, **kwargs):
        if path.name == "visible.py":
            raise PermissionError("synthetic unreadable file")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    report = scan(context)
    assert report.completeness is Completeness.PARTIAL
    assert report.summary.failed_files == 1
    assert any(item.code == "source-read-error" for item in report.diagnostics)
    assert any(item.code == "interface-source-read-error" for item in report.diagnostics)
    assert next(
        item for item in report.providers if item.provider_id == "native-interfaces"
    ).state is (ProviderState.PARTIAL)


@pytest.mark.parametrize("newline", (b"\n", b"\r\n", b"\r"))
def test_python_scan_hashes_original_bytes_across_sections(tmp_path, newline):
    source = (
        b"class Record:\n    name: str\n\n"
        b"def adapt(record: dict[str, str]):\n    return record.get('name', '')\n"
    )
    payload = source.replace(b"\n", newline)
    target = tmp_path / "sample.py"
    target.write_bytes(payload)
    report = scan(discover_context(target, AnalysisConfig(test_roots=())))
    expected = hashlib.sha256(payload).hexdigest()
    assert report.source_files[0].source_sha256 == expected
    assert report.modules[0].source_sha256 == expected
    assert report.modules[0].symbols
    assert not any(item.code == "interface-source-changed" for item in report.diagnostics)
    assert not any(item.code == "contract-source-read-error" for item in report.diagnostics)


def test_newline_only_changes_are_not_stable_source(tmp_path):
    from code_analysis.clone_reporting import collect_clone_sources
    from code_analysis.dictionary_reporting import integrate_dictionary_report

    target = tmp_path / "sample.py"
    payload = b"def calculate(value):\n    return value + 1\n"
    target.write_bytes(payload)
    context = discover_context(target, AnalysisConfig(test_roots=()))
    report = scan(context)
    target.write_bytes(payload.replace(b"\n", b"\r\n"))
    with pytest.raises(OSError, match="source changed during clone analysis"):
        collect_clone_sources(context, report)
    with pytest.raises(OSError, match="source changed during dictionary analysis"):
        integrate_dictionary_report(context, report)


def test_crlf_contract_declarations_bind_original_source_bytes(tmp_path):
    from code_analysis.service import scan_contract_inventory

    payload = (
        b"from typing import TypedDict\r\n"
        b"class Record(TypedDict):\r\n    name: str\r\n    count: int\r\n"
    )
    target = tmp_path / "sample.py"
    target.write_bytes(payload)
    report = scan_contract_inventory(discover_context(target, AnalysisConfig(test_roots=())))
    expected = hashlib.sha256(payload).hexdigest()
    assert len(report.contracts) == 1 and report.modules[0].source_sha256 == expected
    assert all(item.source_sha256 == expected for item in report.contracts[0].evidence)
    assert not any(item.code == "contract-source-read-error" for item in report.diagnostics)


def test_relative_imports_are_local_and_deduplicated(ordinary_report) -> None:
    pairs = [(edge.source, edge.target) for edge in ordinary_report.import_edges]
    assert pairs == [("alpha.a", "alpha.b"), ("alpha.b", "alpha.a")]
    assert ordinary_report.summary.import_edges == 2


def test_strictness_and_symbol_filter_control_findings() -> None:
    config = AnalysisConfig.with_overrides(
        strictness=Strictness.STRICT,
        branches=0,
        weighted_score=999,
        function_loc=999,
        arguments=999,
        nesting=999,
        symbol_filter="calculate",
    )
    report = scan(discover_context(FIXTURES / "ordinary", config))
    assert [
        finding.symbol for finding in report.findings if finding.category != "import-cycle"
    ] == ["calculate"]
