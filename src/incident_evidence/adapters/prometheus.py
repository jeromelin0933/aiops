"""Fail-closed Prometheus ``query_range`` matrix adapter for Candidate-B."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import re

import requests

from ..contracts import (
    BoundsOmissionFacts,
    CollectionProvenance,
    EvidenceCompleteness,
    EvidenceSource,
    QueryProvenance,
    SourceAdapterRequest,
    SourceCollectionSummary,
    SourceStatus,
)
from ..errors import EvidenceDomainError, EvidenceFailureKind
from ..security import (
    FORBIDDEN_PRODUCTION_FIELDS,
    SENSITIVE_FIELD_NAMES,
    build_selector_fact,
    validate_safe_text,
)


_LABEL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_METRIC_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]*$")


@dataclass(frozen=True, slots=True)
class PrometheusSampleRecord:
    timestamp: datetime
    timestamp_seconds: str
    labels: tuple[tuple[str, str], ...]
    value: float
    value_text: str


@dataclass(frozen=True, slots=True)
class PrometheusSourceResult:
    summary: SourceCollectionSummary
    records: tuple[PrometheusSampleRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        if self.summary.source is not EvidenceSource.PROMETHEUS:
            raise ValueError("Prometheus result requires a Prometheus source summary")
        if self.summary.record_count != len(self.records):
            raise ValueError("summary count must match Prometheus records")

    @property
    def status(self) -> SourceStatus:
        return self.summary.status


class PrometheusRangeAdapter:
    """Collect a bounded successful matrix through an injectable HTTP client."""

    def __init__(
        self,
        endpoint: str,
        *,
        selector_allowlist: Mapping[EvidenceSource, frozenset[str]],
        http_client: Callable[..., object] = requests.get,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("endpoint must be a non-empty string")
        if not isinstance(selector_allowlist, Mapping):
            raise TypeError("selector_allowlist must be a mapping")
        self._endpoint = endpoint
        self._selector_allowlist = selector_allowlist
        self._http_client = http_client

    def collect(self, request: SourceAdapterRequest) -> PrometheusSourceResult:
        if request.source is not EvidenceSource.PROMETHEUS:
            raise ValueError("Prometheus adapter requires a PROMETHEUS request")
        try:
            query = self._query(request)
        except (EvidenceDomainError, TypeError, ValueError):
            return self._failure(
                request,
                SourceStatus.INVALID,
                "unsafe selector or query",
                omit_selectors=True,
            )
        params = {
            "query": query,
            "start": _decimal_text(_datetime_decimal(request.logical_window.start)),
            "end": _decimal_text(_datetime_decimal(request.logical_window.end)),
            "step": _decimal_text(Decimal(str(request.query_resolution_seconds))),
        }
        fetched = self._fetch(request, params)
        if isinstance(fetched, PrometheusSourceResult):
            return fetched
        try:
            records, input_count, limit_reached = self._normalize(fetched, request)
        except (EvidenceDomainError, InvalidOperation, OverflowError, TypeError, ValueError):
            return self._failure(request, SourceStatus.INVALID, "response validation failed")
        if limit_reached:
            return self._failure(
                request,
                SourceStatus.INVALID,
                "record limit exceeded",
                observed_count=input_count,
                truncation=True,
            )
        deduped = tuple(sorted(set(records), key=_record_key))
        status = SourceStatus.AVAILABLE if deduped else SourceStatus.EMPTY
        omitted = input_count - len(deduped)
        bounds = BoundsOmissionFacts(
            request.bounds_policy_identity,
            input_count,
            len(deduped),
            omitted,
            "canonical duplicate" if omitted else None,
            False,
            None,
            False,
            None,
            False,
            bool(omitted),
            input_count if omitted else None,
            len(deduped) if omitted else None,
            False,
            None,
            EvidenceCompleteness.FULL,
        )
        return PrometheusSourceResult(
            _summary(request, status, bounds, (), page_count=1), deduped
        )

    def _query(self, request: SourceAdapterRequest) -> str:
        if not _METRIC_NAME.fullmatch(request.query_resource):
            raise ValueError("Prometheus query resource must be a metric name")
        if request.query_resolution_seconds is None:
            raise ValueError("Prometheus range query requires a resolution")
        parts = []
        for selector in request.selectors:
            if (
                not _LABEL_NAME.fullmatch(selector.field_name)
                or _forbidden_field(selector.field_name)
            ):
                raise ValueError("selector field is unsafe")
            validated = build_selector_fact(
                request.source,
                selector.field_name,
                selector.normalized_value,
                selector_policy_version=selector.selector_policy_version,
                allowlist=self._selector_allowlist,
            )
            if validated != selector:
                raise ValueError("selector escaping is not canonical")
            validate_safe_text(selector.normalized_value, field_path=selector.field_name)
            parts.append(f'{selector.field_name}="{selector.escaped_value}"')
        return request.query_resource + ("{" + ",".join(parts) + "}" if parts else "")

    def _fetch(
        self, request: SourceAdapterRequest, params: Mapping[str, str]
    ) -> object | PrometheusSourceResult:
        try:
            response = self._http_client(
                self._endpoint, params=params, timeout=request.timeout_seconds
            )
        except (OSError, requests.Timeout, requests.ConnectionError):
            return self._failure(request, SourceStatus.UNAVAILABLE, "source request unavailable")
        except requests.RequestException:
            return self._failure(request, SourceStatus.UNAVAILABLE, "source transport failed")
        status_code = getattr(response, "status_code", 200)
        if not isinstance(status_code, int):
            return self._failure(request, SourceStatus.INVALID, "invalid HTTP response")
        if status_code >= 500:
            return self._failure(request, SourceStatus.UNAVAILABLE, "source service unavailable")
        if status_code >= 400:
            return self._failure(request, SourceStatus.INVALID, "source rejected query")
        if hasattr(response, "json"):
            try:
                return response.json()
            except (TypeError, ValueError):
                return self._failure(request, SourceStatus.INVALID, "response is not JSON")
        return response

    @staticmethod
    def _normalize(
        payload: object, request: SourceAdapterRequest
    ) -> tuple[list[PrometheusSampleRecord], int, bool]:
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ValueError("unrecognized Prometheus response")
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("resultType") != "matrix":
            raise ValueError("Prometheus resultType must be matrix")
        series_values = data.get("result")
        if not isinstance(series_values, list):
            raise ValueError("Prometheus result must be a list")
        start = _datetime_decimal(request.logical_window.start)
        end = _datetime_decimal(request.logical_window.end)
        records: list[PrometheusSampleRecord] = []
        count = 0
        for series in series_values:
            if not isinstance(series, dict) or set(series) != {"metric", "values"}:
                raise ValueError("invalid Prometheus series")
            labels = _labels(series["metric"], request)
            values = series["values"]
            if not isinstance(values, list):
                raise ValueError("invalid Prometheus samples")
            for sample in values:
                count += 1
                if not isinstance(sample, list) or len(sample) != 2:
                    raise ValueError("invalid Prometheus sample tuple")
                raw_timestamp, raw_value = sample
                if isinstance(raw_timestamp, bool) or not isinstance(
                    raw_timestamp, (int, float, str)
                ):
                    raise ValueError("invalid Prometheus timestamp")
                timestamp_decimal = Decimal(str(raw_timestamp))
                if not timestamp_decimal.is_finite() or not start <= timestamp_decimal <= end:
                    raise ValueError("Prometheus sample is outside the logical window")
                if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float, str)):
                    raise ValueError("invalid Prometheus value")
                value_decimal = Decimal(str(raw_value))
                value = float(value_decimal)
                if not value_decimal.is_finite() or not math.isfinite(value):
                    raise ValueError("Prometheus value must be finite")
                if value == 0:
                    value = 0.0
                    value_decimal = Decimal(0)
                timestamp = datetime.fromtimestamp(float(timestamp_decimal), tz=timezone.utc)
                if count <= request.max_records:
                    records.append(
                        PrometheusSampleRecord(
                            timestamp,
                            _decimal_text(timestamp_decimal),
                            labels,
                            value,
                            _decimal_text(value_decimal),
                        )
                    )
        return records, count, count > request.max_records

    @staticmethod
    def _failure(
        request: SourceAdapterRequest,
        status: SourceStatus,
        finding: str,
        *,
        observed_count: int | None = None,
        truncation: bool = False,
        omit_selectors: bool = False,
    ) -> PrometheusSourceResult:
        kind = (
            EvidenceFailureKind.SOURCE_UNAVAILABLE
            if status is SourceStatus.UNAVAILABLE
            else EvidenceFailureKind.SOURCE_INVALID
        )
        bounds = BoundsOmissionFacts(
            request.bounds_policy_identity,
            observed_count,
            0,
            observed_count,
            finding,
            False,
            None,
            False,
            None,
            False,
            False,
            None,
            None,
            truncation,
            finding if truncation else None,
            EvidenceCompleteness.DEGRADED,
        )
        return PrometheusSourceResult(
            _summary(
                request,
                status,
                bounds,
                (finding,),
                page_count=1 if status is SourceStatus.INVALID else None,
                failure_kind=kind,
                omit_selectors=omit_selectors,
            ),
            (),
        )


def _labels(value: object, request: SourceAdapterRequest) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or len(value) > request.max_labels_per_record:
        raise ValueError("invalid Prometheus labels")
    labels = []
    for name, item in value.items():
        if (
            not isinstance(name, str)
            or not _LABEL_NAME.fullmatch(name)
            or _forbidden_field(name)
        ):
            raise ValueError("invalid Prometheus label name")
        if not isinstance(item, str) or len(item) > request.max_label_value_characters:
            raise ValueError("invalid Prometheus label value")
        validate_safe_text(item, field_path=f"prometheus.labels.{name}")
        labels.append((name, item))
    return tuple(sorted(labels))


def _datetime_decimal(value: datetime) -> Decimal:
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    microseconds = (
        delta.days * 86400 * 1_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    return Decimal(microseconds) / Decimal(1_000_000)


def _forbidden_field(value: str) -> bool:
    normalized = value.strip().lower().replace("-", "_")
    return normalized in SENSITIVE_FIELD_NAMES or normalized in FORBIDDEN_PRODUCTION_FIELDS


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _record_key(record: PrometheusSampleRecord) -> tuple[object, ...]:
    return (record.labels, Decimal(record.timestamp_seconds), Decimal(record.value_text))


def _summary(
    request: SourceAdapterRequest,
    status: SourceStatus,
    bounds: BoundsOmissionFacts,
    findings: tuple[str, ...],
    *,
    page_count: int | None,
    failure_kind: EvidenceFailureKind | None = None,
    omit_selectors: bool = False,
) -> SourceCollectionSummary:
    collection = CollectionProvenance(
        EvidenceSource.PROMETHEUS,
        request.adapter_contract_version,
        request.logical_window,
        page_count,
        True,
        status,
    )
    query_provenance = request.query_provenance
    if omit_selectors:
        query_provenance = QueryProvenance(
            request.source,
            request.query_semantic_identity,
            request.query_version,
            request.logical_window,
            (),
            request.adapter_contract_version,
            request.bounds_policy_identity,
            request.request_budget_identity,
        )
    return SourceCollectionSummary(
        EvidenceSource.PROMETHEUS,
        status,
        bounds.included_count,
        query_provenance,
        collection,
        bounds,
        findings,
        failure_kind,
    )


__all__ = [
    "PrometheusRangeAdapter",
    "PrometheusSampleRecord",
    "PrometheusSourceResult",
]
