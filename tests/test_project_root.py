from __future__ import annotations

from pathlib import Path

from code_analysis.cli import ExitCode, main
from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context, find_project_root, iter_python_files


def test_nested_source_target_uses_project_root_and_remains_bounded(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "src" / "package"
    sibling = project / "src" / "other"
    source.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n', encoding="utf-8"
    )
    selected = source / "selected.py"
    selected.write_text("value = 1\n", encoding="utf-8")
    (sibling / "excluded.py").write_text("value = 2\n", encoding="utf-8")

    root = find_project_root(source)
    context = discover_context(source, AnalysisConfig(), project_root=root)

    assert root == project
    assert context.source.physical_root == project
    assert context.source.logical_source_roots == ("src",)
    assert iter_python_files(context) == (selected,)


def test_project_root_falls_back_to_nearest_git_boundary(tmp_path: Path) -> None:
    project = tmp_path / "project"
    target = project / "lib"
    target.mkdir(parents=True)
    (project / ".git").mkdir()
    assert find_project_root(target) == project


def test_cli_places_nested_target_workspace_at_project_root(tmp_path: Path, capsys) -> None:
    project = tmp_path / "project"
    source = project / "src" / "package"
    source.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n', encoding="utf-8"
    )
    (source / "module.py").write_text("value = 1\n", encoding="utf-8")

    result = main(["scan", str(source), "--language", "python", "--quiet"])

    assert result in {ExitCode.SUCCESS, ExitCode.ENODATA}
    assert (project / ".code-analysis" / "workspace.json").is_file()
    assert not (source / ".code-analysis").exists()
    assert capsys.readouterr().out == ""
