"""Independent malformed-side and duplicate-option boundary checks."""

import pytest
from pydantic import ValidationError

from code_analysis.models import SourceLanguage
from code_analysis.projection_models import ComparisonChange
from code_analysis.static_comparison_source import (
    ComparisonSourceOption,
    ComparisonSourcePreviewKind,
    ComparisonSourceSideState,
    StaticComparisonSourceBinding,
    StaticComparisonSourceSide,
    _option_id,
)


@pytest.mark.parametrize("change", tuple(ComparisonChange))
def test_change_kind_reconciles_missing_sides(change):
    state = (
        ComparisonSourceSideState.NO_EVIDENCE
        if change in (ComparisonChange.ADDED, ComparisonChange.REMOVED)
        else ComparisonSourceSideState.MISSING_RECORD
    )
    with pytest.raises(ValidationError):
        StaticComparisonSourceBinding(
            position=0,
            subject_id="module:alpha",
            record_kind="module",
            change=change,
            baseline=StaticComparisonSourceSide(
                report_sha256="a" * 64, state=state, message="test"
            ),
            candidate=StaticComparisonSourceSide(
                report_sha256="b" * 64, state=state, message="test"
            ),
        )


def test_duplicate_source_options_are_rejected():
    fields = {
        "logical_path": "alpha.py",
        "source_sha256": "a" * 64,
        "language": SourceLanguage.PYTHON,
        "start_line": 1,
        "end_line": 2,
        "preview_kind": ComparisonSourcePreviewKind.EVIDENCE_SPAN,
    }
    option = ComparisonSourceOption(option_id=_option_id(**fields), label="alpha.py:1-2", **fields)
    with pytest.raises(ValidationError):
        StaticComparisonSourceSide(
            report_sha256="b" * 64,
            state=ComparisonSourceSideState.AVAILABLE,
            options=(option, option),
            message="test",
        )
