from dataclasses import replace
import sys
from types import SimpleNamespace

import pytest

from knowledge_index import (
    BuildFailureCode, BuildOperationKey, CanonicalKnowledgeQuery,
    KnowledgeBuildService, ProviderEmbeddingRequest, QueryEmbeddingRequest,
    RetrySafetyDisposition, SqliteKnowledgeStore,
)
from knowledge_index.google_embedding_adapter import GoogleEmbeddingAdapter, create_google_client
from _knowledge_build_testkit import DeterministicIndex, capability, limits, manifest_and_plan


_DEFAULT_RESPONSE = object()


class FakeModels:
    def __init__(self, *, error=None, dimension=768, response=_DEFAULT_RESPONSE):
        self.error = error
        self.dimension = dimension
        self.response = response
        self.calls = []

    def embed_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if self.response is not _DEFAULT_RESPONSE:
            return self.response
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.25] * self.dimension) for _ in kwargs["contents"]])


class FakeProviderError(Exception):
    def __init__(self, *, code=None, status=None, status_code=None, message="synthetic provider failure"):
        super().__init__(message)
        self.code = code
        self.status = status
        self.status_code = status_code


def approved_capability():
    return replace(capability(), embedding_dimension=768)


def task(config):
    return config.get("task_type") if isinstance(config, dict) else config.task_type


def test_document_and_query_tasks_use_one_fixed_model_execution(tmp_path) -> None:
    selected = approved_capability()
    _, chunks, identity = manifest_and_plan(tmp_path, profile=selected)
    models = FakeModels()
    adapter = GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected)
    result = adapter.embed(ProviderEmbeddingRequest(identity_input_id(identity), selected, chunks, limits()))
    assert not result.failure and len(result.embeddings) == len(chunks)
    assert len(models.calls) == 1
    assert models.calls[0]["model"] == "text-embedding-004"
    assert task(models.calls[0]["config"]) == "RETRIEVAL_DOCUMENT"
    query = QueryEmbeddingRequest(
        identity_input_id(identity), CanonicalKnowledgeQuery("1.0", "1.0", "database timeout"), selected, limits()
    )
    query_result = adapter.embed_query(query)
    assert query_result.failure_detail is None and len(query_result.vector) == 768
    assert len(models.calls) == 2 and task(models.calls[1]["config"]) == "RETRIEVAL_QUERY"


def identity_input_id(identity):
    from knowledge_index import build_identity
    return build_identity(identity)


def test_substitution_bounds_quota_and_malformed_results_fail_closed(tmp_path) -> None:
    selected = approved_capability()
    with pytest.raises(ValueError):
        GoogleEmbeddingAdapter(SimpleNamespace(models=FakeModels()), replace(selected, model="substitute"))
    _, chunks, identity = manifest_and_plan(tmp_path, profile=selected)
    adapter = GoogleEmbeddingAdapter(SimpleNamespace(models=FakeModels()), selected)
    bounded = adapter.embed(ProviderEmbeddingRequest(identity_input_id(identity), selected, chunks, limits(maximum_request_bytes=1)))
    assert bounded.failure.code is BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED
    assert adapter._client.models.calls == []

    quota_models = FakeModels(error=FakeProviderError(
        code=429,
        status="RESOURCE_EXHAUSTED",
        message="api_key=synthetic-secret",
    ))
    quota = GoogleEmbeddingAdapter(SimpleNamespace(models=quota_models), selected).embed(
        ProviderEmbeddingRequest(identity_input_id(identity), selected, chunks, limits())
    )
    assert quota.failure.code is BuildFailureCode.PROVIDER_EXHAUSTED
    assert quota.failure.retry_safety is RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED
    assert "synthetic-secret" not in repr(quota)
    assert len(quota_models.calls) == 1
    malformed = GoogleEmbeddingAdapter(SimpleNamespace(models=FakeModels(dimension=3)), selected).embed(
        ProviderEmbeddingRequest(identity_input_id(identity), selected, chunks, limits())
    )
    assert malformed.failure.code is BuildFailureCode.PROVIDER_CONTRACT_INVALID


