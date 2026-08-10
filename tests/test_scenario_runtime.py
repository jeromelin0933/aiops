from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.scenario_runtime import MockDataRuntime, ScenarioConfigLoader, ScenarioPhase
import src.scenario_runtime.config as config_module


class FakeClock:
    def __init__(self, now: float = 100.0): self.now = now
    def __call__(self) -> float: return self.now
    def advance(self, seconds: float) -> None: self.now += seconds


@pytest.fixture
def config():
    return ScenarioConfigLoader.load("configs/scenarios.yaml")


def test_valid_config_and_all_scenarios(config):
    assert set(config.scenarios) == set(__import__("src.scenario_runtime", fromlist=["ScenarioId"]).ScenarioId)
    assert config.random_seed == 42
    assert config.raw["background_errors"]["enabled"] is False
    assert config.raw["metrics"]["baseline"]["api_requests_per_sec"] > 0


def test_invalid_yaml_is_rejected_before_generator_start(tmp_path):
    path = tmp_path / "invalid-scenarios.yaml"
    path.write_text("runtime: [unclosed", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        ScenarioConfigLoader.load(path)


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c.pop("runtime"), "missing root.runtime"),
    (lambda c: c["scenarios"].pop("S6"), "exactly S1 through S6"),
    (lambda c: c["scenarios"].update({"S7": {"duration_seconds": 1}}), "exactly S1 through S6"),
    (lambda c: c["scenarios"]["S1"].update({"duration_seconds": 0}), "duration_seconds"),
    (lambda c: c["metrics"].update({"exporter_port": 0}), "exporter_port"),
    (lambda c: c["metrics"]["baseline"].update({"api_requests_per_sec": 0}), "baseline qps must be positive"),
    (lambda c: c["metrics"]["baseline"].update({"api_p95_latency_ms": 3000}), "baseline latency"),
    (lambda c: c["metrics"]["jitter"].update({"memory_max_delta": 40}), "memory jitter"),
    (lambda c: c["background_errors"].update({"enabled": True}), "background_errors"),
    (lambda c: c["scenarios"]["S1"].update({"unauthorized_count": 9}), "unauthorized_count"),
    (lambda c: c["scenarios"]["S2"].update({"api_p95_latency_ms": 2999}), "S2 latency"),
    (lambda c: c["scenarios"]["S3"].update({"system_memory_usage_pct": 89}), "S3 memory"),
    (lambda c: c["scenarios"]["S4"].update({"status_code": 499}), "S4 status_code"),
    (lambda c: c["scenarios"]["S5"].update({"affected_service_count": 4}), "S5 affected_service_count"),
    (lambda c: c["scenarios"]["S5"].update({"affected_service_count": 6}), "exceeds the formal generator service set"),
    (lambda c: c["scenarios"]["S6"].update({"rate_limit_log_count": 19}), "S6 rate_limit_log_count"),
    (lambda c: c["scenarios"]["S6"].update({"qps_spike_multiplier": 2.9}), "qps_spike_multiplier"),
    (lambda c: c["runtime"].update({"recovery_seconds": 59}), "recovery_seconds"),
])
def test_invalid_config_is_rejected(monkeypatch, mutate, message):
    raw = yaml.safe_load(Path("configs/scenarios.yaml").read_text(encoding="utf-8"))
    mutate(raw)
    original_safe_load = config_module.yaml.safe_load
    monkeypatch.setattr(
        config_module.yaml,
        "safe_load",
        lambda handle: raw if Path(handle.name).name == "scenarios.yaml" else original_safe_load(handle),
    )
    with pytest.raises(ValueError, match=message):
        ScenarioConfigLoader.load("configs/scenarios.yaml")


def test_lifecycle_repeated_trigger_and_isolation(config):
    clock = FakeClock()
    seen = []
    runtime = MockDataRuntime(config, clock=clock, tick_sink=lambda snapshot, rng: seen.append((snapshot.phase, snapshot.active_scenario)))
    assert runtime.start().phase is ScenarioPhase.BASELINE
    runtime.tick()
    accepted = runtime.trigger("S1")
    assert accepted.accepted and accepted.snapshot.phase is ScenarioPhase.INJECTING
    assert not runtime.trigger("S2").accepted
    clock.advance(config.duration_for(accepted.snapshot.active_scenario))
    assert runtime.tick().phase is ScenarioPhase.RECOVERY
    assert not runtime.trigger("S2").accepted
    clock.advance(config.recovery_seconds)
    assert runtime.tick().phase is ScenarioPhase.BASELINE
    assert runtime.snapshot().active_scenario is None
    assert runtime.trigger("S2").accepted
    assert runtime.snapshot().trigger_count == 2
    assert any(phase is ScenarioPhase.BASELINE for phase, _ in seen)


def test_stop_and_unknown_trigger(config):
    runtime = MockDataRuntime(config, clock=FakeClock())
    unknown = runtime.trigger("bad")
    assert not unknown.accepted and "unknown scenario" in unknown.reason
    runtime.start()
    stopped = runtime.stop()
    assert stopped.phase is ScenarioPhase.STOPPED
    assert stopped.stop_requested
    assert not runtime.trigger("S1").accepted


def test_phase_sequence_rejects_only_until_recovery_completes(config):
    clock = FakeClock()
    phases = []
    runtime = MockDataRuntime(config, clock=clock, tick_sink=lambda state, _: phases.append(state.phase))
    runtime.start()
    runtime.tick()
    assert runtime.trigger("S3").accepted
    assert not runtime.trigger("S1").accepted
    clock.advance(config.duration_for(__import__("src.scenario_runtime", fromlist=["ScenarioId"]).ScenarioId.S3))
    assert runtime.tick().phase is ScenarioPhase.RECOVERY
    assert not runtime.trigger("S1").accepted
    clock.advance(config.recovery_seconds)
    assert runtime.tick().phase is ScenarioPhase.BASELINE
    assert runtime.trigger("S1").accepted
    assert phases == [ScenarioPhase.BASELINE, ScenarioPhase.RECOVERY, ScenarioPhase.BASELINE]


def test_fixed_seed_is_reproducible(config):
    first = MockDataRuntime(config, clock=FakeClock())
    second = MockDataRuntime(config, clock=FakeClock())
    assert [first.random.uniform(-2, 2) for _ in range(5)] == [second.random.uniform(-2, 2) for _ in range(5)]


def test_run_forever_uses_injected_sleep_without_real_wait(config):
    config = ScenarioConfigLoader.load("configs/scenarios.yaml")
    runtime = MockDataRuntime(config, clock=FakeClock())
    sleeps = []
    def sleeper(value):
        sleeps.append(value)
        runtime.stop()
    runtime.run_forever(sleep=sleeper)
    assert sleeps == [config.tick_seconds]
