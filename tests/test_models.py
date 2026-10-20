from __future__ import annotations

import pytest
from pydantic import ValidationError

from code_analysis.config import (
    AnalysisConfig,
    ArchitectureLayer,
    CloneThresholds,
    ContractBoundaryRule,
    PhaseFourLimits,
    PhaseThreeLimits,
    RuntimeConfig,
    Strictness,
    Thresholds,
    WorkspaceConfig,
)
from code_analysis.models import Completeness, Confidence, Evidence, SourceSpan
from code_analysis.report_models import (
    BoundaryClass,
    CloneFragment,
    CloneKind,
    CommonalityDimensionKind,
    CoverageArtifactRecord,
    CoverageBindingState,
    DefaultKind,
    DefaultValue,
    DictionaryKeyKind,
    DictionaryObservation,
    DictionaryOperationKind,
    DictionaryPredicate,
    DictionaryShape,
    FieldRequiredness,
    GitPathScope,
    JsonLiteralValue,
    MutationTiming,
    ProposalAdmissionFunnel,
    ProposalAdmissionRejectionReason,
    ProposalCandidateKind,
    ProposalGate,
    ProposalGateDecision,
    ProposalGateOutcome,
    ProposalRejectionExample,
    RefactorProposal,
    RefactorProposalKind,
    RefactorProposalState,
    RequirednessKind,
    ResponsibilityCluster,
    ShapeDiscriminator,
    ShapeField,
    ShapeLifecycleState,
    ShapeVariant,
    TypeExpression,
    TypeKind,
    UnknownFieldPolicy,
)


def test_strict_threshold_rejects_bool_as_int() -> None:
    with pytest.raises(ValidationError):
        Thresholds(function_loc=True)


def test_source_span_rejects_invalid_line() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(path="a.py", start_line=0, start_column=0, end_line=1, end_column=0)


def test_strictness_preset_and_override() -> None:
    config = AnalysisConfig.with_overrides(strictness=Strictness.STRICT, branches=2)
    assert config.thresholds.function_loc == 50
    assert config.thresholds.branches == 2
    assert config.clone_thresholds.exact_min_lines == 4
    assert config.identity() == config.identity()


def test_phase_three_configuration_is_strict_bounded_and_identity_owned() -> None:
    layers = (
        ArchitectureLayer(
            name="application",
            module_patterns=("package.app.*",),
            may_import_layers=("domain",),
        ),
        ArchitectureLayer(name="domain", module_patterns=("package.domain.*",)),
    )
    config = AnalysisConfig(
        clone_thresholds=CloneThresholds(sequence_min_coverage=0.75),
        phase_three_limits=PhaseThreeLimits(max_candidate_pairs=10),
        architecture_layers=layers,
    )
    baseline = AnalysisConfig()
    assert config.identity() == baseline.identity()
    assert config.phase_two_identity() == baseline.phase_two_identity()
    assert config.phase_three_identity() != baseline.phase_three_identity()
    assert baseline.phase_three_limits.min_responsibility_ownership_dimensions == 2
    assert baseline.phase_three_limits.min_responsibility_clone_nonblank_lines == 12
    assert baseline.clone_thresholds.commonality_min_bilateral_coverage == 0.25
    assert baseline.clone_thresholds.commonality_max_feature_subject_fraction == 0.10
    assert AnalysisConfig.with_overrides(base=config).architecture_layers == layers
    with pytest.raises(ValidationError):
        CloneThresholds(exact_min_lines=True)
    with pytest.raises(ValidationError):
        CloneThresholds(commonality_min_bilateral_coverage=True)
    with pytest.raises(ValidationError):
        CloneThresholds(commonality_max_feature_subject_fraction=0.0)
    with pytest.raises(ValidationError):
        PhaseThreeLimits(max_cluster_members=1)
    with pytest.raises(ValidationError):
        PhaseThreeLimits(min_responsibility_ownership_dimensions=True)
    with pytest.raises(ValidationError):
        PhaseThreeLimits(min_responsibility_clone_nonblank_lines=0)
    with pytest.raises(ValidationError, match="unknown allowed layers"):
        AnalysisConfig(
            architecture_layers=(
                ArchitectureLayer(
                    name="application",
                    module_patterns=("package.*",),
                    may_import_layers=("missing",),
                ),
            )
        )


