"""Typed exhaustive registry for report-only projection builders."""

from __future__ import annotations

from code_analysis.projections._catalog import (
    ProjectionBuildError,
    ProjectionName,
    ProjectionSpec,
    SelectionPolicy,
)
from code_analysis.projections.actionable import (
    _candidate_architecture_change_projection,
    _candidate_behavioral_difference_projection,
    _candidate_qualification_projection,
    _cross_module_duplication_projection,
    _interface_mismatch_projection,
    _internal_contract_risk_projection,
    _ownership_contract_flow_projection,
    _schema_drift_projection,
)
from code_analysis.projections.candidates import (
    _abstraction_cost_projection,
    _candidate_worklist_projection,
    _owner_alternative_projection,
)
from code_analysis.projections.contracts import (
    _contract_boundary_graph_projection,
    _contract_boundary_projection,
    _contract_inventory_projection,
)
from code_analysis.projections.lexical_calls import _lexical_call_graph_projection
from code_analysis.projections.qualification import (
    _change_coupling_graph_projection,
    _change_coupling_projection,
    _coverage_file_projection,
    _coverage_matrix_projection,
    _generation_change_projection,
    _git_churn_projection,
    _proposal_coverage_projection,
    _test_impact_graph_projection,
    _test_impact_projection,
)
from code_analysis.projections.refactoring import (
    _architecture_contract_projection,
    _architecture_estimate_projection,
    _behavior_difference_projection,
    _canonical_owner_projection,
    _clone_cluster_projection,
    _clone_graph_projection,
    _clone_membership_projection,
    _divergence_reason_projection,
    _duplicate_callers_tests_projection,
    _duplicate_function_projection,
    _duplicate_ownership_projection,
    _extraction_candidate_projection,
    _function_commonality_projection,
    _javascript_refactor_packet_projection,
    _module_combination_candidate_projection,
    _refactor_packet_projection,
    _responsibility_projection,
    _state_side_effect_graph_projection,
    _state_side_effect_projection,
)
from code_analysis.projections.signal_quality import (
    _commonality_admission_projection,
    _prevalent_feature_projection,
    _proposal_admission_projection,
    _proposal_decision_projection,
    _proposal_gate_projection,
    _refactor_shortlist_projection,
    _signal_quality_projection,
)
from code_analysis.projections.static import (
    _complexity_projection,
    _cycle_list_projection,
    _cycle_projection,
    _dependency_neighborhood_projection,
    _dependency_projection,
    _finding_projection,
    _health_projection,
    _hierarchy_projection,
    _module_projection,
    _scc_projection,
    _subject_finding_projection,
    _symbol_projection,
)
from code_analysis.projections.static_calls import (
    _static_call_graph_projection,
    _static_call_sites_projection,
)
from code_analysis.projections.structure_graphs import (
    _conversion_graph_projection,
    _structure_lifecycle_graph_projection,
)
from code_analysis.projections.structures import (
    _conversion_churn_projection,
    _dictionary_flow_projection,
    _ownership_alias_flow_projection,
    _representation_candidate_projection,
    _review_packet_projection,
    _shape_variant_projection,
    _structure_lifecycle_projection,
    _unresolved_coverage_projection,
    _validation_divergence_projection,
)
from code_analysis.projections.symbol_graph import _symbol_graph_projection

