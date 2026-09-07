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
    ResolvedState, RetryDisposition, SqliteCorrelationStateStore,
    StateDomainErrorCode, StateDomainValidationError, StateStoreIntegrityError,
    TerminalOutcome,
)


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def _pending(event_id="EVT-PENDING"):
    return ActivePendingRecord(event_id, NOW, NOW + timedelta(seconds=30), CorrelationPolicyKind.STRONG_ANCHOR, "POLICY-BRUTE-FORCE-DETECTED", "1.0", PendingReason.NO_COMPATIBLE_CANDIDATE)


def _block(event_id="EVT-BLOCK"):
    return BlockedCorrelationRecord(event_id, FailureKind.CORRELATION_DOMAIN_FAILURE, CorrelationErrorCode.INVALID_INCIDENT_VIEW, EvaluationPhase.PENDING_RECHECK, NOW, NOW, 1, RetryDisposition.RETRYABLE, "POLICY-BRUTE-FORCE-DETECTED", "1.0")


def _intent(event_id="EVT-INTENT", operation_id="OP-1", decision_type=DecisionType.CREATE_NEW):
    fingerprint = NormalizedFingerprint("brute_force_detected", (("source_ip", "1.2.3.4"),))
    if decision_type is DecisionType.ATTACH_EXISTING:
        return CorrelationMutationIntent(operation_id, event_id, TerminalOutcome.ATTACHED_TO_INCIDENT, decision_type, "POLICY-BRUTE-FORCE-DETECTED", "1.0", CorrelationFamily.ATTACK_SOURCE, DecisionReasonCode.EXACT_STRONG_IDENTITY_MATCH, "INC-1", fingerprint, AnchorStrength.STRONG, AnchorTransition.NONE, NOW)
    return CorrelationMutationIntent(operation_id, event_id, TerminalOutcome.CREATED_INCIDENT, decision_type, "POLICY-BRUTE-FORCE-DETECTED", "1.0", CorrelationFamily.ATTACK_SOURCE, DecisionReasonCode.NO_COMPATIBLE_CANDIDATE, None, fingerprint, AnchorStrength.STRONG, AnchorTransition.NONE, NOW)


def _processed(event_id="EVT-PROCESSED", incident_id="INC-NEW"):
    return ProcessedCorrelationRecord(event_id, TerminalOutcome.CREATED_INCIDENT, NOW, incident_id, None, "POLICY-BRUTE-FORCE-DETECTED", "1.0")


def _claim(store, event_id):
    claim = store.acquire_claim(event_id)
    assert claim is not None
    return claim


def test_durable_records_survive_close_reopen_and_reconstruct(tmp_path):
    database = tmp_path / "correlation-state.sqlite"
    store = SqliteCorrelationStateStore(database)
    pending = _pending()
    block = _block()
    intent = _intent()
    processed_intent = _intent("EVT-PROCESSED", "OP-PROCESSED")
    processed = _processed()
    seed_pending(store, pending)
    seed_block(store, block)
    store.begin_intent(intent, _claim(store, intent.event_id))
    processed_claim = _claim(store, processed_intent.event_id)
    store.begin_intent(processed_intent, processed_claim)
    store.finalize_processed(processed, processed_claim)
    store.close()

    reopened = SqliteCorrelationStateStore(database)
    assert reopened.resolve(pending.event_id).pending == pending
    assert reopened.resolve(block.event_id).blocked == block
    assert reopened.resolve(intent.event_id).intent == intent
    assert reopened.get_processed(processed.event_id) == processed
    reopened.close()


