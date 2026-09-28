"""Field-based causal proof and lossless D-to-A projection checks."""

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from _incident_evidence_store_testkit import bounds_facts, success
from incident_evidence import CaptureSuccess, EvidenceCompleteness as BEvidenceCompleteness, EvidenceRevision, EvidenceSnapshot
from knowledge_index.contracts import RetrievalResolution
from llm_generation.config import InvocationBounds, parse_generation_config
from llm_generation.contracts import (
    CausalAssertion, CausalRelation, Claim, EvidenceReference, GenerationInput,
    Hypothesis, InvocationMetadata, ResultContent, ValidatedGenerationResult,
    ValidationFact, VersionedIdentity,
)
from llm_generation.identity import semantic_commitment
from llm_generation.projection import project_artifact
from llm_generation.schema import StructuredOutputError, parse_structured_output
from llm_generation.validation import (
    CAUSAL_RULE_ID, CAUSAL_RULE_VERSION, GenerationValidationError,
    derive_causal_support_proof, evidence_fact_commitment, observed_fact_text,
    validate_generation_result,
)
from rca_persistence.contracts import (
    AttemptLineageRead, DiagnosticConclusion, EvidenceCompleteness, EvidentialSupport,
    GenerationAttempt, GenerationLifecycle, GenerationProvenance, LogicalTryIdentity,
)
from rca_shared.claim_types import ClaimCategory
from test_llm_generation_phase2 import case as phase2_case


def _configuration(*, policy_version=CAUSAL_RULE_VERSION):
    bounds = {name: 32768 for name in InvocationBounds.__dataclass_fields__}
    bounds.update(maximum_invocations=1, timeout_seconds=30, maximum_hypotheses=8, maximum_claims=32)
    raw = {
        "version": "2.0", "provider": "google", "model": "gemini-2.5-flash",
        "profile": {"identity": "profile-2", "version": "2"},
        "prompt": {"identity": "prompt-2", "version": "2"},
        "result_schema": {"identity": "schema-2", "version": "2"},
        "configuration": {"identity": "", "version": "2"},
        "bounds": bounds, "hidden_retries_disabled": True,
        "prompt_template": "Analyze admitted evidence", "result_schema_commitment": "a" * 64,
        "causal_rule_policy_id": CAUSAL_RULE_ID,
        "causal_rule_policy_version": policy_version,
    }
    commitment = semantic_commitment((
        raw["version"], raw["provider"], raw["model"],
        VersionedIdentity(**raw["profile"]), VersionedIdentity(**raw["prompt"]),
        VersionedIdentity(**raw["result_schema"]), "2", InvocationBounds(**bounds),
        True, raw["prompt_template"], raw["result_schema_commitment"],
        raw["causal_rule_policy_id"], policy_version,
    ))
    raw["configuration_commitment"] = commitment
    raw["configuration"]["identity"] = "cfg_" + commitment
    return parse_generation_config(raw)


def _event(event_id, timestamp, event_type, selectors):
    return {
        "event_id": event_id, "detected_at": timestamp,
        "event_source": "log_event_detection", "event_type": event_type,
        "severity": "CRITICAL", "selector_values": [list(pair) for pair in selectors],
    }


