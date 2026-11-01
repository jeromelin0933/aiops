"""Real corpus; synthetic Human records, clock, credential, usage and HTTP only."""
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
import json
import httpx

import pytest

from knowledge_index import KnowledgeBuildService, SqliteKnowledgeStore, BuildOperationKey, KnowledgeReadStatus
from knowledge_index import gemini_diagnostic as d
from knowledge_index import gemini_diagnostic_controls as c
from test_knowledge_gemini_demo_gate import full54, sealed
from test_knowledge_gemini_build_integration import setup_build, stage
from test_knowledge_gemini_diagnostic_batching import batched
from test_knowledge_gemini_diagnostic_single_probe import probe
from test_knowledge_gemini_embedding2_serialization import offline_sdk
from test_knowledge_gemini_diagnostic_controls import safety_parse

pytestmark = pytest.mark.parametrize('setup_build', ['spec014-gemini-embedding-2-profile-v1'], indirect=True)


class Clock:
    def __init__(self):
        self.mono = 100.0
        self.origin = datetime.now(timezone.utc)
        self.skew = 0
    def read(self):
        return self.mono, self.origin + timedelta(seconds=self.mono-100+self.skew)
    def sleep(self, seconds):
        self.mono += seconds


def rebind(p, record):
    b=p['binding']; digest=c.authorization_record_digest(record)
    p['binding']=replace(b, record=record, sdk_byte_evidence=replace(b.sdk_byte_evidence,
        authorization_record_sha256=digest))
    p['value']['binding'].update(authorization_record_sha256=digest, bounds_basis=record.bounds_basis,
        cost_basis=record.cost_basis)
    for child, (_,_,commitment) in zip(p['value']['batch_sdk_evidence'],
                                      c.DiagnosticBatchPlan.from_mapping(p['value']['sequential_batch_plan']).ranges):
        child['authorization_record_sha256']=c.authorization_record_digest(replace(record,input_commitment=commitment))


@pytest.fixture
def bounded54(full54, monkeypatch):
    p=full54; v=p['value']; r=p['binding'].record
    v.pop('accepted_input_token_bounds');v.pop('token_evidence')
    v['provider_limits'].update(item_limit='UNKNOWN', per_item_token_limit='UNKNOWN',
        aggregate_token_limit='UNKNOWN', payload_byte_limit='UNKNOWN')
    risk=c.Full54BoundedRisk(c.FULL54_RISK_AUTHORITY,'synthetic-exact-contract-acceptance',
        'synthetic-project-exclusive-session', 'synthetic-Human-dashboard')
    v['full54_bounded_risk']=asdict(risk)
    v['publication']['method_acceptance_reference']=risk.contract_approval_reference
    plan=v['sequential_batch_plan']; plan['admission_basis']=c.FULL54_RISK_BASIS
    plan['maximum_batch_tokens']=73728
    plan['quota'].update(rpm=100,tpm=30000,rpd=1000,request_units_per_invocation='UNKNOWN')
    plan['quota']['source']['reference']=risk.quota_evidence_reference
    v['free_tier_evidence']['quota_evidence'].update(rpm=100,tpm=30000,rpd=1000)
    v['free_tier_evidence']['quota_evidence']['source']['reference']=risk.quota_evidence_reference
    v['pricing_evidence']['account_reference']='gen-lang-client-0149178738'
    v['credential_binding']['secret_revision']='rev-1'
    v['free_tier_evidence']['account_context'].update(account_reference='gen-lang-client-0149178738',secret_revision='rev-1')
    monkeypatch.setitem(c.os.environ,'GEMINI_CREDENTIAL_REVISION','rev-1')
    parsed=c.DiagnosticBatchPlan.from_mapping(plan)
    scope=replace(r.p0_full54_history_scope,batch_plan_commitment=parsed.commitment)
    r=replace(r, p0_full54_history_scope=scope, bounds_basis='SEQUENTIAL_BATCH_PLAN_V1:'+parsed.commitment,
        project_identity='gen-lang-client-0149178738',credential_profile_revision='rev-1',
        maximum_item_tokens=8192,maximum_aggregate_tokens=73728,maximum_usage_units=442368,
        maximum_item_bytes=1302,cost_basis='FULL54_INTERNAL_HOLD_V1',full54_bounded_risk=risk)
    rebind(p,r)
    p['clock']=Clock();monkeypatch.setattr(d,'_risk_clock',p['clock'].read)
    monkeypatch.setattr(d.time,'sleep',p['clock'].sleep)
    return p


