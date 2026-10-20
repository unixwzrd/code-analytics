"""Offline pstats boundary: bounded loading, exact metrics and portable identities.

The worker reads profiler data, never imports the profiled program. Native payloads
are quarantined here; callers receive strict, immutable report records.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from code_analysis import __version__
from code_analysis.models import (
    Completeness,
    Diagnostic,
    DiagnosticLevel,
    ProviderResult,
    ProviderState,
)
from code_analysis.profile_models import (
    CallerRepresentation,
    FrameClassification,
    ProfileFrame,
    ProfileProducer,
    ProfileReport,
    ResourceObservation,
    RuntimeCallEdge,
)
from code_analysis.pstats_table import ProfileStats
from code_analysis.report_models import AnalysisReport
from code_analysis.runtime_identity import CollectionMetadata, RuntimeRoot, read_bounded_artifact
from code_analysis.runtime_models import (
    CollectionIdentity,
    ProcessProofState,
    RuntimeArtifact,
    RuntimeEvidence,
    RuntimeProcessProof,
)
from code_analysis.runtime_spec import RuntimeAnalysisConfig

ProfileKey = tuple[str, int, str]


@dataclass(frozen=True, slots=True)
class FrameMetrics:
    primitive: int
    total: int
    self_seconds: float
    cumulative_seconds: float


def metrics(value: object) -> FrameMetrics:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise ValueError("invalid cProfile metrics shape")
    primitive, total, self_seconds, cumulative = value
    if type(primitive) is not int or type(total) is not int or not 0 <= primitive <= total:
        raise ValueError("invalid cProfile call counts")
    if any(
        type(v) not in (int, float) or not math.isfinite(v) or v < 0
        for v in (self_seconds, cumulative)
    ):
        raise ValueError("invalid cProfile timing")
    return FrameMetrics(primitive, total, float(self_seconds), float(cumulative))


def native_key(value: object) -> ProfileKey:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError("invalid pstats key")
    path, line, name = value
    if (
        type(path) is not str
        or type(name) is not str
        or (not path and line != 0)
        or not name
        or type(line) is not int
        or line < 0
    ):
        raise ValueError("invalid pstats frame identity")
    if len(path) > 16384 or len(name) > 16384 or "\0" in path or "\0" in name:
        raise ValueError("oversized or invalid pstats frame label")
    return path, line, name


@dataclass(frozen=True, slots=True)
class CallerMetrics:
    representation: CallerRepresentation
    primitive: int | None
    total: int
    self_seconds: float | None
    cumulative_seconds: float | None


class InvalidCaller(ValueError):
    """A malformed caller representation, never a resource failure."""


def caller_metrics(value: object) -> CallerMetrics:
    # cProfile.snapshot_stats writes callers as nc, cc, tt, ct; frame records
    # use cc, nc, tt, ct. Recursive calls make the distinction observable.
    if type(value) is int:
        if value < 0:
            raise InvalidCaller("invalid pstats caller count")
        return CallerMetrics(CallerRepresentation.COUNT, None, value, None, None)
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise InvalidCaller("unsupported pstats caller representation")
    try:
        result = metrics((value[1], value[0], value[2], value[3]))
    except ValueError:
        raise InvalidCaller("invalid pstats caller tuple metrics") from None
    return CallerMetrics(
        CallerRepresentation.TIMED,
        result.primitive,
        result.total,
        result.self_seconds,
        result.cumulative_seconds,
    )


def native_signature(stats: Mapping[ProfileKey, object]) -> tuple[object, ...]:
    result: list[object] = []
    for key, value in sorted(stats.items()):
        native_key(key)
        if not isinstance(value, tuple) or len(value) != 5 or not isinstance(value[4], dict):
            raise ValueError("invalid pstats frame value")
        result.append(
            (
                key,
                metrics(value[:4]),
                tuple((native_key(k), caller_metrics(v)) for k, v in sorted(value[4].items())),
            )
        )
    return tuple(result)


def _worker(source: Path, output: Path, max_frames: int, max_edges: int) -> None:
    """Validate every input record, but retain only configured bounded records."""
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Darwin rejects RLIMIT_AS here. Input/output bytes and the parent deadline
    # still bound this worker; never report an unavailable address-space cap.
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024**2, 512 * 1024**2))
    stats = ProfileStats(str(source)).stats
    if not stats:
        raise ValueError("empty profile")
    retained: list[object] = []
    edges = 0
    total_edges = 0
    all_keys = set(stats)
    # Validate all values before sorting or retention; no coercion/zero fallback.
    for key, value in stats.items():
        native_key(key)
        if not isinstance(value, tuple) or len(value) != 5 or not isinstance(value[4], dict):
            raise ValueError("invalid pstats record")
        metrics(value[:4])
        for caller, caller_value in value[4].items():
            all_keys.add(native_key(caller))
            caller_metrics(caller_value)
    retained_keys = set(sorted(all_keys)[:max_frames])
    for key, value in sorted(stats.items()):
        total_edges += len(value[4])
        if key not in retained_keys:
            continue
        callers = []
        for caller, caller_value in sorted(value[4].items()):
            if caller in retained_keys and edges < max_edges:
                callers.append((caller, caller_value))
                edges += 1
        retained.append((key, value[:4], callers))
    payload = {
        "frames": retained,
        "caller_only": sorted(retained_keys - set(stats)),
        "original_frames": len(all_keys),
        "original_edges": total_edges,
    }
    output.write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False), encoding="utf-8")


def _portable_key(
    key: ProfileKey, roots: tuple[RuntimeRoot, ...]
) -> tuple[ProfileKey, FrameClassification]:
    path, line, name = key
    # profile.Profile represents builtins with an empty filename at line zero.
    if path == "":
        return ("native/profile-builtins", line, name), FrameClassification.NATIVE
    if path == "~":
        return ("native/builtins", line, name), FrameClassification.NATIVE
    if path.startswith("<") and path.endswith(">"):
        token = hashlib.sha256(path.encode()).hexdigest()[:20]
        return (f"generated/{token}", line, name), FrameClassification.GENERATED
    normalized = Path(path)
    for root in sorted(roots, key=lambda item: (-len(item.physical.parts), item.logical)):
        try:
            relative = normalized.relative_to(root.physical).as_posix()
        except ValueError:
            continue
        if ".." in Path(relative).parts:
            raise ValueError("profile path escapes declared root")
        portable = relative if root.classification == "project" else f"{root.logical}/{relative}"
        return (portable, line, name), FrameClassification(root.classification)
    # Distinct unresolved paths cannot collapse to the same basename. This digest
    # identifies a source location, not a secret or an environment value.
    token = hashlib.sha256(path.encode()).hexdigest()[:24]
    return (f"unresolved/{token}", line, name), FrameClassification.UNRESOLVED


def _identity(prefix: str, *values: str) -> str:
    return prefix + "-" + hashlib.sha256("\x1f".join(values).encode()).hexdigest()[:20]


def _records(
    payload: object, *, roots: tuple[RuntimeRoot, ...], artifact_sha: str, separate: bool
) -> tuple[tuple[ProfileFrame, ...], tuple[RuntimeCallEdge, ...], bytes | None]:
    """The JSON payload is produced only by our bounded worker."""
    if not isinstance(payload, dict):
        raise ValueError("invalid worker payload")
    raw_frames = payload["frames"]
    caller_only = {native_key(key) for key in payload["caller_only"]}
    key_map: dict[ProfileKey, ProfileKey] = {}
    classifications: dict[ProfileKey, FrameClassification] = {}
    reverse: dict[ProfileKey, ProfileKey] = {}
    for raw_key in [item[0] for item in raw_frames] + list(caller_only):
        key = native_key(raw_key)
        portable, category = _portable_key(key, roots)
        if portable in reverse and reverse[portable] != key:
            raise ValueError("portable pstats identity collision")
        reverse[portable], key_map[key], classifications[key] = key, portable, category
    ids = {
        key: _identity(
            "profile-frame",
            *((artifact_sha,) if separate else ()),
            portable[0],
            str(portable[1]),
            portable[2],
        )
        for key, portable in key_map.items()
    }
    frames: list[ProfileFrame] = []
    edges: list[RuntimeCallEdge] = []
    sanitized: dict[ProfileKey, object] = {}
    for raw_key, raw_metrics, raw_callers in raw_frames:
        key = native_key(raw_key)
        value = metrics(raw_metrics)
        path, line, function = key_map[key]
        frames.append(
            ProfileFrame(
                frame_id=ids[key],
                path=path,
                line=line,
                function=function,
                classification=classifications[key],
                primitive_calls=value.primitive,
                total_calls=value.total,
                total_seconds=value.self_seconds,
                cumulative_seconds=value.cumulative_seconds,
                per_call_seconds=value.self_seconds / value.total if value.total else None,
                cumulative_per_primitive_call_seconds=(
                    value.cumulative_seconds / value.primitive if value.primitive else None
                ),
                artifact_sha256=artifact_sha,
            )
        )
        callers: dict[ProfileKey, object] = {}
        for raw_caller, raw_value in raw_callers:
            caller = native_key(raw_caller)
            edge = caller_metrics(raw_value)
            callers[key_map[caller]] = raw_value if type(raw_value) is int else tuple(raw_value)
            edges.append(
                RuntimeCallEdge(
                    runtime_edge_id=_identity("runtime-edge", ids[caller], ids[key]),
                    caller_frame_id=ids[caller],
                    callee_frame_id=ids[key],
                    caller_representation=edge.representation,
                    primitive_calls=edge.primitive,
                    total_calls=edge.total,
                    total_seconds=edge.self_seconds,
                    cumulative_seconds=edge.cumulative_seconds,
                )
            )
        sanitized[key_map[key]] = (*raw_metrics, callers)
    for key in sorted(caller_only):
        path, line, function = key_map[key]
        frames.append(
            ProfileFrame(
                frame_id=ids[key],
                path=path,
                line=line,
                function=function,
                classification=classifications[key],
                primitive_calls=None,
                total_calls=None,
                total_seconds=None,
                cumulative_seconds=None,
                per_call_seconds=None,
                metrics_observed=False,
                artifact_sha256=artifact_sha,
            )
        )
    # Serialize only original frame records. Caller-only keys remain references,
    # exactly as in the input; no synthetic zero measurements are introduced.
    portable_bytes = None
    with tempfile.TemporaryDirectory(prefix="code-analysis-pstats-reload-") as directory:
        import marshal

        portable_path = Path(directory) / "portable.prof"
        portable_path.write_bytes(marshal.dumps(sanitized))
        restored = ProfileStats(str(portable_path)).stats if sanitized else {}
        if native_signature(restored) != native_signature(sanitized):
            raise ValueError("portable pstats reload changed metrics or identities")
        portable_bytes = portable_path.read_bytes() if sanitized else None
    return (
        tuple(sorted(frames, key=lambda item: item.frame_id)),
        tuple(sorted(edges, key=lambda item: item.runtime_edge_id)),
        portable_bytes,
    )


def ingest_profiles(
    paths: tuple[Path, ...],
    *,
    config: RuntimeAnalysisConfig | None = None,
    repo: Path | None = None,
    metadata: CollectionMetadata | None = None,
    source_report: AnalysisReport | None = None,
) -> tuple[ProfileReport, dict[str, bytes]]:
    """Normalize unchanged artifacts separately; raw files cannot prove collection identity."""
    config = config or RuntimeAnalysisConfig()
    if not paths:
        raise OSError(errno.EINVAL, "runtime profile input is required")
    if len(paths) > config.max_inputs:
        raise OSError(errno.ENODATA, "runtime profile input count exceeds analytical bounds")
    inputs: dict[str, tuple[Path, bytes, RuntimeArtifact]] = {}
    for path in paths:
        data, artifact = read_bounded_artifact(
            path, maximum=config.max_profile_bytes, kind="profile"
        )
        inputs.setdefault(artifact.sha256, (path, data, artifact))
    bindings = metadata.producer_bindings if metadata else ()
    bound_hashes = {item.profile_sha256 for item in bindings}
    if bound_hashes - inputs.keys():
        raise OSError(errno.EBADMSG, "producer declaration does not match an input profile hash")
    producers = {item.producer for item in bindings}
    producer = (
        next(iter(producers))
        if len(producers) == 1 and bound_hashes == inputs.keys()
        else ProfileProducer.UNKNOWN
    )
    roots = metadata.roots if metadata else ()
    if repo is not None and not any(item.classification == "project" for item in roots):
        roots = (
            *roots,
            RuntimeRoot(physical=repo.absolute(), logical="project", classification="project"),
        )
    all_frames: list[ProfileFrame] = []
    all_edges: list[RuntimeCallEdge] = []
    local: dict[str, bytes] = {}
    frame_count = edge_count = 0
    try:
        for artifact_sha, (path, data, artifact) in sorted(inputs.items()):
            with tempfile.TemporaryDirectory(prefix="code-analysis-pstats-input-") as directory:
                raw, output = Path(directory) / "input.prof", Path(directory) / "facts.json"
                raw.write_bytes(data)
                environment = {
                    "PATH": os.defpath,
                    "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
                    "PYTHONHASHSEED": "0",
                }
                try:
                    result = subprocess.run(
                        (
                            sys.executable,
                            "-m",
                            "code_analysis.profile_ingestion",
                            str(raw),
                            str(output),
                            str(config.max_frames),
                            str(config.max_edges),
                        ),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env=environment,
                        cwd=directory,
                        timeout=config.ingestion_seconds,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    raise OSError(errno.ETIMEDOUT, "pstats ingestion worker timed out") from None
                if result.returncode:
                    if result.returncode == errno.EINVAL:
                        raise OSError(
                            errno.EBADMSG, "pstats worker rejected invalid caller representation"
                        )
                    if result.returncode == errno.ENOMEM or result.returncode < 0:
                        raise OSError(
                            errno.ENODATA, "pstats worker exhausted resources or was terminated"
                        )
                    raise OSError(
                        errno.EBADMSG,
                        "pstats worker rejected malformed input",
                    )
                encoded, _ = read_bounded_artifact(output, maximum=512 * 1024**2, kind="external")
                payload = json.loads(encoded)
                frames, edges, portable = _records(
                    payload, roots=roots, artifact_sha=artifact_sha, separate=len(inputs) > 1
                )
                frame_count += payload["original_frames"]
                edge_count += payload["original_edges"]
                all_frames.extend(frames)
                all_edges.extend(edges)
                if portable is not None:
                    local[f"portable-{artifact_sha}.prof"] = portable
            _, after = read_bounded_artifact(path, maximum=config.max_profile_bytes, kind="profile")
            if after != artifact:
                raise OSError(errno.EBADMSG, "profile artifact changed during normalization")
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise OSError(errno.EBADMSG, "invalid normalized pstats evidence") from exc
    # Global analytical bounds apply across all artifacts as well as each worker.
    frames = tuple(sorted(all_frames, key=lambda item: item.frame_id)[: config.max_frames])
    known = {item.frame_id for item in frames}
    edges = tuple(
        sorted(
            (
                item
                for item in all_edges
                if item.caller_frame_id in known and item.callee_frame_id in known
            ),
            key=lambda item: item.runtime_edge_id,
        )[: config.max_edges]
    )
    truncated = frame_count > len(frames) or edge_count > len(edges)
    caller_only = tuple(item.frame_id for item in frames if not item.metrics_observed)
    limitations = {
        "Ingestion is byte/deadline bounded; an OS address-space cap is "
        "not available on every host.",
        "Standalone pstats evidence has no independently captured "
        "workload status or process resources.",
        "Standalone pstats evidence is unbound to source hashes and "
        "symbols unless captured metadata agrees.",
        "Profiler timings include overhead and do not establish an optimization win.",
        "Caller representation describes each edge, not the producer of an entire capture.",
        "Cumulative node times overlap and must not be summed as elapsed time.",
        "Self per-call uses total calls; cumulative per-call uses "
        "primitive calls; zero denominators are null.",
        "Native work and child processes may be absent; unobserved code is not dead code.",
    }
    if bindings:
        limitations.add(
            "Producer declarations are supplied metadata bound to exact input SHA-256 values, "
            "not independently authenticated capture attestation."
        )
    if len(producers) > 1:
        limitations.add(
            "Mixed declared producers: per-artifact bindings are retained; "
            "no single report producer is asserted."
        )
    if bound_hashes != inputs.keys():
        limitations.add(
            "Producer identity is unknown for undeclared inputs: "
            "standalone pstats does not identify the capture producer."
        )
    if len(inputs) > 1:
        limitations.add(
            "Input profiles remain separate observations; unrelated "
            "captures are not pooled repetitions."
        )
    if caller_only:
        limitations.add("Caller-only nodes have unknown metrics, not zero-cost observations.")
    if any(edge.caller_representation == CallerRepresentation.COUNT for edge in edges):
        limitations.add(
            "Integer callers supply exact total counts only; primitive caller counts and "
            "edge self/cumulative timings are unavailable, not zero."
        )
    if truncated:
        limitations.add(
            "Profile frames or edges exceed analytical bounds; raw inputs are unchanged."
        )
    diagnostics = tuple(
        Diagnostic(
            code=code, level=DiagnosticLevel.WARNING, message=message, provider="standalone-pstats"
        )
        for code, message in (
            *(
                (
                    (
                        "runtime-profile-truncated",
                        "Profile inventory exceeds configured analytical bounds.",
                    ),
                )
                if truncated
                else ()
            ),
            *(
                (("runtime-caller-only", "Caller-only references have unresolved frame metrics."),)
                if caller_only
                else ()
            ),
        )
    )
    process = metadata.process if metadata else RuntimeProcessProof()
    if metadata and process.state == ProcessProofState.VERIFIED:
        process = process.model_copy(update={"state": ProcessProofState.DECLARED})
    runtime = RuntimeEvidence(
        artifacts=tuple(value[2] for _, value in sorted(inputs.items())),
        collection=metadata.collection if metadata else CollectionIdentity(),
        process=process,
        original_frame_count=frame_count,
        original_edge_count=edge_count,
        caller_only_frame_ids=caller_only,
    )
    report = ProfileReport(
        tool_version=__version__,
        producer=producer,
        producer_bindings=bindings,
        profile_identity_sha256="0" * 64,
        source_report_sha256=None,
        workload_identity_sha256=runtime.collection.workload_sha256,
        repetitions=1,
        expected_status="UNAVAILABLE",
        observed_statuses=(None,),
        frames=frames,
        runtime_edges=edges,
        statistics=(),
        resources=(ResourceObservation(repetition=1),),
        runtime=runtime,
        providers=(
            ProviderResult(
                provider_id="standalone-pstats",
                state=ProviderState.PARTIAL,
                duration_ms=0,
                observations=("callers", "callees", "timings"),
                completeness=Completeness.PARTIAL,
            ),
        ),
        diagnostics=diagnostics,
        completeness=Completeness.PARTIAL,
        limitations=tuple(sorted(limitations)),
    )
    from code_analysis.runtime_correlation import correlate_runtime

    report = correlate_runtime(
        report, source_report=source_report, metadata=metadata, config=config
    )
    return report, local


if __name__ == "__main__":
    try:
        _worker(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
    except InvalidCaller:
        raise SystemExit(errno.EINVAL) from None
    except MemoryError:
        raise SystemExit(errno.ENOMEM) from None
    except (OSError, ValueError, TypeError, EOFError):
        raise SystemExit(errno.EBADMSG) from None
