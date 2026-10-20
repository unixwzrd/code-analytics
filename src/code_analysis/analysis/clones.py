"""Immutable native clone fingerprints and bounded inverted-index clustering."""

from __future__ import annotations

import ast
import hashlib
import io
import textwrap
import tokenize
from dataclasses import dataclass

from code_analysis.report_models import CloneKind


@dataclass(frozen=True, slots=True)
class CloneSource:
    module: str
    logical_path: str
    source_sha256: str
    source: str


@dataclass(frozen=True, slots=True)
class CloneFingerprint:
    kind: CloneKind
    logical_name: str
    module: str
    logical_path: str
    source_sha256: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    fingerprint_sha256: str
    structural_signature: str
    nonblank_lines: int
    token_count: int
    ast_node_count: int
    statement_count: int


@dataclass(frozen=True, slots=True)
class CloneGroup:
    kind: CloneKind
    fingerprint_sha256: str
    members: tuple[CloneFingerprint, ...]


@dataclass(frozen=True, slots=True)
class CloneDiscovery:
    fingerprints: tuple[CloneFingerprint, ...]
    groups: tuple[CloneGroup, ...]
    candidate_pairs: int
    truncated_pairs: int
    truncated_clusters: int
    truncated_members: int


def _suppress_contained_groups(groups: list[CloneGroup]) -> list[CloneGroup]:
    retained: list[CloneGroup] = []
    ordered = sorted(
        groups,
        key=lambda item: (
            item.kind.value,
            tuple(sorted({member.logical_name for member in item.members})),
            -sum(member.end_line - member.start_line + 1 for member in item.members),
            item.fingerprint_sha256,
        ),
    )
    for candidate in ordered:
        candidate_subjects = tuple(sorted({item.logical_name for item in candidate.members}))
        contained = False
        for outer in retained:
            if outer.kind is not candidate.kind:
                continue
            if tuple(sorted({item.logical_name for item in outer.members})) != candidate_subjects:
                continue
            outer_by_subject = {item.logical_name: item for item in outer.members}
            if all(
                member.logical_name in outer_by_subject
                and outer_by_subject[member.logical_name].start_line <= member.start_line
                and outer_by_subject[member.logical_name].end_line >= member.end_line
                for member in candidate.members
            ):
                contained = True
                break
        if not contained:
            retained.append(candidate)
    return sorted(retained, key=lambda item: (item.kind.value, item.fingerprint_sha256))


@dataclass(frozen=True, slots=True)
class _Body:
    logical_name: str
    module: str
    logical_path: str
    source_sha256: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    statements: tuple[ast.stmt, ...]
    source_lines: tuple[str, ...]


