from dataclasses import FrozenInstanceError
from typing import get_type_hints

import pytest

from knowledge_index import (
    ActivationOperationKey,
    KnowledgeLocalReadiness,
    KnowledgePersistence,
    KnowledgeReadResult,
    KnowledgeReadStatus,
    KnowledgeSnapshotKey,
    KnowledgeValidationError,
    RetentionHoldKey,
    SnapshotFinalizationKey,
    RetrievalOperationKey,
    SqliteKnowledgeStore,
)


@pytest.mark.parametrize(
    "enum_type, values",
    [
        (KnowledgeReadStatus, {"FOUND", "NOT_FOUND", "UNAVAILABLE", "INVALID", "REPAIR_REQUIRED"}),
        (KnowledgeLocalReadiness, {"READY", "NOT_INITIALIZED", "UNAVAILABLE", "MISMATCH", "REPAIR_REQUIRED"}),
    ],
)
def test_durable_statuses_are_closed(enum_type, values) -> None:
    assert {item.value for item in enum_type} == values


@pytest.mark.parametrize(
    "key_type", [ActivationOperationKey, RetrievalOperationKey, KnowledgeSnapshotKey, RetentionHoldKey, SnapshotFinalizationKey]
)
def test_durable_keys_are_frozen_bounded_and_type_separated(key_type) -> None:
    key = key_type("opaque-1")
    with pytest.raises(FrozenInstanceError):
        key.value = "changed"
    assert key != next(t for t in (ActivationOperationKey, RetrievalOperationKey, KnowledgeSnapshotKey, RetentionHoldKey, SnapshotFinalizationKey) if t is not key_type)("opaque-1")
    with pytest.raises(KnowledgeValidationError):
        key_type("")


@pytest.mark.parametrize("value", ["api_key", "token", "password", "secret"])
def test_durable_keys_reject_secret_shaped_identity_values(value: str) -> None:
    with pytest.raises(KnowledgeValidationError):
        RetrievalOperationKey(value)


def test_read_result_enforces_status_value_pairing() -> None:
    with pytest.raises(KnowledgeValidationError):
        KnowledgeReadResult(KnowledgeReadStatus.FOUND)
    with pytest.raises(KnowledgeValidationError):
        KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND, object())


def test_sqlite_store_implements_candidate_c_protocol(tmp_path) -> None:
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    try:
        assert isinstance(store, KnowledgePersistence)
        assert "KnowledgeReadResult" in str(get_type_hints(KnowledgePersistence.get_build_lineage)["return"])
        assert callable(store.create_staged_build)
        assert callable(store.create_build_validation)
        assert callable(store.get_staged_build)
        assert callable(store.get_build_validation)
        assert callable(store.get_retrieval_operation_read)
        assert callable(store.record_retrieval_recovery)
    finally:
        store.close()