_ENTRIES = (
    ProjectionSpec(
        ProjectionName.CANDIDATE_BEHAVIORAL_DIFFERENCES,
        _candidate_behavioral_difference_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.CROSS_MODULE_DUPLICATION,
        _cross_module_duplication_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.INTERNAL_CONTRACT_RISKS,
        _internal_contract_risk_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.PRODUCER_CONSUMER_SCHEMA_DRIFT,
        _schema_drift_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.FRONTEND_BACKEND_INTERFACE_MISMATCHES,
        _interface_mismatch_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.CANDIDATE_QUALIFICATION,
        _candidate_qualification_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.OWNERSHIP_CONTRACT_FLOW,
        _ownership_contract_flow_projection,
        SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
    ),
    ProjectionSpec(
        ProjectionName.CANDIDATE_ARCHITECTURE_CHANGES,
        _candidate_architecture_change_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.CANDIDATE_WORKLIST,
        _candidate_worklist_projection,
        SelectionPolicy.CANDIDATE_WORKLIST,
    ),
    ProjectionSpec(
        ProjectionName.ABSTRACTION_COST,
        _abstraction_cost_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.EXISTING_OWNER_ALTERNATIVES,
        _owner_alternative_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.SIGNAL_QUALITY, _signal_quality_projection, SelectionPolicy.TABLE
    ),
    ProjectionSpec(
        ProjectionName.COMMONALITY_ADMISSION,
        _commonality_admission_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(
        ProjectionName.PREVALENT_FEATURES,
        _prevalent_feature_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(
        ProjectionName.PROPOSAL_ADMISSION,
        _proposal_admission_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(
        ProjectionName.PROPOSAL_DECISIONS,
        _proposal_decision_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(
        ProjectionName.PROPOSAL_GATES,
        _proposal_gate_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.REFACTOR_SHORTLIST,
        _refactor_shortlist_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(ProjectionName.HEALTH, _health_projection, SelectionPolicy.TABLE),
    ProjectionSpec(ProjectionName.MODULES, _module_projection, SelectionPolicy.TABLE),
    ProjectionSpec(ProjectionName.SYMBOLS, _symbol_projection, SelectionPolicy.TABLE),
    ProjectionSpec(
        ProjectionName.SYMBOL_GRAPH,
        _symbol_graph_projection,
        SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
    ),
    ProjectionSpec(
        ProjectionName.LEXICAL_CALL_GRAPH,
        _lexical_call_graph_projection,
        SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
    ),
    ProjectionSpec(
        ProjectionName.STATIC_CALL_SITES,
        _static_call_sites_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.STATIC_CALL_GRAPH,
        _static_call_graph_projection,
        SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
    ),
    ProjectionSpec(ProjectionName.FINDINGS, _finding_projection, SelectionPolicy.TABLE),
    ProjectionSpec(
        ProjectionName.SUBJECT_FINDINGS,
        _subject_finding_projection,
        SelectionPolicy.SUBJECT_FINDINGS,
    ),
    ProjectionSpec(ProjectionName.HIERARCHY, _hierarchy_projection, SelectionPolicy.TABLE),
    ProjectionSpec(ProjectionName.COMPLEXITY, _complexity_projection, SelectionPolicy.TABLE),
    ProjectionSpec(
        ProjectionName.DEPENDENCIES,
        _dependency_projection,
        SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
    ),
    ProjectionSpec(
        ProjectionName.DEPENDENCY_NEIGHBORHOOD,
        _dependency_neighborhood_projection,
        SelectionPolicy.DEPENDENCY_NEIGHBORHOOD,
    ),
    ProjectionSpec(ProjectionName.SCCS, _scc_projection, SelectionPolicy.TABLE),
    ProjectionSpec(ProjectionName.CYCLES, _cycle_projection, SelectionPolicy.GRAPH),
    ProjectionSpec(ProjectionName.CYCLE_LIST, _cycle_list_projection, SelectionPolicy.TABLE),
    ProjectionSpec(
        ProjectionName.CONTRACTS,
        _contract_inventory_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.CONTRACT_BOUNDARIES,
        _contract_boundary_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.CONTRACT_BOUNDARY_GRAPH,
        _contract_boundary_graph_projection,
        SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
    ),
    ProjectionSpec(ProjectionName.SHAPE_VARIANTS, _shape_variant_projection, SelectionPolicy.TABLE),
    ProjectionSpec(
        ProjectionName.DICTIONARY_FLOW,
        _dictionary_flow_projection,
        SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
    ),
    ProjectionSpec(
        ProjectionName.VALIDATION_DIVERGENCE,
        _validation_divergence_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(
        ProjectionName.OWNERSHIP_ALIAS_FLOW,
        _ownership_alias_flow_projection,
        SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
    ),
    ProjectionSpec(
        ProjectionName.STRUCTURE_LIFECYCLE,
        _structure_lifecycle_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(
        ProjectionName.STRUCTURE_LIFECYCLE_GRAPH,
        _structure_lifecycle_graph_projection,
        SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
    ),
    ProjectionSpec(
        ProjectionName.CONVERSION_CHURN, _conversion_churn_projection, SelectionPolicy.TABLE
    ),
    ProjectionSpec(
        ProjectionName.CONVERSION_GRAPH,
        _conversion_graph_projection,
        SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
    ),
    ProjectionSpec(
        ProjectionName.REPRESENTATION_CANDIDATES,
        _representation_candidate_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(
        ProjectionName.REVIEW_PACKET,
        _review_packet_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.UNRESOLVED_COVERAGE,
        _unresolved_coverage_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(ProjectionName.CLONE_CLUSTERS, _clone_cluster_projection, SelectionPolicy.TABLE),
    ProjectionSpec(
        ProjectionName.CLONE_GRAPH, _clone_graph_projection, SelectionPolicy.IDENTITY_FOCUSED_GRAPH
    ),
    ProjectionSpec(
        ProjectionName.FUNCTION_COMMONALITY,
        _function_commonality_projection,
        SelectionPolicy.SPARSE_MATRIX,
    ),
    ProjectionSpec(
        ProjectionName.MODULE_RESPONSIBILITIES,
        _responsibility_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(
        ProjectionName.STATE_SIDE_EFFECTS, _state_side_effect_projection, SelectionPolicy.TABLE
    ),
    ProjectionSpec(
        ProjectionName.STATE_SIDE_EFFECT_GRAPH,
        _state_side_effect_graph_projection,
        SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
    ),
    ProjectionSpec(
        ProjectionName.EXTRACTION_CANDIDATES,
        _extraction_candidate_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(
        ProjectionName.MODULE_COMBINATION_CANDIDATES,
        _module_combination_candidate_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(
        ProjectionName.ARCHITECTURE_ESTIMATES,
        _architecture_estimate_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(
        ProjectionName.ARCHITECTURE_CONTRACT_SUGGESTIONS,
        _architecture_contract_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(
        ProjectionName.REFACTOR_PACKET,
        _refactor_packet_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(ProjectionName.TEST_IMPACT, _test_impact_projection, SelectionPolicy.TABLE),
    ProjectionSpec(
        ProjectionName.TEST_IMPACT_GRAPH,
        _test_impact_graph_projection,
        SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
    ),
    ProjectionSpec(ProjectionName.GIT_CHURN, _git_churn_projection, SelectionPolicy.TABLE),
    ProjectionSpec(
        ProjectionName.CHANGE_COUPLING, _change_coupling_projection, SelectionPolicy.SPARSE_MATRIX
    ),
    ProjectionSpec(
        ProjectionName.CHANGE_COUPLING_GRAPH,
        _change_coupling_graph_projection,
        SelectionPolicy.IDENTITY_FOCUSED_GRAPH,
    ),
    ProjectionSpec(
        ProjectionName.GENERATION_CHANGES, _generation_change_projection, SelectionPolicy.TABLE
    ),
    ProjectionSpec(ProjectionName.COVERAGE_FILES, _coverage_file_projection, SelectionPolicy.TABLE),
    ProjectionSpec(
        ProjectionName.COVERAGE_MATRIX, _coverage_matrix_projection, SelectionPolicy.SPARSE_MATRIX
    ),
    ProjectionSpec(
        ProjectionName.UNCOVERED_PROPOSAL_SUBJECTS,
        _proposal_coverage_projection,
        SelectionPolicy.TABLE,
    ),
    ProjectionSpec(
        ProjectionName.DUPLICATE_FUNCTIONS,
        _duplicate_function_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.CLONE_MEMBERSHIP,
        _clone_membership_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.BEHAVIOR_DIFFERENCES,
        _behavior_difference_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.DUPLICATE_OWNERSHIP,
        _duplicate_ownership_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.DUPLICATE_CALLERS_TESTS,
        _duplicate_callers_tests_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.DIVERGENCE_REASONS,
        _divergence_reason_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
    ProjectionSpec(
        ProjectionName.CANONICAL_OWNER, _canonical_owner_projection, SelectionPolicy.BOUNDED_REVIEW
    ),
    ProjectionSpec(
        ProjectionName.JAVASCRIPT_REFACTOR_PACKET,
        _javascript_refactor_packet_projection,
        SelectionPolicy.BOUNDED_REVIEW,
    ),
)


def _build_registry() -> dict[ProjectionName, ProjectionSpec]:
    names = tuple(item.name for item in _ENTRIES)
    if len(names) != len(set(names)):
        raise RuntimeError("projection registry contains duplicate names")
    missing = set(ProjectionName) - set(names)
    extra = set(names) - set(ProjectionName)
    if missing or extra:
        raise RuntimeError(
            "projection registry mismatch: "
            f"missing={sorted(item.value for item in missing)}, "
            f"extra={sorted(item.value for item in extra)}"
        )
    return {item.name: item for item in _ENTRIES}


PROJECTION_REGISTRY = _build_registry()


def projection_spec(name: ProjectionName) -> ProjectionSpec:
    try:
        return PROJECTION_REGISTRY[name]
    except KeyError as exc:
        raise ProjectionBuildError(f"projection is not registered: {name}") from exc
