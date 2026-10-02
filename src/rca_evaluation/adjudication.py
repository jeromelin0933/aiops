"""Authority-bound blind review and append-only Stage 2 adjudication."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from enum import Enum

from rca_persistence.contracts import EvidenceCompleteness

from .calibration import ConclusionFacts, KnowledgeBoundaryInput
from .claims import ClaimEvaluationStatus
from .identity import commitment, require_identity
from .isolation import _decoded_variants
from .ledger import LedgerRecord
from .leakage_policy import TRUSTED_LEAKAGE_POLICY, TrustedLeakagePolicy
from .observation import CapturedInputBoundary
from .scoring import (
    CausalClass, ExecutionScorability, OracleScenarioCriteria,
    RootCauseEvaluation, TopHypothesis, evaluate_root_cause,
)
from .source_authority import AuthoritativeProductionFacts, S3SourceAuthority
from .sqlite_store import SqliteEvaluationStore


class ReviewerAuthority(str, Enum):
    HUMAN = "HUMAN"
    LLM_EXPLORATORY = "LLM_EXPLORATORY"


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
                 expected_policy_commitment: str) -> None:
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
        self._store = store
        self._source_authority = source_authority
        self._isolation_policy = isolation_policy

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

    def finalize_stage2(self, execution_id: str, expected_criteria: OracleScenarioCriteria,
                        stage1: Stage1Consensus,
                        submissions: tuple[Stage2Submission, ...]) -> OfficialJudgment:
        authoritative = self.resolve_authoritative_criteria(execution_id)
        if expected_criteria != authoritative:
            raise ValueError("caller criteria do not exactly match authoritative Oracle scenario")
        if stage1.execution_id != execution_id:
            raise ValueError("Stage 1 consensus belongs to another execution")
        packet = self._verify_stage1(stage1)
        facts = self.derive_root_cause_facts(stage1)
        self._validate_review_set(submissions, stage="STAGE2")
        expected_input = stage2_input_commitment(authoritative, stage1, facts.facts_commitment)
        if any(item.input_commitment != expected_input for item in submissions):
            raise ValueError("Stage 2 input does not bind exact Stage 1 and Oracle")
        if any(item.oracle_revision_id != authoritative.oracle_revision_id for item in submissions):
            raise ValueError("Stage 2 reviewer used a different Oracle revision")
        if any(item.derived_facts_commitment != facts.facts_commitment for item in submissions):
            raise ValueError("Stage 2 facts contradict authoritative derivation")
        review_ids = []
        evaluations: dict[str, RootCauseEvaluation] = {}
        for submission in submissions:
            evaluation = self._evaluate(authoritative, submission.top_hypothesis, facts)
            evaluations[submission.reviewer_id] = evaluation
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
                "root_cause_success": evaluation.root_cause_identification_success,
                "reason_code": submission.reason_code, "rationale": submission.rationale,
                "reviewed_at": submission.reviewed_at,
            }
            review = self._store.append(LedgerRecord.create(
                "review", [execution_id, "STAGE2", authoritative.scenario_commitment,
                           submission.reviewer_id], payload, execution_id,
            ))
            review_ids.append(review.record_id)
        selected = self._select_consensus(submissions, stage="STAGE2")
        evaluation = evaluations[selected.reviewer_id]
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
        }
        judgment = self._store.append(LedgerRecord.create(
            "judgment", [execution_id, "original"], payload, execution_id,
        ))
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
            source.validated_result_id: source.production_output_commitment,
            source.evidence_snapshot_id: source.evidence_commitment,
            source.knowledge_snapshot_id: source.knowledge_commitment,
        }
        for item in facts:
            if allowed_sources.get(item.source_identity) != item.source_commitment:
                raise ValueError("Stage 1 reviewed fact source lineage is invalid")
        contradiction = by_kind[ReviewedFactKind.MATERIAL_CONTRADICTION][0]
        if source.machine_major_contradiction and contradiction.determination is FactDetermination.FALSE:
            raise ValueError("Stage 1 contradiction fact contradicts production authority")
        competing = by_kind[ReviewedFactKind.COMPETING_ALTERNATIVE][0]
        if source.machine_competing_alternatives and competing.determination is FactDetermination.FALSE:
            raise ValueError("Stage 1 competing-alternative fact contradicts production authority")

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
        plan = self._store.get_plan(run.parent_id)
        if plan is None or json.loads(run.semantic_json).get("oracle_revision_id") != plan.oracle_revision_id:
            raise ValueError("execution plan or Oracle pin is invalid")
        return plan, run, scenario_id
