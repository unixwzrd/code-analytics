"""Domain-owned deterministic projection builders."""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import PurePosixPath

from code_analysis.finding_suppression_models import FindingSuppression
from code_analysis.models import Module, SourceLanguage, Symbol
from code_analysis.projection_models import (
    ColumnAlignment,
    EdgeDirection,
    GraphCluster,
    GraphEdge,
    GraphNode,
    GraphProjection,
    HierarchyNode,
    HierarchyProjection,
    ProjectionMetric,
    ProjectionSelection,
    ScalarKind,
    SortDirection,
    TableColumn,
    TableProjection,
    TableRow,
    TableSort,
)
from code_analysis.projections._catalog import ProjectionBuildError, ProjectionName
from code_analysis.projections._shared import (
    _bounded_rows,
    _edge_id,
    _integer,
    _limitations,
    _projection_id,
    _projection_state,
    _report_input,
    _string,
    _table_base,
)
from code_analysis.report_models import AnalysisReport
from code_analysis.subject_findings import (
    SUBJECT_FINDING_SORT,
    SubjectFindingSelectionError,
    aggregate_subject_findings,
    subject_finding_columns,
    subject_finding_facts,
    unscoped_scope_limitation,
)

Report = AnalysisReport


def _health_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    rows: list[TableRow] = []
    for provider in report.providers:
        rows.append(
            TableRow(
                row_id=f"provider:{provider.provider_id}",
                cells=(
                    _string("provider"),
                    _string(provider.provider_id),
                    _string(provider.state.value),
                    _string(provider.completeness.value),
                    _string(", ".join(provider.observations)),
                ),
            )
        )
    for index, diagnostic in enumerate(report.diagnostics):
        rows.append(
            TableRow(
                row_id=f"diagnostic:{diagnostic.code}:{index:06d}",
                cells=(
                    _string("diagnostic"),
                    _string(diagnostic.code),
                    _string(diagnostic.level.value),
                    _string(report.completeness.value),
                    _string(diagnostic.message),
                ),
            )
        )
    rows.sort(key=lambda row: row.row_id)
    bounded, truncated = _bounded_rows(rows, selection)
    return TableProjection(
        **_table_base(
            report,
            ProjectionName.HEALTH,
            selection,
            "Analysis health and provenance",
            len(bounded),
            truncated,
        ),
        columns=(
            TableColumn(
                column_id="record-type",
                label="Record type",
                position=0,
                value_kind=ScalarKind.STRING,
            ),
            TableColumn(
                column_id="identity",
                label="Identity",
                position=1,
                value_kind=ScalarKind.STRING,
            ),
            TableColumn(
                column_id="state",
                label="State",
                position=2,
                value_kind=ScalarKind.STRING,
            ),
            TableColumn(
                column_id="completeness",
                label="Completeness",
                position=3,
                value_kind=ScalarKind.STRING,
            ),
            TableColumn(
                column_id="detail",
                label="Detail",
                position=4,
                value_kind=ScalarKind.STRING,
            ),
        ),
        rows=bounded,
    )


def _module_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    finding_counts = Counter(finding.module for finding in report.findings)
    records = sorted(
        (
            module
            for module in report.modules
            if not selection.languages or module.language in selection.languages
        ),
        key=lambda module: (
            -module.fan_in,
            -module.fan_out,
            module.name,
        ),
    )
    rows = [
        TableRow(
            row_id=module.name,
            cells=(
                _string(module.name),
                _string(module.path),
                _integer(module.line_count),
                _integer(module.fan_in),
                _integer(module.fan_out),
                _integer(finding_counts[module.name]),
                _string(module.parse_state.value),
            ),
        )
        for module in records
    ]
    bounded, truncated = _bounded_rows(rows, selection)
    return TableProjection(
        **_table_base(
            report,
            ProjectionName.MODULES,
            selection,
            "Module metrics",
            len(bounded),
            truncated,
        ),
        columns=(
            TableColumn(
                column_id="module",
                label="Module",
                position=0,
                value_kind=ScalarKind.STRING,
            ),
            TableColumn(
                column_id="path",
                label="Path",
                position=1,
                value_kind=ScalarKind.STRING,
            ),
            TableColumn(
                column_id="line-count",
                label="Lines",
                position=2,
                value_kind=ScalarKind.INTEGER,
                alignment=ColumnAlignment.RIGHT,
            ),
            TableColumn(
                column_id="fan-in",
                label="Fan in",
                position=3,
                value_kind=ScalarKind.INTEGER,
                alignment=ColumnAlignment.RIGHT,
            ),
            TableColumn(
                column_id="fan-out",
                label="Fan out",
                position=4,
                value_kind=ScalarKind.INTEGER,
                alignment=ColumnAlignment.RIGHT,
            ),
            TableColumn(
                column_id="findings",
                label="Findings",
                position=5,
                value_kind=ScalarKind.INTEGER,
                alignment=ColumnAlignment.RIGHT,
            ),
            TableColumn(
                column_id="parse-state",
                label="Parse state",
                position=6,
                value_kind=ScalarKind.STRING,
            ),
        ),
        rows=bounded,
        sort=(
            TableSort(column_id="fan-in", direction=SortDirection.DESCENDING),
            TableSort(column_id="fan-out", direction=SortDirection.DESCENDING),
            TableSort(column_id="module"),
        ),
    )


