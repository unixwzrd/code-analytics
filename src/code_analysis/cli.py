from __future__ import annotations

import argparse
import errno
import hashlib
import json
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from code_analysis.artifact_viewer import ViewerCommand

from pydantic import ValidationError

from code_analysis.cache import ContentCache
from code_analysis.comparisons import ComparisonBuildError, build_report_delta
from code_analysis.config import (
    AnalysisConfig,
    JavaScriptModuleMode,
    LanguageSelection,
    Strictness,
    load_config_file,
    load_project_config,
)
from code_analysis.context import AnalysisContext
from code_analysis.coverage_evidence import import_coverage
from code_analysis.coverage_runtime import run_coverage_workload
from code_analysis.decision_models import MaintainerDecision
from code_analysis.decisions import (
    effective_decisions,
    export_ledger,
    import_ledger,
    ledger_sha256,
    load_ledger,
    set_decision,
)
from code_analysis.errors import ConfigurationError, InvalidArgumentError
from code_analysis.git_reporting import build_git_history
from code_analysis.history import build_generation_history, render_change_report
from code_analysis.models import Completeness, ProviderResult, ProviderState, SourceLanguage
from code_analysis.profiling import (
    build_profile_diff,
    load_profile_json,
    normalize_profile_artifact,
    profile_workload,
    render_profile_diff,
    render_profile_graph,
    render_profile_json,
    render_profile_markdown,
    render_profile_tsv,
    render_static_runtime_overlay,
)
from code_analysis.project import discover_context, find_project_root
from code_analysis.projection_models import (
    ProjectionCollapse,
    ProjectionFindingCategory,
    ProjectionFindingSeverity,
    ProjectionSelection,
)
from code_analysis.projections import ProjectionBuildError, ProjectionName, build_projection
from code_analysis.projections.candidates import build_candidate_worklist_projection
from code_analysis.providers.registry import doctor_document, inventory_tool_paths, inventory_tools
from code_analysis.queries import (
    QueryBuildError,
    build_profile_query,
    build_query,
    render_query_json,
    render_query_markdown,
)
from code_analysis.query_models import QuerySelector, QuerySelectorKind
from code_analysis.regressions import build_regression_report, render_regression
from code_analysis.report_models import RefactorCandidateKind
from code_analysis.reports.json_report import canonical_json, load_json, render_json
from code_analysis.reports.markdown import render_markdown
from code_analysis.reports.projection_views import (
    ProjectionFormat,
    ProjectionRenderError,
    render_projection,
    render_projection_binary,
)
from code_analysis.runtime_execution import resolve_expected_status
from code_analysis.semantic_nomination import (
    SemanticProviderUnavailable,
    build_semantic_nomination_report,
    render_semantic_nomination,
)
from code_analysis.service import (
    scan,
    scan_clones,
    scan_contract_inventory,
    scan_dictionary_shapes,
    scan_refactor,
)
from code_analysis.workspace import (
    CacheStatistics,
    GenerationKind,
    WorkspacePaths,
    commit_generation,
    create_run_record,
    list_generations,
    open_workspace,
    prepare_workspace,
    prune_generations,
    set_generation_pin,
    write_workspace_results,
)


class ExitCode(IntEnum):
    """Process statuses mapped to names defined by the host's ``errno.h``."""

    SUCCESS = 0
    EPERM = errno.EPERM
    EINVAL = errno.EINVAL
    ENODATA = errno.ENODATA
    EBADMSG = errno.EBADMSG
    EPROTO = errno.EPROTO
    EFAULT = errno.EFAULT
    ETIMEDOUT = errno.ETIMEDOUT


