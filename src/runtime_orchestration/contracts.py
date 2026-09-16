"""Runtime-local contracts for orchestration continuity, never domain truth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from .clock import canonical_utc


class RuntimeWorkKind(str, Enum):
    DOMAIN_OPERATION = "DOMAIN_OPERATION"
    AUTO_ASSIGN = "AUTO_ASSIGN"


class RuntimeWorkStatus(str, Enum):
    OUTSTANDING = "OUTSTANDING"
    COMPLETED = "COMPLETED"
    EXHAUSTED = "EXHAUSTED"
    FAILED_CLOSED = "FAILED_CLOSED"


_TERMINAL_STATUSES = frozenset(
    {
        RuntimeWorkStatus.COMPLETED,
        RuntimeWorkStatus.EXHAUSTED,
        RuntimeWorkStatus.FAILED_CLOSED,
    }
)


def _identifier(value: object, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty, trimmed string")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeWorkRecord:
    """Minimal durable execution state for one orchestration obligation.

    References identify domain authority but do not copy Event, decision,
    Pending, Processed, Incident, Shadow, or workflow business state.
    """

    work_id: str
    work_kind: RuntimeWorkKind
    event_id: str
    stage: str
    next_action: str
    attempt_count: int
    retry_limit: int
    status: RuntimeWorkStatus
    created_at: datetime
    updated_at: datetime
    observed_at: datetime
    incident_id: str | None = None
    operation_id: str | None = None
    workflow_operation_id: str | None = None
    next_retry_at: datetime | None = None
    last_attempt_at: datetime | None = None
    source_domain: str | None = None
    source_error_code: str | None = None
    source_retry_disposition: str | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        _identifier(self.work_id, "work_id")
        _identifier(self.event_id, "event_id")
        _identifier(self.stage, "stage")
        _identifier(self.next_action, "next_action")
        for field in ("incident_id", "operation_id", "workflow_operation_id"):
            _identifier(getattr(self, field), field, optional=True)
        if not isinstance(self.work_kind, RuntimeWorkKind):
            raise TypeError("work_kind must be RuntimeWorkKind")
        if not isinstance(self.status, RuntimeWorkStatus):
            raise TypeError("status must be RuntimeWorkStatus")
        if isinstance(self.attempt_count, bool) or not isinstance(self.attempt_count, int):
            raise TypeError("attempt_count must be an integer")
        if self.attempt_count < 0:
            raise ValueError("attempt_count must not be negative")
        if isinstance(self.retry_limit, bool) or not isinstance(self.retry_limit, int):
            raise TypeError("retry_limit must be an integer")
        if self.retry_limit < 1:
            raise ValueError("retry_limit must be positive")
        if self.attempt_count > self.retry_limit:
            raise ValueError("attempt_count must not exceed the durable retry limit")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")

        for field in ("created_at", "updated_at", "observed_at"):
            object.__setattr__(self, field, canonical_utc(getattr(self, field), field=field))
        for field in ("next_retry_at", "last_attempt_at"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, canonical_utc(value, field=field))
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.status in _TERMINAL_STATUSES and self.next_retry_at is not None:
            raise ValueError("terminal work cannot retain next_retry_at")
        if self.status is RuntimeWorkStatus.EXHAUSTED and self.attempt_count < self.retry_limit:
            raise ValueError("EXHAUSTED work must have consumed its durable retry limit")

        evidence = (self.source_domain, self.source_error_code, self.source_retry_disposition)
        if any(value is not None for value in evidence):
            if not all(value is not None for value in evidence):
                raise ValueError("source failure authority fields must be provided together")
            for field in ("source_domain", "source_error_code", "source_retry_disposition"):
                _identifier(getattr(self, field), field)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class RuntimeWorkCorruption:
    """One safely isolated row that remains operator-visible and untouched."""

    record_key: str
    error_code: str
    detail: str


@dataclass(frozen=True, slots=True)
class RuntimeWorkEnumeration:
    records: tuple[RuntimeWorkRecord, ...]
    isolated_corruptions: tuple[RuntimeWorkCorruption, ...]


class RuntimeWorkStore(Protocol):
    def create(self, record: RuntimeWorkRecord) -> RuntimeWorkRecord: ...
    def get(self, work_id: str) -> RuntimeWorkRecord | None: ...
    def enumerate_all(self) -> RuntimeWorkEnumeration: ...
    def update(
        self, record: RuntimeWorkRecord, *, expected_revision: int
    ) -> RuntimeWorkRecord: ...
    def complete(
        self, work_id: str, *, observed_at: datetime, expected_revision: int | None = None
    ) -> RuntimeWorkRecord: ...
    def close(self) -> None: ...
