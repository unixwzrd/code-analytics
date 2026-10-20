from __future__ import annotations

import errno
import hashlib
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.cli import ExitCode, main
from code_analysis.config import AnalysisConfig
from code_analysis.profile_models import ProfileReport, ResourceObservation
from code_analysis.profiling import (
    build_profile_diff,
    load_profile_json,
    profile_workload,
    render_profile_graph,
    render_profile_json,
    render_profile_markdown,
    render_profile_tsv,
    render_static_runtime_overlay,
)
from code_analysis.project import discover_context
from code_analysis.reports.json_report import render_json
from code_analysis.service import scan

GOLDEN = Path(__file__).parent / "golden"


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    module = root / "workload.py"
    module.write_text(
        "def recursive(value):\n"
        "    if value <= 0:\n"
        "        return 1\n"
        "    return recursive(value - 1) + 1\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    print(recursive(4))\n",
        encoding="utf-8",
    )
    return root, module


def _source_report(root: Path):
    return scan(discover_context(root, AnalysisConfig()))


def test_profile_requires_explicit_authorization(tmp_path: Path, capsys) -> None:
    root, script = _project(tmp_path)
    source_report = tmp_path / "report.json"
    source_report.write_text(render_json(_source_report(root)), encoding="utf-8")
    status = main(
        [
            "profile",
            "--source-report",
            str(source_report),
            "--cwd",
            str(root),
            "--",
            sys.executable,
            str(script),
        ]
    )
    captured = capsys.readouterr()
    assert status == ExitCode.EPERM
    assert captured.out == ""
    assert "requires --allow-execution" in captured.err
    assert not (root / ".code-analysis").exists()


def test_profile_captures_recursion_edges_resources_and_local_streams(
    tmp_path: Path, capsys
) -> None:
    root, script = _project(tmp_path)
    source_report = tmp_path / "report.json"
    source_report.write_text(render_json(_source_report(root)), encoding="utf-8")
    status = main(
        [
            "profile",
            "--source-report",
            str(source_report),
            "--allow-execution",
            "--cwd",
            str(root),
            "--repeat",
            "3",
            "--quiet",
            "--",
            sys.executable,
            str(script),
        ]
    )
    captured = capsys.readouterr()
    assert status == ExitCode.SUCCESS
    assert captured.out == ""
    assert "untrusted project code" in captured.err
    latest = root / ".code-analysis" / "generations" / "profile" / "latest"
    report = load_profile_json((latest / "profile.json").read_text(encoding="utf-8"))
    recursive = next(item for item in report.frames if item.function == "recursive")
    assert recursive.path == "workload.py"
    assert recursive.total_calls >= recursive.primitive_calls
    assert any(item.callee_frame_id == recursive.frame_id for item in report.runtime_edges)
    assert report.repetitions == 3
    assert len(report.resources) == 3
    assert len(report.statistics) == len(report.frames)
    assert (latest / "stdout-001.bin").read_text(encoding="utf-8") == "5\n"
    provider = next(item for item in report.providers if item.provider_id == "stdlib-cprofile")
    streams = b"".join((latest / f"stdout-{index:03d}.bin").read_bytes() for index in range(1, 4))
    assert provider.stdout_sha256 == hashlib.sha256(streams).hexdigest()
    portable = (latest / "portable-001.prof").read_bytes()
    assert str(tmp_path).encode() not in portable
    assert "/Users/" not in render_profile_json(report)
    assert render_profile_markdown(report).startswith("# Runtime profile evidence")
    assert render_profile_tsv(report).startswith("frame_id\tpath")
    assert render_profile_graph(report, mermaid=False).startswith("digraph runtime_calls")
    assert render_profile_graph(report, mermaid=True).startswith("flowchart LR")
    static_report = _source_report(root)
    assert render_static_runtime_overlay(report, static_report, mermaid=False).startswith(
        "digraph static_runtime_overlay"
    )
    assert render_static_runtime_overlay(report, static_report, mermaid=True).startswith(
        "flowchart LR"
    )
    capsys.readouterr()
    assert (
        main(
            [
                "profile-report",
                str(latest / "raw-001.prof"),
                "--format",
                "json",
            ]
        )
        == ExitCode.ENODATA
    )
    standalone = capsys.readouterr()
    assert '"completeness": "partial"' in standalone.out
    assert str(tmp_path) not in standalone.out
    assert "unbound to source hashes" in standalone.out
    assert (
        main(
            [
                "profile-report",
                str(latest / "raw-001.prof"),
                "--source-report",
                str(source_report),
                "--source-root",
                str(root),
                "--format",
                "json-compact",
            ]
        )
        == ExitCode.ENODATA
    )
    bound = capsys.readouterr()
    assert '"path":"workload.py"' in bound.out