class ErrnoArgumentParser(argparse.ArgumentParser):
    """Argument parser whose usage failure preserves the errno contract."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(int(ExitCode.EINVAL), f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = ErrnoArgumentParser(prog="code-analysis")
    subparsers = parser.add_subparsers(dest="command")
    doctor = subparsers.add_parser("doctor", help="report interpreter and provider provenance")
    doctor.add_argument("--json", action="store_true", help="retained for explicit output identity")

    providers = subparsers.add_parser("providers", help="list provider availability")
    providers.add_argument("--json", action="store_true")

    scan_parser = subparsers.add_parser(
        "scan", help="statically analyze a Python and JavaScript source tree"
    )
    scan_parser.add_argument("path", nargs="?", default=".")
    scan_parser.add_argument("--config", type=Path)
    scan_parser.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        help="add an exact directory name to exclusions (repeatable; no glob matching)",
    )
    output = scan_parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit authoritative JSON")
    output.add_argument("--json-compact", action="store_true", help="emit compact JSON")
    output.add_argument("--markdown", action="store_true", help="emit concise Markdown")
    scan_parser.add_argument("--output", "-o", type=Path)
    workspace = scan_parser.add_mutually_exclusive_group()
    workspace.add_argument(
        "--no-workspace", action="store_true", help="disable project-local reports and cache"
    )
    workspace.add_argument("--workspace", type=Path, help="use an explicit owned workspace")
    scan_parser.add_argument(
        "--summary", action="store_true", help="function-analysis alias for Markdown summary"
    )
    scan_parser.add_argument("--only-refactor-targets", action="store_true")
    scan_parser.add_argument("--only-local-imports", action="store_true")
    scan_parser.add_argument("--all-imports", action="store_true")
    scan_parser.add_argument(
        "--max-items", type=int, help="function-analysis display flag; accepted but ignored"
    )
    scan_parser.add_argument("--max-findings", type=int)
    scan_parser.add_argument(
        "--strictness", choices=[item.value for item in Strictness], default=None
    )
    scan_parser.add_argument("--god-loc", type=int)
    scan_parser.add_argument("--god-branches", type=int)
    scan_parser.add_argument("--god-nesting", type=int)
    scan_parser.add_argument("--god-args", type=int)
    scan_parser.add_argument("--god-score", type=int)
    scan_parser.add_argument("--symbol-filter")
    scan_parser.add_argument("--fail-on-findings", action="store_true")
    scan_parser.add_argument("--no-color", action="store_true")
    scan_parser.add_argument("--quiet", action="store_true")
    scan_parser.add_argument("--verbose", action="store_true")
    _add_language_options(scan_parser)

    for command_name, help_text in (
        ("clones", "analyze clone and function-commonality evidence"),
        ("refactor", "build bounded Phase 3 refactoring evidence"),
    ):
        phase_three_parser = subparsers.add_parser(command_name, help=help_text)
        phase_three_parser.add_argument("path", nargs="?", default=".")
        phase_three_parser.add_argument("--config", type=Path)
        phase_three_output = phase_three_parser.add_mutually_exclusive_group()
        phase_three_output.add_argument("--json", action="store_true")
        phase_three_output.add_argument("--json-compact", action="store_true")
        phase_three_output.add_argument("--markdown", action="store_true")
        phase_three_parser.add_argument("--output", "-o", type=Path)
        phase_three_parser.add_argument(
            "--exclude-dir",
            action="append",
            default=[],
            help="add an exact directory name to exclusions (repeatable; no glob matching)",
        )
        phase_three_workspace = phase_three_parser.add_mutually_exclusive_group()
        phase_three_workspace.add_argument("--no-workspace", action="store_true")
        phase_three_workspace.add_argument("--workspace", type=Path)
        phase_three_imports = phase_three_parser.add_mutually_exclusive_group()
        phase_three_imports.add_argument("--only-local-imports", action="store_true")
        phase_three_imports.add_argument("--all-imports", action="store_true")
        phase_three_parser.add_argument("--max-findings", type=int)
        phase_three_parser.add_argument(
            "--strictness", choices=[item.value for item in Strictness], default=None
        )
        phase_three_parser.add_argument("--symbol-filter")
        phase_three_parser.add_argument("--quiet", action="store_true")
        _add_language_options(phase_three_parser)
        phase_three_parser.set_defaults(
            fail_on_findings=False,
            god_args=None,
            god_branches=None,
            god_loc=None,
            god_nesting=None,
            god_score=None,
            max_items=None,
            no_color=False,
            only_refactor_targets=False,
            summary=False,
            verbose=False,
        )

    contracts_parser = subparsers.add_parser(
        "contracts", help="inventory statically declared data contracts"
    )
    contracts_parser.add_argument("path", nargs="?", default=".")
    contracts_parser.add_argument("--config", type=Path)
    contract_output = contracts_parser.add_mutually_exclusive_group()
    contract_output.add_argument("--json", action="store_true", help="emit authoritative JSON")
    contract_output.add_argument("--json-compact", action="store_true", help="emit compact JSON")
    contract_output.add_argument("--markdown", action="store_true", help="emit concise Markdown")
    contracts_parser.add_argument("--output", "-o", type=Path)
    contract_workspace = contracts_parser.add_mutually_exclusive_group()
    contract_workspace.add_argument(
        "--no-workspace", action="store_true", help="disable project-local reports and cache"
    )
    contract_workspace.add_argument(
        "--workspace", type=Path, help="use an explicit owned workspace"
    )
    contract_imports = contracts_parser.add_mutually_exclusive_group()
    contract_imports.add_argument("--only-local-imports", action="store_true")
    contract_imports.add_argument("--all-imports", action="store_true")
    contracts_parser.add_argument("--quiet", action="store_true")
    _add_focused_language_option(contracts_parser)

    dictionary_parser = subparsers.add_parser(
        "dict-shapes", help="analyze dictionary shapes and bounded data flow"
    )
    dictionary_parser.add_argument("path", nargs="?", default=".")
    dictionary_parser.add_argument("--config", type=Path)
    dictionary_output = dictionary_parser.add_mutually_exclusive_group()
    dictionary_output.add_argument("--json", action="store_true", help="emit authoritative JSON")
    dictionary_output.add_argument("--json-compact", action="store_true", help="emit compact JSON")
    dictionary_output.add_argument("--markdown", action="store_true", help="emit concise Markdown")
    dictionary_parser.add_argument("--output", "-o", type=Path)
    dictionary_workspace = dictionary_parser.add_mutually_exclusive_group()
    dictionary_workspace.add_argument(
        "--no-workspace", action="store_true", help="disable project-local reports and cache"
    )
    dictionary_workspace.add_argument(
        "--workspace", type=Path, help="use an explicit owned workspace"
    )
    dictionary_imports = dictionary_parser.add_mutually_exclusive_group()
    dictionary_imports.add_argument("--only-local-imports", action="store_true")
    dictionary_imports.add_argument("--all-imports", action="store_true")
    dictionary_parser.add_argument("--quiet", action="store_true")
    _add_focused_language_option(dictionary_parser)

    summary = subparsers.add_parser("summary", help="render a saved JSON report")
    summary.add_argument("report", type=Path)
    summary.add_argument("--max-findings", type=int)

    index_parser = subparsers.add_parser(
        "index", help="prepare a disposable paged browsing cache from saved JSON"
    )
    index_parser.add_argument("report", type=Path)
    index_parser.add_argument("--compare-profile", type=Path, help="saved candidate Python profile")
    index_parser.add_argument("-o", "--output", type=Path, required=True)
    index_parser.add_argument(
        "--profile-report", type=Path, help="saved Python profile to index; no execution"
    )
    index_parser.add_argument(
        "--compare", type=Path, help="candidate report; positional report is the baseline"
    )

    tui_parser = subparsers.add_parser("tui", help="browse a prepared index in the terminal")
    tui_parser.add_argument("index", nargs="?", type=Path)
    tui_parser.add_argument(
        "--workspace", type=Path, help="owned workspace for explicit saved-run selection"
    )
    tui_parser.add_argument("--report-sha256", help="required with an existing index")
    tui_parser.add_argument("--page-size", type=int, default=25)
    tui_parser.add_argument("--source-root", type=Path)
    tui_parser.add_argument("--display-config", type=Path, help="trusted local display JSON")
    tui_parser.add_argument(
        "--collection-root", type=Path, help="confined terminal collection planning root"
    )
    tui_parser.add_argument(
        "--collection-python",
        type=Path,
        action="append",
        default=[],
        help="additional trusted Python interpreter for collection (repeatable)",
    )
    tui_parser.add_argument(
        "--allow-collection", action="store_true", help="enable confirmed terminal collection jobs"
    )
    tui_parser.add_argument(
        "--viewer-command", help="JSON argument array for an absolute viewer launcher"
    )
    tui_parser.add_argument(
        "--no-viewer", action="store_true", help="show artifact paths without launching a viewer"
    )

    html = subparsers.add_parser("html", help="export a self-contained saved-report HTML document")
    html.add_argument("report", type=Path)
    html.add_argument("--compare", type=Path)
    html.add_argument(
        "--projection", action="append", choices=[item.value for item in ProjectionName]
    )
    html.add_argument("--max-records", type=int, default=100)
    html.add_argument("-o", "--output", type=Path, required=True)

    web = subparsers.add_parser("web", help="serve the read-only report explorer on loopback")
    web.add_argument("report", type=Path)
    web.add_argument("--compare", type=Path)
    web.add_argument("--profile-report", type=Path)
    web.add_argument("--compare-profile", type=Path)
    web.add_argument(
        "--source-root", type=Path, help="explicit root for hash-checked source previews"
    )
    web.add_argument("--workspace", type=Path, help="owned workspace for saved-run selection")
    web.add_argument(
        "--collection-root",
        type=Path,
        help="enable confined collection command planning beneath this directory; no execution",
    )
    web.add_argument(
        "--collection-python",
        type=Path,
        action="append",
        default=[],
        help="additional trusted Python interpreter for collection (repeatable)",
    )
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument(
        "--allow-collection",
        action="store_true",
        help="enable explicitly confirmed browser collection jobs",
    )
    web.add_argument("--port", type=int, default=0)
    web.add_argument("--display-config", type=Path, help="trusted local display JSON")
    web.add_argument("--open", action="store_true", help="open the session URL in a browser")

    ui = subparsers.add_parser("ui", help="open the latest saved report in the local UI")
    ui.add_argument("report", nargs="?", type=Path, help="report JSON or generation directory")
    ui.add_argument("--compare", type=Path, help="optional comparison report or generation")
    ui.add_argument("--profile-report", type=Path)
    ui.add_argument("--compare-profile", type=Path)
    ui.add_argument(
        "--source-root", type=Path, help="explicit root for hash-checked source previews"
    )
    ui.add_argument("--workspace", type=Path, help="owned workspace for saved-run selection")
    ui.add_argument(
        "--collection-root",
        type=Path,
        help="enable collection planning, including startup without a saved report",
    )
    ui.add_argument(
        "--collection-python",
        type=Path,
        action="append",
        default=[],
        help="additional trusted Python interpreter for collection (repeatable)",
    )
    ui.add_argument("--host", default="127.0.0.1")
    ui_collection = ui.add_mutually_exclusive_group()
    ui_collection.add_argument(
        "--allow-collection",
        action="store_true",
        default=True,
        help="enable explicitly confirmed browser collection jobs (default)",
    )
    ui_collection.add_argument(
        "--no-collection",
        dest="allow_collection",
        action="store_false",
        help="allow previews but disable browser collection jobs",
    )
    ui.add_argument("--port", type=int, default=0)
    ui.add_argument(
        "--foreground", action="store_true", help="serve in this terminal for debugging"
    )
    ui.add_argument("--display-config", type=Path, help="trusted local display JSON")
    ui_browser = ui.add_mutually_exclusive_group()
    ui_browser.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        default=None,
        help="open the session URL in a browser",
    )
    ui_browser.add_argument(
        "--no-open",
        dest="open_browser",
        action="store_false",
        help="serve the UI without opening a browser",
    )

    ui_stop = subparsers.add_parser("ui-stop", help="stop an authenticated background UI session")
    ui_stop.add_argument(
        "--session", help="session identity printed at launch; default is current project"
    )
    ui_stop.add_argument(
        "--cancel-jobs",
        action="store_true",
        help="explicitly cancel active collection/profile jobs",
    )

    candidates = subparsers.add_parser(
        "candidates", help="render an actionable worklist from a saved report"
    )
    candidates.add_argument("report", type=Path)
    candidates.add_argument("--workspace", type=Path)
    candidates.add_argument(
        "--format",
        choices=("json", "tsv", "markdown"),
        default="markdown",
    )
    candidates.add_argument("--module-filter", action="append", default=[])
    candidates.add_argument("--exclude", action="append", default=[])
    candidates.add_argument(
        "--kind",
        action="append",
        default=[],
        choices=[item.value for item in RefactorCandidateKind],
    )
    candidates.add_argument("--cross-module-only", action="store_true")
    candidates.add_argument("--production-only", action="store_true")
    candidates.add_argument(
        "--state",
        action="append",
        default=[],
        choices=(
            "observed",
            "blocked",
            "reviewable",
            "reviewable-incomplete",
            "ready-for-maintainer-review",
        ),
    )
    candidates.add_argument(
        "--language",
        action="append",
        default=[],
        choices=[item.value for item in SourceLanguage],
    )
    candidates.add_argument("--min-shared-dimensions", type=int, default=0)
    candidates.add_argument("--prefer-existing-owner", action="store_true")
    candidates.add_argument("--top", type=int, default=25)
    candidates.add_argument(
        "--decision",
        action="append",
        default=[],
        choices=(
            *(item.value for item in MaintainerDecision),
            "changed-since-decision",
            "undecided",
        ),
    )

    decisions_parser = subparsers.add_parser(
        "decisions", help="manage the project-local maintainer decision ledger"
    )
    decision_commands = decisions_parser.add_subparsers(dest="decision_command", required=True)
    decision_list = decision_commands.add_parser("list", help="list effective decisions")
    decision_list.add_argument("path", type=Path)
    decision_list.add_argument("--workspace", type=Path)
    decision_list.add_argument("--json", action="store_true")
    decision_set = decision_commands.add_parser("set", help="append a maintainer decision")
    decision_set.add_argument("path", type=Path)
    decision_set.add_argument("candidate_id")
    decision_set.add_argument("--report", type=Path, required=True)
    decision_set.add_argument(
        "--decision", choices=[item.value for item in MaintainerDecision], required=True
    )
    decision_set.add_argument("--reason", required=True)
    decision_set.add_argument("--proposal")
    decision_set.add_argument("--workspace", type=Path)
    decision_export = decision_commands.add_parser("export", help="export the ledger")
    decision_export.add_argument("path", type=Path)
    decision_export.add_argument("output", type=Path)
    decision_export.add_argument("--workspace", type=Path)
    decision_import = decision_commands.add_parser("import", help="import decision records")
    decision_import.add_argument("path", type=Path)
    decision_import.add_argument("input", type=Path)
    decision_import.add_argument("--workspace", type=Path)
    decision_import.add_argument(
        "--conflict", choices=("error", "keep-local", "use-incoming"), default="error"
    )
    decision_import.add_argument("--dry-run", action="store_true")

    query_parser = subparsers.add_parser(
        "query", help="query a bounded neighborhood in a saved report"
    )
    query_parser.add_argument("report", type=Path)
    query_selector = query_parser.add_mutually_exclusive_group(required=True)
    query_selector.add_argument("--contract")
    query_selector.add_argument("--shape")
    query_selector.add_argument("--structure")
    query_selector.add_argument("--finding")
    query_selector.add_argument("--clone")
    query_selector.add_argument("--duplicate")
    query_selector.add_argument("--call-site")
    query_selector.add_argument("--responsibility")
    query_selector.add_argument("--proposal")
    query_selector.add_argument("--refactor-candidate")
    query_selector.add_argument("--symbol")
    query_selector.add_argument("--test")
    query_selector.add_argument("--coverage")
    query_selector.add_argument("--git-change")
    query_selector.add_argument("--generation")
    query_selector.add_argument("--profile-frame")
    query_selector.add_argument("--runtime-edge")
    query_selector.add_argument("--dependency-path", nargs=2, metavar=("SOURCE", "TARGET"))
    query_output = query_parser.add_mutually_exclusive_group()
    query_output.add_argument("--json", action="store_true")
    query_output.add_argument("--json-compact", action="store_true")
    query_output.add_argument("--markdown", action="store_true")
    query_parser.add_argument("--limit", type=int, default=50)
    query_parser.add_argument("--after")
    query_parser.add_argument("--output", "-o", type=Path)

    diff_parser = subparsers.add_parser("diff", help="compare compatible retained report evidence")
    diff_parser.add_argument("baseline", type=Path)
    diff_parser.add_argument("candidate", type=Path)
    diff_parser.add_argument(
        "--format",
        default=ProjectionFormat.JSON.value,
        choices=("json", "json-compact", "tsv", "markdown"),
    )
    diff_parser.add_argument("--max-records", type=int, default=500)
    diff_parser.add_argument(
        "--include-unchanged",
        action="store_true",
        help="include unchanged records in the bounded comparison inventory",
    )
    diff_parser.add_argument("--output", "-o", type=Path)

    regress_parser = subparsers.add_parser(
        "regress", help="compare domain-specific evidence in compatible retained reports"
    )
    regress_parser.add_argument("baseline", type=Path)
    regress_parser.add_argument("candidate", type=Path)
    regress_parser.add_argument("--workspace", type=Path)
    regress_parser.add_argument(
        "--format",
        default="json",
        choices=("json", "json-compact", "tsv", "markdown"),
    )
    regress_parser.add_argument("--top", type=int, default=25)
    regress_parser.add_argument("--output", "-o", type=Path)

    semantic_parser = subparsers.add_parser(
        "semantic-nominate",
        help="nominate bounded pairs from a retained report using an existing local model",
    )
    semantic_parser.add_argument("report", type=Path)
    semantic_parser.add_argument("--model-directory", type=Path, required=True)
    semantic_parser.add_argument("--top", type=int, default=1_000)
    semantic_output = semantic_parser.add_mutually_exclusive_group()
    semantic_output.add_argument("--json", action="store_true")
    semantic_output.add_argument("--json-compact", action="store_true")
    semantic_parser.add_argument("--output", "-o", type=Path)

    export_parser = subparsers.add_parser(
        "export", help="project a saved report without rescanning source"
    )
    export_parser.add_argument("report", type=Path)
    export_parser.add_argument(
        "--projection",
        required=True,
        choices=[item.value for item in ProjectionName],
    )
    export_parser.add_argument(
        "--format",
        default=ProjectionFormat.JSON.value,
        choices=[item.value for item in ProjectionFormat],
    )
    export_parser.add_argument("--max-records", type=int, default=500)
    export_parser.add_argument("--max-nodes", type=int, default=100)
    export_parser.add_argument("--max-edges", type=int, default=250)
    export_parser.add_argument(
        "--collapse",
        choices=[item.value for item in ProjectionCollapse],
        default=ProjectionCollapse.NONE.value,
    )
    export_parser.add_argument(
        "--select",
        action="append",
        default=[],
        metavar="ID",
        help="select a module or contract ID for a focused projection; may be repeated",
    )
    export_parser.add_argument(
        "--edge-kind",
        action="append",
        default=[],
        choices=("runtime", "type_checking", "optional", "lazy"),
        help="retain one dependency edge kind; may be repeated",
    )
    export_parser.add_argument("--depth", type=int)
    export_parser.add_argument(
        "--module-prefix",
        action="append",
        default=[],
        metavar="MODULE",
        help="retain findings for an exact dotted module or its descendants; may be repeated",
    )
    export_parser.add_argument(
        "--category",
        action="append",
        default=[],
        choices=[item.value for item in ProjectionFindingCategory],
        help="retain one finding category; may be repeated",
    )
    export_parser.add_argument(
        "--severity",
        action="append",
        default=[],
        choices=[item.value for item in ProjectionFindingSeverity],
        help="retain one finding severity; may be repeated",
    )
    export_parser.add_argument(
        "--rule",
        action="append",
        default=[],
        metavar="RULE_ID",
        help="retain one finding rule ID; may be repeated",
    )
    export_parser.add_argument(
        "--kind",
        action="append",
        default=[],
        choices=[item.value for item in RefactorCandidateKind],
        help="retain one refactor candidate kind; may be repeated for candidate-worklist",
    )
    export_parser.add_argument("--output", "-o", type=Path)
    export_parser.add_argument(
        "--language",
        action="append",
        default=[],
        choices=[item.value for item in SourceLanguage],
    )

    workspace_parser = subparsers.add_parser(
        "workspace", help="inspect and retain owned local generations"
    )
    workspace_commands = workspace_parser.add_subparsers(dest="workspace_command", required=True)
    workspace_list = workspace_commands.add_parser("list", help="list validated generations")
    workspace_list.add_argument("path", nargs="?", default=".")
    workspace_list.add_argument("--workspace", dest="explicit_workspace", type=Path)
    workspace_list.add_argument("--json", action="store_true")
    for command_name in ("pin", "unpin"):
        command = workspace_commands.add_parser(command_name)
        command.add_argument("path")
        command.add_argument("generation_id")
        command.add_argument("--workspace", dest="explicit_workspace", type=Path)
    workspace_prune = workspace_commands.add_parser("prune", help="prune validated generations")
    workspace_prune.add_argument("path", nargs="?", default=".")
    workspace_prune.add_argument("--workspace", dest="explicit_workspace", type=Path)
    workspace_prune.add_argument("--keep-older", type=int)
    workspace_prune.add_argument("--dry-run", action="store_true")

    history_parser = subparsers.add_parser("history", help="collect explicit bounded Git evidence")
    history_parser.add_argument("path", nargs="?", default=".")
    history_parser.add_argument("--config", type=Path)
    history_parser.add_argument(
        "--generations",
        action="store_true",
        help="compare retained analysis generations instead of reading Git",
    )
    history_parser.add_argument("--workspace", dest="explicit_workspace", type=Path)
    history_parser.add_argument("--markdown", action="store_true")
    history_parser.add_argument("--output", "-o", type=Path)

    coverage_import = subparsers.add_parser(
        "coverage-import", help="bind coverage.py evidence to a saved static report"
    )
    coverage_import.add_argument("report", type=Path)
    coverage_import.add_argument("coverage_data", nargs="+", type=Path)
    coverage_import.add_argument("--source-root", type=Path)
    coverage_import.add_argument("--workspace", type=Path)
    coverage_import.add_argument("--replace", action="store_true")
    coverage_import.add_argument("--json-compact", action="store_true")
    coverage_import.add_argument("--output", "-o", type=Path)

    coverage_run = subparsers.add_parser(
        "coverage-run", help="execute an explicitly authorized Python coverage workload"
    )
    coverage_run.add_argument("report", type=Path)
    coverage_run.add_argument("--allow-execution", action="store_true")
    coverage_run.add_argument("--cwd", type=Path, default=Path.cwd())
    coverage_run.add_argument("--workspace", type=Path)
    coverage_run.add_argument("--timeout", type=float, default=300.0)
    coverage_run.add_argument("--expected-status", default="SUCCESS")
    coverage_run.add_argument("--repeat", type=int, default=1)
    coverage_run.add_argument("--env", action="append", default=[])
    coverage_run.add_argument("--quiet", action="store_true")
    coverage_run.add_argument("--verbose", action="store_true")
    coverage_run.add_argument("--json-compact", action="store_true")
    coverage_run.add_argument("--output", "-o", type=Path)

    profile = subparsers.add_parser(
        "profile", help="execute an explicitly authorized Python cProfile workload"
    )
    profile.add_argument("--source-report", required=True, type=Path)
    profile.add_argument("--allow-execution", action="store_true")
    profile.add_argument("--cwd", type=Path, default=Path.cwd())
    profile.add_argument("--workspace", type=Path)
    profile.add_argument("--timeout", type=float, default=300.0)
    profile.add_argument("--expected-status", default="SUCCESS")
    profile.add_argument("--repeat", type=int, default=1)
    profile.add_argument("--env", action="append", default=[])
    profile.add_argument("--quiet", action="store_true")
    profile.add_argument("--verbose", action="store_true")
    profile.add_argument("--json-compact", action="store_true")
    profile.add_argument("--output", "-o", type=Path)

    profile_report = subparsers.add_parser(
        "profile-report", help="render a saved portable profile report"
    )
    profile_report.add_argument("profile", type=Path)
    profile_report.add_argument("--source-report", type=Path)
    profile_report.add_argument("--source-root", type=Path)
    profile_report.add_argument("--static-report", type=Path)
    profile_report.add_argument(
        "--format",
        choices=(
            "json",
            "json-compact",
            "markdown",
            "tsv",
            "dot",
            "mermaid",
            "overlay-dot",
            "overlay-mermaid",
        ),
        default="markdown",
    )
    profile_report.add_argument("--output", "-o", type=Path)

    profile_diff = subparsers.add_parser("profile-diff", help="compare compatible profiles")
    profile_diff.add_argument("baseline", type=Path)
    profile_diff.add_argument("candidate", type=Path)
    profile_diff.add_argument("--markdown", action="store_true")
    profile_diff.add_argument("--diagnostic-only", action="store_true")
    profile_diff.add_argument("--output", "-o", type=Path)
    from code_analysis.runtime_cli import add_runtime_parser

    add_runtime_parser(subparsers)
    return parser


def _add_language_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--language",
        action="append",
        choices=[item.value for item in LanguageSelection],
        help="select auto, python, or javascript; explicit languages may be repeated",
    )
    parser.add_argument(
        "--javascript-module-mode",
        choices=[item.value for item in JavaScriptModuleMode],
        default=None,
    )


def _add_focused_language_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--language",
        action="append",
        choices=[item.value for item in LanguageSelection],
        help="select auto or python; other languages are inapplicable to this focused command",
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    workload: tuple[str, ...] = ()
    if arguments and arguments[0] in {"coverage-run", "profile"} and "--" in arguments:
        separator = arguments.index("--")
        workload = tuple(arguments[separator + 1 :])
        arguments = arguments[:separator]
    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.command is None:
        args = argparse.Namespace(
            command="ui",
            report=None,
            compare=None,
            host="127.0.0.1",
            port=0,
            open_browser=None,
        )
    if args.command in {"coverage-run", "profile"}:
        args.workload = workload
    try:
        if args.command == "runtime":
            from code_analysis.runtime_cli import runtime_command

            return runtime_command(args)
        if args.command == "doctor":
            sys.stdout.write(doctor_document())
            return ExitCode.SUCCESS
        if args.command == "providers":
            return _providers(args.json)
        if args.command == "summary":
            return _summary(args.report, args.max_findings)
        if args.command == "index":
            from code_analysis.browse_index_cli import prepare_index_command

            return prepare_index_command(
                args.report, args.output, args.compare, args.profile_report, args.compare_profile
            )
        if args.command == "tui":
            from code_analysis.tui_cli import tui_command

            values = (
                args.index,
                args.report_sha256,
                args.page_size,
                args.source_root,
                args.viewer_command,
                args.no_viewer,
                args.workspace,
                args.display_config,
                args.collection_root,
                args.allow_collection,
            )
            if args.collection_python:
                return tui_command(*values, collection_pythons=tuple(args.collection_python))
            return tui_command(*values)
        if args.command == "html":
            return _html(args)
        if args.command == "web":
            return _web(args)
        if args.command == "ui":
            return _ui(args)
        if args.command == "ui-stop":
            return _ui_stop(args)
        if args.command == "candidates":
            return _candidates(args)
        if args.command == "decisions":
            return _decisions(args)
        if args.command == "query":
            return _query(args)
        if args.command == "diff":
            return _diff(args)
        if args.command == "regress":
            return _regress(args)
        if args.command == "semantic-nominate":
            return _semantic_nominate(args)
        if args.command == "export":
            return _export(
                args.report,
                ProjectionName(args.projection),
                ProjectionFormat(args.format),
                args.max_records,
                args.max_nodes,
                args.max_edges,
                ProjectionCollapse(args.collapse),
                tuple(args.select),
                tuple(args.edge_kind),
                args.depth,
                tuple(args.module_prefix),
                tuple(ProjectionFindingCategory(item) for item in args.category),
                tuple(ProjectionFindingSeverity(item) for item in args.severity),
                tuple(args.rule),
                tuple(SourceLanguage(item) for item in args.language),
                tuple(RefactorCandidateKind(item) for item in args.kind),
                args.output,
            )
        if args.command == "workspace":
            return _workspace_command(args)
        if args.command == "history":
            return _history(args)
        if args.command == "coverage-import":
            return _coverage_import(args)
        if args.command == "coverage-run":
            return _coverage_run(args)
        if args.command == "profile":
            return _profile(args)
        if args.command == "profile-report":
            return _profile_report(args)
        if args.command == "profile-diff":
            return _profile_diff(args)
        if args.command in {"scan", "clones", "refactor"}:
            return _scan(args)
        if args.command == "contracts":
            return _contracts(args)
        if args.command == "dict-shapes":
            return _dict_shapes(args)
        parser.error("unknown command")
    except InvalidArgumentError as exc:
        print(f"invalid argument: {exc}", file=sys.stderr)
        return ExitCode.EINVAL
    except (ConfigurationError, ValidationError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return ExitCode.EBADMSG
    except OSError as exc:
        print(f"I/O error: {exc}", file=sys.stderr)
        return exc.errno if exc.errno is not None and exc.errno > 0 else errno.EIO
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return ExitCode.EFAULT
    return ExitCode.EFAULT


def _providers(as_json: bool) -> int:
    providers = inventory_tools(portable=False)
    if as_json:
        payload = [provider.model_dump(mode="json") for provider in providers]
        sys.stdout.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    else:
        for provider in providers:
            version = f" ({provider.version})" if provider.version else ""
            executable = f" [{provider.executable}]" if provider.executable else ""
            print(f"{provider.provider_id}: {provider.state.value}{version}{executable}")
    return ExitCode.SUCCESS


def _owned_workspace(
    path_value: str, explicit: Path | None
) -> tuple[AnalysisContext, WorkspacePaths]:
    target = Path(path_value).expanduser()
    config_root = find_project_root(target)
    config = load_project_config(config_root)
    context = discover_context(target, config, project_root=config_root)
    configured = Path(config.workspace.path).expanduser() if config.workspace.path else None
    if configured is not None and not configured.is_absolute():
        configured = context.source.physical_root / configured
    return context, prepare_workspace(context, explicit_path=explicit or configured)


def _workspace_command(args: argparse.Namespace) -> int:
    context, workspace = _owned_workspace(args.path, args.explicit_workspace)
    if args.workspace_command == "list":
        records = list_generations(workspace)
        pins = {item.name for item in workspace.pins.iterdir() if item.is_file()}
        if args.json:
            payload = [
                {**item.model_dump(mode="json"), "pinned": item.generation_id in pins}
                for item in records
            ]
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        else:
            for item in records:
                pinned = " pinned" if item.generation_id in pins else ""
                print(f"{item.kind.value} {item.generation_id}{pinned}")
        return ExitCode.SUCCESS
    if args.workspace_command in {"pin", "unpin"}:
        set_generation_pin(workspace, args.generation_id, pinned=args.workspace_command == "pin")
        return ExitCode.SUCCESS
    removed = prune_generations(
        workspace,
        keep_older=(
            args.keep_older
            if args.keep_older is not None
            else context.config.workspace.keep_older_generations
        ),
        max_unpinned_bytes=context.config.workspace.unpinned_generation_budget_bytes,
        dry_run=args.dry_run,
    )
    for generation_id in removed:
        print(generation_id)
    return ExitCode.SUCCESS


def _history(args: argparse.Namespace) -> int:
    if args.generations:
        _context, workspace = _owned_workspace(args.path, args.explicit_workspace)
        report = build_generation_history(workspace)
    else:
        target = Path(args.path).expanduser()
        config_root = find_project_root(target)
        config = load_config_file(args.config) if args.config else load_project_config(config_root)
        report = build_git_history(discover_context(target, config, project_root=config_root))
    rendered = render_change_report(report, markdown=args.markdown)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    if report.completeness is Completeness.COMPLETE:
        return ExitCode.SUCCESS
    if report.completeness is Completeness.FAILED:
        return ExitCode.EPROTO
    return ExitCode.ENODATA


def _coverage_import(args: argparse.Namespace) -> int:
    report = load_json(args.report.read_text(encoding="utf-8"))
    source_root = args.source_root.expanduser().resolve() if args.source_root else None
    artifact_hashes = tuple(
        hashlib.sha256(path.read_bytes()).hexdigest() for path in args.coverage_data
    )
    workload_identity = hashlib.sha256("\x1f".join(artifact_hashes).encode()).hexdigest()
    updated = import_coverage(
        report,
        tuple(path.expanduser() for path in args.coverage_data),
        source_root=source_root,
        workload_identity_sha256=workload_identity,
        replace=args.replace,
    )
    rendered = render_json(updated, compact=args.json_compact)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    workspace = _coverage_workspace(args.report, args.workspace, source_root)
    if workspace is not None:
        artifacts = {
            "report.json": canonical_json(updated).encode(),
            **{
                f"coverage-input-{index:03d}{path.suffix}": path.read_bytes()
                for index, path in enumerate(args.coverage_data, start=1)
            },
        }
        commit_generation(
            workspace,
            kind=GenerationKind.COVERAGE,
            artifacts=artifacts,
            command=("code-analysis", "coverage-import", str(args.report)),
            interpreter_path=sys.executable,
            portable_identity_sha256=hashlib.sha256(canonical_json(updated).encode()).hexdigest(),
        )
    return _report_exit(updated, fail_on_findings=False)


def _coverage_run(args: argparse.Namespace) -> int:
    if not args.allow_execution:
        print("execution denied: coverage-run requires --allow-execution", file=sys.stderr)
        return ExitCode.EPERM
    command = tuple(args.workload)
    if not command:
        raise InvalidArgumentError("coverage-run requires a command after --")
    expected = resolve_expected_status(args.expected_status)
    additions = tuple(_parse_environment_assignment(value) for value in args.env)
    cwd = args.cwd.expanduser().resolve()
    config = load_project_config(cwd)
    if args.repeat > config.phase_four_limits.max_repetitions:
        raise InvalidArgumentError("coverage repetition count exceeds configured bound")
    print(
        "warning: executing untrusted project code for explicit coverage collection",
        file=sys.stderr,
    )
    capture = run_coverage_workload(
        command,
        cwd=cwd,
        timeout_seconds=args.timeout,
        repeat=args.repeat,
        expected_status=expected,
        environment_allowlist=config.runtime.environment_allowlist,
        environment_additions=additions,
    )
    context = discover_context(cwd, config)
    workspace = prepare_workspace(context, explicit_path=args.workspace)
    base = load_json(args.report.read_text(encoding="utf-8"))
    updated = base
    import_payloads = [payload for payload in capture.coverage_json if payload]
    if import_payloads:
        with tempfile.TemporaryDirectory(prefix="code-analysis-coverage-import-") as temporary:
            paths = []
            for index, payload in enumerate(import_payloads, start=1):
                path = Path(temporary) / f"coverage-{index:03d}.json"
                path.write_bytes(payload)
                paths.append(path)
            updated = import_coverage(
                base,
                tuple(paths),
                source_root=cwd,
                workload_identity_sha256=capture.workload_identity_sha256,
                limits=config.phase_four_limits,
            )
    statuses = tuple(item.status for item in capture.results)
    timed_out = any(item.timed_out for item in capture.results)
    unexpected = any(status != expected for status in statuses)
    stdout_payload = b"".join(item.stdout for item in capture.results)
    stderr_payload = b"".join(item.stderr for item in capture.results)
    run_provider = ProviderResult(
        provider_id="coverage-run",
        state=(
            ProviderState.TIMEOUT
            if timed_out
            else ProviderState.NONZERO if unexpected else ProviderState.SUCCESS
        ),
        executable="python-current-interpreter",
        version=sys.version.split()[0],
        exit_code=statuses[-1] if statuses else None,
        duration_ms=int(sum(item.wall_seconds for item in capture.results) * 1000),
        stdout_sha256=hashlib.sha256(stdout_payload).hexdigest(),
        stderr_sha256=hashlib.sha256(stderr_payload).hexdigest(),
        observations=("explicit-workload", "owned-process-session"),
        completeness=(Completeness.FAILED if timed_out or unexpected else updated.completeness),
    )
    updated = updated.model_copy(
        update={
            "providers": (
                *(item for item in updated.providers if item.provider_id != "coverage-run"),
                run_provider,
            ),
            "completeness": (
                Completeness.FAILED if timed_out or unexpected else updated.completeness
            ),
        }
    )
    portable_json = canonical_json(updated)
    local_artifacts = {
        "report.json": portable_json.encode(),
        "execution.json": json.dumps(
            {"expected_status": expected, "observed_statuses": statuses},
            indent=2,
            sort_keys=True,
        ).encode()
        + b"\n",
    }
    for index, result in enumerate(capture.results, start=1):
        local_artifacts[f"stdout-{index:03d}.bin"] = result.stdout
        local_artifacts[f"stderr-{index:03d}.bin"] = result.stderr
        if capture.coverage_data[index - 1]:
            local_artifacts[f"coverage-{index:03d}.data"] = capture.coverage_data[index - 1]
        if capture.coverage_json[index - 1]:
            local_artifacts[f"coverage-{index:03d}.json"] = capture.coverage_json[index - 1]
    commit_generation(
        workspace,
        kind=GenerationKind.COVERAGE,
        artifacts=local_artifacts,
        command=("code-analysis", "coverage-run", *command),
        interpreter_path=sys.executable,
        portable_identity_sha256=hashlib.sha256(portable_json.encode()).hexdigest(),
    )
    if args.output:
        args.output.write_text(render_json(updated, compact=args.json_compact), encoding="utf-8")
    elif not args.quiet:
        sys.stdout.write(render_json(updated, compact=args.json_compact))
    if timed_out:
        return ExitCode.ETIMEDOUT
    if unexpected:
        return ExitCode.EPROTO
    return _report_exit(updated, fail_on_findings=False)


def _profile(args: argparse.Namespace) -> int:
    if not args.allow_execution:
        print("execution denied: profile requires --allow-execution", file=sys.stderr)
        return ExitCode.EPERM
    command = tuple(args.workload)
    if not command:
        raise InvalidArgumentError("profile requires a command after --")
    if args.timeout <= 0:
        raise InvalidArgumentError("--timeout must be greater than zero")
    expected = resolve_expected_status(args.expected_status)
    additions = tuple(_parse_environment_assignment(value) for value in args.env)
    cwd = args.cwd.expanduser().resolve()
    config = load_project_config(cwd)
    source_report = load_json(args.source_report.read_text(encoding="utf-8"))
    print(
        "warning: executing untrusted project code for explicit profiling",
        file=sys.stderr,
    )
    capture = profile_workload(
        source_report,
        command,
        cwd=cwd,
        config=config,
        expected_status=expected,
        expected_status_name=args.expected_status,
        repeat=args.repeat,
        timeout_seconds=args.timeout,
        environment_additions=additions,
    )
    workspace = prepare_workspace(discover_context(cwd, config), explicit_path=args.workspace)
    commit_generation(
        workspace,
        kind=GenerationKind.PROFILE,
        artifacts=capture.local_artifacts,
        command=("code-analysis", "profile", *command),
        interpreter_path=sys.executable,
        portable_identity_sha256=capture.report.profile_identity_sha256,
    )
    rendered = render_profile_json(capture.report, compact=args.json_compact)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    elif not args.quiet:
        sys.stdout.write(rendered)
    if capture.timed_out:
        return ExitCode.ETIMEDOUT
    if capture.unexpected_status:
        return ExitCode.EPROTO
    if capture.report.completeness != Completeness.COMPLETE:
        return ExitCode.ENODATA
    return ExitCode.SUCCESS


def _profile_report(args: argparse.Namespace) -> int:
    report = _load_profile_path(
        args.profile,
        source_report_path=args.source_report,
        source_root=args.source_root,
    )
    renderings = {
        "json": render_profile_json(report),
        "json-compact": render_profile_json(report, compact=True),
        "markdown": render_profile_markdown(report),
        "tsv": render_profile_tsv(report),
        "dot": render_profile_graph(report, mermaid=False),
        "mermaid": render_profile_graph(report, mermaid=True),
    }
    if args.format.startswith("overlay-"):
        if args.static_report is None:
            raise InvalidArgumentError("static/runtime overlay requires --static-report")
        static_report = load_json(args.static_report.read_text(encoding="utf-8"))
        rendered = render_static_runtime_overlay(
            report, static_report, mermaid=args.format == "overlay-mermaid"
        )
    else:
        rendered = renderings[args.format]
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return ExitCode.SUCCESS if report.completeness == Completeness.COMPLETE else ExitCode.ENODATA


def _profile_diff(args: argparse.Namespace) -> int:
    baseline = _load_profile_path(args.baseline)
    candidate = _load_profile_path(args.candidate)
    report = build_profile_diff(baseline, candidate, diagnostic_only=args.diagnostic_only)
    rendered = render_profile_diff(report, markdown=args.markdown)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return ExitCode.SUCCESS if report.claim_eligible else ExitCode.ENODATA


def _load_profile_path(
    path: Path,
    *,
    source_report_path: Path | None = None,
    source_root: Path | None = None,
):
    selected = path / "profile.json" if path.is_dir() else path
    if selected.suffix == ".prof":
        source_report = (
            load_json(source_report_path.read_text(encoding="utf-8"))
            if source_report_path is not None
            else None
        )
        root = source_root.expanduser().resolve() if source_root is not None else None
        config = load_project_config(root or Path.cwd())
        return normalize_profile_artifact(
            selected,
            source_report=source_report,
            source_root=root,
            config=config,
        )
    return load_profile_json(selected.read_text(encoding="utf-8"))


def _parse_environment_assignment(value: str) -> tuple[str, str]:
    name, separator, assignment = value.partition("=")
    if not separator or not name or not name.replace("_", "A").isalnum():
        raise InvalidArgumentError("--env requires NAME=VALUE")
    return name, assignment


def _coverage_workspace(
    report_path: Path, explicit: Path | None, source_root: Path | None
) -> WorkspacePaths | None:
    if explicit is not None:
        return open_workspace(explicit)
    for parent in report_path.expanduser().absolute().parents:
        if (parent / "workspace.json").is_file():
            return open_workspace(parent)
    if source_root is not None:
        config = load_project_config(source_root)
        return prepare_workspace(discover_context(source_root, config))
    return None


def _summary(path: Path, max_findings: int | None) -> int:
    report = load_json(path.read_text(encoding="utf-8"))
    sys.stdout.write(render_markdown(report, finding_limit=max_findings))
    return _report_exit(report, fail_on_findings=False)


def _html(args: argparse.Namespace) -> int:
    from code_analysis.reports.html_report import render_html_document
    from code_analysis.web_service import ReportViewService

    if not 1 <= args.max_records <= 500:
        raise InvalidArgumentError("HTML records must be between 1 and 500")
    inputs = {args.report.resolve()}
    if args.compare is not None:
        inputs.add(args.compare.resolve())
    if args.output.resolve() in inputs:
        raise InvalidArgumentError("HTML output must not overwrite an input report")
    names = tuple(
        dict.fromkeys(
            args.projection or ["modules", "complexity", "findings", "dependencies", "health"]
        )
    )
    if len(names) > 12:
        raise InvalidArgumentError("HTML supports at most twelve selected projections")
    service = ReportViewService.load(args.report)
    comparison = ReportViewService.load(args.compare).primary if args.compare is not None else None
    try:
        projections = tuple(
            build_projection(
                service.primary,
                ProjectionName(name),
                max_records=args.max_records,
                max_nodes=50,
                max_edges=100,
            )
            for name in names
        )
    except ProjectionBuildError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    if comparison is not None:
        try:
            projections += (
                build_report_delta(service.primary, comparison, max_records=args.max_records),
            )
        except ComparisonBuildError as exc:
            raise InvalidArgumentError(str(exc)) from exc
    document = render_html_document("Code Analysis Suite — saved report", projections)
    args.output.write_text(document, encoding="utf-8")
    for report in (service.primary, comparison):
        if report is not None:
            status = _report_exit(report, fail_on_findings=False)
            if status != ExitCode.SUCCESS:
                return status
    return (
        ExitCode.ENODATA
        if any(
            item.truncated or item.completeness is not Completeness.COMPLETE for item in projections
        )
        else ExitCode.SUCCESS
    )


def _web(args: argparse.Namespace) -> int:
    from code_analysis.display_preferences import load_display_preferences

    preferences = load_display_preferences(getattr(args, "display_config", None))
    if getattr(args, "background", False):
        from code_analysis.web_launcher import launch_options, start_session

        if args.host not in {"127.0.0.1", "localhost", "::1"} or not 0 <= args.port <= 65535:
            raise InvalidArgumentError("UI requires a loopback host and port between 0 and 65535")
        record, started = start_session(launch_options(args))
        print(
            f"Code Analysis UI {'started' if started else 'reopened'}: {record.url}",
            file=sys.stderr,
        )
        print(f"Stop: code-analysis ui-stop --session {record.session_id}", file=sys.stderr)
        print(
            f"Private server log: {record.session_id}.log in the UI state directory",
            file=sys.stderr,
        )
        if args.open:
            _open_web_url(record.url, preferences.browser_command)
        return ExitCode.SUCCESS
    if getattr(args, "allow_collection", False) and not getattr(args, "collection_root", None):
        raise InvalidArgumentError("--allow-collection requires --collection-root")
    if getattr(args, "collection_python", ()) and not getattr(args, "collection_root", None):
        raise InvalidArgumentError("--collection-python requires --collection-root")
    from code_analysis.web_server import create_server
    from code_analysis.web_service import ReportViewService

    try:
        service = (
            ReportViewService.load(args.report, args.compare) if args.report is not None else None
        )
    except ComparisonBuildError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    workspace = getattr(args, "workspace", None)
    if workspace is None and args.report is not None:
        for parent in args.report.absolute().parents:
            if parent.name == ".code-analysis" and (parent / "workspace.json").is_file():
                workspace = parent
                break
    managed = getattr(args, "managed_session", None)
    server = create_server(
        service,
        host=args.host,
        port=args.port,
        workspace=workspace,
        source_root=getattr(args, "source_root", None),
        profile_report=getattr(args, "profile_report", None),
        compare_profile=getattr(args, "compare_profile", None),
        collection_root=getattr(args, "collection_root", None),
        allow_collection=getattr(args, "allow_collection", False),
        collection_pythons=tuple(getattr(args, "collection_python", ())),
        **({"token": managed.token} if managed is not None else {}),
    )
    if managed is not None:
        server.instance_id = managed.instance_id
    server.shutdown_enabled = True
    host, port = server.server_address[:2]
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else str(host)
    if ":" in display_host:
        display_host = f"[{display_host}]"
    url = f"http://{display_host}:{port}/?token={server.app.token}"
    mode = "collection-enabled" if getattr(args, "allow_collection", False) else "read-only"
    print(f"Code Analysis {mode} UI: {url}", file=sys.stderr)
    if managed is None:
        print("Press Ctrl-C to stop.", file=sys.stderr)
    try:
        if managed is not None:
            import signal
            import threading

            from code_analysis.web_launcher import publish_session

            managed = publish_session(managed, port)
            signal.signal(
                signal.SIGTERM,
                lambda *_: threading.Thread(target=server.shutdown, daemon=True).start(),
            )
        if args.open:
            _open_web_url(url, preferences.browser_command)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if managed is not None:
            from code_analysis.web_launcher import remove_session

            remove_session(managed)
    return ExitCode.SUCCESS


def _ui(args: argparse.Namespace) -> int:
    explicit_collection_root = getattr(args, "collection_root", None)
    if getattr(args, "collection_python", ()) and explicit_collection_root is None:
        raise InvalidArgumentError("--collection-python requires --collection-root")
    allow_collection = getattr(args, "allow_collection", True)
    collection_root = explicit_collection_root or Path.cwd().expanduser().absolute()
    report = _resolve_ui_report(args.report)
    comparison = _resolve_report_input(args.compare) if args.compare is not None else None
    if report is None and comparison is not None:
        raise InvalidArgumentError("a comparison requires an initial saved report")
    open_browser = args.open_browser
    if open_browser is None:
        from code_analysis.display_preferences import load_display_preferences

        preferences = load_display_preferences(getattr(args, "display_config", None))
        open_browser = preferences.browser_auto_open
        if open_browser is None:
            open_browser = sys.platform == "darwin"
    return _web(
        argparse.Namespace(
            report=report,
            compare=comparison,
            host=args.host,
            port=args.port,
            open=open_browser,
            workspace=getattr(args, "workspace", None),
            source_root=getattr(args, "source_root", None),
            profile_report=getattr(args, "profile_report", None),
            compare_profile=getattr(args, "compare_profile", None),
            collection_root=collection_root,
            allow_collection=allow_collection,
            collection_python=tuple(getattr(args, "collection_python", ())),
            display_config=getattr(args, "display_config", None),
            background=not getattr(args, "foreground", False),
        )
    )


def _ui_stop(args: argparse.Namespace) -> int:
    from code_analysis.web_launcher import stop_session

    record = stop_session(args.session, cancel_jobs=args.cancel_jobs)
    print(f"Stopped Code Analysis UI session {record.session_id}", file=sys.stderr)
    return ExitCode.SUCCESS


def _resolve_ui_report(report: Path | None, *, cwd: Path | None = None) -> Path | None:
    if report is not None:
        return _resolve_report_input(report)
    starting_directory = (cwd or Path.cwd()).expanduser().absolute()
    for directory in (starting_directory, *starting_directory.parents):
        workspace_root = directory / ".code-analysis"
        if not workspace_root.exists() and not workspace_root.is_symlink():
            continue
        workspace = open_workspace(workspace_root)
        generations = list_generations(workspace, kind=GenerationKind.ANALYSIS)
        if not generations:
            return None
        latest = max(generations, key=lambda item: item.generation_id)
        if "report.json" not in {artifact.name for artifact in latest.artifacts}:
            raise OSError(
                errno.EBADMSG,
                "the latest analysis generation has no report.json artifact",
                latest.generation_id,
            )
        return (
            workspace.kind_directory(GenerationKind.ANALYSIS) / latest.generation_id / "report.json"
        )
    return None


def _resolve_report_input(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    if candidate.is_dir():
        candidate = candidate / "report.json"
    if not candidate.is_file():
        raise FileNotFoundError(errno.ENOENT, "analysis report was not found", str(candidate))
    return candidate


def _open_web_url(url: str, browser_command: ViewerCommand | None = None) -> None:
    from code_analysis.artifact_viewer import ViewerCommand, launch_display

    if browser_command is not None:
        launch_display(url, browser_command)
        return
    command = "open" if sys.platform == "darwin" else "xdg-open"
    executable = shutil.which(command)
    if executable is None:
        raise OSError(errno.ENOENT, f"browser launcher is unavailable: {command}")
    launch_display(url, ViewerCommand(arguments=(executable,)))


def _candidates(args: argparse.Namespace) -> int:
    if not 1 <= args.top <= 10_000:
        raise InvalidArgumentError("--top must be between 1 and 10000")
    if not 0 <= args.min_shared_dimensions <= 32:
        raise InvalidArgumentError("--min-shared-dimensions must be between 0 and 32")
    states = set(args.state)
    if "reviewable" in states:
        states.remove("reviewable")
        states.update(("reviewable-incomplete", "ready-for-maintainer-review"))
    report = load_json(args.report.read_text(encoding="utf-8"))
    decisions = {}
    selected_ledger_sha256 = None
    if args.workspace is not None:
        ledger = load_ledger(open_workspace(args.workspace))
        decisions = effective_decisions(ledger, report)
        selected_ledger_sha256 = ledger_sha256(ledger)
    try:
        selection = ProjectionSelection(
            max_records=args.top,
            candidate_module_patterns=tuple(sorted(set(args.module_filter))),
            candidate_exclude_patterns=tuple(sorted(set(args.exclude))),
            candidate_kinds=tuple(
                sorted(
                    {RefactorCandidateKind(item) for item in args.kind}, key=lambda item: item.value
                )
            ),
            candidate_states=tuple(sorted(states)),
            candidate_decisions=tuple(sorted(set(args.decision))),
            cross_module_only=args.cross_module_only,
            production_only=args.production_only,
            min_shared_dimensions=args.min_shared_dimensions,
            prefer_existing_owner=args.prefer_existing_owner,
            languages=tuple(sorted({SourceLanguage(item) for item in args.language})),
        )
        projection = build_candidate_worklist_projection(
            report, selection, decisions, selected_ledger_sha256
        )
        rendered = render_projection(projection, ProjectionFormat(args.format))
    except (ProjectionBuildError, ProjectionRenderError, ValidationError) as exc:
        raise InvalidArgumentError(str(exc)) from exc
    sys.stdout.write(rendered)
    report_exit = _report_exit(report, fail_on_findings=False)
    return (
        report_exit
        if report_exit != ExitCode.SUCCESS
        else ExitCode.ENODATA if projection.truncated else ExitCode.SUCCESS
    )


def _decision_workspace(path: Path, explicit: Path | None, *, create: bool) -> WorkspacePaths:
    project_root = find_project_root(path)
    config = load_project_config(project_root)
    context = discover_context(path, config, project_root=project_root)
    root = explicit if explicit is not None else context.source.physical_root / ".code-analysis"
    return prepare_workspace(context, explicit_path=explicit) if create else open_workspace(root)


def _decisions(args: argparse.Namespace) -> int:
    create = args.decision_command in {"set", "import"}
    workspace = _decision_workspace(args.path, args.workspace, create=create)
    if args.decision_command == "list":
        ledger = load_ledger(workspace)
        records = tuple(
            item.record
            for item in sorted(
                effective_decisions(ledger).values(),
                key=lambda item: (item.record.candidate_id, item.record.decision_id),
            )
        )
        if args.json:
            payload = {
                "schema_name": ledger.schema_name,
                "schema_version": ledger.schema_version,
                "target_identity_sha256": ledger.target_identity_sha256,
                "effective_records": [item.model_dump(mode="json") for item in records],
            }
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write("candidate\tdecision\treason\tdecision_id\n")
            for record in records:
                sys.stdout.write(
                    f"{record.candidate_id}\t{record.decision.value}\t"
                    f"{record.reason}\t{record.decision_id}\n"
                )
        return ExitCode.SUCCESS
    if args.decision_command == "set":
        report = load_json(args.report.read_text(encoding="utf-8"))
        record = set_decision(
            workspace,
            report,
            candidate_id=args.candidate_id,
            proposal_id=args.proposal,
            decision=MaintainerDecision(args.decision),
            reason=args.reason,
        )
        sys.stdout.write(json.dumps(record.model_dump(mode="json"), sort_keys=True) + "\n")
        return ExitCode.SUCCESS
    if args.decision_command == "export":
        export_ledger(workspace, args.output)
        return ExitCode.SUCCESS
    ledger = import_ledger(
        workspace,
        args.input,
        conflict=args.conflict,
        dry_run=args.dry_run,
    )
    sys.stdout.write(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "record_count": len(ledger.records),
                "ledger_sha256": ledger_sha256(ledger),
            },
            sort_keys=True,
        )
        + "\n"
    )
    return ExitCode.SUCCESS


def _query(args: argparse.Namespace) -> int:
    selected_path = args.report / "profile.json" if args.report.is_dir() else args.report
    text = selected_path.read_text(encoding="utf-8")
    schema_name = json.loads(text).get("schema_name")
    if args.dependency_path is not None:
        selector = QuerySelector(
            kind=QuerySelectorKind.DEPENDENCY_PATH,
            identity=args.dependency_path[0],
            target_identity=args.dependency_path[1],
        )
    else:
        kind, identity = next(
            (QuerySelectorKind(name.replace("_", "-")), getattr(args, name))
            for name in (
                "contract",
                "shape",
                "structure",
                "finding",
                "clone",
                "duplicate",
                "call_site",
                "responsibility",
                "proposal",
                "refactor_candidate",
                "symbol",
                "test",
                "coverage",
                "git_change",
                "generation",
                "profile_frame",
                "runtime_edge",
            )
            if getattr(args, name) is not None
        )
        selector = QuerySelector(kind=kind, identity=identity)
    try:
        if schema_name == "code-analysis-profile-report":
            if args.after is not None:
                raise QueryBuildError("profile query continuation is not supported")
            response = build_profile_query(load_profile_json(text), selector, limit=args.limit)
            report_exit = (
                ExitCode.SUCCESS
                if response.source_completeness is Completeness.COMPLETE
                else ExitCode.ENODATA
            )
        else:
            report = load_json(text)
            response = build_query(report, selector, limit=args.limit, after=args.after)
            report_exit = _report_exit(report, fail_on_findings=False)
    except QueryBuildError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    rendered = (
        render_query_markdown(response)
        if args.markdown
        else render_query_json(response, compact=args.json_compact)
    )
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return report_exit


def _diff(args: argparse.Namespace) -> int:
    baseline = load_json(args.baseline.read_text(encoding="utf-8"))
    candidate = load_json(args.candidate.read_text(encoding="utf-8"))
    try:
        projection = build_report_delta(
            baseline,
            candidate,
            max_records=args.max_records,
            include_unchanged=args.include_unchanged,
        )
        rendered = render_projection(projection, ProjectionFormat(args.format))
    except (ComparisonBuildError, ProjectionRenderError) as exc:
        raise InvalidArgumentError(str(exc)) from exc
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered, encoding="utf-8")
    baseline_exit = _report_exit(baseline, fail_on_findings=False)
    if baseline_exit != ExitCode.SUCCESS:
        return baseline_exit
    candidate_exit = _report_exit(candidate, fail_on_findings=False)
    if candidate_exit != ExitCode.SUCCESS:
        return candidate_exit
    return ExitCode.ENODATA if projection.truncated else ExitCode.SUCCESS


def _regress(args: argparse.Namespace) -> int:
    baseline = load_json(args.baseline.read_text(encoding="utf-8"))
    candidate = load_json(args.candidate.read_text(encoding="utf-8"))
    decisions = {}
    if args.workspace is not None:
        ledger = load_ledger(open_workspace(args.workspace))
        decisions = effective_decisions(ledger, candidate)
    try:
        report = build_regression_report(
            baseline,
            candidate,
            max_records=args.top,
            decisions=decisions,
        )
        rendered = render_regression(report, args.format)
    except ComparisonBuildError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return (
        ExitCode.ENODATA
        if report.completeness is not Completeness.COMPLETE or report.truncated
        else ExitCode.SUCCESS
    )


def _semantic_nominate(args: argparse.Namespace) -> int:
    source = load_json(args.report.read_text(encoding="utf-8"))
    try:
        report = build_semantic_nomination_report(
            source,
            args.model_directory,
            max_nominations=args.top,
        )
    except SemanticProviderUnavailable as exc:
        print(f"semantic provider unavailable: {exc}", file=sys.stderr)
        return ExitCode.ENODATA
    except ComparisonBuildError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    rendered = render_semantic_nomination(report, compact=args.json_compact)
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return (
        ExitCode.ENODATA
        if report.completeness is not Completeness.COMPLETE or report.omitted_pairs
        else ExitCode.SUCCESS
    )


def _export(
    path: Path,
    projection_name: ProjectionName,
    output_format: ProjectionFormat,
    max_records: int,
    max_nodes: int,
    max_edges: int,
    collapse: ProjectionCollapse,
    selected_ids: tuple[str, ...],
    edge_kinds: tuple[str, ...],
    depth: int | None,
    module_prefixes: tuple[str, ...],
    finding_categories: tuple[ProjectionFindingCategory, ...],
    finding_severities: tuple[ProjectionFindingSeverity, ...],
    finding_rule_ids: tuple[str, ...],
    languages: tuple[SourceLanguage, ...],
    candidate_kinds: tuple[RefactorCandidateKind, ...],
    output: Path | None,
) -> int:
    if not 1 <= max_records <= 100_000:
        raise InvalidArgumentError("--max-records must be between 1 and 100000")
    if not 1 <= max_nodes <= 100_000:
        raise InvalidArgumentError("--max-nodes must be between 1 and 100000")
    if not 0 <= max_edges <= 250_000:
        raise InvalidArgumentError("--max-edges must be between 0 and 250000")
    if depth is not None and not 0 <= depth <= 100:
        raise InvalidArgumentError("--depth must be between 0 and 100")
    report = load_json(path.read_text(encoding="utf-8"))
    try:
        projection = build_projection(
            report,
            projection_name,
            max_records=max_records,
            max_nodes=max_nodes,
            max_edges=max_edges,
            collapse=collapse,
            selected_ids=selected_ids,
            edge_kinds=edge_kinds,
            depth=depth,
            module_prefixes=module_prefixes,
            finding_categories=finding_categories,
            finding_severities=finding_severities,
            finding_rule_ids=finding_rule_ids,
            languages=languages,
            candidate_kinds=candidate_kinds,
        )
    except (ProjectionBuildError, ValidationError) as exc:
        raise InvalidArgumentError(str(exc)) from exc
    try:
        if output_format in {ProjectionFormat.SVG, ProjectionFormat.PNG}:
            rendered_binary = render_projection_binary(projection, output_format)
            if output is None:
                sys.stdout.buffer.write(rendered_binary)
            else:
                output.write_bytes(rendered_binary)
            report_exit = _report_exit(report, fail_on_findings=False)
            return (
                report_exit
                if report_exit != ExitCode.SUCCESS
                else ExitCode.ENODATA if projection.truncated else ExitCode.SUCCESS
            )
        rendered = render_projection(projection, output_format)
    except ProjectionRenderError as exc:
        raise InvalidArgumentError(str(exc)) from exc
    if output is None:
        sys.stdout.write(rendered)
    else:
        output.write_text(rendered, encoding="utf-8")
    report_exit = _report_exit(report, fail_on_findings=False)
    return (
        report_exit
        if report_exit != ExitCode.SUCCESS
        else ExitCode.ENODATA if projection.truncated else ExitCode.SUCCESS
    )


def _scan(args: argparse.Namespace) -> int:
    started_at = datetime.now(UTC)
    started = time.monotonic()
    target_path = Path(args.path).expanduser()
    config_root = find_project_root(target_path)
    base = load_config_file(args.config) if args.config else load_project_config(config_root)
    additional_excludes = tuple(args.exclude_dir)
    if any(
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(character) < 32 for character in name)
        for name in additional_excludes
    ):
        raise InvalidArgumentError("--exclude-dir requires an exact directory name, not a path")
    if additional_excludes:
        base = AnalysisConfig.model_validate(
            {
                **base.model_dump(),
                "excludes": tuple(dict.fromkeys((*base.excludes, *additional_excludes))),
            }
        )
    only_local = (
        False if args.all_imports else True if args.only_local_imports else base.only_local_imports
    )
    selected_languages = (
        tuple(LanguageSelection(item) for item in args.language)
        if args.language is not None
        else None
    )
    if selected_languages is not None and (
        len(set(selected_languages)) != len(selected_languages)
        or (
            LanguageSelection.AUTO in selected_languages
            and selected_languages != (LanguageSelection.AUTO,)
        )
    ):
        raise InvalidArgumentError("--language values must be unique and auto cannot be combined")
    config = AnalysisConfig.with_overrides(
        strictness=Strictness(args.strictness) if args.strictness else None,
        function_loc=args.god_loc,
        branches=args.god_branches,
        nesting=args.god_nesting,
        arguments=args.god_args,
        weighted_score=args.god_score,
        only_local_imports=only_local,
        max_findings=args.max_findings,
        symbol_filter=args.symbol_filter,
        languages=selected_languages,
        javascript_module_mode=(
            JavaScriptModuleMode(args.javascript_module_mode)
            if args.javascript_module_mode is not None
            else None
        ),
        base=base,
    )
    context = discover_context(target_path, config, project_root=config_root)
    configured_workspace = (
        Path(config.workspace.path).expanduser() if config.workspace.path is not None else None
    )
    if configured_workspace is not None and not configured_workspace.is_absolute():
        configured_workspace = context.source.physical_root / configured_workspace
    workspace_enabled = not args.no_workspace and (
        args.workspace is not None or config.workspace.enabled
    )
    workspace = None
    cache: ContentCache | None = None
    cache_statistics = CacheStatistics(enabled=config.workspace.cache_enabled)
    if workspace_enabled:
        workspace = prepare_workspace(context, explicit_path=args.workspace or configured_workspace)
        if config.workspace.cache_enabled:
            cache = ContentCache(workspace, maximum_bytes=config.workspace.cache_max_bytes)
            cache_statistics = CacheStatistics(
                enabled=True,
                evictions=cache.evict(),
                bytes=cache.size_bytes(),
            )
    report = (
        scan_clones(context, cache)
        if args.command == "clones"
        else scan_refactor(context, cache) if args.command == "refactor" else scan(context, cache)
    )
    if args.max_items is not None and args.verbose:
        print(
            "note: --max-items is accepted for migration but does not truncate reports",
            file=sys.stderr,
        )
    report_json = canonical_json(report)
    report_markdown = render_markdown(report)
    if args.json or args.json_compact:
        rendered = render_json(report, compact=args.json_compact)
    elif args.command == "clones":
        rendered = render_projection(
            build_projection(report, ProjectionName.CLONE_CLUSTERS),
            ProjectionFormat.MARKDOWN,
        )
    elif args.command == "refactor":
        rendered = render_projection(
            build_projection(report, ProjectionName.REFACTOR_PACKET),
            ProjectionFormat.MARKDOWN,
        )
    else:
        rendered = report_markdown
    if args.only_refactor_targets and not args.json and not args.json_compact:
        rendered = render_markdown(report, finding_limit=args.max_findings)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    elif not args.quiet:
        sys.stdout.write(rendered)
    if workspace is not None:
        if cache is not None:
            cache_statistics = CacheStatistics(
                enabled=True,
                hits=cache.hits,
                misses=cache.misses,
                invalidations=cache.invalidations,
                evictions=cache_statistics.evictions,
                bytes=cache.size_bytes(),
            )
        report_sha256 = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
        run = create_run_record(
            context,
            workspace,
            started_at=started_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            command=("code-analysis", args.command, str(target_path)),
            providers=inventory_tool_paths(),
            portable_report_sha256=report_sha256,
            interpreter_path=sys.executable,
            cache=cache_statistics,
        )
        write_workspace_results(
            workspace,
            report_json=report_json,
            report_markdown=report_markdown,
            run=run,
        )
    for diagnostic in report.diagnostics:
        print(
            f"{diagnostic.level.value}: {diagnostic.code}: {diagnostic.message}",
            file=sys.stderr,
        )
    return _report_exit(report, fail_on_findings=args.fail_on_findings)


def _contracts(args: argparse.Namespace) -> int:
    return _focused_analysis(args, dictionary=False)


def _dict_shapes(args: argparse.Namespace) -> int:
    return _focused_analysis(args, dictionary=True)


def _focused_analysis(args: argparse.Namespace, *, dictionary: bool) -> int:
    started_at = datetime.now(UTC)
    started = time.monotonic()
    target_path = Path(args.path).expanduser()
    config_root = find_project_root(target_path)
    base = load_config_file(args.config) if args.config else load_project_config(config_root)
    only_local = (
        False if args.all_imports else True if args.only_local_imports else base.only_local_imports
    )
    selected_languages = tuple(LanguageSelection(item) for item in (args.language or ()))
    if len(set(selected_languages)) != len(selected_languages) or (
        LanguageSelection.AUTO in selected_languages
        and selected_languages != (LanguageSelection.AUTO,)
    ):
        raise InvalidArgumentError("--language values must be unique and auto cannot be combined")
    if any(
        item not in {LanguageSelection.AUTO, LanguageSelection.PYTHON}
        for item in selected_languages
    ):
        raise InvalidArgumentError(
            "contracts and dict-shapes support only auto or python language selection"
        )
    config = AnalysisConfig.with_overrides(
        only_local_imports=only_local,
        languages=(LanguageSelection.PYTHON,),
        base=base,
    )
    context = discover_context(target_path, config, project_root=config_root)
    configured_workspace = (
        Path(config.workspace.path).expanduser() if config.workspace.path is not None else None
    )
    if configured_workspace is not None and not configured_workspace.is_absolute():
        configured_workspace = context.source.physical_root / configured_workspace
    workspace_enabled = not args.no_workspace and (
        args.workspace is not None or config.workspace.enabled
    )
    workspace = None
    cache_statistics = CacheStatistics(enabled=config.workspace.cache_enabled)
    if workspace_enabled:
        workspace = prepare_workspace(context, explicit_path=args.workspace or configured_workspace)
        if config.workspace.cache_enabled:
            cache = ContentCache(workspace, maximum_bytes=config.workspace.cache_max_bytes)
            cache_statistics = CacheStatistics(
                enabled=True,
                evictions=cache.evict(),
                bytes=cache.size_bytes(),
            )
    report = scan_dictionary_shapes(context) if dictionary else scan_contract_inventory(context)
    report_json = canonical_json(report)
    report_markdown = render_markdown(report)
    rendered = (
        render_json(report, compact=args.json_compact)
        if args.json or args.json_compact
        else report_markdown
    )
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    elif not args.quiet:
        sys.stdout.write(rendered)
    if workspace is not None:
        report_sha256 = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
        run = create_run_record(
            context,
            workspace,
            started_at=started_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            command=(
                "code-analysis",
                "dict-shapes" if dictionary else "contracts",
                str(target_path),
            ),
            providers=inventory_tool_paths(),
            portable_report_sha256=report_sha256,
            interpreter_path=sys.executable,
            cache=cache_statistics,
        )
        write_workspace_results(
            workspace,
            report_json=report_json,
            report_markdown=report_markdown,
            run=run,
        )
    for diagnostic in report.diagnostics:
        print(
            f"{diagnostic.level.value}: {diagnostic.code}: {diagnostic.message}",
            file=sys.stderr,
        )
    return _report_exit(report, fail_on_findings=False)


def _report_exit(report: object, *, fail_on_findings: bool) -> int:
    from code_analysis.report_models import AnalysisReport

    if not isinstance(report, AnalysisReport):
        return ExitCode.EFAULT
    if any(
        provider.state
        in {
            ProviderState.FAILED,
            ProviderState.NONZERO,
            ProviderState.TIMEOUT,
            ProviderState.MALFORMED,
        }
        for provider in report.providers
    ):
        return ExitCode.EPROTO
    if report.completeness is not Completeness.COMPLETE:
        return ExitCode.ENODATA
    if fail_on_findings and report.findings:
        return ExitCode.EPERM
    return ExitCode.SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
