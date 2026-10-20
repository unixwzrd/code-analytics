"""Saved-generation change evidence; this module never reads analyzed source files."""

from __future__ import annotations

import hashlib
import itertools
import json

from code_analysis import __version__
from code_analysis.change_models import ChangeEvidenceSource, ChangeReport
from code_analysis.models import Completeness
from code_analysis.report_models import ChangeCouplingRecord, GenerationChangeRecord
from code_analysis.reports.json_report import load_json
from code_analysis.workspace import GenerationKind, WorkspacePaths, list_generations


def build_generation_history(workspace: WorkspacePaths) -> ChangeReport:
    manifests = list_generations(workspace, kind=GenerationKind.ANALYSIS)
    reports = []
    for manifest in manifests:
        directory = workspace.kind_directory(GenerationKind.ANALYSIS) / manifest.generation_id
        reports.append(
            (manifest, load_json((directory / "report.json").read_text(encoding="utf-8")))
        )
    changes: list[GenerationChangeRecord] = []
    path_intervals: dict[str, set[str]] = {}
    for (baseline_manifest, baseline), (candidate_manifest, candidate) in itertools.pairwise(
        reports
    ):
        baseline_hashes = {item.path: item.source_sha256 for item in baseline.modules}
        candidate_hashes = {item.path: item.source_sha256 for item in candidate.modules}
        added = tuple(sorted(set(candidate_hashes) - set(baseline_hashes)))
        removed = tuple(sorted(set(baseline_hashes) - set(candidate_hashes)))
        changed = tuple(
            sorted(
                path
                for path in set(baseline_hashes) & set(candidate_hashes)
                if baseline_hashes[path] != candidate_hashes[path]
            )
        )
        identity = _stable_id(
            "generation-change",
            baseline_manifest.portable_identity_sha256,
            candidate_manifest.portable_identity_sha256,
        )
        record = GenerationChangeRecord(
            generation_change_id=identity,
            baseline_identity_sha256=baseline_manifest.portable_identity_sha256,
            candidate_identity_sha256=candidate_manifest.portable_identity_sha256,
            added_paths=added,
            changed_paths=changed,
            removed_paths=removed,
            limitations=(
                "Generation intervals are snapshot evidence, not atomic commit coupling.",
            ),
        )
        changes.append(record)
        for path in (*added, *changed, *removed):
            path_intervals.setdefault(path, set()).add(identity)
    coupling = []
    for left, right in itertools.combinations(sorted(path_intervals), 2):
        shared = path_intervals[left] & path_intervals[right]
        if not shared:
            continue
        coupling.append(
            ChangeCouplingRecord(
                coupling_id=_stable_id("change-coupling", "generation", left, right),
                left_path=left,
                right_path=right,
                shared_changes=len(shared),
                left_total=len(path_intervals[left]),
                right_total=len(path_intervals[right]),
                jaccard=len(shared) / len(path_intervals[left] | path_intervals[right]),
                commit_ids=tuple(sorted(shared)),
            )
        )
    target_payload = (
        reports[-1][1].target.model_dump(mode="json") if reports else {"workspace": "owned"}
    )
    target_identity = hashlib.sha256(
        json.dumps(target_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload = {
        "target": target_identity,
        "changes": [item.model_dump(mode="json") for item in changes],
        "coupling": [item.model_dump(mode="json") for item in coupling],
    }
    change_identity = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ChangeReport(
        tool_version=__version__,
        change_identity_sha256=change_identity,
        target_identity_sha256=target_identity,
        source=ChangeEvidenceSource.GENERATIONS,
        generation_changes=tuple(sorted(changes, key=lambda item: item.generation_change_id)),
        coupling=tuple(sorted(coupling, key=lambda item: item.coupling_id)),
        diagnostics=(),
        completeness=Completeness.COMPLETE,
        limitations=(
            "Generation history compares retained source hashes without rescanning source.",
            "Generation intervals are snapshot evidence, not atomic commit coupling.",
        ),
    )


def render_change_report(report: ChangeReport, *, markdown: bool = False) -> str:
    if not markdown:
        return (
            json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
            + "\n"
        )
    if report.source is ChangeEvidenceSource.GIT:
        repository = report.git_repository
        lines = [
            "# Git change evidence",
            "",
            f"- Schema: `{report.schema_name}@{report.schema_version}`",
            f"- Completeness: `{report.completeness.value}`",
            f"- Path scope: `{repository.path_scope.value if repository else 'unavailable'}`",
            f"- Commits: {len(report.git_commits)}",
            f"- Path changes: {len(report.git_path_changes)}",
            f"- Coupling pairs: {len(report.coupling)}",
            "",
            "| Pair | Shared changes | Left total | Right total | Jaccard |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        lines.extend(
            f"| `{item.left_path}` / `{item.right_path}` | {item.shared_changes} | "
            f"{item.left_total} | {item.right_total} | {item.jaccard:.3f} |"
            for item in report.coupling
        )
    else:
        lines = [
            "# Retained-generation change evidence",
            "",
            f"- Schema: `{report.schema_name}@{report.schema_version}`",
            f"- Completeness: `{report.completeness.value}`",
            f"- Intervals: {len(report.generation_changes)}",
            f"- Coupling pairs: {len(report.coupling)}",
            "",
            "| Interval | Added | Changed | Removed |",
            "| --- | ---: | ---: | ---: |",
        ]
        lines.extend(
            f"| `{item.generation_change_id}` | {len(item.added_paths)} | "
            f"{len(item.changed_paths)} | {len(item.removed_paths)} |"
            for item in report.generation_changes
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in report.limitations)
    return "\n".join(lines) + "\n"


def _stable_id(prefix: str, *values: str) -> str:
    return f"{prefix}-{hashlib.sha256(chr(31).join(values).encode()).hexdigest()[:20]}"
