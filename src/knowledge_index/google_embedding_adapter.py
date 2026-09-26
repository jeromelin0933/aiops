"""Approved Google text-embedding-004 adapter with one bounded execution."""

from __future__ import annotations

from typing import Any

from .contracts import (
    BuildFailureCode,
    BuildFailureFact,
    EmbeddingVector,
    ProviderCapability,
    ProviderEmbeddingRequest,
    ProviderEmbeddingResult,
    QueryEmbeddingRequest,
    QueryEmbeddingResult,
    RetrySafetyDisposition,
)


APPROVED_PROVIDER = "google"
APPROVED_MODEL = "text-embedding-004"
APPROVED_DIMENSION = 768


def create_google_client(*, timeout_seconds: float) -> Any:
    """Create an SDK client using only Google's environment/ADC boundary."""
    from google import genai
    from google.genai import types

    retry = types.HttpRetryOptions(attempts=1)
    options = types.HttpOptions(timeout=int(timeout_seconds * 1000), retry_options=retry)
    return genai.Client(http_options=options)


def _config(task_type: str) -> Any:
    try:
        from google.genai import types
        return types.EmbedContentConfig(task_type=task_type, auto_truncate=False)
    except ImportError:
        return {"task_type": task_type, "auto_truncate": False}


def _values(response: Any) -> tuple[tuple[float, ...], ...]:
    embeddings = getattr(response, "embeddings", None)
    if embeddings is None:
        raise ValueError("provider response has no embeddings")
    return tuple(tuple(float(value) for value in item.values) for item in embeddings)


class GoogleEmbeddingAdapter:
    """Implements both Candidate-C embedding ports without retry authority."""

    def __init__(self, client: Any, capability: ProviderCapability) -> None:
        if (
            capability.provider != APPROVED_PROVIDER
            or capability.model != APPROVED_MODEL
            or capability.embedding_dimension != APPROVED_DIMENSION
            or not capability.hidden_retries_disabled
        ):
            raise ValueError("Google embedding capability is not the approved fixed baseline")
        self._client = client
        self._capability = capability

    def capability(self) -> ProviderCapability:
        return self._capability

    @staticmethod
    def _failure(code: BuildFailureCode, detail: str, retry: RetrySafetyDisposition) -> BuildFailureFact:
        return BuildFailureFact(code, "provider", detail, retry)

    def embed(self, request: ProviderEmbeddingRequest) -> ProviderEmbeddingResult:
        size = sum(len(chunk.content.encode("utf-8")) for chunk in request.chunks)
        projected_units = len(request.chunks)
        if (
            size > request.limits.maximum_request_bytes
            or projected_units > request.limits.maximum_batch_items
            or projected_units > request.limits.maximum_cost_units
            or projected_units > request.limits.maximum_rate_units
            or projected_units > request.limits.maximum_quota_units
            or projected_units > request.limits.maximum_resource_units
        ):
            return ProviderEmbeddingResult(
                self._capability, (), 0, 0, 0, 0, 0,
                self._failure(BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED, "provider request exceeds approved bounds", RetrySafetyDisposition.DO_NOT_RETRY),
            )
        if request.capability != self._capability or request.limits.maximum_invocations < 1:
            return ProviderEmbeddingResult(
                self._capability, (), 0, 0, 0, 0, 0,
                self._failure(BuildFailureCode.PROFILE_MISMATCH, "provider capability continuity failed", RetrySafetyDisposition.DO_NOT_RETRY),
            )
        try:
            response = self._client.models.embed_content(
                model=APPROVED_MODEL,
                contents=[chunk.content for chunk in request.chunks],
                config=_config("RETRIEVAL_DOCUMENT"),
            )
            vectors = _values(response)
        except Exception as exc:
            name = type(exc).__name__.lower()
            exhausted = any(word in name for word in ("quota", "resource", "rate", "exhaust"))
            return ProviderEmbeddingResult(
                self._capability, (), 1, 0, 0, 0, 0,
                self._failure(
                    BuildFailureCode.PROVIDER_EXHAUSTED if exhausted else BuildFailureCode.PROVIDER_UNAVAILABLE,
                    "provider quota or resource exhausted" if exhausted else "provider invocation unavailable",
                    RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED if exhausted else RetrySafetyDisposition.SAME_OPERATION_ONLY,
                ),
            )
        if len(vectors) != len(request.chunks) or any(len(vector) != APPROVED_DIMENSION for vector in vectors):
            return ProviderEmbeddingResult(
                self._capability, (), 1, 0, 0, 0, 0,
                self._failure(BuildFailureCode.PROVIDER_CONTRACT_INVALID, "provider returned incompatible embedding facts", RetrySafetyDisposition.DO_NOT_RETRY),
            )
        embeddings = tuple(EmbeddingVector(chunk.chunk_identity, vector) for chunk, vector in zip(request.chunks, vectors))
        units = len(embeddings)
        return ProviderEmbeddingResult(self._capability, embeddings, 1, units, units, units, units)

    def embed_query(self, request: QueryEmbeddingRequest) -> QueryEmbeddingResult:
        size = len(request.query.text.encode("utf-8"))
        if (
            request.capability != self._capability
            or size > request.limits.maximum_request_bytes
            or request.limits.maximum_batch_items < 1
            or request.limits.maximum_invocations < 1
        ):
            return QueryEmbeddingResult(self._capability, (), 0, "query embedding request violates approved bounds")
        try:
            response = self._client.models.embed_content(
                model=APPROVED_MODEL,
                contents=[request.query.text],
                config=_config("RETRIEVAL_QUERY"),
            )
            vectors = _values(response)
        except Exception:
            return QueryEmbeddingResult(self._capability, (), 1, "query embedding provider is unavailable")
        if len(vectors) != 1 or len(vectors[0]) != APPROVED_DIMENSION:
            return QueryEmbeddingResult(self._capability, (), 1, "query embedding response is incompatible")
        return QueryEmbeddingResult(self._capability, vectors[0], 1, None, 1, 1, 1, 1)
