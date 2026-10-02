"""Focused S3 authority, denominator, calibration, and adjudication tests."""

import base64
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from incident_management.contracts import IncidentStatus
from knowledge_index.contracts import (
    KnowledgeReadResult, KnowledgeReadStatus, KnowledgeSnapshotKey, RetrievalResolution,
)
from llm_generation.contracts import (
    CausalAssertion, CausalRelation, Claim, EvidenceReference, GenerationInput, GenerationPin, Hypothesis,
    InvocationMetadata, LocalReadStatus, ResultContent, ValidatedGenerationResult,
    ValidationFact, VersionedIdentity,
)
from rca_persistence.contracts import (
    DiagnosticConclusion, EvidenceCompleteness, EvidentialSupport,
    LogicalTryIdentity, LogicalTryResultKind,
)
from rca_shared.claim_types import ClaimCategory

from rca_evaluation.adjudication import (
    AdmittedBlindPacket, BlindPacket, BlindReviewSubmission, FactDetermination,
    ReviewedFactKind, ReviewerAuthority, ReviewIncomplete, Stage1ReviewedFact,
    Stage2Submission, TwoStageAdjudicator, stage2_input_commitment,
)
from rca_evaluation.authority import PM_AUTHORITY, TrustedApprovalRegistry, TrustedOracleApproval
from rca_evaluation.calibration import ConclusionFacts, KnowledgeBoundaryInput
from rca_evaluation.claims import (
    AtomicClaimFinding, ClaimDisposition, ClaimEvaluationStatus,
    GovernedClaimDecomposition, evaluate_unsupported_claims,
    production_claim_commitment,
)
from rca_evaluation.identity import commitment, identity
from rca_evaluation.ledger import LedgerRecord
from rca_evaluation.leakage_policy import TRUSTED_LEAKAGE_POLICY, TrustedLeakagePolicy
from rca_evaluation.observation import CapturedInputBoundary, PublicObservationResolver
from rca_evaluation.oracle import EvaluationPlan, OracleRevision
from rca_evaluation.scoring import (
    CausalClass, ExecutionScorability, OracleScenarioCriteria,
    OverclaimFinding, TopHypothesis, evaluate_root_cause,
)
from rca_evaluation.source_authority import S3SourceAuthority
from rca_evaluation.sqlite_store import SqliteEvaluationStore


RULES = {
    "S1": (CausalClass.CREDENTIAL_ABUSE_BRUTE_FORCE, "credential-operation", ("brute_force_detected", "same_source", "same_user", "repeated_401"), "account_compromised"),
    "S2": (CausalClass.DATABASE_LATENCY_CASCADE, "dependency-service-chain", ("cross_service_failure", "shared_trace", "core_db", "db_ap_gateway", "high_latency"), "specific_sql"),
    "S3": (CausalClass.MEMORY_EXHAUSTION_SERVICE_FAILURE, "service-oom-failure", ("oom_crash_detected", "out_of_memory_error", "high_memory", "service_failure"), "memory_leak"),
    "S4": (CausalClass.EXTERNAL_DEPENDENCY_FAILURE, "external-request-failure", ("external_dependency_failure", "external_service", "transaction_lineage", "failure_evidence"), "global_outage"),
    "S5": (CausalClass.SHARED_DATABASE_CONNECTIVITY_CASCADE, "shared-connectivity-multiservice", ("downstream_cascade_failure", "multiple_services", "core_db", "connection_refused", "independent_traces"), "firewall_change"),
    "S6": (CausalClass.REQUEST_SURGE_RATE_LIMIT, "service-surge-throttling", ("rate_limit_storm", "target_service", "repeated_429", "quota_context", "request_spike"), "ddos"),
}


def oracle_semantics():
    scenarios = {}
    for scenario_id, (causal_class, granularity, evidence, forbidden) in RULES.items():
        scenarios[scenario_id] = {
            "O1": {"causal_class": causal_class.value},
            "O2": {"accepted_granularities": [granularity]},
            "O3": {"accepted_alternatives": [f"{scenario_id}-approved-alternative"]},
            "O4": {"material_forbidden_overclaims": [forbidden]},
            "O5": {"required_evidence_classes": list(evidence)},
            "O6": {"knowledge_expected": "MATCH"},
            "O7": {
                "required_evidence_classes": list(evidence[:-1]),
                "allowed_conclusions": ["MOST_SUPPORTED", "INCONCLUSIVE"],
            },
            "O8": {"unacceptable": ["wrong-causal-class"]},
            "O9": {"refresh": "material-change-only"},
            "O10": {"top_hypothesis_required": True},
        }
    return {
        "scenarios": scenarios,
        "cross_scenario_rules": ["S2 latency is distinct from S5 connectivity"],
    }


def approved_oracle():
    return OracleRevision.approved(
        oracle_set_id="PM-S1-S6", semantics=oracle_semantics(),
        created_at="2026-10-02T00:00:00Z", approval_authority=PM_AUTHORITY,
        approval_reference="PM-017-S3", approved_at="2026-10-02T01:00:00Z",
    )


def trusted(revision):
    return TrustedApprovalRegistry((TrustedOracleApproval(
        revision.approval_reference, PM_AUTHORITY, revision.oracle_revision_id,
        revision.semantic_content_commitment, revision.approved_at,
        revision.parent_revision_id,
    ),))


