"""SPEC-013 demo-v2 offline contracts; synthetic publications, mock transports."""
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import re
import socket
import sqlite3

import pytest

from incident_evidence import (CaptureCommand, EvidenceCaptureService, EvidenceSource,
    SourceStatus, CaptureTerminalKind, SqliteEvidenceStore, load_evidence_policy)
from incident_evidence.demo_v2 import *
from incident_evidence.security import build_selector_fact
from test_incident_evidence_loki_adapter import request as log_request, SELECTOR_ALLOWLIST
from test_incident_evidence_prometheus_adapter import request as metric_request
from test_incident_evidence_trusted_core import event, incident, IncidentReader, EventReader, command

LOKI="http://offline-loki.test/loki/api/v1/query_range"
PROM="http://offline-prom.test/api/v1/query_range"
REV="a"*40


@pytest.fixture(autouse=True)
def isolation(monkeypatch,tmp_path):
    def deny(*args,**kwargs):raise AssertionError("no actual provider/source requests")
    monkeypatch.setattr(socket.socket,"connect",deny)
    monkeypatch.setattr(socket,"getaddrinfo",deny)
    original=sqlite3.connect
    def connect(path,*a,**kw):
        assert Path(path).resolve().is_relative_to(tmp_path)
        return original(path,*a,**kw)
    monkeypatch.setattr(sqlite3,"connect",connect)


def policy_binding(coverage=True, **changes):
    now=datetime.now(timezone.utc)
    evidence={"reference":"SYNTHETIC-COVERAGE-NOT-DEPLOYMENT-PROOF","contract_sha256":"b"*64,
        "deployment_config_sha256":"c"*64,"backend_limit":10000,
        "exhaustive_until_limit":True,"all_streams_included":True,"no_silent_caps":True,
        "warnings_observable":True,"indexed_labels":["service_name","downstream_service","source_ip"]}
    evidence.update(changes)
    record={"version":VERSION,"decision":"APPROVE","issuer_role":"SPEC_LEAD",
        "reference":"SYNTHETIC-POLICY-NOT-A-CAPTURE-GRANT","scope":"COMPETITION_DEMO_ONLY",
        "source_revision":REV,"spec_patch_sha256":SPEC_PATCH_SHA,
        "valid_from":(now-timedelta(hours=1)).isoformat(),"expires_at":(now+timedelta(hours=1)).isoformat(),
        "loki_endpoint_sha256":hashlib.sha256(LOKI.encode()).hexdigest(),
        "prometheus_endpoint_sha256":hashlib.sha256(PROM.encode()).hexdigest(),
        "coverage_evidence":evidence if coverage else None}
    raw=json.dumps(record).encode()
    return DemoPolicyBinding.from_published_record(raw,expected_sha256=hashlib.sha256(raw).hexdigest(),
        expected_source_revision=REV,now=now)


def fact(name,value,source=EvidenceSource.LOKI):
    return build_selector_fact(source,name,value,selector_policy_version="selectors-v1",allowlist=SELECTOR_ALLOWLIST)


def service_or(names=("svc-a",), *, revision=REV):
    provenance=MembershipProvenanceV1("INC-1","d"*64,(("EVT-1","e"*64),),"source-v1",revision)
    return BoundedServiceOrV1(tuple(fact("service_name",s) for s in names),fact("downstream_service","core-db"),provenance)


def trace_request(*, binding=None,n=3,names=("svc-a",)):
    trace=fact("trace_id","t-1")
    base=log_request(max_records=n,max_content_bytes=1024,selectors=(fact("service_name","svc-a"),
        fact("downstream_service","core-db"),trace),query_semantic_identity=TRACE_PROFILE,query_version=TRACE_QUERY,
        adapter_contract_version="loki-demo-adapter-v2")
    return DemoEvidenceRequestV2(base,binding or policy_binding(),service_or(names),StructuredTraceV2(trace))


def raw_payload(req, lines):
    stamp=str(int(req.base_request.logical_window.start.timestamp())*10**9+10**9)
    return {"status":"success","data":{"resultType":"streams","result":[
        {"stream":{"service_name":"svc-a","downstream_service":"core-db"},
         "values":[[stamp,line] for line in lines]}]}}


