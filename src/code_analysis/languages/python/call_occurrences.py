"""Bounded syntactic Python call occurrence facts; target code is never imported or executed."""

from __future__ import annotations

import ast
import heapq
from dataclasses import dataclass, field, replace
from enum import StrEnum


class LexicalScopeKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    ASYNC_FUNCTION = "async-function"
    LAMBDA = "lambda"
    COMPREHENSION = "comprehension"


class CallEvaluationContext(StrEnum):
    MODULE_BODY = "module-body"
    CLASS_BODY = "class-body"
    FUNCTION_BODY = "function-body"
    PARAMETER_DEFAULT = "parameter-default"
    DECORATOR = "decorator"
    ANNOTATION = "annotation"
    CLASS_BASE = "class-base"
    CLASS_KEYWORD = "class-keyword"
    LAMBDA_BODY = "lambda-body"
    LAMBDA_DEFAULT = "lambda-default"
    COMPREHENSION_OUTER_ITER = "comprehension-outer-iter"
    COMPREHENSION_INNER = "comprehension-inner"


class CallExecutionDisposition(StrEnum):
    EAGER = "eager"
    DEFERRED = "deferred"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PythonLexicalScopeRecord:
    kind: LexicalScopeKind
    name: str | None
    definition_line: int
    definition_column: int

    def __post_init__(self) -> None:
        if isinstance(self.definition_line, bool) or not isinstance(self.definition_line, int):
            raise TypeError("definition_line must be an exact int")
        if isinstance(self.definition_column, bool) or not isinstance(self.definition_column, int):
            raise TypeError("definition_column must be an exact int")
        if self.definition_line < 1:
            raise ValueError("definition_line must be positive")
        if self.definition_column < 0:
            raise ValueError("definition_column must be nonnegative")


