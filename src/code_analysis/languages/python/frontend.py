"""Standard-library AST front end for language-neutral Python inventory facts."""

from __future__ import annotations

import ast
import platform
from dataclasses import asdict

from code_analysis.languages.base import (
    FrontendContext,
    FrontendResult,
    NormalizedComplexityFact,
    NormalizedImportFact,
    NormalizedSymbolFact,
    SourceUnit,
)
from code_analysis.languages.python.native_ast import complexity_for_function
from code_analysis.models import (
    AvailabilityState,
    Completeness,
    Diagnostic,
    DiagnosticLevel,
    ModuleSystem,
    ParserProvenance,
    ProviderAvailability,
    SourceLanguage,
)
from code_analysis.report_models import (
    AdapterCapabilityKind,
    AdapterCapabilityRecord,
    AdapterCapabilityState,
)


class PythonFrontend:
    """Normalize Python AST inventory without exposing AST nodes."""

    frontend_id = "python-ast"
    source_language = SourceLanguage.PYTHON

    @property
    def capabilities(self) -> tuple[AdapterCapabilityRecord, ...]:
        partial = {
            AdapterCapabilityKind.DATA_FLOW: (
                "Bounded data flow is completed by Python-owned analyzers."
            ),
            AdapterCapabilityKind.CONTRACTS_SHAPES: (
                "Contracts and dictionary shapes are completed by Python-owned analyzers."
            ),
            AdapterCapabilityKind.TEST_DISCOVERY: (
                "Framework discovery and dynamic tests remain unresolved."
            ),
            AdapterCapabilityKind.COVERAGE_BINDING: (
                "Coverage binding requires separately imported or executed coverage evidence."
            ),
        }
        return tuple(
            AdapterCapabilityRecord(
                capability=kind,
                state=(
                    AdapterCapabilityState.PARTIAL
                    if kind in partial
                    else AdapterCapabilityState.SUPPORTED
                ),
                limitations=((partial[kind],) if kind in partial else ()),
            )
            for kind in AdapterCapabilityKind
        )

    def availability(self, context: FrontendContext) -> ProviderAvailability:
        del context
        return ProviderAvailability(
            provider_id=self.frontend_id,
            state=AvailabilityState.AVAILABLE,
            version=platform.python_version(),
        )

    def analyze(self, unit: SourceUnit, context: FrontendContext) -> FrontendResult:
        diagnostics: tuple[Diagnostic, ...] = ()
        try:
            tree = ast.parse(unit.source, filename=unit.logical_path, type_comments=True)
        except SyntaxError as exc:
            diagnostics = (
                Diagnostic(
                    code="source-syntax-error",
                    level=DiagnosticLevel.ERROR,
                    message=f"syntax error at line {exc.lineno or 1}: {exc.msg}",
                    path=unit.logical_path,
                    provider=self.frontend_id,
                    language=SourceLanguage.PYTHON,
                ),
            )
            return _result(
                unit,
                context.configuration_sha256,
                (),
                (),
                diagnostics,
                Completeness.FAILED,
            )

        symbols = tuple(_symbols(tree))
        imports = tuple(_imports(tree))
        return _result(
            unit,
            context.configuration_sha256,
            symbols,
            imports,
            diagnostics,
            Completeness.COMPLETE,
        )


def _result(
    unit: SourceUnit,
    configuration_sha256: str,
    symbols: tuple[NormalizedSymbolFact, ...],
    imports: tuple[NormalizedImportFact, ...],
    diagnostics: tuple[Diagnostic, ...],
    completeness: Completeness,
) -> FrontendResult:
    return FrontendResult(
        unit=unit,
        symbols=symbols,
        imports=imports,
        exports=(),
        functions=(),
        states=(),
        module_system=ModuleSystem.UNRESOLVED,
        syntax_mode="python",
        provenance=ParserProvenance(
            frontend_id="python-ast",
            logical_path=unit.logical_path,
            parser_name="python-ast",
            parser_version=platform.python_version(),
            grammar_name="python",
            grammar_version=platform.python_version(),
            grammar_abi=None,
            language=SourceLanguage.PYTHON,
            syntax_mode=None,
            module_system=None,
            configuration_sha256=configuration_sha256,
            source_sha256=unit.source_sha256,
            normalization_identity="python-ast-inventory:v1",
        ),
        diagnostics=diagnostics,
        completeness=completeness,
    )


