"""Typed dimension extractors for transparent function-feature profiles."""

from __future__ import annotations

import ast
import builtins
from collections import Counter
from dataclasses import dataclass

from code_analysis.analysis.clones import CloneSource
from code_analysis.report_models import CommonalityDimensionKind


@dataclass(frozen=True, slots=True)
class FunctionFeatures:
    logical_name: str
    module: str
    logical_path: str
    source_sha256: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    callees: tuple[str, ...]
    signature_shapes: tuple[str, ...]
    attributes_globals: tuple[str, ...]
    exceptions: tuple[str, ...]
    side_effects: tuple[str, ...]
    control_flow: tuple[str, ...]
    dictionary_schema_operations: tuple[str, ...]
    contract_ids: tuple[str, ...]
    weak_semantic_signals: tuple[str, ...]
    unresolved_call_count: int

    def dimensions(self) -> tuple[tuple[CommonalityDimensionKind, tuple[str, ...]], ...]:
        return (
            (CommonalityDimensionKind.CALLEES, self.callees),
            (CommonalityDimensionKind.SIGNATURES, self.signature_shapes),
            (CommonalityDimensionKind.ATTRIBUTES_GLOBALS, self.attributes_globals),
            (CommonalityDimensionKind.EXCEPTIONS, self.exceptions),
            (CommonalityDimensionKind.SIDE_EFFECTS, self.side_effects),
            (CommonalityDimensionKind.CONTROL_FLOW, self.control_flow),
            (CommonalityDimensionKind.DICTIONARY_SCHEMA, self.dictionary_schema_operations),
            (CommonalityDimensionKind.CONTRACTS, self.contract_ids),
        )

    def ownership_dimensions(
        self,
    ) -> tuple[tuple[CommonalityDimensionKind, tuple[str, ...]], ...]:
        return tuple(
            item
            for item in self.dimensions()
            if item[0]
            not in {CommonalityDimensionKind.SIGNATURES, CommonalityDimensionKind.CONTROL_FLOW}
        )


@dataclass(frozen=True, slots=True)
class CallScope:
    parent: str | None
    bindings: frozenset[str]
    callables: dict[str, str]


@dataclass(frozen=True, slots=True)
class FeatureExtractionContext:
    source: CloneSource
    tree: ast.Module
    call_scopes: dict[str, CallScope]
    import_aliases: dict[str, str]
    possible_builtin_shadows: frozenset[str]
    receiver_calls: dict[str, dict[str, str]]


