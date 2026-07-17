"""Aggregate parsed logs or raw features into event-time window features."""

from collections import Counter
from datetime import datetime, timezone
from typing import Optional, Union

from src.event_detection.model.schema import RawFeatures, WindowFeatureVector


class WindowFeatureAggregator:
    """Create model features from one event-time-bounded batch of logs."""

    def __init__(self, window_seconds: int = 60, min_log_count: int = 5):
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if min_log_count <= 0:
            raise ValueError("min_log_count must be positive")
        self.window_seconds = window_seconds
        self.min_log_count = min_log_count

    def has_enough(self, entries: list) -> bool:
        return len(entries) >= self.min_log_count

    def aggregate(
        self,
        entries: list[Union[dict, RawFeatures]],
        window_end: Optional[Union[str, datetime]] = None,
    ) -> WindowFeatureVector:
        """Aggregate entries whose event time is within ``[end-window, end]``.

        If ``window_end`` is omitted, the newest event timestamp is used. Entries
        without timestamps remain eligible so callers may aggregate prepared
        batches of ``RawFeatures``. No wall-clock time is consulted.
        """
        selected = self._select_window(entries, window_end)
        if not selected:
            return WindowFeatureVector()

        total = len(selected)
        values = [self._values(item) for item in selected]
        error_count = sum(value["level"] == "ERROR" for value in values)
        warn_count = sum(value["level"] == "WARN" for value in values)
        statuses = [value["status_code"] for value in values]
        durations = [value["duration_ms"] for value in values]
        memories = [value["memory_usage_pct"] for value in values]

        def present(field):
            return [value[field] for value in values if value[field] is not None]

        source_ips = present("source_ip")
        downstreams = present("downstream_service")
        targets = present("target_service")

        return WindowFeatureVector(
            total_log_count=float(total),
            error_count=float(error_count), warn_count=float(warn_count),
            error_rate=float(error_count / total), warn_rate=float(warn_count / total),
            status_4xx_count=float(sum(400 <= code < 500 for code in statuses)),
            status_5xx_count=float(sum(500 <= code < 600 for code in statuses)),
            status_401_count=float(sum(code == 401 for code in statuses)),
            status_429_count=float(sum(code == 429 for code in statuses)),
            unique_service_count=float(len(set(present("service_name")))),
            unique_trace_id_count=float(len(set(present("trace_id")))),
            unique_source_ip_count=float(len(set(source_ips))),
            unique_downstream_count=float(len(set(downstreams))),
            unique_external_service_count=float(len(set(present("external_service")))),
            unique_target_service_count=float(len(set(targets))),
            max_same_source_ip_count=float(self._maximum_count(source_ips)),
            max_same_downstream_count=float(self._maximum_count(downstreams)),
            max_same_target_service_count=float(self._maximum_count(targets)),
            max_duration_ms=float(max(durations, default=0.0)),
            mean_duration_ms=float(sum(durations) / len(durations)) if durations else 0.0,
            max_memory_pct=float(max(memories, default=0.0)),
            mean_memory_pct=float(sum(memories) / len(memories)) if memories else 0.0,
            oom_count=float(sum(value["is_oom"] for value in values)),
        )

    def _select_window(self, entries, window_end):
        timed = [(entry, self._timestamp(entry)) for entry in entries]
        timestamps = [timestamp for _, timestamp in timed if timestamp is not None]
        end = self._parse_timestamp(window_end) if window_end is not None else (
            max(timestamps) if timestamps else None
        )
        if end is None:
            return list(entries)
        start_timestamp = end.timestamp() - self.window_seconds
        return [
            entry for entry, timestamp in timed
            if timestamp is None or start_timestamp <= timestamp.timestamp() <= end.timestamp()
        ]

    @classmethod
    def _timestamp(cls, entry):
        value = entry.get("_parsed_timestamp") or entry.get("timestamp") if isinstance(entry, dict) else entry.raw_timestamp
        return cls._parse_timestamp(value)

    @staticmethod
    def _parse_timestamp(value):
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            normalized = value.strip()
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise ValueError(f"invalid event timestamp: {value}") from exc
        else:
            raise TypeError("event timestamp must be a datetime or ISO-8601 string")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _maximum_count(items):
        return Counter(items).most_common(1)[0][1] if items else 0

    @staticmethod
    def _values(entry):
        if isinstance(entry, RawFeatures):
            return {
                "level": entry.level.upper(), "status_code": int(entry.status_code),
                "duration_ms": float(entry.duration_ms),
                "memory_usage_pct": float(entry.memory_usage_pct),
                "service_name": entry.service_name, "trace_id": entry.raw_trace_id,
                "source_ip": entry.raw_source_ip,
                "downstream_service": entry.raw_downstream_service,
                "external_service": entry.raw_external_service,
                "target_service": entry.raw_target_service,
                "is_oom": bool(entry.is_oom),
            }
        if not isinstance(entry, dict):
            raise TypeError("entries must contain parsed log dicts or RawFeatures")
        error_type = str(entry.get("error_type") or "")
        memory = entry.get("memory_usage_pct")
        return {
            "level": str(entry.get("level") or "INFO").upper(),
            "status_code": int(entry.get("status_code", 200)),
            "duration_ms": float(entry.get("duration_ms") or 0.0),
            "memory_usage_pct": float(memory) if memory is not None else 0.0,
            "service_name": entry.get("service_name"), "trace_id": entry.get("trace_id"),
            "source_ip": entry.get("source_ip"),
            "downstream_service": entry.get("downstream_service"),
            "external_service": entry.get("external_service"),
            "target_service": entry.get("target_service"),
            "is_oom": "OutOfMemory" in error_type or "OOM" in error_type,
        }
