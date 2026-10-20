from __future__ import annotations

import errno
import hashlib
import json
from datetime import UTC, datetime

import pytest
from conftest import FIXTURES

from code_analysis.config import load_config_file
from code_analysis.decision_models import DecisionLedger, MaintainerDecision
from code_analysis.decisions import (
    CandidateSignatureCatalog,
    candidate_signature,
    effective_decisions,
    effective_decisions_from_signatures,
    load_ledger,
    set_decision,
)
from code_analysis.project import discover_context
from code_analysis.service import scan_refactor
from code_analysis.workspace import prepare_workspace


def _report():
    context = discover_context(
        FIXTURES / "proposal_project",
        load_config_file(FIXTURES / "proposal-config.toml"),
    )
    return scan_refactor(context), context


def _prior_signature(report, candidate_id: str) -> str:
    """The pre-catalog algorithm, retained only to prove byte-for-byte parity."""

    candidate = next(
        (item for item in report.refactor_candidates if item.candidate_id == candidate_id), None
    )
    observation = next(
        (item for item in report.candidate_observations if item.candidate_id == candidate_id), None
    )
    if candidate is None or observation is None:
        raise OSError(errno.EBADMSG, "report does not contain the candidate observation")
    proposal_ids = set(observation.proposal_ids)
    cost_ids = {observation.cost_estimate_id} if observation.cost_estimate_id else set()
    owner_ids = set(observation.owner_alternative_ids)
    invariant_ids = set(observation.behavioral_invariant_ids)
    difference_ids = set(observation.behavioral_difference_ids)
    payload = {
        "candidate": candidate.model_dump(mode="json"),
        "observation": observation.model_dump(
            mode="json",
            exclude={"run_identity_sha256", "target_identity_sha256", "source_sha256s"},
        ),
        "proposals": [
            item.model_dump(mode="json")
            for item in report.refactor_proposals
            if item.proposal_id in proposal_ids
        ],
        "costs": [
            item.model_dump(mode="json")
            for item in report.abstraction_cost_estimates
            if item.cost_estimate_id in cost_ids
        ],
        "owners": [
            item.model_dump(mode="json")
            for item in report.owner_alternatives
            if item.alternative_id in owner_ids
        ],
        "behavior_invariants": [
            item.model_dump(mode="json")
            for item in report.behavioral_invariants
            if item.invariant_id in invariant_ids
        ],
        "behavior_differences": [
            item.model_dump(mode="json")
            for item in report.candidate_behavior_differences
            if item.difference_id in difference_ids
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_catalog_preserves_prior_signatures_and_report_collection_order() -> None:
    report, _context = _report()
    first_observation, second_observation = report.candidate_observations
    shared_references = report.model_copy(
        update={
            "candidate_observations": (
                first_observation,
                second_observation.model_copy(
                    update={
                        "proposal_ids": first_observation.proposal_ids,
                        "cost_estimate_id": first_observation.cost_estimate_id,
                        "owner_alternative_ids": first_observation.owner_alternative_ids,
                        "behavioral_invariant_ids": first_observation.behavioral_invariant_ids,
                        "behavioral_difference_ids": first_observation.behavioral_difference_ids,
                    }
                ),
            )
        }
    )
    ordered = report.model_copy(
        update={
            "refactor_proposals": tuple(reversed(report.refactor_proposals)),
            "abstraction_cost_estimates": tuple(reversed(report.abstraction_cost_estimates)),
            "owner_alternatives": tuple(reversed(report.owner_alternatives)),
            "behavioral_invariants": tuple(reversed(report.behavioral_invariants)),
        }
    )
    for current in (report, ordered, shared_references):
        catalog = CandidateSignatureCatalog.from_report(current)
        for candidate in current.refactor_candidates:
            expected = _prior_signature(current, candidate.candidate_id)
            assert catalog.signature(candidate.candidate_id) == expected
            assert candidate_signature(current, candidate.candidate_id) == expected


def test_catalog_missing_identity_and_signature_mapping_fail_closed(tmp_path) -> None:
    report, context = _report()
    candidate = report.refactor_candidates[0]
    observation = next(
        item
        for item in report.candidate_observations
        if item.candidate_id == candidate.candidate_id
    )
    workspace = prepare_workspace(context, explicit_path=tmp_path / "workspace")
    record = set_decision(
        workspace,
        report,
        candidate_id=candidate.candidate_id,
        proposal_id=observation.proposal_ids[0],
        decision=MaintainerDecision.REJECTED_ABSTRACTION_COST,
        reason="The retained estimate is unfavorable.",
        recorded_at=datetime(2026, 9, 19, tzinfo=UTC),
    )
    with pytest.raises(OSError) as error:
        CandidateSignatureCatalog.from_report(report).signature("refactor-candidate-" + "0" * 20)
    assert error.value.errno == errno.EBADMSG

    ledger = load_ledger(workspace)
    missing = effective_decisions_from_signatures(ledger, {})
    assert missing[candidate.candidate_id].changed_since_decision
    matching = effective_decisions_from_signatures(
        ledger, {candidate.candidate_id: record.reconsideration_signature_sha256}
    )
    assert not matching[candidate.candidate_id].changed_since_decision
    changed = effective_decisions_from_signatures(ledger, {candidate.candidate_id: "0" * 64})
    assert changed[candidate.candidate_id].changed_since_decision


def test_effective_decisions_builds_one_catalog_for_active_ledger_candidates(
    tmp_path, monkeypatch
) -> None:
    report, context = _report()
    workspace = prepare_workspace(context, explicit_path=tmp_path / "workspace")
    candidate = report.refactor_candidates[0]
    observation = next(
        item
        for item in report.candidate_observations
        if item.candidate_id == candidate.candidate_id
    )
    set_decision(
        workspace,
        report,
        candidate_id=candidate.candidate_id,
        proposal_id=observation.proposal_ids[0],
        decision=MaintainerDecision.ACCEPTED,
        reason="The candidate is ready for a maintainer decision.",
        recorded_at=datetime(2026, 9, 19, tzinfo=UTC),
    )
    calls = 0
    original = CandidateSignatureCatalog.from_report.__func__

    def counted(cls, current):
        nonlocal calls
        calls += 1
        return original(cls, current)

    monkeypatch.setattr(CandidateSignatureCatalog, "from_report", classmethod(counted))
    decisions = effective_decisions(load_ledger(workspace), report)
    assert calls == 1
    assert not decisions[candidate.candidate_id].changed_since_decision


def test_catalog_does_not_rewalk_linked_collections_for_repeated_signatures() -> None:
    report, _context = _report()

    class CountedTuple(tuple):
        iterations = 0

        def __iter__(self):
            type(self).iterations += 1
            return super().__iter__()

    counted = report.model_copy()
    for field_name in (
        "refactor_candidates",
        "candidate_observations",
        "refactor_proposals",
        "abstraction_cost_estimates",
        "owner_alternatives",
        "behavioral_invariants",
        "candidate_behavior_differences",
    ):
        object.__setattr__(counted, field_name, CountedTuple(getattr(report, field_name)))
    catalog = CandidateSignatureCatalog.from_report(counted)
    iterations_after_catalog = CountedTuple.iterations
    for candidate in counted.refactor_candidates:
        catalog.signature(candidate.candidate_id)
        catalog.signature(candidate.candidate_id)
    assert CountedTuple.iterations == iterations_after_catalog + 1


def test_no_report_reconciliation_uses_only_the_effective_record(tmp_path) -> None:
    report, context = _report()
    workspace = prepare_workspace(context, explicit_path=tmp_path / "workspace")
    candidate = report.refactor_candidates[0]
    observation = next(
        item
        for item in report.candidate_observations
        if item.candidate_id == candidate.candidate_id
    )
    first = set_decision(
        workspace,
        report,
        candidate_id=candidate.candidate_id,
        proposal_id=observation.proposal_ids[0],
        decision=MaintainerDecision.ACCEPTED,
        reason="Initial maintainer review.",
        recorded_at=datetime(2026, 9, 18, tzinfo=UTC),
    )
    second = set_decision(
        workspace,
        report,
        candidate_id=candidate.candidate_id,
        proposal_id=observation.proposal_ids[0],
        decision=MaintainerDecision.DEFERRED_PREREQUISITE,
        reason="A prerequisite remains unresolved.",
        recorded_at=datetime(2026, 9, 19, tzinfo=UTC),
    )
    stored = load_ledger(workspace)
    assert stored.records == (first, second)
    # Deliberately bypass strict load-time ordering to model a foreign in-memory
    # ledger. Reconciliation must still not use a superseded record's signature.
    reversed_records = DecisionLedger.model_construct(
        target_identity_sha256=stored.target_identity_sha256,
        records=(second, first),
    )
    effective = effective_decisions(reversed_records)
    assert effective[candidate.candidate_id].record == second
    assert not effective[candidate.candidate_id].changed_since_decision
