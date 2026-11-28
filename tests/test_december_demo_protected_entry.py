"""Synthetic/OFFLINE acceptance and TEMP ledger; never real authority or Git."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import sqlite3
import uuid

import pytest

from knowledge_index import gemini_diagnostic_controls as c
from runtime_orchestration import december_demo_entry as entry
from test_knowledge_december_query_safety import query_demo
from test_knowledge_gemini_query_counting import query_counting, counting, offline_sdk
from test_knowledge_gemini_diagnostic_controls import safety_parse


@pytest.fixture(autouse=True)
def sealed(offline_sdk):
    yield
    assert offline_sdk.blocked_calls == []


@pytest.fixture
def admitted(query_counting, monkeypatch):
    f = query_counting
    profile = 'spec014-gemini-embedding-2-profile-v1'
    grant = replace(f['grant'], credential_profile_identity=profile)
    e4 = replace(f['e4'], authorization_record_sha256=c.authorization_record_digest(grant))
    f['value']['credential_binding']['profile_identity'] = profile
    f['value']['free_tier_evidence']['account_context']['profile_identity'] = profile
    f['value']['binding']['authorization_record_sha256'] = c.authorization_record_digest(grant)
    f.update(grant=grant, e4=e4, binding=replace(f['binding'], record=grant, sdk_byte_evidence=e4))
    monkeypatch.setenv('GEMINI_CREDENTIAL_PROFILE', profile)
    f = query_demo.__wrapped__(f, monkeypatch)
    request, value, _, _ = f['current']
    record = f['target']
    now = datetime.now(timezone.utc)
    value['december_demo_execution'] = dict(
        scope_identity=c.DECEMBER_EXECUTION_SCOPE, decision='APPROVE_DECEMBER_DEMO_EXECUTION',
        mode='OFFLINE_FIXTURE', grant_reference='OFFLINE-PM-EXECUTION-FIXTURE',
        issuer_identity='SYNTHETIC OFFLINE REVIEWER NOT REAL AUTHORITY',
        valid_from=(now-timedelta(minutes=1)).isoformat(), expires_at='2026-12-17T15:59:00+00:00',
        diagnostic_authorization_sha256=c.authorization_record_digest(record),
        repository_revision=record.source_revision,
        account_reference=value['pricing_evidence']['account_reference'],
        acceptance_reference='OFFLINE-EXTERNAL-ACCEPTANCE-FIXTURE', accepted_by=record.spec_lead_identity,
        accepted_at=(now-timedelta(minutes=1)).isoformat(), acceptance_expires_at='2026-12-17T15:59:00+00:00',
        run_id='dec17-fixture-'+uuid.uuid4().hex[:12], generation_model='gemini-3.5-flash-lite',
        generation_profile='demo-google-gemini35-flash-lite-generation-profile-v1',
        generation_profile_version='2', generation_api='v1beta', physical_call_allocation=[1,1,1],
        operation_scopes=list(c.DECEMBER_OPERATION_SCOPES), external_files=[
            dict(path=str(c.DECEMBER_EXTERNAL_ROOT/name),version='1',
                 sha256=sha256((c.DECEMBER_EXTERNAL_ROOT/name).read_bytes()).hexdigest())
            for name in sorted(c.DECEMBER_EXTERNAL_NAMES)])
    def load_safety():
        artifact = safety_parse(f['current'][1])
        binding = f['target_binding']
        artifact.matches_record_input(f['target'], binding.approved_input, binding.sdk_byte_evidence, f['target'].source_revision)
        return artifact
    monkeypatch.setattr(c, 'load_live_binding', lambda:f['target_binding'])
    monkeypatch.setattr(c, 'load_live_safety_artifact', load_safety)
    monkeypatch.setattr(c, 'LiveDiagnosticUsage', lambda:f['store'])
    f.update(scope=value['december_demo_execution'], load_safety=load_safety)
    return f


def test_complete_protected_offline_scope_native_publication_and_same_try_replay(admitted):
    f=admitted
    first=entry.execute_from_host()
    assert first['status']=='OFFLINE_SMOKE_PASS' and first['execution_mode']=='OFFLINE_FIXTURE'
    assert first['demo_publication']=='APPLIED_OFFLINE_MOCK_RESULT'
    assert first['native_reopen_readback']=='PASS'
    again=entry.execute_from_host()
    assert again['replay'] and again['provider_calls_this_execution']==0
    assert again['version_id']==first['version_id']
    assert again['recorded_result_classification']=='RECORDED_OFFLINE_MOCK_RESULT'
    assert len(f['calls'])==1  # original synthetic counting observation only


@pytest.mark.parametrize('field,value', [
    ('grant_reference','PENDING'), ('grant_reference','PM-PRD004-20261125-STAGE1-01'),
    ('decision','PREPARATION_ONLY'), ('expires_at','2000-01-01T00:00:00+00:00'),
    ('repository_revision','f'*40), ('diagnostic_authorization_sha256','f'*64),
    ('account_reference','wrong'), ('generation_model','gemini-2.5-flash'),
    ('generation_profile_version','3'), ('generation_api','v1'),
    ('physical_call_allocation',[0,1,1]), ('operation_scopes',[]),
    ('accepted_at','2099-01-01T00:00:00+00:00'), ('acceptance_reference','PENDING'),
    ('acceptance_expires_at','2000-01-01T00:00:00+00:00'), ('accepted_by','wrong'),
])
def test_invalid_scope_before_external_import_or_any_write(admitted,monkeypatch,field,value):
    f=admitted;f['scope'][field]=value
    monkeypatch.setattr(entry,'_compile_accepted',lambda *_:pytest.fail('invalid authority reached code loading'))
    with pytest.raises(c.DiagnosticDenied):entry.execute_from_host()


@pytest.mark.parametrize('missing', ['december_demo_execution','acceptance_reference'])
def test_missing_grant_or_acceptance_cannot_inherit_query_permission(admitted,monkeypatch,missing):
    if missing=='december_demo_execution':admitted['current'][1].pop(missing)
    else:admitted['scope'].pop(missing)
    monkeypatch.setattr(entry,'_compile_accepted',lambda *_:pytest.fail('unaccepted code loaded'))
    with pytest.raises(c.DiagnosticDenied):entry.execute_from_host()


def test_external_hash_drift_rejected_before_loading(admitted,monkeypatch):
    admitted['scope']['external_files'][0]['sha256']='0'*64
    monkeypatch.setattr(entry,'_compile_accepted',lambda *_:pytest.fail('drifted code loaded'))
    with pytest.raises(c.DiagnosticDenied):entry.execute_from_host()


def test_credential_revision_mismatch_before_loading(admitted,monkeypatch):
    monkeypatch.setenv('GEMINI_CREDENTIAL_REVISION','wrong')
    monkeypatch.setattr(entry,'_compile_accepted',lambda *_:pytest.fail('credential mismatch reached code'))
    with pytest.raises(c.DiagnosticDenied):entry.execute_from_host()


@pytest.mark.parametrize('state', ['UNKNOWN','IN_FLIGHT','TIMEOUT','CONSUMED'])
def test_fence_or_used_allocation_before_import(admitted,monkeypatch,state):
    f=admitted
    with sqlite3.connect(str(f['path'])) as db:
        if state=='CONSUMED':
            db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved,usage_observed) VALUES(?, 'used','SUCCESS',1,1,1,'1')",(f['target'].campaign_id,))
        else:
            db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved) VALUES('other','x',?,1,1,1)",(state,))
    monkeypatch.setattr(entry,'_compile_accepted',lambda *_:pytest.fail('fenced allocation reached code'))
    with pytest.raises(c.DiagnosticDenied):entry.execute_from_host()


def test_partial_unknown_workspace_does_not_resubmit(admitted):
    scope=admitted['scope'];work=c.DECEMBER_EXTERNAL_ROOT/'runs'/('protected-'+scope['run_id'])
    work.mkdir()
    with pytest.raises(ValueError,match='UNKNOWN'):entry.execute_from_host()
    assert list(work.iterdir())==[]


def test_budget_check_uses_accepted_tokens_not_envelope_bytes(admitted):
    record=replace(admitted['target'],maximum_usage_units=299)
    with pytest.raises(c.DiagnosticDenied,match='allocation'):
        c.inspect_december_execution_ledger(record,before_query=True,required_usage_units=300)


def test_revoked_scope_stops_native_mutation(admitted,monkeypatch):
    touched=[]
    class Native:
        def commit_result(self,*args):touched.append(args)
    snapshots={p.name:p.read_bytes() for p in c.DECEMBER_EXTERNAL_ROOT.iterdir() if p.name in c.DECEMBER_EXTERNAL_NAMES}
    safety=admitted['load_safety']()
    context=entry.AdmittedDecemberExecution(admitted['target_binding'],safety,
        safety.demo_execution_scope(admitted['target']),snapshots,entry._SEAL)
    port=context.protect_port(Native(),'D')
    admitted['scope']['expires_at']='2000-01-01T00:00:00+00:00'
    with pytest.raises(c.DiagnosticDenied):port.commit_result('never committed')
    assert touched==[]


def test_distinct_offline_campaigns_have_distinct_native_try_and_grant(admitted):
    f=admitted;first=entry.execute_from_host();scope=deepcopy(f['scope'])
    f['current']=f['prepare_demo']('offline-distinct-second')
    scope.update(run_id='dec17-fixture-'+uuid.uuid4().hex[:12],
        grant_reference='OFFLINE-PM-INDEPENDENT-SECOND-FIXTURE',
        diagnostic_authorization_sha256=c.authorization_record_digest(f['target']))
    f['current'][1]['december_demo_execution']=scope
    second=entry.execute_from_host()
    assert first['execution_grant']!=second['execution_grant']
    assert first['attempt_id']!=second['attempt_id']
    assert first['version_id']!=second['version_id']
    assert second['execution_mode']=='OFFLINE_FIXTURE'


def test_accepted_source_is_locked_against_writes():
    p=c.DECEMBER_EXTERNAL_ROOT/'offline_ports.py';before=sha256(p.read_bytes()).hexdigest()
    with entry._locked_source(p):
        with pytest.raises(OSError):
            with p.open('ab') as stream:stream.write(b'UNREACHABLE')
    assert sha256(p.read_bytes()).hexdigest()==before


def test_real_port_branch_with_only_mock_transports_and_persistence(admitted,monkeypatch):
    """Exercise REAL wiring with explicitly SYNTHETIC authority/readers/clients.

    Nothing is published, and no real credential, Google or Host writer exists.
    The emitted test run is labelled separately, never offered as Real PASS.
    """
    import json
    from types import SimpleNamespace
    import httpx
    import knowledge_index
    import knowledge_index.chroma_index_adapter as chroma
    import llm_generation.google_generation_adapter as google
    from knowledge_index import ActivationAuthorityRecord,ActivationOperationKey
    from llm_generation.contracts import Claim,EvidenceReference,Hypothesis,Guidance
    from llm_generation.validation import observed_fact_text,evidence_fact_commitment
    from rca_shared.claim_types import ClaimCategory
    from rca_persistence.contracts import EvidentialSupport,GuidanceSource,DiagnosticConclusion,EvidenceCompleteness
    from test_llm_generation_phase2 import _structured
    admitted['scope'].update(mode='REAL_PROVIDER',grant_reference='PM-UNIT-EXECUTION',
        issuer_identity='SYNTHETIC UNIT FIXTURE NOT PM AUTHORITY')
    # This legacy synthetic campaign is not a production/delegated target.
    # Extend only this test's pinned legacy set to exercise the sealed mock ports.
    if admitted['target'].december_debug_call_budget is None:
        monkeypatch.setattr(c, 'DECEMBER_LEGACY_EXECUTION_CAMPAIGNS',
            c.DECEMBER_LEGACY_EXECUTION_CAMPAIGNS | {admitted['target'].campaign_id})
    wires=[];resources=[]
    original=entry._compile_accepted
    def compile_fixture(name,data,path):
        module=original(name,data,path)
        if name!='offline_ports':return module
        staged,validation,active=module.load_target_facts()  # existing RO metadata only
        work=c.DECEMBER_EXTERNAL_ROOT/'runs'/('protected-'+admitted['scope']['run_id'])
        class Store(module.OfflineCFixture):
            def read_activation(self):
                return self.result(ActivationAuthorityRecord(1,ActivationOperationKey(active['operation_id']),
                    staged.build_identity,active['validated_build_commitment'],active['result_commitment']))
            def close(self):resources.append('closed-mock-C')
        store=Store(work,staged,validation,active)
        index=module.OfflineIndex(staged,'none')
        index._client=SimpleNamespace(_system=SimpleNamespace(stop=lambda:resources.append('stopped-mock-index')))
        monkeypatch.setattr(knowledge_index,'SqliteKnowledgeStore',lambda *_:store)
        monkeypatch.setattr(chroma,'ChromaIndexAdapter',lambda *_a,**_k:index)
        def transport(request):
            wires.append(request.url.path)
            if request.url.path.endswith(':countTokens'):
                return httpx.Response(200,json={'totalTokens':1024})
            payload=json.loads(json.loads(request.content)['contents'][0]['parts'][0]['text'])
            evidence=json.loads(payload['evidence']);assert json.loads(payload['knowledge'])['chunks']==[]
            fact=evidence['event_projections'][0];text=observed_fact_text(fact)
            uncertain='Public evidence does not establish a cause.';action='Review independent service observations.'
            fake=SimpleNamespace(contract_version='1',summary=text+'; '+uncertain,summary_claim_ids=('c1','c2'),
                severity_assessment=text,severity_claim_ids=('c1',),diagnostic_conclusion=DiagnosticConclusion.INCONCLUSIVE,
                hypotheses=(Hypothesis(1,uncertain,EvidentialSupport.LOW,('e1',),(),(),uncertain,('c2',)),),
                remediation=(Guidance(action,GuidanceSource.MODEL_SUGGESTED,(),('c3',)),),prevention=(),
                limitations=('SYNTHETIC OFFLINE TEST RESPONSE; not Google evidence',),
                evidence_completeness=EvidenceCompleteness.FULL,knowledge_gap=True,
                evidence_references=(EvidenceReference('e1',evidence['snapshot_id'],'1','/event_projections/0',evidence_fact_commitment(fact)),),
                knowledge_references=(),claims=(Claim('c1',ClaimCategory.OBSERVED_FACT,text,('e1',)),
                    Claim('c2',ClaimCategory.ANALYTICAL_INFERENCE,uncertain,('e1',),evidential_support=EvidentialSupport.LOW),
                    Claim('c3',ClaimCategory.MODEL_SUGGESTED_GUIDANCE,action)))
            from llm_generation.config import load_generation_config
            result=_structured(fake,load_generation_config(c.DECEMBER_EXTERNAL_ROOT/'generation-demo.json'))
            return httpx.Response(200,json={'modelVersion':'gemini-3.5-flash-lite',
                'candidates':[{'content':{'role':'model','parts':[{'text':json.dumps(result)}]},'finishReason':'STOP'}],
                'usageMetadata':{'promptTokenCount':1024,'candidatesTokenCount':256,'thoughtsTokenCount':8,'totalTokenCount':1288}})
        def client(timeout_seconds,*,api_version):
            assert timeout_seconds==30 and api_version=='v1beta'
            return module.sdk_client(api_version,transport)  # explicit fake key + MockTransport
        monkeypatch.setattr(google,'create_google_generation_client',client)
        return module
    monkeypatch.setattr(entry,'_compile_accepted',compile_fixture)
    result=entry.execute_from_host()
    work=Path(result['run_root'])
    (work/'SYNTHETIC_OFFLINE_TEST_ONLY.json').write_text(json.dumps({'actual_google_calls':0,
        'authority':'SYNTHETIC READER FIXTURE, NEVER PUBLISHED','providers':'MockTransport',
        'C_persistence_and_index':'MOCK PORTS, NOT FORMAL AUTHORITY'}),encoding='utf-8')
    assert result['status']=='PROTECTED_DEMO_COMPLETED'
    assert result['demo_publication']=='APPLIED_PROTECTED_DEMO_RESULT'
    assert admitted['current'][3]==[True]  # one GUARDED query embedding, with actual native TEMP reservation
    assert wires==['/v1beta/models/gemini-3.5-flash-lite:countTokens','/v1beta/models/gemini-3.5-flash-lite:generateContent']
    assert 'closed-mock-C' in resources and 'stopped-mock-index' in resources
