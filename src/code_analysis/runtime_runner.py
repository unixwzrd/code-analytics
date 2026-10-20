"""Explicit managed workload lifecycle with proven, bounded child ownership."""

from __future__ import annotations

import errno
import hashlib
import importlib.metadata
import json
import os
import signal
import sys
import sysconfig
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, StrictBool, StrictInt, StrictStr

from code_analysis import __version__
from code_analysis.config import AnalysisConfig
from code_analysis.models import (
    Completeness,
    Diagnostic,
    DiagnosticLevel,
    ProviderResult,
    ProviderState,
    StrictModel,
)
from code_analysis.profile_ingestion import ingest_profiles
from code_analysis.profile_models import ProfileReport, ResourceObservation
from code_analysis.report_models import AnalysisReport
from code_analysis.runtime_artifacts import publish_runtime_report
from code_analysis.runtime_correlation import profile_content_identity
from code_analysis.runtime_execution import controlled_environment, resolve_expected_status
from code_analysis.runtime_identity import CollectionMetadata, RuntimeRoot, read_bounded_artifact
from code_analysis.runtime_models import (
    CollectionIdentity,
    LifecycleOutcome,
    LifecycleState,
    ProcessProofState,
    RuntimeEvidence,
    RuntimeProcessProof,
    RuntimeSourceHash,
    RuntimeStepObservation,
    SourceBindingState,
    runtime_digest,
)
from code_analysis.runtime_process import OwnedRuntimeProcess
from code_analysis.runtime_spec import (
    ArgumentRole,
    RunnerMode,
    RuntimeArgument,
    RuntimeCommand,
    RuntimeRunSpec,
)


class SubjectProfileSidecar(StrictModel):
    subject_pid: StrictInt = Field(ge=1)
    parent_pid: StrictInt = Field(ge=1)
    start_identity: StrictStr
    run_nonce: StrictStr
    collection_scope: StrictStr
    serialization_complete: StrictBool
    profile_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class RuntimeRunResult:
    report: ProfileReport
    status: int
    local_directory: Path


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _command_identity(command: RuntimeCommand) -> tuple[str, ...] | None:
    tokens = tuple(item.semantic_token() for item in command.arguments)
    return (
        tuple(token for token in tokens if token is not None)
        if all(token is not None for token in tokens)
        else None
    )


def _capture_sources(source: AnalysisReport | None, root: Path) -> tuple[RuntimeSourceHash, ...]:
    result = []
    for item in source.source_files if source else ():
        path = root / item.logical_path
        if path.is_symlink() or any(
            parent.is_symlink() for parent in path.parents if parent != root.parent
        ):
            raise OSError(errno.EBADMSG, "runtime source binding cannot follow symlinks")
        if not path.resolve().is_relative_to(root.resolve()):
            raise OSError(errno.EBADMSG, "runtime source binding escapes declared root")
        _, artifact = read_bounded_artifact(
            path, maximum=67108864, kind="external", allow_empty=True
        )
        if artifact.sha256 != item.source_sha256:
            raise OSError(errno.EBADMSG, "runtime source differs from saved static report")
        result.append(RuntimeSourceHash(path=item.logical_path, sha256=artifact.sha256))
    return tuple(sorted(result, key=lambda item: item.path))


def _collection(spec: RuntimeRunSpec, sources: tuple[RuntimeSourceHash, ...]) -> CollectionIdentity:
    subject = _command_identity(spec.subject)
    workloads = tuple(_command_identity(command) for command in spec.workloads)
    cleanup = tuple(_command_identity(command) for command in spec.cleanup)
    readiness = (
        _command_identity(spec.readiness.command)
        if spec.readiness and spec.readiness.command
        else spec.readiness.semantic_identity if spec.readiness else "none"
    )
    known_environment = spec.environment_semantic_identity is not None and all(
        item.semantic_identity is not None for item in spec.environment_add
    )
    workload = {
        "inherited_environment_identity": spec.environment_semantic_identity,
        "commands": workloads,
        "readiness": readiness,
        "cleanup": cleanup,
        "cleanup_statuses": tuple(command.expected_status for command in spec.cleanup),
        "subject_timeout_seconds": spec.subject.timeout_seconds,
        "workload_timeouts": tuple(command.timeout_seconds for command in spec.workloads),
        "expected_statuses": tuple(command.expected_status for command in spec.workloads),
        "environment_remove": spec.environment_remove,
        "environment": tuple((item.name, item.semantic_identity) for item in spec.environment_add),
        "graceful_signal": spec.graceful_signal,
    }
    dependencies = sorted(
        (distribution.metadata.get("Name", "unknown"), distribution.version)
        for distribution in importlib.metadata.distributions()
    )
    return CollectionIdentity(
        source_fingerprint_sha256=(
            _hash([item.model_dump(mode="json") for item in sources]) if sources else None
        ),
        interpreter_identity=f"{sys.implementation.name}-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        platform_identity=sys.platform,
        dependencies_sha256=_hash(dependencies),
        semantic_argv=subject,
        workload_sha256=(
            _hash(workload)
            if known_environment
            and readiness is not None
            and all(item is not None for item in (*workloads, *cleanup))
            else None
        ),
        profiler_configuration_sha256=_hash(
            {
                "limits": runtime_digest(spec.limits),
                "mode": spec.mode.value,
                "expected_status": spec.subject.expected_status,
            }
        ),
        collection_scope="subject-interpreter",
    )