class _AstNormalizer(ast.NodeTransformer):
    def visit_Name(self, node: ast.Name) -> ast.AST:
        return ast.copy_location(ast.Name(id="NAME", ctx=node.ctx), node)

    def visit_arg(self, node: ast.arg) -> ast.AST:
        annotation = self.visit(node.annotation) if node.annotation is not None else None
        return ast.copy_location(ast.arg(arg="ARG", annotation=annotation), node)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        category = type(node.value).__name__.upper()
        return ast.copy_location(ast.Constant(value=f"<{category}>"), node)

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        return ast.copy_location(
            ast.Attribute(value=self.visit(node.value), attr="ATTRIBUTE", ctx=node.ctx), node
        )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _without_docstring(statements: list[ast.stmt]) -> tuple[ast.stmt, ...]:
    if statements and isinstance(statements[0], ast.Expr):
        value = statements[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return tuple(statements[1:])
    return tuple(statements)


def _bodies(source: CloneSource, *, include_module_aggregates: bool = False) -> tuple[_Body, ...]:
    tree = ast.parse(source.source, filename=source.logical_path)
    lines = tuple(source.source.splitlines())
    bodies: list[_Body] = []

    def add(logical_name: str, statements: tuple[ast.stmt, ...]) -> None:
        if not statements:
            return
        first, last = statements[0], statements[-1]
        bodies.append(
            _Body(
                logical_name=logical_name,
                module=source.module,
                logical_path=source.logical_path,
                source_sha256=source.source_sha256,
                start_line=first.lineno,
                start_column=first.col_offset,
                end_line=last.end_lineno or last.lineno,
                end_column=last.end_col_offset or last.col_offset,
                statements=statements,
                source_lines=lines,
            )
        )

    if include_module_aggregates:
        module_statements = [
            statement
            for statement in tree.body
            if not isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        add(source.module, _without_docstring(module_statements))

    def walk(statements: list[ast.stmt], parents: tuple[str, ...]) -> None:
        for statement in statements:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = ".".join((*parents, statement.name))
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(f"{source.module}:{qualified}", _without_docstring(statement.body))
                walk(statement.body, (*parents, statement.name))

    walk(tree.body, ())
    return tuple(sorted(bodies, key=lambda item: (item.logical_name, item.start_line)))


def _text(body: _Body) -> str:
    selected = body.source_lines[body.start_line - 1 : body.end_line]
    return textwrap.dedent("\n".join(line.rstrip() for line in selected)).strip()


def _tokens(text: str) -> tuple[str, ...]:
    ignored = {
        tokenize.ENCODING,
        tokenize.COMMENT,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.ENDMARKER,
    }
    return tuple(
        f"{token.type}:{token.string}"
        for token in tokenize.tokenize(io.BytesIO(text.encode("utf-8")).readline)
        if token.type not in ignored
    )


def _normalized_ast(statements: tuple[ast.stmt, ...]) -> str:
    module = ast.Module(body=list(statements), type_ignores=[])
    normalized = _AstNormalizer().visit(module)
    return ast.dump(normalized, annotate_fields=True, include_attributes=False)


def _statement_fingerprints(statements: tuple[ast.stmt, ...]) -> tuple[str, ...]:
    return tuple(_digest(_normalized_ast((statement,))) for statement in statements)


def _control_call_events(statements: tuple[ast.stmt, ...]) -> tuple[str, ...]:
    events: list[str] = []
    control_names = {
        ast.If: "if",
        ast.For: "for",
        ast.AsyncFor: "async-for",
        ast.While: "while",
        ast.Try: "try",
        ast.With: "with",
        ast.AsyncWith: "async-with",
        ast.Match: "match",
        ast.Raise: "raise",
        ast.Return: "return",
    }

    class Visitor(ast.NodeVisitor):
        def visit(self, node: ast.AST) -> None:
            name = control_names.get(type(node))
            if name is not None:
                events.append(f"control:{name}")
            super().visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name):
                target = node.func.id
            elif isinstance(node.func, ast.Attribute):
                target = node.func.attr
            else:
                target = "dynamic"
            events.append(f"call:{target}")
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

    visitor = Visitor()
    for statement in statements:
        visitor.visit(statement)
    return tuple(events)


def _fingerprint(
    body: _Body,
    kind: CloneKind,
    value: str,
    signature: str,
    *,
    span: tuple[int, int, int, int] | None = None,
    statement_count: int | None = None,
) -> CloneFingerprint:
    text = _text(body)
    tokens = _tokens(text)
    nodes = sum(1 for statement in body.statements for _node in ast.walk(statement))
    start_line, start_column, end_line, end_column = span or (
        body.start_line,
        body.start_column,
        body.end_line,
        body.end_column,
    )
    return CloneFingerprint(
        kind=kind,
        logical_name=body.logical_name,
        module=body.module,
        logical_path=body.logical_path,
        source_sha256=body.source_sha256,
        start_line=start_line,
        start_column=start_column,
        end_line=end_line,
        end_column=end_column,
        fingerprint_sha256=_digest(value),
        structural_signature=signature,
        nonblank_lines=sum(bool(line.strip()) for line in text.splitlines()),
        token_count=len(tokens),
        ast_node_count=nodes,
        statement_count=statement_count if statement_count is not None else len(body.statements),
    )


def discover_clone_fingerprints(
    sources: tuple[CloneSource, ...],
    *,
    exact_min_lines: int,
    token_min_tokens: int,
    ast_min_nodes: int,
    statement_window: int,
    sequence_min_events: int,
    max_candidate_pairs: int,
    max_clusters: int,
    max_cluster_members: int,
    max_clone_window_statements: int = 64,
    include_module_aggregates: bool = False,
) -> CloneDiscovery:
    fingerprints: list[CloneFingerprint] = []
    for source in sorted(sources, key=lambda item: (item.module, item.logical_path)):
        fingerprints.extend(
            fingerprint_clone_source(
                source,
                exact_min_lines=exact_min_lines,
                token_min_tokens=token_min_tokens,
                ast_min_nodes=ast_min_nodes,
                statement_window=statement_window,
                sequence_min_events=sequence_min_events,
                max_clone_window_statements=max_clone_window_statements,
                include_module_aggregates=include_module_aggregates,
            )
        )

    return cluster_clone_fingerprints(
        tuple(fingerprints),
        max_candidate_pairs=max_candidate_pairs,
        max_clusters=max_clusters,
        max_cluster_members=max_cluster_members,
    )


def fingerprint_clone_source(
    source: CloneSource,
    *,
    exact_min_lines: int,
    token_min_tokens: int,
    ast_min_nodes: int,
    statement_window: int,
    sequence_min_events: int,
    max_clone_window_statements: int = 64,
    include_module_aggregates: bool = False,
) -> tuple[CloneFingerprint, ...]:
    """Produce immutable fingerprints for one content-addressed source unit."""

    fingerprints: list[CloneFingerprint] = []
    for body in _bodies(source, include_module_aggregates=include_module_aggregates):
        text = _text(body)
        token_values = _tokens(text)
        normalized_ast = _normalized_ast(body.statements)
        node_count = sum(1 for statement in body.statements for _node in ast.walk(statement))
        bounded_body = len(body.statements) <= max_clone_window_statements
        if (
            bounded_body
            and sum(bool(line.strip()) for line in text.splitlines()) >= exact_min_lines
        ):
            fingerprints.append(
                _fingerprint(body, CloneKind.EXACT_TEXT, text, "normalized-body-text:v1")
            )
        if bounded_body and len(token_values) >= token_min_tokens:
            fingerprints.append(
                _fingerprint(
                    body,
                    CloneKind.NORMALIZED_TOKEN,
                    "\0".join(token_values),
                    "python-significant-tokens:v1",
                )
            )
        if bounded_body and node_count >= ast_min_nodes:
            fingerprints.append(
                _fingerprint(
                    body,
                    CloneKind.NORMALIZED_AST,
                    normalized_ast,
                    "identifier-literal-category-ast:v1",
                )
            )
        statement_values = _statement_fingerprints(body.statements)
        if len(statement_values) >= statement_window:
            for index in range(len(statement_values) - statement_window + 1):
                window = statement_values[index : index + statement_window]
                first = body.statements[index]
                last = body.statements[index + statement_window - 1]
                fingerprints.append(
                    _fingerprint(
                        body,
                        CloneKind.STATEMENT_SEQUENCE,
                        "\0".join(window),
                        f"normalized-statement-window:v1:{statement_window}",
                        span=(
                            first.lineno,
                            first.col_offset,
                            last.end_lineno or last.lineno,
                            last.end_col_offset or last.col_offset,
                        ),
                        statement_count=statement_window,
                    )
                )
        events = _control_call_events(body.statements)
        if len(events) >= sequence_min_events:
            fingerprints.append(
                _fingerprint(
                    body,
                    CloneKind.CONTROL_CALL_SEQUENCE,
                    "\0".join(events),
                    f"control-call-sequence:v1:{len(events)}",
                )
            )
    return tuple(
        sorted(
            fingerprints,
            key=lambda item: (
                item.kind.value,
                item.logical_name,
                item.start_line,
                item.end_line,
                item.fingerprint_sha256,
            ),
        )
    )


def cluster_clone_fingerprints(
    fingerprints: tuple[CloneFingerprint, ...],
    *,
    max_candidate_pairs: int,
    max_clusters: int,
    max_cluster_members: int,
) -> CloneDiscovery:
    """Cluster precomputed fingerprints using a bounded inverted index."""

    index: dict[tuple[CloneKind, str], list[CloneFingerprint]] = {}
    for item in fingerprints:
        index.setdefault((item.kind, item.fingerprint_sha256), []).append(item)
    candidate_pairs = 0
    truncated_pairs = 0
    truncated_clusters = 0
    truncated_members = 0
    groups: list[CloneGroup] = []
    retained_fingerprints: set[CloneFingerprint] = set()
    for (kind, digest), raw_members in sorted(index.items(), key=lambda item: item[0]):
        members = tuple(
            sorted(
                raw_members,
                key=lambda item: (item.logical_name, item.start_line, item.end_line),
            )
        )
        distinct_subjects = {item.logical_name for item in members}
        if len(distinct_subjects) < 2:
            continue
        subject_count = len(distinct_subjects)
        prospective = subject_count * (subject_count - 1) // 2
        if candidate_pairs + prospective > max_candidate_pairs:
            truncated_pairs += prospective
            continue
        candidate_pairs += prospective
        if len(groups) >= max_clusters:
            truncated_clusters += 1
            continue
        if len(members) > max_cluster_members:
            truncated_members += len(members) - max_cluster_members
            members = members[:max_cluster_members]
        groups.append(CloneGroup(kind=kind, fingerprint_sha256=digest, members=members))
        retained_fingerprints.update(members)
    groups = _suppress_contained_groups(groups)
    retained_fingerprints = {member for group in groups for member in group.members}
    return CloneDiscovery(
        fingerprints=tuple(
            sorted(
                retained_fingerprints,
                key=lambda item: (
                    item.kind.value,
                    item.logical_name,
                    item.start_line,
                    item.end_line,
                    item.fingerprint_sha256,
                ),
            )
        ),
        groups=tuple(groups),
        candidate_pairs=candidate_pairs,
        truncated_pairs=truncated_pairs,
        truncated_clusters=truncated_clusters,
        truncated_members=truncated_members,
    )