def conclusion(kind=DiagnosticConclusion.IDENTIFIED, *, complete=True, leading=True,
               contradiction=False, competing=False, grounded=True):
    return ConclusionFacts(kind, grounded, complete, leading, contradiction, competing)


def knowledge(resolution=RetrievalResolution.MATCH, *, gap=False):
    return KnowledgeBoundaryInput(resolution, gap, False, False, False)


def score(revision, scenario_id, **overrides):
    causal_class, granularity, evidence, _ = RULES[scenario_id]
    values = {
        "criteria": OracleScenarioCriteria.from_revision(revision, scenario_id),
        "top_hypothesis": TopHypothesis(causal_class, granularity),
        "lower_ranked_causal_classes": (), "observed_evidence_classes": evidence,
        "detected_overclaim_ids": (), "evidence_completeness": EvidenceCompleteness.FULL,
        "conclusion_facts": conclusion(), "knowledge_facts": knowledge(),
        "claim_status": ClaimEvaluationStatus.SCORED,
    }
    values.update(overrides)
    return evaluate_root_cause(**values)


@pytest.mark.parametrize("scenario_id", tuple(RULES))
def test_all_six_canonical_causal_classes_pass(scenario_id):
    assert score(approved_oracle(), scenario_id).root_cause_identification_success


def test_noncompensating_root_cause_and_cross_scenario_rules():
    revision = approved_oracle()
    criteria = OracleScenarioCriteria.from_revision(revision, "S2")
    alternative = score(
        revision, "S2", top_hypothesis=TopHypothesis(
            CausalClass.OTHER, RULES["S2"][1], "S2-approved-alternative",
        ),
    )
    assert alternative.root_cause_identification_success
    wrong = score(
        revision, "S2", top_hypothesis=TopHypothesis(CausalClass.OTHER, RULES["S2"][1]),
        lower_ranked_causal_classes=(criteria.expected_causal_class,),
    )
    assert not wrong.root_cause_identification_success
    confused = score(
        revision, "S2", top_hypothesis=TopHypothesis(
            CausalClass.SHARED_DATABASE_CONNECTIVITY_CASCADE, RULES["S5"][1],
        ), observed_evidence_classes=RULES["S5"][2],
    )
    assert not confused.root_cause_identification_success
    assert not score(
        revision, "S1", top_hypothesis=TopHypothesis(RULES["S1"][0], "too-coarse"),
    ).root_cause_identification_success
    assert not score(
        revision, "S1", observed_evidence_classes=RULES["S1"][2][:-1],
    ).required_evidence_satisfied
    overclaim = score(revision, "S1", detected_overclaim_ids=(RULES["S1"][3],))
    assert overclaim.forbidden_overclaim_detected is OverclaimFinding.MATERIAL
    assert not overclaim.root_cause_identification_success
    assert not score(
        revision, "S1", scorability=ExecutionScorability.NOT_EVALUABLE,
    ).root_cause_identification_success


def test_degraded_calibration_and_no_match_are_oracle_driven():
    revision = approved_oracle()
    degraded = score(
        revision, "S1", evidence_completeness=EvidenceCompleteness.DEGRADED,
        observed_evidence_classes=RULES["S1"][2][:-1],
        conclusion_facts=conclusion(DiagnosticConclusion.MOST_SUPPORTED, complete=False),
    )
    assert degraded.conclusion_calibration_valid and degraded.root_cause_identification_success
    disallowed = score(
        revision, "S1", evidence_completeness=EvidenceCompleteness.DEGRADED,
        observed_evidence_classes=RULES["S1"][2][:-1],
        conclusion_facts=conclusion(DiagnosticConclusion.IDENTIFIED),
    )
    assert not disallowed.conclusion_calibration_valid
    full_most = score(
        revision, "S1",
        conclusion_facts=conclusion(DiagnosticConclusion.MOST_SUPPORTED, complete=False),
    )
    assert full_most.conclusion_calibration_valid
    no_match = knowledge(RetrievalResolution.NO_MATCH, gap=True)
    assert score(revision, "S4", knowledge_facts=no_match).root_cause_identification_success
    assert not score(
        revision, "S4", knowledge_facts=replace(no_match, fabricated_reference=True),
    ).knowledge_boundary_valid


def typed_claims():
    return (
        Claim("C1", ClaimCategory.OBSERVED_FACT, "Metric spike", ("E1",)),
        Claim("C2", ClaimCategory.ANALYTICAL_INFERENCE, "Latency caused failure",
              ("E2",), (), (), EvidentialSupport.HIGH),
        Claim("G1", ClaimCategory.MODEL_SUGGESTED_GUIDANCE, "Inspect deployment"),
    )


def ucr_inputs():
    claims = typed_claims()
    atoms = tuple(commitment({"atomic": value}) for value in (
        "metric-spike", "latency", "caused-failure", "inspect-deployment",
    ))
    decomposition = (
        GovernedClaimDecomposition("C1", production_claim_commitment(claims[0]), (atoms[0],)),
        GovernedClaimDecomposition("C2", production_claim_commitment(claims[1]), atoms[1:3]),
        GovernedClaimDecomposition("G1", production_claim_commitment(claims[2]), (atoms[3],)),
    )
    findings = (
        AtomicClaimFinding("C1", atoms[0], ClaimDisposition.GROUNDED),
        AtomicClaimFinding("C2", atoms[1], ClaimDisposition.UNSUPPORTED),
        AtomicClaimFinding("C2", atoms[2], ClaimDisposition.CONTRADICTED),
        AtomicClaimFinding("G1", atoms[3], ClaimDisposition.NOT_APPLICABLE),
    )
    return claims, decomposition, atoms[:3], findings


