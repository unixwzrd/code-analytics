from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, StrictStr

from code_analysis.context import AnalysisContext
from code_analysis.models import (
    AvailabilityState,
    Completeness,
    Diagnostic,
    DiagnosticLevel,
    ProviderAvailability,
    ProviderResult,
    ProviderState,
)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    provider_id: str
    command: str | None = None
    version_args: tuple[str, ...] = ("--version",)
    import_name: str | None = None
    distribution: str | None = None


TOOL_SPECS = (
    ToolSpec("function-analysis", "function-analysis", ()),
    ToolSpec("pyreverse", "pyreverse"),
    ToolSpec("graphviz", "dot", ("-V",)),
    ToolSpec("ruff", "ruff"),
    ToolSpec("black", "black"),
    ToolSpec("mypy", "mypy"),
    ToolSpec("pyright", "pyright"),
    ToolSpec("pytest", "pytest"),
    ToolSpec("coverage", "coverage"),
    ToolSpec("radon", "radon"),
    ToolSpec("lizard", "lizard"),
    ToolSpec("vulture", "vulture"),
    ToolSpec("libcst", import_name="libcst", distribution="libcst"),
    ToolSpec("import-linter", "lint-imports", distribution="import-linter"),
    ToolSpec("tree-sitter", import_name="tree_sitter", distribution="tree-sitter"),
    ToolSpec(
        "tree-sitter-javascript",
        import_name="tree_sitter_javascript",
        distribution="tree-sitter-javascript",
    ),
    ToolSpec(
        "sentence-transformers",
        import_name="sentence_transformers",
        distribution="sentence-transformers",
    ),
    ToolSpec("ruby-prism", "ruby", ("-v",)),
)


class ObservationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    observations: tuple[StrictStr, ...]


def inventory_tools(*, portable: bool) -> tuple[ProviderAvailability, ...]:
    return tuple(_availability(spec, portable=portable) for spec in TOOL_SPECS)


def inventory_tool_paths() -> tuple[ProviderAvailability, ...]:
    """Resolve local provider paths without executing version commands."""
    results: list[ProviderAvailability] = []
    for spec in TOOL_SPECS:
        path = shutil.which(spec.command) if spec.command else None
        importable = (
            spec.import_name is not None and importlib.util.find_spec(spec.import_name) is not None
        )
        module_path = _module_path(spec.import_name) if importable else None
        available = path is not None or importable
        detail = (
            _javascript_grammar_detail()
            if available and spec.provider_id == "tree-sitter-javascript"
            else (
                "Prism availability requires executable validation"
                if available and spec.provider_id == "ruby-prism"
                else None
            )
        )
        results.append(
            ProviderAvailability(
                provider_id=spec.provider_id,
                state=(AvailabilityState.AVAILABLE if available else AvailabilityState.UNAVAILABLE),
                executable=path or module_path,
                version=(
                    _distribution_version(spec.distribution)
                    if available and spec.distribution
                    else None
                ),
                detail=(detail if available else "provider was not found without execution"),
            )
        )
    return tuple(results)


def _distribution_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _module_path(import_name: str | None) -> str | None:
    if import_name is None:
        return None
    spec = importlib.util.find_spec(import_name)
    return spec.origin if spec is not None else None


