"""Path B: labelled resource-risk units, TEMP ledger and MockTransport only."""
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
import httpx

import pytest
from knowledge_index import gemini_diagnostic as d, gemini_diagnostic_controls as c
from test_knowledge_gemini_query_counting import query_counting, counting, offline_sdk
from test_knowledge_december_minimum_safety import minimum
from test_knowledge_december_fence_policy import composite, seed, rows
from test_knowledge_query_count_success_review import stage2
from test_knowledge_gemini_diagnostic_controls import facts, safety_parse


@pytest.fixture(autouse=True)
def sealed(offline_sdk):
    yield
    assert offline_sdk.blocked_calls == []


@pytest.fixture
def bounded(query_counting, monkeypatch):
    # Exact external Demo credential PROFILE metadata, still a fake secret.
    f=query_counting
    grant=replace(f['grant'],credential_profile_identity='spec014-gemini-embedding-2-profile-v1')
    f['value']['credential_binding']['profile_identity']=grant.credential_profile_identity
    f['value']['free_tier_evidence']['account_context']['profile_identity']=grant.credential_profile_identity
    f.update(grant=grant,e4=replace(f['e4'],authorization_record_sha256=c.authorization_record_digest(grant)))
    monkeypatch.setenv('GEMINI_CREDENTIAL_PROFILE',grant.credential_profile_identity)
    f=minimum.__wrapped__(query_counting,monkeypatch)
    base=replace(f['grant'],purpose='CONTROLLED_PROVIDER_DIAGNOSTIC',
        campaign_id='offline-bounded-query',pm_approval_reference='OFFLINE-INDEPENDENT-BOUNDED-GRANT',
        maximum_item_tokens=8192,maximum_aggregate_tokens=8192,maximum_usage_units=8192,
        maximum_payload_bytes=8192,normalization_tolerance=0.001,
        december_demo_fence_review=None,project_identity=None,credential_profile_revision=None,
        bounds_basis=c.DEMO_QUERY_RISK_BASIS,cost_basis=c.DEMO_QUERY_RISK_COST)
    review=composite(f,base)
    target=replace(base,december_demo_fence_review=review,
        project_identity=review.project_identity,credential_profile_revision=review.credential_profile_revision)
    f['target']=target
    import test_knowledge_query_count_success_review as helper
    monkeypatch.setattr(helper,'facts',lambda **kw:facts(pm_approval_reference=target.pm_approval_reference,**kw))
    request,value,credentials,calls=stage2(f,monkeypatch)
    value.pop('provider_limits');value.pop('token_evidence');value.pop('accepted_input_token_bounds')
    value.update(artifact_revision=c.DEMO_QUERY_RISK_REVISION,operation=target.purpose,
        project_identity=target.project_identity,query_binding=c.query_counting_binding(),
        outbound_eligibility='APPROVED_SYNTHETIC_NON_SECRET',
        pricing_evidence=dict(account_reference=target.december_demo_fence_review.account_reference,
            price_tier='FREE_TIER_TEXT_V1',currency='USD'),
        free_tier_evidence=deepcopy(f['value']['free_tier_evidence']),
        validity=dict(not_before='2026-11-25T15:00:00Z',expires_at='2026-12-17T15:59:00Z'),
        resource_risk=dict(method=c.DEMO_QUERY_RISK_BASIS,units=8192,token_count='UNKNOWN',
            provider_token_limits='UNKNOWN',maximum_input_bytes=274,maximum_request_bytes=8192,maximum_requests=1))
    value['free_tier_evidence']['data_use_permission']['pm_approval_reference']=target.pm_approval_reference
    monkeypatch.setattr(d,'load_live_safety_artifact',lambda:safety_parse(value))
    seed(f)
    f.update(request=request,risk_value=value,risk_credentials=credentials,risk_calls=calls)
    return f


