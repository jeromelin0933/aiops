"""Persistence-neutral Candidate A contracts for SPEC-012.

This module defines RCA-domain vocabulary and semantic ports only.  It owns no
SQLite schema, Incident mutation, runtime scheduling, evidence collection,
knowledge retrieval, generation, validation, or cross-store orchestration.
External-domain facts are retained as opaque, non-secret references.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Protocol

from rca_shared.claim_types import ClaimCategory


class GenerationLifecycle(str, Enum):
    PENDING = "PENDING"
    GENERATING = "GENERATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EvidenceCompleteness(str, Enum):
    FULL = "FULL"
    DEGRADED = "DEGRADED"


class CurrentFreshness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"


class DiagnosticConclusion(str, Enum):
    IDENTIFIED = "IDENTIFIED"
    MOST_SUPPORTED = "MOST_SUPPORTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class EvidentialSupport(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ArtifactStatementKind(str, Enum):
    OBSERVED_FACT = "OBSERVED_FACT"
    ANALYTICAL_INFERENCE = "ANALYTICAL_INFERENCE"
    KNOWLEDGE_BACKED_GUIDANCE = "KNOWLEDGE_BACKED_GUIDANCE"


class GuidanceSource(str, Enum):
    SOP_BACKED = "SOP_BACKED"
    MODEL_SUGGESTED = "MODEL_SUGGESTED"


class LogicalTryResultKind(str, Enum):
    VALIDATED_RESULT = "VALIDATED_RESULT"
    FAILURE = "FAILURE"


class AdmittedRetryDisposition(str, Enum):
    """A Candidate-D decision recorded by Candidate A, not computed here."""

    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


class VersionRole(str, Enum):
    COMMITTED_UNPUBLISHED = "COMMITTED_UNPUBLISHED"
    CURRENT = "CURRENT"
    HISTORICAL = "HISTORICAL"


class PublicationDisposition(str, Enum):
    A_SIDE_COMMITTED = "A_SIDE_COMMITTED"
    APPLIED = "APPLIED"
    PRECONDITION_SUPERSEDED = "PRECONDITION_SUPERSEDED"
    TARGET_ALREADY_CURRENT_CONFLICT = "TARGET_ALREADY_CURRENT_CONFLICT"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


class RecoveryCandidateKind(str, Enum):
    COMMITTED_UNPUBLISHED_VERSION = "COMMITTED_UNPUBLISHED_VERSION"
    UNRESOLVED_PUBLICATION = "UNRESOLVED_PUBLICATION"
    ATTEMPT_TRY_RECONCILIATION = "ATTEMPT_TRY_RECONCILIATION"
    STALE_CURRENT = "STALE_CURRENT"


class RcaErrorCode(str, Enum):
    INVALID_CONTRACT = "INVALID_CONTRACT"
    NOT_FOUND = "NOT_FOUND"
    SEMANTIC_CONFLICT = "SEMANTIC_CONFLICT"
    IDENTITY_LINEAGE_CONFLICT = "IDENTITY_LINEAGE_CONFLICT"
    RECEIPT_REPLAY_CONFLICT = "RECEIPT_REPLAY_CONFLICT"
    INVALID_REFERENCE = "INVALID_REFERENCE"
    INTEGRITY_CORRUPTION = "INTEGRITY_CORRUPTION"
    SCHEMA_INCOMPATIBILITY = "SCHEMA_INCOMPATIBILITY"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    LOCAL_READINESS_FAILURE = "LOCAL_READINESS_FAILURE"
    PUBLICATION_EVIDENCE_INCONSISTENCY = "PUBLICATION_EVIDENCE_INCONSISTENCY"


class RcaFailureDisposition(str, Enum):
    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


RCA_ERROR_DISPOSITIONS: Mapping[RcaErrorCode, RcaFailureDisposition] = MappingProxyType(
    {
        RcaErrorCode.INVALID_CONTRACT: RcaFailureDisposition.NON_RETRYABLE,
        RcaErrorCode.NOT_FOUND: RcaFailureDisposition.NON_RETRYABLE,
        RcaErrorCode.SEMANTIC_CONFLICT: RcaFailureDisposition.NON_RETRYABLE,
        RcaErrorCode.IDENTITY_LINEAGE_CONFLICT: RcaFailureDisposition.REPAIR_REQUIRED,
        RcaErrorCode.RECEIPT_REPLAY_CONFLICT: RcaFailureDisposition.REPAIR_REQUIRED,
        RcaErrorCode.INVALID_REFERENCE: RcaFailureDisposition.NON_RETRYABLE,
        RcaErrorCode.INTEGRITY_CORRUPTION: RcaFailureDisposition.REPAIR_REQUIRED,
        RcaErrorCode.SCHEMA_INCOMPATIBILITY: RcaFailureDisposition.REPAIR_REQUIRED,
        RcaErrorCode.MIGRATION_REQUIRED: RcaFailureDisposition.REPAIR_REQUIRED,
        RcaErrorCode.REPAIR_REQUIRED: RcaFailureDisposition.REPAIR_REQUIRED,
        RcaErrorCode.LOCAL_READINESS_FAILURE: RcaFailureDisposition.RETRYABLE,
        RcaErrorCode.PUBLICATION_EVIDENCE_INCONSISTENCY: RcaFailureDisposition.REPAIR_REQUIRED,
    }
)


class RcaDomainError(RuntimeError):
    """Typed, safe Candidate A failure."""

    def __init__(
        self,
        code: RcaErrorCode,
        message: str,
        *,
        operation_id: str | None = None,
        aggregate_id: str | None = None,
        attempt_id: str | None = None,
        version_id: str | None = None,
        field_path: str | None = None,
    ) -> None:
        if not isinstance(code, RcaErrorCode):
            raise TypeError("code must be an RcaErrorCode")
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        super().__init__(message)
        self.code = code
        self.retry_disposition = RCA_ERROR_DISPOSITIONS[code]
        self.operation_id = operation_id
        self.aggregate_id = aggregate_id
        self.attempt_id = attempt_id
        self.version_id = version_id
        self.field_path = field_path


class RcaContractError(RcaDomainError):
    def __init__(self, message: str, *, field_path: str | None = None) -> None:
        super().__init__(RcaErrorCode.INVALID_CONTRACT, message, field_path=field_path)


def _fail(message: str, field_path: str | None = None) -> None:
    raise RcaContractError(message, field_path=field_path)


def _reference(value: object, field_name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(
            f"{field_name} must be a non-empty reference without surrounding whitespace",
            field_name,
        )
    if any(character in value for character in ("\n", "\r", "\x00")):
        _fail(f"{field_name} must be a single-line opaque reference", field_name)
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(f"{field_name} must be non-empty text without surrounding whitespace", field_name)
    return value


def _aware_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail(f"{field_name} must be a timezone-aware datetime", field_name)
    return value.astimezone(timezone.utc)


def _tuple_of_references(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        _fail(f"{field_name} must be an iterable of references", field_name)
    try:
        references = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        _fail(f"{field_name} must be an iterable of references", field_name)
    for reference in references:
        _reference(reference, field_name)
    if len(references) != len(set(references)):
        _fail(f"{field_name} must contain unique references", field_name)
    return references


def _tuple_of_text(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        _fail(f"{field_name} must be an iterable of text values", field_name)
    try:
        values = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        _fail(f"{field_name} must be an iterable of text values", field_name)
    for item in values:
        _text(item, field_name)
    return values


def _require_enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        _fail(f"{field_name} must be a {enum_type.__name__}", field_name)


@dataclass(frozen=True, slots=True)
class RcaAggregate:
    aggregate_id: str
    incident_id: str

    def __post_init__(self) -> None:
        _reference(self.aggregate_id, "aggregate_id")
        _reference(self.incident_id, "incident_id")


@dataclass(frozen=True, slots=True)
class GenerationProvenance:
    """Only stable, non-secret generation identities are legal here."""

    provider_id: str
    model_id: str
    prompt_id: str
    configuration_id: str
    credential_profile_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "provider_id",
            "model_id",
            "prompt_id",
            "configuration_id",
            "credential_profile_id",
        ):
            _reference(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class AttemptLineage:
    attempt_id: str
    aggregate_id: str
    evidence_snapshot_id: str
    evidence_revision_id: str
    knowledge_snapshot_id: str
    generation_provenance: GenerationProvenance

    def __post_init__(self) -> None:
        for field_name in (
            "attempt_id",
            "aggregate_id",
            "evidence_snapshot_id",
            "evidence_revision_id",
            "knowledge_snapshot_id",
        ):
            _reference(getattr(self, field_name), field_name)
        if not isinstance(self.generation_provenance, GenerationProvenance):
            _fail("generation_provenance must be GenerationProvenance", "generation_provenance")

    @property
    def credential_profile_id(self) -> str:
        return self.generation_provenance.credential_profile_id


def require_equivalent_attempt_lineage(
    existing: AttemptLineage, candidate: AttemptLineage
) -> AttemptLineage:
    """Return an equivalent replay or fail on stable-identity contradiction."""

    if not isinstance(existing, AttemptLineage) or not isinstance(candidate, AttemptLineage):
        raise TypeError("existing and candidate must be AttemptLineage values")
    if existing.attempt_id != candidate.attempt_id:
        raise RcaDomainError(
            RcaErrorCode.SEMANTIC_CONFLICT,
            "attempt lineage comparison requires the same attempt_id",
            attempt_id=candidate.attempt_id,
        )
    if existing != candidate:
        raise RcaDomainError(
            RcaErrorCode.IDENTITY_LINEAGE_CONFLICT,
            "attempt_id resolves to contradictory immutable lineage",
            attempt_id=candidate.attempt_id,
        )
    return existing


@dataclass(frozen=True, slots=True)
class GenerationAttempt:
    lineage: AttemptLineage
    lifecycle: GenerationLifecycle
    latest_try_ordinal: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.lineage, AttemptLineage):
            _fail("lineage must be AttemptLineage", "lineage")
        _require_enum(self.lifecycle, GenerationLifecycle, "lifecycle")
        if self.latest_try_ordinal is not None and (
            isinstance(self.latest_try_ordinal, bool)
            or not isinstance(self.latest_try_ordinal, int)
            or self.latest_try_ordinal < 1
        ):
            _fail("latest_try_ordinal must be a positive integer or None", "latest_try_ordinal")


@dataclass(frozen=True, slots=True)
class LogicalTryIdentity:
    attempt_id: str
    try_ordinal: int

    def __post_init__(self) -> None:
        _reference(self.attempt_id, "attempt_id")
        if isinstance(self.try_ordinal, bool) or not isinstance(self.try_ordinal, int) or self.try_ordinal < 1:
            _fail("try_ordinal must be a positive logical ordinal", "try_ordinal")


@dataclass(frozen=True, slots=True)
class LogicalTryOutcome:
    identity: LogicalTryIdentity
    result_kind: LogicalTryResultKind
    retry_disposition: AdmittedRetryDisposition
    occurred_at: datetime
    validated_result_id: str | None = None
    failure_code: str | None = None
    safe_failure_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity, LogicalTryIdentity):
            _fail("identity must be LogicalTryIdentity", "identity")
        _require_enum(self.result_kind, LogicalTryResultKind, "result_kind")
        _require_enum(self.retry_disposition, AdmittedRetryDisposition, "retry_disposition")
        object.__setattr__(self, "occurred_at", _aware_utc(self.occurred_at, "occurred_at"))
        _reference(self.validated_result_id, "validated_result_id", nullable=True)
        _reference(self.failure_code, "failure_code", nullable=True)
        if self.safe_failure_message is not None:
            _text(self.safe_failure_message, "safe_failure_message")
        if self.result_kind is LogicalTryResultKind.VALIDATED_RESULT:
            if self.validated_result_id is None or any(
                value is not None for value in (self.failure_code, self.safe_failure_message)
            ):
                _fail("validated result requires only validated_result_id")
        elif self.validated_result_id is not None or self.failure_code is None:
            _fail("failure result requires failure_code and no validated_result_id")


@dataclass(frozen=True, slots=True)
class AttemptLineageRead:
    """One point-in-time coherent Attempt core, summary, and Try history."""

    attempt: GenerationAttempt
    try_outcomes: tuple[LogicalTryOutcome, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, GenerationAttempt):
            _fail("attempt must be GenerationAttempt", "attempt")
        try:
            outcomes = tuple(self.try_outcomes)
        except TypeError:
            _fail("try_outcomes must be an ordered iterable", "try_outcomes")
        if any(not isinstance(outcome, LogicalTryOutcome) for outcome in outcomes):
            _fail("try_outcomes must contain LogicalTryOutcome values", "try_outcomes")
        attempt_id = self.attempt.lineage.attempt_id
        if any(outcome.identity.attempt_id != attempt_id for outcome in outcomes):
            _fail("all Try outcomes must belong to the read Attempt", "try_outcomes")
        ordinals = tuple(outcome.identity.try_ordinal for outcome in outcomes)
        if ordinals != tuple(range(1, len(outcomes) + 1)):
            _fail("Try history must be ordered and continuous from ordinal 1", "try_outcomes")
        for outcome in outcomes[:-1]:
            if outcome.result_kind is LogicalTryResultKind.VALIDATED_RESULT:
                _fail(
                    "validated Try result must be the final outcome in Attempt history",
                    "try_outcomes",
                )
            if outcome.retry_disposition is not AdmittedRetryDisposition.RETRYABLE:
                _fail(
                    "terminal Try disposition must be the final outcome in Attempt history",
                    "try_outcomes",
                )
        expected_latest = None if not outcomes else outcomes[-1].identity.try_ordinal
        if self.attempt.latest_try_ordinal != expected_latest:
            _fail("Attempt lifecycle summary must identify the latest durable Try", "attempt")
        if self.attempt.lifecycle in {
            GenerationLifecycle.PENDING,
            GenerationLifecycle.GENERATING,
        } and outcomes:
            _fail(
                "non-terminal Attempt lifecycle cannot summarize a terminal Try outcome",
                "attempt.lifecycle",
            )
        if self.attempt.lifecycle is GenerationLifecycle.COMPLETED and (
            not outcomes or outcomes[-1].result_kind is not LogicalTryResultKind.VALIDATED_RESULT
        ):
            _fail("COMPLETED Attempt requires a latest validated Try result", "attempt.lifecycle")
        if self.attempt.lifecycle is GenerationLifecycle.FAILED and (
            not outcomes or outcomes[-1].result_kind is not LogicalTryResultKind.FAILURE
        ):
            _fail("FAILED Attempt requires a latest failure Try result", "attempt.lifecycle")
        object.__setattr__(self, "try_outcomes", outcomes)


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    reference_id: str
    statement_kind: ArtifactStatementKind
    description: str

    def __post_init__(self) -> None:
        _reference(self.reference_id, "reference_id")
        _require_enum(self.statement_kind, ArtifactStatementKind, "statement_kind")
        _text(self.description, "description")


@dataclass(frozen=True, slots=True)
class KnowledgeReference:
    reference_id: str
    corpus_id: str
    index_id: str
    document_id: str
    document_version: str
    section_id: str
    chunk_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "reference_id",
            "corpus_id",
            "index_id",
            "document_id",
            "document_version",
            "section_id",
            "chunk_id",
        ):
            _reference(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class ArtifactEvidenceFact:
    reference_id: str
    evidence_snapshot_id: str
    reference_schema_version: str
    canonical_path: str
    canonical_fact_commitment: str

    def __post_init__(self) -> None:
        for name in ("reference_id", "evidence_snapshot_id", "reference_schema_version", "canonical_path", "canonical_fact_commitment"):
            _reference(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class ArtifactKnowledgeFact:
    reference_id: str
    knowledge_snapshot_id: str
    snapshot_commitment: str
    corpus_id: str
    build_id: str
    index_id: str
    document_id: str
    document_version: str
    section_id: str
    chunk_id: str
    content_commitment: str
    metadata_commitment: str

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            _reference(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class ArtifactClaim:
    claim_id: str
    category: ClaimCategory
    text: str
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    knowledge_reference_ids: tuple[str, ...]
    evidential_support: EvidentialSupport | None = None

    def __post_init__(self) -> None:
        _reference(self.claim_id, "claim_id")
        _require_enum(self.category, ClaimCategory, "category")
        _text(self.text, "text")
        for name in ("supporting_evidence_ids", "contradicting_evidence_ids", "knowledge_reference_ids"):
            if not isinstance(getattr(self, name), tuple):
                _fail(f"{name} must be an explicit tuple", name)
            _tuple_of_references(getattr(self, name), name)
        if self.evidential_support is not None:
            _require_enum(self.evidential_support, EvidentialSupport, "evidential_support")


@dataclass(frozen=True, slots=True)
class ArtifactCausalAssertion:
    inference_claim_id: str
    cause_claim_ids: tuple[str, ...]
    effect_claim_ids: tuple[str, ...]
    relation: str

    def __post_init__(self) -> None:
        _reference(self.inference_claim_id, "inference_claim_id")
        for name in ("cause_claim_ids", "effect_claim_ids"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not values:
                _fail(f"{name} must be an explicit non-empty tuple", name)
            _tuple_of_references(values, name)
        if set(self.cause_claim_ids) & set(self.effect_claim_ids):
            _fail("causal endpoints must be distinct", "cause_claim_ids")
        if self.relation not in {"CAUSES", "CONTRIBUTES_TO"}:
            _fail("unsupported causal relation", "relation")


@dataclass(frozen=True, slots=True)
class ArtifactCausalSupportProof:
    proof_id: str
    inference_claim_id: str
    causal_rule_id: str
    causal_rule_version: str
    supporting_observed_claim_ids: tuple[str, ...]
    evidence_snapshot_id: str
    proof_commitment: str

    def __post_init__(self) -> None:
        for name in ("proof_id", "inference_claim_id", "causal_rule_id", "causal_rule_version", "evidence_snapshot_id", "proof_commitment"):
            _reference(getattr(self, name), name)
        if not isinstance(self.supporting_observed_claim_ids, tuple) or not self.supporting_observed_claim_ids:
            _fail("causal proof requires explicit Observed Fact support", "supporting_observed_claim_ids")
        _tuple_of_references(self.supporting_observed_claim_ids, "supporting_observed_claim_ids")


@dataclass(frozen=True, slots=True)
class ArtifactClaimSemantics:
    """A-side structural record of admitted D semantics; no grounding authority."""

    schema_version: str
    claims: tuple[ArtifactClaim, ...]
    evidence_references: tuple[ArtifactEvidenceFact, ...]
    knowledge_references: tuple[ArtifactKnowledgeFact, ...]
    summary_claim_ids: tuple[str, ...]
    severity_claim_ids: tuple[str, ...]
    hypothesis_claim_ids: tuple[tuple[str, ...], ...]
    remediation_claim_ids: tuple[tuple[str, ...], ...]
    prevention_claim_ids: tuple[tuple[str, ...], ...]
    causal_assertions: tuple[ArtifactCausalAssertion, ...] = ()
    causal_proofs: tuple[ArtifactCausalSupportProof, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version not in {"1", "2"}:
            _fail("unsupported claim semantics version", "schema_version")
        if self.schema_version == "1" and (self.causal_assertions or self.causal_proofs):
            _fail("legacy typed claim schema cannot carry causal semantics", "schema_version")
        for name, kind in (("claims", ArtifactClaim), ("evidence_references", ArtifactEvidenceFact), ("knowledge_references", ArtifactKnowledgeFact)):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(not isinstance(item, kind) for item in values):
                _fail(f"{name} must be an immutable typed tuple", name)
            identity = "claim_id" if name == "claims" else "reference_id"
            if len({getattr(item, identity) for item in values}) != len(values):
                _fail(f"{name} identities must be unique", name)
        claim_ids = {item.claim_id for item in self.claims}
        claim_by_id = {item.claim_id: item for item in self.claims}
        evidence_ids = {item.reference_id for item in self.evidence_references}
        knowledge_ids = {item.reference_id for item in self.knowledge_references}
        if not isinstance(self.causal_assertions, tuple) or any(
            not isinstance(item, ArtifactCausalAssertion) for item in self.causal_assertions
        ) or not isinstance(self.causal_proofs, tuple) or any(
            not isinstance(item, ArtifactCausalSupportProof) for item in self.causal_proofs
        ):
            _fail("causal semantics must be immutable typed tuples", "causal_assertions")
        assertions = {item.inference_claim_id: item for item in self.causal_assertions}
        if len(assertions) != len(self.causal_assertions):
            _fail("causal assertion inference identity must be unique", "causal_assertions")
        for assertion in self.causal_assertions:
            inference = claim_by_id.get(assertion.inference_claim_id)
            if inference is None or inference.category is not ClaimCategory.ANALYTICAL_INFERENCE:
                _fail("causal assertion must resolve to an Analytical Inference", "causal_assertions")
            for claim_id in assertion.cause_claim_ids + assertion.effect_claim_ids:
                endpoint = claim_by_id.get(claim_id)
                if endpoint is None or endpoint.category is not ClaimCategory.OBSERVED_FACT:
                    _fail("causal endpoint must resolve to an Observed Fact", "causal_assertions")
        if len({item.proof_id for item in self.causal_proofs}) != len(self.causal_proofs) or len({
            item.inference_claim_id for item in self.causal_proofs
        }) != len(self.causal_proofs):
            _fail("causal proof identity must be unique", "causal_proofs")
        for proof in self.causal_proofs:
            assertion = assertions.get(proof.inference_claim_id)
            if assertion is None or proof.supporting_observed_claim_ids != (
                assertion.cause_claim_ids + assertion.effect_claim_ids
            ):
                _fail("causal proof must resolve to local Observed Facts", "causal_proofs")
        for claim in self.claims:
            if not set(claim.supporting_evidence_ids + claim.contradicting_evidence_ids) <= evidence_ids:
                _fail("claim contains unresolved Evidence reference", "claims")
            if not set(claim.knowledge_reference_ids) <= knowledge_ids:
                _fail("claim contains unresolved Knowledge reference", "claims")
        for name in ("summary_claim_ids", "severity_claim_ids"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not set(_tuple_of_references(values, name)) <= claim_ids:
                _fail(f"{name} contains unresolved claim", name)
        for name in ("hypothesis_claim_ids", "remediation_claim_ids", "prevention_claim_ids"):
            groups = getattr(self, name)
            if not isinstance(groups, tuple):
                _fail(f"{name} must be an explicit tuple", name)
            for group in groups:
                if not isinstance(group, tuple) or not set(_tuple_of_references(group, name)) <= claim_ids:
                    _fail(f"{name} contains unresolved claim", name)


@dataclass(frozen=True, slots=True)
class ArtifactProvenance:
    evidence_snapshot_id: str
    evidence_revision_id: str
    knowledge_snapshot_id: str
    evidence_references: tuple[EvidenceReference, ...]
    knowledge_references: tuple[KnowledgeReference, ...]
    generation: GenerationProvenance

    def __post_init__(self) -> None:
        for field_name in (
            "evidence_snapshot_id",
            "evidence_revision_id",
            "knowledge_snapshot_id",
        ):
            _reference(getattr(self, field_name), field_name)
        evidence = tuple(self.evidence_references)
        knowledge = tuple(self.knowledge_references)
        if any(not isinstance(item, EvidenceReference) for item in evidence):
            _fail("evidence_references must contain EvidenceReference values")
        if any(not isinstance(item, KnowledgeReference) for item in knowledge):
            _fail("knowledge_references must contain KnowledgeReference values")
        if len({item.reference_id for item in evidence}) != len(evidence):
            _fail("evidence reference identities must be unique")
        if len({item.reference_id for item in knowledge}) != len(knowledge):
            _fail("knowledge reference identities must be unique")
        if not isinstance(self.generation, GenerationProvenance):
            _fail("generation must be GenerationProvenance", "generation")
        object.__setattr__(self, "evidence_references", evidence)
        object.__setattr__(self, "knowledge_references", knowledge)


@dataclass(frozen=True, slots=True)
class TypedArtifactProvenanceRead:
    """Read projection of persisted typed provenance, never a second store."""

    evidence_snapshot_id: str
    evidence_revision_id: str
    knowledge_snapshot_id: str
    evidence_references: tuple[ArtifactEvidenceFact, ...]
    knowledge_references: tuple[ArtifactKnowledgeFact, ...]
    generation: GenerationProvenance

    def __post_init__(self) -> None:
        for name in ("evidence_snapshot_id", "evidence_revision_id", "knowledge_snapshot_id"):
            _reference(getattr(self, name), name)
        if not isinstance(self.evidence_references, tuple) or any(
            not isinstance(item, ArtifactEvidenceFact) for item in self.evidence_references
        ):
            _fail("typed Evidence provenance must be an immutable tuple")
        if not isinstance(self.knowledge_references, tuple) or any(
            not isinstance(item, ArtifactKnowledgeFact) for item in self.knowledge_references
        ):
            _fail("typed Knowledge provenance must be an immutable tuple")
        if not isinstance(self.generation, GenerationProvenance):
            _fail("generation must be GenerationProvenance", "generation")


@dataclass(frozen=True, slots=True)
class RcaHypothesis:
    rank: int
    statement: str
    evidential_support: EvidentialSupport
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    knowledge_reference_ids: tuple[str, ...]
    reasoning_summary: str

    def __post_init__(self) -> None:
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            _fail("rank must be a positive integer", "rank")
        _text(self.statement, "statement")
        _require_enum(self.evidential_support, EvidentialSupport, "evidential_support")
        object.__setattr__(self, "supporting_evidence_ids", _tuple_of_references(self.supporting_evidence_ids, "supporting_evidence_ids"))
        object.__setattr__(self, "contradicting_evidence_ids", _tuple_of_references(self.contradicting_evidence_ids, "contradicting_evidence_ids"))
        object.__setattr__(self, "knowledge_reference_ids", _tuple_of_references(self.knowledge_reference_ids, "knowledge_reference_ids"))
        _text(self.reasoning_summary, "reasoning_summary")


@dataclass(frozen=True, slots=True)
class RcaAction:
    description: str
    source: GuidanceSource
    knowledge_reference_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.description, "description")
        _require_enum(self.source, GuidanceSource, "source")
        references = _tuple_of_references(self.knowledge_reference_ids, "knowledge_reference_ids")
        if self.source is GuidanceSource.SOP_BACKED and not references:
            _fail("SOP_BACKED action requires knowledge provenance", "knowledge_reference_ids")
        object.__setattr__(self, "knowledge_reference_ids", references)


@dataclass(frozen=True, slots=True)
class RcaArtifact:
    summary: str
    severity_assessment: str
    diagnostic_conclusion: DiagnosticConclusion
    hypotheses: tuple[RcaHypothesis, ...]
    remediation_actions: tuple[RcaAction, ...]
    prevention_actions: tuple[RcaAction, ...]
    limitations: tuple[str, ...]
    evidence_completeness: EvidenceCompleteness
    knowledge_gap: bool
    provenance: ArtifactProvenance
    claim_semantics: ArtifactClaimSemantics | None = None

    def __post_init__(self) -> None:
        _text(self.summary, "summary")
        _text(self.severity_assessment, "severity_assessment")
        _require_enum(self.diagnostic_conclusion, DiagnosticConclusion, "diagnostic_conclusion")
        _require_enum(self.evidence_completeness, EvidenceCompleteness, "evidence_completeness")
        if not isinstance(self.knowledge_gap, bool):
            _fail("knowledge_gap must be a bool", "knowledge_gap")
        hypotheses = tuple(self.hypotheses)
        if not hypotheses or any(not isinstance(item, RcaHypothesis) for item in hypotheses):
            _fail("hypotheses must contain at least one RcaHypothesis", "hypotheses")
        if tuple(item.rank for item in hypotheses) != tuple(range(1, len(hypotheses) + 1)):
            _fail("hypothesis ranks must be ordered, unique, and continuous from 1", "hypotheses")
        remediation = tuple(self.remediation_actions)
        prevention = tuple(self.prevention_actions)
        if any(not isinstance(item, RcaAction) for item in remediation + prevention):
            _fail("action collections must contain RcaAction values")
        if not isinstance(self.provenance, ArtifactProvenance):
            _fail("provenance must be ArtifactProvenance", "provenance")
        semantics = self.claim_semantics
        if semantics is not None:
            if not isinstance(semantics, ArtifactClaimSemantics):
                _fail("claim_semantics must be typed", "claim_semantics")
            if (len(semantics.hypothesis_claim_ids) != len(hypotheses)
                    or len(semantics.remediation_claim_ids) != len(remediation)
                    or len(semantics.prevention_claim_ids) != len(prevention)):
                _fail("presentation coverage does not match Artifact fields", "claim_semantics")
            if any(item.evidence_snapshot_id != self.provenance.evidence_snapshot_id for item in semantics.evidence_references):
                _fail("Evidence fact Snapshot lineage mismatch", "claim_semantics")
            if any(item.knowledge_snapshot_id != self.provenance.knowledge_snapshot_id for item in semantics.knowledge_references):
                _fail("Knowledge fact Snapshot lineage mismatch", "claim_semantics")
            if any(item.evidence_snapshot_id != self.provenance.evidence_snapshot_id for item in semantics.causal_proofs):
                _fail("causal proof Evidence Snapshot lineage mismatch", "claim_semantics")
        limitations = _tuple_of_text(self.limitations, "limitations")
        if semantics is None:
            evidence_ids = {item.reference_id for item in self.provenance.evidence_references}
            knowledge_ids = {item.reference_id for item in self.provenance.knowledge_references}
        else:
            evidence_ids = {item.reference_id for item in semantics.evidence_references}
            knowledge_ids = {item.reference_id for item in semantics.knowledge_references}
            if not {item.reference_id for item in self.provenance.evidence_references} <= evidence_ids:
                _fail("legacy Evidence provenance adds an unadmitted typed reference", "provenance")
            if not {item.reference_id for item in self.provenance.knowledge_references} <= knowledge_ids:
                _fail("legacy Knowledge provenance adds an unadmitted typed reference", "provenance")
            typed_knowledge = {item.reference_id: item for item in semantics.knowledge_references}
            for legacy in self.provenance.knowledge_references:
                typed = typed_knowledge[legacy.reference_id]
                for name in ("corpus_id", "index_id", "document_id", "document_version", "section_id", "chunk_id"):
                    if getattr(legacy, name) != getattr(typed, name):
                        _fail("Knowledge reference identity contradicts typed provenance", "provenance")
        for hypothesis in hypotheses:
            if not set(hypothesis.supporting_evidence_ids + hypothesis.contradicting_evidence_ids) <= evidence_ids:
                _fail("hypothesis contains unresolved evidence provenance", "hypotheses")
            if not set(hypothesis.knowledge_reference_ids) <= knowledge_ids:
                _fail("hypothesis contains unresolved knowledge provenance", "hypotheses")
        for action in remediation + prevention:
            if not set(action.knowledge_reference_ids) <= knowledge_ids:
                _fail("action contains unresolved knowledge provenance", "actions")
        object.__setattr__(self, "hypotheses", hypotheses)
        object.__setattr__(self, "remediation_actions", remediation)
        object.__setattr__(self, "prevention_actions", prevention)
        object.__setattr__(self, "limitations", limitations)


@dataclass(frozen=True, slots=True)
class RcaVersion:
    version_id: str
    aggregate_id: str
    version_number: int
    attempt_id: str
    artifact: RcaArtifact
    publication_operation_id: str
    role: VersionRole

    def __post_init__(self) -> None:
        for field_name in ("version_id", "aggregate_id", "attempt_id", "publication_operation_id"):
            _reference(getattr(self, field_name), field_name)
        if isinstance(self.version_number, bool) or not isinstance(self.version_number, int) or self.version_number < 1:
            _fail("version_number must be a positive integer", "version_number")
        if not isinstance(self.artifact, RcaArtifact):
            _fail("artifact must be RcaArtifact", "artifact")
        _require_enum(self.role, VersionRole, "role")


@dataclass(frozen=True, slots=True)
class PublicationTargetIdentity:
    """Stable publication replay identity; execution time is intentionally absent."""

    publication_operation_id: str
    aggregate_id: str
    incident_id: str
    target_version_id: str
    expected_current_version_id: str | None

    def __post_init__(self) -> None:
        for field_name in (
            "publication_operation_id",
            "aggregate_id",
            "incident_id",
            "target_version_id",
        ):
            _reference(getattr(self, field_name), field_name)
        _reference(self.expected_current_version_id, "expected_current_version_id", nullable=True)


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    target: PublicationTargetIdentity
    authoritative_now: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.target, PublicationTargetIdentity):
            _fail("target must be PublicationTargetIdentity", "target")
        object.__setattr__(self, "authoritative_now", _aware_utc(self.authoritative_now, "authoritative_now"))


@dataclass(frozen=True, slots=True)
class PublicationResult:
    target: PublicationTargetIdentity
    disposition: PublicationDisposition
    recorded_at: datetime
    resulting_current_version_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, PublicationTargetIdentity):
            _fail("target must be PublicationTargetIdentity", "target")
        _require_enum(self.disposition, PublicationDisposition, "disposition")
        object.__setattr__(self, "recorded_at", _aware_utc(self.recorded_at, "recorded_at"))
        _reference(self.resulting_current_version_id, "resulting_current_version_id", nullable=True)
        if self.disposition is PublicationDisposition.APPLIED:
            if self.resulting_current_version_id != self.target.target_version_id:
                _fail("APPLIED publication must make its target the resulting Current")
        elif self.disposition is PublicationDisposition.A_SIDE_COMMITTED:
            if self.resulting_current_version_id is not None:
                _fail("A-side commit is not authorized Current evidence")


@dataclass(frozen=True, slots=True)
class CurrentRca:
    aggregate_id: str
    current_version_id: str
    freshness: CurrentFreshness
    material_evidence_revision_basis: str

    def __post_init__(self) -> None:
        _reference(self.aggregate_id, "aggregate_id")
        _reference(self.current_version_id, "current_version_id")
        _require_enum(self.freshness, CurrentFreshness, "freshness")
        _reference(self.material_evidence_revision_basis, "material_evidence_revision_basis")


@dataclass(frozen=True, slots=True)
class CurrentRcaRead:
    """One point-in-time coherent Current relationship, Version, and Artifact."""

    current: CurrentRca
    version: RcaVersion
    artifact: RcaArtifact
    attempt_lineage: AttemptLineageRead
    publication_result: PublicationResult

    def __post_init__(self) -> None:
        if not isinstance(self.current, CurrentRca):
            _fail("current must be CurrentRca", "current")
        if not isinstance(self.version, RcaVersion):
            _fail("version must be RcaVersion", "version")
        if not isinstance(self.artifact, RcaArtifact):
            _fail("artifact must be RcaArtifact", "artifact")
        if not isinstance(self.attempt_lineage, AttemptLineageRead):
            _fail("attempt_lineage must be AttemptLineageRead", "attempt_lineage")
        if not isinstance(self.publication_result, PublicationResult):
            _fail("publication_result must be PublicationResult", "publication_result")
        if self.current.aggregate_id != self.version.aggregate_id:
            _fail("Current and Version must belong to the same Aggregate", "version.aggregate_id")
        if self.current.current_version_id != self.version.version_id:
            _fail("Current must identify the returned Version", "current.current_version_id")
        if self.version.role is not VersionRole.CURRENT:
            _fail("Current read Version must have CURRENT role", "version.role")
        if self.version.artifact != self.artifact:
            _fail("Current Version and returned Artifact must be identical", "artifact")
        if self.attempt_lineage.attempt.lineage.attempt_id != self.version.attempt_id:
            _fail("Current lineage must identify the Version Attempt", "attempt_lineage")
        if (
            self.publication_result.disposition is not PublicationDisposition.APPLIED
            or self.publication_result.target.target_version_id != self.version.version_id
            or self.publication_result.resulting_current_version_id != self.version.version_id
        ):
            _fail("Current requires matching authorized APPLIED publication evidence")


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    kind: RecoveryCandidateKind
    aggregate_id: str
    attempt_id: str | None = None
    version_id: str | None = None
    publication_operation_id: str | None = None

    def __post_init__(self) -> None:
        _require_enum(self.kind, RecoveryCandidateKind, "kind")
        _reference(self.aggregate_id, "aggregate_id")
        for field_name in ("attempt_id", "version_id", "publication_operation_id"):
            _reference(getattr(self, field_name), field_name, nullable=True)
        if self.kind in {
            RecoveryCandidateKind.COMMITTED_UNPUBLISHED_VERSION,
            RecoveryCandidateKind.UNRESOLVED_PUBLICATION,
        } and (self.version_id is None or self.publication_operation_id is None):
            _fail("publication recovery candidate requires version and operation identities")
        if self.kind is RecoveryCandidateKind.ATTEMPT_TRY_RECONCILIATION and self.attempt_id is None:
            _fail("Try reconciliation candidate requires attempt_id")
        if self.kind is RecoveryCandidateKind.STALE_CURRENT and self.version_id is None:
            _fail("stale Current candidate requires version_id")


@dataclass(frozen=True, slots=True)
class CreateAggregateRequest:
    operation_id: str
    aggregate_id: str
    incident_id: str
    authoritative_now: datetime

    def __post_init__(self) -> None:
        for field_name in ("operation_id", "aggregate_id", "incident_id"):
            _reference(getattr(self, field_name), field_name)
        object.__setattr__(self, "authoritative_now", _aware_utc(self.authoritative_now, "authoritative_now"))

    @property
    def replay_identity(self) -> tuple[str, str, str]:
        return self.operation_id, self.aggregate_id, self.incident_id


@dataclass(frozen=True, slots=True)
class AdmitAttemptRequest:
    operation_id: str
    lineage: AttemptLineage
    authoritative_now: datetime

    def __post_init__(self) -> None:
        _reference(self.operation_id, "operation_id")
        if not isinstance(self.lineage, AttemptLineage):
            _fail("lineage must be AttemptLineage", "lineage")
        object.__setattr__(self, "authoritative_now", _aware_utc(self.authoritative_now, "authoritative_now"))

    @property
    def replay_identity(self) -> tuple[str, AttemptLineage]:
        return self.operation_id, self.lineage


class RcaReadPort(Protocol):
    """Purpose-specific reads; no writable persistence primitive is exposed."""

    def get_aggregate(self, aggregate_id: str) -> RcaAggregate | None: ...
    def get_aggregate_by_incident(self, incident_id: str) -> RcaAggregate | None: ...
    def get_attempt_lineage(self, attempt_id: str) -> AttemptLineageRead | None: ...
    def get_version(self, version_id: str) -> RcaVersion | None: ...
    def get_version_history(self, aggregate_id: str) -> tuple[RcaVersion, ...]: ...
    def get_artifact(self, version_id: str) -> RcaArtifact | None: ...
    def get_artifact_provenance(self, version_id: str) -> ArtifactProvenance | TypedArtifactProvenanceRead | None: ...
    def get_current(self, aggregate_id: str) -> CurrentRcaRead | None: ...
    def get_freshness_lineage(self, aggregate_id: str) -> tuple[CurrentRca, ...]: ...
    def get_publication_result(self, publication_operation_id: str) -> PublicationResult | None: ...
    def enumerate_recovery_candidates(self) -> tuple[RecoveryCandidate, ...]: ...
    def validate_local_readiness(self) -> None: ...


class RcaMutationPort(Protocol):
    """Semantic commands only; scheduling and cross-store composition stay external."""

    def create_or_discover_aggregate(self, request: CreateAggregateRequest) -> RcaAggregate: ...
    def admit_attempt(self, request: AdmitAttemptRequest) -> GenerationAttempt: ...
    def mark_attempt_generating(
        self,
        operation_id: str,
        attempt_id: str,
        authoritative_now: datetime,
    ) -> GenerationAttempt: ...
    def record_try_outcome(self, operation_id: str, outcome: LogicalTryOutcome) -> LogicalTryOutcome: ...
    def commit_validated_artifact(
        self,
        operation_id: str,
        attempt_id: str,
        artifact: RcaArtifact,
        publication_target: PublicationTargetIdentity,
        authoritative_now: datetime,
    ) -> RcaVersion: ...
    def complete_authorized_publication(self, result: PublicationResult) -> PublicationResult: ...
    def apply_authorized_freshness(self, current: CurrentRca) -> CurrentRca: ...
