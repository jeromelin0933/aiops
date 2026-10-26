"""Slice 3 synthetic port integration; no durable authority or live provider."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from knowledge_index import (
    ActivationOperationKey, BuildDocumentProvenance, BuildOperationKey,
    CanonicalKnowledgeQuery, KnowledgeBuildService, KnowledgeReadResult,
    KnowledgeReadStatus, KnowledgeRetrievalService, ProviderEmbeddingRequest,
    QueryEmbeddingRequest, RawRetrievalBatch,
    QueryFilter, RetrievalFailureCode, RetrievalOperationKey, RetrievalOperationRequest,
    RetrievalResolution, RetrySafetyDisposition, StagedBuildRecord,
    derive_chunk_metadata_commitment,
)
from knowledge_index.chroma_index_adapter import ChromaIndexAdapter, collection_name
from knowledge_index.config import GEMINI_EMBEDDING2_PROFILE
from knowledge_index.contracts import (
    ArtifactTrust, BuildIdentityInput, ChunkIdentityInput, FrozenRetrievalOperation,
    IndexArtifactFacts,
)
from knowledge_index.google_embedding_adapter import (
    GoogleEmbeddingAdapter, _GeminiEmbedding2OfflineClient,
)
from knowledge_index.identity import build_identity, chunk_identity
from knowledge_index.manifest import admit_manifest
from knowledge_index.sqlite_store import (
    KnowledgeStoreIntegrityError, _retrieval_compatibility_matches,
)
from _knowledge_build_testkit import DeterministicIndex, limits, manifest_and_plan
from _knowledge_retrieval_testkit import policy, profile
from test_knowledge_gemini_embedding2_adapter import fixture_request, proof, selected


def _query(request: ProviderEmbeddingRequest, text: str = "approved procedure") -> QueryEmbeddingRequest:
    return QueryEmbeddingRequest(
        request.build_identity, CanonicalKnowledgeQuery("1.0", "1.0", text),
        request.capability, request.limits,
    )


def test_approved_manifest_mapping_crosses_fake_build_and_adapter_without_claim(tmp_path):
    cap = selected()
    raw_a, chunks_a, identity_a = manifest_and_plan(
        tmp_path, profile=cap, name="one.txt", manifest_id="one",
    )
    raw_b, chunks_b, _ = manifest_and_plan(
        tmp_path, profile=cap, name="two.txt", manifest_id="two",
    )
    raw_a["documents"] += raw_b["documents"]
    admission = admit_manifest(raw_a, source_root=tmp_path)
    assert admission.accepted and admission.manifest_commitment
    admitted = {item.source_path: item for item in admission.admitted_documents}
    governed = {item.source_path: item for item in admission.manifest.documents}
    chunks = []
    for source, original, ordinal in (
        ("one.txt", chunks_a[0], 0), ("one.txt", chunks_a[0], 1),
        ("two.txt", chunks_b[0], 0),
    ):
        approved = governed[source]
        document = admitted[source]
        provenance = BuildDocumentProvenance(
            document.document_identity, document.document_version_identity,
            source, document.content_hash, approved.approval_reference,
            approved.source_classification, approved.outbound_eligible,
            approved.content_type, approved.metadata,
        )
        identity = chunk_identity(ChunkIdentityInput(
            original.document_identity, original.document_version_identity,
            original.section_identity, ordinal, original.content_hash,
            identity_a.chunking_profile_identity,
        ))
        chunks.append(replace(
            original, ordinal=ordinal, chunk_identity=identity,
            metadata_commitment=derive_chunk_metadata_commitment(
                admission.manifest_commitment, provenance,
                chunk_identity=identity, section_identity=original.section_identity,
                ordinal=ordinal, content_hash=original.content_hash,
            ),
        ))
    identity_input = replace(
        identity_a, manifest_commitment=admission.manifest_commitment,
        ordered_chunk_identities=tuple(item.chunk_identity for item in chunks),
    )
    client = _GeminiEmbedding2OfflineClient()
    adapter = GoogleEmbeddingAdapter(client, cap, gemini_verification=proof())
    captured = []

    class CaptureProvider:
        def capability(self):
            return adapter.capability()

        def embed(self, request):
            captured.append(request)
            return adapter.embed(request)

    store = MagicMock()
    store.get_staged_build.return_value = KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND)
    store.create_staged_build.side_effect = lambda record: record
    # The fake claim boundary records the attempt; it owns no database.
    result = KnowledgeBuildService(store, CaptureProvider(), DeterministicIndex()).stage(
        operation_key=BuildOperationKey("slice3-synthetic-stage"),
        raw_manifest=raw_a, source_root=tmp_path, chunks=tuple(chunks),
        identity_input=identity_input, limits=limits(timeout_seconds=30),
        required_capability_identity=cap.capability_identity,
    )
    assert len(captured) == 1 and store.claim_build_operation.call_count == 1
    assert result.record is not None and result.failures == ()
    forwarded = captured[0]
    assert forwarded.chunks == tuple(chunks)
    assert dict(forwarded.approved_document_ids) == {
        chunks[0].document_identity: "doc-one",
        chunks[2].document_identity: "doc-two",
    }
    assert [item.parts[0].text for item in client.calls[0]["contents"]] == [
        f"title: {dict(forwarded.approved_document_ids)[chunk.document_identity]} | text: {chunk.content}"
        for chunk in chunks
    ]
    assert all(chunk.document_identity not in item.parts[0].text
               for chunk, item in zip(chunks, client.calls[0]["contents"], strict=True))
    assert len(client.calls) == 1


def _fake_retrieval(request: ProviderEmbeddingRequest, provider):
    cap = request.capability
    retrieval_profile = profile(
        profile_identity="synthetic-gemini-retrieval-unready-v1",
        embedding_profile_identity=cap.embedding_profile_identity,
        provider=cap.provider, model=cap.model, embedding_dimension=768,
    )
    applicability = policy(policy_identity="synthetic-gemini-applicability-unready-v1")
    operation = RetrievalOperationRequest(
        RetrievalOperationKey("slice3-synthetic-retrieve"),
        CanonicalKnowledgeQuery("1.0", "1.0", "approved procedure"),
        retrieval_profile, applicability, (), cap.profile_reference,
        cap.capability_identity,
    )
    artifact = IndexArtifactFacts(
        request.build_identity, "a" * 64, ArtifactTrust.TEST_ONLY,
        cap.provider, cap.model, cap.embedding_profile_identity, 768,
        retrieval_profile.index_engine, retrieval_profile.index_schema_identity,
        (), True,
    )
    staged = MagicMock(spec=StagedBuildRecord)
    staged.build_identity = request.build_identity
    staged.staged_commitment = "b" * 64
    staged.lineage_commitment = "c" * 64
    staged.artifact = artifact
    staged.chunks = ()
    staged.document_provenance = ()
    frozen = FrozenRetrievalOperation(
        operation, "d" * 64, request.build_identity, 1,
        ActivationOperationKey("synthetic-activation"), "e" * 64,
        staged.staged_commitment, artifact.artifact_commitment,
        staged.lineage_commitment,
    )
    store = MagicMock()
    store.freeze_retrieval_operation.return_value = frozen
    store.get_staged_build.return_value = KnowledgeReadResult(KnowledgeReadStatus.FOUND, staged)
    store.get_build_validation.return_value = KnowledgeReadResult(KnowledgeReadStatus.FOUND, object())
    index = MagicMock()
    index.inspect.return_value = artifact
    index.query.return_value = RawRetrievalBatch(request.build_identity, artifact.artifact_commitment, ())
    service = KnowledgeRetrievalService(store, provider, index)
    return service, operation, store, index


class _MemoryCollection:
    def __init__(self, metadata):
        self.metadata = metadata
        self.configuration = {"hnsw": {"space": "l2"}}

    def get(self, *, include):
        assert include == ["metadatas", "embeddings"]
        return {"ids": [], "metadatas": [], "embeddings": []}


class _MemoryChromaClient:
    def __init__(self, collections):
        self.collections = collections
        self.lookups = []

    def get_collection(self, name, *, embedding_function):
        assert embedding_function is None
        self.lookups.append(name)
        return self.collections[name]


def _physical_retrieval(request, provider, tmp_path):
    """Use production store compatibility and Chroma inspection with no durable I/O."""
    _, operation, store, _ = _fake_retrieval(request, provider)
    operation = replace(operation, retrieval_profile=replace(
        operation.retrieval_profile, index_engine="chromadb",
    ))
    staged = store.get_staged_build.return_value.value
    staged.profile_reference = request.capability.profile_reference
    staged.capability_identity = request.capability.capability_identity
    staged.build_input = SimpleNamespace(distance_metric="l2", canonicalization_version="1.0")
    payload = {
        "build_identity": request.build_identity,
        "provider": request.capability.provider,
        "model": request.capability.model,
        "embedding_profile_identity": request.capability.embedding_profile_identity,
        "embedding_dimension": 768,
        "index_engine": "chromadb",
        "index_schema_identity": operation.retrieval_profile.index_schema_identity,
        "distance_metric": "l2",
        "entries": (),
    }
    commitment = ChromaIndexAdapter._artifact_commitment(payload)
    staged.artifact = replace(
        staged.artifact, artifact_commitment=commitment,
        artifact_trust=ArtifactTrust.APPROVED, index_engine="chromadb",
        distance_metric="l2",
    )
    metadata = {**payload, "artifact_commitment": commitment}
    del metadata["entries"]
    collection = _MemoryCollection(metadata)
    client = _MemoryChromaClient({collection_name(request.build_identity): collection})
    index = ChromaIndexAdapter(
        tmp_path / "unused-in-memory-only", index_schema_identity=payload["index_schema_identity"],
        distance_metric="l2", client=client,
    )
    frozen = replace(
        store.freeze_retrieval_operation.return_value,
        request=operation, artifact_commitment=commitment,
    )

    def checked_freeze(selected):
        if not _retrieval_compatibility_matches(selected, staged):
            raise KnowledgeStoreIntegrityError("synthetic active build is incompatible")
        return replace(frozen, request=selected)

    store.freeze_retrieval_operation.side_effect = checked_freeze
    return KnowledgeRetrievalService(store, provider, index), operation, store, client, collection


def test_physical_collection_binding_accepts_exact_gemini_build(tmp_path):
    request = fixture_request(tmp_path)
    client = _GeminiEmbedding2OfflineClient()
    provider = GoogleEmbeddingAdapter(client, request.capability, gemini_verification=proof())
    service, operation, store, physical, _ = _physical_retrieval(request, provider, tmp_path)
    assert _retrieval_compatibility_matches(operation, store.get_staged_build.return_value.value)
    result = service.retrieve(operation, request.limits)
    assert result.resolution is RetrievalResolution.NO_MATCH and result.knowledge_gap
    assert result.frozen_build_identity == request.build_identity
    assert len(client.calls) == 1
    assert physical.lookups and set(physical.lookups) == {collection_name(request.build_identity)}


def test_legacy_build_injection_is_rejected_by_repository_compatibility(tmp_path):
    request = fixture_request(tmp_path)
    client = _GeminiEmbedding2OfflineClient()
    provider = GoogleEmbeddingAdapter(client, request.capability, gemini_verification=proof())
    service, operation, store, physical, _ = _physical_retrieval(request, provider, tmp_path)
    staged = store.get_staged_build.return_value.value
    _, _, gemini_input = manifest_and_plan(
        tmp_path / "legacy-input", profile=request.capability,
        name="legacy-source.txt", manifest_id="legacy-source",
    )
    legacy_build = build_identity(replace(
        gemini_input, embedding_model="text-embedding-004",
        embedding_profile_identity="spec014-google-document-query-v1",
    ))
    assert legacy_build != request.build_identity
    staged.build_identity = legacy_build
    staged.artifact = replace(
        staged.artifact, build_identity=legacy_build,
        model="text-embedding-004",
        embedding_profile_identity="spec014-google-document-query-v1",
    )
    assert not _retrieval_compatibility_matches(operation, staged)
    result = service.retrieve(operation, request.limits)
    assert result.resolution is RetrievalResolution.REPAIR_REQUIRED
    assert result.failures[0].code is RetrievalFailureCode.FROZEN_BUILD_INVALID
    assert result.failures[0].retry_safety is RetrySafetyDisposition.DO_NOT_RETRY
    assert client.calls == [] and physical.lookups == []


@pytest.mark.parametrize("wrong_profile", [False, True])
def test_wrong_gemini_source_or_profile_build_has_no_exact_collection_and_cannot_fallback(
    tmp_path, wrong_profile,
):
    request = fixture_request(tmp_path)
    client = _GeminiEmbedding2OfflineClient()
    provider = GoogleEmbeddingAdapter(client, request.capability, gemini_verification=proof())
    service, operation, store, physical, _ = _physical_retrieval(request, provider, tmp_path)
    _, _, other_source_input = manifest_and_plan(
        tmp_path / "other-source", profile=request.capability,
        name="different.txt", manifest_id="different-source",
    )
    wrong_input = replace(
        other_source_input,
        embedding_profile_identity=(
            "synthetic-other-gemini-profile-v1" if wrong_profile
            else request.capability.embedding_profile_identity
        ),
    )
    wrong_build = build_identity(wrong_input)
    assert wrong_build != request.build_identity
    frozen = store.freeze_retrieval_operation.return_value
    store.freeze_retrieval_operation.side_effect = lambda selected: replace(
        frozen, request=selected, frozen_build_identity=wrong_build,
    )
    result = service.retrieve(operation, request.limits)
    assert result.resolution is RetrievalResolution.REPAIR_REQUIRED
    assert result.failures[0].code is RetrievalFailureCode.INDEX_INVALID
    assert result.failures[0].retry_safety is RetrySafetyDisposition.DO_NOT_RETRY
    assert client.calls == []
    assert physical.lookups == [collection_name(wrong_build)]
    assert collection_name(request.build_identity) in physical.collections


def test_collection_name_rebind_with_wrong_build_metadata_is_rejected(tmp_path):
    request = fixture_request(tmp_path)
    client = _GeminiEmbedding2OfflineClient()
    provider = GoogleEmbeddingAdapter(client, request.capability, gemini_verification=proof())
    service, operation, _, physical, collection = _physical_retrieval(request, provider, tmp_path)
    collection.metadata = {**collection.metadata, "build_identity": "kbld_" + "f" * 64}
    result = service.retrieve(operation, request.limits)
    assert result.resolution is RetrievalResolution.INVALID
    assert result.failures[0].code is RetrievalFailureCode.INDEX_INVALID
    assert result.failures[0].retry_safety is RetrySafetyDisposition.DO_NOT_RETRY
    assert client.calls == []
    assert physical.lookups == [collection_name(request.build_identity)]


def test_fake_retrieval_preserves_new_identity_and_frozen_no_match(tmp_path):
    request = fixture_request(tmp_path)
    client = _GeminiEmbedding2OfflineClient()
    provider = GoogleEmbeddingAdapter(client, request.capability, gemini_verification=proof())
    service, operation, store, index = _fake_retrieval(request, provider)
    assert operation.retrieval_profile.profile_identity != request.capability.embedding_profile_identity
    assert operation.applicability_policy.policy_identity != operation.retrieval_profile.profile_identity
    assert not GEMINI_EMBEDDING2_PROFILE.retrieval_calibrated
    assert not GEMINI_EMBEDDING2_PROFILE.applicability_calibrated
    result = service.retrieve(operation, request.limits)
    assert result.resolution is RetrievalResolution.NO_MATCH and result.knowledge_gap
    assert len(client.calls) == 1
    assert client.calls[0]["contents"][0].parts[0].text == "task: search result | query: approved procedure"
    assert index.query.call_args.args[0] == request.build_identity
    assert store.freeze_retrieval_operation.call_count == 1
    gemini_input = BuildIdentityInput(
        "kmf_" + "1" * 64, (request.chunks[0].chunk_identity,),
        "chunk-profile-v1", "1.0", "google", "gemini-embedding-2",
        request.capability.embedding_profile_identity, 768,
        "provider-normalized-conditional-v1", "chromadb",
        "spec014-chroma-l2-v2", "spec014-knowledge-metadata-v1",
        "spec014-build-contract-v2", "l2",
    )
    legacy_input = replace(
        gemini_input, embedding_model="text-embedding-004",
        embedding_profile_identity="spec014-google-document-query-v1",
    )
    assert build_identity(gemini_input) != build_identity(legacy_input)
    assert collection_name(build_identity(gemini_input)) != collection_name(build_identity(legacy_input))


@pytest.mark.parametrize("change", [
    {"model": "text-embedding-004"},
    {"embedding_dimension": 3},
    {"embedding_profile_identity": "spec014-google-document-query-v1"},
])
def test_fake_retrieval_rejects_mismatched_capability_before_query(tmp_path, change):
    request = fixture_request(tmp_path)
    client = _GeminiEmbedding2OfflineClient()
    provider = GoogleEmbeddingAdapter(client, request.capability, gemini_verification=proof())
    service, operation, store, index = _fake_retrieval(request, provider)
    mismatched = replace(operation, retrieval_profile=replace(operation.retrieval_profile, **change))
    store.freeze_retrieval_operation.return_value = replace(
        store.freeze_retrieval_operation.return_value, request=mismatched,
    )
    result = service.retrieve(mismatched, request.limits)
    assert result.resolution is RetrievalResolution.INVALID
    assert result.failures[0].code is RetrievalFailureCode.COMPATIBILITY_MISMATCH
    assert len(client.calls) == 0 and index.query.call_count == 0


def test_fake_retrieval_rejects_wrong_artifact_before_query(tmp_path):
    request = fixture_request(tmp_path)
    client = _GeminiEmbedding2OfflineClient()
    provider = GoogleEmbeddingAdapter(client, request.capability, gemini_verification=proof())
    service, operation, _, index = _fake_retrieval(request, provider)
    index.inspect.return_value = replace(index.inspect.return_value, model="text-embedding-004")
    result = service.retrieve(operation, request.limits)
    assert result.resolution is RetrievalResolution.REPAIR_REQUIRED
    assert result.failures[0].code is RetrievalFailureCode.INDEX_INVALID
    assert len(client.calls) == 0 and index.query.call_count == 0


def test_fake_retrieval_keeps_legacy_failure_mapping_for_typed_gemini(tmp_path):
    request = fixture_request(tmp_path)
    provider = GoogleEmbeddingAdapter(object(), request.capability, gemini_verification=proof())
    service, operation, _, index = _fake_retrieval(request, provider)
    result = service.retrieve(operation, request.limits)
    assert result.resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE
    assert result.failures[0].code is RetrievalFailureCode.PROVIDER_UNAVAILABLE
    assert result.failures[0].retry_safety is RetrySafetyDisposition.SAME_OPERATION_ONLY
    assert index.query.call_count == 0
    typed = provider.embed_query(_query(request))
    assert typed.failure.field == "provider_authorization"
    assert typed.invocation_count == 0


@pytest.mark.parametrize("forbidden_key", ["scenario_id", "ground_truth", "oracle"])
def test_retrieval_rejects_evaluation_only_filter_keys_before_provider(tmp_path, forbidden_key):
    request = fixture_request(tmp_path)
    client = _GeminiEmbedding2OfflineClient()
    provider = GoogleEmbeddingAdapter(client, request.capability, gemini_verification=proof())
    service, operation, store, index = _fake_retrieval(request, provider)
    forbidden = replace(operation, query=CanonicalKnowledgeQuery(
        "1.0", "1.0", "approved procedure", (QueryFilter(forbidden_key, "S1"),),
    ))
    store.freeze_retrieval_operation.return_value = replace(
        store.freeze_retrieval_operation.return_value, request=forbidden,
    )
    result = service.retrieve(forbidden, request.limits)
    assert result.resolution is RetrievalResolution.INVALID
    assert result.failures[0].code is RetrievalFailureCode.QUERY_INVALID
    assert client.calls == [] and index.query.call_count == 0
