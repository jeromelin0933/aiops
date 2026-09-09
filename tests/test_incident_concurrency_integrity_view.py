from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import json
from threading import Barrier
import sqlite3

import pytest

from src.alert_correlation import (
    AnchorStrength,
    AnchorTransition,
    CorrelationDecision,
    CorrelationFamily,
    DecisionReasonCode,
    DecisionType,
    IncidentCorrelationView,
    NormalizedFingerprint,
)
from src.alert_correlation.state import CorrelationMutationIntent, RetryDisposition
from src.incident_management import (
    IncidentDomainError,
    IncidentErrorCode,
    IncidentManager,
    IncidentMutationRequest,
    IncidentSeverity,
    SqliteIncidentStore,
)
import src.incident_management.sqlite_store as sqlite_store_module


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _fingerprint(source_ip="192.0.2.10", event_type="brute_force_detected"):
    return NormalizedFingerprint.from_mapping(event_type, {"source_ip": source_ip})


def _event(
    event_id,
    *,
    detected_at="2026-09-09T11:59:00Z",
    severity="LOW",
    event_type="brute_force_detected",
):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "detected_at": detected_at,
        "severity": severity,
    }


def _request(decision, event, operation_id, *, now=NOW):
    intent = CorrelationMutationIntent.from_decision(
        operation_id=operation_id,
        event_id=event["event_id"],
        decision=decision,
        created_at=NOW - timedelta(minutes=5),
    )
    return IncidentMutationRequest(intent, event, now)


def _create_request(
    event_id="EVT-1",
    operation_id="OP-CREATE",
    *,
    strength=AnchorStrength.STRONG,
    severity="LOW",
    family=CorrelationFamily.ATTACK_SOURCE,
    detected_at="2026-09-09T11:59:00Z",
):
    decision = CorrelationDecision(
        decision_type=DecisionType.CREATE_NEW,
        policy_id="POLICY-CREATE",
        policy_version="1.0",
        correlation_family=family,
        reason_code=(
            DecisionReasonCode.NO_COMPATIBLE_CANDIDATE
            if strength is AnchorStrength.STRONG
            else DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED
        ),
        normalized_fingerprint=_fingerprint() if strength is AnchorStrength.STRONG else None,
        anchor_strength=strength,
    )
    return _request(
        decision,
        _event(event_id, detected_at=detected_at, severity=severity),
        operation_id,
    )


def _attach_request(
    event_id,
    operation_id,
    *,
    severity,
    detected_at="2026-09-09T12:01:00Z",
    now=NOW + timedelta(minutes=2),
):
    decision = CorrelationDecision(
        decision_type=DecisionType.ATTACH_EXISTING,
        policy_id="POLICY-ATTACH",
        policy_version="1.0",
        correlation_family=CorrelationFamily.ATTACK_SOURCE,
        reason_code=DecisionReasonCode.EXACT_STRONG_IDENTITY_MATCH,
        target_incident_id="INC-1",
        normalized_fingerprint=_fingerprint(),
        anchor_strength=AnchorStrength.STRONG,
    )
    return _request(
        decision,
        _event(event_id, detected_at=detected_at, severity=severity),
        operation_id,
        now=now,
    )


def _promotion_request(event_id, operation_id, source_ip):
    fingerprint = _fingerprint(source_ip)
    decision = CorrelationDecision(
        decision_type=DecisionType.ATTACH_EXISTING,
        policy_id="POLICY-STRONG",
        policy_version="2.0",
        correlation_family=CorrelationFamily.ATTACK_SOURCE,
        reason_code=DecisionReasonCode.WEAK_TO_STRONG_PROMOTION,
        target_incident_id="INC-1",
        normalized_fingerprint=fingerprint,
        anchor_strength=AnchorStrength.STRONG,
        anchor_transition=AnchorTransition.WEAK_TO_STRONG,
    )
    return _request(
        decision,
        _event(event_id, detected_at="2026-09-09T12:01:00Z", severity="HIGH"),
        operation_id,
        now=NOW + timedelta(minutes=2),
    )


