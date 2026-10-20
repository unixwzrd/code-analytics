from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.collection_artifacts import discover_collection_generation
from code_analysis.collection_commands import CollectionPlanner, CollectionRequest
from code_analysis.collection_execution import execute_collection


def test_trusted_choices_preserve_launcher_and_profile_interpreter(tmp_path):
    launcher = tmp_path / "selected-python"
    launcher.symlink_to(sys.executable)
    (tmp_path / "source.py").write_text("raise AssertionError('preview must not execute')\n")
    (tmp_path / "report.json").write_text("{}")
    planner = CollectionPlanner(tmp_path, interpreters=(launcher, launcher))
    try:
        choices = planner.context(report_available=False).interpreters
        assert len(choices) == 2
        assert choices[0].id == "current" and choices[0].path == sys.executable
        selected = choices[1]
        assert selected.path == str(launcher)
        for operation in ("scan", "profile"):
            options = (
                {"target": "source.py"}
                if operation == "scan"
                else {
                    "source_report": "report.json",
                    "workload": "source.py",
                    "allow_execution": True,
                }
            )
            preview = planner.preview(
                CollectionRequest(operation=operation, interpreter_id=selected.id, **options)
            )
            assert preview.interpreter == preview.argv[0] == str(launcher)
            assert preview.argv[1:4] == ("-I", "-m", "code_analysis")
            if operation == "profile":
                assert preview.argv[preview.argv.index("--") + 1] == str(launcher)
        assert planner.preview(CollectionRequest()).interpreter == sys.executable
    finally:
        planner.close()


def test_browser_cannot_supply_an_executable_or_unknown_choice(tmp_path):
    planner = CollectionPlanner(tmp_path)
    try:
        for identity in (sys.executable, "python-not-trusted", "../python"):
            with pytest.raises(ValueError, match="trusted at startup"):
                planner.preview(CollectionRequest(interpreter_id=identity))
        with pytest.raises(ValidationError):
            CollectionRequest(interpreter_id=True)
    finally:
        planner.close()


def test_trusted_interpreter_replacement_is_not_silently_accepted(tmp_path):
    executable = tmp_path / "python"
    executable.write_text("#!/bin/sh\nexit 99\n")
    executable.chmod(0o700)
    planner = CollectionPlanner(tmp_path, interpreters=(executable,))
    try:
        choice = planner.context(report_available=False).interpreters[1]
        planner.preview(CollectionRequest(interpreter_id=choice.id))
        executable.write_text("#!/bin/sh\nexit 100\n")
        with pytest.raises(ValueError, match="interpreter changed"):
            planner.preview(CollectionRequest(interpreter_id=choice.id))
    finally:
        planner.close()


def test_interpreter_choice_is_path_stable_not_configuration_order(tmp_path):
    paths = (tmp_path / "first", tmp_path / "second")
    for path in paths:
        path.symlink_to(sys.executable)
    planners = [CollectionPlanner(tmp_path, interpreters=order) for order in (paths, paths[::-1])]
    try:
        identities = [
            {choice.path: choice.id for choice in p.context(report_available=False).interpreters}
            for p in planners
        ]
        assert identities[0] == identities[1]
    finally:
        for planner in planners:
            planner.close()


def test_invalid_trusted_paths_and_choice_bound(tmp_path):
    plain = tmp_path / "not-executable"
    plain.write_text("not Python")
    for path in (Path("relative"), tmp_path, plain):
        with pytest.raises(ValueError):
            CollectionPlanner(tmp_path, interpreters=(path,))
    with pytest.raises(ValueError, match="at most 32"):
        CollectionPlanner(tmp_path, interpreters=(Path(sys.executable),) * 33)
    with pytest.raises(FileNotFoundError):
        CollectionPlanner(tmp_path, interpreters=(tmp_path / "missing",))
    # Mere configuration checks do not execute the trusted path.
    executable = tmp_path / "launcher"
    executable.write_text("#!/bin/sh\ntouch should-not-exist\n")
    executable.chmod(0o700)
    planner = CollectionPlanner(tmp_path, interpreters=(executable,))
    planner.close()
    assert not os.path.exists(tmp_path / "should-not-exist")


def test_selected_launcher_executes_and_binds_its_generation(tmp_path):
    launcher = tmp_path / "chosen-python"
    launcher.symlink_to(sys.executable)
    (tmp_path / "sample.py").write_text("def answer():\n    return 42\n")
    planner = CollectionPlanner(tmp_path, interpreters=(launcher,))
    try:
        choice = planner.context(report_available=False).interpreters[1]
        request = CollectionRequest(interpreter_id=choice.id, target="sample.py")
        result = execute_collection(
            planner, request, reviewed_command=planner.preview(request), cancel=threading.Event()
        )
        assert result.receipt.state == "completed", result.stderr.decode()
        binding = discover_collection_generation(result, tmp_path / ".code-analysis")
        assert binding.process == result.receipt.process
        assert result.receipt.command.interpreter == str(launcher)
        (tmp_path / "report.json").write_bytes(result.stdout)
        profile_request = CollectionRequest(
            operation="profile",
            interpreter_id=choice.id,
            source_report="report.json",
            workload="sample.py",
            allow_execution=True,
            workspace=".code-analysis",
        )
        profile = execute_collection(
            planner,
            profile_request,
            reviewed_command=planner.preview(profile_request),
            cancel=threading.Event(),
        )
        assert profile.receipt.state in {"completed", "partial"}, profile.stderr.decode()
        assert profile.receipt.report_validated
        profile_binding = discover_collection_generation(profile, tmp_path / ".code-analysis")
        assert profile_binding.process == profile.receipt.process
        assert profile.receipt.command.interpreter == str(launcher)
    finally:
        planner.close()


def test_trusted_but_incompatible_launcher_fails_without_fallback(tmp_path):
    launcher = tmp_path / "unavailable-python"
    launcher.write_text("#!/bin/sh\necho 'Code Analysis unavailable' >&2\nexit 1\n")
    launcher.chmod(0o700)
    planner = CollectionPlanner(tmp_path, interpreters=(launcher,))
    try:
        choice = planner.context(report_available=False).interpreters[1]
        request = CollectionRequest(interpreter_id=choice.id)
        result = execute_collection(
            planner, request, reviewed_command=planner.preview(request), cancel=threading.Event()
        )
        assert result.receipt.state == "failed"
        assert result.receipt.status == result.receipt.returncode == 1
        assert result.report is None
        assert b"Code Analysis unavailable" in result.stderr
        assert not (tmp_path / ".code-analysis").exists()
    finally:
        planner.close()