def test_ucr_complete_dispositions_and_advisory_exclusion():
    result = evaluate_unsupported_claims(*ucr_inputs())
    assert result.status is ClaimEvaluationStatus.SCORED
    assert (result.numerator, result.denominator, result.rate) == (2, 3, 2 / 3)
    guidance = typed_claims()[-1]
    atom = commitment({"atom": "only-guidance"})
    zero = evaluate_unsupported_claims(
        (guidance,),
        (GovernedClaimDecomposition("G1", production_claim_commitment(guidance), (atom,)),),
        (), (AtomicClaimFinding("G1", atom, ClaimDisposition.NOT_APPLICABLE),),
    )
    assert zero.status is ClaimEvaluationStatus.NO_ELIGIBLE_CLAIMS
    assert zero.denominator == 0 and zero.rate is None


@pytest.mark.parametrize("claim_index", (0, 1))
def test_eligible_factual_or_causal_atom_cannot_escape_denominator(claim_index):
    claims, decomposition, assertions, findings = ucr_inputs()
    findings = list(findings)
    finding_index = 0 if claim_index == 0 else 1
    findings[finding_index] = replace(
        findings[finding_index], disposition=ClaimDisposition.NOT_APPLICABLE,
    )
    result = evaluate_unsupported_claims(claims, decomposition, assertions, tuple(findings))
    assert result.status is ClaimEvaluationStatus.INVALID_OUTPUT
    assert "ELIGIBLE_ATOM_CANNOT_BE_NOT_APPLICABLE" in result.integrity_errors


def test_authoritative_causal_assertion_cannot_be_marked_not_applicable():
    observed_cause = Claim("CAUSE", ClaimCategory.OBSERVED_FACT, "Cause fact", ("E1",))
    observed_effect = Claim("EFFECT", ClaimCategory.OBSERVED_FACT, "Effect fact", ("E2",))
    causal = Claim(
        "CAUSAL", ClaimCategory.ANALYTICAL_INFERENCE, "Cause produced effect",
        ("E1", "E2"), (), (), EvidentialSupport.HIGH,
        CausalAssertion(("CAUSE",), ("EFFECT",), CausalRelation.CAUSES),
    )
    claims = (observed_cause, observed_effect, causal)
    atoms = tuple(commitment({"causal-test": item.claim_id}) for item in claims)
    decomposition = tuple(
        GovernedClaimDecomposition(item.claim_id, production_claim_commitment(item), (atom,))
        for item, atom in zip(claims, atoms)
    )
    findings = tuple(
        AtomicClaimFinding(item.claim_id, atom, (
            ClaimDisposition.NOT_APPLICABLE if item is causal else ClaimDisposition.GROUNDED
        )) for item, atom in zip(claims, atoms)
    )
    result = evaluate_unsupported_claims(claims, decomposition, atoms, findings)
    assert result.status is ClaimEvaluationStatus.INVALID_OUTPUT
    assert "ELIGIBLE_ATOM_CANNOT_BE_NOT_APPLICABLE" in result.integrity_errors


def test_ambiguity_missing_extra_and_duplicate_handling_fail_closed():
    claims, decomposition, assertions, findings = ucr_inputs()
    missing = evaluate_unsupported_claims(claims, decomposition, assertions, findings[:-2] + findings[-1:])
    assert missing.status is ClaimEvaluationStatus.INVALID_OUTPUT
    extra = evaluate_unsupported_claims(
        claims, decomposition, assertions,
        (*findings, AtomicClaimFinding("C1", commitment({"extra": 1}), ClaimDisposition.GROUNDED)),
    )
    assert extra.status is ClaimEvaluationStatus.INVALID_OUTPUT
    duplicate = evaluate_unsupported_claims(claims, decomposition, assertions, (*findings, findings[0]))
    assert duplicate.status is ClaimEvaluationStatus.INVALID_OUTPUT


def generation_result(*, contradiction=False, competing=False, answer_text=None,
                      support=EvidentialSupport.HIGH,
                      diagnostic=DiagnosticConclusion.IDENTIFIED,
                      completeness=EvidenceCompleteness.FULL):
    pin = GenerationPin(
        "provider", "model", VersionedIdentity("profile", "1"),
        VersionedIdentity("prompt", "1"), VersionedIdentity("schema", "1"),
        VersionedIdentity("config", "1"),
    )
    source = GenerationInput(
        LogicalTryIdentity("ATT-1", 1), "OP-1", "ES-1", "ER-1", "KS-1",
        RetrievalResolution.MATCH, pin, "evidence projection", "knowledge projection",
    )
    reference = EvidenceReference("E1", "ES-1", "1", "/events/0", "a" * 64)
    text = answer_text or CausalClass.CREDENTIAL_ABUSE_BRUTE_FORCE.value
    claim = Claim("C1", ClaimCategory.OBSERVED_FACT, text, ("E1",))
    top = Hypothesis(
        1, text, support, ("E1",), ("E1",) if contradiction else (),
        (), "evidence-derived analysis", ("C1",),
    )
    hypotheses = (top,)
    if competing:
        hypotheses += (Hypothesis(
            2, "Alternative production hypothesis", EvidentialSupport.HIGH,
            ("E1",), (), (), "alternative analysis", ("C1",),
        ),)
    content = ResultContent(
        source, "1", text, ("C1",), "High", ("C1",),
        diagnostic, hypotheses, (), (), ("Bounded evidence",),
        completeness, False, (reference,), (), (claim,),
        (ValidationFact("schema", "1", "b" * 64),),
        InvocationMetadata(1, 10, 5, 15, 100),
    )
    return ValidatedGenerationResult.from_content(content)


