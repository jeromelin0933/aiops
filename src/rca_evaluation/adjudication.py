"""Authority-bound blind review and append-only Stage 2 adjudication."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from enum import Enum

from rca_persistence.contracts import EvidenceCompleteness

from .calibration import ConclusionFacts, KnowledgeBoundaryInput
from .claims import (
    AtomicClaimFinding, ClaimEvaluationStatus, GovernedClaimDecomposition,
    evaluate_unsupported_claims, production_claim_commitment,
)
from .identity import commitment, identity, require_identity
from .isolation import _decoded_variants
from .ledger import LedgerRecord
from .leakage_policy import TRUSTED_LEAKAGE_POLICY, TrustedLeakagePolicy
from .observation import CapturedInputBoundary
from .scoring import (
    CausalClass, ExecutionScorability, OracleScenarioCriteria,
    RootCauseEvaluation, TopHypothesis, evaluate_root_cause,
)
from .source_authority import AuthoritativeProductionFacts, S3SourceAuthority
from .sqlite_store import EvaluationRunSnapshot, SqliteEvaluationStore


class ReviewerAuthority(str, Enum):
    HUMAN = "HUMAN"
    LLM_EXPLORATORY = "LLM_EXPLORATORY"


ADJUDICATION_EVALUATOR_IDENTITY = "SPEC-017-S3-GOVERNED-ADJUDICATOR-v1"
ADJUDICATION_EVALUATOR_VERSION = "1"
_ADJUDICATION_ISSUANCE_AUTHORITY = object()


def is_adjudication_issuance_authority(authority: object) -> bool:
    return authority is _ADJUDICATION_ISSUANCE_AUTHORITY


class ReviewedFactKind(str, Enum):
    EVIDENCE_CLASS_PRESENT = "EVIDENCE_CLASS_PRESENT"
    OPERATIONAL_CAUSE_GROUNDED = "OPERATIONAL_CAUSE_GROUNDED"
    COMPLETE_CAUSAL_CHAIN = "COMPLETE_CAUSAL_CHAIN"
    MATERIAL_CONTRADICTION = "MATERIAL_CONTRADICTION"
    COMPETING_ALTERNATIVE = "COMPETING_ALTERNATIVE"
    MATERIAL_OVERCLAIM = "MATERIAL_OVERCLAIM"
    KNOWLEDGE_INCIDENT_TRUTH_DEPENDENCY = "KNOWLEDGE_INCIDENT_TRUTH_DEPENDENCY"


class FactDetermination(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"


class ReviewIncomplete(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdjudicationVerification:
    execution_id: str
    judgment_id: str | None
    complete: bool
    blockers: tuple[str, ...]


def verify_persisted_adjudication(
    snapshot: EvaluationRunSnapshot, execution_id: str,
) -> AdjudicationVerification:
    """Verify persisted review facts using the adjudication contract itself."""
    require_identity(execution_id, "execution")
    records = tuple(record for record in snapshot.records
                    if record.parent_id == execution_id)
    payloads = {record.record_id: json.loads(record.semantic_json)
                for record in records}
    judgments = tuple(record for record in records if record.domain == "judgment")
    judgment_id = judgments[0].record_id if len(judgments) == 1 else None
    blockers: list[str] = []
    try:
        if len(judgments) != 1:
            raise ReviewIncomplete("ORIGINAL_JUDGMENT_CARDINALITY")
        judgment = payloads[judgment_id]
        if (judgment.get("kind") != "ORIGINAL"
                or judgment.get("oracle_revision_id") != snapshot.oracle_revision_id):
            raise ReviewIncomplete("JUDGMENT_AUTHORITY_BINDING_INVALID")
        source_ids = judgment.get("source_record_ids")
        source_hashes = judgment.get("source_record_commitments")
        claim_id = judgment.get("claim_result_record_id")
        claim_record = next(
            (item for item in records if item.record_id == claim_id), None)
        claim_payload = payloads.get(claim_id)
        judgment_material = dict(judgment)
        judgment_seal = judgment_material.pop("judgment_commitment", None)
        if (judgment.get("governed_adjudicator_identity")
                != ADJUDICATION_EVALUATOR_IDENTITY
                or judgment.get("adjudicator_version")
                    != ADJUDICATION_EVALUATOR_VERSION
                or judgment.get("evaluation_run_id")
                    != snapshot.evaluation_run_id
                or judgment.get("execution_id") != execution_id
                or not isinstance(source_ids, list)
                or not isinstance(source_hashes, list)
                or len(source_ids) != len(source_hashes)
                or len(source_ids) < 3
                or judgment_seal != commitment(judgment_material)
                or claim_record is None or claim_record.domain != "observation"
                or claim_record.parent_id != execution_id
                or claim_record.semantic_commitment
                    != judgment.get("claim_result_record_commitment")
                or claim_id not in source_ids
                or not isinstance(claim_payload, dict)
                or claim_payload.get("kind") != "UNSUPPORTED_CLAIM_RESULT"
                or claim_payload.get("governed_adjudicator_identity")
                    != ADJUDICATION_EVALUATOR_IDENTITY
                or claim_payload.get("evaluation_run_id")
                    != snapshot.evaluation_run_id
                or claim_payload.get("execution_id") != execution_id
                or judgment.get("unsupported_claim_result")
                    != claim_payload.get("unsupported_claim_result")):
            raise ReviewIncomplete("PROTECTED_JUDGMENT_AUTHORITY_INVALID")
        for source_id, source_hash in zip(source_ids, source_hashes):
            source_record = next(
                (item for item in records if item.record_id == source_id), None)
            if (source_record is None or source_record.parent_id != execution_id
                    or source_record.semantic_commitment != source_hash):
                raise ReviewIncomplete("JUDGMENT_SOURCE_LINEAGE_INVALID")
        claim_findings = claim_payload.get("claim_adjudications")
        ucr = claim_payload.get("unsupported_claim_result")
        if not isinstance(claim_findings, list) or not isinstance(ucr, dict):
            raise ReviewIncomplete("PROTECTED_UCR_AUTHORITY_INVALID")
        numerator = sum(
            isinstance(item, dict) and item.get("disposition") in {
                "UNSUPPORTED", "CONTRADICTED"} for item in claim_findings)
        contradicted = sum(
            isinstance(item, dict) and item.get("disposition") == "CONTRADICTED"
            for item in claim_findings)
        denominator = len(claim_findings)
        expected_ucr = {
            "status": "SCORED" if denominator else "NO_ELIGIBLE_CLAIMS",
            "numerator": numerator, "denominator": denominator,
            "contradicted_count": contradicted,
            "rate_fraction": None if not denominator else [numerator, denominator],
        }
        if ucr != expected_ucr:
            raise ReviewIncomplete("PROTECTED_UCR_AUTHORITY_INVALID")
        packets = tuple((record, payloads[record.record_id]) for record in records
                        if record.domain == "observation"
                        and payloads[record.record_id].get("kind") == "BLIND_PACKET")
        if len(packets) != 1:
            raise ReviewIncomplete("STAGE1_BLIND_PACKET_MISSING")
        packet_record, packet_payload = packets[0]
        packet = packet_payload.get("packet")
        if (not isinstance(packet, dict)
                or packet.get("schema") != "SPEC-017-S3-BLIND-PACKET-v2"
                or not isinstance(packet.get("captured"), dict)
                or packet["captured"].get("execution_id") != execution_id
                or packet_payload.get("packet_commitment") != commitment(packet)
                or not isinstance(packet.get("source"), dict)
                or packet_payload.get("source_commitment") != commitment(packet["source"])
                or packet_payload.get("isolation_policy_commitment") !=
                    packet.get("isolation_policy_commitment")):
            raise ReviewIncomplete("STAGE1_BLIND_PACKET_LINEAGE_INVALID")
        source = packet["source"]

        consensus_records = tuple(
            (record, payloads[record.record_id]) for record in records
            if record.domain == "review"
            and payloads[record.record_id].get("stage") == "STAGE1_CONSENSUS")
        if len(consensus_records) != 1:
            raise ReviewIncomplete("STAGE1_CONSENSUS_MISSING")
        consensus_record, consensus_payload = consensus_records[0]
        review_ids = consensus_payload.get("review_ids")
        if not isinstance(review_ids, list):
            raise ReviewIncomplete("STAGE1_REVIEW_IDS_INVALID")
        facts_by_review = []
        reviewers = []
        for review_id in review_ids:
            review = payloads.get(review_id)
            record = next((item for item in records if item.record_id == review_id), None)
            if (record is None or record.domain != "review" or review is None
                    or review.get("stage") != "STAGE1"
                    or review.get("item_id") != consensus_payload.get("item_id")
                    or review.get("input_commitment") !=
                        packet_payload.get("packet_commitment")
                    or review.get("blind_packet_record_id") != packet_record.record_id
                    or review.get("source_commitment") !=
                        packet_payload.get("source_commitment")):
                raise ReviewIncomplete("STAGE1_REVIEW_LINEAGE_INVALID")
            facts = tuple(Stage1ReviewedFact.from_material(item)
                          for item in review.get("reviewed_facts", ()))
            if review.get("reviewed_facts_commitment") != commitment(
                    [item.material() for item in facts]):
                raise ReviewIncomplete("STAGE1_FACT_COMMITMENT_INVALID")
            _validate_reviewed_facts_material(source, facts)
            BlindReviewSubmission(
                review.get("reviewer_id"), review.get("item_id"),
                review.get("input_commitment"), facts,
                review.get("reason_code"), review.get("rationale"),
                review.get("reviewed_at")).validate()
            reviewers.append(review.get("reviewer_id")); facts_by_review.append(facts)
        if len(reviewers) not in {2, 3} or len(set(reviewers)) != len(reviewers):
            raise ReviewIncomplete("STAGE1_REVIEWER_SET_INVALID")
        selected_facts = facts_by_review[0] if facts_by_review[0] == facts_by_review[1] else (
            facts_by_review[2] if len(facts_by_review) == 3 else None)
        if selected_facts is None or (facts_by_review[0] == facts_by_review[1]) != (
                len(facts_by_review) == 2):
            raise ReviewIncomplete("STAGE1_AGREEMENT_INVALID")
        material = {
            "execution_id": execution_id,
            "item_id": consensus_payload.get("item_id"),
            "blind_packet_record_id": packet_record.record_id,
            "blind_packet_commitment": packet_payload.get("packet_commitment"),
            "source_commitment": packet_payload.get("source_commitment"),
            "reviewed_facts": [item.material() for item in selected_facts],
            "consensus_facts_commitment": commitment(
                [item.material() for item in selected_facts]),
            "review_ids": review_ids,
        }
        if (consensus_payload != {"stage": "STAGE1_CONSENSUS", **material,
                                  "consensus_commitment": commitment(material)}):
            raise ReviewIncomplete("STAGE1_CONSENSUS_COMMITMENT_INVALID")

        stage2_ids = judgment.get("stage2_review_ids")
        if not isinstance(stage2_ids, list):
            raise ReviewIncomplete("STAGE2_REVIEW_IDS_INVALID")
        submissions = []
        for review_id in stage2_ids:
            review = payloads.get(review_id)
            record = next((item for item in records if item.record_id == review_id), None)
            top = review.get("top_hypothesis") if isinstance(review, dict) else None
            if (record is None or record.domain != "review" or not isinstance(review, dict)
                    or review.get("stage") != "STAGE2" or not isinstance(top, dict)):
                raise ReviewIncomplete("STAGE2_REVIEW_LINEAGE_INVALID")
            submission = Stage2Submission(
                review.get("reviewer_id"), review.get("input_commitment"),
                review.get("oracle_revision_id"),
                review.get("derived_facts_commitment"),
                TopHypothesis(CausalClass(top.get("causal_class")),
                              top.get("granularity"),
                              top.get("accepted_alternative_id")),
                review.get("reason_code"), review.get("rationale"),
                review.get("reviewed_at"))
            submission.validate(); submissions.append(submission)
            if (review.get("oracle_scenario_commitment") !=
                    judgment.get("oracle_scenario_commitment")
                    or review.get("production_output_commitment") !=
                    source.get("production_output_commitment")
                    or review.get("typed_claims_commitment") !=
                    source.get("typed_claims_commitment")):
                raise ReviewIncomplete("STAGE2_AUTHORITY_BINDING_INVALID")
        selected = TwoStageAdjudicator._select_consensus(
            tuple(submissions), stage="STAGE2")
        expected_input = commitment({
            "blind_packet_commitment": packet_payload.get("packet_commitment"),
            "stage1_consensus_commitment": consensus_payload.get("consensus_commitment"),
            "stage1_consensus_facts_commitment":
                consensus_payload.get("consensus_facts_commitment"),
            "derived_root_cause_facts_commitment":
                judgment.get("derived_root_cause_facts_commitment"),
            "oracle_revision_id": judgment.get("oracle_revision_id"),
            "oracle_scenario_id": judgment.get("oracle_scenario_id"),
            "oracle_scenario_commitment": judgment.get("oracle_scenario_commitment"),
        })
        scoring = judgment.get("root_cause_predicates")
        if (any(item.input_commitment != expected_input for item in submissions)
                or any(item.derived_facts_commitment !=
                       judgment.get("derived_root_cause_facts_commitment")
                       for item in submissions)
                or judgment.get("blind_packet_commitment") !=
                    packet_payload.get("packet_commitment")
                or judgment.get("stage1_consensus_fact_commitment") !=
                    consensus_payload.get("consensus_facts_commitment")
                or judgment.get("stage1_consensus_commitment") !=
                    consensus_payload.get("consensus_commitment")
                or judgment.get("production_output_commitment") !=
                    source.get("production_output_commitment")
                or judgment.get("typed_claims_commitment") !=
                    source.get("typed_claims_commitment")
                or not isinstance(scoring, dict)
                or judgment.get("final_scoring_commitment") != commitment(scoring)
                or bool(scoring.get("root_cause_identification_success")) !=
                    bool(payloads[stage2_ids[submissions.index(selected)]].get(
                        "root_cause_success"))
                or judgment.get("result") != (
                    "PASS" if scoring.get("root_cause_identification_success") else "FAIL")):
            raise ReviewIncomplete("JUDGMENT_EXACT_BINDING_INVALID")
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        blockers.append(str(exc) or type(exc).__name__)
    return AdjudicationVerification(
        execution_id, judgment_id, not blockers, tuple(sorted(set(blockers))))


def _text(value: str, field: str, maximum: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be bounded non-empty text")
    return value


def _sha256(value: str, field: str) -> str:
    _text(value, field, 71)
    if len(value) != 71 or not value.startswith("sha256:"):
        raise ValueError(f"{field} must be a SHA-256 commitment")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise ValueError(f"{field} must be a SHA-256 commitment") from exc
    return value


def _source_commitment(value: str) -> str:
    if isinstance(value, str) and len(value) == 64:
        try:
            int(value, 16)
            return value
        except ValueError:
            pass
    return _sha256(value, "fact source commitment")


@dataclass(frozen=True, slots=True)
class Stage1ReviewedFact:
    kind: ReviewedFactKind
    subject_id: str
    determination: FactDetermination
    source_identity: str
    source_commitment: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReviewedFactKind) or not isinstance(
                self.determination, FactDetermination):
            raise TypeError("Stage 1 fact kind and determination must be closed")
        _text(self.subject_id, "fact subject", 256)
        _text(self.source_identity, "fact source identity", 256)
        _source_commitment(self.source_commitment)

    def material(self) -> dict:
        return {
            "kind": self.kind.value, "subject_id": self.subject_id,
            "determination": self.determination.value,
            "source_identity": self.source_identity,
            "source_commitment": self.source_commitment,
        }

    @classmethod
    def from_material(cls, value: object) -> "Stage1ReviewedFact":
        if not isinstance(value, dict) or set(value) != {
            "kind", "subject_id", "determination", "source_identity", "source_commitment",
        }:
            raise ValueError("Stage 1 reviewed fact schema is invalid")
        return cls(
            ReviewedFactKind(value["kind"]), value["subject_id"],
            FactDetermination(value["determination"]), value["source_identity"],
            value["source_commitment"],
        )


def _validate_reviewed_facts_material(
    source: dict[str, object], facts: tuple[Stage1ReviewedFact, ...],
) -> None:
    required = {
        ReviewedFactKind.OPERATIONAL_CAUSE_GROUNDED,
        ReviewedFactKind.COMPLETE_CAUSAL_CHAIN,
        ReviewedFactKind.MATERIAL_CONTRADICTION,
        ReviewedFactKind.COMPETING_ALTERNATIVE,
        ReviewedFactKind.KNOWLEDGE_INCIDENT_TRUTH_DEPENDENCY,
    }
    by_kind = {kind: [item for item in facts if item.kind is kind] for kind in required}
    if any(len(by_kind[kind]) != 1 or by_kind[kind][0].subject_id != "conclusion"
           for kind in required):
        raise ValueError("Stage 1 mandatory conclusion facts are incomplete")
    allowed_sources = {
        source.get("validated_result_id"): source.get("production_output_commitment"),
        source.get("evidence_snapshot_id"): source.get("evidence_commitment"),
        source.get("knowledge_snapshot_id"): source.get("knowledge_commitment"),
    }
    if None in allowed_sources:
        raise ValueError("Stage 1 source authority material is incomplete")
    for item in facts:
        if allowed_sources.get(item.source_identity) != item.source_commitment:
            raise ValueError("Stage 1 reviewed fact source lineage is invalid")
    machine = source.get("machine_facts")
    if not isinstance(machine, dict):
        raise ValueError("Stage 1 machine facts are incomplete")
    contradiction = machine.get("major_contradiction")
    competing = machine.get("competing_alternatives")
    if (isinstance(contradiction, dict) and contradiction.get("value") is True
            and by_kind[ReviewedFactKind.MATERIAL_CONTRADICTION][0].determination
                is FactDetermination.FALSE):
        raise ValueError("Stage 1 contradiction fact contradicts production authority")
    if (isinstance(competing, dict) and competing.get("value") is True
            and by_kind[ReviewedFactKind.COMPETING_ALTERNATIVE][0].determination
                is FactDetermination.FALSE):
        raise ValueError("Stage 1 competing-alternative fact contradicts production authority")


@dataclass(frozen=True, slots=True)
class RootCauseFacts:
    lower_ranked_causal_classes: tuple[CausalClass, ...]
    observed_evidence_classes: tuple[str, ...]
    detected_overclaim_ids: tuple[str, ...]
    evidence_completeness: EvidenceCompleteness
    conclusion: ConclusionFacts
    knowledge: KnowledgeBoundaryInput
    claim_status: ClaimEvaluationStatus
    scorability: ExecutionScorability = ExecutionScorability.SCORABLE

    def __post_init__(self) -> None:
        if (not isinstance(self.evidence_completeness, EvidenceCompleteness)
                or not isinstance(self.conclusion, ConclusionFacts)
                or not isinstance(self.knowledge, KnowledgeBoundaryInput)
                or not isinstance(self.claim_status, ClaimEvaluationStatus)
                or not isinstance(self.scorability, ExecutionScorability)):
            raise TypeError("derived root cause facts require closed values")

    def material(self) -> dict:
        return {
            "lower_ranked_causal_classes": [item.value for item in self.lower_ranked_causal_classes],
            "observed_evidence_classes": list(self.observed_evidence_classes),
            "detected_overclaim_ids": list(self.detected_overclaim_ids),
            "evidence_completeness": self.evidence_completeness.value,
            "conclusion": {
                "conclusion": self.conclusion.conclusion.value,
                "operational_cause_grounded": self.conclusion.operational_cause_grounded,
                "complete_causal_chain": self.conclusion.complete_causal_chain,
                "leading_hypothesis": self.conclusion.leading_hypothesis,
                "major_contradiction": self.conclusion.major_contradiction,
                "competing_alternatives_unresolved": self.conclusion.competing_alternatives_unresolved,
            },
            "knowledge": {
                "resolution": self.knowledge.resolution.value,
                "knowledge_gap": self.knowledge.knowledge_gap,
                "fabricated_reference": self.knowledge.fabricated_reference,
                "sop_backed_guidance": self.knowledge.sop_backed_guidance,
                "incident_truth_depends_on_knowledge": self.knowledge.incident_truth_depends_on_knowledge,
                "unavailable_reported_as_no_match": self.knowledge.unavailable_reported_as_no_match,
            },
            "claim_status": self.claim_status.value,
            "scorability": self.scorability.value,
        }

    @property
    def facts_commitment(self) -> str:
        return commitment(self.material())


@dataclass(frozen=True, slots=True)
class BlindPacket:
    captured: CapturedInputBoundary
    source: AuthoritativeProductionFacts
    audit_metadata: tuple[tuple[str, str], ...]
    isolation_policy_identity: str
    isolation_policy_commitment: str

    def __post_init__(self) -> None:
        if (not isinstance(self.captured, CapturedInputBoundary)
                or not isinstance(self.source, AuthoritativeProductionFacts)):
            raise TypeError("blind packet source must be authoritative")
        if self.captured.execution_id != self.source.execution_id:
            raise ValueError("blind packet capture and source execution differ")
        if (not isinstance(self.audit_metadata, tuple)
                or any(not isinstance(item, tuple) or len(item) != 2 for item in self.audit_metadata)
                or len({key for key, _ in self.audit_metadata}) != len(self.audit_metadata)
                or tuple(sorted(self.audit_metadata)) != self.audit_metadata):
            raise ValueError("blind packet audit metadata must be canonical and unique")
        for key, value in self.audit_metadata:
            _text(key, "audit metadata key", 160)
            _text(value, "audit metadata value", 512)
        _text(self.isolation_policy_identity, "isolation policy identity", 160)
        _sha256(self.isolation_policy_commitment, "isolation policy commitment")

    def material(self) -> dict:
        return {
            "schema": "SPEC-017-S3-BLIND-PACKET-v2",
            "captured": {
                "execution_id": self.captured.execution_id,
                "event_id": self.captured.event_id,
                "event_content_commitment": self.captured.event_content_commitment,
            },
            "source": self.source.material(),
            "audit_metadata": [list(item) for item in self.audit_metadata],
            "isolation_policy_identity": self.isolation_policy_identity,
            "isolation_policy_commitment": self.isolation_policy_commitment,
        }

    @property
    def packet_commitment(self) -> str:
        return commitment(self.material())


@dataclass(frozen=True, slots=True)
class AdmittedBlindPacket:
    record_id: str
    execution_id: str
    packet_commitment: str
    source_commitment: str
    isolation_policy_commitment: str


@dataclass(frozen=True, slots=True)
class BlindReviewSubmission:
    reviewer_id: str
    item_id: str
    input_commitment: str
    reviewed_facts: tuple[Stage1ReviewedFact, ...]
    reason_code: str
    rationale: str
    reviewed_at: str
    authority: ReviewerAuthority = ReviewerAuthority.HUMAN

    def validate(self) -> None:
        for name in ("reviewer_id", "item_id", "reason_code", "rationale", "reviewed_at"):
            _text(getattr(self, name), name)
        _sha256(self.input_commitment, "input_commitment")
        if (not isinstance(self.reviewed_facts, tuple) or not self.reviewed_facts
                or any(not isinstance(item, Stage1ReviewedFact) for item in self.reviewed_facts)):
            raise ValueError("Stage 1 requires a non-empty reviewed fact set")
        keys = [(item.kind.value, item.subject_id) for item in self.reviewed_facts]
        if len(keys) != len(set(keys)) or tuple(sorted(keys)) != tuple(keys):
            raise ValueError("Stage 1 reviewed facts must be canonical and unique")
        if self.authority is not ReviewerAuthority.HUMAN:
            raise ValueError("LLM judge cannot be an official reviewer")

    @property
    def reviewed_facts_commitment(self) -> str:
        return commitment([item.material() for item in self.reviewed_facts])


@dataclass(frozen=True, slots=True)
class Stage1Consensus:
    consensus_record_id: str
    execution_id: str
    item_id: str
    blind_packet_record_id: str
    blind_packet_commitment: str
    source_commitment: str
    reviewed_facts: tuple[Stage1ReviewedFact, ...]
    consensus_facts_commitment: str
    review_ids: tuple[str, ...]
    consensus_commitment: str


@dataclass(frozen=True, slots=True)
class Stage2Submission:
    reviewer_id: str
    input_commitment: str
    oracle_revision_id: str
    derived_facts_commitment: str
    top_hypothesis: TopHypothesis
    reason_code: str
    rationale: str
    reviewed_at: str
    authority: ReviewerAuthority = ReviewerAuthority.HUMAN

    def validate(self) -> None:
        for name in ("reviewer_id", "reason_code", "rationale", "reviewed_at"):
            _text(getattr(self, name), name)
        _sha256(self.input_commitment, "input_commitment")
        _sha256(self.derived_facts_commitment, "derived_facts_commitment")
        require_identity(self.oracle_revision_id, "oracle_revision")
        if not isinstance(self.top_hypothesis, TopHypothesis):
            raise TypeError("Stage 2 requires a typed top hypothesis")
        if self.authority is not ReviewerAuthority.HUMAN:
            raise ValueError("LLM judge cannot be an official reviewer")


@dataclass(frozen=True, slots=True)
class ClaimAdjudicationSubmission:
    reviewer_id: str
    input_commitment: str
    decomposition: tuple[GovernedClaimDecomposition, ...]
    factual_assertion_commitments: tuple[str, ...]
    findings: tuple[AtomicClaimFinding, ...]
    reason_code: str
    rationale: str
    reviewed_at: str
    authority: ReviewerAuthority = ReviewerAuthority.HUMAN

    @property
    def claim_facts_commitment(self) -> str:
        return commitment({
            "decomposition": [{
                "production_claim_id": item.production_claim_id,
                "production_claim_commitment": item.production_claim_commitment,
                "atomic_commitments": list(item.atomic_commitments),
            } for item in self.decomposition],
            "factual_assertion_commitments": list(
                self.factual_assertion_commitments),
            "findings": [{
                "production_claim_id": item.production_claim_id,
                "atomic_commitment": item.atomic_commitment,
                "disposition": item.disposition.value,
            } for item in self.findings],
        })

    def validate(self) -> None:
        for name in ("reviewer_id", "reason_code", "rationale", "reviewed_at"):
            _text(getattr(self, name), name)
        _sha256(self.input_commitment, "input_commitment")
        if self.authority is not ReviewerAuthority.HUMAN:
            raise ValueError("LLM judge cannot be an official claim reviewer")
        if (not self.decomposition or not self.findings
                or any(not isinstance(item, GovernedClaimDecomposition)
                       for item in self.decomposition)
                or any(not isinstance(item, AtomicClaimFinding)
                       for item in self.findings)):
            raise ValueError("claim adjudication requires typed complete facts")


@dataclass(frozen=True, slots=True)
class OfficialJudgment:
    judgment_record: LedgerRecord
    evaluation: RootCauseEvaluation
    stage2_review_ids: tuple[str, ...]


def stage2_input_commitment(criteria: OracleScenarioCriteria, stage1: Stage1Consensus,
                            derived_facts_commitment: str) -> str:
    return commitment({
        "blind_packet_commitment": stage1.blind_packet_commitment,
        "stage1_consensus_commitment": stage1.consensus_commitment,
        "stage1_consensus_facts_commitment": stage1.consensus_facts_commitment,
        "derived_root_cause_facts_commitment": derived_facts_commitment,
        "oracle_revision_id": criteria.oracle_revision_id,
        "oracle_scenario_id": criteria.scenario_id,
        "oracle_scenario_commitment": criteria.scenario_commitment,
    })


def claim_adjudication_input_commitment(
    criteria: OracleScenarioCriteria, stage1: Stage1Consensus,
    source: AuthoritativeProductionFacts,
) -> str:
    return commitment({
        "execution_id": stage1.execution_id,
        "blind_packet_commitment": stage1.blind_packet_commitment,
        "stage1_consensus_commitment": stage1.consensus_commitment,
        "oracle_revision_id": criteria.oracle_revision_id,
        "oracle_scenario_id": criteria.scenario_id,
        "oracle_scenario_commitment": criteria.scenario_commitment,
        "production_output_commitment": source.production_output_commitment,
        "typed_claims_commitment": source.typed_claims_commitment,
    })


_FORBIDDEN_METADATA_FIELDS = frozenset({
    "expectedcausalclass", "expectedanswer", "expectedrootcause", "acceptedalternative",
    "oracleanswer", "oraclescoringrule", "scenarioanswer", "groundtruth", "hiddendiagnosis",
})


def _canonical_governed(value: str) -> str:
    """Exact governed representation normalization; no semantic or fuzzy matching."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


