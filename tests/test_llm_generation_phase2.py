"""Candidate-D deterministic validation and pure A Artifact projection."""

from dataclasses import asdict, replace
import json
from types import SimpleNamespace

import pytest

from _incident_evidence_store_testkit import success
from _knowledge_snapshot_testkit import environment, limits, request, terminal_request
from knowledge_index.contracts import (
    KnowledgeProvenanceChunk, KnowledgeProvenanceProjection, KnowledgeReadResult,
    KnowledgeReadStatus, RetrievalApplicability, RetrievalEvaluationDisposition,
    RetrievalRejectionReason, RetrievalResolution,
)
from llm_generation.config import InvocationBounds, parse_generation_config
from llm_generation.contracts import (
    Claim, EvidenceReference, FailureClass, GenerationInput, Guidance, Hypothesis,
    InvocationMetadata, KnowledgeReference, ResultContent, ValidationFact,
)
from llm_generation.identity import semantic_commitment
from llm_generation.projection import project_artifact
from llm_generation.schema import StructuredOutputError, parse_structured_output
from llm_generation.validation import (
    DegradedAuthorizationFact, GenerationValidationError, evidence_fact_commitment, observed_fact_text,
    validate_generation_result,
)
from rca_persistence.contracts import (
    AttemptLineage, AttemptLineageRead, DiagnosticConclusion, EvidenceCompleteness, EvidentialSupport,
    GenerationAttempt, GenerationLifecycle, GenerationProvenance, GuidanceSource,
    LogicalTryIdentity,
)
from rca_shared.claim_types import ClaimCategory


HASH = "a" * 64


def _config():
    bounds = {name: 32768 for name in InvocationBounds.__dataclass_fields__}
    bounds.update(maximum_invocations=1, timeout_seconds=30, maximum_hypotheses=8, maximum_claims=32)
    value = {
        "version": "1.0", "provider": "google", "model": "gemini-2.5-flash",
        "profile": {"identity": "profile-1", "version": "1"},
        "prompt": {"identity": "prompt-1", "version": "1"},
        "result_schema": {"identity": "schema-1", "version": "1"},
        "configuration": {"identity": "", "version": "1"},
        "bounds": bounds, "hidden_retries_disabled": True,
        "prompt_template": "Analyze admitted evidence", "result_schema_commitment": HASH,
    }
    from llm_generation.contracts import VersionedIdentity
    value["configuration_commitment"] = semantic_commitment((
        value["version"], value["provider"], value["model"],
        VersionedIdentity(**value["profile"]), VersionedIdentity(**value["prompt"]),
        VersionedIdentity(**value["result_schema"]), "1", InvocationBounds(**bounds),
        True, value["prompt_template"], HASH,
    ))
    value["configuration"]["identity"] = "cfg_" + value["configuration_commitment"]
    return parse_generation_config(value)


def _provenance(snapshot, chunk=None, *, eligible=True):
    chunks = ()
    if chunk is not None:
        chunks = (KnowledgeProvenanceChunk(
            chunk.chunk_identity, chunk.document_identity, chunk.document_version_identity,
            chunk.section_identity, chunk.content_commitment, chunk.metadata_commitment,
            "SOP", "SOP_BACKED_ELIGIBLE" if eligible else "CONTEXTUAL_ONLY", eligible,
            1, 0.95, RetrievalApplicability.DIRECT,
            True, RetrievalEvaluationDisposition.INCLUDED,
            RetrievalRejectionReason.NONE, (), (), (),
        ),)
    return KnowledgeProvenanceProjection(
        snapshot.snapshot_key, snapshot.snapshot_commitment, snapshot.schema_version,
        snapshot.resolution, snapshot.source_status, snapshot.operation_key,
        snapshot.activation_generation, snapshot.activation_operation_key,
        snapshot.frozen_build_identity, snapshot.lineage_commitment,
        snapshot.staged_commitment, snapshot.validation_commitment,
        snapshot.artifact_commitment, "manifest-1", "manifest-schema-1", "1",
        "canonical-1", snapshot.manifest_commitment, "corpus-1", "1",
        snapshot.retrieval_profile_identity, snapshot.retrieval_profile_version,
        snapshot.applicability_policy_identity, snapshot.applicability_policy_version,
        "google", "embedding-model", "embedding-profile", 768,
        "chromadb", "index-schema-1", snapshot.query_commitment, (), chunks,
        snapshot.failures,
    )


