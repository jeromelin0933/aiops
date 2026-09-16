from datetime import datetime, timedelta, timezone

import pytest

from alert_correlation import (
    AnchorStrength,
    AnchorTransition,
    CorrelationErrorCode,
    CorrelationEvaluationError,
    CorrelationFamily,
    DecisionReasonCode,
    DecisionType,
    EvaluationPhase,
    NormalizedFingerprint,
)
from alert_correlation.state import (
    ActivePendingRecord,
    ClaimAbandonmentProof,
    CorrelationMutationIntent,
    CorrelationPolicyKind,
    PendingReason,
    ProcessedCorrelationRecord,
    RetryDisposition,
    SqliteCorrelationStateStore,
    TerminalOutcome,
)
from event_detection.store.event_store import EventStore
from runtime_orchestration import (
    DurableEventIntake,
    RuntimeBootstrap,
    RuntimeClock,
    RuntimeDispatchKind,
    RuntimeLifecycleState,
    RuntimeNotReadyError,
    RuntimeStateRecoveryClassifier,
    RuntimeWorkKind,
    RuntimeWorkRecord,
    RuntimeWorkStatus,
    SqliteRuntimeWorkStore,
    StartupBarrierError,
    runtime_work_id,
)


NOW = datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc)
CONFIG = "configs/runtime_orchestration.yaml"
FINGERPRINT = NormalizedFingerprint("brute_force_detected", (("source_ip", "1.2.3.4"),))


def _clock() -> RuntimeClock:
    return RuntimeClock(lambda: NOW, lambda: 0.0, lambda _seconds: None)


def _runtime(tmp_path, event_store=None, state_store=None, work_store=None):
    event_store = event_store or EventStore(tmp_path / "events.jsonl")
    state_store = state_store or SqliteCorrelationStateStore(tmp_path / "state.sqlite3")
    work_store = work_store or SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
    classifier = RuntimeStateRecoveryClassifier(state_store)
    runtime = RuntimeBootstrap(
        config_path=CONFIG,
        event_intake=DurableEventIntake(event_store),
        state_recovery=classifier,
        runtime_work_store=work_store,
        clock=_clock(),
    )
    return runtime, event_store, state_store, work_store, classifier


def _pending(event_id: str, expires_at: datetime) -> ActivePendingRecord:
    return ActivePendingRecord(
        event_id,
        expires_at - timedelta(seconds=30),
        expires_at,
        CorrelationPolicyKind.STRONG_ANCHOR,
        "POLICY-BRUTE-FORCE-DETECTED",
        "1.0",
        PendingReason.NO_COMPATIBLE_CANDIDATE,
    )


def _intent(event_id: str, operation_id: str, outcome=TerminalOutcome.CREATED_INCIDENT):
    decision_type = {
        TerminalOutcome.CREATED_INCIDENT: DecisionType.CREATE_NEW,
        TerminalOutcome.ATTACHED_TO_INCIDENT: DecisionType.ATTACH_EXISTING,
    }[outcome]
    return CorrelationMutationIntent(
        operation_id,
        event_id,
        outcome,
        decision_type,
        "POLICY-BRUTE-FORCE-DETECTED",
        "1.0",
        CorrelationFamily.ATTACK_SOURCE,
        DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
        "INC-EXISTING" if outcome is TerminalOutcome.ATTACHED_TO_INCIDENT else None,
        FINGERPRINT,
        AnchorStrength.STRONG,
        AnchorTransition.NONE,
        NOW,
    )


def _processed(state_store, event_id: str, outcome: TerminalOutcome):
    claim = state_store.acquire_claim(event_id)
    assert claim is not None
    state_store.begin_intent(_intent(event_id, f"OP-{event_id}", outcome), claim)
    record = ProcessedCorrelationRecord(
        event_id,
        outcome,
        NOW,
        "INC-NEW" if outcome is TerminalOutcome.CREATED_INCIDENT else "INC-EXISTING",
        None,
        "POLICY-BRUTE-FORCE-DETECTED",
        "1.0",
    )
    state_store.finalize_processed(record, claim)


def _classification(snapshot):
    return {item.event_id: item for item in snapshot.states.classifications}


