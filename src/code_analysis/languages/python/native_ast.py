from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

from code_analysis.config import AnalysisConfig

BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.ExceptHandler,
    ast.With,
    ast.AsyncWith,
    ast.Match,
    ast.BoolOp,
    ast.IfExp,
)
NESTING_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.Match,
)


@dataclass(frozen=True, slots=True)
class InternalComplexity:
    physical_loc: int
    logical_loc: int
    branches: int
    nesting_depth: int
    argument_count: int
    cyclomatic_complexity: int
    weighted_score: int
    returns: int
    raises: int


@dataclass(frozen=True, slots=True)
class InternalSymbol:
    qualified_name: str
    name: str
    kind: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    decorators: tuple[str, ...]
    parameters: tuple[str, ...]
    returns: str | None
    complexity: InternalComplexity | None


@dataclass(frozen=True, slots=True)
class RawImport:
    module: str
    level: int
    names: tuple[str, ...]
    kind: str


@dataclass(frozen=True, slots=True)
class ParsedModule:
    name: str
    path: Path
    logical_path: str
    source_sha256: str
    line_count: int
    nonblank_line_count: int
    symbols: tuple[InternalSymbol, ...]
    imports: tuple[RawImport, ...]


class _ComplexityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.branches = 0
        self.depth = 0
        self.max_depth = 0
        self.returns = 0
        self.raises = 0
        self.logical = 0

    def visit(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        self.logical += isinstance(node, ast.stmt)
        if isinstance(node, ast.Return):
            self.returns += 1
        if isinstance(node, ast.Raise):
            self.raises += 1
        if isinstance(node, BRANCH_NODES):
            self.branches += 1
        nested = isinstance(node, NESTING_NODES)
        if nested:
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)
        super().visit(node)
        if nested:
            self.depth -= 1