def _availability(spec: ToolSpec, *, portable: bool) -> ProviderAvailability:
    path = shutil.which(spec.command) if spec.command else None
    if spec.import_name and importlib.util.find_spec(spec.import_name) is None:
        return ProviderAvailability(
            provider_id=spec.provider_id,
            state=AvailabilityState.UNAVAILABLE,
            detail=f"Python distribution {spec.distribution or spec.import_name} is not installed",
        )
    if spec.command and path is None:
        return ProviderAvailability(
            provider_id=spec.provider_id,
            state=AvailabilityState.UNAVAILABLE,
            detail=f"executable {spec.command} was not found",
        )
    version: str | None = None
    detail: str | None = None
    if spec.version_args and path:
        try:
            completed = subprocess.run(
                [path, *spec.version_args],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = (completed.stdout or completed.stderr).strip().splitlines()
            version = " | ".join(output[:2]) or None
            if completed.returncode != 0:
                detail = f"version command exited {completed.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = f"version unavailable: {type(exc).__name__}"
    elif spec.distribution:
        try:
            version = importlib.metadata.version(spec.distribution)
        except importlib.metadata.PackageNotFoundError:
            version = None
    elif path:
        detail = "local script has no version command"
    if spec.provider_id == "tree-sitter-javascript":
        detail = _javascript_grammar_detail()
    if spec.provider_id == "ruby-prism" and path:
        try:
            prism = subprocess.run(
                [path, "-rprism", "-e", "print Prism::VERSION"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            prism = None
        if prism is None or prism.returncode:
            return ProviderAvailability(
                provider_id=spec.provider_id,
                state=AvailabilityState.UNAVAILABLE,
                executable=(path if not portable else Path(path).name),
                version=version,
                detail="Ruby executable is present but Prism is unavailable",
            )
        detail = f"Prism {prism.stdout.strip()}"
    module_path = _module_path(spec.import_name)
    local_path = path or module_path
    executable = local_path if not portable else (Path(local_path).name if local_path else None)
    return ProviderAvailability(
        provider_id=spec.provider_id,
        state=AvailabilityState.AVAILABLE,
        executable=executable,
        version=version,
        detail=detail,
    )


def _javascript_grammar_detail() -> str | None:
    try:
        import tree_sitter_javascript
        from tree_sitter import Language

        language = Language(tree_sitter_javascript.language())
    except (ImportError, OSError, TypeError, ValueError):
        return None
    semantic_version = language.semantic_version
    if semantic_version is None:
        return None
    semantic = ".".join(str(item) for item in semantic_version)
    return f"grammar ABI {language.abi_version}; language {semantic}"


class _ProviderExecutionFields(TypedDict):
    provider_id: str
    executable: str
    exit_code: int
    duration_ms: int
    stdout_sha256: str
    stderr_sha256: str


class JsonCommandProvider:
    """Generic strict JSON provider runner used by future external integrations."""

    def __init__(
        self,
        provider_id: str,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.provider_id = provider_id
        self.arguments = arguments
        self.timeout_seconds = timeout_seconds

    def availability(self, context: AnalysisContext) -> ProviderAvailability:
        del context
        executable = shutil.which(self.arguments[0])
        if executable is None:
            return ProviderAvailability(
                provider_id=self.provider_id,
                state=AvailabilityState.UNAVAILABLE,
                detail="provider executable was not found",
            )
        return ProviderAvailability(
            provider_id=self.provider_id,
            state=AvailabilityState.AVAILABLE,
            executable=Path(executable).name,
        )

    def run(self, context: AnalysisContext) -> ProviderResult:
        del context
        start = time.monotonic()
        executable = shutil.which(self.arguments[0])
        if executable is None:
            return _failed_result(
                self.provider_id,
                ProviderState.UNAVAILABLE,
                start,
                "provider-unavailable",
                "provider executable was not found",
            )
        environment = {
            key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}
        }
        try:
            completed = subprocess.run(
                [executable, *self.arguments[1:]],
                check=False,
                capture_output=True,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            return ProviderResult(
                provider_id=self.provider_id,
                state=ProviderState.TIMEOUT,
                executable=Path(executable).name,
                duration_ms=int((time.monotonic() - start) * 1000),
                stdout_sha256=_digest(exc.stdout or b""),
                stderr_sha256=_digest(exc.stderr or b""),
                diagnostics=(
                    Diagnostic(
                        code="provider-timeout",
                        level=DiagnosticLevel.ERROR,
                        message=f"provider exceeded {self.timeout_seconds:g} second timeout",
                        provider=self.provider_id,
                    ),
                ),
                completeness=Completeness.FAILED,
            )
        except OSError as exc:
            return _failed_result(
                self.provider_id,
                ProviderState.FAILED,
                start,
                "provider-execution-error",
                type(exc).__name__,
            )
        stdout = completed.stdout
        stderr = completed.stderr
        common: _ProviderExecutionFields = {
            "provider_id": self.provider_id,
            "executable": Path(executable).name,
            "exit_code": completed.returncode,
            "duration_ms": int((time.monotonic() - start) * 1000),
            "stdout_sha256": _digest(stdout),
            "stderr_sha256": _digest(stderr),
        }
        if completed.returncode != 0:
            return ProviderResult(
                **common,
                state=ProviderState.NONZERO,
                diagnostics=(
                    Diagnostic(
                        code="provider-nonzero",
                        level=DiagnosticLevel.ERROR,
                        message=f"provider exited with status {completed.returncode}",
                        provider=self.provider_id,
                    ),
                ),
                completeness=Completeness.FAILED,
            )
        try:
            envelope = ObservationEnvelope.model_validate_json(stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            return ProviderResult(
                **common,
                state=ProviderState.MALFORMED,
                diagnostics=(
                    Diagnostic(
                        code="provider-malformed-output",
                        level=DiagnosticLevel.ERROR,
                        message=f"provider output failed strict validation: {type(exc).__name__}",
                        provider=self.provider_id,
                    ),
                ),
                completeness=Completeness.FAILED,
            )
        return ProviderResult(
            **common,
            state=ProviderState.SUCCESS,
            observations=envelope.observations,
            completeness=Completeness.COMPLETE,
        )


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _failed_result(
    provider_id: str,
    state: ProviderState,
    start: float,
    code: str,
    message: str,
) -> ProviderResult:
    return ProviderResult(
        provider_id=provider_id,
        state=state,
        duration_ms=int((time.monotonic() - start) * 1000),
        diagnostics=(
            Diagnostic(
                code=code,
                level=DiagnosticLevel.ERROR,
                message=message,
                provider=provider_id,
            ),
        ),
        completeness=Completeness.FAILED,
    )


def doctor_document() -> str:
    payload = {
        "python": {
            "path": sys.executable,
            "version": sys.version.split()[0],
            "implementation": sys.implementation.name,
            "conda_environment": os.environ.get("CONDA_DEFAULT_ENV"),
        },
        "providers": [item.model_dump(mode="json") for item in inventory_tools(portable=False)],
    }
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"
