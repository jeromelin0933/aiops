from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("chromadb")

from knowledge_index import KnowledgeValidationError, ProviderEmbeddingRequest, build_identity
from knowledge_index.chroma_index_adapter import ChromaIndexAdapter, collection_name
from _knowledge_build_testkit import DeterministicProvider, capability, limits, manifest_and_plan


def stage(root: Path):
    selected = capability()
    _, chunks, identity = manifest_and_plan(root / "sources", profile=selected)
    build_id = build_identity(identity)
    request = ProviderEmbeddingRequest(build_id, selected, chunks, limits())
    result = DeterministicProvider(selected).embed(request)
    adapter = ChromaIndexAdapter(
        root / "chroma", index_schema_identity="index-schema-v1",
        distance_metric="l2")
    artifact = adapter.stage(build_id, request, result)
    return adapter, artifact, request, result


def test_stage_reopen_inspect_probe_and_query_exact_build(tmp_path: Path) -> None:
    adapter, artifact, request, result = stage(tmp_path)
    assert adapter.inspect(artifact.build_identity) == artifact
    assert adapter.probe(artifact.build_identity, maximum_results=1).returned_count == 1
    reopened = ChromaIndexAdapter(
        tmp_path / "chroma", index_schema_identity="index-schema-v1",
        distance_metric="l2")
    assert reopened.inspect(artifact.build_identity) == artifact
    batch = reopened.query(artifact.build_identity, result.embeddings[0].values, candidate_limit=1)
    assert batch.build_identity == artifact.build_identity
    assert len(batch.candidates) == 1
    assert collection_name(artifact.build_identity).startswith("spec014-kbld-")
    collection = adapter._client.get_collection(
        collection_name(artifact.build_identity), embedding_function=None)
    assert collection.configuration["hnsw"]["space"] == "l2"
    assert artifact.distance_metric == "l2"
    assert not hasattr(adapter, "activate") and not hasattr(adapter, "reset")


def test_same_build_replay_is_idempotent_and_metadata_drift_fails_closed(tmp_path: Path) -> None:
    adapter, artifact, request, result = stage(tmp_path)
    assert adapter.stage(artifact.build_identity, request, result) == artifact
    collection = adapter._client.get_collection(collection_name(artifact.build_identity), embedding_function=None)
    changed = dict(collection.metadata)
    changed["model"] = "substitute"
    collection.modify(metadata=changed)
    with pytest.raises(KnowledgeValidationError):
        adapter.inspect(artifact.build_identity)


def test_unsupported_or_physical_metric_mismatch_fails_closed(
    tmp_path: Path, monkeypatch,
) -> None:
    with pytest.raises(KnowledgeValidationError, match="unsupported"):
        ChromaIndexAdapter(
            tmp_path / "unsupported", index_schema_identity="index-schema-v1",
            distance_metric="cosine")

    adapter, artifact, _, _ = stage(tmp_path)
    collection = adapter._client.get_collection(
        collection_name(artifact.build_identity), embedding_function=None)
    incompatible = SimpleNamespace(
        metadata=collection.metadata,
        configuration={
            **collection.configuration,
            "hnsw": {**collection.configuration["hnsw"], "space": "cosine"},
        },
        get=collection.get,
    )
    monkeypatch.setattr(adapter, "_get", lambda _build: incompatible)
    with pytest.raises(KnowledgeValidationError, match="physical distance metric"):
        adapter.inspect(artifact.build_identity)

    incompatible.configuration = {}
    with pytest.raises(KnowledgeValidationError, match="unavailable"):
        adapter.inspect(artifact.build_identity)
