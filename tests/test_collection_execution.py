from __future__ import annotations

import errno
import hashlib
import marshal
import threading

import pytest
from pydantic import ValidationError

from code_analysis.collection_commands import CollectionPlanner, CollectionRequest
from code_analysis.collection_execution import (
    CollectionExecutionLimits,
    CollectionExecutionReceipt,
    execute_collection,
)
from code_analysis.reports.json_report import canonical_json


def test_import_worker_retains_partial_evidence_with_verified_generation(planner):
    from code_analysis.collection_artifacts import discover_collection_generation
    from code_analysis.config import AnalysisConfig
    from code_analysis.project import discover_context
    from code_analysis.workspace import prepare_workspace

    workspace = prepare_workspace(discover_context(planner.root, AnalysisConfig()))
    payload = marshal.dumps({("sample.py", 1, "answer"): (2, 2, 0.1, 0.1, {})})
    profile = planner.root / "capture.prof"
    profile.write_bytes(payload)
    request = CollectionRequest(
        operation="import-profile", profiles=("capture.prof",), workspace=".code-analysis"
    )
    result = execute_collection(
        planner, request, reviewed_command=planner.preview(request), cancel=threading.Event()
    )
    assert result.receipt.state == "partial", result.stderr.decode()
    assert result.receipt.status == errno.ENODATA
    assert result.receipt.report_validated
    binding = discover_collection_generation(result, workspace.root)
    assert binding.artifact_name == "profile.json"
    assert profile.read_bytes() == payload


@pytest.fixture
def planner(tmp_path):
    (tmp_path / "sample.py").write_text("def answer():\n    return 42\n")
    value = CollectionPlanner(tmp_path)
    yield value
    value.close()


def test_static_worker_exact_preview_identity_and_no_target_execution(planner):
    # Both cwd shadowing and PYTHONPATH must not replace the analyzer entrypoint.
    (planner.root / "code_analysis.py").write_text("raise RuntimeError('target executed')\n")
    request = CollectionRequest(target="sample.py")
    command = planner.preview(request)
    observed = []
    result = execute_collection(
        planner,
        request,
        reviewed_command=command,
        cancel=threading.Event(),
        on_started=observed.append,
    )
    assert result.receipt.state == "completed", result.stderr.decode()
    assert result.receipt.command.argv == command.argv
    assert result.receipt.returncode == result.receipt.status == 0
    assert observed == [result.receipt.process]
    assert result.receipt.process.start_time > 0
    assert result.report is not None
    assert result.receipt.stdout_sha256 == hashlib.sha256(result.stdout).hexdigest()
    assert result.receipt.stderr_sha256 == hashlib.sha256(result.stderr).hexdigest()
    assert (planner.root / ".code-analysis" / "workspace.json").is_file()


def test_changed_preview_fails_before_launch(planner):
    original = CollectionRequest(target="sample.py")
    with pytest.raises(OSError) as caught:
        execute_collection(
            planner,
            CollectionRequest(target="."),
            reviewed_command=planner.preview(original),
            cancel=threading.Event(),
        )
    assert caught.value.errno == errno.EINVAL
    assert not (planner.root / ".code-analysis").exists()


def test_prelaunch_cancel_has_no_process_or_workspace(planner):
    request = CollectionRequest(target="sample.py")
    cancel = threading.Event()
    cancel.set()
    result = execute_collection(
        planner, request, reviewed_command=planner.preview(request), cancel=cancel
    )
    assert result.receipt.state == "cancelled"
    assert result.receipt.status == errno.ECANCELED
    assert result.receipt.process is None and result.receipt.returncode is None
    assert result.report is None
    assert not (planner.root / ".code-analysis").exists()


def test_malformed_source_is_partial_not_empty_success(planner):
    (planner.root / "sample.py").write_text("def broken(:\n")
    request = CollectionRequest(target="sample.py")
    result = execute_collection(
        planner, request, reviewed_command=planner.preview(request), cancel=threading.Event()
    )
    assert result.receipt.state == "partial"
    assert result.receipt.status == result.receipt.returncode == errno.ENODATA
    assert result.receipt.report_validated and result.report.diagnostics


@pytest.mark.parametrize(
    "field,value",
    [
        ("wall_seconds", True),
        ("stream_bytes", True),
        ("wall_seconds", 0.0),
        ("forced_seconds", 0.0),
    ],
)
def test_limits_are_strict(field, value):
    with pytest.raises(ValidationError):
        CollectionExecutionLimits(**{field: value})


def test_stream_bound_remains_partial(planner):
    request = CollectionRequest(target="sample.py")
    result = execute_collection(
        planner,
        request,
        reviewed_command=planner.preview(request),
        cancel=threading.Event(),
        limits=CollectionExecutionLimits(stream_bytes=1024),
    )
    assert result.receipt.status == errno.ENODATA
    assert result.receipt.stream_truncated
    assert not result.receipt.report_validated and result.report is None
    assert len(result.stdout) <= 1024


def test_missing_identity_provider_does_not_start_command(planner, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "psutil", None)
    request = CollectionRequest(target="sample.py")
    result = execute_collection(
        planner, request, reviewed_command=planner.preview(request), cancel=threading.Event()
    )
    assert result.receipt.status == errno.ENODATA and result.receipt.process is None
    assert not (planner.root / ".code-analysis").exists()


