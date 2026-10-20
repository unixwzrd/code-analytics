"""A valid but wrong diagram must fail the same-provider golden oracle."""

import shutil
from pathlib import Path

import pytest
from graphviz_assertions import assert_graphviz_golden, render_dot_reference

from code_analysis.reports.projection_views import ProjectionFormat

pytestmark = pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz is unavailable")
DOT = b'digraph G { alpha -> beta [label="call", color="red"]; }'


@pytest.mark.parametrize("output_format", [ProjectionFormat.SVG, ProjectionFormat.PNG])
@pytest.mark.parametrize(
    "changed",
    [
        b'digraph G { alpha -> beta [label="other", color="red"]; }',
        b'digraph G { alpha -> gamma [label="call", color="red"]; }',
        b'digraph G { alpha -> beta [label="call", color="blue"]; }',
        b"digraph G {}",
    ],
    ids=["label", "edge", "style", "blank"],
)
def test_graphviz_oracle_rejects_valid_but_wrong_image(
    tmp_path: Path, output_format: ProjectionFormat, changed: bytes
) -> None:
    golden = tmp_path / "expected.dot"
    golden.write_bytes(DOT)

    def render(requested: ProjectionFormat) -> bytes:
        return render_dot_reference(changed if requested is output_format else DOT, requested)

    with pytest.raises(AssertionError, match="rendered graph differs"):
        assert_graphviz_golden(render, golden)


def test_graphviz_oracle_rejects_unstable_rendering(tmp_path: Path) -> None:
    golden = tmp_path / "expected.dot"
    golden.write_bytes(DOT)
    count = 0

    def render(output_format: ProjectionFormat) -> bytes:
        nonlocal count
        count += 1
        return render_dot_reference(DOT, output_format) + b" " * count

    with pytest.raises(AssertionError, match="same-provider rendering changed"):
        assert_graphviz_golden(render, golden)


def test_graphviz_oracle_accepts_exact_same_provider_graph(tmp_path: Path) -> None:
    golden = tmp_path / "expected.dot"
    golden.write_bytes(DOT)
    assert_graphviz_golden(lambda output_format: render_dot_reference(DOT, output_format), golden)