def _symbols(tree: ast.Module) -> list[NormalizedSymbolFact]:
    records: list[NormalizedSymbolFact] = []

    def walk(body: list[ast.stmt], parents: tuple[tuple[str, str], ...]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                kind = "class" if not parents else "nested_class"
                qualified = ".".join((*[name for name, _ in parents], node.name))
                records.append(_symbol(node, qualified, kind, (), None, None))
                walk(node.body, (*parents, (node.name, "class")))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not parents:
                    kind = "function"
                elif parents[-1][1] == "class":
                    kind = "method"
                else:
                    kind = "nested_function"
                qualified = ".".join((*[name for name, _ in parents], node.name))
                parameters = _parameters(node.args)
                records.append(
                    _symbol(
                        node,
                        qualified,
                        kind,
                        parameters,
                        ast.unparse(node.returns) if node.returns else None,
                        NormalizedComplexityFact(**asdict(complexity_for_function(node))),
                    )
                )
                walk(node.body, (*parents, (node.name, "function")))

    walk(tree.body, ())
    return records


def _symbol(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    qualified: str,
    kind: str,
    parameters: tuple[str, ...],
    returns: str | None,
    complexity: NormalizedComplexityFact | None,
) -> NormalizedSymbolFact:
    return NormalizedSymbolFact(
        qualified_name=qualified,
        name=node.name,
        kind=kind,
        start_line=node.lineno,
        start_column=node.col_offset,
        end_line=node.end_lineno or node.lineno,
        end_column=node.end_col_offset or node.col_offset,
        parameters=parameters,
        returns=returns,
        complexity=complexity,
        async_=isinstance(node, ast.AsyncFunctionDef),
        generator=False if isinstance(node, ast.ClassDef) else _is_generator(node),
    )


def _is_generator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Detect yields owned by *node* without descending into nested scopes."""

    class YieldFinder(ast.NodeVisitor):
        found = False

        def visit_Yield(self, child: ast.Yield) -> None:
            del child
            self.found = True

        def visit_YieldFrom(self, child: ast.YieldFrom) -> None:
            del child
            self.found = True

        def visit_FunctionDef(self, child: ast.FunctionDef) -> None:
            if child is node:
                self.generic_visit(child)

        def visit_AsyncFunctionDef(self, child: ast.AsyncFunctionDef) -> None:
            if child is node:
                self.generic_visit(child)

        def visit_Lambda(self, child: ast.Lambda) -> None:
            del child

        def visit_ClassDef(self, child: ast.ClassDef) -> None:
            del child

    finder = YieldFinder()
    finder.visit(node)
    return finder.found


def _parameters(args: ast.arguments) -> tuple[str, ...]:
    return (
        tuple(item.arg for item in (*args.posonlyargs, *args.args, *args.kwonlyargs))
        + ((args.vararg.arg,) if args.vararg else ())
        + ((args.kwarg.arg,) if args.kwarg else ())
    )


def _imports(tree: ast.Module) -> list[NormalizedImportFact]:
    records: list[NormalizedImportFact] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                records.append(
                    NormalizedImportFact(
                        specifier=alias.name,
                        kind="import",
                        dynamic=False,
                        start_line=node.lineno,
                        start_column=node.col_offset,
                        end_line=node.end_lineno or node.lineno,
                        end_column=node.end_col_offset or node.col_offset,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            records.append(
                NormalizedImportFact(
                    specifier=("." * node.level) + (node.module or ""),
                    kind="from-import",
                    dynamic=False,
                    start_line=node.lineno,
                    start_column=node.col_offset,
                    end_line=node.end_lineno or node.lineno,
                    end_column=node.end_col_offset or node.col_offset,
                )
            )
    return sorted(records, key=lambda item: (item.start_line, item.start_column, item.specifier))
