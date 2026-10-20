"""Deterministic text renderers for validated report projections."""

from __future__ import annotations

import errno
import hashlib
import json
import shutil
import subprocess
from enum import StrEnum

from code_analysis.projection_models import (
    ComparisonProjection,
    EdgeDirection,
    EdgeResolution,
    GraphNode,
    GraphProjection,
    HierarchyProjection,
    MatrixProjection,
    Projection,
    ProjectionScalar,
    ReviewPacketProjection,
    ScalarKind,
    TableProjection,
)
from code_analysis.reports.projection_json import render_projection_json


class ProjectionFormat(StrEnum):
    HTML = "html"
    JSON = "json"
    JSON_COMPACT = "json-compact"
    TSV = "tsv"
    MARKDOWN = "markdown"
    DOT = "dot"
    MERMAID = "mermaid"
    SVG = "svg"
    PNG = "png"


class ProjectionRenderError(ValueError):
    """The selected rendering is not valid for the projection kind."""


def render_projection(projection: Projection, output_format: ProjectionFormat) -> str:
    if output_format is ProjectionFormat.HTML:
        from code_analysis.reports.html_report import render_html_document

        return render_html_document(projection.title, (projection,))
    if output_format is ProjectionFormat.JSON:
        return render_projection_json(projection)
    if output_format is ProjectionFormat.JSON_COMPACT:
        return render_projection_json(projection, compact=True)
    if output_format is ProjectionFormat.TSV:
        if isinstance(projection, TableProjection):
            return render_tsv(projection)
        if isinstance(projection, ComparisonProjection):
            return render_comparison_tsv(projection)
        if isinstance(projection, MatrixProjection):
            return render_matrix_tsv(projection)
        raise ProjectionRenderError("TSV requires a table, matrix, or comparison projection")
    if output_format is ProjectionFormat.MARKDOWN:
        return render_projection_markdown(projection)
    if output_format is ProjectionFormat.DOT:
        if isinstance(projection, GraphProjection):
            return render_dot(projection)
        if isinstance(projection, HierarchyProjection):
            return render_hierarchy_dot(projection)
    elif output_format is ProjectionFormat.MERMAID:
        if isinstance(projection, GraphProjection):
            return render_mermaid(projection)
        if isinstance(projection, HierarchyProjection):
            return render_hierarchy_mermaid(projection)
    raise ProjectionRenderError(f"{output_format.value} requires a graph or hierarchy projection")


