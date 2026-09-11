"""SPEC-008 Incident-domain logical contracts.

This module deliberately depends on the implemented SPEC-006 and SPEC-007
types.  It does not define another correlation decision, fingerprint, intent,
or Runtime Event model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import TypeAlias

from src.alert_correlation import (
    AnchorStrength,
    AnchorTransition,
    CorrelationDecision,
    CorrelationFamily,
    DecisionReasonCode,
    DecisionType,
    NormalizedFingerprint,
)
from src.alert_correlation.state import (
    CorrelationMutationIntent,
    RetryDisposition,
    TerminalOutcome,
)


class IncidentStatus(str, Enum):
    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    CLOSED = "CLOSED"


CORRELATION_OPEN_STATUSES = frozenset(
    {IncidentStatus.OPEN, IncidentStatus.ASSIGNED, IncidentStatus.IN_PROGRESS}
)
CORRELATION_CLOSED_STATUSES = frozenset(
    {IncidentStatus.AWAITING_REVIEW, IncidentStatus.CLOSED}
)


class IncidentSeverity(IntEnum):
    """The closed, comparable SPEC-008 severity order."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    def __str__(self) -> str:
        return self.name


RCA_INITIAL_STATUS = "PENDING"


class IncidentAuditAction(str, Enum):
    INCIDENT_CREATED = "INCIDENT_CREATED"
    CORRELATION_ATTACHED = "CORRELATION_ATTACHED"


class IncidentAuditEffect(str, Enum):
    EVENT_ATTACHED = "EVENT_ATTACHED"
    SEVERITY_ESCALATED = "SEVERITY_ESCALATED"
    ANCHOR_PROMOTED = "ANCHOR_PROMOTED"


class IncidentMutationCompletion(str, Enum):
    SUCCEEDED = "SUCCEEDED"


class IncidentErrorCode(str, Enum):
    INVALID_INCIDENT_MUTATION = "INVALID_INCIDENT_MUTATION"
    INCIDENT_NOT_FOUND = "INCIDENT_NOT_FOUND"
    INCIDENT_NOT_CORRELATION_OPEN = "INCIDENT_NOT_CORRELATION_OPEN"
    EVENT_OWNERSHIP_CONFLICT = "EVENT_OWNERSHIP_CONFLICT"
    MUTATION_RECEIPT_CONFLICT = "MUTATION_RECEIPT_CONFLICT"
    STALE_CORRELATION_EVENT_TIME = "STALE_CORRELATION_EVENT_TIME"
    CORRELATION_CONTEXT_CONFLICT = "CORRELATION_CONTEXT_CONFLICT"
    INVALID_ANCHOR_PROMOTION = "INVALID_ANCHOR_PROMOTION"
    MALFORMED_INCIDENT_RECORD = "MALFORMED_INCIDENT_RECORD"
    UNSUPPORTED_INCIDENT_STATE_VERSION = "UNSUPPORTED_INCIDENT_STATE_VERSION"
    INCIDENT_STORE_INTEGRITY_FAILURE = "INCIDENT_STORE_INTEGRITY_FAILURE"
    TRANSIENT_INCIDENT_STORE_FAILURE = "TRANSIENT_INCIDENT_STORE_FAILURE"


INCIDENT_ERROR_DISPOSITIONS: Mapping[IncidentErrorCode, RetryDisposition] = (
    MappingProxyType(
        {
            IncidentErrorCode.INVALID_INCIDENT_MUTATION: RetryDisposition.NON_RETRYABLE,
            IncidentErrorCode.INCIDENT_NOT_FOUND: RetryDisposition.REPAIR_REQUIRED,
            IncidentErrorCode.INCIDENT_NOT_CORRELATION_OPEN: RetryDisposition.REPAIR_REQUIRED,
            IncidentErrorCode.EVENT_OWNERSHIP_CONFLICT: RetryDisposition.REPAIR_REQUIRED,
            IncidentErrorCode.MUTATION_RECEIPT_CONFLICT: RetryDisposition.REPAIR_REQUIRED,
            IncidentErrorCode.STALE_CORRELATION_EVENT_TIME: RetryDisposition.REPAIR_REQUIRED,
            IncidentErrorCode.CORRELATION_CONTEXT_CONFLICT: RetryDisposition.REPAIR_REQUIRED,
            IncidentErrorCode.INVALID_ANCHOR_PROMOTION: RetryDisposition.REPAIR_REQUIRED,
            IncidentErrorCode.MALFORMED_INCIDENT_RECORD: RetryDisposition.REPAIR_REQUIRED,
            IncidentErrorCode.UNSUPPORTED_INCIDENT_STATE_VERSION: RetryDisposition.REPAIR_REQUIRED,
            IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE: RetryDisposition.REPAIR_REQUIRED,
            IncidentErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE: RetryDisposition.RETRYABLE,
        }
    )
)


class IncidentDomainError(ValueError):
    """A typed, safe SPEC-008 failure."""

    def __init__(
        self,
        code: IncidentErrorCode,
        message: str,
        *,
        operation_id: str | None = None,
        event_id: str | None = None,
        incident_id: str | None = None,
        field_path: str | None = None,
    ) -> None:
        if not isinstance(code, IncidentErrorCode):
            raise TypeError("code must be an IncidentErrorCode")
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        super().__init__(message)
        self.code = code
        self.retry_disposition = INCIDENT_ERROR_DISPOSITIONS[code]
        self.operation_id = operation_id
        self.event_id = event_id
        self.incident_id = incident_id
        self.field_path = field_path


