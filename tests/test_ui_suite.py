"""Qualification plumbing must not turn missing tools or skipped tests into green gates."""

from __future__ import annotations

import errno
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from ui_suite import (
    UiExecutionCase,
    UiExecutionEvidence,
    UiTestModule,
    classify_module,
    discover_ui_modules,
)

from tools import test_ui as gate


@pytest.mark.parametrize(
    ("source", "browser"),
    [
        ('def test_a():\n    pytest.importorskip("playwright.sync_api")\n', True),
        ("from playwright.sync_api import sync_playwright\n", True),
        ("import playwright.sync_api as browser\n", True),
        ("@pytest.mark.ui_smoke\ndef test_a(): pass\n", False),
    ],
)
def test_ui_inventory_discovers_browser_imports_and_explicit_journeys(tmp_path, source, browser):
    path = tmp_path / "test_new_feature.py"
    path.write_text(source)
    assert classify_module(path).browser is browser
    assert [item.name for item in discover_ui_modules(tmp_path)] == [path.name]


def test_ui_inventory_ignores_sample_text_and_is_stably_ordered(tmp_path):
    (tmp_path / "test_sample.py").write_text('text = "playwright.sync_api"\n')
    (tmp_path / "test_ui_z.py").write_text("def test_z(): pass\n")
    (tmp_path / "test_ui_a.py").write_text("def test_a(): pass\n")
    assert [item.name for item in discover_ui_modules(tmp_path)] == ["test_ui_a.py", "test_ui_z.py"]
    (tmp_path / "test_broken.py").write_text("def (\n")
    with pytest.raises(SyntaxError):
        discover_ui_modules(tmp_path)


@pytest.mark.parametrize(
    ("tests", "failures", "errors", "skipped", "code", "state"),
    [
        (2, 0, 0, 0, 0, "passed"),
        (2, 1, 0, 0, 1, "failed"),
        (2, 0, 1, 0, 1, "failed"),
        (2, 0, 0, 1, 0, "incomplete"),
        (0, 0, 0, 0, 0, "incomplete"),
        (2, 0, 0, 0, 3, "failed"),
    ],
)
def test_ui_gate_preserves_failed_skipped_empty_and_native_statuses(
    tmp_path, tests, failures, errors, skipped, code, state
):
    xml = tmp_path / "junit.xml"
    xml.write_text(
        f'<testsuites><testsuite tests="{tests}" failures="{failures}" '
        f'errors="{errors}" skipped="{skipped}"/></testsuites>'
    )
    assert gate.result_status(gate.read_counts(xml), code) == state


def test_ui_counts_reject_coercion_extras_contradictions_and_malformed_xml(tmp_path):
    valid = {"tests": 2, "failures": 0, "errors": 0, "skipped": 0}
    for values in (
        {**valid, "tests": True},
        {**valid, "other": 1},
        {**valid, "failures": 3},
        {**valid, "errors": -1},
    ):
        with pytest.raises(ValidationError):
            gate.Counts(**values)
    xml = tmp_path / "junit.xml"
    xml.write_text("<testsuites/>")
    with pytest.raises(ValueError, match="no test suite"):
        gate.read_counts(xml)


def test_ui_gate_does_not_inherit_hidden_pytest_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only -k never_run")
    log = tmp_path / "pytest.log"
    assert (
        gate.run_pytest(
            (sys.executable, "-c", 'import os; print(os.environ.get("PYTEST_ADDOPTS", "unset"))'),
            log,
        )
        == 0
    )
    assert log.read_text().strip() == "unset"