def test_profile_expected_errno_and_unexpected_status(tmp_path: Path) -> None:
    root, script = _project(tmp_path)
    script.write_text(f"raise SystemExit({errno.ENODATA})\n", encoding="utf-8")
    report = _source_report(root)
    expected = profile_workload(
        report,
        (sys.executable, str(script)),
        cwd=root,
        config=AnalysisConfig(),
        expected_status=errno.ENODATA,
        expected_status_name="ENODATA",
        repeat=1,
        timeout_seconds=5,
        environment_additions=(),
    )
    assert not expected.unexpected_status
    assert expected.report.observed_statuses == (errno.ENODATA,)
    unexpected = profile_workload(
        report,
        (sys.executable, str(script)),
        cwd=root,
        config=AnalysisConfig(),
        expected_status=0,
        expected_status_name="SUCCESS",
        repeat=1,
        timeout_seconds=5,
        environment_additions=(),
    )
    assert unexpected.unexpected_status


def test_profile_timeout_and_command_environment_guards(tmp_path: Path) -> None:
    root, script = _project(tmp_path)
    script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    report = _source_report(root)
    capture = profile_workload(
        report,
        (sys.executable, str(script)),
        cwd=root,
        config=AnalysisConfig(),
        expected_status=0,
        expected_status_name="SUCCESS",
        repeat=1,
        timeout_seconds=0.1,
        environment_additions=(),
    )
    assert capture.timed_out
    assert capture.report.observed_statuses == (errno.ETIMEDOUT,)
    with pytest.raises(OSError) as exc_info:
        profile_workload(
            report,
            ("/bin/echo", "no"),
            cwd=root,
            config=AnalysisConfig(),
            expected_status=0,
            expected_status_name="SUCCESS",
            repeat=1,
            timeout_seconds=1,
            environment_additions=(),
        )
    assert exc_info.value.errno == errno.EINVAL
    with pytest.raises(OSError) as exc_info:
        profile_workload(
            report,
            (sys.executable, str(script)),
            cwd=root,
            config=AnalysisConfig(),
            expected_status=0,
            expected_status_name="SUCCESS",
            repeat=1,
            timeout_seconds=1,
            environment_additions=(("SECRET_VALUE", "hidden"),),
        )
    assert exc_info.value.errno == errno.EPERM


def test_profile_stream_bound_is_enforced_and_bytes_are_retained(
    tmp_path: Path, monkeypatch
) -> None:
    import code_analysis.profiling as profiling

    retained: list[tuple[bytes, bool, int]] = []

    class ObservedProcess(profiling.OwnedRuntimeProcess):
        def close(self) -> None:
            retained.append((bytes(self.stdout), self.overflow, self.stream_limit))
            super().close()

    monkeypatch.setattr(profiling, "OwnedRuntimeProcess", ObservedProcess)
    root, script = _project(tmp_path)
    script.write_text("import sys; sys.stdout.write('x' * (9 * 1024 * 1024)); sys.stdout.flush()\n")
    with pytest.raises(OSError) as exc_info:
        profile_workload(
            _source_report(root),
            (sys.executable, str(script)),
            cwd=root,
            config=AnalysisConfig(),
            expected_status=0,
            expected_status_name="SUCCESS",
            repeat=1,
            # Exercise overflow, not a competing short deadline under suite load.
            # Production keeps its 8 MiB cap; timeout behavior has separate tests.
            timeout_seconds=30,
            environment_additions=(),
        )
    assert exc_info.value.errno == errno.ENODATA
    assert len(retained) == 1
    stdout, overflow, limit = retained[0]
    assert limit == 8 * 1024 * 1024
    assert overflow
    assert stdout == b"x" * limit


