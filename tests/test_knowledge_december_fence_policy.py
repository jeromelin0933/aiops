"""December composite admission: synthetic publications and temporary ledger only."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from contextlib import contextmanager
from types import SimpleNamespace
import json
import sqlite3

import httpx
import pytest

from knowledge_index import gemini_diagnostic as d, gemini_diagnostic_controls as c
from test_knowledge_query_count_success_review import (
    reviewed, query_counting, counting, offline_sdk, stage2, FIELDS,
)


@pytest.fixture(autouse=True)
def sealed(offline_sdk):
    yield
    assert offline_sdk.blocked_calls == []


def history():
    common = dict(authority_reference=c._P0_UNBLOCK_AUTHORITY,
        target_campaign_id='spec014-full54-demo-20261102-01',
        target_pm_approval_reference='PM-SPEC014-FULL54-DEMO-LIVE-20261102-01')
    return (
        # Literal accepted Human readback facts, independent of production pins.
        c.P0UnknownAttemptReview(**common,
            review_reference='SPECLEAD-SPEC014-D1-UNKNOWN-REVIEW-20261030-01',
            reviewer_identity='AIOps SPEC Lead', recorded_at='2026-10-30T16:38:47.616824+00:00',
            historical_campaign_id='spec014-direct-embed52-20261030-01',
            historical_request_id='cf02cd6e4ee94bd68f02dfbf99a75151',
            historical_row_sha256='59a710e59e43df7f7412e8a94eb2fee941c8ee11ae3ceb59fbcf2c3dbcd1ea55',
            historical_evidence_sha256='f4d15ad3ed22a21ada94c706ec1380c09c83b091f5015f94cca9882b3ed5f03e'),
        c.P0SecondUnknownAttemptReview(**common,
            review_reference='SPECLEAD-SPEC014-D2-UNKNOWN-REVIEW-20261031-01',
            reviewer_identity='SPEC Lead', recorded_at='2026-10-31T12:58:56+08:00',
            historical_campaign_id='spec014-direct-embed52-followup-20261031-01',
            historical_request_id='ad7716655be04e919daf4db6b79a1ec7',
            historical_row_sha256='3fa85e092ba49fe8ff48043bab0ee36658f3c5e0f10e899522e52dc3318e91ca',
            historical_evidence_sha256='0f796fe2c971c40c396b4b10d64208b027ed8ddb99cfc87565bd9e48ce205215'),
    )


def seed(f):
    with sqlite3.connect(str(f['path'])) as db:
        for row in (c._P0_HISTORY_ROW, c._P0_SECOND_HISTORY_ROW):
            db.execute('INSERT INTO attempts('+','.join(FIELDS)+') VALUES(?,?,?,?,?,?,?,?)',
                       tuple(row[k] for k in FIELDS))


def composite(f, target, success=None):
    project = f['grant'].project_identity
    expiry = '2026-12-17T23:59:00+08:00'
    if success is not None:
        success = replace(success, target_campaign_id=target.campaign_id,
            target_pm_approval_reference=target.pm_approval_reference,
            target_account_reference=f['value']['pricing_evidence']['account_reference'], expires_at=expiry,
            review_reference='OFFLINE-SUCCESS-REVIEW-'+target.campaign_id,
            recorded_at=datetime.now(timezone.utc).isoformat())
    return c.DecemberDemoFenceReview(
        policy_identity=c.DECEMBER_FENCE_POLICY,
        policy_approval_reference='OFFLINE-POLICY-REVIEW-NOT-LIVE',
        review_reference='OFFLINE-TARGET-REVIEW-'+target.campaign_id,
        reviewer_identity=target.spec_lead_identity,
        recorded_at=datetime.now(timezone.utc).isoformat(), expires_at=expiry,
        historical_publication_sha256=c._DECEMBER_HISTORICAL_PUBLICATION,
        historical_reviews=history(), target_campaign_id=target.campaign_id,
        target_pm_approval_reference=target.pm_approval_reference,
        target_source_revision=target.source_revision, target_input_commitment=target.input_commitment,
        target_host_identity=target.authorized_host, target_runner_identity=target.runner_identity,
        credential_profile_identity=target.credential_profile_identity,
        credential_profile_revision=f['grant'].credential_profile_revision,
        project_identity=project, account_reference=f['value']['pricing_evidence']['account_reference'],
        query_sha256=c.query_counting_binding()['canonical_query_sha256'],
        envelope_sha256=c.query_counting_binding()['envelope_sha256'],
        model_identity='gemini-embedding-2',
        embedding_profile_identity='spec014-gemini-embedding-2-embedding-v1',
        counting_success_review=success)


def embed_target(f, name='review-target', with_success=True):
    base = replace(f['target'], query_count_success_review=None, december_demo_fence_review=None,
                   project_identity=None, credential_profile_revision=None, campaign_id=name,
                   pm_approval_reference='OFFLINE-INDEPENDENT-GRANT-'+name)
    review = composite(f, base, f['review'] if with_success else None)
    return replace(base, december_demo_fence_review=review,
        project_identity=review.project_identity,
        credential_profile_revision=review.credential_profile_revision)


def blocking(f, target):
    with sqlite3.connect(str(f['path'])) as db:
        return c._blocking_fence_count(db, target)


def rows(f):
    with sqlite3.connect(str(f['path'])) as db:
        return db.execute('SELECT '+','.join(FIELDS)+' FROM attempts ORDER BY campaign_id,request_id').fetchall()


def publish_count(f, monkeypatch):
    review = composite(f, f['grant'])
    grant = replace(f['grant'], december_demo_fence_review=review)
    e4 = replace(f['e4'], authorization_record_sha256=c.authorization_record_digest(grant))
    f['value']['binding']['authorization_record_sha256'] = c.authorization_record_digest(grant)
    f.update(grant=grant, e4=e4, binding=replace(f['binding'], record=grant, sdk_byte_evidence=e4))
    # Reuse the real resolver against SYNTHETIC environment values only.
    monkeypatch.setattr(d, 'resolve_diagnostic_credential', c.resolve_diagnostic_credential)
    return grant


def publish_embed(f, monkeypatch):
    import test_knowledge_query_count_success_review as legacy
    from test_knowledge_gemini_diagnostic_controls import facts
    monkeypatch.setattr(legacy,'facts',lambda **kw: facts(
        pm_approval_reference=f['target'].pm_approval_reference,**kw))
    request, value, credentials, calls = stage2(f, monkeypatch)
    value['pricing_evidence']['account_reference'] = f['target'].december_demo_fence_review.account_reference
    return request, value, credentials, calls


def test_stage1_preserves_history_and_stops_after_exact_count(query_counting, monkeypatch):
    f=query_counting; seed(f); grant=publish_count(f, monkeypatch)
    before=rows(f)
    def send(request):
        f['calls'].append(request)
        return httpx.Response(200, json={'totalTokens':128})
    monkeypatch.setattr(d, '_create_live_transport', lambda:httpx.MockTransport(send))
    assert blocking(f, grant)==0
    result=d.run_query_counting_observation(live_opt_in=True)
    assert result.outcome=='SUCCESS' and len(f['calls'])==1
    assert all(row in rows(f) for row in before)
    assert blocking(f, grant)==1  # counting success remains a RAW usage-UNKNOWN fence
    with pytest.raises(c.DiagnosticDenied): d.run_query_counting_observation(live_opt_in=True)
    assert len(f['calls'])==1


def test_stage2_and_repeatable_campaign_reuse_same_success_without_recount(reviewed, monkeypatch):
    f=reviewed; seed(f); before=rows(f)
    for campaign in ('first-embedding', 'second-embedding'):
        f['target']=embed_target(f,campaign)
        assert blocking(f,f['target'])==0
        request,value,credentials,calls=publish_embed(f,monkeypatch)
        result=d.run_live(request,live_opt_in=True)
        assert result.outcome=='SUCCESS' and len(calls)==1
        assert all(row in rows(f) for row in before)
        with pytest.raises(c.DiagnosticDenied):d.run_live(request,live_opt_in=True)
        assert len(calls)==1
    assert len(f['calls'])==1  # only the original sealed counting fixture call
    with sqlite3.connect(str(f['path'])) as db:
        assert c._blocking_fence_count(db)==3
        assert db.execute('SELECT SUM(usage_reserved),SUM(cost_reserved) FROM attempts').fetchone()==(16985,5)


@pytest.mark.parametrize('field,value', [
    ('policy_identity','other'),('policy_approval_reference','PENDING'),
    ('target_campaign_id','other'),('target_pm_approval_reference','other'),
    ('target_source_revision','a'*40),('target_input_commitment','f'*64),
    ('target_host_identity','other'),('target_runner_identity','other'),
    ('credential_profile_identity','other'),('credential_profile_revision','other'),
    ('project_identity','other'),
    ('query_sha256','f'*64),('envelope_sha256','f'*64),
    ('model_identity','text-embedding-004'),('embedding_profile_identity','legacy'),
    ('reviewer_identity','other'),('expires_at','2026-11-24T00:00:00+00:00'),
    ('expires_at','2027-01-01T00:00:00+00:00'),('historical_publication_sha256','f'*64),
])
def test_target_tamper_fails_before_reservation(query_counting,field,value):
    f=query_counting; seed(f); before=rows(f)
    with pytest.raises(c.DiagnosticDenied):
        replace(f['grant'], december_demo_fence_review=replace(composite(f,f['grant']),**{field:value}))
    assert rows(f)==before and not f['calls'] and not f['credentials']


@pytest.mark.parametrize('field,value', [
    ('review_reference','wrong'),('historical_evidence_sha256','f'*64),
    ('historical_row_sha256','f'*64),('target_campaign_id','new-target'),
    ('target_pm_approval_reference','new-pm'),('reviewer_identity','other'),
    ('recorded_at','2099-01-01T00:00:00+00:00'),
])
def test_historical_review_tamper_not_retargeted(query_counting,field,value):
    f=query_counting; review=composite(f,f['grant'])
    changed=replace(review.historical_reviews[0],**{field:value})
    with pytest.raises(c.DiagnosticDenied):replace(f['grant'],december_demo_fence_review=replace(review,
        historical_reviews=(changed,review.historical_reviews[1])))


def test_duplicate_history_and_missing_rows_never_subtracted(query_counting):
    f=query_counting; review=composite(f,f['grant'])
    with pytest.raises(c.DiagnosticDenied):replace(f['grant'],december_demo_fence_review=replace(review,
        historical_reviews=(review.historical_reviews[0],review.historical_reviews[0])))
    target=replace(f['grant'],december_demo_fence_review=review)
    with pytest.raises(c.DiagnosticDenied):blocking(f,target)


@pytest.mark.parametrize('outcome,usage', [('UNKNOWN','UNKNOWN'),('TIMEOUT','UNKNOWN'),
    ('IN_FLIGHT','UNKNOWN'),('FAILED','UNKNOWN'),('SUCCESS','UNKNOWN'),('SUCCESS','101')])
def test_every_unreviewed_fence_blocks(query_counting,outcome,usage):
    f=query_counting; seed(f); target=replace(f['grant'],december_demo_fence_review=composite(f,f['grant']))
    with sqlite3.connect(str(f['path'])) as db:
        db.execute('INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved,usage_observed) VALUES(?,?,?,1,100,1,?)',('unreviewed','x',outcome,usage))
    assert blocking(f,target)==1
    with pytest.raises(c.DiagnosticDenied):f['store'].reserve(target,'new',items=1,usage_units=1,cost_micro_units=1)
    assert len(rows(f))==3


@pytest.mark.parametrize('change',['origin','new_fence','own_hold','own_observation','expiry'])
def test_final_count_guard_rejects_post_reservation_change(query_counting,monkeypatch,change):
    f=query_counting; seed(f); target=publish_count(f,monkeypatch)
    f['store'].reserve(target,'own',items=1,usage_units=1,cost_micro_units=1)
    f['store'].revalidate_december_review(target,'own',items=1,usage_units=1,cost_micro_units=1)
    if change=='expiry':
        class Later(datetime):
            @classmethod
            def now(cls,tz=None):return datetime(2026,12,18,tzinfo=timezone.utc)
        monkeypatch.setattr(c,'datetime',Later)
    else:
        with sqlite3.connect(str(f['path'])) as db:
            if change=='origin':db.execute('UPDATE attempts SET cost_reserved=2 WHERE campaign_id=?',(c._P0_HISTORY_ROW['campaign_id'],))
            elif change=='new_fence':db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved) VALUES('other','x','UNKNOWN',1,1,1)")
            elif change=='own_hold':db.execute('UPDATE attempts SET cost_reserved=2 WHERE request_id=?',('own',))
            else:db.execute('UPDATE attempts SET observation_json=NULL WHERE request_id=?',('own',))
    with pytest.raises(c.DiagnosticDenied):f['store'].revalidate_december_review(target,'own',items=1,usage_units=1,cost_micro_units=1)


def test_atomic_competitors_only_one_reservation(query_counting):
    f=query_counting; seed(f); target=replace(f['grant'],december_demo_fence_review=composite(f,f['grant']))
    gate=Barrier(2)
    def reserve(index):
        gate.wait(timeout=5)
        try:
            f['store'].reserve(target,str(index),items=1,usage_units=1,cost_micro_units=1)
            return True
        except c.DiagnosticDenied:return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve,[1,2]))==1
    assert len(rows(f))==3


def test_wire_digest_and_legacy_scope_preserved(query_counting):
    f=query_counting; target=replace(f['grant'],december_demo_fence_review=composite(f,f['grant']))
    wire=asdict(target)
    assert c.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())==target
    legacy=asdict(f['grant']);legacy.pop('december_demo_fence_review')
    assert c.DiagnosticAuthorizationRecord.from_json(json.dumps(legacy).encode())==f['grant']
    assert c.authorization_record_digest(target)!=c.authorization_record_digest(f['grant'])
    with pytest.raises(c.DiagnosticDenied):replace(f['grant'],p0_two_unknown_reviews=history())
    with pytest.raises(c.DiagnosticDenied):replace(target,p0_two_unknown_reviews=history())


@pytest.mark.parametrize('bad',[{},[],True,{'policy_identity':c.DECEMBER_FENCE_POLICY}])
def test_closed_wire_rejects_missing_or_extra(query_counting,bad):
    wire=asdict(query_counting['grant']);wire['december_demo_fence_review']=bad
    with pytest.raises(c.DiagnosticDenied):c.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())


def test_composite_does_not_grant_token_proof(reviewed,monkeypatch):
    f=reviewed; seed(f); f['target']=embed_target(f)
    request,value,credentials,calls=publish_embed(f,monkeypatch)
    value.pop('accepted_input_token_bounds')
    with pytest.raises(c.DiagnosticDenied):d.run_live(request,live_opt_in=True)
    assert not credentials and not calls and len(rows(f))==3


@pytest.mark.parametrize('change',['origin','new_fence','account'])
def test_actual_embedding_final_guard_and_safety(reviewed,monkeypatch,change):
    f=reviewed; seed(f); f['target']=embed_target(f)
    request,value,credentials,calls=publish_embed(f,monkeypatch)
    if change=='account':
        value['pricing_evidence']['account_reference']='other'
        with pytest.raises(c.DiagnosticDenied):d.run_live(request,live_opt_in=True)
        assert not credentials
    else:
        original=d.resolve_diagnostic_credential
        def mutate(*args):
            credential=original(*args)
            with sqlite3.connect(str(f['path'])) as db:
                if change=='origin':db.execute('UPDATE attempts SET cost_reserved=2 WHERE campaign_id=?',(c._P0_HISTORY_ROW['campaign_id'],))
                else:db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved) VALUES('other','x','UNKNOWN',1,1,1)")
            return credential
        monkeypatch.setattr(d,'resolve_diagnostic_credential',mutate)
        assert d.run_live(request,live_opt_in=True).outcome!='SUCCESS'
    assert not calls


@pytest.mark.parametrize('change',['origin','new_fence'])
def test_actual_count_final_guard_no_send_after_credential(query_counting,monkeypatch,change):
    f=query_counting; seed(f); publish_count(f,monkeypatch)
    def credential(*args):
        result=c.resolve_diagnostic_credential(*args)
        with sqlite3.connect(str(f['path'])) as db:
            if change=='origin':db.execute('UPDATE attempts SET cost_reserved=2 WHERE campaign_id=?',(c._P0_HISTORY_ROW['campaign_id'],))
            else:db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved) VALUES('other','x','UNKNOWN',1,1,1)")
        return result
    monkeypatch.setattr(d,'resolve_diagnostic_credential',credential)
    def send(request):
        f['calls'].append(request)
        raise AssertionError('transport must not be called')
    monkeypatch.setattr(d,'_create_live_transport',lambda:httpx.MockTransport(send))
    result=d.run_query_counting_observation(live_opt_in=True)
    assert result.outcome!='SUCCESS' and result.provider_attempts==0 and not f['calls']
    assert result.code=='DECEMBER_REVIEW_MISMATCH'
    assert all(row[6]=='UNKNOWN' for row in rows(f))
    assert any(row[2]=='FAILED' for row in rows(f))


def test_count_success_not_subtracted_without_its_review(reviewed):
    f=reviewed;seed(f); target=embed_target(f,with_success=False)
    assert blocking(f,target)==1
    with pytest.raises(c.DiagnosticDenied):f['store'].reserve(target,'own',items=1,usage_units=300,cost_micro_units=1)


@pytest.mark.parametrize('change',['response','usage','count_zero'])
def test_nested_count_success_row_tamper_rejected(reviewed,change):
    f=reviewed;seed(f);target=embed_target(f)
    with sqlite3.connect(str(f['path'])) as db:
        if change=='usage':db.execute('UPDATE attempts SET usage_observed=? WHERE campaign_id=?',('1',f['origin_row'][0]))
        else:
            observation=json.loads(f['origin_row'][-1])
            if change=='response':observation['response_sha256']='f'*64
            else:observation['observed_token_count']=0
            db.execute('UPDATE attempts SET observation_json=? WHERE campaign_id=?',(json.dumps(observation),f['origin_row'][0]))
    with pytest.raises(c.DiagnosticDenied):blocking(f,target)


def test_policy_and_implementation_authority_are_not_live_grants(query_counting):
    for reference in (c.DECEMBER_FENCE_POLICY,c._DECEMBER_IMPLEMENTATION_AUTHORITY):
        with pytest.raises(c.DiagnosticDenied):replace(query_counting['grant'],pm_approval_reference=reference)


@pytest.mark.parametrize('change',['extra','duplicate','historical_missing','success_incomplete'])
def test_nested_wire_is_closed(query_counting,change):
    f=query_counting;target=replace(f['grant'],december_demo_fence_review=composite(f,f['grant']))
    value=asdict(target); policy=value['december_demo_fence_review']
    if change=='extra':policy['allow_all_future_rows']=True
    elif change=='duplicate':policy['historical_reviews']=(policy['historical_reviews'][0],policy['historical_reviews'][0])
    elif change=='historical_missing':policy['historical_reviews'][0].pop('historical_evidence_sha256')
    else:policy['counting_success_review']={'review_reference':'missing'}
    with pytest.raises(c.DiagnosticDenied):c.DiagnosticAuthorizationRecord.from_json(json.dumps(value).encode())


def test_protected_preflight_reports_raw_reviewed_remaining_without_writes(reviewed,monkeypatch):
    f=reviewed;seed(f);f['target']=embed_target(f)
    request,value,credentials,calls=publish_embed(f,monkeypatch)
    root=f['path'].parent
    paths=c.FixedHostPaths(root,root/'authority',root/'campaign.json',root/'input.json',root,f['path'])
    binding=replace(f['target_binding'],paths=paths)
    info=f['path'].stat();identity=(info.st_dev,info.st_ino)
    monkeypatch.setattr(c,'_bootstrap_identity',lambda *_:paths)
    monkeypatch.setattr(c,'validate_host_paths',lambda *_a,**_k:{paths.attempts:SimpleNamespace(identity=identity)})
    monkeypatch.setattr(c,'load_live_binding',lambda:binding)
    from test_knowledge_gemini_diagnostic_controls import safety_parse
    monkeypatch.setattr(c,'load_live_safety_artifact',lambda:safety_parse(value))
    @contextmanager
    def guard(*_):yield
    monkeypatch.setattr(c,'_guard_db_path',guard)
    before=f['path'].read_bytes()
    result=c.inspect_local_counting_store(runner_sid=f['target'].runner_identity,
        source_revision=f['target'].source_revision,fresh=False,for_authorized_campaign=True)
    assert (result['fences'],result['reviewed_historical_fences'],
            result['reviewed_query_success_fences'],result['blocking_fences'])==(3,2,1,0)
    assert f['path'].read_bytes()==before and not credentials and not calls


def test_f01_real_historical_facts_admit_independent_target_without_rewriting(query_counting):
    f=query_counting; seed(f); before=rows(f)
    review=composite(f,f['grant'])
    target=replace(f['grant'],december_demo_fence_review=review)
    d1,d2=review.historical_reviews
    assert (d1.reviewer_identity,d1.recorded_at)==(
        'AIOps SPEC Lead','2026-10-30T16:38:47.616824+00:00')
    assert (d2.reviewer_identity,d2.recorded_at)==(
        'SPEC Lead','2026-10-31T12:58:56+08:00')
    assert review.reviewer_identity==target.spec_lead_identity
    assert review.reviewer_identity not in (d1.reviewer_identity,d2.reviewer_identity)
    assert blocking(f,target)==0 and rows(f)==before
    assert [c._p0_commitment(dict(zip(FIELDS,row))) for row in before]==[
        '59a710e59e43df7f7412e8a94eb2fee941c8ee11ae3ceb59fbcf2c3dbcd1ea55',
        '3fa85e092ba49fe8ff48043bab0ee36658f3c5e0f10e899522e52dc3318e91ca']
    f['store'].reserve(target,'own',items=1,usage_units=1,cost_micro_units=1)
    f['store'].revalidate_december_review(target,'own',items=1,usage_units=1,cost_micro_units=1)
    assert all(row in rows(f) for row in before)
    assert not f['credentials'] and not f['calls']


@pytest.mark.parametrize('index,field,value',[
    (0,'reviewer_identity','SPEC Lead'),
    (1,'reviewer_identity','AIOps SPEC Lead'),
    (0,'reviewer_identity','synthetic-lead'),
    (1,'reviewer_identity','synthetic-lead'),
    (0,'recorded_at','2026-10-30T16:38:47.616825+00:00'),
    (1,'recorded_at','2026-10-31T12:58:57+08:00'),
    (0,'review_reference','SPECLEAD-SPEC014-D2-UNKNOWN-REVIEW-20261031-01'),
    (1,'review_reference','SPECLEAD-SPEC014-D1-UNKNOWN-REVIEW-20261030-01'),
    (0,'historical_row_sha256','f'*64),
    (1,'historical_row_sha256','f'*64),
    (0,'historical_evidence_sha256','f'*64),
    (1,'historical_evidence_sha256','f'*64),
    (0,'target_campaign_id','new-target'),
    (1,'target_pm_approval_reference','new-pm'),
])
def test_f01_exact_historical_fields_cannot_be_swapped_or_overwritten(query_counting,index,field,value):
    f=query_counting; seed(f); before=rows(f);review=composite(f,f['grant'])
    altered=list(review.historical_reviews)
    altered[index]=replace(altered[index],**{field:value})
    with pytest.raises(c.DiagnosticDenied):
        replace(f['grant'],december_demo_fence_review=replace(review,historical_reviews=tuple(altered)))
    assert rows(f)==before and not f['credentials'] and not f['calls']


@pytest.mark.parametrize('field',['policy_approval_reference','review_reference'])
def test_approval_reference_tamper_breaks_existing_e4_safety_before_credentials(query_counting,monkeypatch,field):
    f=query_counting; seed(f); grant=publish_count(f,monkeypatch);before=rows(f)
    # A nonempty string is not approval proof: the original protected E4/Safety
    # digest must still bind it. No real publication or credential is loaded.
    changed=replace(grant,december_demo_fence_review=replace(
        grant.december_demo_fence_review,**{field:'unrelated-nonempty-reference'}))
    f['binding']=replace(f['binding'],record=changed)
    with pytest.raises(c.DiagnosticDenied):d.run_query_counting_observation(live_opt_in=True)
    assert rows(f)==before and not f['credentials'] and not f['calls']
