"""Candidate-B bounded source adapters."""

from .loki import LokiLogRecord, LokiRangeAdapter, LokiSourceResult
from .prometheus import (
    PrometheusRangeAdapter,
    PrometheusSampleRecord,
    PrometheusSourceResult,
)

__all__ = [
    "LokiLogRecord",
    "LokiRangeAdapter",
    "LokiSourceResult",
    "PrometheusRangeAdapter",
    "PrometheusSampleRecord",
    "PrometheusSourceResult",
]
