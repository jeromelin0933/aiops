"""Immutable SPEC-007 logical records and fail-closed validation helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from math import isfinite

from ..contracts import (
    AnchorStrength,
    AnchorTransition,
    CorrelationDecision,
    CorrelationErrorCode,
    CorrelationFamily,
    DecisionReasonCode,
    DecisionType,
    EvaluationPhase,
    NormalizedFingerprint,
)
from ..policy import CorrelationPolicy, EvidenceClass


class PendingReason(str, Enum):
    NO_COMPATIBLE_CANDIDATE = "NO_COMPATIBLE_CANDIDATE"
    MULTIPLE_COMPATIBLE_CANDIDATES = "MULTIPLE_COMPATIBLE_CANDIDATES"


class CorrelationPolicyKind(str, Enum):
    STRONG_ANCHOR = "STRONG_ANCHOR"
    WEAK_SUPPORTING_KNOWN = "WEAK_SUPPORTING_KNOWN"


class TerminalOutcome(str, Enum):
    ATTACHED_TO_INCIDENT = "ATTACHED_TO_INCIDENT"
    CREATED_INCIDENT = "CREATED_INCIDENT"
    SHADOWED = "SHADOWED"


class FailureKind(str, Enum):
    CORRELATION_DOMAIN_FAILURE = "CORRELATION_DOMAIN_FAILURE"
    STATE_DOMAIN_FAILURE = "STATE_DOMAIN_FAILURE"
    INTERNAL_FAILURE = "INTERNAL_FAILURE"


class RetryDisposition(str, Enum):
    RETRYABLE = "RETRYABLE"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    NON_RETRYABLE = "NON_RETRYABLE"


class StateDomainErrorCode(str, Enum):
    MALFORMED_STATE_RECORD = "MALFORMED_STATE_RECORD"
    DANGLING_EVENT_REFERENCE = "DANGLING_EVENT_REFERENCE"
    DANGLING_INCIDENT_REFERENCE = "DANGLING_INCIDENT_REFERENCE"
    DANGLING_SHADOW_REFERENCE = "DANGLING_SHADOW_REFERENCE"
    TERMINAL_OWNERSHIP_CONFLICT = "TERMINAL_OWNERSHIP_CONFLICT"
    MUTATION_INTENT_CONFLICT = "MUTATION_INTENT_CONFLICT"
    UNSUPPORTED_STATE_VERSION = "UNSUPPORTED_STATE_VERSION"
    STORE_INTEGRITY_FAILURE = "STORE_INTEGRITY_FAILURE"


class StateDomainValidationError(ValueError):
    """A malformed or contradictory SPEC-007 record; callers must fail closed."""

    def __init__(self, code: StateDomainErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> None:
    raise StateDomainValidationError(StateDomainErrorCode.MALFORMED_STATE_RECORD, message)


def _reference(value: object, field_name: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{field_name} must be a non-empty reference without surrounding whitespace")


def _policy_identity(policy_id: object, policy_version: object, *, nullable: bool = False) -> None:
    if nullable and policy_id is None and policy_version is None:
        return
    for field_name, value in (("policy_id", policy_id), ("policy_version", policy_version)):
        if not isinstance(value, str) or not value or value != value.strip():
            _invalid(f"{field_name} must be a non-empty string without surrounding whitespace")


def _aware_timestamp(value: object, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid(f"{field_name} must be a timezone-aware datetime")


@dataclass(frozen=True, slots=True)
class PendingGraceConfig:
    pending_grace_seconds: float = 30.0

    def __post_init__(self) -> None:
        value = self.pending_grace_seconds
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _invalid("pending_grace_seconds must be a finite positive number")
        value = float(value)
        if not isfinite(value) or value <= 0:
            _invalid("pending_grace_seconds must be a finite positive number")
        object.__setattr__(self, "pending_grace_seconds", value)


@dataclass(frozen=True, slots=True)
class ActivePendingRecord:
    event_id: str
    entered_pending_at: datetime
    expires_at: datetime
    correlation_policy: CorrelationPolicyKind
    policy_id: str
    policy_version: str
    pending_reason: PendingReason

    def __post_init__(self) -> None:
        _reference(self.event_id, "event_id")
        _aware_timestamp(self.entered_pending_at, "entered_pending_at")
        _aware_timestamp(self.expires_at, "expires_at")
        if self.expires_at <= self.entered_pending_at:
            _invalid("expires_at must be later than entered_pending_at")
        if not isinstance(self.correlation_policy, CorrelationPolicyKind):
            _invalid("correlation_policy must be a CorrelationPolicyKind")
        _policy_identity(self.policy_id, self.policy_version)
        if not isinstance(self.pending_reason, PendingReason):
            _invalid("pending_reason must be a PendingReason")

    def with_pending_reason_from_decision(
        self, decision: CorrelationDecision
    ) -> "ActivePendingRecord":
        """Return the sole permitted Pending update, preserving immutable fields."""
        _pending_reason_from_decision(decision, self.policy_id, self.policy_version)
        return replace(self, pending_reason=PendingReason(decision.reason_code.value))


PolicyResolver = Callable[[str, str], CorrelationPolicy | None]


def validate_active_pending_record(
    record: ActivePendingRecord, policy_resolver: PolicyResolver
) -> None:
    """Verify the persisted Pending evidence kind against its exact SPEC-006 policy."""
    if not isinstance(record, ActivePendingRecord):
        _invalid("record must be an ActivePendingRecord")
    if not callable(policy_resolver):
        _invalid("policy_resolver must be callable")
    policy = policy_resolver(record.policy_id, record.policy_version)
    if not isinstance(policy, CorrelationPolicy):
        raise StateDomainValidationError(
            StateDomainErrorCode.MALFORMED_STATE_RECORD,
            "exact Pending policy is unavailable or malformed",
        )
    expected = {
        EvidenceClass.STRONG: CorrelationPolicyKind.STRONG_ANCHOR,
        EvidenceClass.KNOWN_WEAK: CorrelationPolicyKind.WEAK_SUPPORTING_KNOWN,
    }.get(policy.evidence_class)
    if expected is None or record.correlation_policy is not expected:
        raise StateDomainValidationError(
            StateDomainErrorCode.MALFORMED_STATE_RECORD,
            "Pending correlation_policy does not match exact policy evidence class",
        )


@dataclass(frozen=True, slots=True)
class ProcessedCorrelationRecord:
    event_id: str
    terminal_outcome: TerminalOutcome
    resolved_at: datetime
    incident_id: str | None
    shadow_ref: str | None
    policy_id: str
    policy_version: str

    def __post_init__(self) -> None:
        _reference(self.event_id, "event_id")
        if not isinstance(self.terminal_outcome, TerminalOutcome):
            _invalid("terminal_outcome must be a TerminalOutcome")
        _aware_timestamp(self.resolved_at, "resolved_at")
        _policy_identity(self.policy_id, self.policy_version)
        if self.terminal_outcome is TerminalOutcome.SHADOWED:
            _reference(self.shadow_ref, "shadow_ref")
            if self.incident_id is not None:
                _invalid("SHADOWED requires incident_id=None")
        else:
            _reference(self.incident_id, "incident_id")
            if self.shadow_ref is not None:
                _invalid("Incident outcomes require shadow_ref=None")


@dataclass(frozen=True, slots=True)
class BlockedCorrelationRecord:
    event_id: str
    failure_kind: FailureKind
    failure_code: CorrelationErrorCode | StateDomainErrorCode | str
    evaluation_phase: EvaluationPhase | None
    first_failed_at: datetime
    last_failed_at: datetime
    attempt_count: int
    retry_disposition: RetryDisposition
    policy_id: str | None = None
    policy_version: str | None = None
    field_path: str | None = None
    incident_id: str | None = None

    def __post_init__(self) -> None:
        _reference(self.event_id, "event_id")
        if not isinstance(self.failure_kind, FailureKind):
            _invalid("failure_kind must be a FailureKind")
        _aware_timestamp(self.first_failed_at, "first_failed_at")
        _aware_timestamp(self.last_failed_at, "last_failed_at")
        if self.last_failed_at < self.first_failed_at:
            _invalid("last_failed_at must not precede first_failed_at")
        if isinstance(self.attempt_count, bool) or not isinstance(self.attempt_count, int) or self.attempt_count < 1:
            _invalid("attempt_count must be a positive integer")
        if not isinstance(self.retry_disposition, RetryDisposition):
            _invalid("retry_disposition must be a RetryDisposition")
        _policy_identity(self.policy_id, self.policy_version, nullable=True)
        _reference(self.field_path, "field_path", nullable=True)
        _reference(self.incident_id, "incident_id", nullable=True)
        if self.evaluation_phase is not None and not isinstance(self.evaluation_phase, EvaluationPhase):
            _invalid("evaluation_phase must be an EvaluationPhase or None")
        if self.failure_kind is FailureKind.CORRELATION_DOMAIN_FAILURE:
            if not isinstance(self.failure_code, CorrelationErrorCode):
                _invalid("correlation-domain Block requires a CorrelationErrorCode")
            if self.evaluation_phase is None:
                _invalid("evaluation-origin Block requires its real EvaluationPhase")
        elif self.failure_kind is FailureKind.STATE_DOMAIN_FAILURE:
            if not isinstance(self.failure_code, StateDomainErrorCode):
                _invalid("state-domain Block requires a StateDomainErrorCode")
            if self.evaluation_phase is not None:
                _invalid("state-domain Block must not invent an EvaluationPhase")
        elif self.failure_code != "INTERNAL_FAILURE" or self.evaluation_phase is not None:
            _invalid("internal Block requires INTERNAL_FAILURE and evaluation_phase=None")


def terminal_outcome_for_decision(decision_type: DecisionType) -> TerminalOutcome:
    if not isinstance(decision_type, DecisionType):
        _invalid("decision_type must be a SPEC-006 DecisionType")
    outcomes = {
        DecisionType.ATTACH_EXISTING: TerminalOutcome.ATTACHED_TO_INCIDENT,
        DecisionType.CREATE_NEW: TerminalOutcome.CREATED_INCIDENT,
        DecisionType.ROUTE_SHADOW: TerminalOutcome.SHADOWED,
    }
    try:
        return outcomes[decision_type]
    except KeyError as exc:
        raise StateDomainValidationError(
            StateDomainErrorCode.MUTATION_INTENT_CONFLICT,
            "ENTER_PENDING cannot create a MutationIntent",
        ) from exc


def _pending_reason_from_decision(
    decision: CorrelationDecision, policy_id: str, policy_version: str
) -> PendingReason:
    if not isinstance(decision, CorrelationDecision) or decision.decision_type is not DecisionType.ENTER_PENDING:
        raise StateDomainValidationError(
            StateDomainErrorCode.MUTATION_INTENT_CONFLICT,
            "only a SPEC-006 ENTER_PENDING Decision may update Pending reason",
        )
    if (decision.policy_id, decision.policy_version) != (policy_id, policy_version):
        raise StateDomainValidationError(
            StateDomainErrorCode.MUTATION_INTENT_CONFLICT,
            "ENTER_PENDING Decision policy identity must match active Pending",
        )
    try:
        return PendingReason(decision.reason_code.value)
    except ValueError as exc:
        raise StateDomainValidationError(
            StateDomainErrorCode.MUTATION_INTENT_CONFLICT,
            "ENTER_PENDING Decision has an invalid Pending reason",
        ) from exc


@dataclass(frozen=True, slots=True)
class CorrelationMutationIntent:
    operation_id: str
    event_id: str
    intended_terminal_outcome: TerminalOutcome
    decision_type: DecisionType
    policy_id: str
    policy_version: str
    correlation_family: CorrelationFamily
    reason_code: DecisionReasonCode
    target_incident_id: str | None
    normalized_fingerprint: NormalizedFingerprint | None
    anchor_strength: AnchorStrength | None
    anchor_transition: AnchorTransition
    created_at: datetime

    def __post_init__(self) -> None:
        _reference(self.operation_id, "operation_id")
        _reference(self.event_id, "event_id")
        if not isinstance(self.intended_terminal_outcome, TerminalOutcome):
            _invalid("intended_terminal_outcome must be a TerminalOutcome")
        if not isinstance(self.decision_type, DecisionType):
            _invalid("decision_type must be a SPEC-006 DecisionType")
        if terminal_outcome_for_decision(self.decision_type) is not self.intended_terminal_outcome:
            _invalid("MutationIntent outcome must match DecisionType")
        _policy_identity(self.policy_id, self.policy_version)
        if not isinstance(self.correlation_family, CorrelationFamily):
            _invalid("correlation_family must be a SPEC-006 CorrelationFamily")
        if not isinstance(self.reason_code, DecisionReasonCode):
            _invalid("reason_code must be a SPEC-006 DecisionReasonCode")
        _reference(self.target_incident_id, "target_incident_id", nullable=True)
        if self.decision_type is DecisionType.ATTACH_EXISTING:
            _reference(self.target_incident_id, "target_incident_id")
        elif self.target_incident_id is not None:
            _invalid("only ATTACH_EXISTING may contain target_incident_id")
        if self.normalized_fingerprint is not None and not isinstance(self.normalized_fingerprint, NormalizedFingerprint):
            _invalid("normalized_fingerprint must be a SPEC-006 NormalizedFingerprint or None")
        if self.anchor_strength is not None and not isinstance(self.anchor_strength, AnchorStrength):
            _invalid("anchor_strength must be a SPEC-006 AnchorStrength or None")
        if not isinstance(self.anchor_transition, AnchorTransition):
            _invalid("anchor_transition must be a SPEC-006 AnchorTransition")
        _aware_timestamp(self.created_at, "created_at")

    @classmethod
    def from_decision(
        cls, *, operation_id: str, event_id: str, decision: CorrelationDecision, created_at: datetime
    ) -> "CorrelationMutationIntent":
        if not isinstance(decision, CorrelationDecision):
            _invalid("decision must be a SPEC-006 CorrelationDecision")
        return cls(
            operation_id=operation_id,
            event_id=event_id,
            intended_terminal_outcome=terminal_outcome_for_decision(decision.decision_type),
            decision_type=decision.decision_type,
            policy_id=decision.policy_id,
            policy_version=decision.policy_version,
            correlation_family=decision.correlation_family,
            reason_code=decision.reason_code,
            target_incident_id=decision.target_incident_id,
            normalized_fingerprint=decision.normalized_fingerprint,
            anchor_strength=decision.anchor_strength,
            anchor_transition=decision.anchor_transition,
            created_at=created_at,
        )
