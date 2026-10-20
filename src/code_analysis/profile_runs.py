"""Owned saved-profile catalog; distinguish artifact hashes from workload identities."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, StrictInt, StrictStr, model_validator

from code_analysis.models import StrictModel
from code_analysis.ui_runtime import RuntimeViewService
from code_analysis.workspace import GenerationKind, WorkspacePaths, list_generations


class ProfileRun(StrictModel):
    generation_id: StrictStr = Field(pattern=r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{12}$")
    created_at: StrictStr
    profile_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class ProfileRunCatalog(StrictModel):
    records: tuple[ProfileRun, ...] = Field(max_length=500)
    omitted: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="after")
    def reconcile(self) -> ProfileRunCatalog:
        ids = tuple(item.generation_id for item in self.records)
        if ids != tuple(sorted(set(ids), reverse=True)):
            raise ValueError("profile catalog identities must be unique and newest-first")
        return self


def profile_catalog(workspace: WorkspacePaths) -> ProfileRunCatalog:
    records = tuple(
        ProfileRun(
            generation_id=manifest.generation_id,
            created_at=manifest.created_at,
            profile_identity_sha256=manifest.portable_identity_sha256,
            artifact_sha256=artifact.sha256,
        )
        for manifest in reversed(list_generations(workspace, kind=GenerationKind.PROFILE))
        for artifact in manifest.artifacts
        if artifact.name == "profile.json"
    )
    return ProfileRunCatalog(records=records[:500], omitted=max(0, len(records) - 500))


def select_profiles(
    workspace: WorkspacePaths, baseline: str | None, candidate: str | None
) -> RuntimeViewService | None:
    if baseline is None:
        if candidate is not None:
            raise ValueError("a comparison profile requires a baseline profile")
        return None
    catalog = {item.generation_id: item for item in profile_catalog(workspace).records}
    selected = (baseline,) if candidate is None else (baseline, candidate)
    if any(item not in catalog for item in selected):
        raise ValueError("profile generation is absent from the bounded workspace catalog")

    def path(identity: str) -> Path:
        return workspace.kind_directory(GenerationKind.PROFILE) / identity / "profile.json"

    runtime = RuntimeViewService.load(
        path(baseline),
        path(candidate) if candidate else None,
        expected_sha256=catalog[baseline].artifact_sha256,
        expected_comparison_sha256=catalog[candidate].artifact_sha256 if candidate else None,
    )
    identities = (
        (runtime.profile.profile_identity_sha256,)
        if runtime.comparison is None
        else (runtime.profile.profile_identity_sha256, runtime.comparison.profile_identity_sha256)
    )
    if identities != tuple(catalog[item].profile_identity_sha256 for item in selected):
        raise ValueError("profile identity does not match its generation manifest")
    after = {item.generation_id: item for item in profile_catalog(workspace).records}
    if any(after.get(item) != catalog[item] for item in selected):
        raise ValueError("profile generation changed while loading")
    return runtime