class IncidentReads:
    def __init__(self):
        self.incident = SimpleNamespace(incident_id="INC-1", event_ids=("EVT-1",))

    def event_has_incident_owner(self, event_id):
        return event_id == "EVT-1"

    def list_incidents_by_workflow_status(self, status):
        return (self.incident,) if status is IncidentStatus.OPEN else ()

    def get_incident(self, incident_id):
        return self.incident if incident_id == "INC-1" else None

    def get_rca_relationship(self, incident_id):
        return SimpleNamespace(incident_id="INC-1", current_version_id="VER-1")


class RcaReads:
    def __init__(self, result):
        lineage = SimpleNamespace(
            attempt_id="ATT-1", aggregate_id="AGG-1", evidence_snapshot_id="ES-1",
            evidence_revision_id="ER-1", knowledge_snapshot_id="KS-1",
        )
        outcome = SimpleNamespace(
            identity=LogicalTryIdentity("ATT-1", 1),
            result_kind=LogicalTryResultKind.VALIDATED_RESULT,
            validated_result_id=result.validated_result_id,
        )
        self.aggregate = SimpleNamespace(aggregate_id="AGG-1", incident_id="INC-1")
        self.version = SimpleNamespace(version_id="VER-1", aggregate_id="AGG-1", attempt_id="ATT-1")
        self.current = SimpleNamespace(current=SimpleNamespace(aggregate_id="AGG-1"), version=self.version)
        self.provenance = SimpleNamespace(
            evidence_snapshot_id="ES-1", evidence_revision_id="ER-1", knowledge_snapshot_id="KS-1",
        )
        self.lineage = SimpleNamespace(attempt=SimpleNamespace(lineage=lineage), try_outcomes=(outcome,))

    def get_aggregate_by_incident(self, incident_id):
        return self.aggregate

    def get_current(self, aggregate_id):
        return self.current

    def get_version_history(self, aggregate_id):
        return (self.version,)

    def get_artifact_provenance(self, version_id):
        return self.provenance

    def get_attempt_lineage(self, attempt_id):
        return self.lineage


class EvidenceReads:
    def resolve_snapshot(self, snapshot_id):
        return SimpleNamespace(
            snapshot_id="ES-1", revision_id="ER-1", incident_id="INC-1",
            canonical_snapshot_content='{"events":["E1"]}',
        )

    def resolve_revision(self, revision_id):
        return SimpleNamespace(revision_id="ER-1", incident_id="INC-1", integrity_identity="ER-1-integrity")


class KnowledgeReads:
    def __init__(self):
        self.snapshot_commitment = commitment({"knowledge": "KS-1"})
        self.snapshot = SimpleNamespace(
            snapshot_key=KnowledgeSnapshotKey("KS-1"),
            snapshot_commitment=self.snapshot_commitment,
            resolution=RetrievalResolution.MATCH, knowledge_gap=False,
        )

    def read_snapshot(self, key):
        return KnowledgeReadResult(KnowledgeReadStatus.FOUND, self.snapshot)

    def read_provenance(self, key):
        return KnowledgeReadResult(KnowledgeReadStatus.FOUND, SimpleNamespace(
            snapshot_identity=KnowledgeSnapshotKey("KS-1"),
            snapshot_commitment=self.snapshot_commitment,
        ))


class GenerationReads:
    def __init__(self, result):
        self.value = result

    def result(self, result_id):
        if result_id != self.value.validated_result_id:
            return SimpleNamespace(status=LocalReadStatus.NOT_FOUND, value=None)
        return SimpleNamespace(status=LocalReadStatus.FOUND, value=self.value)


