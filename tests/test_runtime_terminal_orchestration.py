"""SPEC-011 Phase 3 real-domain terminal orchestration evidence."""

from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from alert_correlation import AlertCorrelationPolicyEngine, DecisionType
from alert_correlation.policy import DEFAULT_POLICY_REGISTRY
from alert_correlation.state import (
    ClaimAbandonmentProof,
    ResolvedState,
    SqliteCorrelationStateStore,
    TerminalOutcome,
)
from incident_management import (
    IncidentDomainError,
    IncidentErrorCode,
    IncidentManager,
    IncidentStatus,
    SqliteIncidentStore,
)
from runtime_orchestration import (
    DurableEventIntake,
    InitialCorrelationOrchestrator,
    InitialExecutionStatus,
    RuntimeClock,
    RuntimeBootstrap,
    RuntimeConcurrentClaimError,
    RuntimeClaimAuthorityError,
    RuntimeLifecycleState,
    RuntimeNotReadyError,
    RuntimeOwnershipIntegrityError,
    RuntimeStateRecoveryClassifier,
    SqliteRuntimeWorkStore,
    runtime_operation_id,
)
from shadow_management import (
    ShadowManager,
    ShadowReason,
    ShadowRecord,
    ShadowReviewStatus,
    SqliteShadowStore,
)


NOW = datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc)


def _event(
    event_id: str,
    event_type: str,
    detected_at: datetime,
    *,
    source_ip: str | None = None,
    severity: str = "HIGH",
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "detected_at": detected_at.isoformat().replace("+00:00", "Z"),
        "event_source": "log_event_detection",
        "event_type": event_type,
        "detection_method": "rule_based",
        "severity": severity,
        "confidence": 0.99,
        "service_name": "auth-api",
        "trace_id": None,
        "source_ip": source_ip,
        "downstream_service": None,
        "external_service": None,
        "status": "OPEN",
        "triggered_features": {},
        "raw_log_sample": [],
    }


class _AuthoritativeEvents:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events
        self.scans = 0

    def read_all_authoritative(self) -> list[dict[str, object]]:
        self.scans += 1
        return self.events


class _Readiness:
    state = RuntimeLifecycleState.READY


class _RuntimeHarness:
    def __init__(self, tmp_path, events: list[dict[str, object]]) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.events = _AuthoritativeEvents(events)
        self.state = SqliteCorrelationStateStore(tmp_path / "state.sqlite3")
        self.incidents = SqliteIncidentStore(str(tmp_path / "incident.sqlite3"))
        self.shadows = SqliteShadowStore(tmp_path / "shadow.sqlite3")
        incident_ids = count(1)
        shadow_ids = count(1)
        self.incident_manager = IncidentManager(
            self.incidents,
            incident_id_factory=lambda: f"INC-{next(incident_ids)}",
        )
        self.shadow_manager = ShadowManager(
            self.shadows,
            DEFAULT_POLICY_REGISTRY,
            self.incidents,
            shadow_id_factory=lambda: f"SHADOW-{next(shadow_ids)}",
        )
        self.readiness = _Readiness()
        self.runtime = InitialCorrelationOrchestrator(
            event_intake=DurableEventIntake(self.events),
            state_store=self.state,
            policy_engine=AlertCorrelationPolicyEngine(),
            incident_store=self.incidents,
            incident_manager=self.incident_manager,
            shadow_store=self.shadows,
            shadow_manager=self.shadow_manager,
            clock=RuntimeClock(lambda: NOW, lambda: 0.0, lambda _seconds: None),
            readiness=self.readiness,
        )

    def close(self) -> None:
        self.state.close()
        self.incidents.close()
        self.shadows.close()


@pytest.fixture
def harnesses(tmp_path):
    opened: list[_RuntimeHarness] = []

    def create(events: list[dict[str, object]]) -> _RuntimeHarness:
        harness = _RuntimeHarness(tmp_path / f"case-{len(opened)}", events)
        opened.append(harness)
        return harness

    yield create
    for harness in opened:
        harness.close()