def collect_lines(lines,req=None,**flags):
    req=req or trace_request();payload=raw_payload(req,lines);payload.update(flags);calls=[]
    def http(*a,**kw):calls.append(kw);return payload
    result=LokiDemoV2Adapter(LOKI,selector_allowlist=SELECTOR_ALLOWLIST,http_client=http).collect(req)
    return result,calls


@pytest.mark.parametrize("nrows,status", [(0,SourceStatus.EMPTY),(3,SourceStatus.AVAILABLE),(4,SourceStatus.INVALID)])
def test_raw_n_and_n_plus_one_before_dedup(nrows,status):
    result,calls=collect_lines(['{"trace_id":"t-1"}']*nrows)
    assert len(calls)==1 and calls[0]["params"]["limit"]=="4"
    assert "trace_id" not in calls[0]["params"]["query"]
    assert result.base_result.status is status
    raw=result.collection.raw_fetch_coverage
    assert raw.observed_raw_lower_bound==nrows
    if nrows==4:
        assert raw.overflow_sentinel_seen and raw.raw_response_count is None
        assert result.collection.parsing_coverage is None
    elif nrows:
        assert raw.raw_response_count==3 and result.base_result.summary.record_count==1
        assert result.collection.parsing_coverage.filtered_result_count==1


@pytest.mark.parametrize("binding", [lambda:policy_binding(False),lambda:policy_binding(backend_limit=3),
    lambda:policy_binding(no_silent_caps=False),lambda:policy_binding(indexed_labels=[])])
def test_below_sentinel_is_not_complete_without_actual_pinned_basis(binding):
    result,_=collect_lines([],trace_request(binding=binding()))
    assert result.base_result.status is SourceStatus.INVALID
    assert result.collection.raw_fetch_coverage.fetch_completeness=="UNKNOWN"
    assert result.collection.parsing_coverage is None
    assert result.base_result.summary.bounds.completeness_impact.value=="DEGRADED"


@pytest.mark.parametrize("flags", [{"warnings":["cap"]},{"truncated":True},{"continuation":"token"},
    {"partial":None},{"nextPageToken":"more"}])
def test_backend_uncertainty_is_invalid_not_empty(flags):
    result,_=collect_lines([],**flags)
    assert result.base_result.status is SourceStatus.INVALID


@pytest.mark.parametrize("line", ['{','[]','{"trace_id":17}','{"trace_id":null}','{"trace_id":""}'])
def test_parser_invalid_lines_cannot_be_discarded(line):
    result,_=collect_lines([line])
    assert result.base_result.status is SourceStatus.INVALID
    assert result.collection.parsing_coverage.invalid_count==1


def test_missing_and_other_trace_are_counted_legitimate_empty():
    result,_=collect_lines(['{"message":"ok"}','{"trace_id":"other"}'])
    assert result.base_result.status is SourceStatus.EMPTY
    parsed=result.collection.parsing_coverage
    assert (parsed.scanned_count,parsed.missing_trace_count,parsed.nonmatching_trace_count)==(2,1,1)
    assert parsed.invalid_count==0
    assert result.query.original_selectors and result.query.scope=="INCIDENT_BOUNDED_LOG"


def test_parser_error_label_is_invalid():
    req=trace_request();payload=raw_payload(req,['{"trace_id":"t-1"}'])
    payload['data']['result'][0]['stream']['__error__']='JSONParserErr'
    result=LokiDemoV2Adapter(LOKI,selector_allowlist=SELECTOR_ALLOWLIST,http_client=lambda *a,**kw:payload).collect(req)
    assert result.base_result.status is SourceStatus.INVALID


def test_source_unavailable_stays_unavailable():
    def fail(*a,**kw):raise OSError("offline")
    result=LokiDemoV2Adapter(LOKI,selector_allowlist=SELECTOR_ALLOWLIST,http_client=fail).collect(trace_request())
    assert result.base_result.status is SourceStatus.UNAVAILABLE


@pytest.mark.parametrize("names", [("a",),tuple(f"svc-{i}" for i in range(8)),("b","a","b")])
def test_or_is_bounded_sorted_deduplicated(names):
    bounded=service_or(names);values=tuple(f.normalized_value for f in bounded.service_facts)
    assert values==tuple(sorted(set(names)))
    assert bounded.membership_provenance.event_projection_version==EVENT_PROJECTION
    assert len(bounded.membership_provenance.provenance_commitment)==64


