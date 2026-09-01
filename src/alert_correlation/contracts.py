"""Immutable logical contracts for the alert correlation policy engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping, TypeAlias


class EvidenceClass(str, Enum):
    STRONG = "STRONG"
    KNOWN_WEAK = "KNOWN_WEAK"
    UNKNOWN = "UNKNOWN"


class CorrelationFamily(str, Enum):
    ATTACK_SOURCE = "ATTACK_SOURCE"
    CROSS_SERVICE_LATENCY = "CROSS_SERVICE_LATENCY"
    MEMORY_OOM = "MEMORY_OOM"
    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"
    DOWNSTREAM_CASCADE = "DOWNSTREAM_CASCADE"
    RATE_LIMIT = "RATE_LIMIT"
    UNKNOWN = "UNKNOWN"


class AnchorStrength(str, Enum):
    STRONG = "STRONG"
    WEAK = "WEAK"


class AnchorTransition(str, Enum):
    NONE = "NONE"
    WEAK_TO_STRONG = "WEAK_TO_STRONG"


class EvaluationPhase(str, Enum):
    INITIAL = "INITIAL"
    PENDING_RECHECK = "PENDING_RECHECK"
    PENDING_EXPIRED = "PENDING_EXPIRED"


class DecisionType(str, Enum):
    ATTACH_EXISTING = "ATTACH_EXISTING"
    CREATE_NEW = "CREATE_NEW"
    ENTER_PENDING = "ENTER_PENDING"
    ROUTE_SHADOW = "ROUTE_SHADOW"


class DecisionReasonCode(str, Enum):
    EXACT_STRONG_IDENTITY_MATCH = "EXACT_STRONG_IDENTITY_MATCH"
    UNIQUE_COMPATIBLE_CANDIDATE = "UNIQUE_COMPATIBLE_CANDIDATE"
    WEAK_TO_STRONG_PROMOTION = "WEAK_TO_STRONG_PROMOTION"
    NO_COMPATIBLE_CANDIDATE = "NO_COMPATIBLE_CANDIDATE"
    MULTIPLE_COMPATIBLE_CANDIDATES = "MULTIPLE_COMPATIBLE_CANDIDATES"
    PENDING_EXPIRED_UNRESOLVED = "PENDING_EXPIRED_UNRESOLVED"
    INSUFFICIENT_OPERATIONAL_IDENTITY = "INSUFFICIENT_OPERATIONAL_IDENTITY"


class CorrelationErrorCode(str, Enum):
    INVALID_EVENT_ENVELOPE = "INVALID_EVENT_ENVELOPE"
    POLICY_NOT_REGISTERED = "POLICY_NOT_REGISTERED"
    POLICY_VERSION_UNAVAILABLE = "POLICY_VERSION_UNAVAILABLE"
    MISSING_REQUIRED_IDENTITY = "MISSING_REQUIRED_IDENTITY"
    INVALID_IDENTITY_VALUE = "INVALID_IDENTITY_VALUE"
    INVALID_INCIDENT_VIEW = "INVALID_INCIDENT_VIEW"
    INCONSISTENT_CORRELATION_CONTEXT = "INCONSISTENT_CORRELATION_CONTEXT"


IdentityItem: TypeAlias = tuple[str, str]
DiagnosticValue: TypeAlias = str | int | float | bool | None
DiagnosticItem: TypeAlias = tuple[str, DiagnosticValue]


@dataclass(frozen=True, slots=True)
class NormalizedFingerprint:
    """Structured, canonical Strong identity without string concatenation."""

    event_type: str
    identity: tuple[IdentityItem, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise ValueError("fingerprint event_type must be a non-empty string")
        if self.event_type != self.event_type.strip():
            raise ValueError("fingerprint event_type must not contain surrounding whitespace")

        try:
            raw_items = tuple(self.identity)
        except TypeError as exc:
            raise TypeError("fingerprint identity must be an iterable of field pairs") from exc
        if not raw_items:
            raise ValueError("fingerprint identity must contain at least one field")

        normalized: list[IdentityItem] = []
        names: set[str] = set()
        for item in raw_items:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("fingerprint identity entries must be (name, value) tuples")
            name, value = item
            if not isinstance(name, str) or not name or name != name.strip():
                raise ValueError("fingerprint identity field names must be non-empty strings")
            if not isinstance(value, str) or not value:
                raise ValueError("fingerprint identity values must be non-empty strings")
            if name in names:
                raise ValueError(f"duplicate fingerprint identity field: {name}")
            names.add(name)
            normalized.append((name, value))

        object.__setattr__(self, "identity", tuple(sorted(normalized)))

    @classmethod
    def from_mapping(
        cls,
        event_type: str,
        identity: Mapping[str, str],
    ) -> "NormalizedFingerprint":
        if not isinstance(identity, Mapping):
            raise TypeError("fingerprint identity must be a mapping")
        return cls(event_type=event_type, identity=tuple(identity.items()))

    def as_mapping(self) -> Mapping[str, str]:
        """Return a read-only named-field view of the canonical identity."""
        return MappingProxyType(dict(self.identity))


@dataclass(frozen=True, slots=True)
class IncidentCorrelationView:
    incident_id: str
    status: str
    last_correlated_at: datetime
    correlation_family: CorrelationFamily
    anchor_strength: AnchorStrength
    normalized_fingerprint: NormalizedFingerprint | None
    anchor_event_type: str | None = None


@dataclass(frozen=True, slots=True)
class CorrelationEvaluationContext:
    evaluation_phase: EvaluationPhase
    policy_id: str | None = None
    policy_version: str | None = None


@dataclass(frozen=True, slots=True)
class CorrelationDecision:
    decision_type: DecisionType
    policy_id: str
    policy_version: str
    correlation_family: CorrelationFamily
    reason_code: DecisionReasonCode
    target_incident_id: str | None = None
    normalized_fingerprint: NormalizedFingerprint | None = None
    anchor_strength: AnchorStrength | None = None
    anchor_transition: AnchorTransition = AnchorTransition.NONE
    diagnostics: tuple[DiagnosticItem, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.decision_type, DecisionType):
            raise TypeError("decision_type must be a DecisionType")
        for field_name in ("policy_id", "policy_version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(
                    f"{field_name} must be a non-empty string without "
                    "surrounding whitespace"
                )
        if not isinstance(self.correlation_family, CorrelationFamily):
            raise TypeError("correlation_family must be a CorrelationFamily")
        if not isinstance(self.reason_code, DecisionReasonCode):
            raise TypeError("reason_code must be a DecisionReasonCode")
        if self.target_incident_id is not None and (
            not isinstance(self.target_incident_id, str)
            or not self.target_incident_id
            or self.target_incident_id != self.target_incident_id.strip()
        ):
            raise ValueError("target_incident_id must be a non-empty reference")
        if (
            self.normalized_fingerprint is not None
            and not isinstance(self.normalized_fingerprint, NormalizedFingerprint)
        ):
            raise TypeError(
                "normalized_fingerprint must be a NormalizedFingerprint or None"
            )
        if self.anchor_strength is not None and not isinstance(
            self.anchor_strength, AnchorStrength
        ):
            raise TypeError("anchor_strength must be an AnchorStrength or None")
        if not isinstance(self.anchor_transition, AnchorTransition):
            raise TypeError("anchor_transition must be an AnchorTransition")

        self._validate_decision_invariants()

        try:
            raw_items = tuple(self.diagnostics)
        except TypeError as exc:
            raise TypeError("decision diagnostics must be an iterable of field pairs") from exc

        normalized: list[DiagnosticItem] = []
        names: set[str] = set()
        for item in raw_items:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("decision diagnostics entries must be (name, value) tuples")
            name, value = item
            if not isinstance(name, str) or not name or name != name.strip():
                raise ValueError("decision diagnostic names must be non-empty strings")
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise TypeError("decision diagnostic values must be immutable scalar values")
            if name in names:
                raise ValueError(f"duplicate decision diagnostic: {name}")
            names.add(name)
            normalized.append((name, value))
        object.__setattr__(self, "diagnostics", tuple(sorted(normalized)))

    def _validate_decision_invariants(self) -> None:
        if self.decision_type is DecisionType.ATTACH_EXISTING:
            if self.target_incident_id is None:
                raise ValueError("ATTACH_EXISTING requires target_incident_id")
            if self.anchor_strength is None:
                raise ValueError("ATTACH_EXISTING requires effective anchor_strength")
            if self.reason_code not in {
                DecisionReasonCode.EXACT_STRONG_IDENTITY_MATCH,
                DecisionReasonCode.UNIQUE_COMPATIBLE_CANDIDATE,
                DecisionReasonCode.WEAK_TO_STRONG_PROMOTION,
            }:
                raise ValueError("ATTACH_EXISTING reason_code is invalid")
        else:
            if self.target_incident_id is not None:
                raise ValueError(
                    "target_incident_id is only valid for ATTACH_EXISTING"
                )

        if self.decision_type is DecisionType.CREATE_NEW:
            if self.anchor_strength is None:
                raise ValueError("CREATE_NEW requires resulting anchor_strength")
            if self.reason_code not in {
                DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
                DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED,
            }:
                raise ValueError("CREATE_NEW reason_code is invalid")
        elif self.decision_type is DecisionType.ENTER_PENDING:
            if self.anchor_strength is not None:
                raise ValueError("ENTER_PENDING anchor_strength must be None")
            if self.reason_code not in {
                DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
                DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES,
            }:
                raise ValueError("ENTER_PENDING reason_code is invalid")
        elif self.decision_type is DecisionType.ROUTE_SHADOW:
            if self.anchor_strength is not None:
                raise ValueError("ROUTE_SHADOW anchor_strength must be None")
            if self.reason_code is not DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY:
                raise ValueError("ROUTE_SHADOW reason_code is invalid")
            if self.correlation_family is not CorrelationFamily.UNKNOWN:
                raise ValueError("ROUTE_SHADOW requires UNKNOWN correlation family")
            if self.normalized_fingerprint is not None:
                raise ValueError("ROUTE_SHADOW fingerprint must be None")

        if self.anchor_transition is AnchorTransition.WEAK_TO_STRONG:
            if (
                self.decision_type is not DecisionType.ATTACH_EXISTING
                or self.reason_code is not DecisionReasonCode.WEAK_TO_STRONG_PROMOTION
                or self.anchor_strength is not AnchorStrength.STRONG
                or self.normalized_fingerprint is None
            ):
                raise ValueError(
                    "WEAK_TO_STRONG requires promotion ATTACH_EXISTING"
                )
        elif self.reason_code is DecisionReasonCode.WEAK_TO_STRONG_PROMOTION:
            raise ValueError("promotion reason requires WEAK_TO_STRONG transition")

        if (
            self.decision_type is not DecisionType.ATTACH_EXISTING
            and self.anchor_transition is not AnchorTransition.NONE
        ):
            raise ValueError(
                "non-attach decisions require anchor_transition=NONE"
            )

        if self.reason_code is DecisionReasonCode.EXACT_STRONG_IDENTITY_MATCH:
            if (
                self.decision_type is not DecisionType.ATTACH_EXISTING
                or self.anchor_strength is not AnchorStrength.STRONG
                or self.anchor_transition is not AnchorTransition.NONE
                or self.normalized_fingerprint is None
            ):
                raise ValueError("exact Strong match invariants are not satisfied")
        elif self.reason_code is DecisionReasonCode.UNIQUE_COMPATIBLE_CANDIDATE:
            if (
                self.decision_type is not DecisionType.ATTACH_EXISTING
                or self.anchor_transition is not AnchorTransition.NONE
            ):
                raise ValueError("unique compatible candidate invariants are not satisfied")

        if self.decision_type is DecisionType.CREATE_NEW:
            if self.anchor_strength is AnchorStrength.STRONG:
                if self.normalized_fingerprint is None:
                    raise ValueError("Strong CREATE_NEW requires a fingerprint")
            elif (
                self.normalized_fingerprint is not None
                or self.reason_code
                is not DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED
            ):
                raise ValueError("Weak CREATE_NEW must be unresolved Pending expiry")


@dataclass(frozen=True, slots=True)
class CorrelationEvaluationError:
    error_code: CorrelationErrorCode
    event_id: str
    event_type: str
    policy_id: str | None = None
    policy_version: str | None = None
    incident_id: str | None = None
    field_path: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.error_code, CorrelationErrorCode):
            raise TypeError("error_code must be a CorrelationErrorCode")
        for field_name in ("event_id", "event_type", "message"):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"{field_name} must be a string")
        for field_name in (
            "policy_id",
            "policy_version",
            "incident_id",
            "field_path",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string or None")


@dataclass(frozen=True, slots=True)
class CorrelationEvaluationSuccess:
    decision: CorrelationDecision

    def __post_init__(self) -> None:
        if not isinstance(self.decision, CorrelationDecision):
            raise TypeError("Success requires a CorrelationDecision")


@dataclass(frozen=True, slots=True)
class CorrelationEvaluationFailure:
    error: CorrelationEvaluationError

    def __post_init__(self) -> None:
        if not isinstance(self.error, CorrelationEvaluationError):
            raise TypeError("Failure requires a CorrelationEvaluationError")


CorrelationEvaluationResult: TypeAlias = (
    CorrelationEvaluationSuccess | CorrelationEvaluationFailure
)