@dataclass(frozen=True, slots=True)
class PythonCallOccurrenceFact:
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    callee_dotted: str | None
    scope_path: tuple[PythonLexicalScopeRecord, ...]
    evaluation_context: CallEvaluationContext
    execution_disposition: CallExecutionDisposition
    conditional: bool
    deferred: bool

    def __post_init__(self) -> None:
        for name in ("start_line", "start_column", "end_line", "end_column"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an exact int")


@dataclass(frozen=True, slots=True)
class PythonCallOccurrenceBatch:
    occurrences: tuple[PythonCallOccurrenceFact, ...]
    total_observed: int
    total_retained: int
    total_omitted: int
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.total_retained != len(self.occurrences):
            raise ValueError("total_retained must match occurrences length")
        if self.total_observed != self.total_retained + self.total_omitted:
            raise ValueError("observed counts must reconcile retained and omitted")
        if tuple(sorted(set(self.limitations))) != self.limitations:
            raise ValueError("limitations must be unique and sorted")


_LIMITATION_DECORATOR_APPLICATION = "implicit-decorator-application-not-modeled"
_LIMITATION_CONTEXT_MANAGER = "implicit-context-manager-calls-not-modeled"
_LIMITATION_FUTURE_ANNOTATIONS = "future-annotations-defer-annotation-execution"
_LIMITATION_TRUNCATED = "truncated-at-max-occurrences"


def discover_python_call_occurrences(
    source: str,
    *,
    max_occurrences: int = 100_000,
) -> PythonCallOccurrenceBatch:
    if isinstance(max_occurrences, bool) or not isinstance(max_occurrences, int):
        raise TypeError("max_occurrences must be an exact int")
    if max_occurrences <= 0:
        raise ValueError("max_occurrences must be positive")

    tree = ast.parse(source)
    future_annotations = _module_uses_future_annotations(tree)
    module_scope = PythonLexicalScopeRecord(
        LexicalScopeKind.MODULE,
        None,
        1,
        0,
    )
    collector = _CallCollector(
        future_annotations=future_annotations,
        module_scope=module_scope,
        max_occurrences=max_occurrences,
    )
    collector.collect(tree)
    return collector.build_batch()


def _module_uses_future_annotations(tree: ast.Module) -> bool:
    for statement in tree.body:
        if not isinstance(statement, ast.ImportFrom) or statement.level:
            continue
        if statement.module != "__future__":
            continue
        if any(alias.name == "annotations" for alias in statement.names):
            return True
    return False


def _safe_dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _safe_dotted(node.value)
        if prefix is None:
            return None
        return f"{prefix}.{node.attr}"
    return None


def _call_span_key(node: ast.Call, traversal_ordinal: int) -> tuple[int, int, int, int, int]:
    end_line = node.end_lineno or node.lineno
    end_column = node.end_col_offset if node.end_col_offset is not None else node.col_offset
    return (node.lineno, node.col_offset, end_line, end_column, traversal_ordinal)


def _invert_span_key(key: tuple[int, int, int, int, int]) -> tuple[int, int, int, int, int]:
    return tuple(-part for part in key)


@dataclass(frozen=True, slots=True)
class _VisitFrame:
    scope_path: tuple[PythonLexicalScopeRecord, ...]
    evaluation_context: CallEvaluationContext
    execution_disposition: CallExecutionDisposition
    conditional_depth: int


@dataclass(frozen=True, slots=True, order=True)
class _RetainedCall:
    inverted_span_key: tuple[int, int, int, int, int]
    fact: PythonCallOccurrenceFact = field(compare=False)


class _CallCollector:
    def __init__(
        self,
        *,
        future_annotations: bool,
        module_scope: PythonLexicalScopeRecord,
        max_occurrences: int,
    ) -> None:
        self._future_annotations = future_annotations
        self._module_scope = module_scope
        self._max_occurrences = max_occurrences
        self._retained_heap: list[_RetainedCall] = []
        self._total_observed = 0
        self._traversal_ordinal = 0
        self._limitations: set[str] = {
            _LIMITATION_DECORATOR_APPLICATION,
            _LIMITATION_CONTEXT_MANAGER,
            "syntactic-context-is-not-runtime-execution-or-call-order",
            "callee-spelling-does-not-establish-name-binding-or-dispatch",
        }
        if future_annotations:
            self._limitations.add(_LIMITATION_FUTURE_ANNOTATIONS)

    def collect(self, tree: ast.Module) -> None:
        frame = _VisitFrame(
            (self._module_scope,),
            CallEvaluationContext.MODULE_BODY,
            CallExecutionDisposition.EAGER,
            0,
        )
        for statement in tree.body:
            self._visit_stmt(statement, frame)

    def build_batch(self) -> PythonCallOccurrenceBatch:
        ordered = sorted(
            (entry.fact for entry in self._retained_heap),
            key=lambda fact: (
                fact.start_line,
                fact.start_column,
                fact.end_line,
                fact.end_column,
            ),
        )
        omitted = self._total_observed - len(ordered)
        if omitted:
            self._limitations.add(_LIMITATION_TRUNCATED)
        limitations = tuple(sorted(self._limitations))
        return PythonCallOccurrenceBatch(
            occurrences=tuple(ordered),
            total_observed=self._total_observed,
            total_retained=len(ordered),
            total_omitted=omitted,
            limitations=limitations,
        )

    def _record_call(self, node: ast.Call, frame: _VisitFrame) -> None:
        self._total_observed += 1
        ordinal = self._traversal_ordinal
        self._traversal_ordinal += 1
        fact = self._materialize(node, frame)
        span_key = _call_span_key(node, ordinal)
        inverted = _invert_span_key(span_key)
        entry = _RetainedCall(inverted, fact)
        if len(self._retained_heap) < self._max_occurrences:
            heapq.heappush(self._retained_heap, entry)
            return
        if inverted <= self._retained_heap[0].inverted_span_key:
            return
        heapq.heapreplace(self._retained_heap, entry)

    def _materialize(self, node: ast.Call, frame: _VisitFrame) -> PythonCallOccurrenceFact:
        end_line = node.end_lineno or node.lineno
        end_column = node.end_col_offset if node.end_col_offset is not None else node.col_offset
        disposition = _effective_disposition(frame)
        return PythonCallOccurrenceFact(
            start_line=node.lineno,
            start_column=node.col_offset,
            end_line=end_line,
            end_column=end_column,
            callee_dotted=_safe_dotted(node.func),
            scope_path=frame.scope_path,
            evaluation_context=frame.evaluation_context,
            execution_disposition=disposition,
            conditional=frame.conditional_depth > 0,
            deferred=frame.execution_disposition is CallExecutionDisposition.DEFERRED,
        )

    def _visit_stmt(self, node: ast.stmt, frame: _VisitFrame) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._visit_function_definition(node, frame)
            return
        if isinstance(node, ast.ClassDef):
            self._visit_class_definition(node, frame)
            return
        if isinstance(node, ast.If):
            self._visit_expr(node.test, _with_conditional(frame))
            for statement in node.body:
                self._visit_stmt(statement, _in_conditional_branch(frame))
            for statement in node.orelse:
                self._visit_stmt(statement, _in_conditional_branch(frame))
            return
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self._visit_expr(node.iter, frame)
            self._visit_assignment_target(node.target, _with_conditional(frame))
            for statement in node.body:
                self._visit_stmt(statement, _in_conditional_branch(frame))
            for statement in node.orelse:
                self._visit_stmt(statement, _in_conditional_branch(frame))
            return
        if isinstance(node, ast.While):
            self._visit_expr(node.test, _with_conditional(frame))
            for statement in node.body:
                self._visit_stmt(statement, _in_conditional_branch(frame))
            for statement in node.orelse:
                self._visit_stmt(statement, _in_conditional_branch(frame))
            return
        if isinstance(node, (ast.Try, ast.TryStar)):
            for statement in node.body:
                self._visit_stmt(statement, frame)
            for handler in node.handlers:
                if handler.type is not None:
                    self._visit_expr(handler.type, _with_conditional(frame))
                for statement in handler.body:
                    self._visit_stmt(statement, _in_conditional_branch(frame))
            for statement in node.orelse:
                self._visit_stmt(statement, _in_conditional_branch(frame))
            for statement in node.finalbody:
                self._visit_stmt(statement, frame)
            return
        if isinstance(node, ast.With):
            for item in node.items:
                self._visit_expr(item.context_expr, frame)
                if item.optional_vars is not None:
                    self._visit_assignment_target(item.optional_vars, frame)
            for statement in node.body:
                self._visit_stmt(statement, frame)
            return
        if isinstance(node, ast.Match):
            self._visit_expr(node.subject, frame)
            for case in node.cases:
                self._visit_pattern(case.pattern, _in_conditional_branch(frame))
                if case.guard is not None:
                    self._visit_expr(case.guard, _with_conditional(_in_conditional_branch(frame)))
                for statement in case.body:
                    self._visit_stmt(statement, _in_conditional_branch(frame))
            return
        if isinstance(node, ast.TypeAlias):
            self._visit_type_params(node.type_params, frame)
            self._visit_deferred_type_surface(node.value, frame)
            return
        if isinstance(node, ast.Expr):
            self._visit_expr(node.value, frame)
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            self._visit_assignment_stmt(node, frame)
            return
        if isinstance(node, ast.Return):
            if node.value is not None:
                self._visit_expr(node.value, frame)
            return
        if isinstance(node, (ast.Raise, ast.Assert)):
            if isinstance(node, ast.Assert):
                self._visit_expr(node.test, frame)
                if node.msg is not None:
                    self._visit_expr(node.msg, _with_conditional(frame))
            elif node.exc is not None:
                self._visit_expr(node.exc, frame)
            if isinstance(node, ast.Raise) and node.cause is not None:
                self._visit_expr(node.cause, frame)
            return
        if isinstance(node, ast.Delete):
            for target in node.targets:
                self._visit_assignment_target(target, frame)
            return
        self._visit_stmt_children(node, frame)

    def _visit_stmt_children(self, node: ast.AST, frame: _VisitFrame) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                self._visit_stmt(child, frame)
            elif isinstance(child, ast.expr):
                self._visit_expr(child, frame)
            else:
                self._visit_stmt_children(child, frame)

    def _visit_assignment_stmt(self, node: ast.stmt, frame: _VisitFrame) -> None:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                self._visit_assignment_target(target, frame)
            self._visit_expr(node.value, frame)
            return
        if isinstance(node, ast.AnnAssign):
            self._visit_assignment_target(node.target, frame)
            if node.value is not None:
                self._visit_expr(node.value, frame)
            if node.annotation is not None:
                self._visit_annotation(node.annotation, frame)
            return
        if isinstance(node, ast.AugAssign):
            self._visit_assignment_target(node.target, frame)
            self._visit_expr(node.value, frame)

    def _visit_assignment_target(self, node: ast.expr, frame: _VisitFrame) -> None:
        if isinstance(node, (ast.List, ast.Tuple)):
            for element in node.elts:
                self._visit_assignment_target(element, frame)
            return
        if isinstance(node, ast.Starred):
            self._visit_assignment_target(node.value, frame)
            return
        if isinstance(node, ast.Subscript):
            self._visit_expr(node.value, frame)
            self._visit_expr(node.slice, frame)
            return
        if isinstance(node, ast.Attribute):
            self._visit_expr(node.value, frame)
            return

    def _visit_function_definition(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        enclosing: _VisitFrame,
    ) -> None:
        for decorator in node.decorator_list:
            self._visit_expr(
                decorator,
                replace(enclosing, evaluation_context=CallEvaluationContext.DECORATOR),
            )
        self._visit_signature(node, enclosing)
        kind = (
            LexicalScopeKind.ASYNC_FUNCTION
            if isinstance(node, ast.AsyncFunctionDef)
            else LexicalScopeKind.FUNCTION
        )
        function_scope = PythonLexicalScopeRecord(
            kind,
            node.name,
            node.lineno,
            node.col_offset,
        )
        body_frame = replace(
            enclosing,
            scope_path=(*enclosing.scope_path, function_scope),
            evaluation_context=CallEvaluationContext.FUNCTION_BODY,
            execution_disposition=CallExecutionDisposition.DEFERRED,
        )
        for statement in node.body:
            self._visit_stmt(statement, body_frame)

    def _visit_class_definition(self, node: ast.ClassDef, enclosing: _VisitFrame) -> None:
        for decorator in node.decorator_list:
            self._visit_expr(
                decorator,
                replace(enclosing, evaluation_context=CallEvaluationContext.DECORATOR),
            )
        self._visit_type_params(node.type_params, enclosing)
        for base in node.bases:
            self._visit_expr(
                base,
                replace(enclosing, evaluation_context=CallEvaluationContext.CLASS_BASE),
            )
        for keyword in node.keywords:
            self._visit_expr(
                keyword.value,
                replace(enclosing, evaluation_context=CallEvaluationContext.CLASS_KEYWORD),
            )
        class_scope = PythonLexicalScopeRecord(
            LexicalScopeKind.CLASS,
            node.name,
            node.lineno,
            node.col_offset,
        )
        body_frame = replace(
            enclosing,
            scope_path=(*enclosing.scope_path, class_scope),
            evaluation_context=CallEvaluationContext.CLASS_BODY,
        )
        for statement in node.body:
            self._visit_stmt(statement, body_frame)

    def _visit_signature(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        enclosing: _VisitFrame,
    ) -> None:
        self._visit_type_params(node.type_params, enclosing)
        default_frame = replace(
            enclosing,
            evaluation_context=CallEvaluationContext.PARAMETER_DEFAULT,
        )
        positional = list(node.args.posonlyargs) + list(node.args.args)
        defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
        for arg, default in zip(positional, defaults, strict=False):
            if arg.annotation is not None:
                self._visit_annotation(arg.annotation, enclosing)
            if default is not None:
                self._visit_expr(default, default_frame)
        if node.args.vararg is not None and node.args.vararg.annotation is not None:
            self._visit_annotation(node.args.vararg.annotation, enclosing)
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=False):
            if arg.annotation is not None:
                self._visit_annotation(arg.annotation, enclosing)
            if default is not None:
                self._visit_expr(default, default_frame)
        if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
            self._visit_annotation(node.args.kwarg.annotation, enclosing)
        if node.returns is not None:
            self._visit_annotation(node.returns, enclosing)

    def _visit_type_params(
        self,
        type_params: list[ast.type_param] | None,
        enclosing: _VisitFrame,
    ) -> None:
        if not type_params:
            return
        for param in type_params:
            if isinstance(param, ast.TypeVar) and param.bound is not None:
                self._visit_deferred_type_surface(param.bound, enclosing)
            # Python 3.12 ParamSpec/TypeVarTuple have no default expression.

    def _visit_annotation(self, node: ast.expr, enclosing: _VisitFrame) -> None:
        if self._future_annotations:
            disposition = CallExecutionDisposition.DEFERRED
        else:
            disposition = CallExecutionDisposition.UNKNOWN
        self._visit_expr(
            node,
            replace(
                enclosing,
                evaluation_context=CallEvaluationContext.ANNOTATION,
                execution_disposition=disposition,
            ),
        )

    def _visit_deferred_type_surface(self, node: ast.expr, enclosing: _VisitFrame) -> None:
        self._visit_expr(
            node,
            replace(
                enclosing,
                evaluation_context=CallEvaluationContext.ANNOTATION,
                execution_disposition=CallExecutionDisposition.UNKNOWN,
            ),
        )

    def _visit_expr(self, node: ast.expr, frame: _VisitFrame) -> None:
        if isinstance(node, ast.Call):
            self._record_call(node, frame)
        if isinstance(node, ast.Lambda):
            self._visit_lambda(node, frame)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            self._visit_comprehension(node, frame)
            return
        if isinstance(node, ast.IfExp):
            self._visit_expr(node.test, _with_conditional(frame))
            self._visit_expr(node.body, _in_conditional_branch(frame))
            self._visit_expr(node.orelse, _in_conditional_branch(frame))
            return
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                self._visit_expr(value, _with_conditional(frame))
            return
        if isinstance(node, ast.Compare):
            self._visit_expr(node.left, frame)
            comparator_frame = frame if len(node.comparators) == 1 else _with_conditional(frame)
            for comparator in node.comparators:
                self._visit_expr(comparator, comparator_frame)
            return
        if isinstance(node, ast.NamedExpr):
            self._visit_expr(node.value, frame)
            self._visit_assignment_target(node.target, frame)
            return
        self._visit_expr_children(node, frame)

    def _visit_expr_children(self, node: ast.AST, frame: _VisitFrame) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self._visit_expr(child, frame)
            else:
                self._visit_expr_children(child, frame)

    def _visit_lambda(self, node: ast.Lambda, frame: _VisitFrame) -> None:
        default_frame = replace(
            frame,
            evaluation_context=CallEvaluationContext.LAMBDA_DEFAULT,
        )
        positional = list(node.args.posonlyargs) + list(node.args.args)
        defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
        for default in defaults:
            if default is not None:
                self._visit_expr(default, default_frame)
        for default in node.args.kw_defaults:
            if default is not None:
                self._visit_expr(default, default_frame)
        lambda_scope = PythonLexicalScopeRecord(
            LexicalScopeKind.LAMBDA,
            None,
            node.lineno,
            node.col_offset,
        )
        body_frame = replace(
            frame,
            scope_path=(*frame.scope_path, lambda_scope),
            evaluation_context=CallEvaluationContext.LAMBDA_BODY,
            execution_disposition=CallExecutionDisposition.DEFERRED,
        )
        self._visit_expr(node.body, body_frame)

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
        frame: _VisitFrame,
    ) -> None:
        generators = node.generators
        is_generator = isinstance(node, ast.GeneratorExp)
        comp_scope = PythonLexicalScopeRecord(
            LexicalScopeKind.COMPREHENSION,
            None,
            node.lineno,
            node.col_offset,
        )
        inner_disposition = (
            CallExecutionDisposition.DEFERRED if is_generator else frame.execution_disposition
        )
        inner_conditional_depth = frame.conditional_depth + 1
        inner_frame = replace(
            frame,
            scope_path=(*frame.scope_path, comp_scope),
            evaluation_context=CallEvaluationContext.COMPREHENSION_INNER,
            execution_disposition=inner_disposition,
            conditional_depth=inner_conditional_depth,
        )
        for index, generator in enumerate(generators):
            if index == 0:
                self._visit_expr(
                    generator.iter,
                    replace(
                        frame,
                        evaluation_context=CallEvaluationContext.COMPREHENSION_OUTER_ITER,
                    ),
                )
            else:
                self._visit_expr(generator.iter, inner_frame)
            self._visit_assignment_target(generator.target, inner_frame)
            for if_clause in generator.ifs:
                self._visit_expr(if_clause, _with_conditional(inner_frame))
        if isinstance(node, ast.DictComp):
            self._visit_expr(node.key, inner_frame)
            self._visit_expr(node.value, inner_frame)
        else:
            self._visit_expr(node.elt, inner_frame)

    def _visit_pattern(self, node: ast.AST, frame: _VisitFrame) -> None:
        if isinstance(node, ast.MatchAs):
            if node.pattern is not None:
                self._visit_pattern(node.pattern, frame)
            return
        if isinstance(node, ast.MatchStar):
            return
        if isinstance(node, ast.MatchMapping):
            for key in node.keys:
                self._visit_expr(key, frame)
            for pattern in node.patterns:
                self._visit_pattern(pattern, frame)
            return
        if isinstance(node, ast.MatchSequence):
            for pattern in node.patterns:
                self._visit_pattern(pattern, frame)
            return
        if isinstance(node, ast.Starred):
            self._visit_pattern(node.value, frame)
            return
        if isinstance(node, (ast.List, ast.Tuple)):
            for element in node.elts:
                self._visit_pattern(element, frame)