def _bootstrap_argv(spec: RuntimeRunSpec, nonce: str) -> tuple[str, ...]:
    argv = spec.subject.argv()
    if spec.mode == RunnerMode.APPLICATION_MANAGED:
        return argv
    if Path(argv[0]).resolve() != Path(sys.executable).resolve():
        raise OSError(errno.EINVAL, "python-module requires the current interpreter")
    if len(argv) >= 3 and argv[1] == "-m":
        kind, target, arguments = "module", argv[2], argv[3:]
    elif len(argv) >= 2 and argv[1].endswith(".py"):
        kind, target, arguments = "script", argv[1], argv[2:]
    else:
        raise OSError(errno.EINVAL, "runtime Python workload requires -m MODULE or a Python script")
    return (
        sys.executable,
        str(Path(__file__).with_name("profile_bootstrap.py")),
        str(spec.profile_path),
        str(spec.metadata_path),
        nonce,
        kind,
        target,
        *arguments,
    )


def _run_command(
    command: RuntimeCommand,
    spec: RuntimeRunSpec,
    environment: dict[str, str],
    deadline: float,
    local: Path,
    label: str,
    *,
    subject: OwnedRuntimeProcess | None = None,
    observations: list[RuntimeStepObservation] | None = None,
) -> None:
    if time.monotonic() >= deadline:
        raise OSError(errno.ETIMEDOUT, "runtime command deadline exhausted before launch")
    process = OwnedRuntimeProcess(
        command.argv(),
        cwd=spec.cwd,
        environment=environment,
        stream_limit=spec.limits.max_stream_bytes,
    )
    failure: OSError | None = None
    shutdown_status = None
    companions = (subject,) if subject is not None else ()
    try:
        status = process.wait(
            min(deadline, time.monotonic() + command.timeout_seconds), companions=companions
        )
        if status != resolve_expected_status(command.expected_status):
            raise OSError(errno.EPROTO, "runtime workload returned unexpected status")
    except OSError as exc:
        failure = exc
    finally:
        try:
            forced = process.shutdown(
                graceful=signal.SIGTERM,
                graceful_seconds=spec.limits.graceful_seconds,
                forced_seconds=spec.limits.forced_seconds,
                deadline=deadline,
                companions=companions,
            )
            if forced:
                raise OSError(errno.ETIMEDOUT, "runtime command required forced termination")
        except OSError as exc:
            shutdown_status = exc.errno or errno.EPROTO
            failure = failure or exc
        finally:
            try:
                _retain_step(process, local, label, observations, failure, shutdown_status)
            except OSError as exc:
                failure = failure or exc
            finally:
                process.close()
    if failure is not None:
        raise failure


def _retain_step(
    process: OwnedRuntimeProcess,
    local: Path,
    label: str,
    observations: list[RuntimeStepObservation] | None,
    failure: OSError | None,
    shutdown_status: int | None,
) -> None:
    if observations is not None:
        observations.append(
            RuntimeStepObservation(
                step=label,
                observed_status=process.process.poll(),
                failure_status=(failure.errno or errno.EPROTO) if failure else None,
                shutdown_status=shutdown_status,
                stdout_sha256=hashlib.sha256(process.stdout).hexdigest(),
                stderr_sha256=hashlib.sha256(process.stderr).hexdigest(),
                stdout_bytes=len(process.stdout),
                stderr_bytes=len(process.stderr),
                stream_truncated=process.overflow,
            )
        )
    (local / f"{label}.stdout").write_bytes(process.stdout)
    (local / f"{label}.stderr").write_bytes(process.stderr)


