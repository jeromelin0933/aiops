"""One reviewed case only. Synthetic target approvals/key/transport, temporary SQLite.

Copies only the non-secret reviewed row facts. Never reads formal history or
publishes a review/live grant. Production reservation/completion stay in use.
"""
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

import httpx
import pytest

from knowledge_index import gemini_diagnostic as d
from knowledge_index import gemini_diagnostic_controls as c
from test_knowledge_gemini_diagnostic_single_probe import probe
from test_knowledge_gemini_embedding2_serialization import offline_sdk
from test_knowledge_gemini_diagnostic_controls import safety_parse
from test_knowledge_gemini_diagnostic_local_host import host, SID, REVISION


def review_for(record):
    return c.P0UnknownAttemptReview(c._P0_UNBLOCK_AUTHORITY,
        'SPECLEAD-SPEC014-D1-UNKNOWN-REVIEW-20261030-01', record.spec_lead_identity,
        datetime.now(timezone.utc).isoformat(), c._P0_HISTORY_ROW['campaign_id'],
        c._P0_HISTORY_ROW['request_id'], c._P0_HISTORY_ROW_SHA256,
        c._P0_HISTORY_EVIDENCE_SHA256, record.campaign_id, record.pm_approval_reference)


def install_target(p, monkeypatch, *, runner=None, paths=None):
    target=replace(p['grant'],campaign_id='synthetic-independently-authorized-p0',
        pm_approval_reference='synthetic-separate-pm-live-grant',
        runner_identity=runner or p['grant'].runner_identity,
        source_revision=REVISION if paths is not None else p['grant'].source_revision,
        authorized_host='LAPTOP-C5CUESSG' if paths is not None else p['grant'].authorized_host)
    target=replace(target,p0_unknown_review=review_for(target))
    approved=replace(p['approved'],campaign_id=target.campaign_id,source_revision=target.source_revision)
    e4=replace(p['e4'],campaign_id=target.campaign_id,pm_approval_reference=target.pm_approval_reference,
               source_revision=target.source_revision,
               authorization_record_sha256=c.authorization_record_digest(target))
    p['value']['binding'].update(campaign_id=target.campaign_id,source_revision=target.source_revision,
        authorization_record_sha256=c.authorization_record_digest(target))
    p['value']['free_tier_evidence']['data_use_permission']['pm_approval_reference']=target.pm_approval_reference
    binding=c.LiveHostBinding(target,approved,paths,target.runner_identity,e4)
    monkeypatch.setattr(d,'load_live_binding',lambda:binding)
    p.update(target=target,binding=binding)
    return target