def parse_module(path: Path, logical_path: str, name: str) -> ParsedModule:
    payload = path.read_bytes()
    source = payload.decode("utf-8")
    tree = ast.parse(source, filename=logical_path)
    symbols: list[InternalSymbol] = []
    imports: list[RawImport] = []

    def walk_body(body: list[ast.stmt], parents: tuple[tuple[str, str], ...]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                kind = "class" if not parents else "nested_class"
                qualified = ".".join((*[name for name, _ in parents], node.name))
                symbols.append(_class_symbol(node, qualified, kind))
                walk_body(node.body, (*parents, (node.name, "class")))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not parents:
                    kind = "function"
                elif parents[-1][1] == "class":
                    kind = "method"
                else:
                    kind = "nested_function"
                qualified = ".".join((*[name for name, _ in parents], node.name))
                symbols.append(_function_symbol(node, qualified, kind))
                walk_body(node.body, (*parents, (node.name, "function")))

    walk_body(tree.body, ())
    imports.extend(_extract_imports(tree))
    return ParsedModule(
        name=name,
        path=path,
        logical_path=logical_path,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        line_count=len(source.splitlines()),
        nonblank_line_count=sum(bool(line.strip()) for line in source.splitlines()),
        symbols=tuple(symbols),
        imports=tuple(imports),
    )


def _class_symbol(node: ast.ClassDef, qualified: str, kind: str) -> InternalSymbol:
    return InternalSymbol(
        qualified_name=qualified,
        name=node.name,
        kind=kind,
        start_line=node.lineno,
        start_column=node.col_offset,
        end_line=node.end_lineno or node.lineno,
        end_column=node.end_col_offset or node.col_offset,
        decorators=tuple(_unparse(item) for item in node.decorator_list),
        parameters=(),
        returns=None,
        complexity=None,
    )


def _function_symbol(
    node: ast.FunctionDef | ast.AsyncFunctionDef, qualified: str, kind: str
) -> InternalSymbol:
    complexity = complexity_for_function(node)
    args = node.args
    parameters = (
        tuple(item.arg for item in (*args.posonlyargs, *args.args, *args.kwonlyargs))
        + ((args.vararg.arg,) if args.vararg else ())
        + ((args.kwarg.arg,) if args.kwarg else ())
    )
    return InternalSymbol(
        qualified_name=qualified,
        name=node.name,
        kind=kind,
        start_line=node.lineno,
        start_column=node.col_offset,
        end_line=node.end_lineno or node.lineno,
        end_column=node.end_col_offset or node.col_offset,
        decorators=tuple(_unparse(item) for item in node.decorator_list),
        parameters=parameters,
        returns=_unparse(node.returns) if node.returns else None,
        complexity=complexity,
    )


def complexity_for_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> InternalComplexity:
    """Return the native complexity observation for a parsed function."""

    visitor = _ComplexityVisitor()
    for statement in node.body:
        visitor.visit(statement)
    args = node.args
    parameters = (
        tuple(item.arg for item in (*args.posonlyargs, *args.args, *args.kwonlyargs))
        + ((args.vararg.arg,) if args.vararg else ())
        + ((args.kwarg.arg,) if args.kwarg else ())
    )
    physical_loc = (node.end_lineno or node.lineno) - node.lineno + 1
    score = physical_loc // 25 + visitor.branches * 2 + visitor.max_depth * 2 + len(parameters)
    complexity = InternalComplexity(
        physical_loc=physical_loc,
        logical_loc=visitor.logical,
        branches=visitor.branches,
        nesting_depth=visitor.max_depth,
        argument_count=len(parameters),
        cyclomatic_complexity=visitor.branches + 1,
        weighted_score=score,
        returns=visitor.returns,
        raises=visitor.raises,
    )
    return complexity


def _extract_imports(tree: ast.AST) -> list[RawImport]:
    imports: list[RawImport] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function_depth = 0
            self.optional_depth = 0
            self.type_checking_depth = 0

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.function_depth += 1
            self.generic_visit(node)
            self.function_depth -= 1

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)

        def visit_If(self, node: ast.If) -> None:
            is_type = isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
            self.type_checking_depth += int(is_type)
            self.generic_visit(node)
            self.type_checking_depth -= int(is_type)

        def visit_Try(self, node: ast.Try) -> None:
            catches_import = any(
                isinstance(handler.type, ast.Name) and handler.type.id == "ImportError"
                for handler in node.handlers
            )
            self.optional_depth += int(catches_import)
            self.generic_visit(node)
            self.optional_depth -= int(catches_import)

        def kind(self) -> str:
            if self.type_checking_depth:
                return "type_checking"
            if self.optional_depth:
                return "optional"
            if self.function_depth:
                return "lazy"
            return "runtime"

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                imports.append(RawImport(alias.name, 0, (), self.kind()))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            imports.append(
                RawImport(
                    node.module or "",
                    node.level,
                    tuple(alias.name for alias in node.names if alias.name != "*"),
                    self.kind(),
                )
            )

    Visitor().visit(tree)
    return imports


def _unparse(node: ast.AST) -> str:
    return ast.unparse(node)


def exceeds_thresholds(symbol: InternalSymbol, config: AnalysisConfig) -> bool:
    metric = symbol.complexity
    if metric is None:
        return False
    threshold = config.thresholds
    return (
        metric.physical_loc > threshold.function_loc
        or metric.branches > threshold.branches
        or metric.nesting_depth > threshold.nesting
        or metric.argument_count > threshold.arguments
        or metric.weighted_score >= threshold.weighted_score
    )


def is_orchestration(symbol: InternalSymbol) -> bool:
    metric = symbol.complexity
    if metric is None:
        return False
    prefixes = ("apply_", "cmd_", "dispatch_", "handle_", "main", "process_", "run", "serve")
    return symbol.name.lower().startswith(prefixes) or (
        metric.branches >= 10 and metric.argument_count <= 3
    )
