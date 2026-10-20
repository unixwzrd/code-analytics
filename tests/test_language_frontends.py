"""Peer adapters share normalized boundaries without eager optional parsers."""

import ast
import hashlib
import inspect
import subprocess
import sys
from dataclasses import asdict, fields, is_dataclass
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import BaseModel

from code_analysis.config import AnalysisConfig
from code_analysis.languages.base import FrontendContext, FrontendResult, SourceUnit
from code_analysis.languages.javascript import JavaScriptFrontend
from code_analysis.languages.python import PythonFrontend
from code_analysis.languages.registry import FRONTEND_REGISTRY, frontend_for
from code_analysis.languages.ruby import RubyPrismFrontend
from code_analysis.models import AvailabilityState, Completeness, SourceLanguage
from code_analysis.report_models import AdapterCapabilityKind


def _unit(language: SourceLanguage, source: str, extension: str) -> SourceUnit:
    data = source.encode()
    return SourceUnit(
        logical_path=f"sample.{extension}",
        module_name="sample",
        source=source,
        source_bytes=data,
        source_sha256=hashlib.sha256(data).hexdigest(),
        language=language,
    )


def _context() -> FrontendContext:
    config = AnalysisConfig()
    return FrontendContext(config, config.identity())


def _assert_normalized(value: object) -> None:
    assert not isinstance(value, ast.AST)
    module = type(value).__module__
    assert (
        not module.startswith("code_analysis.languages.")
        or module == "code_analysis.languages.base"
    )
    assert not type(value).__module__.startswith("tree_sitter")
    if is_dataclass(value) and not isinstance(value, type):
        for field in fields(value):
            _assert_normalized(getattr(value, field.name))
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            _assert_normalized(getattr(value, name))
    elif isinstance(value, (tuple, list)):
        for item in value:
            _assert_normalized(item)
    elif isinstance(value, dict):
        for item in value.values():
            _assert_normalized(item)


@pytest.mark.parametrize(
    "language,frontend_class,source,extension",
    [
        (SourceLanguage.PYTHON, PythonFrontend, "def work(value):\n    return value\n", "py"),
        (
            SourceLanguage.JAVASCRIPT,
            JavaScriptFrontend,
            "function work(value) { return value; }\n",
            "js",
        ),
        (SourceLanguage.RUBY, RubyPrismFrontend, "def work(value)\n  value\nend\n", "rb"),
    ],
)
def test_peer_frontends_share_registry_and_normalized_interface(
    language, frontend_class, source, extension
) -> None:
    assert set(FRONTEND_REGISTRY) == set(SourceLanguage)
    package_name = f"code_analysis.languages.{language.value}"
    package = import_module(package_name)
    assert package.__file__.endswith("/__init__.py")
    assert frontend_class.__module__ == f"{package_name}.frontend"
    assert getattr(package, frontend_class.__name__) is frontend_class
    frontend = frontend_for(language)
    assert type(frontend) is frontend_class
    assert frontend.source_language is language
    assert tuple(inspect.signature(frontend.analyze).parameters) == ("unit", "context")
    assert tuple(item.capability for item in frontend.capabilities) == tuple(AdapterCapabilityKind)
    context = _context()
    availability = frontend.availability(context)
    assert availability.provider_id == frontend.frontend_id
    if availability.state is not AvailabilityState.AVAILABLE:
        pytest.skip(f"optional parser unavailable: {availability}")
    result = frontend.analyze(_unit(language, source, extension), context)
    assert isinstance(result, FrontendResult)
    assert result.completeness is Completeness.COMPLETE
    assert result.provenance.frontend_id == frontend.frontend_id
    assert result.provenance.language is language
    assert result.symbols[0].qualified_name == "work"
    _assert_normalized(result)


def test_python_nested_generator_and_complexity_facts_are_preserved() -> None:
    source = (
        "from .helpers import ready\n\n"
        "def outer(value: int) -> int:\n"
        "    def nested():\n"
        "        yield value\n"
        "    if value:\n"
        "        return value\n"
        "    return 0\n\n"
        "async def stream(values):\n"
        "    for value in values:\n"
        "        yield value\n"
    )
    result = PythonFrontend().analyze(_unit(SourceLanguage.PYTHON, source, "py"), _context())
    outer, nested, stream = result.symbols
    assert [
        (item.qualified_name, item.kind, item.async_, item.generator) for item in result.symbols
    ] == [
        ("outer", "function", False, False),
        ("outer.nested", "nested_function", False, True),
        ("stream", "function", True, True),
    ]
    assert (outer.start_line, outer.end_line, outer.parameters, outer.returns) == (
        3,
        8,
        ("value",),
        "int",
    )
    assert asdict(outer.complexity) == {
        "physical_loc": 6,
        "logical_loc": 3,
        "branches": 1,
        "nesting_depth": 1,
        "argument_count": 1,
        "cyclomatic_complexity": 2,
        "weighted_score": 5,
        "returns": 2,
        "raises": 0,
    }
    assert nested.complexity.branches == 0
    assert stream.complexity.branches == 1
    assert [(item.specifier, item.kind, item.start_line) for item in result.imports] == [
        (".helpers", "from-import", 1)
    ]
    assert result.provenance.normalization_identity == "python-ast-inventory:v1"
    assert result.functions == result.states == result.exports == ()
    _assert_normalized(result)


def test_python_syntax_failure_is_explicit_and_normalized() -> None:
    result = PythonFrontend().analyze(
        _unit(SourceLanguage.PYTHON, "def broken(:\n", "py"), _context()
    )
    assert result.completeness is Completeness.FAILED
    assert result.symbols == result.imports == ()
    assert [item.code for item in result.diagnostics] == ["source-syntax-error"]
    assert result.diagnostics[0].path == "sample.py"
    assert result.diagnostics[0].provider == "python-ast"
    _assert_normalized(result)


def test_old_python_parser_locations_are_absent() -> None:
    package_root = Path(import_module("code_analysis").__file__).parent
    for relative in (
        "languages/python.py",
        "analysis/native_ast.py",
        "analysis/python_call_occurrences.py",
        "analysis/imports.py",
    ):
        assert not (package_root / relative).exists()


def test_fresh_python_only_scan_does_not_load_optional_parsers(tmp_path: Path) -> None:
    (tmp_path / "only.py").write_text("def work(value):\n    return value\n", encoding="utf-8")
    script = """
import importlib.abc
import sys

class NoOptionalParser(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'tree_sitter', 'tree_sitter_javascript'}:
            raise AssertionError('Python-only scan attempted optional parser: ' + fullname)

sys.meta_path.insert(0, NoOptionalParser())
from code_analysis.config import AnalysisConfig, LanguageSelection
from code_analysis.project import discover_context
from code_analysis.service import scan
from code_analysis.models import Completeness, SourceLanguage
from pathlib import Path
config = AnalysisConfig(languages=(LanguageSelection.PYTHON,))
report = scan(discover_context(Path(sys.argv[1]), config))
assert report.completeness is Completeness.COMPLETE
assert report.target.languages == (SourceLanguage.PYTHON,)
optional = {'tree_sitter', 'tree_sitter_javascript'}
assert not any(name.split('.')[0] in optional for name in sys.modules)
"""
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], check=True, capture_output=True, text=True
    )