def insert_history(usage, **changes):
    row=dict(c._P0_HISTORY_ROW);row.update(changes)
    with closing(usage._connect()) as db:
        db.execute('INSERT INTO attempts('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        db.commit()


def history(usage):
    with closing(usage._connect(read_only=True)) as db:
        return db.execute('SELECT * FROM attempts WHERE campaign_id=? AND request_id=?',
            (c._P0_HISTORY_ROW['campaign_id'],c._P0_HISTORY_ROW['request_id'])).fetchone()


@pytest.fixture
def reviewed(probe,monkeypatch):
    p=probe
    install_target(p,monkeypatch)
    insert_history(p['usage'])
    p['before']=history(p['usage'])
    p['calls'].clear();p['credentials'].clear()
    def credentials(record,artifact):
        with closing(p['usage']._connect(read_only=True)) as db:
            assert db.execute('SELECT outcome FROM attempts WHERE campaign_id=?',(record.campaign_id,)).fetchone()==('IN_FLIGHT',)
        p['credentials'].append(True)
        return c.resolve_diagnostic_credential(record,artifact)
    monkeypatch.setattr(d,'resolve_diagnostic_credential',credentials)
    def transport(mode='success'):
        def send(request):
            p['calls'].append(True)
            assert request.content==p['body']
            if mode=='unknown':raise httpx.ConnectError('synthetic-only')
            if mode=='timeout':raise httpx.ReadTimeout('synthetic-only')
            return httpx.Response(200,json={'embeddings':[{'values':[1.]+[0.]*767}],
                                          'usageMetadata':{'promptTokenCount':42}})
        monkeypatch.setattr(d,'_create_live_transport',lambda:httpx.MockTransport(send))
    transport()
    p['transport']=transport
    yield p
    assert history(p['usage'])==p['before']  # Original row NEVER changes, including holds/null evidence.


def test_reviewed_exact_case_allows_only_new_mock_campaign(reviewed):
    p=reviewed
    wire=json.dumps(asdict(p['target'])).encode()
    assert c.DiagnosticAuthorizationRecord.from_json(wire)==p['target']
    result=d.run_live(p['request'],live_opt_in=True)
    assert result.outcome=='SUCCESS' and result.usage=='42' and result.provider_attempts==1
    assert len(p['calls'])==len(p['credentials'])==1
    with closing(p['usage']._connect(read_only=True)) as db:
        assert c._blocking_fence_count(db)==1
        assert c._blocking_fence_count(db,p['target'])==0
        assert db.execute('SELECT SUM(usage_reserved),SUM(cost_reserved) FROM attempts').fetchone()==(16384,2)
    with pytest.raises(c.DiagnosticDenied) as denied:d.run_live(p['request'],live_opt_in=True)
    assert denied.value.code=='BUDGET_EXHAUSTED'
    assert len(p['calls'])==1  # No refund, no reuse of old budget, no retry.


@pytest.mark.parametrize('outcome',['UNKNOWN','TIMEOUT','IN_FLIGHT'])
def test_other_unresolved_attempts_still_block(reviewed,outcome):
    p=reviewed
    insert_history(p['usage'],campaign_id='another-campaign',request_id='other-request',outcome=outcome)
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert p['calls']==p['credentials']==[]


@pytest.mark.parametrize('mode',['unknown','timeout'])
def test_followup_ambiguity_is_not_covered_by_historical_exception(reviewed,mode):
    p=reviewed;p['transport'](mode)
    result=d.run_live(p['request'],live_opt_in=True)
    assert result.outcome==mode.upper()
    with pytest.raises(c.DiagnosticDenied) as denied:d.run_live(p['request'],live_opt_in=True)
    assert denied.value.code=='RUNNER_CONFLICT'
    assert len(p['calls'])==len(p['credentials'])==1


@pytest.mark.parametrize('field,value',[
    ('authority_reference','wrong-pm'),('historical_request_id','wrong-request'),
    ('historical_campaign_id','wrong-history'),('historical_row_sha256','0'*64),
    ('historical_evidence_sha256','0'*64),('target_campaign_id','wrong-target'),
    ('target_pm_approval_reference','wrong-live-pm'),('reviewer_identity','wrong-reviewer'),
    ('recorded_at','2099-01-01T00:00:00+00:00'),('recorded_at','2000-01-01T00:00:00+00:00'),
    ('review_reference',''),('review_reference','UNRELATED-NONEMPTY-REVIEW'),
])
def test_wrong_review_binding_rejected_before_admission(reviewed,field,value):
    p=reviewed
    with pytest.raises(c.DiagnosticDenied):
        replace(p['target'],p0_unknown_review=replace(p['target'].p0_unknown_review,**{field:value}))
    assert p['calls']==p['credentials']==[]


@pytest.mark.parametrize('reference',['','UNRELATED-NONEMPTY-REVIEW'])
def test_wrong_review_reference_rejected_by_runtime_before_reservation(reviewed,monkeypatch,reference):
    p=reviewed
    # Deliberately bypass construction validation to prove actual admission
    # revalidates the reference. Keep all other grant/E4/safety bindings valid.
    target=replace(p['target'])
    wrong_review=replace(target.p0_unknown_review,review_reference=reference)
    object.__setattr__(target,'p0_unknown_review',wrong_review)
    digest=c.authorization_record_digest(target)
    e4=replace(p['binding'].sdk_byte_evidence,authorization_record_sha256=digest)
    p['value']['binding']['authorization_record_sha256']=digest
    monkeypatch.setattr(d,'load_live_binding',lambda:replace(p['binding'],record=target,sdk_byte_evidence=e4))
    with closing(p['usage']._connect(read_only=True)) as db:
        before=db.execute('SELECT * FROM attempts').fetchall()
    with pytest.raises(c.DiagnosticDenied) as denied:
        d.run_live(p['request'],live_opt_in=True)
    assert denied.value.code=='HISTORICAL_REVIEW_MISMATCH'
    assert p['calls']==p['credentials']==[]
    with closing(p['usage']._connect(read_only=True)) as db:
        assert db.execute('SELECT * FROM attempts').fetchall()==before


@pytest.mark.parametrize('field,value', [('usage_reserved',8191),('cost_reserved',2),
    ('outcome','TIMEOUT'),('observation_json','{}'),('usage_observed','0')])
def test_changed_historical_facts_are_not_exempt(probe,monkeypatch,field,value):
    p=probe;install_target(p,monkeypatch);insert_history(p['usage'],**{field:value})
    with pytest.raises(c.DiagnosticDenied) as denied:
        p['usage'].reserve(p['target'],'new-request',items=1,usage_units=8192,cost_micro_units=1)
    assert denied.value.code=='HISTORICAL_REVIEW_MISMATCH'
    assert p['calls']==p['credentials']==[]


def test_missing_review_and_missing_opt_in_still_deny(reviewed,monkeypatch):
    p=reviewed
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=False)
    target=replace(p['target'],p0_unknown_review=None)
    monkeypatch.setattr(d,'load_live_binding',lambda:replace(p['binding'],record=target))
    # Supply consistent non-exception metadata to reach the same global SQL fence.
    e4=replace(p['binding'].sdk_byte_evidence,authorization_record_sha256=c.authorization_record_digest(target))
    p['value']['binding']['authorization_record_sha256']=c.authorization_record_digest(target)
    monkeypatch.setattr(d,'load_live_binding',lambda:replace(p['binding'],record=target,sdk_byte_evidence=e4))
    with pytest.raises(c.DiagnosticDenied) as denied:d.run_live(p['request'],live_opt_in=True)
    assert denied.value.code=='RUNNER_CONFLICT' and p['calls']==p['credentials']==[]


