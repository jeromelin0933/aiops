"""Black-box Docker E2E fixture/probe using only public store APIs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

from alert_correlation.state import SqliteCorrelationStateStore
from event_detection.store.event_store import EventStore
from incident_management import SqliteIncidentStore
from runtime_orchestration import SqliteRuntimeWorkStore
from shadow_management import SqliteShadowStore


EVENT_PATH = Path("/app/events/event_store.jsonl")
STATE_ROOT = Path("/app/var/runtime_orchestration")
NOW = datetime.now(timezone.utc) - timedelta(seconds=5)
EVENT_IDS = (
    "DOCKER-A-CREATE",
    "DOCKER-B-ATTACH",
    "DOCKER-C-PENDING",
    "DOCKER-D-SHADOW",
)


def _event(event_id: str, event_type: str, *, source_ip: str | None = None) -> dict:
    return {
        "event_id": event_id,
        "detected_at": NOW.isoformat().replace("+00:00", "Z"),
        "event_source": "log_event_detection",
        "event_type": event_type,
        "detection_method": "rule_based",
        "severity": "HIGH",
        "confidence": 0.99,
        "service_name": "auth-api",
        "trace_id": None,
        "source_ip": source_ip,
        "downstream_service": None,
        "external_service": None,
        "status": "OPEN",
        "triggered_features": {},
        "raw_log_sample": [],
    }


def seed() -> None:
    store = EventStore(EVENT_PATH)
    if store.read_all_authoritative():
        raise RuntimeError("Docker E2E EventStore must start empty")
    for event in (
        _event("DOCKER-A-CREATE", "brute_force_detected", source_ip="203.0.113.70"),
        _event("DOCKER-B-ATTACH", "brute_force_detected", source_ip="203.0.113.70"),
        _event("DOCKER-C-PENDING", "high_latency_detected"),
        _event("DOCKER-D-SHADOW", "general_log_anomaly"),
    ):
        store.write(event)
    print(json.dumps({"seeded": len(EVENT_IDS)}, sort_keys=True))


def snapshot() -> None:
    state = SqliteCorrelationStateStore(STATE_ROOT / "correlation_state.sqlite3")
    incidents = SqliteIncidentStore(str(STATE_ROOT / "incidents.sqlite3"))
    shadows = SqliteShadowStore(STATE_ROOT / "shadows.sqlite3")
    work = SqliteRuntimeWorkStore(STATE_ROOT / "runtime_work.sqlite3")
    try:
        resolved = {event_id: state.resolve(event_id) for event_id in EVENT_IDS}
        incident_ids = sorted(
            {
                item.processed.incident_id
                for item in resolved.values()
                if item.processed is not None and item.processed.incident_id is not None
            }
        )
        incident_records = [incidents.get_incident(item) for item in incident_ids]
        result = {
            "states": {
                event_id: {
                    "processed": (
                        item.processed.terminal_outcome.value
                        if item.processed is not None
                        else None
                    ),
                    "incident_id": (
                        item.processed.incident_id if item.processed is not None else None
                    ),
                    "pending": item.pending is not None,
                }
                for event_id, item in resolved.items()
            },
            "incidents": [
                {
                    "incident_id": record.incident_id,
                    "status": record.status.value,
                    "assignee": record.assignee,
                    "events": list(record.event_ids),
                    "workflow_audits": len(
                        incidents.list_workflow_audit(record.incident_id)
                    ),
                }
                for record in incident_records
                if record is not None
            ],
            "shadow_exists": shadows.get_shadow_by_event_id("DOCKER-D-SHADOW")
            is not None,
            "shadow_has_incident_owner": incidents.event_has_incident_owner(
                "DOCKER-D-SHADOW"
            ),
            "work": [
                {
                    "work_id": record.work_id,
                    "status": record.status.value,
                    "attempt_count": record.attempt_count,
                }
                for record in work.enumerate_all().records
            ],
            "work_corruptions": len(work.enumerate_all().isolated_corruptions),
        }
        print(json.dumps(result, sort_keys=True))
    finally:
        state.close()
        incidents.close()
        shadows.close()
        work.close()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"seed", "snapshot"}:
        raise SystemExit("usage: e2e_probe.py seed|snapshot")
    seed() if sys.argv[1] == "seed" else snapshot()
