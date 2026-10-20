"""Standard-library-only smoke of the installed package in fresh processes.

CI runs this file in a wheel-only venv without pytest or optional extras. The
import guard also makes the same checks meaningful in a rich developer venv.
"""

from __future__ import annotations

import importlib.resources
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SMOKE = r"""
import contextlib
import errno
import importlib.abc
import importlib.util
import io
import json
import sys
from pathlib import Path

OPTIONAL = {"tree_sitter", "tree_sitter_javascript", "coverage", "psutil",
            "sentence_transformers", "playwright", "textual", "rich"}

class MissingOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in OPTIONAL:
            raise ModuleNotFoundError("optional provider disabled", name=fullname)
        return None

sys.meta_path.insert(0, MissingOptional())
original_find_spec = importlib.util.find_spec
def without_optional_spec(name, package=None):
    if name.split(".")[0] in OPTIONAL:
        return None
    return original_find_spec(name, package)
importlib.util.find_spec = without_optional_spec
from code_analysis.cli import main
from code_analysis.reports.json_report import load_json, canonical_json
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.reports.projection_views import ProjectionFormat, render_projection
from code_analysis.web_service import ReportViewService

root = Path(sys.argv[1])
language = sys.argv[2]
source = root / ("sample.py" if language == "python" else "sample.js")
source.write_text(
    "def increment(value: int) -> int:\n    return value + 1\n"
    if language == "python" else "export function increment(value) { return value + 1; }\n",
    encoding="utf-8",
)
output, errors = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
    status = main(["scan", str(source), "--language", language, "--json-compact", "--no-workspace"])
assert output.getvalue(), (status, errors.getvalue())
report = load_json(output.getvalue())
assert report.target.languages[0].value == language
assert report.source_files
assert not (root / ".code-analysis").exists()
if language == "python":
    assert status == 0, errors.getvalue()
    assert report.completeness.value == "complete"
    assert report.modules[0].symbols
    assert not OPTIONAL.intersection(sys.modules)
    source.unlink()  # Presentation must work with neither source nor optional extras.
    saved = root / "report.json"
    saved.write_text(canonical_json(report), encoding="utf-8")
    service = ReportViewService.load(saved)
    assert service.modules(offset=0, limit=10).records
    projection = build_projection(report, ProjectionName.MODULES)
    assert "sample" in render_projection(projection, ProjectionFormat.MARKDOWN)
    assert "<html" in render_projection(projection, ProjectionFormat.HTML)
else:
    assert status == errno.ENODATA, (status, errors.getvalue())
    assert report.completeness.value == "partial"
    assert report.diagnostics
    assert any(item.state.value == "unavailable" for item in report.providers)
print(json.dumps({"language": language, "status": status,
                  "completeness": report.completeness.value}))
"""


class BaseInstallTests(unittest.TestCase):
    def test_ruby_prism_resources_are_packaged(self) -> None:
        package_root = importlib.resources.files("code_analysis")
        ruby_dir = package_root / "languages" / "ruby"
        for name in ("prism_driver.rb", "call_occurrences.rb"):
            resource = ruby_dir / name
            self.assertTrue(
                resource.is_file(),
                f"missing packaged Ruby resource: code_analysis/languages/ruby/{name}",
            )

    def _run_smoke(self, language: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, "-I", "-c", _SMOKE, temporary, language],
                cwd=Path(temporary),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_python_and_saved_views_without_optional_providers(self) -> None:
        self._run_smoke("python")

    def test_selected_javascript_is_explicitly_unavailable(self) -> None:
        self._run_smoke("javascript")