def test_phase_four_configuration_is_strict_bounded_and_identity_owned() -> None:
    configured = AnalysisConfig(
        phase_four_limits=PhaseFourLimits(max_git_commits=12),
        git_path_scope=GitPathScope.REPOSITORY,
        runtime=RuntimeConfig(timeout_seconds=12.5),
    )
    assert configured.phase_four_identity() != AnalysisConfig().phase_four_identity()
    assert AnalysisConfig.with_overrides(base=configured).phase_four_limits.max_git_commits == 12
    assert AnalysisConfig.with_overrides(base=configured).git_path_scope is GitPathScope.REPOSITORY
    assert (
        configured.phase_three_identity()
        == AnalysisConfig(
            phase_four_limits=PhaseFourLimits(max_git_commits=12),
            runtime=RuntimeConfig(timeout_seconds=12.5),
        ).phase_three_identity()
    )
    with pytest.raises(ValidationError):
        PhaseFourLimits(max_tests=True)
    with pytest.raises(ValidationError):
        PhaseFourLimits(max_repetitions=21)
    with pytest.raises(ValidationError):
        PhaseFourLimits(min_git_shared_changes=True)
    with pytest.raises(ValidationError):
        PhaseFourLimits(min_git_shared_changes=0)
    with pytest.raises(ValidationError, match="unique and sorted"):
        RuntimeConfig(environment_allowlist=("PATH", "LANG"))


def test_unbound_coverage_cannot_claim_complete_evidence() -> None:
    with pytest.raises(ValidationError, match="unbound coverage"):
        CoverageArtifactRecord(
            coverage_id="coverage-" + "1" * 20,
            artifact_sha256="1" * 64,
            workload_identity_sha256="2" * 64,
            source_report_sha256="3" * 64,
            binding=CoverageBindingState.SOURCE_MISMATCH,
            completeness=Completeness.COMPLETE,
        )


def test_workspace_configuration_is_strict_and_local_only() -> None:
    with pytest.raises(ValidationError):
        WorkspaceConfig(enabled=False, path="/state")
    with pytest.raises(ValidationError):
        WorkspaceConfig(cache_max_bytes=True)
    default = AnalysisConfig()
    external = AnalysisConfig(workspace=WorkspaceConfig(path="/different/host/state"))
    assert default.identity() == external.identity()
    assert default.workspace.identity() != external.workspace.identity()


def test_contract_boundary_configuration_is_strict_portable_and_identity_bound() -> None:
    rule = ContractBoundaryRule(
        contract_pattern="package.contracts.*",
        site_pattern="package.api:*",
        boundary_class=BoundaryClass.HTTP_RESPONSE,
    )
    configured = AnalysisConfig(contract_boundaries=(rule,))
    assert configured.identity() == AnalysisConfig().identity()
    assert configured.phase_two_identity() != AnalysisConfig().phase_two_identity()
    assert AnalysisConfig.with_overrides(base=configured).contract_boundaries == (rule,)
    with pytest.raises(ValidationError, match="decisive nonlocal"):
        ContractBoundaryRule(
            contract_pattern="*",
            site_pattern="*",
            boundary_class=BoundaryClass.MODULE_LOCAL,
        )
    with pytest.raises(ValidationError, match="unique and sorted"):
        AnalysisConfig(
            contract_boundaries=(
                rule.model_copy(update={"site_pattern": "z:*"}),
                rule.model_copy(update={"site_pattern": "a:*"}),
            )
        )


