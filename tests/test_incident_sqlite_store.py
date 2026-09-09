from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import sqlite3

import pytest

from src.alert_correlation import (
    AnchorStrength,
    CorrelationDecision,
    CorrelationFamily,
    DecisionReasonCode,
    DecisionType,
    NormalizedFingerprint,
)
from src.alert_correlation.state import CorrelationMutationIntent, RetryDisposition
from src.alert_correlation.state.sqlite_store import SqliteCorrelationStateStore
from src.incident_management import (
    DEFAULT_DATABASE_PATH,
    RCA_INITIAL_STATUS,
    SCHEMA_VERSION,
    IncidentAuditAction,
    IncidentAuditEffect,
    IncidentAuditEntry,
    IncidentCorrelationContext,
    IncidentDomainError,
    IncidentErrorCode,
    IncidentMutationCompletion,
    IncidentMutationRequest,
    IncidentOperationReceipt,
    IncidentOperationResult,
    IncidentRecord,
    IncidentSeverity,
    IncidentStatus,
    OperationReceiptSemanticIdentity,
    SqliteIncidentStore,
)

from _incident_store_testkit import (
    execute_controlled_sql,
    seed_complete_incident,
    seed_incomplete_incident_state,
    seed_then_crash,
)


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
FINGERPRINT = NormalizedFingerprint.from_mapping(
    "brute_force_detected", {"source_ip": "192.0.2.10"}
)
EXPECTED_TABLES = {
    "incident_store_metadata",
    "incidents",
    "incident_events",
    "incident_operation_receipts",
    "incident_audit",
}
SPEC_007_TABLES = {
    "correlation_state_processed",
    "correlation_state_intents",
    "correlation_state_pending",
    "correlation_state_blocked",
    "correlation_state_claims",
    "correlation_state_claim_generations",
}


def _event(event_id="EVT-1", severity="CRITICAL"):
    return {
        "event_id": event_id,
        "event_type": "brute_force_detected",
        "detected_at": "2026-09-09T11:59:00Z",
        "severity": severity,
    }


def _intent(event_id="EVT-1", operation_id="OP-1"):
    decision = CorrelationDecision(
        decision_type=DecisionType.CREATE_NEW,
        policy_id="POLICY-BRUTE-FORCE",
        policy_version="1.0",
        correlation_family=CorrelationFamily.ATTACK_SOURCE,
        reason_code=DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
        normalized_fingerprint=FINGERPRINT,
        anchor_strength=AnchorStrength.STRONG,
    )
    return CorrelationMutationIntent.from_decision(
        operation_id=operation_id,
        event_id=event_id,
        decision=decision,
        created_at=NOW - timedelta(minutes=2),
    )


def _audit(event_id="EVT-1", operation_id="OP-1", incident_id="INC-1"):
    return IncidentAuditEntry(
        operation_id=operation_id,
        event_id=event_id,
        incident_id=incident_id,
        policy_id="POLICY-BRUTE-FORCE",
        policy_version="1.0",
        reason_code=DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
        action=IncidentAuditAction.INCIDENT_CREATED,
        effects=(IncidentAuditEffect.EVENT_ATTACHED,),
        occurred_at=NOW,
    )


def _record(event_id="EVT-1", operation_id="OP-1", incident_id="INC-1"):
    return IncidentRecord(
        incident_id=incident_id,
        event_ids=(event_id,),
        anchor_event_id=event_id,
        status=IncidentStatus.OPEN,
        severity=IncidentSeverity.CRITICAL,
        created_at=NOW,
        updated_at=NOW,
        last_correlated_at=NOW - timedelta(minutes=1),
        closed_at=None,
        assignee=None,
        reviewer=None,
        correlation_context=IncidentCorrelationContext(
            correlation_family=CorrelationFamily.ATTACK_SOURCE,
            anchor_strength=AnchorStrength.STRONG,
            anchor_event_id=event_id,
            anchor_event_type="brute_force_detected",
            normalized_fingerprint=FINGERPRINT,
            anchor_policy_id="POLICY-BRUTE-FORCE",
            anchor_policy_version="1.0",
        ),
        audit_trail=(_audit(event_id, operation_id, incident_id),),
        rca_status=RCA_INITIAL_STATUS,
        rca_ref=None,
        external_refs=(),
    )


def _receipt(event_id="EVT-1", operation_id="OP-1", incident_id="INC-1"):
    request = IncidentMutationRequest(
        _intent(event_id, operation_id), _event(event_id), NOW
    )
    return IncidentOperationReceipt(
        result=IncidentOperationResult(
            operation_id=operation_id,
            event_id=event_id,
            mutation_kind=DecisionType.CREATE_NEW,
            incident_id=incident_id,
            completion_result=IncidentMutationCompletion.SUCCEEDED,
            completed_at=NOW,
        ),
        immutable_mutation_identity=OperationReceiptSemanticIdentity.from_request(request),
    )


