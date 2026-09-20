from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from typing import get_type_hints

import pytest

from rca_persistence import (
    RCA_ERROR_DISPOSITIONS,
    AdmitAttemptRequest,
    AdmittedRetryDisposition,
    ArtifactProvenance,
    ArtifactStatementKind,
    AttemptLineageRead,
    AttemptLineage,
    CreateAggregateRequest,
    CurrentFreshness,
    CurrentRca,
    CurrentRcaRead,
    DiagnosticConclusion,
    EvidenceCompleteness,
    EvidenceReference,
    EvidentialSupport,
    GenerationAttempt,
    GenerationLifecycle,
    GenerationProvenance,
    GuidanceSource,
    KnowledgeReference,
    LogicalTryIdentity,
    LogicalTryOutcome,
    LogicalTryResultKind,
    PublicationDisposition,
    PublicationRequest,
    PublicationResult,
    PublicationTargetIdentity,
    RcaAction,
    RcaAggregate,
    RcaArtifact,
    RcaContractError,
    RcaDomainError,
    RcaErrorCode,
    RcaFailureDisposition,
    RcaHypothesis,
    RcaMutationPort,
    RcaReadPort,
    RcaVersion,
    RecoveryCandidate,
    RecoveryCandidateKind,
    VersionRole,
    require_equivalent_attempt_lineage,
)


NOW = datetime(2026, 9, 20, 8, 30, tzinfo=timezone.utc)


def _generation() -> GenerationProvenance:
    return GenerationProvenance("gemini", "model-1", "prompt-1", "config-1", "profile-1")


def _lineage() -> AttemptLineage:
    return AttemptLineage("ATT-1", "AGG-1", "ES-1", "ER-1", "KS-1", _generation())


def _artifact() -> RcaArtifact:
    evidence = EvidenceReference("E-1", ArtifactStatementKind.OBSERVED_FACT, "Database latency rose")
    knowledge = KnowledgeReference("K-1", "CORPUS-1", "INDEX-1", "DOC-1", "3", "SEC-2", "CHUNK-4")
    provenance = ArtifactProvenance("ES-1", "ER-1", "KS-1", (evidence,), (knowledge,), _generation())
    hypothesis = RcaHypothesis(
        1,
        "Database saturation is the most supported cause",
        EvidentialSupport.HIGH,
        ("E-1",),
        (),
        ("K-1",),
        "The observed latency and approved runbook agree",
    )
    remediation = RcaAction("Reduce database load", GuidanceSource.SOP_BACKED, ("K-1",))
    prevention = RcaAction("Review capacity limits", GuidanceSource.MODEL_SUGGESTED)
    return RcaArtifact(
        "The database became saturated",
        "HIGH",
        DiagnosticConclusion.MOST_SUPPORTED,
        (hypothesis,),
        (remediation,),
        (prevention,),
        ("No host-level trace was available",),
        EvidenceCompleteness.DEGRADED,
        False,
        provenance,
    )


def test_happy_path_constructs_immutable_candidate_a_contracts() -> None:
    aggregate = RcaAggregate("AGG-1", "INC-1")
    attempt = GenerationAttempt(_lineage(), GenerationLifecycle.GENERATING, 1)
    artifact = _artifact()
    target = PublicationTargetIdentity("PUB-1", "AGG-1", "INC-1", "VER-1", None)
    version = RcaVersion("VER-1", "AGG-1", 1, "ATT-1", artifact, "PUB-1", VersionRole.COMMITTED_UNPUBLISHED)
    current = CurrentRca("AGG-1", "VER-1", CurrentFreshness.FRESH, "ER-1")

    assert aggregate.incident_id == "INC-1"
    assert attempt.lineage.credential_profile_id == "profile-1"
    assert version.artifact.provenance.evidence_revision_id == "ER-1"
    assert target.expected_current_version_id is None
    assert current.material_evidence_revision_basis == "ER-1"
    with pytest.raises(FrozenInstanceError):
        aggregate.incident_id = "INC-2"  # type: ignore[misc]