def _shape_evidence() -> Evidence:
    return Evidence(
        span=SourceSpan(
            path="package/module.py",
            start_line=1,
            start_column=0,
            end_line=1,
            end_column=12,
        ),
        provider="native-ast",
        observation="dictionary literal",
        confidence=Confidence.EXACT,
        source_sha256="1" * 64,
    )


def test_phase_three_records_are_strict_and_proposals_remain_incomplete() -> None:
    evidence = _shape_evidence()
    fragment = CloneFragment(
        fragment_id="clone-fragment-00000000000000000000",
        kind=CloneKind.EXACT_TEXT,
        subject_id="subject-00000000000000000000",
        fingerprint_sha256="0" * 64,
        structural_signature="body-lines:v1",
        nonblank_lines=6,
        token_count=20,
        ast_node_count=12,
        statement_count=4,
        evidence=(evidence,),
    )
    assert fragment.model_copy() == fragment
    common = {
        "proposal_id": "refactor-proposal-00000000000000000000",
        "candidate_id": "refactor-candidate-00000000000000000000",
        "candidate_observation_id": "candidate-observation-00000000000000000000",
        "kind": RefactorProposalKind.EXTRACT_RESPONSIBILITY,
        "state": RefactorProposalState.REVIEWABLE_INCOMPLETE,
        "primary_decision_reason": "Static gates passed; Phase 4 evidence is required.",
        "gate_decisions": tuple(
            ProposalGateDecision(
                decision_id=f"proposal-gate-{index:020x}",
                gate=gate,
                outcome=(
                    ProposalGateOutcome.UNRESOLVED
                    if gate in {ProposalGate.TEST_IMPACT, ProposalGate.COVERAGE_BINDING}
                    else (
                        ProposalGateOutcome.NOT_APPLICABLE
                        if gate is ProposalGate.RUNTIME_CLAIM
                        else ProposalGateOutcome.PASSED
                    )
                ),
                blocking=False,
                reason=f"{gate.value} decision",
            )
            for index, gate in enumerate(ProposalGate)
        ),
        "problem_statement": "Two functions share a bounded responsibility.",
        "affected_subject_ids": ("subject-00000000000000000000",),
        "affected_modules": ("package.module",),
        "architecture_estimate_id": "architecture-estimate-00000000000000000000",
        "current_ownership": ("package.module",),
        "proposed_logical_ownership": "package.shared",
        "allowed_paths": ("package/module.py",),
        "compatibility_concerns": ("Public callers require review.",),
        "rollback_criteria": ("Restore current ownership if validation fails.",),
        "validation_needs": ("Phase 4 test-impact mapping.",),
        "unresolved_maintainer_decisions": ("Select final module ownership.",),
        "evidence": (evidence,),
        "confidence": Confidence.INFERRED,
        "completeness": Completeness.PARTIAL,
    }
    with pytest.raises(ValidationError, match="test-impact"):
        RefactorProposal.model_validate({**common, "limitations": ("Static evidence only.",)})
    proposal = RefactorProposal.model_validate(
        {
            **common,
            "limitations": (
                "Phase 4 test-impact evidence is unavailable.",
                "Static evidence only.",
            ),
        }
    )
    assert proposal.state is RefactorProposalState.REVIEWABLE_INCOMPLETE