def _symbol_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    records = sorted(
        (
            (module, symbol)
            for module in report.modules
            if not selection.languages or module.language in selection.languages
            for symbol in module.symbols
        ),
        key=lambda item: (item[0].name, item[1].qualified_name),
    )
    rows = [
        TableRow(
            row_id=f"{module.name}:{symbol.qualified_name}",
            cells=(
                _string(module.name),
                _string(symbol.qualified_name),
                _string(symbol.name),
                _string(symbol.kind),
                _string(symbol.span.path),
                _integer(symbol.span.start_line),
                _integer(symbol.span.end_line),
                _string(", ".join(symbol.parameters)),
                _string(symbol.returns or ""),
                _string(", ".join(symbol.decorators)),
            ),
        )
        for module, symbol in records
    ]
    bounded, truncated = _bounded_rows(rows, selection)
    definitions = (
        ("module", "Module", ScalarKind.STRING),
        ("qualified-symbol", "Qualified symbol", ScalarKind.STRING),
        ("name", "Name", ScalarKind.STRING),
        ("kind", "Kind", ScalarKind.STRING),
        ("path", "Path", ScalarKind.STRING),
        ("start-line", "Start line", ScalarKind.INTEGER),
        ("end-line", "End line", ScalarKind.INTEGER),
        ("parameters", "Parameters", ScalarKind.STRING),
        ("returns", "Returns", ScalarKind.STRING),
        ("decorators", "Decorators", ScalarKind.STRING),
    )
    return TableProjection(
        **_table_base(
            report,
            ProjectionName.SYMBOLS,
            selection,
            "Symbol inventory",
            len(bounded),
            truncated,
        ),
        columns=tuple(
            TableColumn(
                column_id=column_id,
                label=label,
                position=position,
                value_kind=value_kind,
                alignment=(
                    ColumnAlignment.RIGHT
                    if value_kind is ScalarKind.INTEGER
                    else ColumnAlignment.LEFT
                ),
            )
            for position, (column_id, label, value_kind) in enumerate(definitions)
        ),
        rows=bounded,
        sort=(
            TableSort(column_id="module"),
            TableSort(column_id="qualified-symbol"),
        ),
    )


def _finding_suppression_cells(
    suppression: FindingSuppression | None,
) -> tuple[str, str, str, str]:
    if suppression is None:
        return ("not-suppressed", "", "", "")
    return (
        suppression.status.value,
        suppression.evaluated_on,
        suppression.expires_on,
        suppression.rationale,
    )


