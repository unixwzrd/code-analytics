"""Publication hygiene and supplied tutorial evidence, without running tutorial code."""

from __future__ import annotations

import hashlib
import subprocess
import tomllib
from pathlib import Path

import pytest

from code_analysis import __version__
from code_analysis.models import SourceLanguage
from code_analysis.reports.json_report import load_json

ROOT = Path(__file__).resolve().parents[1]
TUTORIALS = ROOT / "tutorials"


@pytest.mark.parametrize(
    "path",
    [
        ".ruff_cache/results.json",
        "nested/.code-analysis/generations/report.json",
        "nested/.playwright-cli/screenshot.png",
        "project/node_modules/example/index.js",
        "project/.vite/cache.json",
        "project/vendor/bundle/example.rb",
        "project/.ruby-lsp/index.json",
        "output/qualification/report.json",
        "private/operator-notes.md",
        "private/README.md",
        "docs/private/operator-notes.md",
        ".env.production",
        "nested/credentials.json",
        "nested/secrets.json",
        "nested/server.key",
        "nested/client.p12",
        "nested/worker.pid",
        "nested/session.log",
        "nested/index.sqlite3",
        "nested/result.tmp",
    ],
)
def test_ignore_policy_protects_local_and_private_artifacts(path: str) -> None:
    result = subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "--no-index", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (path, result.stderr)


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "LICENSE",
        "package-lock.json",
        "tests/golden/minimal-report.json",
        "tests/fixtures/javascript/src/main.js",
        "tutorials/python/beginner/evidence/actual-report.json",
        "tutorials/python/beginner/evidence/expected-evidence.json",
        "tutorials/python/beginner/evidence/qualification.md",
    ],
)
def test_ignore_policy_keeps_deliberate_source_and_evidence(path: str) -> None:
    result = subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "--no-index", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, (path, result.stdout, result.stderr)


def test_no_private_directory_content_is_tracked() -> None:
    paths = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True)
    assert not [path for path in paths.splitlines() if "private" in Path(path).parts]


def test_beta_metadata_and_documented_tutorial_entry_point_agree() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["version"] == __version__
    assert "Development Status :: 4 - Beta" in project["classifiers"]
    readme = (ROOT / "README.md").read_text()
    assert f"`{__version__}`" in readme
    assert f"--branch v{__version__}" in readme
    assert (ROOT / "CHANGELOG.md").read_text().splitlines()[2].startswith(f"## {__version__} — ")
    assert "tutorials/python/beginner/README.md" in readme
    assert "docs/beta.md" in readme


@pytest.mark.parametrize("language", ["python", "javascript", "ruby"])
@pytest.mark.parametrize("level", ["beginner", "intermediate", "advanced"])
def test_supplied_tutorial_reports_remain_source_bound(language: str, level: str) -> None:
    root = TUTORIALS / language / level
    report_text = (root / "evidence/actual-report.json").read_text()
    report = load_json(report_text)
    assert report.source_files
    assert {item.language for item in report.source_files} == {SourceLanguage(language)}
    for source in report.source_files:
        path = root / source.logical_path
        assert path.resolve().is_relative_to(root.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source.source_sha256
    assert not any(value in report_text for value in ("/Users/", "/Volumes/", "/private/tmp/"))
    readme = (root / "README.md").read_text()
    assert "code-analysis ui evidence/actual-report.json --source-root ." in readme
    assert (root / "HINTS.md").is_file()
    assert (root / "SOLUTIONS.md").is_file()
