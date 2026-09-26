from knowledge_index import (
    CanonicalKnowledgeQuery,
    KnowledgeRetrievalService,
    QueryFilter,
    QueryEmbeddingProviderPort,
    RetrievalIndexPort,
    RetrievalResolution,
    SqliteKnowledgeStore,
)
from _knowledge_build_testkit import limits
from _knowledge_retrieval_testkit import (
    QueryProvider,
    RetrievalIndex,
    candidates_for,
    request,
    profile,
    stage_activate,
)
from knowledge_index.retrieval import _query_embedding_request_bytes


def test_deterministic_test_adapters_satisfy_owned_ports(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, build_index = stage_activate(store, tmp_path / "one", "one")
        provider = QueryProvider()
        index = RetrievalIndex(build_index.artifacts, candidates_for(staged))
        assert isinstance(provider, QueryEmbeddingProviderPort)
        assert isinstance(index, RetrievalIndexPort)
        result = KnowledgeRetrievalService(store, provider, index).retrieve(request(), limits())
        assert result.resolution is RetrievalResolution.MATCH
        assert provider.calls == 1
        assert index.query_calls[0][0] == staged.build_identity


def test_operational_unavailable_is_not_no_match_and_has_no_hidden_retry(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, build_index = stage_activate(store, tmp_path / "one", "one")
        provider = QueryProvider(error=RuntimeError("offline"))
        index = RetrievalIndex(build_index.artifacts, candidates_for(staged))
        result = KnowledgeRetrievalService(store, provider, index).retrieve(request(), limits())
        assert result.resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE
        assert provider.calls == 1
        assert index.query_calls == []


def test_index_unavailable_is_not_no_match(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, build_index = stage_activate(store, tmp_path / "one", "one")
        index = RetrievalIndex(build_index.artifacts, query_error=RuntimeError("offline"))
        result = KnowledgeRetrievalService(store, QueryProvider(), index).retrieve(request(), limits())
        assert result.resolution is RetrievalResolution.RETRIEVAL_UNAVAILABLE


def test_oversized_request_is_rejected_before_provider_or_index(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, build_index = stage_activate(store, tmp_path / "one", "one")
        provider = QueryProvider()
        index = RetrievalIndex(build_index.artifacts, candidates_for(staged))
        result = KnowledgeRetrievalService(store, provider, index).retrieve(
            request(), limits(maximum_request_bytes=1)
        )
        assert result.resolution is RetrievalResolution.INVALID
        assert provider.calls == 0
        assert index.inspect_calls == []
        assert index.query_calls == []


def test_query_over_profile_byte_bound_is_rejected_before_external_calls(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, build_index = stage_activate(store, tmp_path / "one", "one")
        provider = QueryProvider()
        index = RetrievalIndex(build_index.artifacts, candidates_for(staged))
        oversized = request(retrieval_profile=profile(max_query_bytes=1))
        result = KnowledgeRetrievalService(store, provider, index).retrieve(
            oversized, limits()
        )
        assert result.resolution is RetrievalResolution.INVALID
        assert provider.calls == 0
        assert index.inspect_calls == []
        assert index.query_calls == []


def test_exact_request_byte_bound_is_accepted(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, build_index = stage_activate(store, tmp_path / "one", "one")
        query_size = len(request().query.text.encode("utf-8"))
        exact_request = request(retrieval_profile=profile(max_query_bytes=query_size))
        frozen = store.freeze_retrieval_operation(exact_request)
        exact = _query_embedding_request_bytes(frozen)
        provider = QueryProvider()
        index = RetrievalIndex(build_index.artifacts, candidates_for(staged))
        result = KnowledgeRetrievalService(store, provider, index).retrieve(
            exact_request, limits(maximum_request_bytes=exact)
        )
        assert result.resolution is RetrievalResolution.MATCH
        assert provider.calls == 1
        assert len(index.query_calls) == 1


def test_empty_query_disposition_is_typed_invalid_before_external_calls(tmp_path) -> None:
    from dataclasses import replace

    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, build_index = stage_activate(store, tmp_path / "one", "one")
        provider = QueryProvider()
        index = RetrievalIndex(build_index.artifacts, candidates_for(staged))
        empty = replace(request(), query=CanonicalKnowledgeQuery(
            "1.0", "1.0", "", (QueryFilter("service", "payments"),)
        ))
        result = KnowledgeRetrievalService(store, provider, index).retrieve(empty, limits())
        assert result.resolution is RetrievalResolution.INVALID
        assert provider.calls == 0
        assert index.inspect_calls == []


def test_unsupported_filter_disposition_is_not_unavailable_or_no_match(tmp_path) -> None:
    from dataclasses import replace

    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        staged, build_index = stage_activate(store, tmp_path / "one", "one")
        provider = QueryProvider()
        index = RetrievalIndex(build_index.artifacts, candidates_for(staged))
        unsupported = replace(request(), query=CanonicalKnowledgeQuery(
            "1.0", "1.0", "payments procedure",
            (QueryFilter("service", "payments"), QueryFilter("team", "payments")),
        ))
        result = KnowledgeRetrievalService(store, provider, index).retrieve(
            unsupported, limits()
        )
        assert result.resolution is RetrievalResolution.INVALID
        assert provider.calls == 0
        assert index.inspect_calls == []
        assert index.query_calls == []
