"""SPEC-005 JSONL log generator.

The generator only creates mock input data.  It deliberately has no dependency
on event detection, event storage, or classifier code.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.scenario_runtime.schema import ScenarioId, ScenarioPhase, ScenarioRuntimeSnapshot

LOG_DIR = "./logs"
LOG_FILE = str(Path(LOG_DIR) / "aiops.json.log")

_SERVICES = ("checkout-api", "catalog-api", "payment-api", "orders-api", "gateway-api")
_S5_SERVICES = ("checkout-api", "catalog-api", "payment-api", "orders-api", "gateway-api")


class LogGenerator:
    """Append contract-valid baseline and scenario records to the formal JSONL path."""

    def __init__(self, config: Any, *, output_path: str | Path | None = None) -> None:
        self._config = config
        self.output_path = Path(output_path or config.raw["log"]["output_path"])
        self._sequence = 0
        self._injected_for_trigger: set[int] = set()

    @staticmethod
    def timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _record(self, *, rng: Any, level: str = "INFO", service_name: str | None = None,
                trace_id: str | None = None, status_code: int = 200, duration_ms: float = 80,
                error_type: str | None = None, error_message: str | None = None,
                source_ip: str | None = None, user_id: str | None = None,
                transaction_id: str | None = None, downstream_service: str | None = None,
                external_service: str | None = None, target_service: str | None = None,
                memory_usage_pct: float | None = None, rate_limit_quota: float | None = None) -> dict[str, Any]:
        self._sequence += 1
        return {
            "timestamp": self.timestamp(), "level": level,
            "service_name": service_name or rng.choice(_SERVICES),
            "trace_id": trace_id or f"trace-mock-{self._sequence:06d}",
            "status_code": int(status_code), "duration_ms": float(duration_ms),
            "error_type": error_type, "error_message": error_message,
            "source_ip": source_ip or f"198.51.100.{10 + (self._sequence % 20)}",
            "user_id": user_id or f"user_mock_{1 + (self._sequence % 50):03d}",
            "transaction_id": transaction_id or f"TXN-MOCK-{self._sequence:06d}",
            "downstream_service": downstream_service, "external_service": external_service,
            "target_service": target_service, "memory_usage_pct": memory_usage_pct,
            "rate_limit_quota": rate_limit_quota,
        }

    def baseline_records(self, rng: Any) -> list[dict[str, Any]]:
        count = self._config.raw["log"]["baseline_records_per_tick"]
        return [self._record(rng=rng, duration_ms=rng.randint(40, 180)) for _ in range(count)]

    def scenario_records(self, scenario_id: ScenarioId, rng: Any) -> list[dict[str, Any]]:
        scenario = self._config.scenarios[scenario_id]
        if scenario_id is ScenarioId.S1:
            return [self._record(rng=rng, level="WARN", service_name="auth-api", status_code=401,
                                 duration_ms=45, error_type="AuthenticationFailed",
                                 error_message="Mock credential rejected", source_ip=scenario["source_ip"],
                                 user_id=scenario["user_id"])
                    for _ in range(scenario["unauthorized_count"])]
        if scenario_id is ScenarioId.S2:
            trace = f"{scenario['trace_id_prefix']}-{self._sequence + 1:06d}"
            return [self._record(rng=rng, level="ERROR", service_name=service, trace_id=trace,
                                 status_code=status, duration_ms=duration, error_type="QueryTimeout",
                                 error_message="Mock database query exceeded timeout",
                                 downstream_service=scenario["downstream_service"])
                    for service, status, duration in (("storage-api", 500, 4200), ("payment-api", 504, 4600), ("gateway-api", 504, 4900))]
        if scenario_id is ScenarioId.S3:
            return [self._record(rng=rng, level="ERROR", service_name="payment-api", status_code=500,
                                 duration_ms=800, error_type="OutOfMemoryError",
                                 error_message="Mock heap allocation exhausted", memory_usage_pct=95.0)]
        if scenario_id is ScenarioId.S4:
            return [self._record(rng=rng, level="ERROR", service_name="payment-api",
                                 status_code=scenario["status_code"], duration_ms=3500,
                                 error_type="ExternalServiceTimeout", error_message="Mock external service timeout",
                                 external_service=scenario["external_service"])]
        if scenario_id is ScenarioId.S5:
            downstream = scenario["downstream_service"]
            return [self._record(rng=rng, level="ERROR", service_name=service, status_code=503,
                                 duration_ms=1200, error_type=scenario["error_type"],
                                 error_message="Mock downstream connection refused", downstream_service=downstream)
                    for service in _S5_SERVICES[:scenario["affected_service_count"]]]
        if scenario_id is ScenarioId.S6:
            return [self._record(rng=rng, level="WARN", service_name="gateway-api", status_code=429,
                                 duration_ms=25, error_type="RateLimitExceeded",
                                 error_message="Mock rate limit quota exceeded", target_service=scenario["target_service"],
                                 rate_limit_quota=100.0)
                    for _ in range(scenario["rate_limit_log_count"])]
        raise ValueError(f"unsupported scenario: {scenario_id}")

    def write_records(self, records: list[dict[str, Any]]) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")

    def tick(self, snapshot: ScenarioRuntimeSnapshot, rng: Any) -> None:
        if snapshot.phase is ScenarioPhase.INJECTING and snapshot.active_scenario is not None:
            if snapshot.trigger_count not in self._injected_for_trigger:
                self.write_records(self.scenario_records(snapshot.active_scenario, rng))
                self._injected_for_trigger.add(snapshot.trigger_count)
            return
        if snapshot.phase in (ScenarioPhase.BASELINE, ScenarioPhase.RECOVERY):
            self.write_records(self.baseline_records(rng))


# Compatibility helpers for callers that used the original module functions.
def get_timestamp() -> str:
    return LogGenerator.timestamp()


def write_log(log_dict: dict[str, Any]) -> None:
    path = Path(LOG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(log_dict, allow_nan=False, separators=(",", ":")) + "\n")


def main() -> int:
    """Keep ``python -m src.log_generator.log_generator`` usable via the formal runtime."""
    from scripts.run_mock_runtime import main as runtime_main
    return runtime_main()


if __name__ == "__main__":
    raise SystemExit(main())
