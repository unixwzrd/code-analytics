"""Independent association checks for candidate-source index receipts."""

import pytest
from pydantic import ValidationError
from test_indexed_candidate_static_inventory import (
    MINIMAL,
    _build_distinct_static_comparison_index,
)
from test_runtime_comparison_source_browsing import build_comparison_index

from code_analysis.browse_index_models import BrowseIndexManifest
from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.reports.json_report import load_json
from code_analysis.service import scan


def test_primary_only_receipt_rejects_false_candidate_association(ordinary_report, tmp_path):
    _, index, _ = build_comparison_index(ordinary_report, tmp_path)
    payload = index.manifest.model_dump()
    payload["runtime"]["comparison"]["candidate_overview"]["source_matches"] = False
    with pytest.raises(ValidationError, match="candidate/static binding"):
        BrowseIndexManifest.model_validate(payload)


def test_report_association_does_not_depend_on_nonempty_inventory(ordinary_report, tmp_path):
    minimal = load_json(MINIMAL.read_text(encoding="utf-8"))
    _, index, _, _, _, _ = _build_distinct_static_comparison_index(
        ordinary_report, minimal, tmp_path
    )
    payload = index.manifest.model_dump()
    # Row-count reconciliation is independently enforced against SQLite; a saved
    # report association itself does not depend on having any source records.
    payload["candidate_source_file_count"] = 0
    manifest = BrowseIndexManifest.model_validate(payload)
    assert manifest.runtime.comparison.candidate_overview.source_matches


def test_real_generations_preserve_ids_and_select_each_source_hash(tmp_path):
    project = tmp_path / "project"
    source = project / "alpha" / "a.py"
    source.parent.mkdir(parents=True)
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    baseline = scan(discover_context(project, AnalysisConfig()))
    source.write_text("def value():\n    return 2\n", encoding="utf-8")
    candidate = scan(discover_context(project, AnalysisConfig()))
    before, after = baseline.source_files[0], candidate.source_files[0]
    assert before.source_file_id == after.source_file_id
    assert before.logical_path == after.logical_path == "alpha/a.py"
    assert before.source_sha256 != after.source_sha256
    _, index, _, delta, _, _ = _build_distinct_static_comparison_index(
        baseline, candidate, tmp_path
    )
    for side, expected in (("baseline", before), ("candidate", after)):
        key = next(
            row.frame_identity
            for row in delta.records
            if getattr(row, f"{side}_total_seconds") is not None
        )
        preview = index.runtime_comparison_source(key, side)
        assert preview.source.logical_path == expected.logical_path
        assert preview.source.source_sha256 == expected.source_sha256