def _invoke_at_barrier(barrier, manager, request):
    barrier.wait()
    try:
        return manager.apply_correlation_mutation(request)
    except IncidentDomainError as exc:
        return exc


def _assert_error(code, callable_, *args):
    with pytest.raises(IncidentDomainError) as raised:
        callable_(*args)
    assert raised.value.code is code
    return raised.value


def test_same_event_different_operation_race_has_one_owner_and_one_loser(tmp_path):
    path = tmp_path / "same-event.db"
    SqliteIncidentStore(str(path)).close()
    stores = [SqliteIncidentStore(str(path)), SqliteIncidentStore(str(path))]
    managers = [
        IncidentManager(stores[0], incident_id_factory=lambda: "INC-A"),
        IncidentManager(stores[1], incident_id_factory=lambda: "INC-B"),
    ]
    requests = [
        _create_request(operation_id="OP-A"),
        _create_request(operation_id="OP-B"),
    ]
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda pair: _invoke_at_barrier(barrier, pair[0], pair[1]),
                zip(managers, requests),
            )
        )

    successes = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, IncidentDomainError)]
    assert len(successes) == len(failures) == 1
    assert failures[0].code is IncidentErrorCode.EVENT_OWNERSHIP_CONFLICT
    assert failures[0].retry_disposition is RetryDisposition.REPAIR_REQUIRED

    with SqliteIncidentStore(str(path)) as reader:
        views = reader.list_correlation_views()
        assert len(views) == 1
        assert views[0].incident_id == successes[0].incident_id
        assert reader.get_incident(views[0].incident_id).event_ids == ("EVT-1",)
        reader.validate_integrity()
    for store in stores:
        store.close()


def test_same_operation_race_returns_one_stable_authoritative_result(tmp_path):
    path = tmp_path / "same-operation.db"
    SqliteIncidentStore(str(path)).close()
    stores = [SqliteIncidentStore(str(path)), SqliteIncidentStore(str(path))]
    managers = [
        IncidentManager(stores[0], incident_id_factory=lambda: "INC-A"),
        IncidentManager(stores[1], incident_id_factory=lambda: "INC-B"),
    ]
    request = _create_request()
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda manager: _invoke_at_barrier(barrier, manager, request),
                managers,
            )
        )

    assert outcomes[0] == outcomes[1]
    with SqliteIncidentStore(str(path)) as reader:
        incident = reader.get_incident(outcomes[0].incident_id)
        assert incident.event_ids == ("EVT-1",)
        assert len(incident.audit_trail) == 1
        assert reader.get_operation_result("OP-CREATE") == outcomes[0]
    for store in stores:
        store.close()


def test_different_events_same_incident_race_has_no_lost_update(tmp_path):
    path = tmp_path / "same-incident.db"
    with SqliteIncidentStore(str(path)) as setup_store:
        IncidentManager(
            setup_store, incident_id_factory=lambda: "INC-1"
        ).apply_correlation_mutation(_create_request())
        original_context = setup_store.get_incident("INC-1").correlation_context

    stores = [SqliteIncidentStore(str(path)), SqliteIncidentStore(str(path))]
    managers = [IncidentManager(store) for store in stores]
    requests = [
        _attach_request("EVT-A", "OP-A", severity="MEDIUM"),
        _attach_request("EVT-B", "OP-B", severity="CRITICAL"),
    ]
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda pair: _invoke_at_barrier(barrier, pair[0], pair[1]),
                zip(managers, requests),
            )
        )

    assert not any(isinstance(outcome, Exception) for outcome in outcomes)
    with SqliteIncidentStore(str(path)) as reader:
        incident = reader.get_incident("INC-1")
        assert incident.event_ids[0] == "EVT-1"
        assert set(incident.event_ids[1:]) == {"EVT-A", "EVT-B"}
        assert incident.severity is IncidentSeverity.CRITICAL
        assert len(incident.audit_trail) == 3
        assert incident.correlation_context == original_context
        assert reader.get_operation_result("OP-A") is not None
        assert reader.get_operation_result("OP-B") is not None
        reader.validate_integrity()
    for store in stores:
        store.close()