def test_profile_python_console_script_shebang_is_supported(tmp_path: Path) -> None:
    root, _script = _project(tmp_path)
    console = root / "tool"
    console.write_text(f"#!{sys.executable}\nprint('console')\n", encoding="utf-8")
    console.chmod(0o755)
    capture = profile_workload(
        _source_report(root),
        (str(console),),
        cwd=root,
        config=AnalysisConfig(),
        expected_status=0,
        expected_status_name="SUCCESS",
        repeat=1,
        timeout_seconds=5,
        environment_additions=(),
    )
    assert capture.report.observed_statuses == (0,)
    assert capture.local_artifacts["stdout-001.bin"] == b"console\n"


def test_profile_without_psutil_discloses_direct_child_ownership_limit(
    tmp_path: Path, monkeypatch
) -> None:
    root, script = _project(tmp_path)
    monkeypatch.setattr("code_analysis.profiling._psutil_available", lambda: False)
    capture = profile_workload(
        _source_report(root),
        (sys.executable, str(script)),
        cwd=root,
        config=AnalysisConfig(),
        expected_status=0,
        expected_status_name="SUCCESS",
        repeat=1,
        timeout_seconds=5,
        environment_additions=(),
    )
    assert any("no proven descendant ownership" in item for item in capture.report.limitations)
    provider = next(
        item for item in capture.report.providers if item.provider_id == "psutil-resources"
    )
    assert provider.state.value == "unavailable"


def test_profile_diff_requires_bound_source_and_workload(tmp_path: Path) -> None:
    root, script = _project(tmp_path)
    capture = profile_workload(
        _source_report(root),
        (sys.executable, str(script)),
        cwd=root,
        config=AnalysisConfig(),
        expected_status=0,
        expected_status_name="SUCCESS",
        repeat=1,
        timeout_seconds=5,
        environment_additions=(),
    )
    # The original runner did not capture independent PID/environment proof.
    with pytest.raises(OSError) as missing_identity:
        build_profile_diff(capture.report, capture.report)
    assert missing_identity.value.errno == errno.EINVAL
    diff = build_profile_diff(capture.report, capture.report, diagnostic_only=True)
    assert all(item.absolute_total_seconds_delta == 0 for item in diff.records)
    incompatible = capture.report.model_copy(update={"workload_identity_sha256": "f" * 64})
    with pytest.raises(OSError) as exc_info:
        build_profile_diff(capture.report, incompatible)
    assert exc_info.value.errno == errno.EINVAL


def test_profile_models_reject_extra_bool_and_repetition_mismatch(tmp_path: Path) -> None:
    root, script = _project(tmp_path)
    report = profile_workload(
        _source_report(root),
        (sys.executable, str(script)),
        cwd=root,
        config=AnalysisConfig(),
        expected_status=0,
        expected_status_name="SUCCESS",
        repeat=1,
        timeout_seconds=5,
        environment_additions=(),
    ).report
    payload = json.loads(render_profile_json(report))
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        ProfileReport.model_validate(payload)
    with pytest.raises(ValidationError):
        ResourceObservation(
            repetition=True,
            wall_seconds=0.0,
            user_cpu_seconds=0.0,
            system_cpu_seconds=0.0,
        )
    with pytest.raises(ValidationError):
        report.model_copy(update={"observed_statuses": ()}).model_validate(
            report.model_copy(update={"observed_statuses": ()}).model_dump()
        )


def test_profile_goldens_are_authoritative_and_portable() -> None:
    json_text = (GOLDEN / "phase4-profile-report.json").read_text(encoding="utf-8")
    report = load_profile_json(json_text)
    expected = {
        "phase4-profile-report.json": render_profile_json(report),
        "phase4-profile-report.md": render_profile_markdown(report),
        "phase4-profile-hotspots.tsv": render_profile_tsv(report),
        "phase4-profile-call-graph.dot": render_profile_graph(report, mermaid=False),
        "phase4-profile-call-graph.mmd": render_profile_graph(report, mermaid=True),
    }
    for name, rendered in expected.items():
        assert rendered == (GOLDEN / name).read_text(encoding="utf-8")
        assert "/Users/" not in rendered
        assert "/Volumes/" not in rendered
