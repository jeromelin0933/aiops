import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from _state_store_testkit import seed_block, seed_pending

from src.alert_correlation import (
    AnchorStrength, AnchorTransition, CorrelationFamily, DecisionReasonCode,
    DecisionType, EvaluationPhase, NormalizedFingerprint, CorrelationErrorCode,
)
from src.alert_correlation.state import (
    ActivePendingRecord, BlockedCorrelationRecord, CorrelationMutationIntent,
    CorrelationPolicyKind, FailureKind, PendingReason, ProcessedCorrelationRecord,
    RecoveryAction, RetryDisposition, SqliteCorrelationStateStore,
    StateDomainErrorCode, StateDomainValidationError, StateRecoveryService,
    StateStoreStartupError, TerminalOutcome,
)


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
FINGERPRINT = NormalizedFingerprint("brute_force_detected", (("source_ip", "1.2.3.4"),))


def _pending(event_id):
    return ActivePendingRecord(event_id, NOW, NOW + timedelta(seconds=30), CorrelationPolicyKind.STRONG_ANCHOR, "POLICY-BRUTE-FORCE-DETECTED", "1.0", PendingReason.NO_COMPATIBLE_CANDIDATE)


def _block(event_id):
    return BlockedCorrelationRecord(event_id, FailureKind.CORRELATION_DOMAIN_FAILURE, CorrelationErrorCode.INVALID_INCIDENT_VIEW, EvaluationPhase.PENDING_RECHECK, NOW, NOW, 1, RetryDisposition.REPAIR_REQUIRED, "POLICY-BRUTE-FORCE-DETECTED", "1.0")


def _intent(event_id, operation_id="OP-1"):
    return CorrelationMutationIntent(operation_id, event_id, TerminalOutcome.CREATED_INCIDENT, DecisionType.CREATE_NEW, "POLICY-BRUTE-FORCE-DETECTED", "1.0", CorrelationFamily.ATTACK_SOURCE, DecisionReasonCode.NO_COMPATIBLE_CANDIDATE, None, FINGERPRINT, AnchorStrength.STRONG, AnchorTransition.NONE, NOW)


def _processed(event_id, *, incident_id="INC-1", resolved_at=NOW):
    return ProcessedCorrelationRecord(event_id, TerminalOutcome.CREATED_INCIDENT, resolved_at, incident_id, None, "POLICY-BRUTE-FORCE-DETECTED", "1.0")


def _finalize(store, record):
    claim = store.acquire_claim(record.event_id)
    assert claim is not None
    store.begin_intent(_intent(record.event_id, f"OP-{record.event_id}"), claim)
    store.finalize_processed(record, claim)


class ReferencePortDouble:
    def __init__(self, *, events=(), incidents=(), shadows=()):
        self.events = set(events)
        self.incidents = set(incidents)
        self.shadows = set(shadows)

    def event_exists(self, event_id):
        return event_id in self.events

    def incident_exists(self, incident_id):
        return incident_id in self.incidents

    def shadow_exists(self, shadow_ref):
        return shadow_ref in self.shadows


def test_restart_reconstructs_pending_pending_blocked_unresolved_intent_and_processed(tmp_path):
    database = tmp_path / "state.sqlite"
    store = SqliteCorrelationStateStore(database)
    seed_pending(store, _pending("EVT-PENDING"))
    seed_pending(store, _pending("EVT-PENDING-BLOCKED"))
    seed_block(store, _block("EVT-PENDING-BLOCKED"))
    claim = store.acquire_claim("EVT-INTENT")
    assert claim is not None
    store.begin_intent(_intent("EVT-INTENT"), claim)
    _finalize(store, _processed("EVT-PROCESSED"))
    store.close()

    reopened = SqliteCorrelationStateStore(database)
    result = StateRecoveryService(reopened).reconstruct()
    actions = {state.entry.event_id: state.action for state in result.states}
    assert actions == {
        "EVT-INTENT": RecoveryAction.RECONCILE_INTENT,
        "EVT-PENDING": RecoveryAction.PRESERVE_PENDING,
        "EVT-PENDING-BLOCKED": RecoveryAction.PRESERVE_PENDING_BLOCKED,
        "EVT-PROCESSED": RecoveryAction.TERMINAL_NOOP,
    }
    assert result.findings == ()
    assert StateRecoveryService(reopened).reconstruct() == result


