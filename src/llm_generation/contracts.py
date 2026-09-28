"""Immutable Candidate-D semantic values. Validation against public authority is a later phase."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from knowledge_index.contracts import RetrievalResolution
from rca_shared.claim_types import ClaimCategory
from rca_persistence.contracts import (
    AdmittedRetryDisposition as RetrySafety,
    DiagnosticConclusion,
    EvidenceCompleteness,
    EvidentialSupport,
    GenerationProvenance,
    GuidanceSource,
    LogicalTryIdentity,
)

from .identity import semantic_commitment, validated_result_id


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_DANGEROUS = re.compile(r"(?i)(api[_-]?key|password|bearer\s|authorization\s*:|secret|ground[_ -]?truth|scenario[_ -]?id|expected[_ -]?(answer|root[_ -]?cause|causal[_ -]?class)|validator[_ -]?answer)")
_OPAQUE_SECRET = re.compile(r"(?i)(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}")


def safe_text(value: object, name: str, limit: int = 4096, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value.encode("utf-8")) > limit or _DANGEROUS.search(value) or _OPAQUE_SECRET.search(value):
        raise ValueError(f"{name} must be bounded, non-secret and Ground-Truth-free")
    if identifier and not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{name} must be a stable opaque identifier")
    return value


def _tuple(value: object, kind: type, name: str, *, maximum: int = 64) -> tuple:
    if not isinstance(value, tuple) or len(value) > maximum or any(not isinstance(item, kind) for item in value):
        raise ValueError(f"{name} must be a bounded immutable tuple")
    return value


def _ids(value: object, name: str) -> tuple[str, ...]:
    result = _tuple(value, str, name)
    for item in result:
        safe_text(item, name, 160, identifier=True)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must be unique")
    return result


class FailureClass(str, Enum):
    MALFORMED_PROVIDER_OUTPUT = "MALFORMED_PROVIDER_OUTPUT"
    STRUCTURED_PARSE = "STRUCTURED_PARSE"
    RESULT_SCHEMA = "RESULT_SCHEMA"
    EVIDENCE_REFERENCE = "EVIDENCE_REFERENCE"
    KNOWLEDGE_AUTHORITY = "KNOWLEDGE_AUTHORITY"
    GROUNDING = "GROUNDING"
    INVALID_INPUT = "INVALID_INPUT"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_QUOTA = "PROVIDER_QUOTA"
    CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
    INVOCATION_BOUND = "INVOCATION_BOUND"
    IDENTITY_CONTRADICTION = "IDENTITY_CONTRADICTION"
    LOCAL_INTEGRITY = "LOCAL_INTEGRITY"


class CausalRelation(str, Enum):
    CAUSES = "CAUSES"
    CONTRIBUTES_TO = "CONTRIBUTES_TO"


class LocalReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"


@dataclass(frozen=True, slots=True)
class VersionedIdentity:
    identity: str
    version: str

    def __post_init__(self) -> None:
        safe_text(self.identity, "identity", 160, identifier=True)
        safe_text(self.version, "version", 160, identifier=True)


@dataclass(frozen=True, slots=True)
class GenerationPin:
    provider: str
    model: str
    profile: VersionedIdentity
    prompt: VersionedIdentity
    result_schema: VersionedIdentity
    configuration: VersionedIdentity

    def __post_init__(self) -> None:
        safe_text(self.provider, "provider", 160, identifier=True)
        safe_text(self.model, "model", 160, identifier=True)
        if any(not isinstance(item, VersionedIdentity) for item in (self.profile, self.prompt, self.result_schema, self.configuration)):
            raise TypeError("all generation resources require exact versioned identities")

    def matches_attempt(self, provenance: GenerationProvenance) -> bool:
        if not isinstance(provenance, GenerationProvenance):
            raise TypeError("expected public A-side GenerationProvenance")
        return (self.provider, self.model, self.profile.identity, self.prompt.identity, self.configuration.identity) == (
            provenance.provider_id, provenance.model_id, provenance.credential_profile_id,
            provenance.prompt_id, provenance.configuration_id,
        )


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    reference_id: str
    evidence_snapshot_id: str
    reference_schema_version: str
    canonical_path: str
    canonical_fact_commitment: str

    def __post_init__(self) -> None:
        for name in ("reference_id", "evidence_snapshot_id", "reference_schema_version"):
            safe_text(getattr(self, name), name, 160, identifier=True)
        safe_text(self.canonical_path, "canonical_path", 512)
        if not self.canonical_path.startswith("/") or "//" in self.canonical_path or ".." in self.canonical_path:
            raise ValueError("evidence path must be canonical")
        if not isinstance(self.canonical_fact_commitment, str) or not _HASH.fullmatch(self.canonical_fact_commitment):
            raise ValueError("invalid fact commitment")


@dataclass(frozen=True, slots=True)
class KnowledgeReference:
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
        for name in ("reference_id", "knowledge_snapshot_id", "corpus_id", "build_id", "index_id", "document_id", "document_version", "section_id", "chunk_id"):
            safe_text(getattr(self, name), name, 160, identifier=True)
        for name in ("snapshot_commitment", "content_commitment", "metadata_commitment"):
            if not isinstance(getattr(self, name), str) or not _HASH.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a SHA-256 commitment")


@dataclass(frozen=True, slots=True)
class CausalAssertion:
    cause_claim_ids: tuple[str, ...]
    effect_claim_ids: tuple[str, ...]
    relation: CausalRelation

    def __post_init__(self) -> None:
        if not _ids(self.cause_claim_ids, "cause_claim_ids") or not _ids(self.effect_claim_ids, "effect_claim_ids"):
            raise ValueError("causal assertion requires cause and effect Observed Facts")
        if set(self.cause_claim_ids) & set(self.effect_claim_ids):
            raise ValueError("causal cause and effect must be distinct")
        if not isinstance(self.relation, CausalRelation):
            raise TypeError("causal relation must be closed")


@dataclass(frozen=True, slots=True)
class Claim:
    claim_id: str
    category: ClaimCategory
    text: str
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    knowledge_reference_ids: tuple[str, ...] = ()
    evidential_support: EvidentialSupport | None = None
    causal_assertion: CausalAssertion | None = None

    def __post_init__(self) -> None:
        safe_text(self.claim_id, "claim_id", 160, identifier=True)
        if not isinstance(self.category, ClaimCategory):
            raise TypeError("claim category must be closed")
        safe_text(self.text, "claim text")
        for name in ("supporting_evidence_ids", "contradicting_evidence_ids", "knowledge_reference_ids"):
            _ids(getattr(self, name), name)
        if self.category is ClaimCategory.OBSERVED_FACT and (len(self.supporting_evidence_ids) != 1 or self.knowledge_reference_ids or self.evidential_support is not None):
            raise ValueError("observed fact requires exactly one evidence fact")
        if self.category is ClaimCategory.ANALYTICAL_INFERENCE and (not self.supporting_evidence_ids or not isinstance(self.evidential_support, EvidentialSupport)):
            raise ValueError("analytical inference requires evidence and support grade")
        if self.category is ClaimCategory.KNOWLEDGE_BACKED_GUIDANCE and not self.knowledge_reference_ids:
            raise ValueError("knowledge-backed guidance requires provenance")
        if self.category is ClaimCategory.MODEL_SUGGESTED_GUIDANCE and self.knowledge_reference_ids:
            raise ValueError("model suggestion cannot fabricate knowledge provenance")
        if self.causal_assertion is not None and (
            self.category is not ClaimCategory.ANALYTICAL_INFERENCE
            or not isinstance(self.causal_assertion, CausalAssertion)
        ):
            raise ValueError("only an Analytical Inference may carry CausalAssertion")


@dataclass(frozen=True, slots=True)
class CausalSupportProof:
    proof_id: str
    inference_claim_id: str
    causal_rule_id: str
    causal_rule_version: str
    supporting_observed_claim_ids: tuple[str, ...]
    evidence_snapshot_id: str
    proof_commitment: str

    def __post_init__(self) -> None:
        for name in ("proof_id", "inference_claim_id", "causal_rule_id", "causal_rule_version", "evidence_snapshot_id"):
            safe_text(getattr(self, name), name, 160, identifier=True)
        if not _ids(self.supporting_observed_claim_ids, "supporting_observed_claim_ids"):
            raise ValueError("causal proof requires observed support")
        if not isinstance(self.proof_commitment, str) or not _HASH.fullmatch(self.proof_commitment):
            raise ValueError("causal proof commitment is invalid")


@dataclass(frozen=True, slots=True)
class Hypothesis:
    rank: int
    statement: str
    evidential_support: EvidentialSupport
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    knowledge_reference_ids: tuple[str, ...]
    reasoning_summary: str
    claim_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.rank) is not int or self.rank < 1 or not isinstance(self.evidential_support, EvidentialSupport):
            raise ValueError("hypothesis rank/support invalid")
        for name in ("statement", "reasoning_summary"):
            safe_text(getattr(self, name), name)
        for name in ("supporting_evidence_ids", "contradicting_evidence_ids", "knowledge_reference_ids", "claim_ids"):
            _ids(getattr(self, name), name)
        if not self.supporting_evidence_ids or not self.claim_ids:
            raise ValueError("hypothesis needs evidence and claim coverage")


@dataclass(frozen=True, slots=True)
class Guidance:
    description: str
    source: GuidanceSource
    knowledge_reference_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        safe_text(self.description, "guidance")
        if not isinstance(self.source, GuidanceSource):
            raise TypeError("guidance source must be closed")
        _ids(self.knowledge_reference_ids, "knowledge_reference_ids")
        _ids(self.claim_ids, "claim_ids")
        if not self.claim_ids or (self.source is GuidanceSource.SOP_BACKED and not self.knowledge_reference_ids) or (self.source is GuidanceSource.MODEL_SUGGESTED and self.knowledge_reference_ids):
            raise ValueError("guidance authority or claim coverage invalid")


@dataclass(frozen=True, slots=True)
class GenerationInput:
    try_identity: LogicalTryIdentity
    operation_id: str
    evidence_snapshot_id: str
    evidence_revision_id: str
    knowledge_snapshot_id: str
    knowledge_resolution: RetrievalResolution
    pin: GenerationPin
    evidence_projection: str
    knowledge_projection: str
    degraded_authorization_reference: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.try_identity, LogicalTryIdentity) or not isinstance(self.pin, GenerationPin):
            raise TypeError("input needs canonical A-side Try identity and exact pin")
        if not isinstance(self.knowledge_resolution, RetrievalResolution):
            raise TypeError("input requires canonical Candidate-C typed resolution")
        for name in ("operation_id", "evidence_snapshot_id", "evidence_revision_id", "knowledge_snapshot_id"):
            safe_text(getattr(self, name), name, 160, identifier=True)
        for name in ("evidence_projection", "knowledge_projection"):
            safe_text(getattr(self, name), name, 32768)
        if self.degraded_authorization_reference is not None:
            safe_text(self.degraded_authorization_reference, "degraded_authorization_reference", 160, identifier=True)


@dataclass(frozen=True, slots=True)
class ValidationFact:
    stage: str
    contract_version: str
    commitment: str

    def __post_init__(self) -> None:
        safe_text(self.stage, "validation stage", 160, identifier=True)
        safe_text(self.contract_version, "validation contract version", 160, identifier=True)
        if not isinstance(self.commitment, str) or not _HASH.fullmatch(self.commitment):
            raise ValueError("validation fact requires commitment")


@dataclass(frozen=True, slots=True)
class InvocationMetadata:
    """Bounded, non-secret accounting facts; no provider payload or SDK object."""

    invocation_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    duration_ms: int

    def __post_init__(self) -> None:
        limits = {
            "invocation_count": (1, 1),
            "input_tokens": (0, 1_000_000),
            "output_tokens": (0, 1_000_000),
            "total_tokens": (0, 2_000_000),
            "duration_ms": (0, 600_000),
        }
        for name, (minimum, maximum) in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be bounded invocation metadata")
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("total token accounting is contradictory")


@dataclass(frozen=True, slots=True)
class GenerationFailure:
    try_identity: LogicalTryIdentity
    operation_id: str
    failure_class: FailureClass
    retry_safety: RetrySafety
    safe_detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.try_identity, LogicalTryIdentity):
            raise TypeError("canonical Try identity required")
        safe_text(self.operation_id, "operation_id", 160, identifier=True)
        if not isinstance(self.failure_class, FailureClass) or not isinstance(self.retry_safety, RetrySafety):
            raise TypeError("failure class and retry safety are independent closed enums")
        safe_text(self.safe_detail, "failure detail", 256)
        if self.failure_class in {FailureClass.IDENTITY_CONTRADICTION, FailureClass.LOCAL_INTEGRITY} and self.retry_safety is not RetrySafety.REPAIR_REQUIRED:
            raise ValueError("identity or integrity contradiction needs repair")
        if self.retry_safety is RetrySafety.RETRYABLE and self.failure_class not in {FailureClass.PROVIDER_TIMEOUT, FailureClass.PROVIDER_UNAVAILABLE, FailureClass.PROVIDER_QUOTA, FailureClass.MALFORMED_PROVIDER_OUTPUT, FailureClass.STRUCTURED_PARSE}:
            raise ValueError("failure class is not explicitly regeneration-safe")


@dataclass(frozen=True, slots=True)
class RecoveryFact:
    validated_result_id: str
    try_identity: LogicalTryIdentity
    operation_id: str
    semantic_commitment: str
    evidence_snapshot_id: str
    evidence_revision_id: str
    knowledge_snapshot_id: str
    pin: GenerationPin
    complete_and_resolvable: bool

    def __post_init__(self) -> None:
        if self.validated_result_id != validated_result_id(self.semantic_commitment):
            raise ValueError("recovery identity contradicts commitment")
        if not isinstance(self.try_identity, LogicalTryIdentity) or not isinstance(self.pin, GenerationPin) or type(self.complete_and_resolvable) is not bool:
            raise TypeError("recovery fact type invalid")
        for name in ("operation_id", "evidence_snapshot_id", "evidence_revision_id", "knowledge_snapshot_id"):
            safe_text(getattr(self, name), name, 160, identifier=True)


@dataclass(frozen=True, slots=True)
class ResultContent:
    input: GenerationInput
    contract_version: str
    summary: str
    summary_claim_ids: tuple[str, ...]
    severity_assessment: str
    severity_claim_ids: tuple[str, ...]
    diagnostic_conclusion: DiagnosticConclusion
    hypotheses: tuple[Hypothesis, ...]
    remediation: tuple[Guidance, ...]
    prevention: tuple[Guidance, ...]
    limitations: tuple[str, ...]
    evidence_completeness: EvidenceCompleteness
    knowledge_gap: bool
    evidence_references: tuple[EvidenceReference, ...]
    knowledge_references: tuple[KnowledgeReference, ...]
    claims: tuple[Claim, ...]
    validation_facts: tuple[ValidationFact, ...]
    invocation_metadata: InvocationMetadata
    causal_proofs: tuple[CausalSupportProof, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.input, GenerationInput):
            raise TypeError("result input invalid")
        if self.contract_version not in {"1", "2"}:
            raise ValueError("unsupported result contract version")
        if not isinstance(self.invocation_metadata, InvocationMetadata):
            raise TypeError("result needs bounded sanitized invocation metadata")
        safe_text(self.contract_version, "contract_version", 160, identifier=True)
        for name in ("summary", "severity_assessment"):
            safe_text(getattr(self, name), name)
        if not isinstance(self.diagnostic_conclusion, DiagnosticConclusion) or not isinstance(self.evidence_completeness, EvidenceCompleteness) or type(self.knowledge_gap) is not bool:
            raise TypeError("result conclusion/completeness invalid")
        for name, kind in (("hypotheses", Hypothesis), ("remediation", Guidance), ("prevention", Guidance), ("evidence_references", EvidenceReference), ("knowledge_references", KnowledgeReference), ("claims", Claim), ("validation_facts", ValidationFact)):
            _tuple(getattr(self, name), kind, name)
        _tuple(self.causal_proofs, CausalSupportProof, "causal_proofs")
        if self.contract_version == "1" and (
            self.causal_proofs or any(item.causal_assertion is not None for item in self.claims)
        ):
            raise ValueError("legacy result contract cannot carry causal semantics")
        if not self.hypotheses or tuple(h.rank for h in self.hypotheses) != tuple(range(1, len(self.hypotheses) + 1)) or not self.validation_facts:
            raise ValueError("ranked hypotheses and validation facts required")
        _tuple(self.limitations, str, "limitations")
        for value in self.limitations:
            safe_text(value, "limitation")
        for name in ("evidence_references", "knowledge_references", "claims"):
            values = getattr(self, name)
            if len({item.reference_id if name != "claims" else item.claim_id for item in values}) != len(values):
                raise ValueError(f"{name} identities must be unique")
        evidence = {item.reference_id for item in self.evidence_references}
        knowledge = {item.reference_id for item in self.knowledge_references}
        claims = {item.claim_id for item in self.claims}
        claim_by_id = {item.claim_id: item for item in self.claims}
        proof_ids: set[str] = set()
        proof_inference_ids: set[str] = set()
        for proof in self.causal_proofs:
            inference = claim_by_id.get(proof.inference_claim_id)
            if proof.proof_id in proof_ids or proof.inference_claim_id in proof_inference_ids:
                raise ValueError("causal proof identity or inference must be unique")
            proof_ids.add(proof.proof_id)
            proof_inference_ids.add(proof.inference_claim_id)
            if (inference is None or inference.category is not ClaimCategory.ANALYTICAL_INFERENCE
                    or inference.causal_assertion is None
                    or proof.evidence_snapshot_id != self.input.evidence_snapshot_id
                    or proof.supporting_observed_claim_ids != (
                        inference.causal_assertion.cause_claim_ids + inference.causal_assertion.effect_claim_ids
                    )):
                raise ValueError("causal proof does not resolve to one local assertion")
        for claim in self.claims:
            assertion = claim.causal_assertion
            if assertion is None:
                continue
            for claim_id in assertion.cause_claim_ids + assertion.effect_claim_ids:
                observed = claim_by_id.get(claim_id)
                if observed is None or observed.category is not ClaimCategory.OBSERVED_FACT:
                    raise ValueError("causal endpoints must resolve to local Observed Facts")
        for name in ("summary_claim_ids", "severity_claim_ids"):
            if not set(_ids(getattr(self, name), name)) <= claims:
                raise ValueError(f"{name} must reference typed result claims")
        if any(item.evidence_snapshot_id != self.input.evidence_snapshot_id for item in self.evidence_references) or any(item.knowledge_snapshot_id != self.input.knowledge_snapshot_id for item in self.knowledge_references):
            raise ValueError("reference Snapshot lineage mismatch")
        for item in (*self.claims, *self.hypotheses):
            if not set(item.supporting_evidence_ids + item.contradicting_evidence_ids) <= evidence or not set(item.knowledge_reference_ids) <= knowledge:
                raise ValueError("unresolved claim or hypothesis reference")
        for item in (*self.hypotheses, *self.remediation, *self.prevention):
            if not set(item.claim_ids) <= claims:
                raise ValueError("unresolved presentation claim coverage")
        if self.knowledge_gap and any(item.source is GuidanceSource.SOP_BACKED for item in (*self.remediation, *self.prevention)):
            raise ValueError("knowledge gap cannot have SOP-backed guidance")


@dataclass(frozen=True, slots=True)
class ValidatedGenerationResult:
    validated_result_id: str
    semantic_commitment: str
    content: ResultContent

    def __post_init__(self) -> None:
        if not isinstance(self.content, ResultContent):
            raise TypeError("result content invalid")
        expected = semantic_commitment(self.content)
        if self.semantic_commitment != expected or self.validated_result_id != validated_result_id(expected):
            raise ValueError("result identity contradicts immutable semantic content")

    @classmethod
    def from_content(cls, content: ResultContent) -> "ValidatedGenerationResult":
        commitment = semantic_commitment(content)
        return cls(validated_result_id(commitment), commitment, content)
