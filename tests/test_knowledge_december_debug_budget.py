"""Synthetic target grants and TEMP native ledger only; no host or provider."""
from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json
import sqlite3
import pytest
from knowledge_index import gemini_diagnostic_controls as c
from test_knowledge_december_bounded_query import bounded, query_counting, counting, offline_sdk
from test_knowledge_december_fence_policy import rows

@pytest.fixture(autouse=True)
def sealed(offline_sdk):
    yield
    assert offline_sdk.blocked_calls == []


def target(f, index=0, grant_reference=None, **changes):
    name=c.DECEMBER_DEBUG_TARGETS[index]
    with sqlite3.connect(str(f['path'])) as db:
        baseline=c._december_debug_baseline(db)
    base=f['target']; reference=grant_reference or 'OFFLINE-DELEGATED-SPECLEAD-'+str(index)
    if not reference.startswith(c.DECEMBER_DEBUG_GRANT_PREFIX):
        reference=c.DECEMBER_DEBUG_GRANT_PREFIX+name+'/'+reference
    review=replace(base.december_demo_fence_review,target_campaign_id=name,
        target_pm_approval_reference=reference)
    budget=c.DecemberDebugCallBudget(c.DECEMBER_DEBUG_BUDGET,
        c.DECEMBER_DEBUG_PM_REFERENCE,base.spec_lead_identity,baseline,8,20,3,
        c.DECEMBER_DEBUG_PM_SHA256)
    return replace(base,campaign_id=name,pm_approval_reference=reference,
        expires_at='2026-12-17T23:59:00+08:00',
        december_demo_fence_review=review,december_debug_call_budget=budget,**changes)


def reserve(f, record, request='one'):
    f['store'].reserve(record,request,items=1,usage_units=8192,cost_micro_units=1)


def seed_anchor(f, record, outcome='SUCCESS', usage='73'):
    # Synthetic offline result, never an invocation or write to host authority.
    with sqlite3.connect(str(f['path'])) as db:
        db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved,usage_observed,observation_json) VALUES(?, 'synthetic',?,1,8192,1,?,?)",
            (record.campaign_id,outcome,usage,json.dumps(c._debug_reservation_binding(record,'synthetic'))))


def test_atomic_reservation_full_three_slots_and_final_guard(bounded):
    f=bounded;r=target(f);before=rows(f)
    reserve(f,r)
    f['store'].revalidate_december_review(r,'one',items=1,usage_units=8192,cost_micro_units=1)
    with sqlite3.connect(str(f['path'])) as db:
        state=c._check_december_debug_budget(db,r,pending=False)
    assert state==dict(prior_occupancy=8,reserved_target_allocations=1,conservative_occupancy=11,cumulative_ceiling=20)
    assert all(row in rows(f) for row in before)
    assert rows(f)[0:] and len(rows(f))==len(before)+1
    with pytest.raises(c.DiagnosticDenied,match='consumed'):reserve(f,r,'two')


def test_two_target_allocations_never_refund_and_no_automatic_fifth(bounded):
    f=bounded;r=target(f);before=rows(f)
    seed_anchor(f,r)
    second=target(f,1);reserve(f,second)
    with sqlite3.connect(str(f['path'])) as db:
        state=c._check_december_debug_budget(db,second,pending=False)
    assert state['conservative_occupancy']==14<=20
    assert all(row in rows(f) for row in before)
    assert len(rows(f))==len(before)+2
    with pytest.raises(c.DiagnosticDenied):reserve(f,second,'again')


def test_target05_cannot_precede_target04(bounded):
    r=target(bounded,1);before=rows(bounded)
    with pytest.raises(c.DiagnosticDenied,match='cannot precede'):reserve(bounded,r)
    assert rows(bounded)==before


@pytest.mark.parametrize('field,value',[
    ('prior_occupancy',0),('prior_occupancy',True),('cumulative_ceiling',21),
    ('cumulative_ceiling',10),('target_allocation',4),('target_allocation',2),
    ('policy_identity','other'),('pm_decision_reference','PENDING'),
    ('delegated_issuer','wrong'),('baseline_ledger_sha256','invalid'),
])
def test_budget_or_delegation_tamper_denied(bounded,field,value):
    r=target(bounded)
    with pytest.raises(c.DiagnosticDenied):
        replace(r,december_debug_call_budget=replace(r.december_debug_call_budget,**{field:value}))


