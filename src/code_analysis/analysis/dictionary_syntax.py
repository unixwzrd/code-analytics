"""Lexical dictionary syntax resolution and normalization helpers."""

from __future__ import annotations

import ast
import json
import math

from code_analysis.analysis.dictionary_values import (
    DictionaryDefaultObservation,
    DictionaryKeyKind,
    InternalDefaultKind,
    InternalLockKind,
    InternalSourceSpan,
)


def _span(node: ast.expr | ast.stmt | ast.arg, logical_path: str) -> InternalSourceSpan:
    return InternalSourceSpan(
        logical_path=logical_path,
        start_line=node.lineno,
        start_column=node.col_offset,
        end_line=node.end_lineno or node.lineno,
        end_column=node.end_col_offset or node.col_offset,
    )


def _is_dictionary_annotation(node: ast.expr | None, aliases: dict[str, str]) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Subscript):
        node = node.value
    resolved = _resolved_name(node, aliases)
    return resolved in {
        "dict",
        "builtins.dict",
        "typing.Dict",
        "typing.Mapping",
        "typing.MutableMapping",
        "collections.abc.Mapping",
        "collections.abc.MutableMapping",
    }


def _annotation_root_name(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Subscript):
        node = node.value
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _call_root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _scope_shadowed_names(body: list[ast.stmt]) -> set[str]:
    shadowed: set[str] = set()
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                shadowed.add(node.id)
                continue
            bound_name: str | None = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound_name = node.name
            if isinstance(node, ast.ExceptHandler):
                bound_name = node.name
            if isinstance(node, (ast.MatchAs, ast.MatchStar)):
                bound_name = node.name
            if isinstance(node, ast.MatchMapping):
                bound_name = node.rest
            if bound_name is not None:
                shadowed.add(bound_name)
    return shadowed


def _instance_member_bindings(
    node: ast.ClassDef,
    *,
    module: str,
    class_name: str,
    aliases: dict[str, str],
    module_shadowed: set[str],
) -> tuple[
    dict[str, str],
    dict[str, tuple[str, InternalLockKind]],
    dict[str, tuple[str, ast.expr]],
]:
    dictionaries: dict[str, str] = {}
    locks: dict[str, tuple[str, InternalLockKind]] = {}
    factories: dict[str, tuple[str, ast.expr]] = {}
    if _is_dataclass_class(node, aliases, module_shadowed):
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(
                statement.target, ast.Name
            ):
                continue
            factory = _dataclass_default_factory(
                statement.value,
                aliases=aliases,
                shadowed=module_shadowed,
            )
            if factory is None:
                continue
            owner = f"{module}:{class_name}:self.{statement.target.id}"
            resolved_factory = _resolved_name(factory, aliases)
            annotation_root = _annotation_root_name(statement.annotation)
            if (
                resolved_factory in {"dict", "builtins.dict"}
                and annotation_root not in module_shadowed
                and _is_dictionary_annotation(statement.annotation, aliases)
            ):
                dictionaries[statement.target.id] = owner
                factories[statement.target.id] = (owner, factory)
            lock_kind = _lock_factory_kind(factory, aliases, module_shadowed)
            if lock_kind is not None:
                locks[statement.target.id] = (owner, lock_kind)
    for statement in node.body:
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _is_non_instance_method(statement, aliases):
            continue
        positional = (*statement.args.posonlyargs, *statement.args.args)
        if not positional:
            continue
        instance_name = positional[0].arg
        shadowed = _scope_shadowed_names(statement.body)
        shadowed.update(argument.arg for argument in positional)

        class MemberVisitor(ast.NodeVisitor):
            def __init__(self, bound_instance_name: str, bound_shadowed: set[str]) -> None:
                self.instance_name = bound_instance_name
                self.shadowed = bound_shadowed

            def visit_FunctionDef(self, child: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                return

            def visit_AsyncFunctionDef(self, child: ast.AsyncFunctionDef) -> None:
                self.visit_FunctionDef(child)

            def visit_ClassDef(self, child: ast.ClassDef) -> None:
                return

            def visit_Assign(self, child: ast.Assign) -> None:
                for target in child.targets:
                    self._record(target, child.value, None)
                self.generic_visit(child.value)

            def visit_AnnAssign(self, child: ast.AnnAssign) -> None:
                self._record(child.target, child.value, child.annotation)
                if child.value is not None:
                    self.generic_visit(child.value)

            def _record(
                self,
                target: ast.expr,
                value: ast.expr | None,
                annotation: ast.expr | None,
            ) -> None:
                if (
                    not isinstance(target, ast.Attribute)
                    or not isinstance(target.value, ast.Name)
                    or target.value.id != self.instance_name
                ):
                    return
                owner = f"{module}:{class_name}:self.{target.attr}"
                annotation_root = _annotation_root_name(annotation)
                annotated_dictionary = (
                    annotation_root not in self.shadowed
                    and _is_dictionary_annotation(annotation, aliases)
                )
                if isinstance(value, (ast.Dict, ast.DictComp)) or annotated_dictionary:
                    dictionaries[target.attr] = owner
                if isinstance(value, ast.Call):
                    root = _call_root_name(value.func)
                    kind = (
                        None
                        if root is None or root in self.shadowed
                        else {
                            "threading.Lock": InternalLockKind.THREAD,
                            "threading.RLock": InternalLockKind.THREAD,
                            "multiprocessing.Lock": InternalLockKind.PROCESS,
                            "multiprocessing.RLock": InternalLockKind.PROCESS,
                            "asyncio.Lock": InternalLockKind.ASYNC_TASK,
                        }.get(_resolved_name(value.func, aliases))
                    )
                    if kind is not None:
                        locks[target.attr] = (owner, kind)

        visitor = MemberVisitor(instance_name, shadowed)
        for child in statement.body:
            visitor.visit(child)
    return dictionaries, locks, factories


def _class_body_bindings(
    node: ast.ClassDef,
    *,
    module: str,
    class_name: str,
    aliases: dict[str, str],
    module_shadowed: set[str],
    excluded_names: set[str],
) -> tuple[dict[str, str], dict[str, tuple[str, InternalLockKind]]]:
    dictionaries: dict[str, str] = {}
    locks: dict[str, tuple[str, InternalLockKind]] = {}
    for statement in node.body:
        target: ast.Name | None = None
        value: ast.expr | None = None
        annotation: ast.expr | None = None
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            target = statement.targets[0]
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            target = statement.target
            value = statement.value
            annotation = statement.annotation
        if target is None or target.id in excluded_names:
            continue
        owner = f"{module}:{class_name}:<class>.{target.id}"
        annotation_root = _annotation_root_name(annotation)
        if isinstance(value, (ast.Dict, ast.DictComp)) or (
            annotation_root not in module_shadowed
            and _is_dictionary_annotation(annotation, aliases)
        ):
            dictionaries[target.id] = owner
        if isinstance(value, ast.Call):
            lock_kind = _lock_factory_kind(value.func, aliases, module_shadowed)
            if lock_kind is not None:
                locks[target.id] = (owner, lock_kind)
    return dictionaries, locks


def _is_dataclass_class(
    node: ast.ClassDef,
    aliases: dict[str, str],
    shadowed: set[str],
) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        root = _call_root_name(target)
        if (
            root is not None
            and root not in shadowed
            and _resolved_name(target, aliases) in {"dataclass", "dataclasses.dataclass"}
        ):
            return True
    return False


def _dataclass_default_factory(
    node: ast.expr | None,
    *,
    aliases: dict[str, str],
    shadowed: set[str],
) -> ast.expr | None:
    if not isinstance(node, ast.Call):
        return None
    root = _call_root_name(node.func)
    if root is None or root in shadowed:
        return None
    if _resolved_name(node.func, aliases) not in {"dataclasses.field"}:
        return None
    return next(
        (keyword.value for keyword in node.keywords if keyword.arg == "default_factory"),
        None,
    )


def _lock_factory_kind(
    node: ast.expr,
    aliases: dict[str, str],
    shadowed: set[str],
) -> InternalLockKind | None:
    root = _call_root_name(node)
    if root is None or root in shadowed:
        return None
    return {
        "threading.Lock": InternalLockKind.THREAD,
        "threading.RLock": InternalLockKind.THREAD,
        "multiprocessing.Lock": InternalLockKind.PROCESS,
        "multiprocessing.RLock": InternalLockKind.PROCESS,
        "asyncio.Lock": InternalLockKind.ASYNC_TASK,
    }.get(_resolved_name(node, aliases))


def _is_non_instance_method(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    aliases: dict[str, str],
) -> bool:
    return any(
        _resolved_name(decorator, aliases)
        in {"classmethod", "builtins.classmethod", "staticmethod", "builtins.staticmethod"}
        for decorator in node.decorator_list
    )


def _is_class_method(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    aliases: dict[str, str],
) -> bool:
    return any(
        _resolved_name(decorator, aliases) in {"classmethod", "builtins.classmethod"}
        for decorator in node.decorator_list
    )


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".", 1)[0]] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _resolved_name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _resolved_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _key_fact(node: ast.expr) -> tuple[DictionaryKeyKind, str, str | None]:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return DictionaryKeyKind.LITERAL_STRING, node.value, None
        return (
            DictionaryKeyKind.LITERAL_NON_STRING,
            _canonical_repr(node.value),
            type(node.value).__name__,
        )
    return DictionaryKeyKind.DYNAMIC, ast.unparse(node), None