def _case(*, downstream="payments", cause_type="oom_crash_detected", effect_time="2026-09-21T11:59:30Z", completeness=BEvidenceCompleteness.FULL):
    degraded_bounds = bounds_facts(
        observed_candidate_count=2, included_count=1, omitted_count=1,
        omission_reason="bounded capture", completeness_impact=BEvidenceCompleteness.DEGRADED,
    ) if completeness is BEvidenceCompleteness.DEGRADED else None
    base = success(completeness=completeness, loki_bounds=degraded_bounds)
    cause = _event("EVT-CAUSE", "2026-09-21T11:58:30Z", cause_type, (("service_name", "payments"),))
    effect = _event("EVT-EFFECT", effect_time, "downstream_cascade_failure",
                    (("service_name", "multiple"), ("downstream_service", downstream)))
    semantic = dict(base.revision.semantic_content)
    semantic["events"] = [cause, effect]
    revision = EvidenceRevision.from_content(
        incident_id=base.command.incident_id,
        canonicalization_version=base.command.canonicalization_version,
        semantic_content=semantic,
    )
    payload = dict(base.snapshot.snapshot_content)
    payload["semantic_evidence"] = semantic
    payload["event_projections"] = [cause, effect]
    snapshot = EvidenceSnapshot.from_content(
        base.command, revision_id=revision.revision_id,
        completeness=base.snapshot.completeness,
        source_statuses=base.snapshot.source_statuses,
        snapshot_content=payload,
    )
    captured = CaptureSuccess(base.command, snapshot, revision)
    config = _configuration()
    source = GenerationInput(
        LogicalTryIdentity("attempt-1", 1), "operation-1", snapshot.snapshot_id,
        revision.revision_id, "knowledge-1", RetrievalResolution.NO_MATCH,
        config.pin, "approved evidence", "approved knowledge",
    )
    references = tuple(EvidenceReference(
        f"e{index + 1}", snapshot.snapshot_id, "1",
        f"/semantic_evidence/events/{index}", evidence_fact_commitment(event),
    ) for index, event in enumerate((cause, effect)))
    observed = tuple(Claim(
        f"c{index + 1}", ClaimCategory.OBSERVED_FACT,
        observed_fact_text(event), (references[index].reference_id,),
    ) for index, event in enumerate((cause, effect)))
    assertion = CausalAssertion(("c1",), ("c2",), CausalRelation.CAUSES)
    inference = Claim(
        "c3", ClaimCategory.ANALYTICAL_INFERENCE,
        "OOM in payments caused the downstream cascade", ("e1", "e2"), (), (),
        EvidentialSupport.HIGH, assertion,
    )
    hypothesis = Hypothesis(
        1, inference.text, EvidentialSupport.HIGH, ("e1", "e2"), (), (),
        inference.text, ("c3",),
    )
    content = ResultContent(
        source, "2", inference.text, ("c3",), inference.text, ("c3",),
        DiagnosticConclusion.IDENTIFIED, (hypothesis,), (), (), ("bounded Event rule",),
        EvidenceCompleteness(base.snapshot.completeness.value), True, references, (), observed + (inference,),
        (ValidationFact("schema", "2", config.result_schema_commitment),),
        InvocationMetadata(1, 10, 5, 15, 100),
    )
    reader = SimpleNamespace(
        resolve_snapshot=lambda _: captured.snapshot,
        resolve_revision=lambda _: captured.revision,
    )
    return content, config, reader


def _admission_case(phase2_case, *, content=None, config=None, reader=None, with_proof=True):
    if content is None:
        content, config, reader = _case()
    source = replace(content.input, knowledge_snapshot_id=phase2_case.snapshot.snapshot_key.value)
    inference = content.claims[-1]
    canonical_text = f"{content.claims[0].text} CAUSES {content.claims[1].text}"
    inference = replace(inference, text=canonical_text)
    hypothesis = replace(content.hypotheses[0], statement=canonical_text, reasoning_summary=canonical_text)
    content = replace(
        content, input=source, claims=content.claims[:-1] + (inference,),
        hypotheses=(hypothesis,), summary=canonical_text, severity_assessment=canonical_text,
    )
    if with_proof:
        proof = derive_causal_support_proof(content, config, reader, inference.claim_id)
        content = replace(content, causal_proofs=(proof,))
    old = phase2_case.attempts.get_attempt_lineage("attempt-1")
    generation = GenerationProvenance(
        config.pin.provider, config.pin.model, config.pin.prompt.identity,
        config.pin.configuration.identity, config.pin.profile.identity,
    )
    lineage = replace(
        old.attempt.lineage, evidence_snapshot_id=source.evidence_snapshot_id,
        evidence_revision_id=source.evidence_revision_id,
        knowledge_snapshot_id=source.knowledge_snapshot_id, generation_provenance=generation,
    )
    attempts = SimpleNamespace(get_attempt_lineage=lambda _: AttemptLineageRead(
        GenerationAttempt(lineage, GenerationLifecycle.PENDING), (),
    ))
    return content, config, attempts, reader, phase2_case.knowledge