def test_target_cannot_drop_budget_or_switch_grant(bounded):
    r=target(bounded)
    with pytest.raises(c.DiagnosticDenied):replace(r,december_debug_call_budget=None)
    with pytest.raises(c.DiagnosticDenied):replace(r,pm_approval_reference='OTHER')
    with pytest.raises(c.DiagnosticDenied):replace(r,model_identity='text-embedding-004')
    with pytest.raises(c.DiagnosticDenied):replace(r,maximum_invocations=2)


@pytest.mark.parametrize('outcome,usage',[
    ('UNKNOWN','UNKNOWN'),('TIMEOUT','UNKNOWN'),('IN_FLIGHT','UNKNOWN'),('SUCCESS','9000')])
def test_other_new_fence_or_over_usage_still_denied(bounded,outcome,usage):
    f=bounded;r=target(f);before=rows(f)
    with sqlite3.connect(str(f['path'])) as db:
        db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved,usage_observed) VALUES('other-new','fence',?,1,1,1,?)",(outcome,usage))
    changed=rows(f)
    with pytest.raises(c.DiagnosticDenied,match='baseline changed'):reserve(f,r)
    assert rows(f)==changed and all(row in changed for row in before)


def test_unknown_target_hold_preserved_and_blocks_other_target(bounded):
    f=bounded;r=target(f);reserve(f,r);f['store'].finish(r,'one','UNKNOWN')
    before=rows(f);second=target(f,1)
    with pytest.raises(c.DiagnosticDenied):reserve(f,second)
    assert rows(f)==before
    with sqlite3.connect(str(f['path'])) as db:
        state=c._check_december_debug_budget(db,r,pending=False)
    assert state['conservative_occupancy']==11


def test_atomic_duplicate_concurrency(bounded):
    f=bounded;r=target(f);barrier=Barrier(2)
    def attempt(n):
        barrier.wait()
        try:reserve(f,r,'request-'+str(n));return True
        except c.DiagnosticDenied:return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(attempt,range(2)))
    assert sum(results)==1
    with sqlite3.connect(str(f['path'])) as db:
        assert db.execute('SELECT COUNT(*) FROM attempts WHERE campaign_id=?',(r.campaign_id,)).fetchone()[0]==1


def test_final_guard_baseline_tamper_rejected(bounded):
    f=bounded;r=target(f);reserve(f,r)
    with sqlite3.connect(str(f['path'])) as db:
        db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved,usage_observed) VALUES('new-outside','x','SUCCESS',1,1,1,'1')")
    with pytest.raises(c.DiagnosticDenied):
        f['store'].revalidate_december_review(r,'one',items=1,usage_units=8192,cost_micro_units=1)


def test_existing_history_commitment_changed_rejected(bounded):
    f=bounded;r=target(f)
    with sqlite3.connect(str(f['path'])) as db:
        db.execute("UPDATE attempts SET cost_reserved=cost_reserved+1 WHERE campaign_id=?",(c._P0_HISTORY_ROW['campaign_id'],))
    with pytest.raises(c.DiagnosticDenied,match='baseline changed'):reserve(f,r)


def test_native_readback_requires_committed_anchor(bounded,monkeypatch):
    f=bounded;r=target(f)
    monkeypatch.setattr(c,'LiveDiagnosticUsage',lambda:f['store'])
    state=c.inspect_december_execution_ledger(r,before_query=True,required_usage_units=8192)
    assert state['conservative_occupancy']==11
    with pytest.raises(c.DiagnosticDenied):c.inspect_december_execution_ledger(r,before_query=False,required_usage_units=8192)
    seed_anchor(f,r)
    assert c.inspect_december_execution_ledger(r,before_query=False,required_usage_units=8192)['conservative_occupancy']==11
    with pytest.raises(c.DiagnosticDenied):c.inspect_december_execution_ledger(r,before_query=True,required_usage_units=8192)


def test_parser_digest_and_legacy_compatibility(bounded):
    legacy=bounded['target'];old=asdict(legacy);old.pop('december_debug_call_budget')
    assert c.DiagnosticAuthorizationRecord.from_json(json.dumps(old).encode())==legacy
    assert c.authorization_record_digest(c.DiagnosticAuthorizationRecord.from_json(json.dumps(old).encode()))==c.authorization_record_digest(legacy)
    r=target(bounded);wire=asdict(r)
    assert c.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())==r
    assert c.authorization_record_digest(r)!=c.authorization_record_digest(legacy)
    wire['december_debug_call_budget']['extra']='unapproved'
    with pytest.raises(c.DiagnosticDenied):c.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())


