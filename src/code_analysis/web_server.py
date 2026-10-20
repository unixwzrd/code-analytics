"""Loopback-only HTTP transport for the read-only report interface."""

from __future__ import annotations

import errno
import json
import os
import platform
import secrets
import socket
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import TypeAdapter, ValidationError

from code_analysis.collection_commands import (
    CollectionContext,
    CollectionPlanner,
    CollectionRequest,
)
from code_analysis.collection_service import (
    CollectionJobSelection,
    CollectionLaunch,
    CollectionSession,
    CollectionWorkspace,
)
from code_analysis.comparison_browse import (
    ComparisonPageRequest,
    comparison_filters_from_params,
    comparison_page_projection,
)
from code_analysis.dependency_graph import DependencyGraphRequest
from code_analysis.dependency_navigation import DependencyRequest, DependencyResult
from code_analysis.models import Completeness, SourceLanguage, StrictModel
from code_analysis.project_browsing import ProjectDocumentAccess
from code_analysis.projection_models import (
    GraphProjection,
    ProjectionCollapse,
    ProjectionFindingCategory,
    ProjectionFindingSeverity,
)
from code_analysis.projections import ProjectionName
from code_analysis.report_models import RefactorCandidateKind
from code_analysis.reports.projection_views import (
    ProjectionFormat,
    render_projection,
    render_projection_binary,
)
from code_analysis.ui_decisions import decision_ledger_status, read_decision_snapshot
from code_analysis.ui_models import BrowseFacets, UIEvidenceBinding
from code_analysis.ui_projections import (
    UIProjectionCatalog,
    UIProjectionRequest,
    graph_node_selection,
    project_report,
)
from code_analysis.ui_runs import RunRepository, UIRunCatalog
from code_analysis.ui_runtime import (
    RuntimeComparison,
    RuntimeOverview,
    RuntimePanel,
    RuntimeViewService,
    parse_runtime_comparison_source_query,
    render_runtime_comparison_json,
    render_runtime_comparison_markdown,
    render_runtime_comparison_tsv,
    runtime_comparison_download_headers,
    runtime_comparison_source_preview,
    runtime_comparison_source_unavailable,
)
from code_analysis.web_lifecycle import UILifecycleStatus, UIShutdownRequest
from code_analysis.web_service import ReportViewService

if TYPE_CHECKING:
    from code_analysis.runtime_queries import RuntimeQueryProjection, RuntimeQueryRequest

_STATIC_ROOT: Final = Path(__file__).with_name("web_static")
_ASSETS: Final = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/evidence.js": ("evidence.js", "text/javascript; charset=utf-8"),
    "/candidate.js": ("candidate.js", "text/javascript; charset=utf-8"),
    "/projections.js": ("projections.js", "text/javascript; charset=utf-8"),
    "/heatmaps.js": ("heatmaps.js", "text/javascript; charset=utf-8"),
    "/dependencies.js": ("dependencies.js", "text/javascript; charset=utf-8"),
    "/runtime.js": ("runtime.js", "text/javascript; charset=utf-8"),
    "/comparison.js": ("comparison.js", "text/javascript; charset=utf-8"),
    "/collection.js": ("collection.js", "text/javascript; charset=utf-8"),
    "/workflow.js": ("workflow.js", "text/javascript; charset=utf-8"),
    "/help.js": ("help.js", "text/javascript; charset=utf-8"),
    "/session.js": ("session.js", "text/javascript; charset=utf-8"),
    "/collection_jobs.js": ("collection_jobs.js", "text/javascript; charset=utf-8"),
    "/code_browser.js": ("code_browser.js", "text/javascript; charset=utf-8"),
    "/project_browser.js": ("project_browser.js", "text/javascript; charset=utf-8"),
    "/collection_picker.js": ("collection_picker.js", "text/javascript; charset=utf-8"),
    "/code_drafts.js": ("code_drafts.js", "text/javascript; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/localops_adapter.js": ("localops_adapter.js", "text/javascript; charset=utf-8"),
}

# Closed asset allowlist: bundled, unmodified toolkit modules only. URL paths
# never become filesystem paths, and traversal cannot select arbitrary files.
for _name in (
    "chart.js",
    "code-editor.js",
    "content-panel.js",
    "contracts.js",
    "data-table.js",
    "dialogs.js",
    "dom.js",
    "format.js",
    "forms.js",
    "index.js",
    "manifest.js",
    "manifest-validation.js",
    "navigation.js",
    "primitives.js",
    "result-reader.js",
    "shell.js",
    "task-monitor.js",
    "styles.css",
):
    _ASSETS[f"/localops/{_name}"] = (
        f"localops/{_name}",
        "text/css; charset=utf-8" if _name.endswith(".css") else "text/javascript; charset=utf-8",
    )


@dataclass(frozen=True, slots=True)
class WebApplication:
    service: ReportViewService | None
    token: str
    runs: RunRepository | None = None
    source_root: Path | None = None
    runtime: RuntimeViewService | None = None
    collection: CollectionPlanner | None = None
    collection_session: CollectionSession | None = None
    project_documents: ProjectDocumentAccess = field(default_factory=ProjectDocumentAccess)


class ReportHTTPServer(ThreadingHTTPServer):
    app: WebApplication

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        self.address_family = socket.AF_INET6 if address[0] == "::1" else socket.AF_INET
        super().__init__(address, handler)
        self.lifecycle_lock = threading.RLock()
        self.instance_id = secrets.token_hex(16)
        self.stopping = False
        self.shutdown_enabled = False  # Library/TUI handoffs retain their own lifecycle owner.

    def lifecycle_status(self) -> UILifecycleStatus:
        with self.lifecycle_lock:
            session = self.app.collection_session
            active = (
                sum(job.state in {"queued", "running"} for job in session.catalog().records)
                if session
                else 0
            )
            return UILifecycleStatus(
                instance_id=self.instance_id,
                pid=os.getpid(),
                active_jobs=active,
                stopping=self.stopping,
                shutdown_available=self.shutdown_enabled,
            )

    def prepare_stop(self, request: UIShutdownRequest) -> UILifecycleStatus:
        with self.lifecycle_lock:
            if not self.shutdown_enabled:
                raise OSError(errno.EPERM, "this UI server's lifecycle is controlled by its owner")
            if not request.confirmed:
                raise OSError(errno.EPERM, "explicit UI shutdown confirmation required")
            status = self.lifecycle_status()
            if status.active_jobs and not request.cancel_jobs:
                raise OSError(errno.EBUSY, "active collection jobs require cancellation consent")
            self.stopping = True
            return self.lifecycle_status()

    def server_close(self) -> None:
        super().server_close()
        app = getattr(self, "app", None)
        if app is not None and app.collection_session is not None:
            app.collection_session.close()
        if app is not None and app.collection is not None:
            app.collection.close()


