import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from runtime_orchestration.clock import RuntimeClock, canonical_utc, format_utc
from runtime_orchestration.config import RuntimeConfigError, load_runtime_config
from runtime_orchestration.identity import (
    automatic_assignment_operation_id,
    runtime_operation_id,
    runtime_work_id,
)
from runtime_orchestration.telemetry import RuntimeTelemetryEvent, StdlibRuntimeTelemetry


UTC_INSTANT = datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc)
OFFSET_INSTANT = datetime(2026, 9, 13, 10, 0, tzinfo=timezone(timedelta(hours=8)))


def test_runtime_clock_accepts_timezone_aware_equivalent_instants_without_real_sleep():
    slept: list[float] = []
    clock = RuntimeClock(lambda: OFFSET_INSTANT, lambda: 12.5, slept.append)

    assert clock.now() == UTC_INSTANT
    assert clock.monotonic() == 12.5
    clock.sleep(4)
    assert slept == [4.0]
    assert format_utc(OFFSET_INSTANT) == "2026-09-13T02:00:00.000000Z"
    with pytest.raises(TypeError, match="timezone-aware"):
        canonical_utc(datetime(2026, 9, 13))


def test_stable_work_and_operation_identities_are_deterministic_and_unambiguous():
    work = runtime_work_id("AUTO_ASSIGN", "EVT-1", "INC-1")
    assert work == runtime_work_id("AUTO_ASSIGN", "EVT-1", "INC-1")
    assert work != runtime_work_id("AUTO_ASSIGN", "EVT-1INC", "-1")
    assert runtime_operation_id("INCIDENT_CREATE", "EVT-1") == runtime_operation_id(
        "INCIDENT_CREATE", "EVT-1"
    )
    assert automatic_assignment_operation_id("INC-1", "runtime-actor") == (
        automatic_assignment_operation_id("INC-1", "runtime-actor")
    )
    assert automatic_assignment_operation_id("INC-1", "runtime-actor") != (
        automatic_assignment_operation_id("INC-1", "another-actor")
    )


def test_committed_runtime_config_is_valid_and_orchestration_only():
    config = load_runtime_config("configs/runtime_orchestration.yaml")
    assert config.pending_scan_seconds == 1.0
    assert config.retry_delays_seconds == (1.0, 2.0, 4.0, 8.0)
    assert config.work_store.path == "var/runtime_orchestration/runtime_work.sqlite3"
    assert config.automation_actor == "runtime-auto-assign"
    assert config.assignment_policy.policy_id == "POC-ROUND-ROBIN"
    assert config.assignment_policy.engineers == (
        "engineer-a",
        "engineer-b",
        "engineer-c",
    )


@pytest.mark.parametrize(
    "mutation, expected",
    [
        (lambda raw: raw.update({"fingerprint": "forbidden"}), "unsupported"),
        (lambda raw: raw["retry"].update({"limit": 0}), "positive integer"),
        (lambda raw: raw["retry"].update({"delays_seconds": [1]}), "count"),
        (lambda raw: raw["work_store"].update({"path": "C:/domain.sqlite"}), "repo-relative"),
        (lambda raw: raw["work_store"].update({"adapter": "domain-db"}), "sqlite3"),
        (lambda raw: raw["assignment"].update({"engineers": []}), "valid SPEC-009"),
    ],
)
def test_runtime_config_rejects_invalid_or_domain_semantic_overrides(
    tmp_path, mutation, expected
):
    import yaml

    raw = yaml.safe_load(open("configs/runtime_orchestration.yaml", encoding="utf-8"))
    mutation(raw)
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(RuntimeConfigError, match=expected):
        load_runtime_config(path)


def test_runtime_config_malformed_and_unsupported_authority_fails(tmp_path):
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("runtime: [", encoding="utf-8")
    with pytest.raises(RuntimeConfigError, match="unreadable"):
        load_runtime_config(malformed)

    unsupported = tmp_path / "unsupported.yaml"
    text = open("configs/runtime_orchestration.yaml", encoding="utf-8").read()
    unsupported.write_text(text.replace('version: "1.0"', 'version: "2.0"'), encoding="utf-8")
    with pytest.raises(RuntimeConfigError, match="unsupported"):
        load_runtime_config(unsupported)


def test_structured_telemetry_uses_stdlib_logging_and_rejects_payload_fields(caplog):
    logger = logging.getLogger("test.runtime.telemetry")
    telemetry = StdlibRuntimeTelemetry(logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        telemetry.emit(
            RuntimeTelemetryEvent.RETRY_SCHEDULED,
            observed_at=OFFSET_INSTANT,
            event_id="EVT-1",
            attempt=2,
            next_eligibility=OFFSET_INSTANT,
        )
    payload = json.loads(caplog.records[-1].message)
    assert payload == {
        "attempt": 2,
        "event_id": "EVT-1",
        "next_eligibility": "2026-09-13T02:00:00.000000Z",
        "observed_at": "2026-09-13T02:00:00.000000Z",
        "runtime_event": "RETRY_SCHEDULED",
    }
    with pytest.raises(ValueError, match="unsupported telemetry fields"):
        telemetry.emit(
            RuntimeTelemetryEvent.WORK_OBSERVED,
            observed_at=UTC_INSTANT,
            event_payload={"forbidden": True},
        )
