from dataclasses import replace
from types import SimpleNamespace

import pytest

from knowledge_index import (
    BuildFailureCode, CanonicalKnowledgeQuery, ProviderEmbeddingRequest,
    QueryEmbeddingRequest,
)
from knowledge_index.google_embedding_adapter import GoogleEmbeddingAdapter
from _knowledge_build_testkit import capability, limits, manifest_and_plan


class FakeModels:
    def __init__(self, *, error=None, dimension=768):
        self.error = error
        self.dimension = dimension
        self.calls = []

    def embed_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.25] * self.dimension) for _ in kwargs["contents"]])


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

    class QuotaError(Exception):
        pass

    quota_models = FakeModels(error=QuotaError("api_key=synthetic-secret"))
    quota = GoogleEmbeddingAdapter(SimpleNamespace(models=quota_models), selected).embed(
        ProviderEmbeddingRequest(identity_input_id(identity), selected, chunks, limits())
    )
    assert quota.failure.code is BuildFailureCode.PROVIDER_EXHAUSTED
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