def test_nine_and_aggregate_rejected():
    with pytest.raises(ValueError):service_or(tuple(f"svc-{i}" for i in range(9)))
    with pytest.raises(ValueError):service_or(("multiple",))


def test_regex_is_literal_and_logql_encoded():
    bounded=service_or(('api|.*','api"\\name'))
    part=bounded.query_parts()[0];pattern=json.loads(part.split('=~',1)[1])
    assert re.fullmatch(pattern,'api|.*')
    assert not re.fullmatch(pattern,'unrelated')
    with pytest.raises(TypeError):BoundedServiceOrV1(regex=".*")
    with pytest.raises(ValueError):service_or(("bad\nname",))


def test_unknown_profile_and_policy_forgery_rejected():
    req=trace_request()
    with pytest.raises(ValueError):replace(req,base_request=replace(req.base_request,query_version="unknown"))
    with pytest.raises(ValueError):replace(req,trace_filter=None)
    with pytest.raises(ValueError):replace(req.policy,record_json='{}').require_current()


def global_request():
    base=metric_request(query_resource="api_p95_latency_ms",query_resolution_seconds=15.0,
        query_semantic_identity=GLOBAL_PROFILE,query_version="promql-global-context-v1",
        adapter_contract_version="prometheus-demo-adapter-v2")
    return DemoEvidenceRequestV2(base,policy_binding(),scope="GLOBAL")


def global_payload(req,series=1):
    stamp=req.base_request.logical_window.start.timestamp()+1
    return {"status":"success","data":{"resultType":"matrix","result":[
        {"metric":{"__name__":"api_p95_latency_ms","job":"demo"},"values":[[stamp,"4500"]]}
        for _ in range(series)]}}


def test_global_original_and_applied_predicates_context_only():
    req=global_request();payload=global_payload(req);calls=[]
    def http(*a,**kw):calls.append(kw);return payload
    result=PrometheusDemoV2Adapter(PROM,selector_allowlist=SELECTOR_ALLOWLIST,http_client=http).collect(req)
    assert len(calls)==1 and calls[0]['params']['query']=='api_p95_latency_ms'
    context=result.global_context
    assert context.scope=='GLOBAL' and context.unit=='ms' and context.step_seconds==15
    assert context.original_selectors==req.original_selectors and context.applied_selectors==()
    assert context.strength=='CONTEXTUAL_ONLY' and context.strong_evidence_eligible is False
    with pytest.raises(ValueError):context.require_consumer(scopes=frozenset(),strengths=frozenset())
    with pytest.raises(ValueError):replace(context,strong_evidence_eligible=True)


@pytest.mark.parametrize("series,status", [(0,SourceStatus.EMPTY),(1,SourceStatus.AVAILABLE),(2,SourceStatus.INVALID)])
def test_global_series_ambiguity(series,status):
    req=global_request();payload=global_payload(req,series)
    result=PrometheusDemoV2Adapter(PROM,selector_allowlist=SELECTOR_ALLOWLIST,http_client=lambda *a,**kw:payload).collect(req)
    assert result.base_result.status is status


def demo_service(tmp_path, *, coverage=True, admission=None, store=None, trace_identity='t-1', transform_metrics=None,
                 transform_logs=None):
    policy=load_evidence_policy('configs/incident_evidence.yaml');binding=policy_binding(coverage)
    ev=event(service_name='svc-a',downstream_service='core-db',trace_id=trace_identity,source_ip=None)
    inc=incident()
    # Fingerprint and source must remain consistent; use the public fixture's original source identity.
    ev['source_ip']='192.0.2.10'
    incidents=type('PublicIncidentReader',(),{'get_incident':lambda self,_id:inc})()
    events=EventReader([ev]);calls=[]
    def logs(*a,**kw):
        calls.append('LOKI');stamp=str(int(kw['params']['start'])+10**9)
        payload={"status":"success","data":{"resultType":"streams","result":[
            {"stream":{"service_name":"svc-a","downstream_service":"core-db","source_ip":"192.0.2.10"},
             "values":[[stamp,'{"trace_id":"t-1","message":"observed connection"}']]}]}}
        return transform_logs(payload) if transform_logs else payload
    def metrics(*a,**kw):
        calls.append('PROMETHEUS')
        payload={"status":"success","data":{"resultType":"matrix","result":[
            {"metric":{"__name__":"api_p95_latency_ms","job":"demo"},
             "values":[[float(kw['params']['start'])+1,"4500"]]}]}}
        return transform_metrics(payload) if transform_metrics else payload
    store=store or SqliteEvidenceStore(tmp_path/'evidence.sqlite3')
    service=EvidenceCaptureService(store=store,incident_reader=incidents,event_reader=events,policy=policy,
        demo_v2=binding,admission_policy=admission,adapters={EvidenceSource.LOKI:LokiDemoV2Adapter(LOKI,selector_allowlist=policy.selector_allowlist,http_client=logs),
            EvidenceSource.PROMETHEUS:PrometheusDemoV2Adapter(PROM,selector_allowlist=policy.selector_allowlist,http_client=metrics)})
    cmd=replace(command(),capture_operation_id='CAP-DEMO-OFFLINE',config_identity=service._effective_config_identity,
        canonicalization_version=CANONICAL_VERSION,source_policy_version=SOURCE_VERSION)
    return service,store,cmd,calls


