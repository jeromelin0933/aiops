"""Deterministic capture-plan construction after trusted-core stabilization."""

from __future__ import annotations

from .contracts import (
    CaptureCommand,
    CapturePlan,
    EvidenceSource,
    IncidentCaptureProjection,
    PlannedSelector,
    TrustedEvent,
)
from .errors import invalid_capture_command
from .policy import EvidencePolicy
from .security import build_selector_fact
from .time_semantics import derive_collection_windows, derive_episode


def build_capture_plan(
    command: CaptureCommand,
    incident: IncidentCaptureProjection,
    events: tuple[TrustedEvent, ...],
    policy: EvidencePolicy,
) -> CapturePlan:
    """Build a pure plan. The caller must already have stabilized I1/I2."""
    expected = {
        "capture_contract_version": policy.capture_contract_version,
        "canonicalization_version": policy.canonicalization_version,
        "source_policy_version": policy.source_policy_version,
        "bounds_policy_version": policy.bounds_policy_version,
        "config_identity": policy.config_identity,
    }
    for field, value in expected.items():
        if getattr(command, field) != value:
            raise invalid_capture_command(
                f"{field} does not match the active evidence policy", field_path=field
            )

    episode = derive_episode(event.detected_at for event in events)
    windows = derive_collection_windows(
        episode,
        command.snapshot_at,
        logs_pre_seconds=policy.windows.logs_pre_seconds,
        metrics_pre_seconds=policy.windows.metrics_pre_seconds,
        post_seconds=policy.windows.post_seconds,
    )
    planned: list[PlannedSelector] = []
    event_by_id = {event.event_id: event for event in events}
    selector_source = event_by_id.get(incident.anchor_event_id) if incident.anchor_event_id else events[0]
    if selector_source is None:
        raise ValueError("Incident anchor must resolve to a trusted Event")
    for source in EvidenceSource:
        for field_name, value in selector_source.selector_values:
            if field_name in policy.selector_allowlist[source]:
                planned.append(
                    PlannedSelector(
                        build_selector_fact(
                            source,
                            field_name,
                            value,
                            selector_policy_version=policy.selector_policy_version,
                            allowlist=policy.selector_allowlist,
                        ),
                        selector_source.event_id,
                    )
                )
    selectors = tuple(
        sorted(
            planned,
            key=lambda item: (
                item.selector.source.value,
                item.selector.field_name,
                item.selector.normalized_value,
                item.source_event_id,
            ),
        )
    )
    boundary = windows.default_post_context_boundary
    return CapturePlan(
        command=command,
        incident=incident,
        events=events,
        episode=episode,
        windows=windows,
        selectors=selectors,
        selector_policy_version=policy.selector_policy_version,
        config_identity=policy.config_identity,
        capture_contract_version=policy.capture_contract_version,
        canonicalization_version=policy.canonicalization_version,
        source_policy_version=policy.source_policy_version,
        bounds_policy_version=policy.bounds_policy_version,
        logs_reached_post_context_boundary=windows.logs.end == boundary,
        metrics_reached_post_context_boundary=windows.metrics.end == boundary,
    )


__all__ = ["build_capture_plan"]