@pytest.mark.parametrize(
    "condition",
    [
        "passed",
        "skipped",
        "changed",
        "unavailable",
        "failed",
        "deselected",
        "no-browser",
        "not-run",
        "missing-cases",
        "count-mismatch",
    ],
)
def test_ui_runner_retains_validated_receipt_and_artifact_hashes(tmp_path, monkeypatch, condition):
    root = tmp_path / "checkout"
    (root / "tests").mkdir(parents=True)
    test = root / "tests/test_ui_example.py"
    test.write_text('def test_example():\n    pytest.importorskip("playwright.sync_api")\n')
    monkeypatch.setattr(gate, "ROOT", root)
    captures = root / "output/playwright"
    captures.mkdir(parents=True)
    (captures / "stale.png").write_bytes(b"old image, not from this run")

    def probe():
        if condition == "unavailable":
            raise FileNotFoundError("browser not installed; do not install implicitly")
        return (
            gate.BrowserIdentity(name="chromium", version="test"),
            gate.BrowserIdentity(name="webkit", version="test"),
        )

    def run(command, log):
        assert command[1:3] == ("-m", "pytest")
        assert "xfail_strict=true" in command
        assert "addopts=" in command and "--strict-markers" in command
        log.write_text("synthetic pytest result\n")
        xml = Path(
            next(
                part.removeprefix("--junitxml=")
                for part in command
                if part.startswith("--junitxml=")
            )
        )
        skipped, failures = int(condition == "skipped"), int(condition == "failed")
        total = 2 if condition == "count-mismatch" else 1
        xml.write_text(
            f'<testsuite tests="{total}" failures="{failures}" errors="0" skipped="{skipped}"/>'
        )
        expected_ids = ("tests/test_ui_example.py::test_example",)
        if condition == "deselected":
            expected_ids += ("tests/test_ui_example.py::test_other",)
        evidence = UiExecutionEvidence(
            expected_node_ids=expected_ids,
            cases=(
                UiExecutionCase(
                    node_id="tests/test_ui_example.py::test_example",
                    direct_browser_check=condition != "no-browser",
                    outcome={"failed": "failed", "skipped": "skipped", "not-run": "not-run"}.get(
                        condition, "passed"
                    ),
                ),
            ),
        )
        evidence_path = Path(
            next(
                part.removeprefix("--ui-evidence=")
                for part in command
                if part.startswith("--ui-evidence=")
            )
        )
        if condition != "missing-cases":
            evidence_path.write_text(evidence.model_dump_json())
        (captures / "new.png").write_bytes(b"current image")
        if condition == "changed":
            test.write_text("def test_changed(): pass\n")
        return int(condition == "failed")

    monkeypatch.setattr(gate, "probe_browsers", probe)
    monkeypatch.setattr(gate, "run_pytest", run)
    output = tmp_path / "artifacts"
    status = gate.main(["--output-dir", str(output)])
    expected = {"passed": 0, "failed": errno.EPROTO}.get(condition, errno.ENODATA)
    assert status == expected
    run_dir = next(output.iterdir())
    receipt = gate.UiTestRun.model_validate_json((run_dir / "result.json").read_text())
    assert receipt.scope == "full"
    assert receipt.status == {"passed": "passed", "failed": "failed"}.get(condition, "incomplete")
    assert receipt.input_count == 1
    assert all((run_dir / item.path).is_file() for item in receipt.artifacts)
    assert not (run_dir / "captures/stale.png").exists()
    if condition != "unavailable":
        assert (run_dir / "captures/new.png").read_bytes() == b"current image"
        assert "captures/new.png" in {item.path for item in receipt.artifacts}
    if condition not in {"passed", "failed"}:
        assert receipt.issues
    if condition == "unavailable":
        assert receipt.counts is None and receipt.pytest_exit_code is None
    if condition == "passed":
        for change in (
            {"counts": None},
            {"pytest_exit_code": 1},
            {"browsers": []},
            {"issues": ["not qualified"]},
            {"command": ["another-interpreter"]},
            {"other": 1},
            {"execution": None},
        ):
            with pytest.raises(ValidationError):
                gate.UiTestRun.model_validate_json(
                    json.dumps(receipt.model_dump(mode="json") | change)
                )
    # Inventories use named fields, and include no source contents or environment values.
    assert set(json.loads((run_dir / "inputs-before.json").read_text())[0]) == {"path", "sha256"}


