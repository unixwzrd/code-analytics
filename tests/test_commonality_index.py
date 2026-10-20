from __future__ import annotations

import math
import random
from itertools import combinations
from pathlib import Path

import pytest

from code_analysis.analysis import commonality_index
from code_analysis.analysis.commonality import generate_commonality_candidates
from code_analysis.analysis.function_features import FunctionFeatures
from code_analysis.config import AnalysisConfig
from code_analysis.project import discover_context
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.service import scan


def _feature(index: int, dimensions: tuple[tuple[str, ...], ...]) -> FunctionFeatures:
    return FunctionFeatures(
        logical_name=f"m:f{index:04}",
        module="m",
        logical_path="m.py",
        source_sha256="a" * 64,
        start_line=index + 1,
        start_column=0,
        end_line=index + 1,
        end_column=1,
        callees=dimensions[0],
        attributes_globals=dimensions[1],
        exceptions=dimensions[2],
        side_effects=dimensions[3],
        dictionary_schema_operations=dimensions[4],
        contract_ids=dimensions[5],
        signature_shapes=("same-signature",),
        control_flow=("if",),
        weak_semantic_signals=("same-name",),
        unresolved_call_count=0,
    )


def _generate(features, clones=(), *, minimum=2, coverage=0.25, prevalence=1.0, bound=50_000):
    return generate_commonality_candidates(
        features,
        clones,
        minimum_ownership_dimensions=minimum,
        minimum_bilateral_coverage=coverage,
        maximum_feature_subject_fraction=prevalence,
        maximum_pairs=bound,
    )


def test_report_dimension_totals_follow_subject_ids_not_logical_names(tmp_path: Path) -> None:
    source = "from api import shared, extra\n"
    for index in range(8):
        source += (
            f"def f{index}():\n    shared()\n"
            + ("    extra()\n" if index % 2 else "")
            + "    raise ValueError('failure')\n"
        )
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    config = AnalysisConfig()
    config = config.model_copy(
        update={
            "clone_thresholds": config.clone_thresholds.model_copy(
                update={"commonality_max_feature_subject_fraction": 1.0}
            )
        }
    )
    report = scan(discover_context(tmp_path, config))
    profiles = {item.subject_id: item for item in report.function_profiles}
    subjects = {item.subject_id: item for item in report.subjects}
    reversed_asymmetric = 0
    for pair in report.function_commonalities:
        left, right = profiles[pair.left_subject_id], profiles[pair.right_subject_id]
        dimension = next(item for item in pair.dimensions if item.kind.value == "callees")
        assert dimension.left_total == len(left.callees)
        assert dimension.right_total == len(right.callees)
        assert set(dimension.shared_identities) == set(left.callees) & set(right.callees)
        assert pair.evidence[0].span == left.evidence[0].span
        assert pair.evidence[1].span == right.evidence[0].span
        reversed_asymmetric += subjects[pair.left_subject_id].logical_name > subjects[
            pair.right_subject_id
        ].logical_name and len(left.callees) != len(right.callees)
    assert reversed_asymmetric, "fixture must exercise reversed, unequal feature dimensions"