def _with_conditional(frame: _VisitFrame) -> _VisitFrame:
    return replace(frame, conditional_depth=frame.conditional_depth + 1)


def _in_conditional_branch(frame: _VisitFrame) -> _VisitFrame:
    return replace(frame, conditional_depth=frame.conditional_depth + 1)


def _effective_disposition(frame: _VisitFrame) -> CallExecutionDisposition:
    if frame.execution_disposition == CallExecutionDisposition.UNKNOWN:
        return CallExecutionDisposition.UNKNOWN
    if frame.conditional_depth > 0:
        if frame.evaluation_context == CallEvaluationContext.LAMBDA_BODY:
            return CallExecutionDisposition.DEFERRED
        if (
            frame.evaluation_context == CallEvaluationContext.COMPREHENSION_INNER
            and frame.execution_disposition == CallExecutionDisposition.DEFERRED
        ):
            return CallExecutionDisposition.DEFERRED
        return CallExecutionDisposition.CONDITIONAL
    return frame.execution_disposition


def static_call_batch_from_source(
    source: str,
    *,
    max_occurrences: int,
) -> PythonCallOccurrenceBatch:
    """Collect bounded Python call facts from captured UTF-8 source text."""

    return discover_python_call_occurrences(source, max_occurrences=max_occurrences)