def test_projected_usage_within_all_bounds_calls_provider_once(tmp_path) -> None:
    selected = approved_capability()
    _, chunks, identity = manifest_and_plan(tmp_path, profile=selected)
    models = FakeModels()
    adapter = GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected)
    result = adapter.embed(ProviderEmbeddingRequest(
        identity_input_id(identity), selected, chunks * 2, limits(
            maximum_cost_units=2,
            maximum_rate_units=2,
            maximum_quota_units=2,
            maximum_resource_units=2,
        ),
    ))
    assert result.failure is None
    assert (result.cost_units, result.rate_units, result.quota_units, result.resource_units) == (2, 2, 2, 2)
    assert len(models.calls) == 1


@pytest.mark.parametrize(
    "bounded_field",
    [
        "maximum_cost_units",
        "maximum_rate_units",
        "maximum_quota_units",
        "maximum_resource_units",
    ],
)
def test_projected_usage_over_any_unit_bound_fails_before_provider_call(tmp_path, bounded_field) -> None:
    selected = approved_capability()
    _, chunks, identity = manifest_and_plan(tmp_path, profile=selected)
    models = FakeModels()
    adapter = GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected)
    result = adapter.embed(ProviderEmbeddingRequest(
        identity_input_id(identity), selected, chunks * 2, limits(**{bounded_field: 1}),
    ))
    assert result.failure.code is BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED
    assert result.invocation_count == 0
    assert models.calls == []


def test_multiple_projected_unit_bounds_fail_deterministically_before_call(tmp_path) -> None:
    selected = approved_capability()
    _, chunks, identity = manifest_and_plan(tmp_path, profile=selected)
    models = FakeModels()
    adapter = GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected)
    request = ProviderEmbeddingRequest(
        identity_input_id(identity), selected, chunks * 2, limits(
            maximum_cost_units=1,
            maximum_rate_units=1,
            maximum_quota_units=1,
            maximum_resource_units=1,
        ),
    )
    first = adapter.embed(request)
    second = adapter.embed(request)
    assert first == second
    assert first.failure.code is BuildFailureCode.PROVIDER_BOUNDS_EXCEEDED
    assert first.invocation_count == 0
    assert models.calls == []


def _document_request(tmp_path):
    selected = approved_capability()
    raw, chunks, identity = manifest_and_plan(tmp_path, profile=selected)
    request = ProviderEmbeddingRequest(identity_input_id(identity), selected, chunks, limits())
    return selected, raw, chunks, identity, request


def _query_request(identity, selected):
    return QueryEmbeddingRequest(
        identity_input_id(identity),
        CanonicalKnowledgeQuery("1.0", "1.0", "database timeout"),
        selected,
        limits(),
    )


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_retry", "expected_detail"),
    [
        (
            FakeProviderError(code=429, status="RESOURCE_EXHAUSTED"),
            BuildFailureCode.PROVIDER_EXHAUSTED,
            RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED,
            "provider quota or resource exhausted",
        ),
        (
            FakeProviderError(status_code=429),
            BuildFailureCode.PROVIDER_EXHAUSTED,
            RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED,
            "provider quota or resource exhausted",
        ),
        (
            FakeProviderError(status="RESOURCE_EXHAUSTED"),
            BuildFailureCode.PROVIDER_EXHAUSTED,
            RetrySafetyDisposition.EXTERNAL_AUTHORIZATION_REQUIRED,
            "provider quota or resource exhausted",
        ),
        (
            FakeProviderError(code=408),
            BuildFailureCode.PROVIDER_UNAVAILABLE,
            RetrySafetyDisposition.SAME_OPERATION_ONLY,
            "provider invocation timed out",
        ),
        (
            TimeoutError("Authorization: Bearer synthetic-secret"),
            BuildFailureCode.PROVIDER_UNAVAILABLE,
            RetrySafetyDisposition.SAME_OPERATION_ONLY,
            "provider invocation timed out",
        ),
        (
            RuntimeError("api_key=synthetic-secret"),
            BuildFailureCode.PROVIDER_UNAVAILABLE,
            RetrySafetyDisposition.SAME_OPERATION_ONLY,
            "provider invocation unavailable",
        ),
    ],
)
def test_invocation_failures_use_typed_bounded_mapping_without_retry_or_secret_leakage(
    tmp_path, error, expected_code, expected_retry, expected_detail
) -> None:
    selected, _, _, _, request = _document_request(tmp_path)
    models = FakeModels(error=error)

    result = GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected).embed(request)

    assert result.failure.code is expected_code
    assert result.failure.retry_safety is expected_retry
    assert result.failure.detail == expected_detail
    assert result.invocation_count == 1
    assert len(models.calls) == 1
    assert "synthetic-secret" not in repr(result)
    assert "Authorization" not in repr(result)
    assert result.failure is not error