def test_processed_has_unique_event_ownership(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    claim = _claim(store, "EVT-1")
    store.begin_intent(_intent("EVT-1"), claim)
    first = _processed("EVT-1", "INC-1")
    store.finalize_processed(first, claim)
    assert store.finalize_processed(first) == first
    with pytest.raises(StateDomainValidationError):
        store.finalize_processed(_processed("EVT-1", "INC-2"))


def test_fixed_precedence_and_pending_block_coexistence(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    seed_pending(store, _pending("EVT-1"))
    seed_block(store, _block("EVT-1"))
    resolved = store.resolve("EVT-1")
    assert resolved.state is ResolvedState.ACTIVE_PENDING_BLOCKED
    assert resolved.pending is not None and resolved.blocked is not None

    store.begin_intent(_intent("EVT-1"), _claim(store, "EVT-1"))
    assert store.resolve("EVT-1").state is ResolvedState.UNRESOLVED_MUTATION_INTENT


def test_processed_matching_stale_intent_is_identified_and_conflict_fails_closed(tmp_path):
    database = tmp_path / "state.sqlite"
    store = SqliteCorrelationStateStore(database)
    intent = _intent("EVT-1", "OP-1")
    processed = _processed("EVT-1")
    claim = _claim(store, "EVT-1")
    store.begin_intent(intent, claim)
    store.finalize_processed(processed, claim)
    store.close()

    connection = sqlite3.connect(database)
    payload = {
        "schema_version": 1,
        "operation_id": intent.operation_id, "event_id": intent.event_id,
        "intended_terminal_outcome": intent.intended_terminal_outcome.value,
        "decision_type": intent.decision_type.value, "policy_id": intent.policy_id,
        "policy_version": intent.policy_version, "correlation_family": intent.correlation_family.value,
        "reason_code": intent.reason_code.value, "target_incident_id": None,
        "normalized_fingerprint": {"event_type": "brute_force_detected", "identity": [["source_ip", "1.2.3.4"]]},
        "anchor_strength": "STRONG", "anchor_transition": "NONE", "created_at": NOW.isoformat(),
    }
    connection.execute("INSERT INTO correlation_state_intents(event_id, operation_id, payload) VALUES (?, ?, ?)", ("EVT-1", "OP-1", json.dumps(payload)))
    connection.commit()
    connection.close()
    reopened = SqliteCorrelationStateStore(database)
    assert reopened.resolve("EVT-1").matching_stale_intent is True
    reopened.close()

    connection = sqlite3.connect(database)
    payload["policy_version"] = "9.9"
    connection.execute("UPDATE correlation_state_intents SET payload = ? WHERE event_id = ?", (json.dumps(payload), "EVT-1"))
    connection.commit()
    connection.close()
    conflicting = SqliteCorrelationStateStore(database)
    with pytest.raises(StateDomainValidationError) as error:
        conflicting.resolve("EVT-1")
    assert error.value.code is StateDomainErrorCode.TERMINAL_OWNERSHIP_CONFLICT


def test_malformed_persisted_state_never_becomes_unseen(tmp_path):
    database = tmp_path / "state.sqlite"
    store = SqliteCorrelationStateStore(database)
    store.close()
    connection = sqlite3.connect(database)
    connection.execute("INSERT INTO correlation_state_pending(event_id, payload) VALUES (?, ?)", ("EVT-BAD", "{not json"))
    connection.commit()
    connection.close()
    reopened = SqliteCorrelationStateStore(database)
    with pytest.raises(StateStoreIntegrityError) as error:
        reopened.resolve("EVT-BAD")
    assert error.value.event_id == "EVT-BAD"


def test_recovery_enumeration_reads_state_only_and_includes_processed_lookup(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    seed_pending(store, _pending("EVT-PENDING"))
    seed_block(store, _block("EVT-BLOCK"))
    store.begin_intent(_intent("EVT-INTENT"), _claim(store, "EVT-INTENT"))
    processed_claim = _claim(store, "EVT-PROCESSED")
    store.begin_intent(_intent("EVT-PROCESSED", "OP-P"), processed_claim)
    store.finalize_processed(_processed("EVT-PROCESSED"), processed_claim)
    entries = {entry.event_id: entry.resolved.state for entry in store.enumerate_recovery_states()}
    assert entries == {
        "EVT-BLOCK": ResolvedState.BLOCKED,
        "EVT-INTENT": ResolvedState.UNRESOLVED_MUTATION_INTENT,
        "EVT-PENDING": ResolvedState.ACTIVE_PENDING,
        "EVT-PROCESSED": ResolvedState.TERMINAL_PROCESSED,
    }
    assert store.get_processed("EVT-PROCESSED").incident_id == "INC-NEW"