def test_capture_publication_replay_aware_read_and_recovery(tmp_path):
    service,store,cmd,calls=demo_service(tmp_path)
    try:
        result=service.capture_evidence(cmd)
        assert result.terminal_kind is CaptureTerminalKind.SUCCESS, result
        before=list(calls);assert service.capture_evidence(cmd)==result and calls==before
        snapshot=service.resolve_snapshot(result.snapshot_id);revision=service.resolve_revision(result.revision_id)
        assert snapshot.snapshot_content['demo_v2']['version']==VERSION
        assert revision.semantic_content['normalized_evidence']['PROMETHEUS'][0]['scope']=='GLOBAL'
        with pytest.raises(EvidenceDomainError):store.resolve_snapshot(result.snapshot_id)
        with pytest.raises(EvidenceDomainError):store.resolve_revision(result.revision_id)
        assert store.enumerate_recovery_facts().snapshots
        path=store.database_path if hasattr(store,'database_path') else tmp_path/'evidence.sqlite3'
    finally:store.close()
    reopened=SqliteEvidenceStore(path)
    try:assert reopened.resolve_snapshot(result.snapshot_id,accepted_demo_version=VERSION)==snapshot
    finally:reopened.close()


def test_unknown_fetch_never_commits_degraded_snapshot(tmp_path):
    service,store,cmd,calls=demo_service(tmp_path,coverage=False)
    try:
        result=service.capture_evidence(cmd)
        assert result.terminal_kind is CaptureTerminalKind.FAILURE
        assert store.enumerate_recovery_facts().snapshots==()
    finally:store.close()


@pytest.mark.parametrize("field,value", [("effective_limit",1),("backend_limit",1),("limit",None)])
def test_response_cap_facts_do_not_override_pinned_contract(field,value):
    result,_=collect_lines([],**{field:value})
    assert result.base_result.status is SourceStatus.INVALID
    assert result.collection.raw_fetch_coverage.fetch_completeness=="UNKNOWN"


def test_unknown_coverage_cannot_use_degraded_invalid_exception(tmp_path):
    from incident_evidence import SourceAdmissionPolicy
    admission=replace(SourceAdmissionPolicy(),degraded_invalid_sources=frozenset({EvidenceSource.LOKI}))
    service,store,cmd,_=demo_service(tmp_path,coverage=False,admission=admission)
    try:
        assert service.capture_evidence(cmd).terminal_kind is CaptureTerminalKind.FAILURE
        assert not store.enumerate_recovery_facts().snapshots
    finally:store.close()


def test_missing_trace_and_bad_profile_cannot_be_bypassed():
    req=trace_request()
    with pytest.raises(ValueError):replace(req,trace_filter=None)
    with pytest.raises(ValueError):replace(req,service_or=None)
    with pytest.raises(ValueError):replace(req,base_request=replace(req.base_request,query_semantic_identity="demo-v3"))


@pytest.mark.parametrize("field,value", [("strong_evidence_eligible",True),("scope","INCIDENT"),("unit","seconds"),
    ("aggregation_facts","ATTRIBUTED_TO_CORE_DB"),("query_provenance",{}),("completeness","DEGRADED")])
