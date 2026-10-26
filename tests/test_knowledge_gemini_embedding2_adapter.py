"""Synthetic, credential-free conditional adapter checks; no provider PASS claim."""

from dataclasses import replace
import hashlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from knowledge_index import (
    BuildFailureCode, BuildFailureFact, BuildOperationKey, CanonicalKnowledgeQuery,
    KnowledgeBuildService, ProviderEmbeddingRequest, QueryEmbeddingRequest,
    QueryEmbeddingResult, RetrySafetyDisposition, KnowledgeReadResult, KnowledgeReadStatus,
)
from knowledge_index.config import (
    GEMINI_EMBEDDING2_PROFILE, select_gemini_embedding2_capability,
)
from knowledge_index.contracts import KnowledgePersistence, KnowledgeValidationError
from knowledge_index.google_embedding_adapter import (
    GeminiEmbedding2Verification, GoogleEmbeddingAdapter, create_google_client,
    _GeminiEmbedding2OfflineClient, _gemini_http_options,
)
from knowledge_index.identity import build_identity, chunk_identity, document_identity
from knowledge_index.contracts import ChunkIdentityInput
from _knowledge_build_testkit import DeterministicIndex, limits, manifest_and_plan
from test_knowledge_gemini_embedding2_serialization import offline_sdk


def selected():
    from knowledge_index import OpaqueExternalReference, OpaqueReferenceType
    return select_gemini_embedding2_capability(
        OpaqueExternalReference(OpaqueReferenceType.CREDENTIAL_PROFILE, "synthetic-profile")
    )


def serialized(contents, config):
    return json.dumps({"requests": [{
        "model": "models/gemini-embedding-2",
        "content": {"role": item.role, "parts": [{"text": part.text} for part in item.parts]},
        "outputDimensionality": config.output_dimensionality,
    } for item in contents]}, ensure_ascii=True).encode("utf-8")


def proof(**changes):
    values = dict(
        count_tokens=lambda value: len(value.split()),
        serialize_payload=serialized,
        max_item_tokens=10000, max_aggregate_tokens=100000,
        max_provider_items=128,
        max_item_bytes=100000, max_payload_bytes=1000000,
        normalization_tolerance=0.01, evidence_identity="synthetic-test-only-v1",
    )
    values.update(changes)
    return GeminiEmbedding2Verification(**values)


class Models:
    def __init__(self, response=None, error=None):
        self.client = _GeminiEmbedding2OfflineClient(response=response, error=error)
        self.calls = self.client.calls


def fixture_request(tmp_path, count=1):
    cap = selected()
    _, original, identity = manifest_and_plan(tmp_path, profile=cap)
    first = original[0]
    chunks = []
    for ordinal in range(count):
        content = f"approved operational procedure {ordinal}"
        digest = hashlib.sha256(content.encode()).hexdigest()
        chunks.append(replace(
            first, ordinal=ordinal, content=content, content_hash=digest,
            chunk_identity=chunk_identity(ChunkIdentityInput(
                first.document_identity, first.document_version_identity,
                first.section_identity, ordinal, digest, "chunk-profile-v1",
            )),
        ))
    request = ProviderEmbeddingRequest(
        build_identity(identity), cap, tuple(chunks),
        limits(timeout_seconds=30, maximum_batch_items=128,
               maximum_cost_units=128, maximum_rate_units=128,
               maximum_quota_units=128, maximum_resource_units=128,
               maximum_request_bytes=1000000),
        ((first.document_identity, "doc-manifest-1"),), "operations",
    )
    return request


def test_profile_is_distinct_and_not_calibrated():
    profile = GEMINI_EMBEDDING2_PROFILE
    assert profile.model == "gemini-embedding-2" and profile.api_version == "v1"
    assert profile.sdk_baseline == "google-genai==2.25.0"
    assert profile.dimension == 768
    assert profile.embedding_profile_identity != "spec014-google-document-query-v1"
    assert profile.normalization_tolerance is None
    assert not profile.retrieval_calibrated and not profile.applicability_calibrated