def test_concurrent_promotion_commits_at_most_one_consistent_anchor(tmp_path):
    path = tmp_path / "promotion-race.db"
    with SqliteIncidentStore(str(path)) as setup_store:
        IncidentManager(
            setup_store, incident_id_factory=lambda: "INC-1"
        ).apply_correlation_mutation(
            _create_request(strength=AnchorStrength.WEAK)
        )

    stores = [SqliteIncidentStore(str(path)), SqliteIncidentStore(str(path))]
    managers = [IncidentManager(store) for store in stores]
    requests = [
        _promotion_request("EVT-A", "OP-A", "192.0.2.11"),
        _promotion_request("EVT-B", "OP-B", "192.0.2.12"),
    ]
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda pair: _invoke_at_barrier(barrier, pair[0], pair[1]),
                zip(managers, requests),
            )
        )

    successes = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, IncidentDomainError)]
    assert len(successes) == len(failures) == 1
    assert failures[0].code is IncidentErrorCode.INVALID_ANCHOR_PROMOTION

    winning_event = successes[0].event_id
    with SqliteIncidentStore(str(path)) as reader:
        incident = reader.get_incident("INC-1")
        assert incident.event_ids == ("EVT-1", winning_event)
        assert incident.anchor_event_id == winning_event
        assert incident.correlation_context.anchor_event_id == winning_event
        assert incident.correlation_context.anchor_strength is AnchorStrength.STRONG
        assert incident.correlation_context.promoted_from_weak is True
        assert len(incident.audit_trail) == 2
        reader.validate_integrity()
    for store in stores:
        store.close()


def test_each_store_operation_uses_a_distinct_sqlite_connection(tmp_path, monkeypatch):
    real_connect = sqlite_store_module.sqlite3.connect
    opened_connections = []

    def tracked_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite_store_module.sqlite3, "connect", tracked_connect)
    store = SqliteIncidentStore(str(tmp_path / "connections.db"))
    initialization_connection = opened_connections[-1]
    manager = IncidentManager(store, incident_id_factory=lambda: "INC-1")
    manager.apply_correlation_mutation(_create_request())
    store.get_incident("INC-1")
    store.get_operation_result("OP-CREATE")
    store.validate_readiness()
    store.close()

    assert len(opened_connections) == 5
    assert len({id(connection) for connection in opened_connections}) == 5
    assert opened_connections[0] is initialization_connection


def test_sqlite_locked_is_narrowly_retryable_and_leaves_no_partial_state(
    tmp_path, monkeypatch
):
    path = tmp_path / "locked.db"
    store = SqliteIncidentStore(str(path))
    manager = IncidentManager(store, incident_id_factory=lambda: "INC-1")
    monkeypatch.setattr(sqlite_store_module, "_BUSY_TIMEOUT_MS", 0)

    lock_connection = sqlite3.connect(path, isolation_level=None)
    lock_connection.execute("BEGIN IMMEDIATE")
    try:
        error = _assert_error(
            IncidentErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE,
            manager.apply_correlation_mutation,
            _create_request(),
        )
        assert error.retry_disposition is RetryDisposition.RETRYABLE
    finally:
        lock_connection.rollback()
        lock_connection.close()

    assert store.get_incident("INC-1") is None
    assert store.get_operation_result("OP-CREATE") is None
    store.validate_integrity()
    store.close()


def test_per_record_anchor_corruption_is_repair_required_without_hiding_healthy_record(
    tmp_path,
):
    path = tmp_path / "per-record.db"
    with SqliteIncidentStore(str(path)) as store:
        IncidentManager(store, incident_id_factory=lambda: "INC-1").apply_correlation_mutation(
            _create_request()
        )
        IncidentManager(store, incident_id_factory=lambda: "INC-2").apply_correlation_mutation(
            _create_request("EVT-2", "OP-2")
        )

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE incidents SET anchor_event_id = 'EVT-NOT-OWNED' WHERE incident_id = 'INC-1'"
        )

    with SqliteIncidentStore(str(path)) as store:
        error = _assert_error(
            IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
            store.get_incident,
            "INC-1",
        )
        assert error.retry_disposition is RetryDisposition.REPAIR_REQUIRED
        _assert_error(
            IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
            store.incident_exists,
            "INC-1",
        )
        assert store.get_incident("INC-2").incident_id == "INC-2"
        _assert_error(
            IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
            store.list_correlation_views,
        )


