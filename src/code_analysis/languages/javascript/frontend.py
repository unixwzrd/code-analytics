"""Tree-sitter JavaScript/JSX front end with parser-private CST traversal."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Node

from code_analysis.languages.base import (
    FrontendContext,
    FrontendResult,
    NormalizedComplexityFact,
    NormalizedExportFact,
    NormalizedFunctionFact,
    NormalizedImportFact,
    NormalizedStateFact,
    NormalizedSymbolFact,
    SourceUnit,
)
from code_analysis.models import (
    AvailabilityState,
    Completeness,
    Diagnostic,
    DiagnosticLevel,
    JavaScriptSyntaxMode,
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

_FUNCTION_TYPES = {
    "function_declaration",
    "function_expression",
    "generator_function_declaration",
    "generator_function",
    "arrow_function",
    "method_definition",
}
_CLASS_TYPES = {"class_declaration", "class"}
_BRANCH_TYPES = {
    "if_statement",
    "for_statement",
    "for_in_statement",
    "while_statement",
    "do_statement",
    "switch_case",
    "catch_clause",
    "ternary_expression",
}
_NESTING_TYPES = _BRANCH_TYPES | {"switch_statement", "try_statement", "with_statement"}
_STATEMENT_TYPES = {
    "break_statement",
    "continue_statement",
    "debugger_statement",
    "do_statement",
    "empty_statement",
    "expression_statement",
    "for_in_statement",
    "for_statement",
    "if_statement",
    "labeled_statement",
    "lexical_declaration",
    "return_statement",
    "switch_statement",
    "throw_statement",
    "try_statement",
    "variable_declaration",
    "while_statement",
    "with_statement",
}
_LITERAL_TYPES = {
    "number",
    "string",
    "template_string",
    "true",
    "false",
    "null",
    "undefined",
    "regex",
}
_IDENTIFIER_TYPES = {
    "identifier",
    "property_identifier",
    "private_property_identifier",
    "shorthand_property_identifier",
    "shorthand_property_identifier_pattern",
}


class JavaScriptFrontend:
    frontend_id = "javascript-tree-sitter"
    source_language = SourceLanguage.JAVASCRIPT

    @property
    def capabilities(self) -> tuple[AdapterCapabilityRecord, ...]:
        partial = {
            AdapterCapabilityKind.DATA_FLOW: "Dynamic JavaScript data flow remains unresolved.",
            AdapterCapabilityKind.CONTRACTS_SHAPES: (
                "Only syntax-evidenced JSON and object-shape operations are retained."
            ),
            AdapterCapabilityKind.TEST_DISCOVERY: (
                "Only generic test-module imports and direct references are retained."
            ),
        }
        unsupported = {
            AdapterCapabilityKind.COVERAGE_BINDING: (
                "JavaScript runtime coverage binding is not implemented."
            )
        }
        return tuple(
            AdapterCapabilityRecord(
                capability=kind,
                state=(
                    AdapterCapabilityState.UNSUPPORTED
                    if kind in unsupported
                    else (
                        AdapterCapabilityState.PARTIAL
                        if kind in partial
                        else AdapterCapabilityState.SUPPORTED
                    )
                ),
                limitations=(
                    (unsupported[kind],)
                    if kind in unsupported
                    else ((partial[kind],) if kind in partial else ())
                ),
            )
            for kind in AdapterCapabilityKind
        )

    def availability(self, context: FrontendContext) -> ProviderAvailability:
        del context
        available = all(
            importlib.util.find_spec(name) is not None
            for name in ("tree_sitter", "tree_sitter_javascript")
        )
        return ProviderAvailability(
            provider_id=self.frontend_id,
            state=AvailabilityState.AVAILABLE if available else AvailabilityState.UNAVAILABLE,
            version=(_distribution_version("tree-sitter-javascript") if available else None),
            detail=None if available else "optional JavaScript parser extra is not installed",
        )

    def analyze(self, unit: SourceUnit, context: FrontendContext) -> FrontendResult:
        import tree_sitter_javascript
        from tree_sitter import Language, Parser

        language = Language(tree_sitter_javascript.language())
        tree = Parser(language).parse(unit.source_bytes)
        root = tree.root_node
        diagnostics: list[Diagnostic] = []
        error_nodes = tuple(node for node in _walk(root) if node.type == "ERROR" or node.is_missing)
        if root.has_error or error_nodes:
            diagnostics.append(
                Diagnostic(
                    code="javascript-syntax-error",
                    level=DiagnosticLevel.ERROR,
                    message="Tree-sitter reported ERROR or missing syntax nodes",
                    path=unit.logical_path,
                    provider=self.frontend_id,
                    language=SourceLanguage.JAVASCRIPT,
                )
            )
            completeness = Completeness.PARTIAL
        else:
            completeness = Completeness.COMPLETE

        syntax_mode = (
            JavaScriptSyntaxMode.JSX
            if any(node.type.startswith("jsx_") for node in _walk(root))
            else JavaScriptSyntaxMode.JAVASCRIPT
        )
        imports = _imports(root, unit)
        exports = _exports(root, unit)
        detected_system = _module_system(root, unit)
        module_system = context.module_system_override or detected_system
        if context.module_system_override is ModuleSystem.UNRESOLVED:
            completeness = Completeness.PARTIAL
            diagnostics.append(
                Diagnostic(
                    code="javascript-module-metadata-unresolved",
                    level=DiagnosticLevel.ERROR,
                    message="decisive package metadata is malformed or unsupported",
                    path=unit.logical_path,
                    provider=self.frontend_id,
                    language=SourceLanguage.JAVASCRIPT,
                )
            )
        elif context.module_system_override is not None and detected_system not in {
            ModuleSystem.SCRIPT,
            context.module_system_override,
        }:
            module_system = ModuleSystem.UNRESOLVED
            completeness = Completeness.PARTIAL
            diagnostics.append(
                Diagnostic(
                    code="javascript-module-mode-conflict",
                    level=DiagnosticLevel.ERROR,
                    message="configured module mode conflicts with exact source syntax",
                    path=unit.logical_path,
                    provider=self.frontend_id,
                    language=SourceLanguage.JAVASCRIPT,
                )
            )
        elif detected_system is ModuleSystem.MIXED:
            completeness = Completeness.PARTIAL
            diagnostics.append(
                Diagnostic(
                    code="javascript-mixed-module-mode",
                    level=DiagnosticLevel.ERROR,
                    message="source contains both ESM and CommonJS module evidence",
                    path=unit.logical_path,
                    provider=self.frontend_id,
                    language=SourceLanguage.JAVASCRIPT,
                )
            )

        symbols, functions, states = _symbols_and_functions(root, unit)
        provenance = ParserProvenance(
            frontend_id=self.frontend_id,
            logical_path=unit.logical_path,
            parser_name="tree-sitter",
            parser_version=_distribution_version("tree-sitter"),
            grammar_name="tree-sitter-javascript",
            grammar_version=_distribution_version("tree-sitter-javascript"),
            grammar_abi=language.abi_version,
            language=SourceLanguage.JAVASCRIPT,
            syntax_mode=syntax_mode,
            module_system=module_system,
            configuration_sha256=context.configuration_sha256,
            source_sha256=unit.source_sha256,
            normalization_identity="javascript-normalized-facts:v1",
        )
        return FrontendResult(
            unit=unit,
            symbols=symbols,
            imports=imports,
            exports=exports,
            functions=functions,
            states=states,
            module_system=module_system,
            syntax_mode=syntax_mode.value,
            provenance=provenance,
            diagnostics=tuple(diagnostics),
            completeness=completeness,
        )


def _distribution_version(name: str) -> str:
    return importlib.metadata.version(name)


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _text(node: Node, unit: SourceUnit) -> str:
    return unit.source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _span(node: Node) -> tuple[int, int, int, int]:
    return (
        node.start_point.row + 1,
        node.start_point.column,
        node.end_point.row + 1,
        node.end_point.column,
    )


def _string_value(node: Node, unit: SourceUnit) -> str | None:
    value = _text(node, unit)
    if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
        return value[1:-1]
    return None


def _imports(root: Node, unit: SourceUnit) -> tuple[NormalizedImportFact, ...]:
    facts: list[NormalizedImportFact] = []
    for node in _walk(root):
        source = None
        kind = "runtime"
        dynamic = False
        if node.type == "import_statement":
            source = node.child_by_field_name("source")
            kind = "esm-static"
        elif node.type == "call_expression":
            function = node.child_by_field_name("function")
            arguments = node.child_by_field_name("arguments")
            function_text = _text(function, unit) if function is not None else ""
            named = arguments.named_children if arguments is not None else []
            if function_text in {"require", "import"} and len(named) == 1:
                source = named[0]
                kind = "commonjs" if function_text == "require" else "esm-dynamic"
                dynamic = function_text == "import"
        if source is None:
            continue
        specifier = _string_value(source, unit)
        if specifier is None:
            specifier = "<computed>"
            kind = "dynamic-unresolved"
            dynamic = True
        facts.append(NormalizedImportFact(specifier, kind, dynamic, *_span(node)))
    return tuple(
        sorted(
            set(facts),
            key=lambda item: (item.start_line, item.start_column, item.kind, item.specifier),
        )
    )


def _exports(root: Node, unit: SourceUnit) -> tuple[NormalizedExportFact, ...]:
    facts: list[NormalizedExportFact] = []
    for node in root.named_children:
        if node.type == "export_statement":
            declaration = node.child_by_field_name("declaration")
            source = node.child_by_field_name("source")
            source_value = _string_value(source, unit) if source is not None else None
            text = _text(node, unit).lstrip()
            if text.startswith("export default"):
                facts.append(
                    NormalizedExportFact("default", "default", None, source_value, *_span(node))
                )
            elif declaration is not None:
                name_node = declaration.child_by_field_name("name")
                name = _text(name_node, unit) if name_node is not None else "anonymous"
                facts.append(NormalizedExportFact(name, "named", name, source_value, *_span(node)))
            else:
                for specifier in (
                    child for child in _walk(node) if child.type == "export_specifier"
                ):
                    name_node = specifier.child_by_field_name(
                        "alias"
                    ) or specifier.child_by_field_name("name")
                    local = specifier.child_by_field_name("name")
                    if name_node is not None:
                        facts.append(
                            NormalizedExportFact(
                                _text(name_node, unit),
                                "re-export" if source_value else "named",
                                _text(local, unit) if local is not None else None,
                                source_value,
                                *_span(specifier),
                            )
                        )
        elif node.type == "expression_statement":
            text = _text(node, unit).strip()
            if text.startswith("module.exports"):
                facts.append(NormalizedExportFact("default", "commonjs", None, None, *_span(node)))
            elif text.startswith("exports."):
                name = text[len("exports.") :].split("=", 1)[0].strip()
                facts.append(NormalizedExportFact(name, "commonjs", name, None, *_span(node)))
    return tuple(
        sorted(set(facts), key=lambda item: (item.name, item.start_line, item.start_column))
    )


def _module_system(root: Node, unit: SourceUnit) -> ModuleSystem:
    esm = any(node.type in {"import_statement", "export_statement"} for node in root.named_children)
    commonjs = any(
        "require(" in _text(node, unit)
        or "module.exports" in _text(node, unit)
        or "exports." in _text(node, unit)
        for node in root.named_children
    )
    if esm and commonjs:
        return ModuleSystem.MIXED
    if esm:
        return ModuleSystem.ESM
    if commonjs:
        return ModuleSystem.COMMONJS
    return ModuleSystem.SCRIPT


def _symbol_name(node: Node, unit: SourceUnit) -> str:
    name = node.child_by_field_name("name")
    if name is not None:
        return _text(name, unit)
    parent = node.parent
    if parent is not None and parent.type == "variable_declarator":
        assigned = parent.child_by_field_name("name")
        if assigned is not None:
            return _text(assigned, unit)
    return "<anonymous>"


def _symbols_and_functions(root: Node, unit: SourceUnit) -> tuple[
    tuple[NormalizedSymbolFact, ...],
    tuple[NormalizedFunctionFact, ...],
    tuple[NormalizedStateFact, ...],
]:
    symbols: list[NormalizedSymbolFact] = []
    functions: list[NormalizedFunctionFact] = []
    states: list[NormalizedStateFact] = []
    module_provenance = _module_provenance(root, unit)
    declaration_counts: dict[tuple[tuple[str, ...], str, str], int] = {}

    def visit(node: Node, parents: tuple[tuple[str, str], ...]) -> None:
        is_class = node.type in _CLASS_TYPES
        is_function = node.type in _FUNCTION_TYPES
        if node.type == "field_definition" and parents:
            name_node = node.child_by_field_name("property") or node.named_children[0]
            name = _text(name_node, unit)
            owner = ".".join(item[0] for item in parents)
            symbols.append(
                NormalizedSymbolFact(
                    f"{owner}.{name}",
                    name,
                    "field",
                    *_span(node),
                    (),
                    None,
                    None,
                )
            )
            states.append(NormalizedStateFact(owner, name, True, *_span(node)))
        elif node.type in {"lexical_declaration", "variable_declaration"} and not parents:
            for declaration in (
                child for child in node.named_children if child.type == "variable_declarator"
            ):
                binding_node = declaration.child_by_field_name("name")
                value = declaration.child_by_field_name("value")
                if (
                    binding_node is not None
                    and value is not None
                    and value.type
                    in {
                        "array",
                        "object",
                        "new_expression",
                    }
                ):
                    states.append(
                        NormalizedStateFact(
                            None,
                            _text(binding_node, unit),
                            False,
                            *_span(declaration),
                        )
                    )
        next_parents = parents
        if is_class or is_function:
            name = _symbol_name(node, unit)
            if is_class:
                kind = "class" if not parents else "nested_class"
                complexity = None
            else:
                if node.type == "method_definition":
                    prefix = _text(node, unit).lstrip()
                    kind = (
                        "getter"
                        if prefix.startswith("get ")
                        else (
                            "setter"
                            if prefix.startswith("set ")
                            else "constructor" if name == "constructor" else "method"
                        )
                    )
                else:
                    kind = (
                        "method"
                        if parents and parents[-1][1] == "class"
                        else "function" if not parents else "nested_function"
                    )
                complexity = _complexity(node)
            parent_names = tuple(item[0] for item in parents)
            declaration_key = (parent_names, name, kind)
            discriminator = declaration_counts.get(declaration_key, 0)
            declaration_counts[declaration_key] = discriminator + 1
            component = name if discriminator == 0 else f"{name}#{discriminator + 1}"
            qualified = ".".join((*parent_names, component))
            parameters = _parameters(node, unit) if is_function else ()
            symbols.append(
                NormalizedSymbolFact(
                    qualified,
                    name,
                    kind,
                    *_span(node),
                    parameters,
                    None,
                    complexity,
                    async_=is_function and _text(node, unit).lstrip().startswith("async "),
                    generator=is_function and "generator" in node.type,
                )
            )
            next_parents = (*parents, (component, "class" if is_class else "function"))
            if is_function:
                functions.append(
                    _function_fact(node, unit, qualified, parameters, module_provenance)
                )
        for child in node.named_children:
            visit(child, next_parents)

    for child in root.named_children:
        visit(child, ())
    return (
        tuple(sorted(symbols, key=lambda item: (item.qualified_name, item.start_line))),
        tuple(sorted(functions, key=lambda item: (item.qualified_name, item.start_line))),
        tuple(sorted(states, key=lambda item: (item.owner_qualified_name or "", item.name))),
    )


def _parameters(node: Node, unit: SourceUnit) -> tuple[str, ...]:
    parameters = node.child_by_field_name("parameters")
    if parameters is None:
        parameter = node.child_by_field_name("parameter")
        return (_text(parameter, unit),) if parameter is not None else ()
    values = []
    for child in parameters.named_children:
        parameter = child.child_by_field_name("left")
        if parameter is None and child.type == "rest_pattern" and child.named_children:
            parameter = child.named_children[0]
        values.append(_text(parameter or child, unit))
    return tuple(values)


def _body(node: Node) -> Node:
    return node.child_by_field_name("body") or node


def _complexity(node: Node) -> NormalizedComplexityFact:
    body = _body(node)
    branches = 0
    returns = 0
    raises = 0
    logical = 0
    max_depth = 0

    def visit(current: Node, depth: int) -> None:
        nonlocal branches, returns, raises, logical, max_depth
        if current is not body and current.type in _FUNCTION_TYPES | _CLASS_TYPES:
            return
        if current.type in _BRANCH_TYPES:
            branches += 1
        if current.type == "return_statement":
            returns += 1
        if current.type == "throw_statement":
            raises += 1
        if current.type in _STATEMENT_TYPES:
            logical += 1
        nested = current.type in _NESTING_TYPES
        next_depth = depth + int(nested)
        max_depth = max(max_depth, next_depth)
        for child in current.named_children:
            visit(child, next_depth)

    visit(body, 0)
    parameter_node = node.child_by_field_name("parameters")
    parameters = len(parameter_node.named_children) if parameter_node is not None else 0
    physical = node.end_point.row - node.start_point.row + 1
    return NormalizedComplexityFact(
        physical,
        logical,
        branches,
        max_depth,
        parameters,
        branches + 1,
        physical // 25 + branches * 2 + max_depth * 2 + parameters,
        returns,
        raises,
    )


def _dotted(node: Node, unit: SourceUnit) -> str | None:
    if node.type in {"identifier", "property_identifier", "private_property_identifier", "this"}:
        return _text(node, unit)
    if node.type in {"member_expression", "subscript_expression"}:
        obj = node.child_by_field_name("object")
        prop = node.child_by_field_name("property") or node.child_by_field_name("index")
        left = _dotted(obj, unit) if obj is not None else None
        right = _text(prop, unit) if prop is not None else None
        return f"{left}.{right}" if left and right else None
    if node.type == "call_expression":
        function = node.child_by_field_name("function")
        called = _dotted(function, unit) if function is not None else None
        return f"{called}()" if called else None
    return None


def _function_fact(
    node: Node,
    unit: SourceUnit,
    qualified: str,
    parameters: tuple[str, ...],
    module_provenance: dict[str, str],
) -> NormalizedFunctionFact:
    body = _body(node)
    callees: set[str] = set()
    unresolved: set[str] = set()
    controls: list[str] = []
    exceptions: set[str] = set()
    effects: set[str] = set()
    attributes: set[str] = set()
    schemas: set[str] = set()
    statement_nodes = tuple(
        child for child in body.named_children if child.type in _STATEMENT_TYPES
    )
    statements = [_normalized_signature(child, unit, detailed=False) for child in statement_nodes]
    detailed = [_normalized_signature(child, unit, detailed=True) for child in statement_nodes]
    parameter_set = set(parameters)
    local_names = set(parameters)
    provenance = dict(module_provenance)
    parameter_nodes = node.child_by_field_name("parameters")
    if parameter_nodes is not None:
        local_names.update(_binding_names(parameter_nodes, unit))
    provenance.update(dict.fromkeys(local_names, "local"))
    for current in _walk_function_body(body):
        if current.type == "variable_declarator":
            name = current.child_by_field_name("name")
            if name is not None:
                names = _binding_names(name, unit)
                local_names.update(names)
                value = current.child_by_field_name("value")
                owner = _required_module(value, unit) if value is not None else None
                for local_name in names:
                    provenance[local_name] = owner or "local"
        elif current.type in _FUNCTION_TYPES | _CLASS_TYPES:
            name = current.child_by_field_name("name")
            if name is not None:
                local_name = _text(name, unit)
                local_names.add(local_name)
                provenance[local_name] = "local"
    for current in _walk_function_body(body):
        if current.type in _BRANCH_TYPES or current.type in {
            "return_statement",
            "throw_statement",
            "await_expression",
        }:
            controls.append(f"control:{current.type}")
        if current.type == "throw_statement":
            value = current.named_children[0] if current.named_children else None
            exceptions.add(_text(value, unit)[:80] if value is not None else "throw")
        if current.type == "call_expression":
            target = current.child_by_field_name("function")
            dotted = _dotted(target, unit) if target is not None else None
            if dotted is None:
                unresolved.add("dynamic-call")
            else:
                callees.add(dotted)
                if dotted.endswith("addEventListener"):
                    controls.append("call:event-registration")
                elif dotted.endswith("removeEventListener"):
                    controls.append("call:event-removal")
                if dotted.endswith((".then", ".catch", ".finally")):
                    controls.append("call:promise-chain")
                _classify_call(dotted, effects, schemas, provenance)
        if current.type in {
            "assignment_expression",
            "augmented_assignment_expression",
            "update_expression",
        }:
            left = current.child_by_field_name("left") or current.child_by_field_name("argument")
            dotted = _dotted(left, unit) if left is not None else None
            if dotted and (
                dotted.startswith("this.") or dotted.split(".", 1)[0] not in local_names
            ):
                effects.add("mutable-state-write")
                attributes.add(dotted)
        if current.type == "identifier":
            identifier_name = _text(current, unit)
            if identifier_name not in local_names and identifier_name not in parameter_set:
                attributes.add(identifier_name)
    callee_heads = {item.split(".", 1)[0].removesuffix("()") for item in callees}
    return NormalizedFunctionFact(
        qualified,
        *_span(node),
        body.start_byte,
        body.end_byte,
        parameters,
        tuple(sorted(callees)),
        tuple(sorted(unresolved)),
        tuple(controls),
        tuple(sorted(exceptions)),
        tuple(sorted(effects)),
        tuple(sorted(attributes - callee_heads)),
        tuple(sorted(schemas)),
        tuple(statements),
        tuple(detailed),
    )


def _walk_function_body(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        if child is not node and child.type in _FUNCTION_TYPES | _CLASS_TYPES:
            continue
        yield from _walk_function_body(child)


def _classify_call(
    target: str,
    effects: set[str],
    schemas: set[str],
    provenance: dict[str, str],
) -> None:
    head = target.split(".", 1)[0]
    owner = provenance.get(head)
    unshadowed_global = owner is None
    if target in {"JSON.parse", "JSON.stringify"} and unshadowed_global:
        schemas.add(target)
    if head in {"fetch", "WebSocket", "EventSource"} and unshadowed_global:
        effects.add("network-client")
    if head in {"localStorage", "sessionStorage"} and unshadowed_global:
        effects.add("storage")
    if head in {"document", "window"} and unshadowed_global:
        effects.add("dom")
    if head in {"setTimeout", "setInterval", "clearTimeout", "clearInterval"} and unshadowed_global:
        effects.add("timer")
    if owner in {"fs", "node:fs"}:
        effects.add("filesystem")
    if owner in {"child_process", "node:child_process"}:
        effects.add("subprocess")
    if (target.endswith("addEventListener") or target.endswith("removeEventListener")) and (
        (head in {"document", "window"} and unshadowed_global) or head == "this"
    ):
        effects.add("event-listener")
    if target.endswith((".then", ".catch", ".finally")) and (
        (head in {"Promise", "fetch()"} and provenance.get(head.removesuffix("()")) is None)
        or head == "this"
    ):
        effects.add("promise-chain")


def _module_provenance(root: Node, unit: SourceUnit) -> dict[str, str]:
    provenance: dict[str, str] = {}
    for node in root.named_children:
        if node.type == "import_statement":
            source = node.child_by_field_name("source")
            specifier = _string_value(source, unit) if source is not None else None
            if specifier:
                for current in _walk(node):
                    if current.type == "identifier":
                        provenance[_text(current, unit)] = specifier
        elif node.type in {"lexical_declaration", "variable_declaration"}:
            for declaration in node.named_children:
                if declaration.type != "variable_declarator":
                    continue
                name = declaration.child_by_field_name("name")
                value = declaration.child_by_field_name("value")
                owner = _required_module(value, unit) if value is not None else None
                if name is not None:
                    for local_name in _binding_names(name, unit):
                        provenance[local_name] = owner or "local"
        elif node.type in _FUNCTION_TYPES | _CLASS_TYPES:
            name = node.child_by_field_name("name")
            if name is not None:
                provenance[_text(name, unit)] = "local"
    return provenance


def _binding_names(node: Node, unit: SourceUnit) -> set[str]:
    return {
        _text(current, unit)
        for current in _walk(node)
        if current.type in {"identifier", "shorthand_property_identifier_pattern"}
    }


def _required_module(node: Node, unit: SourceUnit) -> str | None:
    if node is None or node.type != "call_expression":
        return None
    function = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    if function is None or _text(function, unit) != "require" or arguments is None:
        return None
    first = arguments.named_children[0] if arguments.named_children else None
    return _string_value(first, unit) if first is not None else None


def _normalized_signature(node: Node, unit: SourceUnit, *, detailed: bool) -> str:
    parts: list[str] = []

    def visit(current: Node) -> None:
        if current.type in _IDENTIFIER_TYPES:
            value = _text(current, unit)
            parts.append(
                f"ID:{hashlib.sha256(value.encode()).hexdigest()[:12]}" if detailed else "ID"
            )
            return
        if current.type in _LITERAL_TYPES:
            value = _text(current, unit)
            parts.append(
                f"{current.type}:{hashlib.sha256(value.encode()).hexdigest()[:12]}"
                if detailed
                else f"LITERAL:{current.type}"
            )
            return
        parts.append(current.type)
        for child in current.named_children:
            visit(child)

    visit(node)
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()