def test_proposal_admission_funnel_is_strict_and_reconciled() -> None:
    example = ProposalRejectionExample(
        candidate_id="proposal-candidate-" + "1" * 20,
        kind=ProposalCandidateKind.EXTRACT_RESPONSIBILITY,
        responsibility_id="responsibility-" + "2" * 20,
        affected_subject_ids=("subject-" + "1" * 20, "subject-" + "2" * 20),
        affected_modules=("package.module",),
        reason=ProposalAdmissionRejectionReason.INCOMPLETE_CALL_RESOLUTION_ONLY,
        evidence=(_shape_evidence(),),
    )
    funnel = ProposalAdmissionFunnel(
        responsibility_clusters_considered=2,
        proposal_candidates_considered=3,
        admitted_candidates=2,
        call_only_rejections=1,
        too_broad_rejections=0,
        proposal_bound_omissions=1,
        retained_proposals=1,
        rejection_examples=(example,),
    )
    assert funnel.example_limit == 25
    with pytest.raises(ValidationError):
        ProposalAdmissionFunnel.model_validate(
            {**funnel.model_dump(), "proposal_candidates_considered": True}
        )
    with pytest.raises(ValidationError, match="candidate counts"):
        ProposalAdmissionFunnel.model_validate(
            {**funnel.model_dump(), "proposal_candidates_considered": 4}
        )
    with pytest.raises(ValidationError, match="retained counts"):
        ProposalAdmissionFunnel.model_validate({**funnel.model_dump(), "retained_proposals": 0})
    with pytest.raises(ValidationError, match="unique and sorted"):
        ProposalAdmissionFunnel.model_validate(
            {
                **funnel.model_dump(),
                "proposal_candidates_considered": 4,
                "call_only_rejections": 2,
                "rejection_examples": (example, example),
            }
        )


def test_oversized_responsibility_evidence_and_too_broad_rejection_are_retained() -> None:
    members = tuple(f"subject-{index:020d}" for index in range(13))
    cluster = ResponsibilityCluster(
        responsibility_id="responsibility-" + "3" * 20,
        member_subject_ids=members,
        shared_dimensions=(
            CommonalityDimensionKind.CALLEES,
            CommonalityDimensionKind.CONTRACTS,
        ),
        evidence=(
            _shape_evidence(),
            _shape_evidence().model_copy(update={"observation": "second"}),
        ),
        confidence=Confidence.RESOLVED,
        completeness=Completeness.COMPLETE,
        limitations=("Component retained; proposal is too broad.",),
    )
    rejection = ProposalRejectionExample(
        candidate_id="proposal-candidate-" + "4" * 20,
        kind=ProposalCandidateKind.EXTRACT_RESPONSIBILITY,
        responsibility_id=cluster.responsibility_id,
        affected_subject_ids=cluster.member_subject_ids,
        affected_modules=("package.module",),
        reason=ProposalAdmissionRejectionReason.RESPONSIBILITY_COMPONENT_TOO_BROAD,
        evidence=cluster.evidence,
    )
    assert len(cluster.member_subject_ids) == 13
    assert rejection.reason is ProposalAdmissionRejectionReason.RESPONSIBILITY_COMPONENT_TOO_BROAD


