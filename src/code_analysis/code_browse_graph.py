"""Saved-report focused relationship graphs for the code browser."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from code_analysis.code_browse import (
    CodeBrowseBindingError,
    CodeBrowseEntry,
    CodeBrowseReader,
    CodeBrowseRequest,
    _normalize_browse_path,
)
from code_analysis.graph_collapse import COLLAPSE_LIMITATION, collapse_graph
from code_analysis.graph_ownership import graph_ownership
from code_analysis.graph_selection import GraphEdgeIdentity, GraphNodeRank, retain_graph
from code_analysis.models import Completeness, StrictModel
from code_analysis.profile_models import ProfileReport
from code_analysis.projection_models import (
    EdgeDirection,
    GraphEdge,
    GraphNode,
    GraphProjection,
    Projection,
    ProjectionCollapse,
    ProjectionInput,
    ProjectionSelection,
)
from code_analysis.projections import ProjectionBuildError, ProjectionName, build_projection
from code_analysis.projections._shared import _edge_id, _report_input
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json

_CONTAINED_NODE_PREFIX = "code-browse-contained:"
_LAYER_TITLES = {
    "containment": "Code browser span containment",
    "imports": "Code browser import neighborhood",
    "static-calls": "Code browser recorded callee names",
    "static-call-sites": "Code browser static call-site containment",
    "data-flow": "Code browser dictionary flow",
}


class CodeGraphLayer(StrEnum):
    CONTAINMENT = "containment"
    IMPORTS = "imports"
    STATIC_CALLS = "static-calls"
    STATIC_CALL_SITES = "static-call-sites"
    DATA_FLOW = "data-flow"
    RUNTIME_CALLS = "runtime-calls"


class CodeGraphRequest(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    revision: StrictInt = Field(ge=0)
    layer: CodeGraphLayer
    path: StrictStr = ""
    identity: StrictStr = ""
    depth: StrictInt = Field(default=1, ge=0, le=5)
    max_nodes: StrictInt = Field(default=100, ge=1, le=100)
    max_edges: StrictInt = Field(default=250, ge=0, le=250)
    profile_report_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    profile_identity_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        return _normalize_browse_path(value)

    @model_validator(mode="after")
    def require_focus(self) -> Self:
        if not self.path and not self.identity:
            raise ValueError("focused code graph requires path or identity")
        pinned = self.profile_report_sha256 is not None and self.profile_identity_sha256 is not None
        if self.layer is CodeGraphLayer.RUNTIME_CALLS:
            if not pinned:
                raise ValueError("runtime code graph requires both selected profile identities")
        elif self.profile_report_sha256 is not None or self.profile_identity_sha256 is not None:
            raise ValueError("profile identities require the runtime-call layer")
        return self


class CodeGraphLink(StrictModel):
    """An exact saved entity target, never inferred from a graph label."""

    node_id: StrictStr = Field(min_length=1)
    identity: StrictStr = ""
    path: StrictStr = ""
    line: StrictInt | None = Field(default=None, ge=1)
    source_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        return _normalize_browse_path(value)

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        has_location = bool(self.path) or self.line is not None or self.source_sha256 is not None
        if has_location and not (self.path and self.line is not None and self.source_sha256):
            raise ValueError("code graph source location requires path, line and source hash")
        if not self.identity and not has_location:
            raise ValueError("code graph link requires an exact entity or source location")
        return self


class CodeGraphResult(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    revision: StrictInt = Field(ge=0)
    layer: CodeGraphLayer
    state: Literal["available", "unavailable"]
    completeness: Completeness
    message: StrictStr = Field(min_length=1, max_length=512)
    graph: GraphProjection | None = None
    links: tuple[CodeGraphLink, ...] = Field(default=(), max_length=100)
    profile_report_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    profile_identity_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.layer is CodeGraphLayer.RUNTIME_CALLS:
            if self.profile_report_sha256 is None or self.profile_identity_sha256 is None:
                raise ValueError("runtime code graph result requires selected profile identities")
        elif self.profile_report_sha256 is not None or self.profile_identity_sha256 is not None:
            raise ValueError("static code graph result cannot claim a profile binding")
        if list(self.limitations) != sorted(self.limitations) or len(self.limitations) != len(
            set(self.limitations)
        ):
            raise ValueError("code graph limitations must be unique and sorted")
        if self.state == "available":
            if self.graph is None:
                raise ValueError("available code graph requires a graph projection")
            if not self.graph.nodes:
                raise ValueError("available code graph cannot be empty")
            if len(self.graph.inputs) != 1 or self.graph.inputs[0].report_sha256 != (
                self.profile_report_sha256
                if self.layer is CodeGraphLayer.RUNTIME_CALLS
                else self.report_sha256
            ):
                raise ValueError("code graph source identity disagrees with its result")
            if self.completeness is not self.graph.completeness:
                raise ValueError("code graph completeness disagrees with its result")
            if self.layer is CodeGraphLayer.RUNTIME_CALLS and (
                self.graph.inputs[0].report_schema_name != "code-analysis-profile-report"
                or self.graph.inputs[0].run_identity_sha256 != self.profile_identity_sha256
            ):
                raise ValueError("runtime code graph profile provenance disagrees with its result")
            if len(self.graph.nodes) > 100 or len(self.graph.edges) > 250:
                raise ValueError("code graph exceeds browser bounds")
            link_ids = [item.node_id for item in self.links]
            if link_ids != sorted(set(link_ids)):
                raise ValueError("code graph links must be unique and sorted")
            if set(link_ids) - {item.node_id for item in self.graph.nodes}:
                raise ValueError("code graph link references an absent node")
        else:
            if self.graph is not None or self.links:
                raise ValueError(
                    "unavailable code graph cannot include a graph projection or links"
                )
            if self.completeness is Completeness.COMPLETE:
                raise ValueError("unavailable code graph cannot be complete")
        return self


def build_code_graph(
    report: AnalysisReport,
    reader: CodeBrowseReader,
    request: CodeGraphRequest,
    profile: ProfileReport | None = None,
) -> CodeGraphResult:
    digest = hashlib.sha256(canonical_json(report).encode()).hexdigest()
    if digest != request.report_sha256:
        raise CodeBrowseBindingError("code graph request is bound to another report")
    if digest != reader.report_sha256:
        raise CodeBrowseBindingError("code graph reader is bound to another report")

    browse = reader.read(
        CodeBrowseRequest(
            report_sha256=request.report_sha256,
            revision=request.revision,
            path=request.path,
            identity=request.identity,
            limit=100,
        )
    )
    if request.layer is CodeGraphLayer.RUNTIME_CALLS:
        from code_analysis.code_browse_runtime import build_code_runtime_graph

        return build_code_runtime_graph(report, reader, request, profile)
    limitations = list(browse.limitations)
    if request.identity and not request.path and browse.logical_path is None:
        if browse.selection_location_count > 1:
            limitations.append(
                "Choose a recorded source location; this identity spans multiple files."
            )
        return _unavailable(
            request,
            reader,
            "Focused relationship graph requires a single recorded source location.",
            limitations,
        )

    resolved_path = request.path or browse.logical_path
    canonical_identity = ""
    if request.identity:
        canonical_identity = reader.aliases.get(request.identity, request.identity)

    selection = ProjectionSelection(
        max_records=request.max_nodes + request.max_edges,
        max_nodes=request.max_nodes,
        max_edges=request.max_edges,
    )
    graph: Projection
    try:
        if request.layer is CodeGraphLayer.CONTAINMENT:
            if resolved_path is None:
                return _unavailable(
                    request,
                    reader,
                    "Containment graph requires a resolved source path.",
                    limitations,
                )
            graph = _build_containment_graph(
                report,
                reader,
                request,
                resolved_path,
                canonical_identity,
                selection,
                limitations,
            )
        elif request.layer is CodeGraphLayer.IMPORTS:
            module_name = _resolve_module_name(
                report, reader, resolved_path, canonical_identity, browse.selected
            )
            if module_name is None:
                return _unavailable(
                    request,
                    reader,
                    "Import neighborhood requires a recorded module for the selection.",
                    limitations,
                )
            graph = build_projection(
                report,
                ProjectionName.DEPENDENCY_NEIGHBORHOOD,
                selected_ids=(module_name,),
                depth=request.depth,
                max_records=selection.max_records,
                max_nodes=selection.max_nodes,
                max_edges=selection.max_edges,
            )
            assert isinstance(graph, GraphProjection)
            graph = _rebind_projection(graph, report, request, selection)
        elif request.layer is CodeGraphLayer.STATIC_CALLS:
            subject_ids = _lexical_subject_ids(report, reader, resolved_path, canonical_identity)
            if not subject_ids:
                return _unavailable(
                    request,
                    reader,
                    "Recorded callee graph requires saved function profiles for the selection.",
                    limitations,
                )
            graph = build_projection(
                report,
                ProjectionName.LEXICAL_CALL_GRAPH,
                selected_ids=subject_ids,
                max_records=selection.max_records,
                max_nodes=selection.max_nodes,
                max_edges=selection.max_edges,
            )
            assert isinstance(graph, GraphProjection)
            graph = _rebind_projection(graph, report, request, selection)
        elif request.layer is CodeGraphLayer.STATIC_CALL_SITES:
            site_ids = _static_call_site_selection(
                report, reader, resolved_path, canonical_identity, browse.selected
            )
            if site_ids is None:
                return _unavailable(
                    request,
                    reader,
                    "Static call-site graph requires a source file or call occurrence selection.",
                    limitations,
                )
            graph = build_projection(
                report,
                ProjectionName.STATIC_CALL_GRAPH,
                selected_ids=site_ids,
                max_records=selection.max_records,
                max_nodes=selection.max_nodes,
                max_edges=selection.max_edges,
            )
            assert isinstance(graph, GraphProjection)
            graph = _rebind_projection(graph, report, request, selection)
        else:
            flow_ids = _dictionary_flow_seed_ids(
                report, reader, resolved_path, canonical_identity, browse.selected
            )
            if not flow_ids:
                return _unavailable(
                    request,
                    reader,
                    "Dictionary flow graph requires saved flow evidence for the selection.",
                    limitations,
                )
            graph = build_projection(
                report,
                ProjectionName.DICTIONARY_FLOW,
                selected_ids=flow_ids,
                max_records=selection.max_records,
                max_nodes=selection.max_nodes,
                max_edges=selection.max_edges,
            )
            assert isinstance(graph, GraphProjection)
            graph = _rebind_projection(graph, report, request, selection)
    except ProjectionBuildError as error:
        return _unavailable(request, reader, str(error), limitations)

    if not graph.nodes:
        return _unavailable(
            request, reader, "No recorded relationship nodes support this selection.", limitations
        )
    completeness = graph.completeness
    if browse.completeness is Completeness.PARTIAL and completeness is Completeness.COMPLETE:
        completeness = Completeness.PARTIAL
        limitations.append("Saved report completeness is partial.")
    if graph.truncated and completeness is Completeness.COMPLETE:
        completeness = Completeness.PARTIAL
    if graph.completeness is not completeness:
        graph = graph.model_copy(update={"completeness": completeness})
    return CodeGraphResult(
        report_sha256=request.report_sha256,
        revision=request.revision,
        layer=request.layer,
        state="available",
        completeness=completeness,
        message=_available_message(request.layer, graph),
        graph=graph,
        links=_graph_links(graph, reader),
        limitations=tuple(sorted(set(limitations))),
    )


def _graph_links(graph: GraphProjection, reader: CodeBrowseReader) -> tuple[CodeGraphLink, ...]:
    links: list[CodeGraphLink] = []
    for node in graph.nodes:
        identity = node.node_id.removeprefix(_CONTAINED_NODE_PREFIX)
        if identity in reader.identity_map:
            links.append(CodeGraphLink(node_id=node.node_id, identity=identity))
    return tuple(sorted(links, key=lambda item: item.node_id))


def _available_message(layer: CodeGraphLayer, graph: GraphProjection) -> str:
    return (
        f"{_LAYER_TITLES[layer.value]} retained {len(graph.nodes)} nodes and "
        f"{len(graph.edges)} edges from {graph.original_node_count} nodes and "
        f"{graph.original_edge_count} edges."
    )


def _unavailable(
    request: CodeGraphRequest,
    reader: CodeBrowseReader,
    message: str,
    limitations: list[str],
) -> CodeGraphResult:
    completeness = reader.completeness
    if completeness is Completeness.COMPLETE:
        completeness = Completeness.PARTIAL
    return CodeGraphResult(
        report_sha256=request.report_sha256,
        revision=request.revision,
        layer=request.layer,
        state="unavailable",
        completeness=completeness,
        message=message[:512],
        graph=None,
        limitations=tuple(sorted(set(limitations))),
    )


def _contained_node_id(identity: str) -> str:
    return _CONTAINED_NODE_PREFIX + identity


def _build_containment_graph(
    report: AnalysisReport,
    reader: CodeBrowseReader,
    request: CodeGraphRequest,
    path: str,
    canonical_identity: str,
    selection: ProjectionSelection,
    limitations: list[str],
) -> GraphProjection:
    path_index = reader.paths[path]
    entries = path_index.entries
    nodes: dict[str, GraphNode] = {}
    for entry in entries:
        node_id = _contained_node_id(entry.identity)
        nodes[node_id] = GraphNode(
            node_id=node_id,
            label=entry.label,
            kind=f"contained-{entry.kind}",
        )
    edges: list[GraphEdge] = []
    for entry in entries:
        if entry.parent_identity is None or entry.parent_relation is None:
            continue
        parent_id = _contained_node_id(entry.parent_identity)
        node_id = _contained_node_id(entry.identity)
        if parent_id not in nodes:
            continue
        edge_identity = _edge_id(parent_id, node_id, entry.parent_relation)
        edges.append(
            GraphEdge(
                edge_id=edge_identity,
                source_id=parent_id,
                target_id=node_id,
                kind=entry.parent_relation,
                label=entry.parent_relation,
            )
        )
    selected_ids = (
        {
            _contained_node_id(canonical_identity),
        }
        if canonical_identity
        else set()
    )
    return _finalize_containment_graph(
        report, request, selection, tuple(nodes.values()), edges, selected_ids, limitations
    )


def _finalize_containment_graph(
    report: AnalysisReport,
    request: CodeGraphRequest,
    selection: ProjectionSelection,
    nodes: tuple[GraphNode, ...],
    edges: list[GraphEdge],
    selected_ids: set[str],
    limitations: list[str],
) -> GraphProjection:
    ownership = graph_ownership(report)
    modules = {item.name: item for item in report.modules}
    scc_modules = {
        name
        for component in report.strongly_connected_components
        if len(component.modules) > 1
        for name in component.modules
    }

    def rank_context(node: GraphNode) -> GraphNodeRank:
        raw_id = node.node_id.removeprefix(_CONTAINED_NODE_PREFIX)
        module_name = ownership.module_for(raw_id, node.group_id)
        module = modules.get(module_name or "")
        return GraphNodeRank(
            node_id=node.node_id,
            selected=node.node_id in selected_ids,
            scc_member=module_name in scc_modules if module_name else False,
            fan_in=module.fan_in if module is not None else 0,
            fan_out=module.fan_out if module is not None else 0,
        )

    sorted_nodes = tuple(sorted(nodes, key=lambda item: item.node_id))
    sorted_edges = tuple(sorted(edges, key=lambda item: item.edge_id))
    collapsed_nodes, collapsed_edges, _collapsed_keys = collapse_graph(
        sorted_nodes, sorted_edges, selection.collapse, ownership
    )
    original_node_count = len(collapsed_nodes)
    original_edge_count = len(collapsed_edges)
    retention = retain_graph(
        tuple(rank_context(node) for node in collapsed_nodes),
        tuple(
            GraphEdgeIdentity(
                edge_id=edge.edge_id,
                source_id=edge.source_id,
                target_id=edge.target_id,
            )
            for edge in collapsed_edges
        ),
        max_records=selection.max_records,
        max_nodes=selection.max_nodes,
        max_edges=selection.max_edges,
    )
    retained_node_ids = set(retention.node_ids)
    retained_nodes = tuple(
        sorted(
            (node for node in collapsed_nodes if node.node_id in retained_node_ids),
            key=lambda item: item.node_id,
        )
    )
    retained_edge_ids = set(retention.edge_ids)
    retained_edges = tuple(
        sorted(
            (edge for edge in collapsed_edges if edge.edge_id in retained_edge_ids),
            key=lambda item: item.edge_id,
        )
    )
    omitted_nodes = original_node_count - len(retained_nodes)
    omitted_edges = original_edge_count - len(retained_edges)
    truncated = bool(omitted_nodes or omitted_edges)
    graph_limitations = set(limitations)
    if selection.collapse is not ProjectionCollapse.NONE:
        graph_limitations.add(COLLAPSE_LIMITATION)
    if truncated:
        graph_limitations.add("Configured graph node or edge bounds omitted records.")
    graph_limitations.add(
        "Containment edges use saved module membership and span containment only; "
        "missing parents remain explicit."
    )
    source = _report_input(report)
    completeness = report.completeness
    if truncated and completeness is Completeness.COMPLETE:
        completeness = Completeness.PARTIAL
    return GraphProjection(
        projection_id=_code_graph_projection_id(source, request, selection),
        title=_LAYER_TITLES[request.layer.value],
        inputs=(source,),
        selection=selection,
        completeness=completeness,
        record_count=len(retained_nodes) + len(retained_edges),
        truncated=truncated,
        truncation_reason="graph node or edge limit reached" if truncated else None,
        limitations=tuple(sorted(graph_limitations)),
        direction=EdgeDirection.DIRECTED,
        nodes=retained_nodes,
        edges=retained_edges,
        clusters=(),
        original_node_count=original_node_count,
        original_edge_count=original_edge_count,
        omitted_node_count=omitted_nodes,
        omitted_edge_count=omitted_edges,
    )


def _rebind_projection(
    graph: GraphProjection,
    report: AnalysisReport,
    request: CodeGraphRequest,
    selection: ProjectionSelection,
) -> GraphProjection:
    source = _report_input(report)
    if source.report_sha256 != request.report_sha256:
        raise CodeBrowseBindingError("projection source disagrees with the code graph request")
    return graph.model_copy(
        update={
            "projection_id": _code_graph_projection_id(source, request, selection),
            "title": _LAYER_TITLES[request.layer.value],
            "inputs": (source,),
            "selection": selection,
        }
    )


def _code_graph_projection_id(
    source: ProjectionInput,
    request: CodeGraphRequest,
    selection: ProjectionSelection,
) -> str:
    payload = json.dumps(
        {
            "layer": request.layer.value,
            "request": request.model_dump(mode="json"),
            "selection": selection.model_dump(mode="json"),
            "source": source.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "projection-" + hashlib.sha256(payload.encode()).hexdigest()[:20]


def _resolve_module_name(
    report: AnalysisReport,
    reader: CodeBrowseReader,
    path: str | None,
    canonical_identity: str,
    selected: CodeBrowseEntry | None,
) -> str | None:
    if selected is not None:
        return selected.module
    if canonical_identity:
        entry = reader.identity_map.get(canonical_identity)
        if entry is not None:
            return entry.module
        if canonical_identity in {module.name for module in report.modules}:
            return canonical_identity
    if path:
        module = next((item for item in report.modules if item.path == path), None)
        if module is not None:
            return module.name
        source = next((item for item in report.source_files if item.logical_path == path), None)
        if source is not None:
            return source.module_name
    return None


def _lexical_subject_ids(
    report: AnalysisReport,
    reader: CodeBrowseReader,
    path: str | None,
    canonical_identity: str,
) -> tuple[str, ...]:
    if not report.function_profiles:
        return ()
    module_names = {module.name for module in report.modules}

    def profiles_for_module(module_name: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                profile.subject_id
                for profile in report.function_profiles
                if _subject_module(report, reader, profile.subject_id) == module_name
            )
        )

    if canonical_identity:
        entry = reader.identity_map.get(canonical_identity)
        if entry is not None and entry.kind == "module":
            return profiles_for_module(entry.identity)
        if canonical_identity in module_names:
            return profiles_for_module(canonical_identity)
        subject_ids = tuple(
            sorted(
                {
                    profile.subject_id
                    for profile in report.function_profiles
                    if reader.aliases.get(profile.subject_id, profile.subject_id)
                    == canonical_identity
                    or profile.subject_id == canonical_identity
                }
            )
        )
        if subject_ids:
            return subject_ids
        entry = reader.identity_map.get(canonical_identity)
        if entry is None or entry.kind != "source-file":
            return ()
    if path:
        module_name = _resolve_module_name(report, reader, path, "", None)
        if module_name is not None:
            return profiles_for_module(module_name)
    return ()


def _subject_module(report: AnalysisReport, reader: CodeBrowseReader, subject_id: str) -> str:
    canonical = reader.aliases.get(subject_id, subject_id)
    entry = reader.identity_map.get(canonical)
    if entry is not None:
        return entry.module
    subject = next((item for item in report.subjects if item.subject_id == subject_id), None)
    return subject.module if subject is not None else ""


def _static_call_site_selection(
    report: AnalysisReport,
    reader: CodeBrowseReader,
    path: str | None,
    canonical_identity: str,
    selected: CodeBrowseEntry | None,
) -> tuple[str, ...] | None:
    modules = {item.name for item in report.modules}
    if canonical_identity in modules:
        return None
    if selected is not None and selected.kind == "source-file":
        return (selected.source_file_id,)
    if canonical_identity:
        if canonical_identity.startswith("call-occurrence-") and any(
            item.occurrence_id == canonical_identity for item in report.static_call_occurrences
        ):
            return (canonical_identity,)
        entry = reader.identity_map.get(canonical_identity)
        if entry is not None:
            if entry.kind == "source-file":
                return (entry.source_file_id,)
            return None
    if path:
        source = next((item for item in report.source_files if item.logical_path == path), None)
        if source is not None:
            return (source.source_file_id,)
    return None


def _dictionary_flow_seed_ids(
    report: AnalysisReport,
    reader: CodeBrowseReader,
    path: str | None,
    canonical_identity: str,
    selected: CodeBrowseEntry | None,
) -> tuple[str, ...]:
    if not report.flow_edges:
        return ()
    subjects = {item.subject_id: item for item in report.subjects}
    shapes = {item.shape_id: item for item in report.dictionary_shapes}
    if selected is not None and selected.kind in {
        "dictionary-shape-evidence",
        "observed-state",
    }:
        shape = shapes.get(selected.identity)
        if shape is not None:
            return (shape.owner_subject_id,)
    if canonical_identity in shapes:
        return (shapes[canonical_identity].owner_subject_id,)
    if canonical_identity:
        entry = reader.identity_map.get(canonical_identity)
        if entry is not None and entry.kind in {
            "dictionary-shape-evidence",
            "observed-state",
        }:
            shape = shapes.get(entry.identity)
            if shape is not None:
                return (shape.owner_subject_id,)
        if canonical_identity in subjects:
            return (canonical_identity,)
        alias_subject = next(
            (
                subject_id
                for subject_id, alias in reader.aliases.items()
                if alias == canonical_identity
            ),
            None,
        )
        if alias_subject is not None:
            return (alias_subject,)
        if entry is None or entry.kind not in {"source-file", "module"}:
            return ()
    module_name = None
    if canonical_identity in {module.name for module in report.modules}:
        module_name = canonical_identity
    elif path:
        module_name = _resolve_module_name(report, reader, path, "", None)
    if module_name is not None:
        owner_ids = tuple(
            sorted(
                {
                    shape.owner_subject_id
                    for shape in report.dictionary_shapes
                    if subjects.get(shape.owner_subject_id) is not None
                    and subjects[shape.owner_subject_id].module == module_name
                }
            )
        )
        return owner_ids
    return ()
