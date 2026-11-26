"""Exact UNKNOWN classification; synthetic commitments/TEMP ledger only.

Production origin digest pins are asserted separately. Fixture-only replacement
of two digest pins permits tamper tests without accessing the real protected row.
No Human signature/authority is created by these test DTOs.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime,timezone
from hashlib import sha256
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json
import sqlite3
import pytest

from knowledge_index import gemini_diagnostic_controls as c, gemini_diagnostic as d
from test_knowledge_december_bounded_query import bounded
from test_knowledge_gemini_query_counting import query_counting,counting,offline_sdk
from test_knowledge_december_fence_policy import rows
from test_knowledge_gemini_diagnostic_controls import safety_parse


@pytest.fixture(autouse=True)
def sealed(offline_sdk):
    yield
    assert offline_sdk.blocked_calls==[]


def test_production_origin_pins_are_exact_human_readback():
    assert c._DECEMBER_STAGE1_CAMPAIGN=='dec17-s2-query-count-stage1-20261125-01'
    assert c._DECEMBER_STAGE1_SOURCE=='bb99ae92c6805b7322de19089ae05e967b026cc7'
    assert c._DECEMBER_STAGE1_ROW_SHA=='41240e39b7349d6afee2764f8f86f1856da541b7b63112fdde58485051169b83'
    assert c._DECEMBER_STAGE1_OBSERVATION_SHA=='6775aec6a40693f5e10ff11f6cdfaa9222e3305f594098590bcb819cc07d2a58'


@pytest.fixture
def reviewed_unknown(bounded,monkeypatch):
    f=bounded;r=f['target']
    obs=dict(operation=c.QUERY_COUNTING_OPERATION,campaign_id=c._DECEMBER_STAGE1_CAMPAIGN,
        attempt_id=c._DECEMBER_STAGE1_ATTEMPT,source_revision=c._DECEMBER_STAGE1_SOURCE,
        outcome='UNKNOWN',code='TRANSPORT_FAILURE',usage='UNKNOWN',observed_token_count=None,
        response_sha256=None,provider='google',model='gemini-embedding-2',api='v1',sdk='google-genai==2.25.0',
        body_sha256=c.QUERY_COUNTING_BODY,profile_identity=r.credential_profile_identity,
        profile_revision=r.credential_profile_revision,project_identity=r.project_identity,
        host_identity=r.authorized_host,runner_identity=r.runner_identity,query_binding=c.query_counting_binding(),
        recorded_at='2026-11-26T00:00:00+00:00')
    wire=json.dumps(obs,sort_keys=True,separators=(',',':'))
    row=(c._DECEMBER_STAGE1_CAMPAIGN,c._DECEMBER_STAGE1_ATTEMPT,'UNKNOWN',1,1,1,'UNKNOWN',wire)
    fields=('campaign_id','request_id','outcome','items','usage_reserved','cost_reserved','usage_observed','observation_json')
    rowhash=c._p0_commitment(dict(zip(fields,row)));obshash=sha256(wire.encode()).hexdigest()
    monkeypatch.setattr(c,'_DECEMBER_STAGE1_ROW_SHA',rowhash)
    monkeypatch.setattr(c,'_DECEMBER_STAGE1_OBSERVATION_SHA',obshash)
    with sqlite3.connect(f['path']) as db:
        db.execute('INSERT INTO attempts('+','.join(fields)+') VALUES(?,?,?,?,?,?,?,?)',row)
    review=c.DecemberStage1UnknownReview('OFFLINE-EXACT-UNKNOWN-REVIEW',r.spec_lead_identity,
        datetime.now(timezone.utc).isoformat(),r.december_demo_fence_review.expires_at,
        'NON_BLOCKING_FOR_THIS_CAMPAIGN_ONLY',row[0],row[1],c._DECEMBER_STAGE1_SOURCE,
        rowhash,obshash,r.campaign_id,r.pm_approval_reference,r.source_revision)
    target=replace(r,december_demo_fence_review=replace(r.december_demo_fence_review,stage1_unknown_review=review))
    f['target']=target;f['unknown_review']=review;f['unknown_row']=row
    digest=c.authorization_record_digest(target)
    f['target_binding']=replace(f['target_binding'],record=target,
        sdk_byte_evidence=replace(f['target_binding'].sdk_byte_evidence,authorization_record_sha256=digest))
    f['risk_value']['binding']['authorization_record_sha256']=digest
    monkeypatch.setattr(d,'load_live_binding',lambda:f['target_binding'])
    monkeypatch.setattr(d,'load_live_safety_artifact',lambda:safety_parse(f['risk_value']))
    return f


def blocking(f,record=None):
    with sqlite3.connect(f['path']) as db:
        return c._blocking_fence_count(db,record or f['target'])


def test_exact_review_readonly_raw_three_reviewed_three_remaining_zero(reviewed_unknown):
    f=reviewed_unknown;before=f['path'].read_bytes();before_rows=rows(f)
    assert blocking(f)==0
    with sqlite3.connect(f['path']) as db:assert c._blocking_fence_count(db)==3
    assert rows(f)==before_rows and f['path'].read_bytes()==before
    without=replace(f['target'],december_demo_fence_review=replace(f['target'].december_demo_fence_review,stage1_unknown_review=None))
    assert blocking(f,without)==1


@pytest.mark.parametrize('field,value',[
    ('review_reference','PENDING_REVIEW'),('reviewer_identity','wrong'),
    ('recorded_at','2099-01-01T00:00:00+00:00'),('expires_at','2000-01-01T00:00:00+00:00'),
    ('disposition','GLOBAL_ALLOW'),('origin_campaign_id','other'),('origin_request_id','other'),
    ('origin_source_revision','f'*40),('row_sha256','f'*64),('observation_sha256','f'*64),
    ('target_campaign_id','other'),('target_pm_approval_reference','other'),('target_source_revision','f'*40),
])
def test_wrong_review_rejected_before_reservation(reviewed_unknown,field,value):
    f=reviewed_unknown;before=rows(f)
    with pytest.raises(c.DiagnosticDenied):
        replace(f['target'],december_demo_fence_review=replace(f['target'].december_demo_fence_review,
            stage1_unknown_review=replace(f['unknown_review'],**{field:value})))
    assert rows(f)==before and not f['risk_credentials'] and not f['risk_calls']


@pytest.mark.parametrize('field,value',[
    ('outcome','SUCCESS'),('outcome','TIMEOUT'),('outcome','IN_FLIGHT'),('usage_reserved',2),
    ('cost_reserved',2),('usage_observed','0'),('observation_json','{}'),('request_id','changed')])
def test_changed_origin_does_not_classify(reviewed_unknown,field,value):
    f=reviewed_unknown
    with sqlite3.connect(f['path']) as db:
        db.execute('UPDATE attempts SET '+field+'=? WHERE campaign_id=?',(value,c._DECEMBER_STAGE1_CAMPAIGN))
    with pytest.raises(c.DiagnosticDenied):blocking(f)
    assert not f['risk_calls']


@pytest.mark.parametrize('outcome',['UNKNOWN','TIMEOUT','IN_FLIGHT','SUCCESS'])
def test_other_fence_still_blocks(reviewed_unknown,outcome):
    f=reviewed_unknown
    with sqlite3.connect(f['path']) as db:
        db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved) VALUES('other','x',?,1,1,1)",(outcome,))
    assert blocking(f)==1
    before=rows(f)
    with pytest.raises(c.DiagnosticDenied):f['store'].reserve(f['target'],'new',items=1,usage_units=8192,cost_micro_units=1)
    assert rows(f)==before


def test_other_observed_usage_over_reservation_still_blocks(reviewed_unknown):
    f=reviewed_unknown
    with sqlite3.connect(f['path']) as db:
        db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved,usage_observed) VALUES('other','excess','SUCCESS',1,1,1,'2')")
    assert blocking(f)==1
    with pytest.raises(c.DiagnosticDenied):f['store'].reserve(f['target'],'new',items=1,usage_units=8192,cost_micro_units=1)


def test_duplicate_review_key_never_subtracted_twice(reviewed_unknown,monkeypatch):
    f=reviewed_unknown;first=f['target'].december_demo_fence_review.historical_reviews[0]
    # Synthetic mutation to exercise the existing set/duplicate fence. The
    # production fixed Stage1 identity can never name either historical P0.
    monkeypatch.setattr(c,'_DECEMBER_STAGE1_CAMPAIGN',first.historical_campaign_id)
    monkeypatch.setattr(c,'_DECEMBER_STAGE1_ATTEMPT',first.historical_request_id)
    rv=replace(f['unknown_review'],origin_campaign_id=first.historical_campaign_id,
        origin_request_id=first.historical_request_id)
    changed=replace(f['target'],december_demo_fence_review=replace(f['target'].december_demo_fence_review,stage1_unknown_review=rv))
    with pytest.raises(c.DiagnosticDenied,match='duplicate December origin row'):blocking(f,changed)


def test_native_embedding_one_mock_call_preserves_all_original_rows(reviewed_unknown):
    f=reviewed_unknown;before=rows(f)
    result=d.run_live(f['request'],live_opt_in=True)
    assert result.outcome=='SUCCESS' and result.provider_attempts==1
    assert all(row in rows(f) for row in before)
    assert len(f['risk_calls'])==len(f['risk_credentials'])==1 and f['calls']==[]
    with pytest.raises(c.DiagnosticDenied):d.run_live(f['request'],live_opt_in=True)
    assert len(f['risk_calls'])==1


def test_final_guard_rechecks_same_origin_in_native_transaction(reviewed_unknown):
    f=reviewed_unknown;r=f['target']
    f['store'].reserve(r,'new',items=1,usage_units=8192,cost_micro_units=1)
    f['store'].revalidate_december_review(r,'new',items=1,usage_units=8192,cost_micro_units=1)
    with sqlite3.connect(f['path']) as db:
        db.execute('UPDATE attempts SET cost_reserved=2 WHERE campaign_id=?',(c._DECEMBER_STAGE1_CAMPAIGN,))
    with pytest.raises(c.DiagnosticDenied):f['store'].revalidate_december_review(r,'new',items=1,usage_units=8192,cost_micro_units=1)


def test_only_one_competing_reservation_and_original_holds_unchanged(reviewed_unknown):
    f=reviewed_unknown;before=rows(f);gate=Barrier(2)
    def reserve(i):
        gate.wait(timeout=5)
        try:f['store'].reserve(f['target'],str(i),items=1,usage_units=8192,cost_micro_units=1);return True
        except c.DiagnosticDenied:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert sum(pool.map(reserve,[1,2]))==1
    assert all(row in rows(f) for row in before)


@pytest.mark.parametrize('field,value',[
    ('credential_profile_identity','different-profile'),('credential_profile_revision','rev-2'),
    ('project_identity','different-project'),('authorized_host','different-host'),('runner_identity','different-runner')])
def test_even_coherently_rebound_target_cannot_change_origin_context(reviewed_unknown,field,value):
    f=reviewed_unknown;r=f['target'];parent=r.december_demo_fence_review
    names={'authorized_host':'target_host_identity','runner_identity':'target_runner_identity'}
    parent=replace(parent,**{names.get(field,field):value})
    changed=replace(r,**{field:value,'december_demo_fence_review':parent})
    with pytest.raises(c.DiagnosticDenied):blocking(f,changed)


def test_budget_and_expired_review_final_guard_still_stop(reviewed_unknown):
    f=reviewed_unknown;r=f['target'];before=rows(f)
    with pytest.raises(c.DiagnosticDenied):f['store'].reserve(r,'excess',items=1,usage_units=8193,cost_micro_units=1)
    assert rows(f)==before
    f['store'].reserve(r,'new',items=1,usage_units=8192,cost_micro_units=1)
    object.__setattr__(f['unknown_review'],'expires_at','2000-01-01T00:00:00+00:00')
    with pytest.raises(c.DiagnosticDenied):f['store'].revalidate_december_review(r,'new',items=1,usage_units=8192,cost_micro_units=1)


def test_success_review_and_unknown_review_cannot_be_composed(reviewed_unknown):
    f=reviewed_unknown
    with pytest.raises(c.DiagnosticDenied):replace(f['target'],december_demo_fence_review=replace(
        f['target'].december_demo_fence_review,counting_success_review=True))


def test_origin_observation_semantics_fail_even_with_synthetic_rehashed_pins(reviewed_unknown,monkeypatch):
    f=reviewed_unknown;row=list(f['unknown_row']);v=json.loads(row[7]);v['code']='COUNTING_RESULT_OBTAINED'
    row[7]=json.dumps(v);fields=('campaign_id','request_id','outcome','items','usage_reserved','cost_reserved','usage_observed','observation_json')
    rh=c._p0_commitment(dict(zip(fields,row)));oh=sha256(row[7].encode()).hexdigest()
    monkeypatch.setattr(c,'_DECEMBER_STAGE1_ROW_SHA',rh);monkeypatch.setattr(c,'_DECEMBER_STAGE1_OBSERVATION_SHA',oh)
    rv=replace(f['unknown_review'],row_sha256=rh,observation_sha256=oh)
    with pytest.raises(c.DiagnosticDenied):rv.matches_row(f['target'],tuple(row))


def test_optional_wire_and_exact_legacy_composite_digest(bounded,reviewed_unknown):
    f=reviewed_unknown;wire=asdict(f['target'])
    assert c.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())==f['target']
    without=replace(f['target'],december_demo_fence_review=replace(f['target'].december_demo_fence_review,stage1_unknown_review=None))
    old=asdict(without);old['december_demo_fence_review'].pop('stage1_unknown_review')
    assert c.DiagnosticAuthorizationRecord.from_json(json.dumps(old).encode())==without
    for key in ('project_identity','credential_profile_revision','p0_unknown_review','p0_two_unknown_reviews','p0_full54_history_scope','full54_bounded_risk','query_count_success_review','december_demo_fence_review'):
        if old.get(key) is None:old.pop(key,None)
    assert c.authorization_record_digest(without)==sha256(json.dumps(old,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
    assert c.authorization_record_digest(without)!=c.authorization_record_digest(f['target'])


@pytest.mark.parametrize('change',['missing','extra','wrong_type'])
def test_unknown_review_closed_wire(reviewed_unknown,change):
    wire=asdict(reviewed_unknown['target']);v=wire['december_demo_fence_review']['stage1_unknown_review']
    if change=='missing':v.pop('row_sha256')
    elif change=='extra':v['allow_all']=True
    else:wire['december_demo_fence_review']['stage1_unknown_review']=True
    with pytest.raises(c.DiagnosticDenied):c.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())