@pytest.fixture
def case(tmp_path):
    config = _config()
    captured = success()
    store, staged, _, _, service = environment(tmp_path, candidate_score=0.1)
    snapshot = service.resolve(request(), limits()).snapshot
    assert snapshot is not None and snapshot.resolution is RetrievalResolution.NO_MATCH
    provenance = _provenance(snapshot)
    fact = captured.snapshot.snapshot_content["semantic_evidence"]["normalized_evidence"]["LOKI"][0]
    text = observed_fact_text(fact)
    source = GenerationInput(
        LogicalTryIdentity("attempt-1", 1), "operation-1", captured.snapshot.snapshot_id,
        captured.revision.revision_id, snapshot.snapshot_key.value, snapshot.resolution,
        config.pin, "approved evidence", "approved knowledge",
    )
    ref = EvidenceReference(
        "e1", source.evidence_snapshot_id, "1",
        "/semantic_evidence/normalized_evidence/LOKI/0", evidence_fact_commitment(fact),
    )
    claim = Claim("c1", ClaimCategory.OBSERVED_FACT, text, ("e1",))
    hypothesis = Hypothesis(1, text, EvidentialSupport.LOW, ("e1",), (), (), text, ("c1",))
    content = ResultContent(
        source, "1", text, ("c1",), text, ("c1",),
        DiagnosticConclusion.INCONCLUSIVE, (hypothesis,), (), (),
        ("Uncertain",), EvidenceCompleteness(captured.snapshot.completeness.value), True,
        (ref,), (), (claim,),
        (ValidationFact("schema", "1", config.result_schema_commitment),),
        InvocationMetadata(1, 10, 5, 15, 100),
    )
    lineage = AttemptLineage(
        "attempt-1", "aggregate-1", source.evidence_snapshot_id,
        source.evidence_revision_id, source.knowledge_snapshot_id,
        GenerationProvenance(
            config.pin.provider, config.pin.model, config.pin.prompt.identity,
            config.pin.configuration.identity, config.pin.profile.identity,
        ),
    )
    attempts = SimpleNamespace(get_attempt_lineage=lambda _: AttemptLineageRead(
        GenerationAttempt(lineage, GenerationLifecycle.PENDING), (),
    ))
    evidence = SimpleNamespace(
        resolve_snapshot=lambda _: captured.snapshot,
        resolve_revision=lambda _: captured.revision,
    )
    knowledge = SimpleNamespace(
        read_snapshot=lambda _: KnowledgeReadResult(KnowledgeReadStatus.FOUND, snapshot),
        read_provenance=lambda _: KnowledgeReadResult(KnowledgeReadStatus.FOUND, provenance),
    )
    try:
        yield SimpleNamespace(
            content=content, config=config, attempts=attempts, evidence=evidence,
            knowledge=knowledge, snapshot=snapshot, provenance=provenance,
            staged=staged, fact=fact,
        )
    finally:
        store.close()


def _validate(case, content=None, knowledge=None):
    return validate_generation_result(
        content or case.content, case.config, case.attempts, case.evidence,
        knowledge or case.knowledge,
    )


def _structured(content, config):
    data = {
        "schema_identity": config.pin.result_schema.identity,
        "schema_version": config.pin.result_schema.version,
    }
    for name in (
        "contract_version", "summary", "summary_claim_ids", "severity_assessment",
        "severity_claim_ids", "diagnostic_conclusion", "hypotheses", "remediation",
        "prevention", "limitations", "evidence_completeness", "knowledge_gap",
        "evidence_references", "knowledge_references", "claims",
    ):
        value = getattr(content, name)
        if isinstance(value, tuple):
            value = [asdict(item) if hasattr(item, "__dataclass_fields__") else item for item in value]
            if name == "claims" and config.version == "1.0":
                for claim in value:
                    claim.pop("causal_assertion")
        data[name] = value
    return json.loads(json.dumps(data, default=lambda value: value.value))


