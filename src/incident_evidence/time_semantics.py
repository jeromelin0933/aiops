"""Authoritative absolute-time and collection-window semantics for SPEC-013."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .errors import (
    EvidenceDomainError,
    EvidenceFailureKind,
    invalid_capture_command,
)


def canonical_utc(value: datetime, *, field: str = "timestamp") -> datetime:
    """Canonicalize one aware absolute instant or return a typed command error."""
    try:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise invalid_capture_command(
                f"{field} must be a timezone-aware datetime", field_path=field
            )
        return value.astimezone(timezone.utc)
    except EvidenceDomainError:
        raise
    except (OverflowError, TypeError, ValueError) as exc:
        raise invalid_capture_command(
            f"{field} is outside the supported absolute-time range",
            field_path=field,
        ) from exc


def format_utc(value: datetime, *, field: str = "timestamp") -> str:
    return canonical_utc(value, field=field).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Episode:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = canonical_utc(self.start, field="episode_start")
        end = canonical_utc(self.end, field="episode_end")
        if end < start:
            raise EvidenceDomainError(
                EvidenceFailureKind.INVALID_REQUIRED_EVENT,
                "episode_end must not precede episode_start",
                field_path="episode_end",
            )
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True, slots=True)
class LogicalWindow:
    """A logical collection interval closed at both endpoints: [start, end]."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = canonical_utc(self.start, field="window_start")
        end = canonical_utc(self.end, field="window_end")
        if end < start:
            raise invalid_capture_command(
                "logical collection window end must not precede start",
                field_path="window_end",
            )
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True, slots=True)
class CollectionWindows:
    logs: LogicalWindow
    metrics: LogicalWindow
    default_post_context_boundary: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.logs, LogicalWindow):
            raise TypeError("logs must be a LogicalWindow")
        if not isinstance(self.metrics, LogicalWindow):
            raise TypeError("metrics must be a LogicalWindow")
        object.__setattr__(
            self,
            "default_post_context_boundary",
            canonical_utc(
                self.default_post_context_boundary,
                field="default_post_context_boundary",
            ),
        )


def derive_episode(detected_at_values: Iterable[datetime]) -> Episode:
    try:
        raw_values = tuple(detected_at_values)
    except TypeError as exc:
        raise EvidenceDomainError(
            EvidenceFailureKind.INVALID_REQUIRED_EVENT,
            "detected_at values must be iterable",
            field_path="detected_at",
        ) from exc
    try:
        values = tuple(
            canonical_utc(value, field=f"detected_at[{index}]")
            for index, value in enumerate(raw_values)
        )
    except EvidenceDomainError as exc:
        raise EvidenceDomainError(
            EvidenceFailureKind.INVALID_REQUIRED_EVENT,
            "authoritative Event detected_at is invalid",
            field_path=exc.field_path,
        ) from exc
    if not values:
        raise EvidenceDomainError(
            EvidenceFailureKind.INVALID_REQUIRED_EVENT,
            "at least one authoritative Event timestamp is required",
            field_path="detected_at",
        )
    return Episode(min(values), max(values))


def derive_collection_windows(
    episode: Episode,
    snapshot_at: datetime,
    *,
    logs_pre_seconds: int = 120,
    metrics_pre_seconds: int = 300,
    post_seconds: int = 120,
) -> CollectionWindows:
    if not isinstance(episode, Episode):
        raise TypeError("episode must be an Episode")
    snapshot = canonical_utc(snapshot_at, field="snapshot_at")
    for field, value in (
        ("logs_pre_seconds", logs_pre_seconds),
        ("metrics_pre_seconds", metrics_pre_seconds),
        ("post_seconds", post_seconds),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise invalid_capture_command(
                f"{field} must be a non-negative integer", field_path=field
            )
    try:
        upper_boundary = episode.end + timedelta(seconds=post_seconds)
        return CollectionWindows(
            logs=LogicalWindow(
                episode.start - timedelta(seconds=logs_pre_seconds),
                min(snapshot, upper_boundary),
            ),
            metrics=LogicalWindow(
                episode.start - timedelta(seconds=metrics_pre_seconds),
                min(snapshot, upper_boundary),
            ),
            default_post_context_boundary=upper_boundary,
        )
    except EvidenceDomainError:
        raise
    except OverflowError as exc:
        raise invalid_capture_command(
            "collection window is outside the supported absolute-time range",
            field_path="snapshot_at",
        ) from exc


__all__ = [
    "CollectionWindows",
    "Episode",
    "LogicalWindow",
    "canonical_utc",
    "derive_collection_windows",
    "derive_episode",
    "format_utc",
]
