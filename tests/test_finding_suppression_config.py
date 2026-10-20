"""Configuration and model contracts for accepted finding suppressions."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.config import AnalysisConfig, load_config_file
from code_analysis.errors import ConfigurationError
from code_analysis.finding_suppression_models import (
    FINDING_ID_MAX_LENGTH,
    RATIONALE_MAX_LENGTH,
    AcceptedFindingSuppression,
    FindingSuppression,
    FindingSuppressionAnnotation,
    FindingSuppressionStatus,
    suppression_status_for_dates,
    validate_canonical_date_string,
)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "not-suppressed"},
        {"status": "expired"},
        {"evaluated_on": "2030-99-01"},
        {"expires_on": True},
        {"rationale": "   "},
        {"extra": "forbidden"},
    ],
)
def test_shared_annotation_rejects_malformed_saved_state(changes) -> None:
    payload = {
        "status": "accepted",
        "rationale": "Reviewed",
        "evaluated_on": "2026-09-22",
        "expires_on": "2030-01-01",
        **changes,
    }
    with pytest.raises(ValidationError):
        FindingSuppressionAnnotation.model_validate(payload)


def _accepted(
    finding_id: str = "finding-abc123",
    *,
    rationale: str = "Known false positive in fixture",
    expires_on: str = "2030-01-01",
) -> AcceptedFindingSuppression:
    return AcceptedFindingSuppression(
        finding_id=finding_id,
        rationale=rationale,
        expires_on=expires_on,
    )


def test_accepted_suppression_rejects_extras_and_coercion() -> None:
    with pytest.raises(ValidationError):
        AcceptedFindingSuppression.model_validate(
            {
                "finding_id": "finding-x",
                "rationale": "ok",
                "expires_on": "2030-01-01",
                "rule_id": "complexity",
            }
        )
    with pytest.raises(ValidationError):
        AcceptedFindingSuppression(
            finding_id=101,
            rationale="ok",
            expires_on="2030-01-01",
        )


def test_accepted_suppression_requires_trimmed_nonempty_fields() -> None:
    with pytest.raises(ValidationError, match="trimmed"):
        AcceptedFindingSuppression(
            finding_id=" finding-x",
            rationale="ok",
            expires_on="2030-01-01",
        )
    with pytest.raises(ValidationError, match="trimmed"):
        AcceptedFindingSuppression(
            finding_id="finding-x",
            rationale=" trailing ",
            expires_on="2030-01-01",
        )
    with pytest.raises(ValidationError, match="canonical"):
        AcceptedFindingSuppression(
            finding_id="finding-x",
            rationale="ok",
            expires_on="2030-1-01",
        )
    with pytest.raises(ValidationError):
        AcceptedFindingSuppression(
            finding_id="finding-x",
            rationale="ok",
            expires_on=date(2030, 1, 1),
        )


def test_canonical_date_validation_and_leap_day() -> None:
    assert validate_canonical_date_string("2024-02-29", label="expires_on") == "2024-02-29"
    with pytest.raises(ValueError, match="valid calendar date"):
        validate_canonical_date_string("2023-02-29", label="expires_on")


def test_suppression_status_expiry_boundaries() -> None:
    assert suppression_status_for_dates("2029-12-31", "2030-01-01") is (
        FindingSuppressionStatus.ACCEPTED
    )
    assert suppression_status_for_dates("2030-01-01", "2030-01-01") is (
        FindingSuppressionStatus.EXPIRED
    )
    assert suppression_status_for_dates("2030-06-01", "2030-01-01") is (
        FindingSuppressionStatus.EXPIRED
    )


def test_finding_suppression_status_consistency() -> None:
    accepted = _accepted().evaluated("2029-06-01")
    assert accepted.status is FindingSuppressionStatus.ACCEPTED
    expired = _accepted().evaluated("2030-01-01")
    assert expired.status is FindingSuppressionStatus.EXPIRED
    with pytest.raises(ValidationError, match="expired iff"):
        FindingSuppression(
            finding_id="finding-x",
            rationale="ok",
            expires_on="2030-01-01",
            evaluated_on="2029-01-01",
            status=FindingSuppressionStatus.EXPIRED,
        )


def test_analysis_config_suppression_pairing_and_sorting() -> None:
    with pytest.raises(ValidationError, match="suppression-evaluation-date is required"):
        AnalysisConfig(accepted_suppressions=(_accepted(),))
    with pytest.raises(ValidationError, match="without accepted suppressions"):
        AnalysisConfig(suppression_evaluation_date="2030-01-01")
    with pytest.raises(ValidationError, match="unique and sorted"):
        AnalysisConfig(
            accepted_suppressions=(
                _accepted("finding-b"),
                _accepted("finding-a"),
            ),
            suppression_evaluation_date="2029-01-01",
        )
    with pytest.raises(ValidationError, match="unique and sorted"):
        AnalysisConfig(
            accepted_suppressions=(
                _accepted("finding-a"),
                _accepted("finding-a"),
            ),
            suppression_evaluation_date="2029-01-01",
        )
    config = AnalysisConfig(
        accepted_suppressions=(
            _accepted("finding-a"),
            _accepted("finding-b"),
        ),
        suppression_evaluation_date="2029-01-01",
    )
    assert [item.finding_id for item in config.accepted_suppressions] == [
        "finding-a",
        "finding-b",
    ]


def test_analysis_config_bounds() -> None:
    with pytest.raises(ValidationError):
        AcceptedFindingSuppression(
            finding_id="x" * (FINDING_ID_MAX_LENGTH + 1),
            rationale="ok",
            expires_on="2030-01-01",
        )
    with pytest.raises(ValidationError):
        AcceptedFindingSuppression(
            finding_id="finding-x",
            rationale="x" * (RATIONALE_MAX_LENGTH + 1),
            expires_on="2030-01-01",
        )
    too_many = tuple(_accepted(f"finding-{index:05d}") for index in range(10_001))
    with pytest.raises(ValidationError):
        AnalysisConfig(
            accepted_suppressions=too_many,
            suppression_evaluation_date="2029-01-01",
        )


def test_toml_loads_quoted_suppressions(tmp_path: Path) -> None:
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text(
        """