def test_client_factory_selects_v1_30_seconds_one_attempt_without_credentials(monkeypatch):
    observed = {}

    class FakeRetry:
        def __init__(self, *, attempts):
            observed["attempts"] = attempts

    class FakeOptions:
        def __init__(self, *, api_version, timeout, retry_options):
            observed.update(api_version=api_version, timeout=timeout, retry_options=retry_options)

    fake_genai = SimpleNamespace(
        Client=lambda **kwargs: pytest.fail("credential-bearing client must not be constructed"),
        types=SimpleNamespace(HttpRetryOptions=FakeRetry, HttpOptions=FakeOptions),
    )
    monkeypatch.setitem(sys.modules, "google", SimpleNamespace(genai=fake_genai))
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    _gemini_http_options(timeout_seconds=30)
    assert observed["api_version"] == "v1"
    assert observed["timeout"] == 30_000 and observed["attempts"] == 1
    with pytest.raises(ValueError, match="separate diagnostic authority"):
        create_google_client(timeout_seconds=30, model="gemini-embedding-2")


def test_54_independent_contents_preserve_title_identity_order(tmp_path):
    request = fixture_request(tmp_path, 54)
    models = Models()
    result = GoogleEmbeddingAdapter(
        models.client, request.capability, gemini_verification=proof(),
    ).embed(request)
    assert result.failure is None and result.invocation_count == 1
    assert tuple(item.chunk_identity for item in result.embeddings) == tuple(
        chunk.chunk_identity for chunk in request.chunks
    )
    assert len(models.calls) == 1
    call = models.calls[0]
    assert call["model"] == "gemini-embedding-2"
    assert len(call["contents"]) == 54
    assert all(type(item).__name__ == "Content" and len(item.parts) == 1 for item in call["contents"])
    assert [item.parts[0].text for item in call["contents"]] == [
        f"title: doc-manifest-1 | text: {chunk.content}" for chunk in request.chunks
    ]
    assert call["config"].output_dimensionality == 768
    assert call["config"].task_type is None and call["config"].auto_truncate is None


def test_mapping_missing_wrong_or_orphan_fails_before_sdk(tmp_path):
    request = fixture_request(tmp_path)
    models = Models()
    adapter = GoogleEmbeddingAdapter(models.client, request.capability, gemini_verification=proof())
    missing = replace(request, approved_document_ids=(), approved_corpus_id="")
    assert adapter.embed(missing).failure.code is BuildFailureCode.PROFILE_MISMATCH
    wrong = replace(request, approved_document_ids=((request.chunks[0].document_identity, "wrong-title"),))
    assert adapter.embed(wrong).failure.code is BuildFailureCode.PROFILE_MISMATCH
    with pytest.raises(KnowledgeValidationError):
        replace(request, approved_document_ids=(("kdoc_" + "0" * 64, "orphan"),))
    assert models.calls == []


def test_two_documents_use_their_own_approved_titles_not_filenames_or_hashes(tmp_path):
    first = fixture_request(tmp_path / "first")
    cap = first.capability
    _, second_chunks, _ = manifest_and_plan(
        tmp_path / "second", profile=cap, name="unrelated-filename.txt", manifest_id="other",
    )
    second = second_chunks[0]
    request = replace(
        first, chunks=first.chunks + (second,),
        approved_document_ids=tuple(sorted((
            (first.chunks[0].document_identity, "doc-manifest-1"),
            (second.document_identity, "doc-other"),
        ))),
    )
    models = Models()
    result = GoogleEmbeddingAdapter(
        models.client, cap, gemini_verification=proof(),
    ).embed(request)
    assert result.failure is None
    texts = [item.parts[0].text for item in models.calls[0]["contents"]]
    assert texts == [
        f"title: doc-manifest-1 | text: {first.chunks[0].content}",
        f"title: doc-other | text: {second.content}",
    ]
    assert all("unrelated-filename" not in text for text in texts)
    assert all(chunk.document_identity not in text for chunk, text in zip(request.chunks, texts))