def _finding_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    severity_rank = {"error": 3, "warning": 2, "info": 1}
    module_languages = {item.name: item.language for item in report.modules}
    findings = tuple(
        item
        for item in report.findings
        if not selection.languages or module_languages.get(item.module) in selection.languages
    )
    findings = sorted(
        findings,
        key=lambda finding: (
            -severity_rank[finding.severity],
            finding.module,
            finding.rule_id,
            finding.finding_id,
        ),
    )
    rows = [
        TableRow(
            row_id=finding.finding_id,
            cells=(
                _string(finding.finding_id),
                _integer(severity_rank[finding.severity]),
                _string(finding.severity),
                _string(finding.category),
                _string(finding.rule_id),
                _string(finding.module),
                _string(finding.symbol or ""),
                _string(finding.title),
                _string(finding.explanation),
                *(_string(value) for value in _finding_suppression_cells(finding.suppression)),
            ),
        )
        for finding in findings
    ]
    bounded, truncated = _bounded_rows(rows, selection)
    definitions = (
        ("finding-id", "Finding ID", ScalarKind.STRING),
        ("severity-rank", "Severity rank", ScalarKind.INTEGER),
        ("severity", "Severity", ScalarKind.STRING),
        ("category", "Category", ScalarKind.STRING),
        ("rule-id", "Rule ID", ScalarKind.STRING),
        ("module", "Module", ScalarKind.STRING),
        ("symbol", "Symbol", ScalarKind.STRING),
        ("title", "Title", ScalarKind.STRING),
        ("explanation", "Explanation", ScalarKind.STRING),
        ("suppression-status", "Suppression status", ScalarKind.STRING),
        ("suppression-evaluated-on", "Suppression evaluated on", ScalarKind.STRING),
        ("suppression-expires-on", "Suppression expires on", ScalarKind.STRING),
        ("suppression-rationale", "Suppression rationale", ScalarKind.STRING),
    )
    return TableProjection(
        **_table_base(
            report,
            ProjectionName.FINDINGS,
            selection,
            "Findings for refactor review",
            len(bounded),
            truncated,
        ),
        columns=tuple(
            TableColumn(
                column_id=column_id,
                label=label,
                position=position,
                value_kind=value_kind,
                alignment=(
                    ColumnAlignment.RIGHT
                    if value_kind is ScalarKind.INTEGER
                    else ColumnAlignment.LEFT
                ),
            )
            for position, (column_id, label, value_kind) in enumerate(definitions)
        ),
        rows=bounded,
        sort=(
            TableSort(column_id="severity-rank", direction=SortDirection.DESCENDING),
            TableSort(column_id="module"),
            TableSort(column_id="rule-id"),
            TableSort(column_id="finding-id"),
        ),
    )


def _subject_finding_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    facts = subject_finding_facts(report)
    try:
        aggregate = aggregate_subject_findings(facts, selection)
    except SubjectFindingSelectionError as exc:
        raise ProjectionBuildError(str(exc)) from exc
    bounded, truncated = _bounded_rows(list(aggregate.rows), selection)
    base = _table_base(
        report,
        ProjectionName.SUBJECT_FINDINGS,
        selection,
        "Findings grouped by analysis subject",
        len(bounded),
        truncated,
    )
    if aggregate.unscoped_finding_count:
        base["limitations"] = tuple(
            sorted(
                {
                    *base["limitations"],
                    unscoped_scope_limitation(aggregate.unscoped_finding_count),
                }
            )
        )
    return TableProjection(
        **base,
        columns=subject_finding_columns(),
        rows=bounded,
        sort=SUBJECT_FINDING_SORT,
    )


def _module_hierarchy(module: Module) -> tuple[HierarchyNode, ...]:
    if module.language is SourceLanguage.PYTHON:
        parts = module.name.split(".")
        groups = [".".join(parts[:index]) for index in range(1, len(parts))]
    else:
        parts = list(PurePosixPath(module.name).parts)
        groups = [
            f"hierarchy-group:{module.language.value}:{'/'.join(parts[:index])}"
            for index in range(1, len(parts))
        ]
    identities = [*groups, module.name]
    return tuple(
        HierarchyNode(
            node_id=identity,
            parent_id=identities[index - 1] if index else None,
            label=parts[index],
            kind="module" if index == len(identities) - 1 else "package",
        )
        for index, identity in enumerate(identities)
    )


