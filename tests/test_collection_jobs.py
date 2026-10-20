from __future__ import annotations

import errno
import hashlib
import json
import os
import time
from dataclasses import replace

import pytest
from pydantic import ValidationError

from code_analysis.collection_artifacts import discover_collection_generation
from code_analysis.collection_commands import CollectionPlanner, CollectionRequest
from code_analysis.collection_execution import CollectionExecutionLimits
from code_analysis.collection_job_models import CollectionJob
from code_analysis.collection_jobs import CollectionJobs
from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.workspace import prepare_workspace


@pytest.fixture
def jobs(tmp_path):
    (tmp_path / "sample.py").write_text("def answer():\n    return 42\n")
    workspace = prepare_workspace(discover_context(tmp_path, AnalysisConfig()))
    planner = CollectionPlanner(tmp_path)
    controller = CollectionJobs(planner, workspace)
    yield controller
    controller.close()
    planner.close()


def submit(jobs, **values):
    request = CollectionRequest(target="sample.py", workspace=".code-analysis", **values)
    return jobs.submit(request, jobs.planner.preview(request), CollectionExecutionLimits())


def terminal(jobs, identity):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = jobs.get(identity)
        if job.state not in {"queued", "running"}:
            return job
        time.sleep(0.02)
    pytest.fail("collection did not reach a terminal state")


def test_durable_job_reconnect_result_and_reopen(jobs):
    queued = submit(jobs)
    assert queued.state == "queued"
    # Fetching/listing independently of the submitting client does not cancel work.
    assert jobs.list()[0].job_id == queued.job_id
    finished = terminal(jobs, queued.job_id)
    assert finished.state == "completed", finished.reason
    assert finished.binding is not None
    assert jobs.result(queued.job_id).receipt == finished.receipt
    assert (jobs.directory / queued.job_id / "job.json").stat().st_mode & 0o777 == 0o600
    jobs.close()
    reopened = CollectionJobs(jobs.planner, jobs.workspace)
    try:
        assert reopened.get(queued.job_id) == finished
        assert reopened.result(queued.job_id).receipt == finished.receipt
    finally:
        reopened.close()


def test_only_one_controller_and_active_job_per_workspace(jobs):
    with pytest.raises(OSError) as caught:
        CollectionJobs(jobs.planner, jobs.workspace)
    assert caught.value.errno == errno.EBUSY
    job = submit(jobs)
    with pytest.raises(OSError) as caught:
        submit(jobs)
    assert caught.value.errno == errno.EBUSY
    jobs.cancel(job.job_id)
    assert terminal(jobs, job.job_id).state == "cancelled"


def test_selected_workspace_and_reviewed_command_required(jobs):
    request = CollectionRequest(target="sample.py")
    with pytest.raises(OSError) as caught:
        jobs.submit(request, jobs.planner.preview(request), CollectionExecutionLimits())
    assert caught.value.errno == errno.EINVAL
    assert jobs.list() == ()


def test_capture_or_generation_tampering_is_rechecked(jobs):
    job = terminal(jobs, submit(jobs).job_id)
    assert job.binding
    capture = jobs.directory / job.job_id / "stdout"
    payload = capture.read_bytes()
    capture.write_bytes(b"{}")
    with pytest.raises(OSError) as caught:
        jobs.result(job.job_id)
    assert caught.value.errno == errno.EBADMSG
    capture.write_bytes(payload)
    artifact = (
        jobs.workspace.kind_directory(job.binding.kind) / job.binding.generation_id / "report.json"
    )
    artifact.write_bytes(b"{}")
    with pytest.raises(OSError) as caught:
        jobs.result(job.job_id)
    assert caught.value.errno == errno.EBADMSG


def test_discovery_is_bounded_and_missing_identity_is_not_success(jobs):
    first = terminal(jobs, submit(jobs).job_id)
    result = jobs.result(first.job_id)
    second = terminal(jobs, submit(jobs).job_id)
    assert second.binding != first.binding
    assert discover_collection_generation(result, jobs.workspace.root) == first.binding
    with pytest.raises(OSError) as caught:
        discover_collection_generation(result, jobs.workspace.root, maximum_generations=1)
    assert caught.value.errno == errno.ENODATA
    changed = result.receipt.model_copy(
        update={"process": result.receipt.process.model_copy(update={"pid": 1})}
    )
    with pytest.raises(OSError) as caught:
        discover_collection_generation(replace(result, receipt=changed), jobs.workspace.root)
    assert caught.value.errno == errno.ENODATA