def test_initial_create_new_uses_real_policy_and_keeps_incident_open(harnesses) -> None:
    event = _event("EVT-CREATE", "brute_force_detected", NOW, source_ip="203.0.113.1")
    h = harnesses([event])

    outcome = h.runtime.process_authoritative_event("EVT-CREATE")
    incident = h.incidents.get_incident(outcome.processed.incident_id)

    assert outcome.decision.decision_type is DecisionType.CREATE_NEW
    assert outcome.processed.terminal_outcome is TerminalOutcome.CREATED_INCIDENT
    assert incident.status is IncidentStatus.OPEN
    assert incident.assignee is None
    assert h.incidents.get_operation_result(
        runtime_operation_id("TERMINAL_CORRELATION", "EVT-CREATE")
    ) is not None


def test_initial_terminal_execution_cannot_bypass_recovery_barrier(harnesses) -> None:
    h = harnesses([_event("EVT-NOT-READY", "brute_force_detected", NOW, source_ip="203.0.113.100")])
    h.readiness.state = RuntimeLifecycleState.RECOVERY

    with pytest.raises(RuntimeError, match="Recovery Barrier"):
        h.runtime.process_authoritative_event("EVT-NOT-READY")

    assert h.state.resolve("EVT-NOT-READY").claim is None
    assert h.events.scans == 0


def test_bootstrap_alone_cannot_authorize_initial_execution(tmp_path) -> None:
    event = _event("EVT-E2E", "brute_force_detected", NOW, source_ip="203.0.113.101")
    events = _AuthoritativeEvents([event])
    intake = DurableEventIntake(events)
    state = SqliteCorrelationStateStore(tmp_path / "state.sqlite3")
    incidents = SqliteIncidentStore(str(tmp_path / "incident.sqlite3"))
    shadows = SqliteShadowStore(tmp_path / "shadow.sqlite3")
    work = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
    clock = RuntimeClock(lambda: NOW, lambda: 0.0, lambda _seconds: None)
    bootstrap = RuntimeBootstrap(
        config_path="configs/runtime_orchestration.yaml",
        event_intake=intake,
        state_recovery=RuntimeStateRecoveryClassifier(state),
        runtime_work_store=work,
        clock=clock,
    )
    with pytest.raises(RuntimeNotReadyError, match="CorrelationRuntimeCore.startup"):
        bootstrap.recover_to_ready()
    executor = InitialCorrelationOrchestrator(
        event_intake=intake,
        state_store=state,
        policy_engine=AlertCorrelationPolicyEngine(),
        incident_store=incidents,
        incident_manager=IncidentManager(incidents, incident_id_factory=lambda: "INC-E2E"),
        shadow_store=shadows,
        shadow_manager=ShadowManager(shadows, DEFAULT_POLICY_REGISTRY, incidents),
        clock=clock,
        readiness=bootstrap,
    )

    with pytest.raises(RuntimeNotReadyError, match="Recovery Barrier"):
        bootstrap.execute_initial_event("EVT-E2E", executor)
    assert state.resolve("EVT-E2E").processed is None
    assert incidents.get_incident("INC-E2E") is None
    state.close()
    incidents.close()
    shadows.close()
    work.close()


def test_initial_attach_existing_uses_fresh_public_incident_views(harnesses) -> None:
    first = _event("EVT-1", "brute_force_detected", NOW, source_ip="203.0.113.2")
    second = _event(
        "EVT-2", "brute_force_detected", NOW + timedelta(seconds=30), source_ip="203.0.113.2"
    )
    h = harnesses([first, second])
    created = h.runtime.process_authoritative_event("EVT-1")

    attached = h.runtime.process_authoritative_event("EVT-2")
    incident = h.incidents.get_incident(created.processed.incident_id)

    assert attached.decision.decision_type is DecisionType.ATTACH_EXISTING
    assert attached.decision.target_incident_id == created.processed.incident_id
    assert attached.processed.terminal_outcome is TerminalOutcome.ATTACHED_TO_INCIDENT
    assert incident.event_ids == ("EVT-1", "EVT-2")


def test_initial_route_shadow_uses_formal_intent_reason(harnesses) -> None:
    event = _event("EVT-SHADOW", "general_log_anomaly", NOW)
    h = harnesses([event])

    outcome = h.runtime.process_authoritative_event("EVT-SHADOW")
    shadow = h.shadows.get_shadow_by_event_id("EVT-SHADOW")

    assert outcome.decision.decision_type is DecisionType.ROUTE_SHADOW
    assert outcome.processed.terminal_outcome is TerminalOutcome.SHADOWED
    assert shadow.reason is ShadowReason.INSUFFICIENT_OPERATIONAL_IDENTITY


