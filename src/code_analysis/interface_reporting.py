"""Static Python/JavaScript interface boundary correlation."""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from code_analysis.context import AnalysisContext
from code_analysis.models import (
    Completeness,
    Confidence,
    Diagnostic,
    DiagnosticLevel,
    Evidence,
    ProviderResult,
    ProviderState,
    SourceLanguage,
    SourceSpan,
)
from code_analysis.project import (
    iter_javascript_files,
    iter_python_files,
    javascript_module_name,
    logical_path,
    module_name,
)
from code_analysis.report_models import (
    AnalysisReport,
    BoundaryClass,
    InterfaceCorrelation,
    InterfaceEndpoint,
    InterfaceMismatch,
    InterfaceMismatchKind,
    InterfacePayloadField,
    InterfacePayloadVariant,
    InterfaceRole,
    InterfaceTransport,
    RequirednessKind,
    SectionCoverage,
    SectionState,
    SourceFileRecord,
    TypeExpression,
    TypeKind,
)

_HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put"}
_PARAMETER = re.compile(r"\{[^{}]+\}")
_TEMPLATE_EXPRESSION = re.compile(r"\$\{[^{}]+\}")


@dataclass(frozen=True, slots=True)
class _FieldFact:
    name: str
    requiredness: RequirednessKind
    allows_null: bool | None
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class _VariantFact:
    boundary_class: BoundaryClass
    fields: tuple[_FieldFact, ...]
    evidence: Evidence
    completeness: Completeness = Completeness.COMPLETE
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _EndpointFact:
    language: SourceLanguage
    module: str
    logical_name: str
    role: InterfaceRole
    transport: InterfaceTransport
    method: str | None
    normalized_path: str
    variants: tuple[_VariantFact, ...]
    status_codes: tuple[int, ...]
    error_codes: tuple[str, ...]
    event_names: tuple[str, ...]
    retry_observations: tuple[str, ...]
    lifecycle_observations: tuple[str, ...]
    evidence: Evidence
    completeness: Completeness
    limitations: tuple[str, ...]


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def _normalize_path(value: str) -> str:
    parsed = urlsplit(value)
    path = parsed.path if parsed.scheme or parsed.netloc else value.split("?", 1)[0]
    path = _PARAMETER.sub("{}", _TEMPLATE_EXPRESSION.sub("{}", path))
    path = "/" + "/".join(item for item in path.split("/") if item)
    return path or "/"


def _evidence(
    *,
    path: str,
    digest: str,
    language: SourceLanguage,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
    observation: str,
) -> Evidence:
    return Evidence(
        span=SourceSpan(
            path=path,
            start_line=max(1, start_line),
            start_column=max(0, start_column),
            end_line=max(1, end_line),
            end_column=max(0, end_column),
            language=language,
        ),
        source_sha256=digest,
        provider="native-interfaces",
        observation=observation,
        confidence=Confidence.EXACT,
        language=language,
    )


def _python_string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _python_dict_fields(
    node: ast.Dict, path: str, digest: str, boundary: BoundaryClass
) -> _VariantFact:
    evidence = _evidence(
        path=path,
        digest=digest,
        language=SourceLanguage.PYTHON,
        start_line=node.lineno,
        start_column=node.col_offset,
        end_line=node.end_lineno or node.lineno,
        end_column=node.end_col_offset or node.col_offset,
        observation=f"literal {boundary.value} payload",
    )
    fields = tuple(
        sorted(
            (
                _FieldFact(
                    name=key.value,
                    requiredness=RequirednessKind.ALWAYS,
                    allows_null=isinstance(value, ast.Constant) and value.value is None,
                    evidence=evidence,
                )
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ),
            key=lambda item: item.name,
        )
    )
    return _VariantFact(boundary, fields, evidence)


def _classify_nullability(expression: TypeExpression) -> bool | None:
    """Classify top-level nullability from the normalized type-expression tree."""

    if expression.kind is TypeKind.OPTIONAL:
        return True
    if expression.kind is TypeKind.UNION:
        members = tuple(_classify_nullability(argument) for argument in expression.arguments)
        if any(member is True for member in members):
            return True
        if any(member is None for member in members):
            return None
        return False
    if expression.kind is TypeKind.LITERAL:
        return any(value is None for value in expression.literal_values)
    if expression.kind is TypeKind.UNKNOWN:
        return None
    return expression.kind in {TypeKind.SCALAR, TypeKind.NAMED} and expression.name in {
        "None",
        "NoneType",
    }