[tool.code-analysis]
suppression-evaluation-date = "2029-06-15"

[[tool.code-analysis.accepted-suppressions]]
finding-id = "finding-older"
rationale = "Documented waiver"
expires-on = "2030-12-31"

[[tool.code-analysis.accepted-suppressions]]
finding-id = "finding-younger"
rationale = "Temporary noise"
expires-on = "2029-12-01"
""".strip() + "\n",
        encoding="utf-8",
    )
    config = load_config_file(config_path)
    assert config.suppression_evaluation_date == "2029-06-15"
    assert len(config.accepted_suppressions) == 2
    assert config.accepted_suppressions[0].finding_id == "finding-older"
    assert config.accepted_suppressions[1].expires_on == "2029-12-01"


def test_toml_rejects_unquoted_dates_and_conflicts(tmp_path: Path) -> None:
    unquoted = tmp_path / "unquoted.toml"
    unquoted.write_text(
        """
[tool.code-analysis]
suppression-evaluation-date = 2029-06-15

[[tool.code-analysis.accepted-suppressions]]
finding-id = "finding-x"
rationale = "ok"
expires-on = "2030-01-01"
""".strip() + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="quoted"):
        load_config_file(unquoted)

    conflict_date = tmp_path / "conflict-date.toml"
    conflict_date.write_text(
        """
[tool.code-analysis]
suppression-evaluation-date = "2029-01-01"
suppression_evaluation_date = "2029-01-02"
""".strip() + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="cannot be combined"):
        load_config_file(conflict_date)

    conflict_rules = tmp_path / "conflict-rules.toml"
    conflict_rules.write_text(
        """
[tool.code-analysis]
suppression-evaluation-date = "2029-01-01"
accepted_suppressions = []
accepted-suppressions = []
""".strip() + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="cannot be combined"):
        load_config_file(conflict_rules)

    bad_table = tmp_path / "bad-table.toml"
    bad_table.write_text(
        """
[tool.code-analysis]
suppression-evaluation-date = "2029-01-01"
accepted-suppressions = "not-a-list"
""".strip() + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="array of tables"):
        load_config_file(bad_table)


def test_configuration_identity_reflects_suppression_inputs() -> None:
    base = AnalysisConfig()
    assert base.identity() == AnalysisConfig().identity()

    with_rules = AnalysisConfig(
        accepted_suppressions=(_accepted("finding-a"),),
        suppression_evaluation_date="2029-01-01",
    )
    assert base.identity() != with_rules.identity()

    rationale_change = AnalysisConfig(
        accepted_suppressions=(
            AcceptedFindingSuppression(
                finding_id="finding-a",
                rationale="Different rationale",
                expires_on="2030-01-01",
            ),
        ),
        suppression_evaluation_date="2029-01-01",
    )
    assert with_rules.identity() != rationale_change.identity()

    expiry_change = AnalysisConfig(
        accepted_suppressions=(
            AcceptedFindingSuppression(
                finding_id="finding-a",
                rationale="Known false positive in fixture",
                expires_on="2031-01-01",
            ),
        ),
        suppression_evaluation_date="2029-01-01",
    )
    assert with_rules.identity() != expiry_change.identity()

    date_change = AnalysisConfig(
        accepted_suppressions=(_accepted("finding-a"),),
        suppression_evaluation_date="2029-06-01",
    )
    assert with_rules.identity() != date_change.identity()

    assert with_rules.phase_two_identity() != date_change.phase_two_identity()
    assert with_rules.phase_three_identity() != date_change.phase_three_identity()
    assert with_rules.phase_four_identity() != date_change.phase_four_identity()