@pytest.fixture
def adjudication_context(tmp_path, request):
    result = getattr(request, "param", None) or generation_result()
    revision = approved_oracle()
    store = SqliteEvaluationStore(
        tmp_path / "rca_evaluation" / "evaluation.db", approvals=trusted(revision),
    )
    store.admit_oracle(revision)
    plan = EvaluationPlan.admit(
        revision, fixture_revision="fixture-r1", production_build="build-a",
        model="model-a", prompt="prompt-a", schema="schema-a",
        corpus_index="index-a", configuration="config-a", repetitions=2,
    )
    store.admit_plan(plan)
    run = store.append(LedgerRecord.create(
        "evaluation_run", [plan.evaluation_plan_id, "run"],
        {"oracle_revision_id": revision.oracle_revision_id}, plan.evaluation_plan_id,
    ))
    execution = store.append(LedgerRecord.create(
        "execution", [run.record_id, "S1", 1],
        {"scenario_id": "S1", "repetition": 1}, run.record_id,
    ))
    captured = CapturedInputBoundary(execution.record_id, "EVT-1", commitment({"event": "EVT-1"}))
    rca = RcaReads(result)
    knowledge_reads = KnowledgeReads()
    generation_reads = GenerationReads(result)
    observations = PublicObservationResolver(
        IncidentReads(), rca, EvidenceReads(), knowledge_reads, generation_reads,
    )
    authority = S3SourceAuthority(observations, generation_reads, knowledge_reads)
    adjudicator = TwoStageAdjudicator(
        store, authority, TRUSTED_LEAKAGE_POLICY,
        expected_policy_commitment=TRUSTED_LEAKAGE_POLICY.policy_commitment,
    )
    try:
        yield SimpleNamespace(
            revision=revision, store=store, execution=execution, captured=captured,
            result=result, authority=authority, adjudicator=adjudicator,
        )
    finally:
        store.close()


def source_facts(context):
    return context.authority.resolve(context.captured)


def admit(context, **overrides):
    source = source_facts(context)
    values = {
        "expected_production_output_commitment": source.production_output_commitment,
        "expected_typed_claims_commitment": source.typed_claims_commitment,
    }
    values.update(overrides)
    return context.adjudicator.admit_blind_packet(context.captured, **values)


def reviewed_facts(source, **values):
    settings = {
        ReviewedFactKind.OPERATIONAL_CAUSE_GROUNDED: True,
        ReviewedFactKind.COMPLETE_CAUSAL_CHAIN: True,
        ReviewedFactKind.MATERIAL_CONTRADICTION: False,
        ReviewedFactKind.COMPETING_ALTERNATIVE: False,
        ReviewedFactKind.KNOWLEDGE_INCIDENT_TRUTH_DEPENDENCY: False,
    }
    settings.update(values)
    facts = []
    for kind, result in settings.items():
        identity_value = source.knowledge_snapshot_id if kind is ReviewedFactKind.KNOWLEDGE_INCIDENT_TRUTH_DEPENDENCY else source.validated_result_id
        commitment_value = source.knowledge_commitment if kind is ReviewedFactKind.KNOWLEDGE_INCIDENT_TRUTH_DEPENDENCY else source.production_output_commitment
        facts.append(Stage1ReviewedFact(
            kind, "conclusion", FactDetermination.TRUE if result else FactDetermination.FALSE,
            identity_value, commitment_value,
        ))
    for evidence_class in RULES["S1"][2]:
        facts.append(Stage1ReviewedFact(
            ReviewedFactKind.EVIDENCE_CLASS_PRESENT, evidence_class,
            FactDetermination.TRUE, source.evidence_snapshot_id, source.evidence_commitment,
        ))
    return tuple(sorted(facts, key=lambda item: (item.kind.value, item.subject_id)))


def blind(reviewer, admitted, facts):
    return BlindReviewSubmission(
        reviewer, "semantic-facts", admitted.packet_commitment, facts,
        "BLIND_SEMANTIC_REVIEW", "Reviewed only admitted production and public facts",
        "2026-10-02T02:00:00Z",
    )


def consensus(context, facts=None):
    admitted = admit(context)
    source = source_facts(context)
    facts = facts or reviewed_facts(source)
    result = context.adjudicator.record_stage1(
        context.execution.record_id, admitted,
        (blind("R1", admitted, facts), blind("R2", admitted, facts)),
    )
    return admitted, result


def stage2_pair(context, criteria, stage1, top, derived):
    bound = stage2_input_commitment(criteria, stage1, derived)
    def item(reviewer):
        return Stage2Submission(
            reviewer, bound, context.revision.oracle_revision_id, derived, top,
            "CRITERIA_APPLICATION", "Applied exact pinned criteria",
            "2026-10-02T03:00:00Z",
        )
    return item("R3"), item("R4")


def persist_packet_without_admission(context, metadata, policy=TRUSTED_LEAKAGE_POLICY):
    source = source_facts(context)
    packet = BlindPacket(
        context.captured, source, metadata, policy.policy_identity, policy.policy_commitment,
    )
    record = context.store.append(LedgerRecord.create(
        "observation", [context.execution.record_id, "S3_BLIND_PACKET"], {
            "kind": "BLIND_PACKET", "packet_commitment": packet.packet_commitment,
            "source_commitment": source.source_commitment,
            "isolation_policy_commitment": policy.policy_commitment,
            "packet": packet.material(),
        }, context.execution.record_id,
    ))
    return AdmittedBlindPacket(
        record.record_id, context.execution.record_id, packet.packet_commitment,
        source.source_commitment, policy.policy_commitment,
    )


def test_blind_packet_commitments_are_resolved_from_public_authority(adjudication_context):
    context = adjudication_context
    admitted = admit(context)
    assert context.store.get(admitted.record_id) is not None
    with pytest.raises(ValueError, match="production output commitment"):
        admit(context, expected_production_output_commitment="0" * 64)
    with pytest.raises(ValueError, match="typed claims commitment"):
        admit(context, expected_typed_claims_commitment=commitment({"forged": "claims"}))
    other = generation_result(answer_text="Unrelated production answer")
    with pytest.raises(ValueError, match="production output commitment"):
        admit(context, expected_production_output_commitment=other.semantic_commitment)