def _canonical_repr(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def _value_type_repr(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant):
        return type(node.value).__name__
    containers = {
        ast.Dict: "dict",
        ast.DictComp: "dict",
        ast.List: "list",
        ast.ListComp: "list",
        ast.Set: "set",
        ast.SetComp: "set",
        ast.Tuple: "tuple",
    }
    for node_type, name in containers.items():
        if isinstance(node, node_type):
            return name
    return None


def _literal_json(node: ast.expr) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    if not _is_json_value(value):
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _default_observation(
    node: ast.expr | None,
    logical_path: str,
    fallback_node: ast.expr | ast.stmt | ast.arg,
) -> DictionaryDefaultObservation:
    span = _span(node if node is not None else fallback_node, logical_path)
    if node is None:
        return DictionaryDefaultObservation(kind=InternalDefaultKind.ABSENT, span=span)
    if isinstance(node, ast.Constant) and node.value is None:
        return DictionaryDefaultObservation(kind=InternalDefaultKind.EXPLICIT_NULL, span=span)
    literal_json = _literal_json(node)
    if literal_json is not None:
        return DictionaryDefaultObservation(
            kind=InternalDefaultKind.JSON_LITERAL,
            span=span,
            literal_value_json=literal_json,
        )
    try:
        detail = ast.unparse(node)
    except ValueError:
        return DictionaryDefaultObservation(
            kind=InternalDefaultKind.UNRESOLVED,
            span=span,
            detail=type(node).__name__,
        )
    return DictionaryDefaultObservation(
        kind=InternalDefaultKind.EXPRESSION,
        span=span,
        detail=detail,
    )


def _is_json_value(value: object) -> bool:
    if value is None or type(value) in {bool, int, float, str}:
        return not isinstance(value, float) or math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(type(key) is str and _is_json_value(item) for key, item in value.items())
    return False