def scope_for(f, record):
    from datetime import datetime, timedelta, timezone
    now=datetime.now(timezone.utc)
    return c.DecemberDemoExecutionScope(c.DECEMBER_EXECUTION_SCOPE,
        'APPROVE_DECEMBER_DEMO_EXECUTION','OFFLINE_FIXTURE',record.pm_approval_reference,
        record.spec_lead_identity,(now-timedelta(minutes=1)).isoformat(),record.expires_at,
        c.authorization_record_digest(record),record.source_revision,
        f['risk_value']['pricing_evidence']['account_reference'],
        'OFFLINE-EXTERNAL-ACCEPTANCE',record.spec_lead_identity,now.isoformat(),record.expires_at,
        record.campaign_id,'gemini-3.5-flash-lite',
        'demo-google-gemini35-flash-lite-generation-profile-v1','2','v1beta',
        (1,1,1),c.DECEMBER_OPERATION_SCOPES,tuple(
            c.ExternalDemoCommitment(str(c.DECEMBER_EXTERNAL_ROOT/name),'2','a'*64)
            for name in sorted(c.DECEMBER_EXTERNAL_NAMES)))


def test_delegated_grant_scope_and_three_operation_binding(bounded):
    r=target(bounded);scope=scope_for(bounded,r)
    scope.matches_record(r,bounded['risk_value'])
    wire=json.loads(json.dumps(asdict(scope)))
    assert c.DecemberDemoExecutionScope.from_mapping(wire)==scope


@pytest.mark.parametrize('field,value',[
    ('issuer_identity','wrong'),('grant_reference','OTHER-DELEGATED-GRANT'),
    ('repository_revision','f'*40),('run_id',c.DECEMBER_DEBUG_TARGETS[1]),
    ('generation_model','other-model'),('generation_profile_version','3'),
    ('generation_profile_version','1'),
    ('scope_identity','UNRELATED-PARENT-SCOPE'),('decision','PREPARATION_ONLY'),
    ('physical_call_allocation',(0,1,1)),('expires_at','2000-01-01T00:00:00+00:00'),
])
def test_wrong_target_grant_source_model_expiry_denied(bounded,field,value):
    r=target(bounded);scope=replace(scope_for(bounded,r),**{field:value})
    with pytest.raises(c.DiagnosticDenied):scope.matches_record(r,bounded['risk_value'])


def test_usage_cost_budget_still_enforced(bounded):
    f=bounded;r=target(f);before=rows(f)
    with pytest.raises(c.DiagnosticDenied,match='budget'):
        reserve(f,replace(r,maximum_usage_units=8191))
    assert rows(f)==before


def test_duplicate_anchor_corruption_not_deducted_twice(bounded):
    f=bounded;r=target(f);seed_anchor(f,r)
    with sqlite3.connect(str(f['path'])) as db:
        db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved,usage_observed) VALUES(?, 'second','SUCCESS',1,8192,1,'73')",(r.campaign_id,))
        with pytest.raises(c.DiagnosticDenied,match='duplicate'):
            c._check_december_debug_budget(db,r,pending=False)