@pytest.mark.parametrize('gate',['source','credential','pm-preparation'])
def test_review_never_bypasses_other_gates(reviewed,monkeypatch,gate):
    p=reviewed
    if gate=='source':monkeypatch.setattr(d,'resolve_repository_source',lambda _:('f'*40,True))
    elif gate=='credential':monkeypatch.setitem(c.os.environ,'GEMINI_CREDENTIAL_PROFILE','wrong-profile')
    else:
        with pytest.raises(c.DiagnosticDenied):replace(p['target'],pm_approval_reference=c._P0_UNBLOCK_AUTHORITY)
        assert p['calls']==[]
        return
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert p['calls']==[]


def test_legacy_digest_and_closed_review_json_schema(probe):
    r=probe['grant']; old=asdict(r);old.pop('p0_unknown_review');old.pop('p0_two_unknown_reviews')
    for field in ('project_identity','credential_profile_revision'):
        if old[field] is None:old.pop(field)
    expected=hashlib.sha256(json.dumps(old,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
    assert c.authorization_record_digest(r)==expected
    legacy_wire=asdict(r);legacy_wire.pop('p0_unknown_review')
    assert c.DiagnosticAuthorizationRecord.from_json(json.dumps(legacy_wire).encode())==r
    wire=asdict(r);wire['p0_unknown_review']={'approved':True}
    with pytest.raises(c.DiagnosticDenied):c.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())


def test_preflight_keeps_raw_fence_and_matches_atomic_eligibility(probe,host,monkeypatch):
    p=probe;paths,_,_=host
    c.initialize_local_counting_store(runner_sid=SID,source_revision=REVISION)  # Isolated tmp_path only.
    p['usage']._path=paths.attempts
    insert_history(p['usage'])
    install_target(p,monkeypatch,runner=SID,paths=paths)
    monkeypatch.setattr(c,'load_live_binding',lambda:p['binding'])
    monkeypatch.setattr(c,'load_live_safety_artifact',lambda:safety_parse(p['value']))
    before=paths.attempts.read_bytes()
    state=c.inspect_local_counting_store(runner_sid=SID,source_revision=REVISION,
                                         for_authorized_campaign=True)
    assert (state['fences'],state['blocking_fences'],state['reviewed_historical_fences'])==(1,0,1)
    assert paths.attempts.read_bytes()==before
    p['usage'].reserve(p['target'],'new-request',items=1,usage_units=8192,cost_micro_units=1)
    state=c.inspect_local_counting_store(runner_sid=SID,source_revision=REVISION,
                                         for_authorized_campaign=True)
    assert (state['fences'],state['blocking_fences'])==(2,1)


def bind_two_target(p, target, monkeypatch):
    digest=c.authorization_record_digest(target)
    e4=replace(p['binding'].sdk_byte_evidence,authorization_record_sha256=digest)
    p['value']['binding']['authorization_record_sha256']=digest
    p['binding']=replace(p['binding'],record=target,sdk_byte_evidence=e4)
    p['target']=target
    monkeypatch.setattr(d,'load_live_binding',lambda:p['binding'])


@pytest.fixture
def two_reviewed(reviewed,monkeypatch):
    p=reviewed
    # Accepted historical facts only; target approval/key/transport are synthetic.
    insert_history(p['usage'],**dict(c._P0_SECOND_HISTORY_ROW))
    first=p['target'].p0_unknown_review
    second=c.P0SecondUnknownAttemptReview(c._P0_UNBLOCK_AUTHORITY,c._P0_SECOND_HUMAN_REVIEW_REFERENCE,
        c._P0_SECOND_REVIEWER,c._P0_SECOND_RECORDED_AT,
        c._P0_SECOND_HISTORY_ROW['campaign_id'],c._P0_SECOND_HISTORY_ROW['request_id'],
        c._P0_SECOND_HISTORY_ROW_SHA256,c._P0_SECOND_HISTORY_EVIDENCE_SHA256,
        first.target_campaign_id,first.target_pm_approval_reference)
    target=replace(p['target'],p0_unknown_review=None,p0_two_unknown_reviews=(first,second))
    bind_two_target(p,target,monkeypatch)
    yield p
    with closing(p['usage']._connect(read_only=True)) as db:
        assert db.execute('SELECT * FROM attempts WHERE campaign_id=?',
            (c._P0_SECOND_HISTORY_ROW['campaign_id'],)).fetchone()==tuple(c._P0_SECOND_HISTORY_ROW.values())


def test_two_exact_reviews_preserve_raw_history_and_admit_independent_mock(two_reviewed):
    p=two_reviewed;r=p['target']
    assert c.DiagnosticAuthorizationRecord.from_json(json.dumps(asdict(r)).encode())==r
    with closing(p['usage']._connect(read_only=True)) as db:
        assert (c._blocking_fence_count(db),c._blocking_fence_count(db,r))==(2,0)
        assert db.execute('SELECT COUNT(*),SUM(usage_reserved),SUM(cost_reserved) FROM attempts').fetchone()==(2,16384,2)
    result=d.run_live(p['request'],live_opt_in=True)
    assert result.outcome=='SUCCESS' and len(p['calls'])==len(p['credentials'])==1
    with closing(p['usage']._connect(read_only=True)) as db:
        assert db.execute('SELECT SUM(usage_reserved),SUM(cost_reserved) FROM attempts').fetchone()==(24576,3)
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert len(p['calls'])==1


def test_first_review_alone_does_not_exempt_second_case(reviewed):
    p=reviewed;insert_history(p['usage'],**dict(c._P0_SECOND_HISTORY_ROW))
    with pytest.raises(c.DiagnosticDenied) as denied:d.run_live(p['request'],live_opt_in=True)
    assert denied.value.code=='RUNNER_CONFLICT' and p['calls']==p['credentials']==[]


@pytest.mark.parametrize('field,value',[
    ('review_reference','UNRELATED-NONEMPTY-REVIEW'),('historical_request_id','wrong-attempt'),
    ('historical_campaign_id','wrong-campaign'),('historical_row_sha256','0'*64),
    ('historical_evidence_sha256','0'*64),('reviewer_identity','wrong-reviewer'),
    ('recorded_at','2026-10-30T12:00:00+08:00'),
    ('recorded_at','2026-10-31T12:58:55+08:00'),
    ('recorded_at','2099-01-01T00:00:00+00:00'),('recorded_at','2000-01-01T00:00:00+00:00'),
    ('target_campaign_id','wrong-target'),('target_pm_approval_reference','wrong-grant'),
])
def test_two_review_binding_is_rechecked_at_runtime(two_reviewed,monkeypatch,field,value):
    p=two_reviewed;r=replace(p['target'])
    first,second=r.p0_two_unknown_reviews
    # Bypass construction only, NEVER the classifier/reservation. Rebind the
    # digest so the independent runtime check must reject the bad review.
    object.__setattr__(r,'p0_two_unknown_reviews',(first,replace(second,**{field:value})))
    bind_two_target(p,r,monkeypatch)
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert p['calls']==p['credentials']==[]
    with closing(p['usage']._connect(read_only=True)) as db:
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()==(2,)


@pytest.mark.parametrize('outcome',['UNKNOWN','TIMEOUT','IN_FLIGHT'])
def test_two_reviews_never_exempt_other_unresolved_attempts(two_reviewed,outcome):
    p=two_reviewed;insert_history(p['usage'],campaign_id='third-history',request_id='third',outcome=outcome)
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert p['calls']==p['credentials']==[]


@pytest.mark.parametrize('mode',['unknown','timeout'])
def test_two_reviews_do_not_exempt_new_campaign_failure(two_reviewed,mode):
    p=two_reviewed;p['transport'](mode)
    assert d.run_live(p['request'],live_opt_in=True).outcome==mode.upper()
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert len(p['calls'])==len(p['credentials'])==1


@pytest.mark.parametrize('gate',['pending-human','no-opt-in','no-authority','source','e4','safety','budget'])
def test_two_review_never_bypasses_other_gates(two_reviewed,monkeypatch,gate):
    p=two_reviewed
    if gate=='pending-human':monkeypatch.setattr(c,'_P0_SECOND_HUMAN_REVIEW_REFERENCE',None)
    elif gate=='no-authority':monkeypatch.setattr(d,'load_live_binding',lambda:(_ for _ in ()).throw(c.DiagnosticDenied('no authority')))
    elif gate=='source':monkeypatch.setattr(d,'resolve_repository_source',lambda _:('0'*40,True))
    elif gate=='e4':p['binding']=replace(p['binding'],sdk_byte_evidence=replace(p['binding'].sdk_byte_evidence,authorization_record_sha256='0'*64))
    elif gate=='safety':p['value']['binding']['authorization_record_sha256']='0'*64
    elif gate=='budget':insert_history(p['usage'],campaign_id=p['target'].campaign_id,
        request_id='already-consumed',outcome='SUCCESS',usage_observed='1')
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=gate!='no-opt-in')
    assert p['calls']==p['credentials']==[]