def test_no_unverified_token_or_normalization_evidence_invokes_provider(tmp_path):
    request = fixture_request(tmp_path)
    models = Models()
    result = GoogleEmbeddingAdapter(models.client, request.capability).embed(request)
    assert result.failure.code is BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED
    assert result.failure.field == "provider_bounds" and result.invocation_count == 0
    assert models.calls == []


@pytest.mark.parametrize("overrides", [
    {"evidence_identity": "pretend-approved-live-v99"},
    {"max_item_tokens": 10**12, "max_aggregate_tokens": 10**12,
     "max_provider_items": 10**12, "max_item_bytes": 10**12,
     "max_payload_bytes": 10**12},
    {"normalization_tolerance": 0.99},
])
def test_caller_supplied_evidence_cannot_authorize_live_like_client(tmp_path, overrides):
    request = fixture_request(tmp_path)
    attempts = []
    technical_calls = []

    def token_counter(_value):
        technical_calls.append("token")
        return 1

    def serializer(contents, config):
        technical_calls.append("serialization")
        return serialized(contents, config)

    class LiveLikeModels:
        def embed_content(self, **kwargs):
            attempts.append(kwargs)
            raise AssertionError("transport must never be reached")

    verification = proof(
        count_tokens=token_counter, serialize_payload=serializer, **overrides,
    )
    client = SimpleNamespace(models=LiveLikeModels())
    adapter = GoogleEmbeddingAdapter(client, request.capability, gemini_verification=verification)
    result = adapter.embed(request)
    assert result.failure.code is BuildFailureCode.PROVIDER_UNAVAILABLE
    assert result.failure.field == "provider_authorization"
    assert result.invocation_count == 0 and attempts == [] and technical_calls == []

    query = QueryEmbeddingRequest(
        request.build_identity, CanonicalKnowledgeQuery("1.0", "1.0", "database timeout"),
        request.capability, request.limits,
    )
    query_result = adapter.embed_query(query)
    assert query_result.invocation_count == 0
    assert query_result.failure_detail == "live Gemini invocation is unauthorized"
    assert query_result.failure.code is BuildFailureCode.PROVIDER_UNAVAILABLE
    assert query_result.failure.field == "provider_authorization"
    assert query_result.failure.retry_safety is RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED
    assert attempts == [] and technical_calls == []


def test_subclass_cannot_override_closed_offline_invocation_path(tmp_path):
    request = fixture_request(tmp_path)
    attempts = []

    class UnsafeSubclass(_GeminiEmbedding2OfflineClient):
        def embed_content(self, **kwargs):
            attempts.append(kwargs)
            raise AssertionError("subclass invocation must be denied")

    result = GoogleEmbeddingAdapter(
        UnsafeSubclass(), request.capability, gemini_verification=proof(),
    ).embed(request)
    assert result.failure.field == "provider_authorization"
    assert result.invocation_count == 0 and attempts == []


@pytest.mark.parametrize("change", [
    {"max_item_tokens": 1}, {"max_aggregate_tokens": 1},
    {"max_item_bytes": 1}, {"max_payload_bytes": 1},
    {"serialize_payload": lambda _contents, _config: b"{}"},
])
def test_item_token_and_payload_bounds_fail_closed(tmp_path, change):
    request = fixture_request(tmp_path)
    models = Models()
    result = GoogleEmbeddingAdapter(
        models.client, request.capability,
        gemini_verification=proof(**change),
    ).embed(request)
    assert result.failure is not None and result.invocation_count == 0
    assert result.failure.code in (BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED, BuildFailureCode.PROVIDER_CONTRACT_INVALID)
    assert models.calls == []