def _hierarchy_projection(report: Report, selection: ProjectionSelection) -> HierarchyProjection:
    modules = tuple(
        module
        for module in report.modules
        if not selection.languages or module.language in selection.languages
    )
    if len({module.name for module in modules}) != len(modules):
        raise ProjectionBuildError("hierarchy has ambiguous recorded module identities")
    all_nodes: dict[str, HierarchyNode] = {}
    depths: dict[str, int] = {}
    for module in modules:
        for depth, node in enumerate(_module_hierarchy(module)):
            previous = all_nodes.get(node.node_id)
            if previous is not None and previous.parent_id != node.parent_id:
                raise ProjectionBuildError("hierarchy has conflicting recorded parent identities")
            if previous is None or node.kind == "module":
                all_nodes[node.node_id] = node
            depths[node.node_id] = depth
    ordered = sorted(all_nodes, key=lambda identity: (depths[identity], identity))
    selected_ids = set(ordered[: selection.max_records])
    truncated = len(selected_ids) < len(all_nodes)
    nodes = tuple(all_nodes[identity] for identity in sorted(selected_ids))
    source = _report_input(report)
    return HierarchyProjection(
        projection_id=_projection_id(source, ProjectionName.HIERARCHY, selection),
        title="Package and module hierarchy",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, truncated),
        record_count=len(nodes),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=_limitations(report, truncated),
        nodes=nodes,
    )


def _complexity_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    records: list[tuple[Module, Symbol]] = []
    for module in report.modules:
        for symbol in module.symbols:
            if symbol.complexity is not None:
                records.append((module, symbol))
    records.sort(
        key=lambda item: (
            -item[1].complexity.weighted_score,
            -item[1].complexity.physical_loc,
            item[0].name,
            item[1].qualified_name,
        )
    )
    rows = [
        TableRow(
            row_id=f"{module.name}:{symbol.qualified_name}",
            cells=(
                _string(module.name),
                _string(symbol.qualified_name),
                _string(symbol.kind),
                _string(symbol.span.path),
                _integer(symbol.span.start_line),
                _integer(symbol.complexity.physical_loc),
                _integer(symbol.complexity.branches),
                _integer(symbol.complexity.nesting_depth),
                _integer(symbol.complexity.argument_count),
                _integer(symbol.complexity.cyclomatic_complexity),
                _integer(symbol.complexity.weighted_score),
            ),
        )
        for module, symbol in records
    ]
    bounded, truncated = _bounded_rows(rows, selection)
    integer_columns = {
        "line",
        "physical-loc",
        "branches",
        "nesting",
        "arguments",
        "cyclomatic",
        "weighted-score",
    }
    definitions = (
        ("module", "Module"),
        ("symbol", "Symbol"),
        ("kind", "Kind"),
        ("path", "Path"),
        ("line", "Line"),
        ("physical-loc", "Physical LOC"),
        ("branches", "Branches"),
        ("nesting", "Nesting"),
        ("arguments", "Arguments"),
        ("cyclomatic", "Cyclomatic"),
        ("weighted-score", "Weighted score"),
    )
    return TableProjection(
        **_table_base(
            report,
            ProjectionName.COMPLEXITY,
            selection,
            "Complexity hotspots",
            len(bounded),
            truncated,
        ),
        columns=tuple(
            TableColumn(
                column_id=column_id,
                label=label,
                position=position,
                value_kind=(
                    ScalarKind.INTEGER if column_id in integer_columns else ScalarKind.STRING
                ),
                alignment=(
                    ColumnAlignment.RIGHT if column_id in integer_columns else ColumnAlignment.LEFT
                ),
            )
            for position, (column_id, label) in enumerate(definitions)
        ),
        rows=bounded,
        sort=(
            TableSort(column_id="weighted-score", direction=SortDirection.DESCENDING),
            TableSort(column_id="physical-loc", direction=SortDirection.DESCENDING),
            TableSort(column_id="module"),
            TableSort(column_id="symbol"),
        ),
    )


def _scc_id(modules: tuple[str, ...]) -> str:
    digest = hashlib.sha256("\0".join(modules).encode()).hexdigest()[:12]
    return f"scc-{digest}"


def _cycle_id(modules: tuple[str, ...]) -> str:
    digest = hashlib.sha256("\0".join(modules).encode()).hexdigest()[:12]
    return f"cycle-{digest}"


def _graph_node(module: Module, finding_count: int, group_id: str | None) -> GraphNode:
    return GraphNode(
        node_id=module.name,
        label=module.name,
        kind="module",
        group_id=group_id,
        metrics=(
            ProjectionMetric(name="fan-in", value=_integer(module.fan_in)),
            ProjectionMetric(name="fan-out", value=_integer(module.fan_out)),
            ProjectionMetric(name="findings", value=_integer(finding_count)),
            ProjectionMetric(name="line-count", value=_integer(module.line_count)),
        ),
    )