def test_matching_stale_intent_is_deterministically_resolved_and_conflict_is_repair_required(tmp_path):
    database = tmp_path / "state.sqlite"
    store = SqliteCorrelationStateStore(database)
    _finalize(store, _processed("EVT-MATCH"))
    store.close()
    intent = _intent("EVT-MATCH", "OP-STALE")
    payload = _intent_payload(intent)
    connection = sqlite3.connect(database)
    connection.execute("INSERT INTO correlation_state_intents(event_id, operation_id, payload) VALUES (?, ?, ?)", (intent.event_id, intent.operation_id, json.dumps(payload)))
    connection.commit()
    connection.close()
    reopened = SqliteCorrelationStateStore(database)
    result = StateRecoveryService(reopened).reconstruct()
    assert result.findings == ()
    assert reopened.resolve("EVT-MATCH").intent is None

    connection = sqlite3.connect(database)
    payload["policy_version"] = "conflict"
    payload["operation_id"] = "OP-CONFLICT"
    connection.execute("INSERT INTO correlation_state_intents(event_id, operation_id, payload) VALUES (?, ?, ?)", ("EVT-MATCH", "OP-CONFLICT", json.dumps(payload)))
    connection.commit()
    connection.close()
    conflict = StateRecoveryService(reopened).reconstruct()
    assert conflict.findings[0].error_code is StateDomainErrorCode.TERMINAL_OWNERSHIP_CONFLICT


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        ("{not json", StateDomainErrorCode.MALFORMED_STATE_RECORD),
        (json.dumps({"schema_version": 99}), StateDomainErrorCode.UNSUPPORTED_STATE_VERSION),
        (json.dumps({"schema_version": 1}), StateDomainErrorCode.MALFORMED_STATE_RECORD),
    ],
)
def test_malformed_or_unsupported_persisted_state_is_per_event_repair_required(tmp_path, payload, expected_code):
    database = tmp_path / "state.sqlite"
    store = SqliteCorrelationStateStore(database)
    store.close()
    connection = sqlite3.connect(database)
    connection.execute("INSERT INTO correlation_state_pending(event_id, payload) VALUES (?, ?)", ("EVT-BAD", payload))
    connection.commit()
    connection.close()
    result = StateRecoveryService(SqliteCorrelationStateStore(database)).reconstruct()
    assert result.states == ()
    assert result.findings[0].event_id == "EVT-BAD"
    assert result.findings[0].error_code is expected_code
    assert result.findings[0].retry_disposition is RetryDisposition.REPAIR_REQUIRED


def test_reference_port_reports_dangling_event_incident_and_shadow_without_repairing(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    _finalize(store, _processed("EVT-EVENT", incident_id="INC-OK"))
    _finalize(store, _processed("EVT-INCIDENT", incident_id="INC-MISSING"))
    shadow = ProcessedCorrelationRecord("EVT-SHADOW", TerminalOutcome.SHADOWED, NOW, None, "SHADOW-MISSING", "POLICY-GENERAL-LOG-ANOMALY", "1.0")
    claim = store.acquire_claim("EVT-SHADOW")
    assert claim is not None
    shadow_intent = CorrelationMutationIntent("OP-SHADOW", "EVT-SHADOW", TerminalOutcome.SHADOWED, DecisionType.ROUTE_SHADOW, "POLICY-GENERAL-LOG-ANOMALY", "1.0", CorrelationFamily.UNKNOWN, DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY, None, None, None, AnchorTransition.NONE, NOW)
    store.begin_intent(shadow_intent, claim)
    store.finalize_processed(shadow, claim)
    port = ReferencePortDouble(events={"EVT-INCIDENT", "EVT-SHADOW"}, incidents=set(), shadows=set())
    result = StateRecoveryService(store).reconstruct(port)
    findings = {finding.event_id: finding.error_code for finding in result.findings}
    assert findings == {
        "EVT-EVENT": StateDomainErrorCode.DANGLING_EVENT_REFERENCE,
        "EVT-INCIDENT": StateDomainErrorCode.DANGLING_INCIDENT_REFERENCE,
        "EVT-SHADOW": StateDomainErrorCode.DANGLING_SHADOW_REFERENCE,
    }
    assert store.get_processed("EVT-INCIDENT").incident_id == "INC-MISSING"


def test_store_unreadable_fails_startup_and_retention_guard_does_not_clear_state(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    seed_pending(store, _pending("EVT-1"))
    with pytest.raises(StateDomainValidationError):
        store.retention_cleanup_guard()
    assert store.resolve("EVT-1").pending is not None
    store._connection.close()  # Simulates a store that cannot be enumerated at startup.
    with pytest.raises(StateStoreStartupError):
        StateRecoveryService(store).reconstruct()


def test_processed_survives_age_and_pending_expiry_is_not_ttl(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    _finalize(store, _processed("EVT-OLD", resolved_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    expired = ActivePendingRecord("EVT-EXPIRED", NOW - timedelta(days=10), NOW - timedelta(days=9), CorrelationPolicyKind.STRONG_ANCHOR, "POLICY-BRUTE-FORCE-DETECTED", "1.0", PendingReason.NO_COMPATIBLE_CANDIDATE)
    seed_pending(store, expired)
    result = StateRecoveryService(store).reconstruct()
    actions = {state.entry.event_id: state.action for state in result.states}
    assert actions["EVT-OLD"] is RecoveryAction.TERMINAL_NOOP
    assert actions["EVT-EXPIRED"] is RecoveryAction.PRESERVE_PENDING
    assert store.get_processed("EVT-OLD") is not None
    assert store.resolve("EVT-EXPIRED").pending == expired


def _intent_payload(intent):
    return {
        "schema_version": 1, "operation_id": intent.operation_id, "event_id": intent.event_id,
        "intended_terminal_outcome": intent.intended_terminal_outcome.value,
        "decision_type": intent.decision_type.value, "policy_id": intent.policy_id,
        "policy_version": intent.policy_version, "correlation_family": intent.correlation_family.value,
        "reason_code": intent.reason_code.value, "target_incident_id": intent.target_incident_id,
        "normalized_fingerprint": {"event_type": FINGERPRINT.event_type, "identity": [list(item) for item in FINGERPRINT.identity]},
        "anchor_strength": "STRONG", "anchor_transition": "NONE", "created_at": intent.created_at.isoformat(),
    }
