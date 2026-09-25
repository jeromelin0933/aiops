from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from knowledge_index import KnowledgeRetrievalService, KnowledgeSnapshotService, SqliteKnowledgeStore
from _knowledge_build_testkit import limits
from _knowledge_retrieval_testkit import QueryProvider, RetrievalIndex, request
from _knowledge_snapshot_testkit import environment, terminal_request


def test_concurrent_same_operation_commits_one_snapshot(tmp_path) -> None:
    store, staged, _, index, _ = environment(tmp_path)
    store.close()

    def run():
        local = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
        provider = QueryProvider()
        retrieval_index = RetrievalIndex(index.artifacts, ())
        service = KnowledgeSnapshotService(
            local, KnowledgeRetrievalService(local, provider, retrieval_index)
        )
        try:
            return service.resolve(request(), limits()).snapshot
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert results[0] == results[1]
    assert results[0] is not None
    assert results[0].frozen_build_identity == staged.build_identity


def test_normal_completion_and_terminal_finalization_have_one_winner(tmp_path) -> None:
    store, _, _, index, _ = environment(tmp_path)
    store.freeze_retrieval_operation(request())
    store.close()
    barrier = Barrier(2)

    def normal():
        local = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
        service = KnowledgeSnapshotService(
            local,
            KnowledgeRetrievalService(local, QueryProvider(), RetrievalIndex(index.artifacts, ())),
        )
        barrier.wait()
        try:
            service.resolve(request(), limits())
            return "normal"
        except Exception:
            return "conflict"
        finally:
            local.close()

    def terminal():
        local = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
        service = KnowledgeSnapshotService(
            local,
            KnowledgeRetrievalService(local, QueryProvider(), RetrievalIndex(index.artifacts, ())),
        )
        barrier.wait()
        try:
            service.finalize_unavailable(terminal_request())
            return "terminal"
        except Exception:
            return "conflict"
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(normal), pool.submit(terminal)]
        results = [item.result() for item in outcomes]
    assert results.count("conflict") == 1
    reopened = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    operation = reopened.get_operation(request().operation_key).value
    assert operation.completed and operation.snapshot_key is not None
    reopened.close()