def test_verified_provider_item_bound_rejects_54_before_sdk(tmp_path):
    request = fixture_request(tmp_path, 54)
    models = Models()
    result = GoogleEmbeddingAdapter(
        models.client, request.capability,
        gemini_verification=proof(max_provider_items=53),
    ).embed(request)
    assert result.failure.code is BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED
    assert result.invocation_count == 0 and models.calls == []


@pytest.mark.parametrize("vectors", [
    [], [[1.0] + [0.0] * 767] * 2, [[1.0] * 3],
    [[float("nan")] + [0.0] * 767], [[float("inf")] + [0.0] * 767],
    [[0.0] * 768], [[0.5] + [0.0] * 767], [["1"] + [0.0] * 767],
])
def test_bad_response_shape_values_and_normalization_fail_closed(tmp_path, vectors):
    request = fixture_request(tmp_path)
    models = Models(SimpleNamespace(embeddings=[SimpleNamespace(values=item) for item in vectors]))
    result = GoogleEmbeddingAdapter(
        models.client, request.capability, gemini_verification=proof(),
    ).embed(request)
    assert result.failure.code is BuildFailureCode.PROVIDER_CONTRACT_INVALID
    assert result.failure.field == "response_contract" and result.invocation_count == 1
    assert len(models.calls) == 1


@pytest.mark.parametrize("response", [
    SimpleNamespace(), SimpleNamespace(embeddings=None),
    SimpleNamespace(embeddings=[SimpleNamespace()]),
    SimpleNamespace(embeddings=[SimpleNamespace(values=None)]),
    SimpleNamespace(embeddings=[SimpleNamespace(values=[])]),
])
def test_missing_response_fields_fail_closed(tmp_path, response):
    request = fixture_request(tmp_path)
    models = Models(response=response)
    result = GoogleEmbeddingAdapter(
        models.client, request.capability, gemini_verification=proof(),
    ).embed(request)
    assert result.failure.code is BuildFailureCode.PROVIDER_CONTRACT_INVALID
    assert result.failure.field == "response_contract" and len(models.calls) == 1


def test_query_uses_asymmetric_envelope_and_single_invocation(tmp_path):
    request = fixture_request(tmp_path)
    models = Models()
    adapter = GoogleEmbeddingAdapter(models.client, request.capability, gemini_verification=proof())
    query = QueryEmbeddingRequest(
        request.build_identity, CanonicalKnowledgeQuery("1.0", "1.0", "database timeout"),
        request.capability, request.limits,
    )
    result = adapter.embed_query(query)
    assert result.failure_detail is None and result.invocation_count == 1
    assert result.failure is None
    assert models.calls[0]["contents"][0].parts[0].text == "task: search result | query: database timeout"


@pytest.mark.parametrize("failure_detail,failure", [
    (None, BuildFailureFact(BuildFailureCode.PROVIDER_UNAVAILABLE, "transport", "transport: failed", RetrySafetyDisposition.DO_NOT_RETRY)),
    ("failed", BuildFailureFact(BuildFailureCode.PROVIDER_UNAVAILABLE, "transport", "different", RetrySafetyDisposition.DO_NOT_RETRY)),
    ("failed", object()),
])
def test_query_typed_failure_must_match_legacy_detail(failure_detail, failure):
    cap = selected()
    with pytest.raises(KnowledgeValidationError):
        QueryEmbeddingResult(cap, (1.0,) if failure_detail is None else (), 1, failure_detail, failure=failure)
    legacy = QueryEmbeddingResult(cap, (), 1, "legacy query failure")
    assert legacy.failure is None and legacy.failure_detail == "legacy query failure"


