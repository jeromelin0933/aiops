"""SPEC-007 narrow-closure coverage for runtime claim and Failure handling."""

from datetime import datetime, timedelta, timezone

import pytest

from src.alert_correlation import (
    DEFAULT_POLICY_REGISTRY, CorrelationDecision, CorrelationErrorCode,
    CorrelationEvaluationError, CorrelationEvaluationFailure, CorrelationFamily,
    DecisionReasonCode, DecisionType, EvaluationPhase,
)
from src.alert_correlation.state import (
    ClaimAbandonmentProof, PendingGraceConfig, PendingReason,
    PendingStateService, ResolvedState, RetryDisposition,
    SqliteCorrelationStateStore, StateDomainValidationError,
)


NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


def _service(store):
    return PendingStateService(store, DEFAULT_POLICY_REGISTRY.resolve_exact, PendingGraceConfig(30))


def _decision(reason=DecisionReasonCode.NO_COMPATIBLE_CANDIDATE):
    return CorrelationDecision(
        DecisionType.ENTER_PENDING, "POLICY-BRUTE-FORCE-DETECTED", "1.0",
        CorrelationFamily.ATTACK_SOURCE, reason,
    )


def _claim(store, event_id):
    claim = store.acquire_claim(event_id)
    assert claim is not None
    return claim


def _failure(event_id, code, *, policy_id="POLICY-BRUTE-FORCE-DETECTED", policy_version="1.0"):
    return CorrelationEvaluationFailure(CorrelationEvaluationError(
        code, event_id, "brute_force_detected", policy_id, policy_version,
    ))


def test_store_public_surface_exposes_no_claimless_pending_or_block_mutators():
    """Normal production writes have no bypass around claim/fencing validation."""
    assert not hasattr(SqliteCorrelationStateStore, "put_pending")
    assert not hasattr(SqliteCorrelationStateStore, "put_block")


