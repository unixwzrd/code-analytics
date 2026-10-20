"""Strict local runtime specifications; parsing never authorizes execution."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, StrictFloat, StrictInt, StrictStr, model_validator

from code_analysis.models import StrictModel

Seconds = Annotated[StrictFloat, Field(gt=0.0, le=86400.0, allow_inf_nan=False)]


class ArgumentRole(StrEnum):
    EXECUTABLE = "executable"
    MODULE = "module"
    SCRIPT = "script"
    ARGUMENT = "argument"
    OUTPUT_PATH = "output-path"
    PRIVATE_PATH = "private-path"
    SECRET_REFERENCE = "secret-reference"


class RuntimeArgument(StrictModel):
    role: ArgumentRole
    value: StrictStr = Field(min_length=1, max_length=16384)
    semantic_identity: StrictStr | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def valid_argument(self) -> Self:
        if "\0" in self.value:
            raise ValueError("argv cannot contain NUL")
        return self

    def semantic_token(self) -> str | None:
        if self.role == ArgumentRole.OUTPUT_PATH:
            return "output-path"
        if self.role in {ArgumentRole.PRIVATE_PATH, ArgumentRole.SECRET_REFERENCE}:
            return self.semantic_identity
        if self.role in {ArgumentRole.EXECUTABLE, ArgumentRole.SCRIPT}:
            return self.semantic_identity
        return self.value


class RuntimeCommand(StrictModel):
    arguments: tuple[RuntimeArgument, ...] = Field(min_length=1, max_length=256)
    expected_status: StrictStr = "SUCCESS"
    timeout_seconds: Seconds = 60.0

    @model_validator(mode="after")
    def executable_first(self) -> Self:
        if self.arguments[0].role != ArgumentRole.EXECUTABLE:
            raise ValueError("first argv role must be executable")
        if any(item.role == ArgumentRole.EXECUTABLE for item in self.arguments[1:]):
            raise ValueError("executable role may appear only first")
        from code_analysis.runtime_execution import resolve_expected_status

        try:
            resolve_expected_status(self.expected_status)
        except OSError as exc:
            raise ValueError("unknown symbolic expected status") from exc
        return self

    def argv(self) -> tuple[str, ...]:
        return tuple(item.value for item in self.arguments)


class RuntimeCategoryRule(StrictModel):
    rule_id: StrictStr = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    category: StrictStr = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    priority: StrictInt = Field(default=100, ge=0, le=10000)
    path_pattern: StrictStr = Field(default="*", min_length=1, max_length=256)
    function_pattern: StrictStr = Field(default="*", min_length=1, max_length=256)

    @model_validator(mode="after")
    def portable_patterns(self) -> Self:
        if self.path_pattern.startswith("/") or "\\" in self.path_pattern:
            raise ValueError("category patterns use logical portable paths")
        if ".." in self.path_pattern.split("/"):
            raise ValueError("category patterns cannot escape logical roots")
        return self


class RuntimeAnalysisConfig(StrictModel):
    total_seconds: Seconds = 300.0
    readiness_seconds: Seconds = 30.0
    readiness_interval_seconds: Seconds = 0.25
    workload_seconds: Seconds = 60.0
    graceful_seconds: Seconds = 10.0
    forced_seconds: Seconds = 5.0
    cleanup_seconds: Seconds = 10.0
    ingestion_seconds: Seconds = 30.0
    max_profile_bytes: StrictInt = Field(default=67108864, ge=1, le=1073741824)
    max_metadata_bytes: StrictInt = Field(default=4194304, ge=1, le=67108864)
    max_stream_bytes: StrictInt = Field(default=8388608, ge=1, le=67108864)
    max_frames: StrictInt = Field(default=100000, ge=1, le=1000000)
    max_edges: StrictInt = Field(default=250000, ge=1, le=2000000)
    max_inputs: StrictInt = Field(default=20, ge=1, le=20)
    max_workloads: StrictInt = Field(default=32, ge=1, le=256)
    max_readiness_attempts: StrictInt = Field(default=128, ge=1, le=10000)
    category_rules: tuple[RuntimeCategoryRule, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def unique_rules(self) -> Self:
        ids = [item.rule_id for item in self.category_rules]
        if ids != sorted(set(ids)):
            raise ValueError("category rule IDs must be unique and sorted")
        if self.readiness_interval_seconds > self.readiness_seconds:
            raise ValueError("readiness interval exceeds readiness deadline")
        return self


class RunnerMode(StrEnum):
    PYTHON_MODULE = "python-module"
    APPLICATION_MANAGED = "application-managed"


class EnvironmentAddition(StrictModel):
    name: StrictStr = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value: StrictStr
    semantic_identity: StrictStr | None = None


class ReadinessProbe(StrictModel):
    url: StrictStr | None = None
    command: RuntimeCommand | None = None
    semantic_identity: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    expected_http_status: StrictInt = Field(default=200, ge=100, le=599)

    @model_validator(mode="after")
    def one_probe(self) -> Self:
        if (self.url is None) == (self.command is None):
            raise ValueError("readiness requires exactly one URL or command")
        if self.url is not None:
            from urllib.parse import urlsplit

            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("readiness URL requires HTTP(S) and a host")
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError("readiness URL cannot contain credentials or fragment")
        return self


class RuntimeRunSpec(StrictModel):
    """Local-only specification: values and physical paths never enter portable JSON."""

    mode: RunnerMode
    repo: Path
    cwd: Path
    output: Path
    profile_path: Path
    metadata_path: Path
    subject: RuntimeCommand
    readiness: ReadinessProbe | None = None
    workloads: tuple[RuntimeCommand, ...] = Field(default=(), max_length=256)
    cleanup: tuple[RuntimeCommand, ...] = Field(default=(), max_length=32)
    required_artifacts: tuple[Path, ...] = Field(default=(), max_length=256)
    external_inputs: tuple[Path, ...] = Field(default=(), max_length=256)
    environment_remove: tuple[StrictStr, ...] = ()
    environment_add: tuple[EnvironmentAddition, ...] = ()
    environment_semantic_identity: StrictStr | None = Field(
        default=None, min_length=1, max_length=256
    )
    graceful_signal: StrictStr = "SIGTERM"
    forced_signal: StrictStr = "SIGKILL"
    limits: RuntimeAnalysisConfig = RuntimeAnalysisConfig()

    @model_validator(mode="after")
    def validate_local_spec(self) -> Self:
        paths = (self.repo, self.cwd, self.output, self.profile_path, self.metadata_path)
        if any(
            not path.is_absolute()
            for path in (*paths, *self.required_artifacts, *self.external_inputs)
        ):
            raise ValueError("runtime paths must be explicit absolute local paths")
        if len(set(self.external_inputs)) != len(self.external_inputs):
            raise ValueError("external input paths must be unique")
        if self.profile_path == self.metadata_path:
            raise ValueError("profile and metadata paths must differ")
        names = [item.name for item in self.environment_add]
        if len(names) != len(set(names)) or len(set(self.environment_remove)) != len(
            self.environment_remove
        ):
            raise ValueError("environment assignments/removals must be unique")
        if set(names) & set(self.environment_remove):
            raise ValueError("environment cannot add and remove the same name")
        if len(self.workloads) > self.limits.max_workloads:
            raise ValueError("workload count exceeds configured limit")
        if self.workloads and self.readiness is None:
            raise ValueError("service workloads require a readiness probe")
        if self.graceful_signal not in {"SIGTERM", "SIGINT"} or self.forced_signal != "SIGKILL":
            raise ValueError("shutdown requires TERM/INT followed by KILL")
        return self