def test_six_batches_risk_units_separate_from_usage_build_validate_no_activation(bounded54,tmp_path):
    p=bounded54;r=p['binding'].record
    assert c.DiagnosticAuthorizationRecord.from_json(json.dumps(asdict(r)).encode())==r
    assert 'token_evidence' not in safety_parse(p['value']).fields
    d._prepare_sequential_batches(p['request'],p['binding'],safety_parse(p['value']),lambda r,q: c.DiagnosticExecutionFacts(r.source_revision,True,r.pm_approval_reference,r.spec_lead_identity,r.runner_identity,r.authorized_host,r.credential_profile_identity,datetime.now(timezone.utc)))
    with SqliteKnowledgeStore(tmp_path/'risk-knowledge.sqlite3') as store:
        svc=KnowledgeBuildService(store,p['provider'],p['index'])
        result=stage(svc,p)
        assert result.record is not None and not result.failures
        assert not svc.validate(result.record.build_identity,BuildOperationKey('risk-validate'),maximum_probe_results=12).findings
        assert store.read_activation().status is KnowledgeReadStatus.NOT_FOUND
    assert len(p['calls'])==len(p['credentials'])==6
    assert p['clock'].mono>=490  # Initial quiet period and five post-receipt periods.
    with closing(p['usage']._connect(read_only=True)) as db:
        rows=db.execute('SELECT outcome,usage_reserved,usage_observed,cost_reserved FROM attempts WHERE campaign_id=?',(r.campaign_id,)).fetchall()
        assert len(rows)==6 and all(x[0]=='SUCCESS' and x[1]==73728 and 0<int(x[2])<=30000 and x[3]==1 for x in rows)
        assert c._blocking_fence_count(db)==2
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert len(p['calls'])==6


@pytest.mark.parametrize('change',['no-approval','direction-as-grant','no-risk','project','revision','query','formal',
    'source','child','quota','accounting','missing-budget','token-mixing','contract-mismatch','missing-safety','expired'])
def test_unapproved_or_mixed_risk_never_reserves(bounded54,change):
    p=bounded54;w=json.loads(json.dumps(asdict(p['binding'].record)));v=p['value']
    if change=='no-approval':w['full54_bounded_risk']['contract_approval_reference']='PENDING'
    elif change=='direction-as-grant':w['pm_approval_reference']=c.FULL54_RISK_AUTHORITY
    elif change=='no-risk':w['full54_bounded_risk']=None
    elif change=='project':w['project_identity']='other-project'
    elif change=='revision':w['credential_profile_revision']='rev-2'
    elif change=='query':w['input_kind']='query'
    elif change=='formal':w['p0_full54_history_scope']['delivery_scope']='PRODUCTION'
    elif change=='source':w['source_revision']='c'*40
    elif change=='child':w['p0_full54_history_scope']['batch_input_commitments'][0]='a'*64
    elif change=='quota':v['sequential_batch_plan']['quota']['tpm']=999999
    elif change=='accounting':v['sequential_batch_plan']['quota']['request_units_per_invocation']=9
    elif change=='missing-budget':w['maximum_cost_micro_units']=0
    elif change=='token-mixing':v['accepted_input_token_bounds']={'a'*64:8192}
    elif change=='contract-mismatch':v['publication']['method_acceptance_reference']='unrelated'
    elif change=='missing-safety':v.pop('full54_bounded_risk')
    elif change=='expired':w['expires_at']='2020-01-01T00:00:00+00:00'
    try:
        p['binding']=replace(p['binding'],record=c.DiagnosticAuthorizationRecord.from_json(json.dumps(w).encode()))
        d.run_live(p['request'],live_opt_in=True)
    except c.DiagnosticDenied:pass
    else:pytest.fail('invalid risk grant admitted')
    assert p['calls']==p['credentials']==[]
    with closing(p['usage']._connect(read_only=True)) as db:assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()==(2,)


@pytest.mark.parametrize('outcome',['UNKNOWN','TIMEOUT','IN_FLIGHT'])
def test_extra_fence_blocks_risk(bounded54,outcome):
    p=bounded54; row=dict(c._P0_SECOND_HISTORY_ROW);row.update(campaign_id='extra',request_id='extra',outcome=outcome)
    with closing(p['usage']._connect()) as db:
        db.execute('INSERT INTO attempts('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()));db.commit()
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert p['calls']==p['credentials']==[]


@pytest.mark.parametrize('mode',['backward','skew','unavailable','stalled'])
def test_untrusted_clock_stops_before_reservation(bounded54,monkeypatch,mode):
    p=bounded54
    def sleep(seconds):
        if mode=='backward':p['clock'].mono-=seconds
        elif mode=='skew':p['clock'].skew+=10
        elif mode=='unavailable':p['clock'].mono=float('nan')
    monkeypatch.setattr(d.time,'sleep',sleep)
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert p['calls']==p['credentials']==[]