def test_generation_lifecycle_is_closed_and_separate_from_completeness() -> None:
    assert {item.value for item in GenerationLifecycle} == {"PENDING", "GENERATING", "COMPLETED", "FAILED"}
    assert {item.value for item in EvidenceCompleteness} == {"FULL", "DEGRADED"}
    assert "PARTIAL" not in GenerationLifecycle.__members__
    with pytest.raises(RcaContractError, match="GenerationLifecycle"):
        GenerationAttempt(_lineage(), "PARTIAL")  # type: ignore[arg-type]
    with pytest.raises(RcaContractError, match="EvidenceCompleteness"):
        replace(_artifact(), evidence_completeness=GenerationLifecycle.COMPLETED)


def test_attempt_lineage_equivalent_replay_and_contradiction_are_typed() -> None:
    existing = _lineage()
    assert require_equivalent_attempt_lineage(existing, _lineage()) is existing
    contradictory = replace(existing, evidence_revision_id="ER-2")
    with pytest.raises(RcaDomainError) as captured:
        require_equivalent_attempt_lineage(existing, contradictory)
    assert captured.value.code is RcaErrorCode.IDENTITY_LINEAGE_CONFLICT
    assert captured.value.retry_disposition is RcaFailureDisposition.REPAIR_REQUIRED


def test_logical_try_identity_is_logical_ordinal_not_physical_invocation() -> None:
    identity = LogicalTryIdentity("ATT-1", 2)
    assert identity == LogicalTryIdentity("ATT-1", 2)
    assert not hasattr(identity, "physical_invocation_id")
    with pytest.raises(RcaContractError, match="positive logical ordinal"):
        LogicalTryIdentity("ATT-1", 0)


def test_try_outcome_enforces_typed_success_and_failure_shapes() -> None:
    success = LogicalTryOutcome(
        LogicalTryIdentity("ATT-1", 1), LogicalTryResultKind.VALIDATED_RESULT,
        AdmittedRetryDisposition.NON_RETRYABLE, NOW, validated_result_id="VALID-1",
    )
    failure = LogicalTryOutcome(
        LogicalTryIdentity("ATT-1", 2), LogicalTryResultKind.FAILURE,
        AdmittedRetryDisposition.RETRYABLE, NOW, failure_code="PROVIDER_TIMEOUT",
        safe_failure_message="Provider timed out",
    )
    assert success.validated_result_id == "VALID-1"
    assert failure.failure_code == "PROVIDER_TIMEOUT"
    with pytest.raises(RcaContractError, match="requires failure_code"):
        LogicalTryOutcome(
            LogicalTryIdentity("ATT-1", 3), LogicalTryResultKind.FAILURE,
            AdmittedRetryDisposition.RETRYABLE, NOW,
        )


def test_artifact_requires_resolvable_evidence_and_knowledge_provenance() -> None:
    artifact = _artifact()
    assert artifact.hypotheses[0].supporting_evidence_ids == ("E-1",)
    bad_hypothesis = replace(artifact.hypotheses[0], supporting_evidence_ids=("E-MISSING",))
    with pytest.raises(RcaContractError, match="unresolved evidence"):
        replace(artifact, hypotheses=(bad_hypothesis,))
    with pytest.raises(RcaContractError, match="knowledge provenance"):
        RcaAction("Follow the runbook", GuidanceSource.SOP_BACKED)


def test_artifact_ranking_is_bounded_structure_not_free_text_payload() -> None:
    artifact = _artifact()
    rank_two = replace(artifact.hypotheses[0], rank=2)
    with pytest.raises(RcaContractError, match="continuous from 1"):
        replace(artifact, hypotheses=(rank_two,))
    assert not hasattr(artifact, "raw_provider_response")
    assert not hasattr(artifact, "payload")


def test_publication_replay_identity_excludes_authoritative_now() -> None:
    target = PublicationTargetIdentity("PUB-1", "AGG-1", "INC-1", "VER-1", None)
    first = PublicationRequest(target, NOW)
    retry = PublicationRequest(target, NOW + timedelta(hours=1))
    assert first.target == retry.target
    assert first.authoritative_now != retry.authoritative_now
    assert not hasattr(target, "authoritative_now")