@pytest.mark.parametrize("minimum", [1, 2, 3, 6])
@pytest.mark.parametrize("coverage,prevalence", [(0.25, 0.1), (0.5, 0.4), (1.0, 1.0)])
def test_index_matches_exhaustive_small_fixture_oracle(minimum, coverage, prevalence) -> None:
    rng = random.Random(44)
    features = tuple(
        _feature(
            i,
            tuple(
                tuple(sorted(rng.sample([str(n) for n in range(9)], rng.randrange(5))))
                for _ in range(6)
            ),
        )
        for i in range(30)
    )
    threshold = max(2, math.ceil(len(features) * prevalence))
    postings = {}
    for feature in features:
        for kind, values in feature.ownership_dimensions():
            for value in values:
                postings.setdefault((kind, value), set()).add(feature.logical_name)
    clones = ((features[0].logical_name, features[-1].logical_name),)
    expected = set(clones)
    for left, right in combinations(features, 2):
        qualifying = 0
        for (kind, lv), (_, rv) in zip(
            left.ownership_dimensions(), right.ownership_dimensions(), strict=True
        ):
            shared = {v for v in set(lv) & set(rv) if len(postings[kind, v]) <= threshold}
            qualifying += (
                bool(shared)
                and len(shared) / len(lv) >= coverage
                and len(shared) / len(rv) >= coverage
            )
        if qualifying >= minimum:
            expected.add((left.logical_name, right.logical_name))
    result = _generate(features, clones, minimum=minimum, coverage=coverage, prevalence=prevalence)
    assert result.pairs == tuple(sorted(expected))
    assert result.truncated_pairs == 0
    assert result.index_dimension_rejections_exact
    assert (
        _generate(
            tuple(reversed(features)),
            clones,
            minimum=minimum,
            coverage=coverage,
            prevalence=prevalence,
        )
        == result
    )


def test_dense_single_dimension_is_counted_without_enumerating_pairs(monkeypatch) -> None:
    features = tuple(_feature(i, (("shared",), (), (), (), (), ())) for i in range(4000))
    visited = 0
    original = commonality_index._positions

    def counted(mask):
        nonlocal visited
        for value in original(mask):
            visited += 1
            yield value

    monkeypatch.setattr(commonality_index, "_positions", counted)
    result = _generate(features, bound=5)
    assert not result.pairs and result.unique_candidates == result.truncated_pairs == 0
    assert result.index_dimension_rejections == 4000 * 3999 // 2
    assert result.index_dimension_rejections_exact
    assert len(result.rejection_examples) == visited == 25


def test_dense_eligible_index_stops_at_candidate_budget(monkeypatch) -> None:
    features = tuple(_feature(i, (("call",), ("state",), (), (), (), ())) for i in range(4000))
    visited = 0
    original = commonality_index._positions

    def counted(mask):
        nonlocal visited
        for value in original(mask):
            visited += 1
            yield value

    monkeypatch.setattr(commonality_index, "_positions", counted)
    result = _generate(features, bound=5)
    assert len(result.pairs) == result.unique_candidates == 5
    assert result.truncated_pairs == 1 and not result.index_dimension_rejections_exact
    assert visited == 6


def test_clones_bypass_preselection_and_still_obey_bounds() -> None:
    features = tuple(_feature(i, ((), (), (), (), (), ())) for i in range(3))
    clones = (
        (features[0].logical_name, features[1].logical_name),
        (features[1].logical_name, features[2].logical_name),
    )
    complete = _generate(features, clones)
    assert complete.pairs == clones and complete.clone_admissions == 2
    bounded = _generate(features, clones, bound=1)
    assert bounded.pairs == clones[:1] and bounded.truncated_pairs == 1
    assert not bounded.index_dimension_rejections_exact


def test_complete_index_exclusions_remain_visible_when_no_pair_is_admitted(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(
        "def helper(value):\n    return value\n"
        + "\n".join(f"def f{i}():\n    return helper({i})\n" for i in range(2))
    )
    report = scan(discover_context(tmp_path, AnalysisConfig()))
    admission = report.signal_quality.commonality_admission
    assert admission is not None and admission.index_dimension_rejections == 1
    assert admission.index_dimension_rejections_exact and not admission.admitted_pairs
    coverage = next(s for s in report.section_coverage if s.section == "function-commonality")
    assert coverage.state.value == "complete" and not coverage.truncated
    projection = build_projection(report, ProjectionName.COMMONALITY_ADMISSION)
    row = next(row for row in projection.rows if row.row_id == "index-dimension-rejections")
    assert row.cells[1].integer_value == 1 and row.cells[2].string_value == "exact"
