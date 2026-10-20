"""Indexed, source-free storage and browsing for static structure graphs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.graph_collapse import COLLAPSE_LIMITATION, collapse_graph
from code_analysis.graph_ownership import GraphOwnership, graph_ownership
from code_analysis.graph_selection import GraphEdgeIdentity, GraphNodeRank, retain_graph
from code_analysis.models import Completeness, Module, SourceLanguage, StrictModel
from code_analysis.projection_models import (
    EdgeDirection,
    GraphCluster,
    GraphEdge,
    GraphNode,
    GraphProjection,
    ProjectionCollapse,
    ProjectionInput,
    ProjectionSelection,
)
from code_analysis.projections import ProjectionName, build_projection
from code_analysis.projections._shared import _report_input
from code_analysis.projections.lexical_calls import _unknown_node
from code_analysis.report_models import AnalysisReport, SectionState

GRAPH_PROJECTIONS = (
    ProjectionName.OWNERSHIP_ALIAS_FLOW,
    ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
    ProjectionName.CONVERSION_GRAPH,
    ProjectionName.DEPENDENCIES,
    ProjectionName.DICTIONARY_FLOW,
    ProjectionName.LEXICAL_CALL_GRAPH,
    ProjectionName.STATIC_CALL_GRAPH,
)

_STATIC_CALL_SECTION = "static-call-occurrences"

GRAPH_NODE_TABLE_SQL = (
    "CREATE TABLE static_graph_nodes (name TEXT NOT NULL, node_id TEXT NOT NULL, "
    "scc_member INTEGER NOT NULL, fan_in INTEGER NOT NULL, fan_out INTEGER NOT NULL, "
    "languages TEXT NOT NULL, owner_module TEXT, owner_language TEXT, payload TEXT NOT NULL, "
    "PRIMARY KEY (name, node_id)) WITHOUT ROWID"
)
GRAPH_EDGE_TABLE_SQL = (
    "CREATE TABLE static_graph_edges (name TEXT NOT NULL, edge_id TEXT NOT NULL, "
    "source_id TEXT NOT NULL, target_id TEXT NOT NULL, languages TEXT NOT NULL, "
    "payload TEXT NOT NULL, "
    "PRIMARY KEY (name, edge_id)) WITHOUT ROWID"
)
GRAPH_CLUSTER_TABLE_SQL = (
    "CREATE TABLE static_graph_clusters (name TEXT NOT NULL, cluster_id TEXT NOT NULL, "
    "payload TEXT NOT NULL, PRIMARY KEY (name, cluster_id)) WITHOUT ROWID"
)

_MAX_PAYLOAD_BYTES = 1024**2
_MAX_RESULT_BYTES = 8 * 1024**2
_PREPARED_SELECTION = ProjectionSelection(max_records=100_000, max_nodes=100_000, max_edges=250_000)


class StaticGraphSelectionError(ValueError):
    """A requested identity is absent from the prepared static graph."""


class StaticGraphReadLimitError(ValueError):
    """Valid graph evidence exceeds the bounded read budget; narrow the focus."""


class StaticGraphOwner(StrictModel):
    module: StrictStr = Field(min_length=1)
    language: SourceLanguage


class StaticGraphSummary(StrictModel):
    name: ProjectionName
    title: StrictStr = Field(min_length=1)
    source: ProjectionInput
    completeness: Completeness
    available: StrictBool
    unavailable_reason: StrictStr | None = None
    preparation: ProjectionSelection = _PREPARED_SELECTION
    truncated: StrictBool = False
    truncation_reason: StrictStr | None = None
    limitations: tuple[StrictStr, ...] = ()
    direction: EdgeDirection = EdgeDirection.DIRECTED
    node_count: StrictInt = Field(ge=0)
    edge_count: StrictInt = Field(ge=0)
    cluster_count: StrictInt = Field(ge=0)
    original_node_count: StrictInt = Field(ge=0)
    original_edge_count: StrictInt = Field(ge=0)
    owner_modules: tuple[StaticGraphOwner, ...] = ()

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.name not in GRAPH_PROJECTIONS:
            raise ValueError("unsupported static graph name")
        if self.available == (self.unavailable_reason is not None):
            raise ValueError("static graph availability requires exactly one reason state")
        if not self.available and (
            self.owner_modules
            or any(
                (
                    self.node_count,
                    self.edge_count,
                    self.cluster_count,
                    self.original_node_count,
                    self.original_edge_count,
                )
            )
        ):
            raise ValueError("unavailable static graph cannot have records")
        if self.truncated != (self.truncation_reason is not None):
            raise ValueError("static graph truncation requires exactly one reason state")
        if self.truncated and self.completeness is Completeness.COMPLETE:
            raise ValueError("truncated static graph cannot be complete")
        if self.preparation != _PREPARED_SELECTION:
            raise ValueError("static graph preparation must use canonical bounds")
        if self.original_node_count < self.node_count or self.original_edge_count < self.edge_count:
            raise ValueError("static graph original counts cannot be smaller than prepared counts")
        if not self.truncated and (
            self.original_node_count > self.node_count or self.original_edge_count > self.edge_count
        ):
            raise ValueError("static graph omitted original records require truncation")
        if (
            self.node_count > self.preparation.max_nodes
            or self.edge_count > self.preparation.max_edges
            or self.node_count + self.edge_count > self.preparation.max_records
        ):
            raise ValueError("static graph counts exceed preparation bounds")
        if list(self.limitations) != sorted(self.limitations) or len(self.limitations) != len(
            set(self.limitations)
        ):
            raise ValueError("static graph limitations must be unique and sorted")
        owner_keys = [(item.module, item.language.value) for item in self.owner_modules]
        unique_modules = {item.module for item in self.owner_modules}
        if owner_keys != sorted(set(owner_keys)) or len(unique_modules) != len(self.owner_modules):
            raise ValueError("static graph owner modules must be unique and sorted")
        return self


class StaticGraphRequest(StrictModel):
    name: ProjectionName
    selected_ids: tuple[StrictStr, ...] = Field(default=(), max_length=25)
    languages: tuple[SourceLanguage, ...] = ()
    collapse: ProjectionCollapse = ProjectionCollapse.NONE
    max_records: StrictInt = Field(default=500, ge=1, le=500)
    max_nodes: StrictInt = Field(default=60, ge=1, le=100)
    max_edges: StrictInt = Field(default=120, ge=0, le=250)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.name not in GRAPH_PROJECTIONS:
            raise ValueError("unsupported static graph name")
        if list(self.selected_ids) != sorted(self.selected_ids) or len(self.selected_ids) != len(
            set(self.selected_ids)
        ):
            raise ValueError("selected IDs must be unique and sorted")
        if any(not value for value in self.selected_ids):
            raise ValueError("selected IDs must be nonempty")
        if list(self.languages) != sorted(self.languages, key=lambda item: item.value) or len(
            self.languages
        ) != len(set(self.languages)):
            raise ValueError("languages must be unique and sorted")
        return self


def _language_json(languages: set[SourceLanguage]) -> str:
    if not languages:
        raise ValueError("static graph record must have language membership")
    return json.dumps(sorted(item.value for item in languages), separators=(",", ":"))


def _parse_language_json(value: str) -> set[SourceLanguage]:
    if not isinstance(value, str) or len(value.encode()) > 64:
        raise ValueError("invalid static graph language membership")
    try:
        raw = json.loads(value)
        if not isinstance(raw, list) or len(raw) > len(SourceLanguage):
            raise ValueError
        languages = tuple(SourceLanguage(item) for item in raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("invalid static graph language membership") from error
    if not languages or list(languages) != sorted(set(languages), key=lambda item: item.value):
        raise ValueError("invalid static graph language membership")
    if value != _language_json(set(languages)):
        raise ValueError("noncanonical static graph language membership")
    return set(languages)


def _language_memberships(
    report: AnalysisReport, name: ProjectionName, projection: GraphProjection
) -> tuple[dict[str, set[SourceLanguage]], dict[str, set[SourceLanguage]]]:
    """Derive selection membership from recorded owners/flows and graph topology."""
    subjects = {item.subject_id: item for item in report.subjects}
    node_languages: dict[str, set[SourceLanguage]] = {
        item.node_id: set() for item in projection.nodes
    }
    edge_languages: dict[str, set[SourceLanguage]] = {
        item.edge_id: set() for item in projection.edges
    }
    edges = {item.edge_id: item for item in projection.edges}
    if name in {
        ProjectionName.OWNERSHIP_ALIAS_FLOW,
        ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
    }:
        if name is ProjectionName.OWNERSHIP_ALIAS_FLOW:
            relation_accessors = (
                lambda item: (item.owner_subject_id,),
                lambda item: item.producer_subject_ids,
                lambda item: item.writer_subject_ids,
                lambda item: item.mutator_subject_ids,
                lambda item: item.reader_subject_ids,
                lambda item: item.alias_subject_ids,
            )
        else:
            relation_accessors = (
                lambda item: (item.owner_subject_id,),
                lambda item: item.constructor_subject_ids,
                lambda item: item.producer_subject_ids,
                lambda item: item.reader_subject_ids,
                lambda item: item.writer_subject_ids,
                lambda item: item.mutator_subject_ids,
                lambda item: item.serializer_subject_ids,
                lambda item: item.alias_subject_ids,
            )
        for structure in report.data_structures:
            language = subjects[structure.owner_subject_id].language
            related = {structure.structure_id}
            related.update(
                identity for accessor in relation_accessors for identity in accessor(structure)
            )
            for identity in related & node_languages.keys():
                node_languages[identity].add(language)
        if name is ProjectionName.STRUCTURE_LIFECYCLE_GRAPH:
            for shape in report.dictionary_shapes:
                language = subjects[shape.owner_subject_id].language
                if shape.shape_id in node_languages:
                    node_languages[shape.shape_id].add(language)
                for state in shape.lifecycle_states:
                    if state.state_id in node_languages:
                        node_languages[state.state_id].add(language)
        for edge_id, edge in edges.items():
            edge_languages[edge_id].update(
                node_languages[edge.source_id] & node_languages[edge.target_id]
            )
    elif name is ProjectionName.STATIC_CALL_GRAPH:
        sources = {item.source_file_id: item.language for item in report.source_files}
        occurrences = {item.occurrence_id: item for item in report.static_call_occurrences}
        for node_id in node_languages:
            if node_id in sources:
                node_languages[node_id].add(sources[node_id])
            elif node_id in occurrences:
                node_languages[node_id].add(occurrences[node_id].language)
            else:
                raise ValueError("static call graph node has no saved source or occurrence")
        for edge_id, edge in edges.items():
            edge_languages[edge_id].update(
                node_languages[edge.source_id] & node_languages[edge.target_id]
            )
    elif name is ProjectionName.DEPENDENCIES:
        modules = {item.name: item.language for item in report.modules}
        for node_id in node_languages:
            language = modules.get(node_id)
            if language is None:
                raise ValueError("dependency graph node has no recorded module")
            node_languages[node_id].add(language)
        for edge_id, edge in edges.items():
            edge_languages[edge_id].update({modules[edge.source_id], modules[edge.target_id]})
    elif name is ProjectionName.LEXICAL_CALL_GRAPH:
        profiles = {item.subject_id: item for item in report.function_profiles}
        names: dict[tuple[SourceLanguage, str], list[str]] = {}
        for subject in report.subjects:
            names.setdefault((subject.language, subject.logical_name), []).append(
                subject.subject_id
            )
        for subject_id, profile in profiles.items():
            caller_language = profile.language
            if subject_id in node_languages:
                node_languages[subject_id].add(caller_language)
            for callee in profile.callees:
                matches = names.get((caller_language, callee), ())
                target_id = (
                    matches[0]
                    if len(matches) == 1
                    else _unknown_node(subject_id, callee, bool(matches)).node_id
                )
                if target_id in node_languages:
                    node_languages[target_id].add(caller_language)
        for edge_id, edge in edges.items():
            profile = profiles.get(edge.source_id)
            if profile is None:
                raise ValueError("lexical call graph edge source has no function profile")
            edge_languages[edge_id].add(profile.language)
    else:
        flows = {item.flow_id: item for item in report.flow_edges}
        relevant_flows = tuple(
            item
            for item in report.flow_edges
            if name is ProjectionName.DICTIONARY_FLOW
            or item.kind.value in {"conversion", "serialization"}
        )
        for flow in relevant_flows:
            languages = {
                subjects[flow.source_id].language,
                subjects[flow.target_id].language,
            }
            if flow.source_id in node_languages:
                node_languages[flow.source_id].update(languages)
            if flow.target_id in node_languages:
                node_languages[flow.target_id].update(languages)
        for edge_id in edges:
            flow = flows.get(edge_id)
            if flow is None:
                continue
            languages = {
                subjects[flow.source_id].language,
                subjects[flow.target_id].language,
            }
            edge_languages[edge_id].update(languages)
    if any(not value for value in node_languages.values()) or any(
        not value for value in edge_languages.values()
    ):
        raise ValueError("static graph language membership is incomplete")
    return node_languages, edge_languages


def _node_rank_maps(report: AnalysisReport) -> tuple[dict[str, Module], set[str], dict[str, str]]:
    modules = {item.name: item for item in report.modules}
    scc_modules = {
        name
        for component in report.strongly_connected_components
        if len(component.modules) > 1
        for name in component.modules
    }
    return modules, scc_modules, dict(graph_ownership(report).node_modules)


def _rank_for(
    node: GraphNode,
    modules: dict[str, Module],
    scc_modules: set[str],
    node_modules: dict[str, str],
) -> tuple[bool, int, int]:
    module_name = (
        node.node_id
        if node.node_id in modules
        else node.group_id if node.group_id in modules else node_modules.get(node.node_id)
    )
    module = modules.get(module_name or "")
    return (
        module_name in scc_modules,
        module.fan_in if module is not None else 0,
        module.fan_out if module is not None else 0,
    )


def _checked_payload(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("static graph payload must be text")
    if len(value.encode()) > _MAX_PAYLOAD_BYTES:
        raise ValueError("static graph record exceeds one-MiB bound")
    return value


def _charge_read_payload(value: str, total_bytes: int) -> tuple[str, int]:
    payload = _checked_payload(value)
    total_bytes += len(payload.encode())
    if total_bytes > _MAX_RESULT_BYTES:
        raise StaticGraphReadLimitError(
            "static graph read exceeds eight-MiB byte budget; narrow the selected neighborhood"
        )
    return payload, total_bytes


def _static_call_section_state(report: AnalysisReport) -> SectionState | None:
    for item in report.section_coverage:
        if item.section == _STATIC_CALL_SECTION:
            return item.state
    return None


def _static_call_graph_unavailable_reason(report: AnalysisReport) -> str | None:
    state = _static_call_section_state(report)
    if state is None:
        return "static-call-graph requires a static-call-occurrences section entry"
    if state is SectionState.DISABLED:
        return "static-call-occurrences section is disabled"
    if state is SectionState.UNAVAILABLE:
        return "static-call-occurrences section is unavailable"
    if state is SectionState.FAILED and not report.static_call_occurrences:
        return "static-call-occurrences section failed without retained call-site rows"
    return None


def _unavailable_reason(report: AnalysisReport, name: ProjectionName) -> str | None:
    if name is ProjectionName.STATIC_CALL_GRAPH:
        return _static_call_graph_unavailable_reason(report)
    if (
        name
        in {
            ProjectionName.OWNERSHIP_ALIAS_FLOW,
            ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
        }
        and not report.data_structures
    ):
        return f"{name.value} requires recorded data-structure evidence"
    if name is ProjectionName.CONVERSION_GRAPH and not any(
        flow.kind.value in {"conversion", "serialization"} for flow in report.flow_edges
    ):
        return "conversion-graph requires recorded conversion or serialization evidence"
    if name is ProjectionName.DICTIONARY_FLOW and not report.flow_edges:
        return "dictionary-flow requires recorded resolved flow evidence"
    if name is ProjectionName.LEXICAL_CALL_GRAPH and not report.function_profiles:
        return "lexical-call-graph requires recorded function-profile evidence"
    if name is ProjectionName.DEPENDENCIES and not report.modules:
        return "dependencies requires recorded module evidence"
    return None


def write_static_graphs(
    connection: sqlite3.Connection, report: AnalysisReport
) -> tuple[StaticGraphSummary, ...]:
    connection.execute(GRAPH_NODE_TABLE_SQL)
    connection.execute(GRAPH_EDGE_TABLE_SQL)
    connection.execute(GRAPH_CLUSTER_TABLE_SQL)
    source = _report_input(report)
    ownership = graph_ownership(report)
    modules, scc_modules, node_modules = _node_rank_maps(report)
    summaries: list[StaticGraphSummary] = []
    for name in GRAPH_PROJECTIONS:
        unavailable_reason = _unavailable_reason(report, name)
        if unavailable_reason is not None:
            summaries.append(
                StaticGraphSummary(
                    name=name,
                    title=name.value.replace("-", " ").title(),
                    source=source,
                    completeness=report.completeness,
                    available=False,
                    unavailable_reason=unavailable_reason,
                    node_count=0,
                    edge_count=0,
                    cluster_count=0,
                    original_node_count=0,
                    original_edge_count=0,
                )
            )
            continue
        projection = build_projection(
            report, name, max_records=100_000, max_nodes=100_000, max_edges=250_000
        )
        if not isinstance(projection, GraphProjection):
            raise ValueError(f"{name.value} did not produce a graph")
        node_languages, edge_languages = _language_memberships(report, name, projection)
        used_owner_modules = {
            module
            for node in projection.nodes
            if (module := ownership.module_for(node.node_id, node.group_id)) is not None
        }
        for node in projection.nodes:
            scc_member, fan_in, fan_out = _rank_for(node, modules, scc_modules, node_modules)
            owner_module = ownership.module_for(node.node_id, node.group_id)
            owner_language = (
                ownership.module_languages[owner_module].value if owner_module is not None else None
            )
            payload = _checked_payload(node.model_dump_json())
            connection.execute(
                "INSERT INTO static_graph_nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name.value,
                    node.node_id,
                    scc_member,
                    fan_in,
                    fan_out,
                    _language_json(node_languages[node.node_id]),
                    owner_module,
                    owner_language,
                    payload,
                ),
            )
        for edge in projection.edges:
            payload = _checked_payload(edge.model_dump_json())
            connection.execute(
                "INSERT INTO static_graph_edges VALUES (?, ?, ?, ?, ?, ?)",
                (
                    name.value,
                    edge.edge_id,
                    edge.source_id,
                    edge.target_id,
                    _language_json(edge_languages[edge.edge_id]),
                    payload,
                ),
            )
        for cluster in projection.clusters:
            connection.execute(
                "INSERT INTO static_graph_clusters VALUES (?, ?, ?)",
                (name.value, cluster.cluster_id, _checked_payload(cluster.model_dump_json())),
            )
        summaries.append(
            StaticGraphSummary(
                name=name,
                title=projection.title,
                source=projection.inputs[0],
                completeness=projection.completeness,
                available=True,
                unavailable_reason=None,
                preparation=projection.selection,
                truncated=projection.truncated,
                truncation_reason=projection.truncation_reason,
                limitations=projection.limitations,
                direction=projection.direction,
                node_count=len(projection.nodes),
                edge_count=len(projection.edges),
                cluster_count=len(projection.clusters),
                original_node_count=projection.original_node_count,
                original_edge_count=projection.original_edge_count,
                owner_modules=tuple(
                    StaticGraphOwner(
                        module=module,
                        language=ownership.module_languages[module],
                    )
                    for module in sorted(used_owner_modules)
                ),
            )
        )
    return tuple(summaries)


def validate_static_graphs(
    connection: sqlite3.Connection, summaries: tuple[StaticGraphSummary, ...]
) -> None:
    expected_schema = {
        "static_graph_nodes": GRAPH_NODE_TABLE_SQL,
        "static_graph_edges": GRAPH_EDGE_TABLE_SQL,
        "static_graph_clusters": GRAPH_CLUSTER_TABLE_SQL,
    }
    for table, sql in expected_schema.items():
        if connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
        ).fetchone() != (sql,):
            raise ValueError("static graph index schema mismatch")
    if tuple(item.name for item in summaries) != GRAPH_PROJECTIONS:
        raise ValueError("static graph summaries must contain canonical names exactly once")
    summary_by_name = {item.name.value: item for item in summaries}
    for table, count_field in (
        ("static_graph_nodes", "node_count"),
        ("static_graph_edges", "edge_count"),
        ("static_graph_clusters", "cluster_count"),
    ):
        actual = dict(connection.execute(f"SELECT name, count(*) FROM {table} GROUP BY name"))
        expected = {
            name: getattr(summary, count_field)
            for name, summary in summary_by_name.items()
            if getattr(summary, count_field)
        }
        if actual != expected:
            raise ValueError("static graph index counts disagree with receipt")
    node_memberships: dict[tuple[str, str], set[SourceLanguage]] = {}
    used_owners: dict[str, set[str]] = {name: set() for name in summary_by_name}
    owner_receipts = {
        name: {item.module: item.language for item in summary.owner_modules}
        for name, summary in summary_by_name.items()
    }
    for (
        name,
        node_id,
        scc_member,
        fan_in,
        fan_out,
        languages,
        owner_module,
        owner_language,
        payload,
    ) in connection.execute(
        "SELECT name, node_id, scc_member, fan_in, fan_out, languages, owner_module, "
        "owner_language, payload "
        "FROM static_graph_nodes"
    ):
        if name not in summary_by_name or scc_member not in (0, 1) or fan_in < 0 or fan_out < 0:
            raise ValueError("invalid static graph node ranking metadata")
        node = GraphNode.model_validate_json(_checked_payload(payload))
        if node.node_id != node_id:
            raise ValueError("static graph node identity mismatch")
        node_memberships[(name, node_id)] = _parse_language_json(languages)
        if (owner_module is None) != (owner_language is None):
            raise ValueError("static graph node owner metadata is incomplete")
        if owner_module is not None:
            if not isinstance(owner_module, str) or not owner_module:
                raise ValueError("invalid static graph node owner module")
            try:
                parsed_owner_language = SourceLanguage(owner_language)
            except (TypeError, ValueError) as error:
                raise ValueError("invalid static graph node owner language") from error
            if owner_receipts[name].get(owner_module) is not parsed_owner_language:
                raise ValueError("static graph node owner metadata disagrees with receipt")
            used_owners[name].add(owner_module)
    for name, receipt in owner_receipts.items():
        if used_owners[name] != set(receipt):
            raise ValueError("static graph owner receipt disagrees with used node owners")
    for name, edge_id, source_id, target_id, languages, payload in connection.execute(
        "SELECT name, edge_id, source_id, target_id, languages, payload FROM static_graph_edges"
    ):
        edge = GraphEdge.model_validate_json(_checked_payload(payload))
        if edge.edge_id != edge_id or edge.source_id != source_id or edge.target_id != target_id:
            raise ValueError("static graph edge identity mismatch")
        known = connection.execute(
            "SELECT count(*) FROM static_graph_nodes WHERE name = ? AND node_id IN (?, ?)",
            (name, source_id, target_id),
        ).fetchone()[0]
        if known != len({source_id, target_id}):
            raise ValueError("static graph edge has a dangling endpoint")
        membership = _parse_language_json(languages)
        source_membership = node_memberships[(name, source_id)]
        target_membership = node_memberships[(name, target_id)]
        if name == ProjectionName.DEPENDENCIES.value:
            union_membership = source_membership | target_membership
            if not membership.issubset(union_membership):
                raise ValueError("static graph edge language membership disagrees with endpoints")
            if not (
                membership.issubset(source_membership) and membership.issubset(target_membership)
            ) and not (membership & source_membership and membership & target_membership):
                raise ValueError("static graph edge language membership disagrees with endpoints")
        elif not membership.issubset(source_membership) or not membership.issubset(
            target_membership
        ):
            raise ValueError("static graph edge language membership disagrees with endpoints")
    for name, cluster_id, payload in connection.execute(
        "SELECT name, cluster_id, payload FROM static_graph_clusters"
    ):
        cluster = GraphCluster.model_validate_json(_checked_payload(payload))
        if cluster.cluster_id != cluster_id:
            raise ValueError("static graph cluster identity mismatch")
        known = connection.execute(
            "SELECT count(*) FROM static_graph_nodes WHERE name = ? AND node_id IN "
            f"({','.join('?' for _ in cluster.node_ids)})",
            (name, *cluster.node_ids),
        ).fetchone()[0]
        if known != len(cluster.node_ids):
            raise ValueError("static graph cluster has an absent node")


def _static_call_source_file_evidence(
    nodes: tuple[GraphNode, ...], edges: tuple[GraphEdge, ...]
) -> dict[str, tuple[str, ...]]:
    call_sites = {node.node_id: node.evidence_ids for node in nodes if node.kind == "call-site"}
    evidence_by_source: dict[str, set[str]] = {}
    for edge in edges:
        if edge.kind != "source-contains-call-site":
            continue
        evidence_by_source.setdefault(edge.source_id, set()).update(
            call_sites.get(edge.target_id, ())
        )
    return {
        source_id: tuple(sorted(identities)) for source_id, identities in evidence_by_source.items()
    }


def _align_static_call_graph_nodes(
    nodes: tuple[GraphNode, ...], edges: tuple[GraphEdge, ...]
) -> tuple[GraphNode, ...]:
    file_evidence = _static_call_source_file_evidence(nodes, edges)
    return tuple(
        (
            node.model_copy(update={"evidence_ids": file_evidence.get(node.node_id, ())})
            if node.kind == "source-file"
            else node
        )
        for node in nodes
    )


def _result_projection_id(summary: StaticGraphSummary, request: StaticGraphRequest) -> str:
    value = json.dumps(
        {
            "source": summary.source.model_dump(mode="json"),
            "name": summary.name.value,
            "preparation": summary.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "projection-" + hashlib.sha256(value.encode()).hexdigest()[:20]


def read_static_graph(
    connection: sqlite3.Connection,
    summaries: tuple[StaticGraphSummary, ...],
    request: StaticGraphRequest,
) -> GraphProjection:
    matching = [item for item in summaries if item.name is request.name]
    if len(matching) != 1:
        raise ValueError("static graph summary is absent or duplicated")
    summary = matching[0]
    if not summary.available:
        raise ValueError(summary.unavailable_reason or "static graph is unavailable")
    all_node_rows = connection.execute(
        "SELECT node_id, scc_member, fan_in, fan_out, languages, owner_module, owner_language "
        "FROM static_graph_nodes "
        "WHERE name = ? ORDER BY node_id",
        (request.name.value,),
    ).fetchall()
    all_edge_rows = connection.execute(
        "SELECT edge_id, source_id, target_id, languages FROM static_graph_edges "
        "WHERE name = ? ORDER BY edge_id",
        (request.name.value,),
    ).fetchall()
    if len(all_node_rows) != summary.node_count or len(all_edge_rows) != summary.edge_count:
        raise ValueError("static graph records disagree with receipt")
    requested_languages = set(request.languages)
    node_rows = [
        row
        for row in all_node_rows
        if not requested_languages or requested_languages & _parse_language_json(row[4])
    ]
    if requested_languages and not node_rows:
        suffix = " (the prepared graph was truncated)" if summary.truncated else ""
        raise StaticGraphSelectionError(
            "no prepared graph records match the selected languages" + suffix
        )
    known_ids = {row[0] for row in node_rows}
    edge_rows = [
        row[:3]
        for row in all_edge_rows
        if (not requested_languages or requested_languages & _parse_language_json(row[3]))
        and row[1] in known_ids
        and row[2] in known_ids
    ]
    unknown = set(request.selected_ids) - known_ids
    if unknown:
        suffix = " (the prepared graph was truncated)" if summary.truncated else ""
        raise StaticGraphSelectionError(
            "selected identities are absent from the prepared graph" + suffix
        )
    selected = set(request.selected_ids)
    if selected:
        neighbor_ids = {
            endpoint
            for edge_id, source_id, target_id in edge_rows
            if source_id in selected or target_id in selected
            for endpoint in (source_id, target_id)
        } | selected
        node_rows = [row for row in node_rows if row[0] in neighbor_ids]
        edge_rows = [row for row in edge_rows if row[1] in neighbor_ids and row[2] in neighbor_ids]
    total_payload_bytes = 0
    node_payloads: dict[str, GraphNode] = {}
    edge_payloads: dict[str, GraphEdge] = {}
    if request.collapse is not ProjectionCollapse.NONE:
        for node_id, *_metadata in node_rows:
            row = connection.execute(
                "SELECT payload FROM static_graph_nodes WHERE name = ? AND node_id = ?",
                (request.name.value, node_id),
            ).fetchone()
            if row is None:
                raise ValueError("focused static graph node is absent")
            payload, total_payload_bytes = _charge_read_payload(row[0], total_payload_bytes)
            node = GraphNode.model_validate_json(payload)
            if node.node_id != node_id:
                raise ValueError("static graph node identity mismatch")
            node_payloads[node_id] = node
        for edge_id, source_id, target_id in edge_rows:
            row = connection.execute(
                "SELECT payload FROM static_graph_edges WHERE name = ? AND edge_id = ?",
                (request.name.value, edge_id),
            ).fetchone()
            if row is None:
                raise ValueError("focused static graph edge is absent")
            payload, total_payload_bytes = _charge_read_payload(row[0], total_payload_bytes)
            edge = GraphEdge.model_validate_json(payload)
            if (edge.edge_id, edge.source_id, edge.target_id) != (edge_id, source_id, target_id):
                raise ValueError("static graph edge identity mismatch")
            edge_payloads[edge_id] = edge
        module_languages = {item.module: item.language for item in summary.owner_modules}
        node_modules = {
            node_id: owner_module
            for node_id, *_rank, owner_module, owner_language in node_rows
            if owner_module is not None
            and module_languages.get(owner_module) is SourceLanguage(owner_language)
        }
        ownership = GraphOwnership(module_languages, node_modules)
        collapsed_nodes, collapsed_edges, node_keys = collapse_graph(
            tuple(node_payloads[node_id] for node_id, *_metadata in node_rows),
            tuple(edge_payloads[edge_id] for edge_id, *_endpoints in edge_rows),
            request.collapse,
            ownership,
        )
        for record in (*collapsed_nodes, *collapsed_edges):
            if len(record.model_dump_json().encode("utf-8")) > _MAX_PAYLOAD_BYTES:
                raise StaticGraphReadLimitError(
                    "generated collapsed graph record exceeds payload bound; narrow the graph focus"
                )
        rank_by_id = {
            node_id: (bool(scc_member), fan_in, fan_out)
            for node_id, scc_member, fan_in, fan_out, *_metadata in node_rows
        }
        node_rows = [
            (
                node.node_id,
                *rank_by_id.get(node.node_id, (False, 0, 0)),
                None,
                None,
                None,
            )
            for node in collapsed_nodes
        ]
        edge_rows = [(edge.edge_id, edge.source_id, edge.target_id) for edge in collapsed_edges]
        node_payloads = {node.node_id: node for node in collapsed_nodes}
        edge_payloads = {edge.edge_id: edge for edge in collapsed_edges}
        selected = {node_keys.get(identity, identity) for identity in selected}
    focused_node_count = len(node_rows)
    focused_edge_count = len(edge_rows)
    retention = retain_graph(
        tuple(
            GraphNodeRank(
                node_id=node_id,
                selected=node_id in selected,
                scc_member=bool(scc_member),
                fan_in=fan_in,
                fan_out=fan_out,
            )
            for node_id, scc_member, fan_in, fan_out, *_metadata in node_rows
        ),
        tuple(GraphEdgeIdentity(*row) for row in edge_rows),
        max_records=request.max_records,
        max_nodes=request.max_nodes,
        max_edges=request.max_edges,
    )
    nodes = []
    for node_id in retention.node_ids:
        if node_id in node_payloads:
            nodes.append(node_payloads[node_id])
            continue
        row = connection.execute(
            "SELECT payload FROM static_graph_nodes WHERE name = ? AND node_id = ?",
            (request.name.value, node_id),
        ).fetchone()
        if row is None:
            raise ValueError("retained static graph node is absent")
        payload, total_payload_bytes = _charge_read_payload(row[0], total_payload_bytes)
        nodes.append(GraphNode.model_validate_json(payload))
    edges = []
    for edge_id in retention.edge_ids:
        if edge_id in edge_payloads:
            edges.append(edge_payloads[edge_id])
            continue
        row = connection.execute(
            "SELECT payload FROM static_graph_edges WHERE name = ? AND edge_id = ?",
            (request.name.value, edge_id),
        ).fetchone()
        if row is None:
            raise ValueError("retained static graph edge is absent")
        payload, total_payload_bytes = _charge_read_payload(row[0], total_payload_bytes)
        edges.append(GraphEdge.model_validate_json(payload))
    retained_ids = set(retention.node_ids)
    clusters = []
    for (payload,) in connection.execute(
        "SELECT payload FROM static_graph_clusters WHERE name = ? ORDER BY cluster_id",
        (request.name.value,),
    ):
        payload, total_payload_bytes = _charge_read_payload(payload, total_payload_bytes)
        cluster = GraphCluster.model_validate_json(payload)
        intersection = tuple(sorted(retained_ids & set(cluster.node_ids)))
        minimum_members = 2 if request.name is ProjectionName.DEPENDENCIES else 1
        if len(intersection) >= minimum_members:
            clusters.append(cluster.model_copy(update={"node_ids": intersection}))
    cluster_ids = {item.cluster_id for item in clusters}
    nodes = [
        item.model_copy(
            update={"group_id": item.group_id if item.group_id in cluster_ids else None}
        )
        for item in nodes
    ]
    omitted_nodes = focused_node_count - len(nodes)
    omitted_edges = focused_edge_count - len(edges)
    display_truncated = summary.truncated or omitted_nodes > 0 or omitted_edges > 0
    limitations = set(summary.limitations)
    if request.collapse is not ProjectionCollapse.NONE:
        limitations.add(COLLAPSE_LIMITATION)
    if display_truncated:
        limitations.add("Prepared or requested graph bounds omitted records.")
    if summary.truncated:
        limitations.add(
            "The prepared graph was partial before request-time selection; its receipt "
            f"retained {summary.node_count} nodes and {summary.edge_count} edges from "
            f"original counts {summary.original_node_count} and {summary.original_edge_count}."
        )
    completeness = summary.completeness
    if display_truncated and completeness is Completeness.COMPLETE:
        completeness = Completeness.PARTIAL
    if request.name is ProjectionName.STATIC_CALL_GRAPH:
        nodes = _align_static_call_graph_nodes(tuple(nodes), tuple(edges))
    selection = ProjectionSelection(
        selected_ids=request.selected_ids,
        languages=request.languages,
        collapse=request.collapse,
        max_records=request.max_records,
        max_nodes=request.max_nodes,
        max_edges=request.max_edges,
    )
    return GraphProjection(
        projection_id=_result_projection_id(summary, request),
        title=summary.title,
        inputs=(summary.source,),
        selection=selection,
        completeness=completeness,
        record_count=len(nodes) + len(edges),
        truncated=display_truncated,
        truncation_reason=(
            "prepared or requested graph bound reached" if display_truncated else None
        ),
        limitations=tuple(sorted(limitations)),
        direction=summary.direction,
        nodes=tuple(nodes),
        edges=tuple(edges),
        clusters=tuple(clusters),
        original_node_count=focused_node_count,
        original_edge_count=focused_edge_count,
        omitted_node_count=omitted_nodes,
        omitted_edge_count=omitted_edges,
    )
