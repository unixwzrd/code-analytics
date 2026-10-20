from __future__ import annotations

import pytest

from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.service import scan
from code_analysis.ui_navigation import NavigationPage, navigation_page


def test_navigation_preserves_saved_spans_and_selection(ordinary_report) -> None:
    module = next(item for item in ordinary_report.modules if item.symbols)
    page = navigation_page(ordinary_report, module.name)
    assert page.completeness == ordinary_report.completeness
    assert {item.span for item in page.records} == {item.span for item in module.symbols}
    for item in page.records:
        assert navigation_page(ordinary_report, item.identity).records == (item,)
    subject = ordinary_report.subjects[0]
    selected = navigation_page(ordinary_report, subject.subject_id)
    assert {item.span for item in selected.records} == {item.span for item in subject.evidence}
    with pytest.raises(ValueError):
        navigation_page(ordinary_report, "not-a-report-identity")
    with pytest.raises(ValueError):
        navigation_page(ordinary_report, module.name, True)


def test_navigation_paging_and_empty_module_source(tmp_path) -> None:
    source = tmp_path / "sample.py"
    source.write_text("\n".join(f"def function_{i}():\n    return {i}\n" for i in range(31)))
    report = scan(discover_context(tmp_path, AnalysisConfig(test_roots=())))
    source.unlink()  # Navigation reads saved spans only.
    first = navigation_page(report, "sample")
    assert first.eligible == 31 and len(first.records) == 25 and first.next_offset == 25
    last = navigation_page(report, "sample", first.next_offset)
    assert len(last.records) == 6 and last.next_offset is None
    assert not navigation_page(report, "sample", 100).records
    with pytest.raises(ValueError):
        NavigationPage.model_validate(first.model_dump() | {"next_offset": 24})
    source.write_text("value = 1\n")
    empty = scan(discover_context(tmp_path, AnalysisConfig(test_roots=())))
    assert navigation_page(empty, "sample").records[0].kind == "module"


def test_state_navigation_uses_recorded_owner_and_effect_spans(tmp_path) -> None:
    (tmp_path / "sample.py").write_text(
        "import os\nregistry = []\ndef cleanup():\n"
        "    registry.append(1)\n    os.remove('not-executed')\n"
    )
    report = scan(discover_context(tmp_path, AnalysisConfig(test_roots=())))
    assert report.state_resources and report.side_effects
    for resource in report.state_resources:
        page = navigation_page(report, resource.state_resource_id)
        assert {item.span for item in page.records} == {
            item.span for item in resource.declaration_evidence
        }
    for effect in report.side_effects:
        page = navigation_page(report, effect.side_effect_id)
        assert {item.span for item in page.records} == {item.span for item in effect.evidence}
