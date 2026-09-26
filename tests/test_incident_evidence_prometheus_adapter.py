import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from incident_evidence import (
    EvidenceFailureKind,
    EvidenceSource,
    LogicalWindow,
    SelectorFact,
    SourceAdapterRequest,
    SourceStatus,
    load_evidence_policy,
)
from incident_evidence.adapters import PrometheusRangeAdapter


FIXTURES = Path(__file__).parent / "fixtures" / "incident_evidence"
SELECTOR_ALLOWLIST = load_evidence_policy(
    Path(__file__).parents[1] / "configs" / "incident_evidence.yaml"
).selector_allowlist
UTC = timezone.utc


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def request(**overrides):
    values = {
        "source": EvidenceSource.PROMETHEUS,
        "incident_id": "INC-1",
        "logical_window": LogicalWindow(
            datetime(2026, 9, 21, 2, 0, tzinfo=UTC),
            datetime(2026, 9, 21, 2, 1, tzinfo=UTC),
        ),
        "selectors": (
            SelectorFact(EvidenceSource.PROMETHEUS, "service_name", "payments", "payments", "selectors-v1"),
        ),
        "query_resource": "api_requests_per_sec",
        "query_semantic_identity": "prom-range-api-qps-v1",
        "query_version": "promql-v1",
        "adapter_contract_version": "prom-adapter-v1",
        "bounds_policy_identity": "bounds-v1",
        "request_budget_identity": "timeout-2s-v1",
        "timeout_seconds": 2,
        "max_records": 10,
        "max_content_bytes": 256,
        "max_labels_per_record": 8,
        "max_label_value_characters": 64,
        "query_resolution_seconds": 15,
    }
    values.update(overrides)
    return SourceAdapterRequest(**values)


def adapter(payload):
    return PrometheusRangeAdapter(
        "http://prom.local/api/v1/query_range",
        selector_allowlist=SELECTOR_ALLOWLIST,
        http_client=lambda *_a, **_k: payload,
    )


def test_matrix_is_deduplicated_and_deterministically_sorted():
    result = adapter(fixture("prometheus_success.json")).collect(request())
    assert result.status is SourceStatus.AVAILABLE
    assert [(dict(r.labels)["instance"], r.timestamp_seconds, r.value_text) for r in result.records] == [
        ("a", "1789956020", "11"),
        ("b", "1789956010", "10"),
        ("b", "1789956030", "12.5"),
    ]
    assert result.summary.bounds.dedup_applied is True
    assert result.summary.bounds.omitted_count == 1


def test_empty_matrix_is_typed_empty():
    result = adapter(fixture("prometheus_empty.json")).collect(request())
    assert result.status is SourceStatus.EMPTY
    assert result.records == ()


@pytest.mark.parametrize("failure", [TimeoutError("late"), requests.ConnectionError("offline")])
def test_transport_failure_is_unavailable(failure):
    def fail(*_args, **_kwargs):
        raise failure

    result = PrometheusRangeAdapter(
        "http://prom",
        selector_allowlist=SELECTOR_ALLOWLIST,
        http_client=fail,
    ).collect(request())
    assert result.status is SourceStatus.UNAVAILABLE
    assert result.summary.safe_failure_kind is EvidenceFailureKind.SOURCE_UNAVAILABLE


class Response:
    def __init__(self, status_code):
        self.status_code = status_code

    def json(self):
        return {}


def test_http_503_is_unavailable_and_400_is_invalid():
    assert adapter(Response(503)).collect(request()).status is SourceStatus.UNAVAILABLE
    assert adapter(Response(400)).collect(request()).status is SourceStatus.INVALID


@pytest.mark.parametrize("name", ["prometheus_wrong_type.json", "prometheus_out_of_window.json"])
def test_wrong_type_and_out_of_window_are_invalid(name):
    result = adapter(fixture(name)).collect(request())
    assert result.status is SourceStatus.INVALID
    assert result.records == ()


@pytest.mark.parametrize("value", ["NaN", "Inf", "-Inf"])
def test_non_finite_values_are_invalid(value):
    payload = fixture("prometheus_success.json")
    payload["data"]["result"][0]["values"][0][1] = value
    assert adapter(payload).collect(request()).status is SourceStatus.INVALID


def test_malformed_tuple_unsafe_labels_and_unsafe_query_are_invalid():
    malformed = fixture("prometheus_success.json")
    malformed["data"]["result"][0]["values"][0] = [1789956010]
    assert adapter(malformed).collect(request()).status is SourceStatus.INVALID

    unsafe = fixture("prometheus_success.json")
    unsafe["data"]["result"][0]["metric"]["note"] = "token=abc"
    assert adapter(unsafe).collect(request()).status is SourceStatus.INVALID

    assert adapter(fixture("prometheus_empty.json")).collect(
        request(query_resource='metric{job="unsafe"}')
    ).status is SourceStatus.INVALID

    forbidden_selector = SelectorFact(EvidenceSource.PROMETHEUS, "ground_truth", "none", "none", "selectors-v1")
    forbidden = adapter(fixture("prometheus_empty.json")).collect(request(selectors=(forbidden_selector,)))
    assert forbidden.status is SourceStatus.INVALID
    assert forbidden.summary.query_provenance.selectors == ()


@pytest.mark.parametrize("field_name", ["pod", "authorization", "ground_truth"])
def test_unapproved_selector_is_rejected_before_transport(field_name):
    calls = 0

    def should_not_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return fixture("prometheus_empty.json")

    selector = SelectorFact(
        EvidenceSource.PROMETHEUS,
        field_name,
        "payments-0",
        "payments-0",
        "selectors-v1",
    )
    result = PrometheusRangeAdapter(
        "http://prom.local/api/v1/query_range",
        selector_allowlist=SELECTOR_ALLOWLIST,
        http_client=should_not_run,
    ).collect(request(selectors=(selector,)))

    assert result.status is SourceStatus.INVALID
    assert calls == 0


def test_limit_is_fail_closed_with_explicit_omission_facts():
    result = adapter(fixture("prometheus_success.json")).collect(request(max_records=2))
    assert result.status is SourceStatus.INVALID
    assert result.summary.bounds.truncation_applied is True
    assert result.summary.bounds.observed_candidate_count == 4
    assert result.summary.bounds.omitted_count == 4


def test_query_params_and_provenance_are_deterministic_and_secret_free():
    captured = {}

    def fake(url, *, params, timeout):
        captured.update(url=url, params=params, timeout=timeout)
        return fixture("prometheus_empty.json")

    result = PrometheusRangeAdapter(
        "http://user:pw@prom/api?api_key=x",
        selector_allowlist=SELECTOR_ALLOWLIST,
        http_client=fake,
    ).collect(request())
    assert captured["params"] == {
        "query": 'api_requests_per_sec{service_name="payments"}',
        "start": "1789956000",
        "end": "1789956060",
        "step": "15",
    }
    assert captured["timeout"] == 2.0
    provenance = repr(result.summary.query_provenance)
    assert "api_key" not in provenance and "user" not in provenance and "pw" not in provenance
