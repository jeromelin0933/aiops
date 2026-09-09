"""Immutable logical contracts for the SPEC-010 Shadow domain.

The contract consumes actual SPEC-006 policy objects and actual SPEC-007
``CorrelationMutationIntent`` objects.  It intentionally does not model a
replacement Event, Decision, or Intent authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Protocol, TypeAlias

from alert_correlation.contracts import (
    AnchorTransition,
    CorrelationFamily,
    DecisionReasonCode,
    DecisionType,
    EvidenceClass,
)
from alert_correlation.policy import CorrelationPolicy
from alert_correlation.state.contracts import (
    CorrelationMutationIntent,
    RetryDisposition,
    TerminalOutcome,
)


class ShadowReason(str, Enum):
    INSUFFICIENT_OPERATIONAL_IDENTITY = "INSUFFICIENT_OPERATIONAL_IDENTITY"
    NO_COMPATIBLE_INCIDENT = "NO_COMPATIBLE_INCIDENT"
    MULTIPLE_COMPATIBLE_INCIDENTS = "MULTIPLE_COMPATIBLE_INCIDENTS"


class ShadowReviewStatus(str, Enum):
    UNREVIEWED = "UNREVIEWED"


class ShadowDomainErrorCode(str, Enum):
    INVALID_SHADOW_MUTATION = "INVALID_SHADOW_MUTATION"
    SHADOW_NOT_FOUND = "SHADOW_NOT_FOUND"
    SHADOW_EVENT_OWNERSHIP_CONFLICT = "SHADOW_EVENT_OWNERSHIP_CONFLICT"
    INCIDENT_EVENT_OWNERSHIP_CONFLICT = "INCIDENT_EVENT_OWNERSHIP_CONFLICT"
    MUTATION_RECEIPT_CONFLICT = "MUTATION_RECEIPT_CONFLICT"
    MALFORMED_SHADOW_RECORD = "MALFORMED_SHADOW_RECORD"
    UNSUPPORTED_SHADOW_STATE_VERSION = "UNSUPPORTED_SHADOW_STATE_VERSION"
    SHADOW_STORE_INTEGRITY_FAILURE = "SHADOW_STORE_INTEGRITY_FAILURE"
    TRANSIENT_SHADOW_STORE_FAILURE = "TRANSIENT_SHADOW_STORE_FAILURE"


SHADOW_ERROR_RETRY_DISPOSITIONS: Mapping[ShadowDomainErrorCode, RetryDisposition] = (
    MappingProxyType({
        ShadowDomainErrorCode.INVALID_SHADOW_MUTATION: RetryDisposition.NON_RETRYABLE,
        ShadowDomainErrorCode.SHADOW_NOT_FOUND: RetryDisposition.NON_RETRYABLE,
        ShadowDomainErrorCode.SHADOW_EVENT_OWNERSHIP_CONFLICT: RetryDisposition.REPAIR_REQUIRED,
        ShadowDomainErrorCode.INCIDENT_EVENT_OWNERSHIP_CONFLICT: RetryDisposition.REPAIR_REQUIRED,
        ShadowDomainErrorCode.MUTATION_RECEIPT_CONFLICT: RetryDisposition.REPAIR_REQUIRED,
        ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD: RetryDisposition.REPAIR_REQUIRED,
        ShadowDomainErrorCode.UNSUPPORTED_SHADOW_STATE_VERSION: RetryDisposition.REPAIR_REQUIRED,
        ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE: RetryDisposition.REPAIR_REQUIRED,
        ShadowDomainErrorCode.TRANSIENT_SHADOW_STORE_FAILURE: RetryDisposition.RETRYABLE,
    })
)


def retry_disposition_for_shadow_error(code: ShadowDomainErrorCode) -> RetryDisposition:
    """Return the PM-adjudicated disposition; no catch-all mapping exists."""
    if not isinstance(code, ShadowDomainErrorCode):
        raise TypeError("code must be a ShadowDomainErrorCode")
    return SHADOW_ERROR_RETRY_DISPOSITIONS[code]


class ShadowDomainError(RuntimeError):
    """A typed Shadow-domain failure with its closed retry disposition."""

    def __init__(self, code: ShadowDomainErrorCode, message: str) -> None:
        if not isinstance(code, ShadowDomainErrorCode):
            raise TypeError("code must be a ShadowDomainErrorCode")
        super().__init__(message)
        self.code = code
        self.retry_disposition = retry_disposition_for_shadow_error(code)


class ShadowDomainValidationError(ShadowDomainError):
    """A fail-closed malformed Shadow contract boundary."""

    def __init__(self, message: str) -> None:
        super().__init__(ShadowDomainErrorCode.INVALID_SHADOW_MUTATION, message)


def _reference(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ShadowDomainValidationError(
            f"{field_name} must be a non-empty string without surrounding whitespace"
        )
    return value


def _aware_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ShadowDomainValidationError(f"{field_name} must be a timezone-aware datetime")
    return value


def validate_shadow_creation_reason(reason: ShadowReason) -> None:
    """Gate the v1 creation path without narrowing the domain vocabulary."""
    if not isinstance(reason, ShadowReason):
        raise ShadowDomainValidationError("reason must be a ShadowReason")
    if reason is not ShadowReason.INSUFFICIENT_OPERATIONAL_IDENTITY:
        raise ShadowDomainValidationError("current Shadow creation only permits INSUFFICIENT_OPERATIONAL_IDENTITY")


@dataclass(frozen=True, slots=True)
class ShadowRecord:
    shadow_id: str
    event_id: str
    entered_shadow_at: datetime
    reason: ShadowReason
    review_status: ShadowReviewStatus
    policy_id: str
    policy_version: str

    def __post_init__(self) -> None:
        _reference(self.shadow_id, "shadow_id")
        _reference(self.event_id, "event_id")
        _aware_timestamp(self.entered_shadow_at, "entered_shadow_at")
        if not isinstance(self.reason, ShadowReason):
            raise ShadowDomainValidationError("reason must be a ShadowReason")
        if self.review_status is not ShadowReviewStatus.UNREVIEWED:
            raise ShadowDomainValidationError("v1 review_status must be UNREVIEWED")
        _reference(self.policy_id, "policy_id")
        _reference(self.policy_version, "policy_version")


# A projection, not a replacement Event DTO.  The tuple is (event_id, event_type).
ShadowEventProjection: TypeAlias = tuple[str, str]


def _event_projection(event: object) -> ShadowEventProjection:
    if not isinstance(event, Mapping):
        raise ShadowDomainValidationError("event must be a mapping supplied by the authoritative Event boundary")
    return (_reference(event.get("event_id"), "event.event_id"), _reference(event.get("event_type"), "event.event_type"))


class ExactPolicyLookup(Protocol):
    """Read-only exact policy identity lookup; latest-policy fallback is forbidden."""

    def resolve_exact(self, policy_id: str, policy_version: str) -> CorrelationPolicy | None: ...


class IncidentOwnershipLookup(Protocol):
    """Read-only semantic Event-to-Incident ownership boundary.

    ``SqliteIncidentStore.event_has_incident_owner`` is the implemented
    SPEC-008 provider.  Consumers must not substitute persistence reads or
    infer clean absence after a provider failure.
    """

    def event_has_incident_owner(self, event_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ShadowMutationRequest:
    """Thin handoff: a real Intent, authoritative Event mapping, and mutation time."""

    intent: CorrelationMutationIntent
    event: Mapping[str, object]
    now: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.intent, CorrelationMutationIntent):
            raise ShadowDomainValidationError("intent must be a real CorrelationMutationIntent")
        _event_projection(self.event)
        _aware_timestamp(self.now, "now")

    @property
    def event_projection(self) -> ShadowEventProjection:
        return _event_projection(self.event)


@dataclass(frozen=True, slots=True)
class ShadowMutationResult:
    """The durable result returned for a successful operation or equivalent replay."""

    operation_id: str
    shadow_id: str
    entered_shadow_at: datetime

    def __post_init__(self) -> None:
        _reference(self.operation_id, "operation_id")
        _reference(self.shadow_id, "shadow_id")
        _aware_timestamp(self.entered_shadow_at, "entered_shadow_at")


@dataclass(frozen=True, slots=True)
class ShadowReceiptSemanticIdentity:
    """Receipt comparison input: complete real Intent plus minimal Event projection.

    ``now`` is intentionally absent: retry-call time is not semantic identity.
    """

    intent: CorrelationMutationIntent
    event_id: str
    event_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.intent, CorrelationMutationIntent):
            raise ShadowDomainValidationError("intent must be a real CorrelationMutationIntent")
        _reference(self.event_id, "event_id")
        _reference(self.event_type, "event_type")
        if self.event_id != self.intent.event_id:
            raise ShadowDomainValidationError("receipt event_id must match intent.event_id")


def receipt_semantic_identity(request: ShadowMutationRequest) -> ShadowReceiptSemanticIdentity:
    if not isinstance(request, ShadowMutationRequest):
        raise ShadowDomainValidationError("request must be a ShadowMutationRequest")
    event_id, event_type = request.event_projection
    return ShadowReceiptSemanticIdentity(request.intent, event_id, event_type)


@dataclass(frozen=True, slots=True)
class ShadowOperationReceipt:
    semantic_identity: ShadowReceiptSemanticIdentity
    result: ShadowMutationResult

    def __post_init__(self) -> None:
        if not isinstance(self.semantic_identity, ShadowReceiptSemanticIdentity):
            raise ShadowDomainValidationError("semantic_identity must be a ShadowReceiptSemanticIdentity")
        if not isinstance(self.result, ShadowMutationResult):
            raise ShadowDomainValidationError("result must be a ShadowMutationResult")
        if self.result.operation_id != self.semantic_identity.intent.operation_id:
            raise ShadowDomainValidationError("receipt result operation_id must match intent.operation_id")


class ShadowReadPort(Protocol):
    """Logical read boundary only; persistence remains a later phase."""

    def get_shadow(self, shadow_id: str) -> ShadowRecord | None: ...
    def get_shadow_by_event_id(self, event_id: str) -> ShadowRecord | None: ...
    def shadow_exists(self, shadow_id: str) -> bool: ...
    def get_operation_result(self, operation_id: str) -> ShadowMutationResult | None: ...
    def enumerate_shadows(
        self,
        *,
        reason: ShadowReason | None = None,
        review_status: ShadowReviewStatus | None = None,
    ) -> tuple[ShadowRecord, ...]: ...


def validate_legal_shadow_mutation(
    request: ShadowMutationRequest, policy_lookup: ExactPolicyLookup
) -> None:
    """Fail closed unless a real Intent has current legal ROUTE_SHADOW semantics."""
    if not isinstance(request, ShadowMutationRequest):
        raise ShadowDomainValidationError("request must be a ShadowMutationRequest")
    if not callable(getattr(policy_lookup, "resolve_exact", None)):
        raise ShadowDomainValidationError("policy_lookup must provide resolve_exact")

    intent = request.intent
    if intent.decision_type is not DecisionType.ROUTE_SHADOW:
        raise ShadowDomainValidationError("only ROUTE_SHADOW may enter the Shadow domain")
    if intent.intended_terminal_outcome is not TerminalOutcome.SHADOWED:
        raise ShadowDomainValidationError("ROUTE_SHADOW requires TerminalOutcome.SHADOWED")
    if intent.correlation_family is not CorrelationFamily.UNKNOWN:
        raise ShadowDomainValidationError("ROUTE_SHADOW requires CorrelationFamily.UNKNOWN")
    if intent.reason_code is not DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY:
        raise ShadowDomainValidationError("ROUTE_SHADOW requires INSUFFICIENT_OPERATIONAL_IDENTITY")
    if intent.target_incident_id is not None or intent.normalized_fingerprint is not None:
        raise ShadowDomainValidationError("ROUTE_SHADOW requires null target_incident_id and normalized_fingerprint")
    if intent.anchor_strength is not None or intent.anchor_transition is not AnchorTransition.NONE:
        raise ShadowDomainValidationError("ROUTE_SHADOW requires null anchor_strength and NONE anchor_transition")

    event_id, event_type = request.event_projection
    if event_id != intent.event_id:
        raise ShadowDomainValidationError("event.event_id must match intent.event_id")
    policy = policy_lookup.resolve_exact(intent.policy_id, intent.policy_version)
    if not isinstance(policy, CorrelationPolicy):
        raise ShadowDomainValidationError("exact policy identity is unavailable or malformed")
    if policy.event_type != event_type:
        raise ShadowDomainValidationError("event.event_type must match the exact policy")
    if (
        policy.evidence_class is not EvidenceClass.UNKNOWN
        or policy.correlation_family is not CorrelationFamily.UNKNOWN
        or not policy.is_current
    ):
        raise ShadowDomainValidationError("exact policy must be a current registered UNKNOWN policy")
