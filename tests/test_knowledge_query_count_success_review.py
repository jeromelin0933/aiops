"""Narrow reviewed Query-count fence; same isolated native ledger, no live I/O."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from knowledge_index import gemini_diagnostic as d, gemini_diagnostic_controls as c
from knowledge_index import QueryEmbeddingRequest, CanonicalKnowledgeQuery
from knowledge_index.config import load_knowledge_config, select_gemini_embedding2_capability
from knowledge_index.contracts import OpaqueExternalReference, OpaqueReferenceType
from test_knowledge_gemini_query_counting import query_counting, run as count_run
from test_knowledge_gemini_diagnostic_counting import counting
from test_knowledge_gemini_embedding2_serialization import offline_sdk
from test_knowledge_gemini_diagnostic_controls import record, facts, safety_case, safety_parse
from test_knowledge_gemini_diagnostic import _host_e4_observation

FIELDS=('campaign_id','request_id','outcome','items','usage_reserved','cost_reserved','usage_observed','observation_json')


@pytest.fixture(autouse=True)
def sealed(offline_sdk):
    yield
    assert offline_sdk.blocked_calls==[]


@pytest.fixture
def reviewed(query_counting):
    f=query_counting;f['provider']();assert count_run().outcome=='SUCCESS'
    with sqlite3.connect(str(f['path'])) as db:
        row=db.execute('SELECT '+','.join(FIELDS)+' FROM attempts').fetchone()
    obs=json.loads(row[-1])
    base=record(campaign_id='review-target',credential_profile_identity=f['grant'].credential_profile_identity,
        input_scope_identity='build:'+c.QUERY_COUNTING_BUILD,input_commitment=c.QUERY_COUNTING_INPUT,
        maximum_invocations=1,maximum_items=1,maximum_item_tokens=1000,maximum_aggregate_tokens=1000,
        maximum_item_bytes=274,maximum_payload_bytes=1000,maximum_usage_units=1000,
        valid_from='2000-01-01T00:00:00+00:00',expires_at='2099-01-01T00:00:00+00:00',
        bounds_basis='fixture-approved-bounds',cost_basis='fixture-approved-price')
    review=c.QueryCountingSuccessReview(
        authority_reference='PM 2026-11-22 Query-count Success Fence Review Decision',
        disposition='NON_BLOCKING_FOR_THIS_CAMPAIGN_ONLY',review_reference='OFFLINE-REVIEW-NOT-A-LIVE-APPROVAL',
        reviewer_identity=base.spec_lead_identity,recorded_at=datetime.now(timezone.utc).isoformat(),
        expires_at='2098-01-01T00:00:00+00:00',origin_campaign_id=row[0],origin_request_id=row[1],
        origin_source_revision=obs['source_revision'],origin_authorization_sha256=obs['authorization_sha256'],
        row_sha256=c._p0_commitment(dict(zip(FIELDS,row))),observation_sha256=sha256(row[-1].encode()).hexdigest(),
        response_sha256=obs['response_sha256'],project_identity=obs['project_identity'],secret_revision=obs['profile_revision'],
        target_campaign_id=base.campaign_id,target_pm_approval_reference=base.pm_approval_reference,
        target_source_revision=base.source_revision,target_credential_profile_identity=base.credential_profile_identity,
        target_account_reference='fixture-account',query_sha256=c.query_counting_binding()['canonical_query_sha256'],
        envelope_sha256=c.query_counting_binding()['envelope_sha256'],model_identity='gemini-embedding-2',
        embedding_profile_identity='spec014-gemini-embedding-2-embedding-v1')
    f.update(target=replace(base,query_count_success_review=review),review=review,origin_row=row)
    return f


def classified(f,record_override=None):
    with sqlite3.connect(str(f['path'])) as db:
        return c._blocking_fence_count(db,f['target'] if record_override is None else record_override)


def test_success_review_readonly_exact_campaign_preserves_all_bytes(reviewed):
    f=reviewed;before=f['path'].read_bytes()
    assert classified(f)==0
    with sqlite3.connect(str(f['path'])) as db:
        assert c._blocking_fence_count(db)==1
        assert c._blocking_fence_count(db,replace(f['target'],query_count_success_review=None))==1
        assert db.execute('SELECT '+','.join(FIELDS)+' FROM attempts').fetchone()==f['origin_row']
    assert f['path'].read_bytes()==before and len(f['calls'])==1


@pytest.mark.parametrize('field,value',[
    ('target_campaign_id','other'),('target_source_revision','a'*40),('target_pm_approval_reference','other'),
    ('target_credential_profile_identity','other'),('reviewer_identity','other'),('query_sha256','f'*64),
    ('envelope_sha256','f'*64),('embedding_profile_identity','legacy'),('model_identity','other'),
    ('origin_source_revision','a'*40),('expires_at','2020-01-01T00:00:00+00:00'),
    ('disposition','NON_BLOCKING_FOR_ALL'),('authority_reference','other')])
def test_wrong_or_expired_binding_never_admits(reviewed,field,value):
    f=reviewed
    with pytest.raises(c.DiagnosticDenied):replace(f['target'],query_count_success_review=replace(f['review'],**{field:value}))
    assert len(f['calls'])==1


@pytest.mark.parametrize('mutation',['row','response','count','model','query','profile','hold','outcome'])
def test_changed_row_or_response_is_rejected(reviewed,mutation):
    f=reviewed;obs=json.loads(f['origin_row'][-1])
    with sqlite3.connect(str(f['path'])) as db:
        if mutation=='row':db.execute("UPDATE attempts SET request_id='changed'")
        elif mutation=='hold':db.execute('UPDATE attempts SET cost_reserved=2')
        elif mutation=='outcome':db.execute("UPDATE attempts SET outcome='UNKNOWN'")
        else:
            if mutation=='response':obs['response_sha256']='f'*64
            if mutation=='count':obs['observed_token_count']=129
            if mutation=='model':obs['model']='other'
            if mutation=='query':obs['query_binding']['envelope_sha256']='f'*64
            if mutation=='profile':obs['profile_identity']='other'
            db.execute('UPDATE attempts SET observation_json=?',(json.dumps(obs),))
    with pytest.raises(c.DiagnosticDenied):classified(f)
    assert len(f['calls'])==1


@pytest.mark.parametrize('outcome',['UNKNOWN','TIMEOUT','IN_FLIGHT','FAILED'])
def test_even_rehashed_non_success_is_not_reviewable(reviewed,outcome):
    f=reviewed
    with sqlite3.connect(str(f['path'])) as db:
        db.execute('UPDATE attempts SET outcome=?',(outcome,))
        row=db.execute('SELECT '+','.join(FIELDS)+' FROM attempts').fetchone()
    review=replace(f['review'],row_sha256=c._p0_commitment(dict(zip(FIELDS,row))))
    with pytest.raises(c.DiagnosticDenied):classified(f,replace(f['target'],query_count_success_review=review))


@pytest.mark.parametrize('outcome',['UNKNOWN','TIMEOUT','IN_FLIGHT','SUCCESS'])
def test_unreviewed_other_fence_blocks_atomic_reservation(reviewed,outcome):
    f=reviewed
    with sqlite3.connect(str(f['path'])) as db:
        db.execute('INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved) VALUES(?,?,?,1,100,1)',('other','other',outcome))
    assert classified(f)==1
    with pytest.raises(c.DiagnosticDenied):f['store'].reserve(f['target'],'target',items=1,usage_units=300,cost_micro_units=1)
    assert len(f['rows']())==2 and len(f['calls'])==1


def test_atomic_reservation_and_final_revalidation_keep_origin_holds(reviewed):
    f=reviewed;f['store'].reserve(f['target'],'target',items=1,usage_units=300,cost_micro_units=1)
    f['store'].revalidate_query_success_review(f['target'],'target',items=1,usage_units=300,cost_micro_units=1)
    with sqlite3.connect(str(f['path'])) as db:
        assert db.execute('SELECT '+','.join(FIELDS)+' FROM attempts WHERE campaign_id=?',(f['origin_row'][0],)).fetchone()==f['origin_row']
        assert db.execute('SELECT SUM(usage_reserved),SUM(cost_reserved) FROM attempts').fetchone()==(301,2)
    with pytest.raises(c.DiagnosticDenied):f['store'].reserve(f['target'],'second',items=1,usage_units=1,cost_micro_units=1)
    assert len(f['calls'])==1


@pytest.mark.parametrize('change',['origin','new_fence','expiry'])
def test_final_guard_denies_mutation_after_reservation(reviewed,change,monkeypatch):
    f=reviewed;f['store'].reserve(f['target'],'target',items=1,usage_units=300,cost_micro_units=1)
    target=f['target']
    if change=='expiry':
        class Later(datetime):
            @classmethod
            def now(cls,tz=None):return datetime(2098,6,1,tzinfo=timezone.utc)
        monkeypatch.setattr(c,'datetime',Later)
    else:
        with sqlite3.connect(str(f['path'])) as db:
            if change=='origin':db.execute('UPDATE attempts SET cost_reserved=2 WHERE campaign_id=?',(f['origin_row'][0],))
            else:db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved) VALUES('other','x','UNKNOWN',1,1,1)")
    with pytest.raises(c.DiagnosticDenied):f['store'].revalidate_query_success_review(target,'target',items=1,usage_units=300,cost_micro_units=1)
    assert len(f['calls'])==1


def test_wire_roundtrip_digest_binds_review_and_legacy_omits_absent(reviewed):
    f=reviewed;wire=asdict(f['target'])
    assert c.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())==f['target']
    legacy=replace(f['target'],query_count_success_review=None)
    old=asdict(legacy)
    for name in ('project_identity','credential_profile_revision','p0_unknown_review','p0_two_unknown_reviews','p0_full54_history_scope','full54_bounded_risk','query_count_success_review','december_demo_fence_review','december_debug_call_budget'):
        if old[name] is None:old.pop(name)
    assert c.authorization_record_digest(legacy)==sha256(json.dumps(old,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
    assert c.authorization_record_digest(f['target'])!=c.authorization_record_digest(legacy)


def test_review_implementation_authority_is_not_a_live_grant():
    with pytest.raises(c.DiagnosticDenied):record(pm_approval_reference=c.QUERY_SUCCESS_REVIEW_AUTHORITY)


@pytest.mark.parametrize('raw',[{},[],True,{'review_reference':'missing-other-required-fields'}])
def test_optional_review_present_requires_closed_complete_schema(raw):
    wire=asdict(record());wire['query_count_success_review']=raw
    with pytest.raises(c.DiagnosticDenied):c.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())


def test_atomic_two_competitors_cannot_both_reserve(reviewed):
    f=reviewed;barrier=Barrier(2)
    def reserve(index):
        barrier.wait(timeout=5)
        try:
            f['store'].reserve(f['target'],'target-'+str(index),items=1,usage_units=300,cost_micro_units=1)
            return True
        except c.DiagnosticDenied:return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(reserve,[1,2]))
    assert sum(results)==1 and len(f['rows']())==2 and len(f['calls'])==1


@pytest.mark.parametrize('field,value',[
    ('observed_token_count',0),('model','text-embedding-004'),
    ('query_binding',dict(c.query_counting_binding(),generation=True)),
])
def test_even_rehashed_success_must_have_exact_typed_observation(reviewed,field,value):
    f=reviewed;obs=json.loads(f['origin_row'][-1]);obs[field]=value;raw=json.dumps(obs)
    with sqlite3.connect(str(f['path'])) as db:
        db.execute('UPDATE attempts SET observation_json=?',(raw,))
        row=db.execute('SELECT '+','.join(FIELDS)+' FROM attempts').fetchone()
    review=replace(f['review'],row_sha256=c._p0_commitment(dict(zip(FIELDS,row))),observation_sha256=sha256(raw.encode()).hexdigest())
    with pytest.raises(c.DiagnosticDenied):classified(f,replace(f['target'],query_count_success_review=review))


def stage2(f,monkeypatch):
    """Sealed synthetic publication for actual existing embedding dispatch."""
    from pathlib import Path
    cap=select_gemini_embedding2_capability(OpaqueExternalReference(OpaqueReferenceType.CREDENTIAL_PROFILE,f['target'].credential_profile_identity))
    cfg=load_knowledge_config(Path(__file__).resolve().parents[1]/'configs/knowledge_index.yaml')
    request=QueryEmbeddingRequest(c.QUERY_COUNTING_BUILD,CanonicalKnowledgeQuery('1.0','1.0',c.QUERY_COUNTING_TEXT,()),cap,cfg.limits)
    target=f['target'];approved=c.ApprovedDiagnosticInput(1,target.campaign_id,target.source_revision,target.purpose,'query',target.input_scope_identity,target.input_commitment,(c.QUERY_COUNTING_BUILD,),(c.QUERY_COUNTING_ENVELOPE,))
    prepared=d._preflight(load_record=lambda:target,resolve_facts=lambda *_:facts(credential_profile_identity=target.credential_profile_identity),
        safety=lambda r,texts:(d._offline_proof(r),d._offline_safety_basis(r,texts),sum(len(t.encode()) for t in texts),1),request=request,live_opt_in=True,offline_harness=True)
    e4,_=_host_e4_observation(prepared,approved)
    value=safety_case()[3]
    value['binding'].update(campaign_id=target.campaign_id,authorization_record_sha256=c.authorization_record_digest(target),source_revision=target.source_revision,kind='query',input_scope_identity=target.input_scope_identity,input_commitment=target.input_commitment,bounds_basis=target.bounds_basis,cost_basis=target.cost_basis)
    value['credential_binding'].update(profile_identity=target.credential_profile_identity)
    value['e4_reference'].update(reference=e4.reference,sdk_body_sha256=e4.sdk_body_sha256,canonical_json_sha256=e4.canonical_json_sha256,sdk_body_bytes=e4.sdk_body_bytes)
    bounds={sha256(c.QUERY_COUNTING_ENVELOPE.encode()).hexdigest():300}
    value['accepted_input_token_bounds']=bounds
    value['token_evidence'].update(method_id='APPROVED_FIXED_INPUT_UPPER_BOUNDS_V1',implementation_sha256=sha256(Path(c.__file__).read_bytes()).hexdigest(),data_sha256=sha256(json.dumps(bounds,sort_keys=True,separators=(',',':')).encode()).hexdigest())
    binding=c.LiveHostBinding(target,approved,None,target.runner_identity,e4)
    f['target_binding']=binding
    monkeypatch.setattr(d,'load_live_binding',lambda:binding)
    monkeypatch.setattr(d,'load_live_safety_artifact',lambda:safety_parse(value))
    credentials=[];calls=[]
    def credential(*args):
        credentials.append(True);return c.resolve_diagnostic_credential(*args)
    monkeypatch.setattr(d,'resolve_diagnostic_credential',credential)
    def send(request):
        calls.append(True)
        return httpx.Response(200,json={'embeddings':[{'values':[1.]+[0.]*767}],'usageMetadata':{'promptTokenCount':128}})
    monkeypatch.setattr(d,'_create_live_transport',lambda:httpx.MockTransport(send))
    return request,value,credentials,calls


def test_actual_shared_dispatch_runs_once_with_unchanged_origin(reviewed,monkeypatch):
    f=reviewed;request,value,credentials,calls=stage2(f,monkeypatch)
    result=d.run_live(request,live_opt_in=True)
    assert result.outcome=='SUCCESS' and result.provider_attempts==len(calls)==len(credentials)==1
    with sqlite3.connect(str(f['path'])) as db:
        assert db.execute('SELECT '+','.join(FIELDS)+' FROM attempts WHERE campaign_id=?',(f['origin_row'][0],)).fetchone()==f['origin_row']
    with pytest.raises(c.DiagnosticDenied):d.run_live(request,live_opt_in=True)
    assert len(calls)==len(credentials)==1


def test_actual_final_guard_checks_row_after_credential(reviewed,monkeypatch):
    f=reviewed;request,value,credentials,calls=stage2(f,monkeypatch)
    old=d.resolve_diagnostic_credential
    def changed(*args):
        result=old(*args)
        with sqlite3.connect(str(f['path'])) as db:db.execute('UPDATE attempts SET cost_reserved=2 WHERE campaign_id=?',(f['origin_row'][0],))
        return result
    monkeypatch.setattr(d,'resolve_diagnostic_credential',changed)
    result=d.run_live(request,live_opt_in=True)
    assert result.outcome!='SUCCESS' and not calls and credentials==[True]


def test_review_does_not_create_token_safety_evidence(reviewed,monkeypatch):
    f=reviewed;request,value,credentials,calls=stage2(f,monkeypatch)
    value.pop('accepted_input_token_bounds')
    with pytest.raises(c.DiagnosticDenied):d.run_live(request,live_opt_in=True)
    assert not calls and not credentials and len(f['rows']())==1


def test_current_campaign_readonly_preflight_reports_raw_and_blocking(reviewed,monkeypatch):
    f=reviewed;request,value,credentials,calls=stage2(f,monkeypatch)
    root=f['path'].parent;paths=c.FixedHostPaths(root,root/'authority',root/'campaign.json',root/'input.json',root,f['path'])
    binding=replace(f['target_binding'],paths=paths)
    info=f['path'].stat();identity=(info.st_dev,info.st_ino)
    monkeypatch.setattr(c,'_bootstrap_identity',lambda *_:paths)
    monkeypatch.setattr(c,'validate_host_paths',lambda *_a,**_k:{paths.attempts:SimpleNamespace(identity=identity)})
    monkeypatch.setattr(c,'load_live_binding',lambda:binding)
    monkeypatch.setattr(c,'load_live_safety_artifact',lambda:safety_parse(value))
    @contextmanager
    def guard(*_):yield
    monkeypatch.setattr(c,'_guard_db_path',guard)
    before=f['path'].read_bytes()
    result=c.inspect_local_counting_store(runner_sid=f['target'].runner_identity,source_revision=f['target'].source_revision,
        fresh=False,for_authorized_campaign=True)
    assert result['fences']==1 and result['blocking_fences']==0
    assert result['reviewed_query_success_fences']==1 and result['reviewed_historical_fences']==0
    assert f['path'].read_bytes()==before and not calls and not credentials


@pytest.mark.parametrize('field,value',[('target_account_reference','other-account'),('secret_revision','other-revision')])
def test_review_account_binding_checked_before_credentials(reviewed,monkeypatch,field,value):
    f=reviewed
    f['target']=replace(f['target'],query_count_success_review=replace(f['review'],**{field:value}))
    request,metadata,credentials,calls=stage2(f,monkeypatch)
    with pytest.raises(c.DiagnosticDenied):d.run_live(request,live_opt_in=True)
    assert not credentials and not calls
