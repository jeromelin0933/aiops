from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from _state_store_testkit import seed_block, seed_pending

from src.alert_correlation import (
    AnchorStrength, AnchorTransition, CorrelationDecision, CorrelationFamily,
    DecisionReasonCode, DecisionType, NormalizedFingerprint,
)
from src.alert_correlation.state import (
    ActivePendingRecord, BlockedCorrelationRecord, ClaimAbandonmentProof,
    CorrelationMutationIntent, CorrelationPolicyKind, FailureKind, PendingReason,
    ProcessedCorrelationRecord, ResolvedState, RetryDisposition,
    SqliteCorrelationStateStore, StateDomainErrorCode, StateDomainValidationError,
    TerminalOutcome,
)


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
FINGERPRINT = NormalizedFingerprint("brute_force_detected", (("source_ip", "1.2.3.4"),))


def _decision(decision_type: DecisionType) -> CorrelationDecision:
    if decision_type is DecisionType.ATTACH_EXISTING:
        return CorrelationDecision(decision_type, "POLICY-BRUTE-FORCE-DETECTED", "1.0", CorrelationFamily.ATTACK_SOURCE, DecisionReasonCode.EXACT_STRONG_IDENTITY_MATCH, "INC-TARGET", FINGERPRINT, AnchorStrength.STRONG, AnchorTransition.NONE)
    if decision_type is DecisionType.CREATE_NEW:
        return CorrelationDecision(decision_type, "POLICY-BRUTE-FORCE-DETECTED", "1.0", CorrelationFamily.ATTACK_SOURCE, DecisionReasonCode.NO_COMPATIBLE_CANDIDATE, None, FINGERPRINT, AnchorStrength.STRONG, AnchorTransition.NONE)
    return CorrelationDecision(decision_type, "POLICY-GENERAL-LOG-ANOMALY", "1.0", CorrelationFamily.UNKNOWN, DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY)


def _intent(event_id="EVT-1", operation_id="OP-1", decision_type=DecisionType.CREATE_NEW):
    return CorrelationMutationIntent.from_decision(
        operation_id=operation_id, event_id=event_id,
        decision=_decision(decision_type), created_at=NOW,
    )


class DownstreamPortDouble:
    """Narrow test-only port; it neither creates an Incident nor persists a Shadow."""

    def __init__(self, *, succeed: bool, destination: str = "INC-CREATED") -> None:
        self.succeed = succeed
        self.destination = destination
        self.calls: list[tuple[str, str]] = []

    def execute(self, intent: CorrelationMutationIntent) -> ProcessedCorrelationRecord:
        self.calls.append((intent.event_id, intent.operation_id))
        if not self.succeed:
            raise RuntimeError("simulated downstream failure")
        if intent.intended_terminal_outcome is TerminalOutcome.SHADOWED:
            return ProcessedCorrelationRecord(intent.event_id, TerminalOutcome.SHADOWED, NOW, None, "SHADOW-1", intent.policy_id, intent.policy_version)
        incident_id = intent.target_incident_id or self.destination
        return ProcessedCorrelationRecord(intent.event_id, intent.intended_terminal_outcome, NOW, incident_id, None, intent.policy_id, intent.policy_version)


def test_same_event_concurrent_claim_has_one_winner(tmp_path):
    database = tmp_path / "state.sqlite"

    def acquire():
        store = SqliteCorrelationStateStore(database)
        try:
            return store.acquire_claim("EVT-1")
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _: acquire(), range(2)))
    assert sum(claim is not None for claim in claims) == 1