def test_bounded_query_no_count_or_token_proof_native_dispatch_and_usage(bounded):
    f=bounded;before=rows(f)
    a=safety_parse(f['risk_value']);a.matches_record_input(f['target'],f['target_binding'].approved_input,f['target_binding'].sdk_byte_evidence,f['target'].source_revision)
    proof,basis,units,hold=d._safety_from_artifact(f['target'],(c.QUERY_COUNTING_ENVELOPE,),a)
    assert basis.status=='BOUNDED_RISK' and basis.token_method==c.DEMO_QUERY_RISK_BASIS
    assert basis.provider_item_token_limit is basis.provider_aggregate_token_limit is None
    assert basis.price_micro_units_per_million_tokens is None and units==8192 and hold==1
    result=d.run_live(f['request'],live_opt_in=True)
    assert result.outcome=='SUCCESS' and result.admission_usage_basis==c.DEMO_QUERY_RISK_BASIS
    assert len(f['risk_calls'])==len(f['risk_credentials'])==1 and f['calls']==[]
    assert all(row in rows(f) for row in before)
    with pytest.raises(c.DiagnosticDenied):d.run_live(f['request'],live_opt_in=True)
    assert len(f['risk_calls'])==1


@pytest.mark.parametrize('path,value',[
    (('resource_risk','token_count'),274),
    (('resource_risk','provider_token_limits'),8192),
    (('resource_risk','maximum_requests'),2),
    (('resource_risk','units'),1),
    (('resource_risk','maximum_input_bytes'),275),
    (('resource_risk','maximum_request_bytes'),8193),
    (('query_binding','generation'),2),
    (('project_identity',),'other'),
    (('credential_binding','secret_revision'),'other'),
    (('binding','authorization_record_sha256'),'f'*64),
    (('outbound_eligibility',),'ALLOW_ALL'),
])
def test_wrong_risk_or_binding_before_credential(bounded,path,value):
    f=bounded;v=f['risk_value']
    for key in path[:-1]:v=v[key]
    v[path[-1]]=value;before=rows(f)
    with pytest.raises(c.DiagnosticDenied):d.run_live(f['request'],live_opt_in=True)
    assert rows(f)==before and not f['risk_calls'] and not f['risk_credentials']


@pytest.mark.parametrize('field', ['token_evidence','accepted_input_token_bounds'])
def test_risk_schema_rejects_disguised_token_proof(bounded,field):
    bounded['risk_value'][field]={}
    with pytest.raises(c.DiagnosticDenied):d.run_live(bounded['request'],live_opt_in=True)
    assert not bounded['risk_calls'] and not bounded['risk_credentials']


@pytest.mark.parametrize('state',['UNKNOWN','TIMEOUT','IN_FLIGHT','SUCCESS'])
def test_unreviewed_fence_not_waived_by_path_b(bounded,state):
    f=bounded
    with sqlite3.connect(f['path']) as db:
        db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved) VALUES('stage1-or-other','unknown',?,1,1,1)",(state,))
    before=rows(f)
    with pytest.raises(c.DiagnosticDenied):d.run_live(f['request'],live_opt_in=True)
    assert rows(f)==before and not f['risk_calls'] and not f['risk_credentials']


def test_budget_still_blocks_atomic_reservation(bounded):
    f=bounded;before=rows(f)
    with pytest.raises(c.DiagnosticDenied):f['store'].reserve(f['target'],'new',items=1,usage_units=8193,cost_micro_units=1)
    assert rows(f)==before


def test_different_query_cannot_reuse_risk_admission(bounded):
    f=bounded;changed=replace(f['request'],query=replace(f['request'].query,text='other query'))
    with pytest.raises(c.DiagnosticDenied):d.run_live(changed,live_opt_in=True)
    assert not f['risk_calls'] and not f['risk_credentials']


def test_risk_transport_loss_preserves_unknown_and_hold_no_retry(bounded,monkeypatch):
    f=bounded
    def lost(request):
        f['risk_calls'].append(True)
        raise httpx.ReadError('SYNTHETIC RESPONSE LOSS')
    monkeypatch.setattr(d,'_create_live_transport',lambda:httpx.MockTransport(lost))
    result=d.run_live(f['request'],live_opt_in=True)
    assert result.outcome=='UNKNOWN' and result.provider_attempts==1
    with sqlite3.connect(f['path']) as db:
        row=db.execute('SELECT outcome,usage_reserved,cost_reserved,usage_observed FROM attempts WHERE campaign_id=?',(f['target'].campaign_id,)).fetchone()
    assert row==('UNKNOWN',8192,1,'UNKNOWN')
    with pytest.raises(c.DiagnosticDenied):d.run_live(f['request'],live_opt_in=True)
    assert len(f['risk_calls'])==1


