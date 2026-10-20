from __future__ import annotations

from code_analysis.report_models import AnalysisReport


def render_markdown(report: AnalysisReport, *, finding_limit: int | None = None) -> str:
    summary = report.summary
    lines = [
        "# Code Analysis Summary",
        "",
        f"Completeness: **{report.completeness.value}**",
        "",
        f"Analyzed {summary.parsed_files}/{summary.discovered_files} Python files, "
        f"{summary.modules} modules, and {summary.functions + summary.methods} callable symbols.",
        "",
        "## Stable counts",
        "",
        f"- Classes: {summary.classes}",
        f"- Functions: {summary.functions}",
        f"- Methods: {summary.methods}",
        f"- Import edges: {summary.import_edges}",
        f"- Strongly connected components: {summary.strongly_connected_components}",
        f"- Canonical cycles: {summary.cycles}",
        f"- Findings: {summary.findings}",
        f"- Dictionary shapes: {summary.dictionary_shapes}",
        f"- Dictionary observations: {summary.dictionary_observations}",
        f"- Resolved flow edges: {summary.flow_edges}",
        f"- Unresolved edges: {summary.unresolved_edges}",
        f"- Validation patterns: {summary.validation_patterns}",
        f"- Data structures: {summary.data_structures}",
        f"- Clone clusters: {summary.clone_clusters}",
        f"- Function commonalities: {summary.function_commonalities}",
        f"- Mutable state resources: {summary.state_resources}",
        f"- Side effects: {summary.side_effects}",
        f"- Responsibility clusters: {summary.responsibility_clusters}",
        f"- Refactoring proposals: {summary.refactor_proposals}",
    ]
    if report.candidate_classifications:
        lines.append(f"- Representation candidates: {summary.candidate_classifications}")
    lines.extend(
        [
            f"- Contract subjects: {summary.subjects}",
            f"- Contracts: {summary.contracts}",
            "",
            "## Contract inventory",
            "",
        ]
    )
    if not report.contracts:
        lines.append("No statically declared contracts were reported.")
    for contract in report.contracts:
        subject = next(
            item for item in report.subjects if item.subject_id == contract.defining_subject_id
        )
        boundaries = ", ".join(
            sorted({boundary.boundary_class.value for boundary in contract.boundaries})
        )
        formats = (
            f"; serialization: {', '.join(contract.serialization_formats)}"
            if contract.serialization_formats
            else ""
        )
        versions = (
            f"; schema versions: {', '.join(contract.schema_versions)}"
            if contract.schema_versions
            else ""
        )
        lines.append(
            f"- **{contract.kind.value}** `{subject.logical_name}` — "
            f"{len(contract.members)} members; boundaries: {boundaries}{formats}{versions} "
            f"(`{contract.contract_id}`)"
        )
    lines.extend(["", "## Dictionary shapes", ""])
    if not report.dictionary_shapes:
        lines.append("No evidenced dictionary shapes were reported.")
    subjects = {item.subject_id: item for item in report.subjects}
    for shape in report.dictionary_shapes:
        owner = subjects[shape.owner_subject_id]
        field_count = sum(len(variant.fields) for variant in shape.variants)
        lines.append(
            f"- `{owner.logical_name}` — {len(shape.variants)} variants, "
            f"{field_count} variant fields, {len(shape.lifecycle_states)} lifecycle states "
            f"(`{shape.shape_id}`)"
        )
    lines.extend(["", "## Validation patterns", ""])
    if not report.validation_patterns:
        lines.append("No structural validation patterns were reported.")
    for pattern in report.validation_patterns:
        lines.append(
            f"- **{pattern.classification}** `{pattern.rule}` — "
            f"{len(pattern.sites)} sites across {len(pattern.subject_ids)} subjects "
            f"(`{pattern.validation_id}`)"
        )
    lines.extend(["", "## Data structures", ""])
    if not report.data_structures:
        lines.append("No significant data structures were reported.")
    for structure in report.data_structures:
        owner = subjects[structure.owner_subject_id]
        lines.append(
            f"- `{owner.logical_name}` — {structure.scope.value} "
            f"with {len(structure.reader_subject_ids)} readers, "
            f"{len(structure.writer_subject_ids)} writers, and "
            f"{len(structure.alias_subject_ids)} aliases (`{structure.structure_id}`)"
        )
    if report.candidate_classifications:
        lines.extend(["", "## Representation candidates", ""])
        for candidate in report.candidate_classifications:
            subject = subjects[candidate.subject_id]
            lines.append(
                f"- **{candidate.candidate.value}** `{subject.logical_name}` — "
                f"{candidate.confidence.value} confidence, "
                f"{len(candidate.counter_evidence)} counter-evidence sites "
                f"(`{candidate.candidate_id}`)"
            )
    lines.extend(["", "## Findings", ""])
    findings = report.findings if finding_limit is None else report.findings[:finding_limit]
    if not findings:
        lines.append("No threshold findings were reported.")
    for finding in findings:
        location = finding.evidence[0].span
        symbol = f" `{finding.symbol}`" if finding.symbol else ""
        lines.append(
            f"- **{finding.rule_id}** `{finding.module}`{symbol} — {finding.title} "
            f"(`{location.path}:{location.start_line}`)"
        )
    if report.diagnostics:
        lines.extend(["", "## Diagnostics", ""])
        lines.extend(
            f"- **{diagnostic.level.value}** `{diagnostic.code}` — {diagnostic.message}"
            for diagnostic in report.diagnostics
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {limitation}" for limitation in report.limitations)
    return "\n".join(lines) + "\n"