def test_generic_field_rule_produces_stable_proof_and_lossless_projection():
    content, config, reader = _case()
    proof = derive_causal_support_proof(content, config, reader, "c3")
    assert derive_causal_support_proof(content, config, reader, "c3") == proof
    assert (proof.causal_rule_id, proof.causal_rule_version) == (CAUSAL_RULE_ID, CAUSAL_RULE_VERSION)
    assert proof.supporting_observed_claim_ids == ("c1", "c2")
    admitted = ValidatedGenerationResult.from_content(replace(content, causal_proofs=(proof,)))
    artifact = project_artifact(admitted)
    assert artifact.claim_semantics.schema_version == "2"
    assert asdict(artifact.claim_semantics.causal_assertions[0]) == {
        "inference_claim_id": "c3", "cause_claim_ids": ("c1",),
        "effect_claim_ids": ("c2",), "relation": "CAUSES",
    }
    assert asdict(artifact.claim_semantics.causal_proofs[0]) == asdict(proof)
    assert project_artifact(admitted) == artifact
    assert ValidatedGenerationResult.from_content(replace(content, causal_proofs=(replace(proof, proof_commitment="b" * 64),))).semantic_commitment != admitted.semantic_commitment
    assert ValidatedGenerationResult.from_content(replace(content, claims=content.claims[:-1] + (
        replace(content.claims[-1], causal_assertion=CausalAssertion(("c1",), ("c2",), CausalRelation.CONTRIBUTES_TO)),
    ), causal_proofs=(proof,))).semantic_commitment != admitted.semantic_commitment


@pytest.mark.parametrize("change", ["downstream", "cause_type", "effect_time", "prose"])
def test_rule_rejects_unrelated_facts_and_prose_only_support(change):
    options = {"downstream": "unrelated"} if change == "downstream" else {}
    options = {"cause_type": "other_event"} if change == "cause_type" else options
    options = {"effect_time": "2026-09-21T11:58:00Z"} if change == "effect_time" else options
    content, config, reader = _case(**options)
    if change == "prose":
        content = replace(content, claims=content.claims[:-1] + (
            replace(content.claims[-1], text="payments payments payments caused cascade"),
        ))
        # Text cannot create or strengthen proof: the same structured facts still decide it.
        assert derive_causal_support_proof(content, config, reader, "c3") == derive_causal_support_proof(_case()[0], config, reader, "c3")
    else:
        with pytest.raises(GenerationValidationError):
            derive_causal_support_proof(content, config, reader, "c3")


def test_local_claim_structure_relation_and_policy_identity_fail_closed():
    content, config, reader = _case()
    inference = content.claims[-1]
    with pytest.raises(ValueError):
        replace(content, claims=content.claims[:-1] + (
            replace(inference, causal_assertion=CausalAssertion(("missing",), ("c2",), CausalRelation.CAUSES)),
        ))
    with pytest.raises(ValueError):
        replace(content, claims=content.claims[:-1] + (
            replace(inference, causal_assertion=CausalAssertion(("c3",), ("c2",), CausalRelation.CAUSES)),
        ))
    with pytest.raises((ValueError, TypeError)):
        CausalAssertion(("c1",), ("c2",), "CORRELATED_WITH")
    changed_config = _configuration(policy_version="2")
    assert changed_config.configuration_commitment != config.configuration_commitment
    with pytest.raises(GenerationValidationError):
        derive_causal_support_proof(replace(content, input=replace(content.input, pin=changed_config.pin)), changed_config, reader, "c3")