def test_two_review_wire_is_closed_and_mutually_exclusive(two_reviewed):
    r=two_reviewed['target']
    for reviews in ([],[asdict(r.p0_two_unknown_reviews[0])],
                    [asdict(r.p0_two_unknown_reviews[1]),asdict(r.p0_two_unknown_reviews[0])]):
        wire=asdict(r);wire['p0_two_unknown_reviews']=reviews
        with pytest.raises(c.DiagnosticDenied):c.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())
    with pytest.raises(c.DiagnosticDenied):replace(r,p0_unknown_review=r.p0_two_unknown_reviews[0])


def test_two_review_preflight_uses_same_atomic_classification(two_reviewed,host,monkeypatch):
    p=two_reviewed;paths,_,_=host
    c.initialize_local_counting_store(runner_sid=SID,source_revision=REVISION) # tmp_path only.
    p['usage']._path=paths.attempts
    insert_history(p['usage']);insert_history(p['usage'],**dict(c._P0_SECOND_HISTORY_ROW))
    r=replace(p['target'],runner_identity=SID,authorized_host='LAPTOP-C5CUESSG',source_revision=REVISION)
    a=replace(p['binding'].approved_input,source_revision=REVISION)
    e=replace(p['binding'].sdk_byte_evidence,source_revision=REVISION,
              authorization_record_sha256=c.authorization_record_digest(r))
    p['value']['binding'].update(source_revision=REVISION,authorization_record_sha256=c.authorization_record_digest(r))
    monkeypatch.setattr(c,'load_live_binding',lambda:c.LiveHostBinding(r,a,paths,SID,e))
    monkeypatch.setattr(c,'load_live_safety_artifact',lambda:safety_parse(p['value']))
    before=paths.attempts.read_bytes()
    state=c.inspect_local_counting_store(runner_sid=SID,source_revision=REVISION,for_authorized_campaign=True)
    assert tuple(state[k] for k in ('attempts','fences','reviewed_historical_fences',
                                  'blocking_fences','reserved_usage','reserved_cost'))==(2,2,2,0,16384,2)
    assert paths.attempts.read_bytes()==before
    p['usage'].reserve(r,'new-request',items=1,usage_units=8192,cost_micro_units=1)
    state=c.inspect_local_counting_store(runner_sid=SID,source_revision=REVISION,for_authorized_campaign=True)
    assert (state['fences'],state['blocking_fences'])==(3,1)