@pytest.mark.parametrize("error,code,field,detail,retry", [
    (None, BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED, "provider_bounds",
     "provider_bounds: request exceeds verified item, token, or serialized payload bounds",
     RetrySafetyDisposition.DO_NOT_RETRY),
    (ValueError("api_key=synthetic-secret"), BuildFailureCode.PROVIDER_CONTRACT_INVALID,
     "local_serialization", "local_serialization: local SDK request serialization failed",
     RetrySafetyDisposition.DO_NOT_RETRY),
    (ConnectionError("api_key=synthetic-secret"), BuildFailureCode.PROVIDER_UNAVAILABLE,
     "transport", "transport: embedding transport failed or timed out",
     RetrySafetyDisposition.SAME_OPERATION_ONLY),
    (TimeoutError("api_key=synthetic-secret"), BuildFailureCode.PROVIDER_UNAVAILABLE,
     "transport_timeout", "transport_timeout: embedding transport timed out",
     RetrySafetyDisposition.SAME_OPERATION_ONLY),
    (httpx.ReadTimeout("api_key=synthetic-secret"), BuildFailureCode.PROVIDER_UNAVAILABLE,
     "transport_timeout", "transport_timeout: embedding transport timed out",
     RetrySafetyDisposition.SAME_OPERATION_ONLY),
])
def test_query_failure_classes_are_typed_and_single_attempt(tmp_path, error, code, field, detail, retry):
    request = fixture_request(tmp_path)
    models = Models(error=error)
    verification = proof(max_item_tokens=1) if error is None else proof()
    adapter = GoogleEmbeddingAdapter(models.client, request.capability, gemini_verification=verification)
    query = QueryEmbeddingRequest(
        request.build_identity, CanonicalKnowledgeQuery("1.0", "1.0", "database timeout"),
        request.capability, request.limits,
    )
    result = adapter.embed_query(query)
    assert result.failure.code is code and result.failure.field == field
    assert result.failure.detail == result.failure_detail == detail
    assert result.failure.retry_safety is retry
    assert "synthetic-secret" not in result.failure_detail
    assert result.invocation_count == (0 if error is None else 1)
    assert len(models.calls) == result.invocation_count


@pytest.mark.parametrize("vectors", [[], [[1.0] * 3], [[0.0] * 768]])
def test_query_response_contract_failure_is_typed(tmp_path, vectors):
    request = fixture_request(tmp_path)
    models = Models(SimpleNamespace(embeddings=[SimpleNamespace(values=item) for item in vectors]))
    adapter = GoogleEmbeddingAdapter(models.client, request.capability, gemini_verification=proof())
    query = QueryEmbeddingRequest(
        request.build_identity, CanonicalKnowledgeQuery("1.0", "1.0", "database timeout"),
        request.capability, request.limits,
    )
    result = adapter.embed_query(query)
    assert result.failure.code is BuildFailureCode.PROVIDER_CONTRACT_INVALID
    assert result.failure.field == "response_contract" and result.invocation_count == 1
    assert result.failure.detail == result.failure_detail
    assert result.failure.retry_safety is RetrySafetyDisposition.DO_NOT_RETRY
    assert len(models.calls) == 1