def test_new_target04_budget_guard_native_mock_pipeline(bounded,monkeypatch,tmp_path):
    """New budget scope through existing C/D/A/E with synthetic ports only."""
    from copy import deepcopy
    from hashlib import sha256
    from runtime_orchestration import december_demo_entry as entry
    from pathlib import Path
    from test_knowledge_query_count_success_review import stage2
    from test_knowledge_gemini_diagnostic_controls import facts,safety_parse
    from test_december_demo_protected_entry import test_real_port_branch_with_only_mock_transports_and_persistence
    import test_knowledge_query_count_success_review as helper
    fixture_root=tmp_path/'accepted-external-fixture'
    fixture_root.mkdir();(fixture_root/'runs').mkdir()
    for name in c.DECEMBER_EXTERNAL_NAMES:
        (fixture_root/name).write_bytes((c.DECEMBER_EXTERNAL_ROOT/name).read_bytes())
    monkeypatch.setattr(c,'DECEMBER_EXTERNAL_ROOT',fixture_root)
    f=bounded;r=target(f,grant_reference='UNIT-DELEGATED-TARGET04-NOT-REAL-AUTHORITY')
    f['target']=r
    monkeypatch.setattr(helper,'facts',lambda **kw:facts(pm_approval_reference=r.pm_approval_reference,**kw))
    request,legacy,credentials,calls=stage2(f,monkeypatch)
    value=deepcopy(f['risk_value']);value['binding']=legacy['binding']
    value['e4_reference']=legacy['e4_reference']
    value['free_tier_evidence']['data_use_permission']['pm_approval_reference']=r.pm_approval_reference
    scope=json.loads(json.dumps(asdict(scope_for(f,r))))
    for item in scope['external_files']:
        item['sha256']=sha256(Path(item['path']).read_bytes()).hexdigest()
    value['december_demo_execution']=scope
    def load_safety():
        a=safety_parse(value);b=f['target_binding']
        a.matches_record_input(r,b.approved_input,b.sdk_byte_evidence,r.source_revision)
        return a
    monkeypatch.setattr(c,'load_live_binding',lambda:f['target_binding'])
    monkeypatch.setattr(c,'load_live_safety_artifact',load_safety)
    from knowledge_index import gemini_diagnostic as diagnostic
    monkeypatch.setattr(diagnostic,'load_live_safety_artifact',load_safety)
    monkeypatch.setattr(c,'LiveDiagnosticUsage',lambda:f['store'])
    execute=entry.execute_from_host
    def execute_exact_fixture_grant():
        # The reused mock-port helper otherwise substitutes its legacy test grant.
        scope.update(grant_reference=r.pm_approval_reference,issuer_identity=r.spec_lead_identity)
        return execute()
    monkeypatch.setattr(entry,'execute_from_host',execute_exact_fixture_grant)
    f.update(scope=scope,current=(request,value,credentials,calls),load_safety=load_safety)
    test_real_port_branch_with_only_mock_transports_and_persistence(f,monkeypatch)
    with sqlite3.connect(str(f['path'])) as db:
        state=c._check_december_debug_budget(db,r,pending=False)
    assert state['conservative_occupancy']==11
    assert len(calls)==1 and f['calls']==[]


@pytest.mark.parametrize('campaign', ['dec17-rca-demo-20261128-06','dec17-rca-demo-20261128-07','renamed-target'])
def test_m1_delegated_campaign_cannot_drop_budget_any_boundary(bounded,campaign):
    from copy import copy
    f=bounded;r=target(f);bad=copy(r)
    object.__setattr__(bad,'campaign_id',campaign)
    object.__setattr__(bad,'december_debug_call_budget',None)
    object.__setattr__(bad,'december_demo_fence_review',replace(r.december_demo_fence_review,target_campaign_id=campaign))
    before=rows(f)
    with pytest.raises(c.DiagnosticDenied):bad.__post_init__()
    from test_knowledge_gemini_diagnostic_controls import facts
    with pytest.raises(c.DiagnosticDenied):
        c.validate_authorization(bad,facts(pm_approval_reference=bad.pm_approval_reference),
            live_opt_in=True,kind='query',input_scope_identity=bad.input_scope_identity,
            commitment=bad.input_commitment,item_count=1,item_bytes=(274,),item_tokens=(8192,),
            payload_bytes=431,safety_basis='SYNTHETIC_OFFLINE_ONLY',conservative_usage_units=8192,
            conservative_cost_micro_units=1)
    with pytest.raises(c.DiagnosticDenied):reserve(f,bad)
    with pytest.raises(c.DiagnosticDenied):scope_for(f,r).matches_record(bad,f['risk_value'])
    reserve(f,r)
    with pytest.raises(c.DiagnosticDenied):f['store'].revalidate_december_review(bad,'one',items=1,usage_units=8192,cost_micro_units=1)
    assert len(rows(f))==len(before)+1 and not f['risk_credentials'] and not f['risk_calls']


def test_m1_renamed_december_target_with_plain_reference_still_denied(bounded):
    r=target(bounded)
    wire=asdict(r);wire['campaign_id']='dec17-rca-demo-20261128-06'
    wire['pm_approval_reference']='UNRELATED-GRANT';wire.pop('december_debug_call_budget')
    wire['december_demo_fence_review'].update(target_campaign_id=wire['campaign_id'],target_pm_approval_reference=wire['pm_approval_reference'])
    with pytest.raises(c.DiagnosticDenied):c.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())


