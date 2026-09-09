import sqlite3
from datetime import datetime, timezone

import pytest

from alert_correlation.contracts import AnchorTransition, CorrelationFamily, DecisionReasonCode, DecisionType
from alert_correlation.state.contracts import CorrelationMutationIntent, TerminalOutcome
from alert_correlation.state.sqlite_store import SqliteCorrelationStateStore
from shadow_management.contracts import (
    ShadowDomainErrorCode,
    ShadowMutationResult,
    ShadowOperationReceipt,
    ShadowReason,
    ShadowReceiptSemanticIdentity,
    ShadowRecord,
    ShadowReviewStatus,
)
from shadow_management.sqlite_store import ShadowStoreIntegrityError, SqliteShadowStore
from tests._shadow_store_testkit import insert_malformed_shadow, set_schema_version


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


def _record(event_id: str = "EVT-1") -> ShadowRecord:
    return ShadowRecord("SHADOW-1", event_id, NOW, ShadowReason.INSUFFICIENT_OPERATIONAL_IDENTITY, ShadowReviewStatus.UNREVIEWED, "POLICY-GENERAL-LOG-ANOMALY", "1.0")


def _receipt() -> ShadowOperationReceipt:
    intent = CorrelationMutationIntent("OP-1", "EVT-1", TerminalOutcome.SHADOWED, DecisionType.ROUTE_SHADOW, "POLICY-GENERAL-LOG-ANOMALY", "1.0", CorrelationFamily.UNKNOWN, DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY, None, None, None, AnchorTransition.NONE, NOW)
    return ShadowOperationReceipt(ShadowReceiptSemanticIdentity(intent, "EVT-1", "general_log_anomaly"), ShadowMutationResult("OP-1", "SHADOW-1", NOW))


def _seed(store: SqliteShadowStore) -> None:
    with store._write_transaction():
        store._insert_shadow_record(_record())
        store._insert_operation_receipt(_receipt())


def test_schema_initialization_default_and_independent_database(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    default_store = SqliteShadowStore()
    default_store.close()
    assert (tmp_path / "shadow_store.db").exists()
    state_database = tmp_path / "correlation-state.sqlite"
    shadow_database = tmp_path / "shadow-store.sqlite"
    store = SqliteShadowStore(shadow_database)
    store.close()
    assert shadow_database != state_database
    assert not state_database.exists()
    state_store = SqliteCorrelationStateStore(state_database)
    state_store.close()
    with pytest.raises(ShadowStoreIntegrityError) as error:
        SqliteShadowStore(state_database)
    assert error.value.code is ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE


def test_close_reopen_durability_and_base_reads(tmp_path) -> None:
    database = tmp_path / "shadow.sqlite"
    store = SqliteShadowStore(database)
    _seed(store)
    store.close()
    with SqliteShadowStore(database) as reopened:
        assert reopened.get_shadow("SHADOW-1") == _record()
        assert reopened.get_shadow_by_event_id("EVT-1") == _record()
        assert reopened.shadow_exists("SHADOW-1") is True
        assert reopened.get_operation_result("OP-1") == _receipt().result
        assert reopened.get_shadow("MISSING") is None
        assert reopened.get_shadow_by_event_id("MISSING") is None
        assert reopened.shadow_exists("MISSING") is False
        assert reopened.get_operation_result("MISSING") is None
        reopened.readiness_check()


def test_schema_version_fails_closed_on_reopen(tmp_path) -> None:
    database = tmp_path / "shadow.sqlite"
    SqliteShadowStore(database).close()
    set_schema_version(database, "999")
    with pytest.raises(ShadowStoreIntegrityError) as error:
        SqliteShadowStore(database)
    assert error.value.code is ShadowDomainErrorCode.UNSUPPORTED_SHADOW_STATE_VERSION


def test_existing_shadow_table_without_metadata_is_not_silently_adopted(tmp_path) -> None:
    database = tmp_path / "shadow.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE shadow_records (shadow_id TEXT PRIMARY KEY)")
    with pytest.raises(ShadowStoreIntegrityError) as error:
        SqliteShadowStore(database)
    assert error.value.code is ShadowDomainErrorCode.UNSUPPORTED_SHADOW_STATE_VERSION


def test_malformed_matching_record_never_becomes_not_found_or_silent_skip(tmp_path) -> None:
    database = tmp_path / "shadow.sqlite"
    SqliteShadowStore(database).close()
    insert_malformed_shadow(database, "SHADOW-BAD", "EVT-BAD")
    with SqliteShadowStore(database) as store:
        with pytest.raises(ShadowStoreIntegrityError) as error:
            store.get_shadow("SHADOW-BAD")
        assert error.value.code is ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD
        with pytest.raises(ShadowStoreIntegrityError):
            store.readiness_check()


def test_unique_event_and_operation_constraints_are_local_authority(tmp_path) -> None:
    store = SqliteShadowStore(tmp_path / "shadow.sqlite")
    _seed(store)
    with pytest.raises(sqlite3.IntegrityError):
        with store._write_transaction():
            store._insert_shadow_record(ShadowRecord("SHADOW-2", "EVT-1", NOW, ShadowReason.INSUFFICIENT_OPERATIONAL_IDENTITY, ShadowReviewStatus.UNREVIEWED, "POLICY", "1"))
    with pytest.raises(sqlite3.IntegrityError):
        with store._write_transaction():
            store._insert_operation_receipt(_receipt())
    store.close()


def test_private_transaction_scaffold_rolls_back_without_public_write_bypass(tmp_path) -> None:
    store = SqliteShadowStore(tmp_path / "shadow.sqlite")
    with pytest.raises(RuntimeError):
        with store._write_transaction():
            store._insert_shadow_record(_record())
            raise RuntimeError("force rollback")
    assert store.get_shadow("SHADOW-1") is None
    assert not hasattr(store, "put_shadow")
    assert not hasattr(store, "put_receipt")
    assert not hasattr(store, "reset")
    assert not hasattr(store, "delete")
    store.close()
