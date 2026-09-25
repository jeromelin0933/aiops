import sqlite3

import pytest

from knowledge_index import (
    KnowledgeStoreIntegrityError,
    RetentionSubjectKind,
    SqliteKnowledgeStore,
)
from _knowledge_build_testkit import limits
from _knowledge_retrieval_testkit import request
from _knowledge_snapshot_testkit import environment


def test_snapshot_and_outstanding_operation_hold_build_retention_floor(tmp_path) -> None:
    store, staged, _, _, service = environment(tmp_path)
    store.freeze_retrieval_operation(request())
    assert not store.cleanup_eligible(RetentionSubjectKind.BUILD, staged.build_identity)
    snapshot = service.resolve(request(), limits()).snapshot
    assert snapshot is not None
    assert not store.cleanup_eligible(RetentionSubjectKind.SNAPSHOT, snapshot.snapshot_key.value)
    assert not store.cleanup_eligible(RetentionSubjectKind.CONTENT, snapshot.chunks[0].content_commitment)
    store.close()


def test_snapshot_payload_tampering_fails_strict_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    store, _, _, _, service = environment(tmp_path)
    service.resolve(request(), limits())
    store.close()
    connection = sqlite3.connect(path)
    connection.execute("UPDATE snapshot_envelopes SET snapshot_commitment = ?", ("f" * 64,))
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


def test_legitimate_snapshot_survives_strict_reopen(tmp_path) -> None:
    store, _, _, _, service = environment(tmp_path)
    snapshot = service.resolve(request(), limits()).snapshot
    store.close()
    reopened = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    assert reopened.get_snapshot(snapshot.snapshot_key).value == snapshot
    reopened.close()
