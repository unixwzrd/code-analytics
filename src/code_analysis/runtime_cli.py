"""CLI views over the shared runtime evidence services."""

from __future__ import annotations

import argparse
import errno
import sys
from pathlib import Path

from pydantic import ValidationError

from code_analysis.models import Completeness
from code_analysis.profile_ingestion import ingest_profiles
from code_analysis.profile_models import ProfileReport
from code_analysis.profiling import load_profile_json, render_profile_diff, render_profile_json
from code_analysis.reports.json_report import load_json
from code_analysis.runtime_artifacts import publish_runtime_report
from code_analysis.runtime_comparison import compare_runtime_profiles
from code_analysis.runtime_identity import CollectionMetadata, read_bounded_artifact
from code_analysis.runtime_models import RuntimeManifest, RuntimeSelection
from code_analysis.runtime_queries import (
    RuntimeQueryRequest,
    RuntimeQuerySection,
    build_runtime_query,
    render_runtime_query_json,
    render_runtime_query_markdown,
    render_runtime_query_tsv,
)
from code_analysis.runtime_spec import RuntimeAnalysisConfig


def add_runtime_parser[Parser: argparse.ArgumentParser](
    subparsers: argparse._SubParsersAction[Parser],
) -> None:
    parser = subparsers.add_parser("runtime", help="offline-first cProfile evidence")
    commands = parser.add_subparsers(dest="runtime_command", required=True)
    ingest = commands.add_parser("ingest", help="normalize profiles without executing source")
    ingest.add_argument("--profile", action="append", type=Path, required=True)
    ingest.add_argument("--metadata", type=Path)
    ingest.add_argument("--source-report", type=Path)
    ingest.add_argument("--repo", type=Path)
    ingest.add_argument("--config", type=Path)
    ingest.add_argument("--output", type=Path)
    ingest.add_argument(
        "--workspace", type=Path, help="retain import in an existing owned workspace"
    )
    ingest.add_argument("--json", action="store_true")
    render = commands.add_parser("render", help="produce a report directory using saved JSON only")
    render.add_argument("report", type=Path)
    render.add_argument("--output", type=Path, required=True)
    for selected in (ingest, render):
        selected.add_argument("--max-records", type=int, default=500)
        selected.add_argument("--max-nodes", type=int, default=100)
        selected.add_argument("--max-edges", type=int, default=250)
        selected.add_argument("--direction", choices=("TB", "LR"), default="TB")
        selected.add_argument("--project-only", action="store_true")
    compare = commands.add_parser(
        "compare", help="compare proven identities or explicitly diagnostic evidence"
    )
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    compare.add_argument("--diagnostic-only", action="store_true")
    compare.add_argument("--markdown", action="store_true")
    compare.add_argument("--output", type=Path)
    run = commands.add_parser(
        "run", help="execute a strictly specified, explicitly authorized workload"
    )
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--source-report", type=Path)
    run.add_argument("--allow-execution", action="store_true")
    query = commands.add_parser(
        "query", help="query retained runtime evidence without source access"
    )
    query.add_argument("report", type=Path)
    query.add_argument(
        "--section", choices=tuple(item.value for item in RuntimeQuerySection), required=True
    )
    query.add_argument("--max-records", type=int, default=500)
    query.add_argument("--frame", help="exact frame identity for hotspots, calls, or variance")
    query.add_argument("--format", choices=("json", "tsv", "markdown"), default="json")


def load_runtime_path(path: Path) -> ProfileReport:
    manifest = None
    if path.is_dir():
        directory = path
        manifest_path = directory / "manifest.json"
        if manifest_path.exists():
            data, _ = read_bounded_artifact(manifest_path, maximum=4194304, kind="metadata")
            try:
                manifest = RuntimeManifest.model_validate_json(data)
            except ValueError:
                raise OSError(errno.EBADMSG, "invalid runtime artifact manifest") from None
            summary = next(
                item for item in manifest.artifacts if item.path == "runtime-summary.json"
            )
            path = directory / summary.path
            data, artifact = read_bounded_artifact(path, maximum=512 * 1024**2, kind="external")
            if (
                artifact.sha256 != manifest.source_json_sha256
                or artifact.byte_count != summary.byte_count
            ):
                raise OSError(errno.EBADMSG, "runtime summary does not match manifest")
        else:
            path = directory / "runtime-summary.json"
    if manifest is None:
        data, _ = read_bounded_artifact(path, maximum=512 * 1024**2, kind="external")
    try:
        report = load_profile_json(data.decode("utf-8"))
        if manifest is not None and (
            manifest.original_nodes != len(report.frames)
            or manifest.original_edges != len(report.runtime_edges)
            or (
                report.completeness == Completeness.FAILED
                and manifest.completeness != Completeness.FAILED
            )
            or (
                manifest.completeness == Completeness.COMPLETE
                and report.completeness != Completeness.COMPLETE
            )
        ):
            raise ValueError("manifest contradicts authoritative runtime report")
        return report
    except (UnicodeError, ValueError):
        raise OSError(errno.EBADMSG, "invalid saved runtime report") from None


