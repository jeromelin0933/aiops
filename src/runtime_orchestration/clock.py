"""Separated absolute wall-clock and process-local timing facilities."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone


def canonical_utc(value: datetime, *, field: str = "timestamp") -> datetime:
    """Return one absolute instant in UTC, rejecting ambiguous naive values."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def format_utc(value: datetime, *, field: str = "timestamp") -> str:
    """Serialize an aware instant using the canonical ``...Z`` representation."""
    return canonical_utc(value, field=field).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def parse_utc(value: str, *, field: str = "timestamp") -> datetime:
    """Parse only the canonical UTC representation used by Runtime D2."""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field} must be a canonical UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid canonical UTC timestamp") from exc
    if format_utc(parsed, field=field) != value:
        raise ValueError(f"{field} is not in canonical UTC form")
    return parsed


@dataclass(frozen=True, slots=True)
class RuntimeClock:
    """Injectable clocks and sleeper with deliberately separate authorities.

    ``now`` supplies authoritative wall time. ``monotonic`` and ``sleep`` are
    process-local scheduling facilities and their values must never be persisted.
    """

    wall_now: Callable[[], datetime]
    monotonic_now: Callable[[], float]
    sleeper: Callable[[float], None]

    @classmethod
    def system(cls) -> "RuntimeClock":
        return cls(lambda: datetime.now(timezone.utc), time.monotonic, time.sleep)

    def now(self) -> datetime:
        return canonical_utc(self.wall_now(), field="RuntimeClock.now")

    def monotonic(self) -> float:
        value = self.monotonic_now()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("monotonic clock must return a number")
        return float(value)

    def sleep(self, seconds: float) -> None:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            raise ValueError("sleep duration must be a non-negative number")
        self.sleeper(float(seconds))