def test_oracle_and_canary_leakage_rejected_but_production_answer_allowed(adjudication_context):
    context = adjudication_context
    source = source_facts(context)
    assert CausalClass.CREDENTIAL_ABUSE_BRUTE_FORCE.value in context.result.content.summary
    assert admit(context).source_commitment == source.source_commitment
    with pytest.raises(ValueError, match="Oracle provenance"):
        admit(context, audit_metadata=(("expected_causal_class", RULES["S1"][0].value),))
    with pytest.raises(ValueError, match="Oracle provenance"):
        admit(context, audit_metadata=(("accepted_alternative", "S1-approved-alternative"),))
    encoded = base64.b64encode(b"GT-CANARY-017").decode()
    with pytest.raises(ValueError, match="governed Oracle value or canary"):
        admit(context, audit_metadata=(("note", encoded),))


@pytest.mark.parametrize("leaked", (
    CausalClass.CREDENTIAL_ABUSE_BRUTE_FORCE.value,
    "credential_abuse_brute_force",
    "CrEdEnTiAl_AbUsE_BrUtE_FoRcE",
    "ＣＲＥＤＥＮＴＩＡＬ＿ＡＢＵＳＥ＿ＢＲＵＴＥ＿ＦＯＲＣＥ",
    "s1-APPROVED-alternative",
    base64.b64encode(CausalClass.CREDENTIAL_ABUSE_BRUTE_FORCE.value.encode()).decode(),
    base64.b64encode(b"credential_abuse_brute_force").decode(),
))
def test_governed_value_canonical_variants_are_rejected(adjudication_context, leaked):
    with pytest.raises(ValueError, match="governed Oracle value or canary"):
        admit(adjudication_context, audit_metadata=(("review_note", leaked),))


def test_trusted_canary_policy_is_required_committed_and_not_replaceable(adjudication_context):
    context = adjudication_context
    with pytest.raises(TypeError, match="trusted leakage isolation policy"):
        TwoStageAdjudicator(
            context.store, context.authority, None,
            expected_policy_commitment=TRUSTED_LEAKAGE_POLICY.policy_commitment,
        )
    with pytest.raises(ValueError, match="non-empty"):
        TrustedLeakagePolicy(
            "SPEC-017-S3-BLIND-ISOLATION-v1", (),
            "UNICODE-NFKC-CASEFOLD-ALNUM-v1",
        )
    with pytest.raises(ValueError, match="commitment mismatch"):
        TwoStageAdjudicator(
            context.store, context.authority, TRUSTED_LEAKAGE_POLICY,
            expected_policy_commitment=commitment({"wrong": "policy"}),
        )
    replacement = TrustedLeakagePolicy(
        TRUSTED_LEAKAGE_POLICY.policy_identity, ("CALLER-REPLACEMENT",),
        TRUSTED_LEAKAGE_POLICY.normalization_version,
    )
    with pytest.raises(ValueError, match="replacement leakage policy"):
        TwoStageAdjudicator(
            context.store, context.authority, replacement,
            expected_policy_commitment=replacement.policy_commitment,
        )


def test_exact_and_encoded_trusted_canaries_are_rejected(adjudication_context):
    for canary in (
        TRUSTED_LEAKAGE_POLICY.required_canaries[0],
        base64.b64encode(TRUSTED_LEAKAGE_POLICY.required_canaries[1].encode()).decode(),
    ):
        with pytest.raises(ValueError, match="governed Oracle value or canary"):
            admit(adjudication_context, audit_metadata=(("review_note", canary),))


@pytest.mark.parametrize("leaked", (
    CausalClass.CREDENTIAL_ABUSE_BRUTE_FORCE.value,
    "cReDeNtIaL_aBuSe_bRuTe_FoRcE",
    "S1-approved-ALTERNATIVE",
    base64.b64encode(CausalClass.CREDENTIAL_ABUSE_BRUTE_FORCE.value.encode()).decode(),
))
def test_durable_reload_revalidates_governed_leakage(adjudication_context, leaked):
    context = adjudication_context
    admitted = persist_packet_without_admission(context, (("review_note", leaked),))
    facts = reviewed_facts(source_facts(context))
    with pytest.raises(ValueError, match="governed Oracle value or canary"):
        context.adjudicator.record_stage1(
            context.execution.record_id, admitted,
            (blind("R1", admitted, facts), blind("R2", admitted, facts)),
        )


def test_durable_reload_accepts_valid_packet_and_rejects_policy_mismatch(adjudication_context):
    context = adjudication_context
    admitted = admit(context, audit_metadata=(("review_scope", "production-evidence"),))
    facts = reviewed_facts(source_facts(context))
    consensus_record = context.adjudicator.record_stage1(
        context.execution.record_id, admitted,
        (blind("R1", admitted, facts), blind("R2", admitted, facts)),
    )
    assert context.store.get(consensus_record.consensus_record_id) is not None


