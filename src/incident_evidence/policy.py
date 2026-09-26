"""Strict, versioned Candidate-B policy/config validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

import yaml

from .contracts import EvidenceSource
from .security import (
    FORBIDDEN_PRODUCTION_FIELDS,
    SENSITIVE_FIELD_NAMES,
    ensure_no_ground_truth_fields,
)


class EvidencePolicyConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WindowPolicy:
    logs_pre_seconds: int
    metrics_pre_seconds: int
    post_seconds: int


@dataclass(frozen=True, slots=True)
class BoundsPolicy:
    max_records_per_source: int
    max_content_bytes: int
    max_total_bytes: int
    max_labels_per_record: int
    max_label_value_characters: int


@dataclass(frozen=True, slots=True)
class EvidencePolicy:
    config_version: str
    config_identity: str
    capture_contract_version: str
    canonicalization_version: str
    source_policy_version: str
    bounds_policy_version: str
    selector_policy_version: str
    redaction_policy_version: str
    materiality_rule_versions: tuple[str, ...]
    windows: WindowPolicy
    selector_allowlist: Mapping[EvidenceSource, frozenset[str]]
    bounds: BoundsPolicy


@dataclass(frozen=True, slots=True)
class SourceAdmissionPolicy:
    """Explicit degradation rules, versioned through CaptureCommand config identity."""

    configured_sources: frozenset[EvidenceSource] = frozenset(EvidenceSource)
    degraded_unavailable_sources: frozenset[EvidenceSource] = frozenset()
    degraded_invalid_sources: frozenset[EvidenceSource] = frozenset()
    require_remaining_valid_source: bool = True

    def __post_init__(self) -> None:
        configured = frozenset(self.configured_sources)
        unavailable = frozenset(self.degraded_unavailable_sources)
        invalid = frozenset(self.degraded_invalid_sources)
        if not configured or any(not isinstance(item, EvidenceSource) for item in configured):
            raise ValueError("configured_sources must contain EvidenceSource values")
        if any(not isinstance(item, EvidenceSource) for item in unavailable | invalid):
            raise ValueError("degraded source policies must contain EvidenceSource values")
        if not unavailable <= configured or not invalid <= configured:
            raise ValueError("degraded sources must be configured sources")
        if not isinstance(self.require_remaining_valid_source, bool):
            raise TypeError("require_remaining_valid_source must be bool")
        object.__setattr__(self, "configured_sources", configured)
        object.__setattr__(self, "degraded_unavailable_sources", unavailable)
        object.__setattr__(self, "degraded_invalid_sources", invalid)


_ROOT_KEYS = {
    "config_version",
    "config_identity",
    "capture_contract_version",
    "canonicalization_version",
    "source_policy_version",
    "bounds_policy_version",
    "selector_policy_version",
    "redaction_policy_version",
    "materiality_rule_versions",
    "windows",
    "selector_allowlist",
    "bounds",
}
_WINDOW_KEYS = {"logs_pre_seconds", "metrics_pre_seconds", "post_seconds"}
_BOUNDS_KEYS = {
    "max_records_per_source",
    "max_content_bytes",
    "max_total_bytes",
    "max_labels_per_record",
    "max_label_value_characters",
}
_SELECTOR_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidencePolicyConfigError(f"{label} must be a mapping")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing:
        raise EvidencePolicyConfigError(f"missing {label} keys: {', '.join(missing)}")
    if unknown:
        raise EvidencePolicyConfigError(
            f"unsupported {label} keys: {', '.join(unknown)}"
        )


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise EvidencePolicyConfigError(f"{label} must be a non-empty, trimmed identity")
    return value


def _positive_integer(value: object, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise EvidencePolicyConfigError(f"{label} must be a {qualifier} integer")
    return value


def load_evidence_policy(path: str | Path) -> EvidencePolicy:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise EvidencePolicyConfigError(f"evidence policy is unreadable: {path}") from exc
    try:
        ensure_no_ground_truth_fields(raw)
    except ValueError as exc:
        raise EvidencePolicyConfigError("evidence policy contains forbidden fields") from exc

    root = _mapping(raw, "evidence policy")
    _exact_keys(root, _ROOT_KEYS, "evidence policy")
    if root["config_version"] != "1.0":
        raise EvidencePolicyConfigError("unsupported evidence config version")

    windows_raw = _mapping(root["windows"], "windows")
    _exact_keys(windows_raw, _WINDOW_KEYS, "windows")
    windows = WindowPolicy(
        logs_pre_seconds=_positive_integer(
            windows_raw["logs_pre_seconds"], "windows.logs_pre_seconds", allow_zero=True
        ),
        metrics_pre_seconds=_positive_integer(
            windows_raw["metrics_pre_seconds"],
            "windows.metrics_pre_seconds",
            allow_zero=True,
        ),
        post_seconds=_positive_integer(
            windows_raw["post_seconds"], "windows.post_seconds", allow_zero=True
        ),
    )

    selectors_raw = _mapping(root["selector_allowlist"], "selector_allowlist")
    _exact_keys(selectors_raw, {"LOKI", "PROMETHEUS"}, "selector_allowlist")
    selectors: dict[EvidenceSource, frozenset[str]] = {}
    for source in EvidenceSource:
        values = selectors_raw[source.value]
        if not isinstance(values, list) or not values:
            raise EvidencePolicyConfigError(
                f"selector_allowlist.{source.value} must be a non-empty list"
            )
        normalized = tuple(
            _identity(value, f"selector_allowlist.{source.value}") for value in values
        )
        forbidden = sorted(set(normalized) & FORBIDDEN_PRODUCTION_FIELDS)
        if forbidden:
            raise EvidencePolicyConfigError(
                f"selector_allowlist.{source.value} contains forbidden production fields: "
                + ", ".join(forbidden)
            )
        sensitive = sorted(set(normalized) & SENSITIVE_FIELD_NAMES)
        if sensitive:
            raise EvidencePolicyConfigError(
                f"selector_allowlist.{source.value} contains secret-bearing fields: "
                + ", ".join(sensitive)
            )
        if any(_SELECTOR_FIELD.fullmatch(value) is None for value in normalized):
            raise EvidencePolicyConfigError(
                f"selector_allowlist.{source.value} contains an invalid selector field"
            )
        if len(normalized) != len(set(normalized)):
            raise EvidencePolicyConfigError(
                f"selector_allowlist.{source.value} must be unique"
            )
        selectors[source] = frozenset(normalized)

    bounds_raw = _mapping(root["bounds"], "bounds")
    _exact_keys(bounds_raw, _BOUNDS_KEYS, "bounds")
    bounds = BoundsPolicy(
        **{
            key: _positive_integer(bounds_raw[key], f"bounds.{key}")
            for key in _BOUNDS_KEYS
        }
    )

    rules_raw = root["materiality_rule_versions"]
    if not isinstance(rules_raw, list) or not rules_raw:
        raise EvidencePolicyConfigError(
            "materiality_rule_versions must be a non-empty list"
        )
    rules = tuple(_identity(value, "materiality_rule_versions") for value in rules_raw)
    if len(rules) != len(set(rules)):
        raise EvidencePolicyConfigError("materiality_rule_versions must be unique")

    return EvidencePolicy(
        config_version="1.0",
        config_identity=_identity(root["config_identity"], "config_identity"),
        capture_contract_version=_identity(
            root["capture_contract_version"], "capture_contract_version"
        ),
        canonicalization_version=_identity(
            root["canonicalization_version"], "canonicalization_version"
        ),
        source_policy_version=_identity(
            root["source_policy_version"], "source_policy_version"
        ),
        bounds_policy_version=_identity(
            root["bounds_policy_version"], "bounds_policy_version"
        ),
        selector_policy_version=_identity(
            root["selector_policy_version"], "selector_policy_version"
        ),
        redaction_policy_version=_identity(
            root["redaction_policy_version"], "redaction_policy_version"
        ),
        materiality_rule_versions=rules,
        windows=windows,
        selector_allowlist=MappingProxyType(selectors),
        bounds=bounds,
    )


__all__ = [
    "BoundsPolicy",
    "EvidencePolicy",
    "EvidencePolicyConfigError",
    "SourceAdmissionPolicy",
    "WindowPolicy",
    "load_evidence_policy",
]