def _fail(code: IncidentErrorCode, message: str, **details: str | None) -> None:
    raise IncidentDomainError(code, message, **details)


def _reference(
    value: object,
    field_name: str,
    *,
    nullable: bool = False,
    code: IncidentErrorCode = IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(code, f"{field_name} must be a non-empty reference without surrounding whitespace", field_path=field_name)


def _utc_datetime(
    value: object,
    field_name: str,
    *,
    code: IncidentErrorCode = IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail(code, f"{field_name} must be a timezone-aware datetime", field_path=field_name)
    return value.astimezone(timezone.utc)


def _event_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        _fail(
            IncidentErrorCode.INVALID_INCIDENT_MUTATION,
            "event.detected_at must be an ISO 8601 UTC string",
            field_path="event.detected_at",
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(
            IncidentErrorCode.INVALID_INCIDENT_MUTATION,
            "event.detected_at must be an ISO 8601 UTC string",
            field_path="event.detected_at",
        )
    if parsed.utcoffset().total_seconds() != 0:
        _fail(
            IncidentErrorCode.INVALID_INCIDENT_MUTATION,
            "event.detected_at must use UTC",
            field_path="event.detected_at",
        )
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class IncidentCorrelationContext:
    correlation_family: CorrelationFamily
    anchor_strength: AnchorStrength
    anchor_event_id: str | None
    anchor_event_type: str | None
    normalized_fingerprint: NormalizedFingerprint | None
    anchor_policy_id: str | None
    anchor_policy_version: str | None
    promoted_from_weak: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.correlation_family, CorrelationFamily):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "correlation_family must reuse SPEC-006 CorrelationFamily")
        if not isinstance(self.anchor_strength, AnchorStrength):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "anchor_strength must reuse SPEC-006 AnchorStrength")
        if not isinstance(self.promoted_from_weak, bool):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "promoted_from_weak must be a bool")

        if self.anchor_strength is AnchorStrength.STRONG:
            _reference(self.anchor_event_id, "anchor_event_id")
            _reference(self.anchor_event_type, "anchor_event_type")
            _reference(self.anchor_policy_id, "anchor_policy_id")
            _reference(self.anchor_policy_version, "anchor_policy_version")
            if not isinstance(self.normalized_fingerprint, NormalizedFingerprint):
                _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "Strong context requires a SPEC-006 NormalizedFingerprint")
            if self.normalized_fingerprint.event_type != self.anchor_event_type:
                _fail(
                    IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
                    "Strong context anchor_event_type must match normalized_fingerprint.event_type",
                )
        else:
            if any(
                value is not None
                for value in (
                    self.anchor_event_id,
                    self.anchor_event_type,
                    self.normalized_fingerprint,
                )
            ):
                _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "Weak standalone context requires null anchor identity and fingerprint")
            _reference(self.anchor_policy_id, "anchor_policy_id")
            _reference(self.anchor_policy_version, "anchor_policy_version")
            if self.promoted_from_weak:
                _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "Weak standalone context cannot be marked promoted_from_weak")


@dataclass(frozen=True, slots=True)
class IncidentAuditEntry:
    operation_id: str
    event_id: str
    incident_id: str
    policy_id: str
    policy_version: str
    reason_code: DecisionReasonCode
    action: IncidentAuditAction
    effects: tuple[IncidentAuditEffect, ...]
    occurred_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("operation_id", "event_id", "incident_id", "policy_id", "policy_version"):
            _reference(getattr(self, field_name), field_name)
        if not isinstance(self.reason_code, DecisionReasonCode):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "reason_code must reuse SPEC-006 DecisionReasonCode")
        if not isinstance(self.action, IncidentAuditAction):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "action must be an IncidentAuditAction")
        try:
            effects = tuple(self.effects)
        except TypeError:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "effects must be an iterable")
        if not effects or any(not isinstance(effect, IncidentAuditEffect) for effect in effects):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "effects must contain IncidentAuditEffect values")
        if len(effects) != len(set(effects)) or IncidentAuditEffect.EVENT_ATTACHED not in effects:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "audit effects must be unique and include EVENT_ATTACHED")
        object.__setattr__(self, "effects", effects)
        object.__setattr__(self, "occurred_at", _utc_datetime(self.occurred_at, "occurred_at"))


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    incident_id: str
    event_ids: tuple[str, ...]
    anchor_event_id: str | None
    status: IncidentStatus
    severity: IncidentSeverity
    created_at: datetime
    updated_at: datetime
    last_correlated_at: datetime
    closed_at: datetime | None
    assignee: str | None
    reviewer: str | None
    correlation_context: IncidentCorrelationContext
    audit_trail: tuple[IncidentAuditEntry, ...]
    rca_status: str
    rca_ref: str | None
    external_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _reference(self.incident_id, "incident_id")
        try:
            event_ids = tuple(self.event_ids)
        except TypeError:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "event_ids must be an iterable")
        if not event_ids:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "event_ids must not be empty")
        for event_id in event_ids:
            _reference(event_id, "event_ids")
        if len(event_ids) != len(set(event_ids)):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "event_ids must be ordered unique references")
        object.__setattr__(self, "event_ids", event_ids)

        _reference(self.anchor_event_id, "anchor_event_id", nullable=True)
        if not isinstance(self.status, IncidentStatus):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "status must be an IncidentStatus")
        if not isinstance(self.severity, IncidentSeverity):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "severity must be an IncidentSeverity")

        created_at = _utc_datetime(self.created_at, "created_at")
        updated_at = _utc_datetime(self.updated_at, "updated_at")
        last_correlated_at = _utc_datetime(self.last_correlated_at, "last_correlated_at")
        closed_at = None if self.closed_at is None else _utc_datetime(self.closed_at, "closed_at")
        if updated_at < created_at:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "updated_at must not precede created_at")
        if self.status is IncidentStatus.CLOSED:
            if closed_at is None or closed_at < created_at or updated_at < closed_at:
                _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "CLOSED requires a valid closed_at not after updated_at")
        elif closed_at is not None:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "closed_at is only valid for CLOSED")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "last_correlated_at", last_correlated_at)
        object.__setattr__(self, "closed_at", closed_at)

        _reference(self.assignee, "assignee", nullable=True)
        _reference(self.reviewer, "reviewer", nullable=True)
        if not isinstance(self.correlation_context, IncidentCorrelationContext):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "correlation_context must be an IncidentCorrelationContext")
        if self.anchor_event_id != self.correlation_context.anchor_event_id:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "top-level and context anchor_event_id must match")
        if self.anchor_event_id is not None and self.anchor_event_id not in event_ids:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "anchor_event_id must belong to event_ids")

        try:
            audit_trail = tuple(self.audit_trail)
        except TypeError:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "audit_trail must be an iterable")
        if not audit_trail or any(not isinstance(entry, IncidentAuditEntry) for entry in audit_trail):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "audit_trail must contain IncidentAuditEntry values")
        if any(entry.incident_id != self.incident_id for entry in audit_trail):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "audit entries must belong to the Incident")
        if any(entry.event_id not in event_ids for entry in audit_trail):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "audit Event references must belong to the Incident")
        if len({entry.operation_id for entry in audit_trail}) != len(audit_trail):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "each operation may have only one primary business audit")
        object.__setattr__(self, "audit_trail", audit_trail)

        _reference(self.rca_status, "rca_status")
        _reference(self.rca_ref, "rca_ref", nullable=True)
        try:
            external_refs = tuple(self.external_refs)
        except TypeError:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "external_refs must be an iterable")
        for external_ref in external_refs:
            _reference(external_ref, "external_refs")
        if len(external_refs) != len(set(external_refs)):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "external_refs must be unique references")
        object.__setattr__(self, "external_refs", external_refs)


