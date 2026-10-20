"""Explicit cProfile collection, portable pstats normalization, and aggregation."""

from __future__ import annotations

import errno
import hashlib
import json
import pstats
import shutil
import signal
import statistics
import sys
import sysconfig
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from code_analysis import __version__
from code_analysis.config import AnalysisConfig
from code_analysis.models import (
    Completeness,
    Diagnostic,
    DiagnosticLevel,
    ProviderResult,
    ProviderState,
)
from code_analysis.profile_models import (
    FrameClassification,
    ProfileDiffReport,
    ProfileFrame,
    ProfileProducer,
    ProfileReport,
    ProfileStatistic,
    ResourceObservation,
    RuntimeCallEdge,
)
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json
from code_analysis.runtime_execution import controlled_environment
from code_analysis.runtime_process import OwnedRuntimeProcess

ProfileKey = tuple[str, int, str]
_PROFILE_RUNNER = (
    "import cProfile,runpy,sys\n"
    "output,kind,target,*args=sys.argv[1:]\n"
    "sys.argv=[target,*args]\n"
    "profiler=cProfile.Profile()\n"
    "try:\n"
    " profiler.enable()\n"
    " if kind=='module': runpy.run_module(target,run_name='__main__',alter_sys=True)\n"
    " else: runpy.run_path(target,run_name='__main__')\n"
    "finally:\n"
    " profiler.disable()\n"
    " profiler.dump_stats(output)\n"
)


@dataclass(frozen=True, slots=True)
class InternalFrame:
    original_key: ProfileKey
    portable_key: ProfileKey
    classification: FrameClassification
    subject_id: str | None
    primitive_calls: int
    total_calls: int
    total_seconds: float
    cumulative_seconds: float


@dataclass(frozen=True, slots=True)
class InternalEdge:
    caller: ProfileKey
    callee: ProfileKey
    primitive_calls: int
    total_calls: int
    total_seconds: float
    cumulative_seconds: float


@dataclass(frozen=True, slots=True)
class ProfileCapture:
    report: ProfileReport
    local_artifacts: dict[str, bytes]
    timed_out: bool
    unexpected_status: bool