def test_records_reject_escapes_symlinks_and_contradictions(jobs):
    with pytest.raises(OSError):
        jobs.get("../outside")
    job = terminal(jobs, submit(jobs).job_id)
    path = jobs.directory / job.job_id / "job.json"
    for changes in ({"state": "running"}, {"binding": None}, {"extra": True}):
        with pytest.raises(ValidationError):
            CollectionJob.model_validate({**job.model_dump(), **changes})
    backup = path.with_name("saved.json")
    path.rename(backup)
    path.symlink_to(backup)
    with pytest.raises(OSError):
        jobs.get(job.job_id)


def test_recovery_does_not_adopt_or_signal_live_process(jobs):
    import psutil

    job = terminal(jobs, submit(jobs).job_id)
    path = jobs.directory / job.job_id / "job.json"
    payload = job.model_dump(mode="json")
    payload.update(
        state="running",
        status=None,
        receipt=None,
        binding=None,
        process={"pid": os.getpid(), "start_time": psutil.Process().create_time()},
    )
    path.write_text(json.dumps(payload))
    jobs.close()
    with pytest.raises(OSError) as caught:
        CollectionJobs(jobs.planner, jobs.workspace)
    assert caught.value.errno == errno.EBUSY
    assert json.loads(path.read_text())["state"] == "running"


def test_missing_durable_launch_identity_requires_review(jobs):
    job = terminal(jobs, submit(jobs).job_id)
    path = jobs.directory / job.job_id / "job.json"
    payload = job.model_dump(mode="json")
    payload.update(state="queued", status=None, receipt=None, binding=None, process=None)
    path.write_text(json.dumps(payload))
    jobs.close()
    with pytest.raises(OSError) as caught:
        CollectionJobs(jobs.planner, jobs.workspace)
    assert caught.value.errno == errno.ENODATA


def test_dead_process_recovery_preserves_unknown_outcome(jobs):
    job = terminal(jobs, submit(jobs).job_id)
    path = jobs.directory / job.job_id / "job.json"
    payload = job.model_dump(mode="json")
    payload.update(state="running", status=None, receipt=None, binding=None)
    path.write_text(json.dumps(payload))
    jobs.close()
    reopened = CollectionJobs(jobs.planner, jobs.workspace)
    try:
        recovered = reopened.get(job.job_id)
        assert recovered.state == "failed" and recovered.status == errno.ENODATA
        assert recovered.binding is None
    finally:
        reopened.close()


def test_close_cancels_owned_work_and_prevents_new_submissions(jobs):
    job = submit(jobs)
    jobs.close()
    saved = CollectionJob.model_validate_json(
        (jobs.directory / job.job_id / "job.json").read_bytes()
    )
    assert saved.state == "cancelled"
    with pytest.raises(OSError) as caught:
        submit(jobs)
    assert caught.value.errno == errno.EBADF


def test_short_profile_job_is_retained_and_bound(jobs):
    static = terminal(jobs, submit(jobs).job_id)
    source = (
        jobs.workspace.kind_directory(static.binding.kind)
        / static.binding.generation_id
        / "report.json"
    )
    request = CollectionRequest(
        operation="profile",
        source_report=str(source.relative_to(jobs.planner.root)),
        workload="sample.py",
        workspace=".code-analysis",
        allow_execution=True,
    )
    job = jobs.submit(request, jobs.planner.preview(request), CollectionExecutionLimits())
    finished = terminal(jobs, job.job_id)
    assert finished.state in {"completed", "partial"}, finished.reason
    assert finished.binding.artifact_name == "profile.json"
    assert jobs.result(job.job_id).report.frames