def test_strict_schema_and_security_rejection(case):
    data = _structured(case.content, case.config)
    assert parse_structured_output(data, case.content.input, case.config, case.content.invocation_metadata) == case.content
    for mutation in (
        lambda item: item.update(extra="unknown"),
        lambda item: item.update(schema_version="2"),
        lambda item: item.update(summary="expected root cause"),
        lambda item: item.update(scenario_id="S1"),
        lambda item: item.update(summary="S1"),
        lambda item: item.update(summary="base64:ZXhwZWN0ZWRfcm9vdF9jYXVzZQ=="),
    ):
        candidate = dict(data)
        mutation(candidate)
        with pytest.raises(StructuredOutputError):
            parse_structured_output(candidate, case.content.input, case.config, case.content.invocation_metadata)
    with pytest.raises(StructuredOutputError):
        parse_structured_output('{"x":1,"x":2}', case.content.input, case.config, case.content.invocation_metadata)


def test_evidence_grounding_and_lossless_projection(case):
    admitted = _validate(case)
    artifact = project_artifact(admitted)
    assert artifact.claim_semantics.claims[0].claim_id == "c1"
    assert artifact.claim_semantics.evidence_references[0].canonical_fact_commitment == evidence_fact_commitment(case.fact)
    assert artifact.claim_semantics.summary_claim_ids == artifact.claim_semantics.severity_claim_ids == ("c1",)
    assert artifact.summary == case.content.summary
    assert artifact.severity_assessment == case.content.severity_assessment
    assert artifact.diagnostic_conclusion is case.content.diagnostic_conclusion
    assert artifact.hypotheses[0].statement == case.content.hypotheses[0].statement
    assert artifact.hypotheses[0].evidential_support is case.content.hypotheses[0].evidential_support
    assert artifact.hypotheses[0].supporting_evidence_ids == case.content.hypotheses[0].supporting_evidence_ids
    assert artifact.hypotheses[0].contradicting_evidence_ids == case.content.hypotheses[0].contradicting_evidence_ids
    assert artifact.claim_semantics.hypothesis_claim_ids == (("c1",),)
    assert artifact.limitations == case.content.limitations
    assert artifact.evidence_completeness is case.content.evidence_completeness
    assert artifact.knowledge_gap is case.content.knowledge_gap
    assert artifact.provenance.evidence_snapshot_id == case.content.input.evidence_snapshot_id
    assert artifact.provenance.knowledge_snapshot_id == case.content.input.knowledge_snapshot_id
    assert project_artifact(_validate(case)) == artifact
    for bad_ref in (
        replace(case.content.evidence_references[0], canonical_fact_commitment=HASH),
        replace(case.content.evidence_references[0], canonical_path="/semantic_evidence/events/99"),
        replace(case.content.evidence_references[0], reference_schema_version="2"),
    ):
        with pytest.raises(GenerationValidationError):
            _validate(case, replace(case.content, evidence_references=(bad_ref,)))
    wrong_revision = success("capture-other", evidence="different evidence").revision
    wrong_evidence = SimpleNamespace(
        resolve_snapshot=case.evidence.resolve_snapshot,
        resolve_revision=lambda _: wrong_revision,
    )
    with pytest.raises(GenerationValidationError):
        validate_generation_result(case.content, case.config, case.attempts, wrong_evidence, case.knowledge)
    altered = replace(case.content.claims[0], text="Unobserved assertion")
    with pytest.raises(GenerationValidationError):
        _validate(case, replace(case.content, claims=(altered,), summary=altered.text,
            severity_assessment=altered.text,
            hypotheses=(replace(case.content.hypotheses[0], statement=altered.text,
                reasoning_summary=altered.text),)))


def test_presentation_and_schema_fact_cannot_bypass_grounding(case):
    with pytest.raises(GenerationValidationError):
        _validate(case, replace(case.content, summary="Unsupported causal statement"))
    with pytest.raises(GenerationValidationError):
        _validate(case, replace(case.content, validation_facts=(ValidationFact("schema", "1", HASH),
            ValidationFact("grounding", "1", HASH))))