def test_aggregate_and_attempt_request_replay_identities_exclude_now() -> None:
    first = CreateAggregateRequest("OP-1", "AGG-1", "INC-1", NOW)
    retry = CreateAggregateRequest("OP-1", "AGG-1", "INC-1", NOW + timedelta(minutes=5))
    attempt_first = AdmitAttemptRequest("OP-2", _lineage(), NOW)
    attempt_retry = AdmitAttemptRequest("OP-2", _lineage(), NOW + timedelta(minutes=5))
    assert first.replay_identity == retry.replay_identity
    assert attempt_first.replay_identity == attempt_retry.replay_identity


def test_applied_publication_requires_target_as_resulting_current() -> None:
    target = PublicationTargetIdentity("PUB-1", "AGG-1", "INC-1", "VER-1", None)
    applied = PublicationResult(target, PublicationDisposition.APPLIED, NOW, "VER-1")
    assert applied.resulting_current_version_id == "VER-1"
    with pytest.raises(RcaContractError, match="make its target"):
        PublicationResult(target, PublicationDisposition.APPLIED, NOW, "VER-2")


def test_recovery_vocabulary_requires_kind_specific_identity() -> None:
    candidate = RecoveryCandidate(
        RecoveryCandidateKind.COMMITTED_UNPUBLISHED_VERSION,
        "AGG-1", version_id="VER-1", publication_operation_id="PUB-1",
    )
    assert candidate.version_id == "VER-1"
    with pytest.raises(RcaContractError, match="requires version and operation"):
        RecoveryCandidate(RecoveryCandidateKind.UNRESOLVED_PUBLICATION, "AGG-1")


def test_typed_failure_families_are_distinct_and_exhaustively_mapped() -> None:
    assert set(RCA_ERROR_DISPOSITIONS) == set(RcaErrorCode)
    assert RcaErrorCode.NOT_FOUND is not RcaErrorCode.INTEGRITY_CORRUPTION
    error = RcaDomainError(RcaErrorCode.PUBLICATION_EVIDENCE_INCONSISTENCY, "conflict", version_id="VER-1")
    assert error.retry_disposition is RcaFailureDisposition.REPAIR_REQUIRED
    assert error.version_id == "VER-1"


def test_secret_values_have_no_contract_field_or_generic_escape_hatch() -> None:
    generation = _generation()
    assert generation.credential_profile_id == "profile-1"
    for forbidden in ("credential", "api_key", "token", "secret", "password"):
        assert not hasattr(generation, forbidden)
    with pytest.raises(TypeError):
        GenerationProvenance(
            provider_id="gemini", model_id="model-1", prompt_id="prompt-1",
            configuration_id="config-1", credential_profile_id="profile-1",
            api_key="secret",  # type: ignore[call-arg]
        )


def test_evaluation_ground_truth_has_no_production_contract_surface() -> None:
    artifact = _artifact()
    for forbidden in (
        "scenario_id", "evaluation_run_id", "expected_root_cause",
        "accepted_answer", "validator_expected_answer", "ground_truth",
    ):
        assert not hasattr(artifact, forbidden)
    with pytest.raises(TypeError):
        RcaArtifact(
            **{field: getattr(artifact, field) for field in artifact.__dataclass_fields__},
            scenario_id="S1",  # type: ignore[call-arg]
        )


def test_cross_domain_authorities_are_references_not_embedded_implementations() -> None:
    lineage = _lineage()
    artifact = _artifact()
    assert isinstance(lineage.evidence_snapshot_id, str)
    assert isinstance(lineage.knowledge_snapshot_id, str)
    assert not hasattr(lineage, "evidence_snapshot")
    assert not hasattr(lineage, "knowledge_snapshot")
    assert not hasattr(artifact, "materiality")
    assert not hasattr(artifact, "incident_rca_ref")
    assert not hasattr(artifact, "retry_budget")


