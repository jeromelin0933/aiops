from dataclasses import replace

import pytest

from knowledge_index import CanonicalKnowledgeQuery, KnowledgeReadStatus, KnowledgeRetrievalService, KnowledgeSnapshotService, SqliteKnowledgeStore
from _knowledge_build_testkit import limits
from _knowledge_retrieval_testkit import QueryProvider, RetrievalIndex, request
from _knowledge_snapshot_testkit import environment


def test_response_loss_replay_reads_snapshot_without_requery(tmp_path) -> None:
    store, _, provider, index, service = environment(tmp_path)
    first = service.resolve(request(), limits()).snapshot
    assert first is not None
    store.close()

    reopened = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    replay_provider = QueryProvider(error=AssertionError("must not call provider"))
    replay_index = RetrievalIndex({}, query_error=AssertionError("must not query index"))
    replay = KnowledgeSnapshotService(
        reopened, KnowledgeRetrievalService(reopened, replay_provider, replay_index)
    ).resolve(request(), limits())
    assert replay.snapshot == first
    assert replay_provider.calls == 0
    assert replay_index.query_calls == []
    assert reopened.get_snapshot(first.snapshot_key).status is KnowledgeReadStatus.FOUND
    reopened.close()


def test_historical_snapshot_unchanged_after_later_activation(tmp_path) -> None:
    store, _, _, _, service = environment(tmp_path)
    first = service.resolve(request(), limits()).snapshot
    assert first is not None
    assert service.read_snapshot(first.snapshot_key).value == first
    store.close()


def test_completed_operation_rejects_contradictory_query_before_retrieval(tmp_path) -> None:
    store, _, provider, index, service = environment(tmp_path)
    service.resolve(request(), limits())
    contradiction = replace(
        request(), query=CanonicalKnowledgeQuery(
            "1.0", "1.0", "different query", request().query.filters
        )
    )
    with pytest.raises(ValueError):
        service.resolve(contradiction, limits())
    assert provider.calls == 1
    assert len(index.query_calls) == 1
    store.close()