def test_dictionary_shape_is_strict_frozen_pydantic_structure() -> None:
    evidence = _shape_evidence()
    shape = DictionaryShape(
        shape_id="shape-" + "1" * 20,
        owner_subject_id="subject-" + "1" * 20,
        lifecycle_states=(
            ShapeLifecycleState(
                state_id="state-" + "1" * 20,
                name="created",
                evidence=(evidence,),
            ),
        ),
        variants=(
            ShapeVariant(
                variant_id="variant-" + "1" * 20,
                name="default",
                lifecycle_state_ids=("state-" + "1" * 20,),
                fields=(
                    ShapeField(
                        name="enabled",
                        type_expression=TypeExpression(kind=TypeKind.SCALAR, name="bool"),
                        requiredness_observations=(
                            FieldRequiredness(kind=RequirednessKind.ALWAYS),
                        ),
                        producer_defaults=(
                            DefaultValue(
                                default_id="default-" + "1" * 20,
                                kind=DefaultKind.JSON_LITERAL,
                                json_literal=JsonLiteralValue(value=False),
                                evidence=(evidence,),
                            ),
                        ),
                        evidence=(evidence,),
                        confidence=Confidence.EXACT,
                    ),
                    ShapeField(
                        name="metadata",
                        type_expression=TypeExpression(
                            kind=TypeKind.MAPPING,
                            arguments=(
                                TypeExpression(kind=TypeKind.SCALAR, name="str"),
                                TypeExpression(kind=TypeKind.UNKNOWN),
                            ),
                        ),
                        requiredness_observations=(
                            FieldRequiredness(kind=RequirednessKind.OPTIONAL),
                        ),
                        consumer_fallbacks=(
                            DefaultValue(
                                default_id="default-" + "2" * 20,
                                kind=DefaultKind.JSON_LITERAL,
                                json_literal=JsonLiteralValue(value={}),
                                evidence=(evidence,),
                            ),
                        ),
                        evidence=(evidence,),
                        confidence=Confidence.INFERRED,
                    ),
                ),
                evidence=(evidence,),
                confidence=Confidence.RESOLVED,
            ),
        ),
        unknown_field_policy=UnknownFieldPolicy.FORBID,
        producer_subject_ids=("subject-" + "1" * 20,),
        consumer_subject_ids=("subject-" + "2" * 20,),
        evidence=(evidence,),
        confidence=Confidence.RESOLVED,
    )
    fallback = shape.variants[0].fields[1].consumer_fallbacks[0]
    assert fallback.json_literal == JsonLiteralValue(value={})
    with pytest.raises(ValidationError):
        shape.owner_subject_id = "subject-" + "3" * 20
    with pytest.raises(ValidationError):
        ShapeField(
            name="enabled",
            type_expression=TypeExpression(kind=TypeKind.SCALAR, name="bool"),
            requiredness_observations=({"kind": "always", "condition": True},),
            evidence=(evidence,),
            confidence=Confidence.EXACT,
        )
    with pytest.raises(ValidationError):
        DictionaryShape.model_validate({**shape.model_dump(), "unknown": True})


def test_dictionary_shape_rejects_unstable_or_empty_evidence() -> None:
    evidence = _shape_evidence()

    def field(name: str) -> ShapeField:
        return ShapeField(
            name=name,
            type_expression=TypeExpression(kind=TypeKind.UNKNOWN),
            requiredness_observations=(FieldRequiredness(kind=RequirednessKind.UNKNOWN),),
            evidence=(evidence,),
            confidence=Confidence.HEURISTIC,
        )

    base = {
        "shape_id": "shape-" + "2" * 20,
        "owner_subject_id": "subject-" + "2" * 20,
        "unknown_field_policy": UnknownFieldPolicy.UNKNOWN,
        "evidence": (evidence,),
        "confidence": Confidence.HEURISTIC,
    }
    with pytest.raises(ValidationError, match="unique and sorted"):
        DictionaryShape(
            variants=(
                ShapeVariant(
                    variant_id="variant-" + "1" * 20,
                    name="bad-order",
                    fields=(field("z"), field("a")),
                    evidence=(evidence,),
                    confidence=Confidence.HEURISTIC,
                ),
            ),
            **base,
        )
    with pytest.raises(ValidationError, match="unique and sorted"):
        DictionaryShape(
            variants=(
                ShapeVariant(
                    variant_id="variant-" + "1" * 20,
                    name="duplicate",
                    fields=(field("a"), field("a")),
                    evidence=(evidence,),
                    confidence=Confidence.HEURISTIC,
                ),
            ),
            **base,
        )
    with pytest.raises(ValidationError):
        DictionaryShape(variants=(), **base)


