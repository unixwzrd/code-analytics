"""Independently exercise completeness precedence at the export boundary."""

from pathlib import Path

import pytest

from code_analysis.models import Completeness
from code_analysis.profile_models import ProfileDiffRecord, ProfileDiffReport
from code_analysis.runtime_models import CompatibilityDecision
from code_analysis.ui_runtime import (
    RuntimeComparison,
    RuntimeViewService,
    runtime_comparison_download_headers,
)


@pytest.mark.parametrize("baseline_state", tuple(Completeness))
@pytest.mark.parametrize("candidate_state", tuple(Completeness))
@pytest.mark.parametrize("omitted", (0, 1))
def test_export_completeness_preserves_failed_and_truncated_inputs(
    baseline_state, candidate_state, omitted
):
    profile = RuntimeViewService.load(
        Path(__file__).parent / "golden" / "phase4-profile-report.json"
    ).profile
    # These synthetic copies isolate state combination, not workload qualification.
    runtime = RuntimeViewService(
        profile.model_copy(update={"completeness": baseline_state}),
        profile.model_copy(update={"completeness": candidate_state}),
    )
    diff = ProfileDiffReport(
        baseline_profile_identity_sha256=profile.profile_identity_sha256,
        candidate_profile_identity_sha256=profile.profile_identity_sha256,
        source_report_sha256=None,
        workload_identity_sha256=None,
        diagnostic_only=True,
        claim_eligible=False,
        decisions=(
            CompatibilityDecision(field="synthetic", outcome="matched", reason="Test metadata"),
        ),
        records=tuple(
            ProfileDiffRecord(
                frame_identity=f"synthetic-frame-{number:03d}",
                baseline_total_seconds=None,
                candidate_total_seconds=None,
                absolute_total_seconds_delta=None,
                baseline_cumulative_seconds=None,
                candidate_cumulative_seconds=None,
                absolute_cumulative_seconds_delta=None,
            )
            for number in range(100)
        ),
    )
    comparison = RuntimeComparison(report=diff, eligible=100 + omitted, omitted=omitted)
    expected = (
        Completeness.FAILED
        if Completeness.FAILED in (baseline_state, candidate_state)
        else (
            Completeness.PARTIAL
            if omitted or Completeness.PARTIAL in (baseline_state, candidate_state)
            else Completeness.COMPLETE
        )
    )
    headers = runtime_comparison_download_headers(comparison, runtime)
    assert headers["X-Analysis-Completeness"] == expected.value
    assert headers["X-Projection-Truncated"] == str(bool(omitted)).lower()
