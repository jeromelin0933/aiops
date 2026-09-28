from dataclasses import FrozenInstanceError, fields, replace
import inspect

import pytest

from llm_generation.config import GenerationConfigError, InvocationBounds, parse_generation_config
from llm_generation.contracts import (
    Claim, ClaimCategory, EvidenceReference, FailureClass, GenerationFailure,
    GenerationInput, GenerationPin, Guidance, Hypothesis, InvocationMetadata,
    RecoveryFact, ResultContent, safe_text,
    RetrySafety, ValidatedGenerationResult, ValidationFact, VersionedIdentity,
)
from llm_generation.identity import IdentityContradiction, require_equivalent_result, semantic_commitment
from llm_generation.ports import AttemptPublicReader, EvidencePublicReader, KnowledgePublicReader
from knowledge_index.contracts import RetrievalResolution
from rca_persistence.contracts import (
    AdmittedRetryDisposition, DiagnosticConclusion, EvidenceCompleteness,
    EvidentialSupport, GenerationProvenance, GuidanceSource, LogicalTryIdentity,
)


HASH = "a" * 64


def pin():
    return GenerationPin("google", "gemini-2.5-flash", VersionedIdentity("profile-1", "1"), VersionedIdentity("prompt-1", "1"), VersionedIdentity("schema-1", "1"), VersionedIdentity("cfg_" + HASH, "1"))


def content():
    source = GenerationInput(LogicalTryIdentity("attempt-1", 1), "operation-1", "snapshot-1", "revision-1", "knowledge-1", RetrievalResolution.NO_MATCH, pin(), "approved evidence", "approved knowledge")
    reference = EvidenceReference("e1", "snapshot-1", "1", "/events/0", HASH)
    claim = Claim("c1", ClaimCategory.OBSERVED_FACT, "Observed event", ("e1",))
    hypothesis = Hypothesis(1, "Possible cause", EvidentialSupport.LOW, ("e1",), (), (), "Limited evidence", ("c1",))
    return ResultContent(source, "1", "Observed event", ("c1",), "Advisory low", ("c1",), DiagnosticConclusion.INCONCLUSIVE, (hypothesis,), (), (), ("Limited evidence",), EvidenceCompleteness.DEGRADED, True, (reference,), (), (claim,), (ValidationFact("schema", "1", HASH),), InvocationMetadata(1, 10, 5, 15, 100))


def config_dict(prompt_template="Analyze admitted evidence"):
    bounds = {name: 100 for name in InvocationBounds.__dataclass_fields__}
    bounds.update(maximum_invocations=1, timeout_seconds=30, maximum_hypotheses=8, maximum_claims=32)
    value = {"version": "1.0", "provider": "google", "model": "gemini-2.5-flash", "profile": {"identity": "profile-1", "version": "1"}, "prompt": {"identity": "prompt-1", "version": "1"}, "result_schema": {"identity": "schema-1", "version": "1"}, "configuration": {"identity": "", "version": "1"}, "bounds": bounds, "hidden_retries_disabled": True, "prompt_template": prompt_template, "result_schema_commitment": HASH}
    value["configuration_commitment"] = semantic_commitment((value["version"], "google", "gemini-2.5-flash", pin().profile, pin().prompt, pin().result_schema, "1", InvocationBounds(**bounds), True, value["prompt_template"], HASH))
    value["configuration"]["identity"] = "cfg_" + value["configuration_commitment"]
    return value


def test_immutable_result_identity_and_equivalent_replay():
    result = ValidatedGenerationResult.from_content(content())
    assert result.validated_result_id.startswith("dvr_")
    assert result == ValidatedGenerationResult.from_content(content())
    assert require_equivalent_result(result, ValidatedGenerationResult.from_content(content())) is result
    with pytest.raises(FrozenInstanceError):
        result.content.summary = "changed"
    with pytest.raises(ValueError):
        ValidatedGenerationResult(result.validated_result_id, HASH, result.content)


def test_result_identity_contradiction_and_lineage_change():
    first = ValidatedGenerationResult.from_content(content())
    changed = content()
    second = ValidatedGenerationResult.from_content(replace(changed, summary="Different summary"))
    assert first.semantic_commitment != second.semantic_commitment
    with pytest.raises(IdentityContradiction):
        require_equivalent_result(first, second)


def test_recovery_fact_carries_only_d_owned_lineage():
    result = ValidatedGenerationResult.from_content(content())
    source = result.content.input
    fact = RecoveryFact(result.validated_result_id, source.try_identity, source.operation_id, result.semantic_commitment, source.evidence_snapshot_id, source.evidence_revision_id, source.knowledge_snapshot_id, source.pin, True)
    assert fact.complete_and_resolvable
    assert "try_outcome" not in RecoveryFact.__dataclass_fields__
    assert "publication" not in RecoveryFact.__dataclass_fields__
    with pytest.raises(ValueError):
        RecoveryFact("dvr_" + HASH, source.try_identity, source.operation_id, result.semantic_commitment, source.evidence_snapshot_id, source.evidence_revision_id, source.knowledge_snapshot_id, source.pin, True)


def test_reference_and_input_reject_secret_or_ground_truth_shape():
    with pytest.raises(ValueError):
        EvidenceReference("e1", "snapshot-1", "1", "/../private", HASH)
    with pytest.raises(ValueError):
        GenerationInput(LogicalTryIdentity("attempt-1", 1), "operation-1", "snapshot-1", "revision-1", "knowledge-1", RetrievalResolution.NO_MATCH, pin(), "expected root cause is X", "approved knowledge")