@pytest.mark.parametrize("code", [401, 403, 404, 500, 503])
def test_other_typed_http_failures_use_existing_unavailable_fallback(tmp_path, code) -> None:
    selected, _, _, _, request = _document_request(tmp_path)
    models = FakeModels(error=FakeProviderError(code=code, status="ALLOWLISTED_BUT_COARSE"))

    result = GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected).embed(request)

    assert result.failure.code is BuildFailureCode.PROVIDER_UNAVAILABLE
    assert result.failure.retry_safety is RetrySafetyDisposition.SAME_OPERATION_ONLY
    assert result.failure.detail == "provider invocation unavailable"
    assert len(models.calls) == 1


def test_exception_name_alone_no_longer_claims_quota_exhaustion(tmp_path) -> None:
    class QuotaError(Exception):
        pass

    selected, _, _, _, request = _document_request(tmp_path)
    models = FakeModels(error=QuotaError("resource exhausted api_key=synthetic-secret"))

    result = GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected).embed(request)

    assert result.failure.code is BuildFailureCode.PROVIDER_UNAVAILABLE
    assert result.failure.retry_safety is RetrySafetyDisposition.SAME_OPERATION_ONLY
    assert "synthetic-secret" not in repr(result)
    assert len(models.calls) == 1


def test_hostile_typed_attribute_fails_closed_without_reading_error_text(tmp_path) -> None:
    class HostileTypedError(Exception):
        @property
        def code(self):
            raise RuntimeError("Authorization: Bearer synthetic-secret")

    selected, _, _, _, request = _document_request(tmp_path)
    models = FakeModels(error=HostileTypedError("api_key=synthetic-secret"))

    result = GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected).embed(request)

    assert result.failure.code is BuildFailureCode.PROVIDER_UNAVAILABLE
    assert result.failure.detail == "provider invocation unavailable"
    assert "synthetic-secret" not in repr(result)


def _numeric_response(value, cardinality):
    return SimpleNamespace(embeddings=[
        SimpleNamespace(values=[value] + [0.25] * 767)
        for _ in range(cardinality)
    ])


@pytest.mark.parametrize(
    ("case", "response_factory"),
    [
        ("missing_embeddings", lambda count: SimpleNamespace()),
        ("missing_values", lambda count: SimpleNamespace(embeddings=[SimpleNamespace()])),
        ("non_iterable_embeddings", lambda count: SimpleNamespace(embeddings=1)),
        ("non_iterable_values", lambda count: SimpleNamespace(embeddings=[SimpleNamespace(values=object())])),
        ("invalid_numeric_string", lambda count: _numeric_response("not-a-number", count)),
        ("nan", lambda count: _numeric_response(float("nan"), count)),
        ("positive_infinity", lambda count: _numeric_response(float("inf"), count)),
        ("negative_infinity", lambda count: _numeric_response(float("-inf"), count)),
        ("wrong_cardinality", lambda count: _numeric_response(0.25, count + 1)),
        ("wrong_dimension", lambda count: SimpleNamespace(embeddings=[
            SimpleNamespace(values=[0.25] * 767) for _ in range(count)
        ])),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_local_response_failures_are_non_retryable_provider_contract_failures(
    tmp_path, case, response_factory
) -> None:
    selected, _, chunks, _, request = _document_request(tmp_path)
    models = FakeModels(response=response_factory(len(chunks)))

    result = GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected).embed(request)

    assert result.failure.code is BuildFailureCode.PROVIDER_CONTRACT_INVALID, case
    assert result.failure.retry_safety is RetrySafetyDisposition.DO_NOT_RETRY
    assert result.failure.detail == "provider returned incompatible embedding facts"
    assert result.invocation_count == 1
    assert len(models.calls) == 1


def test_local_response_failure_does_not_escape_to_build_service_fallback(tmp_path) -> None:
    selected, raw, chunks, identity, _ = _document_request(tmp_path)
    models = FakeModels(response=_numeric_response(float("nan"), len(chunks)))
    adapter = GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected)
    store = SqliteKnowledgeStore(tmp_path / "isolated-knowledge.sqlite3")
    try:
        result = KnowledgeBuildService(store, adapter, DeterministicIndex()).stage(
            operation_key=BuildOperationKey("stage-malformed-google-response"),
            raw_manifest=raw,
            source_root=tmp_path,
            chunks=chunks,
            identity_input=identity,
            limits=limits(),
            required_capability_identity=selected.capability_identity,
        )
    finally:
        store.close()

    assert result.record is None
    assert result.failures[0].code is BuildFailureCode.PROVIDER_CONTRACT_INVALID
    assert result.failures[0].retry_safety is RetrySafetyDisposition.DO_NOT_RETRY
    assert len(models.calls) == 1


