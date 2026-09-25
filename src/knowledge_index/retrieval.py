"""Independent frozen retrieval operation for SPEC-014 Candidate C."""

from __future__ import annotations

from .contracts import (
    FrozenRetrievalOperation,
    KnowledgeReadStatus,
    KnowledgeValidationError,
    ProviderInvocationLimits,
    QueryEmbeddingRequest,
    RetrievalFailureCode,
    RetrievalFailureFact,
    RetrievalOperationRequest,
    RetrievalQueryDisposition,
    RetrievalResolution,
    RetrievalResult,
    RetrySafetyDisposition,
    StagedBuildRecord,
)
from .retrieval_ports import QueryEmbeddingProviderPort, RetrievalIndexPort
from .retrieval_resolution import resolve_retrieval
from .identity import canonical_serialize
from .sqlite_store import (
    KnowledgeStoreConflictError,
    KnowledgeStoreIntegrityError,
    KnowledgeStoreUnavailableError,
    SqliteKnowledgeStore,
)


def _failure(
    request: RetrievalOperationRequest,
    frozen_build_identity: str | None,
    resolution: RetrievalResolution,
    code: RetrievalFailureCode,
    field: str,
    detail: str,
    retry: RetrySafetyDisposition,
) -> RetrievalResult:
    return RetrievalResult(
        request.operation_key,
        frozen_build_identity,
        resolution,
        failures=(RetrievalFailureFact(code, field, detail, retry),),
    )


def _query_embedding_request_bytes(frozen: FrozenRetrievalOperation) -> int:
    """Canonical outbound request facts, excluding credentials and runtime limits."""
    request = frozen.request
    payload = {
        "schema_version": "1.0",
        "frozen_build_identity": frozen.frozen_build_identity,
        "query": request.query,
        "profile_reference": request.profile_reference,
        "capability_identity": request.capability_identity,
        "provider": request.retrieval_profile.provider,
        "model": request.retrieval_profile.model,
        "embedding_profile_identity": request.retrieval_profile.embedding_profile_identity,
        "embedding_dimension": request.retrieval_profile.embedding_dimension,
    }
    return len(canonical_serialize(payload).encode("utf-8"))


def _query_preflight_failure(
    frozen: FrozenRetrievalOperation,
    limits: ProviderInvocationLimits,
) -> RetrievalResult | None:
    request = frozen.request
    profile = request.retrieval_profile
    query_bytes = len(request.query.text.encode("utf-8"))
    if (
        not request.query.text
        and profile.empty_query_disposition is RetrievalQueryDisposition.INVALID
    ):
        return _failure(
            request, frozen.frozen_build_identity, RetrievalResolution.INVALID,
            RetrievalFailureCode.QUERY_INVALID, "query",
            "empty query is rejected by the selected retrieval profile",
            RetrySafetyDisposition.DO_NOT_RETRY,
        )
    if query_bytes > profile.max_query_bytes:
        return _failure(
            request, frozen.frozen_build_identity, RetrievalResolution.INVALID,
            RetrievalFailureCode.QUERY_INVALID, "query_size",
            "query exceeds the selected retrieval profile bound",
            RetrySafetyDisposition.DO_NOT_RETRY,
        )
    if (
        any(item.key not in profile.allowed_filter_keys for item in request.query.filters)
        and profile.unsupported_query_disposition is RetrievalQueryDisposition.INVALID
    ):
        return _failure(
            request, frozen.frozen_build_identity, RetrievalResolution.INVALID,
            RetrievalFailureCode.QUERY_INVALID, "query_filter",
            "query contains a filter unsupported by the selected retrieval profile",
            RetrySafetyDisposition.DO_NOT_RETRY,
        )
    if _query_embedding_request_bytes(frozen) > limits.maximum_request_bytes:
        return _failure(
            request, frozen.frozen_build_identity, RetrievalResolution.INVALID,
            RetrievalFailureCode.QUERY_INVALID, "request_size",
            "canonical query request exceeds the provider invocation bound",
            RetrySafetyDisposition.DO_NOT_RETRY,
        )
    return None


