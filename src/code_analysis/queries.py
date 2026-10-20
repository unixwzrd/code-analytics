"""Bounded queries over validated saved reports; this module never reads source trees."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from types import MappingProxyType

from code_analysis.finding_suppression_models import (
    FindingSuppressionAnnotation,
    finding_suppression_annotation,
)
from code_analysis.models import Completeness, Confidence, Evidence
from code_analysis.profile_models import ProfileReport
from code_analysis.query_models import (
    QueryItem,
    QueryItemKind,
    QueryResponse,
    QuerySelector,
    QuerySelectorKind,
)
from code_analysis.report_models import AnalysisReport
from code_analysis.reports.json_report import canonical_json


class QueryBuildError(ValueError):
    """The requested identity or continuation is not present in the saved report."""


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _evidence_for_symbol(
    report: AnalysisReport, module_name: str, qualified_name: str
) -> tuple[Evidence, ...]:
    for module in report.modules:
        if module.name != module_name:
            continue
        for symbol in module.symbols:
            if symbol.qualified_name == qualified_name:
                return (
                    Evidence(
                        span=symbol.span,
                        provider=(
                            "native-ast"
                            if module.language.value == "python"
                            else "javascript-tree-sitter"
                        ),
                        observation=f"declared {symbol.kind} {qualified_name}",
                        confidence=Confidence.EXACT,
                        source_sha256=module.source_sha256,
                        language=module.language,
                    ),
                )
    return ()


def _item(
    *,
    item_id: str,
    kind: QueryItemKind,
    identity: str,
    relationship: str,
    statement: str,
    report: AnalysisReport,
    related_ids: tuple[str, ...] = (),
    evidence: tuple[Evidence, ...] = (),
    confidence: Confidence | None = None,
    limitations: tuple[str, ...] = (),
    finding_suppression: FindingSuppressionAnnotation | None = None,
) -> QueryItem:
    return QueryItem(
        item_id=item_id,
        kind=kind,
        identity=identity,
        relationship=relationship,
        statement=statement,
        related_ids=tuple(sorted(set(related_ids))),
        evidence=evidence,
        confidence=confidence,
        completeness=report.completeness,
        limitations=tuple(sorted(set(limitations))),
        finding_suppression=finding_suppression,
    )


def _static_call_items(report: AnalysisReport) -> dict[str, QueryItem]:
    items: dict[str, QueryItem] = {}
    for call in report.static_call_occurrences:
        related = (f"module:{call.module_name}",)
        if call.enclosing_subject_id is not None:
            related += (call.enclosing_subject_id,)
        items[call.occurrence_id] = _item(
            item_id=call.occurrence_id,
            kind=QueryItemKind.CALL_SITE,
            identity=call.occurrence_id,
            relationship="syntactic-invocation-site",
            statement=(
                f"{call.syntax_kind.value}: {call.callee_spelling or 'dynamic callee'}; "
                f"context {call.evaluation_context.value}; conditional={call.conditional}; "
                f"deferred={call.deferred}; evaluation-unknown={call.evaluation_unknown}"
            ),
            report=report,
            related_ids=related,
            evidence=call.evidence,
            confidence=Confidence.EXACT,
            limitations=(
                *call.limitations,
                "Syntactic evidence does not establish runtime execution or callee binding.",
                "An enclosing subject is a navigation anchor, not a proven runtime caller.",
            ),
        )
    return items


def _entity_items(report: AnalysisReport) -> dict[str, QueryItem]:
    items = _static_call_items(report)
    for module in report.modules:
        item_id = f"module:{module.name}"
        items[item_id] = _item(
            item_id=item_id,
            kind=QueryItemKind.MODULE,
            identity=module.name,
            relationship="module",
            statement=f"{module.path}; fan-in {module.fan_in}; fan-out {module.fan_out}",
            report=report,
        )
        for symbol in module.symbols:
            logical_name = f"{module.name}:{symbol.qualified_name}"
            symbol_id = f"symbol:{logical_name}"
            items[symbol_id] = _item(
                item_id=symbol_id,
                kind=QueryItemKind.SYMBOL,
                identity=logical_name,
                relationship="declared-symbol",
                statement=f"{symbol.kind} in {module.name}",
                report=report,
                related_ids=(item_id,),
                evidence=_evidence_for_symbol(report, module.name, symbol.qualified_name),
                confidence=Confidence.EXACT,
            )
    for subject in report.subjects:
        items[subject.subject_id] = _item(
            item_id=subject.subject_id,
            kind=QueryItemKind.SUBJECT,
            identity=subject.logical_name,
            relationship="analysis-subject",
            statement=f"{subject.kind.value} subject in {subject.module}",
            report=report,
            related_ids=(f"module:{subject.module}",),
            evidence=subject.evidence,
            confidence=subject.evidence[0].confidence,
        )
    for contract in report.contracts:
        related = [contract.defining_subject_id]
        related.extend(boundary.boundary_id for boundary in contract.boundaries)
        items[contract.contract_id] = _item(
            item_id=contract.contract_id,
            kind=QueryItemKind.CONTRACT,
            identity=contract.contract_id,
            relationship="selected-contract",
            statement=f"{contract.kind.value} contract with {len(contract.members)} members",
            report=report,
            related_ids=tuple(related),
            evidence=contract.evidence,
            confidence=contract.evidence[0].confidence,
            limitations=contract.unresolved_behavior,
        )
        for boundary in contract.boundaries:
            items[boundary.boundary_id] = _item(
                item_id=boundary.boundary_id,
                kind=QueryItemKind.BOUNDARY,
                identity=boundary.boundary_id,
                relationship="contract-boundary",
                statement=f"{boundary.boundary_class.value} {boundary.direction.value} boundary",
                report=report,
                related_ids=(contract.contract_id, boundary.site_subject_id),
                evidence=boundary.evidence,
                confidence=boundary.confidence,
                limitations=boundary.limitations,
            )
    for shape in report.dictionary_shapes:
        related = (
            shape.owner_subject_id,
            *shape.producer_subject_ids,
            *shape.consumer_subject_ids,
        )
        items[shape.shape_id] = _item(
            item_id=shape.shape_id,
            kind=QueryItemKind.SHAPE,
            identity=shape.shape_id,
            relationship="selected-shape",
            statement=f"dictionary shape with {len(shape.variants)} variants",
            report=report,
            related_ids=related,
            evidence=shape.evidence,
            confidence=shape.confidence,
            limitations=shape.limitations,
        )
    for observation in report.dictionary_observations:
        related = tuple(
            value
            for value in (observation.subject_id, observation.shape_id, observation.variant_id)
            if value is not None
        )
        items[observation.observation_id] = _item(
            item_id=observation.observation_id,
            kind=QueryItemKind.DICTIONARY_OBSERVATION,
            identity=observation.observation_id,
            relationship="dictionary-observation",
            statement=f"{observation.operation.value} using {observation.key_kind.value} key",
            report=report,
            related_ids=related,
            evidence=observation.evidence,
            confidence=observation.confidence,
        )
    for flow in report.flow_edges:
        items[flow.flow_id] = _item(
            item_id=flow.flow_id,
            kind=QueryItemKind.FLOW,
            identity=flow.flow_id,
            relationship="data-flow",
            statement=f"{flow.kind.value} flow is {flow.resolution.value}",
            report=report,
            related_ids=(flow.source_id, flow.target_id),
            evidence=flow.evidence,
            confidence=flow.confidence,
            limitations=(() if flow.resolution.value == "resolved" else (flow.resolution.value,)),
        )
    for structure in report.data_structures:
        related = (
            structure.owner_subject_id,
            *structure.constructor_subject_ids,
            *structure.producer_subject_ids,
            *structure.reader_subject_ids,
            *structure.writer_subject_ids,
            *structure.mutator_subject_ids,
            *structure.serializer_subject_ids,
            *structure.persistence_boundary_ids,
            *structure.alias_subject_ids,
            *structure.conversion_flow_ids,
        )
        items[structure.structure_id] = _item(
            item_id=structure.structure_id,
            kind=QueryItemKind.STRUCTURE,
            identity=structure.structure_id,
            relationship="selected-structure",
            statement=f"{structure.scope.value} {structure.canonical_type.kind.value} structure",
            report=report,
            related_ids=related,
            evidence=structure.evidence,
            confidence=structure.confidence,
            limitations=structure.unresolved_behavior,
        )
    for validation in report.validation_patterns:
        items[validation.validation_id] = _item(
            item_id=validation.validation_id,
            kind=QueryItemKind.VALIDATION,
            identity=validation.validation_id,
            relationship="validation",
            statement=f"{validation.classification} validation: {validation.rule}",
            report=report,
            related_ids=validation.subject_ids,
            evidence=validation.sites,
            confidence=validation.sites[0].confidence,
            limitations=validation.limitations,
        )
    for candidate in report.candidate_classifications:
        items[candidate.candidate_id] = _item(
            item_id=candidate.candidate_id,
            kind=QueryItemKind.CANDIDATE,
            identity=candidate.candidate_id,
            relationship="representation-candidate",
            statement=candidate.candidate.value,
            report=report,
            related_ids=(candidate.subject_id,),
            evidence=candidate.supporting_evidence,
            confidence=candidate.confidence,
            limitations=candidate.limitations,
        )
    for fragment in report.clone_fragments:
        items[fragment.fragment_id] = _item(
            item_id=fragment.fragment_id,
            kind=QueryItemKind.CLONE_FRAGMENT,
            identity=fragment.fragment_id,
            relationship="clone-fragment",
            statement=(
                f"{fragment.kind.value} fragment with {fragment.nonblank_lines} lines, "
                f"{fragment.token_count} tokens, and {fragment.ast_node_count} AST nodes"
            ),
            report=report,
            related_ids=(fragment.subject_id,),
            evidence=fragment.evidence,
            confidence=Confidence.EXACT,
            limitations=fragment.limitations,
        )
    for cluster in report.clone_clusters:
        items[cluster.cluster_id] = _item(
            item_id=cluster.cluster_id,
            kind=QueryItemKind.CLONE_CLUSTER,
            identity=cluster.cluster_id,
            relationship="selected-clone",
            statement=f"{cluster.kind.value} clone with {len(cluster.fragment_ids)} fragments",
            report=report,
            related_ids=(*cluster.fragment_ids, *cluster.subject_ids, *cluster.related_cluster_ids),
            evidence=cluster.evidence,
            confidence=cluster.confidence,
            limitations=cluster.limitations,
        )
    for profile in report.function_profiles:
        items[profile.profile_id] = _item(
            item_id=profile.profile_id,
            kind=QueryItemKind.FUNCTION_PROFILE,
            identity=profile.profile_id,
            relationship="function-feature-profile",
            statement=f"transparent feature profile for {profile.subject_id}",
            report=report,
            related_ids=(profile.subject_id, *profile.contract_ids),
            evidence=profile.evidence,
            confidence=profile.evidence[0].confidence,
        )
    for commonality in report.function_commonalities:
        items[commonality.commonality_id] = _item(
            item_id=commonality.commonality_id,
            kind=QueryItemKind.FUNCTION_COMMONALITY,
            identity=commonality.commonality_id,
            relationship="function-commonality",
            statement=f"{len(commonality.dimensions)} shared strong dimensions",
            report=report,
            related_ids=(
                commonality.left_subject_id,
                commonality.right_subject_id,
                *commonality.clone_cluster_ids,
            ),
            evidence=commonality.evidence,
            confidence=commonality.confidence,
            limitations=commonality.limitations,
        )
    for resource in report.state_resources:
        items[resource.state_resource_id] = _item(
            item_id=resource.state_resource_id,
            kind=QueryItemKind.STATE_RESOURCE,
            identity=resource.state_resource_id,
            relationship="mutable-state-resource",
            statement=f"{resource.kind.value} state {resource.logical_name}",
            report=report,
            related_ids=(resource.owner_subject_id, *resource.related_structure_ids),
            evidence=(
                *resource.declaration_evidence,
                *resource.read_evidence,
                *resource.write_evidence,
                *resource.cleanup_evidence,
                *resource.guard_evidence,
                *resource.alias_evidence,
            ),
            confidence=resource.confidence,
            limitations=resource.limitations,
        )
    for effect in report.side_effects:
        items[effect.side_effect_id] = _item(
            item_id=effect.side_effect_id,
            kind=QueryItemKind.SIDE_EFFECT,
            identity=effect.side_effect_id,
            relationship="side-effect-observation",
            statement=f"{effect.kind.value}: {effect.target_identity}",
            report=report,
            related_ids=(effect.subject_id,),
            evidence=effect.evidence,
            confidence=effect.confidence,
            limitations=effect.limitations,
        )
    for responsibility in report.responsibility_clusters:
        items[responsibility.responsibility_id] = _item(
            item_id=responsibility.responsibility_id,
            kind=QueryItemKind.RESPONSIBILITY,
            identity=responsibility.responsibility_id,
            relationship="selected-responsibility",
            statement=(
                "responsibility cluster with " f"{len(responsibility.member_subject_ids)} members"
            ),
            report=report,
            related_ids=(
                *responsibility.member_subject_ids,
                *responsibility.clone_cluster_ids,
                *responsibility.state_resource_ids,
                *responsibility.side_effect_ids,
            ),
            evidence=responsibility.evidence,
            confidence=responsibility.confidence,
            limitations=responsibility.limitations,
        )
    for estimate in report.architecture_estimates:
        items[estimate.estimate_id] = _item(
            item_id=estimate.estimate_id,
            kind=QueryItemKind.ARCHITECTURE_ESTIMATE,
            identity=estimate.estimate_id,
            relationship="architecture-estimate",
            statement=(
                f"cross-boundary edges {estimate.before.cross_boundary_edge_count} -> "
                f"{estimate.expected_after.cross_boundary_edge_count}"
            ),
            report=report,
            evidence=estimate.evidence,
            limitations=estimate.limitations,
        )
    for suggestion in report.architecture_contract_suggestions:
        items[suggestion.suggestion_id] = _item(
            item_id=suggestion.suggestion_id,
            kind=QueryItemKind.ARCHITECTURE_CONTRACT,
            identity=suggestion.suggestion_id,
            relationship="architecture-contract-suggestion",
            statement=f"{suggestion.source_layer} may import {suggestion.target_layer}",
            report=report,
            evidence=suggestion.evidence,
            confidence=suggestion.confidence,
            limitations=suggestion.limitations,
        )
    for proposal in report.refactor_proposals:
        related = (
            *((proposal.responsibility_id,) if proposal.responsibility_id is not None else ()),
            *proposal.clone_cluster_ids,
            *proposal.affected_subject_ids,
            *proposal.affected_contract_ids,
            *proposal.affected_shape_ids,
            *proposal.affected_structure_ids,
            *proposal.state_resource_ids,
            *proposal.side_effect_ids,
            *proposal.affected_test_ids,
            *proposal.coverage_observation_ids,
            *proposal.change_coupling_ids,
            proposal.architecture_estimate_id,
        )
        items[proposal.proposal_id] = _item(
            item_id=proposal.proposal_id,
            kind=QueryItemKind.PROPOSAL,
            identity=proposal.proposal_id,
            relationship="selected-proposal",
            statement=f"{proposal.kind.value}: {proposal.state.value}",
            report=report,
            related_ids=related,
            evidence=proposal.evidence,
            confidence=proposal.confidence,
            limitations=proposal.limitations,
        )
    observations = {item.candidate_id: item for item in report.candidate_observations}
    for candidate in report.refactor_candidates:
        observation = observations[candidate.candidate_id]
        items[candidate.candidate_id] = _item(
            item_id=candidate.candidate_id,
            kind=QueryItemKind.REFACTOR_CANDIDATE,
            identity=candidate.candidate_id,
            relationship="refactor-candidate",
            statement=f"{candidate.kind.value}: {observation.state.value}",
            report=report,
            related_ids=(
                *candidate.participating_subject_ids,
                *observation.responsibility_ids,
                *observation.duplicate_ids,
                *observation.proposal_ids,
            ),
            evidence=observation.evidence,
            confidence=Confidence.INFERRED,
            limitations=observation.limitations,
        )
    for module in report.test_modules:
        items[module.test_module_id] = _item(
            item_id=module.test_module_id,
            kind=QueryItemKind.TEST_MODULE,
            identity=module.module,
            relationship="test-module",
            statement=f"{module.framework} test module at {module.path}",
            report=report,
        )
    for test in report.test_cases:
        items[test.test_id] = _item(
            item_id=test.test_id,
            kind=QueryItemKind.TEST_CASE,
            identity=test.qualified_name,
            relationship="test-case",
            statement="qualification test" if test.qualification else "test case",
            report=report,
            related_ids=(test.test_module_id,),
            evidence=test.evidence,
            confidence=Confidence.EXACT,
        )
    for fixture in report.test_fixtures:
        items[fixture.fixture_id] = _item(
            item_id=fixture.fixture_id,
            kind=QueryItemKind.TEST_FIXTURE,
            identity=fixture.qualified_name,
            relationship="test-fixture",
            statement=f"{fixture.scope} fixture",
            report=report,
            related_ids=(fixture.test_module_id, *fixture.dependency_fixture_ids),
            evidence=fixture.evidence,
            confidence=Confidence.EXACT,
        )
    for relationship in report.test_relationships:
        related = tuple(
            value
            for value in (
                relationship.source_test_id,
                relationship.target_subject_id,
                relationship.target_fixture_id,
            )
            if value is not None
        )
        items[relationship.relationship_id] = _item(
            item_id=relationship.relationship_id,
            kind=QueryItemKind.TEST_RELATIONSHIP,
            identity=relationship.relationship_id,
            relationship=relationship.kind.value,
            statement="resolved" if relationship.resolved else "unresolved",
            report=report,
            related_ids=related,
            evidence=relationship.evidence,
            confidence=Confidence.RESOLVED if relationship.resolved else Confidence.INFERRED,
            limitations=relationship.limitations,
        )
    for artifact in report.coverage_artifacts:
        items[artifact.coverage_id] = _item(
            item_id=artifact.coverage_id,
            kind=QueryItemKind.COVERAGE_ARTIFACT,
            identity=artifact.coverage_id,
            relationship="coverage-artifact",
            statement=f"{artifact.binding.value} coverage artifact",
            report=report,
            limitations=artifact.limitations,
        )
    for coverage_file in report.coverage_files:
        items[coverage_file.coverage_file_id] = _item(
            item_id=coverage_file.coverage_file_id,
            kind=QueryItemKind.COVERAGE_FILE,
            identity=coverage_file.path,
            relationship="coverage-file",
            statement=(
                f"{len(coverage_file.covered_lines)} covered and "
                f"{len(coverage_file.missing_lines)} missing lines"
            ),
            report=report,
            related_ids=(coverage_file.coverage_id,),
            confidence=Confidence.RUNTIME_OBSERVED,
            limitations=coverage_file.limitations,
        )
    for observation in report.coverage_observations:
        items[observation.coverage_observation_id] = _item(
            item_id=observation.coverage_observation_id,
            kind=QueryItemKind.COVERAGE_OBSERVATION,
            identity=observation.coverage_observation_id,
            relationship="symbol-coverage",
            statement=f"{len(observation.covered_lines)} covered lines",
            report=report,
            related_ids=(observation.coverage_id, observation.subject_id),
            confidence=Confidence.RUNTIME_OBSERVED,
        )
    for commit in report.git_commits:
        items[commit.commit_id] = _item(
            item_id=commit.commit_id,
            kind=QueryItemKind.GIT_COMMIT,
            identity=commit.commit_id,
            relationship="git-commit",
            statement=f"{commit.category.value} change on {commit.authored_date}",
            report=report,
            limitations=commit.limitations,
        )
    for change in report.git_path_changes:
        items[change.path_change_id] = _item(
            item_id=change.path_change_id,
            kind=QueryItemKind.GIT_PATH_CHANGE,
            identity=change.path,
            relationship="git-path-change",
            statement=f"{change.kind.value} in {change.commit_id}",
            report=report,
            related_ids=(change.commit_id,),
            limitations=change.limitations,
        )
    for change in report.git_symbol_changes:
        items[change.symbol_change_id] = _item(
            item_id=change.symbol_change_id,
            kind=QueryItemKind.GIT_SYMBOL_CHANGE,
            identity=change.subject_id,
            relationship="git-symbol-change",
            statement=f"{change.kind.value} symbol change",
            report=report,
            related_ids=(change.commit_id, change.path_change_id, change.subject_id),
            limitations=change.limitations,
        )
    for coupling in report.change_couplings:
        items[coupling.coupling_id] = _item(
            item_id=coupling.coupling_id,
            kind=QueryItemKind.CHANGE_COUPLING,
            identity=coupling.coupling_id,
            relationship="change-coupling",
            statement=f"{coupling.shared_changes} shared changes",
            report=report,
            related_ids=(coupling.left_path, coupling.right_path, *coupling.commit_ids),
        )
    for generation in report.generation_changes:
        items[generation.generation_change_id] = _item(
            item_id=generation.generation_change_id,
            kind=QueryItemKind.GENERATION_CHANGE,
            identity=generation.generation_change_id,
            relationship="generation-change",
            statement=(
                f"{len(generation.added_paths)} added, {len(generation.changed_paths)} changed, "
                f"{len(generation.removed_paths)} removed paths"
            ),
            report=report,
            limitations=generation.limitations,
        )
    for finding in report.findings:
        related = (
            *finding.subject_ids,
            *finding.affected_contract_ids,
            *finding.affected_shape_ids,
            *finding.affected_structure_ids,
            *finding.affected_clone_cluster_ids,
            *finding.affected_proposal_ids,
        )
        items[finding.finding_id] = _item(
            item_id=finding.finding_id,
            kind=QueryItemKind.FINDING,
            identity=finding.finding_id,
            relationship="selected-finding",
            statement=f"{finding.severity} {finding.rule_id}: {finding.title}",
            report=report,
            related_ids=related,
            evidence=finding.evidence,
            confidence=finding.confidence,
            limitations=finding.limitations,
            finding_suppression=finding_suppression_annotation(finding.suppression),
        )
    for unresolved in report.unresolved_edges:
        related = (unresolved.source_id,)
        items[unresolved.unresolved_id] = _item(
            item_id=unresolved.unresolved_id,
            kind=QueryItemKind.UNRESOLVED,
            identity=unresolved.unresolved_id,
            relationship="unresolved-evidence",
            statement=f"{unresolved.operation}: {unresolved.reason}",
            report=report,
            related_ids=related,
            evidence=unresolved.evidence,
            limitations=(unresolved.reason,),
        )
    for duplicate in report.duplicate_functions:
        items[duplicate.duplicate_id] = _item(
            item_id=duplicate.duplicate_id,
            kind=QueryItemKind.DUPLICATE_FUNCTION,
            identity=duplicate.duplicate_id,
            relationship="duplicate-function",
            statement=(
                f"{duplicate.matched_statements} matched statements; "
                f"canonical owner {duplicate.canonical_owner.state.value}"
            ),
            report=report,
            related_ids=(
                duplicate.left_subject_id,
                duplicate.right_subject_id,
                *duplicate.clone_cluster_ids,
                *duplicate.behavior_difference_ids,
            ),
            evidence=duplicate.evidence,
            confidence=Confidence.RESOLVED,
            limitations=duplicate.limitations,
        )
    return items


def _dependency_path(report: AnalysisReport, source: str, target: str) -> list[str]:
    modules = {module.name for module in report.modules}
    missing = sorted({source, target} - modules)
    if missing:
        raise QueryBuildError("unknown module identity: " + ", ".join(missing))
    adjacency: dict[str, list[str]] = {name: [] for name in modules}
    for edge in report.import_edges:
        if edge.source in modules and edge.target in modules:
            adjacency[edge.source].append(edge.target)
    for neighbors in adjacency.values():
        neighbors.sort()
    queue: deque[tuple[str, list[str]]] = deque([(source, [source])])
    visited = {source}
    while queue:
        current, path = queue.popleft()
        if current == target:
            return path
        for neighbor in adjacency[current]:
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, [*path, neighbor]))
    raise QueryBuildError(f"no dependency path from {source} to {target}")


@dataclass(frozen=True, slots=True)
class PreparedQueries:
    """Report-bound preparation reused during derivative assembly, never persisted."""

    report: AnalysisReport
    entities: Mapping[str, QueryItem]
    report_sha256: str

    @classmethod
    def prepare(cls, report: AnalysisReport) -> PreparedQueries:
        return cls(
            report,
            MappingProxyType(_entity_items(report)),
            hashlib.sha256(canonical_json(report).encode()).hexdigest(),
        )

    def build(
        self, selector: QuerySelector, *, limit: int = 50, after: str | None = None
    ) -> QueryResponse:
        return _build_prepared_query(self, selector, limit=limit, after=after)


def build_query(
    report: AnalysisReport,
    selector: QuerySelector,
    *,
    limit: int = 50,
    after: str | None = None,
) -> QueryResponse:
    return PreparedQueries.prepare(report).build(selector, limit=limit, after=after)


def _build_prepared_query(
    prepared: PreparedQueries,
    selector: QuerySelector,
    *,
    limit: int = 50,
    after: str | None = None,
) -> QueryResponse:
    if not 1 <= limit <= 500:
        raise QueryBuildError("query limit must be between 1 and 500")
    report = prepared.report
    entities = dict(prepared.entities)
    selected_ids: set[str]
    if selector.kind is QuerySelectorKind.DEPENDENCY_PATH:
        assert selector.target_identity is not None
        path = _dependency_path(report, selector.identity, selector.target_identity)
        selected_ids = {f"module:{name}" for name in path}
        for source, target in pairwise(path):
            edges = sorted(
                (
                    edge
                    for edge in report.import_edges
                    if edge.source == source and edge.target == target
                ),
                key=lambda edge: edge.kind,
            )
            edge = edges[0]
            edge_id = f"import:{edge.source}->{edge.target}:{edge.kind}"
            entities[edge_id] = _item(
                item_id=edge_id,
                kind=QueryItemKind.IMPORT_EDGE,
                identity=edge_id,
                relationship="dependency-path-edge",
                statement=f"{edge.kind} import from {edge.source} to {edge.target}",
                report=report,
                related_ids=(f"module:{edge.source}", f"module:{edge.target}"),
                confidence=Confidence.RESOLVED,
            )
            selected_ids.add(edge_id)
    elif selector.kind is QuerySelectorKind.SYMBOL:
        exact_symbol_ids = {
            item_id
            for item_id, item in entities.items()
            if item.kind is QueryItemKind.SYMBOL and item.identity == selector.identity
        }
        if not exact_symbol_ids and ":" not in selector.identity:
            exact_symbol_ids = {
                item_id
                for item_id, item in entities.items()
                if item.kind is QueryItemKind.SYMBOL
                and item.identity.rsplit(":", maxsplit=1)[-1] == selector.identity
            }
        matching_subjects = {
            subject.subject_id
            for subject in report.subjects
            if subject.logical_name == selector.identity
        }
        matches = exact_symbol_ids | matching_subjects
        if not matches:
            raise QueryBuildError(f"unknown symbol identity: {selector.identity}")
        if len(matches) > 1 and ":" not in selector.identity:
            raise QueryBuildError(
                f"ambiguous symbol identity: {selector.identity}; use MODULE:QUALIFIED_NAME"
            )
        selected_ids = matches
    else:
        if selector.identity not in entities:
            raise QueryBuildError(f"unknown {selector.kind.value} identity: {selector.identity}")
        if (
            selector.kind is QuerySelectorKind.CALL_SITE
            and entities[selector.identity].kind is not QueryItemKind.CALL_SITE
        ):
            raise QueryBuildError(f"not a call-site identity: {selector.identity}")
        selected_ids = {selector.identity}

    neighborhood = set(selected_ids)
    for item_id, item in entities.items():
        if item_id in selected_ids or selected_ids.intersection(item.related_ids):
            neighborhood.add(item_id)
            neighborhood.update(value for value in item.related_ids if value in entities)
    ordered = [entities[item_id] for item_id in sorted(neighborhood)]
    if after is not None:
        positions = {item.item_id: index for index, item in enumerate(ordered)}
        if after not in positions:
            raise QueryBuildError("continuation identity is not in this query result")
        ordered = ordered[positions[after] + 1 :]
    truncated = len(ordered) > limit
    page = tuple(ordered[:limit])
    if not page:
        raise QueryBuildError("query continuation has no remaining records")
    report_sha = prepared.report_sha256
    selector_json = json.dumps(
        selector.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    query_id = _stable_id("query", report_sha, selector_json, str(limit), after or "")
    limitations = set(report.limitations)
    if report.completeness is not Completeness.COMPLETE:
        limitations.add("source report is not complete")
    return QueryResponse(
        query_id=query_id,
        source_report_sha256=report_sha,
        source_run_identity_sha256=report.run_identity_sha256,
        selector=selector,
        limit=limit,
        after_identity=after,
        returned_count=len(page),
        next_identity=page[-1].item_id if truncated else None,
        truncated=truncated,
        source_completeness=report.completeness,
        items=page,
        limitations=tuple(sorted(limitations)),
    )


def render_query_json(response: QueryResponse, *, compact: bool = False) -> str:
    payload = response.model_dump(mode="json")
    if compact:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def render_query_markdown(response: QueryResponse) -> str:
    target = response.selector.identity
    if response.selector.target_identity is not None:
        target = f"{target} -> {response.selector.target_identity}"
    lines = [
        "# Code Analysis Query",
        "",
        f"- Selector: `{response.selector.kind.value}` `{target}`",
        f"- Source report: `{response.source_report_sha256}`",
        f"- Completeness: `{response.source_completeness.value}`",
        f"- Returned: {response.returned_count}",
        f"- Truncated: {'yes' if response.truncated else 'no'}",
        "",
        "## Evidence neighborhood",
        "",
    ]
    for item in response.items:
        lines.extend(
            (
                f"### `{item.item_id}`",
                "",
                f"{item.relationship}: {item.statement}",
                "",
            )
        )
        if item.finding_suppression is not None:
            suppression = item.finding_suppression
            lines.append(f"Suppression: `{suppression.status}`.")
            if suppression.status != "not-suppressed":
                lines.extend(
                    (
                        f"- Evaluated on: `{suppression.evaluated_on}`",
                        f"- Expires on: `{suppression.expires_on}`",
                        f"- Rationale: {suppression.rationale}",
                        "",
                    )
                )
            else:
                lines.append("")
    if response.next_identity is not None:
        lines.extend((f"Continue after `{response.next_identity}`.", ""))
    if response.limitations:
        lines.extend(("## Limitations", ""))
        lines.extend(f"- {value}" for value in response.limitations)
        lines.append("")
    return "\n".join(lines)


def build_profile_query(
    report: ProfileReport,
    selector: QuerySelector,
    *,
    limit: int = 50,
) -> QueryResponse:
    if selector.kind not in {QuerySelectorKind.PROFILE_FRAME, QuerySelectorKind.RUNTIME_EDGE}:
        raise QueryBuildError("profile reports require profile-frame or runtime-edge selectors")
    if not 1 <= limit <= 500:
        raise QueryBuildError("query limit must be between 1 and 500")
    items: dict[str, QueryItem] = {}
    for frame in report.frames:
        items[frame.frame_id] = QueryItem(
            item_id=frame.frame_id,
            kind=QueryItemKind.PROFILE_FRAME,
            identity=f"{frame.path}:{frame.line}:{frame.function}",
            relationship="profile-frame",
            statement=(
                f"{frame.total_calls} calls, {frame.total_seconds:.9f}s total, "
                f"{frame.cumulative_seconds:.9f}s cumulative"
            ),
            related_ids=tuple(
                sorted(
                    {
                        edge.runtime_edge_id
                        for edge in report.runtime_edges
                        if frame.frame_id in {edge.caller_frame_id, edge.callee_frame_id}
                    }
                )
            ),
            confidence=Confidence.RUNTIME_OBSERVED,
            completeness=report.completeness,
        )
    for edge in report.runtime_edges:
        items[edge.runtime_edge_id] = QueryItem(
            item_id=edge.runtime_edge_id,
            kind=QueryItemKind.RUNTIME_EDGE,
            identity=edge.runtime_edge_id,
            relationship="runtime-call",
            statement=f"{edge.total_calls} observed calls",
            related_ids=tuple(sorted((edge.callee_frame_id, edge.caller_frame_id))),
            confidence=Confidence.RUNTIME_OBSERVED,
            completeness=report.completeness,
        )
    if selector.identity not in items:
        raise QueryBuildError(f"unknown {selector.kind.value} identity: {selector.identity}")
    selected = items[selector.identity]
    neighborhood_ids = {selected.item_id, *selected.related_ids}
    page = tuple(items[item] for item in sorted(neighborhood_ids) if item in items)[:limit]
    report_json = json.dumps(report.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    report_sha = hashlib.sha256(report_json.encode()).hexdigest()
    return QueryResponse(
        query_id=_stable_id("query", report_sha, selector.kind.value, selector.identity),
        source_report_sha256=report_sha,
        source_run_identity_sha256=report.profile_identity_sha256,
        selector=selector,
        limit=limit,
        returned_count=len(page),
        next_identity=page[-1].item_id if len(neighborhood_ids) > limit else None,
        truncated=len(neighborhood_ids) > limit,
        source_completeness=report.completeness,
        items=page,
        limitations=report.limitations,
    )
