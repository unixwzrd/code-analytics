"""Runtime CLI and lifecycle checks using only local synthetic programs."""

from __future__ import annotations

import errno
import hashlib
import json
import marshal
import sys
from pathlib import Path

import pytest

from code_analysis.cli import main
from code_analysis.config import AnalysisConfig
from code_analysis.models import Completeness
from code_analysis.profile_ingestion import ingest_profiles
from code_analysis.profiling import render_profile_json
from code_analysis.runtime_artifacts import publish_runtime_report
from code_analysis.runtime_comparison import compare_runtime_profiles
from code_analysis.runtime_models import ProcessProofState, RuntimeManifest, RuntimeSelection
from code_analysis.runtime_runner import run_runtime
from code_analysis.runtime_spec import (
    ArgumentRole,
    RunnerMode,
    RuntimeAnalysisConfig,
    RuntimeArgument,
    RuntimeCommand,
    RuntimeRunSpec,
)


def _profile(tmp_path: Path):
    path = tmp_path / "synthetic.prof"
    path.write_bytes(
        marshal.dumps(
            {
                ("module.py", 1, "work"): (1, 1, 0.1, 0.2, {}),
                ("module.py", 10, "other"): (1, 1, 0.2, 0.2, {}),
            }
        )
    )
    return path


def _spec(tmp_path: Path, script: str = "print('worked')\n") -> RuntimeRunSpec:
    root = tmp_path.resolve()
    target = root / "subject.py"
    target.write_text(script, encoding="utf-8")
    command = RuntimeCommand(
        arguments=(
            RuntimeArgument(
                role=ArgumentRole.EXECUTABLE,
                value=sys.executable,
                semantic_identity="qualification-python",
            ),
            RuntimeArgument(
                role=ArgumentRole.SCRIPT, value=str(target), semantic_identity="subject.py"
            ),
        )
    )
    return RuntimeRunSpec(
        mode=RunnerMode.PYTHON_MODULE,
        repo=root,
        cwd=root,
        output=root / "report",
        profile_path=root / "raw.prof",
        metadata_path=root / "sidecar.json",
        subject=command,
        limits=RuntimeAnalysisConfig(total_seconds=5.0, graceful_seconds=0.1, forced_seconds=0.5),
    )


def test_runtime_authorization_precedes_reads_and_writes(tmp_path: Path, capsys) -> None:
    assert main(["runtime", "run", "--spec", str(tmp_path / "does-not-exist.json")]) == errno.EPERM
    assert not list(tmp_path.iterdir())
    assert "requires --allow-execution" in capsys.readouterr().err


def test_new_runtime_offline_cli_and_source_free_render(tmp_path: Path, capsys) -> None:
    raw = _profile(tmp_path)
    assert main(["runtime", "ingest", "--profile", str(raw)]) == errno.ENODATA
    captured = capsys.readouterr()
    report = tmp_path / "only.json"
    report.write_text(captured.out, encoding="utf-8")
    raw.unlink()
    destination = tmp_path.resolve() / "rendered"
    assert (
        main(["runtime", "render", str(report), "--output", str(destination), "--max-nodes", "1"])
        == errno.ENODATA
    )
    manifest = RuntimeManifest.model_validate_json((destination / "manifest.json").read_bytes())
    assert manifest.original_nodes == 2
    assert manifest.retained_nodes == 1
    assert "rankdir=TB" in (destination / "runtime-callgraph.dot").read_text()
    for item in manifest.artifacts:
        if item.sha256:
            assert hashlib.sha256((destination / item.path).read_bytes()).hexdigest() == item.sha256
    assert len(json.loads((destination / "runtime-summary.json").read_text())["frames"]) == 2
    assert main(["runtime", "render", str(report), "--output", str(destination)]) == errno.EEXIST


def test_import_retention_preserves_evidence_and_does_not_scan(tmp_path, capsys, monkeypatch):
    from code_analysis.project import discover_context
    from code_analysis.workspace import (
        GenerationKind,
        list_generations,
        prepare_workspace,
        read_generation,
    )

    workspace = prepare_workspace(discover_context(tmp_path, AnalysisConfig()))
    raw = _profile(tmp_path)
    before = raw.read_bytes()
    command = ["runtime", "ingest", "--profile", str(raw)]
    assert main(command) == errno.ENODATA
    expected = capsys.readouterr().out
    monkeypatch.setattr(
        "code_analysis.project.discover_context",
        lambda *a, **kw: pytest.fail("import rescanned source"),
    )
    assert main([*command, "--workspace", str(workspace.root)]) == errno.ENODATA
    actual = capsys.readouterr().out
    assert actual == expected
    assert raw.read_bytes() == before
    generations = list_generations(workspace, kind=GenerationKind.PROFILE)
    assert len(generations) == 1
    manifest = read_generation(workspace, GenerationKind.PROFILE, generations[0].generation_id)
    assert manifest.command[:3] == ("code-analysis", "runtime", "ingest")
    stored = (
        workspace.kind_directory(GenerationKind.PROFILE) / manifest.generation_id / "profile.json"
    )
    assert json.loads(stored.read_text()) == json.loads(expected)
    assert manifest.portable_identity_sha256 == json.loads(expected)["profile_identity_sha256"]
    assert "--profile" in manifest.command and "--workspace" in manifest.command
    from code_analysis.profile_runs import profile_catalog

    assert profile_catalog(workspace).records[0].generation_id == manifest.generation_id