def _seed(store, event_id="EVT-1", operation_id="OP-1", incident_id="INC-1"):
    record = _record(event_id, operation_id, incident_id)
    receipt = _receipt(event_id, operation_id, incident_id)
    seed_complete_incident(store, record, (receipt,))
    return record, receipt


def _assert_error(code, callable_, *args, **kwargs):
    with pytest.raises(IncidentDomainError) as raised:
        callable_(*args, **kwargs)
    assert raised.value.code is code
    return raised.value


def test_schema_initialization_has_exact_authority_tables_and_metadata(tmp_path):
    path = tmp_path / "incident.db"
    with SqliteIncidentStore(str(path)) as store:
        assert store.database_path == str(path)
        store.validate_readiness()

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        metadata = connection.execute(
            "SELECT singleton, schema_version FROM incident_store_metadata"
        ).fetchall()
        foreign_keys = connection.execute("PRAGMA foreign_key_list(incident_audit)").fetchall()

    assert tables == EXPECTED_TABLES
    assert metadata == [(1, SCHEMA_VERSION)]
    assert foreign_keys
    assert DEFAULT_DATABASE_PATH == "incident_store.db"


def test_unsupported_schema_version_fails_closed(tmp_path):
    path = tmp_path / "unsupported.db"
    SqliteIncidentStore(str(path)).close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE incident_store_metadata SET schema_version = ?", (SCHEMA_VERSION + 1,)
        )

    _assert_error(
        IncidentErrorCode.UNSUPPORTED_INCIDENT_STATE_VERSION,
        SqliteIncidentStore,
        str(path),
    )


def test_incomplete_schema_fails_closed_instead_of_reinitializing(tmp_path):
    path = tmp_path / "incomplete.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE incident_store_metadata(singleton INTEGER, schema_version INTEGER)"
        )

    _assert_error(
        IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
        SqliteIncidentStore,
        str(path),
    )


def test_close_reopen_preserves_incident_receipt_audit_and_order(tmp_path):
    path = tmp_path / "durable.db"
    store = SqliteIncidentStore(str(path))
    record, receipt = _seed(store)
    store.close()

    with SqliteIncidentStore(str(path)) as reopened:
        assert reopened.get_incident("INC-1") == record
        assert reopened.get_operation_result("OP-1") == receipt.result
        assert reopened.incident_exists("INC-1") is True
        reopened.validate_integrity()


def test_event_id_has_global_incident_ownership_constraint(tmp_path):
    with SqliteIncidentStore(str(tmp_path / "ownership.db")) as store:
        _seed(store)
        error = _assert_error(
            IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
            _seed,
            store,
            "EVT-1",
            "OP-2",
            "INC-2",
        )
        assert "UNIQUE" in str(error)
        assert store.incident_exists("INC-2") is False


def test_operation_id_receipt_is_unique_and_transaction_rolls_back(tmp_path):
    with SqliteIncidentStore(str(tmp_path / "receipt.db")) as store:
        _seed(store)
        _assert_error(
            IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
            _seed,
            store,
            "EVT-2",
            "OP-1",
            "INC-2",
        )
        assert store.incident_exists("INC-2") is False
        assert store.get_operation_result("OP-1").event_id == "EVT-1"


def test_ordered_event_ordinals_are_unique_and_contiguous(tmp_path):
    with SqliteIncidentStore(str(tmp_path / "order.db")) as store:
        record, receipt = _seed(store)
        assert store.get_incident("INC-1").event_ids == ("EVT-1",)

        second = _record("EVT-2", "OP-2", "INC-2")
        second_receipt = _receipt("EVT-2", "OP-2", "INC-2")
        with store._transaction() as transaction:
            transaction._insert_incident_state(second)
            transaction._insert_event_reference("INC-2", "EVT-2", 1)
            transaction._insert_operation_receipt(second_receipt)
            transaction._insert_audit_entry(second.audit_trail[0])

        _assert_error(
            IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
            store.get_incident,
            "INC-2",
        )

        with pytest.raises(IncidentDomainError):
            with store._transaction() as transaction:
                transaction._insert_event_reference("INC-1", "EVT-3", 0)