def profile_workload(
    source_report: AnalysisReport,
    command: tuple[str, ...],
    *,
    cwd: Path,
    config: AnalysisConfig,
    expected_status: int,
    expected_status_name: str,
    repeat: int,
    timeout_seconds: float,
    environment_additions: tuple[tuple[str, str], ...],
) -> ProfileCapture:
    if repeat < 1 or repeat > config.phase_four_limits.max_repetitions:
        raise OSError(errno.EINVAL, "profile repetition count exceeds configured bounds")
    environment = controlled_environment(
        config.runtime.environment_allowlist, environment_additions
    )
    source_sha = hashlib.sha256(canonical_json(source_report).encode()).hexdigest()
    workload_identity = hashlib.sha256(
        json.dumps(
            {"command": command, "expected_status": expected_status, "repeat": repeat},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    repetition_frames: list[tuple[InternalFrame, ...]] = []
    repetition_edges: list[tuple[InternalEdge, ...]] = []
    resources: list[ResourceObservation] = []
    statuses: list[int] = []
    local: dict[str, bytes] = {}
    timed_out = False
    unexpected = False
    telemetry_available = _psutil_available()
    with tempfile.TemporaryDirectory(prefix="code-analysis-profile-") as temporary_text:
        temporary = Path(temporary_text)
        for repetition in range(1, repeat + 1):
            raw_path = temporary / f"raw-{repetition:03d}.prof"
            portable_path = temporary / f"portable-{repetition:03d}.prof"
            stdout_path = temporary / f"stdout-{repetition:03d}.bin"
            stderr_path = temporary / f"stderr-{repetition:03d}.bin"
            instrumented = _profile_command(command, raw_path)
            status, timeout, resource = _execute_profile(
                instrumented,
                cwd=cwd,
                environment=environment,
                timeout_seconds=timeout_seconds,
                repetition=repetition,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
            statuses.append(status)
            resources.append(resource)
            local[stdout_path.name] = stdout_path.read_bytes()
            local[stderr_path.name] = stderr_path.read_bytes()
            if raw_path.is_file():
                local[raw_path.name] = raw_path.read_bytes()
                frames, edges = _normalize_profile(
                    raw_path,
                    portable_path,
                    source_report=source_report,
                    source_root=cwd,
                )
                repetition_frames.append(frames)
                repetition_edges.append(edges)
                local[portable_path.name] = portable_path.read_bytes()
            else:
                repetition_frames.append(())
                repetition_edges.append(())
            if timeout:
                timed_out = True
                break
            if status != expected_status:
                unexpected = True
                break
    frames, frame_statistics, frame_truncated = _aggregate_frames(
        repetition_frames, config.phase_four_limits.max_profile_frames
    )
    known_frame_ids = {item.frame_id for item in frames}
    edges, edge_truncated = _aggregate_edges(
        repetition_edges,
        known_frame_ids,
        config.phase_four_limits.max_runtime_edges,
    )
    truncated = frame_truncated + edge_truncated
    diagnostics = (
        (
            Diagnostic(
                code="profile-bound-truncated",
                level=DiagnosticLevel.WARNING,
                message=f"profile bounds omitted {truncated} frames or runtime edges",
                provider="stdlib-cprofile",
            ),
        )
        if truncated
        else ()
    )
    completeness = (
        Completeness.FAILED
        if timed_out or unexpected
        else Completeness.PARTIAL if truncated else Completeness.COMPLETE
    )
    stdout_payload = b"".join(local[name] for name in sorted(local) if name.startswith("stdout-"))
    stderr_payload = b"".join(local[name] for name in sorted(local) if name.startswith("stderr-"))
    providers = (
        ProviderResult(
            provider_id="stdlib-cprofile",
            state=(
                ProviderState.TIMEOUT
                if timed_out
                else (
                    ProviderState.NONZERO
                    if unexpected
                    else ProviderState.PARTIAL if truncated else ProviderState.SUCCESS
                )
            ),
            executable="python-current-interpreter",
            version=sys.version.split()[0],
            exit_code=statuses[-1] if statuses else None,
            duration_ms=int(sum(item.wall_seconds for item in resources) * 1000),
            stdout_sha256=hashlib.sha256(stdout_payload).hexdigest(),
            stderr_sha256=hashlib.sha256(stderr_payload).hexdigest(),
            observations=("callers", "callees", "cumulative-time", "total-time"),
            diagnostics=diagnostics,
            completeness=completeness,
        ),
        ProviderResult(
            provider_id="psutil-resources",
            state=ProviderState.SUCCESS if telemetry_available else ProviderState.UNAVAILABLE,
            executable="python-package:psutil" if telemetry_available else None,
            version=_psutil_version(),
            duration_ms=0,
            observations=(
                (
                    ("cpu", "io", "owned-process-tree", "peak-rss")
                    if any(item.read_bytes is not None for item in resources)
                    else ("cpu", "owned-process-tree", "peak-rss")
                )
                if telemetry_available
                else ()
            ),
            completeness=Completeness.COMPLETE,
        ),
    )
    limitations = {
        "cProfile timings include profiler overhead and do not establish an optimization win.",
        "Child processes and native-extension work are absent unless executed in the "
        "profiled interpreter.",
        "Recursion is represented through primitive and total call counts.",
        "Runtime evidence describes only the supplied workload and is not exhaustive.",
    }
    if not telemetry_available:
        limitations.add(
            "psutil resource telemetry is unavailable; cProfile evidence remains valid, "
            "but this profiling mode has only direct-child cleanup and no proven "
            "descendant ownership."
        )
    if truncated:
        limitations.add("Profile frames or edges are truncated by configured bounds.")
    identity_payload = {
        "source": source_sha,
        "workload": workload_identity,
        "statuses": statuses,
        "frames": [item.model_dump(mode="json") for item in frames],
        "edges": [item.model_dump(mode="json") for item in edges],
        "statistics": [item.model_dump(mode="json") for item in frame_statistics],
    }
    identity = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    report = ProfileReport(
        tool_version=__version__,
        producer=ProfileProducer.CPROFILE,
        profile_identity_sha256=identity,
        source_report_sha256=source_sha,
        workload_identity_sha256=workload_identity,
        repetitions=len(statuses),
        expected_status=expected_status_name,
        observed_statuses=tuple(statuses),
        frames=frames,
        runtime_edges=edges,
        statistics=frame_statistics,
        resources=tuple(resources),
        providers=providers,
        diagnostics=diagnostics,
        completeness=completeness,
        limitations=tuple(sorted(limitations)),
    )
    local["profile.json"] = render_profile_json(report).encode()
    return ProfileCapture(report, local, timed_out, unexpected)


def render_profile_json(report: ProfileReport, *, compact: bool = False) -> str:
    return (
        json.dumps(
            report.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":") if compact else None,
            indent=None if compact else 2,
            allow_nan=False,
        )
        + "\n"
    )


def load_profile_json(text: str) -> ProfileReport:
    return ProfileReport.model_validate_json(text)


def normalize_profile_artifact(
    raw_path: Path,
    *,
    source_report: AnalysisReport | None,
    source_root: Path | None,
    config: AnalysisConfig,
) -> ProfileReport:
    from code_analysis.profile_ingestion import ingest_profiles

    report, _ = ingest_profiles(
        (raw_path,),
        config=config.runtime_analysis,
        repo=source_root,
        source_report=source_report,
    )
    return report


def render_profile_markdown(report: ProfileReport, *, limit: int = 25) -> str:
    hottest = sorted(
        report.frames, key=lambda item: (-(item.cumulative_seconds or 0.0), item.frame_id)
    )[:limit]
    lines = [
        "# Runtime profile evidence",
        "",
        f"- Schema: `{report.schema_name}@{report.schema_version}`",
        f"- Completeness: `{report.completeness.value}`",
        f"- Producer: `{report.producer.value}`",
        f"- Repetitions: {report.repetitions}",
        f"- Frames: {len(report.frames)}",
        f"- Runtime edges: {len(report.runtime_edges)}",
        "",
        "| Function | Class | Calls | Total seconds | Cumulative seconds |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| `{item.path}:{item.line}:{item.function}` | {item.classification.value} | "
        f"{_optional_text(item.total_calls, 'unavailable')} | {_seconds(item.total_seconds)} | "
        f"{_seconds(item.cumulative_seconds)} |"
        for item in hottest
    )
    lines.extend(
        [
            "",
            "## Runtime call edges",
            "",
            "| Caller | Callee | Representation | Calls | Primitive calls | "
            "Total seconds | Cumulative seconds |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    lines.extend(
        f"| `{item.caller_frame_id}` | `{item.callee_frame_id}` | "
        f"{item.caller_representation.value} | {item.total_calls} | "
        f"{_optional_text(item.primitive_calls, 'unavailable')} | "
        f"{_seconds(item.total_seconds)} | {_seconds(item.cumulative_seconds)} |"
        for item in report.runtime_edges[:limit]
    )
    lines.extend(
        [
            "",
            "## Repetition variance",
            "",
            "| Frame | Mean | Median | Minimum | Maximum | Stddev | CV |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    lines.extend(
        f"| `{item.frame_id}` | {item.total_seconds_mean:.9f} | "
        f"{item.total_seconds_median:.9f} | {item.total_seconds_minimum:.9f} | "
        f"{item.total_seconds_maximum:.9f} | "
        f"{item.total_seconds_standard_deviation:.9f} | "
        f"{_optional_text(item.total_seconds_coefficient_of_variation, 'n/a')} |"
        for item in report.statistics[:limit]
    )
    lines.extend(
        [
            "",
            "## Process resources",
            "",
            "| Repetition | Wall | User CPU | System CPU | Peak RSS | Processes |",
            "| ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    lines.extend(
        f"| {item.repetition} | {_seconds(item.wall_seconds)} | "
        f"{_seconds(item.user_cpu_seconds)} | "
        f"{_seconds(item.system_cpu_seconds)} | "
        f"{_optional_text(item.peak_rss_bytes, 'unavailable')} | "
        f"{_optional_text(item.maximum_owned_processes, 'unavailable')} |"
        for item in report.resources
    )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in report.limitations)
    return "\n".join(lines) + "\n"


def render_profile_tsv(report: ProfileReport) -> str:
    lines = ["frame_id\tpath\tline\tfunction\tclass\tcalls\ttotal_seconds\tcumulative_seconds"]
    lines.extend(
        "\t".join(
            (
                item.frame_id,
                item.path,
                str(item.line),
                item.function.replace("\t", " "),
                item.classification.value,
                str(item.total_calls),
                _seconds(item.total_seconds),
                _seconds(item.cumulative_seconds),
            )
        )
        for item in sorted(report.frames, key=lambda value: value.frame_id)
    )
    return "\n".join(lines) + "\n"


def render_profile_graph(report: ProfileReport, *, mermaid: bool) -> str:
    frames = {item.frame_id: item for item in report.frames}
    if mermaid:
        lines = ["flowchart LR"]
        for frame in report.frames:
            label = f"{frame.path}:{frame.line}:{frame.function}".replace('"', "'")
            lines.append(f'  {frame.frame_id.replace("-", "_")}["{label}"]')
        for edge in report.runtime_edges:
            lines.append(
                f'  {edge.caller_frame_id.replace("-", "_")} -->|"{edge.total_calls}"| '
                f'{edge.callee_frame_id.replace("-", "_")}'
            )
        return "\n".join(lines) + "\n"
    lines = ["digraph runtime_calls {"]
    for frame in report.frames:
        label = f"{frame.path}:{frame.line}:{frame.function}".replace('"', r"\"")
        lines.append(f'  "{frame.frame_id}" [label="{label}"];')
    for edge in report.runtime_edges:
        if edge.caller_frame_id in frames and edge.callee_frame_id in frames:
            lines.append(
                f'  "{edge.caller_frame_id}" -> "{edge.callee_frame_id}" '
                f'[label="{edge.total_calls}"];'
            )
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_static_runtime_overlay(
    profile: ProfileReport, source: AnalysisReport, *, mermaid: bool
) -> str:
    subject_labels = {item.subject_id: item.logical_name for item in source.subjects}
    profile_subjects = {item.frame_id: item.subject_id for item in profile.frames}
    runtime = {
        (profile_subjects.get(edge.caller_frame_id), profile_subjects.get(edge.callee_frame_id))
        for edge in profile.runtime_edges
    }
    runtime = {(left, right) for left, right in runtime if left is not None and right is not None}
    logical_subjects = {item.logical_name: item.subject_id for item in source.subjects}
    static = set()
    for item in source.function_profiles:
        for callee in item.callees:
            target = logical_subjects.get(callee)
            if target is not None:
                static.add((item.subject_id, target))
    edges = sorted(static | runtime)
    nodes = sorted({value for edge in edges for value in edge})
    if mermaid:
        lines = ["flowchart LR"]
        for subject_id in nodes:
            label = subject_labels.get(subject_id, subject_id).replace('"', "'")
            lines.append(f'  {subject_id.replace("-", "_")}["{label}"]')
        for left, right in edges:
            kind = (
                "both"
                if (left, right) in static and (left, right) in runtime
                else ("static" if (left, right) in static else "runtime")
            )
            lines.append(f'  {left.replace("-", "_")} -->|"{kind}"| {right.replace("-", "_")}')
        return "\n".join(lines) + "\n"
    lines = ["digraph static_runtime_overlay {"]
    for subject_id in nodes:
        label = subject_labels.get(subject_id, subject_id).replace('"', r"\"")
        lines.append(f'  "{subject_id}" [label="{label}"];')
    for left, right in edges:
        kind = (
            "both"
            if (left, right) in static and (left, right) in runtime
            else ("static" if (left, right) in static else "runtime")
        )
        lines.append(f'  "{left}" -> "{right}" [label="{kind}"];')
    lines.append("}")
    return "\n".join(lines) + "\n"


def build_profile_diff(
    baseline: ProfileReport, candidate: ProfileReport, *, diagnostic_only: bool = False
) -> ProfileDiffReport:
    from code_analysis.runtime_comparison import compare_runtime_profiles

    return compare_runtime_profiles(baseline, candidate, diagnostic_only=diagnostic_only)


def render_profile_diff(report: ProfileDiffReport, *, markdown: bool) -> str:
    if not markdown:
        return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    lines = [
        "# Profile comparison",
        "",
        f"Diagnostic-only: {str(report.diagnostic_only).lower()}; "
        f"claim-eligible: {str(report.claim_eligible).lower()}.",
        "",
        "Compatibility decisions:",
        "",
        *(f"- {item.field}: {item.outcome}. {item.reason}" for item in report.decisions),
        "",
        "| Frame | Total delta | Total relative | Cumulative delta | Cumulative relative |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for item in report.records:
        total_relative = (
            "n/a"
            if item.relative_total_seconds_delta is None
            else f"{item.relative_total_seconds_delta:.6f}"
        )
        cumulative_relative = (
            "n/a"
            if item.relative_cumulative_seconds_delta is None
            else f"{item.relative_cumulative_seconds_delta:.6f}"
        )
        lines.append(
            f"| `{item.frame_identity}` | {_seconds(item.absolute_total_seconds_delta)} | "
            f"{total_relative} | {_seconds(item.absolute_cumulative_seconds_delta)} | "
            f"{cumulative_relative} |"
        )
    return "\n".join(lines) + "\n"


def _profile_command(command: tuple[str, ...], output: Path) -> tuple[str, ...]:
    if not command:
        raise OSError(errno.EINVAL, "profile command cannot be empty")
    first = Path(command[0])
    current = Path(sys.executable).resolve()
    resolved_command = Path(shutil.which(command[0]) or command[0]).resolve()
    if first.name in {"python", "python3"} or resolved_command == current:
        if resolved_command != current:
            raise OSError(errno.EINVAL, "profile requires the current interpreter")
        remainder = command[1:]
        if len(remainder) >= 2 and remainder[0] == "-m":
            return (
                sys.executable,
                "-c",
                _PROFILE_RUNNER,
                str(output),
                "module",
                remainder[1],
                *remainder[2:],
            )
        if remainder and remainder[0].endswith(".py"):
            return (
                sys.executable,
                "-c",
                _PROFILE_RUNNER,
                str(output),
                "path",
                *remainder,
            )
        raise OSError(errno.EINVAL, "unsupported current-interpreter profile command")
    if first.name in {"pytest", "py.test"}:
        return (
            sys.executable,
            "-c",
            _PROFILE_RUNNER,
            str(output),
            "module",
            "pytest",
            *command[1:],
        )
    if resolved_command.is_file():
        try:
            shebang = resolved_command.open("rb").readline(256).decode("utf-8")
        except (OSError, UnicodeError):
            shebang = ""
        if shebang.startswith("#!") and "python" in shebang.lower():
            return (
                sys.executable,
                "-c",
                _PROFILE_RUNNER,
                str(output),
                "path",
                str(resolved_command),
                *command[1:],
            )
    raise OSError(errno.EINVAL, "profile command is not a supported Python workload")


def _execute_profile(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    repetition: int,
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[int, bool, ResourceObservation]:
    # Keep this compatibility path bounded even though its historical API has
    # no runtime configuration parameter. The managed runner uses the same
    # default unless an explicit limit is supplied by its specification.
    process = OwnedRuntimeProcess(
        command,
        cwd=cwd,
        environment=environment,
        stream_limit=8 * 1024 * 1024,
        require_identity=False,
    )
    timed_out = False
    primary: OSError | None = None
    try:
        try:
            process.wait(time.monotonic() + timeout_seconds)
        except OSError as exc:
            primary = exc
            timed_out = exc.errno == errno.ETIMEDOUT
        if process.overflow and primary is None:
            primary = OSError(
                errno.ENODATA, "profile runtime stream exceeded configured byte bound"
            )
    finally:
        try:
            forced = process.shutdown(
                graceful=signal.SIGTERM,
                graceful_seconds=min(5.0, timeout_seconds),
                forced_seconds=5.0,
            )
            if forced and primary is None:
                primary = OSError(errno.ETIMEDOUT, "profile required forced descendant termination")
            if process.overflow and primary is None:
                primary = OSError(
                    errno.ENODATA, "profile stream exceeded byte bound during shutdown"
                )
        except OSError as exc:
            if primary is None:
                primary = exc
        finally:
            try:
                stdout_path.write_bytes(process.stdout)
                stderr_path.write_bytes(process.stderr)
            except OSError as exc:
                primary = primary or exc
            finally:
                resource = process.resources().model_copy(update={"repetition": repetition})
                process.close()
    if primary is not None:
        if primary.errno == errno.ETIMEDOUT:
            return errno.ETIMEDOUT, True, resource
        raise primary
    return process.process.returncode, timed_out, resource


def _normalize_profile(
    raw_path: Path,
    portable_path: Path,
    *,
    source_report: AnalysisReport | None,
    source_root: Path | None,
) -> tuple[tuple[InternalFrame, ...], tuple[InternalEdge, ...]]:
    stats = pstats.Stats(str(raw_path))
    raw_stats = stats.stats
    _profile_graph_signature(raw_stats)
    key_map: dict[ProfileKey, ProfileKey] = {}
    classifications: dict[ProfileKey, FrameClassification] = {}
    subject_ids: dict[ProfileKey, str | None] = {}
    reverse: dict[ProfileKey, ProfileKey] = {}
    for raw_key in raw_stats:
        original = (str(raw_key[0]), int(raw_key[1]), str(raw_key[2]))
        portable_filename, classification = _portable_frame_path(original[0], source_root)
        portable = (portable_filename, original[1], original[2])
        prior = reverse.get(portable)
        if prior is not None and prior != original:
            raise OSError(errno.EBADMSG, "portable profile frame path collision", portable_filename)
        reverse[portable] = original
        key_map[original] = portable
        classifications[original] = classification
        subject_ids[original] = (
            _frame_subject(source_report, portable) if source_report is not None else None
        )
    frames: list[InternalFrame] = []
    edges: list[InternalEdge] = []
    sanitized: dict[ProfileKey, tuple[int, int, float, float, dict[ProfileKey, object]]] = {}
    for raw_key, raw_value in raw_stats.items():
        original = (str(raw_key[0]), int(raw_key[1]), str(raw_key[2]))
        primitive, total, total_seconds, cumulative, callers = raw_value
        frame = InternalFrame(
            original_key=original,
            portable_key=key_map[original],
            classification=classifications[original],
            subject_id=subject_ids[original],
            primitive_calls=int(primitive),
            total_calls=int(total),
            total_seconds=float(total_seconds),
            cumulative_seconds=float(cumulative),
        )
        frames.append(frame)
        portable_callers: dict[ProfileKey, object] = {}
        for caller_key, caller_value in callers.items():
            caller = (str(caller_key[0]), int(caller_key[1]), str(caller_key[2]))
            portable_caller = key_map.get(caller)
            if portable_caller is None:
                raise OSError(
                    errno.ENODATA, "caller-only identity requires offline runtime ingestion"
                )
            cc, nc, tt, ct = _caller_metrics(caller_value)
            edges.append(
                InternalEdge(
                    caller=portable_caller,
                    callee=key_map[original],
                    primitive_calls=cc,
                    total_calls=nc,
                    total_seconds=tt,
                    cumulative_seconds=ct,
                )
            )
            portable_callers[portable_caller] = caller_value
        sanitized[key_map[original]] = (
            int(primitive),
            int(total),
            float(total_seconds),
            float(cumulative),
            portable_callers,
        )
    stats.stats = sanitized
    stats.dump_stats(str(portable_path))
    restored = pstats.Stats(str(portable_path))
    if _profile_graph_signature(restored.stats) != _profile_graph_signature(sanitized):
        raise OSError(errno.EBADMSG, "portable profile reload changed frame or caller identities")
    return (
        tuple(sorted(frames, key=lambda item: item.portable_key)),
        tuple(sorted(edges, key=lambda item: (item.caller, item.callee))),
    )


def _caller_metrics(value: object) -> tuple[int, int, float, float]:
    from code_analysis.profile_ingestion import caller_metrics

    try:
        result = caller_metrics(value)
    except ValueError:
        raise OSError(errno.EBADMSG, "malformed cProfile caller metrics") from None
    if result.primitive is None or result.self_seconds is None or result.cumulative_seconds is None:
        raise OSError(errno.EBADMSG, "controlled cProfile capture requires timed caller tuples")
    return result.primitive, result.total, result.self_seconds, result.cumulative_seconds


def _profile_graph_signature(stats: dict[ProfileKey, object]) -> tuple[object, ...]:
    from code_analysis.profile_ingestion import native_signature

    try:
        return native_signature(stats)
    except ValueError:
        raise OSError(errno.EBADMSG, "malformed cProfile frame/caller metrics") from None


def _frame_identity(frame: ProfileFrame) -> str:
    return f"{frame.path}:{frame.line}:{frame.function}"


def _relative_delta(baseline: float, candidate: float) -> float | None:
    if baseline == 0.0:
        return None
    return (candidate - baseline) / baseline


def _optional_text(value: object | None, unavailable: str) -> str:
    return unavailable if value is None else str(value)


def _seconds(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.9f}"


def _portable_frame_path(
    filename: str, source_root: Path | None
) -> tuple[str, FrameClassification]:
    if filename.startswith("<") and filename.endswith(">"):
        return f"generated/{filename[1:-1]}", FrameClassification.GENERATED
    path = Path(filename)
    if path.is_absolute():
        resolved = path.resolve()
        if source_root is not None:
            with suppress(ValueError):
                return (
                    resolved.relative_to(source_root.resolve()).as_posix(),
                    FrameClassification.PROJECT,
                )
        for key in ("purelib", "platlib"):
            configured = sysconfig.get_paths().get(key)
            if configured:
                with suppress(ValueError):
                    relative = resolved.relative_to(Path(configured).resolve()).as_posix()
                    return f"third-party/{relative}", FrameClassification.THIRD_PARTY
        configured_stdlib = sysconfig.get_paths().get("stdlib")
        if configured_stdlib:
            with suppress(ValueError):
                relative = resolved.relative_to(Path(configured_stdlib).resolve()).as_posix()
                return f"standard-library/{relative}", FrameClassification.STANDARD_LIBRARY
        with suppress(ValueError):
            relative = resolved.relative_to(Path(sys.prefix).resolve()).as_posix()
            return f"environment/{relative}", FrameClassification.ENVIRONMENT
        return f"unresolved/{resolved.name}", FrameClassification.UNRESOLVED
    normalized = PurePosixPath(filename.replace("\\", "/"))
    if filename == "~":
        return "native/builtins", FrameClassification.NATIVE
    return f"unresolved/{normalized.name}", FrameClassification.UNRESOLVED


def _frame_subject(source_report: AnalysisReport, key: ProfileKey) -> str | None:
    path, line, function = key
    candidates = [
        subject.subject_id
        for subject in source_report.subjects
        if subject.logical_name.rsplit(":", 1)[-1].rsplit(".", 1)[-1] == function
        and any(
            evidence.span.path == path
            and evidence.span.start_line <= line <= evidence.span.end_line
            for evidence in subject.evidence
        )
    ]
    return candidates[0] if len(candidates) == 1 else None


def _aggregate_frames(
    repetitions: list[tuple[InternalFrame, ...]], maximum: int
) -> tuple[tuple[ProfileFrame, ...], tuple[ProfileStatistic, ...], int]:
    grouped: dict[ProfileKey, list[InternalFrame]] = {}
    for repetition in repetitions:
        for frame in repetition:
            grouped.setdefault(frame.portable_key, []).append(frame)
    retained_keys = sorted(grouped)[:maximum]
    frames = []
    statistics_records = []
    for key in retained_keys:
        values = grouped[key]
        total_values = [item.total_seconds for item in values]
        call_values = [item.total_calls for item in values]
        cumulative_values = [item.cumulative_seconds for item in values]
        frame_id = _stable_id("profile-frame", key[0], str(key[1]), key[2])
        mean_total = statistics.fmean(total_values)
        frames.append(
            ProfileFrame(
                frame_id=frame_id,
                path=key[0],
                line=key[1],
                function=key[2],
                classification=values[0].classification,
                subject_id=values[0].subject_id,
                primitive_calls=round(statistics.fmean(item.primitive_calls for item in values)),
                total_calls=round(statistics.fmean(call_values)),
                total_seconds=mean_total,
                cumulative_seconds=statistics.fmean(cumulative_values),
                per_call_seconds=(
                    mean_total / statistics.fmean(call_values) if any(call_values) else None
                ),
            )
        )
        standard_deviation = statistics.pstdev(total_values)
        statistics_records.append(
            ProfileStatistic(
                frame_id=frame_id,
                call_count_mean=statistics.fmean(call_values),
                total_seconds_mean=mean_total,
                total_seconds_median=statistics.median(total_values),
                total_seconds_minimum=min(total_values),
                total_seconds_maximum=max(total_values),
                total_seconds_standard_deviation=standard_deviation,
                total_seconds_coefficient_of_variation=(
                    standard_deviation / mean_total if mean_total else None
                ),
            )
        )
    frames.sort(key=lambda item: item.frame_id)
    statistics_records.sort(key=lambda item: item.frame_id)
    return tuple(frames), tuple(statistics_records), max(0, len(grouped) - maximum)


def _aggregate_edges(
    repetitions: list[tuple[InternalEdge, ...]],
    known_frame_ids: set[str],
    maximum: int,
) -> tuple[tuple[RuntimeCallEdge, ...], int]:
    grouped: dict[tuple[ProfileKey, ProfileKey], list[InternalEdge]] = {}
    for repetition in repetitions:
        for edge in repetition:
            grouped.setdefault((edge.caller, edge.callee), []).append(edge)
    records = []
    for (caller, callee), values in sorted(grouped.items()):
        caller_id = _stable_id("profile-frame", caller[0], str(caller[1]), caller[2])
        callee_id = _stable_id("profile-frame", callee[0], str(callee[1]), callee[2])
        if caller_id not in known_frame_ids or callee_id not in known_frame_ids:
            continue
        records.append(
            RuntimeCallEdge(
                runtime_edge_id=_stable_id("runtime-edge", caller_id, callee_id),
                caller_frame_id=caller_id,
                callee_frame_id=callee_id,
                primitive_calls=round(statistics.fmean(item.primitive_calls for item in values)),
                total_calls=round(statistics.fmean(item.total_calls for item in values)),
                total_seconds=statistics.fmean(item.total_seconds for item in values),
                cumulative_seconds=statistics.fmean(item.cumulative_seconds for item in values),
            )
        )
    records.sort(key=lambda item: item.runtime_edge_id)
    return tuple(records[:maximum]), max(0, len(records) - maximum)


def _stable_id(prefix: str, *values: str) -> str:
    return f"{prefix}-{hashlib.sha256(chr(31).join(values).encode()).hexdigest()[:20]}"


def _psutil_available() -> bool:
    try:
        import psutil  # noqa: F401
    except ImportError:
        return False
    return True


def _psutil_version() -> str | None:
    try:
        import psutil
    except ImportError:
        return None
    return psutil.__version__
