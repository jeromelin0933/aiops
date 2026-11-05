"""Offline fixture composition only. Not a production or capture execution entry."""
from __future__ import annotations
from datetime import datetime
import math
from typing import Callable
from incident_evidence import CaptureTerminalKind
from runtime_orchestration import capture_controller as c
from runtime_orchestration.capture_controller import (
    VerifiedCaptureBinding, CaptureComponents, CaptureDenied, _time,
)

def drive_isolated_components(
    verified: VerifiedCaptureBinding, components: CaptureComponents, *,
    monotonic: Callable[[], float], utc_now: Callable[[], datetime],
    output_bytes: Callable[[], int],
) -> dict:
    """Internal composition exercised only on isolated existing components.

    Returned time/resource checks are soft bounds, not forced I/O cancellation.
    The production entry above is blocked rather than asserting hard safety.
    """
    facts, limits = verified.facts, verified.limits
    report = {"run_id": facts["child"]["run_id"], "status": "STOPPED",
              "cycles": 0, "snapshot_id": None, "revision_id": None,
              "provenance": {"execution_binding_sha256": verified.stop.execution_binding_sha256,
                             "source_revision": facts["source_revision"],
                             "input_sha256": facts["scenario_input_sha256"]},
              "raw_metric_samples": "NOT AVAILABLE", "started_at": _time(utc_now()).isoformat()}
    started = previous = monotonic()
    if not math.isfinite(started):
        raise CaptureDenied("untrusted monotonic clock")

    def boundary() -> None:
        nonlocal previous
        tick = monotonic()
        if not math.isfinite(tick) or tick < previous:
            raise CaptureDenied("untrusted monotonic clock")
        previous = tick
        if tick - started >= limits["max_seconds"] or _time(utc_now()) >= verified.expires_at:
            raise CaptureDenied("duration or validity exhausted")
        size = output_bytes()
        if type(size) is not int or not 0 <= size <= limits["max_output_bytes"]:
            raise CaptureDenied("output resource bound exceeded")

    try:
        if (components.core._rca is not None
                or components.core._capture_stop_binding != verified.stop):
            raise CaptureDenied("runtime is not bound to this capture-only operation")
        boundary()
        components.core.startup()
        if components.core.capture_failure is not None:
            raise CaptureDenied("startup capture failure")
        boundary()
        components.scenario.start()
        trigger = components.scenario.trigger(facts["case"].upper())
        if not trigger.accepted:
            raise CaptureDenied("scenario trigger refused")
        for cycle in range(limits["max_cycles"]):
            boundary()
            phase = components.scenario.tick()
            boundary()
            detected = components.detection.run_due_once()
            boundary()
            if detected.failure_count:
                raise CaptureDenied("detection cycle failed; no correlation intake")
            components.core.run_cycle()
            report["cycles"] = cycle + 1
            if components.core.capture_failure is not None or components.core._stop.requested:
                raise CaptureDenied("runtime capture stopped")
            boundary()
            if getattr(phase.phase, "value", phase.phase) != "BASELINE":
                continue
            views = components.incident_reader.list_correlation_views()
            if len(views) != 1:
                raise CaptureDenied("exactly one unambiguous operational Incident required")
            command = components.command_for(views[0].incident_id, utc_now())
            outcome = components.evidence.capture_evidence(command)
            boundary()
            if outcome.terminal_kind is not CaptureTerminalKind.SUCCESS:
                raise CaptureDenied("evidence did not reach terminal success")
            snapshot = components.evidence.resolve_snapshot(outcome.snapshot_id)
            revision = components.evidence.resolve_revision(outcome.revision_id)
            if (snapshot is None or revision is None
                    or snapshot.incident_id != command.incident_id
                    or revision.incident_id != command.incident_id
                    or snapshot.revision_id != revision.revision_id):
                raise CaptureDenied("public snapshot/revision binding mismatch")
            report.update(status="SUCCESS", snapshot_id=snapshot.snapshot_id,
                          revision_id=revision.revision_id)
            return report
        raise CaptureDenied("maximum cycles exhausted")
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        return report
    finally:
        # Request drain; never interrupt mutation, remove work, retry or resume.
        components.core._stop.request_stop()
        try:
            components.scenario.stop()
        finally:
            try:
                components.close()
            except Exception as exc:
                report.update(status="STOPPED", cleanup_failure_type=type(exc).__name__)
            report["ended_at"] = _time(utc_now()).isoformat()
            try:
                c._persist_report(verified, report)
            except Exception as exc:
                report.update(status="STOPPED", persistence_failure_type=type(exc).__name__)
