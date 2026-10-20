"""Bind a completed collection's captured report to one exact owned generation."""

from __future__ import annotations

import errno
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, StrictStr, model_validator

from code_analysis.collection_execution import CollectionExecutionResult, CollectionProcessIdentity
from code_analysis.models import StrictModel
from code_analysis.profile_models import ProfileReport
from code_analysis.profiling import render_profile_json
from code_analysis.reports.json_report import canonical_json
from code_analysis.workspace import (
    GenerationKind,
    open_workspace,
    read_generation,
    validate_workspace_ownership,
)


class CollectionArtifactBinding(StrictModel):
    workspace_path: StrictStr
    generation_id: StrictStr = Field(pattern=r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{12}$")
    kind: Literal[GenerationKind.ANALYSIS, GenerationKind.PROFILE]
    artifact_name: Literal["report.json", "profile.json"]
    process: CollectionProcessIdentity
    artifact_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    report_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    limitations: tuple[StrictStr, ...] = (
        "Binding records a verification event; revalidate before opening retained artifacts.",
        "Same-user target code is not isolated by an operating-system sandbox.",
    )

    @model_validator(mode="after")
    def reconcile(self) -> Self:
        expected = "profile.json" if self.kind == GenerationKind.PROFILE else "report.json"
        if self.artifact_name != expected or not Path(self.workspace_path).is_absolute():
            raise ValueError(
                "binding requires an absolute local workspace and matching artifact kind"
            )
        return self


def bind_collection_generation(
    result: CollectionExecutionResult, workspace_path: Path, generation_id: str
) -> CollectionArtifactBinding:
    """Reject latest aliases, unrelated jobs, changed content and malformed artifacts."""
    receipt, report = result.receipt, result.report
    if not receipt.report_validated or report is None or receipt.process is None:
        raise OSError(errno.ENODATA, "collection has no validated captured report")
    for payload, size, digest in (
        (result.stdout, receipt.stdout_bytes, receipt.stdout_sha256),
        (result.stderr, receipt.stderr_bytes, receipt.stderr_sha256),
    ):
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            raise OSError(errno.EBADMSG, "collection stream does not match its receipt")
    profile = isinstance(report, ProfileReport)
    if profile != (receipt.command.operation in {"profile", "import-profile"}):
        raise OSError(errno.EBADMSG, "collection operation and report kind disagree")
    captured = (
        render_profile_json(report, compact=True)
        if isinstance(report, ProfileReport)
        else canonical_json(report)
    )
    if captured.encode() != result.stdout:
        raise OSError(errno.EBADMSG, "captured report does not match validated report values")
    kind: Literal[GenerationKind.PROFILE, GenerationKind.ANALYSIS] = (
        GenerationKind.PROFILE if profile else GenerationKind.ANALYSIS
    )
    workspace = open_workspace(workspace_path)
    manifest = read_generation(workspace, kind, generation_id)
    marker = validate_workspace_ownership(workspace)
    created = datetime.fromisoformat(manifest.created_at).timestamp()
    command_prefix = (
        ("code-analysis", "runtime", "ingest")
        if receipt.command.operation == "import-profile"
        else ("code-analysis", receipt.command.operation)
    )
    if (
        manifest.process_id != receipt.process.pid
        or manifest.process_start_time != receipt.process.start_time
        or created < receipt.process.start_time
        or created > datetime.now(UTC).timestamp()
        or manifest.interpreter_path != receipt.command.interpreter
        or manifest.project_root != marker.project_root
        or manifest.command[: len(command_prefix)] != command_prefix
    ):
        raise OSError(errno.EBADMSG, "generation does not belong to the captured invocation")
    report_hash = hashlib.sha256(result.stdout).hexdigest()
    portable_identity = (
        report.profile_identity_sha256 if isinstance(report, ProfileReport) else report_hash
    )
    if manifest.portable_identity_sha256 != portable_identity:
        raise OSError(errno.EBADMSG, "generation portable report identity mismatch")
    name: Literal["profile.json", "report.json"] = "profile.json" if profile else "report.json"
    artifact = next((item for item in manifest.artifacts if item.name == name), None)
    persisted = (
        render_profile_json(report).encode() if isinstance(report, ProfileReport) else result.stdout
    )
    artifact_hash = hashlib.sha256(persisted).hexdigest()
    if artifact is None or artifact.sha256 != artifact_hash or artifact.bytes != len(persisted):
        raise OSError(errno.EBADMSG, "generation report bytes differ from captured evidence")
    return CollectionArtifactBinding(
        workspace_path=str(workspace.root),
        generation_id=generation_id,
        kind=kind,
        artifact_name=name,
        artifact_sha256=artifact_hash,
        report_sha256=report_hash,
        process=receipt.process,
    )


def discover_collection_generation(
    result: CollectionExecutionResult, workspace_path: Path, *, maximum_generations: int = 500
) -> CollectionArtifactBinding:
    """Select only a unique generation created by the proven collection process."""
    if result.receipt.process is None or not result.receipt.report_validated:
        raise OSError(errno.ENODATA, "collection has no verified process/report identity")
    if type(maximum_generations) is not int or not 1 <= maximum_generations <= 10_000:
        raise ValueError("invalid generation discovery bound")
    workspace = open_workspace(workspace_path)
    kind = (
        GenerationKind.PROFILE
        if result.receipt.command.operation in {"profile", "import-profile"}
        else GenerationKind.ANALYSIS
    )
    candidates = []
    with os.scandir(workspace.kind_directory(kind)) as entries:
        for count, child in enumerate(entries):
            if count > maximum_generations * 2:
                raise OSError(errno.ENODATA, "generation directory inventory exceeded its bound")
            if child.name == "latest" or child.name.startswith("."):
                continue
            candidates.append(child.name)
            if len(candidates) > maximum_generations:
                raise OSError(errno.ENODATA, "generation discovery exceeded its bound")
    matches = []
    for identity in sorted(candidates):
        try:
            manifest = read_generation(workspace, kind, identity)
        except OSError as exc:
            if exc.errno == errno.EINVAL:
                raise OSError(errno.EBADMSG, "retained generation has an invalid identity") from exc
            raise
        if (manifest.process_id, manifest.process_start_time) == (
            result.receipt.process.pid,
            result.receipt.process.start_time,
        ):
            matches.append(bind_collection_generation(result, workspace_path, identity))
    if len(matches) != 1:
        raise OSError(
            errno.ENODATA if not matches else errno.EBADMSG,
            "collection generation is missing or ambiguous",
        )
    return matches[0]
