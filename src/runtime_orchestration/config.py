"""Strict validation for configuration owned exclusively by orchestration."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml
from incident_management import AssignmentPolicyConfig


class RuntimeConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeWorkStoreConfig:
    adapter: str
    path: str


@dataclass(frozen=True, slots=True)
class RuntimeTelemetryConfig:
    logger_name: str
    level: int


@dataclass(frozen=True, slots=True)
class RuntimeAuthorityStoresConfig:
    event_store_path: str
    correlation_state_path: str
    incident_store_path: str
    shadow_store_path: str


@dataclass(frozen=True, slots=True)
class RuntimeLoopConfig:
    mode: str
    idle_poll_seconds: float


@dataclass(frozen=True, slots=True)
class RuntimeRcaConfig:
    a_store_path: str
    b_store_path: str
    d_store_path: str
    knowledge_config_path: str
    generation_config_path: str
    evidence_config_path: str
    loki_endpoint: str
    prometheus_endpoint: str


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    version: str
    event_intake_poll_seconds: float
    pending_scan_seconds: float
    retry_limit: int
    retry_delays_seconds: tuple[float, ...]
    work_store: RuntimeWorkStoreConfig
    authority_stores: RuntimeAuthorityStoresConfig
    loop: RuntimeLoopConfig
    automation_actor: str
    assignment_policy: AssignmentPolicyConfig
    telemetry: RuntimeTelemetryConfig
    rca: RuntimeRcaConfig | None = None


_ROOT_KEYS = {
    "version",
    "event_intake_poll_seconds",
    "pending_scan_seconds",
    "retry",
    "work_store",
    "authority_stores",
    "loop",
    "automation_actor",
    "assignment",
    "telemetry",
}


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeConfigError(f"{label} must be a mapping")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise RuntimeConfigError(f"missing {label} keys: {', '.join(missing)}")
    if unknown:
        raise RuntimeConfigError(f"unsupported {label} keys: {', '.join(unknown)}")


def _positive_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise RuntimeConfigError(f"{label} must be a positive finite number")
    return float(value)


def _non_empty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RuntimeConfigError(f"{label} must be a non-empty, trimmed string")
    return value


def _repo_relative_path(value: Any, label: str = "work_store.path") -> str:
    text = _non_empty(value, label)
    path = PurePath(text)
    if (len(text) > 256 or path.is_absolute() or ".." in path.parts
        or re.search(r"(?i)(?:api[_-]?key|password|secret|credential|token)[=:]", text)):
        raise RuntimeConfigError(f"{label} must be a safe repo-relative path")
    return text


def _local_endpoint(value: Any, label: str) -> str:
    text = _non_empty(value, label)
    parsed = urlsplit(text)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeConfigError(f"{label} has an invalid port") from exc
    if (len(text) > 256 or parsed.scheme != "http" or parsed.username or parsed.password or
        parsed.query or parsed.fragment or parsed.hostname not in
        {"localhost", "127.0.0.1", "loki", "prometheus"} or not port or
        re.search(r"(?i)(?:api[_-]?key|password|secret|credential|token|bearer)", parsed.path)):
        raise RuntimeConfigError(f"{label} must be a local non-secret HTTP endpoint")
    return text


def load_runtime_config(path: str | Path) -> RuntimeConfig:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeConfigError(f"runtime config is unreadable: {path}") from exc
    root = _mapping(raw, "runtime config")
    if set(root) not in (_ROOT_KEYS, _ROOT_KEYS | {"rca"}):
        raise RuntimeConfigError("runtime config keys are incomplete or unsupported")
    if root["version"] != "1.0":
        raise RuntimeConfigError("unsupported runtime config version")

    retry = _mapping(root["retry"], "retry")
    _exact_keys(retry, {"limit", "delays_seconds"}, "retry")
    limit = retry["limit"]
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise RuntimeConfigError("retry.limit must be a positive integer")
    delays_raw = retry["delays_seconds"]
    if not isinstance(delays_raw, list):
        raise RuntimeConfigError("retry.delays_seconds must be a list")
    delays = tuple(
        _positive_number(value, f"retry.delays_seconds[{index}]")
        for index, value in enumerate(delays_raw)
    )
    if len(delays) != limit:
        raise RuntimeConfigError("retry delays count must equal retry.limit")

    store = _mapping(root["work_store"], "work_store")
    _exact_keys(store, {"adapter", "path"}, "work_store")
    if store["adapter"] != "sqlite3":
        raise RuntimeConfigError("work_store.adapter must be sqlite3 for the Phase 1 PoC")

    telemetry = _mapping(root["telemetry"], "telemetry")
    _exact_keys(telemetry, {"logger_name", "level"}, "telemetry")
    level_name = _non_empty(telemetry["level"], "telemetry.level").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int) or level_name not in {
        "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
    }:
        raise RuntimeConfigError("telemetry.level must be a standard logging level")

    authority = _mapping(root["authority_stores"], "authority_stores")
    _exact_keys(
        authority,
        {
            "event_store_path",
            "correlation_state_path",
            "incident_store_path",
            "shadow_store_path",
        },
        "authority_stores",
    )
    authority_stores = RuntimeAuthorityStoresConfig(
        *(
            _repo_relative_path(authority[field], f"authority_stores.{field}")
            for field in (
                "event_store_path",
                "correlation_state_path",
                "incident_store_path",
                "shadow_store_path",
            )
        )
    )

    loop = _mapping(root["loop"], "loop")
    _exact_keys(loop, {"mode", "idle_poll_seconds"}, "loop")
    mode = _non_empty(loop["mode"], "loop.mode")
    if mode not in {"run-until-idle", "continuous"}:
        raise RuntimeConfigError("loop.mode must be run-until-idle or continuous")

    assignment = _mapping(root["assignment"], "assignment")
    _exact_keys(
        assignment,
        {"policy_id", "policy_version", "engineers", "default_reviewer"},
        "assignment",
    )
    engineers = assignment["engineers"]
    if not isinstance(engineers, list):
        raise RuntimeConfigError("assignment.engineers must be an ordered list")
    try:
        assignment_policy = AssignmentPolicyConfig(
            _non_empty(assignment["policy_id"], "assignment.policy_id"),
            _non_empty(assignment["policy_version"], "assignment.policy_version"),
            tuple(
                _non_empty(engineer, f"assignment.engineers[{index}]")
                for index, engineer in enumerate(engineers)
            ),
            _non_empty(assignment["default_reviewer"], "assignment.default_reviewer"),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigError("assignment must be a valid SPEC-009 policy") from exc

    rca = None
    if "rca" in root:
        raw_rca = _mapping(root["rca"], "rca")
        fields = {"a_store_path", "b_store_path", "d_store_path",
                  "knowledge_config_path", "generation_config_path",
                  "evidence_config_path", "loki_endpoint", "prometheus_endpoint"}
        _exact_keys(raw_rca, fields, "rca")
        rca = RuntimeRcaConfig(
            *(_repo_relative_path(raw_rca[name], f"rca.{name}") for name in (
                "a_store_path", "b_store_path", "d_store_path",
                "knowledge_config_path", "generation_config_path", "evidence_config_path")),
            _local_endpoint(raw_rca["loki_endpoint"], "rca.loki_endpoint"),
            _local_endpoint(raw_rca["prometheus_endpoint"], "rca.prometheus_endpoint"),
        )

    return RuntimeConfig(
        version="1.0",
        event_intake_poll_seconds=_positive_number(
            root["event_intake_poll_seconds"], "event_intake_poll_seconds"
        ),
        pending_scan_seconds=_positive_number(root["pending_scan_seconds"], "pending_scan_seconds"),
        retry_limit=limit,
        retry_delays_seconds=delays,
        work_store=RuntimeWorkStoreConfig("sqlite3", _repo_relative_path(store["path"])),
        authority_stores=authority_stores,
        loop=RuntimeLoopConfig(
            mode,
            _positive_number(loop["idle_poll_seconds"], "loop.idle_poll_seconds"),
        ),
        automation_actor=_non_empty(root["automation_actor"], "automation_actor"),
        assignment_policy=assignment_policy,
        telemetry=RuntimeTelemetryConfig(
            _non_empty(telemetry["logger_name"], "telemetry.logger_name"), level
        ),
        rca=rca,
    )