def test_knowledge_no_match_and_exact_lineage(case):
    assert _validate(case).content.knowledge_gap is True
    wrong = replace(case.provenance, snapshot_commitment=HASH)
    knowledge = SimpleNamespace(
        read_snapshot=case.knowledge.read_snapshot,
        read_provenance=lambda _: KnowledgeReadResult(KnowledgeReadStatus.FOUND, wrong),
    )
    with pytest.raises(GenerationValidationError):
        _validate(case, knowledge=knowledge)


def test_explicit_inference_sets_and_projection_order(case):
    inference = Claim(
        "c2", ClaimCategory.ANALYTICAL_INFERENCE, "Possible association from e1",
        ("e1",), (), (), EvidentialSupport.LOW,
    )
    hypothesis = replace(
        case.content.hypotheses[0], statement=inference.text,
        reasoning_summary=inference.text, claim_ids=("c2",),
    )
    content = replace(
        case.content, claims=case.content.claims + (inference,),
        hypotheses=(hypothesis,), summary=inference.text, summary_claim_ids=("c2",),
    )
    admitted = _validate(case, content)
    artifact = project_artifact(admitted)
    assert tuple(item.claim_id for item in artifact.claim_semantics.claims) == ("c1", "c2")
    assert artifact.claim_semantics.claims[1].contradicting_evidence_ids == ()
    assert artifact.claim_semantics.claims[1].evidential_support is EvidentialSupport.LOW
    reordered = replace(content, claims=(inference, case.content.claims[0]))
    assert project_artifact(_validate(case, reordered)) != artifact
    with pytest.raises(ValueError):
        replace(inference, contradicting_evidence_ids=None)
    definite = replace(inference, text="Definitely caused by e1")
    with pytest.raises(GenerationValidationError):
        _validate(case, replace(content, claims=(case.content.claims[0], definite),
            summary=definite.text, hypotheses=(replace(hypothesis, statement=definite.text,
                reasoning_summary=definite.text),)))
    with pytest.raises(GenerationValidationError):
        _validate(case, replace(case.content, diagnostic_conclusion=DiagnosticConclusion.IDENTIFIED))


def _unrelated_identified(case, statement="Possible database corruption caused the incident"):
    """All reference facts are valid; the cited log says only 'same evidence'."""

    inference = Claim(
        "causal-1", ClaimCategory.ANALYTICAL_INFERENCE, statement,
        ("e1",), (), (), EvidentialSupport.HIGH,
    )
    hypothesis = replace(
        case.content.hypotheses[0], statement=statement, reasoning_summary=statement,
        evidential_support=EvidentialSupport.HIGH, claim_ids=(inference.claim_id,),
    )
    return replace(
        case.content, claims=case.content.claims + (inference,),
        hypotheses=(hypothesis,), summary=statement, summary_claim_ids=(inference.claim_id,),
        severity_assessment=statement, severity_claim_ids=(inference.claim_id,),
        diagnostic_conclusion=DiagnosticConclusion.IDENTIFIED,
    )


def test_valid_reference_high_empty_contradiction_does_not_prove_cause(case):
    candidate = _unrelated_identified(case)
    assert candidate.evidence_references[0].canonical_fact_commitment == evidence_fact_commitment(case.fact)
    assert candidate.claims[-1].supporting_evidence_ids == ("e1",)
    assert candidate.claims[-1].contradicting_evidence_ids == ()
    assert candidate.claims[-1].evidential_support is EvidentialSupport.HIGH
    with pytest.raises(GenerationValidationError) as rejected:
        _validate(case, candidate)
    assert rejected.value.failure_class is FailureClass.GROUNDING


def test_identified_rejects_hedged_and_over_granular_causality(case):
    for statement in (
        "Possible database corruption caused the incident",
        "Possible database corruption caused every regional incident",
    ):
        candidate = _unrelated_identified(case, statement)
        with pytest.raises(GenerationValidationError) as rejected:
            _validate(case, candidate)
        assert rejected.value.failure_class is FailureClass.GROUNDING
        assert candidate.diagnostic_conclusion is DiagnosticConclusion.IDENTIFIED


