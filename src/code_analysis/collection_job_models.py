"""Strict local collection-job state; separate from portable analysis evidence."""

from __future__ import annotations

import errno
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, StrictInt, StrictStr, model_validator

from code_analysis.collection_artifacts import CollectionArtifactBinding
from code_analysis.collection_commands import CollectionCommand, CollectionRequest
from code_analysis.collection_execution import (
    CollectionExecutionLimits,
    CollectionExecutionReceipt,
    CollectionProcessIdentity,
)
from code_analysis.models import StrictModel


class CollectionJob(StrictModel):
    schema_name: Literal["code-analysis-collection-job"] = "code-analysis-collection-job"
    job_id: StrictStr = Field(pattern=r"^[a-f0-9]{32}$")
    created_at: StrictStr
    updated_at: StrictStr
    collection_root: StrictStr
    workspace_path: StrictStr
    request: CollectionRequest
    command: CollectionCommand
    limits: CollectionExecutionLimits
    state: Literal["queued", "running", "completed", "partial", "failed", "cancelled"]
    status: StrictInt | None = Field(default=None, ge=0)
    process: CollectionProcessIdentity | None = None
    receipt: CollectionExecutionReceipt | None = None
    binding: CollectionArtifactBinding | None = None
    reason: StrictStr = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def reconcile(self) -> Self:
        if (
            not Path(self.collection_root).is_absolute()
            or not Path(self.workspace_path).is_absolute()
        ):
            raise ValueError("job roots must be absolute local paths")
        if self.request.operation != self.command.operation:
            raise ValueError("job request and reviewed command disagree")
        created, updated = datetime.fromisoformat(self.created_at), datetime.fromisoformat(
            self.updated_at
        )
        if created.tzinfo is None or updated.tzinfo is None or updated < created:
            raise ValueError("job timestamps must be ordered and timezone-aware")
        active = self.state in {"queued", "running"}
        if active != (self.status is None):
            raise ValueError("only active jobs omit terminal status")
        if self.state == "queued" and self.process is not None:
            raise ValueError("queued job cannot claim a process")
        if self.state == "running" and self.process is None:
            raise ValueError("running job requires process identity")
        if self.state == "completed" and (self.status != 0 or self.binding is None):
            raise ValueError("completed jobs require success and a bound generation")
        if self.state == "completed" and (
            self.receipt is None or self.receipt.state != "completed"
        ):
            raise ValueError("completed jobs require completed capture evidence")
        if self.state == "partial" and self.status != errno.ENODATA:
            raise ValueError("partial jobs require ENODATA")
        if self.state == "cancelled" and self.status != errno.ECANCELED:
            raise ValueError("cancelled jobs require ECANCELED")
        if self.state == "failed" and self.status == 0:
            raise ValueError("failed jobs cannot have success status")
        if active and (self.receipt is not None or self.binding is not None):
            raise ValueError("active jobs cannot claim terminal evidence")
        if self.receipt is not None and (
            self.receipt.command != self.command or self.receipt.process != self.process
        ):
            raise ValueError("job receipt belongs to a different command/process")
        if self.binding is not None and (
            self.receipt is None
            or not self.receipt.report_validated
            or self.binding.process != self.process
            or self.binding.workspace_path != self.workspace_path
            or self.binding.report_sha256 != self.receipt.stdout_sha256
        ):
            raise ValueError("job binding disagrees with captured identity")
        return self


class CollectionJobLog(StrictModel):
    job_id: StrictStr = Field(pattern=r"^[a-f0-9]{32}$")
    stream: Literal["stderr"] = "stderr"
    text: StrictStr = Field(max_length=16_384)
    total_bytes: StrictInt = Field(ge=0)
    shown_bytes: StrictInt = Field(ge=0, le=16_384)
    omitted_bytes: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def reconcile(self) -> Self:
        if self.shown_bytes + self.omitted_bytes != self.total_bytes:
            raise ValueError("log byte counts do not reconcile")
        return self
