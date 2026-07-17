"""Data structures shared by the Phase 1 log processing pipeline."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RawFeatures:
    """Features extracted from one parsed log entry before encoding."""

    status_code: int = 200
    duration_ms: int = 0
    memory_usage_pct: float = 0.0
    rate_limit_quota: int = 0
    service_name: str = "unknown"
    error_type: str = "unknown"
    level: str = "INFO"
    has_source_ip: bool = False
    has_downstream_service: bool = False
    has_external_service: bool = False
    has_transaction_id: bool = False
    has_target_service: bool = False
    is_error: bool = False
    is_warn: bool = False
    is_5xx: bool = False
    is_4xx: bool = False
    is_401: bool = False
    is_429: bool = False
    is_oom: bool = False
    raw_source_ip: Optional[str] = None
    raw_downstream_service: Optional[str] = None
    raw_external_service: Optional[str] = None
    raw_trace_id: Optional[str] = None
    raw_transaction_id: Optional[str] = None
    raw_target_service: Optional[str] = None
    raw_timestamp: Optional[str] = None


@dataclass
class EncodedFeatureVector:
    """Fixed-order, 19-dimensional numeric representation of RawFeatures."""

    status_code: float = 0.0
    duration_ms: float = 0.0
    memory_usage_pct: float = 0.0
    rate_limit_quota: float = 0.0
    service_name_encoded: float = 0.0
    error_type_encoded: float = 0.0
    level_encoded: float = 1.0
    has_source_ip: float = 0.0
    has_downstream_service: float = 0.0
    has_external_service: float = 0.0
    has_transaction_id: float = 0.0
    has_target_service: float = 0.0
    is_error: float = 0.0
    is_warn: float = 0.0
    is_5xx: float = 0.0
    is_4xx: float = 0.0
    is_401: float = 0.0
    is_429: float = 0.0
    is_oom: float = 0.0

    def to_list(self) -> list:
        """Return feature values in the stable training/inference order."""
        return [
            self.status_code, self.duration_ms,
            self.memory_usage_pct, self.rate_limit_quota,
            self.service_name_encoded, self.error_type_encoded,
            self.level_encoded, self.has_source_ip,
            self.has_downstream_service, self.has_external_service,
            self.has_transaction_id, self.has_target_service,
            self.is_error, self.is_warn, self.is_5xx, self.is_4xx,
            self.is_401, self.is_429, self.is_oom,
        ]

    @staticmethod
    def feature_names() -> list:
        """Return names corresponding exactly to :meth:`to_list`."""
        return [
            "status_code", "duration_ms", "memory_usage_pct",
            "rate_limit_quota", "service_name_encoded",
            "error_type_encoded", "level_encoded", "has_source_ip",
            "has_downstream_service", "has_external_service",
            "has_transaction_id", "has_target_service", "is_error",
            "is_warn", "is_5xx", "is_4xx", "is_401", "is_429",
            "is_oom",
        ]


@dataclass
class WindowFeatureVector:
    """Fixed-order, 23-dimensional summary of one log event-time window."""

    total_log_count: float = 0.0
    error_count: float = 0.0
    warn_count: float = 0.0
    error_rate: float = 0.0
    warn_rate: float = 0.0
    status_4xx_count: float = 0.0
    status_5xx_count: float = 0.0
    status_401_count: float = 0.0
    status_429_count: float = 0.0
    unique_service_count: float = 0.0
    unique_trace_id_count: float = 0.0
    unique_source_ip_count: float = 0.0
    unique_downstream_count: float = 0.0
    unique_external_service_count: float = 0.0
    unique_target_service_count: float = 0.0
    max_same_source_ip_count: float = 0.0
    max_same_downstream_count: float = 0.0
    max_same_target_service_count: float = 0.0
    max_duration_ms: float = 0.0
    mean_duration_ms: float = 0.0
    max_memory_pct: float = 0.0
    mean_memory_pct: float = 0.0
    oom_count: float = 0.0

    def to_list(self) -> list:
        """Return values in the stable training/inference order."""
        return [getattr(self, name) for name in self.feature_names()]

    @staticmethod
    def feature_names() -> list:
        """Return names corresponding exactly to :meth:`to_list`."""
        return [
            "total_log_count", "error_count", "warn_count", "error_rate",
            "warn_rate", "status_4xx_count", "status_5xx_count",
            "status_401_count", "status_429_count", "unique_service_count",
            "unique_trace_id_count", "unique_source_ip_count",
            "unique_downstream_count", "unique_external_service_count",
            "unique_target_service_count", "max_same_source_ip_count",
            "max_same_downstream_count", "max_same_target_service_count",
            "max_duration_ms", "mean_duration_ms", "max_memory_pct",
            "mean_memory_pct", "oom_count",
        ]


@dataclass
class WindowSummary:
    """Window metadata used for event classification, not model inference."""

    window_start: str = ""
    window_end: str = ""
    total_log_count: int = 0
    error_count: int = 0
    warn_count: int = 0
    unique_services: list = field(default_factory=list)
    top_error_types: list = field(default_factory=list)
    max_duration_ms: float = 0.0
    mean_duration_ms: float = 0.0
    max_memory_pct: float = 0.0
    source_ip_401_counts: dict = field(default_factory=dict)
    trace_error_services: dict = field(default_factory=dict)
    trace_downstreams: dict = field(default_factory=dict)
    downstream_error_services: dict = field(default_factory=dict)
    target_429_counts: dict = field(default_factory=dict)
    external_failure_logs: list = field(default_factory=list)
    raw_log_sample: list = field(default_factory=list)
