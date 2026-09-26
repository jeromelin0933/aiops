"""Candidate-C owned ports for one frozen retrieval operation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .contracts import (
    IndexArtifactFacts,
    ProviderCapability,
    QueryEmbeddingRequest,
    QueryEmbeddingResult,
    RawRetrievalBatch,
)


@runtime_checkable
class QueryEmbeddingProviderPort(Protocol):
    """Embeds one bounded query; credentials remain behind the adapter."""

    def capability(self) -> ProviderCapability: ...

    def embed_query(self, request: QueryEmbeddingRequest) -> QueryEmbeddingResult: ...


@runtime_checkable
class RetrievalIndexPort(Protocol):
    """Queries only the exact immutable artifact selected by Candidate C."""

    def inspect(self, build_identity: str) -> IndexArtifactFacts | None: ...

    def query(
        self,
        build_identity: str,
        vector: tuple[float, ...],
        *,
        candidate_limit: int,
    ) -> RawRetrievalBatch: ...
