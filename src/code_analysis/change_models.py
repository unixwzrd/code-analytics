"""Persisted Git and retained-generation change evidence contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictStr, model_validator

from code_analysis.models import Completeness, Diagnostic, ProviderResult, StrictModel
from code_analysis.report_models import (
    ChangeCouplingRecord,
    GenerationChangeRecord,
    GitCommitObservation,
    GitPathChangeRecord,
    GitRepositoryIdentity,
)


class ChangeEvidenceSource(StrEnum):
    GIT = "git"
    GENERATIONS = "generations"


class ChangeReport(StrictModel):
    schema_name: Literal["code-analysis-change-report"] = "code-analysis-change-report"
    schema_version: Literal["1.0.0"] = "1.0.0"
    tool_version: StrictStr
    change_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    target_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source: ChangeEvidenceSource
    git_repository: GitRepositoryIdentity | None = None
    git_commits: tuple[GitCommitObservation, ...] = ()
    git_path_changes: tuple[GitPathChangeRecord, ...] = ()
    generation_changes: tuple[GenerationChangeRecord, ...] = ()
    coupling: tuple[ChangeCouplingRecord, ...] = ()
    providers: tuple[ProviderResult, ...] = ()
    diagnostics: tuple[Diagnostic, ...]
    completeness: Completeness
    limitations: tuple[StrictStr, ...]

    @model_validator(mode="after")
    def validate_order_and_source(self) -> Self:
        for label, records, attribute in (
            ("Git commit", self.git_commits, "commit_id"),
            ("Git path change", self.git_path_changes, "path_change_id"),
            ("generation change", self.generation_changes, "generation_change_id"),
            ("change coupling", self.coupling, "coupling_id"),
        ):
            values = [getattr(record, attribute) for record in records]
            if values != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"{label} IDs must be unique and sorted")
        if self.source is ChangeEvidenceSource.GIT:
            if self.generation_changes:
                raise ValueError("Git change report cannot contain generation changes")
            commit_ids = {item.commit_id for item in self.git_commits}
            if any(item.commit_id not in commit_ids for item in self.git_path_changes):
                raise ValueError("Git path change references an unknown commit")
            known_paths = {item.path for item in self.git_path_changes}
            for item in self.coupling:
                if not set(item.commit_ids) <= commit_ids:
                    raise ValueError("Git coupling references an unknown commit")
                if item.left_path not in known_paths or item.right_path not in known_paths:
                    raise ValueError("Git coupling references an unknown path")
        elif self.git_repository is not None or self.git_commits or self.git_path_changes:
            raise ValueError("generation change report cannot contain Git records")
        else:
            interval_ids = {item.generation_change_id for item in self.generation_changes}
            if any(not set(item.commit_ids) <= interval_ids for item in self.coupling):
                raise ValueError("generation coupling references an unknown interval")
        if list(self.limitations) != sorted(self.limitations) or len(self.limitations) != len(
            set(self.limitations)
        ):
            raise ValueError("change report limitations must be unique and sorted")
        return self