def test_canonical_knowledge_resolution_is_required_without_private_store_coupling():
    source = content().input
    assert source.knowledge_resolution is RetrievalResolution.NO_MATCH
    assert type(source.knowledge_resolution) is RetrievalResolution
    for invalid in ("NO_MATCH", None, object()):
        with pytest.raises(TypeError):
            replace(source, knowledge_resolution=invalid)
    assert not any("store" in item.name or "database" in item.name for item in fields(GenerationInput))


def test_result_metadata_is_bounded_sanitized_and_has_no_raw_payload_slot():
    result = content()
    assert result.invocation_metadata == InvocationMetadata(1, 10, 5, 15, 100)
    assert {item.name for item in fields(InvocationMetadata)} == {"invocation_count", "input_tokens", "output_tokens", "total_tokens", "duration_ms"}
    with pytest.raises(ValueError):
        InvocationMetadata(1, 10, 5, 15, 600_001)
    with pytest.raises(ValueError):
        InvocationMetadata(1, 10, 5, 15, "sk-EXAMPLE1234567890")
    with pytest.raises(TypeError):
        replace(result, invocation_metadata="raw provider response")


def test_summary_and_severity_claim_coverage_reuses_result_claims():
    result = content()
    assert result.summary_claim_ids == result.severity_claim_ids == (result.claims[0].claim_id,)
    with pytest.raises(ValueError):
        replace(result, summary_claim_ids=("unknown-claim",))
    with pytest.raises(ValueError):
        replace(result, severity_claim_ids=("unknown-claim",))
    assert ValidatedGenerationResult.from_content(result).content is result


def test_secret_shape_rejection_does_not_echo_and_keeps_normal_text():
    value = "sk-EXAMPLE1234567890"
    with pytest.raises(ValueError) as rejected:
        safe_text(value, "prompt_template")
    assert value not in str(rejected.value)
    assert safe_text("Analyze admitted evidence", "prompt_template") == "Analyze admitted evidence"
    with pytest.raises(GenerationConfigError) as config_rejected:
        parse_generation_config(config_dict(value))
    assert value not in str(config_rejected.value)
    with pytest.raises(ValueError):
        safe_text("Use Ground Truth", "prompt_template")
    with pytest.raises(GenerationConfigError):
        parse_generation_config(config_dict("Use Ground Truth"))


def test_closed_types_and_canonical_a_type_identity():
    assert RetrySafety is AdmittedRetryDisposition
    assert {item.value for item in RetrySafety} == {"RETRYABLE", "NON_RETRYABLE", "REPAIR_REQUIRED"}
    assert {item.value for item in ClaimCategory} == {"OBSERVED_FACT", "ANALYTICAL_INFERENCE", "KNOWLEDGE_BACKED_GUIDANCE", "MODEL_SUGGESTED_GUIDANCE"}
    with pytest.raises(TypeError):
        Claim("c2", "OBSERVED_FACT", "fact", ("e1",))
    with pytest.raises(ValueError):
        Guidance("Do something", GuidanceSource.MODEL_SUGGESTED, ("k1",), ("c1",))


def test_failure_class_is_separate_from_retry_safety():
    identity = LogicalTryIdentity("attempt-1", 1)
    assert GenerationFailure(identity, "operation-1", FailureClass.PROVIDER_TIMEOUT, RetrySafety.RETRYABLE, "Timed out").failure_class is FailureClass.PROVIDER_TIMEOUT
    assert GenerationFailure(identity, "operation-1", FailureClass.PROVIDER_TIMEOUT, RetrySafety.NON_RETRYABLE, "Timed out").retry_safety is RetrySafety.NON_RETRYABLE
    with pytest.raises(ValueError):
        GenerationFailure(identity, "operation-1", FailureClass.IDENTITY_CONTRADICTION, RetrySafety.RETRYABLE, "Conflict")
    with pytest.raises(ValueError):
        GenerationFailure(identity, "operation-1", FailureClass.RESULT_SCHEMA, RetrySafety.RETRYABLE, "Bad schema")


def test_exact_pins_match_public_attempt_projection():
    expected = GenerationProvenance("google", "gemini-2.5-flash", "prompt-1", "cfg_" + HASH, "profile-1")
    assert pin().matches_attempt(expected)
    assert not pin().matches_attempt(GenerationProvenance("google", "other-model", "prompt-1", "cfg_" + HASH, "profile-1"))


def test_strict_config_bounds_secrets_ground_truth_and_commitment():
    assert parse_generation_config(config_dict()).pin.configuration.identity == config_dict()["configuration"]["identity"]
    cases = []
    value = config_dict(); value["unexpected"] = 1; cases.append(value)
    value = config_dict(); value["api_key"] = "secret"; cases.append(value)
    value = config_dict(); value["bounds"]["timeout_seconds"] = 0; cases.append(value)
    value = config_dict(); value["bounds"]["maximum_invocations"] = 2; cases.append(value)
    value = config_dict(); value["prompt_template"] = "Use Ground Truth"; cases.append(value)
    value = config_dict(); value["prompt_template"] = "Bearer abc"; cases.append(value)
    value = config_dict(); value["model"] = "other"; cases.append(value)
    for candidate in cases:
        with pytest.raises(GenerationConfigError):
            parse_generation_config(candidate)


def test_public_ports_have_only_semantic_reads():
    assert set(AttemptPublicReader.__dict__) & {"get_attempt_lineage"} == {"get_attempt_lineage"}
    assert {name for name, member in EvidencePublicReader.__dict__.items() if inspect.isfunction(member) and not name.startswith("__")} == {"resolve_snapshot", "resolve_revision"}
    assert {name for name, member in KnowledgePublicReader.__dict__.items() if inspect.isfunction(member) and not name.startswith("__")} == {"read_snapshot", "read_provenance"}