def test_import_destination_errors_precede_profile_reads(tmp_path):
    missing = tmp_path / "missing.prof"
    assert (
        main(
            [
                "runtime",
                "ingest",
                "--profile",
                str(missing),
                "--workspace",
                str(tmp_path / "workspace"),
                "--output",
                str(tmp_path / "output"),
            ]
        )
        == errno.EINVAL
    )
    assert not list(tmp_path.iterdir())
    assert (
        main(
            [
                "runtime",
                "ingest",
                "--profile",
                str(missing),
                "--workspace",
                str(tmp_path / "workspace"),
            ]
        )
        != 0
    )
    assert not list(tmp_path.iterdir())


def test_missing_graphviz_is_explicit(tmp_path: Path, monkeypatch) -> None:
    report, _ = ingest_profiles((_profile(tmp_path),))
    monkeypatch.setattr("code_analysis.runtime_artifacts.shutil.which", lambda _: None)
    manifest = publish_runtime_report(report, tmp_path.resolve() / "output")
    unavailable = [item for item in manifest.artifacts if item.status == "unavailable"]
    assert len(unavailable) == 3
    assert all(item.sha256 is None for item in unavailable)
    assert manifest.completeness == Completeness.PARTIAL


def test_comparison_requires_identity_not_just_equal_unknowns(tmp_path: Path) -> None:
    report, _ = ingest_profiles((_profile(tmp_path),))
    with pytest.raises(OSError) as error:
        compare_runtime_profiles(report, report)
    assert error.value.errno == errno.EINVAL
    comparison = compare_runtime_profiles(report, report, diagnostic_only=True)
    assert not comparison.claim_eligible
    assert any(item.outcome == "unresolved" for item in comparison.decisions)
    reduced = report.model_copy(update={"frames": report.frames[:1], "runtime_edges": ()})
    changed = compare_runtime_profiles(report, reduced, diagnostic_only=True)
    assert any(
        item.candidate_total_seconds is None and item.absolute_total_seconds_delta is None
        for item in changed.records
    )


