"""Authoritative report contracts for static and data-structure analysis."""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import (
    Field,
    JsonValue,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from code_analysis.finding_suppression_models import FindingSuppression
from code_analysis.finding_suppressions import (
    FindingSuppressionEvaluation,
    validate_finding_suppressions,
)
from code_analysis.models import (
    CommandIdentity,
    Completeness,
    ComplexityObservation,
    Confidence,
    ConfigurationIdentity,
    Diagnostic,
    EnvironmentProvenance,
    Evidence,
    ImportCycle,
    ImportEdge,
    JavaScriptSyntaxMode,
    Module,
    ModuleSystem,
    ParserProvenance,
    ProviderResult,
    SourceLanguage,
    SourceParseState,
    StaticSummaryCounts,
    StrictModel,
    StrongComponent,
    TargetIdentity,
)
from code_analysis.static_call_models import StaticCallOccurrence, StaticCallSourceCoverage
from code_analysis.static_call_validation import validate_static_call_occurrences

SCHEMA_NAME: Final = "code-analysis-report"
SCHEMA_VERSION: Final = "1.0.0"


def _require_sorted_unique(label: str, values: Sequence[str] | Sequence[int]) -> None:
    if list(values) != sorted(values) or len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique and sorted")


class ContractKind(StrEnum):
    PYDANTIC_MODEL = "pydantic-model"
    DATACLASS = "dataclass"
    TYPED_DICT = "typed-dict"
    NAMED_TUPLE = "named-tuple"
    PROTOCOL = "protocol"
    ENUM = "enum"
    LITERAL = "literal"
    DICTIONARY = "dictionary"
    MAPPING = "mapping"
    UNTYPED_CONTAINER = "untyped-container"
    CUSTOM_COLLECTION = "custom-collection"


class BoundaryClass(StrEnum):
    PERSISTED_DOCUMENT = "persisted-document"
    HTTP_REQUEST = "http-request"
    HTTP_RESPONSE = "http-response"
    CLI_RENDER_OUTPUT = "cli-render-output"
    EXTERNAL_OPAQUE_PAYLOAD = "external-opaque-payload"
    INTERNAL_MUTABLE_INDEX = "internal-mutable-index"
    INTERNAL_RECORD = "internal-record"
    CONFIGURATION_MERGE = "configuration-merge"
    HASH_FINGERPRINT_INPUT = "hash-fingerprint-input"
    TEST_QUALIFICATION_EVIDENCE = "test-qualification-evidence"
    THIRD_PARTY_PAYLOAD = "third-party-payload"
    CROSS_SUBSYSTEM_INTERNAL = "cross-subsystem-internal"
    MODULE_LOCAL = "module-local"
    UNCLASSIFIED_INTERNAL = "unclassified-internal"


class BoundaryDirection(StrEnum):
    DEFINITION = "definition"
    PRODUCER = "producer"
    CONSUMER = "consumer"
    BIDIRECTIONAL = "bidirectional"


class InterfaceTransport(StrEnum):
    HTTP = "http"
    SSE = "sse"
    WEBSOCKET = "websocket"


class InterfaceRole(StrEnum):
    PRODUCER = "producer"
    CONSUMER = "consumer"


class InterfaceMismatchKind(StrEnum):
    METHOD = "method"
    ROUTE = "route"
    REQUEST_FIELD = "request-field"
    RESPONSE_FIELD = "response-field"
    MISSING_NULL = "missing-null"
    STATUS_CODE = "status-code"
    ERROR_CODE = "error-code"
    EVENT_NAME = "event-name"
    PAYLOAD_VARIANT = "payload-variant"
    RETRY = "retry"
    LIFECYCLE = "lifecycle"


class RequirednessKind(StrEnum):
    ALWAYS = "always"
    OPTIONAL = "optional"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


class DefaultKind(StrEnum):
    ABSENT = "absent"
    EXPLICIT_NULL = "explicit-null"
    JSON_LITERAL = "json-literal"
    FACTORY = "factory"
    EXPRESSION = "expression"
    UNRESOLVED = "unresolved"


class DictionaryKeyKind(StrEnum):
    LITERAL_STRING = "literal-string"
    LITERAL_NON_STRING = "literal-non-string"
    DYNAMIC = "dynamic"
    UNRESOLVED = "unresolved"


class DictionaryOperationKind(StrEnum):
    LITERAL = "literal"
    FACTORY = "factory"
    COMPREHENSION = "comprehension"
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    GET = "get"
    MEMBERSHIP = "membership"
    TRUTHINESS = "truthiness"
    COERCION = "coercion"
    UPDATE = "update"
    CLEAR = "clear"
    POP = "pop"
    SET_DEFAULT = "setdefault"
    MERGE = "merge"
    ITERATION = "iteration"
    UNPACK = "unpack"
    ARGUMENT = "argument"
    RETURN = "return"
    VALIDATION = "validation"


class MutationTiming(StrEnum):
    NOT_MUTATION = "not-mutation"
    BEFORE_VALIDATION = "before-validation"
    AFTER_VALIDATION = "after-validation"
    UNKNOWN = "unknown"


class DictionaryPredicate(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    TRUTHY = "truthy"
    FALSY = "falsy"


class SubjectKind(StrEnum):
    MODULE = "module"
    SYMBOL = "symbol"
    BOUNDARY = "boundary"
    EXTERNAL = "external"


class SourceFileRecord(StrictModel):
    source_file_id: StrictStr = Field(pattern=r"^source-file-[0-9a-f]{20}$")
    logical_path: StrictStr = Field(min_length=1)
    logical_root: StrictStr = Field(min_length=1)
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    language: SourceLanguage
    parse_state: SourceParseState
    syntax_mode: JavaScriptSyntaxMode | None = None
    module_system: ModuleSystem | None = None
    module_name: StrictStr = Field(min_length=1)
    physical_loc: StrictInt = Field(ge=0)
    nonblank_loc: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_modes(self) -> Self:
        if self.nonblank_loc > self.physical_loc:
            raise ValueError("source nonblank LOC cannot exceed physical LOC")
        if self.language is not SourceLanguage.JAVASCRIPT and (
            self.syntax_mode is not None or self.module_system is not None
        ):
            raise ValueError("non-JavaScript source files cannot carry JavaScript modes")
        return self


class AdapterCapabilityKind(StrEnum):
    PARSING_PROVENANCE = "parsing-provenance"
    SOURCE_SPANS = "source-spans"
    MODULE_IDENTITY = "module-identity"
    SYMBOLS = "symbols"
    DEPENDENCIES = "dependencies"
    CALLS = "calls"
    CONTROL_FLOW = "control-flow"
    DATA_FLOW = "data-flow"
    EXCEPTIONS = "exceptions"
    SIDE_EFFECTS = "side-effects"
    CONTRACTS_SHAPES = "contracts-shapes"
    CLONE_NORMALIZATION = "clone-normalization"
    TEST_DISCOVERY = "test-discovery"
    COVERAGE_BINDING = "coverage-binding"


class AdapterCapabilityState(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


class AdapterCapabilityRecord(StrictModel):
    capability: AdapterCapabilityKind
    state: AdapterCapabilityState
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_capability(self) -> Self:
        _require_sorted_unique("adapter capability limitations", self.limitations)
        if self.state is not AdapterCapabilityState.SUPPORTED and not self.limitations:
            raise ValueError("non-supported adapter capability requires a limitation")
        return self


class SectionAssemblyFailure(StrictModel):
    failure_id: StrictStr = Field(pattern=r"^assembly-failure-[0-9a-f]{20}$")
    section: StrictStr = Field(min_length=1)
    record_kind: StrictStr = Field(min_length=1)
    language: SourceLanguage
    affected_ids: tuple[StrictStr, ...] = ()
    field_paths: tuple[StrictStr, ...] = Field(min_length=1)
    diagnostic_code: StrictStr = Field(min_length=1)

    @model_validator(mode="after")
    def validate_failure(self) -> Self:
        _require_sorted_unique("assembly failure affected IDs", self.affected_ids)
        _require_sorted_unique("assembly failure field paths", self.field_paths)
        return self


class LanguageFrontendCoverage(StrictModel):
    language: SourceLanguage
    frontend_id: StrictStr = Field(min_length=1)
    state: SectionState
    discovered: StrictInt = Field(ge=0)
    complete: StrictInt = Field(ge=0)
    partial: StrictInt = Field(ge=0)
    failed: StrictInt = Field(ge=0)
    unavailable: StrictInt = Field(ge=0)
    diagnostic_codes: tuple[StrictStr, ...] = ()
    capabilities: tuple[AdapterCapabilityRecord, ...] = Field(min_length=len(AdapterCapabilityKind))
    assembly_failures: tuple[SectionAssemblyFailure, ...] = ()

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.discovered != self.complete + self.partial + self.failed + self.unavailable:
            raise ValueError("language front-end counts must reconcile")
        _require_sorted_unique("language front-end diagnostic codes", self.diagnostic_codes)
        capability_values = tuple(item.capability.value for item in self.capabilities)
        if capability_values != tuple(item.value for item in AdapterCapabilityKind):
            raise ValueError(
                "language front-end capabilities must contain every capability in order"
            )
        failure_ids = tuple(item.failure_id for item in self.assembly_failures)
        _require_sorted_unique("language front-end assembly failure IDs", failure_ids)
        if any(item.language is not self.language for item in self.assembly_failures):
            raise ValueError("language front-end assembly failures must match coverage language")
        return self


class ModuleExportRecord(StrictModel):
    export_id: StrictStr = Field(pattern=r"^module-export-[0-9a-f]{20}$")
    module: StrictStr = Field(min_length=1)
    source_file_id: StrictStr = Field(pattern=r"^source-file-[0-9a-f]{20}$")
    name: StrictStr = Field(min_length=1)
    kind: Literal["default", "named", "re-export", "commonjs"]
    local_name: StrictStr | None = None
    source_specifier: StrictStr | None = None
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class BehaviorDifferenceKind(StrEnum):
    CONSTANT = "constant"
    DEFAULT = "default"
    BRANCH = "branch"
    CALL = "call"
    EXCEPTION = "exception"
    STATE = "state"
    SIDE_EFFECT = "side-effect"


class DivergenceReason(StrEnum):
    CONTRACT = "contract"
    STATE_OWNERSHIP = "state-ownership"
    SIDE_EFFECT = "side-effect"
    EXCEPTION = "exception"
    CALL_DIRECTION = "call-direction"
    UNRESOLVED = "unresolved"


class CanonicalOwnerState(StrEnum):
    SUGGESTED = "suggested"
    UNRESOLVED = "unresolved"


class BehaviorDifference(StrictModel):
    difference_id: StrictStr = Field(pattern=r"^behavior-difference-[0-9a-f]{20}$")
    kind: BehaviorDifferenceKind
    left_identity: StrictStr = Field(min_length=1)
    right_identity: StrictStr = Field(min_length=1)
    statement_index: StrictInt | None = Field(default=None, ge=0)
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class CanonicalOwnerSuggestion(StrictModel):
    state: CanonicalOwnerState
    subject_id: StrictStr | None = None
    reason: StrictStr = Field(min_length=1)

    @model_validator(mode="after")
    def validate_subject(self) -> Self:
        if (self.state is CanonicalOwnerState.SUGGESTED) != (self.subject_id is not None):
            raise ValueError("a suggested canonical owner requires exactly one subject")
        return self


class DuplicateFunctionPair(StrictModel):
    duplicate_id: StrictStr = Field(pattern=r"^duplicate-[0-9a-f]{20}$")
    language: SourceLanguage
    left_subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    right_subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    clone_cluster_ids: tuple[StrictStr, ...] = ()
    matched_statements: StrictInt = Field(ge=1)
    left_statement_total: StrictInt = Field(ge=1)
    right_statement_total: StrictInt = Field(ge=1)
    insertions: StrictInt = Field(ge=0)
    deletions: StrictInt = Field(ge=0)
    substitutions: StrictInt = Field(ge=0)
    behavior_difference_ids: tuple[StrictStr, ...] = ()
    divergence_reasons: tuple[DivergenceReason, ...] = ()
    canonical_owner: CanonicalOwnerSuggestion
    evidence: tuple[Evidence, ...] = Field(min_length=2)
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        if self.left_subject_id >= self.right_subject_id:
            raise ValueError("duplicate subject pair must use canonical ascending order")
        for label, values in (
            ("duplicate clone cluster IDs", self.clone_cluster_ids),
            ("duplicate behavior difference IDs", self.behavior_difference_ids),
            ("duplicate limitations", self.limitations),
        ):
            _require_sorted_unique(label, values)
        divergence = tuple(item.value for item in self.divergence_reasons)
        _require_sorted_unique("duplicate divergence reasons", divergence)
        if self.matched_statements > min(self.left_statement_total, self.right_statement_total):
            raise ValueError("matched statements cannot exceed either participant total")
        return self


class TypeKind(StrEnum):
    SCALAR = "scalar"
    LITERAL = "literal"
    UNION = "union"
    OPTIONAL = "optional"
    CONTAINER = "container"
    MAPPING = "mapping"
    NAMED = "named"
    RECURSIVE = "recursive"
    UNKNOWN = "unknown"


class UnknownFieldPolicy(StrEnum):
    FORBID = "forbid"
    IGNORE = "ignore"
    ALLOW = "allow"
    OBSERVED = "observed"
    UNKNOWN = "unknown"


class FlowKind(StrEnum):
    PRODUCER = "producer"
    CONSUMER = "consumer"
    CALL = "call"
    RETURN = "return"
    ARGUMENT = "argument"
    SERIALIZATION = "serialization"
    DESERIALIZATION = "deserialization"
    CONVERSION = "conversion"
    MUTATION = "mutation"
    VALIDATION = "validation"


class ResolutionState(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    TRUNCATED = "truncated"


class StructureScope(StrEnum):
    LOCAL = "local"
    INSTANCE = "instance"
    CLASS = "class"
    MODULE = "module"
    THREAD = "thread"
    PROCESS = "process"
    REQUEST = "request"
    PERSISTENT = "persistent"
    UNKNOWN = "unknown"


class CandidateKind(StrEnum):
    PYDANTIC_MODEL = "pydantic-model"
    DATACLASS = "dataclass"
    TYPED_DICT = "typed-dict"
    PROTOCOL = "protocol"
    UNTYPED_COMPATIBILITY = "intentionally-untyped-compatibility-payload"
    LOCAL_DICTIONARY = "local-implementation-dictionary"


class CloneKind(StrEnum):
    EXACT_TEXT = "exact-text"
    NORMALIZED_TOKEN = "normalized-token"
    NORMALIZED_AST = "normalized-ast"
    STATEMENT_SEQUENCE = "statement-sequence"
    CONTROL_CALL_SEQUENCE = "control-call-sequence"
    VALIDATION_IDIOM = "validation-idiom"
    SERIALIZATION_IDIOM = "serialization-idiom"


class CommonalityDimensionKind(StrEnum):
    CALLEES = "callees"
    SIGNATURES = "signatures"
    ATTRIBUTES_GLOBALS = "attributes-globals"
    EXCEPTIONS = "exceptions"
    SIDE_EFFECTS = "side-effects"
    CONTROL_FLOW = "control-flow"
    DICTIONARY_SCHEMA = "dictionary-schema"
    CONTRACTS = "contracts"


class CandidateRejectionReason(StrEnum):
    INSUFFICIENT_BILATERAL_COVERAGE = "insufficient-bilateral-coverage"
    INSUFFICIENT_OWNERSHIP_DIMENSIONS = "insufficient-ownership-dimensions"


class CandidateRejectionExample(StrictModel):
    example_id: StrictStr = Field(pattern=r"^candidate-rejection-[0-9a-f]{20}$")
    reason: CandidateRejectionReason
    left_subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    right_subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    shared_ownership_dimensions: tuple[CommonalityDimensionKind, ...] = ()

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        if self.left_subject_id >= self.right_subject_id:
            raise ValueError("candidate rejection pair must use canonical ascending order")
        values = tuple(item.value for item in self.shared_ownership_dimensions)
        _require_sorted_unique("candidate rejection dimensions", values)
        return self


class PrevalentFeatureExclusion(StrictModel):
    exclusion_id: StrictStr = Field(pattern=r"^prevalent-feature-[0-9a-f]{20}$")
    dimension: CommonalityDimensionKind
    feature_identity: StrictStr = Field(min_length=1)
    subject_count: StrictInt = Field(ge=1)
    subject_threshold: StrictInt = Field(ge=2)

    @model_validator(mode="after")
    def validate_prevalence(self) -> Self:
        if self.subject_count <= self.subject_threshold:
            raise ValueError("prevalent feature must exceed its subject threshold")
        return self


class CommonalityAdmissionFunnel(StrictModel):
    profile_count: StrictInt = Field(ge=0)
    postings_considered: StrictInt = Field(ge=0)
    prevalent_features_suppressed: StrictInt = Field(ge=0)
    unique_candidates: StrictInt = Field(ge=0)
    index_dimension_rejections: StrictInt = Field(ge=0)
    index_dimension_rejections_exact: StrictBool
    candidate_bound_omissions: StrictInt = Field(ge=0)
    candidate_bound_omissions_exact: StrictBool
    coverage_rejections: StrictInt = Field(ge=0)
    dimension_rejections: StrictInt = Field(ge=0)
    clone_admissions: StrictInt = Field(ge=0)
    admitted_pairs: StrictInt = Field(ge=0)
    prevalent_feature_examples: tuple[PrevalentFeatureExclusion, ...] = ()
    rejection_examples: tuple[CandidateRejectionExample, ...] = ()

    @model_validator(mode="after")
    def validate_funnel(self) -> Self:
        if not self.index_dimension_rejections_exact and not self.candidate_bound_omissions:
            raise ValueError("inexact index exclusions require a candidate-generation bound")
        if not self.candidate_bound_omissions_exact and self.candidate_bound_omissions == 0:
            raise ValueError("an inexact omission count requires a positive known lower bound")
        if self.admitted_pairs + self.coverage_rejections + self.dimension_rejections != (
            self.unique_candidates
        ):
            raise ValueError("commonality admission counts must reconcile")
        if self.clone_admissions > self.admitted_pairs:
            raise ValueError("clone admissions cannot exceed admitted pairs")
        for records, field_name in (
            (self.prevalent_feature_examples, "exclusion_id"),
            (self.rejection_examples, "example_id"),
        ):
            identities = tuple(getattr(item, field_name) for item in records)
            _require_sorted_unique(f"commonality {field_name} records", identities)
        return self


class SignalQualitySummary(StrictModel):
    commonality_admission: CommonalityAdmissionFunnel | None = None
    proposal_admission: ProposalAdmissionFunnel | None = None
    proposal_decisions: ProposalDecisionFunnel | None = None


class StateResourceKind(StrEnum):
    MUTABLE_GLOBAL = "mutable-global"
    MUTABLE_CLASS_STATE = "mutable-class-state"
    REGISTRY = "registry"
    CACHE = "cache"
    SINGLETON = "singleton"
    LOCK = "lock"
    OTHER = "other"


class SideEffectKind(StrEnum):
    FILESYSTEM_WRITE = "filesystem-write"
    FILESYSTEM_RENAME = "filesystem-rename"
    FILESYSTEM_DELETE = "filesystem-delete"
    ENVIRONMENT_READ = "environment-read"
    ENVIRONMENT_WRITE = "environment-write"
    SUBPROCESS = "subprocess"
    SIGNAL = "signal"
    THREAD_CREATE = "thread-create"
    PROCESS_CREATE = "process-create"
    NETWORK_CLIENT = "network-client"
    NETWORK_LISTENER = "network-listener"
    STORAGE = "storage"
    DOM_MUTATION = "dom-mutation"
    TIMER = "timer"
    EVENT_LISTENER = "event-listener"
    PROMISE_CHAIN = "promise-chain"


class RefactorProposalKind(StrEnum):
    EXTRACT_RESPONSIBILITY = "extract-responsibility"
    COMBINE_MODULES = "combine-modules"
    ARCHITECTURE_CONTRACT = "architecture-contract"


class RefactorProposalState(StrEnum):
    REVIEWABLE_INCOMPLETE = "reviewable-incomplete"
    BLOCKED_INSUFFICIENT_EVIDENCE = "blocked-insufficient-evidence"
    READY_FOR_MAINTAINER_REVIEW = "ready-for-maintainer-review"


class RefactorCandidateKind(StrEnum):
    RESPONSIBILITY_EXTRACTION = "responsibility-extraction"
    MODULE_COMBINATION = "module-combination"
    DUPLICATE_FUNCTIONS = "duplicate-functions"
    ARCHITECTURE_CONTRACT = "architecture-contract"


class RefactorCandidateState(StrEnum):
    OBSERVED = "observed"
    BLOCKED = "blocked"
    REVIEWABLE_INCOMPLETE = "reviewable-incomplete"
    READY_FOR_MAINTAINER_REVIEW = "ready-for-maintainer-review"


class CandidateQualificationKind(StrEnum):
    DIRECT_TESTS = "direct-tests"
    LIKELY_CALL_PATH_TESTS = "likely-call-path-tests"
    BOUND_COVERAGE = "bound-coverage"
    POSITIVE_CASES = "positive-cases"
    NEGATIVE_ADVERSARIAL_CASES = "negative-adversarial-cases"
    PARITY_FIXTURES = "parity-fixtures"
    ROLLBACK_TESTS = "rollback-tests"
    FAILURE_PATH_TESTS = "failure-path-tests"
    CANONICAL_TEST_DISCOVERY = "canonical-test-discovery"


class CandidateQualificationState(StrEnum):
    EVIDENCED = "evidenced"
    MISSING = "missing"
    UNRESOLVED = "unresolved"


class EstimateRange(StrictModel):
    lower: StrictInt | None = Field(default=None, ge=0)
    likely: StrictInt | None = Field(default=None, ge=0)
    upper: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        values = (self.lower, self.likely, self.upper)
        if any(item is None for item in values):
            if not all(item is None for item in values):
                raise ValueError("estimate range values must be all present or all unresolved")
            return self
        assert self.lower is not None and self.likely is not None and self.upper is not None
        if not self.lower <= self.likely <= self.upper:
            raise ValueError("estimate range requires lower <= likely <= upper")
        return self


class SignedEstimateRange(StrictModel):
    lower: StrictInt | None = None
    likely: StrictInt | None = None
    upper: StrictInt | None = None

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        values = (self.lower, self.likely, self.upper)
        if any(item is None for item in values):
            if not all(item is None for item in values):
                raise ValueError("estimate range values must be all present or all unresolved")
            return self
        assert self.lower is not None and self.likely is not None and self.upper is not None
        if not self.lower <= self.likely <= self.upper:
            raise ValueError("estimate range requires lower <= likely <= upper")
        return self


class OwnerAlternativeClass(StrEnum):
    EXISTING_SEMANTIC_OWNER = "existing-semantic-owner"
    EXISTING_PRIMITIVE = "existing-primitive"
    EXISTING_DOMAIN_MODULE = "existing-domain-module"
    NEW_OWNER = "new-owner"


class AbstractionWarningKind(StrEnum):
    GENERIC_COMMON_MODULE = "generic-common-module"
    FORWARDING_MODULE = "forwarding-module"
    REEXPORT_FACADE = "reexport-facade"
    COMPATIBILITY_SHIM = "compatibility-shim"
    OPTION_HEAVY_HELPER = "option-heavy-helper"
    PARALLEL_CONTRACT_FAMILIES = "parallel-contract-families"
    METRIC_ONLY_ABSTRACTION = "metric-only-abstraction"


class BehavioralEventKind(StrEnum):
    EXCEPTION = "exception"
    EFFECT_BEFORE_FAILURE = "effect-before-failure"
    FILE_READ = "file-read"
    MUTATION = "mutation"
    PUBLICATION = "publication"
    BRANCH_PREDICATE = "branch-predicate"
    DEFAULT_HANDLING = "default-handling"
    MISSING_HANDLING = "missing-handling"
    NULL_HANDLING = "null-handling"
    MALFORMED_HANDLING = "malformed-handling"
    UNKNOWN_HANDLING = "unknown-handling"
    SERIALIZATION = "serialization"
    TEMPORARY_WRITE = "temporary-write"
    FSYNC = "fsync"
    REPLACE = "replace"
    ROLLBACK = "rollback"
    CLEANUP = "cleanup"
    RESOURCE_ACQUIRE = "resource-acquire"
    RESOURCE_RELEASE = "resource-release"
    EXTERNAL_CALL = "external-call"


class BehavioralEvent(StrictModel):
    event_id: StrictStr = Field(pattern=r"^behavior-event-[0-9a-f]{20}$")
    subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    kind: BehavioralEventKind
    identity: StrictStr = Field(min_length=1)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    confidence: Confidence


class BehavioralOrderingEdge(StrictModel):
    ordering_id: StrictStr = Field(pattern=r"^behavior-order-[0-9a-f]{20}$")
    subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    before_event_id: StrictStr = Field(pattern=r"^behavior-event-[0-9a-f]{20}$")
    after_event_id: StrictStr = Field(pattern=r"^behavior-event-[0-9a-f]{20}$")
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    confidence: Confidence

    @model_validator(mode="after")
    def validate_edge(self) -> Self:
        if self.before_event_id == self.after_event_id:
            raise ValueError("behavior ordering edge requires distinct events")
        return self


class BehavioralInvariant(StrictModel):
    invariant_id: StrictStr = Field(pattern=r"^behavior-invariant-[0-9a-f]{20}$")
    candidate_id: StrictStr = Field(pattern=r"^refactor-candidate-[0-9a-f]{20}$")
    kind: BehavioralEventKind
    participating_subject_ids: tuple[StrictStr, ...] = Field(min_length=2)
    event_ids: tuple[StrictStr, ...] = Field(min_length=2)
    evidence: tuple[Evidence, ...] = Field(min_length=2)
    completeness: Completeness

    @model_validator(mode="after")
    def validate_invariant(self) -> Self:
        _require_sorted_unique("behavior invariant subjects", self.participating_subject_ids)
        _require_sorted_unique("behavior invariant events", self.event_ids)
        return self


class CandidateBehaviorDifference(StrictModel):
    difference_id: StrictStr = Field(pattern=r"^candidate-behavior-difference-[0-9a-f]{20}$")
    candidate_id: StrictStr = Field(pattern=r"^refactor-candidate-[0-9a-f]{20}$")
    kind: BehavioralEventKind
    left_subject_id: StrictStr
    right_subject_id: StrictStr
    left_identities: tuple[StrictStr, ...] = ()
    right_identities: tuple[StrictStr, ...] = ()
    blocking: StrictBool
    evidence: tuple[Evidence, ...] = Field(min_length=2)
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_difference(self) -> Self:
        _require_sorted_unique("behavior difference left identities", self.left_identities)
        _require_sorted_unique("behavior difference right identities", self.right_identities)
        _require_sorted_unique("behavior difference limitations", self.limitations)
        if self.left_subject_id == self.right_subject_id:
            raise ValueError("behavior difference requires distinct subjects")
        if self.left_identities == self.right_identities:
            raise ValueError("behavior difference requires distinct observations")
        return self


class OwnerAlternative(StrictModel):
    alternative_id: StrictStr = Field(pattern=r"^owner-alternative-[0-9a-f]{20}$")
    candidate_id: StrictStr = Field(pattern=r"^refactor-candidate-[0-9a-f]{20}$")
    owner_class: OwnerAlternativeClass
    logical_owner: StrictStr = Field(min_length=1)
    existing: StrictBool
    viable: StrictBool
    rejection_reasons: tuple[StrictStr, ...] = ()
    warnings: tuple[AbstractionWarningKind, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_alternative(self) -> Self:
        _require_sorted_unique("owner rejection reasons", self.rejection_reasons)
        _require_sorted_unique("owner warnings", tuple(item.value for item in self.warnings))
        if self.viable and self.rejection_reasons:
            raise ValueError("viable owner alternatives cannot retain rejection reasons")
        if not self.viable and not self.rejection_reasons:
            raise ValueError("rejected owner alternatives require reasons")
        return self


class AbstractionCostEstimate(StrictModel):
    cost_estimate_id: StrictStr = Field(pattern=r"^abstraction-cost-[0-9a-f]{20}$")
    candidate_id: StrictStr = Field(pattern=r"^refactor-candidate-[0-9a-f]{20}$")
    existing_duplicate_loc: StrictInt | None = Field(default=None, ge=0)
    expected_deleted_loc: EstimateRange
    added_owner_loc: EstimateRange
    migration_adapter_loc: EstimateRange
    net_production_loc: SignedEstimateRange
    expected_test_requirement_categories: StrictInt = Field(ge=0)
    new_files: EstimateRange
    new_types: EstimateRange
    new_callbacks: EstimateRange
    new_parameters: EstimateRange
    new_options: EstimateRange
    new_imports: EstimateRange
    graph_edge_change: SignedEstimateRange
    derivation: tuple[StrictStr, ...] = Field(min_length=1)
    confidence: Confidence
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_cost(self) -> Self:
        _require_sorted_unique("cost derivation", self.derivation)
        _require_sorted_unique("cost limitations", self.limitations)
        return self


class RefactorCandidate(StrictModel):
    candidate_id: StrictStr = Field(pattern=r"^refactor-candidate-[0-9a-f]{20}$")
    kind: RefactorCandidateKind
    languages: tuple[SourceLanguage, ...] = Field(min_length=1)
    participating_subject_ids: tuple[StrictStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_identity_inputs(self) -> Self:
        _require_sorted_unique("candidate languages", tuple(item.value for item in self.languages))
        _require_sorted_unique("candidate subjects", self.participating_subject_ids)
        return self


class RefactorCandidateObservation(StrictModel):
    observation_id: StrictStr = Field(pattern=r"^candidate-observation-[0-9a-f]{20}$")
    candidate_id: StrictStr = Field(pattern=r"^refactor-candidate-[0-9a-f]{20}$")
    state: RefactorCandidateState
    run_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    target_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    configuration_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256s: tuple[StrictStr, ...] = Field(min_length=1)
    responsibility_ids: tuple[StrictStr, ...] = ()
    duplicate_ids: tuple[StrictStr, ...] = ()
    proposal_ids: tuple[StrictStr, ...] = ()
    cost_estimate_id: StrictStr | None = Field(
        default=None, pattern=r"^abstraction-cost-[0-9a-f]{20}$"
    )
    owner_alternative_ids: tuple[StrictStr, ...] = ()
    behavioral_event_ids: tuple[StrictStr, ...] = ()
    behavioral_invariant_ids: tuple[StrictStr, ...] = ()
    behavioral_difference_ids: tuple[StrictStr, ...] = ()
    qualification_requirement_ids: tuple[StrictStr, ...] = ()
    failed_gate_count: StrictInt = Field(default=0, ge=0)
    unresolved_gate_count: StrictInt = Field(default=0, ge=0)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    counter_evidence: tuple[Evidence, ...] = ()
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        for label, values in (
            ("candidate source hashes", self.source_sha256s),
            ("candidate responsibilities", self.responsibility_ids),
            ("candidate duplicates", self.duplicate_ids),
            ("candidate proposals", self.proposal_ids),
            ("candidate owner alternatives", self.owner_alternative_ids),
            ("candidate behavioral events", self.behavioral_event_ids),
            ("candidate behavioral invariants", self.behavioral_invariant_ids),
            ("candidate behavioral differences", self.behavioral_difference_ids),
            ("candidate qualification requirements", self.qualification_requirement_ids),
            ("candidate limitations", self.limitations),
        ):
            _require_sorted_unique(label, values)
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.source_sha256s
        ):
            raise ValueError("candidate source hashes must be lowercase SHA-256 values")
        return self


class CandidateQualificationRequirement(StrictModel):
    requirement_id: StrictStr = Field(pattern=r"^candidate-qualification-[0-9a-f]{20}$")
    candidate_id: StrictStr = Field(pattern=r"^refactor-candidate-[0-9a-f]{20}$")
    kind: CandidateQualificationKind
    state: CandidateQualificationState
    test_ids: tuple[StrictStr, ...] = ()
    coverage_observation_ids: tuple[StrictStr, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    reason: StrictStr = Field(min_length=1)
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_requirement(self) -> Self:
        _require_sorted_unique("qualification test IDs", self.test_ids)
        _require_sorted_unique(
            "qualification coverage observation IDs", self.coverage_observation_ids
        )
        _require_sorted_unique("qualification limitations", self.limitations)
        if self.state is CandidateQualificationState.EVIDENCED and not (
            self.test_ids or self.coverage_observation_ids or self.evidence
        ):
            raise ValueError("evidenced qualification requires supporting evidence")
        return self


class ProposalGate(StrEnum):
    OWNERSHIP = "ownership"
    DOMAIN_BOUNDARY = "domain-boundary"
    CONTRACT_COMPATIBILITY = "contract-compatibility"
    MUTABLE_STATE = "mutable-state"
    SIDE_EFFECTS = "side-effects"
    GRAPH_EDGE_REDUCTION = "graph-edge-reduction"
    SCC = "scc"
    CYCLE = "cycle"
    CONFIGURED_LAYER = "configured-layer"
    ABSTRACTION_COST = "abstraction-cost"
    BEHAVIOR_PRESERVATION = "behavior-preservation"
    TEST_IMPACT = "test-impact"
    COVERAGE_BINDING = "coverage-binding"
    RUNTIME_CLAIM = "runtime-claim"


class ProposalGateOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNRESOLVED = "unresolved"
    NOT_APPLICABLE = "not-applicable"


class ProposalGateDecision(StrictModel):
    decision_id: StrictStr = Field(pattern=r"^proposal-gate-[0-9a-f]{20}$")
    gate: ProposalGate
    outcome: ProposalGateOutcome
    blocking: StrictBool
    affected_ids: tuple[StrictStr, ...] = ()
    supporting_catalog_ids: tuple[StrictStr, ...] = ()
    counter_catalog_ids: tuple[StrictStr, ...] = ()
    reason: StrictStr = Field(min_length=1)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        for label, values in (
            ("proposal gate affected IDs", self.affected_ids),
            ("proposal gate supporting evidence IDs", self.supporting_catalog_ids),
            ("proposal gate counter-evidence IDs", self.counter_catalog_ids),
        ):
            _require_sorted_unique(label, values)
        if self.blocking and self.outcome not in {
            ProposalGateOutcome.FAILED,
            ProposalGateOutcome.UNRESOLVED,
        }:
            raise ValueError("only failed or unresolved proposal gates may block")
        return self


class ProposalDecisionFunnel(StrictModel):
    total_proposals: StrictInt = Field(ge=0)
    blocked_proposals: StrictInt = Field(ge=0)
    reviewable_incomplete_proposals: StrictInt = Field(ge=0)
    ready_for_maintainer_review_proposals: StrictInt = Field(ge=0)
    failed_gates: StrictInt = Field(ge=0)
    unresolved_gates: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.total_proposals != (
            self.blocked_proposals
            + self.reviewable_incomplete_proposals
            + self.ready_for_maintainer_review_proposals
        ):
            raise ValueError("proposal decision counts must reconcile")
        return self


class ProposalAdmissionRejectionReason(StrEnum):
    INCOMPLETE_CALL_RESOLUTION_ONLY = "incomplete-call-resolution-only"
    RESPONSIBILITY_COMPONENT_TOO_BROAD = "responsibility-component-too-broad"


class ProposalCandidateKind(StrEnum):
    EXTRACT_RESPONSIBILITY = "extract-responsibility"
    COMBINE_MODULES = "combine-modules"


class ProposalRejectionExample(StrictModel):
    candidate_id: StrictStr = Field(pattern=r"^proposal-candidate-[0-9a-f]{20}$")
    kind: ProposalCandidateKind
    responsibility_id: StrictStr = Field(pattern=r"^responsibility-[0-9a-f]{20}$")
    affected_subject_ids: tuple[StrictStr, ...] = Field(min_length=2)
    affected_modules: tuple[StrictStr, ...] = Field(min_length=1)
    reason: ProposalAdmissionRejectionReason
    evidence: tuple[Evidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        _require_sorted_unique("proposal rejection subject IDs", self.affected_subject_ids)
        _require_sorted_unique("proposal rejection modules", self.affected_modules)
        return self


class ProposalAdmissionFunnel(StrictModel):
    responsibility_clusters_considered: StrictInt = Field(ge=0)
    proposal_candidates_considered: StrictInt = Field(ge=0)
    admitted_candidates: StrictInt = Field(ge=0)
    call_only_rejections: StrictInt = Field(ge=0)
    too_broad_rejections: StrictInt = Field(ge=0)
    proposal_bound_omissions: StrictInt = Field(ge=0)
    retained_proposals: StrictInt = Field(ge=0)
    example_limit: Literal[25] = 25
    rejection_examples: tuple[ProposalRejectionExample, ...] = Field(default=(), max_length=25)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.proposal_candidates_considered != (
            self.admitted_candidates + self.call_only_rejections + self.too_broad_rejections
        ):
            raise ValueError("proposal admission candidate counts must reconcile")
        if self.admitted_candidates != (self.retained_proposals + self.proposal_bound_omissions):
            raise ValueError("proposal admission retained counts must reconcile")
        if len(self.rejection_examples) > self.call_only_rejections:
            raise ValueError("proposal rejection examples exceed rejected candidates")
        identities = tuple(item.candidate_id for item in self.rejection_examples)
        _require_sorted_unique("proposal rejection example IDs", identities)
        return self


class SectionState(StrEnum):
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    COMPLETE = "complete"
    PARTIAL = "partial"
    TRUNCATED = "truncated"
    FAILED = "failed"


class TestRelationshipKind(StrEnum):
    MODULE_IMPORT = "module-import"
    DIRECT_SYMBOL = "direct-symbol"
    FIXTURE_USE = "fixture-use"
    PATCH = "patch"
    QUALIFICATION = "qualification"


class CoverageBindingState(StrEnum):
    BOUND = "bound"
    UNBOUND = "unbound"
    SOURCE_MISMATCH = "source-mismatch"


class ChangeKind(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    RENAMED = "renamed"


class CommitCategory(StrEnum):
    FEATURE = "feature"
    FIX = "fix"
    REFACTOR = "refactor"
    FORMATTING = "formatting"
    REVERT = "revert"
    DOCUMENTATION = "documentation"
    UNKNOWN = "unknown"


class GitPathScope(StrEnum):
    PYTHON = "python"
    REPOSITORY = "repository"


class TypeExpression(StrictModel):
    kind: TypeKind
    name: StrictStr | None = None
    arguments: tuple[TypeExpression, ...] = Field(default=(), max_length=32)
    literal_values: tuple[JsonValue, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_depth(self) -> TypeExpression:
        def depth(value: TypeExpression) -> int:
            return 1 + max((depth(argument) for argument in value.arguments), default=0)

        if depth(self) > 16:
            raise ValueError("type-expression depth exceeds 16")
        return self


class JsonLiteralValue(StrictModel):
    value: JsonValue

    @model_validator(mode="after")
    def validate_finite_json(self) -> Self:
        self.canonical_json()
        return self

    def canonical_json(self) -> str:
        try:
            return json.dumps(
                self.value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("JSON literal must be finite canonical JSON") from exc


class DefaultValue(StrictModel):
    default_id: StrictStr = Field(pattern=r"^default-[0-9a-f]{20}$")
    kind: DefaultKind
    json_literal: JsonLiteralValue | None = None
    detail: StrictStr | None = None
    evidence: tuple[Evidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if self.kind is DefaultKind.JSON_LITERAL:
            if self.json_literal is None or self.detail is not None:
                raise ValueError("JSON-literal default requires only json_literal")
            if self.json_literal.value is None:
                raise ValueError("null default must use explicit-null kind")
        elif self.kind in {DefaultKind.FACTORY, DefaultKind.EXPRESSION, DefaultKind.UNRESOLVED}:
            if not self.detail or self.json_literal is not None:
                raise ValueError(f"{self.kind.value} default requires only detail")
        elif self.json_literal is not None or self.detail is not None:
            raise ValueError(f"{self.kind.value} default cannot contain a payload")
        return self


class FieldRequiredness(StrictModel):
    kind: RequirednessKind
    condition: StrictStr | None = None
    evidence: tuple[Evidence, ...] = ()

    @model_validator(mode="after")
    def validate_condition(self) -> Self:
        if self.kind is RequirednessKind.CONDITIONAL:
            if not self.condition or not self.evidence:
                raise ValueError("conditional requiredness requires a condition and evidence")
        elif self.condition is not None:
            raise ValueError("only conditional requiredness may contain a condition")
        return self


class AnalysisSubject(StrictModel):
    subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    kind: SubjectKind
    logical_name: StrictStr = Field(min_length=1)
    module: StrictStr = Field(min_length=1)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    language: SourceLanguage = SourceLanguage.PYTHON


class ContractBoundary(StrictModel):
    boundary_id: StrictStr = Field(pattern=r"^boundary-[0-9a-f]{20}$")
    boundary_class: BoundaryClass
    direction: BoundaryDirection
    site_subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    confidence: Confidence
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_limitations(self) -> Self:
        if list(self.limitations) != sorted(self.limitations) or len(self.limitations) != len(
            set(self.limitations)
        ):
            raise ValueError("contract boundary limitations must be unique and sorted")
        return self


class InterfacePayloadField(StrictModel):
    name: StrictStr = Field(min_length=1)
    requiredness: RequirednessKind
    allows_null: StrictBool | None
    producer_defaults: tuple[DefaultValue, ...] = ()
    consumer_fallbacks: tuple[DefaultValue, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_defaults(self) -> Self:
        for label, values in (
            ("producer defaults", self.producer_defaults),
            ("consumer fallbacks", self.consumer_fallbacks),
        ):
            identities = [item.default_id for item in values]
            _require_sorted_unique(f"interface field {label}", identities)
        return self


class InterfacePayloadVariant(StrictModel):
    variant_id: StrictStr = Field(pattern=r"^interface-variant-[0-9a-f]{20}$")
    boundary_class: BoundaryClass
    fields: tuple[InterfacePayloadField, ...] = ()
    discriminator_field: StrictStr | None = None
    discriminator_value: StrictStr | None = None
    unknown_field_behavior: StrictStr | None = None
    version_behavior: StrictStr | None = None
    persisted_fields: tuple[StrictStr, ...] = ()
    derived_fields: tuple[StrictStr, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_variant(self) -> Self:
        _require_sorted_unique("interface payload fields", tuple(item.name for item in self.fields))
        _require_sorted_unique("interface persisted fields", self.persisted_fields)
        _require_sorted_unique("interface derived fields", self.derived_fields)
        _require_sorted_unique("interface variant limitations", self.limitations)
        if (self.discriminator_field is None) != (self.discriminator_value is None):
            raise ValueError("interface discriminator requires field and value")
        return self


class InterfaceEndpoint(StrictModel):
    endpoint_id: StrictStr = Field(pattern=r"^interface-endpoint-[0-9a-f]{20}$")
    language: SourceLanguage
    module: StrictStr = Field(min_length=1)
    logical_name: StrictStr = Field(min_length=1)
    role: InterfaceRole
    transport: InterfaceTransport
    method: StrictStr | None = None
    normalized_path: StrictStr = Field(min_length=1)
    payload_variant_ids: tuple[StrictStr, ...] = ()
    status_codes: tuple[StrictInt, ...] = ()
    error_codes: tuple[StrictStr, ...] = ()
    event_names: tuple[StrictStr, ...] = ()
    retry_observations: tuple[StrictStr, ...] = ()
    lifecycle_observations: tuple[StrictStr, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_endpoint(self) -> Self:
        for label, values in (
            ("payload variants", self.payload_variant_ids),
            ("status codes", self.status_codes),
            ("error codes", self.error_codes),
            ("event names", self.event_names),
            ("retry observations", self.retry_observations),
            ("lifecycle observations", self.lifecycle_observations),
            ("limitations", self.limitations),
        ):
            _require_sorted_unique(f"interface endpoint {label}", values)
        if self.transport is InterfaceTransport.HTTP and self.method is None:
            raise ValueError("HTTP interface endpoint requires a method")
        if self.transport is not InterfaceTransport.HTTP and self.method is not None:
            raise ValueError("non-HTTP interface endpoint cannot define a method")
        return self


class InterfaceMismatch(StrictModel):
    mismatch_id: StrictStr = Field(pattern=r"^interface-mismatch-[0-9a-f]{20}$")
    kind: InterfaceMismatchKind
    producer_endpoint_id: StrictStr = Field(pattern=r"^interface-endpoint-[0-9a-f]{20}$")
    consumer_endpoint_id: StrictStr = Field(pattern=r"^interface-endpoint-[0-9a-f]{20}$")
    field_name: StrictStr | None = None
    explanation: StrictStr = Field(min_length=1)
    evidence: tuple[Evidence, ...] = Field(min_length=2)
    confidence: Confidence
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_mismatch(self) -> Self:
        if self.producer_endpoint_id == self.consumer_endpoint_id:
            raise ValueError("interface mismatch requires distinct endpoints")
        _require_sorted_unique("interface mismatch limitations", self.limitations)
        return self


class InterfaceCorrelation(StrictModel):
    correlation_id: StrictStr = Field(pattern=r"^interface-correlation-[0-9a-f]{20}$")
    producer_endpoint_id: StrictStr = Field(pattern=r"^interface-endpoint-[0-9a-f]{20}$")
    consumer_endpoint_id: StrictStr = Field(pattern=r"^interface-endpoint-[0-9a-f]{20}$")
    matched_field_names: tuple[StrictStr, ...] = ()
    mismatch_ids: tuple[StrictStr, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=2)
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_correlation(self) -> Self:
        if self.producer_endpoint_id == self.consumer_endpoint_id:
            raise ValueError("interface correlation requires distinct endpoints")
        _require_sorted_unique("interface matched fields", self.matched_field_names)
        _require_sorted_unique("interface mismatch IDs", self.mismatch_ids)
        _require_sorted_unique("interface correlation limitations", self.limitations)
        return self


class ContractMember(StrictModel):
    name: StrictStr = Field(min_length=1)
    type_expression: TypeExpression
    requiredness: FieldRequiredness
    producer_defaults: tuple[DefaultValue, ...] = ()
    consumer_fallbacks: tuple[DefaultValue, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_defaults(self) -> Self:
        for label, defaults in (
            ("producer defaults", self.producer_defaults),
            ("consumer fallbacks", self.consumer_fallbacks),
        ):
            identities = [item.default_id for item in defaults]
            if identities != sorted(identities) or len(identities) != len(set(identities)):
                raise ValueError(f"contract member {label} must be unique and sorted")
        return self


class ContractDefinition(StrictModel):
    contract_id: StrictStr = Field(pattern=r"^contract-[0-9a-f]{20}$")
    kind: ContractKind
    defining_subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    boundaries: tuple[ContractBoundary, ...] = Field(min_length=1)
    members: tuple[ContractMember, ...] = ()
    serialization_formats: tuple[StrictStr, ...] = ()
    serialization_sites: tuple[Evidence, ...] = ()
    schema_versions: tuple[StrictStr, ...] = ()
    invariants: tuple[StrictStr, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    unresolved_behavior: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_boundaries_and_members(self) -> Self:
        boundary_ids = [boundary.boundary_id for boundary in self.boundaries]
        if not boundary_ids:
            raise ValueError("contract definition requires at least one evidenced boundary")
        if boundary_ids != sorted(boundary_ids) or len(boundary_ids) != len(set(boundary_ids)):
            raise ValueError("contract boundaries must be unique and sorted")
        member_names = [member.name for member in self.members]
        if member_names != sorted(member_names) or len(member_names) != len(set(member_names)):
            raise ValueError("contract members must be unique and sorted")
        for label, values in (
            ("serialization formats", self.serialization_formats),
            ("schema versions", self.schema_versions),
            ("invariants", self.invariants),
            ("unresolved behavior", self.unresolved_behavior),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"contract {label} must be unique and sorted")
        return self


class ShapeField(StrictModel):
    name: StrictStr
    type_expression: TypeExpression
    requiredness_observations: tuple[FieldRequiredness, ...] = Field(min_length=1)
    producer_defaults: tuple[DefaultValue, ...] = ()
    consumer_fallbacks: tuple[DefaultValue, ...] = ()
    coercions: tuple[StrictStr, ...] = ()
    conflicts: tuple[StrictStr, ...] = ()
    mutations: tuple[Evidence, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    confidence: Confidence

    @model_validator(mode="after")
    def validate_defaults_and_lists(self) -> Self:
        requiredness = [
            (
                item.kind.value,
                item.condition or "",
                tuple(
                    (
                        evidence.span.path,
                        evidence.span.start_line,
                        evidence.span.start_column,
                    )
                    for evidence in item.evidence
                ),
            )
            for item in self.requiredness_observations
        ]
        if requiredness != sorted(requiredness) or len(requiredness) != len(set(requiredness)):
            raise ValueError("shape field requiredness observations must be unique and sorted")
        for label, defaults in (
            ("producer defaults", self.producer_defaults),
            ("consumer fallbacks", self.consumer_fallbacks),
        ):
            identities = [item.default_id for item in defaults]
            if identities != sorted(identities) or len(identities) != len(set(identities)):
                raise ValueError(f"shape field {label} must be unique and sorted")
        for label, values in (("coercions", self.coercions), ("conflicts", self.conflicts)):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"shape field {label} must be unique and sorted")
        return self


class ShapeDiscriminator(StrictModel):
    field_name: StrictStr = Field(min_length=1)
    values: tuple[JsonLiteralValue, ...] = Field(min_length=1)
    evidence: tuple[Evidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        canonical = [value.canonical_json() for value in self.values]
        if canonical != sorted(canonical) or len(canonical) != len(set(canonical)):
            raise ValueError("shape discriminator values must be unique and sorted")
        return self


class ShapeLifecycleState(StrictModel):
    state_id: StrictStr = Field(pattern=r"^state-[0-9a-f]{20}$")
    name: StrictStr = Field(min_length=1)
    terminal: StrictBool = False
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class ShapeVariant(StrictModel):
    variant_id: StrictStr = Field(pattern=r"^variant-[0-9a-f]{20}$")
    name: StrictStr = Field(min_length=1)
    discriminator: ShapeDiscriminator | None = None
    lifecycle_state_ids: tuple[StrictStr, ...] = ()
    fields: tuple[ShapeField, ...]
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    confidence: Confidence
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_variant(self) -> Self:
        field_names = [field.name for field in self.fields]
        if field_names != sorted(field_names) or len(field_names) != len(set(field_names)):
            raise ValueError("shape variant fields must be unique and sorted")
        if self.discriminator is not None and self.discriminator.field_name not in set(field_names):
            raise ValueError("shape discriminator must reference a field in its variant")
        for label, values in (
            ("lifecycle state IDs", self.lifecycle_state_ids),
            ("limitations", self.limitations),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"shape variant {label} must be unique and sorted")
        return self


class DictionaryShape(StrictModel):
    shape_id: StrictStr = Field(pattern=r"^shape-[0-9a-f]{20}$")
    owner_subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    lifecycle_states: tuple[ShapeLifecycleState, ...] = ()
    variants: tuple[ShapeVariant, ...] = Field(min_length=1)
    unknown_field_policy: UnknownFieldPolicy
    schema_versions: tuple[StrictStr, ...] = ()
    producer_subject_ids: tuple[StrictStr, ...] = ()
    consumer_subject_ids: tuple[StrictStr, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    confidence: Confidence
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_typed_dictionary_structure(self) -> Self:
        state_ids = [state.state_id for state in self.lifecycle_states]
        if state_ids != sorted(state_ids) or len(state_ids) != len(set(state_ids)):
            raise ValueError("dictionary lifecycle states must be unique and sorted")
        known_states = set(state_ids)
        variant_ids = [variant.variant_id for variant in self.variants]
        if variant_ids != sorted(variant_ids) or len(variant_ids) != len(set(variant_ids)):
            raise ValueError("dictionary shape variants must be unique and sorted")
        if any(not set(variant.lifecycle_state_ids) <= known_states for variant in self.variants):
            raise ValueError("shape variant references an unknown lifecycle state")
        for label, values in (
            ("schema versions", self.schema_versions),
            ("producer subject IDs", self.producer_subject_ids),
            ("consumer subject IDs", self.consumer_subject_ids),
            ("limitations", self.limitations),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"dictionary shape {label} must be unique and sorted")
        return self


class DictionaryObservation(StrictModel):
    observation_id: StrictStr = Field(pattern=r"^dict-observation-[0-9a-f]{20}$")
    subject_id: StrictStr = Field(min_length=1)
    shape_id: StrictStr | None = Field(default=None, pattern=r"^shape-[0-9a-f]{20}$")
    variant_id: StrictStr | None = Field(default=None, pattern=r"^variant-[0-9a-f]{20}$")
    key_kind: DictionaryKeyKind
    key_repr: StrictStr | None = None
    key_type: TypeExpression | None = None
    operation: DictionaryOperationKind
    mutation_timing: MutationTiming
    coercion: StrictStr | None = None
    predicate: DictionaryPredicate | None = None
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    confidence: Confidence

    @model_validator(mode="after")
    def validate_key_and_operation(self) -> Self:
        if self.key_kind in {
            DictionaryKeyKind.LITERAL_STRING,
            DictionaryKeyKind.LITERAL_NON_STRING,
            DictionaryKeyKind.DYNAMIC,
        } and (
            self.key_repr is None
            or (self.key_repr == "" and self.key_kind is not DictionaryKeyKind.LITERAL_STRING)
        ):
            raise ValueError(f"{self.key_kind.value} dictionary key requires key_repr")
        if self.key_kind is DictionaryKeyKind.LITERAL_NON_STRING and self.key_type is None:
            raise ValueError("literal non-string dictionary key requires key_type")
        if self.operation is DictionaryOperationKind.COERCION:
            if not self.coercion:
                raise ValueError("dictionary coercion observation requires coercion detail")
        elif self.coercion is not None:
            raise ValueError("only dictionary coercion observations may contain coercion detail")
        predicate_operations = {
            DictionaryOperationKind.MEMBERSHIP,
            DictionaryOperationKind.TRUTHINESS,
            DictionaryOperationKind.VALIDATION,
        }
        if self.operation in predicate_operations and self.predicate is None:
            raise ValueError(f"{self.operation.value} observation requires a predicate")
        if self.operation not in predicate_operations and self.predicate is not None:
            raise ValueError("only predicate observations may contain predicate detail")
        mutation_operations = {
            DictionaryOperationKind.WRITE,
            DictionaryOperationKind.DELETE,
            DictionaryOperationKind.UPDATE,
            DictionaryOperationKind.CLEAR,
            DictionaryOperationKind.POP,
            DictionaryOperationKind.SET_DEFAULT,
            DictionaryOperationKind.MERGE,
        }
        if (
            self.operation not in mutation_operations
            and self.mutation_timing is not MutationTiming.NOT_MUTATION
        ):
            raise ValueError("non-mutation dictionary operation requires not-mutation timing")
        if (
            self.operation in mutation_operations
            and self.mutation_timing is MutationTiming.NOT_MUTATION
        ):
            raise ValueError("dictionary mutation requires mutation timing")
        if self.variant_id is not None and self.shape_id is None:
            raise ValueError("dictionary variant observation requires shape_id")
        return self


class FlowEdge(StrictModel):
    flow_id: StrictStr = Field(pattern=r"^flow-[0-9a-f]{20}$")
    source_id: StrictStr = Field(min_length=1)
    target_id: StrictStr = Field(min_length=1)
    kind: FlowKind
    resolution: ResolutionState
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    confidence: Confidence


class DataStructureRecord(StrictModel):
    structure_id: StrictStr = Field(pattern=r"^structure-[0-9a-f]{20}$")
    owner_subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    scope: StructureScope
    canonical_type: TypeExpression
    constructor_subject_ids: tuple[StrictStr, ...] = ()
    producer_subject_ids: tuple[StrictStr, ...] = ()
    reader_subject_ids: tuple[StrictStr, ...] = ()
    writer_subject_ids: tuple[StrictStr, ...] = ()
    mutator_subject_ids: tuple[StrictStr, ...] = ()
    serializer_subject_ids: tuple[StrictStr, ...] = ()
    persistence_boundary_ids: tuple[StrictStr, ...] = ()
    alias_subject_ids: tuple[StrictStr, ...] = ()
    conversion_flow_ids: tuple[StrictStr, ...] = ()
    invariants: tuple[StrictStr, ...] = ()
    ordering_assumptions: tuple[StrictStr, ...] = ()
    uniqueness_assumptions: tuple[StrictStr, ...] = ()
    identity_assumptions: tuple[StrictStr, ...] = ()
    key_normalization: tuple[StrictStr, ...] = ()
    concurrency: tuple[StrictStr, ...] = ()
    lifetime: tuple[StrictStr, ...] = ()
    cleanup: tuple[StrictStr, ...] = ()
    indexing: tuple[StrictStr, ...] = ()
    growth_indicators: tuple[StrictStr, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    confidence: Confidence
    unresolved_behavior: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_reference_order(self) -> Self:
        for label, values in (
            ("constructor subject IDs", self.constructor_subject_ids),
            ("producer subject IDs", self.producer_subject_ids),
            ("reader subject IDs", self.reader_subject_ids),
            ("writer subject IDs", self.writer_subject_ids),
            ("mutator subject IDs", self.mutator_subject_ids),
            ("serializer subject IDs", self.serializer_subject_ids),
            ("persistence boundary IDs", self.persistence_boundary_ids),
            ("alias subject IDs", self.alias_subject_ids),
            ("conversion flow IDs", self.conversion_flow_ids),
            ("invariants", self.invariants),
            ("ordering assumptions", self.ordering_assumptions),
            ("uniqueness assumptions", self.uniqueness_assumptions),
            ("identity assumptions", self.identity_assumptions),
            ("key normalization", self.key_normalization),
            ("concurrency", self.concurrency),
            ("lifetime", self.lifetime),
            ("cleanup", self.cleanup),
            ("indexing", self.indexing),
            ("growth indicators", self.growth_indicators),
            ("unresolved behavior", self.unresolved_behavior),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"data structure {label} must be unique and sorted")
        return self


class ValidationPattern(StrictModel):
    validation_id: StrictStr = Field(pattern=r"^validation-[0-9a-f]{20}$")
    classification: Literal["structural", "operational"]
    rule: StrictStr = Field(min_length=1)
    field_identity: StrictStr | None = None
    subject_ids: tuple[StrictStr, ...] = Field(min_length=1)
    sites: tuple[Evidence, ...] = Field(min_length=1)
    divergent_enforcement: StrictBool = False
    fail_open: StrictBool = False
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_reference_order(self) -> Self:
        for label, values in (
            ("subject IDs", self.subject_ids),
            ("limitations", self.limitations),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"validation {label} must be unique and sorted")
        return self


class CandidateClassification(StrictModel):
    candidate_id: StrictStr = Field(pattern=r"^candidate-[0-9a-f]{20}$")
    subject_id: StrictStr = Field(min_length=1)
    candidate: CandidateKind
    supporting_evidence: tuple[Evidence, ...] = Field(min_length=1)
    counter_evidence: tuple[Evidence, ...] = ()
    confidence: Confidence
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_limitations(self) -> Self:
        if list(self.limitations) != sorted(self.limitations) or len(self.limitations) != len(
            set(self.limitations)
        ):
            raise ValueError("candidate limitations must be unique and sorted")
        return self


class CloneFragment(StrictModel):
    fragment_id: StrictStr = Field(pattern=r"^clone-fragment-[0-9a-f]{20}$")
    kind: CloneKind
    subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    fingerprint_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    structural_signature: StrictStr = Field(min_length=1)
    nonblank_lines: StrictInt = Field(ge=0)
    token_count: StrictInt = Field(ge=0)
    ast_node_count: StrictInt = Field(ge=0)
    statement_count: StrictInt = Field(ge=0)
    matched_event_count: StrictInt = Field(default=0, ge=0)
    sequence_coverage: StrictFloat = Field(default=0.0, ge=0.0, le=1.0)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    limitations: tuple[StrictStr, ...] = ()
    language: SourceLanguage = SourceLanguage.PYTHON

    @model_validator(mode="after")
    def validate_limitations(self) -> Self:
        _require_sorted_unique("clone fragment limitations", self.limitations)
        if self.kind is CloneKind.CONTROL_CALL_SEQUENCE:
            if not self.matched_event_count or not self.sequence_coverage:
                raise ValueError("control-call fragment requires matched events and coverage")
        elif self.matched_event_count or self.sequence_coverage:
            raise ValueError("only control-call fragments may contain sequence metrics")
        return self


class CloneCluster(StrictModel):
    cluster_id: StrictStr = Field(pattern=r"^clone-cluster-[0-9a-f]{20}$")
    kind: CloneKind
    fragment_ids: tuple[StrictStr, ...] = Field(min_length=2)
    subject_ids: tuple[StrictStr, ...] = Field(min_length=2)
    related_cluster_ids: tuple[StrictStr, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=2)
    counter_evidence: tuple[Evidence, ...] = ()
    confidence: Confidence
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        for label, values in (
            ("clone fragment IDs", self.fragment_ids),
            ("clone subject IDs", self.subject_ids),
            ("related clone cluster IDs", self.related_cluster_ids),
            ("clone cluster limitations", self.limitations),
        ):
            _require_sorted_unique(label, values)
        return self


class FunctionFeatureProfile(StrictModel):
    profile_id: StrictStr = Field(pattern=r"^function-profile-[0-9a-f]{20}$")
    subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    callees: tuple[StrictStr, ...] = ()
    signature_shapes: tuple[StrictStr, ...] = ()
    attributes_globals: tuple[StrictStr, ...] = ()
    exceptions: tuple[StrictStr, ...] = ()
    side_effects: tuple[StrictStr, ...] = ()
    control_flow: tuple[StrictStr, ...] = ()
    dictionary_schema_operations: tuple[StrictStr, ...] = ()
    contract_ids: tuple[StrictStr, ...] = ()
    weak_semantic_signals: tuple[StrictStr, ...] = ()
    unresolved_call_count: StrictInt = Field(default=0, ge=0)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    limitations: tuple[StrictStr, ...] = ()
    language: SourceLanguage = SourceLanguage.PYTHON

    @model_validator(mode="after")
    def validate_features(self) -> Self:
        for label, values in (
            ("callees", self.callees),
            ("signature shapes", self.signature_shapes),
            ("attributes and globals", self.attributes_globals),
            ("exceptions", self.exceptions),
            ("side effects", self.side_effects),
            ("control flow", self.control_flow),
            ("dictionary schema operations", self.dictionary_schema_operations),
            ("contract IDs", self.contract_ids),
            ("weak semantic signals", self.weak_semantic_signals),
            ("limitations", self.limitations),
        ):
            _require_sorted_unique(f"function profile {label}", values)
        return self


class CommonalityDimension(StrictModel):
    kind: CommonalityDimensionKind
    shared_identities: tuple[StrictStr, ...] = Field(min_length=1)
    left_total: StrictInt = Field(ge=1)
    right_total: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def validate_shared(self) -> Self:
        _require_sorted_unique("commonality shared identities", self.shared_identities)
        if len(self.shared_identities) > min(self.left_total, self.right_total):
            raise ValueError("commonality shared identities exceed a participant total")
        return self


class FunctionCommonality(StrictModel):
    commonality_id: StrictStr = Field(pattern=r"^commonality-[0-9a-f]{20}$")
    left_subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    right_subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    dimensions: tuple[CommonalityDimension, ...] = ()
    clone_cluster_ids: tuple[StrictStr, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=2)
    counter_evidence: tuple[Evidence, ...] = ()
    confidence: Confidence
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        if self.left_subject_id >= self.right_subject_id:
            raise ValueError("commonality subject pair must use canonical ascending order")
        kinds = [item.kind.value for item in self.dimensions]
        if kinds != sorted(kinds) or len(kinds) != len(set(kinds)):
            raise ValueError("commonality dimensions must be unique and sorted")
        _require_sorted_unique("commonality clone cluster IDs", self.clone_cluster_ids)
        _require_sorted_unique("commonality limitations", self.limitations)
        if not self.dimensions and not self.clone_cluster_ids:
            raise ValueError("commonality requires a strong dimension or structural clone")
        return self


class StateResource(StrictModel):
    state_resource_id: StrictStr = Field(pattern=r"^state-resource-[0-9a-f]{20}$")
    kind: StateResourceKind
    scope: StructureScope
    owner_subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    logical_name: StrictStr = Field(min_length=1)
    related_structure_ids: tuple[StrictStr, ...] = ()
    declaration_evidence: tuple[Evidence, ...] = Field(min_length=1)
    read_evidence: tuple[Evidence, ...] = ()
    write_evidence: tuple[Evidence, ...] = ()
    cleanup_evidence: tuple[Evidence, ...] = ()
    guard_evidence: tuple[Evidence, ...] = ()
    alias_evidence: tuple[Evidence, ...] = ()
    confidence: Confidence
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        _require_sorted_unique("state related structure IDs", self.related_structure_ids)
        _require_sorted_unique("state limitations", self.limitations)
        if not any(
            (
                self.declaration_evidence,
                self.read_evidence,
                self.write_evidence,
                self.cleanup_evidence,
                self.guard_evidence,
                self.alias_evidence,
            )
        ):
            raise ValueError("state resource requires at least one evidence class")
        return self


class SideEffectObservation(StrictModel):
    side_effect_id: StrictStr = Field(pattern=r"^side-effect-[0-9a-f]{20}$")
    subject_id: StrictStr = Field(pattern=r"^subject-[0-9a-f]{20}$")
    kind: SideEffectKind
    target_identity: StrictStr = Field(min_length=1)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    confidence: Confidence
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_limitations(self) -> Self:
        _require_sorted_unique("side-effect limitations", self.limitations)
        return self


class ResponsibilityCluster(StrictModel):
    responsibility_id: StrictStr = Field(pattern=r"^responsibility-[0-9a-f]{20}$")
    member_subject_ids: tuple[StrictStr, ...] = Field(min_length=2)
    shared_dimensions: tuple[CommonalityDimensionKind, ...] = Field(min_length=2)
    clone_cluster_ids: tuple[StrictStr, ...] = ()
    state_resource_ids: tuple[StrictStr, ...] = ()
    side_effect_ids: tuple[StrictStr, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=2)
    counter_evidence: tuple[Evidence, ...] = ()
    confidence: Confidence
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        for label, values in (
            ("responsibility member subject IDs", self.member_subject_ids),
            (
                "responsibility shared dimensions",
                tuple(item.value for item in self.shared_dimensions),
            ),
            ("responsibility clone cluster IDs", self.clone_cluster_ids),
            ("responsibility state resource IDs", self.state_resource_ids),
            ("responsibility side-effect IDs", self.side_effect_ids),
            ("responsibility limitations", self.limitations),
        ):
            _require_sorted_unique(label, values)
        return self


class ArchitectureMetrics(StrictModel):
    module_count: StrictInt = Field(ge=0)
    import_edge_count: StrictInt = Field(ge=0)
    cross_boundary_edge_count: StrictInt = Field(ge=0)
    scc_count: StrictInt = Field(ge=0)
    cycle_count: StrictInt = Field(ge=0)
    layer_violation_count: StrictInt = Field(ge=0)
    proposed_module_fan_in: StrictInt = Field(ge=0)
    proposed_module_fan_out: StrictInt = Field(ge=0)


class ArchitectureEstimate(StrictModel):
    estimate_id: StrictStr = Field(pattern=r"^architecture-estimate-[0-9a-f]{20}$")
    before: ArchitectureMetrics
    expected_after: ArchitectureMetrics
    assumptions: tuple[StrictStr, ...] = Field(min_length=1)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        _require_sorted_unique("architecture estimate assumptions", self.assumptions)
        _require_sorted_unique("architecture estimate limitations", self.limitations)
        return self


class ArchitectureContractSuggestion(StrictModel):
    suggestion_id: StrictStr = Field(pattern=r"^architecture-contract-[0-9a-f]{20}$")
    source_layer: StrictStr = Field(min_length=1)
    target_layer: StrictStr = Field(min_length=1)
    source_module_patterns: tuple[StrictStr, ...] = Field(min_length=1)
    target_module_patterns: tuple[StrictStr, ...] = Field(min_length=1)
    import_linter_text: StrictStr = Field(min_length=1)
    configured_rule: StrictBool
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    confidence: Confidence
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        _require_sorted_unique("architecture source patterns", self.source_module_patterns)
        _require_sorted_unique("architecture target patterns", self.target_module_patterns)
        _require_sorted_unique("architecture suggestion limitations", self.limitations)
        return self


class RefactorProposal(StrictModel):
    proposal_id: StrictStr = Field(pattern=r"^refactor-proposal-[0-9a-f]{20}$")
    candidate_id: StrictStr = Field(pattern=r"^refactor-candidate-[0-9a-f]{20}$")
    candidate_observation_id: StrictStr = Field(pattern=r"^candidate-observation-[0-9a-f]{20}$")
    kind: RefactorProposalKind
    state: RefactorProposalState
    primary_decision_reason: StrictStr = Field(min_length=1)
    gate_decisions: tuple[ProposalGateDecision, ...] = Field(min_length=1)
    problem_statement: StrictStr = Field(min_length=1)
    responsibility_id: StrictStr | None = Field(
        default=None, pattern=r"^responsibility-[0-9a-f]{20}$"
    )
    clone_cluster_ids: tuple[StrictStr, ...] = ()
    affected_subject_ids: tuple[StrictStr, ...] = Field(min_length=1)
    affected_modules: tuple[StrictStr, ...] = Field(min_length=1)
    affected_contract_ids: tuple[StrictStr, ...] = ()
    affected_shape_ids: tuple[StrictStr, ...] = ()
    affected_structure_ids: tuple[StrictStr, ...] = ()
    state_resource_ids: tuple[StrictStr, ...] = ()
    side_effect_ids: tuple[StrictStr, ...] = ()
    affected_test_ids: tuple[StrictStr, ...] = ()
    coverage_observation_ids: tuple[StrictStr, ...] = ()
    change_coupling_ids: tuple[StrictStr, ...] = ()
    architecture_estimate_id: StrictStr = Field(pattern=r"^architecture-estimate-[0-9a-f]{20}$")
    current_ownership: tuple[StrictStr, ...] = Field(min_length=1)
    proposed_logical_ownership: StrictStr = Field(min_length=1)
    allowed_paths: tuple[StrictStr, ...] = Field(min_length=1)
    dependency_identities: tuple[StrictStr, ...] = ()
    consumer_identities: tuple[StrictStr, ...] = ()
    data_contract_impact: tuple[StrictStr, ...] = ()
    compatibility_concerns: tuple[StrictStr, ...] = Field(min_length=1)
    rollback_criteria: tuple[StrictStr, ...] = Field(min_length=1)
    validation_needs: tuple[StrictStr, ...] = Field(min_length=1)
    unresolved_maintainer_decisions: tuple[StrictStr, ...] = Field(min_length=1)
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    counter_evidence: tuple[Evidence, ...] = ()
    confidence: Confidence
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_proposal(self) -> Self:
        for label, values in (
            ("proposal clone cluster IDs", self.clone_cluster_ids),
            ("proposal affected subject IDs", self.affected_subject_ids),
            ("proposal affected modules", self.affected_modules),
            ("proposal affected contract IDs", self.affected_contract_ids),
            ("proposal affected shape IDs", self.affected_shape_ids),
            ("proposal affected structure IDs", self.affected_structure_ids),
            ("proposal state resource IDs", self.state_resource_ids),
            ("proposal side-effect IDs", self.side_effect_ids),
            ("proposal affected test IDs", self.affected_test_ids),
            ("proposal coverage observation IDs", self.coverage_observation_ids),
            ("proposal change coupling IDs", self.change_coupling_ids),
            ("proposal current ownership", self.current_ownership),
            ("proposal allowed paths", self.allowed_paths),
            ("proposal dependency identities", self.dependency_identities),
            ("proposal consumer identities", self.consumer_identities),
            ("proposal data-contract impact", self.data_contract_impact),
            ("proposal compatibility concerns", self.compatibility_concerns),
            ("proposal rollback criteria", self.rollback_criteria),
            ("proposal validation needs", self.validation_needs),
            ("proposal unresolved decisions", self.unresolved_maintainer_decisions),
            ("proposal limitations", self.limitations),
        ):
            _require_sorted_unique(label, values)
        gate_values = tuple(item.gate.value for item in self.gate_decisions)
        expected_gate_values = tuple(item.value for item in ProposalGate)
        if gate_values != expected_gate_values:
            raise ValueError("proposal gate decisions must contain every gate in stable order")
        blocking = tuple(
            item
            for item in self.gate_decisions
            if item.blocking
            and item.outcome in {ProposalGateOutcome.FAILED, ProposalGateOutcome.UNRESOLVED}
        )
        if self.state is RefactorProposalState.BLOCKED_INSUFFICIENT_EVIDENCE and not blocking:
            raise ValueError("blocked proposal requires a failed or unresolved blocking gate")
        if self.state is not RefactorProposalState.BLOCKED_INSUFFICIENT_EVIDENCE and blocking:
            raise ValueError("non-blocked proposal cannot retain a blocking gate")
        required_test_limit = "Phase 4 test-impact evidence is unavailable."
        if self.state is RefactorProposalState.READY_FOR_MAINTAINER_REVIEW:
            if required_test_limit in self.limitations:
                raise ValueError("review-ready proposal cannot retain missing test-impact evidence")
            if not self.affected_test_ids or not self.coverage_observation_ids:
                raise ValueError("review-ready proposal requires test and coverage evidence")
        elif required_test_limit not in self.limitations:
            raise ValueError("incomplete proposal must retain the missing test-impact limitation")
        return self


class UnresolvedEdge(StrictModel):
    unresolved_id: StrictStr = Field(pattern=r"^unresolved-[0-9a-f]{20}$")
    source_id: StrictStr = Field(min_length=1)
    attempted_target: StrictStr | None = None
    operation: StrictStr = Field(min_length=1)
    reason: StrictStr = Field(min_length=1)
    effect_on_completeness: StrictBool
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class Finding(StrictModel):
    finding_id: StrictStr = Field(min_length=1)
    suppression: FindingSuppression | None = None
    rule_id: StrictStr = Field(min_length=1)
    category: Literal[
        "complexity",
        "orchestration",
        "import-cycle",
        "contract-conflict",
        "shape-conflict",
        "coercion-risk",
        "validation-sprawl",
        "ownership-risk",
        "lifecycle-risk",
        "conversion-churn",
        "growth-risk",
        "contract-candidate",
        "duplication",
        "commonality",
        "state-coupling",
        "side-effect-ownership",
        "cohesion",
        "extraction-candidate",
        "module-combination",
        "architecture-contract",
    ]
    severity: Literal["info", "warning", "error"]
    title: StrictStr
    explanation: StrictStr
    module: StrictStr
    symbol: StrictStr | None = None
    subject_ids: tuple[StrictStr, ...] = ()
    affected_contract_ids: tuple[StrictStr, ...] = ()
    affected_shape_ids: tuple[StrictStr, ...] = ()
    affected_structure_ids: tuple[StrictStr, ...] = ()
    affected_clone_cluster_ids: tuple[StrictStr, ...] = ()
    affected_proposal_ids: tuple[StrictStr, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    counter_evidence: tuple[Evidence, ...] = ()
    confidence: Confidence
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()
    likely_validation_needs: tuple[StrictStr, ...] = ()
    language: SourceLanguage = SourceLanguage.PYTHON

    @model_validator(mode="after")
    def validate_reference_order(self) -> Self:
        if self.suppression is not None and self.suppression.finding_id != self.finding_id:
            raise ValueError("suppression references a different finding")
        for label, values in (
            ("subject IDs", self.subject_ids),
            ("affected contract IDs", self.affected_contract_ids),
            ("affected shape IDs", self.affected_shape_ids),
            ("affected structure IDs", self.affected_structure_ids),
            ("affected clone cluster IDs", self.affected_clone_cluster_ids),
            ("affected proposal IDs", self.affected_proposal_ids),
            ("limitations", self.limitations),
            ("likely validation needs", self.likely_validation_needs),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"finding {label} must be unique and sorted")
        phase_two_categories = {
            "contract-conflict",
            "shape-conflict",
            "coercion-risk",
            "validation-sprawl",
            "ownership-risk",
            "lifecycle-risk",
            "conversion-churn",
            "growth-risk",
            "contract-candidate",
        }
        phase_three_categories = {
            "duplication",
            "commonality",
            "state-coupling",
            "side-effect-ownership",
            "cohesion",
            "extraction-candidate",
            "module-combination",
            "architecture-contract",
        }
        if self.category in phase_two_categories | phase_three_categories:
            affected = (
                self.subject_ids
                or self.affected_contract_ids
                or self.affected_shape_ids
                or self.affected_structure_ids
                or self.affected_clone_cluster_ids
                or self.affected_proposal_ids
            )
            if not affected:
                raise ValueError("governed finding requires at least one affected identity")
            if not self.likely_validation_needs:
                raise ValueError("governed finding requires likely validation needs")
            if not self.counter_evidence and not self.limitations:
                raise ValueError("governed finding requires counter-evidence or limitations")
        if self.severity == "error" and self.confidence is Confidence.HEURISTIC:
            raise ValueError("heuristic evidence cannot produce an error finding")
        return self


class TestModuleRecord(StrictModel):
    test_module_id: StrictStr = Field(pattern=r"^test-module-[0-9a-f]{20}$")
    module: StrictStr = Field(min_length=1)
    path: StrictStr = Field(min_length=1)
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    framework: Literal["pytest", "unittest", "mixed", "unknown", "javascript-generic"]
    language: SourceLanguage = SourceLanguage.PYTHON


class TestCaseRecord(StrictModel):
    test_id: StrictStr = Field(pattern=r"^test-case-[0-9a-f]{20}$")
    test_module_id: StrictStr = Field(pattern=r"^test-module-[0-9a-f]{20}$")
    qualified_name: StrictStr = Field(min_length=1)
    parameter_ids: tuple[StrictStr, ...] = ()
    qualification: StrictBool = False
    evidence: tuple[Evidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        _require_sorted_unique("test parameter IDs", self.parameter_ids)
        return self


class TestFixtureRecord(StrictModel):
    fixture_id: StrictStr = Field(pattern=r"^test-fixture-[0-9a-f]{20}$")
    test_module_id: StrictStr = Field(pattern=r"^test-module-[0-9a-f]{20}$")
    qualified_name: StrictStr = Field(min_length=1)
    scope: Literal["function", "class", "module", "package", "session", "unknown"]
    dependency_fixture_ids: tuple[StrictStr, ...] = ()
    evidence: tuple[Evidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        _require_sorted_unique("fixture dependency IDs", self.dependency_fixture_ids)
        return self


class TestRelationshipRecord(StrictModel):
    relationship_id: StrictStr = Field(pattern=r"^test-relationship-[0-9a-f]{20}$")
    source_test_id: StrictStr | None = None
    source_test_module_id: StrictStr | None = None
    kind: TestRelationshipKind
    target_subject_id: StrictStr | None = None
    target_fixture_id: StrictStr | None = None
    target_module: StrictStr | None = None
    target_text: StrictStr | None = None
    resolved: StrictBool
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if (self.source_test_id is None) == (self.source_test_module_id is None):
            raise ValueError(
                "test relationship requires exactly one test-case or test-module source"
            )
        if not any(
            (self.target_subject_id, self.target_fixture_id, self.target_module, self.target_text)
        ):
            raise ValueError("test relationship requires a target")
        _require_sorted_unique("test relationship limitations", self.limitations)
        return self


class CoverageArtifactRecord(StrictModel):
    coverage_id: StrictStr = Field(pattern=r"^coverage-[0-9a-f]{20}$")
    artifact_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    workload_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    binding: CoverageBindingState
    contexts: tuple[StrictStr, ...] = ()
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        _require_sorted_unique("coverage contexts", self.contexts)
        _require_sorted_unique("coverage limitations", self.limitations)
        if (
            self.binding is not CoverageBindingState.BOUND
            and self.completeness is Completeness.COMPLETE
        ):
            raise ValueError("unbound coverage cannot be complete")
        return self


class SymbolCoverageRecord(StrictModel):
    coverage_observation_id: StrictStr = Field(pattern=r"^coverage-observation-[0-9a-f]{20}$")
    coverage_id: StrictStr = Field(pattern=r"^coverage-[0-9a-f]{20}$")
    subject_id: StrictStr
    covered_lines: tuple[StrictInt, ...] = ()
    missing_lines: tuple[StrictInt, ...] = ()
    contexts: tuple[StrictStr, ...] = ()
    missing_arcs: tuple[tuple[StrictInt, StrictInt], ...] = ()

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        for label, values in (
            ("covered lines", self.covered_lines),
            ("missing lines", self.missing_lines),
            ("coverage contexts", self.contexts),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique and sorted")
        if set(self.covered_lines) & set(self.missing_lines):
            raise ValueError("covered and missing lines cannot overlap")
        if list(self.missing_arcs) != sorted(self.missing_arcs) or len(self.missing_arcs) != len(
            set(self.missing_arcs)
        ):
            raise ValueError("missing arcs must be unique and sorted")
        return self


class CoverageFileRecord(StrictModel):
    coverage_file_id: StrictStr = Field(pattern=r"^coverage-file-[0-9a-f]{20}$")
    coverage_id: StrictStr = Field(pattern=r"^coverage-[0-9a-f]{20}$")
    path: StrictStr = Field(min_length=1)
    source_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    binding: CoverageBindingState
    covered_lines: tuple[StrictInt, ...] = ()
    missing_lines: tuple[StrictInt, ...] = ()
    arcs: tuple[tuple[StrictInt, StrictInt], ...] = ()
    missing_arcs: tuple[tuple[StrictInt, StrictInt], ...] = ()
    contexts: tuple[StrictStr, ...] = ()
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        for label, values in (
            ("covered lines", self.covered_lines),
            ("missing lines", self.missing_lines),
            ("arcs", self.arcs),
            ("missing arcs", self.missing_arcs),
            ("contexts", self.contexts),
            ("coverage file limitations", self.limitations),
        ):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"coverage file {label} must be unique and sorted")
        if set(self.covered_lines) & set(self.missing_lines):
            raise ValueError("coverage file covered and missing lines cannot overlap")
        if self.binding is CoverageBindingState.BOUND and self.source_sha256 is None:
            raise ValueError("bound coverage file requires a source hash")
        return self


class GitCommitObservation(StrictModel):
    commit_id: StrictStr = Field(pattern=r"^[0-9a-f]{40,64}$")
    authored_date: StrictStr = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    category: CommitCategory
    contributor_pseudonym: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    changed_paths: tuple[StrictStr, ...]
    bulk_change: StrictBool = False
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        _require_sorted_unique("Git changed paths", self.changed_paths)
        _require_sorted_unique("Git limitations", self.limitations)
        return self


class GitRepositoryIdentity(StrictModel):
    repository_id: StrictStr = Field(pattern=r"^git-repository-[0-9a-f]{20}$")
    head_commit_id: StrictStr = Field(pattern=r"^[0-9a-f]{40,64}$")
    path_scope: GitPathScope
    tracked_files: StrictInt = Field(ge=0)
    shallow: StrictBool
    observed_non_merge_commits: StrictInt = Field(ge=0)
    truncated: StrictBool = False


class GitPathChangeRecord(StrictModel):
    path_change_id: StrictStr = Field(pattern=r"^path-change-[0-9a-f]{20}$")
    commit_id: StrictStr = Field(pattern=r"^[0-9a-f]{40,64}$")
    path: StrictStr = Field(min_length=1)
    kind: ChangeKind
    previous_path: StrictStr | None = None
    additions: StrictInt | None = Field(default=None, ge=0)
    deletions: StrictInt | None = Field(default=None, ge=0)
    formatting_only: StrictBool = False
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        if self.kind is ChangeKind.RENAMED and self.previous_path is None:
            raise ValueError("renamed path change requires its previous path")
        if self.kind is not ChangeKind.RENAMED and self.previous_path is not None:
            raise ValueError("only renamed path changes may retain a previous path")
        _require_sorted_unique("path-change limitations", self.limitations)
        return self


class GitSymbolChangeRecord(StrictModel):
    symbol_change_id: StrictStr = Field(pattern=r"^symbol-change-[0-9a-f]{20}$")
    commit_id: StrictStr = Field(pattern=r"^[0-9a-f]{40,64}$")
    path_change_id: StrictStr = Field(pattern=r"^path-change-[0-9a-f]{20}$")
    subject_id: StrictStr
    kind: ChangeKind
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        _require_sorted_unique("symbol-change limitations", self.limitations)
        return self


class ChangeCouplingRecord(StrictModel):
    coupling_id: StrictStr = Field(pattern=r"^change-coupling-[0-9a-f]{20}$")
    left_path: StrictStr = Field(min_length=1)
    right_path: StrictStr = Field(min_length=1)
    shared_changes: StrictInt = Field(ge=1)
    left_total: StrictInt = Field(ge=1)
    right_total: StrictInt = Field(ge=1)
    jaccard: StrictFloat = Field(ge=0.0, le=1.0)
    commit_ids: tuple[StrictStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        if self.left_path >= self.right_path:
            raise ValueError("change coupling path pairs must be canonical")
        _require_sorted_unique("change coupling commit IDs", self.commit_ids)
        if self.shared_changes > min(self.left_total, self.right_total):
            raise ValueError("shared changes cannot exceed either side total")
        return self


class GenerationChangeRecord(StrictModel):
    generation_change_id: StrictStr = Field(pattern=r"^generation-change-[0-9a-f]{20}$")
    baseline_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    added_paths: tuple[StrictStr, ...] = ()
    changed_paths: tuple[StrictStr, ...] = ()
    removed_paths: tuple[StrictStr, ...] = ()
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        for label, values in (
            ("added paths", self.added_paths),
            ("changed paths", self.changed_paths),
            ("removed paths", self.removed_paths),
            ("generation limitations", self.limitations),
        ):
            _require_sorted_unique(label, values)
        return self


class SectionCoverage(StrictModel):
    section: Literal[
        "r1-static-analysis",
        "static-call-occurrences",
        "contracts",
        "interfaces",
        "dictionary-shapes",
        "validation",
        "data-structures",
        "clones",
        "function-commonality",
        "state-side-effects",
        "cohesion-refactoring",
        "test-impact",
        "coverage",
        "git-history",
        "generation-history",
    ]
    state: SectionState
    required: StrictBool = True
    parsed: StrictInt = Field(default=0, ge=0)
    skipped: StrictInt = Field(default=0, ge=0)
    failed: StrictInt = Field(default=0, ge=0)
    unresolved: StrictInt = Field(default=0, ge=0)
    truncated: StrictInt = Field(default=0, ge=0)
    diagnostic_codes: tuple[StrictStr, ...] = ()
    languages: tuple[SourceLanguage, ...] = (SourceLanguage.PYTHON,)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        incomplete = self.failed + self.unresolved + self.truncated
        if self.state in {SectionState.DISABLED, SectionState.UNAVAILABLE} and any(
            (self.parsed, self.skipped, incomplete, len(self.diagnostic_codes))
        ):
            raise ValueError(
                "disabled or unavailable analyzer coverage cannot contain observations"
            )
        if self.state is SectionState.COMPLETE and incomplete:
            raise ValueError("complete analyzer coverage cannot contain incomplete counts")
        if self.state is SectionState.TRUNCATED and not self.truncated:
            raise ValueError("truncated analyzer coverage requires a truncated count")
        if list(self.diagnostic_codes) != sorted(self.diagnostic_codes) or len(
            self.diagnostic_codes
        ) != len(set(self.diagnostic_codes)):
            raise ValueError("section diagnostic codes must be unique and sorted")
        language_values = tuple(item.value for item in self.languages)
        _require_sorted_unique("section languages", language_values)
        return self


class SummaryCounts(StaticSummaryCounts):
    source_files: StrictInt = Field(default=0, ge=0)
    module_exports: StrictInt = Field(default=0, ge=0)
    duplicate_functions: StrictInt = Field(default=0, ge=0)
    behavior_differences: StrictInt = Field(default=0, ge=0)
    subjects: StrictInt = Field(default=0, ge=0)
    contracts: StrictInt = Field(default=0, ge=0)
    dictionary_shapes: StrictInt = Field(default=0, ge=0)
    dictionary_observations: StrictInt = Field(default=0, ge=0)
    flow_edges: StrictInt = Field(default=0, ge=0)
    data_structures: StrictInt = Field(default=0, ge=0)
    validation_patterns: StrictInt = Field(default=0, ge=0)
    candidate_classifications: StrictInt = Field(default=0, ge=0)
    unresolved_edges: StrictInt = Field(default=0, ge=0)
    clone_fragments: StrictInt = Field(default=0, ge=0)
    clone_clusters: StrictInt = Field(default=0, ge=0)
    function_profiles: StrictInt = Field(default=0, ge=0)
    function_commonalities: StrictInt = Field(default=0, ge=0)
    state_resources: StrictInt = Field(default=0, ge=0)
    side_effects: StrictInt = Field(default=0, ge=0)
    responsibility_clusters: StrictInt = Field(default=0, ge=0)
    architecture_estimates: StrictInt = Field(default=0, ge=0)
    architecture_contract_suggestions: StrictInt = Field(default=0, ge=0)
    refactor_proposals: StrictInt = Field(default=0, ge=0)
    refactor_candidates: StrictInt = Field(default=0, ge=0)
    candidate_observations: StrictInt = Field(default=0, ge=0)
    abstraction_cost_estimates: StrictInt = Field(default=0, ge=0)
    owner_alternatives: StrictInt = Field(default=0, ge=0)
    behavioral_events: StrictInt = Field(default=0, ge=0)
    behavioral_ordering_edges: StrictInt = Field(default=0, ge=0)
    behavioral_invariants: StrictInt = Field(default=0, ge=0)
    candidate_behavior_differences: StrictInt = Field(default=0, ge=0)
    candidate_qualification_requirements: StrictInt = Field(default=0, ge=0)
    interface_endpoints: StrictInt = Field(default=0, ge=0)
    interface_payload_variants: StrictInt = Field(default=0, ge=0)
    interface_correlations: StrictInt = Field(default=0, ge=0)
    interface_mismatches: StrictInt = Field(default=0, ge=0)
    test_modules: StrictInt = Field(default=0, ge=0)
    test_cases: StrictInt = Field(default=0, ge=0)
    test_fixtures: StrictInt = Field(default=0, ge=0)
    test_relationships: StrictInt = Field(default=0, ge=0)
    coverage_artifacts: StrictInt = Field(default=0, ge=0)
    coverage_observations: StrictInt = Field(default=0, ge=0)
    coverage_files: StrictInt = Field(default=0, ge=0)
    git_commits: StrictInt = Field(default=0, ge=0)
    git_path_changes: StrictInt = Field(default=0, ge=0)
    git_symbol_changes: StrictInt = Field(default=0, ge=0)
    change_couplings: StrictInt = Field(default=0, ge=0)
    generation_changes: StrictInt = Field(default=0, ge=0)
    static_call_occurrences: StrictInt = Field(default=0, ge=0)
    static_calls_observed: StrictInt = Field(default=0, ge=0)
    static_calls_omitted: StrictInt = Field(default=0, ge=0)


class AnalysisReport(StrictModel):
    schema_name: Literal["code-analysis-report"] = SCHEMA_NAME
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    tool_version: StrictStr
    run_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    command: CommandIdentity
    target: TargetIdentity
    environment: EnvironmentProvenance
    configuration: ConfigurationIdentity
    source_files: tuple[SourceFileRecord, ...] = ()
    language_frontends: tuple[LanguageFrontendCoverage, ...] = ()
    parser_provenance: tuple[ParserProvenance, ...] = ()
    modules: tuple[Module, ...]
    module_exports: tuple[ModuleExportRecord, ...] = ()
    complexity_observations: tuple[ComplexityObservation, ...]
    import_edges: tuple[ImportEdge, ...]
    strongly_connected_components: tuple[StrongComponent, ...]
    cycles: tuple[ImportCycle, ...]
    subjects: tuple[AnalysisSubject, ...]
    contracts: tuple[ContractDefinition, ...]
    dictionary_shapes: tuple[DictionaryShape, ...]
    dictionary_observations: tuple[DictionaryObservation, ...]
    flow_edges: tuple[FlowEdge, ...]
    data_structures: tuple[DataStructureRecord, ...]
    validation_patterns: tuple[ValidationPattern, ...]
    candidate_classifications: tuple[CandidateClassification, ...]
    clone_fragments: tuple[CloneFragment, ...] = ()
    clone_clusters: tuple[CloneCluster, ...] = ()
    function_profiles: tuple[FunctionFeatureProfile, ...] = ()
    function_commonalities: tuple[FunctionCommonality, ...] = ()
    duplicate_functions: tuple[DuplicateFunctionPair, ...] = ()
    behavior_differences: tuple[BehaviorDifference, ...] = ()
    signal_quality: SignalQualitySummary = SignalQualitySummary()
    state_resources: tuple[StateResource, ...] = ()
    side_effects: tuple[SideEffectObservation, ...] = ()
    responsibility_clusters: tuple[ResponsibilityCluster, ...] = ()
    architecture_estimates: tuple[ArchitectureEstimate, ...] = ()
    architecture_contract_suggestions: tuple[ArchitectureContractSuggestion, ...] = ()
    refactor_proposals: tuple[RefactorProposal, ...] = ()
    refactor_candidates: tuple[RefactorCandidate, ...] = ()
    candidate_observations: tuple[RefactorCandidateObservation, ...] = ()
    abstraction_cost_estimates: tuple[AbstractionCostEstimate, ...] = ()
    owner_alternatives: tuple[OwnerAlternative, ...] = ()
    behavioral_events: tuple[BehavioralEvent, ...] = ()
    behavioral_ordering_edges: tuple[BehavioralOrderingEdge, ...] = ()
    behavioral_invariants: tuple[BehavioralInvariant, ...] = ()
    candidate_behavior_differences: tuple[CandidateBehaviorDifference, ...] = ()
    candidate_qualification_requirements: tuple[CandidateQualificationRequirement, ...] = ()
    interface_endpoints: tuple[InterfaceEndpoint, ...] = ()
    interface_payload_variants: tuple[InterfacePayloadVariant, ...] = ()
    interface_correlations: tuple[InterfaceCorrelation, ...] = ()
    interface_mismatches: tuple[InterfaceMismatch, ...] = ()
    test_modules: tuple[TestModuleRecord, ...] = ()
    test_cases: tuple[TestCaseRecord, ...] = ()
    test_fixtures: tuple[TestFixtureRecord, ...] = ()
    test_relationships: tuple[TestRelationshipRecord, ...] = ()
    coverage_artifacts: tuple[CoverageArtifactRecord, ...] = ()
    coverage_observations: tuple[SymbolCoverageRecord, ...] = ()
    coverage_files: tuple[CoverageFileRecord, ...] = ()
    git_commits: tuple[GitCommitObservation, ...] = ()
    git_repository: GitRepositoryIdentity | None = None
    git_path_changes: tuple[GitPathChangeRecord, ...] = ()
    git_symbol_changes: tuple[GitSymbolChangeRecord, ...] = ()
    change_couplings: tuple[ChangeCouplingRecord, ...] = ()
    generation_changes: tuple[GenerationChangeRecord, ...] = ()
    unresolved_edges: tuple[UnresolvedEdge, ...]
    static_call_occurrences: tuple[StaticCallOccurrence, ...] = ()
    static_call_coverage: tuple[StaticCallSourceCoverage, ...] = ()
    section_coverage: tuple[SectionCoverage, ...]
    findings: tuple[Finding, ...]
    finding_suppression_evaluation: FindingSuppressionEvaluation | None = None
    diagnostics: tuple[Diagnostic, ...]
    providers: tuple[ProviderResult, ...]
    completeness: Completeness
    limitations: tuple[StrictStr, ...]
    summary: SummaryCounts

    @model_validator(mode="after")
    def validate_stable_counts_and_sections(self) -> Self:
        expected = {
            "source_files": len(self.source_files),
            "module_exports": len(self.module_exports),
            "duplicate_functions": len(self.duplicate_functions),
            "behavior_differences": len(self.behavior_differences),
            "subjects": len(self.subjects),
            "contracts": len(self.contracts),
            "dictionary_shapes": len(self.dictionary_shapes),
            "dictionary_observations": len(self.dictionary_observations),
            "flow_edges": len(self.flow_edges),
            "data_structures": len(self.data_structures),
            "validation_patterns": len(self.validation_patterns),
            "candidate_classifications": len(self.candidate_classifications),
            "unresolved_edges": len(self.unresolved_edges),
            "clone_fragments": len(self.clone_fragments),
            "clone_clusters": len(self.clone_clusters),
            "function_profiles": len(self.function_profiles),
            "function_commonalities": len(self.function_commonalities),
            "state_resources": len(self.state_resources),
            "side_effects": len(self.side_effects),
            "responsibility_clusters": len(self.responsibility_clusters),
            "architecture_estimates": len(self.architecture_estimates),
            "architecture_contract_suggestions": len(self.architecture_contract_suggestions),
            "refactor_proposals": len(self.refactor_proposals),
            "refactor_candidates": len(self.refactor_candidates),
            "candidate_observations": len(self.candidate_observations),
            "abstraction_cost_estimates": len(self.abstraction_cost_estimates),
            "owner_alternatives": len(self.owner_alternatives),
            "behavioral_events": len(self.behavioral_events),
            "behavioral_ordering_edges": len(self.behavioral_ordering_edges),
            "behavioral_invariants": len(self.behavioral_invariants),
            "candidate_behavior_differences": len(self.candidate_behavior_differences),
            "candidate_qualification_requirements": len(self.candidate_qualification_requirements),
            "interface_endpoints": len(self.interface_endpoints),
            "interface_payload_variants": len(self.interface_payload_variants),
            "interface_correlations": len(self.interface_correlations),
            "interface_mismatches": len(self.interface_mismatches),
            "test_modules": len(self.test_modules),
            "test_cases": len(self.test_cases),
            "test_fixtures": len(self.test_fixtures),
            "test_relationships": len(self.test_relationships),
            "coverage_artifacts": len(self.coverage_artifacts),
            "coverage_observations": len(self.coverage_observations),
            "coverage_files": len(self.coverage_files),
            "git_commits": len(self.git_commits),
            "git_path_changes": len(self.git_path_changes),
            "git_symbol_changes": len(self.git_symbol_changes),
            "change_couplings": len(self.change_couplings),
            "generation_changes": len(self.generation_changes),
            "static_call_occurrences": len(self.static_call_occurrences),
            "static_calls_observed": sum(item.observed_count for item in self.static_call_coverage),
            "static_calls_omitted": sum(item.omitted_count for item in self.static_call_coverage),
        }
        if any(getattr(self.summary, key) != value for key, value in expected.items()):
            raise ValueError("summary counts do not match report records")
        admission = self.signal_quality.commonality_admission
        if admission is not None and (
            admission.profile_count != len(self.function_profiles)
            or admission.admitted_pairs != len(self.function_commonalities)
        ):
            raise ValueError("commonality admission counts do not match report records")
        decisions = self.signal_quality.proposal_decisions
        proposal_admission = self.signal_quality.proposal_admission
        if proposal_admission is not None and (
            proposal_admission.responsibility_clusters_considered
            != len(self.responsibility_clusters)
            or proposal_admission.retained_proposals != len(self.refactor_proposals)
        ):
            raise ValueError("proposal admission counts do not match report records")
        if decisions is not None:
            expected_states = {
                RefactorProposalState.BLOCKED_INSUFFICIENT_EVIDENCE: (decisions.blocked_proposals),
                RefactorProposalState.REVIEWABLE_INCOMPLETE: (
                    decisions.reviewable_incomplete_proposals
                ),
                RefactorProposalState.READY_FOR_MAINTAINER_REVIEW: (
                    decisions.ready_for_maintainer_review_proposals
                ),
            }
            if decisions.total_proposals != len(self.refactor_proposals) or any(
                sum(item.state is state for item in self.refactor_proposals) != count
                for state, count in expected_states.items()
            ):
                raise ValueError("proposal decision counts do not match report records")
        names = [section.section for section in self.section_coverage]
        if len(names) != len(set(names)):
            raise ValueError("analyzer section coverage names must be unique")
        required_sections = (
            "r1-static-analysis",
            "static-call-occurrences",
            "contracts",
            "interfaces",
            "dictionary-shapes",
            "validation",
            "data-structures",
            "clones",
            "function-commonality",
            "state-side-effects",
            "cohesion-refactoring",
            "test-impact",
            "coverage",
            "git-history",
            "generation-history",
        )
        if tuple(names) != required_sections:
            raise ValueError("report must contain every required analyzer section in stable order")
        states = {section.state for section in self.section_coverage}
        if SectionState.FAILED in states and self.completeness is not Completeness.FAILED:
            raise ValueError("failed analyzer coverage requires failed report completeness")
        if SectionState.PARTIAL in states and self.completeness is Completeness.COMPLETE:
            raise ValueError("partial analyzer coverage cannot produce a complete report")
        required_incomplete = {
            section.state for section in self.section_coverage if section.required
        } & {SectionState.PARTIAL, SectionState.TRUNCATED, SectionState.FAILED}
        if required_incomplete and self.completeness is Completeness.COMPLETE:
            raise ValueError(
                "incomplete required analyzer coverage cannot produce a complete report"
            )
        coverage_by_name: dict[str, SectionCoverage] = {
            section.section: section for section in self.section_coverage
        }
        commonality = self.signal_quality.commonality_admission
        if commonality is not None and commonality.candidate_bound_omissions:
            coverage = coverage_by_name["function-commonality"]
            if (
                coverage.state
                not in {
                    SectionState.PARTIAL,
                    SectionState.TRUNCATED,
                    SectionState.FAILED,
                }
                or not coverage.truncated
            ):
                raise ValueError("commonality omissions require explicit incomplete coverage")
        disabled_records = (
            ("contracts", self.contracts),
            (
                "interfaces",
                (
                    *self.interface_endpoints,
                    *self.interface_payload_variants,
                    *self.interface_correlations,
                    *self.interface_mismatches,
                ),
            ),
            (
                "dictionary-shapes",
                (*self.dictionary_shapes, *self.dictionary_observations),
            ),
            ("validation", self.validation_patterns),
            ("data-structures", self.data_structures),
            ("clones", (*self.clone_fragments, *self.clone_clusters)),
            (
                "function-commonality",
                (*self.function_profiles, *self.function_commonalities),
            ),
            ("state-side-effects", (*self.state_resources, *self.side_effects)),
            (
                "cohesion-refactoring",
                (
                    *self.responsibility_clusters,
                    *self.architecture_estimates,
                    *self.architecture_contract_suggestions,
                    *self.refactor_proposals,
                    *self.refactor_candidates,
                    *self.candidate_observations,
                    *self.abstraction_cost_estimates,
                    *self.owner_alternatives,
                    *self.behavioral_events,
                    *self.behavioral_ordering_edges,
                    *self.behavioral_invariants,
                    *self.candidate_behavior_differences,
                ),
            ),
            (
                "test-impact",
                (
                    *self.test_modules,
                    *self.test_cases,
                    *self.test_fixtures,
                    *self.test_relationships,
                ),
            ),
            (
                "coverage",
                (*self.coverage_artifacts, *self.coverage_files, *self.coverage_observations),
            ),
            (
                "git-history",
                (
                    *self.git_commits,
                    *((self.git_repository,) if self.git_repository is not None else ()),
                    *self.git_path_changes,
                    *self.git_symbol_changes,
                    *self.change_couplings,
                ),
            ),
            ("generation-history", self.generation_changes),
            (
                "static-call-occurrences",
                (*self.static_call_occurrences, *self.static_call_coverage),
            ),
        )
        for section_name, records in disabled_records:
            if coverage_by_name[section_name].state is SectionState.DISABLED and records:
                raise ValueError(f"disabled {section_name} section cannot contain records")
        identity_fields = (
            (self.source_files, "source_file_id"),
            (self.module_exports, "export_id"),
            (self.subjects, "subject_id"),
            (self.contracts, "contract_id"),
            (self.dictionary_shapes, "shape_id"),
            (self.dictionary_observations, "observation_id"),
            (self.flow_edges, "flow_id"),
            (self.data_structures, "structure_id"),
            (self.validation_patterns, "validation_id"),
            (self.candidate_classifications, "candidate_id"),
            (self.clone_fragments, "fragment_id"),
            (self.clone_clusters, "cluster_id"),
            (self.function_profiles, "profile_id"),
            (self.function_commonalities, "commonality_id"),
            (self.duplicate_functions, "duplicate_id"),
            (self.behavior_differences, "difference_id"),
            (self.state_resources, "state_resource_id"),
            (self.side_effects, "side_effect_id"),
            (self.responsibility_clusters, "responsibility_id"),
            (self.architecture_estimates, "estimate_id"),
            (self.architecture_contract_suggestions, "suggestion_id"),
            (self.refactor_proposals, "proposal_id"),
            (self.refactor_candidates, "candidate_id"),
            (self.candidate_observations, "observation_id"),
            (self.abstraction_cost_estimates, "cost_estimate_id"),
            (self.owner_alternatives, "alternative_id"),
            (self.behavioral_events, "event_id"),
            (self.behavioral_ordering_edges, "ordering_id"),
            (self.behavioral_invariants, "invariant_id"),
            (self.candidate_behavior_differences, "difference_id"),
            (self.candidate_qualification_requirements, "requirement_id"),
            (self.interface_endpoints, "endpoint_id"),
            (self.interface_payload_variants, "variant_id"),
            (self.interface_correlations, "correlation_id"),
            (self.interface_mismatches, "mismatch_id"),
            (self.test_modules, "test_module_id"),
            (self.test_cases, "test_id"),
            (self.test_fixtures, "fixture_id"),
            (self.test_relationships, "relationship_id"),
            (self.coverage_artifacts, "coverage_id"),
            (self.coverage_observations, "coverage_observation_id"),
            (self.coverage_files, "coverage_file_id"),
            (self.git_commits, "commit_id"),
            (self.git_path_changes, "path_change_id"),
            (self.git_symbol_changes, "symbol_change_id"),
            (self.change_couplings, "coupling_id"),
            (self.generation_changes, "generation_change_id"),
            (self.unresolved_edges, "unresolved_id"),
            (self.findings, "finding_id"),
            (self.static_call_occurrences, "occurrence_id"),
            (self.static_call_coverage, "source_file_id"),
        )
        for records, field_name in identity_fields:
            identities = [getattr(record, field_name) for record in records]
            if identities != sorted(identities) or len(identities) != len(set(identities)):
                raise ValueError(f"{field_name} records must be unique and sorted")
        if list(self.limitations) != sorted(self.limitations) or len(self.limitations) != len(
            set(self.limitations)
        ):
            raise ValueError("report limitations must be unique and sorted")
        self._validate_referential_integrity()
        validate_static_call_occurrences(self)
        validate_finding_suppressions(self)
        return self

    def _validate_referential_integrity(self) -> None:
        subject_ids = {subject.subject_id for subject in self.subjects}
        source_file_ids = {item.source_file_id for item in self.source_files}
        source_by_path = {item.logical_path: item for item in self.source_files}
        if len(source_by_path) != len(self.source_files):
            raise ValueError("source-file logical paths must be unique")
        for module in self.modules:
            source = source_by_path.get(module.path)
            if self.source_files and source is None:
                raise ValueError(f"module references unknown source path: {module.path}")
            if source is not None and (
                source.module_name != module.name
                or source.source_sha256 != module.source_sha256
                or source.language is not module.language
                or source.parse_state is not module.parse_state
            ):
                raise ValueError("module and source-file identity disagree")
        frontend_languages = tuple(item.language.value for item in self.language_frontends)
        _require_sorted_unique("language front-end coverage", frontend_languages)
        provenance_keys = tuple(
            (
                item.language.value,
                item.logical_path,
                item.frontend_id,
                item.syntax_mode.value if item.syntax_mode else "",
            )
            for item in self.parser_provenance
        )
        if provenance_keys != tuple(sorted(provenance_keys)) or len(provenance_keys) != len(
            set(provenance_keys)
        ):
            raise ValueError("parser provenance must be unique and sorted")
        javascript_provenance = {
            item.logical_path: item
            for item in self.parser_provenance
            if item.language is SourceLanguage.JAVASCRIPT
        }
        for source in self.source_files:
            if source.language is not SourceLanguage.JAVASCRIPT or source.parse_state not in {
                SourceParseState.COMPLETE,
                SourceParseState.PARTIAL,
            }:
                continue
            provenance = javascript_provenance.get(source.logical_path)
            if provenance is None:
                raise ValueError("parsed JavaScript source requires parser provenance")
            if (
                provenance.source_sha256 != source.source_sha256
                or provenance.syntax_mode is not source.syntax_mode
                or provenance.module_system is not source.module_system
            ):
                raise ValueError("JavaScript source and parser provenance disagree")
        for subject in self.subjects:
            if any(evidence.language is not subject.language for evidence in subject.evidence):
                raise ValueError("subject evidence language must match its subject")
        admission = self.signal_quality.commonality_admission
        if admission is not None:
            rejection_subject_ids = {
                subject_id
                for item in admission.rejection_examples
                for subject_id in (item.left_subject_id, item.right_subject_id)
            }
            missing_rejection_subjects = sorted(rejection_subject_ids - subject_ids)
            if missing_rejection_subjects:
                raise ValueError(
                    "candidate rejection examples reference unknown subject IDs: "
                    + ", ".join(missing_rejection_subjects)
                )
        subject_keys = {
            (subject.kind, subject.module, subject.logical_name) for subject in self.subjects
        }
        if len(subject_keys) != len(self.subjects):
            raise ValueError("analysis subject logical identities must be unique")
        module_names = {module.name for module in self.modules}
        missing_subject_modules = sorted(
            {
                subject.module
                for subject in self.subjects
                if subject.kind is not SubjectKind.EXTERNAL and subject.module not in module_names
            }
        )
        if missing_subject_modules:
            raise ValueError(
                "analysis subjects reference unknown modules: " + ", ".join(missing_subject_modules)
            )
        contract_ids = {contract.contract_id for contract in self.contracts}
        boundary_ids = {
            boundary.boundary_id for contract in self.contracts for boundary in contract.boundaries
        }
        shape_ids = {shape.shape_id for shape in self.dictionary_shapes}
        variant_ids = {
            variant.variant_id for shape in self.dictionary_shapes for variant in shape.variants
        }
        state_ids = {
            state.state_id for shape in self.dictionary_shapes for state in shape.lifecycle_states
        }
        flow_ids = {flow.flow_id for flow in self.flow_edges}
        structure_ids = {structure.structure_id for structure in self.data_structures}
        validation_ids = {validation.validation_id for validation in self.validation_patterns}
        clone_fragment_ids = {fragment.fragment_id for fragment in self.clone_fragments}
        clone_cluster_ids = {cluster.cluster_id for cluster in self.clone_clusters}
        state_resource_ids = {resource.state_resource_id for resource in self.state_resources}
        side_effect_ids = {effect.side_effect_id for effect in self.side_effects}
        responsibility_ids = {cluster.responsibility_id for cluster in self.responsibility_clusters}
        estimate_ids = {estimate.estimate_id for estimate in self.architecture_estimates}
        proposal_ids = {proposal.proposal_id for proposal in self.refactor_proposals}
        refactor_candidate_ids = {item.candidate_id for item in self.refactor_candidates}
        candidate_observation_ids = {item.observation_id for item in self.candidate_observations}
        cost_estimate_ids = {item.cost_estimate_id for item in self.abstraction_cost_estimates}
        owner_alternative_ids = {item.alternative_id for item in self.owner_alternatives}
        behavioral_event_ids = {item.event_id for item in self.behavioral_events}
        behavioral_invariant_ids = {item.invariant_id for item in self.behavioral_invariants}
        candidate_behavior_difference_ids = {
            item.difference_id for item in self.candidate_behavior_differences
        }
        candidate_qualification_ids = {
            item.requirement_id for item in self.candidate_qualification_requirements
        }
        interface_endpoint_ids = {item.endpoint_id for item in self.interface_endpoints}
        interface_variant_ids = {item.variant_id for item in self.interface_payload_variants}
        interface_mismatch_ids = {item.mismatch_id for item in self.interface_mismatches}
        duplicate_ids = {item.duplicate_id for item in self.duplicate_functions}
        difference_ids = {item.difference_id for item in self.behavior_differences}
        test_module_ids = {module.test_module_id for module in self.test_modules}
        test_ids = {test.test_id for test in self.test_cases}
        fixture_ids = {fixture.fixture_id for fixture in self.test_fixtures}
        coverage_ids = {artifact.coverage_id for artifact in self.coverage_artifacts}
        coverage_observation_ids = {
            observation.coverage_observation_id for observation in self.coverage_observations
        }
        coverage_file_ids = {item.coverage_file_id for item in self.coverage_files}
        coupling_ids = {coupling.coupling_id for coupling in self.change_couplings}
        path_change_ids = {change.path_change_id for change in self.git_path_changes}
        default_ids = [
            default.default_id
            for contract in self.contracts
            for member in contract.members
            for defaults in (member.producer_defaults, member.consumer_fallbacks)
            for default in defaults
        ] + [
            default.default_id
            for shape in self.dictionary_shapes
            for variant in shape.variants
            for field in variant.fields
            for defaults in (field.producer_defaults, field.consumer_fallbacks)
            for default in defaults
        ]
        if len(default_ids) != len(set(default_ids)):
            raise ValueError("default IDs must be globally unique")
        all_identity_groups = (
            ("boundary IDs", boundary_ids, sum(len(item.boundaries) for item in self.contracts)),
            (
                "variant IDs",
                variant_ids,
                sum(len(item.variants) for item in self.dictionary_shapes),
            ),
            (
                "lifecycle state IDs",
                state_ids,
                sum(len(item.lifecycle_states) for item in self.dictionary_shapes),
            ),
        )
        for label, identities, record_count in all_identity_groups:
            if len(identities) != record_count:
                raise ValueError(f"{label} must be globally unique")
        known_ids = (
            subject_ids
            | contract_ids
            | boundary_ids
            | shape_ids
            | variant_ids
            | state_ids
            | flow_ids
            | structure_ids
            | validation_ids
            | clone_fragment_ids
            | clone_cluster_ids
            | state_resource_ids
            | side_effect_ids
            | responsibility_ids
            | estimate_ids
            | proposal_ids
            | test_module_ids
            | test_ids
            | fixture_ids
            | coverage_ids
            | coverage_observation_ids
            | coverage_file_ids
            | coupling_ids
            | path_change_ids
            | source_file_ids
            | difference_ids
        )

        def require_known(label: str, values: tuple[str, ...] | list[str], known: set[str]) -> None:
            missing = sorted(set(values) - known)
            if missing:
                raise ValueError(f"{label} references unknown IDs: {', '.join(missing)}")

        for contract in self.contracts:
            require_known(
                f"contract {contract.contract_id} defining subject",
                [contract.defining_subject_id],
                subject_ids,
            )
            require_known(
                f"contract {contract.contract_id} boundary sites",
                [boundary.site_subject_id for boundary in contract.boundaries],
                subject_ids,
            )
        modules_by_name = {item.name: item for item in self.modules}
        for export in self.module_exports:
            require_known(
                f"module export {export.export_id} source",
                [export.source_file_id],
                source_file_ids,
            )
            export_module = modules_by_name.get(export.module)
            if export_module is None or export_module.language is not SourceLanguage.JAVASCRIPT:
                raise ValueError("JavaScript export references an unknown JavaScript module")
        subjects_by_id = {item.subject_id: item for item in self.subjects}
        for duplicate in self.duplicate_functions:
            require_known(
                f"duplicate {duplicate.duplicate_id} subjects",
                [duplicate.left_subject_id, duplicate.right_subject_id],
                subject_ids,
            )
            require_known(
                f"duplicate {duplicate.duplicate_id} clone clusters",
                list(duplicate.clone_cluster_ids),
                clone_cluster_ids,
            )
            require_known(
                f"duplicate {duplicate.duplicate_id} differences",
                list(duplicate.behavior_difference_ids),
                difference_ids,
            )
            if any(
                subjects_by_id[value].language is not duplicate.language
                for value in (duplicate.left_subject_id, duplicate.right_subject_id)
            ):
                raise ValueError("duplicate function language must match both subjects")
        for shape in self.dictionary_shapes:
            require_known(
                f"shape {shape.shape_id} owner",
                [shape.owner_subject_id],
                subject_ids,
            )
            require_known(
                f"shape {shape.shape_id} producers",
                list(shape.producer_subject_ids),
                subject_ids,
            )
            require_known(
                f"shape {shape.shape_id} consumers",
                list(shape.consumer_subject_ids),
                subject_ids,
            )
        variant_owner = {
            variant.variant_id: shape.shape_id
            for shape in self.dictionary_shapes
            for variant in shape.variants
        }
        for observation in self.dictionary_observations:
            require_known(
                f"dictionary observation {observation.observation_id} subject",
                [observation.subject_id],
                known_ids,
            )
            if observation.shape_id is not None:
                require_known(
                    f"dictionary observation {observation.observation_id} shape",
                    [observation.shape_id],
                    shape_ids,
                )
            if observation.variant_id is not None:
                require_known(
                    f"dictionary observation {observation.observation_id} variant",
                    [observation.variant_id],
                    variant_ids,
                )
                if variant_owner[observation.variant_id] != observation.shape_id:
                    raise ValueError("dictionary observation variant does not belong to shape")
        for flow in self.flow_edges:
            require_known(
                f"flow {flow.flow_id}",
                [flow.source_id, flow.target_id],
                known_ids,
            )
        for structure in self.data_structures:
            require_known(
                f"structure {structure.structure_id} owner and operations",
                [
                    structure.owner_subject_id,
                    *structure.constructor_subject_ids,
                    *structure.producer_subject_ids,
                    *structure.reader_subject_ids,
                    *structure.writer_subject_ids,
                    *structure.mutator_subject_ids,
                    *structure.serializer_subject_ids,
                    *structure.alias_subject_ids,
                ],
                subject_ids,
            )
            require_known(
                f"structure {structure.structure_id} persistence boundaries",
                list(structure.persistence_boundary_ids),
                boundary_ids,
            )
            require_known(
                f"structure {structure.structure_id} conversion flows",
                list(structure.conversion_flow_ids),
                flow_ids,
            )
        for validation in self.validation_patterns:
            require_known(
                f"validation {validation.validation_id} subjects",
                list(validation.subject_ids),
                known_ids,
            )
        for candidate in self.candidate_classifications:
            require_known(
                f"candidate {candidate.candidate_id} subject",
                [candidate.subject_id],
                known_ids,
            )
        for fragment in self.clone_fragments:
            require_known(
                f"clone fragment {fragment.fragment_id} subject",
                [fragment.subject_id],
                subject_ids,
            )
        for cluster in self.clone_clusters:
            require_known(
                f"clone cluster {cluster.cluster_id} fragments",
                list(cluster.fragment_ids),
                clone_fragment_ids,
            )
            require_known(
                f"clone cluster {cluster.cluster_id} subjects",
                list(cluster.subject_ids),
                subject_ids,
            )
            require_known(
                f"clone cluster {cluster.cluster_id} related clusters",
                list(cluster.related_cluster_ids),
                clone_cluster_ids,
            )
        for profile in self.function_profiles:
            require_known(
                f"function profile {profile.profile_id} subject",
                [profile.subject_id],
                subject_ids,
            )
            require_known(
                f"function profile {profile.profile_id} contracts",
                list(profile.contract_ids),
                contract_ids,
            )
        for commonality in self.function_commonalities:
            require_known(
                f"commonality {commonality.commonality_id} subjects",
                [commonality.left_subject_id, commonality.right_subject_id],
                subject_ids,
            )
            require_known(
                f"commonality {commonality.commonality_id} clone clusters",
                list(commonality.clone_cluster_ids),
                clone_cluster_ids,
            )
        for resource in self.state_resources:
            require_known(
                f"state resource {resource.state_resource_id} owner",
                [resource.owner_subject_id],
                subject_ids,
            )
            require_known(
                f"state resource {resource.state_resource_id} structures",
                list(resource.related_structure_ids),
                structure_ids,
            )
        for effect in self.side_effects:
            require_known(
                f"side effect {effect.side_effect_id} subject",
                [effect.subject_id],
                subject_ids,
            )
        for responsibility in self.responsibility_clusters:
            require_known(
                f"responsibility {responsibility.responsibility_id} members",
                list(responsibility.member_subject_ids),
                subject_ids,
            )
            require_known(
                f"responsibility {responsibility.responsibility_id} clone clusters",
                list(responsibility.clone_cluster_ids),
                clone_cluster_ids,
            )
            require_known(
                f"responsibility {responsibility.responsibility_id} state resources",
                list(responsibility.state_resource_ids),
                state_resource_ids,
            )
            require_known(
                f"responsibility {responsibility.responsibility_id} side effects",
                list(responsibility.side_effect_ids),
                side_effect_ids,
            )
        proposal_admission = self.signal_quality.proposal_admission
        if proposal_admission is not None:
            for proposal_rejection in proposal_admission.rejection_examples:
                require_known(
                    f"proposal rejection {proposal_rejection.candidate_id} responsibility",
                    [proposal_rejection.responsibility_id],
                    responsibility_ids,
                )
                require_known(
                    f"proposal rejection {proposal_rejection.candidate_id} subjects",
                    list(proposal_rejection.affected_subject_ids),
                    subject_ids,
                )
        for proposal in self.refactor_proposals:
            require_known(
                f"proposal {proposal.proposal_id} candidate",
                [proposal.candidate_id],
                refactor_candidate_ids,
            )
            require_known(
                f"proposal {proposal.proposal_id} candidate observation",
                [proposal.candidate_observation_id],
                candidate_observation_ids,
            )
            if proposal.responsibility_id is not None:
                require_known(
                    f"proposal {proposal.proposal_id} responsibility",
                    [proposal.responsibility_id],
                    responsibility_ids,
                )
            require_known(
                f"proposal {proposal.proposal_id} clone clusters",
                list(proposal.clone_cluster_ids),
                clone_cluster_ids,
            )
            require_known(
                f"proposal {proposal.proposal_id} subjects",
                list(proposal.affected_subject_ids),
                subject_ids,
            )
            require_known(
                f"proposal {proposal.proposal_id} contracts",
                list(proposal.affected_contract_ids),
                contract_ids,
            )
            require_known(
                f"proposal {proposal.proposal_id} shapes",
                list(proposal.affected_shape_ids),
                shape_ids,
            )
            require_known(
                f"proposal {proposal.proposal_id} structures",
                list(proposal.affected_structure_ids),
                structure_ids,
            )
            require_known(
                f"proposal {proposal.proposal_id} state resources",
                list(proposal.state_resource_ids),
                state_resource_ids,
            )
            require_known(
                f"proposal {proposal.proposal_id} side effects",
                list(proposal.side_effect_ids),
                side_effect_ids,
            )
            require_known(
                f"proposal {proposal.proposal_id} architecture estimate",
                [proposal.architecture_estimate_id],
                estimate_ids,
            )
            require_known(
                f"proposal {proposal.proposal_id} tests",
                list(proposal.affected_test_ids),
                test_ids,
            )
            require_known(
                f"proposal {proposal.proposal_id} coverage",
                list(proposal.coverage_observation_ids),
                coverage_observation_ids,
            )
            require_known(
                f"proposal {proposal.proposal_id} change coupling",
                list(proposal.change_coupling_ids),
                coupling_ids,
            )
        for refactor_candidate in self.refactor_candidates:
            require_known(
                f"candidate {refactor_candidate.candidate_id} subjects",
                list(refactor_candidate.participating_subject_ids),
                subject_ids,
            )
            subject_languages = {
                item.language
                for item in self.subjects
                if item.subject_id in refactor_candidate.participating_subject_ids
            }
            if subject_languages != set(refactor_candidate.languages):
                raise ValueError(
                    f"candidate {refactor_candidate.candidate_id} language identity disagrees"
                )
        observations_by_candidate: dict[str, int] = {}
        for candidate_observation in self.candidate_observations:
            require_known(
                f"candidate observation {candidate_observation.observation_id} candidate",
                [candidate_observation.candidate_id],
                refactor_candidate_ids,
            )
            require_known(
                f"candidate observation {candidate_observation.observation_id} responsibilities",
                list(candidate_observation.responsibility_ids),
                responsibility_ids,
            )
            require_known(
                f"candidate observation {candidate_observation.observation_id} duplicates",
                list(candidate_observation.duplicate_ids),
                duplicate_ids,
            )
            require_known(
                f"candidate observation {candidate_observation.observation_id} proposals",
                list(candidate_observation.proposal_ids),
                proposal_ids,
            )
            if candidate_observation.cost_estimate_id is not None:
                require_known(
                    f"candidate observation {candidate_observation.observation_id} cost estimate",
                    [candidate_observation.cost_estimate_id],
                    cost_estimate_ids,
                )
            require_known(
                f"candidate observation {candidate_observation.observation_id} owner alternatives",
                list(candidate_observation.owner_alternative_ids),
                owner_alternative_ids,
            )
            require_known(
                f"candidate observation {candidate_observation.observation_id} behavior events",
                list(candidate_observation.behavioral_event_ids),
                behavioral_event_ids,
            )
            require_known(
                f"candidate observation {candidate_observation.observation_id} behavior invariants",
                list(candidate_observation.behavioral_invariant_ids),
                behavioral_invariant_ids,
            )
            require_known(
                f"candidate observation {candidate_observation.observation_id} "
                "behavior differences",
                list(candidate_observation.behavioral_difference_ids),
                candidate_behavior_difference_ids,
            )
            require_known(
                f"candidate observation {candidate_observation.observation_id} "
                "qualification requirements",
                list(candidate_observation.qualification_requirement_ids),
                candidate_qualification_ids,
            )
            observations_by_candidate[candidate_observation.candidate_id] = (
                observations_by_candidate.get(candidate_observation.candidate_id, 0) + 1
            )
        if set(observations_by_candidate) != refactor_candidate_ids or any(
            count != 1 for count in observations_by_candidate.values()
        ):
            raise ValueError("each refactor candidate requires exactly one report observation")
        for estimate in self.abstraction_cost_estimates:
            require_known(
                f"cost estimate {estimate.cost_estimate_id} candidate",
                [estimate.candidate_id],
                refactor_candidate_ids,
            )
        for alternative in self.owner_alternatives:
            require_known(
                f"owner alternative {alternative.alternative_id} candidate",
                [alternative.candidate_id],
                refactor_candidate_ids,
            )
        for event in self.behavioral_events:
            require_known(
                f"behavior event {event.event_id} subject",
                [event.subject_id],
                subject_ids,
            )
        for edge in self.behavioral_ordering_edges:
            require_known(
                f"behavior ordering {edge.ordering_id} subject",
                [edge.subject_id],
                subject_ids,
            )
            require_known(
                f"behavior ordering {edge.ordering_id} events",
                [edge.before_event_id, edge.after_event_id],
                behavioral_event_ids,
            )
        for invariant in self.behavioral_invariants:
            require_known(
                f"behavior invariant {invariant.invariant_id} candidate",
                [invariant.candidate_id],
                refactor_candidate_ids,
            )
            require_known(
                f"behavior invariant {invariant.invariant_id} subjects",
                list(invariant.participating_subject_ids),
                subject_ids,
            )
            require_known(
                f"behavior invariant {invariant.invariant_id} events",
                list(invariant.event_ids),
                behavioral_event_ids,
            )
        for difference in self.candidate_behavior_differences:
            require_known(
                f"candidate behavior difference {difference.difference_id} candidate",
                [difference.candidate_id],
                refactor_candidate_ids,
            )
            require_known(
                f"candidate behavior difference {difference.difference_id} subjects",
                [difference.left_subject_id, difference.right_subject_id],
                subject_ids,
            )
        for requirement in self.candidate_qualification_requirements:
            require_known(
                f"candidate qualification {requirement.requirement_id} candidate",
                [requirement.candidate_id],
                refactor_candidate_ids,
            )
            require_known(
                f"candidate qualification {requirement.requirement_id} tests",
                list(requirement.test_ids),
                test_ids,
            )
            require_known(
                f"candidate qualification {requirement.requirement_id} coverage",
                list(requirement.coverage_observation_ids),
                coverage_observation_ids,
            )
        for endpoint in self.interface_endpoints:
            require_known(
                f"interface endpoint {endpoint.endpoint_id} variants",
                list(endpoint.payload_variant_ids),
                interface_variant_ids,
            )
        for correlation in self.interface_correlations:
            require_known(
                f"interface correlation {correlation.correlation_id} endpoints",
                [correlation.producer_endpoint_id, correlation.consumer_endpoint_id],
                interface_endpoint_ids,
            )
            require_known(
                f"interface correlation {correlation.correlation_id} mismatches",
                list(correlation.mismatch_ids),
                interface_mismatch_ids,
            )
        for mismatch in self.interface_mismatches:
            require_known(
                f"interface mismatch {mismatch.mismatch_id} endpoints",
                [mismatch.producer_endpoint_id, mismatch.consumer_endpoint_id],
                interface_endpoint_ids,
            )
        for test in self.test_cases:
            require_known(f"test {test.test_id} module", [test.test_module_id], test_module_ids)
        for fixture in self.test_fixtures:
            require_known(
                f"fixture {fixture.fixture_id} module", [fixture.test_module_id], test_module_ids
            )
            require_known(
                f"fixture {fixture.fixture_id} dependencies",
                list(fixture.dependency_fixture_ids),
                fixture_ids,
            )
        for relationship in self.test_relationships:
            if relationship.source_test_id is not None:
                require_known(
                    f"test relationship {relationship.relationship_id} source",
                    [relationship.source_test_id],
                    test_ids | fixture_ids,
                )
            if relationship.source_test_module_id is not None:
                require_known(
                    f"test relationship {relationship.relationship_id} module source",
                    [relationship.source_test_module_id],
                    test_module_ids,
                )
            if relationship.target_subject_id is not None:
                require_known(
                    f"test relationship {relationship.relationship_id} target",
                    [relationship.target_subject_id],
                    subject_ids,
                )
            if relationship.target_fixture_id is not None:
                require_known(
                    f"test relationship {relationship.relationship_id} fixture target",
                    [relationship.target_fixture_id],
                    fixture_ids,
                )
        for coverage_observation in self.coverage_observations:
            require_known(
                f"coverage observation {coverage_observation.coverage_observation_id} artifact",
                [coverage_observation.coverage_id],
                coverage_ids,
            )
            require_known(
                f"coverage observation {coverage_observation.coverage_observation_id} subject",
                [coverage_observation.subject_id],
                subject_ids,
            )
        for item in self.coverage_files:
            require_known(
                f"coverage file {item.coverage_file_id} artifact",
                [item.coverage_id],
                coverage_ids,
            )
        commit_ids = {commit.commit_id for commit in self.git_commits}
        for change in self.git_path_changes:
            require_known(
                f"path change {change.path_change_id} commit", [change.commit_id], commit_ids
            )
        for symbol_change in self.git_symbol_changes:
            require_known(
                f"symbol change {symbol_change.symbol_change_id} commit",
                [symbol_change.commit_id],
                commit_ids,
            )
            require_known(
                f"symbol change {symbol_change.symbol_change_id} path change",
                [symbol_change.path_change_id],
                path_change_ids,
            )
            require_known(
                f"symbol change {symbol_change.symbol_change_id} subject",
                [symbol_change.subject_id],
                subject_ids,
            )
        for coupling in self.change_couplings:
            require_known(
                f"change coupling {coupling.coupling_id} commits",
                list(coupling.commit_ids),
                commit_ids,
            )
        for unresolved in self.unresolved_edges:
            require_known(
                f"unresolved edge {unresolved.unresolved_id} source",
                [unresolved.source_id],
                known_ids,
            )
        for finding in self.findings:
            require_known(
                f"finding {finding.finding_id} subjects",
                list(finding.subject_ids),
                known_ids,
            )
            require_known(
                f"finding {finding.finding_id} contracts",
                list(finding.affected_contract_ids),
                contract_ids,
            )
            require_known(
                f"finding {finding.finding_id} shapes",
                list(finding.affected_shape_ids),
                shape_ids,
            )
            require_known(
                f"finding {finding.finding_id} structures",
                list(finding.affected_structure_ids),
                structure_ids,
            )
            require_known(
                f"finding {finding.finding_id} clone clusters",
                list(finding.affected_clone_cluster_ids),
                clone_cluster_ids,
            )
            require_known(
                f"finding {finding.finding_id} proposals",
                list(finding.affected_proposal_ids),
                proposal_ids,
            )
