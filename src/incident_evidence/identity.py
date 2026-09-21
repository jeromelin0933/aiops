"""Deterministic, domain-separated semantic identity primitives."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence, Set
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .contracts import CaptureCommand
from .time_semantics import format_utc


def _canonical_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical semantic values must be finite")
        return 0.0 if value == 0.0 else value
    if isinstance(value, datetime):
        return format_utc(value)
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(asdict(value))
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical mapping keys must be strings")
        return {
            key: _canonical_value(value[key])
            for key in sorted(value)
        }
    if isinstance(value, Set) and not isinstance(value, (str, bytes, bytearray)):
        canonical_items = [_canonical_value(item) for item in value]
        return sorted(canonical_items, key=_encoded_sort_key)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_canonical_value(item) for item in value]
    raise TypeError(f"unsupported canonical semantic type: {type(value).__name__}")


def _encoded_sort_key(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json(value: object) -> str:
    """Encode semantic content independent of mapping/set representation order."""
    return _encoded_sort_key(_canonical_value(value))


def canonical_unordered_items(values: Sequence[object]) -> tuple[object, ...]:
    """Normalize a physically unordered collection before semantic identity.

    Ordered domain collections must not use this helper. Callers make the
    semantic-order decision explicitly instead of the hash primitive guessing.
    """
    canonical = tuple(_canonical_value(value) for value in values)
    return tuple(sorted(canonical, key=_encoded_sort_key))


def semantic_identity(namespace: str, value: object) -> str:
    if not isinstance(namespace, str) or not namespace or namespace != namespace.strip():
        raise ValueError("identity namespace must be non-empty and trimmed")
    payload = canonical_json(value).encode("utf-8")
    digest = hashlib.sha256(namespace.encode("utf-8") + b"\x00" + payload).hexdigest()
    return f"{namespace}:{digest}"


def capture_command_semantic_projection(command: CaptureCommand) -> dict[str, Any]:
    if not isinstance(command, CaptureCommand):
        raise TypeError("command must be a CaptureCommand")
    return {
        "capture_operation_id": command.capture_operation_id,
        "incident_id": command.incident_id,
        "snapshot_at": command.snapshot_at,
        "capture_contract_version": command.capture_contract_version,
        "canonicalization_version": command.canonicalization_version,
        "source_policy_version": command.source_policy_version,
        "bounds_policy_version": command.bounds_policy_version,
        "config_identity": command.config_identity,
    }


def capture_command_semantic_identity(command: CaptureCommand) -> str:
    return semantic_identity(
        "spec013-capture-command",
        capture_command_semantic_projection(command),
    )


__all__ = [
    "canonical_json",
    "canonical_unordered_items",
    "capture_command_semantic_identity",
    "capture_command_semantic_projection",
    "semantic_identity",
]