class ReportRequestHandler(BaseHTTPRequestHandler):
    server: ReportHTTPServer

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if not self._valid_host():
            self._error(HTTPStatus.BAD_REQUEST, "invalid-host", "loopback Host required")
            return
        if parsed.path in _ASSETS:
            self._asset(parsed.path)
            return
        if parsed.path.startswith("/help/"):
            self._help_asset(parsed.path)
            return
        if not secrets.compare_digest(
            self.headers.get("X-Code-Analysis-Token", ""), self.server.app.token
        ):
            self._error(HTTPStatus.FORBIDDEN, "invalid-token", "session token required")
            return
        try:
            params = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == "/api/session":
                if params:
                    raise ValueError("session status accepts no selectors")
                self._json(HTTPStatus.OK, self.server.lifecycle_status().model_dump(mode="json"))
                return
            if parsed.path == "/api/workflow":
                from code_analysis.ui_workflow import build_workflow

                if set(params) - {"run", "compare_run", "profile_run", "compare_profile_run"}:
                    raise ValueError("workflow accepts only static and profile run selectors")
                app = self.server.app
                service = app.service
                if {"run", "compare_run"}.intersection(params):
                    if app.runs is None:
                        raise ValueError("run selection requires an owned workspace")
                    service = app.runs.select(
                        _single(params, "run", "current"), _single(params, "compare_run", "none")
                    )
                runtime = _selected_runtime(app, params)
                context = _collection_context(app, report_available=service is not None).model_copy(
                    update={"runtime_available": runtime is not None}
                )
                result = build_workflow(
                    context,
                    service,
                    runtime,
                    source_configured=app.source_root is not None,
                    run_catalog_available=app.runs is not None,
                )
                self._json(HTTPStatus.OK, result.model_dump(mode="json"))
                return
            if parsed.path == "/api/collection":
                context = _collection_context(
                    self.server.app, report_available=self.server.app.service is not None
                )
                self._json(HTTPStatus.OK, context.model_dump(mode="json"))
                return
            if parsed.path == "/api/collection/jobs":
                session = self.server.app.collection_session
                if session is None:
                    self._error(
                        HTTPStatus.FORBIDDEN,
                        "collection-disabled",
                        "enable --allow-collection at startup",
                    )
                    return
                if params:
                    raise ValueError("job catalog does not accept query parameters")
                self._json(HTTPStatus.OK, session.catalog().model_dump(mode="json"))
                return
            if parsed.path == "/api/collection/files":
                if self.server.app.collection is None:
                    self._error(
                        HTTPStatus.FORBIDDEN,
                        "collection-disabled",
                        "select --collection-root at startup",
                    )
                    return
                if set(params) - {"path"}:
                    raise ValueError("only path applies to collection browsing")
                listing = self.server.app.collection.browse(_single(params, "path", "."))
                self._json(HTTPStatus.OK, listing.model_dump(mode="json"))
                return
            if parsed.path == "/api/profile-runs":
                from code_analysis.profile_runs import ProfileRunCatalog

                profile_catalog = (
                    self.server.app.runs.profiles()
                    if self.server.app.runs
                    else ProfileRunCatalog(records=())
                )
                self._json(HTTPStatus.OK, profile_catalog.model_dump(mode="json"))
                return
            if parsed.path == "/api/runs":
                refresh = _single(params, "refresh", "0")
                if refresh not in {"0", "1"}:
                    raise ValueError("catalog refresh must be 0 or 1")
                catalog = (
                    (
                        self.server.app.runs.refresh()
                        if refresh == "1"
                        else self.server.app.runs.catalog()
                    )
                    if self.server.app.runs
                    else UIRunCatalog(records=())
                )
                self._json(HTTPStatus.OK, catalog.model_dump(mode="json"))
                return
            service = self.server.app.service
            if service is None and not parsed.path.startswith("/api/runtime"):
                self._error(
                    HTTPStatus.CONFLICT,
                    "no-report",
                    "No saved analysis report is loaded; collection setup is available.",
                )
                return
            if parsed.path != "/api/findings" and any(
                _single(params, key, "") for key in ("severity", "category", "confidence")
            ):
                raise ValueError("finding filters require the findings view")
            if "run" in params or "compare_run" in params:
                if self.server.app.runs is None:
                    raise ValueError("run selection requires an owned workspace")
                service = self.server.app.runs.select(
                    _single(params, "run", "current"), _single(params, "compare_run", "none")
                )
            if parsed.path in {
                "/api/candidate",
                "/api/evidence",
                "/api/navigation",
                "/api/source",
            } and {"report_sha256", "decision_ledger_sha256"}.intersection(params):
                assert service is not None
                binding = UIEvidenceBinding(
                    report_sha256=_single(params, "report_sha256", ""),
                    decision_ledger_sha256=(
                        _single(params, "decision_ledger_sha256", "")
                        if "decision_ledger_sha256" in params
                        else None
                    ),
                )
                if binding.report_sha256 != service.bootstrap().reports[0].report_sha256:
                    raise ValueError("originating evidence view does not match the selected report")
                if binding.decision_ledger_sha256 is not None:
                    if self.server.app.runs is None:
                        raise ValueError("decision-bound evidence requires an owned workspace")
                    snapshot = read_decision_snapshot(
                        service.primary, self.server.app.runs.workspace
                    )
                    if (
                        snapshot is None
                        or snapshot.report_sha256 != binding.report_sha256
                        or snapshot.ledger_sha256 != binding.decision_ledger_sha256
                    ):
                        raise ValueError("originating decision snapshot changed; reselect it")
            offset = _single_int(params, "offset", 0)
            facet_routes = {
                "/api/modules",
                "/api/findings",
                "/api/state-effects",
                "/api/contracts",
                "/api/dictionary-shapes",
                "/api/candidates",
                "/api/clones",
                "/api/test-impact",
            }
            if parsed.path not in facet_routes and any(
                _single(params, key, "") for key in ("module_prefix", "owner")
            ):
                raise ValueError("module/owner facets require a supported evidence table")
            facets = BrowseFacets()
            if parsed.path not in facet_routes | {
                "/api/projection",
                "/api/download",
                "/api/projection-node",
            } and _single(params, "language", ""):
                raise ValueError("language filtering requires a supported evidence view")
            if parsed.path in facet_routes:
                language = _single(params, "language", "")
                facets = BrowseFacets(
                    language=SourceLanguage(language) if language else None,
                    module_prefix=_single(params, "module_prefix", ""),
                    owner=_single(params, "owner", ""),
                )
            limit = _single_int(params, "limit", 100)
            query = _single(params, "q", "")
            if parsed.path == "/api/handoff":
                from code_analysis.static_graph_browse import StaticGraphRequest
                from code_analysis.ui_handoff import WebHandoffSelection

                if set(params) - {"report_sha256", "kind", "identity", "graph_request"}:
                    raise ValueError("unsupported handoff parameters")
                graph_request = _single(params, "graph_request", "")
                assert service is not None
                selection = WebHandoffSelection(
                    report_sha256=_single(params, "report_sha256", ""),
                    kind=TypeAdapter[Literal["report", "finding", "symbol", "graph", "projection"]](
                        Literal["report", "finding", "symbol", "graph", "projection"]
                    ).validate_python(_single(params, "kind", "report")),
                    identity=_single(params, "identity", "") or None,
                    graph_request=(
                        StaticGraphRequest.model_validate_json(graph_request)
                        if graph_request
                        else None
                    ),
                )
                selection.validate_report(service)
                self._json(HTTPStatus.OK, selection.model_dump(mode="json"))
                return
            if parsed.path.startswith("/api/runtime"):
                from code_analysis.runtime_graph_browse import RuntimeGraphRequest

                runtime = self.server.app.runtime
                if parsed.path == "/api/runtime/handoff":
                    from code_analysis.ui_handoff import RuntimeHandoffSelection

                    if set(params) - {
                        "profile_report_sha256",
                        "profile_identity_sha256",
                        "graph_request",
                    }:
                        raise ValueError("unsupported runtime handoff parameters")
                    if runtime is None:
                        raise ValueError("no saved runtime profile is loaded for this session")
                    runtime_selection = RuntimeHandoffSelection(
                        profile_report_sha256=_single(params, "profile_report_sha256", ""),
                        profile_identity_sha256=_single(params, "profile_identity_sha256", ""),
                        graph_request=RuntimeGraphRequest.model_validate_json(
                            _single(params, "graph_request", "")
                        ),
                    )
                    runtime_selection.validate_profile(runtime)
                    self._json(HTTPStatus.OK, runtime_selection.model_dump(mode="json"))
                    return
                runtime = _selected_runtime(self.server.app, params)
                if runtime is not None and parsed.path in {
                    "/api/runtime/graph",
                    "/api/runtime/download",
                }:
                    from code_analysis.projections.runtime import build_runtime_graph
                    from code_analysis.runtime_models import RuntimeSelection

                    # The same request model the indexed reader uses, so a handed-off
                    # selection renders an identical projection on either path.
                    graph_selection = RuntimeGraphRequest(
                        max_nodes=_single_int(params, "max_nodes", 60),
                        max_edges=_single_int(params, "max_edges", 120),
                        project_only=_single_bool(params, "project_only"),
                    )
                    graph = build_runtime_graph(
                        runtime.profile, RuntimeSelection(**graph_selection.model_dump())
                    )
                    if parsed.path.endswith("/graph"):
                        self._json(HTTPStatus.OK, graph.model_dump(mode="json"))
                    else:
                        format_ = ProjectionFormat(_single(params, "format", "json"))
                        if format_ not in {
                            ProjectionFormat.JSON,
                            ProjectionFormat.MARKDOWN,
                            ProjectionFormat.DOT,
                            ProjectionFormat.MERMAID,
                            ProjectionFormat.SVG,
                            ProjectionFormat.PNG,
                            ProjectionFormat.HTML,
                        }:
                            raise ValueError("unsupported runtime graph format")
                        try:
                            payload = (
                                render_projection_binary(graph, format_)
                                if format_ in {ProjectionFormat.SVG, ProjectionFormat.PNG}
                                else render_projection(graph, format_).encode()
                            )
                        except OSError:
                            self._error(
                                HTTPStatus.SERVICE_UNAVAILABLE,
                                "renderer-unavailable",
                                "Graphviz rendering is unavailable or failed",
                            )
                            return
                        self._send_headers(HTTPStatus.OK, "application/octet-stream", len(payload))
                        self.send_header(
                            "Content-Disposition",
                            f'attachment; filename="runtime-calls.{format_.value}"',
                        )
                        self.send_header("X-Analysis-Completeness", graph.completeness.value)
                        self.send_header("X-Projection-Truncated", str(graph.truncated).lower())
                        self._emit_response_body(payload)
                    return
                if parsed.path == "/api/runtime/table-download":
                    from code_analysis.runtime_queries import (
                        render_runtime_query_json,
                        render_runtime_query_markdown,
                        render_runtime_query_tsv,
                    )

                    if runtime is None:
                        raise ValueError("no saved runtime profile supplied")
                    if unknown := set(params) - _RUNTIME_TABLE_DOWNLOAD_PARAMS:
                        raise ValueError(
                            "unsupported runtime table download parameters: "
                            + ", ".join(sorted(unknown))
                        )
                    query_request = _runtime_query_request(params)
                    panel = runtime.panel(query_request)
                    runtime_projection = panel.projection
                    table_format = _single(params, "format", "json")
                    table_renderers = {
                        "json": render_runtime_query_json,
                        "tsv": render_runtime_query_tsv,
                        "markdown": render_runtime_query_markdown,
                    }
                    if table_format not in table_renderers:
                        raise ValueError("unsupported runtime table format")
                    payload = table_renderers[table_format](runtime_projection).encode("utf-8")
                    self._send_headers(
                        HTTPStatus.OK, _RUNTIME_TABLE_MEDIA[table_format], len(payload)
                    )
                    self.send_header(
                        "Content-Disposition",
                        f'attachment; filename="runtime-{runtime_projection.section.value}'
                        f'.{table_format}"',
                    )
                    for key, value in _runtime_table_download_headers(runtime_projection).items():
                        self.send_header(key, value)
                    self._emit_response_body(payload)
                    return
                if parsed.path == "/api/runtime/comparison-download":
                    if runtime is None:
                        raise ValueError("no saved runtime profile supplied")
                    if unknown := set(params) - _RUNTIME_COMPARISON_DOWNLOAD_PARAMS:
                        raise ValueError(
                            "unsupported runtime comparison download parameters: "
                            + ", ".join(sorted(unknown))
                        )
                    comparison = runtime.comparison_view()
                    comparison_format = _single(params, "format", "json")
                    comparison_renderers = {
                        "json": lambda: render_runtime_comparison_json(comparison),
                        "tsv": lambda: render_runtime_comparison_tsv(comparison),
                        "markdown": lambda: render_runtime_comparison_markdown(
                            comparison,
                            runtime,
                            static_identity=(
                                service.bootstrap().reports[0].report_sha256 if service else None
                            ),
                        ),
                    }
                    if comparison_format not in comparison_renderers:
                        raise ValueError("unsupported runtime comparison format")
                    payload = comparison_renderers[comparison_format]().encode("utf-8")
                    self._send_headers(
                        HTTPStatus.OK, _RUNTIME_TABLE_MEDIA[comparison_format], len(payload)
                    )
                    self.send_header(
                        "Content-Disposition",
                        f'attachment; filename="runtime-comparison.{comparison_format}"',
                    )
                    for key, value in runtime_comparison_download_headers(
                        comparison, runtime
                    ).items():
                        self.send_header(key, value)
                    self._emit_response_body(payload)
                    return
                if parsed.path == "/api/runtime/comparison-source":
                    from code_analysis.runtime_source_browse import RuntimeSourceUnavailable

                    if runtime is None:
                        raise ValueError("no saved runtime profile supplied")
                    if service is None:
                        raise ValueError(
                            "runtime comparison source requires a saved analysis report"
                        )
                    comparison_source_query = parse_runtime_comparison_source_query(params)
                    compare_run = _single(params, "compare_run", "none")
                    if runtime.comparison is None or runtime.delta is None:
                        comparison_source_payload = runtime_comparison_source_unavailable(
                            comparison_source_query,
                            "runtime comparison source preview unavailable: "
                            "no comparison was indexed",
                        )
                    else:
                        try:
                            comparison_source_payload = runtime_comparison_source_preview(
                                service,
                                runtime,
                                source_root=self.server.app.source_root,
                                compare_run=compare_run,
                                query=comparison_source_query,
                            )
                        except RuntimeSourceUnavailable as exc:
                            comparison_source_payload = runtime_comparison_source_unavailable(
                                comparison_source_query, str(exc)
                            )
                    self._json(HTTPStatus.OK, comparison_source_payload.model_dump(mode="json"))
                    return
                runtime_value: RuntimeOverview | RuntimePanel | RuntimeComparison
                if parsed.path == "/api/runtime":
                    runtime_value = (
                        runtime.overview(
                            service.bootstrap().reports[0].report_sha256 if service else None
                        )
                        if runtime
                        else RuntimeOverview(
                            available=False,
                            message="Supply --profile-report PROFILE or select a saved profile "
                            "in Saved runs for runtime evidence.",
                        )
                    )
                elif runtime is None:
                    raise ValueError("no saved runtime profile supplied")
                elif parsed.path == "/api/runtime/comparison":
                    runtime_value = runtime.comparison_view()
                elif parsed.path == "/api/runtime/query":
                    runtime_value = runtime.panel(_runtime_query_request(params))
                else:
                    raise ValueError("unknown runtime view")
                self._json(HTTPStatus.OK, runtime_value.model_dump(mode="json"))
                return
            assert service is not None
            if parsed.path in {
                "/api/dependencies",
                "/api/dependencies/graph",
                "/api/dependencies/download",
            }:
                dependency_request = _dependency_request(params)
                if parsed.path == "/api/dependencies":
                    # Validate optional graph bounds here too, so an Apply
                    # request never accepts controls that its later graph or
                    # download would reject.
                    _dependency_graph_request(dependency_request, params)
                    dependency_result = service.dependencies(dependency_request)
                    self._json(
                        HTTPStatus.OK,
                        dependency_result.model_dump(mode="json"),
                        _dependency_headers(dependency_result),
                    )
                    return
                dependency_graph_request = _dependency_graph_request(dependency_request, params)
                graph = service.dependency_graph(dependency_graph_request)
                if parsed.path == "/api/dependencies/graph":
                    self._json(
                        HTTPStatus.OK,
                        graph.model_dump(mode="json"),
                        _dependency_graph_headers(graph, dependency_graph_request),
                    )
                    return
                output_format = ProjectionFormat(_single(params, "format", "json"))
                if output_format not in {
                    ProjectionFormat.JSON,
                    ProjectionFormat.JSON_COMPACT,
                    ProjectionFormat.MARKDOWN,
                    ProjectionFormat.DOT,
                    ProjectionFormat.MERMAID,
                    ProjectionFormat.SVG,
                    ProjectionFormat.PNG,
                    ProjectionFormat.HTML,
                }:
                    raise ValueError("unsupported dependency graph format")
                try:
                    payload = (
                        render_projection_binary(graph, output_format)
                        if output_format in {ProjectionFormat.SVG, ProjectionFormat.PNG}
                        else render_projection(graph, output_format).encode("utf-8")
                    )
                except OSError:
                    self._error(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "renderer-unavailable",
                        "Graphviz rendering is unavailable or failed",
                    )
                    return
                self._send_headers(HTTPStatus.OK, "application/octet-stream", len(payload))
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="dependencies.{output_format.value}"',
                )
                for key, value in _dependency_graph_headers(
                    graph, dependency_graph_request
                ).items():
                    self.send_header(key, value)
                self._emit_response_body(payload)
                return
            if parsed.path == "/api/code-browser/runtime-source":
                from code_analysis.code_browse_runtime import CodeRuntimeSourceRequest

                if set(params) - {
                    "run",
                    "compare_run",
                    "profile_run",
                    "compare_profile_run",
                    "report_sha256",
                    "revision",
                    "profile_report_sha256",
                    "profile_identity_sha256",
                    "frame_id",
                }:
                    raise ValueError("unsupported code-browser runtime source parameters")
                source_request = CodeRuntimeSourceRequest(
                    report_sha256=_single(params, "report_sha256", ""),
                    profile_report_sha256=_single(params, "profile_report_sha256", ""),
                    profile_identity_sha256=_single(params, "profile_identity_sha256", ""),
                    revision=_single_int(params, "revision", 0),
                    frame_id=_single(params, "frame_id", ""),
                )
                runtime = _selected_runtime(self.server.app, params)
                runtime_source = service.code_runtime_source(
                    source_request,
                    runtime.profile if runtime is not None else None,
                )
                self._json(
                    HTTPStatus.OK,
                    runtime_source.model_dump(mode="json"),
                    {
                        "X-Analysis-Completeness": runtime_source.completeness.value,
                    },
                )
                return
            if parsed.path == "/api/code-browser/graph":
                from code_analysis.code_browse_graph import CodeGraphLayer, CodeGraphRequest

                if set(params) - {
                    "run",
                    "compare_run",
                    "report_sha256",
                    "revision",
                    "layer",
                    "path",
                    "identity",
                    "depth",
                    "max_nodes",
                    "max_edges",
                    "profile_run",
                    "compare_profile_run",
                    "profile_report_sha256",
                    "profile_identity_sha256",
                }:
                    raise ValueError("unsupported code-browser graph parameters")
                layer = CodeGraphLayer(_single(params, "layer", "containment"))
                runtime = None
                if layer is CodeGraphLayer.RUNTIME_CALLS:
                    runtime = _selected_runtime(self.server.app, params)
                elif set(params) & {
                    "profile_run",
                    "compare_profile_run",
                    "profile_report_sha256",
                    "profile_identity_sha256",
                }:
                    raise ValueError("profile selection requires the runtime-call layer")
                code_graph = service.code_graph(
                    CodeGraphRequest(
                        report_sha256=_single(params, "report_sha256", ""),
                        revision=_single_int(params, "revision", 0),
                        layer=layer,
                        path=_single(params, "path", ""),
                        identity=_single(params, "identity", ""),
                        depth=_single_int(params, "depth", 1),
                        max_nodes=_single_int(params, "max_nodes", 100),
                        max_edges=_single_int(params, "max_edges", 250),
                        profile_report_sha256=(
                            _single(params, "profile_report_sha256", "")
                            if "profile_report_sha256" in params
                            else None
                        ),
                        profile_identity_sha256=(
                            _single(params, "profile_identity_sha256", "")
                            if "profile_identity_sha256" in params
                            else None
                        ),
                    ),
                    runtime.profile if runtime is not None else None,
                )
                self._json(
                    HTTPStatus.OK,
                    code_graph.model_dump(mode="json"),
                    {
                        "X-Analysis-Completeness": code_graph.completeness.value,
                        "X-Projection-Truncated": str(
                            bool(code_graph.graph and code_graph.graph.truncated)
                        ).lower(),
                    },
                )
                return
            if parsed.path.startswith("/api/project/"):
                assert service is not None
                self._project_get(parsed.path, params, service)
                return
            if parsed.path in {
                "/api/code-browser",
                "/api/code-browser/source",
                "/api/code-browser/document",
            }:
                from code_analysis.code_browse import CodeBrowsePage, CodeBrowseRequest
                from code_analysis.ui_source import (
                    SourceBrowseDocument,
                    SourceBrowsePreview,
                    preview_source,
                    read_source_document,
                )

                allowed_parameters = {
                    "run",
                    "compare_run",
                    "report_sha256",
                    "revision",
                    "path",
                    "identity",
                }
                if parsed.path != "/api/code-browser/document":
                    allowed_parameters.update({"search", "offset", "limit", "line"})
                if set(params) - allowed_parameters:
                    raise ValueError("unsupported code-browser parameters")
                browse_request = CodeBrowseRequest(
                    report_sha256=_single(params, "report_sha256", ""),
                    revision=_single_int(params, "revision", 0),
                    path=_single(params, "path", ""),
                    identity=_single(params, "identity", ""),
                    search=_single(params, "search", ""),
                    offset=_single_int(params, "offset", 0),
                    limit=_single_int(params, "limit", 50),
                    line=_single_int(params, "line", 1) if "line" in params else None,
                )
                browse_page = service.code_browse(browse_request)
                browse_payload: SourceBrowseDocument | SourceBrowsePreview | CodeBrowsePage
                if parsed.path == "/api/code-browser/document":
                    if not browse_page.logical_path:
                        raise ValueError("full source requires a selected saved file")
                    source_file = next(
                        (
                            item
                            for item in service.primary.source_files
                            if item.logical_path == browse_page.logical_path
                        ),
                        None,
                    )
                    if source_file is None:
                        raise ValueError("full source path is absent from the selected report")
                    browse_payload = SourceBrowseDocument(
                        report_sha256=browse_page.report_sha256,
                        revision=browse_page.revision,
                        document=read_source_document(source_file, self.server.app.source_root),
                    )
                elif parsed.path == "/api/code-browser/source":
                    if not browse_page.logical_path:
                        raise ValueError("source browsing requires a selected saved file")
                    start = browse_request.line or 1
                    preview = preview_source(
                        service.primary,
                        self.server.app.source_root,
                        browse_page.logical_path,
                        start=start,
                        end=start + 200,
                    )
                    browse_payload = SourceBrowsePreview(
                        report_sha256=browse_page.report_sha256,
                        revision=browse_page.revision,
                        preview=preview,
                    )
                else:
                    browse_payload = browse_page
                self._json(
                    HTTPStatus.OK,
                    browse_payload.model_dump(mode="json"),
                    {"X-Analysis-Completeness": browse_page.completeness.value},
                )
                return
            if parsed.path == "/api/navigation":
                from code_analysis.ui_navigation import navigation_page

                navigation_result = navigation_page(
                    service.primary, _single(params, "identity", ""), offset
                )
                self._json(HTTPStatus.OK, navigation_result.model_dump(mode="json"))
                return
            if parsed.path == "/api/comparison-download":
                if set(params) - {
                    "run",
                    "compare_run",
                    "baseline_sha256",
                    "candidate_sha256",
                    "offset",
                    "limit",
                    "change",
                    "kind",
                    "q",
                    "format",
                }:
                    raise ValueError("unsupported comparison export selection")
                comparison_selection = ComparisonPageRequest(
                    baseline_sha256=_single(params, "baseline_sha256", ""),
                    candidate_sha256=_single(params, "candidate_sha256", ""),
                    offset=offset,
                    limit=limit,
                    filters=comparison_filters_from_params(params),
                )
                output_format = ProjectionFormat(_single(params, "format", "json-compact"))
                media = {
                    ProjectionFormat.JSON_COMPACT: "application/json",
                    ProjectionFormat.TSV: "text/tab-separated-values",
                    ProjectionFormat.MARKDOWN: "text/markdown",
                }
                if output_format not in media:
                    raise ValueError("comparison export supports JSON, TSV or Markdown")
                comparison_projection = comparison_page_projection(
                    service.comparison_records(
                        offset=offset, limit=limit, filters=comparison_selection.filters
                    ),
                    comparison_selection,
                    service.bootstrap().reports,
                )
                payload = render_projection(comparison_projection, output_format).encode("utf-8")
                if len(payload) > 16 * 1024**2:
                    raise ValueError("comparison export exceeds the 16-MiB bound")
                extension = {
                    ProjectionFormat.JSON_COMPACT: "json",
                    ProjectionFormat.TSV: "tsv",
                    ProjectionFormat.MARKDOWN: "md",
                }[output_format]
                self._send_headers(HTTPStatus.OK, media[output_format], len(payload))
                self.send_header(
                    "Content-Disposition", f'attachment; filename="comparison.{extension}"'
                )
                self.send_header(
                    "X-Code-Analysis-Completeness", comparison_projection.completeness.value
                )
                self.send_header(
                    "X-Code-Analysis-Truncated", str(comparison_projection.truncated).lower()
                )
                self.send_header("X-Code-Analysis-Projection", comparison_projection.projection_id)
                self._emit_response_body(payload)
                return
            if parsed.path in {"/api/comparison-detail", "/api/comparison-source"}:
                from code_analysis.static_comparison_source import (
                    StaticComparisonSelection,
                    StaticComparisonSourceQuery,
                    preview_static_comparison_source,
                    validate_static_comparison_selection,
                )

                is_source_request = parsed.path == "/api/comparison-source"
                allowed = {"run", "compare_run", "position", "baseline_sha256", "candidate_sha256"}
                if is_source_request:
                    allowed.update({"side", "option_id"})
                if set(params) - allowed:
                    raise ValueError("unsupported comparison source selection")
                static_selection = StaticComparisonSelection(
                    position=_single_int(params, "position", -1),
                    baseline_sha256=_single(params, "baseline_sha256", ""),
                    candidate_sha256=_single(params, "candidate_sha256", ""),
                )
                static_binding = service.static_comparison_source_detail(static_selection.position)
                validate_static_comparison_selection(static_binding, static_selection)
                if is_source_request:
                    static_source_query = StaticComparisonSourceQuery(
                        **static_selection.model_dump(),
                        side=TypeAdapter[Literal["baseline", "candidate"]](
                            Literal["baseline", "candidate"]
                        ).validate_python(_single(params, "side", "")),
                        option_id=_single(params, "option_id", ""),
                    )
                    preview = preview_static_comparison_source(
                        static_binding, static_source_query, self.server.app.source_root
                    )
                    self._json(HTTPStatus.OK, preview.model_dump(mode="json"))
                else:
                    self._json(HTTPStatus.OK, static_binding.model_dump(mode="json"))
                return
            if parsed.path in {"/api/test-detail", "/api/test-source"}:
                from code_analysis.ui_source import SourcePreviewIdentity, preview_source_record

                allowed = {"run", "compare_run", "report_sha256", "identity"}
                if parsed.path == "/api/test-source":
                    allowed.add("choice_id")
                if set(params) - allowed:
                    raise ValueError("unsupported test investigation selection")
                if (
                    _single(params, "report_sha256", "")
                    != service.bootstrap().reports[0].report_sha256
                ):
                    raise ValueError("test investigation report identity mismatch")
                detail = service.test_detail(_single(params, "identity", ""))
                if parsed.path == "/api/test-detail":
                    self._json(HTTPStatus.OK, detail.model_dump(mode="json"))
                else:
                    choice = next(
                        (
                            item
                            for item in detail.sources
                            if item.choice_id == _single(params, "choice_id", "")
                        ),
                        None,
                    )
                    if choice is None:
                        raise ValueError("source choice is absent from the selected test record")
                    preview = preview_source_record(
                        SourcePreviewIdentity(
                            logical_path=choice.path, source_sha256=choice.source_sha256
                        ),
                        self.server.app.source_root,
                        start=choice.start,
                        end=min(choice.end, choice.start + 39),
                    )
                    self._json(HTTPStatus.OK, preview.model_dump(mode="json"))
                return
            if parsed.path == "/api/source":
                from code_analysis.ui_source import preview_source

                preview = preview_source(
                    service.primary,
                    self.server.app.source_root,
                    _single(params, "path", ""),
                    start=_single_int(params, "start", 1),
                    end=_single_int(params, "end", 40),
                )
                self._json(HTTPStatus.OK, preview.model_dump(mode="json"))
                return
            if parsed.path == "/api/projections":
                self._json(HTTPStatus.OK, UIProjectionCatalog().model_dump(mode="json"))
                return
            if parsed.path == "/api/candidate-ledger":
                if set(params) - {"run", "compare_run"}:
                    raise ValueError("ledger selection uses only the selected owned workspace")
                workspace = self.server.app.runs.workspace if self.server.app.runs else None
                self._json(
                    HTTPStatus.OK,
                    decision_ledger_status(service.primary, workspace).model_dump(mode="json"),
                )
                return
            if parsed.path == "/api/projection-node":
                node_selection = graph_node_selection(
                    service.primary,
                    _projection_request(params),
                    _single(params, "projection_id", ""),
                    _single(params, "node", ""),
                )
                self._json(HTTPStatus.OK, node_selection.model_dump(mode="json"))
                return
            if parsed.path in {"/api/projection", "/api/download"}:
                projection_request = _projection_request(params)
                decisions = None
                if projection_request.decision_ledger_sha256 is not None:
                    if self.server.app.runs is None:
                        raise ValueError("decision selection requires an owned workspace")
                    decisions = read_decision_snapshot(
                        service.primary, self.server.app.runs.workspace
                    )
                projection = project_report(service.primary, projection_request, decisions)
                if parsed.path == "/api/projection":
                    self._json(HTTPStatus.OK, projection.model_dump(mode="json"))
                    return
                output_format = ProjectionFormat(_single(params, "format", "json"))
                if output_format in {ProjectionFormat.SVG, ProjectionFormat.PNG}:
                    try:
                        payload = render_projection_binary(projection, output_format)
                    except OSError:
                        self._error(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            "renderer-unavailable",
                            "Graphviz rendering is unavailable or failed",
                        )
                        return
                else:
                    payload = render_projection(projection, output_format).encode("utf-8")
                self._send_headers(HTTPStatus.OK, "application/octet-stream", len(payload))
                filename = f"{_single(params, 'name', '')}.{output_format.value}"
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("X-Analysis-Completeness", projection.completeness.value)
                self.send_header("X-Projection-Truncated", str(projection.truncated).lower())
                self._emit_response_body(payload)
                return
            routes: dict[str, Callable[[], StrictModel]] = {
                "/api/bootstrap": lambda: service.bootstrap(),
                "/api/modules": lambda: service.modules(
                    offset=offset, limit=limit, query=query, facets=facets
                ),
                "/api/state-effects": lambda: service.state_effects(
                    offset=offset, limit=limit, query=query, facets=facets
                ),
                "/api/contracts": lambda: service.contracts(
                    offset=offset, limit=limit, query=query, facets=facets
                ),
                "/api/dictionary-shapes": lambda: service.dictionary_shapes(
                    offset=offset, limit=limit, query=query, facets=facets
                ),
                "/api/architecture": lambda: service.architecture(
                    max_nodes=_single_int(params, "max_nodes", 60),
                    max_edges=_single_int(params, "max_edges", 120),
                ),
                "/api/findings": lambda: service.findings(
                    facets=facets,
                    offset=offset,
                    limit=limit,
                    query=query,
                    severity=_single(params, "severity", ""),
                    category=_single(params, "category", ""),
                    confidence=_single(params, "confidence", ""),
                ),
                "/api/candidates": lambda: service.candidates(
                    offset=offset, limit=limit, query=query, facets=facets
                ),
                "/api/clones": lambda: service.clones(
                    offset=offset, limit=limit, query=query, facets=facets
                ),
                "/api/test-impact": lambda: service.test_impact(
                    offset=offset, limit=limit, query=query, facets=facets
                ),
                "/api/providers": lambda: service.providers(offset=offset, limit=limit),
                "/api/diagnostics": lambda: service.diagnostics(
                    offset=offset, limit=limit, query=query
                ),
                "/api/evidence": lambda: service.evidence(
                    kind=_single(params, "kind", ""),
                    identity=_single(params, "identity", ""),
                    limit=limit,
                    after=_single(params, "after", "") or None,
                ),
                "/api/candidate": lambda: service.candidate_packet(
                    _single(params, "identity", ""),
                    limit=_single_int(params, "limit", 25),
                    offset=offset,
                ),
                "/api/comparison": lambda: service.comparison_records(
                    offset=offset, limit=limit, filters=comparison_filters_from_params(params)
                ),
            }
            builder = routes.get(parsed.path)
            if builder is None:
                self._error(HTTPStatus.NOT_FOUND, "not-found", "route not found")
                return
            self._json(HTTPStatus.OK, builder().model_dump(mode="json"))
        except (ValueError, ValidationError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid-request", str(exc))
        except OSError:
            self._error(
                HTTPStatus.BAD_REQUEST,
                "invalid-artifact",
                "saved generation is unavailable or failed integrity validation",
            )

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        """Authenticated collection operations; only confirmed launches execute commands."""
        if not self._valid_host():
            self._error(HTTPStatus.BAD_REQUEST, "invalid-host", "loopback Host required")
            return
        if not secrets.compare_digest(
            self.headers.get("X-Code-Analysis-Token", ""), self.server.app.token
        ):
            self._error(HTTPStatus.FORBIDDEN, "invalid-token", "session token required")
            return
        expected_origin = f"http://{self.headers.get('Host', '')}"
        if self.headers.get("Origin") != expected_origin or self.headers.get(
            "Sec-Fetch-Site", "same-origin"
        ) not in {"same-origin", "none"}:
            self._error(HTTPStatus.FORBIDDEN, "invalid-origin", "same-origin request required")
            return
        if self.path not in {
            "/api/session/stop",
            "/api/project/source",
            "/api/collection/preview",
            "/api/collection/launch",
            "/api/collection/connect",
            "/api/collection/cancel",
            "/api/collection/open",
            "/api/collection/logs",
        }:
            self._error(HTTPStatus.NOT_FOUND, "unknown-route", "no such collection operation")
            return
        planner = self.server.app.collection
        if planner is None and self.path != "/api/session/stop":
            self._error(
                HTTPStatus.FORBIDDEN, "collection-disabled", "select --collection-root at startup"
            )
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding") is not None:
                raise ValueError("a single bounded Content-Length is required")
            length = int(lengths[0])
            if not 1 <= length <= 65_536:
                raise ValueError("preview body must be between 1 and 65536 bytes")
            if (
                self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise ValueError("preview requires application/json")
            self.connection.settimeout(5)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("incomplete preview body")
            if self.path == "/api/session/stop":
                status = self.server.prepare_stop(UIShutdownRequest.model_validate_json(body))
                try:
                    self._json(HTTPStatus.OK, status.model_dump(mode="json"))
                finally:
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif self.path == "/api/project/source":
                self._connect_source(body)
            elif self.path == "/api/collection/preview":
                request = CollectionRequest.model_validate_json(body)
                assert planner is not None
                self._json(HTTPStatus.OK, planner.preview(request).model_dump(mode="json"))
            else:
                self._collection_action(body)
        except (ValueError, OSError) as exc:
            self.close_connection = True
            message = "Collection options or confined input paths are invalid."
            if isinstance(exc, ValidationError):
                message = "; ".join(
                    str(error["msg"])[:200]
                    for error in exc.errors(include_input=False, include_context=False)[:3]
                )
            elif isinstance(exc, ValueError):
                message = str(exc)
            elif isinstance(exc, OSError):
                message = exc.strerror or message
            code = (exc.errno or errno.EIO) if isinstance(exc, OSError) else errno.EINVAL
            error_status = (
                HTTPStatus.FORBIDDEN
                if code == errno.EPERM
                else HTTPStatus.CONFLICT if code == errno.EBUSY else HTTPStatus.BAD_REQUEST
            )
            self._json(
                error_status,
                {"error": {"code": "invalid-collection", "message": message}, "status": code},
            )

    def _connect_source(self, body: bytes) -> None:
        from code_analysis.project_browsing import (
            SourceConnectionRequest,
            source_connection,
            validate_source_connection,
        )

        request = SourceConnectionRequest.model_validate_json(body)
        with self.server.lifecycle_lock:
            app = self.server.app
            service = app.service
            if app.runs is not None:
                service = app.runs.select(request.run, request.compare_run)
            elif request.run != "current" or request.compare_run != "none":
                raise ValueError("run selection requires an owned workspace")
            if service is None or app.collection is None:
                raise OSError(
                    errno.EPERM, "a saved report and configured collection root are required"
                )
            root = validate_source_connection(service.primary, app.collection, request)
            result = source_connection(service.primary, root, app.collection)
            self.server.app = replace(
                app, source_root=root, project_documents=ProjectDocumentAccess()
            )
        self._json(HTTPStatus.OK, result.model_dump(mode="json"))

    def _project_get(
        self, route: str, params: dict[str, list[str]], service: ReportViewService
    ) -> None:
        from code_analysis.project_browsing import (
            document_catalog,
            read_project_document,
            report_identity,
            source_connection,
        )

        allowed = {"run", "compare_run", "report_sha256"}
        if route == "/api/project/documents":
            allowed.add("path")
        elif route == "/api/project/document":
            allowed.update({"path", "root_identity", "sha256"})
        elif route != "/api/project/source":
            raise ValueError("unknown project browsing operation")
        if set(params) - allowed:
            raise ValueError("unsupported project browsing selectors")
        if _single(params, "report_sha256", "") != report_identity(service.primary):
            raise ValueError("project browsing belongs to another saved report")
        result: StrictModel
        with self.server.lifecycle_lock:
            app = self.server.app
            if route == "/api/project/source":
                result = source_connection(service.primary, app.source_root, app.collection)
            elif route == "/api/project/documents":
                catalog = document_catalog(
                    service.primary, app.source_root, _single(params, "path", "")
                )
                app.project_documents.allow(
                    catalog.report_sha256, catalog.root_identity, catalog.records
                )
                result = catalog
            else:
                app.project_documents.require(
                    report_identity(service.primary),
                    _single(params, "root_identity", ""),
                    _single(params, "path", ""),
                    _single(params, "sha256", ""),
                )
                document = read_project_document(
                    service.primary,
                    app.source_root,
                    _single(params, "root_identity", ""),
                    _single(params, "path", ""),
                    _single(params, "sha256", ""),
                )
                app.project_documents.allow(
                    document.report_sha256, document.root_identity, document.links
                )
                result = document
        self._json(HTTPStatus.OK, result.model_dump(mode="json"))

    def _collection_action(self, body: bytes) -> None:
        session = self.server.app.collection_session
        if session is None:
            raise OSError(errno.EPERM, "enable --allow-collection at startup")
        if self.path.endswith("/launch"):
            with self.server.lifecycle_lock:
                if self.server.stopping:
                    raise OSError(errno.EBUSY, "UI server is stopping; no new jobs may start")
                job = session.launch(CollectionLaunch.model_validate_json(body))
            self._json(HTTPStatus.ACCEPTED, job.model_dump(mode="json"))
        elif self.path.endswith("/connect"):
            catalog = session.connect(CollectionWorkspace.model_validate_json(body))
            if self.server.app.runs is None:
                self.server.app = replace(
                    self.server.app,
                    runs=RunRepository(session.selected().workspace.root, self.server.app.service),
                )
            self._json(HTTPStatus.OK, catalog.model_dump(mode="json"))
        elif self.path.endswith("/cancel"):
            selection = CollectionJobSelection.model_validate_json(body)
            job = session.selected().cancel(selection.job_id)
            self._json(HTTPStatus.OK, job.model_dump(mode="json"))
        elif self.path.endswith("/logs"):
            selection = CollectionJobSelection.model_validate_json(body)
            self._json(
                HTTPStatus.OK, session.selected().log(selection.job_id).model_dump(mode="json")
            )
        else:
            selection = CollectionJobSelection.model_validate_json(body)
            jobs = session.selected()
            job = jobs.get(selection.job_id)
            if job.binding is None:
                raise OSError(errno.ENODATA, "job has no verified generation to open")
            from code_analysis.collection_views import collection_views

            views = collection_views(session, selection.job_id)
            service, runtime = views.service, views.runtime
            self.server.app = replace(
                self.server.app,
                service=service,
                runtime=runtime,
                runs=RunRepository(jobs.workspace.root, service),
            )
            self._json(HTTPStatus.OK, {"opened": job.job_id, "kind": job.binding.kind.value})

    def _valid_host(self) -> bool:
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]").lower()
        return host in {"127.0.0.1", "localhost", "::1"}

    def _asset(self, path: str) -> None:
        name, content_type = _ASSETS[path]
        payload = (_STATIC_ROOT / name).read_bytes()
        self._send_headers(HTTPStatus.OK, content_type, len(payload))
        self._emit_response_body(payload)

    def _help_asset(self, path: str) -> None:
        # Public, packaged documentation only. Never map a URL to the filesystem.
        allowed = {
            "/help/index.json": "index.json",
            "/help/styles.css": "styles.css",
            "/help/getting-started.html": "getting-started.html",
            "/help/user-guide.html": "user-guide.html",
            "/help/cli-reference.html": "cli-reference.html",
            "/help/examples.html": "examples.html",
            "/help/reports.html": "reports.html",
        }
        filename = allowed.get(path)
        if filename is None:
            self._error(HTTPStatus.NOT_FOUND, "not-found", "help resource not found")
            return
        from code_analysis.user_help_store import HelpUnavailableError, read_help_asset

        try:
            asset = read_help_asset(filename)
        except HelpUnavailableError:
            self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "help-unavailable",
                "packaged help is unavailable; analytical navigation remains available",
            )
            return
        self._send_headers(HTTPStatus.OK, asset.content_type, len(asset.payload))
        self._emit_response_body(asset.payload)

    def _emit_response_body(self, body: bytes) -> None:
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _json(
        self, status: HTTPStatus, payload: object, headers: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self._send_headers(status, "application/json; charset=utf-8", len(body))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self._emit_response_body(body)

    def _error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    def _send_headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        policy = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")


def create_server(
    service: ReportViewService | None,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    workspace: Path | None = None,
    source_root: Path | None = None,
    profile_report: Path | None = None,
    compare_profile: Path | None = None,
    collection_root: Path | None = None,
    allow_collection: bool = False,
    collection_pythons: tuple[Path, ...] = (),
    runtime_service: RuntimeViewService | None = None,
) -> ReportHTTPServer:
    """``runtime_service`` is an in-process handoff snapshot, not a CLI option."""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("prototype Web UI is loopback-only")
    if allow_collection and collection_root is None:
        raise ValueError("--allow-collection requires --collection-root")
    if collection_pythons and collection_root is None:
        raise ValueError("--collection-python requires --collection-root")
    if not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    if runtime_service is not None and (
        service is not None
        or profile_report
        or compare_profile
        or workspace
        or source_root
        or collection_root
    ):
        raise ValueError(
            "a preloaded runtime handoff excludes saved-report, profile and collection options"
        )
    if (
        service is None
        and runtime_service is None
        and (
            collection_root is None or workspace or profile_report or source_root or compare_profile
        )
    ):
        raise ValueError(
            "collection-only startup requires --collection-root and no saved-report options"
        )
    runs = RunRepository(workspace, service) if workspace and service is not None else None
    if compare_profile is not None and profile_report is None:
        raise ValueError("--compare-profile requires --profile-report")
    runtime = (
        runtime_service
        if runtime_service is not None
        else (RuntimeViewService.load(profile_report, compare_profile) if profile_report else None)
    )
    if source_root is not None:
        source_root = source_root.resolve(strict=True)
        if not source_root.is_dir():
            raise ValueError("source root must be a directory")
    planner = (
        CollectionPlanner(collection_root, interpreters=collection_pythons)
        if collection_root
        else None
    )
    try:
        server = ReportHTTPServer((host, port), ReportRequestHandler)
    except BaseException:
        if planner is not None:
            planner.close()
        raise
    server.app = WebApplication(
        service=service,
        token=token or secrets.token_urlsafe(32),
        runs=runs,
        source_root=source_root,
        runtime=runtime,
        collection=planner,
        collection_session=CollectionSession(planner) if allow_collection and planner else None,
    )
    return server


def _collection_context(app: WebApplication, *, report_available: bool) -> CollectionContext:
    context = (
        app.collection.context(report_available=report_available)
        if app.collection
        else CollectionContext(
            enabled=False,
            root=None,
            host=platform.node(),
            interpreter=sys.executable,
            report_available=report_available,
        )
    )
    return context.model_copy(
        update={
            "execution_available": app.collection_session is not None,
            "runtime_available": app.runtime is not None,
        }
    )


def _selected_runtime(
    app: WebApplication, params: dict[str, list[str]]
) -> RuntimeViewService | None:
    """Resolve only explicit profile choices; timestamps never select evidence."""
    if not {"profile_run", "compare_profile_run"}.intersection(params):
        return app.runtime
    primary = _single(params, "profile_run", "none")
    comparison = _single(params, "compare_profile_run", "none")
    if primary == "current":
        if comparison != "none":
            raise ValueError("initial profiles cannot be paired with a saved run")
        return app.runtime
    if app.runs is None:
        raise ValueError("profile selection requires an owned workspace")
    return app.runs.select_profiles(primary, comparison)


def _single(params: dict[str, list[str]], name: str, default: str) -> str:
    values = params.get(name)
    if not values:
        return default
    if len(values) != 1:
        raise ValueError(f"{name} must be supplied once")
    return values[0]


def _single_int(params: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(_single(params, name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _projection_request(params: dict[str, list[str]]) -> UIProjectionRequest:
    allowed = (set(UIProjectionRequest.model_fields) - {"selected_ids", "languages"}) | {
        "select",
        "language",
        "format",
        "run",
        "compare_run",
        "projection_id",
        "node",
    }
    if unknown := set(params) - allowed:
        raise ValueError(f"unsupported projection parameters: {', '.join(sorted(unknown))}")
    depth = _single(params, "depth", "")
    return UIProjectionRequest(
        name=ProjectionName(_single(params, "name", "modules")),
        selected_ids=tuple(params.get("select", ())),
        languages=tuple(
            sorted(
                {SourceLanguage(value) for value in params.get("language", ()) if value},
                key=lambda item: item.value,
            )
        ),
        max_records=_single_int(params, "max_records", 100),
        max_nodes=_single_int(params, "max_nodes", 50),
        max_edges=_single_int(params, "max_edges", 100),
        depth=int(depth) if depth else None,
        collapse=ProjectionCollapse(_single(params, "collapse", "none")),
        module_prefixes=tuple(sorted(set(params.get("module_prefixes", ())))),
        finding_categories=tuple(
            sorted(
                {ProjectionFindingCategory(item) for item in params.get("finding_categories", ())},
                key=lambda item: item.value,
            )
        ),
        finding_severities=tuple(
            sorted(
                {ProjectionFindingSeverity(item) for item in params.get("finding_severities", ())},
                key=lambda item: item.value,
            )
        ),
        finding_rule_ids=tuple(sorted(set(params.get("finding_rule_ids", ())))),
        candidate_module_patterns=tuple(sorted(set(params.get("candidate_module_patterns", ())))),
        candidate_exclude_patterns=tuple(sorted(set(params.get("candidate_exclude_patterns", ())))),
        candidate_kinds=tuple(
            sorted(
                {RefactorCandidateKind(item) for item in params.get("candidate_kinds", ())},
                key=lambda item: item.value,
            )
        ),
        candidate_states=tuple(sorted(set(params.get("candidate_states", ())))),
        candidate_decisions=tuple(sorted(set(params.get("candidate_decisions", ())))),
        decision_ledger_sha256=_single(params, "decision_ledger_sha256", "") or None,
        decision_report_sha256=_single(params, "decision_report_sha256", "") or None,
        cross_module_only=_single_bool(params, "cross_module_only"),
        production_only=_single_bool(params, "production_only"),
        min_shared_dimensions=_single_int(params, "min_shared_dimensions", 0),
        prefer_existing_owner=_single_bool(params, "prefer_existing_owner"),
    )


def _single_bool(params: dict[str, list[str]], name: str) -> bool:
    value = _single(params, name, "false")
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def _dependency_request(params: dict[str, list[str]]) -> DependencyRequest:
    """Parse the dependency investigation once, rejecting unrelated query fields."""
    allowed = {
        "source",
        "target",
        "mode",
        "max_nodes",
        "max_edges",
        "graph_max_nodes",
        "graph_max_edges",
        "run",
        "compare_run",
        "format",
    }
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"unsupported dependency parameters: {', '.join(sorted(unknown))}")
    target = _single(params, "target", "")
    return DependencyRequest(
        source=_single(params, "source", ""),
        target=target or None,
        mode=TypeAdapter[Literal["path", "component"]](
            Literal["path", "component"]
        ).validate_python(_single(params, "mode", "path")),
        max_nodes=_single_int(params, "max_nodes", 500),
        max_edges=_single_int(params, "max_edges", 5000),
    )


_RUNTIME_TABLE_DOWNLOAD_PARAMS: Final = frozenset(
    {
        "section",
        "frame",
        "max_records",
        "format",
        "profile_run",
        "compare_profile_run",
        "run",
        "compare_run",
    }
)

_RUNTIME_COMPARISON_DOWNLOAD_PARAMS: Final = frozenset(
    {
        "format",
        "profile_run",
        "compare_profile_run",
        "run",
        "compare_run",
    }
)

_RUNTIME_TABLE_MEDIA: Final = {
    "json": "application/json; charset=utf-8",
    "tsv": "text/tab-separated-values; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
}


def _runtime_query_request(params: dict[str, list[str]]) -> RuntimeQueryRequest:
    from code_analysis.runtime_queries import RuntimeQueryRequest, RuntimeQuerySection

    try:
        return RuntimeQueryRequest(
            section=RuntimeQuerySection(_single(params, "section", "hotspots")),
            max_records=_single_int(params, "max_records", 100),
            frame_id=_single(params, "frame", "") or None,
        )
    except (ValidationError, ValueError) as exc:
        raise ValueError("invalid runtime query bounds") from exc


def _runtime_table_download_headers(projection: RuntimeQueryProjection) -> dict[str, str]:
    return {
        "X-Analysis-Completeness": projection.source_completeness.value,
        "X-Projection-Truncated": str(projection.truncated).lower(),
        "X-Runtime-Query-Omitted": str(projection.omitted_count),
    }


def _dependency_graph_request(
    investigation: DependencyRequest, params: dict[str, list[str]]
) -> DependencyGraphRequest:
    return DependencyGraphRequest(
        investigation=investigation,
        max_nodes=_single_int(params, "graph_max_nodes", 100),
        max_edges=_single_int(params, "graph_max_edges", 250),
    )


def _dependency_headers(result: DependencyResult) -> dict[str, str]:
    completeness = (
        Completeness.PARTIAL
        if result.truncated and result.source_completeness is not Completeness.FAILED
        else result.source_completeness
    )
    return {
        "X-Analysis-Completeness": completeness.value,
        "X-Source-Completeness": result.source_completeness.value,
        "X-Dependency-Truncated": str(result.truncated).lower(),
        "X-Dependency-Max-Nodes": str(result.request.max_nodes),
        "X-Dependency-Max-Edges": str(result.request.max_edges),
        "X-Dependency-Visited-Nodes": str(result.visited_nodes),
        "X-Dependency-Examined-Edges": str(result.examined_edges),
    }


def _dependency_graph_headers(
    graph: GraphProjection, request: DependencyGraphRequest
) -> dict[str, str]:
    return {
        "X-Analysis-Completeness": graph.completeness.value,
        "X-Projection-Truncated": str(graph.truncated).lower(),
        "X-Dependency-Graph-Max-Nodes": str(request.max_nodes),
        "X-Dependency-Graph-Max-Edges": str(request.max_edges),
    }