def test_most_supported_and_inconclusive_remain_distinct(case):
    assert _validate(case).content.diagnostic_conclusion is DiagnosticConclusion.INCONCLUSIVE
    statement = "Possible association involving " + observed_fact_text(case.fact)
    candidate = _unrelated_identified(case, statement)
    inference = replace(candidate.claims[-1], evidential_support=EvidentialSupport.MEDIUM)
    most_supported = replace(
        candidate, claims=(candidate.claims[0], inference),
        hypotheses=(replace(candidate.hypotheses[0], evidential_support=EvidentialSupport.MEDIUM),),
        diagnostic_conclusion=DiagnosticConclusion.MOST_SUPPORTED,
    )
    admitted = _validate(case, most_supported)
    assert admitted.content.diagnostic_conclusion is DiagnosticConclusion.MOST_SUPPORTED
    assert admitted.content.claims[-1].evidential_support is EvidentialSupport.MEDIUM
    assert admitted.content.claims[-1].contradicting_evidence_ids == ()


def test_match_sop_requires_exact_governed_public_chunk(case, tmp_path):
    (tmp_path / "match").mkdir()
    store, _, _, _, service = environment(tmp_path / "match")
    try:
        snapshot = service.resolve(request("match-1"), limits()).snapshot
        assert snapshot is not None and snapshot.resolution is RetrievalResolution.MATCH
        original = snapshot.chunks[0]
        governed = replace(original, knowledge_type="SOP", guidance_authority="SOP_BACKED_ELIGIBLE", sop_backed_eligible=True)
        snapshot = replace(snapshot, schema_version="1.1", chunks=(governed,))
        provenance = _provenance(snapshot, governed)
        source = replace(
            case.content.input, knowledge_snapshot_id=snapshot.snapshot_key.value,
            knowledge_resolution=RetrievalResolution.MATCH,
        )
        reference = KnowledgeReference(
            "k1", snapshot.snapshot_key.value, snapshot.snapshot_commitment,
            provenance.corpus_identity, provenance.frozen_build_identity,
            provenance.index_schema_identity, governed.document_identity,
            governed.document_version_identity, governed.section_identity,
            governed.chunk_identity, governed.content_commitment, governed.metadata_commitment,
        )
        guidance_claim = Claim(
            "c2", ClaimCategory.KNOWLEDGE_BACKED_GUIDANCE, "Review approved runbook",
            (), (), ("k1",),
        )
        content = replace(
            case.content, input=source, knowledge_gap=False,
            knowledge_references=(reference,), claims=case.content.claims + (guidance_claim,),
            remediation=(Guidance(guidance_claim.text, GuidanceSource.SOP_BACKED, ("k1",), ("c2",)),),
        )
        old_lineage = case.attempts.get_attempt_lineage("attempt-1").attempt.lineage
        lineage = replace(old_lineage, knowledge_snapshot_id=snapshot.snapshot_key.value)
        attempts = SimpleNamespace(get_attempt_lineage=lambda _: AttemptLineageRead(
            GenerationAttempt(lineage, GenerationLifecycle.PENDING), (),
        ))
        knowledge = SimpleNamespace(
            read_snapshot=lambda _: KnowledgeReadResult(KnowledgeReadStatus.FOUND, snapshot),
            read_provenance=lambda _: KnowledgeReadResult(KnowledgeReadStatus.FOUND, provenance),
        )
        admitted = validate_generation_result(content, case.config, attempts, case.evidence, knowledge)
        artifact = project_artifact(admitted)
        assert artifact.claim_semantics.knowledge_references[0].chunk_id == governed.chunk_identity
        assert artifact.remediation_actions[0].source is GuidanceSource.SOP_BACKED
        knowledge_causal = replace(
            guidance_claim, text="Possible SOP diagnosis proves incident cause",
        )
        knowledge_only = replace(
            content, claims=(case.content.claims[0], knowledge_causal),
            summary=knowledge_causal.text, summary_claim_ids=("c2",),
            severity_assessment=knowledge_causal.text, severity_claim_ids=("c2",),
            hypotheses=(replace(content.hypotheses[0], statement=knowledge_causal.text,
                reasoning_summary=knowledge_causal.text, claim_ids=("c2",),
                evidential_support=EvidentialSupport.HIGH),),
            remediation=(Guidance(knowledge_causal.text, GuidanceSource.SOP_BACKED, ("k1",), ("c2",)),),
            diagnostic_conclusion=DiagnosticConclusion.IDENTIFIED,
        )
        with pytest.raises(GenerationValidationError):
            validate_generation_result(knowledge_only, case.config, attempts, case.evidence, knowledge)
        wrong_ref = replace(reference, content_commitment=HASH)
        with pytest.raises(GenerationValidationError):
            validate_generation_result(
                replace(content, knowledge_references=(wrong_ref,)),
                case.config, attempts, case.evidence, knowledge,
            )
        denied = replace(provenance.chunks[0], sop_backed_eligible=False)
        no_authority = replace(provenance, chunks=(denied,))
        knowledge.read_provenance = lambda _: KnowledgeReadResult(KnowledgeReadStatus.FOUND, no_authority)
        with pytest.raises(GenerationValidationError):
            validate_generation_result(content, case.config, attempts, case.evidence, knowledge)
    finally:
        store.close()


