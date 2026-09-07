"""SPEC-007 Pending and Blocked state transitions, without runtime scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from ..contracts import (
    CorrelationErrorCode,
    CorrelationEvaluationError,
    CorrelationEvaluationFailure,
    CorrelationEvaluationContext,
    CorrelationDecision,
    DecisionType,
    EvaluationPhase,
)
from ..policy import CorrelationPolicy, EvidenceClass
from .contracts import (
    ActivePendingRecord,
    BlockedCorrelationRecord,
    CorrelationPolicyKind,
    FailureKind,
    PendingGraceConfig,
    PendingReason,
    RetryDisposition,
    StateDomainErrorCode,
    StateDomainValidationError,
)
from .sqlite_store import ProcessingClaim, ResolvedState, SqliteCorrelationStateStore


class BlockRetryStatus(str, Enum):
    EXPLICIT_RETRY_ALLOWED = "EXPLICIT_RETRY_ALLOWED"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    NON_RETRYABLE = "NON_RETRYABLE"


class PendingPolicyUnavailableError(StateDomainValidationError):
    """Historical exact policy is unavailable; retained Pending is blocked for repair."""

    def __init__(self, record: BlockedCorrelationRecord | None = None) -> None:
        super().__init__(
            StateDomainErrorCode.MALFORMED_STATE_RECORD,
            "exact Pending policy is unavailable; repair is required",
        )
        self.blocked_record = record


@dataclass(frozen=True, slots=True)
class PendingPhaseResolution:
    pending: ActivePendingRecord
    evaluation_phase: EvaluationPhase
    context: CorrelationEvaluationContext


class PendingStateService:
    """Consumes decisions and injected time; it never invokes SPEC-006 itself."""

    def __init__(
        self,
        store: SqliteCorrelationStateStore,
        policy_resolver,
        config: PendingGraceConfig = PendingGraceConfig(),
    ) -> None:
        if not isinstance(store, SqliteCorrelationStateStore):
            raise TypeError("store must be a SqliteCorrelationStateStore")
        if not callable(policy_resolver):
            raise TypeError("policy_resolver must be callable")
        if not isinstance(config, PendingGraceConfig):
            raise TypeError("config must be a PendingGraceConfig")
        self._store = store
        self._policy_resolver = policy_resolver
        self._config = config

    def accept_enter_pending(
        self,
        event_id: str,
        decision: CorrelationDecision,
        *,
        now: datetime,
        claim: ProcessingClaim | None,
    ) -> ActivePendingRecord:
        """Apply an externally-produced ENTER_PENDING Decision idempotently."""
        _aware_now(now)
        if not isinstance(decision, CorrelationDecision) or decision.decision_type is not DecisionType.ENTER_PENDING:
            raise StateDomainValidationError(
                StateDomainErrorCode.MUTATION_INTENT_CONFLICT,
                "only a SPEC-006 ENTER_PENDING Decision can activate Pending",
            )
        reason = _pending_reason(decision)
        policy_kind = self._policy_kind_or_fail(decision.policy_id, decision.policy_version)
        state = self._store.resolve(event_id)
        if state.state in {ResolvedState.TERMINAL_PROCESSED, ResolvedState.UNRESOLVED_MUTATION_INTENT}:
            raise StateDomainValidationError(
                StateDomainErrorCode.MUTATION_INTENT_CONFLICT,
                "terminal or unresolved Intent state cannot enter Pending",
            )
        if state.pending is not None:
            pending = state.pending
            self._validate_pending_policy(pending)
            updated = pending.with_pending_reason_from_decision(decision)
            return self._store.activate_pending(updated, claim)
        record = ActivePendingRecord(
            event_id=event_id,
            entered_pending_at=now,
            expires_at=now + timedelta(seconds=self._config.pending_grace_seconds),
            correlation_policy=policy_kind,
            policy_id=decision.policy_id,
            policy_version=decision.policy_version,
            pending_reason=reason,
        )
        return self._store.activate_pending(record, claim)

    def record_evaluation_failure(
        self,
        failure: CorrelationEvaluationFailure | CorrelationEvaluationError,
        evaluation_phase: EvaluationPhase,
        *,
        now: datetime,
        claim: ProcessingClaim | None,
    ) -> BlockedCorrelationRecord:
        """Persist a real SPEC-006 Failure without reinterpreting it.

        This is the sole normal-processing failure path: it preserves any
        active Pending continuity and delegates claim/fencing validation and
        the durable, approved error-code mapping to the SQLite store.
        """
        _aware_now(now)
        if isinstance(failure, CorrelationEvaluationFailure):
            error = failure.error
        elif isinstance(failure, CorrelationEvaluationError):
            error = failure
        else:
            raise TypeError("failure must be a SPEC-006 CorrelationEvaluationFailure or CorrelationEvaluationError")
        return self._store.record_evaluation_failure(
            error, evaluation_phase, now=now, claim=claim
        )

    def resolve_pending_phase(
        self, event_id: str, *, now: datetime, claim: ProcessingClaim | None = None
    ) -> PendingPhaseResolution:
        """Resolve one durable Pending record using supplied authoritative time only."""
        _aware_now(now)
        state = self._store.resolve(event_id)
        if state.pending is None:
            raise StateDomainValidationError(
                StateDomainErrorCode.MALFORMED_STATE_RECORD,
                "Pending phase requires an active Pending record",
            )
        pending = state.pending
        phase = (
            EvaluationPhase.PENDING_RECHECK
            if now < pending.expires_at
            else EvaluationPhase.PENDING_EXPIRED
        )
        try:
            self._validate_pending_policy(pending)
        except PendingPolicyUnavailableError:
            # This is a normal evaluation-origin mutation, so stale or
            # claimless callers cannot turn a Pending record into Pending+Block.
            block = self._store.record_evaluation_failure(
                CorrelationEvaluationError(
                    CorrelationErrorCode.POLICY_VERSION_UNAVAILABLE,
                    pending.event_id,
                    "",
                    pending.policy_id,
                    pending.policy_version,
                ),
                phase,
                now=now,
                claim=claim,
            )
            raise PendingPolicyUnavailableError(block) from None
        return PendingPhaseResolution(
            pending=pending,
            evaluation_phase=phase,
            context=CorrelationEvaluationContext(
                evaluation_phase=phase,
                policy_id=pending.policy_id,
                policy_version=pending.policy_version,
            ),
        )

    def block_retry_status(self, event_id: str) -> BlockRetryStatus | None:
        """Describe disposition only; this method neither retries nor repairs."""
        block = self._store.resolve(event_id).blocked
        if block is None:
            return None
        return {
            RetryDisposition.RETRYABLE: BlockRetryStatus.EXPLICIT_RETRY_ALLOWED,
            RetryDisposition.REPAIR_REQUIRED: BlockRetryStatus.REPAIR_REQUIRED,
            RetryDisposition.NON_RETRYABLE: BlockRetryStatus.NON_RETRYABLE,
        }[block.retry_disposition]

    def _policy_kind_or_fail(self, policy_id: str, policy_version: str) -> CorrelationPolicyKind:
        policy = self._policy_resolver(policy_id, policy_version)
        if not isinstance(policy, CorrelationPolicy):
            raise PendingPolicyUnavailableError()
        return _kind_for_policy(policy)

    def _validate_pending_policy(self, pending: ActivePendingRecord) -> None:
        policy = self._policy_resolver(pending.policy_id, pending.policy_version)
        if not isinstance(policy, CorrelationPolicy):
            raise PendingPolicyUnavailableError()
        if _kind_for_policy(policy) is not pending.correlation_policy:
            raise StateDomainValidationError(
                StateDomainErrorCode.MALFORMED_STATE_RECORD,
                "Pending correlation_policy mismatches its exact SPEC-006 policy",
            )

def _kind_for_policy(policy: CorrelationPolicy) -> CorrelationPolicyKind:
    if policy.evidence_class is EvidenceClass.STRONG:
        return CorrelationPolicyKind.STRONG_ANCHOR
    if policy.evidence_class is EvidenceClass.KNOWN_WEAK:
        return CorrelationPolicyKind.WEAK_SUPPORTING_KNOWN
    raise StateDomainValidationError(
        StateDomainErrorCode.MALFORMED_STATE_RECORD,
        "UNKNOWN SPEC-006 policy cannot create ActivePendingRecord",
    )


def _pending_reason(decision: CorrelationDecision) -> PendingReason:
    try:
        return PendingReason(decision.reason_code.value)
    except ValueError as exc:
        raise StateDomainValidationError(
            StateDomainErrorCode.MALFORMED_STATE_RECORD,
            "ENTER_PENDING Decision has an invalid Pending reason",
        ) from exc


def _aware_now(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise StateDomainValidationError(
            StateDomainErrorCode.MALFORMED_STATE_RECORD,
            "authoritative now must be a timezone-aware datetime",
        )