def _await_ready(
    subject: OwnedRuntimeProcess,
    spec: RuntimeRunSpec,
    environment: dict[str, str],
    deadline: float,
    local: Path,
    observations: list[RuntimeStepObservation] | None = None,
) -> None:
    probe = spec.readiness
    if probe is None:
        return
    attempts = 0
    while time.monotonic() < deadline:
        if attempts >= spec.limits.max_readiness_attempts:
            raise OSError(errno.ENODATA, "runtime readiness attempt bound exceeded")
        subject.pump(0.0)
        if subject.process.poll() is not None:
            raise OSError(errno.EPROTO, "subject exited before readiness")
        if subject.overflow:
            raise OSError(errno.ENODATA, "subject stream bound exceeded during readiness")
        try:
            if probe.command:
                command = probe.command
            else:
                assert probe.url is not None  # ReadinessProbe validates command-or-URL.
                command = _url_probe_command(probe.url, probe.expected_http_status)
            _run_command(
                command,
                spec,
                environment,
                deadline,
                local,
                f"readiness-{attempts}",
                subject=subject,
                observations=observations,
            )
            return
        except OSError as exc:
            if exc.errno not in {errno.EPROTO, errno.ETIMEDOUT}:
                raise
        attempts += 1
        subject.pump(
            min(spec.limits.readiness_interval_seconds, max(0.0, deadline - time.monotonic()))
        )
    raise OSError(errno.ETIMEDOUT, "runtime readiness deadline exceeded")


def _url_probe_command(url: str, status: int) -> RuntimeCommand:
    """Isolate DNS/HTTP waits while the parent continues draining the subject."""
    return RuntimeCommand(
        arguments=(
            RuntimeArgument(role=ArgumentRole.EXECUTABLE, value=sys.executable),
            RuntimeArgument(
                role=ArgumentRole.SCRIPT,
                value=str(Path(__file__).with_name("runtime_http_probe.py")),
            ),
            RuntimeArgument(role=ArgumentRole.PRIVATE_PATH, value=url),
            RuntimeArgument(role=ArgumentRole.ARGUMENT, value=str(status)),
        ),
        timeout_seconds=2.0,
    )