def test_intent_is_durable_before_incident_domain_call(harnesses, monkeypatch) -> None:
    h = harnesses([_event("EVT-ORDER", "brute_force_detected", NOW, source_ip="203.0.113.3")])
    original = h.incident_manager.apply_correlation_mutation

    def asserting_call(request):
        resolved = h.state.resolve("EVT-ORDER")
        assert resolved.state is ResolvedState.UNRESOLVED_MUTATION_INTENT
        assert resolved.intent == request.intent
        return original(request)

    monkeypatch.setattr(h.incident_manager, "apply_correlation_mutation", asserting_call)
    h.runtime.process_authoritative_event("EVT-ORDER")


def test_crash_after_intent_resumes_without_policy_reevaluation(harnesses, monkeypatch) -> None:
    h = harnesses([_event("EVT-CRASH", "brute_force_detected", NOW, source_ip="203.0.113.4")])
    original_begin = h.state.begin_intent

    def crash_after_intent(intent, claim):
        original_begin(intent, claim)
        raise RuntimeError("simulated crash after Intent")

    monkeypatch.setattr(h.state, "begin_intent", crash_after_intent)
    with pytest.raises(RuntimeError, match="simulated crash"):
        h.runtime.process_authoritative_event("EVT-CRASH")

    unresolved = h.state.resolve("EVT-CRASH")
    assert unresolved.intent is not None
    assert unresolved.processed is None
    assert h.incidents.event_has_incident_owner("EVT-CRASH") is False
    monkeypatch.setattr(h.state, "begin_intent", original_begin)
    replacement = h.state.reclaim_claim(
        unresolved.claim,
        ClaimAbandonmentProof(
            "EVT-CRASH", unresolved.claim.claim_id, "crashed worker proven abandoned"
        ),
    )

    resumed = h.runtime.resume_authoritative_intent("EVT-CRASH", replacement)
    assert resumed.processed.terminal_outcome is TerminalOutcome.CREATED_INCIDENT
    assert resumed.decision is None


def test_domain_commit_response_lost_reconciles_same_operation(harnesses, monkeypatch) -> None:
    h = harnesses([_event("EVT-LOST", "brute_force_detected", NOW, source_ip="203.0.113.5")])
    original = h.incident_manager.apply_correlation_mutation

    def commit_then_lose(request):
        original(request)
        raise RuntimeError("response lost")

    monkeypatch.setattr(h.incident_manager, "apply_correlation_mutation", commit_then_lose)
    with pytest.raises(RuntimeError, match="response lost"):
        h.runtime.process_authoritative_event("EVT-LOST")
    unresolved = h.state.resolve("EVT-LOST")
    operation_id = unresolved.intent.operation_id
    durable_result = h.incidents.get_operation_result(operation_id)
    assert durable_result is not None
    assert unresolved.processed is None

    def must_not_repeat(_request):
        raise AssertionError("receipt reconciliation must not repeat domain mutation")

    monkeypatch.setattr(h.incident_manager, "apply_correlation_mutation", must_not_repeat)
    resumed = h.runtime.resume_authoritative_intent("EVT-LOST", unresolved.claim)
    assert resumed.reconciled_existing_receipt is True
    assert resumed.processed.incident_id == durable_result.incident_id


def test_domain_commit_before_processed_is_reconciled(harnesses, monkeypatch) -> None:
    h = harnesses([_event("EVT-GAP", "brute_force_detected", NOW, source_ip="203.0.113.6")])
    original_finalize = h.state.finalize_processed

    def crash_before_processed(_record, _claim=None):
        raise RuntimeError("crash before Processed")

    monkeypatch.setattr(h.state, "finalize_processed", crash_before_processed)
    with pytest.raises(RuntimeError, match="before Processed"):
        h.runtime.process_authoritative_event("EVT-GAP")
    unresolved = h.state.resolve("EVT-GAP")
    assert h.incidents.get_operation_result(unresolved.intent.operation_id) is not None
    assert unresolved.processed is None

    monkeypatch.setattr(h.state, "finalize_processed", original_finalize)
    resumed = h.runtime.resume_authoritative_intent("EVT-GAP", unresolved.claim)
    assert resumed.reconciled_existing_receipt is True
    assert h.state.resolve("EVT-GAP").processed == resumed.processed


