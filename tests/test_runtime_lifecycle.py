"""Synthetic lifecycle regressions; never execute a consumer application."""

from __future__ import annotations

import errno
import hashlib
import signal
import sys
import time
from pathlib import Path

import pytest
from test_runtime_commands import _python_command, _spec

from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness
from code_analysis.runtime_models import ProcessProofState, RuntimeStepObservation
from code_analysis.runtime_process import OwnedRuntimeProcess
from code_analysis.runtime_runner import _run_command, run_runtime
from code_analysis.runtime_spec import ReadinessProbe, RunnerMode


def test_exit_drains_all_stdout_and_detects_final_overflow(tmp_path: Path) -> None:
    for limit in (1048576, 256):
        process = OwnedRuntimeProcess(
            (sys.executable, "-c", "import sys; sys.stdout.write('x'*524288)"),
            cwd=tmp_path,
            environment={},
            stream_limit=limit,
        )
        try:
            if limit == 256:
                with pytest.raises(OSError) as error:
                    process.wait(time.monotonic() + 5)
                assert error.value.errno == errno.ENODATA
            else:
                assert process.wait(time.monotonic() + 5) == 0
                assert len(process.stdout) == 524288
        finally:
            process.shutdown(graceful=signal.SIGTERM, graceful_seconds=0.1, forced_seconds=0.5)
            process.close()


def test_subject_timeout_not_total_timeout(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "import time; time.sleep(60)")
    spec = spec.model_copy(
        update={
            "subject": spec.subject.model_copy(update={"timeout_seconds": 0.1}),
        }
    )
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.ETIMEDOUT
    assert result.report.completeness == Completeness.FAILED


def test_loopback_service_url_probe_and_separate_workload(tmp_path: Path) -> None:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    spec = _spec(
        tmp_path,
        "import signal\nfrom http.server import HTTPServer,BaseHTTPRequestHandler\n"
        "running=True\ndef stop(*a):\n global running\n running=False\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        " def log_message(self,*a): pass\n"
        f"server=HTTPServer(('127.0.0.1',{port}),Handler); server.timeout=.05\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "while running: server.handle_request()\nserver.server_close()\n",
    )
    spec = spec.model_copy(
        update={
            "readiness": ReadinessProbe(url=f"http://127.0.0.1:{port}/ready"),
            "workloads": (
                _python_command(
                    "from urllib.request import urlopen; "
                    f"assert urlopen('http://127.0.0.1:{port}/work',timeout=1).read()==b'ok'"
                ),
            ),
            "limits": spec.limits.model_copy(update={"graceful_seconds": 1.0}),
        }
    )
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.ENODATA
    assert result.report.runtime.process.state == ProcessProofState.VERIFIED
    assert any(frame.function == "do_GET" for frame in result.report.frames)
    assert result.report.observed_statuses == (0,)


def test_descendant_cleanup_is_owned_and_bounded(tmp_path: Path) -> None:
    import psutil

    process = OwnedRuntimeProcess(
        (
            sys.executable,
            "-c",
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
            "start_new_session=True); "
            "time.sleep(30)",
        ),
        cwd=tmp_path,
        environment={},
        stream_limit=1024,
    )
    children = ()
    try:
        deadline = time.monotonic() + 5
        while len(process.owned) < 2 and time.monotonic() < deadline:
            process.pump(0.01)
        children = tuple(pid for pid in process.owned if pid != process.process.pid)
        assert children
    finally:
        process.shutdown(graceful=signal.SIGTERM, graceful_seconds=0.1, forced_seconds=1.0)
        process.close()
    assert all(
        not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        for pid in children
    )