def render_projection_binary(projection: Projection, output_format: ProjectionFormat) -> bytes:
    if output_format not in {ProjectionFormat.SVG, ProjectionFormat.PNG}:
        raise ProjectionRenderError("binary rendering requires SVG or PNG")
    executable = shutil.which("dot")
    if executable is None:
        raise OSError(errno.ENOENT, "Graphviz dot provider is unavailable")
    dot = render_projection(projection, ProjectionFormat.DOT).encode()
    try:
        completed = subprocess.run(
            (executable, f"-T{output_format.value}"),
            input=dot,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError(errno.ETIMEDOUT, "Graphviz rendering exceeded 30 seconds") from exc
    if completed.returncode != 0 or not completed.stdout:
        raise OSError(errno.EPROTO, "Graphviz rendering failed")
    return completed.stdout


def _scalar_text(value: ProjectionScalar) -> str:
    if value.kind is ScalarKind.STRING:
        assert value.string_value is not None
        return value.string_value
    if value.kind is ScalarKind.INTEGER:
        assert value.integer_value is not None
        return str(value.integer_value)
    if value.kind is ScalarKind.NUMBER:
        assert value.number_value is not None
        return json.dumps(value.number_value, allow_nan=False)
    if value.kind is ScalarKind.BOOLEAN:
        assert value.boolean_value is not None
        return "true" if value.boolean_value else "false"
    return "null"


def _escape_tsv(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
    )


def render_tsv(projection: TableProjection) -> str:
    lines = ["\t".join(column.column_id for column in projection.columns)]
    lines.extend(
        "\t".join(_escape_tsv(_scalar_text(cell)) for cell in row.cells) for row in projection.rows
    )
    return "\n".join(lines) + "\n"


def render_comparison_tsv(projection: ComparisonProjection) -> str:
    lines = ["subject-id\tchange\tbaseline-sha256\tcandidate-sha256\texplanation"]
    lines.extend(
        "\t".join(
            _escape_tsv(value)
            for value in (
                record.subject_id,
                record.change.value,
                _scalar_text(record.baseline_value),
                _scalar_text(record.candidate_value),
                record.explanation,
            )
        )
        for record in projection.records
    )
    return "\n".join(lines) + "\n"


def render_matrix_tsv(projection: MatrixProjection) -> str:
    metric_names = sorted({metric.name for cell in projection.cells for metric in cell.metrics})
    lines = ["\t".join(("row-id", "column-id", *metric_names, "evidence-ids"))]
    for cell in projection.cells:
        metrics = {item.name: _scalar_text(item.value) for item in cell.metrics}
        lines.append(
            "\t".join(
                _escape_tsv(value)
                for value in (
                    cell.row_id,
                    cell.column_id,
                    *(metrics.get(name, "") for name in metric_names),
                    ", ".join(cell.evidence_ids),
                )
            )
        )
    return "\n".join(lines) + "\n"


def _escape_markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def render_projection_markdown(projection: Projection) -> str:
    lines = [f"# {projection.title}", ""]
    source = projection.inputs[0]
    lines.extend(
        [
            f"- Projection: `{projection.schema_name}@{projection.schema_version}`",
            f"- Source: `{source.report_schema_name}@{source.report_schema_version}`",
            f"- Source SHA-256: `{source.report_sha256}`",
            f"- Completeness: `{projection.completeness.value}`",
            f"- Records: {projection.record_count}",
            f"- Truncated: `{'true' if projection.truncated else 'false'}`",
            "",
        ]
    )
    if isinstance(projection, TableProjection):
        lines.append("| " + " | ".join(column.label for column in projection.columns) + " |")
        lines.append("| " + " | ".join("---" for _ in projection.columns) + " |")
        lines.extend(
            "| " + " | ".join(_escape_markdown(_scalar_text(cell)) for cell in row.cells) + " |"
            for row in projection.rows
        )
    elif isinstance(projection, GraphProjection):
        lines.extend(["## Nodes", "", "| ID | Kind | Label |", "| --- | --- | --- |"])
        lines.extend(
            f"| `{_escape_markdown(node.node_id)}` | "
            f"{_escape_markdown(node.kind)} | {_escape_markdown(node.label)} |"
            for node in projection.nodes
        )
        lines.extend(
            [
                "",
                "## Edges",
                "",
                "| Source | Kind | Target | Resolution |",
                "| --- | --- | --- | --- |",
            ]
        )
        lines.extend(
            f"| `{_escape_markdown(edge.source_id)}` | "
            f"{_escape_markdown(edge.kind)} | "
            f"`{_escape_markdown(edge.target_id)}` | {edge.resolution.value} |"
            for edge in projection.edges
        )
    elif isinstance(projection, MatrixProjection):
        metric_names = sorted({metric.name for cell in projection.cells for metric in cell.metrics})
        headers = ("Row", "Column", *metric_names)
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
        for cell in projection.cells:
            metrics = {item.name: _scalar_text(item.value) for item in cell.metrics}
            values = (
                cell.row_id,
                cell.column_id,
                *(metrics.get(name, "") for name in metric_names),
            )
            lines.append("| " + " | ".join(_escape_markdown(value) for value in values) + " |")
    elif isinstance(projection, HierarchyProjection):
        lines.extend(
            [
                "| ID | Parent | Kind | Label |",
                "| --- | --- | --- | --- |",
            ]
        )
        lines.extend(
            f"| `{_escape_markdown(node.node_id)}` | "
            f"{('`' + _escape_markdown(node.parent_id) + '`') if node.parent_id else ''} | "
            f"{_escape_markdown(node.kind)} | {_escape_markdown(node.label)} |"
            for node in projection.nodes
        )
    elif isinstance(projection, ComparisonProjection):
        lines.extend(
            [
                "- Selection mode: `"
                + ("all-records" if projection.includes_unchanged else "changes-only")
                + "`",
                f"- Eligible records: {projection.eligible_record_count}",
            ]
        )
        if projection.change_counts is not None:
            counts = projection.change_counts
            lines.extend(
                [
                    f"- Global changes: added {counts.added}, removed {counts.removed}, "
                    f"changed {counts.changed}, unchanged {counts.unchanged}",
                    "",
                ]
            )
        else:
            lines.append("")
        if projection.identical:
            lines.extend(["No canonical report changes detected.", ""])
        lines.extend(
            [
                "| Identity | Change | Baseline SHA-256 | Candidate SHA-256 | Explanation |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        lines.extend(
            "| "
            + " | ".join(
                (
                    f"`{_escape_markdown(record.subject_id)}`",
                    record.change.value,
                    _escape_markdown(_scalar_text(record.baseline_value)),
                    _escape_markdown(_scalar_text(record.candidate_value)),
                    _escape_markdown(record.explanation),
                )
            )
            + " |"
            for record in projection.records
        )
    elif isinstance(projection, ReviewPacketProjection):
        for item in projection.items:
            lines.extend(
                [
                    f"## {_escape_markdown(item.title)}",
                    "",
                    _escape_markdown(item.statement),
                    "",
                    f"- Identity: `{_escape_markdown(item.item_id)}`",
                    "- Evidence: "
                    + ", ".join(f"`{_escape_markdown(value)}`" for value in item.evidence_ids),
                ]
            )
            if item.counter_evidence_ids:
                lines.append(
                    "- Counter-evidence: "
                    + ", ".join(
                        f"`{_escape_markdown(value)}`" for value in item.counter_evidence_ids
                    )
                )
            if item.related_ids:
                lines.append(
                    "- Related identities: "
                    + ", ".join(f"`{_escape_markdown(value)}`" for value in item.related_ids)
                )
            if item.review_questions:
                lines.extend(["", "Review questions:", ""])
                lines.extend(f"- {_escape_markdown(value)}" for value in item.review_questions)
            lines.append("")
    else:
        raise ProjectionRenderError(
            f"Markdown rendering is not implemented for {projection.projection_kind}"
        )
    if projection.limitations:
        lines.extend(["", "## Limitations", ""])
        lines.extend(f"- {_escape_markdown(item)}" for item in projection.limitations)
    return "\n".join(lines) + "\n"


def _dot_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _node_label(node: GraphNode) -> str:
    metrics = "\\n".join(f"{metric.name}: {_scalar_text(metric.value)}" for metric in node.metrics)
    return node.label + (("\\n" + metrics) if metrics else "")


def render_dot(projection: GraphProjection) -> str:
    connector = "->" if projection.direction is EdgeDirection.DIRECTED else "--"
    graph_type = "digraph" if projection.direction is EdgeDirection.DIRECTED else "graph"
    lines = [
        f"{graph_type} projection {{",
        '  graph [rankdir="LR", bgcolor="transparent", overlap="false", splines="true"];',
        '  node [shape="box", style="rounded,filled", fillcolor="#eef4ff", '
        'color="#315a9b", fontname="Helvetica"];',
        '  edge [color="#718096", fontname="Helvetica", fontsize="9"];',
    ]
    clustered_nodes: set[str] = set()
    node_by_id = {node.node_id: node for node in projection.nodes}
    for cluster in projection.clusters:
        lines.extend(
            [
                f"  subgraph {_dot_quote('cluster:' + cluster.cluster_id)} {{",
                f"    label={_dot_quote(cluster.label)};",
                '    color="#b42318";',
            ]
        )
        for node_id in cluster.node_ids:
            node = node_by_id[node_id]
            lines.append(f"    {_dot_quote(node.node_id)} [label={_dot_quote(_node_label(node))}];")
            clustered_nodes.add(node_id)
        lines.append("  }")
    for node in projection.nodes:
        if node.node_id not in clustered_nodes:
            lines.append(f"  {_dot_quote(node.node_id)} [label={_dot_quote(_node_label(node))}];")
    styles = {
        EdgeResolution.RESOLVED: "solid",
        EdgeResolution.UNRESOLVED: "dashed",
        EdgeResolution.AMBIGUOUS: "dotted",
        EdgeResolution.TRUNCATED: "bold",
    }
    for edge in projection.edges:
        label = edge.kind + ((": " + edge.label) if edge.label else "")
        lines.append(
            f"  {_dot_quote(edge.source_id)} {connector} {_dot_quote(edge.target_id)} "
            f"[label={_dot_quote(label)}, style={_dot_quote(styles[edge.resolution])}];"
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _mermaid_id(value: str) -> str:
    return "n_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _mermaid_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', "&quot;").replace("\n", "<br/>")


def render_mermaid(projection: GraphProjection) -> str:
    connector = "-->" if projection.direction is EdgeDirection.DIRECTED else "---"
    lines = ["flowchart LR"]
    for node in projection.nodes:
        lines.append(f'  {_mermaid_id(node.node_id)}["{_mermaid_label(node.label)}"]')
    for edge in projection.edges:
        label = edge.kind + ((": " + edge.label) if edge.label else "")
        lines.append(
            f"  {_mermaid_id(edge.source_id)} {connector}|{_mermaid_label(label)}| "
            f"{_mermaid_id(edge.target_id)}"
        )
    return "\n".join(lines) + "\n"


def render_hierarchy_dot(projection: HierarchyProjection) -> str:
    lines = [
        "digraph projection {",
        '  graph [rankdir="TB", bgcolor="transparent"];',
        '  node [shape="box", style="rounded,filled", fillcolor="#eef4ff", '
        'color="#315a9b", fontname="Helvetica"];',
        '  edge [color="#718096"];',
    ]
    for node in projection.nodes:
        label = f"{node.label}\\n{node.kind}"
        lines.append(f"  {_dot_quote(node.node_id)} [label={_dot_quote(label)}];")
    for node in projection.nodes:
        if node.parent_id is not None:
            lines.append(f"  {_dot_quote(node.parent_id)} -> {_dot_quote(node.node_id)};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_hierarchy_mermaid(projection: HierarchyProjection) -> str:
    lines = ["flowchart TB"]
    for node in projection.nodes:
        label = f"{node.label}<br/>{node.kind}"
        lines.append(f'  {_mermaid_id(node.node_id)}["{_mermaid_label(label)}"]')
    for node in projection.nodes:
        if node.parent_id is not None:
            lines.append(f"  {_mermaid_id(node.parent_id)} --> {_mermaid_id(node.node_id)}")
    return "\n".join(lines) + "\n"
