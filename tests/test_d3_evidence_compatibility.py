"""D3 inspection/contract-gap evidence; no approved v2 wire semantics claimed."""
from dataclasses import replace
from pathlib import Path
import socket

import pytest

from incident_evidence import EvidenceSource, SourceStatus, SelectorFact, load_evidence_policy
from incident_evidence.errors import EvidenceDomainError, EvidenceFailureKind
from incident_evidence.security import build_selector_fact
from runtime_orchestration import evidence_compatibility as d3
from test_incident_evidence_trusted_core import event, incident, command, IncidentReader, EventReader
from test_incident_evidence_loki_adapter import request as log_request, adapter, SELECTOR_ALLOWLIST, fixture
from test_incident_evidence_prometheus_adapter import request as metric_request


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError("no source/provider calls")
    monkeypatch.setattr(socket.socket,"connect",deny)
    monkeypatch.setattr(socket,"getaddrinfo",deny)


def policy():return load_evidence_policy(Path(__file__).parents[1]/"configs/incident_evidence.yaml")


def membership(services, dependencies=None):
    events=[event(f"EVT-{n}", service_name=name,
                  downstream_service=(dependencies or ["core-db"]*len(services))[n])
            for n,name in enumerate(services)]
    value=incident(tuple(e["event_id"] for e in events))
    return d3.inspect_incident_membership(command(),IncidentReader(value,value),EventReader(events),policy())


def test_public_membership_dedup_order_and_commitment_provenance():
    result=membership(["service-z","service-a","service-z"])
    assert tuple(f.normalized_value for f in result.service_facts)==("service-a","service-z")
    assert len(result.event_references)==3 and all(len(h)==64 for _,h in result.event_references)
    assert len(result.incident_content_commitment)==64
    assert result.dependency_fact.normalized_value=="core-db"
    assert result.request_status.startswith("PENDING")  # Not a production OR SelectorFact.


def test_eight_services_allowed_as_inspection_only():
    assert len(membership([f"service-{n}" for n in range(8)]).service_facts)==8


@pytest.mark.parametrize("services,deps", [(["multiple"],None),(["a"],[None]),
    (["a","b"],["db-a","db-b"]),([f"service-{n}" for n in range(9)],None)])
def test_missing_conflicting_aggregate_or_over_limit_is_typed_failure(services,deps):
    with pytest.raises(EvidenceDomainError) as caught:membership(services,deps)
    assert caught.value.kind is EvidenceFailureKind.SOURCE_INVALID


def test_missing_authoritative_member_not_invented():
    value=incident(("EVT-MISSING",))
    with pytest.raises(EvidenceDomainError) as caught:
        d3.inspect_incident_membership(command(),IncidentReader(value,value),EventReader([]),policy())
    assert caught.value.kind is EvidenceFailureKind.REFERENCED_EVENT_NOT_FOUND


def test_incident_membership_change_preserves_existing_failure():
    first=incident(("EVT-1",));second=incident(("EVT-1","EVT-2"))
    with pytest.raises(EvidenceDomainError) as caught:
        d3.inspect_incident_membership(command(),IncidentReader(first,second),EventReader([event()]),policy())
    assert caught.value.kind is EvidenceFailureKind.INCIDENT_CHANGED_DURING_CAPTURE


def fact(field,value):return build_selector_fact(EvidenceSource.LOKI,field,value,
    selector_policy_version="selectors-v1",allowlist=SELECTOR_ALLOWLIST)


def trace_request(selectors=None):
    return log_request(query_semantic_identity=d3.TRACE_PROFILE_CANDIDATE,
        query_version=d3.TRACE_QUERY_VERSION_CANDIDATE,
        selectors=selectors if selectors is not None else (fact("service_name","api"),fact("trace_id","trace-1")))


def test_structured_trace_facts_do_not_certify_unobservable_parser_coverage():
    req=trace_request((fact("service_name",'api"\\name'),fact("trace_id",'trace"\\name')))
    assert d3.inspect_structured_trace(req,SELECTOR_ALLOWLIST)==(
        "PENDING PARSER ERROR COVERAGE AND QUERY PROVENANCE CONTRACT",)
    assert req.selectors[0].escaped_value != req.selectors[0].normalized_value
    assert req.timeout_seconds==2 and req.max_records==10


@pytest.mark.parametrize("selectors", [(fact("service_name","api"),),
    (fact("trace_id","t"),), (fact("service_name","multiple"),fact("trace_id","t"))])
def test_structured_trace_invalid_or_missing_concrete_facts_rejected(selectors):
    with pytest.raises(EvidenceDomainError):d3.inspect_structured_trace(trace_request(selectors),SELECTOR_ALLOWLIST)


def test_empty_trace_and_duplicate_trace_rejected_by_existing_canonical_contract():
    with pytest.raises((ValueError,EvidenceDomainError)):fact("trace_id","")
    with pytest.raises(ValueError):trace_request((fact("trace_id","a"),fact("trace_id","b")))


def test_legacy_loki_stays_exact_and_bounded():
    req=log_request(selectors=(fact("service_name","api"),fact("trace_id","t")))
    assert adapter({})._query(req)=='{service_name="api",trace_id="t"}'
    assert req.query_provenance.selectors==req.selectors


def test_legacy_empty_cannot_certify_json_parser_error_coverage():
    response={"status":"success","data":{"resultType":"streams","result":[]}}
    result=adapter(response).collect(log_request())
    assert result.status is SourceStatus.EMPTY
    # A filtered response lacks facts about records removed before normalization.
    assert not hasattr(result.summary.query_provenance,"parser_error_coverage")


def test_legacy_unstructured_line_is_not_a_json_validation_contract():
    response=fixture("loki_success.json")
    response["data"]["result"][0]["values"][0][1]='{"unfinished":'
    # Legacy plain-text logs remain valid; changing this globally would break v1.
    assert adapter(response).collect(log_request()).status is SourceStatus.AVAILABLE


def test_v1_parser_error_label_has_no_structured_coverage_resolution():
    response=fixture("loki_success.json")
    response["data"]["result"][0]["stream"]["__error__"]="JSONParserErr"
    result=adapter(response).collect(log_request())
    assert result.status is SourceStatus.AVAILABLE
    # This characterization blocks reuse of legacy FULL as a JSON parse certificate.
    assert not hasattr(result.summary,"parser_error_count")


def test_v1_multiple_same_field_selectors_cannot_mean_or():
    with pytest.raises(ValueError,match="unique"):
        log_request(selectors=(fact("service_name","a"),fact("service_name","b")))


def test_global_preview_preserves_original_selectors_and_context_only_status():
    req=metric_request(query_resource="api_p95_latency_ms",query_resolution_seconds=15.0)
    view=d3.inspect_global_latency(req)
    assert view["scope_candidate"]=="GLOBAL" and view["unit"]=="ms"
    assert view["original_selectors"]==req.selectors
    assert view["original_query_provenance"]==req.query_provenance
    assert view["strong_evidence_authority"] is False
    assert view["status"].startswith("PENDING") and view["source_status"]=="NOT COLLECTED"


@pytest.mark.parametrize("field,value", [("query_resource","up"),("query_resolution_seconds",30.0)])
def test_global_invalid_resource_or_step_not_relabelled(field,value):
    req=metric_request(query_resource="api_p95_latency_ms",query_resolution_seconds=15.0)
    with pytest.raises(EvidenceDomainError):d3.inspect_global_latency(replace(req,**{field:value}))
