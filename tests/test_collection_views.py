from __future__ import annotations

import errno
import time

import pytest

from code_analysis.collection_commands import CollectionPlanner, CollectionRequest
from code_analysis.collection_execution import CollectionExecutionLimits
from code_analysis.collection_service import CollectionLaunch, CollectionSession
from code_analysis.collection_views import collection_index, collection_views


def finish(session, request):
    job = session.launch(
        CollectionLaunch(
            request=request,
            command=session.planner.preview(request),
            limits=CollectionExecutionLimits(),
            confirmed=True,
        )
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = session.selected().get(job.job_id)
        if job.state not in {"queued", "running"}:
            assert job.binding is not None, job.reason
            return job
        time.sleep(0.02)
    pytest.fail("synthetic collection did not finish")


@pytest.fixture
def collected(tmp_path):
    (tmp_path / "sample.py").write_text("def answer():\n    return 42\nanswer()\n")
    session = CollectionSession(CollectionPlanner(tmp_path))
    try:
        job = finish(session, CollectionRequest(target="sample.py", workspace=".code-analysis"))
        yield session, job, tmp_path
    finally:
        session.close()
        session.planner.close()


def test_static_result_index_without_source_and_tampered_generation(collected):
    session, job, root = collected
    (root / "sample.py").unlink()
    first = collection_index(session, job.job_id)
    second = collection_index(session, job.job_id)
    assert first.database == second.database
    assert first.manifest.runtime is None
    report = (
        root / ".code-analysis/generations/analysis" / job.binding.generation_id / "report.json"
    )
    report.write_text("{}")
    with pytest.raises(OSError) as exc:
        collection_index(session, job.job_id)
    assert exc.value.errno == errno.EBADMSG


def test_profile_index_source_binding_and_missing_source_report(collected, ordinary_report):
    from code_analysis.reports.json_report import canonical_json

    session, job, root = collected
    source = f".code-analysis/generations/analysis/{job.binding.generation_id}/report.json"
    profile = finish(
        session,
        CollectionRequest(
            operation="profile",
            source_report=source,
            workload="sample.py",
            allow_execution=True,
            workspace=".code-analysis",
        ),
    )
    (root / "sample.py").unlink()
    index = collection_index(session, profile.job_id)
    assert index.manifest.runtime is not None
    assert index.manifest.runtime.overview.source_matches
    assert collection_index(session, profile.job_id).database == index.database
    (root / source).write_text(canonical_json(ordinary_report))
    with pytest.raises(OSError) as exc:
        collection_index(session, profile.job_id)
    assert exc.value.errno == errno.EBADMSG
    (root / source).write_text("{")
    with pytest.raises(OSError) as exc:
        collection_views(session, profile.job_id)
    assert exc.value.errno == errno.EBADMSG
    (root / source).unlink()
    with pytest.raises(OSError):
        collection_views(session, profile.job_id)


def test_corrupt_derivative_does_not_fall_back_to_unverified_data(collected):
    session, job, _ = collected
    index = collection_index(session, job.job_id)
    index.database.write_bytes(b"malformed")
    with pytest.raises((OSError, ValueError)):
        collection_index(session, job.job_id)