@pytest.mark.parametrize("status,code,field,detail,retry", [
    (400, BuildFailureCode.PROVIDER_CONTRACT_INVALID, "provider_request",
     "provider_request: provider rejected the embedding request contract", RetrySafetyDisposition.DO_NOT_RETRY),
    (401, BuildFailureCode.PROVIDER_UNAVAILABLE, "provider_authorization",
     "provider authentication or authorization failed", RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED),
    (403, BuildFailureCode.PROVIDER_UNAVAILABLE, "provider_authorization",
     "provider authentication or authorization failed", RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED),
    (408, BuildFailureCode.PROVIDER_UNAVAILABLE, "provider_timeout",
     "provider_timeout: provider request timed out", RetrySafetyDisposition.SAME_OPERATION_ONLY),
    (429, BuildFailureCode.PROVIDER_EXHAUSTED, "provider_rate_limit",
     "provider_rate_limit: provider rate limit exhausted", RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED),
    (500, BuildFailureCode.PROVIDER_UNAVAILABLE, "provider_service",
     "provider_service: provider service is unavailable", RetrySafetyDisposition.SAME_OPERATION_ONLY),
    (503, BuildFailureCode.PROVIDER_UNAVAILABLE, "provider_service",
     "provider_service: provider service is unavailable", RetrySafetyDisposition.SAME_OPERATION_ONLY),
    (504, BuildFailureCode.PROVIDER_UNAVAILABLE, "provider_timeout",
     "provider_timeout: provider request timed out", RetrySafetyDisposition.SAME_OPERATION_ONLY),
    (507, BuildFailureCode.PROVIDER_EXHAUSTED, "provider_resource",
     "provider_resource: provider quota or resource exhausted", RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED),
])
def test_query_provider_http_error_has_typed_provider_field_without_live_transport(
    offline_sdk, tmp_path, status, code, field, detail, retry,
):
    request = fixture_request(tmp_path)
    sdk_client, captured = offline_sdk.make_client(status=status)
    contents = [offline_sdk.types.Content(
        role="user", parts=[offline_sdk.types.Part(text="task: search result | query: database timeout")],
    )]
    with pytest.raises(offline_sdk.errors.APIError) as error:
        sdk_client.models.embed_content(
            model="gemini-embedding-2", contents=contents,
            config=offline_sdk.types.EmbedContentConfig(output_dimensionality=768),
        )
    assert len(captured) == 1
    models = Models(error=error.value)
    adapter = GoogleEmbeddingAdapter(models.client, request.capability, gemini_verification=proof())
    result = adapter.embed_query(QueryEmbeddingRequest(
        request.build_identity, CanonicalKnowledgeQuery("1.0", "1.0", "database timeout"),
        request.capability, request.limits,
    ))
    assert result.failure.code is code and result.failure.field == field
    assert result.failure.detail == result.failure_detail == detail
    assert result.failure.retry_safety is retry
    assert result.invocation_count == 1 and len(models.calls) == 1


def test_query_provider_error_never_copies_sensitive_response_text(offline_sdk, tmp_path):
    request = fixture_request(tmp_path)
    error = offline_sdk.errors.ClientError(401, {"error": {
        "code": 401, "status": "UNAUTHENTICATED",
        "message": "api_key=synthetic-secret Authorization: Bearer synthetic-secret",
    }}, None)
    models = Models(error=error)
    adapter = GoogleEmbeddingAdapter(models.client, request.capability, gemini_verification=proof())
    result = adapter.embed_query(QueryEmbeddingRequest(
        request.build_identity, CanonicalKnowledgeQuery("1.0", "1.0", "database timeout"),
        request.capability, request.limits,
    ))
    assert result.failure.code is BuildFailureCode.PROVIDER_UNAVAILABLE
    assert result.failure.field == "provider_authorization"
    assert result.failure.detail == result.failure_detail == "provider authentication or authorization failed"
    assert result.failure.retry_safety is RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED
    assert "synthetic-secret" not in result.failure_detail
    assert result.invocation_count == 1 and len(models.calls) == 1


def test_build_passes_approved_manifest_id_without_durable_store(tmp_path):
    cap = selected()
    raw, chunks, identity = manifest_and_plan(tmp_path, profile=cap)
    provider = MagicMock()
    provider.capability.return_value = cap
    provider.embed.side_effect = RuntimeError("synthetic stop before index or persistence")
    store = MagicMock(spec=KnowledgePersistence)
    store.get_staged_build.return_value = KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND)
    service = KnowledgeBuildService(store, provider, DeterministicIndex())
    result = service.stage(
        operation_key=BuildOperationKey("synthetic-stage"), raw_manifest=raw,
        source_root=tmp_path, chunks=chunks, identity_input=identity,
        limits=limits(timeout_seconds=30),
        required_capability_identity=cap.capability_identity,
    )
    assert result.failures and provider.embed.call_count == 1
    forwarded = provider.embed.call_args.args[0]
    assert forwarded.approved_document_ids == ((chunks[0].document_identity, "doc-manifest-1"),)
    assert forwarded.approved_corpus_id == raw["corpus_id"]
    assert document_identity(forwarded.approved_corpus_id, "doc-manifest-1") == chunks[0].document_identity
    assert forwarded.chunks == chunks
    store.create_staged_build.assert_not_called()