# A projection value, not a replacement Runtime Event model.
EventMutationProjection: TypeAlias = tuple[str, str, datetime, IncidentSeverity]


def event_mutation_projection(event: Mapping[str, object]) -> EventMutationProjection:
    if not isinstance(event, Mapping):
        _fail(IncidentErrorCode.INVALID_INCIDENT_MUTATION, "event must be a Mapping")
    event_id = event.get("event_id")
    event_type = event.get("event_type")
    _reference(event_id, "event.event_id", code=IncidentErrorCode.INVALID_INCIDENT_MUTATION)
    _reference(event_type, "event.event_type", code=IncidentErrorCode.INVALID_INCIDENT_MUTATION)
    detected_at = _event_timestamp(event.get("detected_at"))
    raw_severity = event.get("severity")
    if not isinstance(raw_severity, str):
        _fail(IncidentErrorCode.INVALID_INCIDENT_MUTATION, "event.severity must be a string", field_path="event.severity")
    try:
        severity = IncidentSeverity[raw_severity]
    except KeyError:
        _fail(IncidentErrorCode.INVALID_INCIDENT_MUTATION, "event.severity is outside the closed severity set", field_path="event.severity")
    return event_id, event_type, detected_at, severity


def _validate_intent_static_semantics(intent: CorrelationMutationIntent) -> None:
    """Reapply the real SPEC-006 decision invariants to a durable Intent.

    SPEC-007 guarantees its own outcome/target contract, while this boundary
    must also reject real class instances whose copied decision semantics are
    contradictory.  This validation uses the upstream contract itself and
    does not inspect any durable Incident or ownership state.
    """
    try:
        CorrelationDecision(
            decision_type=intent.decision_type,
            policy_id=intent.policy_id,
            policy_version=intent.policy_version,
            correlation_family=intent.correlation_family,
            reason_code=intent.reason_code,
            target_incident_id=intent.target_incident_id,
            normalized_fingerprint=intent.normalized_fingerprint,
            anchor_strength=intent.anchor_strength,
            anchor_transition=intent.anchor_transition,
        )
    except (TypeError, ValueError) as exc:
        _fail(
            IncidentErrorCode.INVALID_INCIDENT_MUTATION,
            f"intent has invalid SPEC-006 decision semantics: {exc}",
            operation_id=intent.operation_id,
            event_id=intent.event_id,
        )


@dataclass(frozen=True, slots=True)
class IncidentMutationRequest:
    """Thin handoff: real Intent + authoritative Event Mapping + caller time."""

    intent: CorrelationMutationIntent
    event: Mapping[str, object]
    now: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.intent, CorrelationMutationIntent):
            _fail(IncidentErrorCode.INVALID_INCIDENT_MUTATION, "intent must be a real SPEC-007 CorrelationMutationIntent")
        if self.intent.decision_type not in {DecisionType.CREATE_NEW, DecisionType.ATTACH_EXISTING}:
            _fail(
                IncidentErrorCode.INVALID_INCIDENT_MUTATION,
                "only CREATE_NEW and ATTACH_EXISTING belong to SPEC-008",
                operation_id=self.intent.operation_id,
                event_id=self.intent.event_id,
            )
        _validate_intent_static_semantics(self.intent)
        projection = event_mutation_projection(self.event)
        if projection[0] != self.intent.event_id:
            _fail(
                IncidentErrorCode.INVALID_INCIDENT_MUTATION,
                "event.event_id must equal intent.event_id",
                operation_id=self.intent.operation_id,
                event_id=self.intent.event_id,
                field_path="event.event_id",
            )
        object.__setattr__(
            self,
            "now",
            _utc_datetime(self.now, "now", code=IncidentErrorCode.INVALID_INCIDENT_MUTATION),
        )


