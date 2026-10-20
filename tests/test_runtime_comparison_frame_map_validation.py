"""Focused checks for comparison frame-map receipt validation."""

from __future__ import annotations

import pytest

from code_analysis.runtime_source_browse import require_comparison_source_side


def test_require_comparison_source_side_rejects_unknown_name():
    assert require_comparison_source_side("baseline") == "baseline"
    assert require_comparison_source_side("candidate") == "candidate"
    with pytest.raises(ValueError, match="side"):
        require_comparison_source_side("typo")