def test_managed_finite_subject_serializes_owned_profile(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.ENODATA  # no static source binding supplied
    assert result.report.runtime.process.state == ProcessProofState.VERIFIED
    assert result.report.runtime.process.serialization_complete
    assert result.report.runtime.process.subject_pid != __import__("os").getpid()
    assert result.report.observed_statuses == (0,)
    assert (result.local_directory / "subject.stdout").read_bytes() == b"worked\n"
    assert (spec.output / "runtime-summary.json").is_file()
    assert "/Users/" not in render_profile_json(result.report)


def test_managed_failure_preserves_errno_and_failure_report(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "raise SystemExit(7)\n")
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.EPROTO
    assert result.report.observed_statuses == (7,)
    assert result.report.completeness == Completeness.FAILED
    assert (spec.output / "manifest.json").is_file()


def test_managed_timeout_has_no_successful_empty_evidence(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "import time\ntime.sleep(30)\n")
    spec = spec.model_copy(
        update={
            "limits": RuntimeAnalysisConfig(
                total_seconds=0.2, graceful_seconds=0.1, forced_seconds=0.5
            )
        }
    )
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.ETIMEDOUT
    assert result.report.completeness == Completeness.FAILED
    assert result.report.runtime.process.state != ProcessProofState.VERIFIED


def test_invalid_render_selection_is_einval(tmp_path: Path) -> None:
    assert (
        main(
            [
                "runtime",
                "render",
                str(tmp_path / "missing"),
                "--output",
                str(tmp_path / "out"),
                "--max-nodes",
                "0",
            ]
        )
        == errno.EINVAL
    )


def test_runtime_config_does_not_change_static_identity() -> None:
    ordinary = AnalysisConfig()
    changed = ordinary.model_copy(update={"runtime_analysis": RuntimeAnalysisConfig(max_frames=5)})
    assert ordinary.identity() == changed.identity()
    assert ordinary.phase_three_identity() == changed.phase_three_identity()


def test_saved_runtime_goldens_are_authoritative() -> None:
    from code_analysis.profiling import load_profile_json, render_profile_markdown
    from code_analysis.projections.runtime import hotspot_svg, runtime_dot, runtime_table

    golden = Path(__file__).parent / "golden"
    report = load_profile_json((golden / "runtime-offline-summary.json").read_text())
    artifacts = {
        "runtime-offline-summary.json": render_profile_json(report, compact=True),
        "runtime-offline-summary.md": render_profile_markdown(report),
        "runtime-offline-functions.tsv": runtime_table(report, kind="functions", maximum=500),
        "runtime-offline-callgraph.dot": runtime_dot(report, RuntimeSelection()),
        "runtime-offline-hotspots.svg": hotspot_svg(report, 500),
    }
    for name, rendered in artifacts.items():
        assert rendered == (golden / name).read_text()
        assert "/Users/" not in rendered


def _python_command(expression: str, *, timeout: float = 2.0) -> RuntimeCommand:
    return RuntimeCommand(
        arguments=(
            RuntimeArgument(
                role=ArgumentRole.EXECUTABLE, value=sys.executable, semantic_identity="python"
            ),
            RuntimeArgument(role=ArgumentRole.ARGUMENT, value="-c"),
            RuntimeArgument(role=ArgumentRole.ARGUMENT, value=expression),
        ),
        timeout_seconds=timeout,
    )


def test_long_lived_subject_not_driver_owns_profile(tmp_path: Path) -> None:
    from code_analysis.runtime_spec import ReadinessProbe

    spec = _spec(
        tmp_path,
        "import signal,time\nfrom pathlib import Path\n"
        "running=True\ndef stop(*args):\n global running\n running=False\n"
        "def serve_request():\n Path('response').write_text('ok')\n"
        "signal.signal(signal.SIGTERM,stop)\nPath('ready').touch()\n"
        "while running:\n"
        " if Path('request').exists():\n  serve_request()\n  Path('request').unlink()\n"
        " time.sleep(.01)\n",
    )
    readiness = ReadinessProbe(
        command=_python_command(
            "from pathlib import Path; raise SystemExit(0 if Path('ready').exists() else 1)"
        )
    )
    workload = _python_command(
        "from pathlib import Path; import time; Path('request').touch();\n"
        "while not Path('response').exists(): time.sleep(.01)"
    )
    spec = spec.model_copy(
        update={
            "readiness": readiness,
            "workloads": (workload,),
            "limits": RuntimeAnalysisConfig(
                total_seconds=8.0, readiness_seconds=3.0, graceful_seconds=1.0, forced_seconds=0.5
            ),
        }
    )
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.ENODATA
    assert result.report.runtime.process.state == ProcessProofState.VERIFIED
    assert any(frame.function == "serve_request" for frame in result.report.frames)
    assert result.report.observed_statuses == (0,)
    assert (tmp_path / "response").read_text() == "ok"


def test_primary_failure_is_not_replaced_by_cleanup_timeout(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "raise SystemExit(7)\n")
    spec = spec.model_copy(
        update={"cleanup": (_python_command("import time; time.sleep(20)", timeout=0.1),)}
    )
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.EPROTO
    assert any(
        event.state.value == "cleanup" and event.status == errno.ETIMEDOUT
        for event in result.report.runtime.lifecycle
    )


def test_stream_overflow_retains_bounded_local_bytes(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path, "import sys\nwhile True: sys.stdout.write('x'*65536); sys.stdout.flush()\n"
    )
    spec = spec.model_copy(
        update={
            "limits": RuntimeAnalysisConfig(
                total_seconds=2.0, max_stream_bytes=1024, graceful_seconds=0.1, forced_seconds=0.5
            )
        }
    )
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.ENODATA
    assert result.report.completeness == Completeness.FAILED
    assert (result.local_directory / "subject.stdout").stat().st_size == 1024


def test_application_managed_harness_pid_is_rejected(tmp_path: Path) -> None:
    import os

    spec = _spec(
        tmp_path,
        "import cProfile,json,os,psutil,hashlib\nfrom pathlib import Path\n"
        "p=cProfile.Profile(); p.runcall(sum,[1,2]); p.dump_stats('raw.prof')\n"
        "Path('sidecar.json').write_text(json.dumps(dict(subject_pid=os.getppid(),parent_pid=os.getppid(),"
        "start_identity=str(psutil.Process().create_time()),run_nonce=os.environ['CODE_ANALYSIS_RUN_NONCE'],"
        "collection_scope='subject-interpreter',serialization_complete=True,"
        "profile_sha256=hashlib.sha256(Path('raw.prof').read_bytes()).hexdigest())))\n",
    )
    spec = spec.model_copy(update={"mode": RunnerMode.APPLICATION_MANAGED})
    result = run_runtime(spec, allow_execution=True, config=AnalysisConfig())
    assert result.status == errno.EPROTO
    assert result.report.runtime.process.state != ProcessProofState.VERIFIED
    assert result.report.runtime.process.subject_pid != os.getpid()
