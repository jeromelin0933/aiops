from __future__ import annotations

import json
import math
import random
from datetime import datetime

import pytest

from src.log_generator.log_generator import LogGenerator
from src.scenario_runtime import ScenarioConfigLoader, ScenarioId, ScenarioPhase
from src.scenario_runtime.schema import ScenarioRuntimeSnapshot


@pytest.fixture
def config():
    return ScenarioConfigLoader.load("configs/scenarios.yaml")


def snapshot(phase, scenario=None, trigger_count=0):
    return ScenarioRuntimeSnapshot(phase, scenario, 0.0, None, trigger_count, False)


def test_baseline_jsonl_schema_and_safe_pattern(config, tmp_path):
    generator = LogGenerator(config, output_path=tmp_path / "aiops.json.log")
    generator.tick(snapshot(ScenarioPhase.BASELINE), random.Random(42))
    records = [json.loads(line) for line in generator.output_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    record = records[0]
    assert set(record) == {"timestamp", "level", "service_name", "trace_id", "status_code", "duration_ms", "error_type", "error_message", "source_ip", "user_id", "transaction_id", "downstream_service", "external_service", "target_service", "memory_usage_pct", "rate_limit_quota"}
    assert record["level"] == "INFO" and record["status_code"] == 200
    assert record["error_type"] is None and record["external_service"] is None
    assert record["source_ip"].startswith("198.51.100.")
    assert datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).utcoffset().total_seconds() == 0
    assert isinstance(record["status_code"], int)
    assert isinstance(record["duration_ms"], float) and math.isfinite(record["duration_ms"])
    assert all(record[name] is None or isinstance(record[name], (str, int, float)) for name in record)
    assert not ({"scenario_id", "expected_event_type", "should_trigger", "is_anomaly", "classifier_result"} & record.keys())


def test_jsonl_writer_rejects_nan_and_keeps_runtime_artifacts_in_tmp_path(config, tmp_path):
    output = tmp_path / "logs" / "aiops.json.log"
    generator = LogGenerator(config, output_path=output)
    with pytest.raises(ValueError):
        generator.write_records([{"duration_ms": float("nan")}])
    assert output.parent == tmp_path / "logs"


@pytest.mark.parametrize("scenario_id, expected_count", [
    (ScenarioId.S1, 50), (ScenarioId.S2, 3), (ScenarioId.S3, 1),
    (ScenarioId.S4, 1), (ScenarioId.S5, 5), (ScenarioId.S6, 55),
])
def test_scenario_patterns_meet_formal_minimums(config, tmp_path, scenario_id, expected_count):
    generator = LogGenerator(config, output_path=tmp_path / "aiops.json.log")
    records = generator.scenario_records(scenario_id, random.Random(42))
    assert len(records) >= expected_count
    if scenario_id is ScenarioId.S1:
        assert {record["source_ip"] for record in records} == {"192.0.2.10"}
        assert all(record["status_code"] == 401 for record in records)
        assert all(record["external_service"] is None and record["target_service"] is None and record["error_type"] != "OutOfMemoryError" for record in records)
    elif scenario_id is ScenarioId.S2:
        assert len({record["trace_id"] for record in records}) == 1
        assert len({record["service_name"] for record in records}) >= 3
        assert all(record["level"] == "ERROR" and record["downstream_service"] == "core-db" and record["duration_ms"] >= 3000 for record in records)
    elif scenario_id is ScenarioId.S3:
        assert records[0]["level"] == "ERROR" and records[0]["error_type"] == "OutOfMemoryError"
    elif scenario_id is ScenarioId.S4:
        assert records[0]["external_service"] and records[0]["transaction_id"] and records[0]["status_code"] >= 500
    elif scenario_id is ScenarioId.S5:
        assert len({record["service_name"] for record in records}) >= 5
        assert len({record["trace_id"] for record in records}) == len(records)
        assert all(record["downstream_service"] == "core-db" and record["error_type"] == "ConnectionRefused" for record in records)
    else:
        assert len({record["target_service"] for record in records}) == 1
        assert all(record["status_code"] == 429 and record["rate_limit_quota"] >= 0 for record in records)


def test_injection_happens_once_per_trigger_and_recovery_returns_baseline(config, tmp_path):
    generator = LogGenerator(config, output_path=tmp_path / "aiops.json.log")
    rng = random.Random(42)
    injecting = snapshot(ScenarioPhase.INJECTING, ScenarioId.S1, 1)
    generator.tick(injecting, rng)
    generator.tick(injecting, rng)
    generator.tick(snapshot(ScenarioPhase.RECOVERY, ScenarioId.S1, 1), rng)
    records = [json.loads(line) for line in generator.output_path.read_text(encoding="utf-8").splitlines()]
    assert sum(record["status_code"] == 401 for record in records) == 50
    assert records[-1]["level"] == "INFO" and records[-1]["status_code"] == 200


def test_completed_runtime_can_inject_the_same_scenario_again(config, tmp_path):
    generator = LogGenerator(config, output_path=tmp_path / "aiops.json.log")
    rng = random.Random(42)
    generator.tick(snapshot(ScenarioPhase.INJECTING, ScenarioId.S1, 1), rng)
    generator.tick(snapshot(ScenarioPhase.INJECTING, ScenarioId.S1, 2), rng)
    records = [json.loads(line) for line in generator.output_path.read_text(encoding="utf-8").splitlines()]
    assert sum(record["status_code"] == 401 for record in records) == 100


def test_fixed_seed_reproduces_semantic_baseline_selection(config, tmp_path):
    first = LogGenerator(config, output_path=tmp_path / "one.log").baseline_records(random.Random(42))[0]
    second = LogGenerator(config, output_path=tmp_path / "two.log").baseline_records(random.Random(42))[0]
    assert (first["service_name"], first["duration_ms"], first["source_ip"], first["user_id"]) == (second["service_name"], second["duration_ms"], second["source_ip"], second["user_id"])
