"""Shared validation and candidate foundation for alert correlation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

from .contracts import (
    AnchorStrength,
    AnchorTransition,
    CorrelationDecision,
    CorrelationErrorCode,
    CorrelationEvaluationContext,
    CorrelationEvaluationError,
    CorrelationEvaluationFailure,
    CorrelationEvaluationResult,
    CorrelationEvaluationSuccess,
    CorrelationFamily,
    DecisionReasonCode,
    DecisionType,
    EvaluationPhase,
    EvidenceClass,
    IncidentCorrelationView,
    NormalizedFingerprint,
)
from .policy import (
    DEFAULT_POLICY_REGISTRY,
    CorrelationPolicy,
    IdentityExtractionError,
    PolicyRegistry,
)


_LIFECYCLE_OPEN = frozenset({"OPEN", "ASSIGNED", "IN_PROGRESS"})
_LIFECYCLE_CLOSED = frozenset({"AWAITING_REVIEW", "CLOSED"})
_KNOWN_LIFECYCLE_STATUSES = _LIFECYCLE_OPEN | _LIFECYCLE_CLOSED


@dataclass(frozen=True, slots=True)
class CorrelationEngineConfig:
    correlation_window_seconds: float = 120

    def __post_init__(self) -> None:
        value = self.correlation_window_seconds
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError("correlation_window_seconds must be a finite positive number")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized <= 0:
            raise ValueError(
                "correlation_window_seconds must be a finite positive number"
            )
        object.__setattr__(self, "correlation_window_seconds", normalized)


@dataclass(frozen=True, slots=True)
class _EventEnvelope:
    event_id: str
    event_type: str
    detected_at: datetime


@dataclass(frozen=True, slots=True)
class _StageOneIncident:
    view: IncidentCorrelationView
    last_correlated_at: datetime


@dataclass(frozen=True, slots=True)
class _CandidateView:
    incident_id: str
    status: str
    last_correlated_at: datetime
    correlation_family: CorrelationFamily
    anchor_strength: AnchorStrength
    normalized_fingerprint: NormalizedFingerprint | None
    anchor_event_type: str | None


@dataclass(frozen=True, slots=True)
class _EvaluationFoundation:
    """Validated inputs and candidate sets consumed by Phase 4 decisions."""

    event_id: str
    event_type: str
    detected_at: datetime
    context: CorrelationEvaluationContext
    policy: CorrelationPolicy
    normalized_fingerprint: NormalizedFingerprint | None = None
    tier_1_candidates: tuple[_CandidateView, ...] = ()
    tier_2_candidates: tuple[_CandidateView, ...] = ()
    compatible_candidates: tuple[_CandidateView, ...] = ()


_FoundationResult = _EvaluationFoundation | CorrelationEvaluationFailure
_PolicyResult = CorrelationPolicy | CorrelationEvaluationFailure
_StageOneResult = _StageOneIncident | CorrelationEvaluationFailure | None
_StageTwoResult = _CandidateView | CorrelationEvaluationFailure


@dataclass(frozen=True, slots=True, init=False)
class AlertCorrelationPolicyEngine:
    """Pure deterministic SPEC-006 correlation decision engine."""

    config: CorrelationEngineConfig
    registry: PolicyRegistry

    def __init__(
        self,
        config: CorrelationEngineConfig | None = None,
        registry: PolicyRegistry | None = None,
    ) -> None:
        resolved_config = config if config is not None else CorrelationEngineConfig()
        resolved_registry = (
            registry if registry is not None else DEFAULT_POLICY_REGISTRY
        )
        if not isinstance(resolved_config, CorrelationEngineConfig):
            raise TypeError("config must be a CorrelationEngineConfig")
        if not isinstance(resolved_registry, PolicyRegistry):
            raise TypeError("registry must be a PolicyRegistry")
        object.__setattr__(self, "config", resolved_config)
        object.__setattr__(self, "registry", resolved_registry)

    def evaluate(
        self,
        event: Mapping[str, object],
        incident_views: Sequence[IncidentCorrelationView],
        context: CorrelationEvaluationContext,
    ) -> CorrelationEvaluationResult:
        """Return one business Decision or one structured domain Failure."""
        prepared = self._prepare_evaluation(event, incident_views, context)
        if isinstance(prepared, CorrelationEvaluationFailure):
            return prepared

        if prepared.policy.evidence_class is EvidenceClass.UNKNOWN:
            decision = CorrelationDecision(
                decision_type=DecisionType.ROUTE_SHADOW,
                policy_id=prepared.policy.policy_id,
                policy_version=prepared.policy.policy_version,
                correlation_family=prepared.policy.correlation_family,
                reason_code=DecisionReasonCode.INSUFFICIENT_OPERATIONAL_IDENTITY,
            )
        elif prepared.policy.evidence_class is EvidenceClass.STRONG:
            decision = self._decide_strong(prepared)
        elif prepared.policy.evidence_class is EvidenceClass.KNOWN_WEAK:
            decision = self._decide_known_weak(prepared)
        else:  # Registry bootstrap makes unsupported evidence unreachable.
            raise RuntimeError("unsupported policy evidence class")
        return CorrelationEvaluationSuccess(decision)

    @staticmethod
    def _decide_strong(foundation: _EvaluationFoundation) -> CorrelationDecision:
        fingerprint = foundation.normalized_fingerprint
        if fingerprint is None:
            raise RuntimeError("STRONG evaluation has no normalized fingerprint")

        candidates = foundation.compatible_candidates
        candidate_count = len(candidates)
        diagnostics = (("candidate_count", candidate_count),)

        if candidate_count == 1:
            candidate = candidates[0]
            if foundation.tier_1_candidates:
                reason = DecisionReasonCode.EXACT_STRONG_IDENTITY_MATCH
                transition = AnchorTransition.NONE
            else:
                reason = DecisionReasonCode.WEAK_TO_STRONG_PROMOTION
                transition = AnchorTransition.WEAK_TO_STRONG
            return CorrelationDecision(
                decision_type=DecisionType.ATTACH_EXISTING,
                policy_id=foundation.policy.policy_id,
                policy_version=foundation.policy.policy_version,
                correlation_family=foundation.policy.correlation_family,
                reason_code=reason,
                target_incident_id=candidate.incident_id,
                normalized_fingerprint=fingerprint,
                anchor_strength=AnchorStrength.STRONG,
                anchor_transition=transition,
                diagnostics=diagnostics,
            )

        if foundation.context.evaluation_phase is EvaluationPhase.PENDING_EXPIRED:
            return CorrelationDecision(
                decision_type=DecisionType.CREATE_NEW,
                policy_id=foundation.policy.policy_id,
                policy_version=foundation.policy.policy_version,
                correlation_family=foundation.policy.correlation_family,
                reason_code=DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED,
                normalized_fingerprint=fingerprint,
                anchor_strength=AnchorStrength.STRONG,
                diagnostics=diagnostics,
            )

        if (
            foundation.context.evaluation_phase is EvaluationPhase.INITIAL
            and candidate_count == 0
        ):
            return CorrelationDecision(
                decision_type=DecisionType.CREATE_NEW,
                policy_id=foundation.policy.policy_id,
                policy_version=foundation.policy.policy_version,
                correlation_family=foundation.policy.correlation_family,
                reason_code=DecisionReasonCode.NO_COMPATIBLE_CANDIDATE,
                normalized_fingerprint=fingerprint,
                anchor_strength=AnchorStrength.STRONG,
                diagnostics=diagnostics,
            )

        reason = (
            DecisionReasonCode.NO_COMPATIBLE_CANDIDATE
            if candidate_count == 0
            else DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES
        )
        return CorrelationDecision(
            decision_type=DecisionType.ENTER_PENDING,
            policy_id=foundation.policy.policy_id,
            policy_version=foundation.policy.policy_version,
            correlation_family=foundation.policy.correlation_family,
            reason_code=reason,
            normalized_fingerprint=fingerprint,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _decide_known_weak(
        foundation: _EvaluationFoundation,
    ) -> CorrelationDecision:
        candidates = foundation.compatible_candidates
        candidate_count = len(candidates)
        diagnostics = (("candidate_count", candidate_count),)

        if candidate_count == 1:
            candidate = candidates[0]
            return CorrelationDecision(
                decision_type=DecisionType.ATTACH_EXISTING,
                policy_id=foundation.policy.policy_id,
                policy_version=foundation.policy.policy_version,
                correlation_family=foundation.policy.correlation_family,
                reason_code=DecisionReasonCode.UNIQUE_COMPATIBLE_CANDIDATE,
                target_incident_id=candidate.incident_id,
                anchor_strength=candidate.anchor_strength,
                diagnostics=diagnostics,
            )

        if foundation.context.evaluation_phase is EvaluationPhase.PENDING_EXPIRED:
            return CorrelationDecision(
                decision_type=DecisionType.CREATE_NEW,
                policy_id=foundation.policy.policy_id,
                policy_version=foundation.policy.policy_version,
                correlation_family=foundation.policy.correlation_family,
                reason_code=DecisionReasonCode.PENDING_EXPIRED_UNRESOLVED,
                anchor_strength=AnchorStrength.WEAK,
                diagnostics=diagnostics,
            )

        reason = (
            DecisionReasonCode.NO_COMPATIBLE_CANDIDATE
            if candidate_count == 0
            else DecisionReasonCode.MULTIPLE_COMPATIBLE_CANDIDATES
        )
        return CorrelationDecision(
            decision_type=DecisionType.ENTER_PENDING,
            policy_id=foundation.policy.policy_id,
            policy_version=foundation.policy.policy_version,
            correlation_family=foundation.policy.correlation_family,
            reason_code=reason,
            diagnostics=diagnostics,
        )

    def _prepare_evaluation(
        self,
        event: Mapping[str, object],
        incident_views: Sequence[IncidentCorrelationView],
        context: CorrelationEvaluationContext,
    ) -> _FoundationResult:
        """Validate inputs and collect candidates without making a Decision."""
        envelope = self._validate_event_envelope(event)
        if isinstance(envelope, CorrelationEvaluationFailure):
            return envelope

        policy = self._resolve_policy(envelope, context)
        if isinstance(policy, CorrelationEvaluationFailure):
            return policy

        fingerprint: NormalizedFingerprint | None = None
        if policy.evidence_class is EvidenceClass.STRONG:
            try:
                fingerprint = policy.fingerprint_for(event)
            except IdentityExtractionError as exc:
                return self._failure(
                    exc.error_code,
                    envelope,
                    policy=policy,
                    field_path=exc.field_path,
                    message=str(exc),
                )

        foundation = _EvaluationFoundation(
            event_id=envelope.event_id,
            event_type=envelope.event_type,
            detected_at=envelope.detected_at,
            context=context,
            policy=policy,
            normalized_fingerprint=fingerprint,
        )

        # UNKNOWN is resolved and routed before operational View validation.
        if policy.evidence_class is EvidenceClass.UNKNOWN:
            return foundation

        candidates = self._collect_eligible_candidates(
            envelope,
            policy,
            incident_views,
        )
        if isinstance(candidates, CorrelationEvaluationFailure):
            return candidates

        same_family = tuple(
            candidate
            for candidate in candidates
            if candidate.correlation_family is policy.correlation_family
        )

        if policy.evidence_class is EvidenceClass.KNOWN_WEAK:
            return _EvaluationFoundation(
                event_id=envelope.event_id,
                event_type=envelope.event_type,
                detected_at=envelope.detected_at,
                context=context,
                policy=policy,
                compatible_candidates=same_family,
            )

        if fingerprint is None:  # Registry bootstrap makes this unreachable.
            raise RuntimeError("STRONG policy produced no normalized fingerprint")

        tier_1 = tuple(
            candidate
            for candidate in same_family
            if candidate.anchor_strength is AnchorStrength.STRONG
            and candidate.normalized_fingerprint == fingerprint
        )
        tier_2: tuple[_CandidateView, ...] = ()
        if not tier_1:
            tier_2 = tuple(
                candidate
                for candidate in same_family
                if candidate.anchor_strength is AnchorStrength.WEAK
            )

        return _EvaluationFoundation(
            event_id=envelope.event_id,
            event_type=envelope.event_type,
            detected_at=envelope.detected_at,
            context=context,
            policy=policy,
            normalized_fingerprint=fingerprint,
            tier_1_candidates=tier_1,
            tier_2_candidates=tier_2,
            compatible_candidates=tier_1 if tier_1 else tier_2,
        )

    def _validate_event_envelope(
        self,
        event: object,
    ) -> _EventEnvelope | CorrelationEvaluationFailure:
        if not isinstance(event, Mapping):
            return self._envelope_failure(
                "",
                "",
                "event",
                "event must be a mapping",
            )

        raw_event_id = event.get("event_id")
        raw_event_type = event.get("event_type")
        event_id = raw_event_id if isinstance(raw_event_id, str) else ""
        event_type = raw_event_type if isinstance(raw_event_type, str) else ""

        if (
            not isinstance(raw_event_id, str)
            or not raw_event_id
            or raw_event_id != raw_event_id.strip()
        ):
            return self._envelope_failure(
                event_id,
                event_type,
                "event_id",
                "event_id must be a non-empty string without surrounding whitespace",
            )
        if (
            not isinstance(raw_event_type, str)
            or not raw_event_type
            or raw_event_type != raw_event_type.strip()
        ):
            return self._envelope_failure(
                event_id,
                event_type,
                "event_type",
                "event_type must be a non-empty string without surrounding whitespace",
            )

        detected_at = self._parse_timestamp(
            event.get("detected_at"),
            string_only=True,
            require_utc=True,
        )
        if detected_at is None:
            return self._envelope_failure(
                event_id,
                event_type,
                "detected_at",
                "detected_at must be a parseable timezone-aware UTC ISO 8601 string",
            )
        return _EventEnvelope(event_id, event_type, detected_at)

    def _resolve_policy(
        self,
        envelope: _EventEnvelope,
        context: object,
    ) -> _PolicyResult:
        if not isinstance(context, CorrelationEvaluationContext):
            return self._failure(
                CorrelationErrorCode.INCONSISTENT_CORRELATION_CONTEXT,
                envelope,
                field_path="context",
                message="context must be a CorrelationEvaluationContext",
            )
        if not isinstance(context.evaluation_phase, EvaluationPhase):
            return self._failure(
                CorrelationErrorCode.INCONSISTENT_CORRELATION_CONTEXT,
                envelope,
                field_path="evaluation_phase",
                message="evaluation_phase must be a supported EvaluationPhase",
            )

        if context.evaluation_phase is EvaluationPhase.INITIAL:
            policy = self.registry.resolve_current(envelope.event_type)
            if policy is None:
                return self._failure(
                    CorrelationErrorCode.POLICY_NOT_REGISTERED,
                    envelope,
                    message="event_type has no current correlation policy",
                )
            reference_error = self._validate_optional_initial_reference(
                envelope,
                context,
                policy,
            )
            return reference_error if reference_error is not None else policy

        reference_error = self._validate_required_policy_reference(envelope, context)
        if reference_error is not None:
            return reference_error
        assert context.policy_id is not None
        assert context.policy_version is not None

        policy = self.registry.resolve_exact(context.policy_id, context.policy_version)
        if policy is None:
            return self._failure(
                CorrelationErrorCode.POLICY_VERSION_UNAVAILABLE,
                envelope,
                policy_id=context.policy_id,
                policy_version=context.policy_version,
                field_path="policy_id,policy_version",
                message="required exact policy_id + policy_version is unavailable",
            )
        if policy.event_type != envelope.event_type:
            return self._failure(
                CorrelationErrorCode.INCONSISTENT_CORRELATION_CONTEXT,
                envelope,
                policy=policy,
                field_path="event_type",
                message="historical policy event_type does not match the Event",
            )
        return policy

    def _validate_optional_initial_reference(
        self,
        envelope: _EventEnvelope,
        context: CorrelationEvaluationContext,
        policy: CorrelationPolicy,
    ) -> CorrelationEvaluationFailure | None:
        policy_id = context.policy_id
        policy_version = context.policy_version
        if policy_id is None and policy_version is None:
            return None
        reference_error = self._validate_policy_reference_values(envelope, context)
        if reference_error is not None:
            return reference_error
        if (policy_id, policy_version) != policy.reference:
            return self._failure(
                CorrelationErrorCode.INCONSISTENT_CORRELATION_CONTEXT,
                envelope,
                policy_id=policy_id,
                policy_version=policy_version,
                field_path="policy_id,policy_version",
                message="INITIAL policy reference does not match the current Event policy",
            )
        return None

    def _validate_required_policy_reference(
        self,
        envelope: _EventEnvelope,
        context: CorrelationEvaluationContext,
    ) -> CorrelationEvaluationFailure | None:
        if context.policy_id is None or context.policy_version is None:
            return self._failure(
                CorrelationErrorCode.INCONSISTENT_CORRELATION_CONTEXT,
                envelope,
                policy_id=context.policy_id,
                policy_version=context.policy_version,
                field_path="policy_id,policy_version",
                message="Pending phases require exact policy_id + policy_version",
            )
        return self._validate_policy_reference_values(envelope, context)

    def _validate_policy_reference_values(
        self,
        envelope: _EventEnvelope,
        context: CorrelationEvaluationContext,
    ) -> CorrelationEvaluationFailure | None:
        for field_path, value in (
            ("policy_id", context.policy_id),
            ("policy_version", context.policy_version),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
            ):
                return self._failure(
                    CorrelationErrorCode.INCONSISTENT_CORRELATION_CONTEXT,
                    envelope,
                    policy_id=(
                        context.policy_id
                        if isinstance(context.policy_id, str)
                        else None
                    ),
                    policy_version=(
                        context.policy_version
                        if isinstance(context.policy_version, str)
                        else None
                    ),
                    field_path=field_path,
                    message=(
                        f"{field_path} must be a non-empty string without "
                        "surrounding whitespace"
                    ),
                )
        return None

    def _collect_eligible_candidates(
        self,
        envelope: _EventEnvelope,
        policy: CorrelationPolicy,
        incident_views: object,
    ) -> tuple[_CandidateView, ...] | CorrelationEvaluationFailure:
        if not isinstance(incident_views, Sequence):
            return self._failure(
                CorrelationErrorCode.INVALID_INCIDENT_VIEW,
                envelope,
                policy=policy,
                field_path="incident_views",
                message="incident_views must be a Sequence of IncidentCorrelationView",
            )
        supplied_views = tuple(incident_views)

        candidates: list[_CandidateView] = []
        failures: list[CorrelationEvaluationFailure] = []
        for view in supplied_views:
            stage_one = self._validate_stage_one(envelope, policy, view)
            if isinstance(stage_one, CorrelationEvaluationFailure):
                failures.append(stage_one)
                continue
            if stage_one is None:
                continue

            stage_two = self._validate_stage_two(envelope, policy, stage_one)
            if isinstance(stage_two, CorrelationEvaluationFailure):
                failures.append(stage_two)
                continue
            candidates.append(stage_two)

        if failures:
            return min(failures, key=self._failure_sort_key)
        return tuple(sorted(candidates, key=self._candidate_sort_key))

    def _validate_stage_one(
        self,
        envelope: _EventEnvelope,
        policy: CorrelationPolicy,
        view: object,
    ) -> _StageOneResult:
        if not isinstance(view, IncidentCorrelationView):
            return self._failure(
                CorrelationErrorCode.INVALID_INCIDENT_VIEW,
                envelope,
                policy=policy,
                field_path="incident_views",
                message="each incident view must be an IncidentCorrelationView",
            )

        incident_id = view.incident_id
        safe_incident_id = incident_id if isinstance(incident_id, str) else None
        if (
            not isinstance(incident_id, str)
            or not incident_id
            or incident_id != incident_id.strip()
        ):
            return self._failure(
                CorrelationErrorCode.INVALID_INCIDENT_VIEW,
                envelope,
                policy=policy,
                incident_id=safe_incident_id,
                field_path="incident_id",
                message="incident_id must be a non-empty reference",
            )
        if not isinstance(view.status, str) or view.status not in _KNOWN_LIFECYCLE_STATUSES:
            return self._failure(
                CorrelationErrorCode.INVALID_INCIDENT_VIEW,
                envelope,
                policy=policy,
                incident_id=incident_id,
                field_path="status",
                message="incident status is not a supported lifecycle value",
            )

        last_correlated_at = self._parse_timestamp(
            view.last_correlated_at,
            string_only=False,
            require_utc=False,
        )
        if last_correlated_at is None:
            return self._failure(
                CorrelationErrorCode.INVALID_INCIDENT_VIEW,
                envelope,
                policy=policy,
                incident_id=incident_id,
                field_path="last_correlated_at",
                message="last_correlated_at must be safely comparable with detected_at",
            )

        if view.status in _LIFECYCLE_CLOSED:
            return None

        delta_seconds = (envelope.detected_at - last_correlated_at).total_seconds()
        if not 0 <= delta_seconds <= self.config.correlation_window_seconds:
            return None
        return _StageOneIncident(view, last_correlated_at)

    def _validate_stage_two(
        self,
        envelope: _EventEnvelope,
        policy: CorrelationPolicy,
        stage_one: _StageOneIncident,
    ) -> _StageTwoResult:
        view = stage_one.view
        if not isinstance(view.correlation_family, CorrelationFamily):
            return self._failure(
                CorrelationErrorCode.INVALID_INCIDENT_VIEW,
                envelope,
                policy=policy,
                incident_id=view.incident_id,
                field_path="correlation_family",
                message="eligible incident correlation_family is invalid",
            )
        if not isinstance(view.anchor_strength, AnchorStrength):
            return self._failure(
                CorrelationErrorCode.INVALID_INCIDENT_VIEW,
                envelope,
                policy=policy,
                incident_id=view.incident_id,
                field_path="anchor_strength",
                message="eligible incident anchor_strength is invalid",
            )

        if view.anchor_strength is AnchorStrength.WEAK:
            if view.normalized_fingerprint is not None:
                return self._failure(
                    CorrelationErrorCode.INVALID_INCIDENT_VIEW,
                    envelope,
                    policy=policy,
                    incident_id=view.incident_id,
                    field_path="normalized_fingerprint",
                    message="Weak standalone incident must not have a Strong fingerprint",
                )
            if view.anchor_event_type is not None:
                return self._failure(
                    CorrelationErrorCode.INVALID_INCIDENT_VIEW,
                    envelope,
                    policy=policy,
                    incident_id=view.incident_id,
                    field_path="anchor_event_type",
                    message="Weak standalone incident must not have a Strong anchor type",
                )
        else:
            if not isinstance(view.normalized_fingerprint, NormalizedFingerprint):
                return self._failure(
                    CorrelationErrorCode.INVALID_INCIDENT_VIEW,
                    envelope,
                    policy=policy,
                    incident_id=view.incident_id,
                    field_path="normalized_fingerprint",
                    message="Strong incident requires a normalized fingerprint",
                )
            if (
                not isinstance(view.anchor_event_type, str)
                or not view.anchor_event_type
                or view.anchor_event_type != view.anchor_event_type.strip()
            ):
                return self._failure(
                    CorrelationErrorCode.INVALID_INCIDENT_VIEW,
                    envelope,
                    policy=policy,
                    incident_id=view.incident_id,
                    field_path="anchor_event_type",
                    message="Strong incident requires a valid anchor_event_type",
                )
            if view.anchor_event_type != view.normalized_fingerprint.event_type:
                return self._failure(
                    CorrelationErrorCode.INVALID_INCIDENT_VIEW,
                    envelope,
                    policy=policy,
                    incident_id=view.incident_id,
                    field_path="anchor_event_type",
                    message="anchor_event_type must match fingerprint event_type",
                )

        return _CandidateView(
            incident_id=view.incident_id,
            status=view.status,
            last_correlated_at=stage_one.last_correlated_at,
            correlation_family=view.correlation_family,
            anchor_strength=view.anchor_strength,
            normalized_fingerprint=view.normalized_fingerprint,
            anchor_event_type=view.anchor_event_type,
        )

    @staticmethod
    def _parse_timestamp(
        value: object,
        *,
        string_only: bool,
        require_utc: bool,
    ) -> datetime | None:
        parsed: datetime
        if isinstance(value, str):
            if not value or value != value.strip():
                return None
            candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError:
                return None
        elif isinstance(value, datetime) and not string_only:
            parsed = value
        else:
            return None

        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        if require_utc and parsed.utcoffset() != timedelta(0):
            return None
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _candidate_sort_key(candidate: _CandidateView) -> tuple[object, ...]:
        fingerprint = candidate.normalized_fingerprint
        return (
            candidate.incident_id,
            candidate.status,
            candidate.last_correlated_at.isoformat(),
            candidate.correlation_family.value,
            candidate.anchor_strength.value,
            fingerprint.event_type if fingerprint is not None else "",
            fingerprint.identity if fingerprint is not None else (),
            candidate.anchor_event_type or "",
        )

    @staticmethod
    def _failure_sort_key(
        failure: CorrelationEvaluationFailure,
    ) -> tuple[str, ...]:
        error = failure.error
        return (
            error.error_code.value,
            error.incident_id or "",
            error.field_path or "",
            error.message,
        )

    @staticmethod
    def _envelope_failure(
        event_id: str,
        event_type: str,
        field_path: str,
        message: str,
    ) -> CorrelationEvaluationFailure:
        return CorrelationEvaluationFailure(
            CorrelationEvaluationError(
                error_code=CorrelationErrorCode.INVALID_EVENT_ENVELOPE,
                event_id=event_id,
                event_type=event_type,
                field_path=field_path,
                message=message,
            )
        )

    @staticmethod
    def _failure(
        error_code: CorrelationErrorCode,
        envelope: _EventEnvelope,
        *,
        policy: CorrelationPolicy | None = None,
        policy_id: str | None = None,
        policy_version: str | None = None,
        incident_id: str | None = None,
        field_path: str | None = None,
        message: str,
    ) -> CorrelationEvaluationFailure:
        return CorrelationEvaluationFailure(
            CorrelationEvaluationError(
                error_code=error_code,
                event_id=envelope.event_id,
                event_type=envelope.event_type,
                policy_id=policy.policy_id if policy is not None else policy_id,
                policy_version=(
                    policy.policy_version if policy is not None else policy_version
                ),
                incident_id=incident_id,
                field_path=field_path,
                message=message,
            )
        )