def test_defaults_preserve_source_role_and_structural_kind() -> None:
    evidence = _shape_evidence()
    producer_default = DefaultValue(
        default_id="default-" + "3" * 20,
        kind=DefaultKind.EXPLICIT_NULL,
        evidence=(evidence,),
    )
    consumer_fallback = DefaultValue(
        default_id="default-" + "4" * 20,
        kind=DefaultKind.FACTORY,
        detail="package.module:new_payload",
        evidence=(evidence,),
    )
    field = ShapeField(
        name="payload",
        type_expression=TypeExpression(kind=TypeKind.MAPPING),
        requiredness_observations=(FieldRequiredness(kind=RequirednessKind.OPTIONAL),),
        producer_defaults=(producer_default,),
        consumer_fallbacks=(consumer_fallback,),
        evidence=(evidence,),
        confidence=Confidence.RESOLVED,
    )
    assert field.producer_defaults[0].kind is DefaultKind.EXPLICIT_NULL
    assert field.consumer_fallbacks[0].kind is DefaultKind.FACTORY
    for kind in (DefaultKind.ABSENT, DefaultKind.EXPLICIT_NULL):
        assert (
            DefaultValue(
                default_id="default-" + ("5" if kind is DefaultKind.ABSENT else "6") * 20,
                kind=kind,
                evidence=(evidence,),
            ).detail
            is None
        )
    for kind in (DefaultKind.EXPRESSION, DefaultKind.UNRESOLVED):
        assert (
            DefaultValue(
                default_id="default-" + ("7" if kind is DefaultKind.EXPRESSION else "8") * 20,
                kind=kind,
                detail="dynamic default",
                evidence=(evidence,),
            ).detail
            == "dynamic default"
        )
    with pytest.raises(ValidationError, match="requires only detail"):
        DefaultValue(
            default_id="default-" + "9" * 20,
            kind=DefaultKind.FACTORY,
            evidence=(evidence,),
        )
    with pytest.raises(ValidationError, match="cannot contain a payload"):
        DefaultValue(
            default_id="default-" + "a" * 20,
            kind=DefaultKind.ABSENT,
            detail="not allowed",
            evidence=(evidence,),
        )
    with pytest.raises(ValidationError, match="must use explicit-null"):
        DefaultValue(
            default_id="default-" + "b" * 20,
            kind=DefaultKind.JSON_LITERAL,
            json_literal=JsonLiteralValue(value=None),
            evidence=(evidence,),
        )
    with pytest.raises(ValidationError, match="finite canonical JSON"):
        JsonLiteralValue(value=float("nan"))


def test_shape_variants_keep_discriminators_states_and_conditional_fields_separate() -> None:
    evidence = _shape_evidence()
    discriminator = ShapeDiscriminator(
        field_name="kind",
        values=(JsonLiteralValue(value="batch"),),
        evidence=(evidence,),
    )
    fields = (
        ShapeField(
            name="batch_size",
            type_expression=TypeExpression(kind=TypeKind.SCALAR, name="int"),
            requiredness_observations=(
                FieldRequiredness(
                    kind=RequirednessKind.CONDITIONAL,
                    condition="kind == 'batch'",
                    evidence=(evidence,),
                ),
            ),
            evidence=(evidence,),
            confidence=Confidence.EXACT,
        ),
        ShapeField(
            name="kind",
            type_expression=TypeExpression(kind=TypeKind.LITERAL, literal_values=("batch",)),
            requiredness_observations=(FieldRequiredness(kind=RequirednessKind.ALWAYS),),
            evidence=(evidence,),
            confidence=Confidence.EXACT,
        ),
    )
    shape = DictionaryShape(
        shape_id="shape-" + "3" * 20,
        owner_subject_id="subject-" + "3" * 20,
        lifecycle_states=(
            ShapeLifecycleState(
                state_id="state-" + "3" * 20,
                name="queued",
                evidence=(evidence,),
            ),
        ),
        variants=(
            ShapeVariant(
                variant_id="variant-" + "3" * 20,
                name="batch",
                discriminator=discriminator,
                lifecycle_state_ids=("state-" + "3" * 20,),
                fields=fields,
                evidence=(evidence,),
                confidence=Confidence.EXACT,
            ),
        ),
        unknown_field_policy=UnknownFieldPolicy.FORBID,
        evidence=(evidence,),
        confidence=Confidence.RESOLVED,
    )
    assert shape.variants[0].discriminator == discriminator
    assert (
        shape.variants[0].fields[0].requiredness_observations[0].kind
        is RequirednessKind.CONDITIONAL
    )
    with pytest.raises(ValidationError, match="unknown lifecycle state"):
        DictionaryShape.model_validate(
            {
                **shape.model_dump(),
                "variants": (
                    {
                        **shape.variants[0].model_dump(),
                        "lifecycle_state_ids": ("state-" + "f" * 20,),
                    },
                ),
            }
        )


