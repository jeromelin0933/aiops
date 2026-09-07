"""SPEC-007 Phase 6 integration, concurrency, and crash-boundary evidence.

SPEC-008 and SPEC-010 are deliberately represented by ``IdempotentDomainPort``:
it records an operation result but owns no Incident or Shadow implementation.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from src.alert_correlation import (
    AlertCorrelationPolicyEngine,
    CorrelationErrorCode,
    CorrelationEvaluationContext,
    CorrelationEvaluationFailure,
    CorrelationEvaluationSuccess,
    DecisionType,
    EvaluationPhase,
)
from src.alert_correlation.state import (
    BlockedCorrelationRecord,
    ClaimAbandonmentProof,
    CorrelationMutationIntent,
    FailureKind,
    PendingGraceConfig,
    PendingStateService,
    ProcessedCorrelationRecord,
    ResolvedState,
    RetryDisposition,
    SqliteCorrelationStateStore,
    StateDomainErrorCode,
    StateDomainValidationError,
    StateRecoveryService,
    TerminalOutcome,
)


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def _event(event_id="EVT-PHASE6"):
    return {
        "event_id": event_id,
        "event_type": "brute_force_detected",
        "detected_at": "2026-09-07T12:00:00Z",
        "source_ip": "203.0.113.9",
    }


def _pending_event(event_id="EVT-PHASE6"):
    return {
        "event_id": event_id,
        "event_type": "high_memory_detected",
        "detected_at": "2026-09-07T12:00:00Z",
    }


def _success(result):
    assert isinstance(result, CorrelationEvaluationSuccess)
    return result.decision


class IdempotentDomainPort:
    """Minimal SPEC-008/010 contract double keyed by operation_id."""

    def __init__(self):
        self._results = {}
        self.calls = []

    def execute(self, intent, *, now=NOW):
        self.calls.append(intent.operation_id)
        if intent.operation_id not in self._results:
            if intent.intended_terminal_outcome is TerminalOutcome.SHADOWED:
                result = ProcessedCorrelationRecord(
                    intent.event_id, TerminalOutcome.SHADOWED, now, None,
                    "SHADOW-PHASE6", intent.policy_id, intent.policy_version,
                )
            else:
                result = ProcessedCorrelationRecord(
                    intent.event_id, intent.intended_terminal_outcome, now,
                    intent.target_incident_id or "INC-PHASE6", None,
                    intent.policy_id, intent.policy_version,
                )
            self._results[intent.operation_id] = result
        return self._results[intent.operation_id]


def _terminal_intent(event_id="EVT-PHASE6", operation_id="OP-PHASE6"):
    decision = _success(AlertCorrelationPolicyEngine().evaluate(
        _event(event_id), [], CorrelationEvaluationContext(EvaluationPhase.INITIAL)
    ))
    assert decision.decision_type is DecisionType.CREATE_NEW
    return CorrelationMutationIntent.from_decision(
        operation_id=operation_id, event_id=event_id, decision=decision, created_at=NOW,
    )


def test_initial_pending_recheck_expiry_and_domain_failure_block_use_real_spec_006_contracts(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    engine = AlertCorrelationPolicyEngine()
    pending_decision = _success(engine.evaluate(
        _pending_event(), [], CorrelationEvaluationContext(EvaluationPhase.INITIAL)
    ))
    assert pending_decision.decision_type is DecisionType.ENTER_PENDING

    pending = PendingStateService(store, engine.registry.resolve_exact, PendingGraceConfig(30))
    claim = store.acquire_claim("EVT-PHASE6")
    assert claim is not None
    active = pending.accept_enter_pending("EVT-PHASE6", pending_decision, now=NOW, claim=claim)
    recheck = pending.resolve_pending_phase("EVT-PHASE6", now=NOW + timedelta(seconds=29))
    expired = pending.resolve_pending_phase("EVT-PHASE6", now=active.expires_at)
    assert (recheck.context.policy_id, recheck.context.policy_version) == (
        active.policy_id, active.policy_version
    )
    assert recheck.evaluation_phase is EvaluationPhase.PENDING_RECHECK
    assert expired.evaluation_phase is EvaluationPhase.PENDING_EXPIRED

    failure = engine.evaluate(
        _pending_event(), [object()], recheck.context
    )
    assert isinstance(failure, CorrelationEvaluationFailure)
    assert failure.error.error_code is CorrelationErrorCode.INVALID_INCIDENT_VIEW
    block = pending.record_evaluation_failure(
        failure, recheck.evaluation_phase, now=NOW, claim=claim
    )
    assert block.retry_disposition is RetryDisposition.REPAIR_REQUIRED
    assert store.resolve("EVT-PHASE6").state is ResolvedState.ACTIVE_PENDING_BLOCKED


def test_terminal_decision_intent_and_domain_success_finalize_after_reopen(tmp_path):
    database = tmp_path / "state.sqlite"
    store = SqliteCorrelationStateStore(database)
    intent = _terminal_intent()
    claim = store.acquire_claim(intent.event_id)
    assert claim is not None
    assert store.begin_intent(intent, claim) == intent
    store.close()  # Crash B: Intent survives before any domain mutation.

    reopened = SqliteCorrelationStateStore(database)
    assert reopened.resolve(intent.event_id).state is ResolvedState.UNRESOLVED_MUTATION_INTENT
    port = IdempotentDomainPort()
    result = port.execute(intent)
    reopened.finalize_processed(result, reopened.resolve(intent.event_id).claim)
    assert reopened.resolve(intent.event_id).state is ResolvedState.TERMINAL_PROCESSED
    assert port.calls == [intent.operation_id]


def test_same_event_concurrency_stale_executor_and_duplicate_terminal_replay(tmp_path):
    database = tmp_path / "state.sqlite"

    def claim_once(_):
        store = SqliteCorrelationStateStore(database)
        try:
            return store.acquire_claim("EVT-CONCURRENT")
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim_once, range(2)))
    winner = next(claim for claim in claims if claim is not None)
    assert sum(claim is not None for claim in claims) == 1

    store = SqliteCorrelationStateStore(database)
    replacement = store.reclaim_claim(
        winner, ClaimAbandonmentProof("EVT-CONCURRENT", winner.claim_id, "crash proven"),
    )
    intent = _terminal_intent("EVT-CONCURRENT", "OP-CONCURRENT")
    with pytest.raises(StateDomainValidationError):
        store.begin_intent(intent, winner)
    store.begin_intent(intent, replacement)
    result = IdempotentDomainPort().execute(intent)
    assert store.finalize_processed(result, replacement) == result
    assert store.finalize_processed(result) == result
    conflicting = ProcessedCorrelationRecord(
        intent.event_id, TerminalOutcome.CREATED_INCIDENT, NOW, "INC-OTHER", None,
        intent.policy_id, intent.policy_version,
    )
    with pytest.raises(StateDomainValidationError) as error:
        store.finalize_processed(conflicting)
    assert error.value.code is StateDomainErrorCode.TERMINAL_OWNERSHIP_CONFLICT


def test_crash_a_c_d_and_repeated_recovery_are_idempotent(tmp_path):
    database = tmp_path / "state.sqlite"
    # A: an entered Pending record survives a crash unchanged.
    store = SqliteCorrelationStateStore(database)
    pending_decision = _success(AlertCorrelationPolicyEngine().evaluate(
        _pending_event("EVT-A"), [], CorrelationEvaluationContext(EvaluationPhase.INITIAL)
    ))
    pending = PendingStateService(store, AlertCorrelationPolicyEngine().registry.resolve_exact)
    claim_a = store.acquire_claim("EVT-A")
    assert claim_a is not None
    original = pending.accept_enter_pending("EVT-A", pending_decision, now=NOW, claim=claim_a)
    store.close()
    reopened = SqliteCorrelationStateStore(database)
    assert PendingStateService(reopened, AlertCorrelationPolicyEngine().registry.resolve_exact).resolve_pending_phase(
        "EVT-A", now=NOW + timedelta(seconds=10)
    ).pending == original

    # C: the minimal domain double has already succeeded; recovery repeats only its operation.
    intent = _terminal_intent("EVT-C", "OP-C")
    claim = reopened.acquire_claim("EVT-C")
    assert claim is not None
    reopened.begin_intent(intent, claim)
    port = IdempotentDomainPort()
    successful_result = port.execute(intent)
    reopened.close()
    recovered = SqliteCorrelationStateStore(database)
    recovery = StateRecoveryService(recovered).reconstruct()
    assert {state.entry.event_id for state in recovery.states} == {"EVT-A", "EVT-C"}
    recovered.finalize_processed(port.execute(intent), recovered.resolve("EVT-C").claim)
    assert port.execute(intent) == successful_result
    assert len(port._results) == 1

    # D: once Processed exists, repeated startup recovery is a no-op.
    first = StateRecoveryService(recovered).reconstruct()
    second = StateRecoveryService(recovered).reconstruct()
    assert first == second
    assert recovered.resolve("EVT-C").state is ResolvedState.TERMINAL_PROCESSED
