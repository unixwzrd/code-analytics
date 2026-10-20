"""Bounded syntactic JavaScript call/new occurrence facts; source is never executed."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Node

_CALL_SITE_TYPES = frozenset({"call_expression", "new_expression"})
_FUNCTION_TYPES = frozenset(
    {
        "function_declaration",
        "function_expression",
        "generator_function_declaration",
        "generator_function",
        "arrow_function",
        "method_definition",
    }
)
_CLASS_TYPES = frozenset({"class_declaration", "class"})
_LOGICAL_SHORT_CIRCUIT = frozenset({"&&", "||", "??"})


class JavascriptLexicalScopeKind(StrEnum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    GENERATOR_FUNCTION = "generator-function"
    ASYNC_FUNCTION = "async-function"
    ARROW_FUNCTION = "arrow-function"
    METHOD = "method"


class JavascriptCallSiteKind(StrEnum):
    CALL = "call"
    NEW = "new"


class JavascriptCallEvaluationContext(StrEnum):
    MODULE_BODY = "module-body"
    CLASS_BODY = "class-body"
    CLASS_HERITAGE = "class-heritage"
    FUNCTION_BODY = "function-body"
    ARROW_BODY = "arrow-body"
    PARAMETER_DEFAULT = "parameter-default"
    CLASS_STATIC_FIELD = "class-static-field"
    CLASS_STATIC_BLOCK = "class-static-block"
    CLASS_INSTANCE_FIELD = "class-instance-field"
    COMPUTED_KEY = "computed-key"
    DECORATOR = "decorator"


class CallExecutionDisposition(StrEnum):
    EAGER = "eager"
    DEFERRED = "deferred"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class JavascriptLexicalScopeRecord:
    kind: JavascriptLexicalScopeKind
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
class JavascriptCallOccurrenceFact:
    site_kind: JavascriptCallSiteKind
    start_byte: int
    end_byte: int
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    callee_dotted: str | None
    scope_path: tuple[JavascriptLexicalScopeRecord, ...]
    evaluation_context: JavascriptCallEvaluationContext
    execution_disposition: CallExecutionDisposition
    conditional: bool
    deferred: bool

    def __post_init__(self) -> None:
        for name in (
            "start_byte",
            "end_byte",
            "start_line",
            "start_column",
            "end_line",
            "end_column",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an exact int")
        if self.end_byte < self.start_byte:
            raise ValueError("end_byte must not precede start_byte")


@dataclass(frozen=True, slots=True)
class JavascriptCallOccurrenceBatch:
    occurrences: tuple[JavascriptCallOccurrenceFact, ...]
    total_observed: int
    total_retained: int
    total_omitted: int
    tree_complete: bool
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.total_retained != len(self.occurrences):
            raise ValueError("total_retained must match occurrences length")
        if self.total_observed != self.total_retained + self.total_omitted:
            raise ValueError("observed counts must reconcile retained and omitted")
        if tuple(sorted(set(self.limitations))) != self.limitations:
            raise ValueError("limitations must be unique and sorted")


_LIMITATION_TRUNCATED = "truncated-at-max-occurrences"
_LIMITATION_INCOMPLETE_TREE = "parse-tree-contains-errors-or-missing-nodes"
_LIMITATION_SYNTACTIC = "syntactic-context-is-not-runtime-execution-or-call-order"
_LIMITATION_CALLEE = "callee-spelling-does-not-establish-name-binding-or-dispatch"


def discover_javascript_call_occurrences(
    root: Node,
    source_bytes: bytes,
    *,
    max_occurrences: int = 100_000,
) -> JavascriptCallOccurrenceBatch:
    if isinstance(max_occurrences, bool) or not isinstance(max_occurrences, int):
        raise TypeError("max_occurrences must be an exact int")
    if max_occurrences <= 0:
        raise ValueError("max_occurrences must be positive")
    source_bytes.decode("utf-8")
    if (
        root.type != "program"
        or root.end_byte != len(source_bytes)
        or root.text != source_bytes[root.start_byte : root.end_byte]
    ):
        raise ValueError("JavaScript program tree does not match source bytes")
    prefix = source_bytes[: root.start_byte]
    expected_start = (prefix.count(b"\n"), len(prefix.rsplit(b"\n", 1)[-1]))
    if tuple(root.start_point) != expected_start:
        raise ValueError("JavaScript program tree coordinates do not match source bytes")

    tree_complete = not _tree_is_incomplete(root)
    module_scope = JavascriptLexicalScopeRecord(
        JavascriptLexicalScopeKind.MODULE,
        None,
        1,
        0,
    )
    collector = _CallCollector(
        source_bytes=source_bytes,
        module_scope=module_scope,
        max_occurrences=max_occurrences,
        tree_complete=tree_complete,
    )
    collector.collect(root)
    return collector.build_batch()


def _tree_is_incomplete(root: Node) -> bool:
    if getattr(root, "has_error", False):
        return True
    incomplete = False

    def walk(node: Node) -> None:
        nonlocal incomplete
        if node.type == "ERROR" or getattr(node, "is_missing", False):
            incomplete = True
        for child in node.children:
            walk(child)

    walk(root)
    return incomplete


def _span(node: Node) -> tuple[int, int, int, int, int, int]:
    return (
        node.start_byte,
        node.end_byte,
        node.start_point.row + 1,
        node.start_point.column,
        node.end_point.row + 1,
        node.end_point.column,
    )


def _span_key(node: Node, site_kind: JavascriptCallSiteKind, ordinal: int) -> tuple[int, ...]:
    start_byte, end_byte, start_line, start_column, _end_line, end_column = _span(node)
    kind_rank = 0 if site_kind is JavascriptCallSiteKind.CALL else 1
    return (start_byte, start_line, start_column, end_byte, end_column, kind_rank, ordinal)


def _invert_span_key(key: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(-part for part in key)


def _safe_callee(node: Node | None, source_bytes: bytes) -> str | None:
    if node is None:
        return None
    if node.type in {
        "identifier",
        "property_identifier",
        "private_property_identifier",
        "shorthand_property_identifier",
    }:
        return _node_text(node, source_bytes)
    if node.type in {"this", "super"}:
        return node.type
    if node.type == "import":
        return "import"
    if node.type == "member_expression":
        obj = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        if prop is None or prop.type not in {
            "identifier",
            "property_identifier",
            "private_property_identifier",
        }:
            return None
        left = _safe_callee(obj, source_bytes)
        right = _node_text(prop, source_bytes)
        if left is None or not right:
            return None
        return f"{left}.{right}"
    return None


def _node_text(node: Node, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _call_has_optional_chain(node: Node) -> bool:
    if node.type != "call_expression":
        return False
    if node.child_by_field_name("optional_chain") is not None:
        return True
    function = node.child_by_field_name("function")
    return _expression_has_optional_chain(function)


def _expression_has_optional_chain(node: Node | None) -> bool:
    if node is None:
        return False
    if node.type in {"member_expression", "subscript_expression"}:
        if node.child_by_field_name("optional_chain") is not None:
            return True
        return _expression_has_optional_chain(node.child_by_field_name("object"))
    if node.type == "call_expression":
        return _call_has_optional_chain(node)
    return False


def _scope_name_from_declaration(node: Node, source_bytes: bytes) -> str | None:
    name = node.child_by_field_name("name")
    if name is None:
        return None
    if name.type in {"identifier", "property_identifier"}:
        return _node_text(name, source_bytes)
    return None


def _function_scope_kind(node: Node) -> JavascriptLexicalScopeKind:
    node_type = node.type
    if node_type == "arrow_function":
        return JavascriptLexicalScopeKind.ARROW_FUNCTION
    if node_type == "method_definition":
        return JavascriptLexicalScopeKind.METHOD
    if node_type in {"generator_function_declaration", "generator_function"}:
        return JavascriptLexicalScopeKind.GENERATOR_FUNCTION
    if node.type in {"function_declaration", "function_expression"}:
        prefix = _leading_async_generator(node)
        if prefix == "async":
            return JavascriptLexicalScopeKind.ASYNC_FUNCTION
    return JavascriptLexicalScopeKind.FUNCTION


def _leading_async_generator(node: Node) -> str | None:
    for child in node.children:
        if not child.is_named and child.type == "async":
            return "async"
        if child.is_named:
            break
    return None


def _is_async_function(node: Node) -> bool:
    return _leading_async_generator(node) == "async"


@dataclass(frozen=True, slots=True)
class _VisitFrame:
    scope_path: tuple[JavascriptLexicalScopeRecord, ...]
    evaluation_context: JavascriptCallEvaluationContext
    execution_disposition: CallExecutionDisposition
    conditional_depth: int
    in_formal_parameters: bool


@dataclass(frozen=True, slots=True, order=True)
class _RetainedCall:
    inverted_span_key: tuple[int, ...]
    fact: JavascriptCallOccurrenceFact = field(compare=False)


class _CallCollector:
    def __init__(
        self,
        *,
        source_bytes: bytes,
        module_scope: JavascriptLexicalScopeRecord,
        max_occurrences: int,
        tree_complete: bool,
    ) -> None:
        self._source_bytes = source_bytes
        self._module_scope = module_scope
        self._max_occurrences = max_occurrences
        self._tree_complete = tree_complete
        self._retained_heap: list[_RetainedCall] = []
        self._total_observed = 0
        self._traversal_ordinal = 0
        self._limitations: set[str] = {_LIMITATION_SYNTACTIC, _LIMITATION_CALLEE}
        if not tree_complete:
            self._limitations.add(_LIMITATION_INCOMPLETE_TREE)

    def collect(self, root: Node) -> None:
        frame = _VisitFrame(
            (self._module_scope,),
            JavascriptCallEvaluationContext.MODULE_BODY,
            CallExecutionDisposition.EAGER,
            0,
            False,
        )
        self._visit_node(root, frame)

    def build_batch(self) -> JavascriptCallOccurrenceBatch:
        ordered = sorted(
            (entry.fact for entry in self._retained_heap),
            key=lambda fact: (
                fact.start_byte,
                fact.start_line,
                fact.start_column,
                fact.end_byte,
                fact.end_column,
                fact.site_kind.value,
            ),
        )
        omitted = self._total_observed - len(ordered)
        if omitted:
            self._limitations.add(_LIMITATION_TRUNCATED)
        limitations = tuple(sorted(self._limitations))
        return JavascriptCallOccurrenceBatch(
            occurrences=tuple(ordered),
            total_observed=self._total_observed,
            total_retained=len(ordered),
            total_omitted=omitted,
            tree_complete=self._tree_complete,
            limitations=limitations,
        )

    def _record_site(self, node: Node, frame: _VisitFrame) -> None:
        site_kind = (
            JavascriptCallSiteKind.NEW
            if node.type == "new_expression"
            else JavascriptCallSiteKind.CALL
        )
        self._total_observed += 1
        ordinal = self._traversal_ordinal
        self._traversal_ordinal += 1
        fact = self._materialize(node, frame, site_kind)
        span_key = _span_key(node, site_kind, ordinal)
        inverted = _invert_span_key(span_key)
        entry = _RetainedCall(inverted, fact)
        if len(self._retained_heap) < self._max_occurrences:
            heapq.heappush(self._retained_heap, entry)
            return
        if inverted <= self._retained_heap[0].inverted_span_key:
            return
        heapq.heapreplace(self._retained_heap, entry)

    def _materialize(
        self,
        node: Node,
        frame: _VisitFrame,
        site_kind: JavascriptCallSiteKind,
    ) -> JavascriptCallOccurrenceFact:
        start_byte, end_byte, start_line, start_column, end_line, end_column = _span(node)
        callee_node = (
            node.child_by_field_name("constructor")
            if site_kind is JavascriptCallSiteKind.NEW
            else node.child_by_field_name("function")
        )
        disposition = _effective_disposition(frame, node)
        optional = (
            _call_has_optional_chain(node) if site_kind is JavascriptCallSiteKind.CALL else False
        )
        conditional = frame.conditional_depth > 0 or optional
        deferred = frame.execution_disposition is CallExecutionDisposition.DEFERRED
        return JavascriptCallOccurrenceFact(
            site_kind=site_kind,
            start_byte=start_byte,
            end_byte=end_byte,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
            callee_dotted=_safe_callee(callee_node, self._source_bytes),
            scope_path=frame.scope_path,
            evaluation_context=frame.evaluation_context,
            execution_disposition=disposition,
            conditional=conditional,
            deferred=deferred,
        )

    def _visit_node(self, node: Node, frame: _VisitFrame) -> None:
        node_type = node.type
        if node_type in _CALL_SITE_TYPES:
            self._record_site(node, frame)
            self._visit_call_site_children(node, frame)
            return
        if node_type == "subscript_expression":
            receiver = node.child_by_field_name("object")
            index = node.child_by_field_name("index")
            if receiver is not None:
                self._visit_node(receiver, frame)
            if index is not None:
                index_frame = (
                    _with_conditional(frame) if _expression_has_optional_chain(node) else frame
                )
                self._visit_node(index, index_frame)
            return
        if node_type in _CLASS_TYPES:
            self._visit_class(node, frame)
            return
        if node_type in _FUNCTION_TYPES:
            self._visit_function(node, frame)
            return
        if node_type == "class_body":
            for child in node.named_children:
                self._visit_node(child, frame)
            return
        if node_type == "field_definition":
            self._visit_field_definition(node, frame)
            return
        if node_type == "class_static_block":
            static_frame = replace(
                frame,
                evaluation_context=JavascriptCallEvaluationContext.CLASS_STATIC_BLOCK,
            )
            body = node.child_by_field_name("body")
            if body is not None:
                self._visit_node(body, static_frame)
            return
        if node_type == "statement_block":
            for child in node.named_children:
                self._visit_node(child, frame)
            return
        if node_type == "if_statement":
            self._visit_if(node, frame)
            return
        if node_type in {"for_statement", "for_in_statement"}:
            self._visit_for(node, frame)
            return
        if node_type == "while_statement":
            self._visit_while(node, frame)
            return
        if node_type == "do_statement":
            self._visit_do(node, frame)
            return
        if node_type == "switch_statement":
            self._visit_switch(node, frame)
            return
        if node_type == "try_statement":
            self._visit_try(node, frame)
            return
        if node_type == "ternary_expression":
            self._visit_ternary(node, frame)
            return
        if node_type == "binary_expression":
            self._visit_binary(node, frame)
            return
        if node_type == "await_expression":
            for child in node.named_children:
                self._visit_node(child, frame)
            return
        if node_type == "jsx_expression":
            for child in node.named_children:
                self._visit_node(child, frame)
            return
        if node_type.startswith("jsx_"):
            self._visit_jsx(node, frame)
            return
        if node_type == "decorator":
            deco_frame = replace(
                frame,
                evaluation_context=JavascriptCallEvaluationContext.DECORATOR,
            )
            for child in node.named_children:
                self._visit_node(child, deco_frame)
            return
        if node_type == "formal_parameters":
            param_frame = replace(frame, in_formal_parameters=True)
            for child in node.named_children:
                self._visit_parameter_pattern(child, param_frame)
            return
        if node_type in {"assignment_pattern", "object_assignment_pattern"}:
            if frame.in_formal_parameters:
                self._visit_parameter_pattern(node, frame)
                return
            self._visit_named_children(node, frame)
            return
        self._visit_named_children(node, frame)

    def _visit_call_site_children(self, node: Node, frame: _VisitFrame) -> None:
        if node.type == "new_expression":
            constructor = node.child_by_field_name("constructor")
            if constructor is not None:
                self._visit_node(constructor, frame)
            arguments = node.child_by_field_name("arguments")
            if arguments is not None:
                self._visit_call_arguments(arguments, frame)
            return
        function = node.child_by_field_name("function")
        if function is not None:
            self._visit_node(function, frame)
        arguments = node.child_by_field_name("arguments")
        if arguments is not None:
            argument_frame = _with_conditional(frame) if _call_has_optional_chain(node) else frame
            self._visit_call_arguments(arguments, argument_frame)

    def _visit_call_arguments(self, arguments: Node, frame: _VisitFrame) -> None:
        for child in arguments.named_children:
            self._visit_node(child, frame)

    def _visit_named_children(self, node: Node, frame: _VisitFrame) -> None:
        for child in node.named_children:
            self._visit_node(child, frame)

    def _visit_jsx(self, node: Node, frame: _VisitFrame) -> None:
        for child in node.named_children:
            if child.type == "jsx_expression":
                for inner in child.named_children:
                    self._visit_node(inner, frame)
            else:
                self._visit_node(child, frame)

    def _visit_class(self, node: Node, frame: _VisitFrame) -> None:
        for child in node.named_children:
            if child.type == "decorator":
                deco_frame = replace(
                    frame,
                    evaluation_context=JavascriptCallEvaluationContext.DECORATOR,
                )
                self._visit_node(child, deco_frame)
        heritage = node.child_by_field_name("heritage") or next(
            (child for child in node.named_children if child.type == "class_heritage"),
            None,
        )
        if heritage is not None:
            heritage_frame = replace(
                frame,
                evaluation_context=JavascriptCallEvaluationContext.CLASS_HERITAGE,
            )
            for child in heritage.named_children:
                self._visit_node(child, heritage_frame)
        name = node.child_by_field_name("name")
        scope_name = _scope_name_from_declaration(node, self._source_bytes)
        if name is not None and scope_name is None:
            _, _, line, column, _, _ = _span(name)
        else:
            _, _, line, column, _, _ = _span(node)
        class_scope = JavascriptLexicalScopeRecord(
            JavascriptLexicalScopeKind.CLASS,
            scope_name,
            line,
            column,
        )
        body = node.child_by_field_name("body")
        body_frame = replace(
            frame,
            scope_path=(*frame.scope_path, class_scope),
            evaluation_context=JavascriptCallEvaluationContext.CLASS_BODY,
        )
        if body is not None:
            self._visit_node(body, body_frame)

    def _visit_field_definition(self, node: Node, frame: _VisitFrame) -> None:
        property_node = node.child_by_field_name("property") or (
            node.named_children[0] if node.named_children else None
        )
        if property_node is not None and property_node.type == "computed_property_name":
            key_frame = replace(
                frame,
                evaluation_context=JavascriptCallEvaluationContext.COMPUTED_KEY,
            )
            for child in property_node.named_children:
                self._visit_node(child, key_frame)
        value = node.child_by_field_name("value")
        is_static = any(not child.is_named and child.type == "static" for child in node.children)
        if value is None:
            return
        if is_static:
            value_frame = replace(
                frame,
                evaluation_context=JavascriptCallEvaluationContext.CLASS_STATIC_FIELD,
            )
        else:
            value_frame = replace(
                frame,
                evaluation_context=JavascriptCallEvaluationContext.CLASS_INSTANCE_FIELD,
                execution_disposition=CallExecutionDisposition.DEFERRED,
            )
        self._visit_node(value, value_frame)

    def _visit_function(self, node: Node, frame: _VisitFrame) -> None:
        if node.type == "method_definition":
            property_node = node.child_by_field_name("name") or (
                node.named_children[0] if node.named_children else None
            )
            if property_node is not None and property_node.type == "computed_property_name":
                key_frame = replace(
                    frame,
                    evaluation_context=JavascriptCallEvaluationContext.COMPUTED_KEY,
                )
                for child in property_node.named_children:
                    self._visit_node(child, key_frame)
        scope_kind = _function_scope_kind(node)
        scope_name = _scope_name_from_declaration(node, self._source_bytes)
        if node.type == "method_definition" and scope_name is None:
            property_node = node.child_by_field_name("name")
            if property_node is not None and property_node.type == "property_identifier":
                scope_name = _node_text(property_node, self._source_bytes)
        _, _, line, column, _, _ = _span(node)
        function_scope = JavascriptLexicalScopeRecord(scope_kind, scope_name, line, column)
        parameters = node.child_by_field_name("parameters")
        default_frame = replace(
            frame,
            scope_path=(*frame.scope_path, function_scope),
            evaluation_context=JavascriptCallEvaluationContext.PARAMETER_DEFAULT,
            execution_disposition=CallExecutionDisposition.DEFERRED,
        )
        if parameters is not None:
            self._visit_node(parameters, default_frame)
        body_context = (
            JavascriptCallEvaluationContext.ARROW_BODY
            if node.type == "arrow_function"
            else JavascriptCallEvaluationContext.FUNCTION_BODY
        )
        body_frame = replace(
            frame,
            scope_path=(*frame.scope_path, function_scope),
            evaluation_context=body_context,
            execution_disposition=CallExecutionDisposition.DEFERRED,
        )
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_node(body, body_frame)

    def _visit_parameter_pattern(self, node: Node, frame: _VisitFrame) -> None:
        if node.type in {"assignment_pattern", "object_assignment_pattern"}:
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None:
                self._visit_parameter_pattern(left, frame)
            if right is not None:
                default_frame = replace(
                    frame,
                    evaluation_context=JavascriptCallEvaluationContext.PARAMETER_DEFAULT,
                    conditional_depth=frame.conditional_depth + 1,
                )
                self._visit_node(right, default_frame)
            return
        if node.type in {"object_pattern", "array_pattern"}:
            for child in node.named_children:
                self._visit_parameter_pattern(child, frame)
            return
        if node.type == "rest_pattern":
            for child in node.named_children:
                self._visit_parameter_pattern(child, frame)
            return
        for child in node.named_children:
            self._visit_parameter_pattern(child, frame)

    def _visit_if(self, node: Node, frame: _VisitFrame) -> None:
        condition = node.child_by_field_name("condition")
        if condition is not None:
            self._visit_node(condition, _with_conditional(frame))
        consequence = node.child_by_field_name("consequence")
        if consequence is not None:
            self._visit_node(consequence, _in_conditional_branch(frame))
        alternative = node.child_by_field_name("alternative")
        if alternative is not None:
            self._visit_node(alternative, _in_conditional_branch(frame))

    def _visit_for(self, node: Node, frame: _VisitFrame) -> None:
        for name in ("initializer", "condition", "increment", "left", "right", "value"):
            child = node.child_by_field_name(name)
            if child is not None:
                self._visit_node(child, frame)
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_node(body, _in_conditional_branch(frame))

    def _visit_while(self, node: Node, frame: _VisitFrame) -> None:
        condition = node.child_by_field_name("condition")
        if condition is not None:
            self._visit_node(condition, _with_conditional(frame))
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_node(body, _in_conditional_branch(frame))

    def _visit_do(self, node: Node, frame: _VisitFrame) -> None:
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_node(body, frame)
        condition = node.child_by_field_name("condition")
        if condition is not None:
            self._visit_node(condition, _with_conditional(frame))

    def _visit_switch(self, node: Node, frame: _VisitFrame) -> None:
        value = node.child_by_field_name("value")
        if value is not None:
            self._visit_node(value, frame)
        for child in node.named_children:
            if child.type == "switch_case":
                for case_child in child.named_children:
                    context = (
                        _in_conditional_branch(frame)
                        if case_child.type != "switch_label"
                        else frame
                    )
                    self._visit_node(case_child, context)

    def _visit_try(self, node: Node, frame: _VisitFrame) -> None:
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_node(body, frame)
        for child in node.named_children:
            if child.type == "catch_clause":
                for clause_child in child.named_children:
                    context = (
                        _in_conditional_branch(frame)
                        if clause_child.type == "statement_block"
                        else frame
                    )
                    self._visit_node(clause_child, context)
            elif child.type == "finally_clause":
                for clause_child in child.named_children:
                    self._visit_node(clause_child, frame)

    def _visit_ternary(self, node: Node, frame: _VisitFrame) -> None:
        condition = node.child_by_field_name("condition")
        if condition is not None:
            self._visit_node(condition, _with_conditional(frame))
        consequence = node.child_by_field_name("consequence")
        if consequence is not None:
            self._visit_node(consequence, _in_conditional_branch(frame))
        alternative = node.child_by_field_name("alternative")
        if alternative is not None:
            self._visit_node(alternative, _in_conditional_branch(frame))

    def _visit_binary(self, node: Node, frame: _VisitFrame) -> None:
        operator = None
        for child in node.children:
            if not child.is_named and child.type in _LOGICAL_SHORT_CIRCUIT | {"=", "+=", "-="}:
                operator = child.type
                break
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is not None:
            self._visit_node(left, frame)
        if right is not None:
            right_frame = _with_conditional(frame) if operator in _LOGICAL_SHORT_CIRCUIT else frame
            self._visit_node(right, right_frame)


def _with_conditional(frame: _VisitFrame) -> _VisitFrame:
    return replace(frame, conditional_depth=frame.conditional_depth + 1)


def _in_conditional_branch(frame: _VisitFrame) -> _VisitFrame:
    return replace(frame, conditional_depth=frame.conditional_depth + 1)


def _effective_disposition(
    frame: _VisitFrame,
    node: Node,
) -> CallExecutionDisposition:
    if frame.execution_disposition == CallExecutionDisposition.UNKNOWN:
        return CallExecutionDisposition.UNKNOWN
    if frame.conditional_depth > 0:
        if (
            frame.evaluation_context
            in {
                JavascriptCallEvaluationContext.ARROW_BODY,
                JavascriptCallEvaluationContext.FUNCTION_BODY,
            }
            and frame.execution_disposition is CallExecutionDisposition.DEFERRED
        ):
            return CallExecutionDisposition.DEFERRED
        if _call_has_optional_chain(node):
            return CallExecutionDisposition.CONDITIONAL
        return CallExecutionDisposition.CONDITIONAL
    if _call_has_optional_chain(node):
        return CallExecutionDisposition.CONDITIONAL
    return frame.execution_disposition


@dataclass(frozen=True, slots=True)
class JavascriptStaticCallProviderIdentity:
    available: bool
    parser_version: str | None
    grammar_version: str | None
    grammar_abi: int | None


def javascript_static_call_provider_identity() -> JavascriptStaticCallProviderIdentity:
    import importlib.metadata
    import importlib.util

    available = all(
        importlib.util.find_spec(name) is not None
        for name in ("tree_sitter", "tree_sitter_javascript")
    )
    if not available:
        return JavascriptStaticCallProviderIdentity(False, None, None, None)
    import tree_sitter_javascript
    from tree_sitter import Language

    language = Language(tree_sitter_javascript.language())
    return JavascriptStaticCallProviderIdentity(
        True,
        importlib.metadata.version("tree-sitter"),
        importlib.metadata.version("tree-sitter-javascript"),
        language.abi_version,
    )


def static_call_batch_from_source(
    source_bytes: bytes,
    *,
    max_occurrences: int,
) -> JavascriptCallOccurrenceBatch:
    """Parse captured bytes and collect bounded JavaScript call facts."""

    import tree_sitter_javascript
    from tree_sitter import Language, Parser

    language = Language(tree_sitter_javascript.language())
    root = Parser(language).parse(source_bytes).root_node
    return discover_javascript_call_occurrences(root, source_bytes, max_occurrences=max_occurrences)