def test_canonical_reader_rejects_global_semantic_tampering(tmp_path,field,value):
    from incident_evidence import EvidenceSnapshot
    service,store,cmd,_=demo_service(tmp_path)
    try:
        outcome=service.capture_evidence(cmd);snapshot=service.resolve_snapshot(outcome.snapshot_id)
        content=snapshot.snapshot_content
        content['demo_v2']['sources']['PROMETHEUS']['global_context'][field]=value
        with pytest.raises(ValueError):
            EvidenceSnapshot.from_content(cmd,revision_id=outcome.revision_id,completeness=snapshot.completeness,
                source_statuses=snapshot.source_statuses,snapshot_content=content)
    finally:store.close()


def test_projection_commitment_and_unknown_schema_tampering_rejected(tmp_path):
    from incident_evidence import EvidenceSnapshot
    service,store,cmd,_=demo_service(tmp_path)
    try:
        outcome=service.capture_evidence(cmd);snapshot=service.resolve_snapshot(outcome.snapshot_id)
        for mutation in ('membership','version','query','unknown-predicate'):
            content=snapshot.snapshot_content
            if mutation=='membership':
                content['demo_v2']['sources']['LOKI']['query']['membership_provenance']['event_projection_version']='unknown'
            elif mutation=='version':content['demo_v2']['version']='unknown'
            elif mutation=='query':content['demo_v2']['sources']['LOKI']['query']['server_query']='{}'
            else:content['demo_v2']['sources']['LOKI']['query']['applied_predicate']['unknown']='field'
            with pytest.raises(ValueError):EvidenceSnapshot.from_content(cmd,revision_id=outcome.revision_id,
                completeness=snapshot.completeness,source_statuses=snapshot.source_statuses,snapshot_content=content)
    finally:store.close()


def test_v2_response_loss_keeps_atomic_replay_and_no_extra_fetch(tmp_path):
    class Fault:
        fired=False
        def __call__(self,point):
            if point=='after_commit' and not self.fired:
                self.fired=True;raise RuntimeError('synthetic response loss')
    store=SqliteEvidenceStore(tmp_path/'evidence.sqlite3',fault_injector=Fault())
    service,store,cmd,calls=demo_service(tmp_path,store=store)
    try:
        with pytest.raises(RuntimeError):service.capture_evidence(cmd)
        before=list(calls);outcome=service.capture_evidence(cmd)
        assert outcome.terminal_kind is CaptureTerminalKind.SUCCESS and calls==before
        assert len(store.enumerate_recovery_facts().snapshots)==1
    finally:store.close()


def test_same_semantic_revision_across_v2_capture_operations(tmp_path):
    service,store,cmd,_=demo_service(tmp_path)
    try:
        a=service.capture_evidence(cmd);b=service.capture_evidence(replace(cmd,capture_operation_id='CAP-DEMO-SECOND'))
        assert a.snapshot_id!=b.snapshot_id and a.revision_id==b.revision_id
    finally:store.close()


def test_public_membership_missing_conflict_and_over_limit_are_fail_closed():
    from incident_evidence.trusted_core import admit_capture_plan
    binding=policy_binding();policy=load_evidence_policy('configs/incident_evidence.yaml')
    for names,deps in [(('multiple',),('core-db',)),(('a','b'),('db1','db2')),
                       (tuple(f's{i}' for i in range(9)),('core-db',)*9)]:
        events=[event(f'EVT-{i}',service_name=n,downstream_service=deps[i]) for i,n in enumerate(names)]
        inc=incident(tuple(e['event_id'] for e in events))
        plan=admit_capture_plan(command(),IncidentReader(inc,inc),EventReader(events),policy)
        with pytest.raises(EvidenceDomainError):membership_from_plan(plan,binding)


def test_global_rejects_service_label_and_preserves_unavailability():
    req=global_request();payload=global_payload(req)
    payload['data']['result'][0]['metric']['service_name']='core-db'
    adapter=PrometheusDemoV2Adapter(PROM,selector_allowlist=SELECTOR_ALLOWLIST,http_client=lambda *a,**kw:payload)
    assert adapter.collect(req).base_result.status is SourceStatus.INVALID
    def unavailable(*a,**kw):raise OSError('offline')
    adapter=PrometheusDemoV2Adapter(PROM,selector_allowlist=SELECTOR_ALLOWLIST,http_client=unavailable)
    assert adapter.collect(req).base_result.status is SourceStatus.UNAVAILABLE