def test_provider_cannot_supply_proof_or_ground_truth_as_authority():
    content, config, _ = _case()
    from test_llm_generation_phase2 import _structured
    raw = _structured(content, config)
    proposed = parse_structured_output(raw, content.input, config, content.invocation_metadata)
    assert proposed.claims[-1].causal_assertion == content.claims[-1].causal_assertion
    assert proposed.causal_proofs == ()
    with pytest.raises(StructuredOutputError):
        parse_structured_output({**raw, "causal_proofs": [{"causal_proof": True}]}, content.input, config, content.invocation_metadata)
    with pytest.raises(StructuredOutputError):
        parse_structured_output({**raw, "scenario_id": "S2"}, content.input, config, content.invocation_metadata)
    with pytest.raises(StructuredOutputError):
        parse_structured_output({**raw, "schema_version": "1"}, content.input, config, content.invocation_metadata)


def test_identified_requires_exact_d_proof_and_admits_field_supported_cause(phase2_case):
    content, config, attempts, evidence, knowledge = _admission_case(phase2_case)
    from test_llm_generation_phase2 import _structured
    proposed = parse_structured_output(_structured(content, config), content.input, config, content.invocation_metadata)
    assert proposed.causal_proofs == ()
    content = replace(proposed, causal_proofs=content.causal_proofs)
    admitted = validate_generation_result(content, config, attempts, evidence, knowledge)
    assert admitted.content.diagnostic_conclusion is DiagnosticConclusion.IDENTIFIED
    assert admitted.content.causal_proofs == content.causal_proofs
    projected = project_artifact(admitted)
    assert projected.claim_semantics.causal_assertions[0].relation == "CAUSES"
    assert projected.claim_semantics.causal_proofs[0].proof_commitment == content.causal_proofs[0].proof_commitment
    assert validate_generation_result(content, config, attempts, evidence, knowledge) == admitted


@pytest.mark.parametrize("change", ["missing_proof", "contributes", "rule_id", "rule_version", "proof_commitment", "config_policy", "prose_only", "overclaim"])
def test_identified_rejects_unproved_or_mismatched_semantics(phase2_case, change):
    content, config, attempts, evidence, knowledge = _admission_case(phase2_case)
    inference = content.claims[-1]
    proof = content.causal_proofs[0]
    if change == "missing_proof":
        content = replace(content, causal_proofs=())
    elif change == "contributes":
        content = replace(content, causal_proofs=(), claims=content.claims[:-1] + (
            replace(inference, causal_assertion=replace(inference.causal_assertion, relation=CausalRelation.CONTRIBUTES_TO)),
        ))
    elif change == "rule_version":
        content = replace(content, causal_proofs=(replace(proof, causal_rule_version="2"),))
    elif change == "rule_id":
        content = replace(content, causal_proofs=(replace(proof, causal_rule_id="other-rule"),))
    elif change == "proof_commitment":
        content = replace(content, causal_proofs=(replace(proof, proof_commitment="b" * 64),))
    elif change == "config_policy":
        config = _configuration(policy_version="2")
    elif change == "prose_only":
        content = replace(content, causal_proofs=(), claims=content.claims[:-1] + (
            replace(inference, causal_assertion=None),
        ))
    else:
        overclaim = "OOM caused every unrelated regional incident"
        content = replace(content, claims=content.claims[:-1] + (replace(inference, text=overclaim),),
                          hypotheses=(replace(content.hypotheses[0], statement=overclaim, reasoning_summary=overclaim),),
                          summary=overclaim, severity_assessment=overclaim)
    with pytest.raises(GenerationValidationError):
        validate_generation_result(content, config, attempts, evidence, knowledge)


def test_identified_rejects_wrong_inference_facts_and_snapshot_structurally(phase2_case):
    content, _, _, _, _ = _admission_case(phase2_case)
    proof = content.causal_proofs[0]
    with pytest.raises(ValueError):
        replace(content, causal_proofs=(replace(proof, inference_claim_id="missing"),))
    with pytest.raises(ValueError):
        replace(content, causal_proofs=(replace(proof, supporting_observed_claim_ids=("c2", "c1")),))
    with pytest.raises(ValueError):
        replace(content, causal_proofs=(replace(proof, evidence_snapshot_id="other-snapshot"),))


