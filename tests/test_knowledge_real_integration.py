"""Explicit opt-in Google text-embedding-004 and persistent Chroma evidence."""

import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SPEC014_REAL_INTEGRATION") != "1",
    reason="set RUN_SPEC014_REAL_INTEGRATION=1 with legal Google credentials",
)

from knowledge_index import (
    BuildIdentityInput, ProviderEmbeddingRequest, admit_production_manifest,
    build_identity, load_knowledge_config, plan_chunks,
)
from knowledge_index.chroma_index_adapter import ChromaIndexAdapter
from knowledge_index.google_embedding_adapter import GoogleEmbeddingAdapter, create_google_client


ROOT = Path(__file__).resolve().parents[1]


def test_real_google_embedding_and_chroma_reopen(tmp_path: Path) -> None:
    config = load_knowledge_config(ROOT / "configs" / "knowledge_index.yaml")
    admission = admit_production_manifest("configs/knowledge_manifest.json", repository_root=ROOT)
    chunks = plan_chunks(admission, source_root=ROOT / "docs" / "knowledge", profile=config.chunking)[:1]
    identity_input = BuildIdentityInput(
        admission.manifest_commitment, (chunks[0].chunk_identity,), config.chunking.profile_identity,
        "1.0", "google", "text-embedding-004", config.capability.embedding_profile_identity,
        768, config.normalization_semantics, "chromadb", config.index_schema_identity,
        "spec014-knowledge-metadata-v1", "spec014-build-contract-v1",
    )
    build_id = build_identity(identity_input)
    request = ProviderEmbeddingRequest(build_id, config.capability, chunks, config.limits)
    provider = GoogleEmbeddingAdapter(
        create_google_client(timeout_seconds=config.limits.timeout_seconds), config.capability
    )
    result = provider.embed(request)
    assert result.failure is None
    adapter = ChromaIndexAdapter(tmp_path / "chroma", index_schema_identity=config.index_schema_identity)
    artifact = adapter.stage(build_id, request, result)
    reopened = ChromaIndexAdapter(tmp_path / "chroma", index_schema_identity=config.index_schema_identity)
    assert reopened.inspect(build_id) == artifact
