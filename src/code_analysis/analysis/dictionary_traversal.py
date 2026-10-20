"""Typed AST traversal for dictionary evidence collection."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from code_analysis.analysis.dictionary_syntax import (
    _annotation_root_name,
    _call_root_name,
    _class_body_bindings,
    _default_observation,
    _import_aliases,
    _instance_member_bindings,
    _is_class_method,
    _is_dictionary_annotation,
    _is_non_instance_method,
    _key_fact,
    _literal_json,
    _resolved_name,
    _scope_shadowed_names,
    _span,
    _value_type_repr,
)
from code_analysis.analysis.dictionary_values import (
    DictionaryAliasObservation,
    DictionaryConversionObservation,
    DictionaryDefaultRole,
    DictionaryFieldDefaultObservation,
    DictionaryKeyKind,
    DictionaryMutationGuardObservation,
    DictionaryMutationLockOrderObservation,
    DictionaryOperationKind,
    DictionaryOperationObservation,
    DictionaryPredicate,
    DictionaryStructuralValidationObservation,
    InternalConversionKind,
    InternalLiteralValidationRelation,
    InternalLockKind,
    InternalStructuralValidationKind,
    MutationTiming,
)


@dataclass(frozen=True, slots=True)
class DictionaryTraversalContext:
    tree: ast.Module
    logical_path: str
    module: str
    aliases: dict[str, str]
    parents_by_id: dict[int, ast.AST]


@dataclass(slots=True)
class DictionaryFactAccumulator:
    owners: set[str] = field(default_factory=set)
    aliases: list[DictionaryAliasObservation] = field(default_factory=list)
    operations: list[DictionaryOperationObservation] = field(default_factory=list)
    defaults: list[DictionaryFieldDefaultObservation] = field(default_factory=list)
    conversions: list[DictionaryConversionObservation] = field(default_factory=list)
    mutation_guards: list[DictionaryMutationGuardObservation] = field(default_factory=list)
    lock_orders: list[DictionaryMutationLockOrderObservation] = field(default_factory=list)
    structural_validations: list[DictionaryStructuralValidationObservation] = field(
        default_factory=list
    )


@dataclass(slots=True)
class DictionaryTraversalState:
    parents: tuple[str, ...] = ()
    scopes: list[dict[str, str]] = field(default_factory=lambda: [{}])
    shadowed_names: list[set[str]] = field(default_factory=list)
    owner_overrides: dict[int, str] = field(default_factory=dict)
    write_values: dict[int, ast.expr] = field(default_factory=dict)
    variant_context: tuple[str, ...] = ()
    lock_scopes: list[dict[str, tuple[str, InternalLockKind]]] = field(default_factory=lambda: [{}])
    active_locks: tuple[tuple[str, InternalLockKind], ...] = ()
    instance_name: str | None = None
    instance_dictionary_scopes: list[dict[str, str]] = field(default_factory=list)
    instance_lock_scopes: list[dict[str, tuple[str, InternalLockKind]]] = field(
        default_factory=list
    )
    instance_class_names: list[str] = field(default_factory=list)
    instance_factory_scopes: list[set[str]] = field(default_factory=list)
    owned_scope_kinds: tuple[str, ...] = ()
    scope_types: list[str] = field(default_factory=lambda: ["module"])
    class_receiver_name: str | None = None
    class_dictionary_scopes: list[dict[str, str]] = field(default_factory=list)
    class_lock_scopes: list[dict[str, tuple[str, InternalLockKind]]] = field(default_factory=list)
    validation_branch_continues: bool = False


class DictionaryFactVisitor(ast.NodeVisitor):
    def __init__(
        self, context: DictionaryTraversalContext, facts: DictionaryFactAccumulator
    ) -> None:
        self.context = context
        self.facts = facts
        self.state = DictionaryTraversalState(
            shadowed_names=[_scope_shadowed_names(context.tree.body)]
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        class_name = ".".join((*self.state.parents, node.name))
        dictionaries, locks, factories = _instance_member_bindings(
            node,
            module=self.context.module,
            class_name=class_name,
            aliases=self.context.aliases,
            module_shadowed=self.state.shadowed_names[0],
        )
        class_dictionaries, class_locks = _class_body_bindings(
            node,
            module=self.context.module,
            class_name=class_name,
            aliases=self.context.aliases,
            module_shadowed=self.state.shadowed_names[0],
            excluded_names=set(factories),
        )
        for owner, factory_node in factories.values():
            self.facts.owners.add(owner)
            self.facts.operations.append(
                DictionaryOperationObservation(
                    owner=owner,
                    kind=DictionaryOperationKind.FACTORY,
                    span=_span(factory_node, self.context.logical_path),
                    value_type_repr="dict",
                )
            )
        self.state.instance_dictionary_scopes.append(dictionaries)
        self.state.instance_lock_scopes.append(locks)
        self.state.instance_class_names.append(class_name)
        self.state.instance_factory_scopes.append(set(factories))
        self.state.class_dictionary_scopes.append(class_dictionaries)
        self.state.class_lock_scopes.append(class_locks)
        self._visit_owned_body(node)
        self.state.class_lock_scopes.pop()
        self.state.class_dictionary_scopes.pop()
        self.state.instance_factory_scopes.pop()
        self.state.instance_class_names.pop()
        self.state.instance_lock_scopes.pop()
        self.state.instance_dictionary_scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._visit_owned_body(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _visit_owned_body(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        previous = self.state.parents
        previous_active_locks = self.state.active_locks
        previous_instance_name = self.state.instance_name
        previous_scope_kinds = self.state.owned_scope_kinds
        previous_class_receiver_name = self.state.class_receiver_name
        scope_type = "class" if isinstance(node, ast.ClassDef) else "function"
        self.state.parents = (*self.state.parents, node.name)
        self.state.owned_scope_kinds = (
            *self.state.owned_scope_kinds,
            scope_type,
        )
        self.state.active_locks = ()
        self.state.scopes.append({})
        self.state.lock_scopes.append({})
        self.state.scope_types.append(scope_type)
        self.state.shadowed_names.append(_scope_shadowed_names(node.body))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional = (*node.args.posonlyargs, *node.args.args)
            self.state.instance_name = (
                positional[0].arg
                if self.state.instance_class_names
                and ".".join(previous) == self.state.instance_class_names[-1]
                and positional
                and not _is_non_instance_method(node, self.context.aliases)
                else None
            )
            self.state.class_receiver_name = (
                positional[0].arg
                if self.state.instance_class_names
                and ".".join(previous) == self.state.instance_class_names[-1]
                and positional
                and _is_class_method(node, self.context.aliases)
                else None
            )
            self.state.shadowed_names[-1].update(
                argument.arg
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
            )
            if node.args.vararg:
                self.state.shadowed_names[-1].add(node.args.vararg.arg)
            if node.args.kwarg:
                self.state.shadowed_names[-1].add(node.args.kwarg.arg)
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ):
                if self._is_dictionary_annotation(argument.annotation):
                    owner = self._named_owner(argument.arg)
                    self.state.scopes[-1][argument.arg] = owner
                    self.facts.owners.add(owner)
            if node.args.vararg and self._is_dictionary_annotation(node.args.vararg.annotation):
                owner = self._named_owner(node.args.vararg.arg)
                self.state.scopes[-1][node.args.vararg.arg] = owner
                self.facts.owners.add(owner)
            if node.args.kwarg and self._is_dictionary_annotation(node.args.kwarg.annotation):
                owner = self._named_owner(node.args.kwarg.arg)
                self.state.scopes[-1][node.args.kwarg.arg] = owner
                self.facts.owners.add(owner)
        self.generic_visit(node)
        self.state.shadowed_names.pop()
        self.state.scope_types.pop()
        self.state.lock_scopes.pop()
        self.state.scopes.pop()
        self.state.parents = previous
        self.state.active_locks = previous_active_locks
        self.state.instance_name = previous_instance_name
        self.state.owned_scope_kinds = previous_scope_kinds
        self.state.class_receiver_name = previous_class_receiver_name

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Subscript):
                self.state.write_values[id(target)] = node.value
        names = tuple(target.id for target in node.targets if isinstance(target, ast.Name))
        if (
            len(node.targets) == 1
            and isinstance(node.value, (ast.Dict, ast.DictComp))
            and (owner := self._instance_dictionary_owner(node.targets[0])) is not None
        ):
            self.state.owner_overrides[id(node.value)] = owner
            self.visit(node.value)
            self.facts.owners.add(owner)
            return
        value_owner = self._bound_owner(node.value)
        value_lock = self._bound_lock(node.value)
        for name in names:
            self.state.scopes[-1].pop(name, None)
            self.state.lock_scopes[-1].pop(name, None)
        if (
            len(node.targets) == 1
            and len(names) == 1
            and (lock_kind := self._lock_constructor(node.value)) is not None
        ):
            self.state.lock_scopes[-1][names[0]] = (self._named_owner(names[0]), lock_kind)
            self.generic_visit(node)
            return
        if len(node.targets) == 1 and len(names) == 1 and value_lock is not None:
            self.state.lock_scopes[-1][names[0]] = value_lock
            self.visit(node.value)
            return
        if (
            len(node.targets) == 1
            and len(names) == 1
            and isinstance(node.value, (ast.Dict, ast.DictComp))
        ):
            owner = self._named_owner(names[0])
            self.state.owner_overrides[id(node.value)] = owner
            self.visit(node.value)
            self.state.scopes[-1][names[0]] = owner
            self.facts.owners.add(owner)
            return
        if len(node.targets) == 1 and len(names) == 1 and value_owner is not None:
            alias_identity = self._named_owner(names[0])
            self.state.scopes[-1][names[0]] = value_owner
            if alias_identity != value_owner:
                self.facts.aliases.append(
                    DictionaryAliasObservation(
                        source_owner=value_owner,
                        alias_identity=alias_identity,
                        span=_span(node, self.context.logical_path),
                        variant=self._variant_name(),
                    )
                )
            self.visit(node.value)
            return
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Subscript) and node.value is not None:
            self.state.write_values[id(node.target)] = node.value
        if (
            isinstance(node.target, ast.Name)
            and self.state.instance_name is None
            and self.state.instance_class_names
            and ".".join(self.state.parents) == self.state.instance_class_names[-1]
            and node.target.id in self.state.instance_factory_scopes[-1]
        ):
            self.visit(node.annotation)
            if node.value is not None:
                self.visit(node.value)
            return
        if (
            node.value is not None
            and isinstance(node.value, (ast.Dict, ast.DictComp))
            and (owner := self._instance_dictionary_owner(node.target)) is not None
        ):
            self.state.owner_overrides[id(node.value)] = owner
            self.visit(node.annotation)
            self.visit(node.value)
            self.facts.owners.add(owner)
            return
        if isinstance(node.target, ast.Name):
            value_lock = self._bound_lock(node.value) if node.value is not None else None
            self.state.scopes[-1].pop(node.target.id, None)
            self.state.lock_scopes[-1].pop(node.target.id, None)
            if (
                node.value is not None
                and (lock_kind := self._lock_constructor(node.value)) is not None
            ):
                self.state.lock_scopes[-1][node.target.id] = (
                    self._named_owner(node.target.id),
                    lock_kind,
                )
                self.generic_visit(node)
                return
            if value_lock is not None:
                self.state.lock_scopes[-1][node.target.id] = value_lock
                self.generic_visit(node)
                return
            if isinstance(node.value, (ast.Dict, ast.DictComp)):
                owner = self._named_owner(node.target.id)
                self.state.owner_overrides[id(node.value)] = owner
                self.visit(node.annotation)
                self.visit(node.value)
                self.state.scopes[-1][node.target.id] = owner
                self.facts.owners.add(owner)
                return
            if self._is_dictionary_annotation(node.annotation):
                owner = self._named_owner(node.target.id)
                self.state.scopes[-1][node.target.id] = owner
                self.facts.owners.add(owner)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        owner = self._owner(node)
        if not node.keys:
            self.facts.operations.append(
                DictionaryOperationObservation(
                    owner=owner,
                    kind=DictionaryOperationKind.LITERAL,
                    span=_span(node, self.context.logical_path),
                    variant=self._variant_name(),
                )
            )
        for key, value in zip(node.keys, node.values, strict=True):
            if key is None:
                self.facts.operations.append(
                    DictionaryOperationObservation(
                        owner=owner,
                        kind=DictionaryOperationKind.UNPACK,
                        span=_span(value, self.context.logical_path),
                        variant=self._variant_name(),
                    )
                )
                continue
            key_kind, key_repr, key_type_repr = _key_fact(key)
            self.facts.operations.append(
                DictionaryOperationObservation(
                    owner=owner,
                    kind=DictionaryOperationKind.LITERAL,
                    span=_span(key, self.context.logical_path),
                    variant=self._variant_name(),
                    key_kind=key_kind,
                    key_repr=key_repr,
                    key_type_repr=key_type_repr,
                    value_type_repr=_value_type_repr(value),
                    literal_value_json=_literal_json(value),
                )
            )
        self.generic_visit(node)

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        guards: list[tuple[str, InternalLockKind]] = []
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
            guard = self._bound_lock(item.context_expr)
            if guard is not None:
                guards.append(guard)
        previous = self.state.active_locks
        self.state.active_locks = (*previous, *guards)
        for statement in node.body:
            self.visit(statement)
        self.state.active_locks = previous

    visit_AsyncWith = visit_With

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self.facts.operations.append(
            DictionaryOperationObservation(
                owner=self._owner(node),
                kind=DictionaryOperationKind.COMPREHENSION,
                span=_span(node, self.context.logical_path),
                variant=self._variant_name(),
                key_kind=DictionaryKeyKind.DYNAMIC,
                key_repr=ast.unparse(node.key),
                value_type_repr=_value_type_repr(node.value),
            )
        )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        owner = self._bound_owner(node.value)
        if owner is not None:
            if isinstance(node.ctx, ast.Store):
                kind = DictionaryOperationKind.WRITE
                timing = MutationTiming.UNKNOWN
            elif isinstance(node.ctx, ast.Del):
                kind = DictionaryOperationKind.DELETE
                timing = MutationTiming.UNKNOWN
            else:
                kind = DictionaryOperationKind.READ
                timing = MutationTiming.NOT_MUTATION
            key_kind, key_repr, key_type_repr = _key_fact(node.slice)
            write_value = self.state.write_values.get(id(node))
            operation_observation = DictionaryOperationObservation(
                owner=owner,
                kind=kind,
                span=_span(node, self.context.logical_path),
                variant=self._variant_name(),
                key_kind=key_kind,
                key_repr=key_repr,
                key_type_repr=key_type_repr,
                value_type_repr=(
                    _value_type_repr(write_value) if write_value is not None else None
                ),
                literal_value_json=(
                    _literal_json(write_value) if write_value is not None else None
                ),
                mutation_timing=timing,
            )
            self.facts.operations.append(operation_observation)
            self._record_mutation_guards(operation_observation)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        qualified_name = _resolved_name(node.func, self.context.aliases)
        root_name = _call_root_name(node.func)
        self._record_value_type_call(node)
        self._record_serialization_call(node, qualified_name, root_name)
        if isinstance(node.func, ast.Attribute):
            self._record_mapping_method_call(node)
        else:
            self._record_dict_coercion_call(node)
        self.generic_visit(node)

    def _record_value_type_call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and not self._is_shadowed("isinstance")
            and len(node.args) == 2
            and not node.keywords
            and isinstance(node.args[0], ast.Subscript)
            and (owner := self._bound_owner(node.args[0].value)) is not None
            and (expected_type := self._static_type_repr(node.args[1])) is not None
        ):
            key_kind, key_repr, _key_type_repr = _key_fact(node.args[0].slice)
            negated = isinstance(
                self.context.parents_by_id.get(id(node)), ast.UnaryOp
            ) and isinstance(self.context.parents_by_id[id(node)].op, ast.Not)
            self.facts.structural_validations.append(
                DictionaryStructuralValidationObservation(
                    owner=owner,
                    kind=InternalStructuralValidationKind.VALUE_TYPE,
                    span=_span(node, self.context.logical_path),
                    key_kind=key_kind,
                    key_repr=key_repr,
                    expected_type_repr=expected_type,
                    negated=negated,
                    fail_open=self.state.validation_branch_continues
                    and negated
                    and key_kind is DictionaryKeyKind.LITERAL_STRING,
                )
            )

    def _record_serialization_call(
        self, node: ast.Call, qualified_name: str | None, root_name: str | None
    ) -> None:
        if (
            qualified_name in {"json.dump", "json.dumps"}
            and root_name is not None
            and not self._is_shadowed(root_name)
            and node.args
            and (conversion_owner := self._bound_owner(node.args[0])) is not None
        ):
            self.facts.conversions.append(
                DictionaryConversionObservation(
                    owner=conversion_owner,
                    target_identity=qualified_name,
                    kind=InternalConversionKind.SERIALIZATION,
                    serialization_format="json",
                    span=_span(node, self.context.logical_path),
                )
            )

    def _record_mapping_method_call(self, node: ast.Call) -> None:
        assert isinstance(node.func, ast.Attribute)
        owner = self._bound_owner(node.func.value)
        operation = {
            "get": DictionaryOperationKind.GET,
            "update": DictionaryOperationKind.UPDATE,
            "clear": DictionaryOperationKind.CLEAR,
            "pop": DictionaryOperationKind.POP,
            "setdefault": DictionaryOperationKind.SET_DEFAULT,
        }.get(node.func.attr)
        if owner is None or operation is None:
            return
        if operation is DictionaryOperationKind.CLEAR and (node.args or node.keywords):
            return
        key = (
            node.args[0] if node.args and operation is not DictionaryOperationKind.UPDATE else None
        )
        key_kind, key_repr, key_type_repr = (
            _key_fact(key) if key is not None else (DictionaryKeyKind.UNRESOLVED, None, None)
        )
        mutation = operation in {
            DictionaryOperationKind.UPDATE,
            DictionaryOperationKind.CLEAR,
            DictionaryOperationKind.POP,
            DictionaryOperationKind.SET_DEFAULT,
        }
        observation = DictionaryOperationObservation(
            owner=owner,
            kind=operation,
            span=_span(node, self.context.logical_path),
            variant=self._variant_name(),
            key_kind=key_kind,
            key_repr=key_repr,
            key_type_repr=key_type_repr,
            mutation_timing=(MutationTiming.UNKNOWN if mutation else MutationTiming.NOT_MUTATION),
        )
        self.facts.operations.append(observation)
        self._record_mutation_guards(observation)
        if (
            operation
            in {
                DictionaryOperationKind.GET,
                DictionaryOperationKind.POP,
                DictionaryOperationKind.SET_DEFAULT,
            }
            and key is not None
        ):
            self._record_mapping_default(node, owner, operation, key, key_kind, key_repr)

    def _record_mapping_default(
        self,
        node: ast.Call,
        owner: str,
        operation: DictionaryOperationKind,
        key: ast.expr,
        key_kind: DictionaryKeyKind,
        key_repr: str | None,
    ) -> None:
        default_node = node.args[1] if len(node.args) > 1 else None
        self.facts.defaults.append(
            DictionaryFieldDefaultObservation(
                owner=owner,
                role=(
                    DictionaryDefaultRole.PRODUCER_PROVIDED
                    if operation is DictionaryOperationKind.SET_DEFAULT
                    else DictionaryDefaultRole.CONSUMER_FALLBACK
                ),
                key_kind=key_kind,
                key_repr=key_repr or ast.unparse(key),
                span=_span(node, self.context.logical_path),
                default=_default_observation(default_node, self.context.logical_path, node),
                variant=self._variant_name(),
            )
        )

    def _record_dict_coercion_call(self, node: ast.Call) -> None:
        if not (
            isinstance(node.func, ast.Name)
            and node.func.id == "dict"
            and not self._is_shadowed("dict")
            and node.args
            and (owner := self._bound_owner(node.args[0])) is not None
        ):
            return
        self.facts.operations.append(
            DictionaryOperationObservation(
                owner=owner,
                kind=DictionaryOperationKind.COERCION,
                span=_span(node, self.context.logical_path),
                variant=self._variant_name(),
                coercion="builtins.dict",
            )
        )
        self.facts.conversions.append(
            DictionaryConversionObservation(
                owner=owner,
                target_identity="builtins.dict",
                kind=InternalConversionKind.CONVERSION,
                span=_span(node, self.context.logical_path),
            )
        )

    def visit_Compare(self, node: ast.Compare) -> None:
        literal_validation = self._literal_value_validation(node)
        if literal_validation is not None:
            self.facts.structural_validations.append(literal_validation)
        if len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            exact_keys = self._exact_key_validation(node.left, node.comparators[0])
            if exact_keys is None:
                exact_keys = self._exact_key_validation(node.comparators[0], node.left)
            if exact_keys is not None:
                owner, keys = exact_keys
                self.facts.structural_validations.append(
                    DictionaryStructuralValidationObservation(
                        owner=owner,
                        kind=InternalStructuralValidationKind.EXACT_KEYS,
                        span=_span(node, self.context.logical_path),
                        expected_keys=keys,
                        negated=isinstance(node.ops[0], ast.NotEq),
                        fail_open=self.state.validation_branch_continues
                        and isinstance(node.ops[0], ast.NotEq),
                    )
                )
        if len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)):
            owner = self._bound_owner(node.comparators[0])
            if owner is not None:
                key_kind, key_repr, key_type_repr = _key_fact(node.left)
                self.facts.operations.append(
                    DictionaryOperationObservation(
                        owner=owner,
                        kind=DictionaryOperationKind.MEMBERSHIP,
                        span=_span(node, self.context.logical_path),
                        variant=self._variant_name(),
                        key_kind=key_kind,
                        key_repr=key_repr,
                        key_type_repr=key_type_repr,
                        predicate=(
                            DictionaryPredicate.PRESENT
                            if isinstance(node.ops[0], ast.In)
                            else DictionaryPredicate.ABSENT
                        ),
                        fail_open=self.state.validation_branch_continues
                        and isinstance(node.ops[0], ast.NotIn)
                        and key_kind is DictionaryKeyKind.LITERAL_STRING,
                    )
                )
        self.generic_visit(node)

    def _literal_value_validation(
        self, node: ast.Compare
    ) -> DictionaryStructuralValidationObservation | None:
        if len(node.ops) != 1:
            return None
        operation = node.ops[0]
        field = self._dictionary_field(node.left)
        expected = node.comparators[0]
        if field is None and isinstance(operation, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
            field = self._dictionary_field(expected)
            expected = node.left
        if field is None:
            return None
        owner, key_kind, key_repr = field
        relation: InternalLiteralValidationRelation
        literals: tuple[str, ...]
        if isinstance(operation, (ast.Eq, ast.Is)):
            relation = InternalLiteralValidationRelation.EQUALS
            literal = _literal_json(expected)
            literals = (literal,) if literal is not None else ()
        elif isinstance(operation, (ast.NotEq, ast.IsNot)):
            relation = InternalLiteralValidationRelation.NOT_EQUALS
            literal = _literal_json(expected)
            literals = (literal,) if literal is not None else ()
        elif isinstance(operation, (ast.In, ast.NotIn)) and isinstance(
            expected, (ast.Set, ast.List, ast.Tuple)
        ):
            relation = (
                InternalLiteralValidationRelation.IN
                if isinstance(operation, ast.In)
                else InternalLiteralValidationRelation.NOT_IN
            )
            values = tuple(_literal_json(item) for item in expected.elts)
            literals = (
                tuple(sorted({item for item in values if item is not None}))
                if all(item is not None for item in values)
                else ()
            )
        else:
            return None
        if not literals:
            return None
        return DictionaryStructuralValidationObservation(
            owner=owner,
            kind=InternalStructuralValidationKind.VALUE_LITERAL,
            span=_span(node, self.context.logical_path),
            key_kind=key_kind,
            key_repr=key_repr,
            expected_literals_json=literals,
            literal_relation=relation,
            fail_open=self.state.validation_branch_continues
            and key_kind is DictionaryKeyKind.LITERAL_STRING
            and relation
            in {
                InternalLiteralValidationRelation.NOT_EQUALS,
                InternalLiteralValidationRelation.NOT_IN,
            },
        )

    def _dictionary_field(self, node: ast.expr) -> tuple[str, DictionaryKeyKind, str] | None:
        mapping: ast.expr
        key: ast.expr
        if isinstance(node, ast.Subscript):
            mapping = node.value
            key = node.slice
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 1
            and not node.keywords
        ):
            mapping = node.func.value
            key = node.args[0]
        else:
            return None
        owner = self._bound_owner(mapping)
        if owner is None:
            return None
        key_kind, key_repr, _key_type_repr = _key_fact(key)
        return owner, key_kind, key_repr

    def _exact_key_validation(
        self, candidate: ast.expr, expected: ast.expr
    ) -> tuple[str, tuple[str, ...]] | None:
        if not isinstance(expected, ast.Set):
            return None
        keys = tuple(
            sorted(
                {
                    item.value
                    for item in expected.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
            )
        )
        if not keys or len(keys) != len(expected.elts):
            return None
        mapping = candidate
        if (
            isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Name)
            and candidate.func.id == "set"
            and not self._is_shadowed("set")
            and len(candidate.args) == 1
            and not candidate.keywords
        ):
            mapping = candidate.args[0]
        if (
            isinstance(mapping, ast.Call)
            and isinstance(mapping.func, ast.Attribute)
            and mapping.func.attr == "keys"
            and not mapping.args
            and not mapping.keywords
        ):
            mapping = mapping.func.value
        owner = self._bound_owner(mapping)
        return (owner, keys) if owner is not None else None

    def _static_type_repr(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Tuple):
            items = tuple(self._static_type_repr(item) for item in node.elts)
            if not items or any(item is None for item in items):
                return None
            return "(" + ", ".join(item for item in items if item is not None) + ")"
        if isinstance(node, ast.Name):
            return (
                None if self._is_shadowed(node.id) else _resolved_name(node, self.context.aliases)
            )
        if isinstance(node, ast.Attribute):
            root_name = _call_root_name(node)
            if root_name is None or self._is_shadowed(root_name):
                return None
            return _resolved_name(node, self.context.aliases)
        return None

    def visit_If(self, node: ast.If) -> None:
        previous_branch_state = self.state.validation_branch_continues
        self.state.validation_branch_continues = self._is_nonblocking_notice_body(node.body)
        self._record_truthiness(node.test)
        self.visit(node.test)
        self.state.validation_branch_continues = previous_branch_state
        assigned_names = {
            child.id
            for child in ast.walk(node)
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
        }
        for name in assigned_names:
            self.state.scopes[-1].pop(name, None)
            self.state.lock_scopes[-1].pop(name, None)
        branch_base = self.state.scopes[-1].copy()
        lock_branch_base = self.state.lock_scopes[-1].copy()
        previous_context = self.state.variant_context

        self.state.scopes[-1] = branch_base.copy()
        self.state.lock_scopes[-1] = lock_branch_base.copy()
        self.state.variant_context = (*previous_context, f"if@{node.lineno}:{node.col_offset}:body")
        for statement in node.body:
            self.visit(statement)
        body_scope = self.state.scopes[-1]
        body_lock_scope = self.state.lock_scopes[-1]

        self.state.scopes[-1] = branch_base.copy()
        self.state.lock_scopes[-1] = lock_branch_base.copy()
        self.state.variant_context = (*previous_context, f"if@{node.lineno}:{node.col_offset}:else")
        for statement in node.orelse:
            self.visit(statement)
        else_scope = self.state.scopes[-1]
        else_lock_scope = self.state.lock_scopes[-1]

        joined = branch_base.copy()
        joined_locks = lock_branch_base.copy()
        for name in sorted(assigned_names):
            if name in body_scope and body_scope.get(name) == else_scope.get(name):
                joined[name] = body_scope[name]
            if name in body_lock_scope and body_lock_scope.get(name) == else_lock_scope.get(name):
                joined_locks[name] = body_lock_scope[name]
        self.state.scopes[-1] = joined
        self.state.lock_scopes[-1] = joined_locks
        self.state.variant_context = previous_context

    def _is_nonblocking_notice_body(self, body: list[ast.stmt]) -> bool:
        if not body:
            return False
        for statement in body:
            if isinstance(statement, ast.Pass):
                continue
            if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
                return False
            root_name = _call_root_name(statement.value.func)
            if root_name is None or self._is_shadowed(root_name):
                return False
            if _resolved_name(statement.value.func, self.context.aliases) not in {
                "logging.debug",
                "logging.info",
                "logging.warning",
                "logging.error",
                "logging.exception",
                "logging.critical",
            }:
                return False
        return True

    def visit_While(self, node: ast.While) -> None:
        self._record_truthiness(node.test)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self._record_truthiness(node.test)
        self.generic_visit(node)

    def _record_truthiness(self, node: ast.expr) -> None:
        negated = isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)
        candidate = node.operand if negated else node
        predicate = DictionaryPredicate.FALSY if negated else DictionaryPredicate.TRUTHY
        owner = self._bound_owner(candidate)
        if owner is not None:
            self.facts.operations.append(
                DictionaryOperationObservation(
                    owner=owner,
                    kind=DictionaryOperationKind.TRUTHINESS,
                    span=_span(node, self.context.logical_path),
                    variant=self._variant_name(),
                    predicate=predicate,
                )
            )
            return
        if isinstance(candidate, ast.Subscript):
            owner = self._bound_owner(candidate.value)
            key = candidate.slice
        elif (
            isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Attribute)
            and candidate.func.attr == "get"
            and candidate.args
        ):
            owner = self._bound_owner(candidate.func.value)
            key = candidate.args[0]
        else:
            return
        if owner is None:
            return
        key_kind, key_repr, key_type_repr = _key_fact(key)
        self.facts.operations.append(
            DictionaryOperationObservation(
                owner=owner,
                kind=DictionaryOperationKind.VALIDATION,
                span=_span(node, self.context.logical_path),
                variant=self._variant_name(),
                key_kind=key_kind,
                key_repr=key_repr,
                key_type_repr=key_type_repr,
                predicate=predicate,
                fail_open=self.state.validation_branch_continues
                and predicate is DictionaryPredicate.FALSY
                and key_kind is DictionaryKeyKind.LITERAL_STRING,
            )
        )

    def _owner(self, node: ast.Dict | ast.DictComp) -> str:
        if id(node) in self.state.owner_overrides:
            return self.state.owner_overrides[id(node)]
        scope = ".".join(self.state.parents) if self.state.parents else "<module>"
        return f"{self.context.module}:{scope}:dict@{node.lineno}:{node.col_offset}"

    def _named_owner(self, name: str) -> str:
        scope = ".".join(self.state.parents) if self.state.parents else "<module>"
        if self.state.owned_scope_kinds and self.state.owned_scope_kinds[-1] == "class":
            return f"{self.context.module}:{scope}:<class>.{name}"
        return f"{self.context.module}:{scope}:{name}"

    def _bound_owner(self, node: ast.expr) -> str | None:
        if isinstance(node, (ast.Dict, ast.DictComp)):
            return self._owner(node)
        if isinstance(node, ast.Name):
            for scope, scope_type in zip(
                reversed(self.state.scopes), reversed(self.state.scope_types), strict=True
            ):
                if self.state.scope_types[-1] == "function" and scope_type == "class":
                    continue
                if node.id in scope:
                    return scope[node.id]
        if isinstance(node, ast.Attribute):
            return self._instance_dictionary_owner(node) or self._class_dictionary_owner(node)
        return None

    def _instance_dictionary_owner(self, node: ast.AST) -> str | None:
        if (
            not isinstance(node, ast.Attribute)
            or not isinstance(node.value, ast.Name)
            or node.value.id != self.state.instance_name
            or not self.state.instance_dictionary_scopes
        ):
            return None
        return self.state.instance_dictionary_scopes[-1].get(node.attr)

    def _class_dictionary_owner(self, node: ast.AST) -> str | None:
        if (
            not isinstance(node, ast.Attribute)
            or not isinstance(node.value, ast.Name)
            or not self.state.class_dictionary_scopes
            or not self.state.instance_class_names
        ):
            return None
        class_short_name = self.state.instance_class_names[-1].rsplit(".", 1)[-1]
        if node.value.id not in {self.state.class_receiver_name, class_short_name}:
            return None
        return self.state.class_dictionary_scopes[-1].get(node.attr)

    def _is_dictionary_annotation(self, node: ast.expr | None) -> bool:
        root = _annotation_root_name(node)
        if root is not None and self._is_shadowed(root):
            return False
        return _is_dictionary_annotation(node, self.context.aliases)

    def _is_shadowed(self, name: str) -> bool:
        return any(name in scope for scope in reversed(self.state.shadowed_names))

    def _lock_constructor(self, node: ast.expr) -> InternalLockKind | None:
        if not isinstance(node, ast.Call):
            return None
        root_name = _call_root_name(node.func)
        if root_name is None or self._is_shadowed(root_name):
            return None
        return {
            "threading.Lock": InternalLockKind.THREAD,
            "threading.RLock": InternalLockKind.THREAD,
            "multiprocessing.Lock": InternalLockKind.PROCESS,
            "multiprocessing.RLock": InternalLockKind.PROCESS,
            "asyncio.Lock": InternalLockKind.ASYNC_TASK,
        }.get(_resolved_name(node.func, self.context.aliases))

    def _bound_lock(self, node: ast.expr) -> tuple[str, InternalLockKind] | None:
        if isinstance(node, ast.Name):
            for scope, scope_type in zip(
                reversed(self.state.lock_scopes), reversed(self.state.scope_types), strict=True
            ):
                if self.state.scope_types[-1] == "function" and scope_type == "class":
                    continue
                if node.id in scope:
                    return scope[node.id]
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == self.state.instance_name
            and self.state.instance_lock_scopes
        ):
            return self.state.instance_lock_scopes[-1].get(node.attr)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and self.state.class_lock_scopes
            and self.state.instance_class_names
        ):
            class_short_name = self.state.instance_class_names[-1].rsplit(".", 1)[-1]
            if node.value.id in {self.state.class_receiver_name, class_short_name}:
                return self.state.class_lock_scopes[-1].get(node.attr)
        return None

    def _record_mutation_guards(self, operation: DictionaryOperationObservation) -> None:
        if operation.mutation_timing is MutationTiming.NOT_MUTATION:
            return
        self.facts.mutation_guards.extend(
            DictionaryMutationGuardObservation(
                owner=operation.owner,
                guard_identity=identity,
                kind=kind,
                span=operation.span,
            )
            for identity, kind in self.state.active_locks
        )
        if len(self.state.active_locks) >= 2:
            self.facts.lock_orders.append(
                DictionaryMutationLockOrderObservation(
                    owner=operation.owner,
                    guard_identities=tuple(identity for identity, _kind in self.state.active_locks),
                    span=operation.span,
                )
            )

    def _variant_name(self) -> str | None:
        return "/".join(self.state.variant_context) if self.state.variant_context else None


def traverse_dictionary_facts(
    tree: ast.Module, logical_path: str, module: str
) -> DictionaryFactAccumulator:
    context = DictionaryTraversalContext(
        tree=tree,
        logical_path=logical_path,
        module=module,
        aliases=_import_aliases(tree),
        parents_by_id={
            id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        },
    )
    facts = DictionaryFactAccumulator()
    DictionaryFactVisitor(context, facts).visit(tree)
    return facts