def test_identified_rejects_valid_proof_for_different_inference(phase2_case):
    content, config, attempts, evidence, knowledge = _admission_case(phase2_case)
    additional = replace(content.claims[-1], claim_id="c4")
    content = replace(content, claims=content.claims + (additional,), causal_proofs=())
    other_proof = derive_causal_support_proof(content, config, evidence, "c4")
    content = replace(content, causal_proofs=(other_proof,))
    with pytest.raises(GenerationValidationError):
        validate_generation_result(content, config, attempts, evidence, knowledge)


def test_identified_rejects_represented_contradiction(phase2_case):
    content, config, attempts, evidence, knowledge = _admission_case(phase2_case)
    snapshot = evidence.resolve_snapshot(content.input.evidence_snapshot_id)
    contrary = snapshot.snapshot_content["semantic_evidence"]["normalized_evidence"]["LOKI"][0]
    reference = EvidenceReference(
        "e3", snapshot.snapshot_id, "1", "/semantic_evidence/normalized_evidence/LOKI/0",
        evidence_fact_commitment(contrary),
    )
    inference = replace(content.claims[-1], contradicting_evidence_ids=("e3",))
    hypothesis = replace(content.hypotheses[0], contradicting_evidence_ids=("e3",))
    contradicted = replace(content, evidence_references=content.evidence_references + (reference,),
                          claims=content.claims[:-1] + (inference,), hypotheses=(hypothesis,))
    with pytest.raises(GenerationValidationError):
        validate_generation_result(contradicted, config, attempts, evidence, knowledge)


@pytest.mark.parametrize("change", ["downstream", "time", "degraded"])
def test_identified_rejects_wrong_event_linkage_or_incomplete_snapshot(phase2_case, change):
    options = {"downstream": "unrelated"} if change == "downstream" else {}
    options = {"effect_time": "2026-09-21T11:58:00Z"} if change == "time" else options
    options = {"completeness": BEvidenceCompleteness.DEGRADED} if change == "degraded" else options
    source, config, reader = _case(**options)
    content, config, attempts, evidence, knowledge = _admission_case(
        phase2_case, content=source, config=config, reader=reader, with_proof=False,
    )
    with pytest.raises(GenerationValidationError):
        derive_causal_support_proof(content, config, evidence, "c3")
    source_content, source_config, source_reader = _case()
    valid_proof = derive_causal_support_proof(source_content, source_config, source_reader, "c3")
    content = replace(content, causal_proofs=(replace(
        valid_proof, evidence_snapshot_id=content.input.evidence_snapshot_id,
    ),))
    with pytest.raises(GenerationValidationError):
        validate_generation_result(content, config, attempts, evidence, knowledge)


def test_most_supported_and_inconclusive_do_not_require_causal_proof(phase2_case):
    content, config, attempts, evidence, knowledge = _admission_case(phase2_case, with_proof=False)
    inference = replace(content.claims[-1], evidential_support=EvidentialSupport.MEDIUM,
                        causal_assertion=replace(content.claims[-1].causal_assertion,
                                                 relation=CausalRelation.CONTRIBUTES_TO))
    most_supported = replace(
        content, claims=content.claims[:-1] + (inference,),
        hypotheses=(replace(content.hypotheses[0], evidential_support=EvidentialSupport.MEDIUM),),
        diagnostic_conclusion=DiagnosticConclusion.MOST_SUPPORTED,
    )
    assert validate_generation_result(most_supported, config, attempts, evidence, knowledge).content.diagnostic_conclusion is DiagnosticConclusion.MOST_SUPPORTED
    assert validate_generation_result(phase2_case.content, phase2_case.config, phase2_case.attempts,
                                      phase2_case.evidence, phase2_case.knowledge).content.diagnostic_conclusion is DiagnosticConclusion.INCONCLUSIVE
