"""Small structured telemetry surface backed only by stdlib logging."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from .clock import format_utc


class RuntimeTelemetryEvent(str, Enum):
    STARTUP = "STARTUP"
    RECOVERY = "RECOVERY"
    READY = "READY"
    WORK_OBSERVED = "WORK_OBSERVED"
    WORK_UPDATED = "WORK_UPDATED"
    WORK_COMPLETED = "WORK_COMPLETED"
    WORK_CORRUPT = "WORK_CORRUPT"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    RETRY_EXHAUSTED = "RETRY_EXHAUSTED"
    RECONCILIATION = "RECONCILIATION"
    STOP = "STOP"


_ALLOWED_FIELDS = frozenset(
    {
        "work_id",
        "event_id",
        "stage",
        "policy_id",
        "policy_version",
        "decision",
        "operation_id",
        "workflow_operation_id",
        "claim_result",
        "incident_id",
        "shadow_id",
        "source_domain",
        "source_error_code",
        "retry_disposition",
        "attempt",
        "next_eligibility",
        "reconciliation_result",
        "status",
        "corruption_code",
    }
)


class RuntimeTelemetry(Protocol):
    def emit(
        self, event: RuntimeTelemetryEvent, *, observed_at: datetime, **fields: object
    ) -> None: ...


class StdlibRuntimeTelemetry:
    def __init__(self, logger: logging.Logger, *, level: int = logging.INFO) -> None:
        self._logger = logger
        self._level = level

    def emit(
        self, event: RuntimeTelemetryEvent, *, observed_at: datetime, **fields: object
    ) -> None:
        if not isinstance(event, RuntimeTelemetryEvent):
            raise TypeError("event must be RuntimeTelemetryEvent")
        unknown = sorted(set(fields) - _ALLOWED_FIELDS)
        if unknown:
            raise ValueError(f"unsupported telemetry fields: {', '.join(unknown)}")
        payload: dict[str, Any] = {
            "runtime_event": event.value,
            "observed_at": format_utc(observed_at, field="observed_at"),
        }
        for key, value in fields.items():
            if isinstance(value, datetime):
                value = format_utc(value, field=key)
            elif isinstance(value, Enum):
                value = value.value
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise TypeError(f"telemetry field {key} must be a scalar")
            payload[key] = value
        self._logger.log(self._level, json.dumps(payload, sort_keys=True, separators=(",", ":")))


class NullRuntimeTelemetry:
    def emit(
        self, event: RuntimeTelemetryEvent, *, observed_at: datetime, **fields: object
    ) -> None:
        if not isinstance(event, RuntimeTelemetryEvent):
            raise TypeError("event must be RuntimeTelemetryEvent")