def test_receipt_semantic_contradiction_and_ownership_contradiction_fail_closed(tmp_path):
    receipt_path = tmp_path / "receipt-corrupt.db"
    with SqliteIncidentStore(str(receipt_path)) as store:
        IncidentManager(store, incident_id_factory=lambda: "INC-1").apply_correlation_mutation(
            _create_request()
        )
    with sqlite3.connect(receipt_path) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT immutable_mutation_identity FROM incident_operation_receipts"
            ).fetchone()[0]
        )
        payload["event_id"] = "EVT-CONTRADICTORY"
        connection.execute(
            "UPDATE incident_operation_receipts SET immutable_mutation_identity = ?",
            (json.dumps(payload),),
        )
    with SqliteIncidentStore(str(receipt_path)) as store:
        error = _assert_error(
            IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
            store.get_operation_result,
            "OP-CREATE",
        )
        assert error.retry_disposition is RetryDisposition.REPAIR_REQUIRED

    ownership_path = tmp_path / "ownership-corrupt.db"
    with SqliteIncidentStore(str(ownership_path)) as store:
        IncidentManager(store, incident_id_factory=lambda: "INC-1").apply_correlation_mutation(
            _create_request()
        )
    connection = sqlite3.connect(ownership_path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "UPDATE incident_events SET incident_id = 'INC-MISSING' WHERE event_id = 'EVT-1'"
    )
    connection.commit()
    connection.close()
    with SqliteIncidentStore(str(ownership_path)) as store:
        _assert_error(
            IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
            store.validate_readiness,
        )


def test_unsupported_record_version_and_whole_store_corruption_fail_fast(tmp_path):
    version_path = tmp_path / "version.db"
    with SqliteIncidentStore(str(version_path)) as store:
        IncidentManager(store, incident_id_factory=lambda: "INC-1").apply_correlation_mutation(
            _create_request()
        )
    with sqlite3.connect(version_path) as connection:
        connection.execute("UPDATE incidents SET state_version = 999")
    with SqliteIncidentStore(str(version_path)) as store:
        _assert_error(
            IncidentErrorCode.UNSUPPORTED_INCIDENT_STATE_VERSION,
            store.validate_readiness,
        )

    corrupt_path = tmp_path / "unreadable.db"
    SqliteIncidentStore(str(corrupt_path)).close()
    corrupt_path.write_bytes(b"not a sqlite database")
    error = _assert_error(
        IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
        SqliteIncidentStore,
        str(corrupt_path),
    )
    assert error.retry_disposition is RetryDisposition.REPAIR_REQUIRED


def test_minimal_view_is_exactly_seven_fields_and_enumeration_does_no_filtering(tmp_path):
    path = tmp_path / "views.db"
    with SqliteIncidentStore(str(path)) as store:
        IncidentManager(store, incident_id_factory=lambda: "INC-1").apply_correlation_mutation(
            _create_request()
        )
        IncidentManager(store, incident_id_factory=lambda: "INC-2").apply_correlation_mutation(
            _create_request(
                "EVT-2",
                "OP-2",
                family=CorrelationFamily.DOWNSTREAM_CASCADE,
                detected_at="2020-01-01T00:00:00Z",
            )
        )

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE incidents SET status = 'CLOSED', closed_at = updated_at WHERE incident_id = 'INC-2'"
        )

    with SqliteIncidentStore(str(path)) as store:
        views = store.list_correlation_views()
        assert [view.incident_id for view in views] == ["INC-1", "INC-2"]
        assert views[1].status == "CLOSED"
        assert isinstance(views[0], IncidentCorrelationView)
        assert [field.name for field in fields(IncidentCorrelationView)] == [
            "incident_id",
            "status",
            "last_correlated_at",
            "correlation_family",
            "anchor_strength",
            "normalized_fingerprint",
            "anchor_event_type",
        ]
        assert not any(
            fragment in name
            for name in dir(store)
            for fragment in ("candidate", "window", "compatible")
        )