def test_multiple_approved_documents_reach_build_request_without_reordering(tmp_path):
    from knowledge_index import BuildDocumentProvenance, derive_chunk_metadata_commitment
    from knowledge_index.manifest import admit_manifest

    cap = selected()
    raw_a, chunks_a, identity_a = manifest_and_plan(tmp_path, profile=cap, name="a.txt", manifest_id="a")
    raw_b, chunks_b, _ = manifest_and_plan(tmp_path, profile=cap, name="b.txt", manifest_id="b")
    raw_a["documents"] = raw_a["documents"] + raw_b["documents"]
    admission = admit_manifest(raw_a, source_root=tmp_path)
    assert admission.accepted and admission.manifest_commitment
    by_path = {item.source_path: item for item in admission.admitted_documents}
    governed = {item.source_path: item for item in admission.manifest.documents}
    chunks = []
    for source, chunk in (("a.txt", chunks_a[0]), ("b.txt", chunks_b[0])):
        admitted = by_path[source]
        approved = governed[source]
        provenance = BuildDocumentProvenance(
            admitted.document_identity, admitted.document_version_identity,
            source, admitted.content_hash, approved.approval_reference,
            approved.source_classification, approved.outbound_eligible,
            approved.content_type, approved.metadata,
        )
        chunks.append(replace(chunk, metadata_commitment=derive_chunk_metadata_commitment(
            admission.manifest_commitment, provenance,
            chunk_identity=chunk.chunk_identity, section_identity=chunk.section_identity,
            ordinal=chunk.ordinal, content_hash=chunk.content_hash,
        )))
    identity = replace(
        identity_a, manifest_commitment=admission.manifest_commitment,
        ordered_chunk_identities=tuple(item.chunk_identity for item in chunks),
    )
    provider = MagicMock()
    provider.capability.return_value = cap
    provider.embed.side_effect = RuntimeError("synthetic stop")
    store = MagicMock(spec=KnowledgePersistence)
    store.get_staged_build.return_value = KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND)
    result = KnowledgeBuildService(store, provider, DeterministicIndex()).stage(
        operation_key=BuildOperationKey("synthetic-multiple"), raw_manifest=raw_a,
        source_root=tmp_path, chunks=tuple(chunks), identity_input=identity,
        limits=limits(timeout_seconds=30), required_capability_identity=cap.capability_identity,
    )
    assert result.failures and provider.embed.call_count == 1
    forwarded = provider.embed.call_args.args[0]
    assert forwarded.chunks == tuple(chunks)
    assert dict(forwarded.approved_document_ids) == {
        chunks[0].document_identity: "doc-a", chunks[1].document_identity: "doc-b",
    }
    store.create_staged_build.assert_not_called()


def test_sdk_mock_transport_confirms_adapter_payload_and_route(offline_sdk, tmp_path):
    request = fixture_request(tmp_path, 54)
    client, captured = offline_sdk.make_client()
    adapter = GoogleEmbeddingAdapter(client, request.capability, gemini_verification=proof())
    denied = adapter.embed(request)
    assert denied.failure.field == "provider_authorization"
    assert denied.invocation_count == 0 and captured == []
    texts = tuple(
        f"title: doc-manifest-1 | text: {chunk.content}" for chunk in request.chunks
    )
    contents, config = adapter._gemini_prepare(texts, request.limits)
    result = client.models.embed_content(
        model="gemini-embedding-2", contents=list(contents), config=config,
    )
    # Direct SDK invocation is isolated by the fixture's MockTransport.
    assert len(result.embeddings) == 54
    assert len(captured) == 1
    assert str(captured[0].url).endswith("/v1/models/gemini-embedding-2:batchEmbedContents")
    preflight_payload = serialized(
        tuple(offline_sdk.types.Content(role="user", parts=[offline_sdk.types.Part(text=
            f"title: doc-manifest-1 | text: {chunk.content}")]) for chunk in request.chunks),
        offline_sdk.types.EmbedContentConfig(output_dimensionality=768),
    )
    assert json.loads(captured[0].content) == json.loads(preflight_payload)
    assert len(captured[0].content) <= len(preflight_payload)
    assert len(json.loads(captured[0].content)["requests"]) == 54


