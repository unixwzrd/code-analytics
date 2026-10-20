"""Owned, staged runtime report directories and explicit artifact manifests."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from code_analysis.models import Completeness
from code_analysis.profile_models import ProfileReport
from code_analysis.profiling import render_profile_json, render_profile_markdown
from code_analysis.projections.runtime import (
    hotspot_svg,
    runtime_dot,
    runtime_table,
    select_runtime_graph,
)
from code_analysis.runtime_models import RuntimeArtifactEntry, RuntimeManifest, RuntimeSelection


def publish_runtime_report(
    report: ProfileReport, output: Path, *, selection: RuntimeSelection | None = None
) -> RuntimeManifest:
    """Publish into a new directory only. Never mutate input or existing output."""
    selection = selection or RuntimeSelection()
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise OSError(errno.EEXIST, "runtime output already exists")
    if any(parent.is_symlink() for parent in output.parents):
        raise OSError(errno.EINVAL, "runtime output parents cannot be symlinks")
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.parent / f".{output.name}.publication-lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise OSError(errno.EEXIST, "runtime output publication already in progress") from None
    try:
        os.close(descriptor)
        with tempfile.TemporaryDirectory(
            prefix=f".{output.name}.staging-", dir=output.parent
        ) as temporary:
            staging = Path(temporary)
            manifest = _stage(report, staging, selection)
            if output.exists() or output.is_symlink():
                raise OSError(errno.EEXIST, "runtime output appeared during publication")
            os.rename(staging, output)
        return manifest
    finally:
        lock.unlink()


def _stage(report: ProfileReport, staging: Path, selection: RuntimeSelection) -> RuntimeManifest:
    source = render_profile_json(report, compact=True).encode()
    view = select_runtime_graph(report, selection)
    graph_partial = len(view.frames) < view.original_nodes or len(view.edges) < view.original_edges
    table_partial = (
        len(report.frames) > selection.max_records
        or len(report.runtime_edges) > selection.max_records
    )

    def encoded(value: object) -> bytes:
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode()

    artifacts: dict[str, bytes] = {
        "runtime-summary.json": source,
        "runtime-summary.md": render_profile_markdown(report, limit=selection.max_records).encode(),
        "runtime-categories.json": encoded(
            [item.model_dump(mode="json") for item in report.runtime.categories]
        ),
        "runtime-identity.json": encoded(report.runtime.collection.model_dump(mode="json")),
        "runtime-diagnostics.json": encoded(
            {
                "diagnostics": [item.model_dump(mode="json") for item in report.diagnostics],
                "limitations": report.limitations,
            }
        ),
        "runtime-callgraph.dot": runtime_dot(report, selection).encode(),
        "runtime-hotspots.svg": hotspot_svg(report, selection.max_records).encode(),
    }
    for kind in ("functions", "callers", "callees"):
        artifacts[f"runtime-{kind}.tsv"] = runtime_table(
            report, kind=kind, maximum=selection.max_records
        ).encode()
    statuses: dict[str, str] = dict.fromkeys(artifacts, "complete")
    if graph_partial:
        statuses["runtime-callgraph.dot"] = "partial"
    if len(report.frames) > min(selection.max_records, 50):
        statuses["runtime-hotspots.svg"] = "partial"
    if table_partial:
        for name in (
            "runtime-summary.md",
            "runtime-functions.tsv",
            "runtime-callers.tsv",
            "runtime-callees.tsv",
        ):
            statuses[name] = "partial"
    entries: list[RuntimeArtifactEntry] = []
    dot = shutil.which("dot")
    for name, format_, graph in (
        ("runtime-callgraph.svg", "svg", runtime_dot(report, selection)),
        ("runtime-callgraph.png", "png", runtime_dot(report, selection)),
        ("runtime-static-overlay.svg", "svg", runtime_dot(report, selection, overlay=True)),
    ):
        if dot is None:
            entries.append(
                RuntimeArtifactEntry(
                    path=name, status="unavailable", reason="Graphviz executable unavailable"
                )
            )
            continue
        try:
            rendered = subprocess.run(
                (dot, f"-T{format_}"),
                input=graph.encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            entries.append(
                RuntimeArtifactEntry(
                    path=name, status="failed", reason="Graphviz execution failed or timed out"
                )
            )
            continue
        if rendered.returncode or not rendered.stdout or len(rendered.stdout) > 67108864:
            entries.append(
                RuntimeArtifactEntry(
                    path=name,
                    status="failed",
                    reason="Graphviz returned invalid or oversized output",
                )
            )
            continue
        artifacts[name] = rendered.stdout
        statuses[name] = (
            "partial" if graph_partial or name == "runtime-static-overlay.svg" else "complete"
        )
    for name, data in sorted(artifacts.items()):
        (staging / name).write_bytes(data)
        entries.append(
            RuntimeArtifactEntry(
                path=name,
                sha256=hashlib.sha256(data).hexdigest(),
                byte_count=len(data),
                status=statuses[name],
            )
        )
    complete = report.completeness == Completeness.COMPLETE and all(
        item.status == "complete" for item in entries
    )
    manifest = RuntimeManifest(
        source_json_sha256=hashlib.sha256(source).hexdigest(),
        selection=selection,
        original_nodes=view.original_nodes,
        retained_nodes=len(view.frames),
        original_edges=view.original_edges,
        retained_edges=len(view.edges),
        completeness=(
            Completeness.FAILED
            if report.completeness == Completeness.FAILED
            else Completeness.COMPLETE if complete else Completeness.PARTIAL
        ),
        artifacts=tuple(sorted(entries, key=lambda item: item.path)),
        limitations=(
            "Bounds affect presentation only; authoritative JSON retains the analytical inventory.",
            "Static overlay annotates subjects only; static call-edge evidence is unavailable.",
        ),
    )
    for entry in manifest.artifacts:
        if (
            entry.sha256
            and hashlib.sha256((staging / entry.path).read_bytes()).hexdigest() != entry.sha256
        ):
            raise OSError(errno.EBADMSG, "staged runtime artifact failed digest validation")
    (staging / "manifest.json").write_bytes(encoded(manifest.model_dump(mode="json")))
    return manifest
