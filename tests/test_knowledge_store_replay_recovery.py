from dataclasses import replace

import pytest

from knowledge_index import (
    ActivationOperationKey,
    KnowledgeReadStatus,
    KnowledgeStoreConflictError,
    SqliteKnowledgeStore,
)
from _knowledge_store_testkit import activation, build, digest
from _knowledge_build_testkit import limits
from _knowledge_retrieval_testkit import request
from _knowledge_snapshot_testkit import environment


def test_build_equivalent_replay_and_contradiction(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        record = build()
        assert store.create_build_lineage(record) == store.create_build_lineage(record)
        with pytest.raises(KnowledgeStoreConflictError):
            store.create_build_lineage(replace(record, lineage_commitment=digest("f")))


def test_activation_same_operation_resolves_response_loss(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    record = build()
    requested = activation(record)
    with SqliteKnowledgeStore(path) as store:
        store.create_build_lineage(record)
        store.commit_activation(requested, expected_generation=0)
    with SqliteKnowledgeStore(path) as reopened:
        assert reopened.commit_activation(requested, expected_generation=0) == requested


def test_activation_contradictory_operation_replay_is_rejected(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        one, two = build(1), build(2)
        store.create_build_lineage(one)
        store.create_build_lineage(two)
        store.commit_activation(activation(one), expected_generation=0)
        contradiction = replace(activation(two), operation_key=ActivationOperationKey("activate-1"))
        with pytest.raises(KnowledgeStoreConflictError):
            store.commit_activation(contradiction, expected_generation=0)


def test_restart_uses_exact_active_target_not_newest_record(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    active, newer = build(1), build(2)
    with SqliteKnowledgeStore(path) as store:
        store.create_build_lineage(active)
        store.commit_activation(activation(active), expected_generation=0)
        store.create_build_lineage(newer)
    with SqliteKnowledgeStore(path) as reopened:
        assert reopened.read_activation().value.active_build_identity == active.build_identity


def test_snapshot_equivalent_replay_and_contradiction(tmp_path) -> None:
    store, _, _, _, service = environment(tmp_path)
    first = service.resolve(request(), limits()).snapshot
    assert first is not None
    try:
        completed = store.get_operation(request().operation_key).value
        assert store.complete_operation_with_snapshot(first, expected_revision=1) == completed
        with pytest.raises(KnowledgeStoreConflictError):
            store.complete_operation_with_snapshot(
                replace(first, snapshot_commitment=digest("e")), expected_revision=1
            )
    finally:
        store.close()


def test_missing_activation_target_is_repair_required_not_fallback(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    record = build()
    store = SqliteKnowledgeStore(path)
    store.create_build_lineage(record)
    store.commit_activation(activation(record), expected_generation=0)
    connection = __import__("sqlite3").connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DELETE FROM build_lineage")
    connection.commit()
    connection.close()
    try:
        assert store.read_activation().status is KnowledgeReadStatus.REPAIR_REQUIRED
    finally:
        store.close()