@pytest.mark.parametrize("data", [None, {"result":[None]}, {"result":[{"metric":None}]}])
def test_global_malformed_envelope_is_typed_invalid(data):
    result=PrometheusDemoV2Adapter(PROM,selector_allowlist=SELECTOR_ALLOWLIST,
        http_client=lambda *a,**k:{"status":"success","data":data}).collect(global_request())
    assert result.base_result.status is SourceStatus.INVALID


def test_trace_and_dependency_literal_escaping_preserves_exact_comparison():
    req=trace_request()
    trace=fact("trace_id",'trace"\\quoted')
    dep=fact("downstream_service",'db"\\quoted')
    node=replace(req.service_or,dependency_fact=dep)
    req=replace(req,base_request=replace(req.base_request,selectors=(
        fact("service_name","svc-a"),dep,trace)),service_or=node,trace_filter=StructuredTraceV2(trace))
    result,calls=collect_lines([json.dumps({"trace_id":trace.normalized_value})],req)
    assert result.base_result.status is SourceStatus.AVAILABLE
    assert 'downstream_service='+json.dumps(dep.normalized_value) in calls[0]["params"]["query"]
    assert trace.normalized_value not in calls[0]["params"]["query"]
    assert result.collection.parsing_coverage.filtered_candidate_count==1


def test_duplicate_candidates_have_separate_candidate_and_result_counts():
    result,_=collect_lines(['{"trace_id":"t-1"}']*3)
    parsed=result.collection.parsing_coverage
    assert parsed.filtered_candidate_count==3 and parsed.filtered_result_count==1
    with pytest.raises(ValueError):replace(parsed,filtered_candidate_count=2)


def test_membership_projection_changes_when_public_service_changes():
    from incident_evidence.trusted_core import admit_capture_plan
    binding=policy_binding();policy=load_evidence_policy('configs/incident_evidence.yaml')
    nodes=[]
    for name in ('service-a','service-b'):
        e=event('EVT-1',service_name=name,downstream_service='core-db')
        inc=incident(('EVT-1',))
        plan=admit_capture_plan(command(),IncidentReader(inc,inc),EventReader([e]),policy)
        nodes.append(membership_from_plan(plan,binding))
    assert nodes[0].membership_provenance.event_references!=nodes[1].membership_provenance.event_references
    assert nodes[0].membership_provenance.provenance_commitment!=nodes[1].membership_provenance.provenance_commitment


def test_f01_builder_dto_and_request_boundaries_reject_aggregate_trace():
    with pytest.raises(ValueError):
        build_demo_selector_fact(EvidenceSource.LOKI,'trace_id','multiple',
            selector_policy_version='selectors-v1',allowlist=SELECTOR_ALLOWLIST)
    aggregate=fact('trace_id','multiple')  # Legacy canonical builder deliberately unchanged.
    with pytest.raises(ValueError):StructuredTraceV2(aggregate)
    req=trace_request()
    damaged=StructuredTraceV2(req.trace_filter.trace_fact)
    object.__setattr__(damaged,'trace_fact',aggregate)
    with pytest.raises(ValueError):
        replace(req,base_request=replace(req.base_request,selectors=(aggregate,)),trace_filter=damaged)
    object.__setattr__(req,'trace_filter',damaged)
    calls=[]
    adapter=LokiDemoV2Adapter(LOKI,selector_allowlist=SELECTOR_ALLOWLIST,
        http_client=lambda *a,**kw:calls.append(kw))
    with pytest.raises(ValueError):adapter.collect(req)
    assert calls==[]  # Not a legal EMPTY and no source request.


def test_f01_production_composition_stops_before_fetch_or_snapshot(tmp_path):
    service,store,cmd,calls=demo_service(tmp_path,trace_identity='multiple')
    try:
        outcome=service.capture_evidence(cmd)
        assert outcome.terminal_kind is CaptureTerminalKind.FAILURE
        assert calls==[] and store.enumerate_recovery_facts().snapshots==()
        assert service.capture_evidence(cmd)==outcome and calls==[]
    finally:store.close()


