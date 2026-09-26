"""Local persistent Chroma index capability; never a semantic authority."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .contracts import (
    ArtifactTrust,
    IndexArtifactFacts,
    IndexEntryFact,
    IndexProbeFacts,
    KnowledgeValidationError,
    ProviderEmbeddingRequest,
    ProviderEmbeddingResult,
    RawRetrievalBatch,
    RawRetrievalCandidate,
)
from .identity import canonical_serialize


_BUILD = re.compile(r"^kbld_([0-9a-f]{64})$")


def collection_name(build_identity: str) -> str:
    match = _BUILD.fullmatch(build_identity)
    if match is None:
        raise KnowledgeValidationError("build identity is invalid")
    return f"spec014-kbld-{match.group(1)}"


class ChromaIndexAdapter:
    def __init__(self, storage_path: str | Path, *, index_schema_identity: str, client: Any | None = None) -> None:
        self.storage_path = Path(storage_path)
        self.index_schema_identity = index_schema_identity
        if client is None:
            import chromadb
            from chromadb.config import Settings
            self.storage_path.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(
                path=str(self.storage_path),
                settings=Settings(anonymized_telemetry=False, allow_reset=False),
            )
        self._client = client

    def _get(self, build_identity: str) -> Any | None:
        try:
            return self._client.get_collection(collection_name(build_identity), embedding_function=None)
        except Exception:
            return None

    @staticmethod
    def _artifact_commitment(payload: dict[str, object]) -> str:
        return hashlib.sha256(canonical_serialize(payload).encode("utf-8")).hexdigest()

    def stage(
        self,
        build_identity: str,
        request: ProviderEmbeddingRequest,
        result: ProviderEmbeddingResult,
    ) -> IndexArtifactFacts:
        if request.build_identity != build_identity or result.capability != request.capability or result.failure is not None:
            raise KnowledgeValidationError("index stage inputs contradict the build")
        entries = tuple(sorted((
            IndexEntryFact(chunk.chunk_identity, chunk.metadata_commitment, request.capability.embedding_dimension)
            for chunk in request.chunks
        ), key=lambda item: item.chunk_identity))
        payload = {
            "build_identity": build_identity,
            "provider": request.capability.provider,
            "model": request.capability.model,
            "embedding_profile_identity": request.capability.embedding_profile_identity,
            "embedding_dimension": request.capability.embedding_dimension,
            "index_engine": "chromadb",
            "index_schema_identity": self.index_schema_identity,
            "entries": entries,
        }
        commitment = self._artifact_commitment(payload)
        artifact = IndexArtifactFacts(
            build_identity, commitment, ArtifactTrust.APPROVED,
            request.capability.provider, request.capability.model,
            request.capability.embedding_profile_identity, request.capability.embedding_dimension,
            "chromadb", self.index_schema_identity, entries, True,
        )
        existing = self._get(build_identity)
        if existing is not None:
            if self.inspect(build_identity) != artifact:
                raise KnowledgeValidationError("existing immutable Chroma collection contradicts the build")
            return artifact
        metadata = {
            "build_identity": build_identity,
            "artifact_commitment": commitment,
            "provider": request.capability.provider,
            "model": request.capability.model,
            "embedding_profile_identity": request.capability.embedding_profile_identity,
            "embedding_dimension": request.capability.embedding_dimension,
            "index_engine": "chromadb",
            "index_schema_identity": self.index_schema_identity,
        }
        collection = self._client.create_collection(
            collection_name(build_identity), metadata=metadata, embedding_function=None,
        )
        collection.add(
            ids=[item.chunk_identity for item in request.chunks],
            embeddings=[list(item.values) for item in result.embeddings],
            metadatas=[{"metadata_commitment": item.metadata_commitment} for item in request.chunks],
        )
        if self.inspect(build_identity) != artifact:
            raise KnowledgeValidationError("staged Chroma collection failed exact inspection")
        return artifact

    def inspect(self, build_identity: str) -> IndexArtifactFacts | None:
        collection = self._get(build_identity)
        if collection is None:
            return None
        metadata = dict(collection.metadata or {})
        required = {
            "build_identity", "artifact_commitment", "provider", "model",
            "embedding_profile_identity", "embedding_dimension", "index_engine",
            "index_schema_identity",
        }
        if set(metadata) != required or metadata["build_identity"] != build_identity:
            raise KnowledgeValidationError("Chroma collection metadata lineage is invalid")
        raw = collection.get(include=["metadatas", "embeddings"])
        ids = list(raw.get("ids") or [])
        metadatas = list(raw.get("metadatas") or [])
        embeddings_raw = raw.get("embeddings")
        embeddings = list(embeddings_raw) if embeddings_raw is not None else []
        if len(ids) != len(set(ids)) or len(ids) != len(metadatas) or len(ids) != len(embeddings):
            raise KnowledgeValidationError("Chroma collection cardinality is invalid")
        dimension = int(metadata["embedding_dimension"])
        entries: list[IndexEntryFact] = []
        for identity, item_metadata, vector in zip(ids, metadatas, embeddings):
            if not isinstance(item_metadata, dict) or set(item_metadata) != {"metadata_commitment"} or len(vector) != dimension:
                raise KnowledgeValidationError("Chroma entry metadata or dimension is invalid")
            entries.append(IndexEntryFact(identity, item_metadata["metadata_commitment"], dimension))
        entries_tuple = tuple(sorted(entries, key=lambda item: item.chunk_identity))
        payload = {
            "build_identity": build_identity,
            "provider": metadata["provider"], "model": metadata["model"],
            "embedding_profile_identity": metadata["embedding_profile_identity"],
            "embedding_dimension": dimension, "index_engine": metadata["index_engine"],
            "index_schema_identity": metadata["index_schema_identity"], "entries": entries_tuple,
        }
        commitment = self._artifact_commitment(payload)
        if commitment != metadata["artifact_commitment"] or metadata["index_engine"] != "chromadb" or metadata["index_schema_identity"] != self.index_schema_identity:
            raise KnowledgeValidationError("Chroma artifact commitment or compatibility drifted")
        return IndexArtifactFacts(
            build_identity, commitment, ArtifactTrust.APPROVED,
            str(metadata["provider"]), str(metadata["model"]),
            str(metadata["embedding_profile_identity"]), dimension,
            "chromadb", self.index_schema_identity, entries_tuple, True,
        )

    def probe(self, build_identity: str, *, maximum_results: int) -> IndexProbeFacts:
        artifact = self.inspect(build_identity)
        if artifact is None:
            return IndexProbeFacts(build_identity, False, 0, maximum_results)
        returned = min(len(artifact.entries), maximum_results)
        return IndexProbeFacts(build_identity, True, returned, maximum_results)

    def query(self, build_identity: str, vector: tuple[float, ...], *, candidate_limit: int) -> RawRetrievalBatch:
        artifact = self.inspect(build_identity)
        if artifact is None:
            raise RuntimeError("exact frozen Chroma collection is unavailable")
        if len(vector) != artifact.embedding_dimension or candidate_limit < 1:
            raise KnowledgeValidationError("query vector or candidate bound is incompatible")
        collection = self._get(build_identity)
        assert collection is not None
        count = min(candidate_limit, len(artifact.entries))
        if count == 0:
            return RawRetrievalBatch(build_identity, artifact.artifact_commitment, ())
        raw = collection.query(query_embeddings=[list(vector)], n_results=count, include=["distances", "metadatas"])
        ids = list((raw.get("ids") or [[]])[0])
        distances = list((raw.get("distances") or [[]])[0])
        metadatas = list((raw.get("metadatas") or [[]])[0])
        if not (len(ids) == len(distances) == len(metadatas) <= candidate_limit):
            raise KnowledgeValidationError("Chroma query result cardinality is invalid")
        candidates = tuple(
            RawRetrievalCandidate(identity, float(distance), item_metadata["metadata_commitment"])
            for identity, distance, item_metadata in zip(ids, distances, metadatas)
        )
        return RawRetrievalBatch(build_identity, artifact.artifact_commitment, candidates)
