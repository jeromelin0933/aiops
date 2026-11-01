"""Demo gate reset: real corpus, synthetic live grants/usage/key, isolated stores.

No numeric fixture is provider evidence. No formal authority or exception is
published. Production safety parser, writer, reservation and dispatch are used.
"""
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json

import pytest

from knowledge_index import (KnowledgeBuildService, SqliteKnowledgeStore,
    BuildOperationKey, BuildActivationRequest, ActivationOperationKey, KnowledgeReadStatus)
from knowledge_index import gemini_diagnostic as d
from knowledge_index import gemini_diagnostic_controls as c
from test_knowledge_gemini_build_integration import setup_build, stage
from test_knowledge_gemini_diagnostic_batching import batched
from test_knowledge_gemini_diagnostic_single_probe import probe
from test_knowledge_gemini_embedding2_serialization import offline_sdk
from test_knowledge_gemini_diagnostic_controls import safety_parse


@pytest.fixture(autouse=True)
def sealed(offline_sdk, monkeypatch):
    def forbidden(*args, **kwargs):pytest.fail('formal host resolver forbidden')
    monkeypatch.setattr(c, '_programdata_known_folder', forbidden)
    monkeypatch.setattr(c, '_localappdata_known_folder', forbidden)
    yield
    assert offline_sdk.blocked_calls == []


