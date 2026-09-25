import sqlite3

import pytest

from knowledge_index import (
    KnowledgeLocalReadiness,
    KnowledgeReadStatus,
    KnowledgeStoreClosedError,
    SqliteKnowledgeStore,
)
from _knowledge_store_testkit import activation, build, operation, snapshot


def test_new_store_creates_explicit_schema_and_enables_foreign_keys(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    store = SqliteKnowledgeStore(path)
    store.close()
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT schema_version FROM knowledge_store_metadata").fetchone() == (3,)
    connection.close()


def test_build_activation_operation_and_snapshot_survive_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    record = build()
    with SqliteKnowledgeStore(path) as store:
        assert store.create_build_lineage(record) == record
        assert store.commit_activation(activation(record), expected_generation=0).generation == 1
        assert store.create_operation(operation(record)) == operation(record)
        completed = store.complete_operation_with_snapshot(snapshot(record), expected_revision=1)
        assert completed.completed and completed.revision == 2

    with SqliteKnowledgeStore(path) as reopened:
        assert reopened.get_build_lineage(record.build_identity).value == record
        assert reopened.read_activation().value == activation(record)
        assert reopened.get_operation(operation(record).operation_key).value == completed
        assert reopened.get_snapshot(snapshot(record).snapshot_key).value == snapshot(record)
        assert reopened.local_readiness().status is KnowledgeLocalReadiness.READY


def test_missing_reads_are_typed_not_found(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        assert store.get_build_lineage(f"kbld_{'1' * 64}").status is KnowledgeReadStatus.NOT_FOUND
        assert store.read_activation().status is KnowledgeReadStatus.NOT_FOUND
        assert store.local_readiness().status is KnowledgeLocalReadiness.NOT_INITIALIZED


def test_closed_store_fails_closed(tmp_path) -> None:
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    store.close()
    with pytest.raises(KnowledgeStoreClosedError):
        store.get_build_lineage(build().build_identity)


def test_store_uses_only_its_explicit_database_path(tmp_path) -> None:
    first = SqliteKnowledgeStore(tmp_path / "candidate-c.sqlite3")
    second = SqliteKnowledgeStore(tmp_path / "other-domain.sqlite3")
    try:
        first.create_build_lineage(build())
        assert second.get_build_lineage(build().build_identity).status is KnowledgeReadStatus.NOT_FOUND
    finally:
        first.close()
        second.close()


def test_snapshot_completion_rolls_back_when_lineage_is_wrong(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        record = build()
        store.create_build_lineage(record)
        pending = operation(record)
        store.create_operation(pending)
        wrong = type(snapshot(record))(
            snapshot(record).snapshot_key,
            snapshot(record).operation_key,
            record.build_identity,
            snapshot(record).snapshot_commitment,
            "e" * 64,
        )
        with pytest.raises(Exception):
            store.complete_operation_with_snapshot(wrong, expected_revision=1)
        assert store.get_operation(pending.operation_key).value == pending
        assert store.get_snapshot(wrong.snapshot_key).status is KnowledgeReadStatus.NOT_FOUND
