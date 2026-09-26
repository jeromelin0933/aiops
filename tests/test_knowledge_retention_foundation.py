from dataclasses import replace

import pytest

from knowledge_index import (
    KnowledgeStoreConflictError,
    OpaqueExternalReference,
    OpaqueReferenceType,
    RetentionHoldKey,
    RetentionHoldRecord,
    RetentionHoldStatus,
    RetentionObligationKind,
    RetentionSubjectKind,
    SqliteKnowledgeStore,
)
from _knowledge_store_testkit import build, digest


def _hold(record, key: str = "hold-1") -> RetentionHoldRecord:
    return RetentionHoldRecord(
        RetentionHoldKey(key),
        RetentionSubjectKind.BUILD,
        record.build_identity,
        OpaqueExternalReference(OpaqueReferenceType.RCA_ATTEMPT, "opaque-owner-1"),
        RetentionObligationKind.MATERIAL_FAILED_ATTEMPT,
        digest("e"),
    )


def test_active_hold_blocks_cleanup_and_equivalent_replay(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        record = build()
        store.create_build_lineage(record)
        hold = _hold(record)
        assert store.create_retention_hold(hold) == store.create_retention_hold(hold)
        assert not store.cleanup_eligible(RetentionSubjectKind.BUILD, record.build_identity)


def test_contradictory_hold_replay_is_rejected(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        record = build()
        hold = _hold(record)
        store.create_retention_hold(hold)
        with pytest.raises(KnowledgeStoreConflictError):
            store.create_retention_hold(replace(hold, semantic_commitment=digest("f")))


def test_release_is_idempotent_and_does_not_delete_lineage(tmp_path) -> None:
    with SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3") as store:
        record = build()
        store.create_build_lineage(record)
        store.create_retention_hold(_hold(record))
        released = store.release_retention_hold(RetentionHoldKey("hold-1"), expected_revision=1)
        assert released.status is RetentionHoldStatus.RELEASED
        assert store.release_retention_hold(RetentionHoldKey("hold-1"), expected_revision=1) == released
        assert store.cleanup_eligible(RetentionSubjectKind.BUILD, record.build_identity)
        assert store.get_build_lineage(record.build_identity).value == record


def test_hold_state_survives_exact_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    record = build()
    hold = _hold(record)
    with SqliteKnowledgeStore(path) as store:
        store.create_retention_hold(hold)
    with SqliteKnowledgeStore(path) as reopened:
        assert reopened.get_retention_hold(hold.hold_key).value == hold
        assert not reopened.cleanup_eligible(hold.subject_kind, hold.subject_identity)


def test_foreign_owner_stays_opaque_without_rca_runtime_or_attempt_truth() -> None:
    record = _hold(build())
    assert tuple(record.owner.__dataclass_fields__) == ("reference_type", "value")
    assert not hasattr(record.owner, "attempt")
    assert not hasattr(record.owner, "runtime_work")
    assert not hasattr(record.owner, "published_rca")
