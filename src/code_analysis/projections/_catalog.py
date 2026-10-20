"""Projection names, selection policies, and typed registry records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from code_analysis.projection_models import Projection, ProjectionSelection
from code_analysis.report_models import AnalysisReport


class ProjectionName(StrEnum):
    SIGNAL_QUALITY = "signal-quality"
    COMMONALITY_ADMISSION = "commonality-admission"
    PREVALENT_FEATURES = "prevalent-features"
    PROPOSAL_ADMISSION = "proposal-admission"
    PROPOSAL_DECISIONS = "proposal-decisions"
    PROPOSAL_GATES = "proposal-gates"
    REFACTOR_SHORTLIST = "refactor-shortlist"
    HEALTH = "health"
    MODULES = "modules"
    SYMBOLS = "symbols"
    SYMBOL_GRAPH = "symbol-graph"
    LEXICAL_CALL_GRAPH = "lexical-call-graph"
    STATIC_CALL_SITES = "static-call-sites"
    STATIC_CALL_GRAPH = "static-call-graph"
    FINDINGS = "findings"
    SUBJECT_FINDINGS = "subject-findings"
    HIERARCHY = "hierarchy"
    COMPLEXITY = "complexity"
    DEPENDENCIES = "dependencies"
    DEPENDENCY_NEIGHBORHOOD = "dependency-neighborhood"
    SCCS = "sccs"
    CYCLES = "cycles"
    CYCLE_LIST = "cycle-list"
    CONTRACTS = "contracts"
    CONTRACT_BOUNDARIES = "contract-boundaries"
    CONTRACT_BOUNDARY_GRAPH = "contract-boundary-graph"
    SHAPE_VARIANTS = "shape-variants"
    DICTIONARY_FLOW = "dictionary-flow"
    VALIDATION_DIVERGENCE = "validation-divergence"
    OWNERSHIP_ALIAS_FLOW = "ownership-alias-flow"
    STRUCTURE_LIFECYCLE = "structure-lifecycle"
    STRUCTURE_LIFECYCLE_GRAPH = "structure-lifecycle-graph"
    CONVERSION_CHURN = "conversion-churn"
    CONVERSION_GRAPH = "conversion-graph"
    REPRESENTATION_CANDIDATES = "representation-candidates"
    REVIEW_PACKET = "review-packet"
    UNRESOLVED_COVERAGE = "unresolved-coverage"
    CLONE_CLUSTERS = "clone-clusters"
    CLONE_GRAPH = "clone-graph"
    FUNCTION_COMMONALITY = "function-commonality"
    MODULE_RESPONSIBILITIES = "module-responsibilities"
    STATE_SIDE_EFFECTS = "state-side-effects"
    STATE_SIDE_EFFECT_GRAPH = "state-side-effect-graph"
    EXTRACTION_CANDIDATES = "extraction-candidates"
    MODULE_COMBINATION_CANDIDATES = "module-combination-candidates"
    ARCHITECTURE_ESTIMATES = "architecture-estimates"
    ARCHITECTURE_CONTRACT_SUGGESTIONS = "architecture-contract-suggestions"
    REFACTOR_PACKET = "refactor-packet"
    TEST_IMPACT = "test-impact"
    TEST_IMPACT_GRAPH = "test-impact-graph"
    GIT_CHURN = "git-churn"
    CHANGE_COUPLING = "change-coupling"
    CHANGE_COUPLING_GRAPH = "change-coupling-graph"
    GENERATION_CHANGES = "generation-changes"
    COVERAGE_FILES = "coverage-files"
    COVERAGE_MATRIX = "coverage-matrix"
    UNCOVERED_PROPOSAL_SUBJECTS = "uncovered-proposal-subjects"
    DUPLICATE_FUNCTIONS = "duplicate-functions"
    CLONE_MEMBERSHIP = "clone-membership"
    BEHAVIOR_DIFFERENCES = "behavior-differences"
    DUPLICATE_OWNERSHIP = "duplicate-ownership"
    DUPLICATE_CALLERS_TESTS = "duplicate-callers-tests"
    DIVERGENCE_REASONS = "divergence-reasons"
    CANONICAL_OWNER = "canonical-owner"
    JAVASCRIPT_REFACTOR_PACKET = "javascript-refactor-packet"
    CANDIDATE_WORKLIST = "candidate-worklist"
    ABSTRACTION_COST = "abstraction-cost"
    EXISTING_OWNER_ALTERNATIVES = "existing-owner-alternatives"
    CANDIDATE_BEHAVIORAL_DIFFERENCES = "candidate-behavioral-differences"
    CROSS_MODULE_DUPLICATION = "cross-module-duplication"
    INTERNAL_CONTRACT_RISKS = "internal-contract-risks"
    PRODUCER_CONSUMER_SCHEMA_DRIFT = "producer-consumer-schema-drift"
    FRONTEND_BACKEND_INTERFACE_MISMATCHES = "frontend-backend-interface-mismatches"
    CANDIDATE_QUALIFICATION = "candidate-qualification"
    OWNERSHIP_CONTRACT_FLOW = "ownership-contract-flow"
    CANDIDATE_ARCHITECTURE_CHANGES = "candidate-architecture-changes"


class ProjectionBuildError(ValueError):
    """A requested report projection cannot be constructed."""


class SelectionPolicy(StrEnum):
    TABLE = "table"
    SPARSE_MATRIX = "sparse-matrix"
    GRAPH = "graph"
    IDENTITY_FOCUSED_GRAPH = "identity-focused-graph"
    DEPENDENCY_NEIGHBORHOOD = "dependency-neighborhood"
    BOUNDED_REVIEW = "bounded-review"
    SUBJECT_FINDINGS = "subject-findings"
    CANDIDATE_WORKLIST = "candidate-worklist"


ProjectionBuilder = Callable[[AnalysisReport, ProjectionSelection], Projection]


@dataclass(frozen=True, slots=True)
class ProjectionSpec:
    name: ProjectionName
    builder: ProjectionBuilder
    selection_policy: SelectionPolicy
