"""Typed structural and bounded operational validation facts.

Structural checks require dictionary ownership proven by the syntax extractor.
Operational classification is separately limited to path-provenance checks in
validation contexts and never turns those checks into structural findings.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum

from code_analysis.analysis.dictionary_ops import DictionaryAnalysisFacts
from code_analysis.analysis.dictionary_values import (
    DictionaryOperationKind,
    DictionaryOperationObservation,
    DictionaryPredicate,
    DictionaryStructuralValidationObservation,
    InternalSourceSpan,
    InternalStructuralValidationKind,
)


class StructuralValidationKind(StrEnum):
    KEY_MEMBERSHIP = "dictionary-key-membership"
    KEY_VALUE_TRUTHINESS = "dictionary-key-value-truthiness"
    MAPPING_TRUTHINESS = "dictionary-truthiness"
    MAPPING_COERCION = "dictionary-coercion"
    VALUE_TYPE = "dictionary-value-type"
    EXACT_KEYS = "dictionary-exact-keys"
    VALUE_LITERAL = "dictionary-value-literal"


class OperationalValidationKind(StrEnum):
    FILESYSTEM_EXISTENCE = "filesystem-existence"
    FILESYSTEM_TYPE = "filesystem-type"
    PATH_CANONICALIZATION = "path-canonicalization"
    SYMLINK_STATE = "symlink-state"
    FILESYSTEM_METADATA = "filesystem-metadata"
    PERMISSION = "filesystem-permission"


@dataclass(frozen=True, slots=True)
class OperationalValidationSource:
    module: str
    logical_path: str
    source: str

    def __post_init__(self) -> None:
        if not self.module or not self.logical_path or self.logical_path.startswith("/"):
            raise ValueError("operational validation source requires portable identities")


@dataclass(frozen=True, slots=True)
class OperationalValidationSite:
    owner: str
    kind: OperationalValidationKind
    rule: str
    span: InternalSourceSpan


@dataclass(frozen=True, slots=True)
class OperationalValidationPattern:
    kind: OperationalValidationKind
    rule: str
    sites: tuple[OperationalValidationSite, ...]
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.rule or not self.sites:
            raise ValueError("operational validation pattern requires a rule and sites")
        if tuple(sorted(self.sites, key=_operational_site_sort_key)) != self.sites:
            raise ValueError("operational validation sites must be deterministically sorted")
        if tuple(sorted(set(self.limitations))) != self.limitations:
            raise ValueError("operational validation limitations must be unique and sorted")


@dataclass(frozen=True, slots=True)
class StructuralValidationSite:
    owner: str
    kind: StructuralValidationKind
    rule: str
    span: InternalSourceSpan
    field_identity: str | None = None
    fail_open: bool = False

    def __post_init__(self) -> None:
        if not self.owner or not self.rule:
            raise ValueError("structural validation site identities must be nonempty")
        if not isinstance(self.fail_open, bool):
            raise TypeError("structural validation site fail-open state must be boolean")


@dataclass(frozen=True, slots=True)
class StructuralValidationPattern:
    kind: StructuralValidationKind
    rule: str
    sites: tuple[StructuralValidationSite, ...]
    field_identity: str | None = None
    divergent_enforcement: bool = False
    fail_open: bool = False
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.rule or not self.sites:
            raise ValueError("structural validation pattern requires a rule and sites")
        if tuple(sorted(self.sites, key=_site_sort_key)) != self.sites:
            raise ValueError("structural validation sites must be deterministically sorted")
        if tuple(sorted(set(self.limitations))) != self.limitations:
            raise ValueError("structural validation limitations must be unique and sorted")


def discover_structural_validation_patterns(
    facts: tuple[DictionaryAnalysisFacts, ...],
) -> tuple[StructuralValidationPattern, ...]:
    """Group exact dictionary checks without interpreting operational validation."""

    grouped: dict[tuple[StructuralValidationKind, str], list[StructuralValidationSite]] = {}
    for bundle in facts:
        for operation in bundle.operations:
            classified = _classify_operation(operation)
            if classified is None:
                continue
            key = (classified.kind, classified.rule)
            grouped.setdefault(key, []).append(classified)
        for observation in bundle.structural_validations:
            classified = _classify_structural_observation(observation)
            key = (classified.kind, classified.rule)
            grouped.setdefault(key, []).append(classified)

    semantic_kinds_by_field: dict[str, set[StructuralValidationKind]] = {}
    for sites in grouped.values():
        field_identity = sites[0].field_identity
        if field_identity is not None:
            semantic_kinds_by_field.setdefault(field_identity, set()).add(sites[0].kind)

    patterns = []
    for (kind, rule), sites in sorted(grouped.items(), key=lambda item: item[0]):
        ordered_sites = tuple(sorted(set(sites), key=_site_sort_key))
        field_identity = ordered_sites[0].field_identity
        limitations = list(
            (
                "Truthy/falsy value checks do not distinguish absent, null, zero, "
                "false, or empty values.",
            )
            if kind is StructuralValidationKind.KEY_VALUE_TRUTHINESS
            else ()
        )
        fail_open = any(site.fail_open for site in ordered_sites)
        if fail_open:
            limitations.append(
                "A negative literal-field or exact-key-set branch contains only pass or "
                "standard-library logging; runtime acceptance remains unmeasured."
            )
        divergent = bool(
            field_identity is not None
            and semantic_kinds_by_field.get(field_identity, set())
            >= {
                StructuralValidationKind.KEY_MEMBERSHIP,
                StructuralValidationKind.KEY_VALUE_TRUTHINESS,
            }
        )
        patterns.append(
            StructuralValidationPattern(
                kind=kind,
                rule=rule,
                sites=ordered_sites,
                field_identity=field_identity,
                divergent_enforcement=divergent,
                fail_open=fail_open,
                limitations=tuple(sorted(limitations)),
            )
        )
    return tuple(patterns)


def discover_operational_validation_patterns(
    sources: tuple[OperationalValidationSource, ...],
) -> tuple[OperationalValidationPattern, ...]:
    grouped: dict[tuple[OperationalValidationKind, str], list[OperationalValidationSite]] = {}
    for source in sources:
        tree = ast.parse(source.source)
        aliases = _operational_import_aliases(tree)

        class Visitor(ast.NodeVisitor):
            def __init__(
                self,
                source_tree: ast.Module,
                source_aliases: dict[str, str],
                source_identity: OperationalValidationSource,
            ) -> None:
                self.parents: tuple[str, ...] = ()
                self.path_names: list[set[str]] = [set()]
                self.shadowed_names: list[set[str]] = [_operational_bound_names(source_tree.body)]
                self.aliases = source_aliases
                self.source = source_identity
                self.in_validation = 0

            def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                previous = self.parents
                self.parents = (*self.parents, node.name)
                names = {
                    argument.arg
                    for argument in (
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                    )
                    if _is_path_annotation(argument.annotation, self.aliases)
                }
                self.path_names.append(names)
                shadowed = _operational_bound_names(node.body)
                shadowed.update(
                    argument.arg
                    for argument in (
                        *node.args.posonlyargs,
                        *node.args.args,
                        *node.args.kwonlyargs,
                    )
                )
                self.shadowed_names.append(shadowed)
                for statement in node.body:
                    self.visit(statement)
                self.shadowed_names.pop()
                self.path_names.pop()
                self.parents = previous

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
                if isinstance(node.target, ast.Name) and _is_path_annotation(
                    node.annotation, self.aliases
                ):
                    self.path_names[-1].add(node.target.id)
                self.generic_visit(node)

            def visit_Assign(self, node: ast.Assign) -> None:
                if _is_path_constructor(node.value, self.aliases):
                    self.path_names[-1].update(
                        target.id for target in node.targets if isinstance(target, ast.Name)
                    )
                self.generic_visit(node)

            def visit_If(self, node: ast.If) -> None:
                self._visit_validation(node.test)
                for statement in (*node.body, *node.orelse):
                    self.visit(statement)

            def visit_While(self, node: ast.While) -> None:
                self._visit_validation(node.test)
                for statement in (*node.body, *node.orelse):
                    self.visit(statement)

            def visit_Assert(self, node: ast.Assert) -> None:
                self._visit_validation(node.test)
                if node.msg is not None:
                    self.visit(node.msg)

            def visit_Return(self, node: ast.Return) -> None:
                if node.value is not None and isinstance(
                    node.value, (ast.Call, ast.Compare, ast.BoolOp, ast.UnaryOp)
                ):
                    self._visit_validation(node.value)
                elif node.value is not None:
                    self.visit(node.value)

            def visit_Call(self, node: ast.Call) -> None:
                if self.in_validation:
                    classified = self._classify_call(node)
                    if classified is not None:
                        key = (classified.kind, classified.rule)
                        grouped.setdefault(key, []).append(classified)
                self.generic_visit(node)

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if (
                    self.in_validation
                    and node.attr in {"st_dev", "st_gid", "st_ino", "st_mode", "st_uid"}
                    and self._is_stat_result(node.value)
                ):
                    self._record(
                        node,
                        OperationalValidationKind.FILESYSTEM_METADATA,
                        f"filesystem metadata compared: {node.attr}",
                    )
                self.generic_visit(node)

            def _visit_validation(self, node: ast.expr) -> None:
                self.in_validation += 1
                self.visit(node)
                self.in_validation -= 1

            def _classify_call(self, node: ast.Call) -> OperationalValidationSite | None:
                resolved = _operational_resolved_name(node.func, self.aliases)
                mapping = {
                    "os.access": (
                        OperationalValidationKind.PERMISSION,
                        "filesystem permission checked: os.access",
                    ),
                    "os.path.exists": (
                        OperationalValidationKind.FILESYSTEM_EXISTENCE,
                        "filesystem path exists",
                    ),
                    "os.path.isfile": (
                        OperationalValidationKind.FILESYSTEM_TYPE,
                        "filesystem path type checked: file",
                    ),
                    "os.path.isdir": (
                        OperationalValidationKind.FILESYSTEM_TYPE,
                        "filesystem path type checked: directory",
                    ),
                    "os.path.islink": (
                        OperationalValidationKind.SYMLINK_STATE,
                        "filesystem symlink state checked",
                    ),
                    "os.path.realpath": (
                        OperationalValidationKind.PATH_CANONICALIZATION,
                        "filesystem path canonicalized: realpath",
                    ),
                    "os.path.abspath": (
                        OperationalValidationKind.PATH_CANONICALIZATION,
                        "filesystem path canonicalized: abspath",
                    ),
                    "os.path.commonpath": (
                        OperationalValidationKind.PATH_CANONICALIZATION,
                        "filesystem path containment compared: commonpath",
                    ),
                }
                root_name = _operational_root_name(node.func)
                direct = (
                    None
                    if root_name is not None
                    and any(root_name in scope for scope in reversed(self.shadowed_names))
                    else mapping.get(resolved)
                )
                if direct is not None and node.args:
                    kind, rule = direct
                    return self._site(node, kind, rule)
                if not isinstance(node.func, ast.Attribute) or not _is_path_receiver(
                    node.func.value, self.path_names, self.aliases
                ):
                    return None
                method_mapping = {
                    "exists": (
                        OperationalValidationKind.FILESYSTEM_EXISTENCE,
                        "filesystem path exists",
                    ),
                    "is_file": (
                        OperationalValidationKind.FILESYSTEM_TYPE,
                        "filesystem path type checked: file",
                    ),
                    "is_dir": (
                        OperationalValidationKind.FILESYSTEM_TYPE,
                        "filesystem path type checked: directory",
                    ),
                    "is_symlink": (
                        OperationalValidationKind.SYMLINK_STATE,
                        "filesystem symlink state checked",
                    ),
                    "resolve": (
                        OperationalValidationKind.PATH_CANONICALIZATION,
                        "filesystem path canonicalized: resolve",
                    ),
                    "relative_to": (
                        OperationalValidationKind.PATH_CANONICALIZATION,
                        "filesystem path containment checked: relative_to",
                    ),
                    "is_relative_to": (
                        OperationalValidationKind.PATH_CANONICALIZATION,
                        "filesystem path containment checked: is_relative_to",
                    ),
                    "stat": (
                        OperationalValidationKind.FILESYSTEM_METADATA,
                        "filesystem metadata read: stat",
                    ),
                    "lstat": (
                        OperationalValidationKind.FILESYSTEM_METADATA,
                        "filesystem metadata read: lstat",
                    ),
                }
                classified = method_mapping.get(node.func.attr)
                if classified is None:
                    return None
                kind, rule = classified
                return self._site(node, kind, rule)

            def _is_stat_result(self, node: ast.expr) -> bool:
                if not isinstance(node, ast.Call):
                    return False
                resolved = _operational_resolved_name(node.func, self.aliases)
                root_name = _operational_root_name(node.func)
                if resolved in {"os.stat", "os.lstat"}:
                    return bool(
                        node.args
                        and not (
                            root_name is not None
                            and any(root_name in scope for scope in reversed(self.shadowed_names))
                        )
                    )
                return bool(
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"stat", "lstat"}
                    and _is_path_receiver(node.func.value, self.path_names, self.aliases)
                )

            def _record(self, node: ast.AST, kind: OperationalValidationKind, rule: str) -> None:
                site = self._site(node, kind, rule)
                grouped.setdefault((kind, rule), []).append(site)

            def _site(
                self, node: ast.AST, kind: OperationalValidationKind, rule: str
            ) -> OperationalValidationSite:
                scope = ".".join(self.parents) if self.parents else "<module>"
                return OperationalValidationSite(
                    owner=f"{self.source.module}:{scope}:operational-validation",
                    kind=kind,
                    rule=rule,
                    span=InternalSourceSpan(
                        logical_path=self.source.logical_path,
                        start_line=node.lineno,
                        start_column=node.col_offset,
                        end_line=getattr(node, "end_lineno", node.lineno),
                        end_column=getattr(node, "end_col_offset", node.col_offset),
                    ),
                )

        Visitor(tree, aliases, source).visit(tree)

    limitations = (
        "Operational checks must remain with filesystem, runtime, or security ownership.",
        "Syntax evidence does not prove validation outcome, ordering, or effectiveness.",
    )
    return tuple(
        OperationalValidationPattern(
            kind=kind,
            rule=rule,
            sites=tuple(sorted(set(sites), key=_operational_site_sort_key)),
            limitations=limitations,
        )
        for (kind, rule), sites in sorted(grouped.items(), key=lambda item: item[0])
    )


def _classify_structural_observation(
    observation: DictionaryStructuralValidationObservation,
) -> StructuralValidationSite:
    if observation.kind is InternalStructuralValidationKind.VALUE_TYPE:
        key = observation.key_repr if observation.key_repr is not None else "<unresolved-key>"
        relation = "is not instance of" if observation.negated else "is instance of"
        rule = (
            f"dictionary key value {relation}: "
            f"{observation.key_kind.value}:{key}:{observation.expected_type_repr}"
        )
        field_identity = f"{observation.owner}:{observation.key_kind.value}:{key}"
        kind = StructuralValidationKind.VALUE_TYPE
    elif observation.kind is InternalStructuralValidationKind.EXACT_KEYS:
        relation = "not equal to" if observation.negated else "equal to"
        keys = ",".join(observation.expected_keys)
        rule = f"dictionary keys {relation}: [{keys}]"
        field_identity = None
        kind = StructuralValidationKind.EXACT_KEYS
    else:
        literals = "[" + ",".join(observation.expected_literals_json) + "]"
        rule = (
            f"dictionary key value {observation.literal_relation.value}: "
            f"{observation.key_kind.value}:{observation.key_repr}:{literals}"
        )
        field_identity = f"{observation.owner}:{observation.key_kind.value}:{observation.key_repr}"
        kind = StructuralValidationKind.VALUE_LITERAL
    return StructuralValidationSite(
        owner=observation.owner,
        kind=kind,
        rule=rule,
        span=observation.span,
        field_identity=field_identity,
        fail_open=observation.fail_open,
    )


def _classify_operation(
    operation: DictionaryOperationObservation,
) -> StructuralValidationSite | None:
    if operation.kind is DictionaryOperationKind.MEMBERSHIP:
        if operation.predicate not in {
            DictionaryPredicate.PRESENT,
            DictionaryPredicate.ABSENT,
        }:
            return None
        key = operation.key_repr if operation.key_repr is not None else "<unresolved-key>"
        rule = f"dictionary key {operation.predicate.value}: " f"{operation.key_kind.value}:{key}"
        kind = StructuralValidationKind.KEY_MEMBERSHIP
        field_identity = f"{operation.owner}:{operation.key_kind.value}:{key}"
    elif operation.kind is DictionaryOperationKind.VALIDATION:
        if operation.predicate not in {
            DictionaryPredicate.TRUTHY,
            DictionaryPredicate.FALSY,
        }:
            return None
        key = operation.key_repr if operation.key_repr is not None else "<unresolved-key>"
        rule = (
            f"dictionary key value {operation.predicate.value}: "
            f"{operation.key_kind.value}:{key}"
        )
        kind = StructuralValidationKind.KEY_VALUE_TRUTHINESS
        field_identity = f"{operation.owner}:{operation.key_kind.value}:{key}"
    elif operation.kind is DictionaryOperationKind.TRUTHINESS:
        if operation.predicate not in {
            DictionaryPredicate.TRUTHY,
            DictionaryPredicate.FALSY,
        }:
            return None
        rule = f"dictionary {operation.predicate.value}"
        kind = StructuralValidationKind.MAPPING_TRUTHINESS
        field_identity = None
    elif operation.kind is DictionaryOperationKind.COERCION:
        rule = f"dictionary coercion: {operation.coercion}"
        kind = StructuralValidationKind.MAPPING_COERCION
        field_identity = None
    else:
        return None
    return StructuralValidationSite(
        owner=operation.owner,
        kind=kind,
        rule=rule,
        span=operation.span,
        field_identity=field_identity,
        fail_open=operation.fail_open,
    )


def _site_sort_key(site: StructuralValidationSite) -> tuple[object, ...]:
    return (
        site.owner,
        site.span.logical_path,
        site.span.start_line,
        site.span.start_column,
        site.span.end_line,
        site.span.end_column,
    )


def _operational_site_sort_key(site: OperationalValidationSite) -> tuple[object, ...]:
    return (
        site.owner,
        site.span.logical_path,
        site.span.start_line,
        site.span.start_column,
        site.span.end_line,
        site.span.end_column,
    )


def _operational_import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".", 1)[0]] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _operational_resolved_name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _operational_resolved_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _operational_root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _operational_bound_names(body: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
    return names


def _is_path_annotation(node: ast.expr | None, aliases: dict[str, str]) -> bool:
    return node is not None and _operational_resolved_name(node, aliases) in {
        "Path",
        "pathlib.Path",
    }


def _is_path_constructor(node: ast.expr, aliases: dict[str, str]) -> bool:
    return isinstance(node, ast.Call) and _operational_resolved_name(node.func, aliases) in {
        "Path",
        "pathlib.Path",
    }


def _is_path_receiver(
    node: ast.expr,
    path_names: list[set[str]],
    aliases: dict[str, str],
) -> bool:
    if isinstance(node, ast.Name):
        return any(node.id in scope for scope in reversed(path_names))
    if _is_path_constructor(node, aliases):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr in {"absolute", "expanduser", "resolve"} and _is_path_receiver(
            node.func.value, path_names, aliases
        )
    return False
