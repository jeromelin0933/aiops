"""Fail-closed Loki ``query_range`` adapter for Candidate-B evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

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
_INTEGER = re.compile(r"^-?[0-9]+$")
_NANOSECONDS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True, slots=True)
class LokiLogRecord:
    timestamp: datetime
    timestamp_ns: int
    labels: tuple[tuple[str, str], ...]
    line: str


@dataclass(frozen=True, slots=True)
class LokiSourceResult:
    summary: SourceCollectionSummary
    records: tuple[LokiLogRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        if self.summary.source is not EvidenceSource.LOKI:
            raise ValueError("Loki result requires a Loki source summary")
        if self.summary.record_count != len(self.records):
            raise ValueError("summary count must match Loki records")

    @property
    def status(self) -> SourceStatus:
        return self.summary.status


class LokiRangeAdapter:
    """Collect one bounded Loki range response through an injectable HTTP client."""

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

    def collect(self, request: SourceAdapterRequest) -> LokiSourceResult:
        if request.source is not EvidenceSource.LOKI:
            raise ValueError("Loki adapter requires a LOKI request")
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
            "start": str(_epoch_ns(request.logical_window.start)),
            "end": str(_epoch_ns(request.logical_window.end)),
            "limit": str(request.max_records + 1),
            "direction": "forward",
        }
        fetched = self._fetch(request, params)
        if isinstance(fetched, LokiSourceResult):
            return fetched
        try:
            records, input_count = self._normalize(fetched, request)
        except (EvidenceDomainError, OverflowError, TypeError, ValueError):
            return self._failure(request, SourceStatus.INVALID, "response validation failed")
        if input_count > request.max_records:
            return self._failure(
                request,
                SourceStatus.INVALID,
                "source result limit reached; enumeration incomplete",
                continuation_complete=False,
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
        return LokiSourceResult(
            _summary(request, status, bounds, (), page_count=1), deduped
        )

    def _query(self, request: SourceAdapterRequest) -> str:
        if request.query_resource != "streams":
            raise ValueError("Loki query resource must be streams")
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
        return "{" + ",".join(parts) + "}"

    def _fetch(
        self, request: SourceAdapterRequest, params: Mapping[str, str]
    ) -> object | LokiSourceResult:
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
    def _normalize(payload: object, request: SourceAdapterRequest) -> tuple[list[LokiLogRecord], int]:
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ValueError("unrecognized Loki response")
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("resultType") != "streams":
            raise ValueError("Loki resultType must be streams")
        streams = data.get("result")
        if not isinstance(streams, list):
            raise ValueError("Loki result must be a list")
        start_ns = _epoch_ns(request.logical_window.start)
        end_ns = _epoch_ns(request.logical_window.end)
        records: list[LokiLogRecord] = []
        input_count = 0
        for stream in streams:
            if not isinstance(stream, dict) or set(stream) != {"stream", "values"}:
                raise ValueError("invalid Loki stream")
            labels = _labels(stream["stream"], request)
            values = stream["values"]
            if not isinstance(values, list):
                raise ValueError("invalid Loki values")
            for value in values:
                input_count += 1
                if input_count > request.max_records:
                    return records, input_count
                if not isinstance(value, list) or len(value) != 2:
                    raise ValueError("invalid Loki entry")
                raw_timestamp, line = value
                if not isinstance(raw_timestamp, str) or not _INTEGER.fullmatch(raw_timestamp):
                    raise ValueError("invalid Loki timestamp")
                timestamp_ns = int(raw_timestamp)
                if not start_ns <= timestamp_ns <= end_ns:
                    raise ValueError("Loki entry is outside the logical window")
                if not isinstance(line, str) or len(line.encode("utf-8")) > request.max_content_bytes:
                    raise ValueError("invalid Loki line")
                validate_safe_text(line, field_path="loki.line")
                timestamp = datetime.fromtimestamp(
                    timestamp_ns / _NANOSECONDS_PER_SECOND, tz=timezone.utc
                )
                records.append(LokiLogRecord(timestamp, timestamp_ns, labels, line))
        return records, input_count

    @staticmethod
    def _failure(
        request: SourceAdapterRequest,
        status: SourceStatus,
        finding: str,
        *,
        continuation_complete: bool = True,
        truncation: bool = False,
        omit_selectors: bool = False,
    ) -> LokiSourceResult:
        kind = (
            EvidenceFailureKind.SOURCE_UNAVAILABLE
            if status is SourceStatus.UNAVAILABLE
            else EvidenceFailureKind.SOURCE_INVALID
        )
        bounds = BoundsOmissionFacts(
            request.bounds_policy_identity,
            None,
            0,
            None,
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
        return LokiSourceResult(
            _summary(
                request,
                status,
                bounds,
                (finding,),
                page_count=1 if status is SourceStatus.INVALID else None,
                continuation_complete=continuation_complete,
                failure_kind=kind,
                omit_selectors=omit_selectors,
            ),
            (),
        )


def _labels(value: object, request: SourceAdapterRequest) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or len(value) > request.max_labels_per_record:
        raise ValueError("invalid Loki labels")
    labels = []
    for name, item in value.items():
        if (
            not isinstance(name, str)
            or not _LABEL_NAME.fullmatch(name)
            or _forbidden_field(name)
        ):
            raise ValueError("invalid Loki label name")
        if not isinstance(item, str) or len(item) > request.max_label_value_characters:
            raise ValueError("invalid Loki label value")
        validate_safe_text(item, field_path=f"loki.labels.{name}")
        labels.append((name, item))
    return tuple(sorted(labels))


def _epoch_ns(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (
        delta.days * 86400 * _NANOSECONDS_PER_SECOND
        + delta.seconds * _NANOSECONDS_PER_SECOND
        + delta.microseconds * 1000
    )


def _forbidden_field(value: str) -> bool:
    normalized = value.strip().lower().replace("-", "_")
    return normalized in SENSITIVE_FIELD_NAMES or normalized in FORBIDDEN_PRODUCTION_FIELDS


def _record_key(record: LokiLogRecord) -> tuple[object, ...]:
    return (record.timestamp_ns, record.labels, record.line)


def _summary(
    request: SourceAdapterRequest,
    status: SourceStatus,
    bounds: BoundsOmissionFacts,
    findings: tuple[str, ...],
    *,
    page_count: int | None,
    continuation_complete: bool = True,
    failure_kind: EvidenceFailureKind | None = None,
    omit_selectors: bool = False,
) -> SourceCollectionSummary:
    collection = CollectionProvenance(
        EvidenceSource.LOKI,
        request.adapter_contract_version,
        request.logical_window,
        page_count,
        continuation_complete,
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
        EvidenceSource.LOKI,
        status,
        bounds.included_count,
        query_provenance,
        collection,
        bounds,
        findings,
        failure_kind,
    )


__all__ = ["LokiLogRecord", "LokiRangeAdapter", "LokiSourceResult"]