def _dependency_projection(report: Report, selection: ProjectionSelection) -> GraphProjection:
    finding_counts = Counter(finding.module for finding in report.findings)
    eligible_modules = tuple(
        module
        for module in report.modules
        if not selection.languages or module.language in selection.languages
    )
    eligible_names = {module.name for module in eligible_modules}
    eligible_edges = tuple(
        edge
        for edge in report.import_edges
        if edge.source in eligible_names and edge.target in eligible_names
    )
    cluster_by_node: dict[str, str] = {}
    clusters: list[GraphCluster] = []
    for component in report.strongly_connected_components:
        retained_modules = tuple(item for item in component.modules if item in eligible_names)
        if len(retained_modules) < 2:
            continue
        cluster_id = _scc_id(retained_modules)
        clusters.append(
            GraphCluster(
                cluster_id=cluster_id,
                label="Strongly connected component",
                node_ids=retained_modules,
            )
        )
        cluster_by_node.update(dict.fromkeys(retained_modules, cluster_id))
    ranked = sorted(
        eligible_modules,
        key=lambda module: (
            -(module.fan_in + module.fan_out),
            -finding_counts[module.name],
            module.name,
        ),
    )
    if len(eligible_modules) + len(eligible_edges) <= selection.max_records:
        selected_names = eligible_names
    else:
        node_limit = max(1, selection.max_records // 2)
        selected_names = {module.name for module in ranked[:node_limit]}
    retained_clusters = tuple(
        sorted(
            (cluster for cluster in clusters if set(cluster.node_ids) <= selected_names),
            key=lambda cluster: cluster.cluster_id,
        )
    )
    retained_cluster_ids = {cluster.cluster_id for cluster in retained_clusters}
    nodes = sorted(
        (
            _graph_node(
                module,
                finding_counts[module.name],
                (
                    cluster_by_node.get(module.name)
                    if cluster_by_node.get(module.name) in retained_cluster_ids
                    else None
                ),
            )
            for module in eligible_modules
            if module.name in selected_names
        ),
        key=lambda node: node.node_id,
    )
    all_edges = sorted(
        (
            GraphEdge(
                edge_id=_edge_id(edge.source, edge.target, edge.kind),
                source_id=edge.source,
                target_id=edge.target,
                kind=edge.kind,
            )
            for edge in report.import_edges
            if edge.source in selected_names and edge.target in selected_names
        ),
        key=lambda edge: edge.edge_id,
    )
    edge_limit = max(0, selection.max_records - len(nodes))
    edges = all_edges[:edge_limit]
    eligible_edge_count = sum(
        edge.source in selected_names and edge.target in selected_names for edge in eligible_edges
    )
    truncated = len(nodes) < len(eligible_modules) or len(edges) < eligible_edge_count
    source = _report_input(report)
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.DEPENDENCIES, selection),
        title="Module dependency graph",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, truncated),
        record_count=len(nodes) + len(edges),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=_limitations(report, truncated),
        direction=EdgeDirection.DIRECTED,
        nodes=tuple(nodes),
        edges=tuple(edges),
        clusters=retained_clusters,
    )


