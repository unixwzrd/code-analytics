"""Typed AST facts for static test-impact analysis; target code is never imported."""

from __future__ import annotations

import ast
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from code_analysis.context import AnalysisContext
from code_analysis.project import logical_path
from code_analysis.report_models import TestRelationshipKind

TestFramework = Literal["pytest", "unittest", "mixed", "unknown"]
FixtureScope = Literal["function", "class", "module", "package", "session", "unknown"]


@dataclass(frozen=True, slots=True)
class TestModuleFact:
    module: str
    path: str
    source_sha256: str
    framework: TestFramework


@dataclass(frozen=True, slots=True)
class TestCaseFact:
    module: str
    path: str
    qualified_name: str
    line: int
    end_line: int
    parameters: tuple[str, ...]
    parameter_ids: tuple[str, ...]
    qualification: bool


@dataclass(frozen=True, slots=True)
class FixtureFact:
    module: str
    path: str
    qualified_name: str
    local_name: str
    line: int
    end_line: int
    scope: FixtureScope
    dependencies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TestRelationshipFact:
    source_qualified_name: str
    kind: TestRelationshipKind
    target_module: str | None
    target_symbol: str | None
    target_text: str | None
    resolved: bool
    line: int
    column: int
    observation: str
    limitation: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedTestModule:
    module: TestModuleFact
    cases: tuple[TestCaseFact, ...]
    fixtures: tuple[FixtureFact, ...]
    relationships: tuple[TestRelationshipFact, ...]


def discover_test_paths(context: AnalysisContext) -> tuple[Path, ...]:
    if context.target.is_file():
        return ()
    excluded = set(context.config.excludes)
    paths: set[Path] = set()
    for name in context.config.test_roots:
        root = context.source.physical_root / name
        if not root.is_dir() or root.is_symlink():
            continue
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(
                item
                for item in dirnames
                if item not in excluded and item != "fixtures" and not item.startswith(".")
            )
            parent = Path(directory)
            paths.update(
                parent / filename
                for filename in sorted(filenames)
                if filename.endswith(".py") and not (parent / filename).is_symlink()
            )
    return tuple(sorted(paths, key=lambda item: logical_path(context, item)))


def parse_test_module(context: AnalysisContext, path: Path) -> ParsedTestModule:
    payload = path.read_bytes()
    source = payload.decode("utf-8")
    tree = ast.parse(source, filename=path.name, type_comments=True)
    relative = logical_path(context, path)
    module_name = _test_module_name(relative)
    digest = hashlib.sha256(payload).hexdigest()
    imports, aliases, framework = _imports(tree)
    cases: list[TestCaseFact] = []
    fixtures: list[FixtureFact] = []
    relationships: list[TestRelationshipFact] = []

    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _collect_function(
                statement,
                module_name,
                relative,
                None,
                aliases,
                cases,
                fixtures,
                relationships,
            )
        elif isinstance(statement, ast.ClassDef):
            for child in statement.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _collect_function(
                        child,
                        module_name,
                        relative,
                        statement.name,
                        aliases,
                        cases,
                        fixtures,
                        relationships,
                    )

    sources: list[TestCaseFact | FixtureFact] = [*cases, *fixtures]
    for source_fact in sources:
        for imported in imports:
            relationships.append(
                TestRelationshipFact(
                    source_qualified_name=source_fact.qualified_name,
                    kind=TestRelationshipKind.MODULE_IMPORT,
                    target_module=imported,
                    target_symbol=None,
                    target_text=None,
                    resolved=True,
                    line=source_fact.line,
                    column=0,
                    observation="application module import protects module-level behavior",
                )
            )
    return ParsedTestModule(
        module=TestModuleFact(
            module=module_name,
            path=relative,
            source_sha256=digest,
            framework=framework,
        ),
        cases=tuple(sorted(cases, key=lambda item: item.qualified_name)),
        fixtures=tuple(sorted(fixtures, key=lambda item: item.qualified_name)),
        relationships=tuple(
            sorted(
                set(relationships),
                key=lambda item: (
                    item.source_qualified_name,
                    item.kind.value,
                    item.target_module or "",
                    item.target_symbol or "",
                    item.target_text or "",
                    item.line,
                    item.column,
                ),
            )
        ),
    )


