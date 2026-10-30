"""Candidate-C-owned bounded provider and staged-index ports."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .contracts import (
    IndexArtifactFacts,
    IndexProbeFacts,
    ProviderCapability,
    ProviderEmbeddingRequest,
    ProviderEmbeddingResult,
)


@runtime_checkable
class EmbeddingProviderPort(Protocol):
    """One logical request; only an approved Gemini plan may use bounded sequential
    physical invocations. Implementations must not choose credentials or retry.
    """

    def capability(self) -> ProviderCapability: ...

    def embed(self, request: ProviderEmbeddingRequest) -> ProviderEmbeddingResult: ...


@runtime_checkable
class StagedIndexPort(Protocol):
    """Immutable artifact capability; never an activation authority."""

    def stage(
        self,
        build_identity: str,
        request: ProviderEmbeddingRequest,
        result: ProviderEmbeddingResult,
    ) -> IndexArtifactFacts: ...

    def inspect(self, build_identity: str) -> IndexArtifactFacts | None: ...

    def probe(self, build_identity: str, *, maximum_results: int) -> IndexProbeFacts: ...