def _dependency_neighborhood_projection(
    report: Report, selection: ProjectionSelection
) -> GraphProjection:
    root = selection.selected_ids[0]
    module_by_name = {module.name: module for module in report.modules}
    if root not in module_by_name:
        raise ProjectionBuildError(f"selected module is not present in the report: {root}")
    assert selection.depth is not None
    known_names = set(module_by_name)
    eligible_edges = tuple(
        edge
        for edge in report.import_edges
        if edge.source in known_names
        and edge.target in known_names
        and (not selection.edge_kinds or edge.kind in selection.edge_kinds)
    )
    adjacency: dict[str, set[str]] = {name: set() for name in known_names}
    for edge in eligible_edges:
        adjacency[edge.source].add(edge.target)
        adjacency[edge.target].add(edge.source)
    distances = {root: 0}
    frontier = {root}
    for current_depth in range(selection.depth):
        next_frontier = {
            neighbor
            for module_name in frontier
            for neighbor in adjacency[module_name]
            if neighbor not in distances
        }
        for module_name in next_frontier:
            distances[module_name] = current_depth + 1
        frontier = next_frontier
        if not frontier:
            break

    finding_counts = Counter(finding.module for finding in report.findings)
    ranked_names = sorted(
        distances,
        key=lambda name: (
            distances[name],
            -(module_by_name[name].fan_in + module_by_name[name].fan_out),
            name,
        ),
    )
    full_edge_values = tuple(
        edge for edge in eligible_edges if edge.source in distances and edge.target in distances
    )
    if len(ranked_names) + len(full_edge_values) <= selection.max_records:
        retained_names = set(ranked_names)
    else:
        node_limit = max(1, selection.max_records // 2)
        retained_names = set(ranked_names[:node_limit])

    cluster_by_node: dict[str, str] = {}
    clusters: list[GraphCluster] = []
    for component in report.strongly_connected_components:
        cluster_id = _scc_id(component.modules)
        clusters.append(
            GraphCluster(
                cluster_id=cluster_id,
                label="Strongly connected component",
                node_ids=component.modules,
            )
        )
        cluster_by_node.update(dict.fromkeys(component.modules, cluster_id))
    retained_clusters = tuple(
        sorted(
            (cluster for cluster in clusters if set(cluster.node_ids) <= retained_names),
            key=lambda cluster: cluster.cluster_id,
        )
    )
    retained_cluster_ids = {cluster.cluster_id for cluster in retained_clusters}
    nodes = tuple(
        sorted(
            (
                _graph_node(
                    module_by_name[name],
                    finding_counts[name],
                    (
                        cluster_by_node.get(name)
                        if cluster_by_node.get(name) in retained_cluster_ids
                        else None
                    ),
                )
                for name in retained_names
            ),
            key=lambda node: node.node_id,
        )
    )
    all_edges = sorted(
        (
            GraphEdge(
                edge_id=_edge_id(edge.source, edge.target, edge.kind),
                source_id=edge.source,
                target_id=edge.target,
                kind=edge.kind,
            )
            for edge in full_edge_values
            if edge.source in retained_names and edge.target in retained_names
        ),
        key=lambda edge: edge.edge_id,
    )
    edge_limit = max(0, selection.max_records - len(nodes))
    edges = tuple(all_edges[:edge_limit])
    truncated = len(retained_names) < len(ranked_names) or len(edges) < len(full_edge_values)
    source = _report_input(report)
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.DEPENDENCY_NEIGHBORHOOD, selection),
        title=f"Dependency neighborhood: {root}",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, truncated),
        record_count=len(nodes) + len(edges),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=_limitations(report, truncated),
        direction=EdgeDirection.DIRECTED,
        nodes=nodes,
        edges=edges,
        clusters=retained_clusters,
    )


def _scc_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    import_edges = {(edge.source, edge.target) for edge in report.import_edges}
    cycle_sets = [set(cycle.modules[:-1]) for cycle in report.cycles if len(cycle.modules) >= 2]
    records = sorted(
        report.strongly_connected_components,
        key=lambda component: (-len(component.modules), _scc_id(component.modules)),
    )
    rows = []
    for component in records:
        members = set(component.modules)
        rows.append(
            TableRow(
                row_id=_scc_id(component.modules),
                cells=(
                    _string(_scc_id(component.modules)),
                    _integer(len(component.modules)),
                    _string(", ".join(component.modules)),
                    _integer(
                        sum(
                            source in members and target in members
                            for source, target in import_edges
                        )
                    ),
                    _integer(sum(cycle_members <= members for cycle_members in cycle_sets)),
                ),
            )
        )
    bounded, truncated = _bounded_rows(rows, selection)
    return TableProjection(
        **_table_base(
            report,
            ProjectionName.SCCS,
            selection,
            "Strongly connected components",
            len(bounded),
            truncated,
        ),
        columns=(
            TableColumn(
                column_id="scc-id",
                label="SCC ID",
                position=0,
                value_kind=ScalarKind.STRING,
            ),
            TableColumn(
                column_id="module-count",
                label="Modules",
                position=1,
                value_kind=ScalarKind.INTEGER,
                alignment=ColumnAlignment.RIGHT,
            ),
            TableColumn(
                column_id="members",
                label="Members",
                position=2,
                value_kind=ScalarKind.STRING,
            ),
            TableColumn(
                column_id="internal-edge-count",
                label="Internal edges",
                position=3,
                value_kind=ScalarKind.INTEGER,
                alignment=ColumnAlignment.RIGHT,
            ),
            TableColumn(
                column_id="cycle-count",
                label="Canonical cycles",
                position=4,
                value_kind=ScalarKind.INTEGER,
                alignment=ColumnAlignment.RIGHT,
            ),
        ),
        rows=bounded,
        sort=(
            TableSort(column_id="module-count", direction=SortDirection.DESCENDING),
            TableSort(column_id="scc-id"),
        ),
    )


