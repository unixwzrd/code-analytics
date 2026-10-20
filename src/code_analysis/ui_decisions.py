"""Explicit, read-only decision snapshots for a selected owned workspace."""

from __future__ import annotations

import errno
import hashlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.decision_models import DecisionLedger
from code_analysis.decisions import LEDGER_NAME, ledger_sha256
from code_analysis.models import StrictModel, TargetIdentity
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json
from code_analysis.workspace import (
    WorkspacePaths,
    _read_owned_metadata,
    validate_workspace_ownership,
)

if TYPE_CHECKING:
    from code_analysis.browse_index_models import BrowseIndexManifest


class UIDecisionLedger(StrictModel):
    available: StrictBool
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    ledger_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    record_count: StrictInt = Field(default=0, ge=0)
    unavailable_reason: StrictStr | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def reconcile(self) -> Self:
        if self.available != (self.ledger_sha256 is not None):
            raise ValueError("ledger availability requires its canonical snapshot identity")
        if self.available == (self.unavailable_reason is not None):
            raise ValueError("unavailable ledger requires an explicit reason")
        if not self.available and self.record_count:
            raise ValueError("unavailable ledger cannot claim records")
        return self


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    ledger: DecisionLedger
    report_sha256: str
    ledger_sha256: str


def read_decision_snapshot(
    report: AnalysisReport, workspace: WorkspacePaths
) -> DecisionSnapshot | None:
    """No ledger creation, source reads, decision mutation, or implicit attachment."""
    return _read_target_snapshot(
        report.target, hashlib.sha256(canonical_json(report).encode()).hexdigest(), workspace
    )


def read_index_decision_snapshot(
    manifest: BrowseIndexManifest, workspace: WorkspacePaths
) -> DecisionSnapshot | None:
    """Use the validated index's retained target, never reopen the source report."""
    return _read_target_snapshot(manifest.target, manifest.report_sha256, workspace)


def _read_target_snapshot(
    target: TargetIdentity, report_sha256: str, workspace: WorkspacePaths
) -> DecisionSnapshot | None:
    marker = validate_workspace_ownership(workspace)
    if target.logical_root != marker.logical_root or target.source_roots != marker.source_roots:
        raise OSError(errno.EBADMSG, "report logical target does not match decision workspace")
    try:
        directory = os.open(workspace.decisions, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        try:
            payload = _read_owned_metadata(LEDGER_NAME, directory_fd=directory)
        except FileNotFoundError:
            return None
    finally:
        os.close(directory)
    try:
        ledger = DecisionLedger.model_validate_json(payload)
    except ValueError as exc:
        # Do not send private decision reasons or malformed record values through HTTP errors.
        raise OSError(errno.EBADMSG, "decision ledger is malformed") from exc
    if ledger.target_identity_sha256 != marker.target_identity_sha256:
        raise OSError(errno.EBADMSG, "decision ledger workspace identity mismatch")
    if validate_workspace_ownership(workspace) != marker:
        raise OSError(errno.EBADMSG, "decision workspace changed during snapshot read")
    return DecisionSnapshot(
        ledger=ledger,
        report_sha256=report_sha256,
        ledger_sha256=ledger_sha256(ledger),
    )


def decision_ledger_status(
    report: AnalysisReport, workspace: WorkspacePaths | None
) -> UIDecisionLedger:
    snapshot = read_decision_snapshot(report, workspace) if workspace is not None else None
    digest = (
        snapshot.report_sha256
        if snapshot is not None
        else hashlib.sha256(canonical_json(report).encode()).hexdigest()
    )
    return _ledger_status(snapshot, digest, workspace)


def index_decision_ledger_status(
    manifest: BrowseIndexManifest, workspace: WorkspacePaths | None
) -> UIDecisionLedger:
    snapshot = read_index_decision_snapshot(manifest, workspace) if workspace is not None else None
    return _ledger_status(snapshot, manifest.report_sha256, workspace)


def _ledger_status(
    snapshot: DecisionSnapshot | None, report_sha256: str, workspace: WorkspacePaths | None
) -> UIDecisionLedger:
    return UIDecisionLedger(
        available=snapshot is not None,
        report_sha256=report_sha256,
        ledger_sha256=snapshot.ledger_sha256 if snapshot is not None else None,
        record_count=len(snapshot.ledger.records) if snapshot is not None else 0,
        unavailable_reason=(
            None
            if snapshot is not None
            else (
                "No owned workspace is selected."
                if workspace is None
                else "The selected workspace has no saved decision ledger."
            )
        ),
    )