def test_ui_gate_rejects_support_only_inventory(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_ui_support.py").write_text("def test_support(): pass\n")
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(
        gate, "probe_browsers", lambda: pytest.fail("must not probe empty browser inventory")
    )
    assert gate.main(["--output-dir", str(tmp_path / "artifacts")]) == errno.ENODATA
    receipt = next((tmp_path / "artifacts").glob("*/result.json"))
    assert "No direct browser test cases" in receipt.read_text()


def test_execution_records_reject_duplicate_identities_and_untyped_states():
    case = UiExecutionCase(node_id="test::case", direct_browser_check=True, outcome="passed")
    for change in ({"direct_browser_check": 1}, {"outcome": "almost"}, {"other": 1}):
        with pytest.raises(ValidationError):
            UiExecutionCase.model_validate_json(json.dumps(case.model_dump() | change))
    for expected, cases in (
        (("test::case", "test::case"), (case,)),
        (("test::case",), (case, case)),
    ):
        with pytest.raises(ValidationError):
            UiExecutionEvidence(expected_node_ids=expected, cases=cases)


def test_captures_retain_only_new_or_rewritten_images_with_exact_bytes(tmp_path, monkeypatch):
    root, run = tmp_path / "captures", tmp_path / "run"
    root.mkdir()
    (root / "stale.png").write_bytes(b"old capture")
    (root / "changed.png").write_bytes(b"first version")
    before = {path.name: gate.read_capture(path)[0] for path in gate.capture_paths(root)}
    (root / "changed.png").write_bytes(b"corrected layout")
    (root / "new.png").write_bytes(b"new layout")
    (root / "escape.png").symlink_to(root / "stale.png")
    gate.retain_captures(root, run, before)
    assert {path.name for path in (run / "captures").iterdir()} == {"changed.png", "new.png"}
    assert (run / "captures/changed.png").read_bytes() == b"corrected layout"
    monkeypatch.setattr(gate, "CAPTURE_BYTE_LIMIT", 1)
    with pytest.raises(ValueError, match="bound"):
        gate.read_capture(root / "new.png")


@pytest.fixture
def isolated_ui_tests(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    original = Path(__file__).parent
    for name in ("conftest.py", "ui_suite.py"):
        (tests / name).write_bytes((original / name).read_bytes())
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts="-k good"\n'
        'markers=["ui: UI", "ui_browser: browser", "ui_smoke: smoke", '
        '"ui_direct_browser: direct browser"]\n'
    )
    (tests / "test_ui_example.py").write_text("def test_good(): pass\ndef test_other(): pass\n")
    return tmp_path


def test_gate_clears_config_filters_and_records_exact_selected_cases(
    isolated_ui_tests, monkeypatch
):
    monkeypatch.setattr(gate, "ROOT", isolated_ui_tests)
    run = isolated_ui_tests / "artifacts"
    run.mkdir()
    command = gate.pytest_command((UiTestModule("test_ui_example.py", False, ()),), run, False)
    assert gate.run_pytest(command, run / "pytest.log") == 0
    assert gate.read_counts(run / "junit.xml").tests == 2
    evidence = UiExecutionEvidence.model_validate_json((run / "cases.json").read_text())
    assert len(evidence.expected_node_ids) == len(evidence.cases) == 2
    assert all(case.outcome == "passed" for case in evidence.cases)
    # These are support-only synthetic cases, not evidence of real browser interaction.
    assert not any(case.direct_browser_check for case in evidence.cases)


def test_ordinary_targeted_pytest_ignores_unselected_malformed_module(isolated_ui_tests):
    (isolated_ui_tests / "tests/test_unrelated.py").write_text("def (\n")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_ui_example.py", "-q"],
        cwd=isolated_ui_tests,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
