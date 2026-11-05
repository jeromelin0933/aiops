"""Approved repository stop-hook regression; isolated stores, network denied."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import json
import socket
import re
import pytest
import yaml
from alert_correlation.state import RetryDisposition
from incident_management import IncidentDomainError, IncidentErrorCode
from runtime_orchestration.worker import CaptureStopBinding, CorrelationRuntimeCore
from runtime_orchestration.pending import PendingEventOutcome, PendingOutcomeKind
from runtime_orchestration.config import load_runtime_config
from runtime_orchestration.contracts import RuntimeWorkKind
from runtime_orchestration import RuntimeLifecycleState
from test_runtime_host_e2e import _HostRuntime, _event, NOW

@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def deny(*a, **kw): raise AssertionError("network/provider forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)

# Synthetic evidence for isolated tests only. This is not an approval record.
def binding(**kw):
    fields=dict(run_id="competition-capture-20261104-01-s5", source_revision="a"*40,
                config_sha256="b"*64, execution_binding_sha256="c"*64,
                approval_reference="SYNTHETIC-OFFLINE-TEST-ONLY")
    fields.update(kw)
    return CaptureStopBinding(**fields)

def enable(runtime):
    old=runtime.core
    runtime.core=CorrelationRuntimeCore(
        bootstrap=runtime.bootstrap, initial_dispatcher=runtime.pending,
        pending_scheduler=old._pending_scheduler, auto_assign=runtime.assignment,
        retry=runtime.retry, retry_executor=old._retry_executor,
        clock=runtime.clock, stop=old._stop, telemetry=runtime.telemetry,
        capture_stop_binding=binding())
    return runtime.core

@pytest.mark.parametrize('kw',[{'run_id':'unapproved-run'}, {'approval_reference':''},
                              {'config_sha256':'PENDING'}, {'source_revision':'fake'}])
def test_bad_binding_rejected(kw):
    with pytest.raises(ValueError): binding(**kw)

@pytest.mark.parametrize('kind',[PendingOutcomeKind.ENTERED_PENDING,
    PendingOutcomeKind.REMAINS_PENDING, PendingOutcomeKind.CLAIM_BUSY,
    PendingOutcomeKind.BLOCKED_PRESERVED])
def test_normal_wait_is_not_failure(tmp_path,kind):
    r=_HostRuntime(tmp_path/'normal',[])
    try:
        core=enable(r)
        core._capture_pending_outcome(PendingEventOutcome('e',kind))
        assert core.capture_failure is None
        assert not core._stop.requested
    finally: r.close()

def test_capture_intake_failure_skips_same_cycle_retry_and_pending(tmp_path,monkeypatch):
    r=_HostRuntime(tmp_path/'stop',[_event('e','brute_force_detected',NOW,source_ip='192.0.2.7')])
    core=enable(r); calls=[]
    class Failure(Exception): retry_disposition=RetryDisposition.RETRYABLE
    def fail(e): calls.append('initial-failed'); raise Failure()
    try:
        core.startup()
        monkeypatch.setattr(r.pending,'process_authoritative_event',fail)
        monkeypatch.setattr(r.retry,'enumerate_eligibility',lambda: SimpleNamespace(
            eligible=(SimpleNamespace(work_kind=RuntimeWorkKind.DOMAIN_OPERATION),),next_eligibility=None))
        monkeypatch.setattr(core._retry_executor,'execute_retry',lambda w:calls.append('retry'))
        monkeypatch.setattr(core._pending_scheduler,'poll',lambda **kw:calls.append('pending'))
        core.run_cycle()
        assert calls==['initial-failed']
        assert core.capture_failure.stage=='INITIAL'
        assert core._stop.requested
        assert core.run_cycle().acquired_work==0
        core._capture_fail('later','later')
        assert core.capture_failure.stage=='INITIAL'
    finally:r.close()

def test_durable_retry_survives_capture_stop_and_ordinary_restart(tmp_path,monkeypatch):
    root=tmp_path/'durable'; e=_event('durable','brute_force_detected',NOW,source_ip='192.0.2.8')
    r=_HostRuntime(root,[e]); core=enable(r)
    def fail(request):
        raise IncidentDomainError(IncidentErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE,'synthetic',
            operation_id=request.intent.operation_id,event_id=request.intent.event_id)
    try:
        core.startup()
        monkeypatch.setattr(r.incident_manager,'apply_correlation_mutation',fail)
        core.run_cycle()
        assert core.capture_failure is not None
        rec=r.work.enumerate_all().records[0]
        assert rec.attempt_count==1
        assert r.state.resolve('durable').intent.operation_id==rec.operation_id
        core.run_cycle()
        assert r.work.get(rec.work_id)==rec
    finally:r.close()
    restarted=_HostRuntime(root,[e])
    try:
        restarted.core.startup()
        assert restarted.work.get(rec.work_id)==rec
        assert restarted.bootstrap.state is RuntimeLifecycleState.READY
    finally:restarted.close()

def test_partial_receipt_preserved_without_second_mutation_on_restart(tmp_path,monkeypatch):
    root=tmp_path/'partial'; e=_event('partial','brute_force_detected',NOW,source_ip='192.0.2.9')
    r=_HostRuntime(root,[e]); core=enable(r)
    try:
        core.startup(); original=r.incident_manager.apply_correlation_mutation
        def lost(request): original(request); raise RuntimeError('lost after commit')
        monkeypatch.setattr(r.incident_manager,'apply_correlation_mutation',lost)
        with pytest.raises(RuntimeError):core.run_cycle()
        op=r.state.resolve('partial').intent.operation_id
        assert r.incidents.get_operation_result(op) is not None
        assert core._stop.requested
    finally:r.close()
    restarted=_HostRuntime(root,[e])
    try:
        def forbidden(*a,**kw):pytest.fail('duplicate mutation')
        monkeypatch.setattr(restarted.incident_manager,'apply_correlation_mutation',forbidden)
        restarted.core.startup()
        assert restarted.state.resolve('partial').processed is not None
    finally:restarted.close()

def test_pending_batch_stops_before_second_item(tmp_path,monkeypatch):
    r=_HostRuntime(tmp_path/'pending',[]);core=enable(r);calls=[]
    try:
        core.startup()
        monkeypatch.setattr(r.pending,'_pending_event_ids',lambda:('a','b'))
        monkeypatch.setattr(r.intake,'scan_authoritative',lambda:SimpleNamespace(event_by_id={'a':{},'b':{}}))
        def fail(e,event):calls.append(e);raise RuntimeError('synthetic')
        monkeypatch.setattr(r.pending,'_reevaluate_one',fail)
        result=r.pending.sweep_once(should_stop=lambda:core._stop.requested,
                                   on_outcome=core._capture_pending_outcome)
        assert calls==['a'] and len(result.outcomes)==1
        assert result.candidate_event_ids==('a','b')
        assert core.capture_failure.error_type=='FAILED_CLOSED'
    finally:r.close()

def test_capture_startup_failure_does_not_open_barrier(tmp_path,monkeypatch):
    r=_HostRuntime(tmp_path/'startup',[]);core=enable(r)
    def fail(**kw):raise RuntimeError('startup source unavailable')
    monkeypatch.setattr(r.assignment,'recover_all',fail)
    try:
        with pytest.raises(RuntimeError):core.startup()
        assert core._stop.requested
        assert r.bootstrap.state is not RuntimeLifecycleState.READY
    finally:r.close()

@pytest.mark.parametrize('mode',['rca','config','untyped'])
def test_factory_rejects_binding_before_store_or_provider_creation(tmp_path,monkeypatch,mode):
    import runtime_orchestration.cli as cli
    raw=yaml.safe_load(Path('configs/runtime_orchestration.yaml').read_text(encoding='utf-8'))
    if mode!='rca':raw.pop('rca')
    config_path=tmp_path/'config.yaml';config_path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    config=load_runtime_config(config_path)
    b=binding(config_sha256=sha256(config_path.read_bytes()).hexdigest())
    if mode=='config': b=replace(b,config_sha256='0'*64)
    if mode=='untyped': b=True
    def forbidden(*a,**kw):pytest.fail('store/provider created before guard')
    monkeypatch.setattr(cli,'_build_rca_host',forbidden)
    monkeypatch.setattr(cli,'SqliteRuntimeWorkStore',forbidden)
    root=tmp_path/'must-not-exist'
    with pytest.raises(ValueError):cli.build_runtime_application(config,project_root=root,
        config_path=config_path,capture_stop_binding=b)
    assert not root.exists()

def test_factory_no_rca_provider_construction(tmp_path,monkeypatch):
    import runtime_orchestration.cli as cli
    raw=yaml.safe_load(Path('configs/runtime_orchestration.yaml').read_text(encoding='utf-8'))
    raw.pop('rca'); path=tmp_path/'capture.yaml';path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    config=load_runtime_config(path)
    def forbidden(*a,**kw):pytest.fail('RCA/Knowledge/Google construction')
    for name in ('_build_rca_host','create_google_client','create_google_generation_client'):
        monkeypatch.setattr(cli,name,forbidden)
    app=cli.build_runtime_application(config,project_root=tmp_path/'isolated',config_path=path,
        capture_stop_binding=binding(config_sha256=sha256(path.read_bytes()).hexdigest()))
    try:
        assert app.worker._core._rca is None
        assert app.worker._core._capture_stop_binding is not None
    finally:app.close()

def test_observability_yaml_and_structured_trace_design():
    cfg=yaml.safe_load(Path('docker/promtail/promtail-config.yml').read_text(encoding='utf-8'))
    stages=cfg['scrape_configs'][0]['pipeline_stages']
    expressions=stages[0]['json']['expressions']; labels=stages[1]['labels']
    assert set(labels)=={'service_name','downstream_service'}
    line=json.dumps({'service_name':'api','downstream_service':'core-db','trace_id':'t-123'})
    record=json.loads(line)
    extracted={k:record.get(v) for k,v in expressions.items()}
    assert extracted=={'service_name':'api','downstream_service':'core-db'}
    assert json.loads(line)['trace_id']=='t-123' # raw field preserved, not indexed
    query='{service_name="api",downstream_service="core-db"} | json | trace_id="t-123"'
    assert query.startswith('{service_name=') and '| json | trace_id=' in query
    # This is a design check, NOT an implemented adapter query or Loki engine test.
    compose=Path('docker-compose.yml').read_text(encoding='utf-8')
    assert '${AIOPS_CAPTURE_LOG_MODE:-rw}' in compose
    assert '${AIOPS_CAPTURE_LOG_DIR:-./logs}' in compose
    assert yaml.safe_load(compose)['services']['promtail']['volumes'][0].endswith('${AIOPS_CAPTURE_LOG_MODE:-rw}')


def test_auto_assignment_failure_stops_before_second_durable_obligation(tmp_path,monkeypatch):
    from incident_management import WorkflowDomainError, WorkflowErrorCode
    events=[_event('a','brute_force_detected',NOW,source_ip='192.0.2.11'),
            _event('b','brute_force_detected',NOW,source_ip='192.0.2.12')]
    r=_HostRuntime(tmp_path/'assignment',events);core=enable(r);calls=[]
    try:
        core.startup()
        # Establish real isolated Processed authority, leaving obligations for recovery.
        monkeypatch.setattr(r.terminal,'_post_create_obligation',None)
        for e in events:r.terminal.process_authoritative_event(e['event_id'])
        def fail(request,policy):
            calls.append(request.incident_id)
            raise WorkflowDomainError(WorkflowErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE,
                'synthetic',workflow_operation_id=request.workflow_operation_id,
                incident_id=request.incident_id)
        monkeypatch.setattr(r.incident_manager,'auto_assign_incident',fail)
        core.run_cycle()
        assert len(calls)==1 and core.capture_failure.stage=='AUTO_ASSIGN'
        records=r.work.enumerate_all().records
        assert len(records)==1 and records[0].attempt_count==1
        assert r.state.resolve('b').processed is not None
        assert r.incidents.get_incident(r.state.resolve('b').processed.incident_id).status.value == 'OPEN'
    finally:r.close()

def test_timeout_propagates_after_safe_stop(tmp_path,monkeypatch):
    r=_HostRuntime(tmp_path/'timeout',[_event('t','brute_force_detected',NOW,source_ip='192.0.2.13')])
    core=enable(r)
    def fail(e):raise TimeoutError('synthetic timeout at public call')
    try:
        core.startup();monkeypatch.setattr(r.pending,'process_authoritative_event',fail)
        with pytest.raises(TimeoutError):core.run_cycle()
        assert core._stop.requested and core.capture_failure.error_type=='TimeoutError'
    finally:r.close()

def test_current_trace_query_contract_still_requires_index_label():
    from test_incident_evidence_loki_adapter import adapter,request
    from incident_evidence import EvidenceSource,SelectorFact
    req=request(selectors=(SelectorFact(EvidenceSource.LOKI,'trace_id','trace-test','trace-test','selectors-v1'),))
    query=adapter({})._query(req)
    assert query=='{trace_id="trace-test"}'
    assert '| json' not in query  # Confirmed gap, NOT implemented structured-query support.
