"""Static contract declaration inventory.

This module owns syntax-only Phase 2 contract facts.  It never imports or
executes analyzed code and does not construct persisted report models.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, replace
from enum import StrEnum


class DeclarationKind(StrEnum):
    PYDANTIC_MODEL = "pydantic-model"
    DATACLASS = "dataclass"
    TYPED_DICT = "typed-dict"
    NAMED_TUPLE = "named-tuple"
    PROTOCOL = "protocol"
    ENUM = "enum"
    LITERAL = "literal"


class InternalTypeKind(StrEnum):
    SCALAR = "scalar"
    LITERAL = "literal"
    UNION = "union"
    OPTIONAL = "optional"
    CONTAINER = "container"
    MAPPING = "mapping"
    NAMED = "named"
    UNKNOWN = "unknown"


class InternalDefaultKind(StrEnum):
    ABSENT = "absent"
    EXPLICIT_NULL = "explicit-null"
    JSON_LITERAL = "json-literal"
    FACTORY = "factory"
    EXPRESSION = "expression"
    UNRESOLVED = "unresolved"


class InternalRequirednessKind(StrEnum):
    ALWAYS = "always"
    OPTIONAL = "optional"
    UNKNOWN = "unknown"


class ContractUseDirection(StrEnum):
    PRODUCER = "producer"
    CONSUMER = "consumer"


@dataclass(frozen=True, slots=True, order=True)
class InternalSpan:
    start_line: int
    start_column: int
    end_line: int
    end_column: int


@dataclass(frozen=True, slots=True)
class TypeFact:
    kind: InternalTypeKind
    name: str | None = None
    arguments: tuple[TypeFact, ...] = ()
    literal_json: tuple[str, ...] = ()

    def identity(self) -> tuple[object, ...]:
        return (
            self.kind.value,
            self.name,
            tuple(argument.identity() for argument in self.arguments),
            self.literal_json,
        )


@dataclass(frozen=True, slots=True)
class DefaultFact:
    kind: InternalDefaultKind
    canonical_json: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ContractMemberFact:
    name: str
    type_fact: TypeFact
    requiredness: InternalRequirednessKind
    default: DefaultFact
    span: InternalSpan


@dataclass(frozen=True, slots=True)
class ContractDeclarationFact:
    module: str
    qualified_name: str
    kind: DeclarationKind
    base_names: tuple[str, ...]
    members: tuple[ContractMemberFact, ...]
    span: InternalSpan


@dataclass(frozen=True, slots=True)
class ClassLineageFact:
    module: str
    qualified_name: str
    base_names: tuple[str, ...]
    direct_kind: DeclarationKind | None
    span: InternalSpan


@dataclass(frozen=True, slots=True)
class ContractUseFact:
    contract_identity: str
    module: str
    logical_name: str
    direction: ContractUseDirection
    observation: str
    span: InternalSpan
    serialization_format: str | None = None


def discover_class_lineages(
    source: str, module: str, *, is_package: bool = False
) -> tuple[ClassLineageFact, ...]:
    """Return all class identities and lexical base references."""

    tree = ast.parse(source)
    aliases = _import_aliases(tree, module, is_package=is_package)
    lineages: list[ClassLineageFact] = []

    def walk(body: list[ast.stmt], parents: tuple[str, ...]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                qualified_name = ".".join((*parents, node.name))
                lineages.append(
                    ClassLineageFact(
                        module=module,
                        qualified_name=qualified_name,
                        base_names=tuple(_resolved_name(item, aliases) for item in node.bases),
                        direct_kind=_class_kind(node, aliases),
                        span=_span(node),
                    )
                )
                walk(node.body, (*parents, node.name))

    walk(tree.body, ())
    return tuple(sorted(lineages, key=lambda item: (item.module, item.qualified_name, item.span)))


def resolve_contract_kinds(
    lineages: tuple[ClassLineageFact, ...],
) -> tuple[tuple[str, DeclarationKind], ...]:
    """Resolve inherited local contract kinds to a deterministic fixed point."""

    by_fully_qualified = {f"{item.module}.{item.qualified_name}": item for item in lineages}
    resolved = {
        f"{item.module}.{item.qualified_name}": item.direct_kind
        for item in lineages
        if item.direct_kind is not None
    }
    inheritable = {
        DeclarationKind.PYDANTIC_MODEL,
        DeclarationKind.DATACLASS,
        DeclarationKind.TYPED_DICT,
    }
    changed = True
    while changed:
        changed = False
        for fully_qualified, lineage in sorted(by_fully_qualified.items()):
            if fully_qualified in resolved:
                continue
            candidates = {
                resolved[base]
                for raw_base in lineage.base_names
                if (base := _local_base_identity(lineage.module, raw_base, by_fully_qualified))
                in resolved
                and resolved[base] in inheritable
            }
            if len(candidates) == 1:
                resolved[fully_qualified] = candidates.pop()
                changed = True
    return tuple(sorted(resolved.items()))


def merge_inherited_contract_members(
    declarations: tuple[ContractDeclarationFact, ...],
    lineages: tuple[ClassLineageFact, ...],
) -> tuple[ContractDeclarationFact, ...]:
    """Apply resolvable local inheritance while retaining stable member order."""

    declaration_identities = [f"{item.module}.{item.qualified_name}" for item in declarations]
    if len(declaration_identities) != len(set(declaration_identities)):
        return tuple(
            sorted(
                declarations,
                key=lambda item: (
                    item.module,
                    item.qualified_name,
                    item.kind.value,
                    item.span,
                ),
            )
        )
    by_identity = {f"{item.module}.{item.qualified_name}": item for item in declarations}
    known_lineages = {f"{item.module}.{item.qualified_name}": item for item in lineages}
    merged: dict[str, ContractDeclarationFact] = {}

    def resolve(identity: str, visiting: frozenset[str]) -> ContractDeclarationFact:
        if identity in merged:
            return merged[identity]
        declaration = by_identity[identity]
        if identity in visiting:
            return declaration
        members: dict[str, ContractMemberFact] = {}
        for raw_base in reversed(declaration.base_names):
            base_identity = _local_base_identity(declaration.module, raw_base, known_lineages)
            if base_identity is None or base_identity not in by_identity:
                continue
            base = by_identity[base_identity]
            if base.kind is not declaration.kind:
                continue
            members.update(
                {
                    member.name: member
                    for member in resolve(base_identity, visiting | {identity}).members
                }
            )
        members.update({member.name: member for member in declaration.members})
        result = replace(
            declaration,
            members=tuple(sorted(members.values(), key=lambda item: (item.name, item.span))),
        )
        merged[identity] = result
        return result

    for identity in sorted(by_identity):
        resolve(identity, frozenset())
    return tuple(
        sorted(
            merged.values(),
            key=lambda item: (item.module, item.qualified_name, item.kind.value, item.span),
        )
    )


def discover_contract_declarations(
    source: str,
    module: str,
    *,
    is_package: bool = False,
    resolved_kinds: tuple[tuple[str, DeclarationKind], ...] = (),
) -> tuple[ContractDeclarationFact, ...]:
    """Return deterministic syntax-evidenced contract declarations."""

    tree = ast.parse(source)
    aliases = _import_aliases(tree, module, is_package=is_package)
    resolved = dict(resolved_kinds)
    declarations: list[ContractDeclarationFact] = []

    def walk(body: list[ast.stmt], parents: tuple[str, ...]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                qualified_name = ".".join((*parents, node.name))
                base_names = tuple(_resolved_name(item, aliases) for item in node.bases)
                kind = _class_kind(node, aliases) or resolved.get(f"{module}.{qualified_name}")
                if kind is not None:
                    declarations.append(
                        ContractDeclarationFact(
                            module=module,
                            qualified_name=qualified_name,
                            kind=kind,
                            base_names=base_names,
                            members=_class_members(node, kind, aliases),
                            span=_span(node),
                        )
                    )
                walk(node.body, (*parents, node.name))
            elif not parents:
                literal = _literal_alias(node, module, aliases)
                if literal is not None:
                    declarations.append(literal)
                functional = _functional_contract(node, module, aliases)
                if functional is not None:
                    declarations.append(functional)

    walk(tree.body, ())
    return tuple(
        sorted(
            declarations,
            key=lambda item: (item.module, item.qualified_name, item.kind.value, item.span),
        )
    )


def discover_contract_uses(
    source: str,
    module: str,
    contract_kinds: tuple[tuple[str, DeclarationKind], ...],
    *,
    is_package: bool = False,
) -> tuple[ContractUseFact, ...]:
    """Resolve exact contract construction, annotations, and serialization uses."""

    tree = ast.parse(source)
    aliases = _import_aliases(tree, module, is_package=is_package)
    kind_by_contract = dict(contract_kinds)
    known = set(kind_by_contract)
    uses: list[ContractUseFact] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.parents: tuple[str, ...] = ()
            self.scopes: list[dict[str, str]] = [{}]
            self.control_depth = 0

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            previous = self.parents
            previous_control_depth = self.control_depth
            self.parents = (*self.parents, node.name)
            self.scopes.append({})
            self.control_depth = 0
            for decorator in node.decorator_list:
                self.visit(decorator)
            for statement in node.body:
                self.visit(statement)
            self.scopes.pop()
            self.control_depth = previous_control_depth
            self.parents = previous

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            previous = self.parents
            previous_control_depth = self.control_depth
            self.parents = (*self.parents, node.name)
            self.scopes.append({})
            self.control_depth = 0
            qualified = ".".join(self.parents)
            arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            for argument in arguments:
                self._bind_annotation(argument.arg, argument.annotation)
                self._annotation_uses(
                    argument.annotation,
                    f"{module}:{qualified}:parameter:{argument.arg}",
                    ContractUseDirection.CONSUMER,
                    f"contract parameter annotation {argument.arg}",
                )
            if node.args.vararg is not None:
                self._bind_annotation(node.args.vararg.arg, node.args.vararg.annotation)
                self._annotation_uses(
                    node.args.vararg.annotation,
                    f"{module}:{qualified}:parameter:{node.args.vararg.arg}",
                    ContractUseDirection.CONSUMER,
                    f"contract parameter annotation {node.args.vararg.arg}",
                )
            if node.args.kwarg is not None:
                self._bind_annotation(node.args.kwarg.arg, node.args.kwarg.annotation)
                self._annotation_uses(
                    node.args.kwarg.annotation,
                    f"{module}:{qualified}:parameter:{node.args.kwarg.arg}",
                    ContractUseDirection.CONSUMER,
                    f"contract parameter annotation {node.args.kwarg.arg}",
                )
            self._annotation_uses(
                node.returns,
                f"{module}:{qualified}:return",
                ContractUseDirection.PRODUCER,
                "contract return annotation",
            )
            for decorator in node.decorator_list:
                self.visit(decorator)
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    self.visit(default)
            for statement in node.body:
                self.visit(statement)
            self.scopes.pop()
            self.control_depth = previous_control_depth
            self.parents = previous

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if isinstance(node.target, ast.Name):
                self.scopes[-1].pop(node.target.id, None)
                if self.control_depth == 0:
                    self._bind_annotation(node.target.id, node.annotation)
            self.visit(node.annotation)
            if node.value is not None:
                self.visit(node.value)

        def visit_Assign(self, node: ast.Assign) -> None:
            value_contract = self._value_contract(node.value)
            for target in node.targets:
                for name in self._target_names(target):
                    self.scopes[-1].pop(name, None)
            self.visit(node.value)
            if value_contract is not None and self.control_depth == 0:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.scopes[-1][target.id] = value_contract

        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            if isinstance(node.target, ast.Name):
                self.scopes[-1].pop(node.target.id, None)
            self.generic_visit(node)

        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
            value_contract = self._value_contract(node.value)
            if isinstance(node.target, ast.Name):
                self.scopes[-1].pop(node.target.id, None)
            self.visit(node.value)
            if (
                value_contract is not None
                and self.control_depth == 0
                and isinstance(node.target, ast.Name)
            ):
                self.scopes[-1][node.target.id] = value_contract

        def visit_Delete(self, node: ast.Delete) -> None:
            for target in node.targets:
                for name in self._target_names(target):
                    self.scopes[-1].pop(name, None)

        @staticmethod
        def _assigned_names(node: ast.AST) -> set[str]:
            assigned_names = {
                descendant.id
                for descendant in ast.walk(node)
                if isinstance(descendant, ast.Name) and isinstance(descendant.ctx, ast.Store)
            }
            assigned_names.update(
                descendant.name
                for descendant in ast.walk(node)
                if isinstance(descendant, ast.ExceptHandler) and isinstance(descendant.name, str)
            )
            assigned_names.update(
                descendant.name
                for descendant in ast.walk(node)
                if isinstance(descendant, (ast.MatchAs, ast.MatchStar))
                and descendant.name is not None
            )
            assigned_names.update(
                descendant.rest
                for descendant in ast.walk(node)
                if isinstance(descendant, ast.MatchMapping) and descendant.rest is not None
            )
            return assigned_names

        def _visit_control(self, node: ast.AST) -> None:
            assigned_names = self._assigned_names(node)
            for name in assigned_names:
                self.scopes[-1].pop(name, None)
            self.control_depth += 1
            self.generic_visit(node)
            self.control_depth -= 1

        def visit_If(self, node: ast.If) -> None:
            if self.control_depth != 0:
                self._visit_control(node)
                return
            self.visit(node.test)
            assigned_names = self._assigned_names(node)
            for name in assigned_names:
                self.scopes[-1].pop(name, None)
            branch_base = self.scopes[-1].copy()

            self.scopes[-1] = branch_base.copy()
            for statement in node.body:
                self.visit(statement)
            body_scope = self.scopes[-1]

            self.scopes[-1] = branch_base.copy()
            for statement in node.orelse:
                self.visit(statement)
            else_scope = self.scopes[-1]

            joined = branch_base.copy()
            for name in sorted(assigned_names):
                if name in body_scope and body_scope.get(name) == else_scope.get(name):
                    joined[name] = body_scope[name]
            self.scopes[-1] = joined

        visit_For = _visit_control
        visit_AsyncFor = _visit_control
        visit_While = _visit_control
        visit_With = _visit_control
        visit_AsyncWith = _visit_control
        visit_Try = _visit_control
        visit_TryStar = _visit_control
        visit_Match = _visit_control

        def _visit_comprehension(
            self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
        ) -> None:
            generators = node.generators
            shadowed_names = {
                descendant.id
                for generator in generators
                for descendant in ast.walk(generator.target)
                if isinstance(descendant, ast.Name)
            }
            previous = {
                name: self.scopes[-1][name] for name in shadowed_names if name in self.scopes[-1]
            }
            for name in shadowed_names:
                self.scopes[-1].pop(name, None)
            self.control_depth += 1
            self.generic_visit(node)
            self.control_depth -= 1
            for name in shadowed_names:
                self.scopes[-1].pop(name, None)
            self.scopes[-1].update(previous)

        visit_ListComp = _visit_comprehension
        visit_SetComp = _visit_comprehension
        visit_DictComp = _visit_comprehension
        visit_GeneratorExp = _visit_comprehension

        def visit_Call(self, node: ast.Call) -> None:
            resolved = _resolved_name(node.func, aliases)
            contract_identity = _local_contract_reference(module, resolved, known)
            observation = "contract constructor call"
            serialization_format: str | None = None
            if contract_identity is None and isinstance(node.func, ast.Attribute):
                owner = _resolved_name(node.func.value, aliases)
                owner_contract = _local_contract_reference(module, owner, known)
                if (
                    owner_contract is not None
                    and kind_by_contract[owner_contract] is DeclarationKind.PYDANTIC_MODEL
                    and node.func.attr
                    in {"model_validate", "model_validate_json", "parse_obj", "validate"}
                ):
                    contract_identity = owner_contract
                    observation = f"contract {node.func.attr} call"
                    serialization_format = (
                        "json" if node.func.attr == "model_validate_json" else "mapping"
                    )
                else:
                    bound_contract = self._bound_contract(node.func.value)
                    if bound_contract is not None:
                        kind = kind_by_contract[bound_contract]
                        if kind is DeclarationKind.PYDANTIC_MODEL and node.func.attr in {
                            "model_dump",
                            "model_dump_json",
                        }:
                            contract_identity = bound_contract
                            observation = f"contract {node.func.attr} serialization call"
                            serialization_format = (
                                "json" if node.func.attr == "model_dump_json" else "mapping"
                            )
                        elif kind is DeclarationKind.NAMED_TUPLE and node.func.attr == "_asdict":
                            contract_identity = bound_contract
                            observation = "contract _asdict serialization call"
                            serialization_format = "mapping"
            if (
                contract_identity is None
                and resolved == "dataclasses.asdict"
                and node.args
                and (bound_contract := self._bound_contract(node.args[0])) is not None
                and kind_by_contract[bound_contract] is DeclarationKind.DATACLASS
            ):
                contract_identity = bound_contract
                observation = "contract dataclasses.asdict serialization call"
                serialization_format = "mapping"
            if contract_identity is not None:
                parent = ".".join(self.parents) if self.parents else "<module>"
                uses.append(
                    ContractUseFact(
                        contract_identity=contract_identity,
                        module=module,
                        logical_name=(f"{module}:{parent}:call@{node.lineno}:{node.col_offset}"),
                        direction=ContractUseDirection.PRODUCER,
                        observation=observation,
                        span=_span(node),
                        serialization_format=serialization_format,
                    )
                )
                if serialization_format is not None and "serialization" in observation:
                    uses[-1] = replace(uses[-1], direction=ContractUseDirection.CONSUMER)
            self.generic_visit(node)

        def _bind_annotation(self, name: str, annotation: ast.expr | None) -> None:
            if annotation is None:
                return
            references = _annotation_contract_references(annotation, module, aliases, known)
            if len(references) == 1:
                self.scopes[-1][name] = references[0]

        def _bound_contract(self, node: ast.expr) -> str | None:
            if not isinstance(node, ast.Name):
                return None
            for scope in reversed(self.scopes):
                if node.id in scope:
                    return scope[node.id]
            return None

        @staticmethod
        def _target_names(node: ast.expr) -> tuple[str, ...]:
            return tuple(
                sorted(
                    {
                        descendant.id
                        for descendant in ast.walk(node)
                        if isinstance(descendant, ast.Name)
                        and isinstance(descendant.ctx, (ast.Store, ast.Del))
                    }
                )
            )

        def _value_contract(self, node: ast.expr) -> str | None:
            bound = self._bound_contract(node)
            if bound is not None:
                return bound
            if not isinstance(node, ast.Call):
                return None
            resolved = _resolved_name(node.func, aliases)
            contract_identity = _local_contract_reference(module, resolved, known)
            if contract_identity is not None:
                return contract_identity
            if not isinstance(node.func, ast.Attribute):
                return None
            owner = _resolved_name(node.func.value, aliases)
            owner_contract = _local_contract_reference(module, owner, known)
            if (
                owner_contract is not None
                and kind_by_contract[owner_contract] is DeclarationKind.PYDANTIC_MODEL
                and node.func.attr
                in {"model_validate", "model_validate_json", "parse_obj", "validate"}
            ):
                return owner_contract
            return None

        def _annotation_uses(
            self,
            annotation: ast.expr | None,
            logical_name: str,
            direction: ContractUseDirection,
            observation: str,
        ) -> None:
            if annotation is None:
                return
            for contract_identity in _annotation_contract_references(
                annotation, module, aliases, known
            ):
                uses.append(
                    ContractUseFact(
                        contract_identity=contract_identity,
                        module=module,
                        logical_name=f"{logical_name}:{contract_identity}",
                        direction=direction,
                        observation=observation,
                        span=_span(annotation),
                    )
                )

    Visitor().visit(tree)
    identities = {
        (
            item.contract_identity,
            item.module,
            item.logical_name,
            item.direction,
            item.observation,
            item.span,
            item.serialization_format,
        ): item
        for item in uses
    }
    return tuple(identities[key] for key in sorted(identities, key=repr))


def normalize_annotation(node: ast.expr | None, aliases: dict[str, str] | None = None) -> TypeFact:
    """Normalize a Python annotation without resolving or importing it."""

    if node is None:
        return TypeFact(InternalTypeKind.UNKNOWN)
    aliases = aliases or {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return normalize_annotation(ast.parse(node.value, mode="eval").body, aliases)
        except SyntaxError:
            return TypeFact(InternalTypeKind.NAMED, name=node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        arguments = _stable_types(
            (
                *_union_arguments(normalize_annotation(node.left, aliases)),
                *_union_arguments(normalize_annotation(node.right, aliases)),
            )
        )
        non_none = tuple(item for item in arguments if item.name != "None")
        if len(non_none) + 1 == len(arguments) and len(non_none) == 1:
            return TypeFact(InternalTypeKind.OPTIONAL, arguments=non_none)
        return TypeFact(InternalTypeKind.UNION, arguments=arguments)
    if isinstance(node, ast.Subscript):
        base = _resolved_name(node.value, aliases)
        short = base.rsplit(".", 1)[-1]
        slices = _subscript_items(node.slice)
        if short in {"Annotated", "Required", "NotRequired"} and slices:
            return normalize_annotation(slices[0], aliases)
        if short == "Literal":
            canonical = tuple(_canonical_literal(item) for item in slices)
            if any(item is None for item in canonical):
                return TypeFact(InternalTypeKind.UNKNOWN, name=ast.unparse(node))
            values = tuple(sorted({item for item in canonical if item is not None}))
            return TypeFact(InternalTypeKind.LITERAL, literal_json=values)
        arguments = tuple(normalize_annotation(item, aliases) for item in slices)
        if short == "Optional" and len(arguments) == 1:
            return TypeFact(InternalTypeKind.OPTIONAL, arguments=arguments)
        if short == "Union":
            return TypeFact(InternalTypeKind.UNION, arguments=_stable_types(arguments))
        if short in {"dict", "Dict", "Mapping", "MutableMapping"}:
            return TypeFact(InternalTypeKind.MAPPING, name=short, arguments=arguments)
        if short in {
            "list",
            "List",
            "set",
            "Set",
            "frozenset",
            "FrozenSet",
            "tuple",
            "Tuple",
            "Sequence",
            "Iterable",
            "Collection",
        }:
            return TypeFact(InternalTypeKind.CONTAINER, name=short, arguments=arguments)
        return TypeFact(InternalTypeKind.NAMED, name=base, arguments=arguments)
    name = _resolved_name(node, aliases)
    short = name.rsplit(".", 1)[-1]
    if short in {"str", "int", "float", "bool", "bytes", "None", "NoneType"}:
        return TypeFact(InternalTypeKind.SCALAR, name="None" if short == "NoneType" else short)
    if short == "Any":
        return TypeFact(InternalTypeKind.UNKNOWN, name="Any")
    return TypeFact(InternalTypeKind.NAMED, name=name or ast.unparse(node))


def _import_aliases(tree: ast.Module, module: str, *, is_package: bool) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".", 1)[0]] = item.name
        elif isinstance(node, ast.ImportFrom):
            imported_module = _absolute_import_module(
                module,
                node.module or "",
                node.level,
                is_package=is_package,
            )
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = (
                        f"{imported_module}.{item.name}" if imported_module else item.name
                    )
    return aliases


def _absolute_import_module(current: str, imported: str, level: int, *, is_package: bool) -> str:
    if level == 0:
        return imported
    package_parts = current.split(".") if is_package else current.split(".")[:-1]
    trim = level - 1
    if trim > len(package_parts):
        return imported
    base_parts = package_parts[: len(package_parts) - trim]
    if imported:
        base_parts.extend(imported.split("."))
    return ".".join(base_parts)


def _local_base_identity(
    module: str,
    raw_base: str,
    known: dict[str, ClassLineageFact],
) -> str | None:
    if raw_base in known:
        return raw_base
    same_module = f"{module}.{raw_base}"
    if same_module in known:
        return same_module
    return None


def _local_contract_reference(module: str, resolved_name: str, known: set[str]) -> str | None:
    if resolved_name in known:
        return resolved_name
    same_module = f"{module}.{resolved_name}"
    return same_module if same_module in known else None


def _annotation_contract_references(
    node: ast.expr,
    module: str,
    aliases: dict[str, str],
    known: set[str],
) -> tuple[str, ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            parsed = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return ()
        return _annotation_contract_references(parsed, module, aliases, known)
    references: set[str] = set()
    if isinstance(node, (ast.Name, ast.Attribute)):
        resolved = _resolved_name(node, aliases)
        contract = _local_contract_reference(module, resolved, known)
        if contract is not None:
            references.add(contract)
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr):
            references.update(_annotation_contract_references(child, module, aliases, known))
    return tuple(sorted(references))


def _resolved_name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _resolved_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ast.unparse(node)


def _class_kind(node: ast.ClassDef, aliases: dict[str, str]) -> DeclarationKind | None:
    decorators = {
        _resolved_name(item.func if isinstance(item, ast.Call) else item, aliases)
        for item in node.decorator_list
    }
    if "dataclasses.dataclass" in decorators:
        return DeclarationKind.DATACLASS
    bases = {_resolved_name(item, aliases) for item in node.bases}
    if bases & {"pydantic.BaseModel", "pydantic.v1.BaseModel"}:
        return DeclarationKind.PYDANTIC_MODEL
    if bases & {"typing.TypedDict", "typing_extensions.TypedDict"}:
        return DeclarationKind.TYPED_DICT
    if bases & {"typing.NamedTuple", "typing_extensions.NamedTuple"}:
        return DeclarationKind.NAMED_TUPLE
    if bases & {"typing.Protocol", "typing_extensions.Protocol"}:
        return DeclarationKind.PROTOCOL
    if bases & {
        "enum.Enum",
        "enum.IntEnum",
        "enum.StrEnum",
        "enum.Flag",
        "enum.IntFlag",
    }:
        return DeclarationKind.ENUM
    return None


def _class_members(
    node: ast.ClassDef, kind: DeclarationKind, aliases: dict[str, str]
) -> tuple[ContractMemberFact, ...]:
    total = not (
        kind is DeclarationKind.TYPED_DICT
        and any(
            keyword.arg == "total"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is False
            for keyword in node.keywords
        )
    )
    members: list[ContractMemberFact] = []
    for statement in node.body:
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            annotation_name = (
                _resolved_name(statement.annotation.value, aliases).rsplit(".", 1)[-1]
                if isinstance(statement.annotation, ast.Subscript)
                else ""
            )
            default = _default_fact(statement.value, aliases)
            if kind is DeclarationKind.TYPED_DICT:
                if annotation_name == "Required":
                    requiredness = InternalRequirednessKind.ALWAYS
                elif annotation_name == "NotRequired" or not total:
                    requiredness = InternalRequirednessKind.OPTIONAL
                else:
                    requiredness = InternalRequirednessKind.ALWAYS
            else:
                requiredness = (
                    InternalRequirednessKind.ALWAYS
                    if default.kind is InternalDefaultKind.ABSENT
                    else InternalRequirednessKind.OPTIONAL
                )
            members.append(
                ContractMemberFact(
                    name=statement.target.id,
                    type_fact=normalize_annotation(statement.annotation, aliases),
                    requiredness=requiredness,
                    default=default,
                    span=_span(statement),
                )
            )
        elif kind is DeclarationKind.ENUM and isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    members.append(
                        ContractMemberFact(
                            name=target.id,
                            type_fact=normalize_annotation(
                                _constant_type(statement.value), aliases
                            ),
                            requiredness=InternalRequirednessKind.ALWAYS,
                            default=_default_fact(statement.value, aliases),
                            span=_span(statement),
                        )
                    )
    return tuple(sorted(members, key=lambda item: (item.name, item.span)))


def _literal_alias(
    node: ast.stmt, module: str, aliases: dict[str, str]
) -> ContractDeclarationFact | None:
    name: str | None = None
    annotation: ast.expr | None = None
    value: ast.expr | None = None
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        name, annotation, value = node.target.id, node.annotation, node.value
    elif (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        name, value = node.targets[0].id, node.value
    candidate = value
    if isinstance(annotation, ast.Name) and annotation.id == "TypeAlias":
        candidate = value
    if not isinstance(candidate, ast.Subscript):
        return None
    if _resolved_name(candidate.value, aliases) not in {
        "typing.Literal",
        "typing_extensions.Literal",
    }:
        return None
    return ContractDeclarationFact(
        module=module,
        qualified_name=name or "<unknown>",
        kind=DeclarationKind.LITERAL,
        base_names=(),
        members=(),
        span=_span(node),
    )


def _functional_contract(
    node: ast.stmt, module: str, aliases: dict[str, str]
) -> ContractDeclarationFact | None:
    if not (isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call)):
        return None
    target = (
        node.target
        if isinstance(node, ast.AnnAssign)
        else node.targets[0] if len(node.targets) == 1 else None
    )
    if not isinstance(target, ast.Name):
        return None
    function = _resolved_name(node.value.func, aliases)
    if function in {"typing.NamedTuple", "typing_extensions.NamedTuple"}:
        kind = DeclarationKind.NAMED_TUPLE
    elif function in {"typing.TypedDict", "typing_extensions.TypedDict"}:
        kind = DeclarationKind.TYPED_DICT
    else:
        return None
    total = not any(
        keyword.arg == "total"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in node.value.keywords
    )
    members = _functional_members(node.value, kind, aliases, total=total)
    return ContractDeclarationFact(
        module=module,
        qualified_name=target.id,
        kind=kind,
        base_names=(),
        members=members,
        span=_span(node),
    )


def _functional_members(
    call: ast.Call,
    kind: DeclarationKind,
    aliases: dict[str, str],
    *,
    total: bool,
) -> tuple[ContractMemberFact, ...]:
    if len(call.args) < 2:
        return ()
    fields = call.args[1]
    pairs: list[tuple[str, ast.expr, ast.AST]] = []
    if isinstance(fields, (ast.List, ast.Tuple)):
        for item in fields.elts:
            if (
                isinstance(item, (ast.List, ast.Tuple))
                and len(item.elts) == 2
                and isinstance(item.elts[0], ast.Constant)
                and isinstance(item.elts[0].value, str)
            ):
                pairs.append((item.elts[0].value, item.elts[1], item))
    elif isinstance(fields, ast.Dict):
        for key, value in zip(fields.keys, fields.values, strict=True):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                pairs.append((key.value, value, value))
    members = [
        ContractMemberFact(
            name=name,
            type_fact=normalize_annotation(annotation, aliases),
            requiredness=(
                InternalRequirednessKind.ALWAYS
                if kind is not DeclarationKind.TYPED_DICT or total
                else InternalRequirednessKind.OPTIONAL
            ),
            default=DefaultFact(InternalDefaultKind.ABSENT),
            span=_span(span_node),
        )
        for name, annotation, span_node in pairs
    ]
    return tuple(sorted(members, key=lambda item: (item.name, item.span)))


def _default_fact(node: ast.expr | None, aliases: dict[str, str]) -> DefaultFact:
    if node is None or (isinstance(node, ast.Constant) and node.value is Ellipsis):
        return DefaultFact(InternalDefaultKind.ABSENT)
    if isinstance(node, ast.Constant) and node.value is None:
        return DefaultFact(InternalDefaultKind.EXPLICIT_NULL)
    if isinstance(node, ast.Call):
        short = _resolved_name(node.func, aliases).rsplit(".", 1)[-1]
        if short in {"field", "Field"}:
            for keyword in node.keywords:
                if keyword.arg == "default_factory":
                    return DefaultFact(
                        InternalDefaultKind.FACTORY, detail=ast.unparse(keyword.value)
                    )
                if keyword.arg == "default":
                    return _default_fact(keyword.value, aliases)
            if node.args:
                return _default_fact(node.args[0], aliases)
            return DefaultFact(InternalDefaultKind.ABSENT)
        return DefaultFact(InternalDefaultKind.EXPRESSION, detail=ast.unparse(node))
    try:
        value = ast.literal_eval(node)
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return DefaultFact(InternalDefaultKind.EXPRESSION, detail=ast.unparse(node))
    return DefaultFact(InternalDefaultKind.JSON_LITERAL, canonical_json=canonical)


def _constant_type(node: ast.expr) -> ast.expr | None:
    if isinstance(node, ast.Constant):
        value = node.value
        if value is None:
            return ast.Name(id="None")
        return ast.Name(id=type(value).__name__)
    return None


def _canonical_literal(node: ast.expr) -> str | None:
    try:
        value = ast.literal_eval(node)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None


def _subscript_items(node: ast.expr) -> tuple[ast.expr, ...]:
    return tuple(node.elts) if isinstance(node, ast.Tuple) else (node,)


def _union_arguments(value: TypeFact) -> tuple[TypeFact, ...]:
    return value.arguments if value.kind is InternalTypeKind.UNION else (value,)


def _stable_types(values: tuple[TypeFact, ...]) -> tuple[TypeFact, ...]:
    unique = {value.identity(): value for value in values}
    return tuple(unique[key] for key in sorted(unique, key=repr))


def _span(node: ast.AST) -> InternalSpan:
    return InternalSpan(
        start_line=node.lineno,
        start_column=node.col_offset,
        end_line=node.end_lineno or node.lineno,
        end_column=node.end_col_offset or node.col_offset,
    )