def test_forced_termination_is_failed_capture(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        "import signal,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); Path('ready').touch()\n"
        "while True: time.sleep(.01)\n",
    )
    spec = spec.model_copy(
        update={
            "readiness": ReadinessProbe(
                command=_python_command(
                    "from pathlib import Path; raise SystemExit(0 if Path('ready').exists() else 1)"
                )
            )
        }
    )
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.ETIMEDOUT
    assert result.report.completeness == Completeness.FAILED
    assert result.report.runtime.process.state != ProcessProofState.VERIFIED
    assert (
        next(step for step in result.report.runtime.steps if step.step == "subject").failure_status
        == errno.ETIMEDOUT
    )


def test_chatty_subject_drained_during_probe_and_workload(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        "import signal,time,sys\nfrom pathlib import Path\n"
        "running=True\ndef stop(*a):\n global running\n running=False\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "sys.stdout.write('x'*524288); sys.stdout.flush(); Path('ready').touch()\n"
        "while running:\n"
        " if Path('request').exists():\n"
        "  sys.stderr.write('y'*524288); sys.stderr.flush()\n"
        "  Path('response').touch(); Path('request').unlink()\n"
        " time.sleep(.01)\n",
    )
    probe = _python_command(
        "from pathlib import Path; import time\nwhile not Path('ready').exists(): time.sleep(.01)"
    )
    workload = _python_command(
        "from pathlib import Path; import time; Path('request').touch()\n"
        "while not Path('response').exists(): time.sleep(.01)"
    )
    spec = spec.model_copy(
        update={
            "readiness": ReadinessProbe(command=probe),
            "workloads": (workload,),
            "limits": spec.limits.model_copy(update={"graceful_seconds": 1.0}),
        }
    )
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.ENODATA  # unbound source, not timeout
    assert result.report.runtime.process.state == ProcessProofState.VERIFIED
    streams = {item.step: item for item in result.report.runtime.steps}
    assert streams["subject"].stdout_bytes == 524288
    assert streams["subject"].stderr_bytes == 524288
    assert streams["subject"].stdout_sha256 == hashlib.sha256(b"x" * 524288).hexdigest()
    assert streams["workload-0"].observed_status == 0
    assert streams["readiness-0"].observed_status == 0


def test_command_primary_failure_survives_shutdown_error(tmp_path: Path, monkeypatch) -> None:
    spec = _spec(tmp_path)
    original = OwnedRuntimeProcess.shutdown

    def shutdown(self, **kwargs):
        original(self, **kwargs)
        raise OSError(errno.ETIMEDOUT, "synthetic secondary failure")

    monkeypatch.setattr(OwnedRuntimeProcess, "shutdown", shutdown)
    observations: list[RuntimeStepObservation] = []
    with pytest.raises(OSError) as error:
        _run_command(
            _python_command("raise SystemExit(7)"),
            spec,
            {},
            time.monotonic() + 3,
            tmp_path,
            "workload-0",
            observations=observations,
        )
    assert error.value.errno == errno.EPROTO
    assert observations[0].failure_status == errno.EPROTO
    assert observations[0].shutdown_status == errno.ETIMEDOUT


def test_expired_deadline_never_launches_command(tmp_path: Path, monkeypatch) -> None:
    spec = _spec(tmp_path)
    monkeypatch.setattr(
        "code_analysis.runtime_runner.OwnedRuntimeProcess", lambda *a, **k: pytest.fail("launched")
    )
    with pytest.raises(OSError) as error:
        _run_command(_python_command("pass"), spec, {}, time.monotonic() - 1, tmp_path, "cleanup-0")
    assert error.value.errno == errno.ETIMEDOUT


def test_reused_pid_is_not_signalled(tmp_path: Path, monkeypatch) -> None:
    process = OwnedRuntimeProcess(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        cwd=tmp_path,
        environment={},
        stream_limit=1024,
    )
    try:
        process.owned[process.process.pid] = process.start - 10
        assert process.alive() == ()
        process.send(signal.SIGKILL)
        assert process.process.poll() is None
    finally:
        process.owned[process.process.pid] = process.start
        process.shutdown(graceful=signal.SIGTERM, graceful_seconds=0.1, forced_seconds=0.5)
        process.close()