def test_duplicate_event_is_correlation_terminal_noop(harnesses) -> None:
    h = harnesses([_event("EVT-DUP", "brute_force_detected", NOW, source_ip="203.0.113.7")])
    first = h.runtime.process_authoritative_event("EVT-DUP")
    second = h.runtime.process_authoritative_event("EVT-DUP")

    assert second.status is InitialExecutionStatus.ALREADY_PROCESSED
    assert second.processed == first.processed
    assert len(h.incidents.list_correlation_views()) == 1


def test_concurrent_claim_loser_performs_no_domain_mutation(harnesses) -> None:
    h = harnesses([_event("EVT-RACE", "brute_force_detected", NOW, source_ip="203.0.113.8")])
    winner_claim = h.state.acquire_claim("EVT-RACE")

    with pytest.raises(RuntimeConcurrentClaimError):
        h.runtime.process_authoritative_event("EVT-RACE")

    assert h.state.resolve("EVT-RACE").claim == winner_claim
    assert h.incidents.event_has_incident_owner("EVT-RACE") is False


def test_claim_replacement_during_precheck_fences_domain_call(
    harnesses, monkeypatch
) -> None:
    h = harnesses([_event("EVT-FENCE", "brute_force_detected", NOW, source_ip="203.0.113.88")])
    original_read = h.shadows.get_shadow_by_event_id
    replacement = None

    def reclaim_during_precheck(event_id):
        nonlocal replacement
        resolved = h.state.resolve(event_id)
        if resolved.intent is not None and replacement is None:
            replacement = h.state.reclaim_claim(
                resolved.claim,
                ClaimAbandonmentProof(event_id, resolved.claim.claim_id, "worker proven dead"),
            )
        return original_read(event_id)

    monkeypatch.setattr(h.shadows, "get_shadow_by_event_id", reclaim_during_precheck)
    with pytest.raises(RuntimeClaimAuthorityError):
        h.runtime.process_authoritative_event("EVT-FENCE")

    resolved = h.state.resolve("EVT-FENCE")
    assert resolved.claim == replacement
    assert resolved.intent is not None
    assert resolved.processed is None
    assert h.incidents.event_has_incident_owner("EVT-FENCE") is False


@pytest.mark.parametrize(
    ("path", "first_event", "replay_event", "expected"),
    [
        (
            "CREATE",
            None,
            _event("EVT-CREATE-REPLAY", "brute_force_detected", NOW, source_ip="203.0.113.9"),
            TerminalOutcome.CREATED_INCIDENT,
        ),
        (
            "ATTACH",
            _event("EVT-SEED", "brute_force_detected", NOW, source_ip="203.0.113.10"),
            _event("EVT-ATTACH-REPLAY", "brute_force_detected", NOW + timedelta(seconds=10), source_ip="203.0.113.10"),
            TerminalOutcome.ATTACHED_TO_INCIDENT,
        ),
        (
            "SHADOW",
            None,
            _event("EVT-SHADOW-REPLAY", "general_log_anomaly", NOW),
            TerminalOutcome.SHADOWED,
        ),
    ],
)
def test_create_attach_and_shadow_receipt_replay(
    harnesses, monkeypatch, path, first_event, replay_event, expected
) -> None:
    events = ([first_event] if first_event is not None else []) + [replay_event]
    h = harnesses(events)
    if first_event is not None:
        h.runtime.process_authoritative_event(first_event["event_id"])

    original_finalize = h.state.finalize_processed
    monkeypatch.setattr(
        h.state,
        "finalize_processed",
        lambda _record, _claim=None: (_ for _ in ()).throw(RuntimeError("gap")),
    )
    with pytest.raises(RuntimeError, match="gap"):
        h.runtime.process_authoritative_event(replay_event["event_id"])
    unresolved = h.state.resolve(replay_event["event_id"])
    operation_id = unresolved.intent.operation_id
    assert operation_id == runtime_operation_id(
        "TERMINAL_CORRELATION", replay_event["event_id"]
    )

    monkeypatch.setattr(h.state, "finalize_processed", original_finalize)
    resumed = h.runtime.resume_authoritative_intent(
        replay_event["event_id"], unresolved.claim
    )
    assert resumed.processed.terminal_outcome is expected
    assert resumed.reconciled_existing_receipt is True
    if path == "SHADOW":
        assert h.shadows.get_operation_result(operation_id) is not None
    else:
        assert h.incidents.get_operation_result(operation_id) is not None