def test_unexpected_worker_exception_is_terminal_failure(jobs, monkeypatch):
    def failure(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr("code_analysis.collection_jobs.execute_collection", failure)
    job = terminal(jobs, submit(jobs).job_id)
    assert job.state == "failed" and job.status == errno.EFAULT
    assert job.receipt is None and job.binding is None


def test_failed_capture_write_does_not_publish_success(jobs, monkeypatch):
    original = jobs._atomic

    def fail_stream(fd, name, payload):
        if name == "stdout":
            raise OSError(errno.ENOSPC, "synthetic full disk")
        original(fd, name, payload)

    monkeypatch.setattr(jobs, "_atomic", fail_stream)
    job = terminal(jobs, submit(jobs).job_id)
    assert job.state == "failed" and job.status == errno.ENOSPC
    assert job.binding is None


def test_generation_binding_failure_retains_diagnostic_capture(jobs, monkeypatch):
    def fail_binding(*args, **kwargs):
        raise OSError(errno.EBADMSG, "synthetic mismatch")

    monkeypatch.setattr(
        "code_analysis.collection_jobs.discover_collection_generation", fail_binding
    )
    job = terminal(jobs, submit(jobs).job_id)
    assert job.state == "failed" and job.status == errno.EBADMSG
    assert job.binding is None and job.receipt.report_validated
    assert jobs.result(job.job_id).report is not None


def test_thread_start_failure_leaves_a_durable_failure(jobs, monkeypatch):
    def fail_start(thread):
        raise RuntimeError("synthetic thread failure")

    monkeypatch.setattr("code_analysis.collection_jobs.Thread.start", fail_start)
    with pytest.raises(RuntimeError):
        submit(jobs)
    assert jobs.list()[0].state == "failed"
    assert jobs.list()[0].status == errno.EAGAIN


def test_actual_orphan_stream_bytes_count_toward_storage_bound(jobs):
    job = terminal(jobs, submit(jobs).job_id)
    # Sparse file: no large allocation, but represents retained local byte usage.
    with (jobs.directory / job.job_id / ".interrupted.tmp").open("wb") as artifact:
        artifact.truncate(1024 * 1024 * 1024)
    with pytest.raises(OSError) as caught:
        submit(jobs)
    assert caught.value.errno == errno.ENOSPC


def test_job_metadata_bound_fails_before_launch(jobs):
    request = CollectionRequest(
        target="sample.py",
        workspace=".code-analysis",
        exclude=("a" * 4096,) * 30,
    )
    with pytest.raises(OSError) as caught:
        jobs.submit(request, jobs.planner.preview(request), CollectionExecutionLimits())
    assert caught.value.errno == errno.ENODATA
    assert jobs.list() == ()


def test_discovery_reports_malformed_retained_identity(jobs):
    job = terminal(jobs, submit(jobs).job_id)
    result = jobs.result(job.job_id)
    (jobs.workspace.kind_directory(job.binding.kind) / "invalid-generation").mkdir()
    with pytest.raises(OSError) as caught:
        discover_collection_generation(result, jobs.workspace.root)
    assert caught.value.errno == errno.EBADMSG


def test_storage_reservation_includes_terminal_metadata(jobs, monkeypatch):
    available_for_streams = 1024 * 1024 * 1024 - 2 * CollectionExecutionLimits().stream_bytes
    monkeypatch.setattr(jobs, "_retained_bytes", lambda records: available_for_streams)
    with pytest.raises(OSError) as caught:
        submit(jobs)
    assert caught.value.errno == errno.ENOSPC
    assert jobs.list() == ()


def test_cli_log_is_hash_checked_and_bounded(jobs):
    job = terminal(jobs, submit(jobs).job_id)
    payload = b"<script>not markup</script>" * 1000
    path = jobs.directory / job.job_id / "stderr"
    path.write_bytes(payload)
    receipt = job.receipt.model_copy(
        update={"stderr_bytes": len(payload), "stderr_sha256": hashlib.sha256(payload).hexdigest()}
    )
    jobs._write(jobs._updated(job, receipt=receipt))
    log = jobs.log(job.job_id)
    assert log.shown_bytes == 16_384
    assert log.omitted_bytes + log.shown_bytes == len(payload)
    assert log.text == payload[-16_384:].decode()
    path.write_bytes(b"changed")
    with pytest.raises(OSError) as caught:
        jobs.log(job.job_id)
    assert caught.value.errno == errno.EBADMSG