def test_no_match_model_suggestion_stays_non_authoritative(case):
    suggested = Claim("c2", ClaimCategory.MODEL_SUGGESTED_GUIDANCE, "Consider inspection")
    guidance = Guidance(suggested.text, GuidanceSource.MODEL_SUGGESTED, (), ("c2",))
    content = replace(case.content, claims=case.content.claims + (suggested,), remediation=(guidance,))
    artifact = project_artifact(_validate(case, content))
    assert artifact.remediation_actions[0].source is GuidanceSource.MODEL_SUGGESTED
    with pytest.raises(GenerationValidationError):
        _validate(case, replace(content, summary=suggested.text, summary_claim_ids=("c2",)))


def test_retrieval_unavailable_requires_exact_governed_degraded_fact(case, tmp_path):
    (tmp_path / "unavailable").mkdir()
    store, _, _, _, service = environment(tmp_path / "unavailable", provider_error=RuntimeError("offline"))
    try:
        assert service.resolve(request(), limits()).snapshot is None
        snapshot = service.finalize_unavailable(terminal_request()).snapshot
        assert snapshot is not None
        provenance = _provenance(snapshot)
        source = replace(
            case.content.input, knowledge_snapshot_id=snapshot.snapshot_key.value,
            knowledge_resolution=RetrievalResolution.RETRIEVAL_UNAVAILABLE,
            degraded_authorization_reference="governed-1",
        )
        content = replace(case.content, input=source, knowledge_gap=snapshot.knowledge_gap)
        old_lineage = case.attempts.get_attempt_lineage("attempt-1").attempt.lineage
        lineage = replace(old_lineage, knowledge_snapshot_id=snapshot.snapshot_key.value)
        attempts = SimpleNamespace(get_attempt_lineage=lambda _: AttemptLineageRead(
            GenerationAttempt(lineage, GenerationLifecycle.PENDING), (),
        ))
        knowledge = SimpleNamespace(
            read_snapshot=lambda _: KnowledgeReadResult(KnowledgeReadStatus.FOUND, snapshot),
            read_provenance=lambda _: KnowledgeReadResult(KnowledgeReadStatus.FOUND, provenance),
        )
        with pytest.raises(GenerationValidationError):
            validate_generation_result(content, case.config, attempts, case.evidence, knowledge)
        authorization = DegradedAuthorizationFact(
            "governed-1", "attempt-1", 1, source.evidence_snapshot_id,
            source.knowledge_snapshot_id, True, True,
        )
        assert validate_generation_result(
            content, case.config, attempts, case.evidence, knowledge,
            degraded_authorization=authorization,
        ).content.input.knowledge_resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE
        with pytest.raises(GenerationValidationError):
            validate_generation_result(
                content, case.config, attempts, case.evidence, knowledge,
                degraded_authorization=replace(authorization, trusted_evidence_sufficient=False),
            )
    finally:
        store.close()