def test_application_managed_positive_and_external_input_binding(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        "import cProfile,json,os,psutil,hashlib\nfrom pathlib import Path\n"
        "p=cProfile.Profile(); p.runcall(sum,[1,2]); p.dump_stats('raw.prof')\n"
        "Path('sidecar.json').write_text(json.dumps(dict(subject_pid=os.getpid(),parent_pid=os.getppid(),"
        "start_identity=str(psutil.Process().create_time()),run_nonce=os.environ['CODE_ANALYSIS_RUN_NONCE'],"
        "collection_scope='subject-interpreter',serialization_complete=True,"
        "profile_sha256=hashlib.sha256(Path('raw.prof').read_bytes()).hexdigest())))\n",
    )
    source = tmp_path / "input.json"
    source.write_bytes(b'{"value":1}')
    spec = spec.model_copy(
        update={"mode": RunnerMode.APPLICATION_MANAGED, "external_inputs": (source,)}
    )
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.ENODATA
    assert result.report.runtime.process.state == ProcessProofState.VERIFIED
    assert result.report.runtime.collection.external_artifacts == (
        hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    assert {item.kind for item in result.report.runtime.artifacts} == {
        "metadata",
        "external",
        "profile",
    }


def test_external_input_change_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    source.write_bytes(b"{}")
    spec = _spec(tmp_path, "from pathlib import Path; Path('input.json').write_text('changed')")
    spec = spec.model_copy(update={"external_inputs": (source,)})
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.EBADMSG
    assert result.report.completeness == Completeness.FAILED


def test_missing_psutil_direct_child_fallback_and_managed_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    import builtins

    original = builtins.__import__

    def without_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("synthetic missing optional telemetry")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_psutil)
    command = (sys.executable, "-c", "print('ok')")
    with pytest.raises(OSError) as error:
        OwnedRuntimeProcess(command, cwd=tmp_path, environment={}, stream_limit=1024)
    assert error.value.errno == errno.ENODATA
    process = OwnedRuntimeProcess(
        command, cwd=tmp_path, environment={}, stream_limit=1024, require_identity=False
    )
    try:
        assert process.wait(time.monotonic() + 5) == 0
        assert process.start is None
        assert bytes(process.stdout) == b"ok\n"
        assert process.resources().user_cpu_seconds is None
        assert process.resources().peak_rss_bytes is None
    finally:
        process.shutdown(graceful=signal.SIGTERM, graceful_seconds=0.1, forced_seconds=0.5)
        process.close()


def test_no_cleanup_launch_after_total_deadline(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "import time; time.sleep(30)")
    spec = spec.model_copy(
        update={
            "limits": spec.limits.model_copy(update={"total_seconds": 0.1}),
            "cleanup": (_python_command("from pathlib import Path; Path('unexpected').touch()"),),
        }
    )
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.ETIMEDOUT
    assert not (tmp_path / "unexpected").exists()
    assert any(
        item.state.value == "cleanup" and item.status == errno.ETIMEDOUT
        for item in result.report.runtime.lifecycle
    )


def test_observed_daemonization_is_rejected_and_known_child_stopped(tmp_path: Path) -> None:
    process = OwnedRuntimeProcess(
        (
            sys.executable,
            "-c",
            "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
            "'import time; time.sleep(30)'],start_new_session=True); time.sleep(30)",
        ),
        cwd=tmp_path,
        environment={},
        stream_limit=1024,
    )
    try:
        with pytest.raises(OSError) as error:
            process.wait(time.monotonic() + 5)
        assert error.value.errno == errno.EPROTO
        assert process.detached_descendants
    finally:
        process.shutdown(graceful=signal.SIGTERM, graceful_seconds=0.1, forced_seconds=0.5)
        assert process.alive() == ()
        process.close()