def test_corrupt_event_store_prevents_ready(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"event_id":"EVT-1"}\n{broken', encoding="utf-8")
    runtime, *_ = _runtime(tmp_path, event_store=EventStore(path))

    with pytest.raises(StartupBarrierError):
        runtime.recover_to_ready()
    assert runtime.state is RuntimeLifecycleState.RECOVERY
    assert runtime.last_recovery is None


def test_successful_authoritative_enumeration_and_d2_enumeration(tmp_path):
    runtime, events, _state, work_store, _classifier = _runtime(tmp_path)
    events.write({"event_id": "EVT-2", "value": "second"})
    events.write({"event_id": "EVT-1", "value": "first"})
    work = RuntimeWorkRecord(
        work_id=runtime_work_id(RuntimeWorkKind.DOMAIN_OPERATION, "EVT-1", "OP-1"),
        work_kind=RuntimeWorkKind.DOMAIN_OPERATION,
        event_id="EVT-1",
        stage="RETRY_WAIT",
        next_action="RECONCILE_OPERATION",
        operation_id="OP-1",
        attempt_count=1,
        retry_limit=4,
        next_retry_at=NOW + timedelta(seconds=1),
        last_attempt_at=NOW,
        source_domain="SPEC-008",
        source_error_code="TEMPORARY_UNAVAILABLE",
        source_retry_disposition="RETRYABLE",
        status=RuntimeWorkStatus.OUTSTANDING,
        created_at=NOW,
        updated_at=NOW,
        observed_at=NOW,
    )
    persisted = work_store.create(work)

    snapshot = runtime.begin_recovery()
    assert runtime.state is RuntimeLifecycleState.RECOVERY
    assert tuple(snapshot.events.event_by_id) == ("EVT-2", "EVT-1")
    assert {item.dispatch_kind for item in snapshot.states.classifications} == {
        RuntimeDispatchKind.UNSEEN
    }
    assert snapshot.runtime_work.records == (persisted,)


def test_runtime_intake_has_no_legacy_read_all_dependency(tmp_path):
    class AuthoritativeOnlyStore:
        def __init__(self):
            self.authoritative_calls = 0

        def read_all(self):
            raise AssertionError("legacy read_all must never be called")

        def read_all_authoritative(self):
            self.authoritative_calls += 1
            return [{"event_id": "EVT-1"}]

    store = AuthoritativeOnlyStore()
    runtime, *_ = _runtime(tmp_path, event_store=store)
    runtime.begin_recovery()
    with pytest.raises(RuntimeNotReadyError):
        runtime.dispatch_normal_intake()
    assert store.authoritative_calls == 1


def test_unresolved_intent_classification(tmp_path):
    runtime, events, state, *_ = _runtime(tmp_path)
    events.write({"event_id": "EVT-INTENT"})
    claim = state.acquire_claim("EVT-INTENT")
    assert claim is not None
    state.begin_intent(_intent("EVT-INTENT", "OP-INTENT"), claim)

    item = _classification(runtime.begin_recovery())["EVT-INTENT"]
    assert item.dispatch_kind is RuntimeDispatchKind.UNRESOLVED_INTENT


@pytest.mark.parametrize(
    ("event_id", "expires_at", "expected"),
    [
        ("EVT-ACTIVE", NOW + timedelta(seconds=1), RuntimeDispatchKind.ACTIVE_PENDING),
        ("EVT-EXPIRED", NOW, RuntimeDispatchKind.EXPIRED_PENDING),
    ],
)
def test_active_and_expired_pending_classification(tmp_path, event_id, expires_at, expected):
    runtime, events, state, *_ = _runtime(tmp_path)
    events.write({"event_id": event_id})
    claim = state.acquire_claim(event_id)
    assert claim is not None
    state.activate_pending(_pending(event_id, expires_at), claim)

    item = _classification(runtime.begin_recovery())[event_id]
    assert item.dispatch_kind is expected
    assert item.pending_expires_at == expires_at
    assert state.resolve(event_id).pending.expires_at == expires_at


def test_blocked_preserves_domain_retry_disposition(tmp_path):
    runtime, events, state, *_ = _runtime(tmp_path)
    event_id = "EVT-BLOCKED"
    events.write({"event_id": event_id})
    claim = state.acquire_claim(event_id)
    assert claim is not None
    error = CorrelationEvaluationError(
        CorrelationErrorCode.INVALID_INCIDENT_VIEW,
        event_id,
        "brute_force_detected",
        "POLICY-BRUTE-FORCE-DETECTED",
        "1.0",
    )
    state.record_evaluation_failure(
        error, EvaluationPhase.INITIAL, now=NOW, claim=claim
    )

    item = _classification(runtime.begin_recovery())[event_id]
    assert item.dispatch_kind is RuntimeDispatchKind.BLOCKED
    assert item.retry_disposition is RetryDisposition.REPAIR_REQUIRED
    assert state.resolve(event_id).blocked.retry_disposition is RetryDisposition.REPAIR_REQUIRED


@pytest.mark.parametrize(
    ("event_id", "outcome", "assignment_marker"),
    [
        ("EVT-ATTACHED", TerminalOutcome.ATTACHED_TO_INCIDENT, False),
        ("EVT-CREATED", TerminalOutcome.CREATED_INCIDENT, True),
    ],
)
def test_processed_is_terminal_and_created_incident_marks_assignment_reconciliation(
    tmp_path, event_id, outcome, assignment_marker
):
    runtime, events, state, *_ = _runtime(tmp_path)
    events.write({"event_id": event_id})
    _processed(state, event_id, outcome)

    item = _classification(runtime.begin_recovery())[event_id]
    assert item.dispatch_kind is RuntimeDispatchKind.PROCESSED
    assert item.correlation_terminal is True
    assert item.assignment_reconciliation_candidate is assignment_marker


def test_restart_does_not_reclaim_claim_without_formal_proof(tmp_path):
    runtime, events, state, _work, classifier = _runtime(tmp_path)
    event_id = "EVT-CLAIM"
    events.write({"event_id": event_id})
    original = state.acquire_claim(event_id)
    assert original is not None

    item = _classification(runtime.begin_recovery())[event_id]
    assert item.dispatch_kind is RuntimeDispatchKind.CLAIM_RECLAIM_CANDIDATE
    assert item.claim_id == original.claim_id
    assert state.resolve(event_id).claim == original
    with pytest.raises(TypeError, match="ClaimAbandonmentProof"):
        classifier.safe_reclaim(event_id, None)
    assert state.resolve(event_id).claim == original


def test_valid_safe_reclaim_path_requires_matching_abandonment_proof(tmp_path):
    runtime, events, state, _work, classifier = _runtime(tmp_path)
    event_id = "EVT-CLAIM"
    events.write({"event_id": event_id})
    original = state.acquire_claim(event_id)
    assert original is not None
    runtime.begin_recovery()
    proof = ClaimAbandonmentProof(event_id, original.claim_id, "worker termination proven")

    replacement = classifier.safe_reclaim(event_id, proof)
    assert replacement.claim_id != original.claim_id
    assert replacement.fencing_token == original.fencing_token + 1
    assert state.resolve(event_id).claim == replacement


def test_d2_store_unreadable_prevents_ready(tmp_path):
    work_store = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
    work_store.close()
    runtime, *_ = _runtime(tmp_path, work_store=work_store)

    with pytest.raises(StartupBarrierError):
        runtime.recover_to_ready()
    assert runtime.state is RuntimeLifecycleState.RECOVERY


def test_repeated_recovery_interruption_restarts_from_authoritative_evidence(tmp_path):
    events = EventStore(tmp_path / "events.jsonl")
    events.write({"event_id": "EVT-1"})
    real_state = SqliteCorrelationStateStore(tmp_path / "state.sqlite3")

    class InterruptOnceStatePort:
        def __init__(self):
            self.enumerations = 0

        def recovery_event_ids(self):
            self.enumerations += 1
            if self.enumerations == 1:
                raise RuntimeError("simulated recovery interruption")
            return real_state.recovery_event_ids()

        def resolve(self, event_id):
            return real_state.resolve(event_id)

        def reclaim_claim(self, claim, proof):
            return real_state.reclaim_claim(claim, proof)

    port = InterruptOnceStatePort()
    work = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
    first, *_ = _runtime(tmp_path, event_store=events, state_store=port, work_store=work)
    with pytest.raises(StartupBarrierError):
        first.recover_to_ready()
    assert first.state is RuntimeLifecycleState.RECOVERY

    second, *_ = _runtime(tmp_path, event_store=events, state_store=port, work_store=work)
    result = second.begin_recovery()
    assert second.state is RuntimeLifecycleState.RECOVERY
    assert _classification(result)["EVT-1"].dispatch_kind is RuntimeDispatchKind.UNSEEN
    assert port.enumerations == 2


def test_normal_intake_cannot_run_during_discovery_only_recovery(tmp_path):
    runtime, events, *_ = _runtime(tmp_path)
    events.write({"event_id": "EVT-1"})
    with pytest.raises(RuntimeNotReadyError):
        runtime.dispatch_normal_intake()

    runtime.begin_recovery()
    with pytest.raises(RuntimeNotReadyError):
        runtime.dispatch_normal_intake()


def test_bootstrap_recover_to_ready_cannot_bypass_actionable_recovery(tmp_path):
    runtime, events, state, *_ = _runtime(tmp_path)
    events.write({"event_id": "EVT-INTENT"})
    claim = state.acquire_claim("EVT-INTENT")
    assert claim is not None
    state.begin_intent(_intent("EVT-INTENT", "OP-INTENT"), claim)

    with pytest.raises(RuntimeNotReadyError, match="CorrelationRuntimeCore.startup"):
        runtime.recover_to_ready()
    assert runtime.state is RuntimeLifecycleState.RECOVERY
    assert state.resolve("EVT-INTENT").processed is None
    with pytest.raises(RuntimeNotReadyError):
        runtime.dispatch_normal_intake()
