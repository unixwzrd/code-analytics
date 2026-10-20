"""Strict persisted contracts for bounded saved-report queries."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from code_analysis.finding_suppression_models import FindingSuppressionAnnotation
from code_analysis.models import Completeness, Confidence, Evidence, StrictModel

QUERY_SCHEMA_NAME = "code-analysis-query"
QUERY_SCHEMA_VERSION = "1.0.0"


class QuerySelectorKind(StrEnum):
    CALL_SITE = "call-site"
    CONTRACT = "contract"
    SHAPE = "shape"
    STRUCTURE = "structure"
    FINDING = "finding"
    CLONE = "clone"
    DUPLICATE = "duplicate"
    RESPONSIBILITY = "responsibility"
    PROPOSAL = "proposal"
    REFACTOR_CANDIDATE = "refactor-candidate"
    SYMBOL = "symbol"
    TEST = "test"
    COVERAGE = "coverage"
    GIT_CHANGE = "git-change"
    GENERATION = "generation"
    PROFILE_FRAME = "profile-frame"
    RUNTIME_EDGE = "runtime-edge"
    DEPENDENCY_PATH = "dependency-path"


class QueryItemKind(StrEnum):
    CALL_SITE = "call-site"
    MODULE = "module"
    SYMBOL = "symbol"
    SUBJECT = "subject"
    CONTRACT = "contract"
    BOUNDARY = "boundary"
    SHAPE = "shape"
    DICTIONARY_OBSERVATION = "dictionary-observation"
    FLOW = "flow"
    STRUCTURE = "structure"
    VALIDATION = "validation"
    CANDIDATE = "candidate"
    FINDING = "finding"
    UNRESOLVED = "unresolved"
    IMPORT_EDGE = "import-edge"
    CLONE_FRAGMENT = "clone-fragment"
    CLONE_CLUSTER = "clone-cluster"
    DUPLICATE_FUNCTION = "duplicate-function"
    FUNCTION_PROFILE = "function-profile"
    FUNCTION_COMMONALITY = "function-commonality"
    STATE_RESOURCE = "state-resource"
    SIDE_EFFECT = "side-effect"
    RESPONSIBILITY = "responsibility"
    ARCHITECTURE_ESTIMATE = "architecture-estimate"
    ARCHITECTURE_CONTRACT = "architecture-contract"
    PROPOSAL = "proposal"
    REFACTOR_CANDIDATE = "refactor-candidate"
    TEST_MODULE = "test-module"
    TEST_CASE = "test-case"
    TEST_FIXTURE = "test-fixture"
    TEST_RELATIONSHIP = "test-relationship"
    COVERAGE_ARTIFACT = "coverage-artifact"
    COVERAGE_FILE = "coverage-file"
    COVERAGE_OBSERVATION = "coverage-observation"
    GIT_COMMIT = "git-commit"
    GIT_PATH_CHANGE = "git-path-change"
    GIT_SYMBOL_CHANGE = "git-symbol-change"
    CHANGE_COUPLING = "change-coupling"
    GENERATION_CHANGE = "generation-change"
    PROFILE_FRAME = "profile-frame"
    RUNTIME_EDGE = "runtime-edge"


class QuerySelector(StrictModel):
    kind: QuerySelectorKind
    identity: StrictStr = Field(min_length=1)
    target_identity: StrictStr | None = None

    @model_validator(mode="after")
    def validate_selector(self) -> Self:
        if (self.kind is QuerySelectorKind.DEPENDENCY_PATH) != (self.target_identity is not None):
            raise ValueError("only dependency-path selectors require a target identity")
        return self


class QueryItem(StrictModel):
    item_id: StrictStr = Field(min_length=1)
    kind: QueryItemKind
    identity: StrictStr = Field(min_length=1)
    relationship: StrictStr = Field(min_length=1)
    statement: StrictStr = Field(min_length=1)
    related_ids: tuple[StrictStr, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    confidence: Confidence | None = None
    completeness: Completeness
    limitations: tuple[StrictStr, ...] = ()
    finding_suppression: FindingSuppressionAnnotation | None = None

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        for label, values in (("related IDs", self.related_ids), ("limitations", self.limitations)):
            if list(values) != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"query item {label} must be unique and sorted")
        if self.kind is QueryItemKind.FINDING:
            if self.finding_suppression is None:
                raise ValueError("finding query items require a suppression annotation")
        elif self.finding_suppression is not None:
            raise ValueError("only finding query items may carry suppression annotations")
        return self


class QueryResponse(StrictModel):
    schema_name: Literal["code-analysis-query"] = QUERY_SCHEMA_NAME
    schema_version: Literal["1.0.0"] = QUERY_SCHEMA_VERSION
    query_id: StrictStr = Field(pattern=r"^query-[0-9a-f]{20}$")
    source_report_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    source_run_identity_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    selector: QuerySelector
    limit: StrictInt = Field(ge=1, le=500)
    after_identity: StrictStr | None = None
    returned_count: StrictInt = Field(ge=0)
    next_identity: StrictStr | None = None
    truncated: StrictBool
    source_completeness: Completeness
    items: tuple[QueryItem, ...]
    limitations: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_response(self) -> Self:
        identities = [item.item_id for item in self.items]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise ValueError("query items must be unique and sorted")
        if self.returned_count != len(self.items):
            raise ValueError("query returned count must match items")
        if self.truncated != (self.next_identity is not None):
            raise ValueError("truncated query requires exactly one continuation identity")
        if list(self.limitations) != sorted(self.limitations) or len(self.limitations) != len(
            set(self.limitations)
        ):
            raise ValueError("query limitations must be unique and sorted")
        return self