def _cycle_projection(report: Report, selection: ProjectionSelection) -> GraphProjection:
    module_by_name = {module.name: module for module in report.modules}
    names = sorted({name for cycle in report.cycles for name in cycle.modules})
    finding_counts = Counter(finding.module for finding in report.findings)
    nodes = [_graph_node(module_by_name[name], finding_counts[name], None) for name in names]
    edge_values = {
        (source, target)
        for cycle in report.cycles
        for source, target in zip(cycle.modules, cycle.modules[1:], strict=False)
    }
    edges = sorted(
        (
            GraphEdge(
                edge_id=_edge_id(source, target, "cycle"),
                source_id=source,
                target_id=target,
                kind="cycle",
            )
            for source, target in edge_values
        ),
        key=lambda edge: edge.edge_id,
    )
    combined: list[GraphNode | GraphEdge] = [*nodes, *edges]
    truncated = len(combined) > selection.max_records
    if truncated:
        retained_nodes = nodes[: max(1, selection.max_records // 2)]
        retained_ids = {node.node_id for node in retained_nodes}
        retained_edges = [
            edge
            for edge in edges
            if edge.source_id in retained_ids and edge.target_id in retained_ids
        ][: max(0, selection.max_records - len(retained_nodes))]
        nodes, edges = retained_nodes, retained_edges
    source = _report_input(report)
    return GraphProjection(
        projection_id=_projection_id(source, ProjectionName.CYCLES, selection),
        title="Canonical dependency cycles",
        inputs=(source,),
        selection=selection,
        completeness=_projection_state(report, truncated),
        record_count=len(nodes) + len(edges),
        truncated=truncated,
        truncation_reason="record limit reached" if truncated else None,
        limitations=_limitations(report, truncated),
        direction=EdgeDirection.DIRECTED,
        nodes=tuple(nodes),
        edges=tuple(edges),
    )


def _cycle_list_projection(report: Report, selection: ProjectionSelection) -> TableProjection:
    scc_by_module = {
        module: _scc_id(component.modules)
        for component in report.strongly_connected_components
        for module in component.modules
    }
    records = sorted(
        report.cycles,
        key=lambda cycle: (-(len(cycle.modules) - 1), _cycle_id(cycle.modules)),
    )
    rows = [
        TableRow(
            row_id=_cycle_id(cycle.modules),
            cells=(
                _string(_cycle_id(cycle.modules)),
                _integer(max(0, len(cycle.modules) - 1)),
                _string(cycle.modules[0] if cycle.modules else ""),
                _string(" -> ".join(cycle.modules)),
                _string(scc_by_module.get(cycle.modules[0], "") if cycle.modules else ""),
            ),
        )
        for cycle in records
    ]
    bounded, truncated = _bounded_rows(rows, selection)
    return TableProjection(
        **_table_base(
            report,
            ProjectionName.CYCLE_LIST,
            selection,
            "Canonical dependency-cycle inventory",
            len(bounded),
            truncated,
        ),
        columns=(
            TableColumn(
                column_id="cycle-id",
                label="Cycle ID",
                position=0,
                value_kind=ScalarKind.STRING,
            ),
            TableColumn(
                column_id="module-count",
                label="Modules",
                position=1,
                value_kind=ScalarKind.INTEGER,
                alignment=ColumnAlignment.RIGHT,
            ),
            TableColumn(
                column_id="start-module",
                label="Canonical start",
                position=2,
                value_kind=ScalarKind.STRING,
            ),
            TableColumn(
                column_id="path",
                label="Directed path",
                position=3,
                value_kind=ScalarKind.STRING,
            ),
            TableColumn(
                column_id="scc-id",
                label="SCC ID",
                position=4,
                value_kind=ScalarKind.STRING,
            ),
        ),
        rows=bounded,
        sort=(
            TableSort(column_id="module-count", direction=SortDirection.DESCENDING),
            TableSort(column_id="cycle-id"),
        ),
    )