def _import_command(args: argparse.Namespace) -> tuple[str, ...]:
    """Canonical local import invocation; never identify it as a capture command."""
    command = ["code-analysis", "runtime", "ingest"]
    for path in args.profile:
        command.extend(("--profile", str(path.absolute())))
    for name in ("metadata", "source_report", "repo", "config", "workspace"):
        value = getattr(args, name, None)
        if value is not None:
            command.extend(("--" + name.replace("_", "-"), str(value.absolute())))
    if args.json:
        command.append("--json")
    return tuple(command)


def runtime_command(args: argparse.Namespace) -> int:
    if args.runtime_command == "run":
        if not args.allow_execution:
            raise OSError(errno.EPERM, "runtime run requires --allow-execution")
        from code_analysis.config import load_project_config
        from code_analysis.runtime_runner import run_runtime
        from code_analysis.runtime_spec import RuntimeRunSpec

        data, _ = read_bounded_artifact(args.spec, maximum=4194304, kind="metadata")
        try:
            spec = RuntimeRunSpec.model_validate_json(data)
        except ValueError:
            raise OSError(errno.EINVAL, "invalid runtime run specification") from None
        source = (
            load_json(args.source_report.read_text(encoding="utf-8"))
            if args.source_report
            else None
        )
        result = run_runtime(
            spec, allow_execution=True, config=load_project_config(spec.repo), source_report=source
        )
        return result.status
    if args.runtime_command == "compare":
        comparison_report = compare_runtime_profiles(
            load_runtime_path(args.baseline),
            load_runtime_path(args.candidate),
            diagnostic_only=args.diagnostic_only,
        )
        rendered = render_profile_diff(comparison_report, markdown=args.markdown)
        if args.output:
            with args.output.open("x", encoding="utf-8") as output:
                output.write(rendered)
        else:
            sys.stdout.write(rendered)
        return 0 if comparison_report.claim_eligible else errno.ENODATA
    if args.runtime_command == "query":
        try:
            request = RuntimeQueryRequest(
                section=RuntimeQuerySection(args.section),
                max_records=args.max_records,
                frame_id=args.frame,
            )
        except (ValidationError, ValueError):
            raise OSError(errno.EINVAL, "invalid runtime query bounds") from None
        projection = build_runtime_query(load_runtime_path(args.report), request)
        rendered = {
            "json": render_runtime_query_json,
            "tsv": render_runtime_query_tsv,
            "markdown": render_runtime_query_markdown,
        }[args.format](projection)
        sys.stdout.write(rendered)
        return (
            0
            if projection.source_completeness == Completeness.COMPLETE and not projection.truncated
            else errno.ENODATA
        )
    try:
        selection = RuntimeSelection(
            max_records=args.max_records,
            max_nodes=args.max_nodes,
            max_edges=args.max_edges,
            direction=args.direction,
            project_only=args.project_only,
        )
    except ValidationError:
        raise OSError(errno.EINVAL, "invalid runtime presentation bounds") from None
    workspace = None
    if args.runtime_command == "ingest" and getattr(args, "workspace", None) is not None:
        if args.output is not None:
            raise OSError(errno.EINVAL, "choose --workspace or --output for imported evidence")
        from code_analysis.workspace import open_workspace

        workspace = open_workspace(args.workspace)
    if args.runtime_command == "render":
        report = load_runtime_path(args.report)
    else:
        config = RuntimeAnalysisConfig()
        if args.config:
            from code_analysis.config import load_config_file

            config = load_config_file(args.config).runtime_analysis
        metadata = None
        metadata_artifact = None
        if args.metadata:
            data, metadata_artifact = read_bounded_artifact(
                args.metadata, maximum=config.max_metadata_bytes, kind="metadata"
            )
            try:
                metadata = CollectionMetadata.model_validate_json(data)
            except ValueError:
                raise OSError(errno.EBADMSG, "invalid runtime collection metadata") from None
        source = (
            load_json(args.source_report.read_text(encoding="utf-8"))
            if args.source_report
            else None
        )
        report, local_artifacts = ingest_profiles(
            tuple(args.profile),
            config=config,
            repo=args.repo,
            metadata=metadata,
            source_report=source,
        )
        if metadata_artifact is not None:
            from code_analysis.runtime_correlation import profile_content_identity

            artifacts = {item.sha256: item for item in report.runtime.artifacts}
            artifacts[metadata_artifact.sha256] = metadata_artifact
            runtime = report.runtime.model_copy(
                update={
                    "artifacts": tuple(sorted(artifacts.values(), key=lambda item: item.sha256))
                }
            )
            report = report.model_copy(update={"runtime": runtime})
            report = report.model_copy(
                update={"profile_identity_sha256": profile_content_identity(report)}
            )
            report = ProfileReport.model_validate(report.model_dump())
        if workspace is not None:
            from code_analysis.workspace import GenerationKind, commit_generation

            commit_generation(
                workspace,
                kind=GenerationKind.PROFILE,
                artifacts={**local_artifacts, "profile.json": render_profile_json(report).encode()},
                command=_import_command(args),
                interpreter_path=sys.executable,
                portable_identity_sha256=report.profile_identity_sha256,
            )
    if args.output:
        manifest = publish_runtime_report(report, args.output, selection=selection)
        return 0 if manifest.completeness == Completeness.COMPLETE else errno.ENODATA
    sys.stdout.write(render_profile_json(report, compact=not args.json))
    return 0 if report.completeness == Completeness.COMPLETE else errno.ENODATA
