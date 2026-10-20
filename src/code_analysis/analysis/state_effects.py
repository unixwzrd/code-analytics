"""Exact syntax facts for mutable state and provenance-resolved side effects."""

# The source-bound visitor class is constructed and consumed inside each loop iteration.
# ruff: noqa: B023

from __future__ import annotations

import ast
from dataclasses import dataclass

from code_analysis.analysis.clones import CloneSource
from code_analysis.report_models import SideEffectKind, StateResourceKind, StructureScope


@dataclass(frozen=True, slots=True)
class FactSpan:
    logical_path: str
    source_sha256: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    observation: str


@dataclass(frozen=True, slots=True)
class MutableStateFact:
    module: str
    owner_logical_name: str
    logical_name: str
    kind: StateResourceKind
    scope: StructureScope
    declaration: FactSpan
    reads: tuple[FactSpan, ...]
    writes: tuple[FactSpan, ...]
    cleanup: tuple[FactSpan, ...]
    guards: tuple[FactSpan, ...]
    aliases: tuple[FactSpan, ...]


@dataclass(frozen=True, slots=True)
class SideEffectFact:
    module: str
    subject_logical_name: str
    kind: SideEffectKind
    target_identity: str
    span: FactSpan


MUTATORS = {"add", "append", "clear", "discard", "extend", "pop", "remove", "setdefault", "update"}
CLEANUP_METHODS = {"clear", "close", "shutdown"}
LOCK_CALLS = {
    "asyncio.Lock",
    "multiprocessing.Lock",
    "threading.Lock",
    "threading.RLock",
}


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix is not None else None
    return None


def _span(source: CloneSource, node: ast.AST, observation: str) -> FactSpan:
    return FactSpan(
        source.logical_path,
        source.source_sha256,
        node.lineno,
        node.col_offset,
        node.end_lineno or node.lineno,
        node.end_col_offset or node.col_offset,
        observation,
    )


def _assignment(statement: ast.stmt) -> tuple[str, ast.AST] | None:
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
        return (target.id, statement.value) if isinstance(target, ast.Name) else None
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return (statement.target.id, statement.value) if statement.value is not None else None
    return None


def _state_kind(name: str, value: ast.AST) -> StateResourceKind | None:
    dotted = _dotted(value.func) if isinstance(value, ast.Call) else None
    lowered = name.lower()
    if dotted in LOCK_CALLS:
        return StateResourceKind.LOCK
    mutable = isinstance(value, (ast.List, ast.Dict, ast.Set)) or dotted in {
        "collections.defaultdict",
        "collections.OrderedDict",
        "dict",
        "list",
        "set",
        "weakref.WeakKeyDictionary",
        "weakref.WeakValueDictionary",
    }
    if "registry" in lowered and mutable:
        return StateResourceKind.REGISTRY
    if "cache" in lowered and mutable:
        return StateResourceKind.CACHE
    if ("singleton" in lowered or lowered in {"instance", "default_instance"}) and isinstance(
        value, ast.Call
    ):
        return StateResourceKind.SINGLETON
    if mutable:
        return StateResourceKind.OTHER
    return None


