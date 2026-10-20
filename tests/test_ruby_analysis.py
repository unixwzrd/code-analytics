from __future__ import annotations

import builtins
import json
from pathlib import Path

from code_analysis.cli import ExitCode, main
from code_analysis.config import AnalysisConfig, LanguageSelection, load_project_config
from code_analysis.languages import frontend_for
from code_analysis.models import Completeness, SourceLanguage
from code_analysis.project import discover_context
from code_analysis.reports.json_report import load_json, render_json
from code_analysis.service import scan

FIXTURE = Path(__file__).parent / "fixtures" / "ruby_project"


def test_ruby_prism_inventory_is_static_deterministic_and_portable() -> None:
    config = load_project_config(FIXTURE)
    first = scan(discover_context(FIXTURE, config))
    second = scan(discover_context(FIXTURE, config))
    assert first.completeness is Completeness.COMPLETE
    assert first.target.languages == (SourceLanguage.RUBY,)
    assert {item.name for item in first.modules} == {"a.rb", "b.rb"}
    assert {item.kind for module in first.modules for item in module.symbols} >= {
        "module",
        "class",
        "method",
    }
    assert {(item.source, item.target) for item in first.import_edges} == {("b.rb", "a.rb")}
    assert first.function_profiles
    assert render_json(first) == render_json(second)
    assert load_json(render_json(first)) == first
    for forbidden in ("/Users/", "/Volumes/", "/tmp/", "/private/var/"):
        assert forbidden not in render_json(first)


def test_ruby_cli_and_focused_language_rejection(capsys) -> None:
    result = main(
        [
            "scan",
            str(FIXTURE),
            "--language",
            "ruby",
            "--json-compact",
            "--no-workspace",
        ]
    )
    captured = capsys.readouterr()
    assert result == ExitCode.SUCCESS
    assert json.loads(captured.out)["target"]["languages"] == ["ruby"]
    assert captured.err == ""
    result = main(["contracts", str(FIXTURE), "--language", "ruby", "--no-workspace"])
    captured = capsys.readouterr()
    assert result == ExitCode.EINVAL
    assert "support only auto or python" in captured.err


def test_python_only_scan_does_not_probe_ruby(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "only.py").write_text("value = 1\n", encoding="utf-8")
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith("code_analysis.languages.ruby"):
            raise AssertionError("Python-only scan imported the Ruby adapter")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    report = scan(
        discover_context(
            tmp_path,
            AnalysisConfig(languages=(LanguageSelection.PYTHON,)),
        )
    )
    assert report.target.languages == (SourceLanguage.PYTHON,)


def test_ruby_frontend_capabilities_are_exhaustive() -> None:
    frontend = frontend_for(SourceLanguage.RUBY)
    assert frontend.frontend_id == "ruby-prism"
    assert len(frontend.capabilities) == 14
    assert frontend.capabilities[-1].capability.value == "coverage-binding"


def test_ruby_malformed_source_is_partial(tmp_path: Path) -> None:
    source = tmp_path / "broken.rb"
    source.write_text("def broken(\n", encoding="utf-8")
    report = scan(
        discover_context(
            source,
            AnalysisConfig(languages=(LanguageSelection.RUBY,)),
        )
    )
    assert report.completeness is Completeness.PARTIAL
    assert any(item.code == "ruby-syntax-error" for item in report.diagnostics)
