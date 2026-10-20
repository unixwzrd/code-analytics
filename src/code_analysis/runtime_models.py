"""Portable runtime identity, provenance, correlation, and publication records."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.models import Completeness, ComplexityObservation, StrictModel
from code_analysis.runtime_spec import RuntimeCategoryRule

Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


def runtime_digest(value: StrictModel) -> str:
    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class RuntimeArtifact(StrictModel):
    sha256: Digest
    byte_count: StrictInt = Field(ge=0)
    kind: StrictStr = Field(pattern=r"^(profile|metadata|workload|external)$")


class SourceBindingState(StrEnum):
    UNBOUND = "unbound"
    MATCHED = "matched"
    MISMATCHED = "mismatched"
    UNRESOLVED = "unresolved"


class RuntimeSourceHash(StrictModel):
    path: StrictStr = Field(min_length=1)
    sha256: Digest

    @model_validator(mode="after")
    def portable_path(self) -> Self:
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in self.path
            or path.as_posix() != self.path
        ):
            raise ValueError("runtime source path must be normalized and portable")
        return self


class CollectionIdentity(StrictModel):
    """Semantic inputs only; no PIDs, clock times, temporary paths, or secrets."""

    source_fingerprint_sha256: Digest | None = None
    interpreter_identity: StrictStr | None = None
    platform_identity: StrictStr | None = None
    dependencies_sha256: Digest | None = None
    semantic_argv: tuple[StrictStr, ...] | None = None
    workload_sha256: Digest | None = None
    external_artifacts: tuple[Digest, ...] = ()
    profiler_configuration_sha256: Digest | None = None
    collection_scope: StrictStr | None = None

    @model_validator(mode="after")
    def portable_identity(self) -> Self:
        for value in (
            self.interpreter_identity,
            self.platform_identity,
            self.collection_scope,
            *(self.semantic_argv or ()),
        ):
            if value and ("/Users/" in value or "/Volumes/" in value or value.startswith("/")):
                raise ValueError("collection identity cannot contain physical paths")
        if self.external_artifacts != tuple(sorted(set(self.external_artifacts))):
            raise ValueError("external artifacts must be unique and sorted")
        return self

    def missing_fields(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                name
                for name in type(self).model_fields
                if name != "external_artifacts" and getattr(self, name) is None
            )
        )


class ProcessProofState(StrEnum):
    UNAVAILABLE = "unavailable"
    DECLARED = "declared"
    VERIFIED = "verified"
    MISMATCHED = "mismatched"


class RuntimeProcessProof(StrictModel):
    state: ProcessProofState = ProcessProofState.UNAVAILABLE
    subject_pid: StrictInt | None = Field(default=None, ge=1)
    parent_pid: StrictInt | None = Field(default=None, ge=1)
    start_identity: StrictStr | None = None
    run_nonce: StrictStr | None = None
    profile_sha256: Digest | None = None
    serialization_complete: StrictBool | None = None

    @model_validator(mode="after")
    def verified_proof(self) -> Self:
        if self.state == ProcessProofState.VERIFIED and (
            self.subject_pid is None
            or not self.start_identity
            or not self.run_nonce
            or self.profile_sha256 is None
            or self.serialization_complete is not True
        ):
            raise ValueError(
                "verified process proof requires owner, start, nonce and serialized artifact"
            )
        return self


class LifecycleState(StrEnum):
    VALIDATED = "validated"
    STARTING = "starting"
    READY = "ready"
    WORKLOAD = "workload"
    GRACEFUL_STOP = "graceful-stop"
    PROFILE_VALIDATION = "profile-validation"
    INGESTION = "ingestion"
    PUBLICATION = "publication"
    CLEANUP = "cleanup"
    FAILED = "failed"


class LifecycleOutcome(StrictModel):
    sequence: StrictInt = Field(ge=0)
    state: LifecycleState
    status: StrictInt | None = None
    reason: StrictStr = Field(min_length=1, max_length=512)


class RuntimeStepObservation(StrictModel):
    """Captured local streams and statuses, never their potentially private contents."""

    step: StrictStr = Field(pattern=r"^(subject|readiness-[0-9]+|workload-[0-9]+|cleanup-[0-9]+)$")
    observed_status: StrictInt | None = None
    failure_status: StrictInt | None = Field(default=None, gt=0)
    shutdown_status: StrictInt | None = Field(default=None, gt=0)
    stdout_sha256: Digest
    stderr_sha256: Digest
    stdout_bytes: StrictInt = Field(ge=0)
    stderr_bytes: StrictInt = Field(ge=0)
    stream_truncated: StrictBool = False


class CategoryMatch(StrictModel):
    frame_id: StrictStr
    primary_category: StrictStr | None = None
    rule_ids: tuple[StrictStr, ...] = ()
    categories: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def matched_primary(self) -> Self:
        if self.primary_category is not None and self.primary_category not in self.categories:
            raise ValueError("primary category must be among matched categories")
        if len(set(self.rule_ids)) != len(self.rule_ids) or self.categories != tuple(
            sorted(set(self.categories))
        ):
            raise ValueError("category matches must be unique and categories sorted")
        return self


class RuntimeCorrelation(StrictModel):
    frame_id: StrictStr
    state: SourceBindingState
    subject_ids: tuple[StrictStr, ...] = ()
    evidence_ids: tuple[StrictStr, ...] = ()
    related_ids: tuple[StrictStr, ...] = ()
    module: StrictStr | None = None
    complexity: ComplexityObservation | None = None
    limitations: tuple[StrictStr, ...] = ()


class RuntimeEvidence(StrictModel):
    artifacts: tuple[RuntimeArtifact, ...] = ()
    collection: CollectionIdentity = CollectionIdentity()
    source_binding: SourceBindingState = SourceBindingState.UNBOUND
    source_hashes: tuple[RuntimeSourceHash, ...] = ()
    process: RuntimeProcessProof = RuntimeProcessProof()
    lifecycle: tuple[LifecycleOutcome, ...] = ()
    steps: tuple[RuntimeStepObservation, ...] = ()
    categories: tuple[CategoryMatch, ...] = ()
    category_rules: tuple[RuntimeCategoryRule, ...] = ()
    correlations: tuple[RuntimeCorrelation, ...] = ()
    unobserved_subject_ids: tuple[StrictStr, ...] = ()
    original_frame_count: StrictInt = Field(default=0, ge=0)
    original_edge_count: StrictInt = Field(default=0, ge=0)
    caller_only_frame_ids: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        ids = [item.sha256 for item in self.artifacts]
        if ids != sorted(set(ids)):
            raise ValueError("runtime artifact digests must be unique and sorted")
        paths = [item.path for item in self.source_hashes]
        if paths != sorted(set(paths)):
            raise ValueError("runtime source paths must be unique and sorted")
        if self.source_binding == SourceBindingState.MATCHED and not self.source_hashes:
            raise ValueError("matched source binding requires captured source hashes")
        sequences = [item.sequence for item in self.lifecycle]
        if sequences != list(range(len(sequences))):
            raise ValueError("lifecycle sequence must be contiguous and ordered")
        steps = [item.step for item in self.steps]
        if len(steps) != len(set(steps)):
            raise ValueError("runtime step identities must be unique")
        rules = {item.rule_id: item for item in self.category_rules}
        if list(rules) != sorted(rules) or len(rules) != len(self.category_rules):
            raise ValueError("runtime category rules must be unique and sorted")
        for match in self.categories:
            if any(rule_id not in rules for rule_id in match.rule_ids):
                raise ValueError("runtime category references an unknown rule")
            matched = sorted(
                (rules[key] for key in match.rule_ids),
                key=lambda rule: (rule.priority, rule.rule_id),
            )
            if match.rule_ids != tuple(
                rule.rule_id for rule in matched
            ) or match.categories != tuple(sorted({rule.category for rule in matched})):
                raise ValueError("runtime category definitions disagree with matches")
            if match.primary_category != (matched[0].category if matched else None):
                raise ValueError("runtime primary category disagrees with rule priority")
        if self.process.profile_sha256 and self.process.profile_sha256 not in ids:
            raise ValueError("process proof references unknown profile artifact")
        if self.process.profile_sha256 and not any(
            item.sha256 == self.process.profile_sha256 and item.kind == "profile"
            for item in self.artifacts
        ):
            raise ValueError("process proof must reference a profile artifact")
        return self


class CompatibilityDecision(StrictModel):
    field: StrictStr
    outcome: StrictStr = Field(pattern=r"^(matched|mismatched|unresolved)$")
    reason: StrictStr


class RuntimeSelection(StrictModel):
    max_records: StrictInt = Field(default=500, ge=1, le=10000)
    max_nodes: StrictInt = Field(default=100, ge=1, le=10000)
    max_edges: StrictInt = Field(default=250, ge=1, le=100000)
    direction: StrictStr = Field(default="TB", pattern=r"^(TB|LR)$")
    project_only: StrictBool = False


class RuntimeArtifactEntry(StrictModel):
    path: StrictStr
    sha256: Digest | None = None
    byte_count: StrictInt | None = Field(default=None, ge=1)
    status: StrictStr = Field(pattern=r"^(complete|partial|failed|unavailable)$")
    reason: StrictStr | None = None

    @model_validator(mode="after")
    def confined_file(self) -> Self:
        if (
            PurePosixPath(self.path).name != self.path
            or self.path in {"", ".", "..", "manifest.json"}
            or "\\" in self.path
        ):
            raise ValueError("manifest artifact must be a direct file other than the manifest")
        if (self.sha256 is None) != (self.byte_count is None):
            raise ValueError("artifact size and digest must be present together")
        materialized = self.status in {"complete", "partial"}
        if materialized and self.sha256 is None:
            raise ValueError("materialized artifact requires digest and size")
        if not materialized and self.sha256 is not None:
            raise ValueError("non-materialized artifact cannot have digest or size")
        if self.status in {"failed", "unavailable"} and self.reason is None:
            raise ValueError("non-materialized artifact requires a reason")
        if self.status == "complete" and self.reason is not None:
            raise ValueError("complete artifact cannot have a reason")
        return self


class RuntimeManifest(StrictModel):
    schema_name: StrictStr = Field(
        default="code-analysis-runtime-manifest", pattern=r"^code-analysis-runtime-manifest$"
    )
    schema_version: StrictStr = Field(default="1.0.0", pattern=r"^1\.0\.0$")
    source_json_sha256: Digest
    selection: RuntimeSelection
    original_nodes: StrictInt = Field(ge=0)
    retained_nodes: StrictInt = Field(ge=0)
    original_edges: StrictInt = Field(ge=0)
    retained_edges: StrictInt = Field(ge=0)
    completeness: Completeness
    artifacts: tuple[RuntimeArtifactEntry, ...]
    limitations: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def valid_manifest(self) -> Self:
        paths = [item.path for item in self.artifacts]
        if paths != sorted(set(paths)):
            raise ValueError("manifest artifacts must be unique and sorted")
        summaries = [item for item in self.artifacts if item.path == "runtime-summary.json"]
        if len(summaries) != 1:
            raise ValueError("manifest requires exactly one authoritative runtime summary")
        summary = summaries[0]
        if summary.status != "complete" or summary.sha256 != self.source_json_sha256:
            raise ValueError("manifest source digest must match complete authoritative summary")
        if self.retained_nodes > self.original_nodes or self.retained_edges > self.original_edges:
            raise ValueError("retained graph cannot exceed original")
        incomplete = (
            self.retained_nodes < self.original_nodes
            or self.retained_edges < self.original_edges
            or any(item.status != "complete" for item in self.artifacts)
        )
        if incomplete and self.completeness == Completeness.COMPLETE:
            raise ValueError("incomplete artifacts cannot claim complete publication")
        return self