def _python_contract_variant(
    report: AnalysisReport,
    annotation: ast.AST | None,
    evidence: Evidence,
) -> _VariantFact | None:
    name = annotation.id if isinstance(annotation, ast.Name) else None
    if name is None:
        return None
    subjects = {item.subject_id: item for item in report.subjects}
    contract = next(
        (
            item
            for item in report.contracts
            if subjects[item.defining_subject_id].logical_name.rsplit(":", 1)[-1] == name
        ),
        None,
    )
    if contract is None:
        return None
    fields = tuple(
        InterfacePayloadField(
            name=item.name,
            requiredness=item.requiredness.kind,
            allows_null=_classify_nullability(item.type_expression),
            producer_defaults=item.producer_defaults,
            evidence=item.evidence,
        )
        for item in contract.members
    )
    unresolved_fields = tuple(sorted(item.name for item in fields if item.allows_null is None))
    return _VariantFact(
        BoundaryClass.HTTP_REQUEST,
        tuple(
            _FieldFact(field.name, field.requiredness, field.allows_null, field.evidence[0])
            for field in fields
        ),
        evidence,
        completeness=(Completeness.PARTIAL if unresolved_fields else Completeness.COMPLETE),
        limitations=(
            (
                "Nullability is unresolved for normalized unknown fields: "
                + ", ".join(unresolved_fields)
                + ".",
            )
            if unresolved_fields
            else ()
        ),
    )


def _bound_interface_source(
    path: Path, record: SourceFileRecord, diagnostics: list[Diagnostic]
) -> bytes | None:
    try:
        source = path.read_bytes()
    except OSError as exc:
        diagnostics.append(
            Diagnostic(
                code="interface-source-read-error",
                level=DiagnosticLevel.ERROR,
                message=f"interface source is unavailable: {type(exc).__name__}",
                path=record.logical_path,
                provider="native-interfaces",
                language=record.language,
            )
        )
        return None
    if hashlib.sha256(source).hexdigest() != record.source_sha256:
        diagnostics.append(
            Diagnostic(
                code="interface-source-changed",
                level=DiagnosticLevel.ERROR,
                message="source changed before interface correlation",
                path=record.logical_path,
                provider="native-interfaces",
                language=record.language,
            )
        )
        return None
    return source


