"""Owned atomic storage and reconciliation for local maintainer decisions."""

from __future__ import annotations

import errno
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from code_analysis.decision_models import (
    CandidateDecisionRecord,
    DecisionLedger,
    MaintainerDecision,
)
from code_analysis.report_models import (
    AbstractionCostEstimate,
    AnalysisReport,
    BehavioralInvariant,
    CandidateBehaviorDifference,
    OwnerAlternative,
    RefactorCandidate,
    RefactorCandidateObservation,
    RefactorProposal,
)
from code_analysis.reports.json_report import canonical_json
from code_analysis.workspace import WorkspacePaths, validate_workspace_ownership

LEDGER_NAME = "ledger.json"


@dataclass(frozen=True, slots=True)
class EffectiveDecision:
    record: CandidateDecisionRecord
    changed_since_decision: bool


@dataclass(frozen=True, slots=True)
class CandidateSignatureCatalog:
    """Order-preserving, report-bound candidate reconsideration signatures.

    Linked report collections are indexed once while retaining their original
    collection order in every resulting signature.  A report can therefore
    reconcile many ledger records without re-walking each collection for every
    candidate.
    """

    _candidates: Mapping[str, RefactorCandidate]
    _observations: Mapping[str, RefactorCandidateObservation]
    _proposals: Mapping[str, tuple[RefactorProposal, ...]]
    _costs: Mapping[str, tuple[AbstractionCostEstimate, ...]]
    _owners: Mapping[str, tuple[OwnerAlternative, ...]]
    _invariants: Mapping[str, tuple[BehavioralInvariant, ...]]
    _differences: Mapping[str, tuple[CandidateBehaviorDifference, ...]]

    @classmethod
    def from_report(cls, report: AnalysisReport) -> CandidateSignatureCatalog:
        """Build a shared catalog from already validated report evidence."""

        candidates = {item.candidate_id: item for item in report.refactor_candidates}
        observations = {item.candidate_id: item for item in report.candidate_observations}
        proposal_consumers: dict[str, list[str]] = {}
        cost_consumers: dict[str, list[str]] = {}
        owner_consumers: dict[str, list[str]] = {}
        invariant_consumers: dict[str, list[str]] = {}
        difference_consumers: dict[str, list[str]] = {}

        for candidate_id, observation in observations.items():
            for proposal_id in observation.proposal_ids:
                proposal_consumers.setdefault(proposal_id, []).append(candidate_id)
            if observation.cost_estimate_id is not None:
                cost_consumers.setdefault(observation.cost_estimate_id, []).append(candidate_id)
            for alternative_id in observation.owner_alternative_ids:
                owner_consumers.setdefault(alternative_id, []).append(candidate_id)
            for invariant_id in observation.behavioral_invariant_ids:
                invariant_consumers.setdefault(invariant_id, []).append(candidate_id)
            for difference_id in observation.behavioral_difference_ids:
                difference_consumers.setdefault(difference_id, []).append(candidate_id)

        proposals: dict[str, list[RefactorProposal]] = {}
        for item in report.refactor_proposals:
            for candidate_id in proposal_consumers.get(item.proposal_id, ()):
                proposals.setdefault(candidate_id, []).append(item)
        costs: dict[str, list[AbstractionCostEstimate]] = {}
        for cost_estimate in report.abstraction_cost_estimates:
            for candidate_id in cost_consumers.get(cost_estimate.cost_estimate_id, ()):
                costs.setdefault(candidate_id, []).append(cost_estimate)
        owners: dict[str, list[OwnerAlternative]] = {}
        for owner_alternative in report.owner_alternatives:
            for candidate_id in owner_consumers.get(owner_alternative.alternative_id, ()):
                owners.setdefault(candidate_id, []).append(owner_alternative)
        invariants: dict[str, list[BehavioralInvariant]] = {}
        for invariant in report.behavioral_invariants:
            for candidate_id in invariant_consumers.get(invariant.invariant_id, ()):
                invariants.setdefault(candidate_id, []).append(invariant)
        differences: dict[str, list[CandidateBehaviorDifference]] = {}
        for difference in report.candidate_behavior_differences:
            for candidate_id in difference_consumers.get(difference.difference_id, ()):
                differences.setdefault(candidate_id, []).append(difference)

        return cls(
            _candidates=MappingProxyType(candidates),
            _observations=MappingProxyType(observations),
            _proposals=MappingProxyType({key: tuple(value) for key, value in proposals.items()}),
            _costs=MappingProxyType({key: tuple(value) for key, value in costs.items()}),
            _owners=MappingProxyType({key: tuple(value) for key, value in owners.items()}),
            _invariants=MappingProxyType({key: tuple(value) for key, value in invariants.items()}),
            _differences=MappingProxyType(
                {key: tuple(value) for key, value in differences.items()}
            ),
        )

    def signature(self, candidate_id: str) -> str:
        candidate = self._candidates.get(candidate_id)
        observation = self._observations.get(candidate_id)
        if candidate is None or observation is None:
            raise OSError(errno.EBADMSG, "report does not contain the candidate observation")
        payload = {
            "candidate": candidate.model_dump(mode="json"),
            "observation": observation.model_dump(
                mode="json",
                exclude={"run_identity_sha256", "target_identity_sha256", "source_sha256s"},
            ),
            "proposals": [
                item.model_dump(mode="json") for item in self._proposals.get(candidate_id, ())
            ],
            "costs": [item.model_dump(mode="json") for item in self._costs.get(candidate_id, ())],
            "owners": [item.model_dump(mode="json") for item in self._owners.get(candidate_id, ())],
            "behavior_invariants": [
                item.model_dump(mode="json") for item in self._invariants.get(candidate_id, ())
            ],
            "behavior_differences": [
                item.model_dump(mode="json") for item in self._differences.get(candidate_id, ())
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _ledger_path(workspace: WorkspacePaths) -> Path:
    validate_workspace_ownership(workspace)
    if workspace.decisions.is_symlink() or workspace.decisions.parent != workspace.root:
        raise OSError(errno.EPERM, "decision directory is not an owned direct child")
    return workspace.decisions / LEDGER_NAME


def load_ledger(workspace: WorkspacePaths) -> DecisionLedger:
    marker = validate_workspace_ownership(workspace)
    path = _ledger_path(workspace)
    if not path.exists():
        return DecisionLedger(target_identity_sha256=marker.target_identity_sha256)
    if path.is_symlink() or path.parent != workspace.decisions:
        raise OSError(errno.ELOOP, "decision ledger must not be a symlink", str(path))
    try:
        ledger = DecisionLedger.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OSError(errno.EBADMSG, "decision ledger is malformed", str(path)) from exc
    if ledger.target_identity_sha256 != marker.target_identity_sha256:
        raise OSError(errno.EBADMSG, "decision ledger target identity mismatch", str(path))
    return ledger


def _write_ledger(workspace: WorkspacePaths, ledger: DecisionLedger) -> None:
    marker = validate_workspace_ownership(workspace)
    if ledger.target_identity_sha256 != marker.target_identity_sha256:
        raise OSError(errno.EBADMSG, "decision ledger target identity mismatch")
    path = _ledger_path(workspace)
    payload = json.dumps(ledger.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    temporary = path.parent / f".{LEDGER_NAME}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        DecisionLedger.model_validate_json(temporary.read_text(encoding="utf-8"))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ledger_sha256(ledger: DecisionLedger) -> str:
    payload = json.dumps(
        ledger.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def candidate_signature(report: AnalysisReport, candidate_id: str) -> str:
    return CandidateSignatureCatalog.from_report(report).signature(candidate_id)


def effective_decisions_from_signatures(
    ledger: DecisionLedger, signatures: Mapping[str, str]
) -> dict[str, EffectiveDecision]:
    """Reconcile a ledger against already-computed candidate signatures.

    A missing signature deliberately retains the historic fail-closed behavior:
    the decision is treated as changed rather than as an unchanged rejection.
    """

    superseded = {
        item.superseded_decision_id
        for item in ledger.records
        if item.superseded_decision_id is not None
    }
    result: dict[str, EffectiveDecision] = {}
    for record in ledger.records:
        if record.decision_id in superseded:
            continue
        signature = signatures.get(record.candidate_id)
        changed = signature is None or signature != record.reconsideration_signature_sha256
        result[record.candidate_id] = EffectiveDecision(record, changed)
    return result


def effective_decisions(
    ledger: DecisionLedger, report: AnalysisReport | None = None
) -> dict[str, EffectiveDecision]:
    superseded = {
        item.superseded_decision_id
        for item in ledger.records
        if item.superseded_decision_id is not None
    }
    if report is None:
        return effective_decisions_from_signatures(
            ledger,
            {
                record.candidate_id: record.reconsideration_signature_sha256
                for record in ledger.records
                if record.decision_id not in superseded
            },
        )
    catalog = CandidateSignatureCatalog.from_report(report)
    signatures: dict[str, str] = {}
    for record in ledger.records:
        if record.decision_id in superseded or record.candidate_id in signatures:
            continue
        try:
            signatures[record.candidate_id] = catalog.signature(record.candidate_id)
        except OSError:
            continue
    return effective_decisions_from_signatures(ledger, signatures)


def set_decision(
    workspace: WorkspacePaths,
    report: AnalysisReport,
    *,
    candidate_id: str,
    proposal_id: str | None,
    decision: MaintainerDecision,
    reason: str,
    recorded_at: datetime | None = None,
) -> CandidateDecisionRecord:
    if not reason.strip() or reason != reason.strip():
        raise OSError(errno.EINVAL, "decision reason must be nonempty and trimmed")
    marker = validate_workspace_ownership(workspace)
    if (
        report.target.logical_root != marker.logical_root
        or report.target.source_roots != marker.source_roots
    ):
        raise OSError(errno.EBADMSG, "report target does not match the decision workspace")
    candidate = next(
        (item for item in report.refactor_candidates if item.candidate_id == candidate_id), None
    )
    observation = next(
        (item for item in report.candidate_observations if item.candidate_id == candidate_id), None
    )
    if candidate is None or observation is None:
        raise OSError(errno.EBADMSG, "candidate is absent from the selected report")
    if proposal_id is not None and proposal_id not in observation.proposal_ids:
        raise OSError(errno.EBADMSG, "proposal does not belong to the selected candidate")
    ledger = load_ledger(workspace)
    previous = effective_decisions(ledger).get(candidate_id)
    timestamp = (recorded_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    signature = candidate_signature(report, candidate_id)
    report_sha256 = hashlib.sha256(canonical_json(report).encode("utf-8")).hexdigest()
    identity_payload = (
        candidate_id,
        proposal_id or "",
        decision.value,
        reason,
        report_sha256,
        observation.observation_id,
        signature,
        timestamp,
        previous.record.decision_id if previous is not None else "",
    )
    record = CandidateDecisionRecord(
        decision_id="maintainer-decision-"
        + hashlib.sha256("\0".join(identity_payload).encode()).hexdigest()[:20],
        candidate_id=candidate_id,
        proposal_id=proposal_id,
        decision=decision,
        reason=reason,
        report_sha256=report_sha256,
        observation_id=observation.observation_id,
        reconsideration_signature_sha256=signature,
        recorded_at=timestamp,
        superseded_decision_id=(previous.record.decision_id if previous is not None else None),
    )
    updated = DecisionLedger(
        target_identity_sha256=ledger.target_identity_sha256,
        records=(*ledger.records, record),
    )
    _write_ledger(workspace, updated)
    return record


def export_ledger(workspace: WorkspacePaths, output: Path) -> None:
    ledger = load_ledger(workspace)
    if output.exists() and output.is_symlink():
        raise OSError(errno.ELOOP, "decision export target must not be a symlink", str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(ledger.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    temporary = output.parent / f".{output.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def import_ledger(
    workspace: WorkspacePaths,
    source: Path,
    *,
    conflict: str = "error",
    dry_run: bool = False,
) -> DecisionLedger:
    try:
        incoming = DecisionLedger.model_validate_json(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OSError(errno.EBADMSG, "imported decision ledger is malformed", str(source)) from exc
    local = load_ledger(workspace)
    if incoming.target_identity_sha256 != local.target_identity_sha256:
        raise OSError(errno.EBADMSG, "imported decision ledger target identity mismatch")
    if conflict not in {"error", "keep-local", "use-incoming"}:
        raise OSError(errno.EINVAL, "unknown decision import conflict policy")
    records = list(local.records)
    known = {item.decision_id for item in records}
    local_effective = effective_decisions(local)
    remapped_ids: dict[str, str] = {}
    for record in incoming.records:
        if record.decision_id in known:
            continue
        original_id = record.decision_id
        if record.superseded_decision_id in remapped_ids:
            record = record.model_copy(
                update={"superseded_decision_id": remapped_ids[record.superseded_decision_id]}
            )
        existing = local_effective.get(record.candidate_id)
        if existing is not None:
            if conflict == "error":
                raise OSError(errno.EEXIST, "decision import conflicts with local decision")
            if conflict == "keep-local":
                continue
            record = record.model_copy(
                update={"superseded_decision_id": existing.record.decision_id}
            )
            identity = hashlib.sha256(
                json.dumps(
                    record.model_dump(mode="json", exclude={"decision_id"}), sort_keys=True
                ).encode()
            ).hexdigest()[:20]
            record = record.model_copy(update={"decision_id": f"maintainer-decision-{identity}"})
            remapped_ids[original_id] = record.decision_id
        records.append(record)
        known.add(record.decision_id)
        local_effective = effective_decisions(
            DecisionLedger(
                target_identity_sha256=local.target_identity_sha256,
                records=tuple(records),
            )
        )
    merged = DecisionLedger(
        target_identity_sha256=local.target_identity_sha256,
        records=tuple(records),
    )
    if not dry_run:
        _write_ledger(workspace, merged)
    return merged