@pytest.fixture
def full54(setup_build, monkeypatch, tmp_path):
    p = setup_build
    original = p['binding']
    r = replace(original.record, campaign_id='synthetic-full54-demo-grant',
        pm_approval_reference='synthetic-independent-full54-live-approval',
        maximum_invocations=6, maximum_cost_micro_units=6)
    def review(cls, row, row_sha, evidence_sha, reference, reviewer, recorded):
        return cls(c._P0_UNBLOCK_AUTHORITY, reference, reviewer, recorded,
            row['campaign_id'], row['request_id'], row_sha, evidence_sha,
            r.campaign_id, r.pm_approval_reference)
    first = review(c.P0UnknownAttemptReview, c._P0_HISTORY_ROW, c._P0_HISTORY_ROW_SHA256,
        c._P0_HISTORY_EVIDENCE_SHA256, c._P0_HUMAN_REVIEW_REFERENCE, r.spec_lead_identity,
        datetime.now(timezone.utc).isoformat())
    second = review(c.P0SecondUnknownAttemptReview, c._P0_SECOND_HISTORY_ROW,
        c._P0_SECOND_HISTORY_ROW_SHA256, c._P0_SECOND_HISTORY_EVIDENCE_SHA256,
        c._P0_SECOND_HUMAN_REVIEW_REFERENCE, c._P0_SECOND_REVIEWER, c._P0_SECOND_RECORDED_AT)
    plan = c.DiagnosticBatchPlan.from_mapping(p['value']['sequential_batch_plan'])
    scope = c.P0Full54HistoryScope(c._P0_DEMO_GATE_AUTHORITY, 'COMPETITION_DEMO_ONLY', c.COUNTING_CORPUS,
        plan.commitment, tuple(commitment for _, _, commitment in plan.ranges))
    r = replace(r, p0_two_unknown_reviews=(first, second), p0_full54_history_scope=scope)
    approved = replace(original.approved_input, campaign_id=r.campaign_id)
    e4 = replace(original.sdk_byte_evidence, campaign_id=r.campaign_id,
        pm_approval_reference=r.pm_approval_reference, authorization_record_sha256=c.authorization_record_digest(r))
    value = p['value']
    value['binding'].update(campaign_id=r.campaign_id, authorization_record_sha256=c.authorization_record_digest(r))
    value['free_tier_evidence']['data_use_permission']['pm_approval_reference'] = r.pm_approval_reference
    for row, (_, _, commitment) in zip(value['batch_sdk_evidence'], plan.ranges):
        row.update(campaign_id=r.campaign_id, pm_approval_reference=r.pm_approval_reference,
            authorization_record_sha256=c.authorization_record_digest(replace(r, input_commitment=commitment)))
    usage = c.TemporaryDiagnosticUsage(tmp_path/'full54-history.sqlite3', enable_counting=True)
    with closing(usage._connect()) as db:
        for row in (c._P0_HISTORY_ROW, c._P0_SECOND_HISTORY_ROW):
            db.execute('INSERT INTO attempts('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        db.commit()
    p.update(binding=c.LiveHostBinding(r,approved,None,r.runner_identity,e4),usage=usage,credentials=[])
    monkeypatch.setattr(d,'load_live_binding',lambda:p['binding'])
    monkeypatch.setattr(d,'load_live_safety_artifact',lambda:safety_parse(value))
    monkeypatch.setattr(d,'LiveDiagnosticUsage',lambda:usage)
    def credential(record, artifact):
        with closing(usage._connect(read_only=True)) as db:
            assert db.execute("SELECT COUNT(*) FROM attempts WHERE campaign_id=? AND outcome='IN_FLIGHT'",
                              (r.campaign_id,)).fetchone()==(1,)
        p['credentials'].append(True)
        return c.resolve_diagnostic_credential(record, artifact)
    monkeypatch.setattr(d,'resolve_diagnostic_credential',credential)
    yield p
    with closing(usage._connect(read_only=True)) as db:
        for row in (c._P0_HISTORY_ROW,c._P0_SECOND_HISTORY_ROW):
            assert db.execute('SELECT * FROM attempts WHERE campaign_id=?',(row['campaign_id'],)).fetchone()==tuple(row.values())


def test_full54_exact_grant_preserves_history_and_uses_existing_build_once(full54, tmp_path):
    p=full54; r=p['binding'].record
    assert c.DiagnosticAuthorizationRecord.from_json(json.dumps(asdict(r)).encode())==r
    with closing(p['usage']._connect(read_only=True)) as db:
        assert (c._blocking_fence_count(db),c._blocking_fence_count(db,r))==(2,0)
        assert db.execute('SELECT SUM(usage_reserved),SUM(cost_reserved) FROM attempts').fetchone()==(16384,2)
    with SqliteKnowledgeStore(tmp_path/'knowledge.sqlite3') as store:
        service=KnowledgeBuildService(store,p['provider'],p['index'])
        built=stage(service,p)
        assert built.record is not None and not built.failures
        assert len(p['calls'])==len(p['credentials'])==6
        assert store.read_activation().status is KnowledgeReadStatus.NOT_FOUND
        assert not service.validate(built.record.build_identity,BuildOperationKey('demo-validate'),maximum_probe_results=12).findings
        assert store.read_activation().status is KnowledgeReadStatus.NOT_FOUND
        # Existing explicit owner command, isolated simulation only. This key
        # is durable scope labeling, NOT proof of real approval/publication.
        activated=service.activate(BuildActivationRequest(built.record.build_identity,
            ActivationOperationKey('competition-demo-explicit-owner-command'),0))
        assert activated.authority.active_build_identity==built.record.build_identity
        assert stage(service,p).record==built.record and len(p['calls'])==6
    with closing(p['usage']._connect(read_only=True)) as db:
        assert c._blocking_fence_count(db)==2
        assert db.execute('SELECT COUNT(*),SUM(cost_reserved) FROM attempts WHERE campaign_id=?',
                          (r.campaign_id,)).fetchone()==(6,6)


@pytest.mark.parametrize('change', ['no-scope','no-reviews','authority','query','probe-reuse',
    'global-input','plan','child-input','wrong-review','source-e4','safety','budget','missing-token',
    'pm-preparation','pm-reference','row-hash','review-time','invocations','formal-scope',
    'e4-body','credential-profile'])
def test_invalid_full54_scope_or_safety_never_reserves_or_resolves_credential(full54, change):
    p=full54; b=p['binding']; wire=asdict(b.record)
    if change=='no-scope':wire['p0_full54_history_scope']=None
    elif change=='no-reviews':wire['p0_two_unknown_reviews']=None
    elif change=='authority':wire['p0_full54_history_scope']['authority_reference']='unrelated'
    elif change=='formal-scope':wire['p0_full54_history_scope']['delivery_scope']='PRODUCTION'
    elif change=='query':wire['input_kind']='query'
    elif change=='probe-reuse':wire['pm_approval_reference']='PM-SPEC014-DIRECT-EMBED-THIRD-20261031-01'
    elif change=='global-input':wire['p0_full54_history_scope']['global_input_commitment']='a'*64
    elif change=='plan':wire['p0_full54_history_scope']['batch_plan_commitment']='a'*64
    elif change=='child-input':wire['p0_full54_history_scope']['batch_input_commitments']=['a'*64]
    elif change=='wrong-review':wire['p0_two_unknown_reviews'][0]['review_reference']='UNRELATED-NONEMPTY-REVIEW'
    elif change=='source-e4':wire['source_revision']='c'*40
    elif change=='e4-body':p['binding']=b=replace(b,sdk_byte_evidence=replace(b.sdk_byte_evidence,sdk_body_sha256='a'*64))
    elif change=='credential-profile':wire['credential_profile_identity']='wrong-profile'
    elif change=='safety':p['value']['binding']['authorization_record_sha256']='a'*64
    elif change=='budget':wire['maximum_cost_micro_units']=5
    elif change=='pm-preparation':wire['pm_approval_reference']=c._P0_DEMO_GATE_AUTHORITY
    elif change=='pm-reference':wire['pm_approval_reference']='unrelated-pm-reference'
    elif change=='row-hash':wire['p0_two_unknown_reviews'][1]['historical_row_sha256']='a'*64
    elif change=='review-time':wire['p0_two_unknown_reviews'][1]['recorded_at']='2000-01-01T00:00:00+00:00'
    elif change=='invocations':wire['maximum_invocations']=5
    else:p['value'].pop('accepted_input_token_bounds')
    # Exercise actual parsing -> protected binding seam -> admission/dispatch.
    try:
        record=c.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())
        p['binding']=replace(b,record=record)
        if change=='budget':
            # Rebind all transitive evidence so this tests cumulative budget,
            # rather than merely catching a stale authorization digest.
            p['binding']=replace(p['binding'],sdk_byte_evidence=replace(b.sdk_byte_evidence,
                authorization_record_sha256=c.authorization_record_digest(record)))
            p['value']['binding']['authorization_record_sha256']=c.authorization_record_digest(record)
            plan=c.DiagnosticBatchPlan.from_mapping(p['value']['sequential_batch_plan'])
            for child, (_,_,commitment) in zip(p['value']['batch_sdk_evidence'],plan.ranges):
                child['authorization_record_sha256']=c.authorization_record_digest(replace(record,input_commitment=commitment))
        d.run_live(p['request'],live_opt_in=True)
    except c.DiagnosticDenied:
        pass
    else:pytest.fail('invalid scope/safety was admitted')
    assert p['calls']==p['credentials']==[]
    with closing(p['usage']._connect(read_only=True)) as db:
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()==(2,)