def _test_module_name(relative: str) -> str:
    parts = list(Path(relative).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(
    tree: ast.Module,
) -> tuple[tuple[str, ...], dict[str, tuple[str, str | None]], TestFramework]:
    imported: set[str] = set()
    aliases: dict[str, tuple[str, str | None]] = {}
    pytest_seen = False
    unittest_seen = False
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                aliases[local] = (alias.name, None)
                imported.add(alias.name)
                pytest_seen |= alias.name == "pytest"
                unittest_seen |= alias.name == "unittest"
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            for alias in node.names:
                aliases[alias.asname or alias.name] = (node.module, alias.name)
            pytest_seen |= node.module == "pytest"
            unittest_seen |= node.module == "unittest" or node.module.startswith("unittest.")
    framework: TestFramework = (
        "mixed"
        if pytest_seen and unittest_seen
        else "pytest" if pytest_seen else "unittest" if unittest_seen else "unknown"
    )
    application = tuple(
        sorted(
            name
            for name in imported
            if name.split(".", 1)[0]
            not in {"pytest", "unittest", "mock", "typing", "pathlib", "os", "sys"}
        )
    )
    return application, aliases, framework


def _collect_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    module: str,
    path: str,
    class_name: str | None,
    aliases: dict[str, tuple[str, str | None]],
    cases: list[TestCaseFact],
    fixtures: list[FixtureFact],
    relationships: list[TestRelationshipFact],
) -> None:
    qualified = f"{module}:{class_name + '.' if class_name else ''}{node.name}"
    parameters = tuple(
        argument.arg
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        if argument.arg not in {"self", "cls"}
    )
    fixture_scope = _fixture_scope(node.decorator_list)
    is_test = node.name.startswith("test_")
    if fixture_scope is not None:
        fixtures.append(
            FixtureFact(
                module=module,
                path=path,
                qualified_name=qualified,
                local_name=node.name,
                line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                scope=fixture_scope,
                dependencies=parameters,
            )
        )
    if is_test:
        cases.append(
            TestCaseFact(
                module=module,
                path=path,
                qualified_name=qualified,
                line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                parameters=parameters,
                parameter_ids=_parametrize_names(node.decorator_list),
                qualification="qualif" in node.name.lower(),
            )
        )
    if not is_test and fixture_scope is None:
        return
    for parameter in parameters:
        relationships.append(
            TestRelationshipFact(
                source_qualified_name=qualified,
                kind=TestRelationshipKind.FIXTURE_USE,
                target_module=None,
                target_symbol=parameter,
                target_text=parameter,
                resolved=False,
                line=node.lineno,
                column=node.col_offset,
                observation="function parameter may request a fixture",
                limitation=(
                    "Framework fixture resolution is unresolved until a matching fixture is found."
                ),
            )
        )
    for child in ast.walk(node):
        direct = _direct_reference(child, aliases)
        if direct is not None:
            target_module, target_symbol = direct
            relationships.append(
                TestRelationshipFact(
                    source_qualified_name=qualified,
                    kind=TestRelationshipKind.DIRECT_SYMBOL,
                    target_module=target_module,
                    target_symbol=target_symbol,
                    target_text=f"{target_module}:{target_symbol}",
                    resolved=True,
                    line=getattr(child, "lineno", node.lineno),
                    column=getattr(child, "col_offset", 0),
                    observation="direct imported symbol reference",
                )
            )
        patch = _patch_target(child, aliases)
        if patch is not None:
            target, resolved = patch
            target_module, separator, target_symbol = target.rpartition(".")
            relationships.append(
                TestRelationshipFact(
                    source_qualified_name=qualified,
                    kind=TestRelationshipKind.PATCH,
                    target_module=target_module if separator else None,
                    target_symbol=target_symbol if separator else None,
                    target_text=target,
                    resolved=resolved and bool(separator),
                    line=getattr(child, "lineno", node.lineno),
                    column=getattr(child, "col_offset", 0),
                    observation="exact patch target" if resolved else "dynamic patch target",
                    limitation=(
                        None if resolved else "Dynamic patch target cannot be resolved statically."
                    ),
                )
            )


def _fixture_scope(decorators: list[ast.expr]) -> FixtureScope | None:
    for decorator in decorators:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call is not None else decorator
        name = _dotted_name(target)
        if name not in {"fixture", "pytest.fixture"}:
            continue
        if call is not None:
            for keyword in call.keywords:
                if (
                    keyword.arg == "scope"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    value = keyword.value.value
                    match value:
                        case "function" | "class" | "module" | "package" | "session":
                            return value
                        case _:
                            return "unknown"
        return "function"
    return None


def _parametrize_names(decorators: list[ast.expr]) -> tuple[str, ...]:
    names: set[str] = set()
    for decorator in decorators:
        if not isinstance(decorator, ast.Call) or not _dotted_name(decorator.func).endswith(
            "parametrize"
        ):
            continue
        if (
            decorator.args
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[0].value, str)
        ):
            names.update(
                item.strip() for item in decorator.args[0].value.split(",") if item.strip()
            )
    return tuple(sorted(names))


def _direct_reference(
    node: ast.AST, aliases: dict[str, tuple[str, str | None]]
) -> tuple[str, str] | None:
    if isinstance(node, ast.Name) and node.id in aliases:
        module, imported = aliases[node.id]
        if imported is not None:
            return module, imported
    if isinstance(node, ast.Attribute):
        dotted = _dotted_name(node)
        first, separator, remainder = dotted.partition(".")
        if separator and first in aliases:
            module, imported = aliases[first]
            prefix = f"{imported}." if imported else ""
            return module, prefix + remainder
    return None


def _patch_target(
    node: ast.AST, aliases: dict[str, tuple[str, str | None]]
) -> tuple[str, bool] | None:
    if not isinstance(node, ast.Call):
        return None
    name = _dotted_name(node.func)
    if name.endswith("patch") or name.endswith("setattr"):
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return node.args[0].value, True
        if name.endswith("setattr") and len(node.args) >= 2:
            base = _dotted_name(node.args[0])
            member = node.args[1]
            if base and isinstance(member, ast.Constant) and isinstance(member.value, str):
                first, _separator, remainder = base.partition(".")
                if first in aliases:
                    module, imported = aliases[first]
                    resolved = ".".join(
                        item for item in (module, imported, remainder, member.value) if item
                    )
                    return resolved, True
        if node.args:
            dynamic = _dotted_name(node.args[0])
            if dynamic:
                return dynamic, False
        return name or "dynamic-patch", False
    return None


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""