class TwoStageAdjudicator:
    def __init__(self, store: SqliteEvaluationStore, source_authority: S3SourceAuthority,
                 isolation_policy: TrustedLeakagePolicy, *,
                 expected_policy_commitment: str,
                 reviewer_protocol: dict[str, object] | None = None) -> None:
        if type(store) is not SqliteEvaluationStore:
            raise TypeError("S1 evaluation store authority is required")
        if type(source_authority) is not S3SourceAuthority:
            raise TypeError("S3 public source authority is required")
        if not isinstance(isolation_policy, TrustedLeakagePolicy):
            raise TypeError("trusted leakage isolation policy is required")
        if isolation_policy != TRUSTED_LEAKAGE_POLICY:
            raise ValueError("untrusted, incomplete, or replacement leakage policy")
        _sha256(expected_policy_commitment, "expected_policy_commitment")
        if expected_policy_commitment != isolation_policy.policy_commitment:
            raise ValueError("trusted leakage policy commitment mismatch")
        if not isinstance(reviewer_protocol, dict):
            raise TypeError("sealed reviewer protocol is required")
        stage1 = reviewer_protocol.get("stage1")
        stage2 = reviewer_protocol.get("stage2")
        if (not isinstance(stage1, dict) or not isinstance(stage2, dict)
                or not isinstance(stage1.get("reviewer_ids"), list)
                or len(stage1["reviewer_ids"]) != 2
                or not isinstance(stage1.get("tiebreak_reviewer_id"), str)
                or not isinstance(stage2.get("reviewer_ids"), list)
                or len(stage2["reviewer_ids"]) != 2
                or not isinstance(stage2.get("tiebreak_reviewer_id"), str)
                or reviewer_protocol.get(
                    "machine_hard_failure_override_allowed") is not False
                or reviewer_protocol.get("records_append_only") is not True):
            raise ValueError("sealed reviewer protocol is incomplete")
        stage1_identities = [*stage1["reviewer_ids"],
                             stage1["tiebreak_reviewer_id"]]
        stage2_identities = [*stage2["reviewer_ids"],
                             stage2["tiebreak_reviewer_id"]]
        identities = [*stage1_identities, *stage2_identities]
        if (any(not isinstance(item, str) or not item.strip() for item in identities)
                or len(stage1_identities) != len(set(stage1_identities))
                or len(stage2_identities) != len(set(stage2_identities))):
            raise ValueError(
                "sealed reviewer identities must be present and distinct within each stage")
        self._store = store
        self._source_authority = source_authority
        self._isolation_policy = isolation_policy
        self._reviewer_protocol = json.loads(json.dumps(
            reviewer_protocol, sort_keys=True))

    def resolve_authoritative_criteria(self, execution_id: str) -> OracleScenarioCriteria:
        plan, _, scenario_id = self._require_execution(execution_id)
        revision = self._store.get_oracle(plan.oracle_revision_id)
        if revision is None or revision.semantic_content_commitment != plan.oracle_content_commitment:
            raise ValueError("execution Oracle revision is not the exact admitted plan pin")
        criteria = OracleScenarioCriteria.from_revision(revision, scenario_id)
        if (criteria.oracle_revision_id != plan.oracle_revision_id
                or criteria.oracle_content_commitment != plan.oracle_content_commitment):
            raise ValueError("authoritative Oracle criteria contradict the plan pin")
        return criteria

    def admit_blind_packet(
        self, captured: CapturedInputBoundary, *,
        expected_production_output_commitment: str,
        expected_typed_claims_commitment: str,
        audit_metadata: tuple[tuple[str, str], ...] = (),
    ) -> AdmittedBlindPacket:
        if not isinstance(captured, CapturedInputBoundary):
            raise TypeError("captured S2 input boundary is required")
        self._require_execution(captured.execution_id)
        source = self._source_authority.resolve(captured)
        if source.production_output_commitment != expected_production_output_commitment:
            raise ValueError("production output commitment does not match public authority")
        if source.typed_claims_commitment != expected_typed_claims_commitment:
            raise ValueError("typed claims commitment does not match public authority")
        criteria = self.resolve_authoritative_criteria(captured.execution_id)
        self._validate_blind_metadata(audit_metadata, criteria)
        packet = BlindPacket(
            captured, source, audit_metadata, self._isolation_policy.policy_identity,
            self._isolation_policy.policy_commitment,
        )
        payload = {
            "kind": "BLIND_PACKET", "packet_commitment": packet.packet_commitment,
            "source_commitment": source.source_commitment,
            "isolation_policy_commitment": self._isolation_policy.policy_commitment,
            "packet": packet.material(),
        }
        record = self._store.append(LedgerRecord.create(
            "observation", [captured.execution_id, "S3_BLIND_PACKET"], payload,
            captured.execution_id,
        ))
        return AdmittedBlindPacket(
            record.record_id, captured.execution_id, packet.packet_commitment,
            source.source_commitment, self._isolation_policy.policy_commitment,
        )

    def record_stage1(self, execution_id: str, packet_reference: AdmittedBlindPacket,
                      submissions: tuple[BlindReviewSubmission, ...]) -> Stage1Consensus:
        packet = self._load_blind_packet(packet_reference)
        if packet.source.execution_id != execution_id:
            raise ValueError("blind packet belongs to another execution")
        self._validate_review_set(submissions, stage="STAGE1")
        self._validate_registered_reviewers(
            execution_id, submissions, stage="STAGE1")
        if any(item.input_commitment != packet.packet_commitment for item in submissions):
            raise ValueError("Stage 1 reviewer did not receive the admitted blind packet")
        for submission in submissions:
            self._validate_reviewed_facts(packet.source, submission.reviewed_facts)
        review_ids = []
        for submission in submissions:
            payload = {
                "stage": "STAGE1", "reviewer_id": submission.reviewer_id,
                "item_id": submission.item_id, "input_commitment": submission.input_commitment,
                "blind_packet_record_id": packet_reference.record_id,
                "source_commitment": packet.source.source_commitment,
                "reviewed_facts": [item.material() for item in submission.reviewed_facts],
                "reviewed_facts_commitment": submission.reviewed_facts_commitment,
                "reason_code": submission.reason_code, "rationale": submission.rationale,
                "reviewed_at": submission.reviewed_at, "official_root_cause_verdict": None,
            }
            review = self._store.append(LedgerRecord.create(
                "review", [execution_id, "STAGE1", submission.item_id, submission.reviewer_id],
                payload, execution_id,
            ))
            review_ids.append(review.record_id)
        selected = self._select_consensus(submissions, stage="STAGE1")
        facts_commitment = selected.reviewed_facts_commitment
        material = {
            "execution_id": execution_id, "item_id": selected.item_id,
            "blind_packet_record_id": packet_reference.record_id,
            "blind_packet_commitment": packet.packet_commitment,
            "source_commitment": packet.source.source_commitment,
            "reviewed_facts": [item.material() for item in selected.reviewed_facts],
            "consensus_facts_commitment": facts_commitment,
            "review_ids": review_ids,
        }
        consensus_commitment = commitment(material)
        record = self._store.append(LedgerRecord.create(
            "review", [execution_id, "STAGE1_CONSENSUS", selected.item_id],
            {"stage": "STAGE1_CONSENSUS", **material,
             "consensus_commitment": consensus_commitment}, execution_id,
        ))
        return Stage1Consensus(
            record.record_id, execution_id, selected.item_id, packet_reference.record_id,
            packet.packet_commitment, packet.source.source_commitment,
            selected.reviewed_facts, facts_commitment, tuple(review_ids), consensus_commitment,
        )

    def derive_root_cause_facts(self, stage1: Stage1Consensus) -> RootCauseFacts:
        packet = self._verify_stage1(stage1)
        values = {(item.kind, item.subject_id): item for item in stage1.reviewed_facts}
        source = packet.source
        evidence = tuple(sorted(
            item.subject_id for item in stage1.reviewed_facts
            if item.kind is ReviewedFactKind.EVIDENCE_CLASS_PRESENT
            and item.determination is FactDetermination.TRUE
        ))
        overclaims = tuple(sorted(
            item.subject_id for item in stage1.reviewed_facts
            if item.kind is ReviewedFactKind.MATERIAL_OVERCLAIM
            and item.determination is FactDetermination.TRUE
        ))
        def value(kind: ReviewedFactKind) -> bool:
            item = values[(kind, "conclusion")]
            return item.determination is FactDetermination.TRUE
        reviewed_contradiction = value(ReviewedFactKind.MATERIAL_CONTRADICTION)
        reviewed_competing = value(ReviewedFactKind.COMPETING_ALTERNATIVE)
        dependency = value(ReviewedFactKind.KNOWLEDGE_INCIDENT_TRUTH_DEPENDENCY)
        conclusion = ConclusionFacts(
            source.diagnostic_conclusion,
            value(ReviewedFactKind.OPERATIONAL_CAUSE_GROUNDED),
            value(ReviewedFactKind.COMPLETE_CAUSAL_CHAIN),
            source.machine_leading_hypothesis,
            source.machine_major_contradiction or reviewed_contradiction,
            source.machine_competing_alternatives or reviewed_competing,
        )
        knowledge = KnowledgeBoundaryInput(
            source.knowledge_resolution, source.knowledge_gap, False,
            source.machine_sop_backed_guidance, dependency, False,
        )
        return RootCauseFacts(
            (), evidence, overclaims, source.evidence_completeness,
            conclusion, knowledge, ClaimEvaluationStatus.SCORED,
        )

    def adjudicate_claims(
        self, execution_id: str, stage1: Stage1Consensus,
        submissions: tuple[ClaimAdjudicationSubmission, ...],
    ) -> LedgerRecord:
        """Issue the sole protected claim/UCR fact from exact public claims."""
        criteria = self.resolve_authoritative_criteria(execution_id)
        if stage1.execution_id != execution_id:
            raise ValueError("Stage 1 consensus belongs to another execution")
        packet = self._verify_stage1(stage1)
        expected_input = claim_adjudication_input_commitment(
            criteria, stage1, packet.source)
        if len(submissions) not in {2, 3}:
            raise ReviewIncomplete(
                "CLAIM_ADJUDICATION requires two reviewers and a third on disagreement")
        self._validate_registered_reviewers(
            execution_id, submissions, stage="STAGE1")
        reviewers = []
        evaluated = []
        for submission in submissions:
            submission.validate()
            if submission.input_commitment != expected_input:
                raise ValueError("claim reviewer did not receive the governed claim input")
            result = evaluate_unsupported_claims(
                packet.source.production_claims, submission.decomposition,
                submission.factual_assertion_commitments, submission.findings)
            if result.status is ClaimEvaluationStatus.INVALID_OUTPUT:
                raise ValueError("claim adjudication facts are incomplete or contradictory")
            reviewers.append(submission.reviewer_id)
            evaluated.append(result)
        if len(set(reviewers)) != len(reviewers):
            raise ValueError("claim reviewers must be independent")
        keys = [item.claim_facts_commitment for item in submissions]
        if keys[0] == keys[1]:
            if len(submissions) != 2:
                raise ValueError("third claim reviewer is only authorized for disagreement")
            selected_index = 0
        else:
            if len(submissions) != 3:
                raise ReviewIncomplete(
                    "CLAIM_ADJUDICATION reviewer disagreement requires a third reviewer")
            if keys[2] not in {keys[0], keys[1]}:
                raise ReviewIncomplete("CLAIM_ADJUDICATION lacks bounded consensus")
            selected_index = 2
        review_records = []
        for submission, result in zip(submissions, evaluated):
            material = {
                "stage": "CLAIM_ADJUDICATION",
                "reviewer_id": submission.reviewer_id,
                "input_commitment": submission.input_commitment,
                "claim_facts_commitment": submission.claim_facts_commitment,
                "result_commitment": result.result_commitment,
                "reason_code": submission.reason_code,
                "rationale": submission.rationale,
                "reviewed_at": submission.reviewed_at,
            }
            review_records.append(self._store.append(LedgerRecord.create(
                "review", [execution_id, "CLAIM_ADJUDICATION",
                           submission.reviewer_id], material, execution_id)))
        selected_submission = submissions[selected_index]
        selected = evaluated[selected_index]
        adjudications = [{
            "production_claim_id": item.production_claim_id,
            "atomic_commitment": item.atomic_commitment,
            "disposition": item.disposition.value,
        } for item in selected.unique_findings]
        ucr = {
            "status": selected.status.value,
            "numerator": selected.numerator,
            "denominator": selected.denominator,
            "contradicted_count": selected.contradicted_count,
            "rate_fraction": (None if selected.rate is None else
                              [selected.numerator, selected.denominator]),
        }
        source_records = (
            self._store.get(stage1.blind_packet_record_id),
            self._store.get(stage1.consensus_record_id),
            *review_records,
        )
        if any(item is None for item in source_records):
            raise ValueError("claim adjudication source record is missing")
        plan, run, scenario_id = self._require_execution(execution_id)
        result_material = {
            "claim_adjudications": adjudications, "ucr": ucr,
            "claim_facts_commitment": selected_submission.claim_facts_commitment,
        }
        payload = {
            "kind": "UNSUPPORTED_CLAIM_RESULT",
            "governed_adjudicator_identity": ADJUDICATION_EVALUATOR_IDENTITY,
            "adjudicator_version": ADJUDICATION_EVALUATOR_VERSION,
            "evaluation_run_id": run.record_id, "execution_id": execution_id,
            "scenario_id": scenario_id,
            "oracle_revision_id": plan.oracle_revision_id,
            "oracle_scenario_commitment": criteria.scenario_commitment,
            "input_commitment": expected_input,
            "production_claim_identities": [
                item.claim_id for item in packet.source.production_claims],
            "production_claim_commitments": [
                production_claim_commitment(item)
                for item in packet.source.production_claims],
            "claim_adjudications": adjudications,
            "claim_review_ids": [item.record_id for item in review_records],
            "source_record_ids": [item.record_id for item in source_records],
            "source_record_commitments": [
                item.semantic_commitment for item in source_records],
            "claim_facts_commitment": selected_submission.claim_facts_commitment,
            "unsupported_claim_result": ucr,
            "governed_result_commitment": commitment(result_material),
        }
        record = LedgerRecord.create(
            "observation", [execution_id, "UNSUPPORTED_CLAIM_RESULT",
                            payload["governed_result_commitment"]],
            payload, execution_id)
        return self._store._append_protected_adjudication(
            record, authority=_ADJUDICATION_ISSUANCE_AUTHORITY)

    def finalize_stage2(self, execution_id: str, expected_criteria: OracleScenarioCriteria,
                        stage1: Stage1Consensus,
                        submissions: tuple[Stage2Submission, ...], *,
                        claim_result_record_id: str | None = None) -> OfficialJudgment:
        authoritative = self.resolve_authoritative_criteria(execution_id)
        if expected_criteria != authoritative:
            raise ValueError("caller criteria do not exactly match authoritative Oracle scenario")
        if stage1.execution_id != execution_id:
            raise ValueError("Stage 1 consensus belongs to another execution")
        packet = self._verify_stage1(stage1)
        facts = self.derive_root_cause_facts(stage1)
        self._validate_review_set(submissions, stage="STAGE2")
        self._validate_registered_reviewers(
            execution_id, submissions, stage="STAGE2")
        expected_input = stage2_input_commitment(authoritative, stage1, facts.facts_commitment)
        if any(item.input_commitment != expected_input for item in submissions):
            raise ValueError("Stage 2 input does not bind exact Stage 1 and Oracle")
        if any(item.oracle_revision_id != authoritative.oracle_revision_id for item in submissions):
            raise ValueError("Stage 2 reviewer used a different Oracle revision")
        if any(item.derived_facts_commitment != facts.facts_commitment for item in submissions):
            raise ValueError("Stage 2 facts contradict authoritative derivation")
        claim_result = self._load_claim_result(
            execution_id, authoritative, stage1, claim_result_record_id)
        claim_payload = json.loads(claim_result.semantic_json)
        review_ids = []
        evaluations = {
            submission.reviewer_id: self._evaluate(
                authoritative, submission.top_hypothesis, facts)
            for submission in submissions}
        selected = self._select_consensus(submissions, stage="STAGE2")
        evaluation = evaluations[selected.reviewer_id]
        final = self._store.get(identity("observation", [execution_id, "final"]))
        final_payload = json.loads(final.semantic_json) if final is not None else {}
        machine_hard_failure = (
            final_payload.get("machine_hard_failure") is True
            or final_payload.get("disposition") in {
                "TERMINAL_PRODUCTION_FAILURE", "INVALID_EVALUATION"})
        if machine_hard_failure and evaluation.root_cause_identification_success:
            raise ValueError("machine hard failure cannot be human-overridden")
        for submission in submissions:
            submission_evaluation = evaluations[submission.reviewer_id]
            payload = {
                "stage": "STAGE2", "reviewer_id": submission.reviewer_id,
                "input_commitment": submission.input_commitment,
                "oracle_revision_id": submission.oracle_revision_id,
                "oracle_scenario_id": authoritative.scenario_id,
                "oracle_scenario_commitment": authoritative.scenario_commitment,
                "production_output_commitment": packet.source.production_output_commitment,
                "typed_claims_commitment": packet.source.typed_claims_commitment,
                "blind_packet_commitment": packet.packet_commitment,
                "stage1_consensus_facts_commitment": stage1.consensus_facts_commitment,
                "derived_facts_commitment": facts.facts_commitment,
                "top_hypothesis": {
                    "causal_class": submission.top_hypothesis.causal_class.value,
                    "granularity": submission.top_hypothesis.granularity,
                    "accepted_alternative_id": submission.top_hypothesis.accepted_alternative_id,
                },
                "root_cause_success":
                    submission_evaluation.root_cause_identification_success,
                "reason_code": submission.reason_code, "rationale": submission.rationale,
                "reviewed_at": submission.reviewed_at,
            }
            review = self._store.append(LedgerRecord.create(
                "review", [execution_id, "STAGE2", authoritative.scenario_commitment,
                           submission.reviewer_id], payload, execution_id,
            ))
            review_ids.append(review.record_id)
        scoring = {
            "canonical_causal_class_match": evaluation.canonical_causal_class_match,
            "approved_accepted_alternative_match": evaluation.approved_accepted_alternative_match,
            "top_hypothesis_accepted": evaluation.top_hypothesis_accepted,
            "granularity_valid": evaluation.granularity_valid,
            "required_evidence_satisfied": evaluation.required_evidence_satisfied,
            "forbidden_overclaim_detected": evaluation.forbidden_overclaim_detected.value,
            "conclusion_calibration_valid": evaluation.conclusion_calibration_valid,
            "knowledge_boundary_valid": evaluation.knowledge_boundary_valid,
            "root_cause_identification_success": evaluation.root_cause_identification_success,
            "disposition": evaluation.disposition.value,
        }
        payload = {
            "kind": "ORIGINAL", "oracle_revision_id": authoritative.oracle_revision_id,
            "governed_adjudicator_identity": ADJUDICATION_EVALUATOR_IDENTITY,
            "adjudicator_version": ADJUDICATION_EVALUATOR_VERSION,
            "evaluation_run_id": self._store.get(execution_id).parent_id,
            "execution_id": execution_id,
            "oracle_scenario_id": authoritative.scenario_id,
            "oracle_scenario_commitment": authoritative.scenario_commitment,
            "production_output_commitment": packet.source.production_output_commitment,
            "typed_claims_commitment": packet.source.typed_claims_commitment,
            "blind_packet_commitment": packet.packet_commitment,
            "stage1_consensus_fact_commitment": stage1.consensus_facts_commitment,
            "stage1_consensus_commitment": stage1.consensus_commitment,
            "derived_root_cause_facts_commitment": facts.facts_commitment,
            "final_scoring_commitment": commitment(scoring),
            "result": "PASS" if evaluation.root_cause_identification_success else "FAIL",
            "stage2_review_ids": review_ids, "root_cause_predicates": scoring,
            "claim_result_record_id": claim_result.record_id,
            "claim_result_record_commitment": claim_result.semantic_commitment,
            "unsupported_claim_result": claim_payload["unsupported_claim_result"],
        }
        source_records = (
            self._store.get(stage1.blind_packet_record_id),
            self._store.get(stage1.consensus_record_id),
            claim_result,
            *(self._store.get(item) for item in review_ids),
        )
        if any(item is None for item in source_records):
            raise ValueError("judgment source record is missing")
        payload["source_record_ids"] = [item.record_id for item in source_records]
        payload["source_record_commitments"] = [
            item.semantic_commitment for item in source_records]
        payload["predicate_scoring_basis_commitment"] = commitment({
            "root_cause": scoring,
            "unsupported_claim_result": payload["unsupported_claim_result"],
        })
        payload["judgment_commitment"] = commitment(payload)
        judgment = self._store._append_protected_adjudication(LedgerRecord.create(
            "judgment", [execution_id, "original"], payload, execution_id,
        ), authority=_ADJUDICATION_ISSUANCE_AUTHORITY)
        return OfficialJudgment(judgment, evaluation, tuple(review_ids))

    def _validate_blind_metadata(self, metadata: tuple[tuple[str, str], ...],
                                 criteria: OracleScenarioCriteria) -> None:
        if (not isinstance(metadata, tuple)
                or any(not isinstance(item, tuple) or len(item) != 2 for item in metadata)
                or tuple(sorted(metadata)) != metadata
                or len({item[0] for item in metadata}) != len(metadata)):
            raise ValueError("blind packet audit metadata must be canonical and unique")
        protected = {
            criteria.expected_causal_class.value, *criteria.accepted_alternatives,
            *self._isolation_policy.required_canaries,
        }
        canonical_protected = {_canonical_governed(value) for value in protected}
        for key, raw in metadata:
            _text(key, "audit metadata key", 160)
            _text(raw, "audit metadata value", 512)
            normalized_key = _canonical_governed(key)
            if any(name in normalized_key for name in _FORBIDDEN_METADATA_FIELDS):
                raise ValueError("blind packet contains forbidden Oracle provenance")
            for decoded in _decoded_variants(raw):
                canonical_value = _canonical_governed(decoded)
                if any(secret and secret in canonical_value for secret in canonical_protected):
                    raise ValueError("blind packet contains governed Oracle value or canary")

    def _validate_reviewed_facts(self, source: AuthoritativeProductionFacts,
                                 facts: tuple[Stage1ReviewedFact, ...]) -> None:
        _validate_reviewed_facts_material(source.material(), facts)

    @staticmethod
    def _evaluate(criteria: OracleScenarioCriteria, top: TopHypothesis,
                  facts: RootCauseFacts) -> RootCauseEvaluation:
        return evaluate_root_cause(
            criteria=criteria, top_hypothesis=top,
            lower_ranked_causal_classes=facts.lower_ranked_causal_classes,
            observed_evidence_classes=facts.observed_evidence_classes,
            detected_overclaim_ids=facts.detected_overclaim_ids,
            evidence_completeness=facts.evidence_completeness,
            conclusion_facts=facts.conclusion, knowledge_facts=facts.knowledge,
            claim_status=facts.claim_status, scorability=facts.scorability,
        )

    @staticmethod
    def _validate_review_set(submissions: tuple[object, ...], *, stage: str) -> None:
        if len(submissions) not in {2, 3}:
            raise ReviewIncomplete(f"{stage} requires two reviewers and a third on disagreement")
        for submission in submissions:
            submission.validate()
        reviewers = [submission.reviewer_id for submission in submissions]
        if len(reviewers) != len(set(reviewers)):
            raise ValueError("reviewers must be independent")
        if len({submission.input_commitment for submission in submissions}) != 1:
            raise ValueError("reviewers must evaluate the same committed input")
        if stage == "STAGE1" and len({submission.item_id for submission in submissions}) != 1:
            raise ValueError("Stage 1 reviewers must decide the same item")

    def _validate_registered_reviewers(
        self, execution_id: str, submissions: tuple[object, ...], *, stage: str,
    ) -> None:
        _, run, _ = self._require_execution(execution_id)
        run_protocol = json.loads(run.semantic_json).get("reviewer_protocol")
        if run_protocol != self._reviewer_protocol:
            raise ValueError("reviewer roster is not bound to the admitted run manifest")
        protocol_stage = self._reviewer_protocol[
            "stage1" if stage == "STAGE1" else "stage2"]
        primary = set(protocol_stage["reviewer_ids"])
        submitted = [item.reviewer_id for item in submissions]
        if set(submitted[:2]) != primary:
            raise ValueError(f"unknown or missing preregistered {stage} reviewer")
        if len(submitted) == 3:
            expected = protocol_stage["tiebreak_reviewer_id"]
            if submitted[2] != expected or expected in primary:
                raise ValueError(f"unregistered {stage} tie-break reviewer")

    @classmethod
    def _select_consensus(cls, submissions: tuple[object, ...], *, stage: str) -> object:
        cls._validate_review_set(submissions, stage=stage)
        if stage == "STAGE1":
            keys = [item.reviewed_facts_commitment for item in submissions]
        else:
            keys = [commitment({
                "causal_class": item.top_hypothesis.causal_class.value,
                "granularity": item.top_hypothesis.granularity,
                "alternative": item.top_hypothesis.accepted_alternative_id,
            }) for item in submissions]
        if keys[0] == keys[1]:
            if len(submissions) != 2:
                raise ValueError("third reviewer is only authorized for disagreement")
            return submissions[0]
        if len(submissions) != 3:
            raise ReviewIncomplete(f"{stage} reviewer disagreement requires a third reviewer")
        return submissions[2]

    def _load_blind_packet(self, reference: AdmittedBlindPacket) -> BlindPacket:
        if not isinstance(reference, AdmittedBlindPacket):
            raise TypeError("admitted blind packet reference is required")
        require_identity(reference.record_id, "observation")
        record = self._store.get(reference.record_id)
        if record is None or record.domain != "observation" or record.parent_id != reference.execution_id:
            raise ValueError("blind packet admission is unresolved")
        payload = json.loads(record.semantic_json)
        packet_data = payload.get("packet")
        if payload.get("kind") != "BLIND_PACKET" or not isinstance(packet_data, dict):
            raise ValueError("blind packet admission kind is invalid")
        if set(packet_data) != {
            "schema", "captured", "source", "audit_metadata",
            "isolation_policy_identity", "isolation_policy_commitment",
        }:
            raise ValueError("blind packet schema is invalid")
        captured_data = packet_data.get("captured")
        if not isinstance(captured_data, dict) or set(captured_data) != {
            "execution_id", "event_id", "event_content_commitment",
        }:
            raise ValueError("blind packet captured boundary is invalid")
        captured = CapturedInputBoundary(
            captured_data["execution_id"], captured_data["event_id"],
            captured_data["event_content_commitment"],
        )
        source = self._source_authority.resolve(captured)
        metadata_data = packet_data.get("audit_metadata")
        if not isinstance(metadata_data, list):
            raise ValueError("blind packet audit metadata is invalid")
        try:
            metadata = tuple((item[0], item[1]) for item in metadata_data)
        except (IndexError, TypeError) as exc:
            raise ValueError("blind packet audit metadata is invalid") from exc
        criteria = self.resolve_authoritative_criteria(captured.execution_id)
        self._validate_blind_metadata(metadata, criteria)
        if (packet_data.get("isolation_policy_identity")
                != self._isolation_policy.policy_identity
                or packet_data.get("isolation_policy_commitment")
                != self._isolation_policy.policy_commitment
                or payload.get("isolation_policy_commitment")
                != self._isolation_policy.policy_commitment
                or reference.isolation_policy_commitment
                != self._isolation_policy.policy_commitment):
            raise ValueError("blind packet trusted isolation policy mismatch")
        packet = BlindPacket(
            captured, source, metadata, self._isolation_policy.policy_identity,
            self._isolation_policy.policy_commitment,
        )
        if (reference.execution_id != captured.execution_id
                or payload.get("packet_commitment") != packet.packet_commitment
                or reference.packet_commitment != packet.packet_commitment
                or payload.get("source_commitment") != source.source_commitment
                or reference.source_commitment != source.source_commitment
                or packet.material() != packet_data):
            raise ValueError("blind packet authority or commitment changed")
        return packet

    def _verify_stage1(self, consensus: Stage1Consensus) -> BlindPacket:
        packet = self._load_blind_packet(AdmittedBlindPacket(
            consensus.blind_packet_record_id, consensus.execution_id,
            consensus.blind_packet_commitment, consensus.source_commitment,
            self._isolation_policy.policy_commitment,
        ))
        reviews = []
        for review_id in consensus.review_ids:
            record = self._store.get(review_id)
            if record is None or record.domain != "review" or record.parent_id != consensus.execution_id:
                raise ValueError("Stage 1 review lineage is unresolved")
            payload = json.loads(record.semantic_json)
            if (payload.get("stage") != "STAGE1"
                    or payload.get("item_id") != consensus.item_id
                    or payload.get("input_commitment") != packet.packet_commitment
                    or payload.get("blind_packet_record_id") != consensus.blind_packet_record_id
                    or payload.get("source_commitment") != packet.source.source_commitment):
                raise ValueError("Stage 1 review lineage contradicts consensus")
            facts = tuple(Stage1ReviewedFact.from_material(item)
                          for item in payload.get("reviewed_facts", ()))
            if payload.get("reviewed_facts_commitment") != commitment(
                    [item.material() for item in facts]):
                raise ValueError("Stage 1 review fact commitment is invalid")
            self._validate_reviewed_facts(packet.source, facts)
            reviews.append(facts)
        if len(reviews) not in {2, 3}:
            raise ValueError("Stage 1 consensus has incomplete review lineage")
        selected = reviews[0] if reviews[0] == reviews[1] else (
            reviews[2] if len(reviews) == 3 else None
        )
        material = {
            "execution_id": consensus.execution_id, "item_id": consensus.item_id,
            "blind_packet_record_id": consensus.blind_packet_record_id,
            "blind_packet_commitment": consensus.blind_packet_commitment,
            "source_commitment": consensus.source_commitment,
            "reviewed_facts": [item.material() for item in consensus.reviewed_facts],
            "consensus_facts_commitment": consensus.consensus_facts_commitment,
            "review_ids": list(consensus.review_ids),
        }
        if (selected != consensus.reviewed_facts
                or commitment([item.material() for item in selected or ()])
                != consensus.consensus_facts_commitment
                or commitment(material) != consensus.consensus_commitment):
            raise ValueError("Stage 1 consensus fact commitment is invalid")
        record = self._store.get(consensus.consensus_record_id)
        expected = {"stage": "STAGE1_CONSENSUS", **material,
                    "consensus_commitment": consensus.consensus_commitment}
        if (record is None or record.domain != "review"
                or json.loads(record.semantic_json) != expected):
            raise ValueError("Stage 1 consensus record is unresolved or contradictory")
        return packet

    def _load_claim_result(
        self, execution_id: str, criteria: OracleScenarioCriteria,
        stage1: Stage1Consensus, record_id: str | None,
    ) -> LedgerRecord:
        if record_id is None:
            raise ValueError("protected claim adjudication result is required")
        require_identity(record_id, "observation")
        record = self._store.get(record_id)
        if (record is None or record.domain != "observation"
                or record.parent_id != execution_id):
            raise ValueError("protected claim adjudication result is unresolved")
        payload = json.loads(record.semantic_json)
        packet = self._verify_stage1(stage1)
        expected_input = claim_adjudication_input_commitment(
            criteria, stage1, packet.source)
        result = payload.get("unsupported_claim_result")
        findings = payload.get("claim_adjudications")
        if (payload.get("kind") != "UNSUPPORTED_CLAIM_RESULT"
                or payload.get("governed_adjudicator_identity")
                    != ADJUDICATION_EVALUATOR_IDENTITY
                or payload.get("adjudicator_version")
                    != ADJUDICATION_EVALUATOR_VERSION
                or payload.get("execution_id") != execution_id
                or payload.get("oracle_revision_id") != criteria.oracle_revision_id
                or payload.get("oracle_scenario_commitment")
                    != criteria.scenario_commitment
                or payload.get("input_commitment") != expected_input
                or not isinstance(result, dict)
                or not isinstance(findings, list)):
            raise ValueError("protected claim adjudication binding is invalid")
        deterministic = {
            "status": result.get("status"),
            "numerator": sum(item.get("disposition") in {
                "UNSUPPORTED", "CONTRADICTED"} for item in findings
                              if isinstance(item, dict)),
            "denominator": len(findings),
            "contradicted_count": sum(
                item.get("disposition") == "CONTRADICTED" for item in findings
                if isinstance(item, dict)),
            "rate_fraction": (None if not findings else [
                sum(item.get("disposition") in {"UNSUPPORTED", "CONTRADICTED"}
                    for item in findings if isinstance(item, dict)), len(findings)]),
        }
        if result != deterministic:
            raise ValueError("protected UCR projection is not deterministic")
        return record

    def _require_execution(self, execution_id: str):
        require_identity(execution_id, "execution")
        self._store.readiness_check()
        execution = self._store.get(execution_id)
        if execution is None or execution.domain != "execution":
            raise ValueError("execution is not admitted")
        scenario_id = json.loads(execution.semantic_json).get("scenario_id")
        if scenario_id not in {f"S{i}" for i in range(1, 7)}:
            raise ValueError("execution scenario identity is invalid")
        run = self._store.get(execution.parent_id)
        if run is None or run.domain != "evaluation_run":
            raise ValueError("execution run is not admitted")
        self._store.require_formal_run(run.record_id)
        plan = self._store.get_plan(run.parent_id)
        if plan is None or json.loads(run.semantic_json).get("oracle_revision_id") != plan.oracle_revision_id:
            raise ValueError("execution plan or Oracle pin is invalid")
        return plan, run, scenario_id
