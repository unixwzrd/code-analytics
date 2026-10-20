from __future__ import annotations

import pytest
from pydantic import ValidationError

from code_analysis.browse_index import BrowseIndex
from code_analysis.browse_index_build import build_browse_index
from code_analysis.config import AnalysisConfig
from code_analysis.dependency_navigation import (
    DependencyRequest,
    DependencyResult,
    investigate_dependencies,
)
from code_analysis.project import discover_context
from code_analysis.service import scan
from code_analysis.web_service import ReportViewService


@pytest.fixture
def dependency_index(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    sources = {
        "__init__.py": "",
        "a.py": "from . import b\n",
        "b.py": "from . import a\nfrom . import c\n",
        "c.py": "value = 1\n",
        "d.py": "value = 2\n",
    }
    for number in range(27):
        sources[f"e{number:02}.py"] = "value = 3\n"
        sources["a.py"] += f"from . import e{number:02}\n"
    for name, content in sources.items():
        (package / name).write_text(content)
    report = scan(discover_context(tmp_path, AnalysisConfig()))
    directory = tmp_path / "index"
    manifest = build_browse_index(ReportViewService(report), directory)
    # All exploration below works after removing its synthetic source inputs.
    for name in sources:
        (package / name).unlink()
    package.rmdir()
    return BrowseIndex.open(directory, report_sha256=manifest.report_sha256)


def test_path_component_and_disconnected_source_free(dependency_index):
    request = DependencyRequest(source="pkg.a", target="pkg.c")
    result = investigate_dependencies(dependency_index, request)
    assert result.path == ("pkg.a", "pkg.b", "pkg.c")
    assert not result.truncated
    assert (
        result.model_dump_json()
        == investigate_dependencies(dependency_index, request).model_dump_json()
    )
    component = investigate_dependencies(
        dependency_index, DependencyRequest(source="pkg.a", mode="component")
    )
    assert component.members == ("pkg.a", "pkg.b")
    assert not component.truncated
    disconnected = investigate_dependencies(
        dependency_index, DependencyRequest(source="pkg.a", target="pkg.d")
    )
    assert disconnected.path == ()
    assert not disconnected.truncated


@pytest.mark.parametrize("bound", [{"max_nodes": 1}, {"max_edges": 1}])
def test_traversal_bounds_are_unresolved_not_absence(dependency_index, bound):
    result = investigate_dependencies(
        dependency_index, DependencyRequest(source="pkg.a", target="pkg.c", **bound)
    )
    assert result.truncated
    assert not result.path
    assert "unresolved" in result.limitations[-1]
    component = investigate_dependencies(
        dependency_index, DependencyRequest(source="pkg.a", mode="component", **bound)
    )
    assert component.truncated
    assert "pkg.a" in component.members
    assert component.examined_edges <= component.request.max_edges
    assert component.visited_nodes <= component.request.max_nodes


def test_self_path_and_unknown_selection(dependency_index):
    result = investigate_dependencies(
        dependency_index, DependencyRequest(source="pkg.a", target="pkg.a")
    )
    assert result.path == ("pkg.a",)
    assert result.examined_edges == 0
    for request in (
        DependencyRequest(source="missing", target="pkg.a"),
        DependencyRequest(source="pkg.a", target="missing"),
    ):
        with pytest.raises(ValueError, match="not present"):
            investigate_dependencies(dependency_index, request)


def test_path_reads_later_adjacency_page(dependency_index):
    result = investigate_dependencies(
        dependency_index, DependencyRequest(source="pkg.a", target="pkg.e26")
    )
    assert result.path == ("pkg.a", "pkg.e26")
    assert result.examined_edges == 28
    assert not result.truncated


def test_partial_source_is_not_promoted(dependency_index):
    from code_analysis.models import Completeness

    class PartialReader:
        def module_navigation(self, module, view="outgoing", offset=0):
            return dependency_index.module_navigation(module, view, offset).model_copy(
                update={"source_completeness": Completeness.PARTIAL}
            )

    result = investigate_dependencies(
        PartialReader(), DependencyRequest(source="pkg.a", target="pkg.c")
    )
    assert result.path == ("pkg.a", "pkg.b", "pkg.c")
    assert not result.truncated
    assert result.source_completeness is Completeness.PARTIAL


@pytest.mark.parametrize(
    "change",
    [
        {"visited_nodes": True},
        {"examined_edges": 20001},
        {"path": ("pkg.c", "pkg.a")},
        {"members": ("pkg.a",)},
        {"path": ("pkg.a", "pkg.b", "pkg.b", "pkg.c")},
        {"visited_nodes": 1},
        {"extra": "unexpected"},
    ],
)
def test_result_rejects_contradictory_or_coercive_records(dependency_index, change):
    result = investigate_dependencies(
        dependency_index, DependencyRequest(source="pkg.a", target="pkg.c")
    )
    payload = result.model_dump()
    payload.update(change)
    with pytest.raises(ValidationError):
        DependencyResult.model_validate(payload)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"target": "x", "mode": "component"},
        {"target": "x", "max_nodes": True},
        {"target": "x", "max_edges": 0},
        {"target": "x", "max_nodes": 2001},
        {"target": "x", "max_edges": 20001},
        {"target": "x", "extra": 1},
    ],
)
def test_strict_dependency_request(options):
    with pytest.raises(ValidationError):
        DependencyRequest(source="pkg.a", **options)