def test_provider_usage_above_risk_budget_is_retained_not_refunded(bounded,monkeypatch):
    f=bounded
    def excessive(request):
        f['risk_calls'].append(True)
        return httpx.Response(200,json={'embeddings':[{'values':[1.]+[0.]*767}],
            'usageMetadata':{'promptTokenCount':9000}})
    monkeypatch.setattr(d,'_create_live_transport',lambda:httpx.MockTransport(excessive))
    with pytest.raises(c.DiagnosticDenied):d.run_live(f['request'],live_opt_in=True)
    with sqlite3.connect(f['path']) as db:
        row=db.execute('SELECT outcome,usage_reserved,cost_reserved,usage_observed FROM attempts WHERE campaign_id=?',(f['target'].campaign_id,)).fetchone()
    assert row==('UNKNOWN',8192,1,'9000') and len(f['risk_calls'])==1


def test_risk_native_c_d_a_e_composition_without_counting_proof(bounded,monkeypatch):
    from datetime import datetime,timezone,timedelta
    from hashlib import sha256
    from test_december_demo_protected_entry import test_real_port_branch_with_only_mock_transports_and_persistence
    import uuid
    f=bounded;value=f['risk_value'];record=f['target'];now=datetime.now(timezone.utc)
    value['december_demo_execution']=dict(
        scope_identity=c.DECEMBER_EXECUTION_SCOPE,decision='APPROVE_DECEMBER_DEMO_EXECUTION',
        mode='OFFLINE_FIXTURE',grant_reference='OFFLINE-FAKE-BOUNDED-GRANT',issuer_identity='SYNTHETIC OFFLINE ONLY',
        valid_from=(now-timedelta(minutes=1)).isoformat(),expires_at='2026-12-17T15:59:00+00:00',
        diagnostic_authorization_sha256=c.authorization_record_digest(record),repository_revision=record.source_revision,
        account_reference=value['pricing_evidence']['account_reference'],acceptance_reference='OFFLINE-FAKE-ACCEPTANCE',
        accepted_by=record.spec_lead_identity,accepted_at=(now-timedelta(minutes=1)).isoformat(),acceptance_expires_at='2026-12-17T15:59:00+00:00',
        run_id='dec17-bounded-'+uuid.uuid4().hex[:10],generation_model='gemini-3.5-flash-lite',
        generation_profile='demo-google-gemini35-flash-lite-generation-profile-v1',generation_profile_version='2',generation_api='v1beta',
        physical_call_allocation=[1,1,1],operation_scopes=list(c.DECEMBER_OPERATION_SCOPES),
        external_files=[dict(path=str(c.DECEMBER_EXTERNAL_ROOT/n),version='1',sha256=sha256((c.DECEMBER_EXTERNAL_ROOT/n).read_bytes()).hexdigest()) for n in sorted(c.DECEMBER_EXTERNAL_NAMES)])
    def load_safety():
        a=safety_parse(value);b=f['target_binding']
        a.matches_record_input(record,b.approved_input,b.sdk_byte_evidence,record.source_revision)
        return a
    monkeypatch.setattr(c,'load_live_binding',lambda:f['target_binding'])
    monkeypatch.setattr(c,'load_live_safety_artifact',load_safety)
    monkeypatch.setattr(c,'LiveDiagnosticUsage',lambda:f['store'])
    f.update(scope=value['december_demo_execution'],current=(f['request'],value,f['risk_credentials'],f['risk_calls']),load_safety=load_safety)
    # Reuse the existing real-port-branch test's explicit MockTransport/Index/C
    # substitutions. Neither Host Chroma nor Google is used by this test.
    test_real_port_branch_with_only_mock_transports_and_persistence(f,monkeypatch)
    assert f['calls']==[] and record.december_demo_fence_review.counting_success_review is None
