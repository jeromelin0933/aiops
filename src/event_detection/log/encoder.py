"""Encode raw log features as a fixed numeric vector."""

from src.event_detection.model.schema import EncodedFeatureVector, RawFeatures


class FeatureEncoder:
    """Encode category, level, and boolean values without fitted state."""

    LEVEL_MAP = {"INFO": 1.0, "WARN": 2.0, "ERROR": 3.0}

    def __init__(self, config: dict):
        known_services = config.get("known_services", [])
        known_error_types = config.get("known_error_types", [])
        self._service_map = {
            service: float(index + 1)
            for index, service in enumerate(known_services)
        }
        self._error_type_map = {
            error_type: float(index + 1)
            for index, error_type in enumerate(known_error_types)
        }

    def encode(self, raw: RawFeatures) -> EncodedFeatureVector:
        return EncodedFeatureVector(
            status_code=float(raw.status_code),
            duration_ms=float(raw.duration_ms),
            memory_usage_pct=float(raw.memory_usage_pct),
            rate_limit_quota=float(raw.rate_limit_quota),
            service_name_encoded=self._service_map.get(raw.service_name, 0.0),
            error_type_encoded=self._error_type_map.get(raw.error_type, 0.0),
            level_encoded=self.LEVEL_MAP.get(raw.level, 1.0),
            has_source_ip=float(raw.has_source_ip),
            has_downstream_service=float(raw.has_downstream_service),
            has_external_service=float(raw.has_external_service),
            has_transaction_id=float(raw.has_transaction_id),
            has_target_service=float(raw.has_target_service),
            is_error=float(raw.is_error),
            is_warn=float(raw.is_warn),
            is_5xx=float(raw.is_5xx),
            is_4xx=float(raw.is_4xx),
            is_401=float(raw.is_401),
            is_429=float(raw.is_429),
            is_oom=float(raw.is_oom),
        )