def _imports(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
        elif isinstance(statement, ast.ImportFrom) and statement.module:
            for alias in statement.names:
                aliases[alias.asname or alias.name] = f"{statement.module}.{alias.name}"
    return aliases


def _resolved_call(call: ast.Call, aliases: dict[str, str], path_names: set[str]) -> str | None:
    dotted = _dotted(call.func)
    if dotted is None:
        return None
    head, _, tail = dotted.partition(".")
    if head in path_names:
        return f"pathlib.Path.{tail}" if tail else "pathlib.Path"
    imported = aliases.get(head)
    if imported is not None:
        return f"{imported}.{tail}" if tail else imported
    if head in {"open", "print"}:
        return head
    return None


def _side_effect_kind(target: str, call: ast.Call) -> SideEffectKind | None:
    if target == "open":
        mode = call.args[1] if len(call.args) > 1 else None
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            return (
                SideEffectKind.FILESYSTEM_WRITE if set(mode.value) & {"w", "a", "x", "+"} else None
            )
        return None
    if target in {
        "os.remove",
        "os.unlink",
        "pathlib.Path.unlink",
        "shutil.rmtree",
    }:
        return SideEffectKind.FILESYSTEM_DELETE
    if target in {"os.rename", "os.replace", "pathlib.Path.rename", "pathlib.Path.replace"}:
        return SideEffectKind.FILESYSTEM_RENAME
    if target in {
        "os.mkdir",
        "os.makedirs",
        "pathlib.Path.mkdir",
        "pathlib.Path.write_bytes",
        "pathlib.Path.write_text",
        "shutil.copy",
        "shutil.copy2",
        "shutil.copyfile",
        "shutil.move",
    }:
        return SideEffectKind.FILESYSTEM_WRITE
    if target in {"os.getenv"}:
        return SideEffectKind.ENVIRONMENT_READ
    if target.startswith("subprocess.") or target in {"os.system", "os.spawn", "os.exec"}:
        return SideEffectKind.SUBPROCESS
    if target in {"signal.signal", "os.kill"}:
        return SideEffectKind.SIGNAL
    if target in {"threading.Thread", "concurrent.futures.ThreadPoolExecutor"}:
        return SideEffectKind.THREAD_CREATE
    if target in {
        "multiprocessing.Process",
        "concurrent.futures.ProcessPoolExecutor",
    }:
        return SideEffectKind.PROCESS_CREATE
    if target in {"socket.socket.bind", "socket.socket.listen", "socketserver.TCPServer"}:
        return SideEffectKind.NETWORK_LISTENER
    if target.startswith(("http.client.", "requests.", "urllib.request.")) or target in {
        "socket.create_connection",
        "socket.socket.connect",
    }:
        return SideEffectKind.NETWORK_CLIENT
    return None


def discover_state_and_side_effects(
    sources: tuple[CloneSource, ...],
) -> tuple[tuple[MutableStateFact, ...], tuple[SideEffectFact, ...]]:
    state_facts: list[MutableStateFact] = []
    effect_facts: list[SideEffectFact] = []
    for source in sorted(sources, key=lambda item: (item.module, item.logical_path)):
        tree = ast.parse(source.source, filename=source.logical_path)
        aliases = _imports(tree)
        declarations: dict[tuple[str, str], tuple[StateResourceKind, StructureScope, ast.AST]] = {}
        for statement in tree.body:
            assignment = _assignment(statement)
            if assignment:
                name, value = assignment
                kind = _state_kind(name, value)
                if kind is not None:
                    declarations[(source.module, name)] = (kind, StructureScope.MODULE, statement)
            if isinstance(statement, ast.ClassDef):
                owner = f"{source.module}:{statement.name}"
                for child in statement.body:
                    assignment = _assignment(child)
                    if assignment:
                        name, value = assignment
                        kind = _state_kind(name, value)
                        if kind is not None:
                            declarations[(owner, name)] = (
                                kind,
                                StructureScope.CLASS,
                                child,
                            )
        observations: dict[tuple[str, str], dict[str, list[FactSpan]]] = {
            key: {"reads": [], "writes": [], "cleanup": [], "guards": [], "aliases": []}
            for key in declarations
        }

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.parents: list[str] = []
                self.current_function: str | None = None
                self.global_names: set[str] = set()
                self.nonlocal_names: set[str] = set()
                self.shadowed_names: set[str] = set()
                self.local_aliases: dict[str, tuple[str, str]] = {}
                self.path_names: set[str] = set()

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.parents.append(node.name)
                for child in node.body:
                    self.visit(child)
                self.parents.pop()

            def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                previous = (
                    self.current_function,
                    self.global_names,
                    self.nonlocal_names,
                    self.shadowed_names,
                    self.local_aliases,
                    self.path_names,
                )
                qualified = ".".join((*self.parents, node.name))
                self.current_function = f"{source.module}:{qualified}"
                self.parents.append(node.name)
                self.global_names = {
                    name
                    for child in ast.walk(node)
                    if isinstance(child, ast.Global)
                    for name in child.names
                }
                self.nonlocal_names = {
                    name
                    for child in ast.walk(node)
                    if isinstance(child, ast.Nonlocal)
                    for name in child.names
                }
                self.shadowed_names = {
                    child.id
                    for child in ast.walk(node)
                    if isinstance(child, ast.Name)
                    and isinstance(child.ctx, ast.Store)
                    and child.id not in self.global_names
                }
                self.local_aliases = {}
                self.path_names = set()
                for child in node.body:
                    self.visit(child)
                self.parents.pop()
                (
                    self.current_function,
                    self.global_names,
                    self.nonlocal_names,
                    self.shadowed_names,
                    self.local_aliases,
                    self.path_names,
                ) = previous

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_function(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_function(node)

            def visit_Global(self, node: ast.Global) -> None:
                self.global_names.update(node.names)
                for name in node.names:
                    key = (source.module, name)
                    if key in observations:
                        observations[key]["writes"].append(
                            _span(source, node, "global declaration permits module-state mutation")
                        )

            def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
                self.nonlocal_names.update(node.names)

            def visit_Assign(self, node: ast.Assign) -> None:
                if self.current_function and len(node.targets) == 1:
                    target = node.targets[0]
                    if isinstance(target, ast.Name):
                        source_name = _dotted(node.value)
                        key = (source.module, source_name or "")
                        if key in observations:
                            self.local_aliases[target.id] = key
                            observations[key]["aliases"].append(
                                _span(source, node, "direct local alias of mutable state")
                            )
                        resolved = (
                            _resolved_call(node.value, aliases, self.path_names)
                            if isinstance(node.value, ast.Call)
                            else None
                        )
                        if resolved == "pathlib.Path":
                            self.path_names.add(target.id)
                self.generic_visit(node)

            def visit_Name(self, node: ast.Name) -> None:
                if node.id in self.shadowed_names:
                    return
                key = (source.module, node.id)
                if key not in observations:
                    return
                if isinstance(node.ctx, ast.Load):
                    observations[key]["reads"].append(
                        _span(source, node, "module mutable state read")
                    )
                elif isinstance(node.ctx, (ast.Store, ast.Del)) and node.id in self.global_names:
                    observations[key]["writes"].append(
                        _span(source, node, "module mutable state mutation")
                    )

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if isinstance(node.value, ast.Name):
                    class_owner = f"{source.module}:{node.value.id}"
                    key = (class_owner, node.attr)
                    if key in observations:
                        bucket = "reads" if isinstance(node.ctx, ast.Load) else "writes"
                        observations[key][bucket].append(
                            _span(source, node, f"class mutable state {bucket[:-1]}")
                        )
                self.generic_visit(node)

            def visit_With(self, node: ast.With) -> None:
                for item in node.items:
                    name = _dotted(item.context_expr)
                    key = (source.module, name or "")
                    if key in observations and declarations[key][0] is StateResourceKind.LOCK:
                        observations[key]["guards"].append(
                            _span(source, node, "recognized lock context")
                        )
                self.generic_visit(node)

            visit_AsyncWith = visit_With

            def visit_Call(self, node: ast.Call) -> None:
                if isinstance(node.func, ast.Attribute):
                    base = _dotted(node.func.value)
                    key = self.local_aliases.get(base or "", (source.module, base or ""))
                    if base and "." in base:
                        class_name, attribute = base.split(".", 1)
                        class_key = (f"{source.module}:{class_name}", attribute)
                        if class_key in observations:
                            key = class_key
                    if key in observations and node.func.attr in MUTATORS:
                        bucket = "cleanup" if node.func.attr in CLEANUP_METHODS else "writes"
                        observations[key][bucket].append(
                            _span(source, node, f"mutable state {node.func.attr} operation")
                        )
                if self.current_function is not None:
                    resolved = _resolved_call(node, aliases, self.path_names)
                    if resolved is not None:
                        kind = _side_effect_kind(resolved, node)
                        if kind is not None:
                            target = (
                                "os.replace-atomic-replacement"
                                if resolved == "os.replace"
                                else resolved
                            )
                            effect_facts.append(
                                SideEffectFact(
                                    source.module,
                                    self.current_function,
                                    kind,
                                    target,
                                    _span(source, node, f"resolved call to {resolved}"),
                                )
                            )
                self.generic_visit(node)

            def visit_Subscript(self, node: ast.Subscript) -> None:
                dotted = _dotted(node.value)
                resolved = aliases.get(dotted or "", dotted)
                if self.current_function and resolved == "os.environ":
                    kind = (
                        SideEffectKind.ENVIRONMENT_READ
                        if isinstance(node.ctx, ast.Load)
                        else SideEffectKind.ENVIRONMENT_WRITE
                    )
                    effect_facts.append(
                        SideEffectFact(
                            source.module,
                            self.current_function,
                            kind,
                            "os.environ",
                            _span(source, node, f"resolved {kind.value}"),
                        )
                    )
                self.generic_visit(node)

        Visitor().visit(tree)
        for (owner, name), (kind, scope, declaration) in sorted(declarations.items()):
            values = observations[(owner, name)]
            state_facts.append(
                MutableStateFact(
                    source.module,
                    owner,
                    f"{owner}:{name}",
                    (
                        StateResourceKind.MUTABLE_GLOBAL
                        if scope is StructureScope.MODULE and kind is StateResourceKind.OTHER
                        else (
                            StateResourceKind.MUTABLE_CLASS_STATE
                            if scope is StructureScope.CLASS and kind is StateResourceKind.OTHER
                            else kind
                        )
                    ),
                    scope,
                    _span(source, declaration, "mutable state declaration"),
                    tuple(values["reads"]),
                    tuple(values["writes"]),
                    tuple(values["cleanup"]),
                    tuple(values["guards"]),
                    tuple(values["aliases"]),
                )
            )
    return (
        tuple(sorted(state_facts, key=lambda item: item.logical_name)),
        tuple(
            sorted(
                effect_facts,
                key=lambda item: (
                    item.subject_logical_name,
                    item.span.start_line,
                    item.kind.value,
                    item.target_identity,
                ),
            )
        ),
    )