def test_evaluation_block_requires_current_claim_and_fences_stale_holder(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    service = _service(store)
    failure = _failure("EVT-1", CorrelationErrorCode.INVALID_INCIDENT_VIEW)

    with pytest.raises(StateDomainValidationError):
        service.record_evaluation_failure(failure, EvaluationPhase.INITIAL, now=NOW, claim=None)
    assert store.resolve("EVT-1").state is ResolvedState.UNSEEN

    first = _claim(store, "EVT-1")
    current = store.reclaim_claim(first, ClaimAbandonmentProof("EVT-1", first.claim_id, "worker stopped"))
    with pytest.raises(StateDomainValidationError):
        service.record_evaluation_failure(failure, EvaluationPhase.INITIAL, now=NOW, claim=first)
    assert store.resolve("EVT-1").state is ResolvedState.UNSEEN

    block = service.record_evaluation_failure(
        failure, EvaluationPhase.INITIAL, now=NOW, claim=current,
    )
    assert block.event_id == "EVT-1"
    assert store.resolve("EVT-1").state is ResolvedState.BLOCKED


def test_initial_pending_requires_current_claim_and_fences_stale_holder(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    service = _service(store)
    with pytest.raises(StateDomainValidationError):
        service.accept_enter_pending("EVT-1", _decision(), now=NOW, claim=None)

    first = _claim(store, "EVT-1")
    current = store.reclaim_claim(first, ClaimAbandonmentProof("EVT-1", first.claim_id, "worker stopped"))
    with pytest.raises(StateDomainValidationError):
        service.accept_enter_pending("EVT-1", _decision(), now=NOW, claim=first)
    assert service.accept_enter_pending("EVT-1", _decision(), now=NOW, claim=current).event_id == "EVT-1"


def test_pending_reason_and_block_mutations_require_current_claim_after_reclaim(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    service = _service(store)
    first = _claim(store, "EVT-1")
    original = service.accept_enter_pending("EVT-1", _decision(), now=NOW, claim=first)
    current = store.reclaim_claim(first, ClaimAbandonmentProof("EVT-1", first.claim_id, "worker stopped"))

    with pytest.raises(StateDomainValidationError):
        service.accept_enter_pending(
            "EVT-1", _decision(DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES),
            now=NOW + timedelta(seconds=1), claim=first,
        )
    with pytest.raises(StateDomainValidationError):
        service.record_evaluation_failure(
            _failure("EVT-1", CorrelationErrorCode.INVALID_INCIDENT_VIEW),
            EvaluationPhase.PENDING_RECHECK, now=NOW + timedelta(seconds=1), claim=first,
        )

    updated = service.accept_enter_pending(
        "EVT-1", _decision(DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES),
        now=NOW + timedelta(seconds=2), claim=current,
    )
    block = service.record_evaluation_failure(
        _failure("EVT-1", CorrelationErrorCode.INVALID_INCIDENT_VIEW),
        EvaluationPhase.PENDING_RECHECK, now=NOW + timedelta(seconds=3), claim=current,
    )
    assert updated.pending_reason is PendingReason.MULTIPLE_COMPATIBLE_CANDIDATES
    assert updated.entered_pending_at == original.entered_pending_at
    assert block.attempt_count == 1
    assert store.resolve("EVT-1").state is ResolvedState.ACTIVE_PENDING_BLOCKED


@pytest.mark.parametrize(
    ("code", "disposition"),
    [
        (CorrelationErrorCode.INVALID_EVENT_ENVELOPE, RetryDisposition.NON_RETRYABLE),
        (CorrelationErrorCode.MISSING_REQUIRED_IDENTITY, RetryDisposition.NON_RETRYABLE),
        (CorrelationErrorCode.INVALID_IDENTITY_VALUE, RetryDisposition.NON_RETRYABLE),
        (CorrelationErrorCode.POLICY_NOT_REGISTERED, RetryDisposition.REPAIR_REQUIRED),
        (CorrelationErrorCode.POLICY_VERSION_UNAVAILABLE, RetryDisposition.REPAIR_REQUIRED),
        (CorrelationErrorCode.INVALID_INCIDENT_VIEW, RetryDisposition.REPAIR_REQUIRED),
        (CorrelationErrorCode.INCONSISTENT_CORRELATION_CONTEXT, RetryDisposition.REPAIR_REQUIRED),
    ],
)
def test_real_spec_006_failures_durably_map_to_approved_disposition(tmp_path, code, disposition):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    service = _service(store)
    event_id = f"EVT-{code.value}"
    block = service.record_evaluation_failure(
        _failure(event_id, code), EvaluationPhase.INITIAL, now=NOW, claim=_claim(store, event_id),
    )
    resolved = store.resolve(event_id)
    assert block.retry_disposition is disposition
    assert resolved.state is ResolvedState.BLOCKED
    assert resolved.pending is None and resolved.processed is None


def test_pending_failure_preserves_continuity_and_repeated_failure_is_single_block(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    service = _service(store)
    claim = _claim(store, "EVT-1")
    pending = service.accept_enter_pending("EVT-1", _decision(), now=NOW, claim=claim)
    first = service.record_evaluation_failure(
        _failure("EVT-1", CorrelationErrorCode.POLICY_VERSION_UNAVAILABLE, policy_version="99.0"),
        EvaluationPhase.PENDING_RECHECK, now=NOW + timedelta(seconds=1), claim=claim,
    )
    second = service.record_evaluation_failure(
        _failure("EVT-1", CorrelationErrorCode.POLICY_VERSION_UNAVAILABLE, policy_version="99.0"),
        EvaluationPhase.PENDING_RECHECK, now=NOW + timedelta(seconds=2), claim=claim,
    )
    resolved = store.resolve("EVT-1")
    assert resolved.pending == pending
    assert second.first_failed_at == first.first_failed_at
    assert second.last_failed_at > first.last_failed_at
    assert second.attempt_count == first.attempt_count + 1
    assert second.policy_version == "99.0"  # never falls back to latest.
    assert resolved.blocked == second and resolved.processed is None


def test_safe_enter_pending_resolution_of_block_remains_claim_fenced(tmp_path):
    store = SqliteCorrelationStateStore(tmp_path / "state.sqlite")
    service = _service(store)
    first = _claim(store, "EVT-1")
    service.record_evaluation_failure(
        _failure("EVT-1", CorrelationErrorCode.INVALID_INCIDENT_VIEW),
        EvaluationPhase.INITIAL, now=NOW, claim=first,
    )
    current = store.reclaim_claim(first, ClaimAbandonmentProof("EVT-1", first.claim_id, "worker stopped"))
    with pytest.raises(StateDomainValidationError):
        service.accept_enter_pending("EVT-1", _decision(), now=NOW + timedelta(seconds=1), claim=first)
    service.accept_enter_pending("EVT-1", _decision(), now=NOW + timedelta(seconds=1), claim=current)
    assert store.resolve("EVT-1").state is ResolvedState.ACTIVE_PENDING
