"""Exactly-one immutable Knowledge Snapshot semantics for Candidate C."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from typing import TYPE_CHECKING

from .contracts import (
    DurableRetrievalCompletion,
    FrozenRetrievalOperation,
    KnowledgePersistence,
    KnowledgeReadResult,
    KnowledgeReadStatus,
    KnowledgeResolutionOutcome,
    KnowledgeSnapshotChunk,
    KnowledgeSnapshotEnvelope,
    KnowledgeSnapshotKey,
    MetadataItem,
    OpaqueExternalReference,
    OperationEnvelope,
    ProviderInvocationLimits,
    RawRetrievalBatch,
    RawRetrievalCandidate,
    RetrievalApplicability,
    RetrievalEvaluationDisposition,
    RetrievalOperationRequest,
    RetrievalRecoveryFact,
    RetrievalResolution,
    RetrievalResult,
    SnapshotFinalizationKey,
    SnapshotSourceStatus,
    StagedBuildRecord,
    TerminalUnavailableRequest,
)
from .identity import canonical_serialize
from .retrieval_resolution import resolve_retrieval

if TYPE_CHECKING:
    from .retrieval import KnowledgeRetrievalService


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_serialize(value).encode("utf-8")).hexdigest()


def derive_query_commitment(frozen: FrozenRetrievalOperation) -> str:
    return _digest({
        "namespace": "KNOWLEDGE_QUERY_PROVENANCE",
        "version": "1.0",
        "query": frozen.request.query,
    })


def snapshot_semantic_payload(snapshot: KnowledgeSnapshotEnvelope) -> dict[str, object]:
    """Return the immutable semantic payload, excluding derived identity fields."""
    return {
        "namespace": "KNOWLEDGE_SNAPSHOT",
        "version": snapshot.schema_version,
        "operation_key": snapshot.operation_key,
        "frozen_build_identity": snapshot.frozen_build_identity,
        "lineage_commitment": snapshot.lineage_commitment,
        "resolution": snapshot.resolution,
        "source_status": snapshot.source_status,
        "knowledge_gap": snapshot.knowledge_gap,
        "payload_truncated": snapshot.payload_truncated,
        "activation_generation": snapshot.activation_generation,
        "activation_operation_key": snapshot.activation_operation_key,
        "manifest_commitment": snapshot.manifest_commitment,
        "validation_commitment": snapshot.validation_commitment,
        "staged_commitment": snapshot.staged_commitment,
        "artifact_commitment": snapshot.artifact_commitment,
        "query_commitment": snapshot.query_commitment,
        "retrieval_profile_identity": snapshot.retrieval_profile_identity,
        "retrieval_profile_version": snapshot.retrieval_profile_version,
        "applicability_policy_identity": snapshot.applicability_policy_identity,
        "applicability_policy_version": snapshot.applicability_policy_version,
        "profile_reference": snapshot.profile_reference,
        "capability_identity": snapshot.capability_identity,
        "external_references": snapshot.external_references,
        "creation_metadata": snapshot.creation_metadata,
        "chunks": snapshot.chunks,
        "evaluations": snapshot.evaluations,
        "failures": snapshot.failures,
        "finalization_key": snapshot.finalization_key,
        "finalization_reference": snapshot.finalization_reference,
        "record_version": snapshot.record_version,
    }


def derive_snapshot_commitment(snapshot: KnowledgeSnapshotEnvelope) -> str:
    return _digest(snapshot_semantic_payload(snapshot))


def derive_snapshot_key(commitment: str) -> KnowledgeSnapshotKey:
    return KnowledgeSnapshotKey(f"ksnp_{commitment}")


def derive_recovery_commitment(
    operation_key: object, failures: tuple[object, ...]
) -> str:
    return _digest({
        "namespace": "KNOWLEDGE_RETRIEVAL_RECOVERY",
        "version": "1.0",
        "operation_key": operation_key,
        "failures": failures,
    })


def retrieval_completion_semantic_payload(
    completion: DurableRetrievalCompletion,
) -> dict[str, object]:
    return {
        "namespace": "KNOWLEDGE_RETRIEVAL_COMPLETION",
        "version": "1.0",
        "operation_key": completion.operation_key,
        "frozen_build_identity": completion.frozen_build_identity,
        "frozen_operation_commitment": completion.frozen_operation_commitment,
        "validation_commitment": completion.validation_commitment,
        "staged_commitment": completion.staged_commitment,
        "artifact_commitment": completion.artifact_commitment,
        "lineage_commitment": completion.lineage_commitment,
        "query_commitment": completion.query_commitment,
        "retrieval_profile_identity": completion.retrieval_profile_identity,
        "retrieval_profile_version": completion.retrieval_profile_version,
        "applicability_policy_identity": completion.applicability_policy_identity,
        "applicability_policy_version": completion.applicability_policy_version,
        "raw_batch": completion.raw_batch,
        "resolution": completion.resolution,
        "knowledge_gap": completion.knowledge_gap,
        "payload_truncated": completion.payload_truncated,
        "evaluations": completion.evaluations,
        "record_version": completion.record_version,
    }


def derive_retrieval_completion_commitment(
    completion: DurableRetrievalCompletion,
) -> str:
    return _digest(retrieval_completion_semantic_payload(completion))


def _result_from_completion(
    frozen: FrozenRetrievalOperation,
    staged: StagedBuildRecord,
    completion: DurableRetrievalCompletion,
) -> RetrievalResult:
    validate_retrieval_completion_authority(completion, frozen, staged)
    return resolve_retrieval(frozen, staged, completion.raw_batch)


def build_retrieval_completion(
    frozen: FrozenRetrievalOperation,
    staged: StagedBuildRecord,
    result: RetrievalResult,
) -> DurableRetrievalCompletion:
    """Capture a completed Slice 4 result as a separate immutable authority."""
    if result.operation_key != frozen.request.operation_key:
        raise ValueError("retrieval result operation contradicts frozen authority")
    if result.resolution not in (RetrievalResolution.MATCH, RetrievalResolution.NO_MATCH):
        raise ValueError("only available terminal retrievals can be completed")
    staged_chunks = {item.chunk_identity: item for item in staged.chunks}
    raw = RawRetrievalBatch(
        frozen.frozen_build_identity,
        frozen.artifact_commitment,
        tuple(
            RawRetrievalCandidate(
                evaluation.chunk_identity,
                evaluation.score,
                staged_chunks[evaluation.chunk_identity].metadata_commitment,
            )
            for evaluation in result.evaluations
        ),
    )
    profile = frozen.request.retrieval_profile
    policy = frozen.request.applicability_policy
    placeholder = DurableRetrievalCompletion(
        frozen.request.operation_key,
        frozen.frozen_build_identity,
        frozen.semantic_commitment,
        frozen.validation_commitment,
        frozen.staged_commitment,
        frozen.artifact_commitment,
        frozen.lineage_commitment,
        derive_query_commitment(frozen),
        profile.profile_identity,
        profile.version,
        policy.policy_identity,
        policy.version,
        raw,
        result.resolution,
        result.knowledge_gap,
        result.payload_truncated,
        result.evaluations,
        "0" * 64,
    )
    completion = replace(
        placeholder,
        semantic_commitment=derive_retrieval_completion_commitment(placeholder),
    )
    validate_retrieval_completion_authority(completion, frozen, staged)
    expected = resolve_retrieval(frozen, staged, raw)
    if expected != result:
        raise ValueError("retrieval result does not exactly re-derive from completion authority")
    return completion


def validate_retrieval_completion_authority(
    completion: DurableRetrievalCompletion,
    frozen: FrozenRetrievalOperation,
    staged: StagedBuildRecord,
) -> None:
    profile = frozen.request.retrieval_profile
    policy = frozen.request.applicability_policy
    if (
        completion.operation_key != frozen.request.operation_key
        or completion.frozen_build_identity != frozen.frozen_build_identity
        or completion.frozen_operation_commitment != frozen.semantic_commitment
        or completion.validation_commitment != frozen.validation_commitment
        or completion.staged_commitment != frozen.staged_commitment
        or completion.artifact_commitment != frozen.artifact_commitment
        or completion.lineage_commitment != frozen.lineage_commitment
        or completion.query_commitment != derive_query_commitment(frozen)
        or completion.retrieval_profile_identity != profile.profile_identity
        or completion.retrieval_profile_version != profile.version
        or completion.applicability_policy_identity != policy.policy_identity
        or completion.applicability_policy_version != policy.version
        or staged.build_identity != frozen.frozen_build_identity
        or staged.staged_commitment != frozen.staged_commitment
        or staged.artifact.artifact_commitment != frozen.artifact_commitment
        or completion.semantic_commitment
        != derive_retrieval_completion_commitment(completion)
    ):
        raise ValueError("retrieval completion provenance contradicts durable authority")
    expected = resolve_retrieval(frozen, staged, completion.raw_batch)
    if (
        expected.resolution is not completion.resolution
        or expected.knowledge_gap is not completion.knowledge_gap
        or expected.payload_truncated is not completion.payload_truncated
        or expected.evaluations != completion.evaluations
    ):
        raise ValueError("retrieval completion semantics do not re-derive")


def _snapshot_chunks(
    result: RetrievalResult, staged: StagedBuildRecord
) -> tuple[KnowledgeSnapshotChunk, ...]:
    chunks = {item.chunk_identity: item for item in staged.chunks}
    output: list[KnowledgeSnapshotChunk] = []
    for candidate in result.candidates:
        source = chunks.get(candidate.chunk_identity)
        if source is None or (
            source.document_identity != candidate.document_identity
            or source.document_version_identity != candidate.document_version_identity
        ):
            raise ValueError("retrieval candidate does not resolve to frozen build content")
        output.append(KnowledgeSnapshotChunk(
            candidate.chunk_identity,
            candidate.document_identity,
            candidate.document_version_identity,
            source.section_identity,
            source.content_hash,
            source.metadata_commitment,
            candidate.score,
            candidate.applicability,
            candidate.content,
            candidate.metadata,
            candidate.content_truncated,
        ))
    return tuple(output)


def build_snapshot(
    frozen: FrozenRetrievalOperation,
    staged: StagedBuildRecord,
    authority: DurableRetrievalCompletion | RetrievalResult,
    *,
    finalization_key: SnapshotFinalizationKey | None = None,
    finalization_reference: OpaqueExternalReference | None = None,
) -> KnowledgeSnapshotEnvelope:
    if isinstance(authority, DurableRetrievalCompletion):
        result = _result_from_completion(frozen, staged, authority)
    elif (
        isinstance(authority, RetrievalResult)
        and authority.resolution in (RetrievalResolution.MATCH, RetrievalResolution.NO_MATCH)
    ):
        # Pure construction compatibility for Slice 4 callers. Publication still
        # requires the separately persisted fact in the store transaction.
        result = _result_from_completion(
            frozen, staged, build_retrieval_completion(frozen, staged, authority)
        )
    elif (
        isinstance(authority, RetrievalResult)
        and authority.resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE
    ):
        result = authority
    else:
        raise ValueError("available Snapshot requires durable retrieval completion authority")
    if result.operation_key != frozen.request.operation_key or result.frozen_build_identity != frozen.frozen_build_identity:
        raise ValueError("retrieval result contradicts frozen operation")
    if result.resolution not in (
        RetrievalResolution.MATCH,
        RetrievalResolution.NO_MATCH,
        RetrievalResolution.RETRIEVAL_UNAVAILABLE,
    ):
        raise ValueError("non-terminal-invalid result cannot become a Snapshot")
    unavailable = result.resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE
    profile = frozen.request.retrieval_profile
    policy = frozen.request.applicability_policy
    placeholder = KnowledgeSnapshotEnvelope(
        KnowledgeSnapshotKey("snapshot-pending"),
        frozen.request.operation_key,
        frozen.frozen_build_identity,
        "0" * 64,
        frozen.lineage_commitment,
        "1.0",
        result.resolution,
        SnapshotSourceStatus.UNAVAILABLE if unavailable else SnapshotSourceStatus.AVAILABLE,
        result.knowledge_gap,
        result.payload_truncated,
        frozen.activation_generation,
        frozen.activation_operation_key,
        staged.manifest_commitment,
        frozen.validation_commitment,
        frozen.staged_commitment,
        frozen.artifact_commitment,
        derive_query_commitment(frozen),
        profile.profile_identity,
        profile.version,
        policy.policy_identity,
        policy.version,
        frozen.request.profile_reference,
        frozen.request.capability_identity,
        frozen.request.external_references,
        (
            MetadataItem("producer", "candidate-c"),
            MetadataItem("snapshot_contract", "1.0"),
        ),
        () if unavailable else _snapshot_chunks(result, staged),
        () if unavailable else result.evaluations,
        result.failures if unavailable else (),
        finalization_key,
        finalization_reference,
    )
    commitment = derive_snapshot_commitment(placeholder)
    return replace(
        placeholder,
        snapshot_key=derive_snapshot_key(commitment),
        snapshot_commitment=commitment,
    )


def validate_snapshot_against_authority(
    snapshot: KnowledgeSnapshotEnvelope,
    frozen: FrozenRetrievalOperation,
    staged: StagedBuildRecord,
    completion: DurableRetrievalCompletion | None,
) -> None:
    if (
        snapshot.operation_key != frozen.request.operation_key
        or snapshot.frozen_build_identity != frozen.frozen_build_identity
        or snapshot.lineage_commitment != frozen.lineage_commitment
        or snapshot.activation_generation != frozen.activation_generation
        or snapshot.activation_operation_key != frozen.activation_operation_key
        or snapshot.manifest_commitment != staged.manifest_commitment
        or snapshot.validation_commitment != frozen.validation_commitment
        or snapshot.staged_commitment != frozen.staged_commitment
        or snapshot.artifact_commitment != frozen.artifact_commitment
        or snapshot.query_commitment != derive_query_commitment(frozen)
        or snapshot.retrieval_profile_identity != frozen.request.retrieval_profile.profile_identity
        or snapshot.retrieval_profile_version != frozen.request.retrieval_profile.version
        or snapshot.applicability_policy_identity != frozen.request.applicability_policy.policy_identity
        or snapshot.applicability_policy_version != frozen.request.applicability_policy.version
        or snapshot.profile_reference != frozen.request.profile_reference
        or snapshot.capability_identity != frozen.request.capability_identity
        or snapshot.external_references != frozen.request.external_references
        or snapshot.snapshot_commitment != derive_snapshot_commitment(snapshot)
        or snapshot.snapshot_key != derive_snapshot_key(snapshot.snapshot_commitment)
    ):
        raise ValueError("Snapshot provenance contradicts durable authority")
    staged_chunks = {item.chunk_identity: item for item in staged.chunks}
    provenance = {
        item.document_version_identity: item for item in staged.document_provenance
    }
    if (
        len({item.chunk_identity for item in snapshot.evaluations})
        != len(snapshot.evaluations)
        or any(item.chunk_identity not in staged_chunks for item in snapshot.evaluations)
    ):
        raise ValueError("Snapshot evaluation trace is not bound to frozen chunks")
    for item in snapshot.chunks:
        source = staged_chunks.get(item.chunk_identity)
        document = provenance.get(item.document_version_identity)
        if source is not None:
            maximum = frozen.request.retrieval_profile.max_content_bytes_per_result
            encoded = source.content.encode("utf-8")
            expected_content = (
                source.content if len(encoded) <= maximum
                else encoded[:maximum].decode("utf-8", errors="ignore")
            )
            expected_truncated = len(encoded) > maximum
        if source is None or (
            item.document_identity != source.document_identity
            or item.document_version_identity != source.document_version_identity
            or item.section_identity != source.section_identity
            or item.content_commitment != source.content_hash
            or item.metadata_commitment != source.metadata_commitment
            or document is None
            or item.metadata != document.metadata
            or item.content != expected_content
            or item.content_truncated is not expected_truncated
        ):
            raise ValueError("Snapshot chunk provenance is invalid")
    if snapshot.resolution is RetrievalResolution.MATCH:
        included = tuple(item.chunk_identity for item in snapshot.evaluations if item.included)
        if included != tuple(item.chunk_identity for item in snapshot.chunks):
            raise ValueError("Snapshot evaluation trace contradicts included chunks")
        evaluation_by_chunk = {item.chunk_identity: item for item in snapshot.evaluations}
        if any(
            (evaluation := evaluation_by_chunk[item.chunk_identity]).score != item.score
            or evaluation.applicability is not item.applicability
            or evaluation.content_truncated is not item.content_truncated
            for item in snapshot.chunks
        ):
            raise ValueError("Snapshot chunks contradict evaluation provenance")
    if snapshot.resolution is RetrievalResolution.NO_MATCH and any(
        item.disposition is RetrievalEvaluationDisposition.REJECTED
        and item.applicability is not RetrievalApplicability.NONE
        for item in snapshot.evaluations
    ):
        raise ValueError("NO_MATCH Snapshot contains applicable knowledge")
    if snapshot.resolution in (RetrievalResolution.MATCH, RetrievalResolution.NO_MATCH):
        if completion is None:
            raise ValueError("available Snapshot lacks durable retrieval completion authority")
        expected = _result_from_completion(frozen, staged, completion)
        if (
            expected.resolution is not snapshot.resolution
            or expected.knowledge_gap is not snapshot.knowledge_gap
            or expected.payload_truncated is not snapshot.payload_truncated
            or expected.evaluations != snapshot.evaluations
            or len(expected.candidates) != len(snapshot.chunks)
            or any(
                candidate.chunk_identity != chunk.chunk_identity
                or candidate.document_identity != chunk.document_identity
                or candidate.document_version_identity != chunk.document_version_identity
                or candidate.score != chunk.score
                or candidate.applicability is not chunk.applicability
                or candidate.content != chunk.content
                or candidate.metadata != chunk.metadata
                or candidate.content_truncated is not chunk.content_truncated
                for candidate, chunk in zip(expected.candidates, snapshot.chunks)
            )
        ):
            raise ValueError("Snapshot retrieval semantics do not re-derive from frozen authority")


class KnowledgeSnapshotService:
    """Completes frozen retrievals without owning Runtime retry policy."""

    def __init__(self, store: KnowledgePersistence, retrieval: KnowledgeRetrievalService) -> None:
        self._store = store
        self._retrieval = retrieval

    def resolve(
        self, request: RetrievalOperationRequest, limits: ProviderInvocationLimits
    ) -> KnowledgeResolutionOutcome:
        existing = self._store.get_retrieval_operation_read(request.operation_key)
        if existing.status is KnowledgeReadStatus.FOUND:
            read = existing.value
            frozen_request = getattr(getattr(read, "frozen", None), "request", None)
            if frozen_request != request:
                raise ValueError("retrieval replay contradicts frozen semantic inputs")
            snapshot_key = getattr(read, "snapshot_key", None)
            if snapshot_key is not None:
                stored = self._store.get_snapshot(snapshot_key)
                if stored.status is not KnowledgeReadStatus.FOUND:
                    raise ValueError("completed operation Snapshot cannot be resolved")
                snapshot = stored.value
                assert isinstance(snapshot, KnowledgeSnapshotEnvelope)
                return KnowledgeResolutionOutcome(snapshot.resolution, snapshot)
            completion = getattr(read, "completion", None)
            if isinstance(completion, DurableRetrievalCompletion):
                snapshot = self._snapshot_for_completion(completion)
                self._store.complete_operation_with_snapshot(snapshot, expected_revision=1)
                stored = self._store.get_snapshot(snapshot.snapshot_key)
                if stored.status is not KnowledgeReadStatus.FOUND:
                    raise ValueError("published Snapshot cannot be read")
                return KnowledgeResolutionOutcome(snapshot.resolution, stored.value)  # type: ignore[arg-type]

        result = self._retrieval.retrieve(request, limits)
        if result.resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE:
            frozen_read = self._store.get_frozen_retrieval_operation(request.operation_key)
            if frozen_read.status is KnowledgeReadStatus.FOUND:
                fact = RetrievalRecoveryFact(
                    request.operation_key,
                    derive_recovery_commitment(request.operation_key, result.failures),
                    result.failures,
                )
                self._store.record_retrieval_recovery(fact)
            return KnowledgeResolutionOutcome(result.resolution, failures=result.failures)
        if result.resolution in (RetrievalResolution.INVALID, RetrievalResolution.REPAIR_REQUIRED):
            return KnowledgeResolutionOutcome(result.resolution, failures=result.failures)
        snapshot = self._snapshot_for_result(request.operation_key, result)
        self._store.complete_operation_with_snapshot(snapshot, expected_revision=1)
        stored = self._store.get_snapshot(snapshot.snapshot_key)
        if stored.status is not KnowledgeReadStatus.FOUND:
            raise ValueError("published Snapshot cannot be read")
        return KnowledgeResolutionOutcome(result.resolution, stored.value)  # type: ignore[arg-type]

    def finalize_unavailable(
        self, request: TerminalUnavailableRequest
    ) -> KnowledgeResolutionOutcome:
        operation = self._store.get_retrieval_operation_read(request.operation_key)
        if operation.status is not KnowledgeReadStatus.FOUND:
            raise ValueError("terminal finalization requires an existing frozen operation")
        snapshot_key = getattr(operation.value, "snapshot_key", None)
        if snapshot_key is not None:
            stored = self._store.get_snapshot(snapshot_key)
            if stored.status is not KnowledgeReadStatus.FOUND:
                raise ValueError("completed operation Snapshot cannot be resolved")
            snapshot = stored.value
            assert isinstance(snapshot, KnowledgeSnapshotEnvelope)
            if (
                snapshot.resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE
                and snapshot.finalization_key == request.finalization_key
                and snapshot.finalization_reference == request.authority_reference
                and snapshot.failures == request.failures
            ):
                return KnowledgeResolutionOutcome(snapshot.resolution, snapshot)
            raise ValueError("contradictory terminal finalization")
        frozen_read = self._store.get_frozen_retrieval_operation(request.operation_key)
        if frozen_read.status is not KnowledgeReadStatus.FOUND:
            raise ValueError("frozen operation cannot be resolved")
        frozen = frozen_read.value
        assert isinstance(frozen, FrozenRetrievalOperation)
        result = RetrievalResult(
            request.operation_key,
            frozen.frozen_build_identity,
            RetrievalResolution.RETRIEVAL_UNAVAILABLE,
            failures=request.failures,
        )
        snapshot = self._snapshot_for_result(
            request.operation_key,
            result,
            finalization_key=request.finalization_key,
            finalization_reference=request.authority_reference,
        )
        self._store.complete_operation_with_snapshot(snapshot, expected_revision=1)
        return KnowledgeResolutionOutcome(snapshot.resolution, snapshot)

    def read_operation(self, key: object) -> KnowledgeReadResult:
        return self._store.get_retrieval_operation_read(key)  # type: ignore[arg-type]

    def read_snapshot(self, key: object) -> KnowledgeReadResult:
        return self._store.get_snapshot(key)  # type: ignore[arg-type]

    def read_provenance(self, key: object) -> KnowledgeReadResult:
        return self.read_snapshot(key)

    def _snapshot_for_result(
        self,
        key: object,
        result: RetrievalResult,
        *,
        finalization_key: SnapshotFinalizationKey | None = None,
        finalization_reference: OpaqueExternalReference | None = None,
    ) -> KnowledgeSnapshotEnvelope:
        frozen_read = self._store.get_frozen_retrieval_operation(key)  # type: ignore[arg-type]
        if frozen_read.status is not KnowledgeReadStatus.FOUND:
            raise ValueError("frozen operation cannot be resolved")
        frozen = frozen_read.value
        assert isinstance(frozen, FrozenRetrievalOperation)
        staged_read = self._store.get_staged_build(frozen.frozen_build_identity)
        if staged_read.status is not KnowledgeReadStatus.FOUND:
            raise ValueError("frozen staged build cannot be resolved")
        staged = staged_read.value
        assert isinstance(staged, StagedBuildRecord)
        if result.resolution in (RetrievalResolution.MATCH, RetrievalResolution.NO_MATCH):
            completion = build_retrieval_completion(frozen, staged, result)
            authoritative = self._store.record_retrieval_completion(completion)
            completion_read = self._store.get_retrieval_completion(result.operation_key)
            if (
                completion_read.status is not KnowledgeReadStatus.FOUND
                or completion_read.value != authoritative
            ):
                raise ValueError("durable retrieval completion cannot be exact-read")
            return build_snapshot(frozen, staged, authoritative)
        return build_snapshot(
            frozen, staged, result,
            finalization_key=finalization_key,
            finalization_reference=finalization_reference,
        )

    def _snapshot_for_completion(
        self, completion: DurableRetrievalCompletion
    ) -> KnowledgeSnapshotEnvelope:
        frozen_read = self._store.get_frozen_retrieval_operation(completion.operation_key)
        if frozen_read.status is not KnowledgeReadStatus.FOUND:
            raise ValueError("frozen operation cannot be resolved")
        frozen = frozen_read.value
        assert isinstance(frozen, FrozenRetrievalOperation)
        staged_read = self._store.get_staged_build(frozen.frozen_build_identity)
        if staged_read.status is not KnowledgeReadStatus.FOUND:
            raise ValueError("frozen staged build cannot be resolved")
        staged = staged_read.value
        assert isinstance(staged, StagedBuildRecord)
        exact = self._store.get_retrieval_completion(completion.operation_key)
        if exact.status is not KnowledgeReadStatus.FOUND or exact.value != completion:
            raise ValueError("retrieval completion authority changed during publication")
        return build_snapshot(frozen, staged, completion)