def _binding_names(node: ast.AST) -> tuple[str, ...]:
    """Bindings whose names are not necessarily represented by Name(Store)."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return (node.id,)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return (node.name,)
    if isinstance(node, ast.ExceptHandler):
        return (node.name,) if node.name else ()
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return tuple(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
    if isinstance(node, (ast.MatchAs, ast.MatchStar)):
        return (node.name,) if node.name else ()
    if isinstance(node, ast.MatchMapping):
        return (node.rest,) if node.rest else ()
    return ()


def _possible_builtin_shadows(tree: ast.Module) -> frozenset[str]:
    # A binder anywhere in the module may belong to an enclosing scope. Keeping
    # such names is conservative: do not erase a captured or shadowed value just
    # because it shares a spelling with a builtin. Star imports remain unknown.
    names: set[str] = set()
    for node in ast.walk(tree):
        names.update(_binding_names(node))
        if isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return frozenset(names)


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix is not None else None
    return None


def _annotation_shape(node: ast.AST | None) -> str:
    if node is None:
        return "unannotated"
    if isinstance(node, ast.Name):
        return f"named:{node.id}"
    if isinstance(node, ast.Subscript):
        base = _dotted(node.value) or type(node.value).__name__
        return f"generic:{base}"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return "union"
    return type(node).__name__.lower()


def _function_nodes(tree: ast.Module, module: str) -> tuple[tuple[str, ast.AST], ...]:
    found: list[tuple[str, ast.AST]] = []

    def walk(statements: list[ast.stmt], parents: tuple[str, ...]) -> None:
        for statement in statements:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = ".".join((*parents, statement.name))
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.append((f"{module}:{qualified}", statement))
                walk(statement.body, (*parents, statement.name))

    walk(tree.body, ())
    return tuple(found)


def _walk_function(
    node: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[ast.AST, ...]:
    found: list[ast.AST] = []
    stack: list[ast.AST] = list(reversed(node.body))
    while stack:
        current = stack.pop()
        found.append(current)
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(current))))
    return tuple(found)


def _import_aliases(source: CloneSource, tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                root = alias.name.split(".", 1)[0]
                aliases[alias.asname or root] = alias.name if alias.asname else root
        elif isinstance(statement, ast.ImportFrom) and statement.module:
            if statement.level:
                package = source.module.split(".")[:-1]
                trim = statement.level - 1
                base = package[: len(package) - trim] if trim <= len(package) else []
                imported_module = ".".join((*base, statement.module))
            else:
                imported_module = statement.module
            for alias in statement.names:
                aliases[alias.asname or alias.name] = f"{imported_module}.{alias.name}"
    return aliases


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    result = {item.arg for item in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
    if node.args.vararg:
        result.add(node.args.vararg.arg)
    if node.args.kwarg:
        result.add(node.args.kwarg.arg)
    return result


def _call_scopes(source: CloneSource, tree: ast.Module) -> dict[str, CallScope]:
    """Prove direct lexical bindings; dynamic/repeated binders stay unresolved."""
    module_scope = f"{source.module}:"
    nodes = dict(_function_nodes(tree, source.module))
    nodes[module_scope] = tree
    scopes: dict[str, CallScope] = {}
    for name, node in nodes.items():
        assert isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef))
        counts: dict[str, int] = {}
        for child in _walk_function(node):
            bindings = (
                tuple(child.names)
                if isinstance(child, (ast.Global, ast.Nonlocal))
                else _binding_names(child)
            )
            for binding in bindings:
                counts[binding] = counts.get(binding, 0) + 1
        if not isinstance(node, ast.Module):
            for parameter in _parameters(node):
                counts[parameter] = counts.get(parameter, 0) + 1
        calls = _import_aliases(source, tree) if isinstance(node, ast.Module) else {}
        for statement in node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                separator = "" if name == module_scope else "."
                calls[statement.name] = f"{name}{separator}{statement.name}"
        namespace_imports: dict[str, int] = {}
        if isinstance(node, ast.Module):
            for statement in node.body:
                if isinstance(statement, ast.Import):
                    for alias in statement.names:
                        if alias.asname is None:
                            root = alias.name.split(".", 1)[0]
                            namespace_imports[root] = namespace_imports.get(root, 0) + 1
        calls = {
            key: value
            for key, value in calls.items()
            if counts.get(key) == 1
            or (key == value and counts.get(key) == namespace_imports.get(key))
        }
        parent = None
        if name != module_scope:
            parent = name.rsplit(".", 1)[0] if "." in name.split(":", 1)[1] else module_scope
            while parent not in nodes:
                parent = (
                    parent.rsplit(".", 1)[0] if "." in parent.split(":", 1)[1] else module_scope
                )
        scopes[name] = CallScope(parent, frozenset(counts), calls)
    return scopes


def _resolve_lexical_name(name: str, scope: str, context: FeatureExtractionContext) -> str | None:
    current: str | None = scope
    while current is not None:
        bindings = context.call_scopes[current]
        if name in bindings.bindings:
            return bindings.callables.get(name)
        current = bindings.parent
    return None


def _class_receiver_calls(node: ast.ClassDef, owner: str) -> dict[str, dict[str, str]]:
    """Resolve only direct, unrebound ordinary method syntax in the same class."""
    methods = {
        child.name: child
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and not child.decorator_list
    }
    bindings = Counter(
        name
        for child in _walk_function(ast.Module(body=node.body, type_ignores=[]))
        for name in _binding_names(child)
    )
    # An assigned/deleted attribute can replace a method even if the definition
    # itself is unique. Inherited methods and decorated descriptors are unknown.
    replaced = {
        child.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute) and isinstance(child.ctx, (ast.Store, ast.Del))
    }
    targets = {
        name: f"{owner}.{name}" for name in methods if bindings[name] == 1 and name not in replaced
    }
    result: dict[str, dict[str, str]] = {}
    for name, method in methods.items():
        parameters = (*method.args.posonlyargs, *method.args.args)
        if not parameters or bindings[name] != 1:
            continue
        receiver = parameters[0].arg
        if any(receiver in _binding_names(child) for child in _walk_function(method)):
            continue
        result[f"{owner}.{name}"] = {
            f"{receiver}.{target}": identity for target, identity in targets.items()
        }
    return result


def _receiver_calls(tree: ast.Module, module: str) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}

    def walk(statements: list[ast.stmt], parents: tuple[str, ...]) -> None:
        for statement in statements:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = (*parents, statement.name)
                if isinstance(statement, ast.ClassDef):
                    result.update(
                        _class_receiver_calls(statement, f"{module}:{'.'.join(qualified)}")
                    )
                walk(statement.body, qualified)

    walk(tree.body, ())
    return result


def _resolve_call(
    function: ast.expr,
    logical_name: str,
    context: FeatureExtractionContext,
) -> str | None:
    target = _dotted(function)
    if isinstance(function, ast.Name):
        return _resolve_lexical_name(function.id, logical_name, context)
    if target:
        head, _, tail = target.partition(".")
        imported = context.import_aliases.get(head)
        if imported and _resolve_lexical_name(head, logical_name, context) == imported:
            return f"{imported}.{tail}" if tail else imported
        return context.receiver_calls.get(logical_name, {}).get(target)
    return None


def _extract_dimensions(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    logical_name: str,
    context: FeatureExtractionContext,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    int,
]:
    locals_ = _parameters(node)
    children = _walk_function(node)
    call_target_ids = {
        id(target)
        for child in children
        if isinstance(child, ast.Call)
        for target in ast.walk(child.func)
    }
    for child in children:
        locals_.update(_binding_names(child))
    for child in children:
        if isinstance(child, (ast.Global, ast.Nonlocal)):
            locals_.difference_update(child.names)
    builtin_names = (
        set(vars(builtins)) - context.possible_builtin_shadows
        if "*" not in context.possible_builtin_shadows
        else set()
    )
    callees: set[str] = set()
    attributes: set[str] = set()
    exceptions: set[str] = set()
    effects: set[str] = set()
    controls: set[str] = set()
    dictionary_ops: set[str] = set()
    unresolved = 0
    for child in children:
        if isinstance(child, ast.Call):
            target = _dotted(child.func)
            resolved = _resolve_call(child.func, logical_name, context)
            if resolved is None:
                unresolved += 1
            else:
                callees.add(resolved)
            if target == "print" and "print" in builtin_names:
                effects.add("output:print")
            elif resolved in {"logging.info", "logging.warning", "logging.error"}:
                effects.add(f"output:{resolved}")
            if isinstance(child.func, ast.Attribute) and child.func.attr in {
                "get",
                "setdefault",
                "update",
                "pop",
                "clear",
                "items",
                "keys",
                "values",
            }:
                dictionary_ops.add(f"mapping-call:{child.func.attr}")
        elif isinstance(child, ast.Subscript):
            dictionary_ops.add("subscript")
        elif isinstance(child, ast.Raise):
            exception = (
                _dotted(child.exc.func)
                if isinstance(child.exc, ast.Call)
                else _dotted(child.exc) if child.exc else None
            )
            exceptions.add(exception or "reraised-or-dynamic")
        elif (
            isinstance(child, ast.Attribute)
            and id(child) not in call_target_ids
            and (dotted := _dotted(child))
        ):
            attributes.add(dotted)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            if (
                id(child) not in call_target_ids
                and child.id not in locals_
                and child.id not in builtin_names
                and child.id not in {"True", "False", "None"}
            ):
                attributes.add(child.id)
        control = {
            ast.If: "if",
            ast.For: "for",
            ast.AsyncFor: "for",
            ast.While: "while",
            ast.Try: "try",
            ast.Match: "match",
            ast.With: "with",
            ast.AsyncWith: "with",
        }
        for kind, label in control.items():
            if isinstance(child, kind):
                controls.add(label)
                break
    return (
        tuple(sorted(callees)),
        tuple(sorted(attributes)),
        tuple(sorted(exceptions)),
        tuple(sorted(effects)),
        tuple(sorted(controls)),
        tuple(sorted(dictionary_ops)),
        unresolved,
    )


# Call targets are represented by the resolved-callee dimension. Excluding those
# same syntax nodes from attributes/globals prevents one ownership fact from
# masquerading as two independent dimensions.


def _extract_function(
    logical_name: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    context: FeatureExtractionContext,
) -> FunctionFeatures:
    parameters = _parameters(node)
    callees, attributes, exceptions, effects, controls, dictionary_ops, unresolved = (
        _extract_dimensions(node, logical_name, context)
    )
    signature = tuple(
        sorted(
            {
                *(
                    f"parameter:{_annotation_shape(item.annotation)}"
                    for item in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
                ),
                f"return:{_annotation_shape(node.returns)}",
                f"arity:{len(parameters)}",
            }
        )
    )
    docstring = ast.get_docstring(node, clean=True)
    weak = tuple(sorted({f"name:{node.name}", *((f"doc:{docstring}",) if docstring else ())}))
    source = context.source
    return FunctionFeatures(
        logical_name=logical_name,
        module=source.module,
        logical_path=source.logical_path,
        source_sha256=source.source_sha256,
        start_line=node.lineno,
        start_column=node.col_offset,
        end_line=node.end_lineno or node.lineno,
        end_column=node.end_col_offset or node.col_offset,
        callees=callees,
        signature_shapes=signature,
        attributes_globals=attributes,
        exceptions=exceptions,
        side_effects=effects,
        control_flow=controls,
        dictionary_schema_operations=dictionary_ops,
        contract_ids=(),
        weak_semantic_signals=weak,
        unresolved_call_count=unresolved,
    )


def discover_function_features(sources: tuple[CloneSource, ...]) -> tuple[FunctionFeatures, ...]:
    """Coordinate typed feature extraction without importing analyzed code."""
    results: list[FunctionFeatures] = []
    for source in sorted(sources, key=lambda item: (item.module, item.logical_path)):
        tree = ast.parse(source.source, filename=source.logical_path)
        context = FeatureExtractionContext(
            source,
            tree,
            _call_scopes(source, tree),
            _import_aliases(source, tree),
            _possible_builtin_shadows(tree),
            _receiver_calls(tree, source.module),
        )
        for logical_name, raw_node in _function_nodes(tree, source.module):
            assert isinstance(raw_node, (ast.FunctionDef, ast.AsyncFunctionDef))
            results.append(_extract_function(logical_name, raw_node, context))
    return tuple(sorted(results, key=lambda item: item.logical_name))
