"""Bind rendered pixels/XML to exact DOT goldens using the current provider."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from code_analysis.reports.projection_views import ProjectionFormat


def render_dot_reference(dot: bytes, output_format: ProjectionFormat) -> bytes:
    executable = shutil.which("dot")
    assert executable is not None, "Graphviz qualification provider is required"
    result = subprocess.run(
        (executable, f"-T{output_format.value}"),
        input=dot,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert result.stdout
    return result.stdout


def assert_graphviz_golden(render: Callable[[ProjectionFormat], bytes], dot_golden: Path) -> None:
    """No layout normalization: both formats must equal an independent DOT rendering."""
    dot = dot_golden.read_bytes()
    for output_format in (ProjectionFormat.SVG, ProjectionFormat.PNG):
        rendered = render(output_format)
        assert rendered == render(output_format), "same-provider rendering changed"
        assert rendered == render_dot_reference(dot, output_format), "rendered graph differs"