def test_f01_aggregate_response_trace_is_invalid_not_empty():
    result,_=collect_lines(['{"trace_id":"multiple"}'])
    assert result.base_result.status is SourceStatus.INVALID and result.base_result.records==()
    assert result.collection.parsing_coverage.invalid_count==1
    assert build_demo_selector_fact(EvidenceSource.LOKI,'trace_id','real-trace',
        selector_policy_version='selectors-v1',allowlist=SELECTOR_ALLOWLIST).normalized_value=='real-trace'


@pytest.mark.parametrize('empty_logs',[False,True])
def test_f01_persisted_request_trace_rejected_even_with_no_admitted_rows(tmp_path,empty_logs):
    from incident_evidence import EvidenceSnapshot, EvidenceRevision
    def empty(payload):
        payload['data']['result']=[];return payload
    service,store,cmd,_=demo_service(tmp_path,transform_logs=empty if empty_logs else None)
    try:
        outcome=service.capture_evidence(cmd);snapshot=service.resolve_snapshot(outcome.snapshot_id)
        content=snapshot.snapshot_content
        aggregate=json.loads(canonical_json(fact('trace_id','multiple')))
        for selectors in (content['provenance']['LOKI']['query']['selectors'],
                content['demo_v2']['sources']['LOKI']['query']['original_selectors']):
            selectors[:]=[aggregate if f['field_name']=='trace_id' else f for f in selectors]
        with pytest.raises(ValueError,match='aggregate'):
            EvidenceSnapshot.from_content(cmd,revision_id=outcome.revision_id,completeness=snapshot.completeness,
                source_statuses=snapshot.source_statuses,snapshot_content=content)
        if not empty_logs:
            semantic=service.resolve_revision(outcome.revision_id).semantic_content
            semantic['normalized_evidence']['LOKI'][0]['message']='{"trace_id":"multiple"}'
            with pytest.raises(ValueError,match='aggregate'):
                EvidenceRevision.from_content(incident_id=cmd.incident_id,canonicalization_version=CANONICAL_VERSION,
                    semantic_content=semantic)
    finally:store.close()


@pytest.mark.parametrize('name', ['api_p95_latency_ms','api_qps',None])
@pytest.mark.parametrize('samples', [True,False])
def test_f02_actual_metric_identity_required_for_sampled_and_empty_series(name,samples):
    req=global_request();payload=global_payload(req)
    if name is None:payload['data']['result'][0]['metric'].pop('__name__')
    else:payload['data']['result'][0]['metric']['__name__']=name
    if not samples:payload['data']['result'][0]['values']=[]
    result=PrometheusDemoV2Adapter(PROM,selector_allowlist=SELECTOR_ALLOWLIST,
        http_client=lambda *a,**kw:payload).collect(req)
    if name=='api_p95_latency_ms':
        assert result.base_result.status is (SourceStatus.AVAILABLE if samples else SourceStatus.EMPTY)
        assert dict(result.global_context.original_labels)['__name__']==name
        assert result.global_context.strength=='CONTEXTUAL_ONLY'
        assert result.global_context.strong_evidence_eligible is False
    else:
        assert result.base_result.status is SourceStatus.INVALID and result.base_result.records==()


def test_f02_wrong_metric_never_commits_successful_snapshot(tmp_path):
    def wrong(payload):
        payload['data']['result'][0]['metric']['__name__']='api_qps';return payload
    service,store,cmd,_=demo_service(tmp_path,transform_metrics=wrong)
    try:
        assert service.capture_evidence(cmd).terminal_kind is CaptureTerminalKind.FAILURE
        assert store.enumerate_recovery_facts().snapshots==()
    finally:store.close()


def test_f02_context_dto_cannot_lose_or_relabel_original_metric():
    req=global_request()
    result=PrometheusDemoV2Adapter(PROM,selector_allowlist=SELECTOR_ALLOWLIST,
        http_client=lambda *a,**kw:global_payload(req)).collect(req)
    for labels in ((),(('__name__','api_qps'),),(('job','demo'),)):
        with pytest.raises(ValueError):replace(result.global_context,original_labels=labels)
    with pytest.raises(ValueError):replace(result.global_context,resource='api_qps')