class _PostMutationShadowRace:
    def __init__(self, real_store, event_id: str) -> None:
        self.real_store = real_store
        self.event_id = event_id
        self.reads = 0

    def get_operation_result(self, operation_id):
        return self.real_store.get_operation_result(operation_id)

    def get_shadow_by_event_id(self, event_id):
        self.reads += 1
        if self.reads == 1:
            return None
        return ShadowRecord(
            "SHADOW-RACE",
            self.event_id,
            NOW,
            ShadowReason.INSUFFICIENT_OPERATIONAL_IDENTITY,
            ShadowReviewStatus.UNREVIEWED,
            "POLICY-GENERAL-LOG-ANOMALY",
            "1.0",
        )


def test_incident_shadow_post_mutation_contradiction_fails_closed(harnesses) -> None:
    event = _event("EVT-CONTRADICTION", "brute_force_detected", NOW, source_ip="203.0.113.11")
    h = harnesses([event])
    race = _PostMutationShadowRace(h.shadows, "EVT-CONTRADICTION")
    h.runtime._shadow_store = race

    with pytest.raises(RuntimeOwnershipIntegrityError):
        h.runtime.process_authoritative_event("EVT-CONTRADICTION")

    resolved = h.state.resolve("EVT-CONTRADICTION")
    assert resolved.intent is not None
    assert resolved.processed is None
    assert h.incidents.event_has_incident_owner("EVT-CONTRADICTION") is True


@pytest.mark.parametrize(
    "error_code",
    [IncidentErrorCode.INCIDENT_NOT_FOUND, IncidentErrorCode.INCIDENT_NOT_CORRELATION_OPEN],
)
def test_stale_candidate_or_lifecycle_refusal_never_forces_attach(
    harnesses, monkeypatch, error_code
) -> None:
    seed = _event("EVT-LIVE", "brute_force_detected", NOW, source_ip="203.0.113.12")
    attach = _event(
        "EVT-STALE", "brute_force_detected", NOW + timedelta(seconds=5), source_ip="203.0.113.12"
    )
    h = harnesses([seed, attach])
    h.runtime.process_authoritative_event("EVT-LIVE")

    def refuse(request):
        assert request.intent.decision_type is DecisionType.ATTACH_EXISTING
        raise IncidentDomainError(
            error_code,
            "concurrent Incident state invalidated candidate",
            operation_id=request.intent.operation_id,
            event_id=request.intent.event_id,
            incident_id=request.intent.target_incident_id,
        )

    monkeypatch.setattr(h.incident_manager, "apply_correlation_mutation", refuse)
    with pytest.raises(IncidentDomainError) as raised:
        h.runtime.process_authoritative_event("EVT-STALE")

    assert raised.value.code is error_code
    unresolved = h.state.resolve("EVT-STALE")
    assert unresolved.intent.decision_type is DecisionType.ATTACH_EXISTING
    assert unresolved.processed is None
    assert h.incidents.event_has_incident_owner("EVT-STALE") is False


def test_domain_failure_cannot_create_partial_terminalization(harnesses, monkeypatch) -> None:
    h = harnesses([_event("EVT-FAIL", "brute_force_detected", NOW, source_ip="203.0.113.13")])

    def fail(request):
        raise IncidentDomainError(
            IncidentErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE,
            "transient failure",
            operation_id=request.intent.operation_id,
            event_id=request.intent.event_id,
        )

    monkeypatch.setattr(h.incident_manager, "apply_correlation_mutation", fail)
    with pytest.raises(IncidentDomainError):
        h.runtime.process_authoritative_event("EVT-FAIL")

    resolved = h.state.resolve("EVT-FAIL")
    assert resolved.intent is not None
    assert resolved.processed is None
    assert h.incidents.get_operation_result(resolved.intent.operation_id) is None
    assert h.shadows.get_operation_result(resolved.intent.operation_id) is None