def run_runtime(
    spec: RuntimeRunSpec,
    *,
    allow_execution: bool,
    config: AnalysisConfig,
    source_report: AnalysisReport | None = None,
) -> RuntimeRunResult:
    """No filesystem writes or commands before explicit execution authorization."""
    if not allow_execution:
        raise OSError(errno.EPERM, "runtime run requires --allow-execution")
    try:
        import psutil  # noqa: F401
    except ImportError:
        raise OSError(
            errno.ENODATA, "managed runtime process identity requires available psutil"
        ) from None
    for path in (spec.output, spec.profile_path, spec.metadata_path):
        if path.exists() or path.is_symlink():
            raise OSError(errno.EEXIST, "runtime output must not overwrite existing artifacts")
        if any(parent.is_symlink() for parent in path.parents):
            raise OSError(errno.EINVAL, "runtime output cannot traverse symlinks")
    if spec.profile_path.is_relative_to(spec.output) or spec.metadata_path.is_relative_to(
        spec.output
    ):
        raise OSError(
            errno.EINVAL, "raw profile/sidecar must be outside the new portable report directory"
        )
    sources = _capture_sources(source_report, spec.repo)
    identity = _collection(spec, sources)
    inputs = tuple(
        read_bounded_artifact(path, maximum=spec.limits.max_profile_bytes, kind="external")[1]
        for path in spec.external_inputs
    )
    identity = identity.model_copy(
        update={"external_artifacts": tuple(sorted({item.sha256 for item in inputs}))}
    )
    environment = controlled_environment(
        allowlist=config.runtime.environment_allowlist,
        additions=tuple((item.name, item.value) for item in spec.environment_add),
    )
    for name in spec.environment_remove:
        environment.pop(name, None)
    nonce = uuid.uuid4().hex
    environment["CODE_ANALYSIS_RUN_NONCE"] = nonce
    argv = _bootstrap_argv(spec, nonce)
    spec.output.parent.mkdir(parents=True, exist_ok=True)
    local = Path(tempfile.mkdtemp(prefix=f".{spec.output.name}.run-", dir=spec.output.parent))
    redacted = spec.model_dump(mode="json")
    # This local specification is still redacted; raw values are never persisted.
    for command in (redacted["subject"], *redacted["workloads"], *redacted["cleanup"]):
        for argument in command["arguments"]:
            if argument["role"] in {"secret-reference", "private-path"}:
                argument["value"] = "[REDACTED]"
    for addition in redacted["environment_add"]:
        addition["value"] = "[REDACTED]"
    if redacted["readiness"] is not None:
        redacted["readiness"] = {"configured": True}
    (local / "resolved-spec.json").write_text(
        json.dumps(redacted, sort_keys=True), encoding="utf-8"
    )
    spec.profile_path.parent.mkdir(parents=True, exist_ok=True)
    spec.metadata_path.parent.mkdir(parents=True, exist_ok=True)
    print("Warning: executing explicitly authorized untrusted project code.", file=sys.stderr)
    started = time.monotonic()
    deadline = started + spec.limits.total_seconds
    events: list[LifecycleOutcome] = []
    observations: list[RuntimeStepObservation] = []
    retained_artifacts = {item.sha256: item for item in inputs}

    def event(state: LifecycleState, reason: str, status: int | None = None) -> None:
        events.append(
            LifecycleOutcome(sequence=len(events), state=state, reason=reason, status=status)
        )

    event(LifecycleState.VALIDATED, "Specification and execution authorization validated.")
    subject = None
    failure = 0
    status = None
    proof = RuntimeProcessProof()
    report = None
    try:
        event(LifecycleState.STARTING, "Launching explicitly owned subject interpreter.")
        subject = OwnedRuntimeProcess(
            argv, cwd=spec.cwd, environment=environment, stream_limit=spec.limits.max_stream_bytes
        )
        subject_deadline = min(deadline, subject.started + spec.subject.timeout_seconds)
        if spec.readiness:
            _await_ready(
                subject,
                spec,
                environment,
                min(subject_deadline, time.monotonic() + spec.limits.readiness_seconds),
                local,
                observations,
            )
            event(LifecycleState.READY, "Configured readiness probe succeeded.")
            for index, command in enumerate(spec.workloads):
                event(LifecycleState.WORKLOAD, f"Executing workload step {index}.")
                _run_command(
                    command,
                    spec,
                    environment,
                    min(subject_deadline, time.monotonic() + spec.limits.workload_seconds),
                    local,
                    f"workload-{index}",
                    subject=subject,
                    observations=observations,
                )
                subject.pump(0.0)
            event(LifecycleState.GRACEFUL_STOP, "Requesting configured graceful shutdown.")
            if subject.shutdown(
                graceful=getattr(signal, spec.graceful_signal),
                graceful_seconds=spec.limits.graceful_seconds,
                forced_seconds=spec.limits.forced_seconds,
                deadline=subject_deadline,
            ):
                raise OSError(
                    errno.ETIMEDOUT,
                    "subject required forced termination; profile is not a successful capture",
                )
        status = subject.wait(subject_deadline)
        if time.monotonic() >= subject_deadline:
            raise OSError(errno.ETIMEDOUT, "subject deadline exhausted")
        if subject.overflow:
            raise OSError(errno.ENODATA, "subject stream exceeded configured byte bound")
        if any(pid != subject.process.pid for pid in subject.alive()):
            raise OSError(errno.EPROTO, "subject exited with undeclared live descendants")
        if status != resolve_expected_status(spec.subject.expected_status):
            raise OSError(errno.EPROTO, "profiled subject returned unexpected status")
        event(
            LifecycleState.PROFILE_VALIDATION,
            "Checking subject ownership and completed serialization.",
        )
        _data, artifact = read_bounded_artifact(
            spec.profile_path, maximum=spec.limits.max_profile_bytes, kind="profile"
        )
        retained_artifacts[artifact.sha256] = artifact
        metadata_bytes, metadata_artifact = read_bounded_artifact(
            spec.metadata_path, maximum=spec.limits.max_metadata_bytes, kind="metadata"
        )
        retained_artifacts[metadata_artifact.sha256] = metadata_artifact
        sidecar = SubjectProfileSidecar.model_validate_json(metadata_bytes)
        if (
            sidecar.subject_pid != subject.process.pid
            or sidecar.start_identity != str(subject.start)
            or sidecar.parent_pid != os.getpid()
            or sidecar.run_nonce != nonce
            or sidecar.profile_sha256 != artifact.sha256
            or not sidecar.serialization_complete
            or sidecar.collection_scope != "subject-interpreter"
        ):
            raise OSError(errno.EPROTO, "profile serialization or subject PID proof mismatch")
        proof = RuntimeProcessProof(
            state=ProcessProofState.VERIFIED,
            subject_pid=sidecar.subject_pid,
            parent_pid=sidecar.parent_pid,
            start_identity=sidecar.start_identity,
            run_nonce=nonce,
            profile_sha256=artifact.sha256,
            serialization_complete=True,
        )
        for required in spec.required_artifacts:
            _, required_artifact = read_bounded_artifact(
                required, maximum=spec.limits.max_profile_bytes, kind="external"
            )
            retained_artifacts[required_artifact.sha256] = required_artifact
        for path, captured in zip(spec.external_inputs, inputs, strict=True):
            _, current = read_bounded_artifact(
                path, maximum=spec.limits.max_profile_bytes, kind="external"
            )
            if current != captured:
                raise OSError(errno.EBADMSG, "external input changed during runtime collection")
        if _capture_sources(source_report, spec.repo) != sources:
            raise OSError(errno.EBADMSG, "source changed during runtime collection")
        roots = [RuntimeRoot(physical=spec.repo, logical="project", classification="project")]
        for name, classification in (("stdlib", "standard-library"), ("purelib", "third-party")):
            root = Path(sysconfig.get_path(name))
            if root not in {item.physical for item in roots}:
                roots.append(
                    RuntimeRoot(
                        physical=root, logical=classification, classification=classification
                    )
                )
        metadata = CollectionMetadata(
            collection=identity,
            process=proof,
            roots=tuple(roots),
            source_hashes=sources,
            observed_status=status,
        )
        event(LifecycleState.INGESTION, "Normalizing serialized subject-owned profile.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OSError(errno.ETIMEDOUT, "runtime deadline exhausted before ingestion")
        report, derived = ingest_profiles(
            (spec.profile_path,),
            config=spec.limits.model_copy(
                update={"ingestion_seconds": min(spec.limits.ingestion_seconds, remaining)}
            ),
            metadata=metadata,
            source_report=source_report,
        )
        if time.monotonic() >= deadline:
            raise OSError(errno.ETIMEDOUT, "runtime deadline exhausted during ingestion")
        for name, value in derived.items():
            (local / name).write_bytes(value)
        (local / "collection-metadata.json").write_text(
            metadata.model_dump_json(), encoding="utf-8"
        )
    except (OSError, ValueError) as exc:
        failure = exc.errno if isinstance(exc, OSError) and exc.errno else errno.EBADMSG
        event(
            LifecycleState.FAILED,
            "Runtime collection failed; inspect restricted local artifacts.",
            failure,
        )
    finally:
        shutdown_status = None
        if subject:
            try:
                if subject.shutdown(
                    graceful=getattr(signal, spec.graceful_signal),
                    graceful_seconds=spec.limits.graceful_seconds,
                    forced_seconds=spec.limits.forced_seconds,
                    deadline=deadline,
                ):
                    failure = failure or errno.ETIMEDOUT
                    shutdown_status = errno.ETIMEDOUT
            except OSError as exc:
                failure = failure or exc.errno or errno.EPROTO
                shutdown_status = exc.errno or errno.EPROTO
            status = subject.process.poll()
            try:
                _retain_step(
                    subject,
                    local,
                    "subject",
                    observations,
                    OSError(failure, "capture failed") if failure else None,
                    shutdown_status,
                )
            except OSError as exc:
                failure = failure or exc.errno or errno.EPROTO
            finally:
                subject.close()
        cleanup_deadline = min(deadline, time.monotonic() + spec.limits.cleanup_seconds)
        for index, command in enumerate(spec.cleanup):
            try:
                _run_command(
                    command,
                    spec,
                    environment,
                    cleanup_deadline,
                    local,
                    f"cleanup-{index}",
                    observations=observations,
                )
                event(LifecycleState.CLEANUP, f"Cleanup step {index} completed.", 0)
            except OSError as exc:
                failure = failure or exc.errno or errno.EPROTO
                event(LifecycleState.CLEANUP, f"Cleanup step {index} failed.", exc.errno)
    if report is None:
        report = ProfileReport(
            tool_version=__version__,
            profile_identity_sha256="0" * 64,
            source_report_sha256=None,
            workload_identity_sha256=identity.workload_sha256,
            repetitions=1,
            expected_status=spec.subject.expected_status,
            observed_statuses=(status,),
            frames=(),
            runtime_edges=(),
            statistics=(),
            resources=(ResourceObservation(repetition=1),),
            providers=(
                ProviderResult(
                    provider_id="managed-cprofile",
                    state=ProviderState.FAILED,
                    duration_ms=0,
                    completeness=Completeness.FAILED,
                ),
            ),
            diagnostics=(
                Diagnostic(
                    code="runtime-collection-failed",
                    level=DiagnosticLevel.ERROR,
                    message="Managed capture failed; no complete runtime evidence is available.",
                ),
            ),
            completeness=Completeness.FAILED,
            limitations=("Failed captures cannot support runtime comparisons.",),
            runtime=RuntimeEvidence(collection=identity),
        )
    event(
        LifecycleState.PUBLICATION,
        "Publishing validated portable evidence; local streams remain private.",
    )
    complete = (
        not failure
        and report.runtime.source_binding == SourceBindingState.MATCHED
        and not identity.missing_fields()
        and not report.diagnostics
        and subject is not None
        and len(subject.owned) == 1
    )
    runtime = report.runtime.model_copy(
        update={
            "process": proof if report.runtime.artifacts else RuntimeProcessProof(),
            "lifecycle": tuple(events),
            "steps": tuple(observations),
            "artifacts": tuple(
                sorted(
                    {
                        **retained_artifacts,
                        **{item.sha256: item for item in report.runtime.artifacts},
                    }.values(),
                    key=lambda item: item.sha256,
                )
            ),
        }
    )
    updated = report.model_copy(
        update={
            "runtime": runtime,
            "diagnostics": (
                *report.diagnostics,
                *(
                    (
                        Diagnostic(
                            code="runtime-descendant-scope-unresolved",
                            level=DiagnosticLevel.WARNING,
                            message="Descendants were observed; sampled ownership is not "
                            "exhaustive process containment.",
                        ),
                    )
                    if subject is not None and len(subject.owned) > 1
                    else ()
                ),
            ),
            "observed_statuses": (status,),
            "expected_status": spec.subject.expected_status,
            "resources": (subject.resources() if subject else ResourceObservation(repetition=1),),
            "providers": (
                ProviderResult(
                    provider_id="managed-cprofile",
                    state=(
                        ProviderState.FAILED
                        if failure
                        else (ProviderState.SUCCESS if complete else ProviderState.PARTIAL)
                    ),
                    duration_ms=0,
                    exit_code=status,
                    stdout_sha256=hashlib.sha256(subject.stdout).hexdigest() if subject else None,
                    stderr_sha256=hashlib.sha256(subject.stderr).hexdigest() if subject else None,
                    completeness=(
                        Completeness.FAILED
                        if failure
                        else (Completeness.COMPLETE if complete else Completeness.PARTIAL)
                    ),
                ),
            ),
            "limitations": tuple(
                sorted(
                    {
                        text
                        for text in report.limitations
                        if not text.startswith("Standalone pstats evidence")
                    }
                    | {
                        "CPU/RSS samples use 100 ms intervals; short-lived children may be missed. "
                        "I/O counters are recorded only where supported; "
                        "absent telemetry is not zero.",
                        "Execution deadlines exclude bounded emergency reaping "
                        "and report publication; "
                        "cleanup commands share the remaining run budget.",
                        "Supplied sidecars are local evidence, not cryptographic attestation.",
                        "Descendant discovery is sampled, not OS containment. "
                        "Fast fork/reparent or daemonization may escape observation; "
                        "observed descendants prevent complete managed scope claims.",
                    }
                )
            ),
            "completeness": (
                Completeness.FAILED
                if failure
                else (Completeness.COMPLETE if complete else Completeness.PARTIAL)
            ),
        }
    )
    updated = updated.model_copy(
        update={"profile_identity_sha256": profile_content_identity(updated)}
    )
    updated = ProfileReport.model_validate(updated.model_dump())
    manifest = publish_runtime_report(updated, spec.output)
    return RuntimeRunResult(
        updated,
        failure or (0 if manifest.completeness == Completeness.COMPLETE else errno.ENODATA),
        local,
    )