class KnowledgeRetrievalService:
    """Executes against one durable frozen build without re-reading Active."""

    def __init__(
        self,
        store: SqliteKnowledgeStore,
        provider: QueryEmbeddingProviderPort,
        index: RetrievalIndexPort,
    ) -> None:
        self._store = store
        self._provider = provider
        self._index = index

    def retrieve(
        self,
        request: RetrievalOperationRequest,
        limits: ProviderInvocationLimits,
    ) -> RetrievalResult:
        frozen: FrozenRetrievalOperation | None = None
        try:
            # This is the only Active read in the operation and happens inside
            # the store's BEGIN IMMEDIATE freeze transaction.
            frozen = self._store.freeze_retrieval_operation(request)
        except KnowledgeStoreConflictError:
            return _failure(
                request, None, RetrievalResolution.INVALID,
                RetrievalFailureCode.REPLAY_CONTRADICTION, "operation",
                "retrieval operation contradicts durable semantic inputs",
                RetrySafetyDisposition.DO_NOT_RETRY,
            )
        except KnowledgeStoreIntegrityError:
            return _failure(
                request, None, RetrievalResolution.REPAIR_REQUIRED,
                RetrievalFailureCode.FROZEN_BUILD_INVALID, "authority",
                "retrieval authority failed integrity validation",
                RetrySafetyDisposition.DO_NOT_RETRY,
            )
        except KnowledgeStoreUnavailableError:
            return _failure(
                request, None, RetrievalResolution.RETRIEVAL_UNAVAILABLE,
                RetrievalFailureCode.INDEX_UNAVAILABLE, "authority",
                "retrieval authority is unavailable",
                RetrySafetyDisposition.SAME_OPERATION_ONLY,
            )

        preflight_failure = _query_preflight_failure(frozen, limits)
        if preflight_failure is not None:
            return preflight_failure

        staged_read = self._store.get_staged_build(frozen.frozen_build_identity)
        validation_read = self._store.get_build_validation(frozen.frozen_build_identity)
        if (
            staged_read.status is not KnowledgeReadStatus.FOUND
            or validation_read.status is not KnowledgeReadStatus.FOUND
            or not isinstance(staged_read.value, StagedBuildRecord)
        ):
            return _failure(
                request, frozen.frozen_build_identity, RetrievalResolution.REPAIR_REQUIRED,
                RetrievalFailureCode.FROZEN_BUILD_INVALID, "frozen_build",
                "exact frozen build can no longer be resolved",
                RetrySafetyDisposition.DO_NOT_RETRY,
            )
        staged = staged_read.value
        try:
            capability = self._provider.capability()
        except Exception:
            return _failure(
                request, frozen.frozen_build_identity,
                RetrievalResolution.RETRIEVAL_UNAVAILABLE,
                RetrievalFailureCode.PROVIDER_UNAVAILABLE, "provider_capability",
                "query embedding capability is temporarily unavailable",
                RetrySafetyDisposition.SAME_OPERATION_ONLY,
            )
        profile = request.retrieval_profile
        if (
            capability.profile_reference != request.profile_reference
            or capability.capability_identity != request.capability_identity
            or capability.provider != profile.provider
            or capability.model != profile.model
            or capability.embedding_profile_identity != profile.embedding_profile_identity
            or capability.embedding_dimension != profile.embedding_dimension
            or not capability.hidden_retries_disabled
        ):
            return _failure(
                request, frozen.frozen_build_identity, RetrievalResolution.INVALID,
                RetrievalFailureCode.COMPATIBILITY_MISMATCH, "provider_capability",
                "query provider does not match the frozen compatibility profile",
                RetrySafetyDisposition.DO_NOT_RETRY,
            )
        try:
            artifact = self._index.inspect(frozen.frozen_build_identity)
        except KnowledgeValidationError:
            return _failure(
                request, frozen.frozen_build_identity, RetrievalResolution.INVALID,
                RetrievalFailureCode.INDEX_INVALID, "index",
                "index inspection returned malformed artifact facts",
                RetrySafetyDisposition.DO_NOT_RETRY,
            )
        except Exception:
            return _failure(
                request, frozen.frozen_build_identity,
                RetrievalResolution.RETRIEVAL_UNAVAILABLE,
                RetrievalFailureCode.INDEX_UNAVAILABLE, "index",
                "exact frozen index is temporarily unavailable",
                RetrySafetyDisposition.SAME_OPERATION_ONLY,
            )
        if artifact is None or artifact != staged.artifact:
            return _failure(
                request, frozen.frozen_build_identity, RetrievalResolution.REPAIR_REQUIRED,
                RetrievalFailureCode.INDEX_INVALID, "index",
                "exact frozen index does not match durable artifact facts",
                RetrySafetyDisposition.DO_NOT_RETRY,
            )
        try:
            embedding = self._provider.embed_query(QueryEmbeddingRequest(
                frozen.frozen_build_identity, request.query, capability, limits
            ))
        except KnowledgeValidationError:
            return _failure(
                request, frozen.frozen_build_identity, RetrievalResolution.INVALID,
                RetrievalFailureCode.PROVIDER_INVALID, "provider",
                "query provider returned malformed bounded facts",
                RetrySafetyDisposition.DO_NOT_RETRY,
            )
        except Exception:
            return _failure(
                request, frozen.frozen_build_identity,
                RetrievalResolution.RETRIEVAL_UNAVAILABLE,
                RetrievalFailureCode.PROVIDER_UNAVAILABLE, "provider",
                "query embedding capability is temporarily unavailable",
                RetrySafetyDisposition.SAME_OPERATION_ONLY,
            )
        if embedding.failure_detail is not None:
            return _failure(
                request, frozen.frozen_build_identity,
                RetrievalResolution.RETRIEVAL_UNAVAILABLE,
                RetrievalFailureCode.PROVIDER_UNAVAILABLE, "provider",
                embedding.failure_detail, RetrySafetyDisposition.SAME_OPERATION_ONLY,
            )
        if (
            embedding.capability != capability
            or len(embedding.vector) != profile.embedding_dimension
            or embedding.invocation_count > limits.maximum_invocations
            or embedding.cost_units > limits.maximum_cost_units
            or embedding.rate_units > limits.maximum_rate_units
            or embedding.quota_units > limits.maximum_quota_units
            or embedding.resource_units > limits.maximum_resource_units
        ):
            return _failure(
                request, frozen.frozen_build_identity, RetrievalResolution.INVALID,
                RetrievalFailureCode.PROVIDER_INVALID, "embedding",
                "query embedding violates the frozen compatibility contract",
                RetrySafetyDisposition.DO_NOT_RETRY,
            )
        try:
            batch = self._index.query(
                frozen.frozen_build_identity,
                embedding.vector,
                candidate_limit=profile.candidate_limit,
            )
        except KnowledgeValidationError:
            return _failure(
                request, frozen.frozen_build_identity, RetrievalResolution.INVALID,
                RetrievalFailureCode.INDEX_INVALID, "index",
                "index returned malformed bounded candidate facts",
                RetrySafetyDisposition.DO_NOT_RETRY,
            )
        except Exception:
            return _failure(
                request, frozen.frozen_build_identity,
                RetrievalResolution.RETRIEVAL_UNAVAILABLE,
                RetrievalFailureCode.INDEX_UNAVAILABLE, "index",
                "exact frozen index query is temporarily unavailable",
                RetrySafetyDisposition.SAME_OPERATION_ONLY,
            )
        return resolve_retrieval(frozen, staged, batch)