def test_unexpected_programming_error_during_response_access_is_not_blanket_masked(tmp_path) -> None:
    class BrokenResponse:
        @property
        def embeddings(self):
            raise RuntimeError("local programming failure")

    selected, _, _, _, request = _document_request(tmp_path)
    models = FakeModels(response=BrokenResponse())

    with pytest.raises(RuntimeError, match="local programming failure"):
        GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected).embed(request)
    assert len(models.calls) == 1


@pytest.mark.parametrize(
    ("response", "expected_detail"),
    [
        (SimpleNamespace(), "query embedding response is incompatible"),
        (_numeric_response(float("nan"), 1), "query embedding response is incompatible"),
        (SimpleNamespace(embeddings=[SimpleNamespace(values=[0.25] * 767)]), "query embedding response is incompatible"),
    ],
)
def test_query_local_parsing_and_vector_failures_preserve_incompatible_response_behavior(
    tmp_path, response, expected_detail
) -> None:
    selected, _, _, identity, _ = _document_request(tmp_path)
    models = FakeModels(response=response)

    result = GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected).embed_query(
        _query_request(identity, selected)
    )

    assert result.failure_detail == expected_detail
    assert result.invocation_count == 1
    assert result.vector == ()
    assert len(models.calls) == 1


def test_query_invocation_failure_preserves_unavailable_behavior_without_secret_leakage(tmp_path) -> None:
    selected, _, _, identity, _ = _document_request(tmp_path)
    models = FakeModels(error=FakeProviderError(
        code=429,
        message="Authorization: Bearer synthetic-secret",
    ))

    result = GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected).embed_query(
        _query_request(identity, selected)
    )

    assert result.failure_detail == "query embedding provider is unavailable"
    assert result.invocation_count == 1
    assert "synthetic-secret" not in repr(result)
    assert len(models.calls) == 1


def test_client_factory_keeps_sdk_hidden_retry_disabled_with_fake_sdk(monkeypatch) -> None:
    observed = {}

    class FakeRetryOptions:
        def __init__(self, *, attempts):
            observed["attempts"] = attempts

    class FakeHttpOptions:
        def __init__(self, *, timeout, retry_options):
            observed["timeout"] = timeout
            observed["retry_options"] = retry_options

    def fake_client(*, http_options):
        observed["http_options"] = http_options
        return SimpleNamespace(fake=True)

    fake_genai = SimpleNamespace(
        Client=fake_client,
        types=SimpleNamespace(HttpRetryOptions=FakeRetryOptions, HttpOptions=FakeHttpOptions),
    )
    monkeypatch.setitem(sys.modules, "google", SimpleNamespace(genai=fake_genai))
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

    client = create_google_client(timeout_seconds=30)

    assert client.fake is True
    assert observed["attempts"] == 1
    assert observed["timeout"] == 30_000


def test_historical_numeric_string_conversion_remains_unchanged(tmp_path) -> None:
    selected, _, _, _, request = _document_request(tmp_path)
    models = FakeModels(response=SimpleNamespace(
        embeddings=[SimpleNamespace(values=["0.25"] * 768)],
    ))
    result = GoogleEmbeddingAdapter(SimpleNamespace(models=models), selected).embed(request)
    assert result.failure is None
    assert result.embeddings[0].values == (0.25,) * 768
