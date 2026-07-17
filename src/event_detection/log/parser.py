"""Parse, validate, and normalize JSON log lines."""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("LogParser")

REQUIRED_FIELDS = {"timestamp", "level", "service_name", "status_code", "duration_ms"}
OPTIONAL_FIELDS_DEFAULT = {
    "trace_id": None,
    "error_type": None,
    "error_message": None,
    "source_ip": None,
    "user_id": None,
    "downstream_service": None,
    "external_service": None,
    "transaction_id": None,
    "memory_usage_pct": None,
    "target_service": None,
    "rate_limit_quota": None,
}


class LogParser:
    """Convert a raw JSON line to a normalized dictionary or ``None``."""

    def parse(self, raw_line: str) -> Optional[dict]:
        try:
            entry = json.loads(raw_line)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug("JSON decode error: %s", exc)
            return None

        if not isinstance(entry, dict) or not self._validate(entry):
            return None

        self._fill_optional(entry)
        if not self._coerce_types(entry):
            return None

        parsed_timestamp = self._parse_timestamp(entry.get("timestamp", ""))
        if parsed_timestamp is None:
            return None
        entry["_parsed_timestamp"] = parsed_timestamp
        return entry

    def _validate(self, entry: dict) -> bool:
        return REQUIRED_FIELDS.issubset(entry)

    def _fill_optional(self, entry: dict) -> None:
        for field, default in OPTIONAL_FIELDS_DEFAULT.items():
            entry.setdefault(field, default)

    def _coerce_types(self, entry: dict) -> bool:
        try:
            entry["status_code"] = int(entry["status_code"])
            entry["duration_ms"] = int(entry["duration_ms"])
            if entry["memory_usage_pct"] is not None:
                entry["memory_usage_pct"] = float(entry["memory_usage_pct"])
            if entry["rate_limit_quota"] is not None:
                entry["rate_limit_quota"] = int(entry["rate_limit_quota"])
        except (ValueError, TypeError):
            return False
        return True

    def _parse_timestamp(self, ts_str: str) -> Optional[datetime]:
        if not isinstance(ts_str, str) or not ts_str.strip():
            return None
        try:
            normalized = ts_str.strip()
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None