def test_durable_reload_rejects_persisted_policy_mismatch(adjudication_context):
    context = adjudication_context
    replacement = TrustedLeakagePolicy(
        "SPEC-017-S3-BLIND-ISOLATION-old", ("OLD-CANARY",),
        TRUSTED_LEAKAGE_POLICY.normalization_version,
    )
    admitted = persist_packet_without_admission(context, (), replacement)
    facts = reviewed_facts(source_facts(context))
    with pytest.raises(ValueError, match="policy mismatch"):
        context.adjudicator.record_stage1(
            context.execution.record_id, admitted,
            (blind("R1", admitted, facts), blind("R2", admitted, facts)),
        )


@pytest.mark.parametrize("kind", (
    ReviewedFactKind.OPERATIONAL_CAUSE_GROUNDED,
    ReviewedFactKind.COMPLETE_CAUSAL_CHAIN,
))
def test_stage1_rejects_forged_grounded_or_complete_fact(adjudication_context, kind):
    context = adjudication_context
    admitted = admit(context)
    source = source_facts(context)
    facts = list(reviewed_facts(source))
    target = next(i for i, item in enumerate(facts) if item.kind is kind)
    facts[target] = replace(facts[target], source_commitment="0" * 64)
    forged = tuple(facts)
    with pytest.raises(ValueError, match="source lineage"):
        context.adjudicator.record_stage1(
            context.execution.record_id, admitted,
            (blind("R1", admitted, forged), blind("R2", admitted, forged)),
        )


def test_stage1_rejects_omitted_competing_alternative_fact(adjudication_context):
    context = adjudication_context
    admitted = admit(context)
    source = source_facts(context)
    incomplete = tuple(item for item in reviewed_facts(source)
                       if item.kind is not ReviewedFactKind.COMPETING_ALTERNATIVE)
    with pytest.raises(ValueError, match="mandatory conclusion facts"):
        context.adjudicator.record_stage1(
            context.execution.record_id, admitted,
            (blind("R3", admitted, incomplete), blind("R4", admitted, incomplete)),
        )


@pytest.mark.parametrize("adjudication_context", (
    generation_result(contradiction=True), generation_result(competing=True),
), indirect=True)
def test_stage1_cannot_hide_authoritative_contradiction_or_competitor(adjudication_context):
    context = adjudication_context
    admitted = admit(context)
    facts = reviewed_facts(source_facts(context))
    with pytest.raises(ValueError, match="contradicts production authority"):
        context.adjudicator.record_stage1(
            context.execution.record_id, admitted,
            (blind("R1", admitted, facts), blind("R2", admitted, facts)),
        )


def test_leading_hypothesis_is_machine_derived_and_not_caller_fact(adjudication_context):
    context = adjudication_context
    admitted, stage1 = consensus(context)
    derived = context.adjudicator.derive_root_cause_facts(stage1)
    assert derived.conclusion.leading_hypothesis
    with pytest.raises(ValueError):
        ReviewedFactKind("LEADING_HYPOTHESIS")
    assert admitted.source_commitment == source_facts(context).source_commitment


@pytest.mark.parametrize("adjudication_context", (
    generation_result(
        support=EvidentialSupport.LOW,
        diagnostic=DiagnosticConclusion.MOST_SUPPORTED,
        completeness=EvidenceCompleteness.DEGRADED,
    ),
), indirect=True)
def test_unsupported_leading_hypothesis_is_rejected(adjudication_context):
    context = adjudication_context
    _, stage1 = consensus(context)
    criteria = context.adjudicator.resolve_authoritative_criteria(context.execution.record_id)
    derived = context.adjudicator.derive_root_cause_facts(stage1)
    assert not derived.conclusion.leading_hypothesis
    top = TopHypothesis(RULES["S1"][0], RULES["S1"][1])
    official = context.adjudicator.finalize_stage2(
        context.execution.record_id, criteria, stage1,
        stage2_pair(context, criteria, stage1, top, derived.facts_commitment),
    )
    assert not official.evaluation.conclusion_calibration_valid
    assert not official.evaluation.root_cause_identification_success


def test_factual_claim_cannot_escape_via_false_advisory_classification():
    factual = Claim("C1", ClaimCategory.OBSERVED_FACT, "Observed outage", ("E1",))
    forged_advisory = Claim("C1", ClaimCategory.MODEL_SUGGESTED_GUIDANCE, "Observed outage")
    atom = commitment({"atomic": "observed-outage"})
    result = evaluate_unsupported_claims(
        (factual,),
        (GovernedClaimDecomposition(
            "C1", production_claim_commitment(forged_advisory), (atom,),
        ),),
        (atom,),
        (AtomicClaimFinding("C1", atom, ClaimDisposition.NOT_APPLICABLE),),
    )
    assert result.status is ClaimEvaluationStatus.INVALID_OUTPUT
    assert "PRODUCTION_CLAIM_COMMITMENT_MISMATCH" in result.integrity_errors


def test_wrong_execution_blind_packet_lineage_is_rejected(adjudication_context):
    context = adjudication_context
    admitted = admit(context)
    facts = reviewed_facts(source_facts(context))
    other_execution = identity("execution", ["other-execution"])
    with pytest.raises(ValueError, match="another execution"):
        context.adjudicator.record_stage1(
            other_execution, admitted,
            (blind("R1", admitted, facts), blind("R2", admitted, facts)),
        )


