from datetime import datetime, timedelta, timezone

import pytest

from _state_store_testkit import seed_block, seed_pending

from src.alert_correlation import (
    DEFAULT_POLICY_REGISTRY, CorrelationDecision, CorrelationErrorCode,
    CorrelationFamily, DecisionReasonCode, DecisionType, EvaluationPhase,
)
from src.alert_correlation.state import (
    BlockRetryStatus, BlockedCorrelationRecord, CorrelationPolicyKind,
    FailureKind, PendingGraceConfig, PendingPolicyUnavailableError,
    PendingReason, PendingStateService, ResolvedState, RetryDisposition,
    SqliteCorrelationStateStore, StateDomainErrorCode, StateDomainValidationError,
)


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def _decision(reason=DecisionReasonCode.NO_COMPATIBLE_CANDIDATE):
    return CorrelationDecision(
        DecisionType.ENTER_PENDING,
        "POLICY-BRUTE-FORCE-DETECTED",
        "1.0",
        CorrelationFamily.ATTACK_SOURCE,
        reason,
    )


def _service(store, resolver=DEFAULT_POLICY_REGISTRY.resolve_exact):
    return PendingStateService(store, resolver, PendingGraceConfig(30))


def _claim(store, event_id):
    claim = store.acquire_claim(event_id)
    assert claim is not None
    return claim


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (29, EvaluationPhase.PENDING_RECHECK),
        (30, EvaluationPhase.PENDING_EXPIRED),
        (31, EvaluationPhase.PENDING_EXPIRED),
    ],
)
def test_injected_authoritative_now_resolves_pending_phase(tmp_path, offset, expected):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    service = _service(store)
    service.accept_enter_pending("EVT-1", _decision(), now=NOW, claim=_claim(store, "EVT-1"))
    result = service.resolve_pending_phase("EVT-1", now=NOW + timedelta(seconds=offset))
    assert result.evaluation_phase is expected
    assert result.context.evaluation_phase is expected
    assert result.context.policy_id == "POLICY-BRUTE-FORCE-DETECTED"
    assert result.context.policy_version == "1.0"