def test_dictionary_observations_distinguish_key_and_operation_semantics() -> None:
    evidence = _shape_evidence()
    base = {
        "subject_id": "subject-" + "4" * 20,
        "evidence": (evidence,),
        "confidence": Confidence.EXACT,
    }
    observations = (
        DictionaryObservation(
            observation_id="dict-observation-" + "1" * 20,
            key_kind=DictionaryKeyKind.LITERAL_STRING,
            key_repr="enabled",
            operation=DictionaryOperationKind.MEMBERSHIP,
            predicate=DictionaryPredicate.PRESENT,
            mutation_timing=MutationTiming.NOT_MUTATION,
            **base,
        ),
        DictionaryObservation(
            observation_id="dict-observation-" + "2" * 20,
            key_kind=DictionaryKeyKind.DYNAMIC,
            key_repr="field_name",
            operation=DictionaryOperationKind.TRUTHINESS,
            predicate=DictionaryPredicate.TRUTHY,
            mutation_timing=MutationTiming.NOT_MUTATION,
            **base,
        ),
        DictionaryObservation(
            observation_id="dict-observation-" + "3" * 20,
            key_kind=DictionaryKeyKind.LITERAL_NON_STRING,
            key_repr="1",
            key_type=TypeExpression(kind=TypeKind.SCALAR, name="int"),
            operation=DictionaryOperationKind.READ,
            mutation_timing=MutationTiming.NOT_MUTATION,
            **base,
        ),
        DictionaryObservation(
            observation_id="dict-observation-" + "4" * 20,
            key_kind=DictionaryKeyKind.UNRESOLVED,
            operation=DictionaryOperationKind.COERCION,
            mutation_timing=MutationTiming.NOT_MUTATION,
            coercion="bool(value)",
            **base,
        ),
        DictionaryObservation(
            observation_id="dict-observation-" + "5" * 20,
            key_kind=DictionaryKeyKind.LITERAL_STRING,
            key_repr="validated",
            operation=DictionaryOperationKind.WRITE,
            mutation_timing=MutationTiming.AFTER_VALIDATION,
            **base,
        ),
    )
    assert {item.operation for item in observations} == {
        DictionaryOperationKind.MEMBERSHIP,
        DictionaryOperationKind.TRUTHINESS,
        DictionaryOperationKind.READ,
        DictionaryOperationKind.COERCION,
        DictionaryOperationKind.WRITE,
    }
    with pytest.raises(ValidationError, match="requires a predicate"):
        DictionaryObservation(
            observation_id="dict-observation-" + "6" * 20,
            key_kind=DictionaryKeyKind.LITERAL_STRING,
            key_repr="enabled",
            operation=DictionaryOperationKind.MEMBERSHIP,
            mutation_timing=MutationTiming.NOT_MUTATION,
            **base,
        )
    with pytest.raises(ValidationError, match="only predicate observations"):
        DictionaryObservation(
            observation_id="dict-observation-" + "7" * 20,
            key_kind=DictionaryKeyKind.LITERAL_STRING,
            key_repr="enabled",
            operation=DictionaryOperationKind.READ,
            predicate=DictionaryPredicate.TRUTHY,
            mutation_timing=MutationTiming.NOT_MUTATION,
            **base,
        )
    assert observations[-1].mutation_timing is MutationTiming.AFTER_VALIDATION
    with pytest.raises(ValidationError, match="requires mutation timing"):
        DictionaryObservation(
            observation_id="dict-observation-" + "6" * 20,
            key_kind=DictionaryKeyKind.LITERAL_STRING,
            key_repr="invalid",
            operation=DictionaryOperationKind.WRITE,
            mutation_timing=MutationTiming.NOT_MUTATION,
            **base,
        )