@dataclass(frozen=True, slots=True)
class OperationReceiptSemanticIdentity:
    """Complete immutable comparison material for one successful operation."""

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
    intent_created_at: datetime
    event_type: str
    event_detected_at: datetime
    event_severity: IncidentSeverity

    def __post_init__(self) -> None:
        for field_name in ("event_id", "policy_id", "policy_version", "event_type"):
            _reference(getattr(self, field_name), field_name)
        if not isinstance(self.intended_terminal_outcome, TerminalOutcome):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "intended_terminal_outcome must reuse SPEC-007 TerminalOutcome")
        if self.decision_type not in {DecisionType.CREATE_NEW, DecisionType.ATTACH_EXISTING}:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "receipt decision_type must belong to SPEC-008")
        expected_outcome = {
            DecisionType.CREATE_NEW: TerminalOutcome.CREATED_INCIDENT,
            DecisionType.ATTACH_EXISTING: TerminalOutcome.ATTACHED_TO_INCIDENT,
        }[self.decision_type]
        if self.intended_terminal_outcome is not expected_outcome:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "receipt outcome must match decision_type")
        if not isinstance(self.correlation_family, CorrelationFamily):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "correlation_family must reuse SPEC-006 CorrelationFamily")
        if not isinstance(self.reason_code, DecisionReasonCode):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "reason_code must reuse SPEC-006 DecisionReasonCode")
        _reference(self.target_incident_id, "target_incident_id", nullable=True)
        if self.decision_type is DecisionType.ATTACH_EXISTING:
            _reference(self.target_incident_id, "target_incident_id")
        elif self.target_incident_id is not None:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "CREATE_NEW receipt must not have target_incident_id")
        if self.normalized_fingerprint is not None and not isinstance(self.normalized_fingerprint, NormalizedFingerprint):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "normalized_fingerprint must reuse SPEC-006 NormalizedFingerprint")
        if self.anchor_strength is not None and not isinstance(self.anchor_strength, AnchorStrength):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "anchor_strength must reuse SPEC-006 AnchorStrength")
        if not isinstance(self.anchor_transition, AnchorTransition):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "anchor_transition must reuse SPEC-006 AnchorTransition")
        if not isinstance(self.event_severity, IncidentSeverity):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "event_severity must be an IncidentSeverity")
        object.__setattr__(self, "intent_created_at", _utc_datetime(self.intent_created_at, "intent_created_at"))
        object.__setattr__(self, "event_detected_at", _utc_datetime(self.event_detected_at, "event_detected_at"))

    @classmethod
    def from_request(cls, request: IncidentMutationRequest) -> "OperationReceiptSemanticIdentity":
        if not isinstance(request, IncidentMutationRequest):
            _fail(IncidentErrorCode.INVALID_INCIDENT_MUTATION, "request must be an IncidentMutationRequest")
        intent = request.intent
        event_id, event_type, detected_at, severity = event_mutation_projection(request.event)
        return cls(
            event_id=event_id,
            intended_terminal_outcome=intent.intended_terminal_outcome,
            decision_type=intent.decision_type,
            policy_id=intent.policy_id,
            policy_version=intent.policy_version,
            correlation_family=intent.correlation_family,
            reason_code=intent.reason_code,
            target_incident_id=intent.target_incident_id,
            normalized_fingerprint=intent.normalized_fingerprint,
            anchor_strength=intent.anchor_strength,
            anchor_transition=intent.anchor_transition,
            intent_created_at=intent.created_at.astimezone(timezone.utc),
            event_type=event_type,
            event_detected_at=detected_at,
            event_severity=severity,
        )


@dataclass(frozen=True, slots=True)
class IncidentOperationResult:
    operation_id: str
    event_id: str
    mutation_kind: DecisionType
    incident_id: str
    completion_result: IncidentMutationCompletion
    completed_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("operation_id", "event_id", "incident_id"):
            _reference(getattr(self, field_name), field_name)
        if self.mutation_kind not in {DecisionType.CREATE_NEW, DecisionType.ATTACH_EXISTING}:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "mutation_kind must be CREATE_NEW or ATTACH_EXISTING")
        if self.completion_result is not IncidentMutationCompletion.SUCCEEDED:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "only successful durable operation results are receipts")
        object.__setattr__(self, "completed_at", _utc_datetime(self.completed_at, "completed_at"))


@dataclass(frozen=True, slots=True)
class IncidentOperationReceipt:
    result: IncidentOperationResult
    immutable_mutation_identity: OperationReceiptSemanticIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.result, IncidentOperationResult):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "result must be an IncidentOperationResult")
        if not isinstance(self.immutable_mutation_identity, OperationReceiptSemanticIdentity):
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "immutable_mutation_identity must be an OperationReceiptSemanticIdentity")
        identity = self.immutable_mutation_identity
        if self.result.event_id != identity.event_id or self.result.mutation_kind is not identity.decision_type:
            _fail(IncidentErrorCode.MALFORMED_INCIDENT_RECORD, "receipt result and immutable identity must agree")


