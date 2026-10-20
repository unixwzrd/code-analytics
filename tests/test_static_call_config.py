from pathlib import Path

import pytest
from pydantic import ValidationError

from code_analysis.config import AnalysisConfig, load_config_file
from code_analysis.errors import ConfigurationError


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "10", 1_000_001])
def test_occurrence_limit_rejects_coercion_and_invalid_bounds(value: object) -> None:
    with pytest.raises(ValidationError):
        AnalysisConfig(max_static_call_occurrences=value)


def test_occurrence_limit_participates_in_analytical_identity() -> None:
    baseline = AnalysisConfig()
    changed = AnalysisConfig(max_static_call_occurrences=10)
    assert baseline.max_static_call_occurrences == 100_000
    assert changed.identity() != baseline.identity()
    assert changed.phase_two_identity() != baseline.phase_two_identity()
    assert changed.phase_three_identity() != baseline.phase_three_identity()
    assert changed.phase_four_identity() != baseline.phase_four_identity()
    assert (
        AnalysisConfig.with_overrides(base=changed, max_findings=3).max_static_call_occurrences
        == 10
    )


def test_occurrence_limit_from_project_configuration(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text("[tool.code-analysis]\nmax-static-call-occurrences = 17\n")
    assert load_config_file(project).max_static_call_occurrences == 17


def test_conflicting_occurrence_limit_keys_fail_closed(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        "[tool.code-analysis]\nmax-static-call-occurrences = 17\n"
        "max_static_call_occurrences = 18\n"
    )
    with pytest.raises(ConfigurationError, match="duplicate"):
        load_config_file(project)
