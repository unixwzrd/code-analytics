"""Bounded syntax-only dictionary producer and consumer flow facts."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from code_analysis.analysis.dictionary_ops import (
    DictionaryAnalysisFacts,
    discover_dictionary_facts,
)
from code_analysis.analysis.dictionary_values import InternalSourceSpan


class InternalFlowKind(StrEnum):
    ARGUMENT = "argument"
    RETURN = "return"


class InternalResolutionState(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class DictionaryFlowObservation:
    source_owner: str
    target_identity: str
    kind: InternalFlowKind
    resolution: InternalResolutionState
    span: InternalSourceSpan
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.source_owner or not self.target_identity:
            raise ValueError("dictionary flow endpoints must be nonempty")
        if self.resolution is InternalResolutionState.RESOLVED:
            if self.reason is not None:
                raise ValueError("resolved dictionary flow cannot contain a failure reason")
        elif not self.reason:
            raise ValueError("unresolved or ambiguous dictionary flow requires a reason")


@dataclass(frozen=True, slots=True)
class DictionaryFlowFacts:
    flows: tuple[DictionaryFlowObservation, ...]
    unresolved_count: int
    ambiguous_count: int
    truncated_count: int

    def __post_init__(self) -> None:
        counts = (self.unresolved_count, self.ambiguous_count, self.truncated_count)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in counts):
            raise TypeError("dictionary flow counts must be exact integers")
        if any(value < 0 for value in counts):
            raise ValueError("dictionary flow counts must be nonnegative")


@dataclass(frozen=True, slots=True)
class _FunctionTarget:
    module: str
    name: str
    qualified_name: str
    parameters: tuple[str, ...]
    parameter_identities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DictionaryCallTarget:
    call_expression: str
    module: str
    function: str
    parameters: tuple[str, ...]
    parameter_identities: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.call_expression or not self.module or not self.function:
            raise ValueError("dictionary call target identities must be nonempty")
        if len(self.parameters) != len(set(self.parameters)):
            raise ValueError("dictionary call target parameters must be unique")
        if len(self.parameters) != len(self.parameter_identities):
            raise ValueError("dictionary call target parameter identities must align")
        if any(not identity for identity in self.parameter_identities) or len(
            self.parameter_identities
        ) != len(set(self.parameter_identities)):
            raise ValueError("dictionary call target parameter identities must be unique")


@dataclass(frozen=True, slots=True)
class DictionaryModuleSource:
    module: str
    logical_path: str
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, str):
            raise TypeError("dictionary module source must be text")
        path = PurePosixPath(self.logical_path)
        if (
            not self.module
            or any(not part for part in self.module.split("."))
            or not self.logical_path
            or path.is_absolute()
            or ".." in path.parts
        ):
            raise ValueError("dictionary module source requires portable identities")


def discover_dictionary_flows(
    source: str,
    logical_path: str,
    module: str,
    *,
    dictionary_facts: DictionaryAnalysisFacts | None = None,
    max_items: int = 1000,
    max_depth: int = 4,
    max_paths_per_owner: int = 100,
    call_targets: tuple[DictionaryCallTarget, ...] = (),
) -> DictionaryFlowFacts:
    """Resolve exact intramodule calls through a deterministic bounded fixed point."""

    _validate_limits(max_items, max_depth, max_paths_per_owner)
    tree = ast.parse(source)
    facts = dictionary_facts or discover_dictionary_facts(source, logical_path, module)
    targets: dict[str, list[_FunctionTarget]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parameters = tuple(
                argument.arg
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
            )
            targets.setdefault(node.name, []).append(
                _FunctionTarget(
                    module=module,
                    name=node.name,
                    qualified_name=node.name,
                    parameters=parameters,
                    parameter_identities=tuple(
                        _parameter_identity(module, node.name, parameter, facts.owners)
                        for parameter in parameters
                    ),
                )
            )
    for call_target in call_targets:
        targets.setdefault(call_target.call_expression, []).append(
            _FunctionTarget(
                module=call_target.module,
                name=call_target.function,
                qualified_name=call_target.function,
                parameters=call_target.parameters,
                parameter_identities=call_target.parameter_identities,
            )
        )

    observations: list[DictionaryFlowObservation] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.parents: tuple[str, ...] = ()
            self.scopes: list[dict[str, str]] = [self._owners_for_scope("<module>")]
            self.call_shadows: list[set[str]] = [set()]

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            previous = self.parents
            self.parents = (*self.parents, node.name)
            scope = ".".join(self.parents)
            self.scopes.append(self._owners_for_scope(scope))
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            ):
                self.scopes[-1].setdefault(
                    argument.arg,
                    f"{module}:{scope}:parameter:{argument.arg}",
                )
            shadowed = _scope_bound_names(node.body)
            shadowed.update(
                argument.arg
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
            )
            if node.args.vararg:
                shadowed.add(node.args.vararg.arg)
            if node.args.kwarg:
                shadowed.add(node.args.kwarg.arg)
            self.call_shadows.append(shadowed)
            self.generic_visit(node)
            self.call_shadows.pop()
            self.scopes.pop()
            self.parents = previous

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node: ast.Assign) -> None:
            names = tuple(target.id for target in node.targets if isinstance(target, ast.Name))
            value_owner = self._bound_owner(node.value)
            for name in names:
                self.scopes[-1].pop(name, None)
            if (
                len(node.targets) == 1
                and len(names) == 1
                and isinstance(node.value, (ast.Dict, ast.DictComp))
            ):
                owner = self._declared_owner(names[0])
                if owner is not None:
                    self.scopes[-1][names[0]] = owner
            elif len(node.targets) == 1 and len(names) == 1 and value_owner is not None:
                self.scopes[-1][names[0]] = value_owner
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if isinstance(node.target, ast.Name):
                self.scopes[-1].pop(node.target.id, None)
                owner = self._declared_owner(node.target.id)
                if owner is not None:
                    self.scopes[-1][node.target.id] = owner
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            dictionary_arguments: list[tuple[str, str]] = []
            for position, argument in enumerate(node.args):
                if (owner := self._bound_owner(argument)) is not None:
                    dictionary_arguments.append((owner, str(position)))
            for keyword in node.keywords:
                if keyword.arg is not None and (owner := self._bound_owner(keyword.value)):
                    dictionary_arguments.append((owner, keyword.arg))
            if not dictionary_arguments:
                self.generic_visit(node)
                return

            call_expression = ast.unparse(node.func)
            call_root = _call_root(node.func)
            call_is_shadowed = call_root is not None and any(
                call_root in scope for scope in reversed(self.call_shadows)
            )
            if call_expression in {"dict", "set"} and not call_is_shadowed:
                self.generic_visit(node)
                return
            candidates = [] if call_is_shadowed else targets.get(call_expression, [])
            for owner, argument_name in dictionary_arguments:
                if len(candidates) == 1:
                    target = candidates[0]
                    parameter_identity = _parameter_identity_for_argument(target, argument_name)
                    if parameter_identity is not None:
                        observations.append(
                            DictionaryFlowObservation(
                                source_owner=owner,
                                target_identity=parameter_identity,
                                kind=InternalFlowKind.ARGUMENT,
                                resolution=InternalResolutionState.RESOLVED,
                                span=_span(node, logical_path),
                            )
                        )
                        continue
                attempted = call_expression
                ambiguous = len(candidates) > 1
                observations.append(
                    DictionaryFlowObservation(
                        source_owner=owner,
                        target_identity=f"{module}:call:{attempted}:argument:{argument_name}",
                        kind=InternalFlowKind.ARGUMENT,
                        resolution=(
                            InternalResolutionState.AMBIGUOUS
                            if ambiguous
                            else InternalResolutionState.UNRESOLVED
                        ),
                        span=_span(node, logical_path),
                        reason=(
                            "multiple project-local call targets share this binding"
                            if ambiguous
                            else "call target or parameter is not exactly resolved locally"
                        ),
                    )
                )
            self.generic_visit(node)

        def visit_Return(self, node: ast.Return) -> None:
            if node.value is not None and (owner := self._bound_owner(node.value)) is not None:
                scope = ".".join(self.parents) if self.parents else "<module>"
                observations.append(
                    DictionaryFlowObservation(
                        source_owner=owner,
                        target_identity=f"{module}:{scope}:return",
                        kind=InternalFlowKind.RETURN,
                        resolution=InternalResolutionState.RESOLVED,
                        span=_span(node, logical_path),
                    )
                )
            self.generic_visit(node)

        def _owners_for_scope(self, scope: str) -> dict[str, str]:
            prefix = f"{module}:{scope}:"
            return {
                owner[len(prefix) :]: owner
                for owner in facts.owners
                if owner.startswith(prefix) and ":" not in owner[len(prefix) :]
            }

        def _declared_owner(self, name: str) -> str | None:
            scope = ".".join(self.parents) if self.parents else "<module>"
            identity = f"{module}:{scope}:{name}"
            return identity if identity in facts.owners else None

        def _bound_owner(self, node: ast.expr) -> str | None:
            if isinstance(node, ast.Name):
                for scope in reversed(self.scopes):
                    if node.id in scope:
                        return scope[node.id]
                return None
            if isinstance(node, ast.Call):
                call_expression = ast.unparse(node.func)
                call_root = _call_root(node.func)
                call_is_shadowed = call_root is not None and any(
                    call_root in scope for scope in reversed(self.call_shadows)
                )
                candidates = [] if call_is_shadowed else targets.get(call_expression, [])
                if len(candidates) == 1:
                    target = candidates[0]
                    return f"{target.module}:{target.qualified_name}:return"
            return None

    Visitor().visit(tree)
    unique = {
        (
            item.source_owner,
            item.target_identity,
            item.kind,
            item.resolution,
            item.span,
            item.reason,
        ): item
        for item in observations
    }
    candidates = tuple(unique[key] for key in sorted(unique, key=repr))
    ordered, bounded_truncated = _reachable_observations(
        candidates,
        seeds=tuple(sorted(facts.owners)),
        max_depth=max_depth,
        max_paths_per_owner=max_paths_per_owner,
    )
    retained = ordered[:max_items]
    return DictionaryFlowFacts(
        flows=retained,
        unresolved_count=sum(
            item.resolution is InternalResolutionState.UNRESOLVED for item in ordered
        ),
        ambiguous_count=sum(
            item.resolution is InternalResolutionState.AMBIGUOUS for item in ordered
        ),
        truncated_count=bounded_truncated + len(ordered) - len(retained),
    )


def discover_project_dictionary_flows(
    modules: tuple[DictionaryModuleSource, ...],
    *,
    max_items: int = 1000,
    max_depth: int = 4,
    max_paths_per_owner: int = 100,
) -> DictionaryFlowFacts:
    """Resolve exact project-local imports before one bounded flow fixed point."""

    _validate_limits(max_items, max_depth, max_paths_per_owner)
    if not modules:
        raise ValueError("dictionary project flow requires at least one module")
    module_names = tuple(item.module for item in modules)
    if list(module_names) != sorted(module_names) or len(module_names) != len(set(module_names)):
        raise ValueError("dictionary project modules must be unique and sorted")

    trees = {item.module: ast.parse(item.source) for item in modules}
    registry: dict[tuple[str, str], list[_FunctionTarget]] = {}
    facts_by_module: dict[str, DictionaryAnalysisFacts] = {}
    for item in modules:
        facts_by_module[item.module] = discover_dictionary_facts(
            item.source,
            item.logical_path,
            item.module,
        )
        for target in _top_level_targets(
            trees[item.module], item.module, facts_by_module[item.module].owners
        ):
            registry.setdefault((item.module, target.name), []).append(target)

    candidates: list[DictionaryFlowObservation] = []
    seeds: set[str] = set()
    known_modules = set(module_names)
    for item in modules:
        facts = facts_by_module[item.module]
        seeds.update(facts.owners)
        targets = _imported_call_targets(
            trees[item.module],
            item.module,
            known_modules,
            registry,
        )
        synthetic_owners = set(facts.owners)
        for target in _top_level_targets(trees[item.module], item.module, facts.owners):
            synthetic_owners.add(f"{item.module}:{target.qualified_name}:return")
            synthetic_owners.update(target.parameter_identities)
        synthetic_owners.update(f"{target.module}:{target.function}:return" for target in targets)
        candidate_facts = DictionaryAnalysisFacts(
            owners=tuple(sorted(synthetic_owners)),
            aliases=facts.aliases,
            operations=facts.operations,
            defaults=facts.defaults,
            conversions=facts.conversions,
            mutation_guards=facts.mutation_guards,
            lock_orders=facts.lock_orders,
            structural_validations=facts.structural_validations,
        )
        candidate_limit = _candidate_observation_limit(trees[item.module])
        extracted = discover_dictionary_flows(
            item.source,
            item.logical_path,
            item.module,
            dictionary_facts=candidate_facts,
            max_items=candidate_limit,
            max_depth=1,
            max_paths_per_owner=candidate_limit,
            call_targets=targets,
        )
        candidates.extend(extracted.flows)

    unique = {_flow_key(item): item for item in candidates}
    ordered_candidates = tuple(unique[key] for key in sorted(unique, key=repr))
    ordered, bounded_truncated = _reachable_observations(
        ordered_candidates,
        seeds=tuple(sorted(seeds)),
        max_depth=max_depth,
        max_paths_per_owner=max_paths_per_owner,
    )
    retained = ordered[:max_items]
    return DictionaryFlowFacts(
        flows=retained,
        unresolved_count=sum(
            item.resolution is InternalResolutionState.UNRESOLVED for item in ordered
        ),
        ambiguous_count=sum(
            item.resolution is InternalResolutionState.AMBIGUOUS for item in ordered
        ),
        truncated_count=bounded_truncated + len(ordered) - len(retained),
    )


def _validate_limits(max_items: int, max_depth: int, max_paths_per_owner: int) -> None:
    for name, value in (
        ("max_items", max_items),
        ("max_depth", max_depth),
        ("max_paths_per_owner", max_paths_per_owner),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"dictionary flow {name} must be a positive exact integer")


def _candidate_observation_limit(tree: ast.Module) -> int:
    argument_edges = sum(
        len(node.args) + sum(keyword.arg is not None for keyword in node.keywords)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    )
    return_edges = sum(isinstance(node, ast.Return) for node in ast.walk(tree))
    return max(1, argument_edges + return_edges)


def _top_level_targets(
    tree: ast.Module,
    module: str,
    owners: tuple[str, ...],
) -> tuple[_FunctionTarget, ...]:
    return tuple(
        _FunctionTarget(
            module=module,
            name=node.name,
            qualified_name=node.name,
            parameters=tuple(
                argument.arg
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
            ),
            parameter_identities=tuple(
                _parameter_identity(module, node.name, argument.arg, owners)
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
            ),
        )
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _imported_call_targets(
    tree: ast.Module,
    current_module: str,
    known_modules: set[str],
    registry: dict[tuple[str, str], list[_FunctionTarget]],
) -> tuple[DictionaryCallTarget, ...]:
    targets: list[DictionaryCallTarget] = []
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name not in known_modules:
                    continue
                prefix = alias.asname or alias.name
                for target in _module_targets(alias.name, registry):
                    targets.append(
                        _call_target(
                            f"{prefix}.{target.name}",
                            target,
                        )
                    )
        elif isinstance(statement, ast.ImportFrom) and statement.level >= 0:
            base = _absolute_import_module(
                current_module,
                statement.module or "",
                statement.level,
                known_modules,
            )
            if base is None:
                continue
            for alias in statement.names:
                if alias.name == "*":
                    continue
                binding = alias.asname or alias.name
                direct = registry.get((base, alias.name), [])
                targets.extend(_call_target(binding, target) for target in direct)
                submodule = f"{base}.{alias.name}" if base else alias.name
                if submodule in known_modules:
                    targets.extend(
                        _call_target(f"{binding}.{target.name}", target)
                        for target in _module_targets(submodule, registry)
                    )
    return tuple(
        sorted(
            targets,
            key=lambda item: (
                item.call_expression,
                item.module,
                item.function,
                item.parameters,
            ),
        )
    )


def _call_target(expression: str, target: _FunctionTarget) -> DictionaryCallTarget:
    return DictionaryCallTarget(
        call_expression=expression,
        module=target.module,
        function=target.qualified_name,
        parameters=target.parameters,
        parameter_identities=target.parameter_identities,
    )


def _module_targets(
    module: str,
    registry: dict[tuple[str, str], list[_FunctionTarget]],
) -> tuple[_FunctionTarget, ...]:
    return tuple(
        target
        for (target_module, _), values in sorted(registry.items())
        if target_module == module
        for target in values
    )


def _absolute_import_module(
    current: str,
    imported: str,
    level: int,
    known_modules: set[str],
) -> str | None:
    if level == 0:
        return imported or None
    current_is_package = current in known_modules and any(
        name.startswith(current + ".") for name in known_modules
    )
    package_parts = current.split(".") if current_is_package else current.split(".")[:-1]
    trim = level - 1
    if trim > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - trim]
    if imported:
        base_parts.extend(imported.split("."))
    return ".".join(base_parts) or None


def _flow_key(item: DictionaryFlowObservation) -> tuple[object, ...]:
    return (
        item.source_owner,
        item.target_identity,
        item.kind,
        item.resolution,
        item.span,
        item.reason,
    )


def _call_root(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _reachable_observations(
    candidates: tuple[DictionaryFlowObservation, ...],
    *,
    seeds: tuple[str, ...],
    max_depth: int,
    max_paths_per_owner: int,
) -> tuple[tuple[DictionaryFlowObservation, ...], int]:
    by_source: dict[str, tuple[DictionaryFlowObservation, ...]] = {}
    sources = sorted({item.source_owner for item in candidates})
    for source in sources:
        by_source[source] = tuple(item for item in candidates if item.source_owner == source)

    retained: dict[tuple[object, ...], DictionaryFlowObservation] = {}
    truncated: set[tuple[str, str, int]] = set()
    for seed in seeds:
        pending: list[tuple[str, int]] = [(seed, 0)]
        visited: set[str] = set()
        path_count = 0
        while pending:
            identity, depth = pending.pop(0)
            if identity in visited:
                continue
            visited.add(identity)
            outgoing = by_source.get(identity, ())
            if depth >= max_depth:
                truncated.update((seed, _observation_identity(item), depth) for item in outgoing)
                continue
            for item in outgoing:
                if path_count >= max_paths_per_owner:
                    truncated.add((seed, _observation_identity(item), depth))
                    continue
                path_count += 1
                key = (
                    item.source_owner,
                    item.target_identity,
                    item.kind,
                    item.resolution,
                    item.span,
                    item.reason,
                )
                retained[key] = item
                if item.resolution is InternalResolutionState.RESOLVED:
                    pending.append((item.target_identity, depth + 1))
    return tuple(retained[key] for key in sorted(retained, key=repr)), len(truncated)


def _observation_identity(item: DictionaryFlowObservation) -> str:
    return "|".join(
        (
            item.source_owner,
            item.target_identity,
            item.kind.value,
            item.resolution.value,
            item.span.logical_path,
            str(item.span.start_line),
            str(item.span.start_column),
        )
    )


def _parameter_identity_for_argument(target: _FunctionTarget, argument: str) -> str | None:
    if argument.isdigit():
        position = int(argument)
        return (
            target.parameter_identities[position]
            if position < len(target.parameter_identities)
            else None
        )
    try:
        position = target.parameters.index(argument)
    except ValueError:
        return None
    return target.parameter_identities[position]


def _parameter_identity(
    module: str,
    function: str,
    parameter: str,
    owners: tuple[str, ...],
) -> str:
    lexical_owner = f"{module}:{function}:{parameter}"
    if lexical_owner in owners:
        return lexical_owner
    return f"{module}:{function}:parameter:{parameter}"


def _span(node: ast.expr | ast.stmt, logical_path: str) -> InternalSourceSpan:
    return InternalSourceSpan(
        logical_path=logical_path,
        start_line=node.lineno,
        start_column=node.col_offset,
        end_line=node.end_lineno or node.lineno,
        end_column=node.end_col_offset or node.col_offset,
    )


def _scope_bound_names(body: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
    return names
