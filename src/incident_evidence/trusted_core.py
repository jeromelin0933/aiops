"""Fail-closed SPEC-013 trusted-core admission."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import math
from typing import Protocol

from incident_management import IncidentDomainError, IncidentRecord

from .capture_plan import build_capture_plan
from .contracts import (
    CaptureCommand,
    CapturePlan,
    IncidentCaptureProjection,
    TrustedEvent,
)
from .errors import EvidenceDomainError, EvidenceFailureKind, trusted_core_failure
from .policy import EvidencePolicy


_EVENT_FIELDS = frozenset(
    {
        "event_id", "detected_at", "event_source", "event_type",
        "detection_method", "severity", "confidence", "service_name",
        "trace_id", "source_ip", "downstream_service", "external_service",
        "status", "triggered_features", "raw_log_sample",
    }
)
_EVENT_CONTRACTS = {
    "log_event_detection": (
        "isolation_forest",
        frozenset(
            {
                "oom_crash_detected", "downstream_cascade_failure",
                "brute_force_detected", "rate_limit_storm",
                "external_dependency_failure", "cross_service_failure",
                "general_log_anomaly",
            }
        ),
    ),
    "metrics_threshold_detection": (
        "threshold",
        frozenset({"high_memory_detected", "high_latency_detected"}),
    ),
    "metrics_iforest_detection": (
        "isolation_forest",
        frozenset({"request_spike_detected", "general_metrics_anomaly"}),
    ),
}
_SEVERITIES = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
_SELECTOR_FIELDS = (
    "service_name", "trace_id", "source_ip", "downstream_service", "external_service",
    "target_service",
)


class IncidentReader(Protocol):
    def get_incident(self, incident_id: str) -> IncidentRecord | None: ...


class AuthoritativeEventReader(Protocol):
    def read_all_authoritative(self) -> list[dict]: ...


def _failure(kind: EvidenceFailureKind, message: str, field: str | None = None) -> EvidenceDomainError:
    return trusted_core_failure(kind, message, field_path=field)


def _enum_value(value: object, field: str) -> str:
    raw = getattr(value, "value", value)
    if not isinstance(raw, (str, int)):
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, f"Incident {field} is invalid", field)
    return str(getattr(value, "name", raw))


def _incident_projection(value: object, expected_id: str) -> IncidentCaptureProjection:
    if value is None:
        raise _failure(EvidenceFailureKind.INCIDENT_NOT_FOUND, "authoritative Incident was not found", "incident_id")
    try:
        incident_id = value.incident_id
        event_ids = tuple(value.event_ids)
        context = value.correlation_context
        fingerprint = context.normalized_fingerprint
        normalized_fingerprint = None if fingerprint is None else (
            fingerprint.event_type,
            tuple(fingerprint.identity),
        )
        projection = IncidentCaptureProjection(
            incident_id=incident_id,
            event_ids=event_ids,
            status=_enum_value(value.status, "status"),
            severity=_enum_value(value.severity, "severity"),
            created_at=_incident_time(value.created_at, "created_at"),
            updated_at=_incident_time(value.updated_at, "updated_at"),
            last_correlated_at=_incident_time(value.last_correlated_at, "last_correlated_at"),
            anchor_event_id=value.anchor_event_id,
            correlation_family=_enum_value(context.correlation_family, "correlation_family"),
            anchor_strength=_enum_value(context.anchor_strength, "anchor_strength"),
            anchor_event_type=context.anchor_event_type,
            normalized_fingerprint=normalized_fingerprint,
            anchor_policy_id=context.anchor_policy_id,
            anchor_policy_version=context.anchor_policy_version,
            promoted_from_weak=context.promoted_from_weak,
        )
    except EvidenceDomainError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "authoritative Incident projection is malformed") from exc
    if not isinstance(incident_id, str) or not incident_id or incident_id != incident_id.strip():
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident identity is invalid", "incident_id")
    if incident_id != expected_id:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident lookup identity contradicts returned content", "incident_id")
    if not event_ids:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident references no Events", "event_ids")
    if any(not isinstance(item, str) or not item or item != item.strip() for item in event_ids):
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident contains an invalid Event identity", "event_ids")
    if len(event_ids) != len(set(event_ids)):
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident contains duplicate Event identities", "event_ids")
    if projection.status not in {"OPEN", "ASSIGNED", "IN_PROGRESS", "AWAITING_REVIEW", "CLOSED"}:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident status is invalid", "status")
    if projection.severity not in _SEVERITIES:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident severity is invalid", "severity")
    if projection.correlation_family not in {
        "ATTACK_SOURCE", "CROSS_SERVICE_LATENCY", "MEMORY_OOM",
        "EXTERNAL_DEPENDENCY", "DOWNSTREAM_CASCADE", "RATE_LIMIT", "UNKNOWN",
    }:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident correlation family is invalid", "correlation_family")
    if projection.anchor_strength not in {"STRONG", "WEAK"}:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident anchor strength is invalid", "anchor_strength")
    if projection.updated_at < projection.created_at:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident timestamps contradict", "updated_at")
    if projection.anchor_event_id is not None and (
        not isinstance(projection.anchor_event_id, str)
        or not projection.anchor_event_id
        or projection.anchor_event_id != projection.anchor_event_id.strip()
        or projection.anchor_event_id not in event_ids
    ):
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident anchor does not resolve to a referenced Event", "anchor_event_id")
    if context.anchor_event_id != projection.anchor_event_id:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident anchor facts contradict", "anchor_event_id")
    for field in ("anchor_event_type", "anchor_policy_id", "anchor_policy_version"):
        item = getattr(projection, field)
        if item is not None and (not isinstance(item, str) or not item or item != item.strip()):
            raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, f"Incident {field} is invalid", field)
    if not isinstance(projection.promoted_from_weak, bool):
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident promoted_from_weak is invalid", "promoted_from_weak")
    if projection.anchor_strength == "STRONG":
        if (
            projection.anchor_event_id is None
            or projection.anchor_event_type is None
            or projection.normalized_fingerprint is None
            or projection.anchor_policy_id is None
            or projection.anchor_policy_version is None
        ):
            raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Strong Incident anchor is incomplete", "correlation_context")
        if projection.normalized_fingerprint[0] != projection.anchor_event_type:
            raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Incident fingerprint contradicts its anchor type", "correlation_context")
    elif any(
        item is not None
        for item in (
            projection.anchor_event_id,
            projection.anchor_event_type,
            projection.normalized_fingerprint,
        )
    ) or projection.promoted_from_weak:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "Weak Incident anchor facts are contradictory", "correlation_context")
    return projection


def _incident_time(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, f"Incident {field} is not an absolute time", field)
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, f"Incident {field} is invalid", field) from exc


def _read_incident(reader: IncidentReader, incident_id: str) -> IncidentCaptureProjection:
    try:
        return _incident_projection(reader.get_incident(incident_id), incident_id)
    except EvidenceDomainError:
        raise
    except IncidentDomainError as exc:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "authoritative Incident read failed closed") from exc
    except Exception as exc:
        raise _failure(EvidenceFailureKind.MALFORMED_INCIDENT_STATE, "authoritative Incident read failed closed") from exc


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _failure(EvidenceFailureKind.INVALID_REQUIRED_EVENT, f"Event {field} is invalid", field)
    return value


def _event_time(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise _failure(EvidenceFailureKind.INVALID_REQUIRED_EVENT, "Event detected_at is invalid", "detected_at")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise _failure(EvidenceFailureKind.INVALID_REQUIRED_EVENT, "Event detected_at is invalid", "detected_at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise _failure(EvidenceFailureKind.INVALID_REQUIRED_EVENT, "Event detected_at must be an unambiguous UTC instant", "detected_at")
    return parsed.astimezone(timezone.utc)


def _json_shape(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_shape(item) for item in value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and _json_shape(item) for key, item in value.items())
    return False


def _validate_event(raw: Mapping[str, object], expected_id: str) -> TrustedEvent:
    if set(raw) != _EVENT_FIELDS:
        raise _failure(EvidenceFailureKind.INVALID_REQUIRED_EVENT, "Event does not match the complete upstream contract")
    event_id = _identity(raw.get("event_id"), "event_id")
    if event_id != expected_id:
        raise _failure(EvidenceFailureKind.CONTRADICTORY_EVENT_IDENTITY, "authoritative Event identity contradicts its content", "event_id")
    event_source = _identity(raw.get("event_source"), "event_source")
    contract = _EVENT_CONTRACTS.get(event_source)
    if contract is None:
        raise _failure(EvidenceFailureKind.INVALID_REQUIRED_EVENT, "Event source is outside the upstream closed set", "event_source")
    event_type = _identity(raw.get("event_type"), "event_type")
    method = _identity(raw.get("detection_method"), "detection_method")
    expected_method, allowed_event_types = contract
    if method != expected_method or event_type not in allowed_event_types:
        raise _failure(
            EvidenceFailureKind.INVALID_REQUIRED_EVENT,
            "Event source, type and detection method contradict the upstream contract",
            "event_source",
        )
    severity = _identity(raw.get("severity"), "severity")
    if severity not in _SEVERITIES:
        raise _failure(EvidenceFailureKind.INVALID_REQUIRED_EVENT, "Event severity is invalid", "severity")
    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise _failure(EvidenceFailureKind.INVALID_REQUIRED_EVENT, "Event confidence is invalid", "confidence")
    _identity(raw.get("service_name"), "service_name")
    for field in ("trace_id", "source_ip", "downstream_service", "external_service"):
        value = raw.get(field)
        if value is not None:
            _identity(value, field)
    if raw.get("status") != "OPEN":
        raise _failure(EvidenceFailureKind.INVALID_REQUIRED_EVENT, "Event status is invalid", "status")
    if not isinstance(raw.get("triggered_features"), Mapping) or not _json_shape(raw["triggered_features"]):
        raise _failure(EvidenceFailureKind.INVALID_REQUIRED_EVENT, "Event triggered_features is invalid", "triggered_features")
    samples = raw.get("raw_log_sample")
    if not isinstance(samples, list) or not all(isinstance(item, Mapping) and _json_shape(item) for item in samples):
        raise _failure(EvidenceFailureKind.INVALID_REQUIRED_EVENT, "Event raw_log_sample is invalid", "raw_log_sample")
    selector_values: list[tuple[str, str]] = []
    for field in _SELECTOR_FIELDS:
        value = raw.get(field)
        if value is None and field == "target_service":
            value = raw["triggered_features"].get(field)
        if value is not None:
            selector_values.append((field, _identity(value, field)))
    return TrustedEvent(event_id, _event_time(raw.get("detected_at")), event_source, event_type, severity, tuple(selector_values))


def _validate_incident_event_consistency(
    incident: IncidentCaptureProjection,
    events: tuple[TrustedEvent, ...],
) -> None:
    """Cross-check SPEC-008 Strong anchor identity against Event authority."""
    if incident.anchor_strength != "STRONG":
        return
    event_by_id = {event.event_id: event for event in events}
    anchor = event_by_id.get(incident.anchor_event_id)
    if anchor is None:
        raise _failure(
            EvidenceFailureKind.CONTRADICTORY_EVENT_IDENTITY,
            "Incident anchor does not resolve to an authoritative Event",
            "anchor_event_id",
        )
    if anchor.event_type != incident.anchor_event_type:
        raise _failure(
            EvidenceFailureKind.CONTRADICTORY_EVENT_IDENTITY,
            "Incident anchor type contradicts the authoritative Event",
            "anchor_event_type",
        )
    selector_values = dict(anchor.selector_values)
    fingerprint_items = incident.normalized_fingerprint[1]
    for field_name, expected_value in fingerprint_items:
        if selector_values.get(field_name) != expected_value:
            raise _failure(
                EvidenceFailureKind.CONTRADICTORY_EVENT_IDENTITY,
                "Incident fingerprint contradicts the authoritative anchor Event",
                field_name,
            )


def _resolve_events(event_reader: AuthoritativeEventReader, referenced: tuple[str, ...]) -> tuple[TrustedEvent, ...]:
    try:
        enumeration = event_reader.read_all_authoritative()
    except Exception as exc:
        raise _failure(EvidenceFailureKind.AUTHORITATIVE_EVENT_ENUMERATION_FAILURE, "authoritative Event enumeration failed") from exc
    if not isinstance(enumeration, list):
        raise _failure(EvidenceFailureKind.AUTHORITATIVE_EVENT_ENUMERATION_FAILURE, "authoritative Event enumeration returned an invalid shape")
    by_id: dict[str, Mapping[str, object]] = {}
    for raw in enumeration:
        if not isinstance(raw, Mapping):
            raise _failure(EvidenceFailureKind.AUTHORITATIVE_EVENT_ENUMERATION_FAILURE, "authoritative Event enumeration contains a non-object")
        event_id = raw.get("event_id")
        if not isinstance(event_id, str) or not event_id or event_id != event_id.strip():
            raise _failure(EvidenceFailureKind.CONTRADICTORY_EVENT_IDENTITY, "authoritative Event has no canonical identity", "event_id")
        if event_id in by_id:
            raise _failure(EvidenceFailureKind.DUPLICATE_EVENT_ID, "authoritative Event identity is duplicated", "event_id")
        by_id[event_id] = raw
    resolved: list[TrustedEvent] = []
    for event_id in referenced:
        raw = by_id.get(event_id)
        if raw is None:
            raise _failure(EvidenceFailureKind.REFERENCED_EVENT_NOT_FOUND, "Incident references an Event absent from authority", "event_ids")
        resolved.append(_validate_event(raw, event_id))
    return tuple(resolved)


def admit_capture_plan(
    command: CaptureCommand,
    incident_reader: IncidentReader,
    event_reader: AuthoritativeEventReader,
    policy: EvidencePolicy,
) -> CapturePlan:
    """Perform I1 -> one Event enumeration/validation -> I2 -> plan admission."""
    if not isinstance(command, CaptureCommand):
        raise TypeError("command must be a CaptureCommand")
    if not isinstance(policy, EvidencePolicy):
        raise TypeError("policy must be an EvidencePolicy")
    first = _read_incident(incident_reader, command.incident_id)
    events = _resolve_events(event_reader, first.event_ids)
    _validate_incident_event_consistency(first, events)
    try:
        second = _read_incident(incident_reader, command.incident_id)
    except EvidenceDomainError as exc:
        if exc.kind is EvidenceFailureKind.INCIDENT_NOT_FOUND:
            raise _failure(
                EvidenceFailureKind.INCIDENT_CHANGED_DURING_CAPTURE,
                "Incident disappeared during trusted-core admission",
            ) from exc
        raise
    if first != second:
        raise _failure(EvidenceFailureKind.INCIDENT_CHANGED_DURING_CAPTURE, "Incident capture-relevant facts changed during trusted-core admission")
    return build_capture_plan(command, first, events, policy)


__all__ = ["AuthoritativeEventReader", "IncidentReader", "admit_capture_plan"]