def _python_endpoints(
    context: AnalysisContext, report: AnalysisReport, diagnostics: list[Diagnostic]
) -> tuple[_EndpointFact, ...]:
    records = {item.logical_path: item for item in report.source_files}
    endpoints: list[_EndpointFact] = []
    for file_path in iter_python_files(context):
        relative = logical_path(context, file_path)
        record = records.get(relative)
        if record is None:
            continue
        source = _bound_interface_source(file_path, record, diagnostics)
        if source is None:
            continue
        digest = record.source_sha256
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError:
            continue
        prefixes: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            target = (
                node.targets[0] if isinstance(node, ast.Assign) and node.targets else node.target
            )
            if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
                continue
            call_name = (
                value.func.id
                if isinstance(value.func, ast.Name)
                else value.func.attr if isinstance(value.func, ast.Attribute) else ""
            )
            if call_name != "APIRouter":
                continue
            prefix = next(
                (_python_string(item.value) for item in value.keywords if item.arg == "prefix"),
                None,
            )
            prefixes[target.id] = prefix or ""
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(
                    decorator.func, ast.Attribute
                ):
                    continue
                method = decorator.func.attr.lower()
                if method not in _HTTP_METHODS and method != "websocket":
                    continue
                route = _python_string(decorator.args[0]) if decorator.args else None
                if route is None:
                    diagnostics.append(
                        Diagnostic(
                            code="interface-route-unresolved",
                            level=DiagnosticLevel.WARNING,
                            message="route decorator path is not a literal",
                            path=relative,
                            provider="native-interfaces",
                            language=SourceLanguage.PYTHON,
                        )
                    )
                    continue
                owner = (
                    decorator.func.value.id if isinstance(decorator.func.value, ast.Name) else ""
                )
                endpoint_evidence = _evidence(
                    path=relative,
                    digest=digest,
                    language=SourceLanguage.PYTHON,
                    start_line=decorator.lineno,
                    start_column=decorator.col_offset,
                    end_line=decorator.end_lineno or decorator.lineno,
                    end_column=decorator.end_col_offset or decorator.col_offset,
                    observation="literal route producer",
                )
                variants: list[_VariantFact] = []
                for argument in node.args.args:
                    variant = _python_contract_variant(
                        report, argument.annotation, endpoint_evidence
                    )
                    if variant is not None:
                        variants.append(variant)
                status_codes = {
                    int(item.value.value)
                    for item in decorator.keywords
                    if item.arg == "status_code"
                    and isinstance(item.value, ast.Constant)
                    and isinstance(item.value.value, int)
                    and not isinstance(item.value.value, bool)
                }
                error_codes: set[str] = set()
                for child in ast.walk(node):
                    if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict):
                        variant = _python_dict_fields(
                            child.value, relative, digest, BoundaryClass.HTTP_RESPONSE
                        )
                        variants.append(variant)
                        for key, value in zip(child.value.keys, child.value.values, strict=True):
                            if (
                                isinstance(key, ast.Constant)
                                and key.value in {"code", "error_code"}
                                and isinstance(value, ast.Constant)
                                and isinstance(value.value, str)
                            ):
                                error_codes.add(value.value)
                endpoints.append(
                    _EndpointFact(
                        language=SourceLanguage.PYTHON,
                        module=module_name(context, file_path),
                        logical_name=f"{module_name(context, file_path)}:{node.name}",
                        role=InterfaceRole.PRODUCER,
                        transport=(
                            InterfaceTransport.WEBSOCKET
                            if method == "websocket"
                            else InterfaceTransport.HTTP
                        ),
                        method=None if method == "websocket" else method.upper(),
                        normalized_path=_normalize_path(prefixes.get(owner, "") + route),
                        variants=tuple(variants),
                        status_codes=tuple(sorted(status_codes)),
                        error_codes=tuple(sorted(error_codes)),
                        event_names=(),
                        retry_observations=(),
                        lifecycle_observations=(),
                        evidence=endpoint_evidence,
                        completeness=(
                            Completeness.PARTIAL
                            if any(item.completeness is Completeness.PARTIAL for item in variants)
                            else Completeness.COMPLETE
                        ),
                        limitations=tuple(
                            sorted(
                                {limitation for item in variants for limitation in item.limitations}
                            )
                        ),
                    )
                )
    return tuple(endpoints)


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _node_text(source: bytes, node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _js_literal(source: bytes, node) -> str | None:
    text = _node_text(source, node)
    if node.type == "string":
        try:
            value = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return None
        return value if isinstance(value, str) else None
    if node.type == "template_string":
        inner = text[1:-1]
        if "${" not in inner:
            return inner
        normalized = _TEMPLATE_EXPRESSION.sub("{}", inner)
        return normalized if "${" not in normalized else None
    return None


def _js_static_urls(source: bytes, node, bindings: dict[str, object]) -> tuple[str, ...]:
    """Resolve a URL's fixed prefix while leaving dynamic values opaque.

    Query values are deliberately not required to be static: endpoint identity is
    the path before ``?``, so a literal path with interpolated query values is
    still complete route evidence.
    """

    literal = _js_literal(source, node)
    if literal is not None:
        return (literal,)
    if node.type == "identifier":
        target = bindings.get(_node_text(source, node))
        return _js_static_urls(source, target, bindings) if target is not None else ()
    if node.type == "ternary_expression":
        branches = node.named_children[1:]
        return tuple(
            sorted(
                {
                    value
                    for branch in branches
                    for value in _js_static_urls(source, branch, bindings)
                }
            )
        )
    if node.type != "binary_expression":
        return ()
    operator = node.child_by_field_name("operator")
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if operator is None or _node_text(source, operator) != "+" or left is None or right is None:
        return ()
    left_values = _js_static_urls(source, left, bindings)
    right_values = _js_static_urls(source, right, bindings)
    if not left_values:
        return ()
    # A fixed path/query prefix is sufficient.  Do not fabricate dynamic text.
    if not right_values:
        return left_values
    return tuple(
        sorted(
            {left_value + right_value for left_value in left_values for right_value in right_values}
        )
    )


def _js_static_url(source: bytes, node, bindings: dict[str, object]) -> str | None:
    values = _js_static_urls(source, node, bindings)
    return values[0] if len(values) == 1 else None


def _js_parameter_names(parameters, source: bytes) -> set[str]:
    names: set[str] = set()
    for item in parameters.named_children:
        if item.type == "identifier":
            names.add(_node_text(source, item))
        elif item.type == "object_pattern":
            for child in item.named_children:
                if child.type == "shorthand_property_identifier_pattern":
                    names.add(_node_text(source, child))
                elif child.type == "pair_pattern":
                    value = child.child_by_field_name("value")
                    if value is not None and value.type == "identifier":
                        names.add(_node_text(source, value))
    return names


def _js_transport_routes(source: bytes, node, bindings: dict[str, object]) -> tuple[str, ...]:
    if node.type != "object":
        return _js_static_urls(source, node, bindings)
    pairs = dict(_js_object_pairs(source, node))
    for key in ("path", "url"):
        if key in pairs:
            return _js_static_urls(source, pairs[key], bindings)
    return ()


def _js_bindings(root, source: bytes) -> dict[str, object]:
    bindings: dict[str, object] = {}
    for node in _walk(root):
        if node.type not in {"variable_declarator", "assignment_expression"}:
            continue
        name = node.child_by_field_name("name") or node.child_by_field_name("left")
        value = node.child_by_field_name("value") or node.child_by_field_name("right")
        if name is not None and value is not None and name.type == "identifier":
            bindings[_node_text(source, name)] = value
    return bindings


def _js_fetch_wrappers(root, source: bytes) -> dict[str, str]:
    """Return local wrapper name -> URL parameter for direct ``fetch`` wrappers."""

    wrappers: dict[str, str] = {}
    for node in _walk(root):
        if node.type not in {"function_declaration", "arrow_function"}:
            continue
        name = node.child_by_field_name("name")
        if name is None and node.parent is not None and node.parent.type == "variable_declarator":
            name = node.parent.child_by_field_name("name")
        parameters = node.child_by_field_name("parameters")
        if name is None or parameters is None:
            continue
        parameter_names = _js_parameter_names(parameters, source)
        for child in _walk(node):
            if child.type != "call_expression":
                continue
            function = child.child_by_field_name("function")
            arguments = child.child_by_field_name("arguments")
            if (
                function is None
                or arguments is None
                or _node_text(source, function) != "fetch"
                or not arguments.named_children
                or arguments.named_children[0].type != "identifier"
            ):
                continue
            parameter = _node_text(source, arguments.named_children[0])
            if parameter in parameter_names:
                wrappers[_node_text(source, name)] = parameter
    return wrappers


def _js_wrapper_targets(root, source: bytes) -> dict[str, str]:
    """Return local one-argument wrappers and their immediate call targets."""

    targets: dict[str, str] = {}
    for node in _walk(root):
        if node.type not in {"function_declaration", "arrow_function"}:
            continue
        name = node.child_by_field_name("name")
        if name is None and node.parent is not None and node.parent.type == "variable_declarator":
            name = node.parent.child_by_field_name("name")
        parameters = node.child_by_field_name("parameters")
        if name is None or parameters is None:
            continue
        parameter_names = _js_parameter_names(parameters, source)
        for child in _walk(node):
            if child.type != "call_expression":
                continue
            function = child.child_by_field_name("function")
            arguments = child.child_by_field_name("arguments")
            if function is None or arguments is None or not arguments.named_children:
                continue
            if arguments.named_children[0].type != "identifier":
                continue
            if _node_text(source, arguments.named_children[0]) not in parameter_names:
                continue
            target = _node_text(source, function)
            if target == "fetch" or target.isidentifier():
                targets[_node_text(source, name)] = target
    return targets


def _js_wrapper_implementation_call(node, source: bytes, wrapper_targets: dict[str, str]) -> bool:
    """Whether a call forwards a wrapper parameter rather than naming an endpoint."""

    arguments = node.child_by_field_name("arguments")
    if (
        arguments is None
        or not arguments.named_children
        or arguments.named_children[0].type != "identifier"
    ):
        return False
    argument_name = _node_text(source, arguments.named_children[0])
    parent = node.parent
    while parent is not None:
        if parent.type in {"function_declaration", "arrow_function"}:
            name = parent.child_by_field_name("name")
            if (
                name is None
                and parent.parent is not None
                and parent.parent.type == "variable_declarator"
            ):
                name = parent.parent.child_by_field_name("name")
            parameters = parent.child_by_field_name("parameters")
            if (
                name is not None
                and _node_text(source, name) in wrapper_targets
                and parameters is not None
                and argument_name in _js_parameter_names(parameters, source)
            ):
                return True
        parent = parent.parent
    return False


def _js_import_targets(
    context: AnalysisContext, file_path: Path, root, source: bytes
) -> dict[str, tuple[Path, str]]:
    """Resolve only literal relative ESM named imports inside the analysis roots."""

    imports: dict[str, tuple[Path, str]] = {}
    source_root = file_path.parent
    known_suffixes = (".js", ".mjs", ".cjs", ".jsx")
    for node in root.named_children:
        if node.type != "import_statement":
            continue
        source_node = next((item for item in node.named_children if item.type == "string"), None)
        raw_target = _js_literal(source, source_node) if source_node is not None else None
        if raw_target is None or not raw_target.startswith("."):
            continue
        base = (source_root / raw_target).resolve()
        candidates = (
            (base,)
            if base.suffix in known_suffixes
            else tuple(
                [base.with_suffix(suffix) for suffix in known_suffixes]
                + [base / f"index{suffix}" for suffix in known_suffixes]
            )
        )
        target = next((item for item in candidates if item.is_file()), None)
        if target is None or not any(
            target == root_dir or root_dir in target.parents
            for root_dir in context.source.source_roots
        ):
            continue
        named_imports = next((item for item in _walk(node) if item.type == "named_imports"), None)
        if named_imports is None:
            continue
        for specifier in named_imports.named_children:
            if specifier.type != "import_specifier":
                continue
            names = [item for item in specifier.named_children if item.type == "identifier"]
            if not names:
                continue
            imported = _node_text(source, names[0])
            local = _node_text(source, names[-1])
            imports[local] = (target, imported)
    return imports


def _js_is_local_transport_wrapper(
    context: AnalysisContext,
    parser,
    file_path: Path,
    name: str,
    visited: set[tuple[Path, str]],
) -> bool:
    identity = (file_path, name)
    if identity in visited:
        return False
    visited.add(identity)
    try:
        source = file_path.read_bytes()
    except OSError:
        return False
    root = parser.parse(source).root_node
    if root.has_error:
        return False
    target = _js_wrapper_targets(root, source).get(name)
    if target == "fetch":
        return True
    if target is not None and target.isidentifier():
        return _js_is_local_transport_wrapper(context, parser, file_path, target, visited)
    imported = _js_import_targets(context, file_path, root, source).get(name)
    if imported is None:
        return False
    imported_path, imported_name = imported
    return _js_is_local_transport_wrapper(context, parser, imported_path, imported_name, visited)


def _js_object_pairs(source: bytes, node) -> tuple[tuple[str, object], ...]:
    if node.type != "object":
        return ()
    pairs: list[tuple[str, object]] = []
    for child in node.named_children:
        if child.type != "pair":
            continue
        key_node = child.child_by_field_name("key")
        value_node = child.child_by_field_name("value")
        if key_node is None or value_node is None:
            continue
        key = _js_literal(source, key_node) or _node_text(source, key_node)
        if key.isidentifier() or key.replace("-", "_").isidentifier():
            pairs.append((key, value_node))
    return tuple(pairs)


def _js_variant(source: bytes, node, evidence: Evidence) -> _VariantFact | None:
    target = node
    if node.type == "call_expression":
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is not None and _node_text(source, function) == "JSON.stringify" and arguments:
            named = arguments.named_children
            target = named[0] if named else node
    pairs = _js_object_pairs(source, target)
    if not pairs:
        return None
    fields = tuple(
        sorted(
            (
                _FieldFact(
                    name=name,
                    requiredness=RequirednessKind.ALWAYS,
                    allows_null=value.type == "null",
                    evidence=evidence,
                )
                for name, value in pairs
            ),
            key=lambda item: item.name,
        )
    )
    return _VariantFact(BoundaryClass.HTTP_REQUEST, fields, evidence)


def _javascript_endpoints(
    context: AnalysisContext, report: AnalysisReport, diagnostics: list[Diagnostic]
) -> tuple[_EndpointFact, ...]:
    try:
        import tree_sitter_javascript
        from tree_sitter import Language, Parser
    except ImportError:
        return ()
    records = {item.logical_path: item for item in report.source_files}
    parser = Parser(Language(tree_sitter_javascript.language()))
    endpoints: list[_EndpointFact] = []
    wrapper_cache: dict[tuple[Path, str], bool] = {}

    def is_local_transport_wrapper(file_path: Path, name: str) -> bool:
        identity = (file_path, name)
        cached = wrapper_cache.get(identity)
        if cached is not None:
            return cached
        resolved = _js_is_local_transport_wrapper(context, parser, file_path, name, set())
        wrapper_cache[identity] = resolved
        return resolved

    for file_path in iter_javascript_files(context):
        relative = logical_path(context, file_path)
        record = records.get(relative)
        if record is None:
            continue
        source = _bound_interface_source(file_path, record, diagnostics)
        if source is None:
            continue
        digest = record.source_sha256
        root = parser.parse(source).root_node
        if root.has_error:
            continue
        bindings = _js_bindings(root, source)
        wrapper_targets = _js_wrapper_targets(root, source)
        imports = _js_import_targets(context, file_path, root, source)
        event_names = tuple(
            sorted(
                {
                    value
                    for node in _walk(root)
                    if node.type == "call_expression"
                    and (function := node.child_by_field_name("function")) is not None
                    and _node_text(source, function).endswith(".addEventListener")
                    and (arguments := node.child_by_field_name("arguments")) is not None
                    and arguments.named_children
                    and (value := _js_literal(source, arguments.named_children[0])) is not None
                }
            )
        )
        for node in _walk(root):
            transport = None
            method: str | None = None
            routes: tuple[str, ...] = ()
            variants: list[_VariantFact] = []
            if node.type == "call_expression":
                function = node.child_by_field_name("function")
                arguments = node.child_by_field_name("arguments")
                if function is None or arguments is None:
                    continue
                named = arguments.named_children
                function_name = _node_text(source, function)
                is_direct_fetch = function_name == "fetch"
                is_wrapper_call = (
                    function_name in wrapper_targets
                    and is_local_transport_wrapper(file_path, function_name)
                ) or (
                    function_name in imports
                    and is_local_transport_wrapper(
                        imports[function_name][0],
                        imports[function_name][1],
                    )
                )
                if not is_direct_fetch and not is_wrapper_call:
                    continue
                if named and _js_wrapper_implementation_call(node, source, wrapper_targets):
                    # This is the wrapper implementation. Its literal callers
                    # supply the endpoint evidence.
                    continue
                routes = _js_transport_routes(source, named[0], bindings) if named else ()
                transport = InterfaceTransport.HTTP
                method = "GET"
                if len(named) > 1 and named[1].type == "object":
                    options = dict(_js_object_pairs(source, named[1]))
                    method_literal = (
                        _js_literal(source, options.get("method"))
                        if options.get("method")
                        else None
                    )
                    if method_literal is not None:
                        method = method_literal.upper()
                    body = options.get("body")
                    endpoint_evidence = _evidence(
                        path=relative,
                        digest=digest,
                        language=SourceLanguage.JAVASCRIPT,
                        start_line=node.start_point[0] + 1,
                        start_column=node.start_point[1],
                        end_line=node.end_point[0] + 1,
                        end_column=node.end_point[1],
                        observation="literal fetch consumer",
                    )
                    if body is not None and (
                        variant := _js_variant(source, body, endpoint_evidence)
                    ):
                        variants.append(variant)
            elif node.type == "new_expression":
                constructor = node.child_by_field_name("constructor")
                arguments = node.child_by_field_name("arguments")
                if constructor is None or arguments is None or not arguments.named_children:
                    continue
                name = _node_text(source, constructor)
                if name not in {"EventSource", "WebSocket"}:
                    continue
                routes = _js_transport_routes(source, arguments.named_children[0], bindings)
                transport = (
                    InterfaceTransport.SSE
                    if name == "EventSource"
                    else InterfaceTransport.WEBSOCKET
                )
            if transport is None:
                continue
            if not routes:
                diagnostics.append(
                    Diagnostic(
                        code="interface-route-unresolved",
                        level=DiagnosticLevel.WARNING,
                        message="JavaScript interface URL is not statically normalizable",
                        path=relative,
                        provider="native-interfaces",
                        language=SourceLanguage.JAVASCRIPT,
                    )
                )
                continue
            evidence = _evidence(
                path=relative,
                digest=digest,
                language=SourceLanguage.JAVASCRIPT,
                start_line=node.start_point[0] + 1,
                start_column=node.start_point[1],
                end_line=node.end_point[0] + 1,
                end_column=node.end_point[1],
                observation=f"literal {transport.value} consumer",
            )
            source_text = source.decode("utf-8")
            retry = tuple(item for item in ("setTimeout", "setInterval") if item in source_text)
            lifecycle = tuple(
                item for item in ("abort", "close", "removeEventListener") if item in source_text
            )
            javascript_module = javascript_module_name(context, file_path)
            for route in routes:
                endpoints.append(
                    _EndpointFact(
                        language=SourceLanguage.JAVASCRIPT,
                        module=javascript_module,
                        logical_name=f"{javascript_module}:{node.start_point[0] + 1}",
                        role=InterfaceRole.CONSUMER,
                        transport=transport,
                        method=method,
                        normalized_path=_normalize_path(route),
                        variants=tuple(variants),
                        status_codes=(),
                        error_codes=(),
                        event_names=event_names if transport is not InterfaceTransport.HTTP else (),
                        retry_observations=retry,
                        lifecycle_observations=lifecycle,
                        evidence=evidence,
                        completeness=Completeness.COMPLETE,
                        limitations=(
                            ("Retry and lifecycle observations are module-scoped syntax evidence.",)
                            if retry or lifecycle
                            else ()
                        ),
                    )
                )
    return tuple(endpoints)


def _public_records(
    facts: tuple[_EndpointFact, ...],
) -> tuple[tuple[InterfaceEndpoint, ...], tuple[InterfacePayloadVariant, ...]]:
    endpoints: list[InterfaceEndpoint] = []
    variants: list[InterfacePayloadVariant] = []
    for fact in facts:
        endpoint_id = _stable_id(
            "interface-endpoint",
            fact.language.value,
            fact.role.value,
            fact.transport.value,
            fact.method or "",
            fact.normalized_path,
            fact.module,
            fact.logical_name,
        )
        variant_ids: list[str] = []
        for position, variant in enumerate(fact.variants):
            variant_id = _stable_id(
                "interface-variant", endpoint_id, variant.boundary_class.value, str(position)
            )
            variant_ids.append(variant_id)
            variants.append(
                InterfacePayloadVariant(
                    variant_id=variant_id,
                    boundary_class=variant.boundary_class,
                    fields=tuple(
                        InterfacePayloadField(
                            name=item.name,
                            requiredness=item.requiredness,
                            allows_null=item.allows_null,
                            evidence=(item.evidence,),
                        )
                        for item in variant.fields
                    ),
                    evidence=(variant.evidence,),
                    completeness=variant.completeness,
                    limitations=variant.limitations,
                )
            )
        endpoints.append(
            InterfaceEndpoint(
                endpoint_id=endpoint_id,
                language=fact.language,
                module=fact.module,
                logical_name=fact.logical_name,
                role=fact.role,
                transport=fact.transport,
                method=fact.method,
                normalized_path=fact.normalized_path,
                payload_variant_ids=tuple(sorted(variant_ids)),
                status_codes=fact.status_codes,
                error_codes=fact.error_codes,
                event_names=fact.event_names,
                retry_observations=fact.retry_observations,
                lifecycle_observations=fact.lifecycle_observations,
                evidence=(fact.evidence,),
                completeness=fact.completeness,
                limitations=fact.limitations,
            )
        )
    return (
        tuple(sorted(endpoints, key=lambda item: item.endpoint_id)),
        tuple(sorted(variants, key=lambda item: item.variant_id)),
    )


def _correlate(
    endpoints: tuple[InterfaceEndpoint, ...],
    variants: tuple[InterfacePayloadVariant, ...],
) -> tuple[tuple[InterfaceCorrelation, ...], tuple[InterfaceMismatch, ...]]:
    by_variant = {item.variant_id: item for item in variants}
    correlations: list[InterfaceCorrelation] = []
    mismatches: list[InterfaceMismatch] = []
    producers = [item for item in endpoints if item.role is InterfaceRole.PRODUCER]
    consumers = [item for item in endpoints if item.role is InterfaceRole.CONSUMER]
    for producer in producers:
        for consumer in consumers:
            if (
                producer.transport is not consumer.transport
                or producer.normalized_path != consumer.normalized_path
                or producer.method != consumer.method
            ):
                continue
            producer_request = next(
                (
                    by_variant[item]
                    for item in producer.payload_variant_ids
                    if by_variant[item].boundary_class is BoundaryClass.HTTP_REQUEST
                ),
                None,
            )
            consumer_request = next(
                (
                    by_variant[item]
                    for item in consumer.payload_variant_ids
                    if by_variant[item].boundary_class is BoundaryClass.HTTP_REQUEST
                ),
                None,
            )
            mismatch_ids: list[str] = []
            matched_fields: tuple[str, ...] = ()
            unresolved_null_fields: list[str] = []
            if producer_request is not None and consumer_request is not None:
                producer_fields = {item.name: item for item in producer_request.fields}
                consumer_fields = {item.name: item for item in consumer_request.fields}
                matched_fields = tuple(sorted(set(producer_fields) & set(consumer_fields)))
                for field_name in sorted(set(producer_fields) ^ set(consumer_fields)):
                    mismatch_id = _stable_id(
                        "interface-mismatch",
                        producer.endpoint_id,
                        consumer.endpoint_id,
                        "request-field",
                        field_name,
                    )
                    mismatch_ids.append(mismatch_id)
                    mismatches.append(
                        InterfaceMismatch(
                            mismatch_id=mismatch_id,
                            kind=InterfaceMismatchKind.REQUEST_FIELD,
                            producer_endpoint_id=producer.endpoint_id,
                            consumer_endpoint_id=consumer.endpoint_id,
                            field_name=field_name,
                            explanation=(
                                "Request field is evidenced on only one side of the interface."
                            ),
                            evidence=(producer.evidence[0], consumer.evidence[0]),
                            confidence=Confidence.EXACT,
                            completeness=Completeness.COMPLETE,
                        )
                    )
                for field_name in matched_fields:
                    left = producer_fields[field_name]
                    right = consumer_fields[field_name]
                    if left.allows_null is None or right.allows_null is None:
                        unresolved_null_fields.append(field_name)
                        continue
                    if left.allows_null == right.allows_null:
                        continue
                    mismatch_id = _stable_id(
                        "interface-mismatch",
                        producer.endpoint_id,
                        consumer.endpoint_id,
                        "missing-null",
                        field_name,
                    )
                    mismatch_ids.append(mismatch_id)
                    mismatches.append(
                        InterfaceMismatch(
                            mismatch_id=mismatch_id,
                            kind=InterfaceMismatchKind.MISSING_NULL,
                            producer_endpoint_id=producer.endpoint_id,
                            consumer_endpoint_id=consumer.endpoint_id,
                            field_name=field_name,
                            explanation="Producer and consumer null assumptions differ.",
                            evidence=(left.evidence[0], right.evidence[0]),
                            confidence=Confidence.EXACT,
                            completeness=Completeness.COMPLETE,
                        )
                    )
            correlation_id = _stable_id(
                "interface-correlation", producer.endpoint_id, consumer.endpoint_id
            )
            correlations.append(
                InterfaceCorrelation(
                    correlation_id=correlation_id,
                    producer_endpoint_id=producer.endpoint_id,
                    consumer_endpoint_id=consumer.endpoint_id,
                    matched_field_names=matched_fields,
                    mismatch_ids=tuple(sorted(mismatch_ids)),
                    evidence=(producer.evidence[0], consumer.evidence[0]),
                    completeness=(
                        Completeness.PARTIAL if unresolved_null_fields else Completeness.COMPLETE
                    ),
                    limitations=tuple(
                        sorted(
                            {
                                "Static route correlation does not transfer authorization or "
                                "domain semantics.",
                                *(
                                    {
                                        "Nullability comparison is unresolved for fields: "
                                        + ", ".join(unresolved_null_fields)
                                        + "."
                                    }
                                    if unresolved_null_fields
                                    else set()
                                ),
                            }
                        )
                    ),
                )
            )
    return (
        tuple(sorted(correlations, key=lambda item: item.correlation_id)),
        tuple(sorted(mismatches, key=lambda item: item.mismatch_id)),
    )


def integrate_interface_report(context: AnalysisContext, report: AnalysisReport) -> AnalysisReport:
    diagnostics: list[Diagnostic] = []
    facts: tuple[_EndpointFact, ...] = ()
    if SourceLanguage.PYTHON in report.target.languages:
        facts += _python_endpoints(context, report, diagnostics)
    if SourceLanguage.JAVASCRIPT in report.target.languages:
        facts += _javascript_endpoints(context, report, diagnostics)
    endpoints, variants = _public_records(
        tuple(
            sorted(
                facts,
                key=lambda item: (
                    item.language.value,
                    item.module,
                    item.logical_name,
                    item.normalized_path,
                ),
            )
        )
    )
    correlations, mismatches = _correlate(endpoints, variants)
    partial_records = sum(
        item.completeness is Completeness.PARTIAL
        for records in (endpoints, variants, correlations, mismatches)
        for item in records
    )
    has_partial = bool(diagnostics or partial_records)
    state = SectionState.PARTIAL if has_partial else SectionState.COMPLETE
    section = SectionCoverage(
        section="interfaces",
        state=state,
        parsed=len(endpoints),
        unresolved=len(diagnostics) + partial_records,
        diagnostic_codes=tuple(sorted({item.code for item in diagnostics})),
        languages=tuple(sorted(report.target.languages, key=lambda item: item.value)),
    )
    sections = []
    replaced = False
    for existing in report.section_coverage:
        if existing.section == "interfaces":
            sections.append(section)
            replaced = True
        else:
            sections.append(existing)
    if not replaced:
        insertion = next(
            index + 1 for index, item in enumerate(sections) if item.section == "contracts"
        )
        sections.insert(insertion, section)
    completeness = (
        Completeness.PARTIAL
        if has_partial or report.completeness is Completeness.PARTIAL
        else report.completeness
    )
    return AnalysisReport.model_validate(
        {
            **report.model_dump(),
            "interface_endpoints": endpoints,
            "interface_payload_variants": variants,
            "interface_correlations": correlations,
            "interface_mismatches": mismatches,
            "section_coverage": tuple(sections),
            "diagnostics": tuple(
                sorted(
                    (*report.diagnostics, *diagnostics),
                    key=lambda item: (item.path or "", item.code),
                )
            ),
            "providers": (
                *report.providers,
                ProviderResult(
                    provider_id="native-interfaces",
                    state=ProviderState.PARTIAL if has_partial else ProviderState.SUCCESS,
                    version=report.tool_version,
                    duration_ms=0,
                    diagnostics=tuple(diagnostics),
                    observations=(
                        "boundary-classification",
                        "producer-consumer-correlation",
                        "payload-variant-comparison",
                    ),
                    completeness=(Completeness.PARTIAL if has_partial else Completeness.COMPLETE),
                ),
            ),
            "completeness": completeness,
            "limitations": tuple(
                sorted(
                    {
                        *report.limitations,
                        "Interface evidence is static and does not transfer authorization or "
                        "domain semantics.",
                    }
                )
            ),
            "summary": {
                **report.summary.model_dump(),
                "interface_endpoints": len(endpoints),
                "interface_payload_variants": len(variants),
                "interface_correlations": len(correlations),
                "interface_mismatches": len(mismatches),
            },
        }
    )