def test_two_review_reservation_failure_still_precedes_credential(two_reviewed,monkeypatch):
    p=two_reviewed;connect=p['usage']._connect
    class CommitFailure:
        def __init__(self,db):self.db=db
        def __getattr__(self,name):return getattr(self.db,name)
        def commit(self):raise sqlite3.OperationalError('isolated reservation failure')
    monkeypatch.setattr(p['usage'],'_connect',lambda **kw:
        connect(**kw) if kw.get('read_only') else CommitFailure(connect(**kw)))
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert p['calls']==p['credentials']==[]
    with closing(connect(read_only=True)) as db:
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()==(2,)


def test_changed_second_row_is_not_exempt_or_repaired(two_reviewed):
    p=two_reviewed;campaign=c._P0_SECOND_HISTORY_ROW['campaign_id']
    with closing(p['usage']._connect()) as db:
        db.execute('UPDATE attempts SET cost_reserved=2 WHERE campaign_id=?',(campaign,));db.commit()
    try:
        with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
        assert p['calls']==p['credentials']==[]
        with closing(p['usage']._connect(read_only=True)) as db:
            assert db.execute('SELECT cost_reserved FROM attempts WHERE campaign_id=?',(campaign,)).fetchone()==(2,)
    finally:
        # Temporary fixture teardown only, never production repair/reset.
        with closing(p['usage']._connect()) as db:
            db.execute('UPDATE attempts SET cost_reserved=1 WHERE campaign_id=?',(campaign,));db.commit()