@pytest.mark.parametrize('name', ['api_qps',None])
def test_f02_canonical_context_and_revision_reject_metric_conflict(tmp_path,name):
    from incident_evidence import EvidenceSnapshot, EvidenceRevision
    from incident_evidence.identity import semantic_identity
    service,store,cmd,_=demo_service(tmp_path)
    try:
        outcome=service.capture_evidence(cmd);snapshot=service.resolve_snapshot(outcome.snapshot_id)
        content=snapshot.snapshot_content
        labels=content['demo_v2']['sources']['PROMETHEUS']['global_context']['original_labels']
        labels[:]=[p for p in labels if p[0]!='__name__']
        if name is not None:labels.append(['__name__',name])
        with pytest.raises(ValueError):
            EvidenceSnapshot.from_content(cmd,revision_id=outcome.revision_id,completeness=snapshot.completeness,
                source_statuses=snapshot.source_statuses,snapshot_content=content)
        semantic=service.resolve_revision(outcome.revision_id).semantic_content
        row=semantic['normalized_evidence']['PROMETHEUS'][0]
        row['labels'].pop('__name__')
        if name is not None:row['labels']['__name__']=name
        row['record_semantic_identity']=semantic_identity('spec013-global-metric-record-v1',
            {k:v for k,v in row.items() if k!='record_semantic_identity'})
        with pytest.raises(ValueError,match='metric identity'):
            EvidenceRevision.from_content(incident_id=cmd.incident_id,canonicalization_version=CANONICAL_VERSION,
                semantic_content=semantic)
    finally:store.close()


@pytest.mark.parametrize('series', [0,1])
def test_f03_empty_series_labels_survive_public_read_replay_and_reopen(tmp_path,series):
    def empty(payload):
        payload['data']['result'][0]['values']=[]
        if series==0:payload['data']['result']=[]
        return payload
    service,store,cmd,calls=demo_service(tmp_path,transform_metrics=empty)
    try:
        outcome=service.capture_evidence(cmd)
        assert outcome.terminal_kind is CaptureTerminalKind.SUCCESS  # Existing admission, not new permission.
        snapshot=service.resolve_snapshot(outcome.snapshot_id)
        context=snapshot.snapshot_content['demo_v2']['sources']['PROMETHEUS']['global_context']
        assert context['samples']==[] and context['strong_evidence_eligible'] is False
        assert context['original_labels']==([['__name__','api_p95_latency_ms'],['job','demo']] if series else [])
        assert dict(snapshot.source_statuses)[EvidenceSource.PROMETHEUS] is SourceStatus.EMPTY
        prior=list(calls);assert service.capture_evidence(cmd)==outcome and calls==prior
    finally:store.close()
    reopened=SqliteEvidenceStore(tmp_path/'evidence.sqlite3')
    try:
        assert reopened.resolve_snapshot(outcome.snapshot_id,accepted_demo_version=VERSION)==snapshot
    finally:reopened.close()


@pytest.mark.parametrize('finding',['f01','f02','f03'])
def test_findings_public_read_and_replay_reject_corrupted_persisted_identity(tmp_path,finding):
    from incident_evidence import EvidenceStoreIntegrityError
    def empty(payload):
        payload['data']['result'][0]['values']=[];return payload
    service,store,cmd,calls=demo_service(tmp_path,transform_metrics=empty if finding=='f03' else None)
    try:
        outcome=service.capture_evidence(cmd);content=service.resolve_snapshot(outcome.snapshot_id).snapshot_content
        if finding=='f01':
            for selectors in (content['provenance']['LOKI']['query']['selectors'],
                    content['demo_v2']['sources']['LOKI']['query']['original_selectors']):
                selectors[:]=[json.loads(canonical_json(fact('trace_id','multiple'))) if f['field_name']=='trace_id'
                    else f for f in selectors]
        else:
            labels=content['demo_v2']['sources']['PROMETHEUS']['global_context']['original_labels']
            labels[:]=[['__name__','api_qps'],['job','demo']]
        # Deliberately corrupt only the isolated SQLite fixture, never formal Evidence.
        with sqlite3.connect(tmp_path/'evidence.sqlite3') as connection:
            connection.execute('UPDATE evidence_snapshots SET canonical_snapshot_content=? WHERE snapshot_id=?',
                (canonical_json(content),outcome.snapshot_id))
        prior=list(calls)
        with pytest.raises(EvidenceStoreIntegrityError):service.resolve_snapshot(outcome.snapshot_id)
        with pytest.raises(EvidenceStoreIntegrityError):service.capture_evidence(cmd)
        assert calls==prior  # No new source request on corrupted replay.
    finally:store.close()
