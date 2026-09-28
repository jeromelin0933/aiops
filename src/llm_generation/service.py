"""Provider-neutral, single-invocation Candidate-D generation flow."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import json
from typing import Protocol

from incident_evidence.contracts import EvidenceRevision, EvidenceSnapshot
from knowledge_index.contracts import (
    KnowledgeProvenanceProjection, KnowledgeReadStatus, KnowledgeSnapshotEnvelope,
    KnowledgeSnapshotKey, RetrievalResolution,
)
from rca_persistence.contracts import AdmittedRetryDisposition as RetrySafety, AttemptLineageRead, LogicalTryIdentity

from .config import GenerationConfig, GenerationConfigError, load_generation_config
from .contracts import (
    FailureClass, GenerationFailure, GenerationInput, InvocationMetadata,
    LocalReadStatus, ValidatedGenerationResult, safe_text,
)
from .identity import semantic_commitment
from .ports import AttemptPublicReader, EvidencePublicReader, KnowledgePublicReader
from .schema import StructuredOutputError, _FORBIDDEN_CONTEXT, parse_structured_output
from .sqlite_store import CandidateDStore
from .validation import (
    DegradedAuthorizationFact, GenerationValidationError,
    derive_causal_support_proof, validate_generation_result,
)


class ProviderFailureKind(str, Enum):
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    QUOTA = "QUOTA"
    RATE = "RATE"
    RESOURCE = "RESOURCE"
    CAPABILITY = "CAPABILITY"
    BOUNDS = "BOUNDS"
    MALFORMED = "MALFORMED"


class ProviderInvocationError(Exception):
    """Adapter-owned typed failure. Exception text is never persisted."""

    def __init__(self, kind: ProviderFailureKind, *, request_sent: bool = False):
        if not isinstance(kind, ProviderFailureKind):
            raise TypeError("closed provider failure kind required")
        super().__init__(kind.value)
        self.kind = kind
        self.request_sent = request_sent


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    provider: str
    model: str
    profile_id: str
    prompt: str
    schema_id: str
    schema_version: str
    timeout_seconds: int
    maximum_output_bytes: int
    maximum_output_tokens: int


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    structured_output: str | bytes | dict[str, object]
    metadata: InvocationMetadata


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    provider: str
    model: str
    profile_id: str
    schema_id: str
    schema_version: str
    hidden_retries_disabled: bool


class GenerationProvider(Protocol):
    capability: ProviderCapability
    def input_token_upper_bound(self, prompt: str, timeout_seconds: int) -> int | None:
        """Return a safe bound for this exact selected model, or None."""
        ...
    def invoke_once(self, request: ProviderRequest) -> ProviderResponse: ...


@dataclass(frozen=True, slots=True)
class ExecutionAuthorization:
    """Caller-presented permission for this exact physical execution, not D authority."""

    operation_id: str
    try_identity: LogicalTryIdentity
    expected_a_outcomes: int
    authorized: bool

    def __post_init__(self) -> None:
        safe_text(self.operation_id, "operation_id", 160, identifier=True)
        if (not isinstance(self.try_identity, LogicalTryIdentity)
            or type(self.expected_a_outcomes) is not int or self.expected_a_outcomes < 0
            or type(self.authorized) is not bool):
            raise ValueError("exact bounded execution authorization required")


@dataclass(frozen=True, slots=True)
class AvailableResources:
    rate_units: int
    quota_units: int
    cost_units: int
    resource_units: int

    def __post_init__(self) -> None:
        if any(type(getattr(self, name)) is not int or getattr(self, name) < 0 for name in (
            "rate_units", "quota_units", "cost_units", "resource_units",
        )):
            raise ValueError("resource facts must be bounded nonnegative integers")


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    status: LocalReadStatus
    result: ValidatedGenerationResult | None = None
    failure: GenerationFailure | None = None
    physical_invocations: int = 0
    admitted: bool = False
    token_preflight_calls: int = 0


def canonical_evidence_projection(snapshot: EvidenceSnapshot) -> str:
    """Bounded allowlist rendered only from one public Candidate-B Snapshot."""
    if not isinstance(snapshot, EvidenceSnapshot):
        raise ValueError("public Evidence Snapshot required")
    content = snapshot.snapshot_content
    rendered = json.dumps({
        "snapshot_id": snapshot.snapshot_id,
        "revision_id": snapshot.revision_id,
        "completeness": snapshot.completeness.value,
        "event_projections": content["event_projections"],
        "semantic_evidence": content["semantic_evidence"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    safe_text(rendered, "admitted Evidence projection", 32768)
    if _FORBIDDEN_CONTEXT.search(rendered):
        raise ValueError("Evidence projection contains forbidden evaluation context")
    return rendered


def canonical_knowledge_projection(
    snapshot: KnowledgeSnapshotEnvelope, provenance: KnowledgeProvenanceProjection,
) -> str:
    """Render included public chunks with exact public governance provenance."""
    if (not isinstance(snapshot, KnowledgeSnapshotEnvelope)
        or not isinstance(provenance, KnowledgeProvenanceProjection)
        or snapshot.snapshot_key != provenance.snapshot_identity
        or snapshot.snapshot_commitment != provenance.snapshot_commitment):
        raise ValueError("public Knowledge Snapshot/provenance mismatch")
    included = tuple(item for item in provenance.chunks if item.included)
    if tuple(item.chunk_identity for item in snapshot.chunks) != tuple(
        item.chunk_identity for item in included
    ):
        raise ValueError("included Knowledge chunks differ from public provenance")
    chunks = []
    for chunk, fact in zip(snapshot.chunks, included):
        if (chunk.content_commitment != fact.content_commitment
            or chunk.metadata_commitment != fact.metadata_commitment
            or chunk.document_identity != fact.document_identity
            or chunk.document_version_identity != fact.document_version_identity
            or chunk.section_identity != fact.section_identity
            or chunk.knowledge_type != fact.knowledge_type
            or chunk.guidance_authority != fact.guidance_authority
            or chunk.sop_backed_eligible is not fact.sop_backed_eligible):
            raise ValueError("Knowledge content/governance differs from public provenance")
        chunks.append({
            "chunk_id": chunk.chunk_identity,
            "document_id": chunk.document_identity,
            "document_version": chunk.document_version_identity,
            "section_id": chunk.section_identity,
            "content": chunk.content,
            "content_commitment": chunk.content_commitment,
            "knowledge_type": chunk.knowledge_type,
            "guidance_authority": chunk.guidance_authority,
            "sop_backed_eligible": chunk.sop_backed_eligible,
            "content_truncated": chunk.content_truncated,
        })
    rendered = json.dumps({
        "snapshot_id": snapshot.snapshot_key.value,
        "snapshot_commitment": snapshot.snapshot_commitment,
        "resolution": snapshot.resolution.value,
        "knowledge_gap": snapshot.knowledge_gap,
        "chunks": chunks,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    safe_text(rendered, "admitted Knowledge projection", 32768)
    if _FORBIDDEN_CONTEXT.search(rendered):
        raise ValueError("Knowledge projection contains forbidden evaluation context")
    return rendered


def render_prompt(source: GenerationInput, config: GenerationConfig) -> str:
    """Use only versioned template and exact admitted projection values."""
    if source.pin != config.pin:
        raise ValueError("prompt configuration pin differs")
    for value in (config.prompt_template, source.evidence_projection, source.knowledge_projection):
        safe_text(value, "prompt component", 32768)
        if _FORBIDDEN_CONTEXT.search(value):
            raise ValueError("prompt contains forbidden evaluation context")
    prompt = json.dumps({
        "prompt_identity": config.pin.prompt.identity,
        "prompt_version": config.pin.prompt.version,
        "template": config.prompt_template,
        "evidence": source.evidence_projection,
        "knowledge": source.knowledge_projection,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(prompt.encode("utf-8")) > min(config.bounds.maximum_request_bytes, config.bounds.maximum_input_bytes):
        raise ValueError("rendered prompt exceeds request/input bound")
    return prompt


def same_try_reinvocation_eligible(
    source: GenerationInput,
    store: CandidateDStore,
    attempts: AttemptPublicReader,
) -> bool:
    """Safety fact only; never authorizes or performs a physical invocation."""
    if store.local_readiness() is not LocalReadStatus.FOUND:
        return False
    result = store.result_for_try(source.try_identity)
    failures = store.failures_for_try(source.try_identity)
    if (result.status is not LocalReadStatus.NOT_FOUND
        or failures.status is not LocalReadStatus.FOUND):
        return False
    commitment = subject_input_commitment(source)
    if any(
        failure.same_try_eligible is not True
        or failure.subject_input_commitment != commitment
        for failure in failures.value
    ):
        return False
    try:
        view = attempts.get_attempt_lineage(source.try_identity.attempt_id)
    except Exception:
        return False
    if not isinstance(view, AttemptLineageRead):
        return False
    lineage = view.attempt.lineage
    return (
        lineage.attempt_id == source.try_identity.attempt_id
        and lineage.evidence_snapshot_id == source.evidence_snapshot_id
        and lineage.evidence_revision_id == source.evidence_revision_id
        and lineage.knowledge_snapshot_id == source.knowledge_snapshot_id
        and source.pin.matches_attempt(lineage.generation_provenance)
        and not any(item.identity == source.try_identity for item in view.try_outcomes)
    )


def subject_input_commitment(source: GenerationInput) -> str:
    """Commit request semantics while deliberately excluding execution operation identity."""
    return semantic_commitment(replace(source, operation_id="subject-operation"))


def _lineage_matches(source: GenerationInput, view: AttemptLineageRead) -> bool:
    lineage = view.attempt.lineage
    return (
        lineage.attempt_id == source.try_identity.attempt_id
        and lineage.evidence_snapshot_id == source.evidence_snapshot_id
        and lineage.evidence_revision_id == source.evidence_revision_id
        and lineage.knowledge_snapshot_id == source.knowledge_snapshot_id
        and source.pin.matches_attempt(lineage.generation_provenance)
    )


_PROVIDER_FAILURE = {
    ProviderFailureKind.TIMEOUT: FailureClass.PROVIDER_TIMEOUT,
    ProviderFailureKind.UNAVAILABLE: FailureClass.PROVIDER_UNAVAILABLE,
    ProviderFailureKind.QUOTA: FailureClass.PROVIDER_QUOTA,
    ProviderFailureKind.RATE: FailureClass.PROVIDER_QUOTA,
    ProviderFailureKind.RESOURCE: FailureClass.PROVIDER_QUOTA,
    ProviderFailureKind.CAPABILITY: FailureClass.CAPABILITY_MISMATCH,
    ProviderFailureKind.BOUNDS: FailureClass.INVOCATION_BOUND,
    ProviderFailureKind.MALFORMED: FailureClass.MALFORMED_PROVIDER_OUTPUT,
}

_SAME_TRY_SAFE_FAILURES = frozenset({
    FailureClass.PROVIDER_TIMEOUT,
    FailureClass.PROVIDER_UNAVAILABLE,
    FailureClass.PROVIDER_QUOTA,
    FailureClass.MALFORMED_PROVIDER_OUTPUT,
    FailureClass.STRUCTURED_PARSE,
})


def _same_try_safety_fact(kind: FailureClass, safety: RetrySafety) -> bool:
    """Approved D-local policy emits eligibility independently of retry disposition."""
    return safety is RetrySafety.RETRYABLE and kind in _SAME_TRY_SAFE_FAILURES


class GenerationService:
    def __init__(
        self, store: CandidateDStore, provider: GenerationProvider,
        attempts: AttemptPublicReader, evidence: EvidencePublicReader,
        knowledge: KnowledgePublicReader,
    ):
        self.store = store
        self.provider = provider
        self.attempts = attempts
        self.evidence = evidence
        self.knowledge = knowledge

    @staticmethod
    def _local_fault(source: GenerationInput, status: LocalReadStatus,
                     calls: int = 0, preflight_calls: int = 0) -> ExecutionReceipt:
        kind = (FailureClass.IDENTITY_CONTRADICTION
                if status is LocalReadStatus.REPAIR_REQUIRED else FailureClass.LOCAL_INTEGRITY)
        return ExecutionReceipt(
            status, failure=GenerationFailure(
                source.try_identity, source.operation_id, kind,
                RetrySafety.REPAIR_REQUIRED, "Candidate-D authority is uncertain",
            ), physical_invocations=calls, admitted=calls > 0,
            token_preflight_calls=preflight_calls,
        )

    def _failure(self, source: GenerationInput, kind: FailureClass,
                 safety: RetrySafety, detail: str, calls: int = 0,
                 preflight_calls: int = 0) -> ExecutionReceipt:
        failure = GenerationFailure(
            source.try_identity, source.operation_id, kind, safety, detail,
            subject_input_commitment(source), _same_try_safety_fact(kind, safety),
        )
        committed = self.store.commit_failure(failure)
        if committed.status is LocalReadStatus.FOUND:
            return ExecutionReceipt(LocalReadStatus.FOUND, failure=committed.value,
                                    physical_invocations=calls, admitted=calls > 0,
                                    token_preflight_calls=preflight_calls)
        return self._local_fault(source, committed.status, calls, preflight_calls)

    def _admit(
        self, source: GenerationInput, config: GenerationConfig,
        grant: ExecutionAuthorization, resources: AvailableResources,
        degraded_authorization: DegradedAuthorizationFact | None,
    ) -> FailureClass | None:
        if (not isinstance(grant, ExecutionAuthorization) or grant.authorized is not True
            or grant.operation_id != source.operation_id or grant.try_identity != source.try_identity):
            return FailureClass.INVALID_INPUT
        try:
            view = self.attempts.get_attempt_lineage(source.try_identity.attempt_id)
        except Exception:
            return FailureClass.LOCAL_INTEGRITY
        if not isinstance(view, AttemptLineageRead):
            return FailureClass.INVALID_INPUT
        lineage = view.attempt.lineage
        if (lineage.attempt_id != source.try_identity.attempt_id
            or lineage.evidence_snapshot_id != source.evidence_snapshot_id
            or lineage.evidence_revision_id != source.evidence_revision_id
            or lineage.knowledge_snapshot_id != source.knowledge_snapshot_id
            or not source.pin.matches_attempt(lineage.generation_provenance)):
            return FailureClass.IDENTITY_CONTRADICTION
        if (source.pin != config.pin
            or config.pin.result_schema.version != ("2" if config.version == "2.0" else "1")):
            return FailureClass.CAPABILITY_MISMATCH
        if (grant.expected_a_outcomes != len(view.try_outcomes)
            or source.try_identity.try_ordinal != len(view.try_outcomes) + 1
            or any(item.identity == source.try_identity for item in view.try_outcomes)):
            return FailureClass.IDENTITY_CONTRADICTION
        try:
            snapshot = self.evidence.resolve_snapshot(source.evidence_snapshot_id)
            revision = self.evidence.resolve_revision(source.evidence_revision_id)
            key = KnowledgeSnapshotKey(source.knowledge_snapshot_id)
            knowledge_read = self.knowledge.read_snapshot(key)
            provenance_read = self.knowledge.read_provenance(key)
        except Exception:
            return FailureClass.LOCAL_INTEGRITY
        if (not isinstance(snapshot, EvidenceSnapshot)
            or not isinstance(revision, EvidenceRevision)
            or snapshot.snapshot_id != source.evidence_snapshot_id
            or snapshot.revision_id != source.evidence_revision_id
            or revision.revision_id != source.evidence_revision_id
            or snapshot.incident_id != revision.incident_id
            or snapshot.snapshot_content["semantic_evidence"] != revision.semantic_content):
            return FailureClass.EVIDENCE_REFERENCE
        if (knowledge_read.status is not KnowledgeReadStatus.FOUND
            or provenance_read.status is not KnowledgeReadStatus.FOUND
            or not isinstance(knowledge_read.value, KnowledgeSnapshotEnvelope)
            or not isinstance(provenance_read.value, KnowledgeProvenanceProjection)
            or knowledge_read.value.snapshot_key != key
            or knowledge_read.value.resolution is not source.knowledge_resolution
            or provenance_read.value.snapshot_identity != key
            or provenance_read.value.snapshot_commitment != knowledge_read.value.snapshot_commitment
            or provenance_read.value.resolution is not knowledge_read.value.resolution):
            return FailureClass.KNOWLEDGE_AUTHORITY
        if source.knowledge_resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE:
            authorization = degraded_authorization
            if (source.degraded_authorization_reference is None
                or not isinstance(authorization, DegradedAuthorizationFact)
                or authorization.reference != source.degraded_authorization_reference
                or authorization.attempt_id != source.try_identity.attempt_id
                or authorization.try_ordinal != source.try_identity.try_ordinal
                or authorization.evidence_snapshot_id != source.evidence_snapshot_id
                or authorization.knowledge_snapshot_id != source.knowledge_snapshot_id
                or authorization.authorized is not True
                or authorization.trusted_evidence_sufficient is not True):
                return FailureClass.KNOWLEDGE_AUTHORITY
        try:
            evidence_projection = canonical_evidence_projection(snapshot)
            knowledge_projection = canonical_knowledge_projection(
                knowledge_read.value, provenance_read.value,
            )
        except (ValueError, KeyError, TypeError):
            return FailureClass.INVALID_INPUT
        if (source.evidence_projection != evidence_projection
            or source.knowledge_projection != knowledge_projection):
            return FailureClass.INVALID_INPUT
        if (not isinstance(resources, AvailableResources)
            or min(resources.rate_units, resources.quota_units,
                   resources.cost_units, resources.resource_units) < 1
            or any((resources.rate_units > config.bounds.maximum_rate_units,
                    resources.quota_units > config.bounds.maximum_quota_units,
                    resources.cost_units > config.bounds.maximum_cost_units,
                    resources.resource_units > config.bounds.maximum_resource_units))
            or config.bounds.maximum_invocations != 1):
            return FailureClass.INVOCATION_BOUND
        return None

    def execute(
        self, source: GenerationInput, config: GenerationConfig,
        grant: ExecutionAuthorization, resources: AvailableResources,
        *, degraded_authorization: DegradedAuthorizationFact | None = None,
    ) -> ExecutionReceipt:
        if not isinstance(source, GenerationInput) or not isinstance(config, GenerationConfig):
            raise TypeError("exact immutable input and versioned configuration required")
        readiness = self.store.local_readiness()
        if readiness is not LocalReadStatus.FOUND:
            return self._local_fault(source, readiness)
        try:
            loaded = load_generation_config(self.store.resource_config_path)
        except GenerationConfigError:
            return self._local_fault(source, LocalReadStatus.REPAIR_REQUIRED)
        if loaded != config:
            return self._failure(source, FailureClass.CAPABILITY_MISMATCH,
                                 RetrySafety.NON_RETRYABLE, "versioned configuration differs")
        # Candidate-A is read before any D execution decision.  The same fresh
        # view supplies both exact lineage validation and the terminal-outcome guard.
        try:
            view = self.attempts.get_attempt_lineage(source.try_identity.attempt_id)
        except Exception:
            return self._local_fault(source, LocalReadStatus.UNAVAILABLE)
        if not isinstance(view, AttemptLineageRead):
            return self._local_fault(source, LocalReadStatus.REPAIR_REQUIRED)
        if not _lineage_matches(source, view):
            return self._failure(
                source, FailureClass.IDENTITY_CONTRADICTION,
                RetrySafety.REPAIR_REQUIRED, "exact Attempt lineage rejected",
            )

        existing = self.store.result_for_try(source.try_identity)
        if existing.status is LocalReadStatus.FOUND:
            authoritative_source = existing.value.content.input
            if replace(authoritative_source, operation_id=source.operation_id) != source:
                return ExecutionReceipt(
                    LocalReadStatus.REPAIR_REQUIRED,
                    failure=GenerationFailure(
                        source.try_identity, source.operation_id,
                        FailureClass.IDENTITY_CONTRADICTION, RetrySafety.REPAIR_REQUIRED,
                        "durable replay input contradicts identity",
                    ),
                )
            return ExecutionReceipt(LocalReadStatus.FOUND, result=existing.value)
        if existing.status is not LocalReadStatus.NOT_FOUND:
            return self._local_fault(source, existing.status)

        if any(item.identity == source.try_identity for item in view.try_outcomes):
            return self._failure(
                source, FailureClass.IDENTITY_CONTRADICTION,
                RetrySafety.REPAIR_REQUIRED,
                "Candidate-A already has authoritative Try outcome",
            )

        prior_failure = self.store.failure(source.operation_id, source.try_identity)
        if prior_failure.status is LocalReadStatus.FOUND:
            if prior_failure.value.subject_input_commitment != subject_input_commitment(source):
                return ExecutionReceipt(
                    LocalReadStatus.REPAIR_REQUIRED,
                    failure=GenerationFailure(
                        source.try_identity, source.operation_id,
                        FailureClass.IDENTITY_CONTRADICTION, RetrySafety.REPAIR_REQUIRED,
                        "durable replay input contradicts identity",
                        subject_input_commitment(source), False,
                    ),
                )
            return ExecutionReceipt(LocalReadStatus.FOUND, failure=prior_failure.value)
        if prior_failure.status is not LocalReadStatus.NOT_FOUND:
            return self._local_fault(source, prior_failure.status)

        subject_failures = self.store.failures_for_try(source.try_identity)
        if subject_failures.status is LocalReadStatus.FOUND:
            if not same_try_reinvocation_eligible(source, self.store, self.attempts):
                return self._failure(
                    source, FailureClass.IDENTITY_CONTRADICTION,
                    RetrySafety.REPAIR_REQUIRED,
                    "Candidate-D Same-Try eligibility rejected",
                )
        elif subject_failures.status is not LocalReadStatus.NOT_FOUND:
            return self._local_fault(source, subject_failures.status)

        rejection = self._admit(source, config, grant, resources, degraded_authorization)
        if rejection is not None:
            safety = RetrySafety.REPAIR_REQUIRED if rejection in {
                FailureClass.IDENTITY_CONTRADICTION, FailureClass.LOCAL_INTEGRITY,
            } else RetrySafety.NON_RETRYABLE
            return self._failure(source, rejection, safety, "exact admission rejected")
        capability = getattr(self.provider, "capability", None)
        if capability != ProviderCapability(
            source.pin.provider, source.pin.model, source.pin.profile.identity,
            source.pin.result_schema.identity, source.pin.result_schema.version, True,
        ):
            return self._failure(source, FailureClass.CAPABILITY_MISMATCH,
                                 RetrySafety.NON_RETRYABLE, "provider capability mismatch")
        try:
            prompt = render_prompt(source, config)
        except ValueError as exc:
            kind = (FailureClass.INVOCATION_BOUND if "bound" in str(exc)
                    else FailureClass.INVALID_INPUT)
            return self._failure(source, kind, RetrySafety.NON_RETRYABLE,
                                 "prompt preflight rejected")
        try:
            token_bound = self.provider.input_token_upper_bound(
                prompt, config.bounds.timeout_seconds,
            )
        except ProviderInvocationError as exc:
            kind = _PROVIDER_FAILURE[exc.kind]
            safety = (RetrySafety.NON_RETRYABLE if kind in {
                FailureClass.CAPABILITY_MISMATCH, FailureClass.INVOCATION_BOUND,
            }
                      else RetrySafety.RETRYABLE)
            return self._failure(source, kind, safety,
                                 "token preflight provider boundary failed",
                                 preflight_calls=int(exc.request_sent))
        except Exception:
            return self._failure(source, FailureClass.PROVIDER_UNAVAILABLE,
                                 RetrySafety.RETRYABLE,
                                 "token preflight provider boundary unavailable",
                                 preflight_calls=1)
        if type(token_bound) is not int or token_bound < 1:
            return self._failure(source, FailureClass.CAPABILITY_MISMATCH,
                                 RetrySafety.NON_RETRYABLE, "safe token accounting unavailable",
                                 preflight_calls=1)
        if token_bound > config.bounds.maximum_input_tokens:
            return self._failure(source, FailureClass.INVOCATION_BOUND,
                                 RetrySafety.NON_RETRYABLE, "input token bound exceeded",
                                 preflight_calls=1)
        request = ProviderRequest(
            source.pin.provider, source.pin.model, source.pin.profile.identity,
            prompt, source.pin.result_schema.identity, source.pin.result_schema.version,
            config.bounds.timeout_seconds, config.bounds.maximum_output_bytes,
            config.bounds.maximum_output_tokens,
        )
        try:
            response = self.provider.invoke_once(request)
        except ProviderInvocationError as exc:
            kind = _PROVIDER_FAILURE[exc.kind]
            safety = (RetrySafety.NON_RETRYABLE if kind in {
                FailureClass.CAPABILITY_MISMATCH, FailureClass.INVOCATION_BOUND,
            }
                      else RetrySafety.RETRYABLE)
            return self._failure(source, kind, safety, "provider boundary failed",
                                 int(exc.request_sent), 1)
        except Exception:
            return self._failure(source, FailureClass.PROVIDER_UNAVAILABLE,
                                 RetrySafety.RETRYABLE, "provider boundary unavailable", 1, 1)
        if not isinstance(response, ProviderResponse):
            return self._failure(source, FailureClass.MALFORMED_PROVIDER_OUTPUT,
                                 RetrySafety.RETRYABLE, "provider response type invalid", 1, 1)
        metadata = response.metadata
        if not isinstance(metadata, InvocationMetadata) or metadata.invocation_count != 1:
            return self._failure(source, FailureClass.INVOCATION_BOUND,
                                 RetrySafety.NON_RETRYABLE, "invocation accounting invalid", 1, 1)
        if (metadata.input_tokens > config.bounds.maximum_input_tokens
            or metadata.output_tokens > config.bounds.maximum_output_tokens
            or metadata.total_tokens > config.bounds.maximum_total_tokens
            or metadata.duration_ms > config.bounds.timeout_seconds * 1000):
            return self._failure(source, FailureClass.INVOCATION_BOUND,
                                 RetrySafety.NON_RETRYABLE, "provider usage exceeded bound", 1, 1)
        if response.structured_output in ("", b"", None):
            return self._failure(source, FailureClass.MALFORMED_PROVIDER_OUTPUT,
                                 RetrySafety.RETRYABLE, "provider output empty", 1, 1)
        try:
            content = parse_structured_output(response.structured_output, source, config, metadata)
        except StructuredOutputError as exc:
            message = str(exc)
            kind = (FailureClass.STRUCTURED_PARSE if isinstance(exc.__cause__, json.JSONDecodeError)
                    else FailureClass.INVALID_INPUT if (
                        "forbidden evaluation context" in message
                        or (isinstance(exc.__cause__, ValueError)
                            and "Ground-Truth-free" in str(exc.__cause__))
                    )
                    else FailureClass.INVOCATION_BOUND if "bound" in message or "exceeds" in message
                    else FailureClass.RESULT_SCHEMA if "schema" in message or "incompatible" in message
                    else FailureClass.STRUCTURED_PARSE)
            safety = RetrySafety.RETRYABLE if kind is FailureClass.STRUCTURED_PARSE else RetrySafety.NON_RETRYABLE
            return self._failure(source, kind, safety, "structured result rejected", 1, 1)
        try:
            proofs = tuple(
                derive_causal_support_proof(content, config, self.evidence, claim.claim_id)
                for claim in content.claims if claim.causal_assertion is not None
            )
            if proofs:
                content = replace(content, causal_proofs=proofs)
            validated = validate_generation_result(
                content, config, self.attempts, self.evidence, self.knowledge,
                degraded_authorization=degraded_authorization,
            )
        except GenerationValidationError as exc:
            safety = RetrySafety.REPAIR_REQUIRED if exc.failure_class in {
                FailureClass.IDENTITY_CONTRADICTION, FailureClass.LOCAL_INTEGRITY,
            } else RetrySafety.NON_RETRYABLE
            return self._failure(source, exc.failure_class, safety,
                                 "deterministic validation rejected", 1, 1)
        committed = self.store.commit_result(validated)
        if committed.status is not LocalReadStatus.FOUND:
            return self._local_fault(source, committed.status, 1, 1)
        return ExecutionReceipt(LocalReadStatus.FOUND, result=committed.value,
                                physical_invocations=1, admitted=True,
                                token_preflight_calls=1)
