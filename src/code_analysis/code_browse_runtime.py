"""Saved-profile runtime source mapping and focused observed-call graphs for the code browser."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Self

from pydantic import Field, StrictInt, StrictStr, model_validator

from code_analysis.code_browse import (
    CodeBrowseBindingError,
    CodeBrowseEntry,
    CodeBrowsePage,
    CodeBrowseReader,
    CodeBrowseRequest,
)
from code_analysis.code_browse_graph import (
    CodeGraphLayer,
    CodeGraphLink,
    CodeGraphRequest,
    CodeGraphResult,
)
from code_analysis.models import Completeness, SourceLanguage, SourceSpan, StrictModel
from code_analysis.profile_models import (
    FrameClassification,
    ProfileFrame,
    ProfileReport,
    RuntimeCallEdge,
)
from code_analysis.profiling import render_profile_json
from code_analysis.projection_models import (
    EdgeDirection,
    ProjectionInput,
    ProjectionInputRole,
    ProjectionSelection,
)
from code_analysis.projections.runtime import RuntimeGraphView, runtime_graph_projection
from code_analysis.report_models import AnalysisReport, SourceFileRecord
from code_analysis.reports.json_report import canonical_json
from code_analysis.runtime_models import RuntimeSelection, RuntimeSourceHash, SourceBindingState

_RUNTIME_LAYER_TITLE = "Code browser runtime observed calls"
_UNSUPPORTED_FOCUS_KINDS = frozenset(
    {
        "contract-evidence",
        "dictionary-shape-evidence",
        "data-structure-evidence",
        "observed-state",
    }
)


class CodeRuntimeSourceRequest(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    profile_report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    profile_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    revision: StrictInt = Field(ge=0)
    frame_id: StrictStr = Field(pattern=r"^profile-frame-[0-9a-f]{20}$")


class CodeRuntimeSourceResult(StrictModel):
    report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    profile_report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    profile_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    revision: StrictInt = Field(ge=0)
    frame_id: StrictStr = Field(pattern=r"^profile-frame-[0-9a-f]{20}$")
    state: Literal["available", "unavailable"]
    completeness: Completeness
    message: StrictStr = Field(min_length=1, max_length=512)
    selector: CodeBrowseRequest | None = None

    @model_validator(mode="after")
    def reconcile_selector(self) -> Self:
        if self.state == "available":
            if self.selector is None:
                raise ValueError("available runtime source requires a browse selector")
            if (
                self.selector.report_sha256 != self.report_sha256
                or self.selector.revision != self.revision
            ):
                raise ValueError("runtime source selector disagrees with pinned report binding")
            if not self.selector.path or self.selector.line is None or self.selector.line < 1:
                raise ValueError(
                    "available runtime source selector requires path and positive line"
                )
        elif self.selector is not None:
            raise ValueError("unavailable runtime source cannot include a browse selector")
        if self.state == "unavailable" and self.completeness is Completeness.COMPLETE:
            raise ValueError("unavailable runtime source cannot be complete")
        return self


@dataclass(frozen=True, slots=True)
class _FrameSourceBinding:
    frame: ProfileFrame
    source: SourceFileRecord
    captured: RuntimeSourceHash
    selector_identity: str


@dataclass(frozen=True, slots=True)
class _FocusContext:
    path: str
    spans: tuple[SourceSpan, ...] | None
    entry: CodeBrowseEntry | None


@dataclass(frozen=True, slots=True)
class _SourceBindings:
    reader: CodeBrowseReader
    matched_report: bool
    sources: Mapping[str, SourceFileRecord]
    captured: Mapping[str, RuntimeSourceHash]


def _source_bindings(
    report: AnalysisReport, reader: CodeBrowseReader, profile: ProfileReport, digest: str
) -> _SourceBindings:
    sources = {item.logical_path: item for item in report.source_files}
    captured = {item.path: item for item in profile.runtime.source_hashes}
    if len(sources) != len(report.source_files):
        raise ValueError("static source record identity is ambiguous")
    if len(captured) != len(profile.runtime.source_hashes):
        raise ValueError("runtime captured source hash is ambiguous")
    return _SourceBindings(
        reader=reader,
        matched_report=(
            profile.source_report_sha256 == digest
            and profile.runtime.source_binding is SourceBindingState.MATCHED
        ),
        sources=MappingProxyType(sources),
        captured=MappingProxyType(captured),
    )


def read_code_runtime_source(
    report: AnalysisReport,
    reader: CodeBrowseReader,
    request: CodeRuntimeSourceRequest,
    profile: ProfileReport | None,
) -> CodeRuntimeSourceResult:
    static_digest = _static_report_digest(report)
    _verify_static_binding(static_digest, reader, request.report_sha256)
    pins = _ProfilePins(
        profile_report_sha256=request.profile_report_sha256,
        profile_identity_sha256=request.profile_identity_sha256,
    )
    if profile is None:
        return _unavailable_source(
            request, reader.completeness, "No saved runtime profile is selected."
        )
    _verify_profile_pins(profile, pins)
    frame = _unique_frame(profile, request.frame_id)
    bindings = _source_bindings(report, reader, profile, static_digest)
    binding = _resolve_frame_source_binding(bindings, frame)
    if binding is None:
        return _unavailable_source(
            request,
            _source_completeness(reader.completeness, profile.completeness),
            "Saved runtime frame has no exact static source mapping.",
        )
    selector = CodeBrowseRequest(
        report_sha256=request.report_sha256,
        revision=request.revision,
        path=binding.frame.path,
        identity=binding.selector_identity,
        line=binding.frame.line,
    )
    return CodeRuntimeSourceResult(
        report_sha256=request.report_sha256,
        profile_report_sha256=request.profile_report_sha256,
        profile_identity_sha256=request.profile_identity_sha256,
        revision=request.revision,
        frame_id=request.frame_id,
        state="available",
        completeness=_source_completeness(reader.completeness, profile.completeness),
        message=(
            f"Runtime frame {request.frame_id} maps to {binding.frame.path}:{binding.frame.line}."
        ),
        selector=selector,
    )


def build_code_runtime_graph(
    report: AnalysisReport,
    reader: CodeBrowseReader,
    request: CodeGraphRequest,
    profile: ProfileReport | None,
) -> CodeGraphResult:
    static_digest = _static_report_digest(report)
    _verify_static_binding(static_digest, reader, request.report_sha256)
    assert request.profile_report_sha256 is not None
    assert request.profile_identity_sha256 is not None
    pins = _ProfilePins(
        profile_report_sha256=request.profile_report_sha256,
        profile_identity_sha256=request.profile_identity_sha256,
    )
    limitations: list[str] = []
    if profile is None:
        return _unavailable_graph(
            request,
            reader,
            "No saved runtime profile is selected.",
            limitations,
        )
    _verify_profile_pins(profile, pins)
    if profile.source_report_sha256 != static_digest:
        limitations.extend(profile.limitations)
        return _unavailable_graph(
            request,
            reader,
            "Runtime profile is not bound to the selected static report.",
            limitations,
            profile=profile,
        )
    if profile.runtime.source_binding is not SourceBindingState.MATCHED:
        limitations.extend(profile.limitations)
        return _unavailable_graph(
            request,
            reader,
            "Runtime source binding is not matched for the selected profile.",
            limitations,
            profile=profile,
        )
    browse = reader.read(
        CodeBrowseRequest(
            report_sha256=request.report_sha256,
            revision=request.revision,
            path=request.path,
            identity=request.identity,
            limit=100,
        )
    )
    limitations.extend(browse.limitations)
    if request.identity and not request.path and browse.selection_location_count > 1:
        limitations.append("Choose a recorded source location; this identity spans multiple files.")
        return _unavailable_graph(
            request,
            reader,
            "Focused runtime graph requires a single recorded source location.",
            limitations,
            profile=profile,
        )
    focus = _resolve_graph_focus(reader, browse, request)
    if focus is None:
        return _unavailable_graph(
            request,
            reader,
            "Runtime call graph does not support this browse selection.",
            limitations,
            profile=profile,
        )
    source_bindings = _source_bindings(report, reader, profile, static_digest)
    candidates = _seed_frames(profile, focus)
    bindings = {
        frame.frame_id: _resolve_frame_source_binding(source_bindings, frame)
        for frame in candidates
    }
    seeds = tuple(frame for frame in candidates if bindings[frame.frame_id] is not None)
    unbound_focus = len(candidates) - len(seeds)
    if unbound_focus:
        limitations.append(
            f"{unbound_focus} possible focus frames lack proven source bindings and were not seeds."
        )
    if not seeds:
        return _unavailable_graph(
            request,
            reader,
            "No source-bound project runtime frames match the focused selection.",
            limitations,
            profile=profile,
        )
    seed_ids = {frame.frame_id for frame in seeds}
    eligible_ids, eligible_edges = _bfs_neighborhood(profile, seed_ids, request.depth)
    frames_by_id = {frame.frame_id: frame for frame in profile.frames}
    eligible_frames = tuple(
        sorted(
            (frames_by_id[node_id] for node_id in eligible_ids if node_id in frames_by_id),
            key=lambda item: item.frame_id,
        )
    )
    retained_frames = _retain_frames(eligible_frames, seed_ids, request.max_nodes)
    retained_ids = {frame.frame_id for frame in retained_frames}
    retained_edges = _retain_edges(eligible_edges, retained_ids, request.max_edges)
    for frame in retained_frames:
        if frame.frame_id not in bindings:
            bindings[frame.frame_id] = _resolve_frame_source_binding(source_bindings, frame)
    unresolved_bindings = [frame for frame in retained_frames if bindings[frame.frame_id] is None]
    completeness = _graph_completeness(
        reader.completeness,
        profile.completeness,
        browse.completeness,
        retained_frames,
        eligible_frames,
        retained_edges,
        eligible_edges,
        bool(unbound_focus or unresolved_bindings),
    )
    if unresolved_bindings:
        limitations.append(
            "Some retained runtime frames have no exact static source mapping; "
            "graph nodes remain observed-only."
        )
    limitations.extend(profile.limitations)
    limitations.append(
        f"Focused neighborhood intentionally excludes {len(profile.frames) - len(eligible_frames)} "
        f"saved frames and {len(profile.runtime_edges) - len(eligible_edges)} saved edges."
    )
    source_input = _profile_projection_input(profile, pins.profile_report_sha256)
    selection = ProjectionSelection(
        max_records=request.max_nodes + request.max_edges,
        max_nodes=request.max_nodes,
        max_edges=request.max_edges,
    )
    view = RuntimeGraphView(
        frames=retained_frames,
        edges=retained_edges,
        original_nodes=len(eligible_frames),
        original_edges=len(eligible_edges),
    )
    runtime_selection = RuntimeSelection(
        max_nodes=request.max_nodes,
        max_edges=request.max_edges if request.max_edges > 0 else 1,
        max_records=request.max_nodes + max(request.max_edges, 1),
        project_only=False,
    )
    graph = runtime_graph_projection(
        view,
        runtime_selection,
        source_input,
        completeness,
        (),
    )
    graph = graph.model_copy(
        update={
            "projection_id": _runtime_graph_projection_id(
                static_digest,
                pins,
                request,
                selection,
                source_input,
            ),
            "title": _RUNTIME_LAYER_TITLE,
            "direction": EdgeDirection.DIRECTED,
            "selection": selection,
            "completeness": completeness,
            "limitations": tuple(
                sorted(
                    {item for item in graph.limitations if not item.startswith("Nodes rank by")}
                    | set(limitations)
                    | {
                        "Scoped workload calls only; not exhaustive program or GPU time.",
                        "Callbacks, threads, and subprocess gaps from the profile are retained.",
                        "Nodes rank by focus membership, cumulative seconds descending, "
                        "then frame ID; "
                        "edges by call count descending, then edge ID.",
                    }
                )
            ),
        }
    )
    links = _graph_frame_links(bindings, retained_frames)
    return CodeGraphResult(
        report_sha256=request.report_sha256,
        revision=request.revision,
        layer=CodeGraphLayer.RUNTIME_CALLS,
        state="available",
        completeness=completeness,
        message=(
            f"{_RUNTIME_LAYER_TITLE} retained {len(graph.nodes)} nodes and "
            f"{len(graph.edges)} edges from {graph.original_node_count} nodes and "
            f"{graph.original_edge_count} edges."
        ),
        graph=graph,
        links=links,
        profile_report_sha256=pins.profile_report_sha256,
        profile_identity_sha256=pins.profile_identity_sha256,
        limitations=tuple(sorted(set(limitations))),
    )


@dataclass(frozen=True, slots=True)
class _ProfilePins:
    profile_report_sha256: str
    profile_identity_sha256: str


def _static_report_digest(report: AnalysisReport) -> str:
    return hashlib.sha256(canonical_json(report).encode()).hexdigest()


def _profile_report_digest(profile: ProfileReport) -> str:
    return hashlib.sha256(render_profile_json(profile, compact=True).encode()).hexdigest()


def _verify_static_binding(digest: str, reader: CodeBrowseReader, report_sha256: str) -> None:
    if digest != report_sha256:
        raise CodeBrowseBindingError("runtime browse request is bound to another report")
    if digest != reader.report_sha256:
        raise CodeBrowseBindingError("runtime browse reader is bound to another report")


def _verify_profile_pins(profile: ProfileReport, pins: _ProfilePins) -> None:
    digest = _profile_report_digest(profile)
    if digest != pins.profile_report_sha256:
        raise CodeBrowseBindingError("runtime browse request is bound to another profile report")
    if profile.profile_identity_sha256 != pins.profile_identity_sha256:
        raise CodeBrowseBindingError("runtime browse request is bound to another profile identity")


def _unique_frame(profile: ProfileReport, frame_id: str) -> ProfileFrame:
    matches = [frame for frame in profile.frames if frame.frame_id == frame_id]
    if not matches:
        raise ValueError("runtime frame is absent from the selected profile")
    if len(matches) != 1:
        raise ValueError("runtime frame identity is ambiguous")
    return matches[0]


def _line_in_span(span: SourceSpan, line: int) -> bool:
    return span.start_line <= line <= span.end_line


def _subject_selector_identity(
    reader: CodeBrowseReader, frame: ProfileFrame, subject_id: str
) -> str | None:
    canonical = reader.aliases.get(subject_id, subject_id)
    entry = reader.identity_map.get(canonical)
    if entry is None:
        return None
    if entry.logical_path != frame.path:
        return None
    if not any(_line_in_span(span, frame.line) for span in entry.spans):
        return None
    paths = reader.identity_paths.get(canonical, ())
    if len(set(paths)) != 1:
        raise ValueError("runtime subject identity is ambiguous")
    return canonical


def _resolve_frame_source_binding(
    bindings: _SourceBindings,
    frame: ProfileFrame,
) -> _FrameSourceBinding | None:
    if not bindings.matched_report:
        return None
    if frame.classification is not FrameClassification.PROJECT or frame.line < 1:
        return None
    source = bindings.sources.get(frame.path)
    if source is None or source.language is not SourceLanguage.PYTHON:
        return None
    if frame.line > source.physical_loc:
        return None
    captured = bindings.captured.get(frame.path)
    if captured is None or captured.sha256 != source.source_sha256:
        return None
    selector_identity = ""
    if frame.subject_id is not None:
        resolved = _subject_selector_identity(bindings.reader, frame, frame.subject_id)
        if resolved is None:
            return None
        selector_identity = resolved
    return _FrameSourceBinding(
        frame=frame,
        source=source,
        captured=captured,
        selector_identity=selector_identity,
    )


def _source_completeness(
    report_completeness: Completeness, profile_completeness: Completeness
) -> Completeness:
    if Completeness.FAILED in (report_completeness, profile_completeness):
        return Completeness.FAILED
    if report_completeness is Completeness.PARTIAL or profile_completeness is Completeness.PARTIAL:
        return Completeness.PARTIAL
    return Completeness.COMPLETE


def _unavailable_source(
    request: CodeRuntimeSourceRequest,
    completeness: Completeness,
    message: str,
) -> CodeRuntimeSourceResult:
    if completeness is Completeness.COMPLETE:
        completeness = Completeness.PARTIAL
    return CodeRuntimeSourceResult(
        report_sha256=request.report_sha256,
        profile_report_sha256=request.profile_report_sha256,
        profile_identity_sha256=request.profile_identity_sha256,
        revision=request.revision,
        frame_id=request.frame_id,
        state="unavailable",
        completeness=completeness,
        message=message[:512],
        selector=None,
    )


def _unavailable_graph(
    request: CodeGraphRequest,
    reader: CodeBrowseReader,
    message: str,
    limitations: list[str],
    *,
    profile: ProfileReport | None = None,
) -> CodeGraphResult:
    completeness = _source_completeness(
        reader.completeness, profile.completeness if profile else Completeness.PARTIAL
    )
    if completeness is Completeness.COMPLETE:
        completeness = Completeness.PARTIAL
    assert request.profile_report_sha256 is not None
    assert request.profile_identity_sha256 is not None
    return CodeGraphResult(
        report_sha256=request.report_sha256,
        revision=request.revision,
        layer=CodeGraphLayer.RUNTIME_CALLS,
        state="unavailable",
        completeness=completeness,
        message=message[:512],
        graph=None,
        profile_report_sha256=request.profile_report_sha256,
        profile_identity_sha256=request.profile_identity_sha256,
        limitations=tuple(sorted(set(limitations))),
    )


def _resolve_graph_focus(
    reader: CodeBrowseReader,
    browse: CodeBrowsePage,
    request: CodeGraphRequest,
) -> _FocusContext | None:
    page = browse
    if request.identity:
        canonical = reader.aliases.get(request.identity, request.identity)
        entry = reader.identity_map.get(canonical)
        if entry is None or entry.kind in _UNSUPPORTED_FOCUS_KINDS:
            return None
        if request.path and entry.logical_path != request.path:
            return None
        return _FocusContext(path=entry.logical_path, spans=entry.spans, entry=entry)
    if request.path:
        return _FocusContext(path=request.path, spans=None, entry=page.selected)
    return None


def _seed_frames(profile: ProfileReport, focus: _FocusContext) -> tuple[ProfileFrame, ...]:
    seeds: list[ProfileFrame] = []
    for frame in profile.frames:
        if frame.path != focus.path:
            continue
        if frame.classification is not FrameClassification.PROJECT or frame.line < 1:
            continue
        if focus.spans is not None and not any(
            _line_in_span(span, frame.line) for span in focus.spans
        ):
            continue
        seeds.append(frame)
    return tuple(sorted(seeds, key=lambda item: item.frame_id))


def _bfs_neighborhood(
    profile: ProfileReport, seed_ids: set[str], depth: int
) -> tuple[set[str], tuple[RuntimeCallEdge, ...]]:
    adjacency: dict[str, set[str]] = {}
    for edge in profile.runtime_edges:
        adjacency.setdefault(edge.caller_frame_id, set()).add(edge.callee_frame_id)
        adjacency.setdefault(edge.callee_frame_id, set()).add(edge.caller_frame_id)
    visited = set(seed_ids)
    frontier = set(seed_ids)
    for _ in range(depth):
        next_ids: set[str] = set()
        for node_id in sorted(frontier):
            for neighbor in adjacency.get(node_id, ()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_ids.add(neighbor)
        frontier = next_ids
    eligible_edges = tuple(
        sorted(
            (
                edge
                for edge in profile.runtime_edges
                if edge.caller_frame_id in visited and edge.callee_frame_id in visited
            ),
            key=lambda item: item.runtime_edge_id,
        )
    )
    return visited, eligible_edges


def _frame_rank(frame: ProfileFrame, seed_ids: set[str]) -> tuple[int, float, str]:
    return (
        0 if frame.frame_id in seed_ids else 1,
        -(frame.cumulative_seconds or 0.0),
        frame.frame_id,
    )


def _retain_frames(
    frames: tuple[ProfileFrame, ...], seed_ids: set[str], max_nodes: int
) -> tuple[ProfileFrame, ...]:
    ordered = sorted(frames, key=lambda item: _frame_rank(item, seed_ids))
    return tuple(sorted(ordered[:max_nodes], key=lambda item: item.frame_id))


def _retain_edges(
    edges: tuple[RuntimeCallEdge, ...], retained_ids: set[str], max_edges: int
) -> tuple[RuntimeCallEdge, ...]:
    eligible = tuple(
        sorted(
            (
                edge
                for edge in edges
                if edge.caller_frame_id in retained_ids and edge.callee_frame_id in retained_ids
            ),
            key=lambda item: (-item.total_calls, item.runtime_edge_id),
        )
    )
    return tuple(sorted(eligible[:max_edges], key=lambda item: item.runtime_edge_id))


def _graph_completeness(
    reader_completeness: Completeness,
    profile_completeness: Completeness,
    browse_completeness: Completeness,
    retained_frames: tuple[ProfileFrame, ...],
    eligible_frames: tuple[ProfileFrame, ...],
    retained_edges: tuple[RuntimeCallEdge, ...],
    eligible_edges: tuple[RuntimeCallEdge, ...],
    unresolved_seed_bindings: bool,
) -> Completeness:
    if Completeness.FAILED in (reader_completeness, profile_completeness, browse_completeness):
        return Completeness.FAILED
    if (
        reader_completeness is Completeness.PARTIAL
        or profile_completeness is Completeness.PARTIAL
        or browse_completeness is Completeness.PARTIAL
        or unresolved_seed_bindings
        or len(retained_frames) < len(eligible_frames)
        or len(retained_edges) < len(eligible_edges)
    ):
        return Completeness.PARTIAL
    return Completeness.COMPLETE


def _profile_projection_input(
    profile: ProfileReport, profile_report_sha256: str
) -> ProjectionInput:
    return ProjectionInput(
        role=ProjectionInputRole.SOURCE,
        report_schema_name=profile.schema_name,
        report_schema_version=profile.schema_version,
        report_sha256=profile_report_sha256,
        run_identity_sha256=profile.profile_identity_sha256,
    )


def _runtime_graph_projection_id(
    static_digest: str,
    pins: _ProfilePins,
    request: CodeGraphRequest,
    selection: ProjectionSelection,
    source: ProjectionInput,
) -> str:
    payload = json.dumps(
        {
            "layer": CodeGraphLayer.RUNTIME_CALLS.value,
            "static_report_sha256": static_digest,
            "profile_report_sha256": pins.profile_report_sha256,
            "profile_identity_sha256": pins.profile_identity_sha256,
            "request": request.model_dump(mode="json"),
            "selection": selection.model_dump(mode="json"),
            "source": source.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "projection-" + hashlib.sha256(payload.encode()).hexdigest()[:20]


def _graph_frame_links(
    bindings: Mapping[str, _FrameSourceBinding | None],
    frames: tuple[ProfileFrame, ...],
) -> tuple[CodeGraphLink, ...]:
    links: list[CodeGraphLink] = []
    for frame in frames:
        binding = bindings[frame.frame_id]
        if binding is None:
            continue
        links.append(
            CodeGraphLink(
                node_id=frame.frame_id,
                identity=binding.selector_identity,
                path=binding.frame.path,
                line=binding.frame.line,
                source_sha256=binding.source.source_sha256,
            )
        )
    return tuple(sorted(links, key=lambda item: item.node_id))
