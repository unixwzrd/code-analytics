from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.change_models import ChangeEvidenceSource
from code_analysis.cli import ExitCode, main
from code_analysis.config import AnalysisConfig, PhaseFourLimits, load_config_file
from code_analysis.git_reporting import build_git_history
from code_analysis.history import render_change_report
from code_analysis.models import Completeness
from code_analysis.project import discover_context
from code_analysis.report_models import CommitCategory, GitPathScope, SectionState
from code_analysis.service import scan


def _git(root: Path, *arguments: str, date: str = "2026-01-01T12:00:00Z") -> None:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Synthetic Author",
        "GIT_AUTHOR_EMAIL": "private@example.invalid",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": "Synthetic Committer",
        "GIT_COMMITTER_EMAIL": "committer@example.invalid",
        "GIT_COMMITTER_DATE": date,
    }
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        env=environment,
    )


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "src" / "package").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n', encoding="utf-8"
    )
    (root / "src" / "package" / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "src" / "package" / "b.py").write_text("def beta():\n    return 2\n")
    for index in range(20):
        (root / f"tracked-{index:02d}.txt").write_text(f"{index}\n", encoding="utf-8")
        (root / f"support-{index:02d}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "feature: initial", date="2026-01-01T12:00:00Z")
    (root / "src" / "package" / "a.py").write_text("def alpha():\n    return 3\n")
    (root / "src" / "package" / "b.py").write_text("def beta():\n    return 4\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "fix: coupled behavior", date="2026-01-02T12:00:00Z")
    (root / "src" / "package" / "a.py").write_text("def alpha():\n    return 3  \n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "format: whitespace", date="2026-01-03T12:00:00Z")
    (root / "tracked-01.txt").write_text("changed 1\n", encoding="utf-8")
    (root / "tracked-02.txt").write_text("changed 2\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "docs: coupled metadata", date="2026-01-04T12:00:00Z")
    _git(root, "mv", "tracked-00.txt", "renamed.txt")
    _git(root, "commit", "-q", "-m", "refactor: rename", date="2026-01-05T12:00:00Z")
    return root


def test_git_history_is_bounded_private_and_referentially_valid(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    config = AnalysisConfig(phase_four_limits=PhaseFourLimits(min_git_shared_changes=1))
    report = build_git_history(discover_context(root, config))
    assert report.source is ChangeEvidenceSource.GIT
    assert report.completeness is Completeness.COMPLETE
    assert report.git_repository is not None
    assert report.git_repository.path_scope is GitPathScope.PYTHON
    assert report.git_repository.tracked_files == 22
    assert len(report.git_commits) == 3
    assert {item.category for item in report.git_commits} >= {
        CommitCategory.FIX,
        CommitCategory.FORMATTING,
    }
    assert report.git_path_changes
    assert all(item.path.endswith(".py") for item in report.git_path_changes)
    assert all(path.endswith(".py") for item in report.git_commits for path in item.changed_paths)
    assert any(item.formatting_only for item in report.git_path_changes)
    assert any(
        {item.left_path, item.right_path} == {"src/package/a.py", "src/package/b.py"}
        for item in report.coupling
    )
    portable = render_change_report(report)
    assert "private@example.invalid" not in portable
    assert "Synthetic Author" not in portable
    assert "coupled behavior" not in portable
    assert str(tmp_path) not in portable


def test_repository_git_scope_is_explicit_and_includes_non_python_paths(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    report = build_git_history(
        discover_context(
            root,
            AnalysisConfig(
                git_path_scope=GitPathScope.REPOSITORY,
                phase_four_limits=PhaseFourLimits(min_git_shared_changes=1),
            ),
        )
    )
    assert report.git_repository is not None
    assert report.git_repository.path_scope is GitPathScope.REPOSITORY
    assert report.git_repository.tracked_files == 43
    assert len(report.git_commits) == 5
    assert any(item.previous_path == "tracked-00.txt" for item in report.git_path_changes)
    assert any(not item.path.endswith(".py") for item in report.git_path_changes)
    assert any(
        not item.left_path.endswith(".py") or not item.right_path.endswith(".py")
        for item in report.coupling
    )
    assert any("Repository-wide Git history" in item for item in report.limitations)


def test_git_path_scope_loads_from_project_configuration(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.code-analysis]\ngit-path-scope = "repository"\n'
        "[tool.code-analysis.phase-four-limits]\nmin-git-shared-changes = 4\n",
        encoding="utf-8",
    )
    config = load_config_file(pyproject)
    assert config.git_path_scope is GitPathScope.REPOSITORY
    assert config.phase_four_limits.min_git_shared_changes == 4


def test_git_commit_bound_is_partial_not_success(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    report = build_git_history(
        discover_context(
            root,
            AnalysisConfig(phase_four_limits=PhaseFourLimits(max_git_commits=1)),
        )
    )
    assert report.completeness is Completeness.PARTIAL
    assert report.providers[0].completeness is Completeness.PARTIAL
    assert len(report.git_commits) == 1


def test_non_git_target_records_optional_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "module.py").write_text("value = 1\n", encoding="utf-8")
    report = build_git_history(discover_context(root, AnalysisConfig()))
    assert report.completeness is Completeness.PARTIAL
    assert report.providers[0].state.value == "unavailable"
    assert report.git_repository is None


def test_git_json_has_no_untyped_or_private_payload(tmp_path: Path) -> None:
    report = build_git_history(discover_context(_repository(tmp_path), AnalysisConfig()))
    payload = json.loads(render_change_report(report))
    assert isinstance(payload["git_commits"], list)
    assert all("contributor_pseudonym" in item for item in payload["git_commits"])
    assert all("subject" not in item and "email" not in item for item in payload["git_commits"])


def test_default_git_coupling_threshold_omits_one_off_pairs(tmp_path: Path) -> None:
    report = build_git_history(discover_context(_repository(tmp_path), AnalysisConfig()))
    assert report.coupling == ()
    assert any("below 3 shared changes" in item for item in report.limitations)


def test_normal_scan_does_not_collect_git_history(tmp_path: Path) -> None:
    report = scan(discover_context(_repository(tmp_path), AnalysisConfig()))
    coverage = next(item for item in report.section_coverage if item.section == "git-history")
    assert coverage.state is SectionState.DISABLED
    assert report.git_repository is None
    assert report.git_commits == ()
    assert report.git_path_changes == ()
    assert report.change_couplings == ()
    assert all(item.provider_id != "native-git-history" for item in report.providers)


def test_explicit_history_cli_emits_standalone_change_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path)
    assert main(["history", str(root)]) == ExitCode.SUCCESS
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_name"] == "code-analysis-change-report"
    assert payload["source"] == "git"
    assert payload["git_repository"]["path_scope"] == "python"
    assert payload["coupling"] == []


def test_git_change_report_rejects_dangling_commit_references(tmp_path: Path) -> None:
    report = build_git_history(discover_context(_repository(tmp_path), AnalysisConfig()))
    payload = report.model_dump()
    payload["git_path_changes"][0]["commit_id"] = "f" * 40
    with pytest.raises(ValidationError, match="unknown commit"):
        type(report).model_validate(payload)