def test_losing_or_stale_claim_cannot_mutate_and_safe_reclaim_fences_old_executor(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    first = store.acquire_claim("EVT-1")
    assert first is not None and store.acquire_claim("EVT-1") is None
    with pytest.raises(StateDomainValidationError):
        store.begin_intent(_intent(), None)

    replacement = store.reclaim_claim(
        first, ClaimAbandonmentProof("EVT-1", first.claim_id, "operator confirmed abandoned worker"),
    )
    assert replacement.fencing_token > first.fencing_token
    with pytest.raises(StateDomainValidationError):
        store.begin_intent(_intent(), first)
    assert store.begin_intent(_intent(), replacement).operation_id == "OP-1"


def test_matching_intent_replay_is_idempotent_and_conflicting_intent_is_rejected(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    claim = store.acquire_claim("EVT-1")
    assert claim is not None
    intent = _intent()
    assert store.begin_intent(intent, claim) == intent
    assert store.begin_intent(intent, claim) == intent
    with pytest.raises(StateDomainValidationError) as error:
        store.begin_intent(_intent(operation_id="OP-OTHER"), claim)
    assert error.value.code is StateDomainErrorCode.TERMINAL_OWNERSHIP_CONFLICT


@pytest.mark.parametrize(
    ("decision_type", "outcome"),
    [
        (DecisionType.ATTACH_EXISTING, TerminalOutcome.ATTACHED_TO_INCIDENT),
        (DecisionType.CREATE_NEW, TerminalOutcome.CREATED_INCIDENT),
        (DecisionType.ROUTE_SHADOW, TerminalOutcome.SHADOWED),
    ],
)
def test_real_spec_006_decisions_keep_same_operation_identity(tmp_path, decision_type, outcome):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    claim = store.acquire_claim("EVT-1")
    assert claim is not None
    intent = _intent(decision_type=decision_type)
    store.begin_intent(intent, claim)
    assert intent.decision_type is decision_type
    assert intent.intended_terminal_outcome is outcome
    assert intent.operation_id == "OP-1"
    if decision_type is DecisionType.ATTACH_EXISTING:
        assert intent.target_incident_id == "INC-TARGET"
    else:
        assert intent.target_incident_id is None


def test_processed_only_after_simulated_downstream_success_and_atomic_finalization(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    pending = ActivePendingRecord("EVT-1", NOW, NOW.replace(second=30), CorrelationPolicyKind.STRONG_ANCHOR, "POLICY-BRUTE-FORCE-DETECTED", "1.0", PendingReason.NO_COMPATIBLE_CANDIDATE)
    blocked = BlockedCorrelationRecord("EVT-1", FailureKind.STATE_DOMAIN_FAILURE, StateDomainErrorCode.MALFORMED_STATE_RECORD, None, NOW, NOW, 1, RetryDisposition.REPAIR_REQUIRED)
    seed_pending(store, pending)
    seed_block(store, blocked)
    claim = store.acquire_claim("EVT-1")
    assert claim is not None
    intent = _intent()
    store.begin_intent(intent, claim)
    failed_port = DownstreamPortDouble(succeed=False)
    with pytest.raises(RuntimeError):
        failed_port.execute(intent)
    assert store.get_processed("EVT-1") is None
    assert store.resolve("EVT-1").state is ResolvedState.UNRESOLVED_MUTATION_INTENT

    succeeded_port = DownstreamPortDouble(succeed=True)
    processed = succeeded_port.execute(intent)
    store.finalize_processed(processed, claim)
    resolved = store.resolve("EVT-1")
    assert succeeded_port.calls == [("EVT-1", "OP-1")]
    assert resolved.state is ResolvedState.TERMINAL_PROCESSED
    assert resolved.processed == processed
    assert resolved.intent is None and resolved.pending is None and resolved.blocked is None


def test_matching_terminal_replay_is_noop_and_conflicting_destination_fails_closed(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    claim = store.acquire_claim("EVT-1")
    assert claim is not None
    intent = _intent(decision_type=DecisionType.ATTACH_EXISTING)
    store.begin_intent(intent, claim)
    processed = DownstreamPortDouble(succeed=True).execute(intent)
    store.finalize_processed(processed, claim)
    assert store.finalize_processed(processed) == processed
    conflict = ProcessedCorrelationRecord("EVT-1", TerminalOutcome.ATTACHED_TO_INCIDENT, NOW, "INC-OTHER", None, intent.policy_id, intent.policy_version)
    with pytest.raises(StateDomainValidationError) as error:
        store.finalize_processed(conflict)
    assert error.value.code is StateDomainErrorCode.TERMINAL_OWNERSHIP_CONFLICT


def test_attach_target_is_required_by_real_intent_contract():
    with pytest.raises(StateDomainValidationError):
        CorrelationMutationIntent(
            "OP-1", "EVT-1", TerminalOutcome.ATTACHED_TO_INCIDENT,
            DecisionType.ATTACH_EXISTING, "POLICY", "1.0",
            CorrelationFamily.ATTACK_SOURCE,
            DecisionReasonCode.EXACT_STRONG_IDENTITY_MATCH, None,
            FINGERPRINT, AnchorStrength.STRONG, AnchorTransition.NONE, NOW,
        )