# SPEC-009 workflow contracts are deliberately separate from the SPEC-008
# correlation mutation and receipt namespace.
class WorkflowAction(str, Enum):
    AUTO_ASSIGN = "AUTO_ASSIGN"
    MANUAL_ASSIGN = "MANUAL_ASSIGN"
    REASSIGN = "REASSIGN"
    START_WORK = "START_WORK"
    SUBMIT_RESOLUTION = "SUBMIT_RESOLUTION"
    REVIEW_ATTEMPT = "REVIEW_ATTEMPT"


class AssignmentSelectionMode(str, Enum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class SopFollowed(str, Enum):
    YES = "YES"
    NO = "NO"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class WorkflowAuditEffect(str, Enum):
    ASSIGNEE_SET = "ASSIGNEE_SET"
    REVIEWER_BOUND = "REVIEWER_BOUND"
    STATUS_CHANGED = "STATUS_CHANGED"
    RESOLUTION_SUBMITTED = "RESOLUTION_SUBMITTED"
    REVIEW_RECORDED = "REVIEW_RECORDED"
    RECOVERY_VERIFIED = "RECOVERY_VERIFIED"
    INCIDENT_CLOSED = "INCIDENT_CLOSED"


class IncidentTimelineSource(str, Enum):
    CORRELATION = "CORRELATION"
    WORKFLOW = "WORKFLOW"


class WorkflowCompletion(str, Enum):
    SUCCEEDED = "SUCCEEDED"


class WorkflowErrorCode(str, Enum):
    INVALID_WORKFLOW_MUTATION = "INVALID_WORKFLOW_MUTATION"
    INCIDENT_NOT_FOUND = "INCIDENT_NOT_FOUND"
    INVALID_LIFECYCLE_TRANSITION = "INVALID_LIFECYCLE_TRANSITION"
    INVALID_ASSIGNMENT_TARGET = "INVALID_ASSIGNMENT_TARGET"
    ASSIGNMENT_NOT_ALLOWED = "ASSIGNMENT_NOT_ALLOWED"
    WORKFLOW_ACTOR_MISMATCH = "WORKFLOW_ACTOR_MISMATCH"
    INVALID_RESOLUTION_EVIDENCE = "INVALID_RESOLUTION_EVIDENCE"
    RESOLUTION_REVISION_CONFLICT = "RESOLUTION_REVISION_CONFLICT"
    INVALID_REVIEW_ATTEMPT = "INVALID_REVIEW_ATTEMPT"
    STALE_RESOLUTION_REVISION = "STALE_RESOLUTION_REVISION"
    CLOSED_INCIDENT_MUTATION_FORBIDDEN = "CLOSED_INCIDENT_MUTATION_FORBIDDEN"
    WORKFLOW_RECEIPT_CONFLICT = "WORKFLOW_RECEIPT_CONFLICT"
    WORKFLOW_TIME_REGRESSION = "WORKFLOW_TIME_REGRESSION"
    MALFORMED_WORKFLOW_STATE = "MALFORMED_WORKFLOW_STATE"
    UNSUPPORTED_INCIDENT_STORE_VERSION = "UNSUPPORTED_INCIDENT_STORE_VERSION"
    INCIDENT_WORKFLOW_INTEGRITY_FAILURE = "INCIDENT_WORKFLOW_INTEGRITY_FAILURE"
    TRANSIENT_INCIDENT_STORE_FAILURE = "TRANSIENT_INCIDENT_STORE_FAILURE"


WORKFLOW_ERROR_DISPOSITIONS: Mapping[WorkflowErrorCode, RetryDisposition] = MappingProxyType(
    {
        **{code: RetryDisposition.NON_RETRYABLE for code in (
            WorkflowErrorCode.INVALID_WORKFLOW_MUTATION,
            WorkflowErrorCode.INCIDENT_NOT_FOUND,
            WorkflowErrorCode.INVALID_LIFECYCLE_TRANSITION,
            WorkflowErrorCode.INVALID_ASSIGNMENT_TARGET,
            WorkflowErrorCode.ASSIGNMENT_NOT_ALLOWED,
            WorkflowErrorCode.WORKFLOW_ACTOR_MISMATCH,
            WorkflowErrorCode.INVALID_RESOLUTION_EVIDENCE,
            WorkflowErrorCode.INVALID_REVIEW_ATTEMPT,
            WorkflowErrorCode.STALE_RESOLUTION_REVISION,
            WorkflowErrorCode.CLOSED_INCIDENT_MUTATION_FORBIDDEN,
            WorkflowErrorCode.WORKFLOW_TIME_REGRESSION,
        )},
        **{code: RetryDisposition.REPAIR_REQUIRED for code in (
            WorkflowErrorCode.RESOLUTION_REVISION_CONFLICT,
            WorkflowErrorCode.WORKFLOW_RECEIPT_CONFLICT,
            WorkflowErrorCode.MALFORMED_WORKFLOW_STATE,
            WorkflowErrorCode.UNSUPPORTED_INCIDENT_STORE_VERSION,
            WorkflowErrorCode.INCIDENT_WORKFLOW_INTEGRITY_FAILURE,
        )},
        WorkflowErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE: RetryDisposition.RETRYABLE,
    }
)


class WorkflowDomainError(ValueError):
    """A typed, safe SPEC-009 workflow-contract failure."""

    def __init__(self, code: WorkflowErrorCode, message: str, *, workflow_operation_id: str | None = None,
                 incident_id: str | None = None, field_path: str | None = None) -> None:
        if not isinstance(code, WorkflowErrorCode):
            raise TypeError("code must be a WorkflowErrorCode")
        super().__init__(message)
        self.code = code
        self.retry_disposition = WORKFLOW_ERROR_DISPOSITIONS[code]
        self.workflow_operation_id = workflow_operation_id
        self.incident_id = incident_id
        self.field_path = field_path


def _workflow_fail(code: WorkflowErrorCode, message: str, **details: str | None) -> None:
    raise WorkflowDomainError(code, message, **details)


def _workflow_reference(value: object, field_name: str, *, nullable: bool = False,
                        code: WorkflowErrorCode = WorkflowErrorCode.INVALID_WORKFLOW_MUTATION) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value or value != value.strip():
        _workflow_fail(code, f"{field_name} must be a non-empty reference without surrounding whitespace", field_path=field_name)


def _workflow_datetime(value: object, field_name: str, *, code: WorkflowErrorCode) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _workflow_fail(code, f"{field_name} must be a timezone-aware datetime", field_path=field_name)
    return value.astimezone(timezone.utc)


def _validate_workflow_semantic_shape(
    *,
    action: object,
    incident_id: object,
    actor: object,
    target_assignee: object,
    resolution: object,
    review: object,
) -> None:
    """Validate the closed caller-supplied projection used for workflow replay."""
    _workflow_reference(incident_id, "incident_id")
    _workflow_reference(actor, "actor")
    if not isinstance(action, WorkflowAction):
        _workflow_fail(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, "action must be a WorkflowAction", field_path="action")

    needs_assignee = action in {WorkflowAction.MANUAL_ASSIGN, WorkflowAction.REASSIGN}
    if needs_assignee:
        _workflow_reference(target_assignee, "target_assignee")
    elif target_assignee is not None:
        _workflow_fail(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, "target_assignee belongs only to assignment actions", field_path="target_assignee")

    if action is WorkflowAction.SUBMIT_RESOLUTION:
        if not isinstance(resolution, ResolutionSubmissionPayload):
            _workflow_fail(WorkflowErrorCode.INVALID_RESOLUTION_EVIDENCE, "SUBMIT_RESOLUTION requires resolution payload", field_path="resolution")
    elif resolution is not None:
        _workflow_fail(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, "resolution belongs only to SUBMIT_RESOLUTION", field_path="resolution")

    if action is WorkflowAction.REVIEW_ATTEMPT:
        if not isinstance(review, ReviewAttemptPayload):
            _workflow_fail(WorkflowErrorCode.INVALID_REVIEW_ATTEMPT, "REVIEW_ATTEMPT requires review payload", field_path="review")
    elif review is not None:
        _workflow_fail(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, "review belongs only to REVIEW_ATTEMPT", field_path="review")


@dataclass(frozen=True, slots=True)
class AssignmentPolicyConfig:
    policy_id: str
    policy_version: str
    engineers: tuple[str, ...]
    default_reviewer: str

    def __post_init__(self) -> None:
        for name in ("policy_id", "policy_version", "default_reviewer"):
            _workflow_reference(getattr(self, name), name)
        try:
            engineers = tuple(self.engineers)
        except TypeError:
            _workflow_fail(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, "engineers must be an ordered iterable", field_path="engineers")
        if isinstance(self.engineers, (str, bytes)):
            _workflow_fail(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, "engineers must be an ordered collection, not a string", field_path="engineers")
        if not engineers:
            _workflow_fail(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, "engineers must not be empty", field_path="engineers")
        for engineer in engineers:
            _workflow_reference(engineer, "engineers")
        if len(engineers) != len(set(engineers)):
            _workflow_fail(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, "engineers must be ordered and unique", field_path="engineers")
        object.__setattr__(self, "engineers", engineers)


@dataclass(frozen=True, slots=True)
class ResolutionSubmissionPayload:
    actual_action: str
    resolution_note: str
    sop_followed: SopFollowed
    additional_note: str | None = None
    deviation_reason: str | None = None

    def __post_init__(self) -> None:
        _workflow_reference(self.actual_action, "actual_action", code=WorkflowErrorCode.INVALID_RESOLUTION_EVIDENCE)
        _workflow_reference(self.resolution_note, "resolution_note", code=WorkflowErrorCode.INVALID_RESOLUTION_EVIDENCE)
        if not isinstance(self.sop_followed, SopFollowed):
            _workflow_fail(WorkflowErrorCode.INVALID_RESOLUTION_EVIDENCE, "sop_followed must be a SopFollowed value", field_path="sop_followed")
        _workflow_reference(self.additional_note, "additional_note", nullable=True, code=WorkflowErrorCode.INVALID_RESOLUTION_EVIDENCE)
        _workflow_reference(self.deviation_reason, "deviation_reason", nullable=True, code=WorkflowErrorCode.INVALID_RESOLUTION_EVIDENCE)
        if self.sop_followed is SopFollowed.NO and self.deviation_reason is None:
            _workflow_fail(WorkflowErrorCode.INVALID_RESOLUTION_EVIDENCE, "SOP NO requires deviation_reason", field_path="deviation_reason")


@dataclass(frozen=True, slots=True)
class ResolutionSubmission:
    resolution_submission_id: str
    incident_id: str
    revision: int
    actual_action: str
    resolution_note: str
    sop_followed: SopFollowed
    additional_note: str | None
    deviation_reason: str | None
    submitted_by: str
    submitted_at: datetime

    def __post_init__(self) -> None:
        _workflow_reference(self.resolution_submission_id, "resolution_submission_id", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
        _workflow_reference(self.incident_id, "incident_id", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            _workflow_fail(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "revision must be a positive integer", field_path="revision")
        ResolutionSubmissionPayload(self.actual_action, self.resolution_note, self.sop_followed, self.additional_note, self.deviation_reason)
        _workflow_reference(self.submitted_by, "submitted_by", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
        object.__setattr__(self, "submitted_at", _workflow_datetime(self.submitted_at, "submitted_at", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE))


@dataclass(frozen=True, slots=True)
class ReviewAttemptPayload:
    target_resolution_revision: int
    review_approved: bool
    review_note: str | None
    recovery_verified: bool
    recovery_note: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.target_resolution_revision, int) or isinstance(self.target_resolution_revision, bool) or self.target_resolution_revision < 1:
            _workflow_fail(WorkflowErrorCode.INVALID_REVIEW_ATTEMPT, "target_resolution_revision must be a positive integer", field_path="target_resolution_revision")
        if not isinstance(self.review_approved, bool) or not isinstance(self.recovery_verified, bool):
            _workflow_fail(WorkflowErrorCode.INVALID_REVIEW_ATTEMPT, "review and recovery facts must be bool", field_path="review_approved/recovery_verified")
        _workflow_reference(self.review_note, "review_note", nullable=True, code=WorkflowErrorCode.INVALID_REVIEW_ATTEMPT)
        _workflow_reference(self.recovery_note, "recovery_note", nullable=True, code=WorkflowErrorCode.INVALID_REVIEW_ATTEMPT)


@dataclass(frozen=True, slots=True)
class ReviewAttempt:
    review_attempt_id: str
    incident_id: str
    resolution_revision: int
    reviewer: str
    review_approved: bool
    review_note: str | None
    recovery_verified: bool
    recovery_note: str | None
    reviewed_at: datetime

    def __post_init__(self) -> None:
        _workflow_reference(self.review_attempt_id, "review_attempt_id", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
        _workflow_reference(self.incident_id, "incident_id", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
        ReviewAttemptPayload(self.resolution_revision, self.review_approved, self.review_note, self.recovery_verified, self.recovery_note)
        _workflow_reference(self.reviewer, "reviewer", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
        object.__setattr__(self, "reviewed_at", _workflow_datetime(self.reviewed_at, "reviewed_at", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE))


@dataclass(frozen=True, slots=True)
class WorkflowMutationRequest:
    workflow_operation_id: str
    incident_id: str
    actor: str
    now: datetime
    action: WorkflowAction
    target_assignee: str | None = None
    resolution: ResolutionSubmissionPayload | None = None
    review: ReviewAttemptPayload | None = None

    def __post_init__(self) -> None:
        _workflow_reference(self.workflow_operation_id, "workflow_operation_id")
        object.__setattr__(self, "now", _workflow_datetime(self.now, "now", code=WorkflowErrorCode.INVALID_WORKFLOW_MUTATION))
        _validate_workflow_semantic_shape(
            action=self.action,
            incident_id=self.incident_id,
            actor=self.actor,
            target_assignee=self.target_assignee,
            resolution=self.resolution,
            review=self.review,
        )


@dataclass(frozen=True, slots=True)
class WorkflowReceiptSemanticIdentity:
    action: WorkflowAction
    incident_id: str
    actor: str
    target_assignee: str | None = None
    resolution: ResolutionSubmissionPayload | None = None
    review: ReviewAttemptPayload | None = None

    def __post_init__(self) -> None:
        _validate_workflow_semantic_shape(
            action=self.action,
            incident_id=self.incident_id,
            actor=self.actor,
            target_assignee=self.target_assignee,
            resolution=self.resolution,
            review=self.review,
        )

    @classmethod
    def from_request(cls, request: WorkflowMutationRequest) -> "WorkflowReceiptSemanticIdentity":
        if not isinstance(request, WorkflowMutationRequest):
            _workflow_fail(WorkflowErrorCode.INVALID_WORKFLOW_MUTATION, "request must be a WorkflowMutationRequest")
        return cls(request.action, request.incident_id, request.actor, request.target_assignee, request.resolution, request.review)


@dataclass(frozen=True, slots=True)
class WorkflowOperationResult:
    workflow_operation_id: str
    incident_id: str
    action: WorkflowAction
    completion_result: WorkflowCompletion
    resulting_status: IncidentStatus
    result_reference: str | None
    completed_at: datetime
    assignment_policy_id: str | None = None
    assignment_policy_version: str | None = None
    selected_assignee: str | None = None
    bound_reviewer: str | None = None
    selection_mode: AssignmentSelectionMode | None = None

    def __post_init__(self) -> None:
        _workflow_reference(self.workflow_operation_id, "workflow_operation_id", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
        _workflow_reference(self.incident_id, "incident_id", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
        if not isinstance(self.action, WorkflowAction) or not isinstance(self.resulting_status, IncidentStatus) or self.completion_result is not WorkflowCompletion.SUCCEEDED:
            _workflow_fail(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "result action, completion, and status must be closed domain values")
        _workflow_reference(self.result_reference, "result_reference", nullable=True, code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
        assignment_values = (self.assignment_policy_id, self.assignment_policy_version, self.selected_assignee, self.bound_reviewer, self.selection_mode)
        if self.action in {WorkflowAction.AUTO_ASSIGN, WorkflowAction.MANUAL_ASSIGN}:
            expected_mode = AssignmentSelectionMode.AUTO if self.action is WorkflowAction.AUTO_ASSIGN else AssignmentSelectionMode.MANUAL
            if self.selection_mode is not expected_mode:
                _workflow_fail(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "assignment result selection mode contradicts action")
            for name in ("assignment_policy_id", "assignment_policy_version", "selected_assignee", "bound_reviewer"):
                _workflow_reference(getattr(self, name), name, code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
        elif any(value is not None for value in assignment_values):
            _workflow_fail(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "assignment provenance belongs only to initial assignment results")
        object.__setattr__(self, "completed_at", _workflow_datetime(self.completed_at, "completed_at", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE))


@dataclass(frozen=True, slots=True)
class WorkflowOperationReceipt:
    result: WorkflowOperationResult
    immutable_workflow_identity: WorkflowReceiptSemanticIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.result, WorkflowOperationResult) or not isinstance(self.immutable_workflow_identity, WorkflowReceiptSemanticIdentity):
            _workflow_fail(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "receipt requires workflow result and semantic identity")
        identity = self.immutable_workflow_identity
        if self.result.incident_id != identity.incident_id or self.result.action is not identity.action:
            _workflow_fail(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "receipt result and identity must agree")


@dataclass(frozen=True, slots=True)
class WorkflowAuditEntry:
    workflow_audit_id: str
    workflow_operation_id: str
    incident_id: str
    actor: str
    action: WorkflowAction
    occurred_at: datetime
    old_status: IncidentStatus
    new_status: IncidentStatus
    effects: tuple[WorkflowAuditEffect, ...]
    reference_id: str | None = None
    assignment_policy_id: str | None = None
    assignment_policy_version: str | None = None

    def __post_init__(self) -> None:
        for name in ("workflow_audit_id", "workflow_operation_id", "incident_id", "actor"):
            _workflow_reference(getattr(self, name), name, code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
        if not isinstance(self.action, WorkflowAction) or not isinstance(self.old_status, IncidentStatus) or not isinstance(self.new_status, IncidentStatus):
            _workflow_fail(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "audit action and statuses must be closed domain values")
        try:
            effects = tuple(self.effects)
        except TypeError:
            _workflow_fail(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "effects must be an iterable", field_path="effects")
        if not effects or any(not isinstance(effect, WorkflowAuditEffect) for effect in effects) or len(effects) != len(set(effects)):
            _workflow_fail(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "effects must be non-empty unique WorkflowAuditEffect values", field_path="effects")
        object.__setattr__(self, "effects", effects)
        _workflow_reference(self.reference_id, "reference_id", nullable=True, code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
        policy_values = (self.assignment_policy_id, self.assignment_policy_version)
        if any(value is not None for value in policy_values):
            if self.action is not WorkflowAction.AUTO_ASSIGN:
                _workflow_fail(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "only AUTO_ASSIGN audit may carry assignment policy provenance")
            _workflow_reference(self.assignment_policy_id, "assignment_policy_id", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
            _workflow_reference(self.assignment_policy_version, "assignment_policy_version", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE)
        object.__setattr__(self, "occurred_at", _workflow_datetime(self.occurred_at, "occurred_at", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE))


@dataclass(frozen=True, slots=True)
class IncidentTimelineEntry:
    """A source-aware, deterministically ordered Incident timeline entry."""

    occurred_at: datetime
    source: IncidentTimelineSource
    source_local_order: str
    correlation_audit: IncidentAuditEntry | None = None
    workflow_audit: WorkflowAuditEntry | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", _workflow_datetime(self.occurred_at, "occurred_at", code=WorkflowErrorCode.MALFORMED_WORKFLOW_STATE))
        if not isinstance(self.source, IncidentTimelineSource) or not isinstance(self.source_local_order, str) or not self.source_local_order:
            _workflow_fail(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "timeline source and stable local order are required")
        correlation = self.correlation_audit
        workflow = self.workflow_audit
        if self.source is IncidentTimelineSource.CORRELATION:
            if not isinstance(correlation, IncidentAuditEntry) or workflow is not None or correlation.occurred_at != self.occurred_at:
                _workflow_fail(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "correlation timeline entry is malformed")
        elif not isinstance(workflow, WorkflowAuditEntry) or correlation is not None or workflow.occurred_at != self.occurred_at:
            _workflow_fail(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "workflow timeline entry is malformed")


__all__ = [
    "AssignmentPolicyConfig",
    "AssignmentSelectionMode",
    "CORRELATION_CLOSED_STATUSES",
    "CORRELATION_OPEN_STATUSES",
    "EventMutationProjection",
    "INCIDENT_ERROR_DISPOSITIONS",
    "IncidentAuditAction",
    "IncidentAuditEffect",
    "IncidentAuditEntry",
    "IncidentCorrelationContext",
    "IncidentDomainError",
    "IncidentErrorCode",
    "IncidentMutationCompletion",
    "IncidentMutationRequest",
    "IncidentOperationReceipt",
    "IncidentOperationResult",
    "IncidentRecord",
    "IncidentSeverity",
    "IncidentStatus",
    "IncidentTimelineEntry",
    "IncidentTimelineSource",
    "OperationReceiptSemanticIdentity",
    "RCA_INITIAL_STATUS",
    "ResolutionSubmission",
    "ResolutionSubmissionPayload",
    "ReviewAttempt",
    "ReviewAttemptPayload",
    "SopFollowed",
    "WorkflowAction",
    "WorkflowAuditEffect",
    "WorkflowAuditEntry",
    "WorkflowCompletion",
    "WorkflowDomainError",
    "WorkflowErrorCode",
    "WorkflowMutationRequest",
    "WorkflowOperationReceipt",
    "WorkflowOperationResult",
    "WorkflowReceiptSemanticIdentity",
    "WORKFLOW_ERROR_DISPOSITIONS",
    "event_mutation_projection",
]