def test_profile_requires_valid_report_before_execution(planner):
    (planner.root / "report.json").write_text("{}")
    request = CollectionRequest(
        operation="profile", source_report="report.json", workload="sample.py", allow_execution=True
    )
    with pytest.raises(ValueError):
        execute_collection(
            planner, request, reviewed_command=planner.preview(request), cancel=threading.Event()
        )
    assert not (planner.root / ".code-analysis").exists()


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_owned_profile_cleanup_and_actual_returncode(planner, ordinary_report, mode):
    (planner.root / "report.json").write_text(canonical_json(ordinary_report))
    (planner.root / "sample.py").write_text("import time\ntime.sleep(30)\n")
    request = CollectionRequest(
        operation="profile", source_report="report.json", workload="sample.py", allow_execution=True
    )
    cancel = threading.Event()
    timers = []

    def started(identity):
        if mode == "cancel":
            timer = threading.Timer(1.0, cancel.set)
            timers.append(timer)
            timer.start()

    result = execute_collection(
        planner,
        request,
        reviewed_command=planner.preview(request),
        cancel=cancel,
        limits=CollectionExecutionLimits(wall_seconds=2.0),
        on_started=started,
    )
    for timer in timers:
        timer.join()
    expected = errno.ECANCELED if mode == "cancel" else errno.ETIMEDOUT
    assert result.receipt.status == expected, result.stderr.decode()
    assert result.receipt.returncode is not None
    import psutil

    assert not psutil.pid_exists(result.receipt.process.pid)


def test_receipt_rejects_unproven_completion(planner):
    request = CollectionRequest(target="sample.py")
    cancel = threading.Event()
    cancel.set()
    result = execute_collection(
        planner, request, reviewed_command=planner.preview(request), cancel=cancel
    )
    payload = result.receipt.model_dump()
    payload.update(state="completed", status=0)
    with pytest.raises(ValidationError):
        CollectionExecutionReceipt.model_validate(payload)


def test_bounded_input_read_rejects_symlink_and_oversized_input(planner):
    (planner.root / "link.py").symlink_to(planner.root / "sample.py")
    with pytest.raises(OSError):
        planner.read_input("link.py", max_bytes=1024)
    with pytest.raises(OSError) as caught:
        planner.read_input("sample.py", max_bytes=1)
    assert caught.value.errno == errno.ENODATA


@pytest.mark.parametrize("payload", [b"", b"not json", b"{}", b"\xff"])
def test_empty_or_malformed_output_never_succeeds(planner, payload):
    from types import SimpleNamespace

    from code_analysis.collection_execution import CollectionProcessIdentity, _result

    process = SimpleNamespace(
        stdout=bytearray(payload),
        stderr=bytearray(),
        overflow=False,
        process=SimpleNamespace(returncode=0),
    )
    result = _result(
        planner.preview(CollectionRequest()),
        process,
        CollectionProcessIdentity(pid=123, start_time=1.0),
        0,
        "Finished",
        False,
    )
    assert result.receipt.state == "failed"
    assert result.receipt.status == errno.EBADMSG
    assert result.report is None


def test_profile_child_session_retains_valid_capture_evidence(planner, ordinary_report):
    (planner.root / "report.json").write_text(canonical_json(ordinary_report))
    request = CollectionRequest(
        operation="profile",
        source_report="report.json",
        workload="sample.py",
        allow_execution=True,
    )
    result = execute_collection(
        planner,
        request,
        reviewed_command=planner.preview(request),
        cancel=threading.Event(),
    )
    # Preserve the existing CLI's collection status, not whole-program coverage.
    assert result.receipt.status == result.receipt.returncode == 0, result.stderr.decode()
    assert result.receipt.report_validated
    assert result.report.schema_name == "code-analysis-profile-report"
    assert result.report.frames
    assert any("not exhaustive" in item for item in result.report.limitations)


def test_cancel_does_not_signal_unrelated_process(planner):
    import subprocess
    import sys

    unrelated = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])
    try:
        cancel = threading.Event()
        request = CollectionRequest(target="sample.py")
        result = execute_collection(
            planner,
            request,
            reviewed_command=planner.preview(request),
            cancel=cancel,
            on_started=lambda identity: cancel.set(),
        )
        assert result.receipt.status == errno.ECANCELED
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_cleanup_failure_cannot_claim_validated_terminal_output(planner, ordinary_report):
    from types import SimpleNamespace

    from code_analysis.collection_execution import CollectionProcessIdentity, _result

    process = SimpleNamespace(
        stdout=bytearray(canonical_json(ordinary_report).encode()),
        stderr=bytearray(),
        overflow=False,
        process=SimpleNamespace(returncode=None),
    )
    result = _result(
        planner.preview(CollectionRequest()),
        process,
        CollectionProcessIdentity(pid=123, start_time=1.0),
        errno.ETIMEDOUT,
        "Owned process cleanup failed.",
        True,
    )
    assert result.receipt.state == "failed"
    assert result.receipt.returncode is None
    assert result.report is None and not result.receipt.report_validated