def test_unicode_escaping_is_covered_by_conservative_serialized_bound(offline_sdk, tmp_path):
    request = fixture_request(tmp_path)
    chunk = request.chunks[0]
    content = '維運 "quoted"\\path\nline'
    altered = replace(
        chunk, content=content, content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )
    request = replace(request, chunks=(altered,))
    client, captured = offline_sdk.make_client()
    adapter = GoogleEmbeddingAdapter(client, request.capability, gemini_verification=proof())
    denied = adapter.embed(request)
    assert denied.failure.field == "provider_authorization" and captured == []
    contents, config = adapter._gemini_prepare(
        (f"title: doc-manifest-1 | text: {content}",), request.limits,
    )
    result = client.models.embed_content(model="gemini-embedding-2", contents=list(contents), config=config)
    assert len(result.embeddings) == 1 and len(captured) == 1
    expected = {"requests": [{
        "model": "models/gemini-embedding-2",
        "content": {"role": "user", "parts": [{"text": f"title: doc-manifest-1 | text: {content}"}]},
        "outputDimensionality": 768,
    }]}
    assert json.loads(captured[0].content) == expected
    assert len(captured[0].content) <= len(json.dumps(expected, ensure_ascii=True, indent=2).encode("utf-8"))


@pytest.mark.parametrize("status,expected", [
    (429, BuildFailureCode.PROVIDER_EXHAUSTED),
    (503, BuildFailureCode.PROVIDER_UNAVAILABLE),
])
def test_mock_http_provider_error_is_distinct_and_single_attempt(offline_sdk, tmp_path, status, expected):
    request = fixture_request(tmp_path)
    client, captured = offline_sdk.make_client(status=status)
    adapter = GoogleEmbeddingAdapter(client, request.capability, gemini_verification=proof())
    denied = adapter.embed(request)
    assert denied.failure.field == "provider_authorization" and captured == []
    contents, config = adapter._gemini_prepare(
        (f"title: doc-manifest-1 | text: {request.chunks[0].content}",), request.limits,
    )
    with pytest.raises(offline_sdk.errors.APIError) as caught:
        client.models.embed_content(model="gemini-embedding-2", contents=list(contents), config=config)
    code, field, _, _ = adapter._gemini_invocation_failure(caught.value)
    assert code is expected and field == "provider_error" and len(captured) == 1


def test_mock_transport_failure_and_local_serialization_classification(offline_sdk, tmp_path):
    request = fixture_request(tmp_path)
    client, captured = offline_sdk.make_client(transport_error=httpx.ConnectError)
    adapter = GoogleEmbeddingAdapter(client, request.capability, gemini_verification=proof())
    denied = adapter.embed(request)
    assert denied.failure.field == "provider_authorization" and captured == []
    contents, config = adapter._gemini_prepare(
        (f"title: doc-manifest-1 | text: {request.chunks[0].content}",), request.limits,
    )
    with pytest.raises(httpx.ConnectError) as caught:
        client.models.embed_content(model="gemini-embedding-2", contents=list(contents), config=config)
    code, field, _, _ = adapter._gemini_invocation_failure(caught.value)
    assert code is BuildFailureCode.PROVIDER_UNAVAILABLE
    assert field == "transport" and len(captured) == 1
    models = Models(error=ValueError("synthetic local serializer error"))
    local = GoogleEmbeddingAdapter(models.client, request.capability, gemini_verification=proof()).embed(request)
    assert local.failure.code is BuildFailureCode.PROVIDER_CONTRACT_INVALID
    assert local.failure.field == "local_serialization" and len(models.calls) == 1
