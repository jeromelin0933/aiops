"""Typed identity policies and immutable registry for SPEC-006."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .contracts import (
    CorrelationErrorCode,
    CorrelationFamily,
    EvidenceClass,
    IdentityItem,
    NormalizedFingerprint,
)


class PolicyDefinitionError(ValueError):
    """A malformed registry definition that must fail during bootstrap."""


class IdentityExtractionError(ValueError):
    """An expected Strong identity contract failure."""

    def __init__(
        self,
        error_code: CorrelationErrorCode,
        field_path: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.field_path = field_path


class IdentityExtractor(ABC):
    """Read Strong identity fields directly from a Runtime Event mapping."""

    @abstractmethod
    def extract(self, event: Mapping[str, object]) -> tuple[IdentityItem, ...]:
        """Return normalized named identity fields or raise a domain error."""


@dataclass(frozen=True, slots=True)
class FieldPathIdentityExtractor(IdentityExtractor):
    """Extract one string identity from a fixed, policy-owned field path."""

    identity_field: str
    field_path: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.identity_field, str)
            or not self.identity_field
            or self.identity_field != self.identity_field.strip()
        ):
            raise PolicyDefinitionError(
                "identity extractor field name must be a non-empty string"
            )
        if not isinstance(self.field_path, tuple) or not self.field_path:
            raise PolicyDefinitionError("identity extractor field path must be non-empty")
        if any(
            not isinstance(segment, str)
            or not segment
            or segment != segment.strip()
            for segment in self.field_path
        ):
            raise PolicyDefinitionError(
                "identity extractor path segments must be non-empty strings"
            )

    def extract(self, event: Mapping[str, object]) -> tuple[IdentityItem, ...]:
        current: object = event
        traversed: list[str] = []
        full_path = ".".join(self.field_path)

        for segment in self.field_path:
            if not isinstance(current, Mapping):
                invalid_path = ".".join(traversed) or full_path
                raise IdentityExtractionError(
                    CorrelationErrorCode.INVALID_IDENTITY_VALUE,
                    invalid_path,
                    f"identity container at {invalid_path} must be a mapping",
                )
            traversed.append(segment)
            if segment not in current or current[segment] is None:
                raise IdentityExtractionError(
                    CorrelationErrorCode.MISSING_REQUIRED_IDENTITY,
                    ".".join(traversed),
                    f"required identity is missing at {'.'.join(traversed)}",
                )
            current = current[segment]

        if not isinstance(current, str):
            raise IdentityExtractionError(
                CorrelationErrorCode.INVALID_IDENTITY_VALUE,
                full_path,
                f"identity at {full_path} must be a string",
            )
        normalized = current.strip()
        if not normalized:
            raise IdentityExtractionError(
                CorrelationErrorCode.INVALID_IDENTITY_VALUE,
                full_path,
                f"identity at {full_path} must not be empty",
            )
        return ((self.identity_field, normalized),)


@dataclass(frozen=True, slots=True)
class CorrelationPolicy:
    policy_id: str
    policy_version: str
    event_type: str
    evidence_class: EvidenceClass
    correlation_family: CorrelationFamily
    identity_extractor: IdentityExtractor | None = None
    is_current: bool = True

    def __post_init__(self) -> None:
        for field_name in ("policy_id", "policy_version", "event_type"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise PolicyDefinitionError(
                    f"{field_name} must be a non-empty string without surrounding whitespace"
                )
        if not isinstance(self.evidence_class, EvidenceClass):
            raise PolicyDefinitionError("evidence_class must be an EvidenceClass")
        if not isinstance(self.correlation_family, CorrelationFamily):
            raise PolicyDefinitionError(
                "correlation_family must be a CorrelationFamily"
            )
        if not isinstance(self.is_current, bool):
            raise PolicyDefinitionError("is_current must be boolean")

        if self.evidence_class is EvidenceClass.STRONG:
            if not isinstance(self.identity_extractor, IdentityExtractor):
                raise PolicyDefinitionError("STRONG policy requires an identity extractor")
        elif self.identity_extractor is not None:
            raise PolicyDefinitionError(
                "KNOWN_WEAK and UNKNOWN policies must not define an identity extractor"
            )

        is_unknown_evidence = self.evidence_class is EvidenceClass.UNKNOWN
        is_unknown_family = self.correlation_family is CorrelationFamily.UNKNOWN
        if is_unknown_evidence != is_unknown_family:
            raise PolicyDefinitionError(
                "UNKNOWN evidence and UNKNOWN correlation family must be paired"
            )

    @property
    def reference(self) -> tuple[str, str]:
        return (self.policy_id, self.policy_version)

    def fingerprint_for(
        self,
        event: Mapping[str, object],
    ) -> NormalizedFingerprint:
        if self.identity_extractor is None:
            raise PolicyDefinitionError(
                f"policy {self.policy_id} does not define Strong identity"
            )
        return NormalizedFingerprint(
            event_type=self.event_type,
            identity=self.identity_extractor.extract(event),
        )


@dataclass(frozen=True, slots=True, init=False)
class PolicyRegistry:
    """Immutable current and exact-version indexes."""

    _policies: tuple[CorrelationPolicy, ...]
    _current_by_event_type: Mapping[str, CorrelationPolicy]
    _by_reference: Mapping[tuple[str, str], CorrelationPolicy]

    def __init__(self, policies: Iterable[CorrelationPolicy]) -> None:
        try:
            supplied = tuple(policies)
        except TypeError as exc:
            raise PolicyDefinitionError("policies must be an iterable") from exc
        if not supplied:
            raise PolicyDefinitionError("registry must contain at least one policy")

        current: dict[str, CorrelationPolicy] = {}
        exact: dict[tuple[str, str], CorrelationPolicy] = {}
        for policy in supplied:
            if not isinstance(policy, CorrelationPolicy):
                raise PolicyDefinitionError(
                    "registry definitions must be CorrelationPolicy instances"
                )
            if policy.reference in exact:
                raise PolicyDefinitionError(
                    "duplicate policy_id + policy_version: "
                    f"{policy.policy_id} {policy.policy_version}"
                )
            exact[policy.reference] = policy
            if policy.is_current:
                if policy.event_type in current:
                    raise PolicyDefinitionError(
                        f"duplicate current event_type: {policy.event_type}"
                    )
                current[policy.event_type] = policy

        canonical = tuple(
            sorted(
                supplied,
                key=lambda policy: (
                    policy.policy_id,
                    policy.policy_version,
                    policy.event_type,
                ),
            )
        )
        object.__setattr__(self, "_policies", canonical)
        object.__setattr__(
            self,
            "_current_by_event_type",
            MappingProxyType(dict(sorted(current.items()))),
        )
        object.__setattr__(
            self,
            "_by_reference",
            MappingProxyType(dict(sorted(exact.items()))),
        )

    @property
    def policies(self) -> tuple[CorrelationPolicy, ...]:
        return self._policies

    @property
    def current_by_event_type(self) -> Mapping[str, CorrelationPolicy]:
        return self._current_by_event_type

    def resolve_current(self, event_type: str) -> CorrelationPolicy | None:
        return self._current_by_event_type.get(event_type)

    def resolve_exact(
        self,
        policy_id: str,
        policy_version: str,
    ) -> CorrelationPolicy | None:
        return self._by_reference.get((policy_id, policy_version))


def _extractor(identity_field: str, *field_path: str) -> IdentityExtractor:
    return FieldPathIdentityExtractor(identity_field, tuple(field_path))


POC_V1_POLICIES = (
    CorrelationPolicy(
        "POLICY-BRUTE-FORCE-DETECTED",
        "1.0",
        "brute_force_detected",
        EvidenceClass.STRONG,
        CorrelationFamily.ATTACK_SOURCE,
        _extractor("source_ip", "source_ip"),
    ),
    CorrelationPolicy(
        "POLICY-CROSS-SERVICE-FAILURE",
        "1.0",
        "cross_service_failure",
        EvidenceClass.STRONG,
        CorrelationFamily.CROSS_SERVICE_LATENCY,
        _extractor("trace_id", "trace_id"),
    ),
    CorrelationPolicy(
        "POLICY-HIGH-LATENCY-DETECTED",
        "1.0",
        "high_latency_detected",
        EvidenceClass.KNOWN_WEAK,
        CorrelationFamily.CROSS_SERVICE_LATENCY,
    ),
    CorrelationPolicy(
        "POLICY-OOM-CRASH-DETECTED",
        "1.0",
        "oom_crash_detected",
        EvidenceClass.STRONG,
        CorrelationFamily.MEMORY_OOM,
        _extractor("service_name", "service_name"),
    ),
    CorrelationPolicy(
        "POLICY-HIGH-MEMORY-DETECTED",
        "1.0",
        "high_memory_detected",
        EvidenceClass.KNOWN_WEAK,
        CorrelationFamily.MEMORY_OOM,
    ),
    CorrelationPolicy(
        "POLICY-EXTERNAL-DEPENDENCY-FAILURE",
        "1.0",
        "external_dependency_failure",
        EvidenceClass.STRONG,
        CorrelationFamily.EXTERNAL_DEPENDENCY,
        _extractor("external_service", "external_service"),
    ),
    CorrelationPolicy(
        "POLICY-DOWNSTREAM-CASCADE-FAILURE",
        "1.0",
        "downstream_cascade_failure",
        EvidenceClass.STRONG,
        CorrelationFamily.DOWNSTREAM_CASCADE,
        _extractor("downstream_service", "downstream_service"),
    ),
    CorrelationPolicy(
        "POLICY-RATE-LIMIT-STORM",
        "1.0",
        "rate_limit_storm",
        EvidenceClass.STRONG,
        CorrelationFamily.RATE_LIMIT,
        _extractor(
            "target_service",
            "triggered_features",
            "target_service",
        ),
    ),
    CorrelationPolicy(
        "POLICY-REQUEST-SPIKE-DETECTED",
        "1.0",
        "request_spike_detected",
        EvidenceClass.KNOWN_WEAK,
        CorrelationFamily.RATE_LIMIT,
    ),
    CorrelationPolicy(
        "POLICY-GENERAL-LOG-ANOMALY",
        "1.0",
        "general_log_anomaly",
        EvidenceClass.UNKNOWN,
        CorrelationFamily.UNKNOWN,
    ),
    CorrelationPolicy(
        "POLICY-GENERAL-METRICS-ANOMALY",
        "1.0",
        "general_metrics_anomaly",
        EvidenceClass.UNKNOWN,
        CorrelationFamily.UNKNOWN,
    ),
)


DEFAULT_POLICY_REGISTRY = PolicyRegistry(POC_V1_POLICIES)
