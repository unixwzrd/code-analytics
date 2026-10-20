"""Shared UI-suite inventory; inspect trusted tests, never analyzed project code."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, model_validator

UI_PREFIXES = ("test_ui_", "test_web_", "test_collection_", "test_code_browser", "test_code_drafts")
UI_SUPPORT = frozenset(
    {
        "test_user_help.py",
        "test_help_sections.py",
        "test_owned_web_handoff.py",
        "test_static_comparison_binding_invariants.py",
        "test_static_comparison_source_browsing.py",
        "test_static_comparison_source_review.py",
        "test_comparison_filters.py",
        "test_comparison_export.py",
    }
)


@dataclass(frozen=True, slots=True)
class UiTestModule:
    name: str
    browser: bool
    browser_tests: tuple[str, ...]


def classify_module(path: Path) -> UiTestModule | None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    browser = any(
        (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("playwright."))
        or (
            isinstance(node, ast.Import)
            and any(alias.name.startswith("playwright") for alias in node.names)
        )
        or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "importorskip"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.startswith("playwright.")
        )
        for node in ast.walk(tree)
    )
    declared = any(
        isinstance(node, ast.Attribute) and node.attr in {"ui", "ui_smoke"}
        for node in ast.walk(tree)
    )
    browser_tests = tuple(
        sorted(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and (
                    call.func.attr in {"sync_playwright", "async_playwright"}
                    or (
                        call.func.attr == "importorskip"
                        and call.args
                        and isinstance(call.args[0], ast.Constant)
                        and str(call.args[0].value).startswith("playwright")
                    )
                )
                for call in ast.walk(node)
            )
        )
    )
    if browser or declared or path.name.startswith(UI_PREFIXES) or path.name in UI_SUPPORT:
        return UiTestModule(path.name, browser, browser_tests)
    return None


def discover_ui_modules(tests: Path) -> tuple[UiTestModule, ...]:
    return tuple(
        module
        for path in sorted(tests.glob("test_*.py"))
        if (module := classify_module(path)) is not None
    )


class UiExecutionCase(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    node_id: str
    direct_browser_check: bool
    outcome: Literal["passed", "failed", "skipped", "not-run"]


class UiExecutionEvidence(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    expected_node_ids: tuple[str, ...]
    cases: tuple[UiExecutionCase, ...]

    @model_validator(mode="after")
    def unique_cases(self) -> UiExecutionEvidence:
        if len(set(self.expected_node_ids)) != len(self.expected_node_ids):
            raise ValueError("duplicate expected test identity")
        if len({item.node_id for item in self.cases}) != len(self.cases):
            raise ValueError("duplicate executed test identity")
        return self


@dataclass(eq=False)
class UiExecutionRecorder:
    path: Path
    expected: tuple[str, ...] = ()
    cases: dict[str, UiExecutionCase] = field(default_factory=dict)

    def expect(self, items: list[pytest.Item], expression: str) -> None:
        if expression not in {"ui", "ui_smoke"}:
            raise pytest.UsageError("UI execution evidence requires -m ui or -m ui_smoke")
        self.expected = tuple(
            sorted(item.nodeid for item in items if item.get_closest_marker(expression))
        )

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.cases = {
            item.nodeid: UiExecutionCase(
                node_id=item.nodeid,
                direct_browser_check=item.get_closest_marker("ui_direct_browser") is not None,
                outcome="not-run",
            )
            for item in session.items
        }

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        previous = self.cases.get(report.nodeid)
        if previous is None:
            return
        if report.failed or (report.passed and hasattr(report, "wasxfail")):
            self.cases[report.nodeid] = previous.model_copy(update={"outcome": "failed"})
        elif previous.outcome != "failed":
            if report.skipped:
                self.cases[report.nodeid] = previous.model_copy(update={"outcome": "skipped"})
            elif report.when == "call":
                self.cases[report.nodeid] = previous.model_copy(update={"outcome": "passed"})

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        evidence = UiExecutionEvidence(
            expected_node_ids=self.expected,
            cases=tuple(self.cases[key] for key in sorted(self.cases)),
        )
        self.path.write_text(evidence.model_dump_json(indent=2) + "\n", encoding="utf-8")