def test_arbitrary_packet_and_stage1_consensus_mismatch_rejected(adjudication_context):
    context = adjudication_context
    fake = AdmittedBlindPacket(
        identity("observation", [context.execution.record_id, "fake"]),
        context.execution.record_id, commitment({"fake": "packet"}),
        commitment({"fake": "source"}), TRUSTED_LEAKAGE_POLICY.policy_commitment,
    )
    facts = reviewed_facts(source_facts(context))
    with pytest.raises(ValueError, match="unresolved"):
        context.adjudicator.record_stage1(
            context.execution.record_id, fake,
            (blind("R1", fake, facts), blind("R2", fake, facts)),
        )
    _, stage1 = consensus(context)
    criteria = context.adjudicator.resolve_authoritative_criteria(context.execution.record_id)
    derived = context.adjudicator.derive_root_cause_facts(stage1)
    top = TopHypothesis(RULES["S1"][0], RULES["S1"][1])
    tampered = replace(stage1, consensus_facts_commitment=commitment({"tampered": 1}))
    with pytest.raises(ValueError, match="consensus"):
        context.adjudicator.finalize_stage2(
            context.execution.record_id, criteria, tampered,
            stage2_pair(context, criteria, stage1, top, derived.facts_commitment),
        )


@pytest.mark.parametrize("field,value", (
    ("expected_causal_class", CausalClass.OTHER),
    ("accepted_alternatives", ("forged",)),
    ("required_full_evidence", ("forged",)),
    ("accepted_granularities", ("forged",)),
    ("allowed_degraded_conclusions", ("IDENTIFIED",)),
    ("top_hypothesis_required", False),
    ("scenario_commitment", "sha256:" + "0" * 64),
))
def test_a001_exact_oracle_authority_remains_closed(adjudication_context, field, value):
    context = adjudication_context
    _, stage1 = consensus(context)
    criteria = context.adjudicator.resolve_authoritative_criteria(context.execution.record_id)
    derived = context.adjudicator.derive_root_cause_facts(stage1)
    top = TopHypothesis(RULES["S1"][0], RULES["S1"][1])
    with pytest.raises(ValueError, match="authoritative Oracle scenario"):
        context.adjudicator.finalize_stage2(
            context.execution.record_id, replace(criteria, **{field: value}), stage1,
            stage2_pair(context, criteria, stage1, top, derived.facts_commitment),
        )


def test_valid_authoritative_two_stage_flow_binds_official_judgment(adjudication_context):
    context = adjudication_context
    admitted, stage1 = consensus(context)
    criteria = context.adjudicator.resolve_authoritative_criteria(context.execution.record_id)
    derived = context.adjudicator.derive_root_cause_facts(stage1)
    top = TopHypothesis(RULES["S1"][0], RULES["S1"][1])
    official = context.adjudicator.finalize_stage2(
        context.execution.record_id, criteria, stage1,
        stage2_pair(context, criteria, stage1, top, derived.facts_commitment),
    )
    assert official.evaluation.root_cause_identification_success
    payload = json.loads(official.judgment_record.semantic_json)
    source = source_facts(context)
    assert payload["production_output_commitment"] == source.production_output_commitment
    assert payload["typed_claims_commitment"] == source.typed_claims_commitment
    assert payload["blind_packet_commitment"] == admitted.packet_commitment
    assert payload["stage1_consensus_fact_commitment"] == stage1.consensus_facts_commitment
    assert payload["oracle_scenario_commitment"] == criteria.scenario_commitment
    assert payload["derived_root_cause_facts_commitment"] == derived.facts_commitment
    assert payload["final_scoring_commitment"] == commitment(payload["root_cause_predicates"])


def test_stage2_rejects_derived_fact_drift_and_llm_review(adjudication_context):
    context = adjudication_context
    admitted, stage1 = consensus(context)
    criteria = context.adjudicator.resolve_authoritative_criteria(context.execution.record_id)
    derived = context.adjudicator.derive_root_cause_facts(stage1)
    top = TopHypothesis(RULES["S1"][0], RULES["S1"][1])
    wrong = commitment({"wrong": "facts"})
    submissions = tuple(
        replace(item, derived_facts_commitment=wrong)
        for item in stage2_pair(
            context, criteria, stage1, top, derived.facts_commitment,
        )
    )
    with pytest.raises(ValueError, match="facts contradict"):
        context.adjudicator.finalize_stage2(
            context.execution.record_id, criteria, stage1,
            submissions,
        )
    source = source_facts(context)
    reviews = reviewed_facts(source)
    with pytest.raises(ValueError, match="LLM judge"):
        context.adjudicator.record_stage1(
            context.execution.record_id, admitted,
            (blind("R7", admitted, reviews), replace(
                blind("R8", admitted, reviews), authority=ReviewerAuthority.LLM_EXPLORATORY,
            )),
        )


def test_two_reviewer_disagreement_requires_third(adjudication_context):
    context = adjudication_context
    admitted = admit(context)
    source = source_facts(context)
    first = reviewed_facts(source)
    second = tuple(
        replace(item, determination=FactDetermination.FALSE)
        if item.kind is ReviewedFactKind.OPERATIONAL_CAUSE_GROUNDED else item
        for item in first
    )
    with pytest.raises(ReviewIncomplete, match="third reviewer"):
        context.adjudicator.record_stage1(
            context.execution.record_id, admitted,
            (blind("R1", admitted, first), blind("R2", admitted, second)),
        )
