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
from incident_evidence.adapters import LokiRangeAdapter


FIXTURES = Path(__file__).parent / "fixtures" / "incident_evidence"
SELECTOR_ALLOWLIST = load_evidence_policy(
    Path(__file__).parents[1] / "configs" / "incident_evidence.yaml"
).selector_allowlist
UTC = timezone.utc


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def request(**overrides):
    values = {
        "source": EvidenceSource.LOKI,
        "incident_id": "INC-1",
        "logical_window": LogicalWindow(
            datetime(2026, 9, 21, 2, 0, tzinfo=UTC),
            datetime(2026, 9, 21, 2, 1, tzinfo=UTC),
        ),
        "selectors": (
            SelectorFact(EvidenceSource.LOKI, "service_name", "payments", "payments", "selectors-v1"),
        ),
        "query_resource": "streams",
        "query_semantic_identity": "loki-stream-query-v1",
        "query_version": "logql-v1",
        "adapter_contract_version": "loki-adapter-v1",
        "bounds_policy_identity": "bounds-v1",
        "request_budget_identity": "timeout-2s-v1",
        "timeout_seconds": 2,
        "max_records": 10,
        "max_content_bytes": 256,
        "max_labels_per_record": 8,
        "max_label_value_characters": 64,
    }
    values.update(overrides)
    return SourceAdapterRequest(**values)


def adapter(payload):
    return LokiRangeAdapter(
        "http://loki.local/loki/api/v1/query_range",
        selector_allowlist=SELECTOR_ALLOWLIST,
        http_client=lambda *_a, **_k: payload,
    )


def test_non_empty_is_deduplicated_and_deterministically_normalized():
    result = adapter(fixture("loki_success.json")).collect(request())

    assert result.status is SourceStatus.AVAILABLE
    assert [record.line for record in result.records] == ["request started", "database unavailable"]
    assert result.summary.bounds.dedup_applied is True
    assert result.summary.bounds.dedup_input_count == 3
    assert result.summary.bounds.dedup_output_count == 2
    assert result.summary.bounds.omitted_count == 1
    assert result.summary.collection_provenance.page_count == 1


def test_empty_success_is_typed_empty_not_an_untyped_list():
    result = adapter(fixture("loki_empty.json")).collect(request())
    assert result.status is SourceStatus.EMPTY
    assert result.records == ()
    assert result.summary.record_count == 0


@pytest.mark.parametrize("failure", [TimeoutError("late"), requests.ConnectionError("offline")])
def test_transport_failure_is_unavailable_with_safe_failure(failure):
    def fail(*_args, **_kwargs):
        raise failure

    result = LokiRangeAdapter(
        "http://user:secret@loki.local/api?token=secret",
        selector_allowlist=SELECTOR_ALLOWLIST,
        http_client=fail,
    ).collect(request())
    assert result.status is SourceStatus.UNAVAILABLE
    assert result.summary.safe_failure_kind is EvidenceFailureKind.SOURCE_UNAVAILABLE
    assert "secret" not in repr(result)


class Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_http_503_is_unavailable_and_400_is_invalid():
    unavailable = adapter(Response(503)).collect(request())
    invalid = adapter(Response(400)).collect(request())
    assert unavailable.status is SourceStatus.UNAVAILABLE
    assert invalid.status is SourceStatus.INVALID


@pytest.mark.parametrize("name", ["loki_malformed.json", "loki_out_of_window.json"])
def test_malformed_and_out_of_window_are_invalid_not_empty(name):
    result = adapter(fixture(name)).collect(request())
    assert result.status is SourceStatus.INVALID
    assert result.records == ()
    assert result.summary.safe_failure_kind is EvidenceFailureKind.SOURCE_INVALID


def test_unsafe_label_or_line_and_noncanonical_selector_are_invalid():
    unsafe_label = fixture("loki_success.json")
    unsafe_label["data"]["result"][0]["stream"]["note"] = "authorization=abc"
    assert adapter(unsafe_label).collect(request()).status is SourceStatus.INVALID

    unsafe_line = fixture("loki_success.json")
    unsafe_line["data"]["result"][0]["values"][0][1] = "Bearer abc.def"
    assert adapter(unsafe_line).collect(request()).status is SourceStatus.INVALID

    bad_selector = SelectorFact(EvidenceSource.LOKI, "service_name", 'pay"ments', 'pay"ments', "selectors-v1")
    assert adapter(fixture("loki_empty.json")).collect(request(selectors=(bad_selector,))).status is SourceStatus.INVALID

    forbidden_selector = SelectorFact(EvidenceSource.LOKI, "authorization", "harmless", "harmless", "selectors-v1")
    forbidden = adapter(fixture("loki_empty.json")).collect(request(selectors=(forbidden_selector,)))
    assert forbidden.status is SourceStatus.INVALID
    assert forbidden.summary.query_provenance.selectors == ()


@pytest.mark.parametrize("field_name", ["pod", "authorization", "ground_truth"])
def test_unapproved_selector_is_rejected_before_transport(field_name):
    calls = 0

    def should_not_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return fixture("loki_empty.json")

    selector = SelectorFact(
        EvidenceSource.LOKI,
        field_name,
        "payments-0",
        "payments-0",
        "selectors-v1",
    )
    result = LokiRangeAdapter(
        "http://loki.local/loki/api/v1/query_range",
        selector_allowlist=SELECTOR_ALLOWLIST,
        http_client=should_not_run,
    ).collect(request(selectors=(selector,)))

    assert result.status is SourceStatus.INVALID
    assert calls == 0


def test_limit_reached_is_invalid_with_explicit_incomplete_bounds():
    result = adapter(fixture("loki_success.json")).collect(request(max_records=2))
    assert result.status is SourceStatus.INVALID
    assert result.summary.collection_provenance.continuation_complete is False
    assert result.summary.bounds.truncation_applied is True
    assert result.summary.bounds.observed_candidate_count is None
    assert result.summary.bounds.omission_reason


def test_request_params_are_closed_bounded_and_provenance_excludes_transport_secrets():
    captured = {}

    def fake(url, *, params, timeout):
        captured.update(url=url, params=params, timeout=timeout)
        return fixture("loki_empty.json")

    result = LokiRangeAdapter(
        "http://user:pw@loki.local/api?token=x",
        selector_allowlist=SELECTOR_ALLOWLIST,
        http_client=fake,
    ).collect(request())
    assert captured["params"] == {
        "query": '{service_name="payments"}',
        "start": "1789956000000000000",
        "end": "1789956060000000000",
        "limit": "11",
        "direction": "forward",
    }
    assert captured["timeout"] == 2.0
    assert "user" not in repr(result.summary.query_provenance)
    assert "token" not in repr(result.summary.query_provenance)
