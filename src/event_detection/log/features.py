"""Extract raw features from normalized log entries."""

from src.event_detection.model.schema import RawFeatures


class FeatureExtractor:
    """Create :class:`RawFeatures` from one parsed log entry."""

    def extract_one(self, entry: dict) -> RawFeatures:
        status_code = int(entry.get("status_code", 200))
        duration_ms = int(entry.get("duration_ms", 0))
        memory_usage_pct = float(entry.get("memory_usage_pct") or 0.0)
        rate_limit_quota = int(entry.get("rate_limit_quota") or 0)
        service_name = str(entry.get("service_name") or "unknown")
        error_type = str(entry.get("error_type") or "unknown")
        level = str(entry.get("level") or "INFO").upper()

        source_ip = entry.get("source_ip")
        downstream_service = entry.get("downstream_service")
        external_service = entry.get("external_service")
        transaction_id = entry.get("transaction_id")
        target_service = entry.get("target_service")

        return RawFeatures(
            status_code=status_code,
            duration_ms=duration_ms,
            memory_usage_pct=memory_usage_pct,
            rate_limit_quota=rate_limit_quota,
            service_name=service_name,
            error_type=error_type,
            level=level,
            has_source_ip=source_ip is not None,
            has_downstream_service=downstream_service is not None,
            has_external_service=external_service is not None,
            has_transaction_id=transaction_id is not None,
            has_target_service=target_service is not None,
            is_error=level == "ERROR",
            is_warn=level == "WARN",
            is_5xx=status_code >= 500,
            is_4xx=400 <= status_code < 500,
            is_401=status_code == 401,
            is_429=status_code == 429,
            is_oom=error_type == "OutOfMemoryError",
            raw_source_ip=source_ip,
            raw_downstream_service=downstream_service,
            raw_external_service=external_service,
            raw_trace_id=entry.get("trace_id"),
            raw_transaction_id=transaction_id,
            raw_target_service=target_service,
            raw_timestamp=entry.get("timestamp"),
        )