def test_public_ports_are_semantic_and_expose_no_storage_or_scheduler_primitive() -> None:
    read_methods = set(RcaReadPort.__dict__)
    mutation_methods = set(RcaMutationPort.__dict__)
    assert {"get_current", "get_version_history", "enumerate_recovery_candidates", "validate_local_readiness"} <= read_methods
    assert {"create_or_discover_aggregate", "commit_validated_artifact", "complete_authorized_publication"} <= mutation_methods
    forbidden_fragments = ("sql", "connection", "table", "scheduler", "retry_budget", "incident_store")
    assert not any(fragment in name for name in read_methods | mutation_methods for fragment in forbidden_fragments)


def test_current_read_returns_one_coherent_relationship_version_and_artifact() -> None:
    artifact = _artifact()
    current = CurrentRca("AGG-1", "VER-1", CurrentFreshness.FRESH, "ER-1")
    version = RcaVersion("VER-1", "AGG-1", 1, "ATT-1", artifact, "PUB-1", VersionRole.CURRENT)
    outcome = LogicalTryOutcome(
        LogicalTryIdentity("ATT-1", 1), LogicalTryResultKind.VALIDATED_RESULT,
        AdmittedRetryDisposition.NON_RETRYABLE, NOW, validated_result_id="VALID-1",
    )
    lineage = AttemptLineageRead(
        GenerationAttempt(_lineage(), GenerationLifecycle.COMPLETED, 1), (outcome,)
    )
    publication = PublicationResult(
        PublicationTargetIdentity("PUB-1", "AGG-1", "INC-1", "VER-1", None),
        PublicationDisposition.APPLIED,
        NOW,
        "VER-1",
    )
    view = CurrentRcaRead(current, version, artifact, lineage, publication)

    assert view.current.current_version_id == view.version.version_id
    assert view.artifact is artifact
    assert get_type_hints(RcaReadPort.get_current)["return"] == CurrentRcaRead | None

    with pytest.raises(RcaContractError, match="identify the returned Version"):
        CurrentRcaRead(replace(current, current_version_id="VER-OTHER"), version, artifact, lineage, publication)
    with pytest.raises(RcaContractError, match="returned Artifact"):
        CurrentRcaRead(current, version, replace(artifact, summary="Different Artifact"), lineage, publication)


def test_attempt_read_returns_one_coherent_core_summary_and_ordered_try_history() -> None:
    first = LogicalTryOutcome(
        LogicalTryIdentity("ATT-1", 1), LogicalTryResultKind.FAILURE,
        AdmittedRetryDisposition.RETRYABLE, NOW, failure_code="TIMEOUT",
    )
    second = LogicalTryOutcome(
        LogicalTryIdentity("ATT-1", 2), LogicalTryResultKind.VALIDATED_RESULT,
        AdmittedRetryDisposition.NON_RETRYABLE, NOW, validated_result_id="VALID-1",
    )
    attempt = GenerationAttempt(_lineage(), GenerationLifecycle.COMPLETED, 2)
    view = AttemptLineageRead(attempt, (first, second))

    assert view.attempt.lineage == _lineage()
    assert tuple(item.identity.try_ordinal for item in view.try_outcomes) == (1, 2)
    assert get_type_hints(RcaReadPort.get_attempt_lineage)["return"] == AttemptLineageRead | None
    assert "get_attempt" not in RcaReadPort.__dict__
    assert "get_try_outcomes" not in RcaReadPort.__dict__

    with pytest.raises(RcaContractError, match="ordered and continuous"):
        AttemptLineageRead(attempt, (second, first))
    with pytest.raises(RcaContractError, match="latest durable Try"):
        AttemptLineageRead(replace(attempt, latest_try_ordinal=1), (first, second))


def test_timestamps_require_timezone_and_are_canonicalized_to_utc() -> None:
    with pytest.raises(RcaContractError, match="timezone-aware"):
        CreateAggregateRequest("OP-1", "AGG-1", "INC-1", datetime(2026, 9, 20))
    offset = timezone(timedelta(hours=8))
    request = CreateAggregateRequest("OP-1", "AGG-1", "INC-1", datetime(2026, 9, 20, 16, 30, tzinfo=offset))
    assert request.authoritative_now == NOW