def test_public_read_apis_and_minimal_view(tmp_path):
    with SqliteIncidentStore(str(tmp_path / "reads.db")) as store:
        record, receipt = _seed(store)
        assert store.get_incident("missing") is None
        assert store.incident_exists("missing") is False
        assert store.event_has_incident_owner("EVT-MISSING") is False
        assert store.get_operation_result("missing") is None
        assert store.get_correlation_view("missing") is None

        assert store.get_incident("INC-1") == record
        assert store.event_has_incident_owner("EVT-1") is True
        assert store.get_operation_result("OP-1") == receipt.result
        view = store.get_correlation_view("INC-1")
        assert view.incident_id == "INC-1"
        assert view.status == "OPEN"
        assert view.anchor_strength is AnchorStrength.STRONG
        assert view.normalized_fingerprint == FINGERPRINT
        assert store.list_correlation_views() == (view,)
        assert set(vars(type(view))) >= {
            "incident_id",
            "status",
            "last_correlated_at",
            "correlation_family",
            "anchor_strength",
            "normalized_fingerprint",
            "anchor_event_type",
        }


def test_event_ownership_read_is_restart_safe_for_owner_and_clean_absence(tmp_path):
    path = tmp_path / "ownership-read.db"
    with SqliteIncidentStore(str(path)) as store:
        _seed(store)
        assert store.event_has_incident_owner("EVT-1") is True
        assert store.event_has_incident_owner("EVT-MISSING") is False

    with SqliteIncidentStore(str(path)) as reopened:
        assert reopened.event_has_incident_owner("EVT-1") is True
        assert reopened.event_has_incident_owner("EVT-MISSING") is False


def test_event_ownership_read_has_no_write_or_duplicate_authority_side_effect(tmp_path):
    path = tmp_path / "ownership-read-only.db"
    with SqliteIncidentStore(str(path)) as store:
        record, receipt = _seed(store)
        before_incident = store.get_incident("INC-1")
        before_result = store.get_operation_result("OP-1")
        with sqlite3.connect(path) as connection:
            before_tables = tuple(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            )
            before_counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in sorted(EXPECTED_TABLES)
            )

        assert store.event_has_incident_owner("EVT-1") is True
        assert store.event_has_incident_owner("EVT-MISSING") is False

        assert store.get_incident("INC-1") == before_incident == record
        assert store.get_operation_result("OP-1") == before_result == receipt.result
        with sqlite3.connect(path) as connection:
            after_tables = tuple(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            )
            after_counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in sorted(EXPECTED_TABLES)
            )
        assert after_tables == before_tables
        assert after_counts == before_counts


def test_event_ownership_read_rejects_malformed_owner_instead_of_returning_false(tmp_path):
    with SqliteIncidentStore(str(tmp_path / "malformed-owner.db")) as store:
        _seed(store)
        execute_controlled_sql(
            store,
            "UPDATE incidents SET correlation_context = ? WHERE incident_id = ?",
            ("{not-json", "INC-1"),
        )

        error = _assert_error(
            IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
            store.event_has_incident_owner,
            "EVT-1",
        )
        assert error.retry_disposition is RetryDisposition.REPAIR_REQUIRED


