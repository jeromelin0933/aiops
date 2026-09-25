from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from knowledge_index import (
    KnowledgeStoreConcurrencyError,
    KnowledgeStoreConflictError,
    SqliteKnowledgeStore,
)
from _knowledge_store_testkit import activation, build, operation, snapshot


def test_activation_generation_is_compare_and_swap_guarded(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        one, two = build(1), build(2)
        store.create_build_lineage(one)
        store.create_build_lineage(two)
        store.commit_activation(activation(one), expected_generation=0)
        with pytest.raises(KnowledgeStoreConcurrencyError):
            store.commit_activation(activation(two, 2, "activate-2"), expected_generation=0)


def test_two_connections_allow_only_one_activation_at_generation(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    records = (build(1), build(2))
    with SqliteKnowledgeStore(path) as store:
        for record in records:
            store.create_build_lineage(record)
    barrier = Barrier(2)

    def attempt(index: int) -> str:
        with SqliteKnowledgeStore(path) as store:
            barrier.wait()
            try:
                store.commit_activation(activation(records[index], 1, f"activate-{index}"), expected_generation=0)
                return "success"
            except KnowledgeStoreConcurrencyError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (0, 1)))
    assert sorted(outcomes) == ["conflict", "success"]


def test_two_connections_cannot_publish_two_snapshots_for_one_operation(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    record = build()
    with SqliteKnowledgeStore(path) as store:
        store.create_build_lineage(record)
        store.create_operation(operation(record))
    barrier = Barrier(2)

    def attempt(index: int) -> str:
        candidate = snapshot(record, snap=f"snapshot-{index}")
        with SqliteKnowledgeStore(path) as store:
            barrier.wait()
            try:
                store.complete_operation_with_snapshot(candidate, expected_revision=1)
                return "success"
            except (KnowledgeStoreConflictError, KnowledgeStoreConcurrencyError):
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (1, 2)))
    assert sorted(outcomes) == ["conflict", "success"]


def test_operation_revision_is_fresh_read_inside_immediate_transaction(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        record = build()
        store.create_build_lineage(record)
        store.create_operation(operation(record))
        store.complete_operation_with_snapshot(snapshot(record), expected_revision=1)
        with pytest.raises(KnowledgeStoreConflictError):
            store.complete_operation_with_snapshot(snapshot(record, snap="different"), expected_revision=1)