@pytest.mark.parametrize('field,value',[
    ('pm_decision_reference','UNRELATED-PM-DECISION'),
    ('pm_decision_sha256','0'*64),('pm_decision_sha256',None)])
def test_m2_parent_decision_identity_denied_at_all_boundaries(bounded,field,value):
    from copy import copy
    f=bounded;r=target(f);bad=copy(r)
    object.__setattr__(bad,'december_debug_call_budget',replace(r.december_debug_call_budget,**{field:value}))
    before=rows(f)
    with pytest.raises(c.DiagnosticDenied):bad.__post_init__()
    from test_knowledge_gemini_diagnostic_controls import facts
    with pytest.raises(c.DiagnosticDenied):
        c.validate_authorization(bad,facts(pm_approval_reference=bad.pm_approval_reference),
            live_opt_in=True,kind='query',input_scope_identity=bad.input_scope_identity,
            commitment=bad.input_commitment,item_count=1,item_bytes=(274,),item_tokens=(8192,),
            payload_bytes=431,safety_basis='SYNTHETIC_OFFLINE_ONLY',conservative_usage_units=8192,
            conservative_cost_micro_units=1)
    with pytest.raises(c.DiagnosticDenied):reserve(f,bad)
    with pytest.raises(c.DiagnosticDenied):scope_for(f,r).matches_record(bad,f['risk_value'])
    wire=asdict(bad)
    with pytest.raises(c.DiagnosticDenied):c.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())
    assert rows(f)==before and not f['risk_credentials']


@pytest.mark.parametrize('origin,target_index',[(0,1),(1,0)])
def test_m3_cross_target_grant_reference_not_reusable(bounded,origin,target_index):
    r=target(bounded,origin)
    with pytest.raises(c.DiagnosticDenied):target(bounded,target_index,grant_reference=r.pm_approval_reference)


def test_m3_shared_human_label_is_still_two_distinct_full_grants(bounded):
    r=target(bounded,0,grant_reference='UNIT-REVIEW');other=target(bounded,1,grant_reference='UNIT-REVIEW')
    assert r.pm_approval_reference!=other.pm_approval_reference
    assert r.campaign_id in r.pm_approval_reference and other.campaign_id in other.pm_approval_reference


@pytest.mark.parametrize('conflict',['grant','source'])
def test_m3_persisted_anchor_refuses_changed_grant_or_source(bounded,conflict):
    f=bounded;r=target(f);reserve(f,r);before=rows(f)
    if conflict=='grant':
        reference=c.DECEMBER_DEBUG_GRANT_PREFIX+r.campaign_id+'/CONFLICTING-UNIT-REFERENCE'
        changed=replace(r,pm_approval_reference=reference,december_demo_fence_review=replace(r.december_demo_fence_review,target_pm_approval_reference=reference))
    else:
        changed=replace(r,source_revision='f'*40,december_demo_fence_review=replace(r.december_demo_fence_review,target_source_revision='f'*40))
    with pytest.raises(c.DiagnosticDenied):reserve(f,changed,'second')
    with pytest.raises(c.DiagnosticDenied):f['store'].revalidate_december_review(changed,'one',items=1,usage_units=8192,cost_micro_units=1)
    with pytest.raises(c.DiagnosticDenied):f['store'].finish(changed,'one','UNKNOWN')
    assert rows(f)==before


def test_m3_duplicate_candidate_publication_does_not_refresh_consumed_allocation(bounded):
    f=bounded;r=target(f);scope=scope_for(f,r);scope.matches_record(r,f['risk_value'])
    reserve(f,r);before=rows(f)
    # Re-parsing identical candidate metadata does not publish anything or grant a second call.
    again=c.DiagnosticAuthorizationRecord.from_json(json.dumps(asdict(r)).encode())
    scope.matches_record(again,f['risk_value'])
    with pytest.raises(c.DiagnosticDenied):reserve(f,again,'another')
    assert rows(f)==before


def test_m1_no_legacy_real_scope_can_enable_unlisted_campaign(bounded):
    from dataclasses import replace
    legacy=replace(bounded['target'],expires_at='2026-12-17T23:59:00+08:00')
    s=scope_for(bounded,legacy)
    s=replace(s,mode='REAL_PROVIDER',grant_reference='UNIT-NOT-A-LIVE-GRANT',run_id='dec17-unlisted-unit-fixture')
    with pytest.raises(c.DiagnosticDenied,match='approved target identity'):
        s.matches_record(legacy,bounded['risk_value'])
