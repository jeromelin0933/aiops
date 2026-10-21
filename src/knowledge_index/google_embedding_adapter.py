"""Approved Google text-embedding-004 adapter with one bounded execution."""

from __future__ import annotations

import math
from enum import Enum
from typing import Any

from .contracts import (
    BuildFailureCode,
    BuildFailureFact,
    EmbeddingVector,
    KnowledgeValidationError,
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


class _ProviderFailureKind(Enum):
    EXHAUSTED = "EXHAUSTED"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"


class _ProviderResponseContractError(ValueError):
    """A bounded local interpretation failure after a successful invocation."""


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


def _typed_attribute(exc: Exception, name: str, expected_type: type) -> Any | None:
    """Read only allowlisted scalar facts; hostile descriptors fail closed."""
    try:
        value = getattr(exc, name, None)
    except Exception:
        return None
    return value if type(value) is expected_type else None


def _provider_failure_kind(exc: Exception) -> _ProviderFailureKind:
    code = _typed_attribute(exc, "code", int)
    status_code = _typed_attribute(exc, "status_code", int)
    status = _typed_attribute(exc, "status", str)
    http_status = code if code is not None else status_code
    if http_status in (429, 507) or status == "RESOURCE_EXHAUSTED":
        return _ProviderFailureKind.EXHAUSTED
    if http_status in (408, 504) or isinstance(exc, TimeoutError) or type(exc).__name__ in {
        "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    }:
        return _ProviderFailureKind.TIMEOUT
    return _ProviderFailureKind.UNAVAILABLE


def _values(response: Any) -> tuple[tuple[float, ...], ...]:
    try:
        embeddings = getattr(response, "embeddings")
    except AttributeError as exc:
        raise _ProviderResponseContractError("missing embeddings") from exc
    if embeddings is None:
        raise _ProviderResponseContractError("missing embeddings")
    try:
        items = tuple(embeddings)
    except (TypeError, ValueError) as exc:
        raise _ProviderResponseContractError("malformed embeddings") from exc

    vectors = []
    for item in items:
        try:
            raw_values = getattr(item, "values")
        except AttributeError as exc:
            raise _ProviderResponseContractError("missing embedding values") from exc
        if raw_values is None:
            raise _ProviderResponseContractError("missing embedding values")
        try:
            values = tuple(raw_values)
        except (TypeError, ValueError) as exc:
            raise _ProviderResponseContractError("malformed embedding values") from exc
        converted = []
        for value in values:
            if isinstance(value, bool):
                raise _ProviderResponseContractError("invalid embedding number")
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise _ProviderResponseContractError("invalid embedding number") from exc
            if not math.isfinite(number):
                raise _ProviderResponseContractError("non-finite embedding number")
            converted.append(number)
        vectors.append(tuple(converted))
    return tuple(vectors)


def _validated_vectors(response: Any, expected_cardinality: int) -> tuple[tuple[float, ...], ...]:
    vectors = _values(response)
    if len(vectors) != expected_cardinality or any(
        len(vector) != APPROVED_DIMENSION for vector in vectors
    ):
        raise _ProviderResponseContractError("incompatible embedding shape")
    return vectors


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
        config = _config("RETRIEVAL_DOCUMENT")
        try:
            response = self._client.models.embed_content(
                model=APPROVED_MODEL,
                contents=[chunk.content for chunk in request.chunks],
                config=config,
            )
        except Exception as exc:
            kind = _provider_failure_kind(exc)
            exhausted = kind is _ProviderFailureKind.EXHAUSTED
            detail = (
                "provider quota or resource exhausted" if exhausted
                else "provider invocation timed out" if kind is _ProviderFailureKind.TIMEOUT
                else "provider invocation unavailable"
            )
            return ProviderEmbeddingResult(
                self._capability, (), 1, 0, 0, 0, 0,
                self._failure(
                    BuildFailureCode.PROVIDER_EXHAUSTED if exhausted else BuildFailureCode.PROVIDER_UNAVAILABLE,
                    detail,
                    RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED if exhausted else RetrySafetyDisposition.SAME_OPERATION_ONLY,
                ),
            )
        try:
            vectors = _validated_vectors(response, len(request.chunks))
            embeddings = tuple(
                EmbeddingVector(chunk.chunk_identity, vector)
                for chunk, vector in zip(request.chunks, vectors)
            )
        except (_ProviderResponseContractError, KnowledgeValidationError):
            return ProviderEmbeddingResult(
                self._capability, (), 1, 0, 0, 0, 0,
                self._failure(BuildFailureCode.PROVIDER_CONTRACT_INVALID, "provider returned incompatible embedding facts", RetrySafetyDisposition.DO_NOT_RETRY),
            )
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
        config = _config("RETRIEVAL_QUERY")
        try:
            response = self._client.models.embed_content(
                model=APPROVED_MODEL,
                contents=[request.query.text],
                config=config,
            )
        except Exception:
            return QueryEmbeddingResult(self._capability, (), 1, "query embedding provider is unavailable")
        try:
            vectors = _validated_vectors(response, 1)
        except _ProviderResponseContractError:
            return QueryEmbeddingResult(self._capability, (), 1, "query embedding response is incompatible")
        return QueryEmbeddingResult(self._capability, vectors[0], 1, None, 1, 1, 1, 1)