def test_pending_reason_changes_preserve_all_pending_continuity_fields(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    service = _service(store)
    claim = _claim(store, "EVT-1")
    original = service.accept_enter_pending("EVT-1", _decision(), now=NOW, claim=claim)
    multiple = service.accept_enter_pending(
        "EVT-1", _decision(DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES),
        now=NOW + timedelta(seconds=5), claim=claim,
    )
    no_candidate = service.accept_enter_pending("EVT-1", _decision(), now=NOW + timedelta(seconds=10), claim=claim)
    assert multiple.pending_reason is PendingReason.MULTIPLE_COMPATIBLE_CANDIDATES
    assert no_candidate.pending_reason is PendingReason.NO_COMPATIBLE_CANDIDATE
    for field in ("entered_pending_at", "expires_at", "correlation_policy", "policy_id", "policy_version"):
        assert getattr(no_candidate, field) == getattr(original, field)


def test_pending_replay_and_restart_keep_original_absolute_expiry(tmp_path):
    database = tmp_path / "state.sqlite"
    store = SqliteCorrelationStateStore(database)
    service = _service(store)
    claim = _claim(store, "EVT-1")
    original = service.accept_enter_pending("EVT-1", _decision(), now=NOW, claim=claim)
    replay = service.accept_enter_pending("EVT-1", _decision(), now=NOW + timedelta(seconds=25), claim=claim)
    assert replay == original
    store.close()

    reopened = SqliteCorrelationStateStore(database)
    recovered = _service(reopened).resolve_pending_phase("EVT-1", now=NOW + timedelta(seconds=25))
    assert recovered.evaluation_phase is EvaluationPhase.PENDING_RECHECK
    assert recovered.pending.expires_at == original.expires_at
    assert _service(reopened).resolve_pending_phase("EVT-1", now=NOW + timedelta(seconds=31)).evaluation_phase is EvaluationPhase.PENDING_EXPIRED


def test_pending_policy_mismatch_fails_closed(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    # This direct persisted record represents integrity damage, not a normal entry path.
    from src.alert_correlation.state import ActivePendingRecord
    seed_pending(store, ActivePendingRecord("EVT-1", NOW, NOW + timedelta(seconds=30), CorrelationPolicyKind.WEAK_SUPPORTING_KNOWN, "POLICY-BRUTE-FORCE-DETECTED", "1.0", PendingReason.NO_COMPATIBLE_CANDIDATE))
    with pytest.raises(StateDomainValidationError):
        _service(store).resolve_pending_phase("EVT-1", now=NOW + timedelta(seconds=1))


def test_exact_policy_unavailable_creates_repair_required_block(tmp_path):
    database = tmp_path / "state.sqlite"
    store = SqliteCorrelationStateStore(database)
    claim = _claim(store, "EVT-1")
    _service(store).accept_enter_pending("EVT-1", _decision(), now=NOW, claim=claim)
    service = _service(store, lambda _policy_id, _version: None)
    with pytest.raises(PendingPolicyUnavailableError) as error:
        service.resolve_pending_phase("EVT-1", now=NOW + timedelta(seconds=1), claim=claim)
    assert error.value.blocked_record.retry_disposition is RetryDisposition.REPAIR_REQUIRED
    assert store.resolve("EVT-1").state is ResolvedState.ACTIVE_PENDING_BLOCKED
    store.close()

    reopened = SqliteCorrelationStateStore(database)
    restarted = _service(reopened, lambda _policy_id, _version: None)
    assert restarted.block_retry_status("EVT-1") is BlockRetryStatus.REPAIR_REQUIRED
    assert reopened.resolve("EVT-1").state is ResolvedState.ACTIVE_PENDING_BLOCKED


def test_blocked_only_and_pending_blocked_enter_pending_resolve_block_safely(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    block = BlockedCorrelationRecord("EVT-BLOCKED", FailureKind.CORRELATION_DOMAIN_FAILURE, CorrelationErrorCode.INVALID_INCIDENT_VIEW, EvaluationPhase.INITIAL, NOW, NOW, 1, RetryDisposition.RETRYABLE, "POLICY-BRUTE-FORCE-DETECTED", "1.0")
    seed_block(store, block)
    claim = _claim(store, "EVT-BLOCKED")
    pending = _service(store).accept_enter_pending("EVT-BLOCKED", _decision(), now=NOW, claim=claim)
    assert pending.event_id == "EVT-BLOCKED"
    assert store.resolve("EVT-BLOCKED").state is ResolvedState.ACTIVE_PENDING

    seed_block(store, BlockedCorrelationRecord("EVT-BLOCKED", FailureKind.CORRELATION_DOMAIN_FAILURE, CorrelationErrorCode.INVALID_INCIDENT_VIEW, EvaluationPhase.PENDING_RECHECK, NOW, NOW, 1, RetryDisposition.RETRYABLE, "POLICY-BRUTE-FORCE-DETECTED", "1.0"))
    assert store.resolve("EVT-BLOCKED").state is ResolvedState.ACTIVE_PENDING_BLOCKED
    updated = _service(store).accept_enter_pending("EVT-BLOCKED", _decision(DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES), now=NOW + timedelta(seconds=3), claim=claim)
    assert updated.pending_reason is PendingReason.MULTIPLE_COMPATIBLE_CANDIDATES
    assert updated.entered_pending_at == NOW
    assert store.resolve("EVT-BLOCKED").state is ResolvedState.ACTIVE_PENDING


def test_non_retryable_and_repair_required_blocks_are_not_automatically_advanced(tmp_path):
    database = tmp_path / "state.sqlite"
    store = SqliteCorrelationStateStore(database)
    non_retryable = BlockedCorrelationRecord("EVT-NON", FailureKind.STATE_DOMAIN_FAILURE, StateDomainErrorCode.MALFORMED_STATE_RECORD, None, NOW, NOW, 1, RetryDisposition.NON_RETRYABLE)
    repair_required = BlockedCorrelationRecord("EVT-REPAIR", FailureKind.STATE_DOMAIN_FAILURE, StateDomainErrorCode.MALFORMED_STATE_RECORD, None, NOW, NOW, 1, RetryDisposition.REPAIR_REQUIRED)
    seed_block(store, non_retryable)
    seed_block(store, repair_required)
    service = _service(store)
    assert service.block_retry_status("EVT-NON") is BlockRetryStatus.NON_RETRYABLE
    assert service.block_retry_status("EVT-REPAIR") is BlockRetryStatus.REPAIR_REQUIRED
    store.close()

    reopened = SqliteCorrelationStateStore(database)
    restarted = _service(reopened)
    assert restarted.block_retry_status("EVT-REPAIR") is BlockRetryStatus.REPAIR_REQUIRED
    assert reopened.resolve("EVT-REPAIR").state is ResolvedState.BLOCKED