def test_event_ownership_read_rejects_dangling_or_incomplete_authority(tmp_path):
    dangling_path = tmp_path / "dangling-owner.db"
    with SqliteIncidentStore(str(dangling_path)) as store:
        _seed(store)
    connection = sqlite3.connect(dangling_path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "UPDATE incident_events SET incident_id = 'INC-MISSING' WHERE event_id = 'EVT-1'"
    )
    connection.commit()
    connection.close()

    with SqliteIncidentStore(str(dangling_path)) as store:
        _assert_error(
            IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
            store.event_has_incident_owner,
            "EVT-1",
        )

    incomplete_path = tmp_path / "incomplete-owner.db"
    with SqliteIncidentStore(str(incomplete_path)) as store:
        _seed(store)
    connection = sqlite3.connect(incomplete_path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DELETE FROM incident_operation_receipts WHERE event_id = 'EVT-1'")
    connection.commit()
    connection.close()

    with SqliteIncidentStore(str(incomplete_path)) as store:
        _assert_error(
            IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
            store.event_has_incident_owner,
            "EVT-1",
        )

    erased_path = tmp_path / "erased-owner-evidence.db"
    with SqliteIncidentStore(str(erased_path)) as store:
        _seed(store)
    connection = sqlite3.connect(erased_path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DELETE FROM incident_audit WHERE event_id = 'EVT-1'")
    connection.execute("DELETE FROM incident_operation_receipts WHERE event_id = 'EVT-1'")
    connection.execute("DELETE FROM incident_events WHERE event_id = 'EVT-1'")
    connection.commit()
    connection.close()

    with SqliteIncidentStore(str(erased_path)) as store:
        _assert_error(
            IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
            store.event_has_incident_owner,
            "EVT-1",
        )


def test_event_ownership_clean_absence_rejects_unsupported_store_version(tmp_path):
    path = tmp_path / "ownership-version.db"
    store = SqliteIncidentStore(str(path))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE incident_store_metadata SET schema_version = ?",
            (SCHEMA_VERSION + 1,),
        )
    try:
        _assert_error(
            IncidentErrorCode.UNSUPPORTED_INCIDENT_STATE_VERSION,
            store.event_has_incident_owner,
            "EVT-MISSING",
        )
    finally:
        store.close()


def test_malformed_matching_incident_fails_closed_for_reads_and_exists(tmp_path):
    with SqliteIncidentStore(str(tmp_path / "malformed.db")) as store:
        _seed(store)
        execute_controlled_sql(
            store,
            "UPDATE incidents SET correlation_context = ? WHERE incident_id = ?",
            ("{not-json", "INC-1"),
        )

        for read in (
            lambda: store.get_incident("INC-1"),
            lambda: store.incident_exists("INC-1"),
            lambda: store.get_correlation_view("INC-1"),
            store.list_correlation_views,
        ):
            _assert_error(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, read)


def test_malformed_receipt_and_state_version_fail_closed(tmp_path):
    with SqliteIncidentStore(str(tmp_path / "malformed-receipt.db")) as store:
        _seed(store)
        execute_controlled_sql(
            store,
            "UPDATE incident_operation_receipts SET immutable_mutation_identity = ? WHERE operation_id = ?",
            ("{}", "OP-1"),
        )
        _assert_error(
            IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
            store.get_operation_result,
            "OP-1",
        )

    with SqliteIncidentStore(str(tmp_path / "state-version.db")) as store:
        _seed(store)
        execute_controlled_sql(
            store,
            "UPDATE incidents SET state_version = 999 WHERE incident_id = 'INC-1'",
        )
        _assert_error(
            IncidentErrorCode.UNSUPPORTED_INCIDENT_STATE_VERSION,
            store.get_incident,
            "INC-1",
        )


def test_readiness_detects_incomplete_local_authority(tmp_path):
    with SqliteIncidentStore(str(tmp_path / "integrity.db")) as store:
        seed_incomplete_incident_state(store, _record())
        _assert_error(
            IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
            store.validate_integrity,
        )


def test_spec_008_refuses_spec_007_physical_database(tmp_path):
    shared_path = tmp_path / "correlation-state.db"
    SqliteCorrelationStateStore(str(shared_path)).close()

    _assert_error(
        IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
        SqliteIncidentStore,
        str(shared_path),
    )
    with sqlite3.connect(shared_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert not tables.intersection(EXPECTED_TABLES)


def test_readiness_rejects_spec_007_tables_added_after_spec_008_initialization(tmp_path):
    shared_path = tmp_path / "reverse-order.db"
    incident_store = SqliteIncidentStore(str(shared_path))
    incident_store.validate_readiness()

    state_store = SqliteCorrelationStateStore(str(shared_path))
    state_store.close()

    error = _assert_error(
        IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
        incident_store.validate_readiness,
    )
    assert "must not share" in str(error)
    incident_store.close()

    with sqlite3.connect(shared_path) as connection:
        tables_after_failure = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert EXPECTED_TABLES <= tables_after_failure
    assert SPEC_007_TABLES <= tables_after_failure


def test_reopen_rejects_reverse_order_shared_database_without_cleanup(tmp_path):
    shared_path = tmp_path / "reverse-order-reopen.db"
    SqliteIncidentStore(str(shared_path)).close()
    SqliteCorrelationStateStore(str(shared_path)).close()

    _assert_error(
        IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
        SqliteIncidentStore,
        str(shared_path),
    )

    with sqlite3.connect(shared_path) as connection:
        tables_after_reopen_failure = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert EXPECTED_TABLES <= tables_after_reopen_failure
    assert SPEC_007_TABLES <= tables_after_reopen_failure


def test_controlled_precommit_crash_rolls_back_everything(tmp_path):
    with SqliteIncidentStore(str(tmp_path / "rollback.db")) as store:
        with pytest.raises(RuntimeError, match="controlled pre-commit crash"):
            seed_then_crash(store, _record())
        assert store.get_incident("INC-1") is None
        assert store.get_operation_result("OP-1") is None
        store.validate_integrity()


def test_no_public_authoritative_write_or_destructive_cleanup_surface():
    public_methods = {
        name
        for name, value in inspect.getmembers(SqliteIncidentStore, inspect.isfunction)
        if not name.startswith("_")
    }
    assert public_methods == {
        "close",
        "get_correlation_view",
        "get_incident",
        "get_operation_result",
        "event_has_incident_owner",
        "incident_exists",
        "list_correlation_views",
        "validate_integrity",
        "validate_readiness",
    }
    forbidden_fragments = {
        "put",
        "insert",
        "append",
        "update",
        "delete",
        "reset",
        "cleanup",
        "ttl",
        "expire",
    }
    assert not any(
        fragment in method
        for method in public_methods
        for fragment in forbidden_fragments
    )