@pytest.mark.parametrize('outcome',['UNKNOWN','TIMEOUT','IN_FLIGHT'])
def test_additional_ambiguity_is_never_reviewed_by_full54_scope(full54,outcome):
    p=full54;row=dict(c._P0_SECOND_HISTORY_ROW)
    row.update(campaign_id='unreviewed',request_id='unreviewed',outcome=outcome)
    with closing(p['usage']._connect()) as db:
        db.execute('INSERT INTO attempts('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()));db.commit()
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert p['calls']==p['credentials']==[]


@pytest.mark.parametrize('failure',['timeout','unknown_usage'])
def test_new_full54_ambiguity_stops_no_staged_index_and_no_retry(full54,tmp_path,failure):
    p=full54;p['transport'](failure=failure)
    with SqliteKnowledgeStore(tmp_path/'failed-knowledge.sqlite3') as store:
        result=stage(KnowledgeBuildService(store,p['provider'],p['index']),p)
        assert result.record is None and result.failures
        assert store.read_activation().status is KnowledgeReadStatus.NOT_FOUND
        assert p['index'].inspect(p['request'].build_identity) is None
    assert len(p['calls'])==(2 if failure=='timeout' else 1)
    before=len(p['calls'])
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert len(p['calls'])==before


def test_full54_without_explicit_opt_in_never_touches_new_attempt(full54):
    p=full54
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=False)
    assert p['calls']==p['credentials']==[]
    with closing(p['usage']._connect(read_only=True)) as db:
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()==(2,)


def test_review_scope_order_is_checked_after_complete_digest_rebinding(full54):
    p=full54; b=p['binding']; r=b.record
    scope=replace(r.p0_full54_history_scope,
        batch_input_commitments=tuple(reversed(r.p0_full54_history_scope.batch_input_commitments)))
    r=replace(r,p0_full54_history_scope=scope)
    p['binding']=replace(b,record=r,sdk_byte_evidence=replace(b.sdk_byte_evidence,
        authorization_record_sha256=c.authorization_record_digest(r)))
    p['value']['binding']['authorization_record_sha256']=c.authorization_record_digest(r)
    plan=c.DiagnosticBatchPlan.from_mapping(p['value']['sequential_batch_plan'])
    for child, (_,_,commitment) in zip(p['value']['batch_sdk_evidence'],plan.ranges):
        child['authorization_record_sha256']=c.authorization_record_digest(replace(r,input_commitment=commitment))
    with pytest.raises(c.DiagnosticDenied) as denied:d.run_live(p['request'],live_opt_in=True)
    assert denied.value.code=='HISTORICAL_REVIEW_MISMATCH'
    assert p['calls']==p['credentials']==[]


@pytest.mark.parametrize('after_reservation',[False,True])
def test_full54_persistence_failure_no_build_and_no_retry(full54,tmp_path,monkeypatch,after_reservation):
    import sqlite3
    p=full54; original=p['usage']._connect; connections=[]
    def fail(*a,**kw):
        connections.append(True)
        if not after_reservation or len(connections)>1:
            raise sqlite3.OperationalError('synthetic isolated I/O failure')
        return original(*a,**kw)
    monkeypatch.setattr(p['usage'],'_connect',fail)
    try:
        with SqliteKnowledgeStore(tmp_path/'persistence-knowledge.sqlite3') as store:
            result=stage(KnowledgeBuildService(store,p['provider'],p['index']),p)
            assert result.record is None and result.failures
            assert store.read_activation().status is KnowledgeReadStatus.NOT_FOUND
        # This fixture resolves credentials only after an additional read;
        # failure after reservation can therefore stop before transport too.
        assert p['calls']==[]
    finally:
        monkeypatch.setattr(p['usage'],'_connect',original)