def test_local_usage_window_and_tpm_guard(bounded54):
    window=d._Full54QuietWindow(); window.completed(30000)
    with pytest.raises(c.DiagnosticDenied):window.completed(1)
    with pytest.raises(c.DiagnosticDenied):d._Full54QuietWindow().completed(30001)


def test_future_receipt_or_expired_window_is_not_quiet(bounded54):
    p=bounded54;window=d._Full54QuietWindow()
    window.quiet_since-=65
    window.receipts=[(p['clock'].mono+1,30000)]
    with pytest.raises(c.DiagnosticDenied):window.before_batch(lambda:None)
    r=replace(p['binding'].record, expires_at=(p['clock'].origin+timedelta(seconds=30)).isoformat())
    window=d._Full54QuietWindow(r)
    p['clock'].sleep(31)
    with pytest.raises(c.DiagnosticDenied):window.check_clock()


def test_risk_reservation_persistence_failure_never_resolves_credential(bounded54,monkeypatch):
    p=bounded54
    def fail(*args,**kwargs):raise c.DiagnosticDenied('synthetic reservation storage unavailable')
    monkeypatch.setattr(p['usage'],'reserve',fail)
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert p['calls']==p['credentials']==[]


def test_risk_no_opt_in(bounded54):
    p=bounded54
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=False)
    assert p['calls']==p['credentials']==[]


@pytest.mark.parametrize('failure',['timeout','unknown_usage','partial'])
def test_risk_failure_no_partial_build_or_resume(bounded54,tmp_path,failure):
    p=bounded54;p['transport'](failure=failure)
    with SqliteKnowledgeStore(tmp_path/'failed-risk.sqlite3') as store:
        result=stage(KnowledgeBuildService(store,p['provider'],p['index']),p)
        assert result.record is None and result.failures
        assert store.read_activation().status is KnowledgeReadStatus.NOT_FOUND
    before=len(p['calls']);assert 0<before<=2
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert len(p['calls'])==before


@pytest.mark.parametrize('mode',['429','usage-over-tpm','usage-zero','persistence'])
def test_receipt_or_persistence_failure_retains_new_fence_and_stops(bounded54,tmp_path,monkeypatch,mode):
    p=bounded54;delegate=d._create_live_transport()
    def send(request):
        response=delegate.handle_request(request)
        if mode=='429':return httpx.Response(429,json={'error':{'code':429,'message':'synthetic quota stop','status':'RESOURCE_EXHAUSTED'}})
        payload=json.loads(response.content)
        if mode.startswith('usage-'):payload['usageMetadata']['promptTokenCount']=30001 if mode=='usage-over-tpm' else 0
        return httpx.Response(200,json=payload)
    monkeypatch.setattr(d,'_create_live_transport',lambda:httpx.MockTransport(send))
    if mode=='persistence':
        def fail(*args,**kwargs):raise c.DiagnosticDenied('synthetic post-transport persistence failure')
        monkeypatch.setattr(p['usage'],'_finish_provider_usage',fail)
    with SqliteKnowledgeStore(tmp_path/'rejected-risk.sqlite3') as store:
        result=stage(KnowledgeBuildService(store,p['provider'],p['index']),p)
        assert result.record is None and result.failures
        assert store.get_staged_build(p['request'].build_identity).status is KnowledgeReadStatus.NOT_FOUND
        assert store.read_activation().status is KnowledgeReadStatus.NOT_FOUND
        assert p['index'].inspect(p['request'].build_identity) is None
    assert len(p['calls'])==1
    with closing(p['usage']._connect(read_only=True)) as db:
        rows=db.execute('SELECT outcome,usage_reserved,usage_observed,cost_reserved FROM attempts WHERE campaign_id=?',
            (p['binding'].record.campaign_id,)).fetchall()
        assert len(rows)==1 and rows[0][1]==73728 and rows[0][3]==1
        if mode=='usage-over-tpm':assert rows[0][0]=='UNKNOWN' and rows[0][2]=='30001'
        if mode=='usage-zero':
            # Complete vectors can mean attempt SUCCESS, never accepted usage
            # or campaign success. The UNKNOWN usage predicate is the fence.
            assert rows[0]==('SUCCESS',73728,'UNKNOWN',1)
        if mode in ('usage-over-tpm','usage-zero','persistence'):assert c._blocking_fence_count(db)==3
        if mode=='persistence':assert rows[0][0]=='IN_FLIGHT'
    before=len(p['calls']); credentials_before=len(p['credentials'])
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert len(p['calls'])==before
    assert len(p['credentials'])==credentials_before
    with closing(p['usage']._connect(read_only=True)) as db:
        assert db.execute('SELECT outcome,usage_reserved,usage_observed,cost_reserved FROM attempts WHERE campaign_id=?',
            (p['binding'].record.campaign_id,)).fetchall()==rows
