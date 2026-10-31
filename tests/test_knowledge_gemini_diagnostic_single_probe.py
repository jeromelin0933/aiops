"""Fixed P0 probe: sealed SDK, synthetic authority/key, isolated formal-schema SQLite."""
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import sqlite3
import socket
import ssl

import httpx
import gzip
import zlib
import pytest

from knowledge_index import gemini_diagnostic as d
from knowledge_index import gemini_diagnostic_controls as c
from knowledge_index.chunking import plan_chunks
from knowledge_index.config import load_knowledge_config
from knowledge_index.manifest import admit_production_manifest
from knowledge_index.identity import document_identity
from test_knowledge_gemini_embedding2_adapter import fixture_request
from test_knowledge_gemini_embedding2_serialization import offline_sdk
from test_knowledge_gemini_diagnostic_controls import record, safety_case, safety_parse
from test_knowledge_gemini_diagnostic_free_tier import free_evidence, configure_free
from test_knowledge_gemini_diagnostic_dispatch import campaign, mock_transport, original_request


@pytest.fixture(autouse=True)
def sealed(offline_sdk):
    yield
    assert offline_sdk.blocked_calls == []


@pytest.fixture
def probe(tmp_path, monkeypatch, offline_sdk):
    root=d.Path(__file__).resolve().parents[1]
    cfg=load_knowledge_config(root/'configs/knowledge_index.yaml')
    manifest=admit_production_manifest('configs/knowledge_manifest.json',repository_root=root)
    chunks=plan_chunks(manifest,source_root=root/'docs/knowledge',profile=cfg.chunking)
    candidate=chunks[52]
    assert len(chunks)==54 and candidate.chunk_identity==c.COUNTING_CHUNK
    mapping=tuple((document_identity(manifest.manifest.corpus_id,x.document_id),x.document_id)
                  for x in manifest.manifest.documents if x.document_id=='ops.shared-database-network-dependency-interruption')
    original=fixture_request(tmp_path)
    request=replace(original,chunks=(candidate,),approved_corpus_id=manifest.manifest.corpus_id,approved_document_ids=mapping)
    scope,text=d._counting_content()  # Corpus-only derivation, no countTokens client/operation.
    assert d._envelopes(request)==('document',(c.COUNTING_CHUNK,),(text,))
    types=offline_sdk.types
    contents=[types.Content(role='user',parts=[types.Part(text=text)])]
    config=types.EmbedContentConfig(output_dimensionality=768)
    client,captured=offline_sdk.make_client()
    client.models.embed_content(model='gemini-embedding-2',contents=contents,config=config)
    actual=captured[0]
    body=actual.content
    grant=record(kind='document',commitment=c.COUNTING_INPUT,input_scope_identity=scope,
        credential_profile_identity=request.capability.profile_reference.value,
        maximum_invocations=1,maximum_items=1,maximum_item_tokens=8192,
        maximum_aggregate_tokens=8192,maximum_usage_units=8192,maximum_item_bytes=528,
        maximum_payload_bytes=4096,maximum_cost_micro_units=1,
        bounds_basis=c.SINGLE_PROBE_BASIS,cost_basis='synthetic-free-probe-basis',
        valid_from='2000-01-01T00:00:00+00:00',expires_at='2099-01-01T00:00:00+00:00',
        project_identity='synthetic-project',credential_profile_revision='r1')
    approved=c.ApprovedDiagnosticInput(1,grant.campaign_id,grant.source_revision,grant.purpose,
        'document',scope,c.COUNTING_INPUT,(c.COUNTING_CHUNK,),(text,))
    e4=c.DiagnosticSdkByteEvidence('HOST_CONTROLLED_E4_ONLY','PINNED_SDK_HTTPX_REQUEST_BODY',
        'synthetic-probe-e4',datetime(2099,1,1,tzinfo=timezone.utc),grant.campaign_id,
        grant.source_revision,c.authorization_record_digest(grant),'google','gemini-embedding-2',
        'google-genai==2.25.0','v1','document',scope,approved.identities,approved.commitment,
        sha256(d._canonical_request(body)).hexdigest(),sha256(body).hexdigest(),len(body),
        'POST',str(actual.url),768,30000,1,pm_approval_reference=grant.pm_approval_reference,
        valid_from=datetime(2000,1,1,tzinfo=timezone.utc))
    value=safety_case()[3]
    value.pop('token_evidence')
    value['binding'].update(campaign_id=grant.campaign_id,authorization_record_sha256=c.authorization_record_digest(grant),
        source_revision=grant.source_revision,kind='document',input_scope_identity=scope,
        input_commitment=c.COUNTING_INPUT,request_url=str(actual.url),bounds_basis=grant.bounds_basis,cost_basis=grant.cost_basis)
    source=dict(value['provider_limits']['source'])
    value['provider_limits']=dict(item_limit='UNKNOWN',per_item_token_limit='UNKNOWN',
        aggregate_token_limit='UNKNOWN',payload_byte_limit='UNKNOWN',source=source)
    value['single_probe']=dict(schema_version=1,pm_decision=c.SINGLE_PROBE_PM_DECISION,
        model_input_limit=8192,exact_input_tokens='NOT VERIFIED',admission_basis='PM_ACCEPTED_MODEL_LIMIT_RISK_CAP',
        outbound_eligibility='APPROVED_SYNTHETIC_NON_SECRET',
        project_identity=grant.project_identity,corpus_commitment=c.COUNTING_CORPUS,
        chunk_identity=c.COUNTING_CHUNK,global_ordinal=52,input_commitment=c.COUNTING_INPUT,
        envelope_bytes=528,payload_bytes=len(body),payload_sha256=sha256(body).hexdigest(),
        truncation='TRUNCATION VERIFICATION LIMITATION',source=dict(source))
    value['e4_reference'].update(reference=e4.reference,sdk_body_sha256=e4.sdk_body_sha256,
        canonical_json_sha256=e4.canonical_json_sha256,sdk_body_bytes=e4.sdk_body_bytes)
    value['credential_binding'].update(profile_identity=grant.credential_profile_identity,secret_revision='r1')
    free_evidence(value,grant)
    # PM's bounded-risk probe does not require numeric project quota evidence.
    value['free_tier_evidence']['quota_evidence'].update(rpm='UNKNOWN',tpm='UNKNOWN',rpd='UNKNOWN')
    binding=c.LiveHostBinding(grant,approved,None,grant.runner_identity,e4)
    monkeypatch.setattr(d,'load_live_binding',lambda:binding)
    monkeypatch.setattr(d,'load_live_safety_artifact',lambda:safety_parse(value))
    monkeypatch.setattr(d,'resolve_repository_source',lambda _: (grant.source_revision,True))
    monkeypatch.setattr(d.socket,'gethostname',lambda:grant.authorized_host)
    temporary=c.TemporaryDiagnosticUsage(tmp_path/'attempts.sqlite',enable_counting=True)
    usage=object.__new__(c.LiveDiagnosticUsage);usage._path=temporary._path
    monkeypatch.setattr(c.LiveDiagnosticUsage,'_connect',c.TemporaryDiagnosticUsage._connect)
    monkeypatch.setattr(d,'LiveDiagnosticUsage',lambda:usage)
    monkeypatch.setitem(c.os.environ,'GEMINI_CREDENTIAL_PROFILE',grant.credential_profile_identity)
    monkeypatch.setitem(c.os.environ,'GEMINI_CREDENTIAL_REVISION','r1')
    monkeypatch.setitem(c.os.environ,'GEMINI_API_KEY','synthetic-probe-key')
    calls=[];credentials=[]
    def rows():
        with closing(sqlite3.connect(str(temporary._path))) as db:
            return db.execute('SELECT outcome,items,usage_reserved,cost_reserved,usage_observed FROM attempts').fetchall()
    def resolve(*args):
        assert rows()==[('IN_FLIGHT',1,8192,1,'UNKNOWN')]
        credentials.append(True)
        return c.resolve_diagnostic_credential(*args)
    monkeypatch.setattr(d,'resolve_diagnostic_credential',resolve)
    def provider(*,mode='valid',usage_tokens=None):
        def send(r):
            assert rows()==[('IN_FLIGHT',1,8192,1,'UNKNOWN')]
            assert r.content==body and str(r.url)==str(actual.url)
            assert r.extensions['timeout']==dict(connect=30.,read=30.,write=30.,pool=30.)
            calls.append(True)
            if mode in ('dns', 'tls'):
                cause = (socket.gaierror(-1, 'synthetic-secret-cause') if mode == 'dns' else
                         ssl.SSLCertVerificationError(1, 'synthetic-secret-cause'))
                raise httpx.ConnectError('synthetic-secret-message') from cause
            if mode == 'read':raise httpx.ReadError('synthetic-secret-message')
            if mode=='timeout':raise httpx.ReadTimeout('synthetic')
            if mode=='unknown':raise httpx.ConnectError('synthetic')
            if mode=='429':return httpx.Response(429,json={'error':{'code':429,'status':'RESOURCE_EXHAUSTED'}})
            vector=[1.]+[0.]*767
            if mode=='dimension':vector.pop()
            if mode=='nan':vector[0]=float('nan')
            if mode=='zero':vector[0]=0.
            if mode=='norm':vector[0]=2.
            data={'embeddings':[] if mode=='cardinality' else [{'values':vector}]}
            if usage_tokens is not None:data['usageMetadata']={'promptTokenCount':usage_tokens}
            return httpx.Response(200,content=json.dumps(data).encode())
        monkeypatch.setattr(d,'_create_live_transport',lambda:httpx.MockTransport(send))
    provider()
    return dict(request=request,grant=grant,approved=approved,e4=e4,value=value,usage=usage,
        calls=calls,credentials=credentials,rows=rows,provider=provider,body=body,text=text)


def test_exact_probe_sdk_payload_and_risk_reservation(probe):
    p=probe
    print('PROBE_OFFLINE_EVIDENCE',json.dumps(dict(input=c.COUNTING_INPUT,envelope_bytes=len(p['text'].encode()),
        payload_bytes=len(p['body']),payload_sha256=sha256(p['body']).hexdigest(),route=p['e4'].request_url)))
    result=d.run_live(p['request'],live_opt_in=True)
    assert result.outcome=='SUCCESS' and result.category=='PROBE_OBSERVATION_ONLY'
    assert result.vector_count==1 and result.usage=='UNKNOWN'
    assert result.admission_usage_basis=='PM_ACCEPTED_MODEL_LIMIT_RISK_CAP'
    assert result.exact_input_token_count=='NOT VERIFIED'
    assert result.truncation_status=='TRUNCATION VERIFICATION LIMITATION'
    assert p['rows']()==[('SUCCESS',1,8192,1,'UNKNOWN')]
    assert len(p['calls'])==len(p['credentials'])==1
    assert p['e4'].request_url.endswith(':batchEmbedContents') and len(p['body'])!=591
    assert len(p['body'])==671 and sha256(p['body']).hexdigest()==c.SINGLE_PROBE_PAYLOAD
    assert 'autoTruncate' not in p['body'].decode() and 'taskType' not in p['body'].decode()
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert len(p['calls'])==1


@pytest.mark.parametrize('encoding', ['identity', 'gzip', 'deflate'])
@pytest.mark.parametrize('damage', ['none', 'corrupt', 'truncated', 'oversize'])
def test_guarded_response_content_decoding(probe, monkeypatch, encoding, damage):
    """Real run_live/guard/SDK; only the physical delegate is synthetic.

    Stream raw wire bytes: Response(content=...) would decode at fixture
    construction, hiding the production streaming decoder boundary.
    """
    p = probe
    body = json.dumps({'embeddings': [{'values': [1.] + [0.] * 767}],
                       'usageMetadata': {'promptTokenCount': 42}}).encode()
    if damage == 'oversize':
        body = b' ' * 1_000_001 + body
    wire = (gzip.compress(body) if encoding == 'gzip' else
            zlib.compress(body) if encoding == 'deflate' else body)
    if damage == 'corrupt':
        wire = b'not-valid-compression-or-json'
    elif damage == 'truncated':
        # Remove only the compression trailer: decoded JSON can be complete.
        wire = wire[:-8] if encoding == 'gzip' else wire[:-4] if encoding == 'deflate' else wire[:len(wire)//2]
    responses = []

    def send(request):
        p['calls'].append(True)
        assert request.content == p['body']
        headers = {'content-type': 'application/json', 'content-length': str(len(wire))}
        if encoding != 'identity':
            headers['content-encoding'] = encoding
        response = httpx.Response(200, headers=headers, stream=httpx.ByteStream(wire))
        responses.append(response)
        return response

    monkeypatch.setattr(d, '_create_live_transport', lambda: httpx.MockTransport(send))
    result = d.run_live(p['request'], live_opt_in=True)
    assert len(p['calls']) == len(p['credentials']) == 1
    assert responses[0].is_closed
    if damage == 'none':
        assert (result.outcome, result.transport_exception_type, result.transport_cause_type) == ('SUCCESS', None, None)
        assert result.vector_count == 1 and result.usage == '42'
    else:
        assert result.outcome != 'SUCCESS' and result.vector_count == 0
        assert result.usage == 'UNKNOWN'
        if encoding != 'identity' and damage in ('corrupt', 'truncated'):
            assert result.transport_exception_type == 'httpx.DecodingError'
            if damage == 'corrupt':
                assert result.transport_cause_type == 'zlib.error'
    with pytest.raises(c.DiagnosticDenied):
        d.run_live(p['request'], live_opt_in=True)
    assert len(p['calls']) == len(p['credentials']) == 1


@pytest.mark.parametrize('encoding', ['gzip', 'deflate'])
def test_decoded_body_with_original_encoding_reproduces_live_exception(encoding):
    # Exact pre-fix reconstruction step: HTTPX interprets content bytes using
    # retained compression headers, although the guard already decoded them.
    decoded = json.dumps({'embeddings': [{'values': [1.] + [0.] * 767}]}).encode()
    with pytest.raises(httpx.DecodingError) as error:
        httpx.Response(200, headers={'content-encoding': encoding}, content=decoded)
    assert isinstance(error.value.__cause__, zlib.error)


@pytest.mark.parametrize('mode,kind,outcome,cause', [
    ('dns', 'DNS', 'UNKNOWN', 'socket.gaierror'),
    ('tls', 'TLS_CERTIFICATE', 'UNKNOWN', 'ssl.SSLCertVerificationError'),
    ('read', 'READ', 'UNKNOWN', None),
    ('timeout', 'TIMEOUT', 'TIMEOUT', None),
    ('429', 'HTTP_STATUS', 'FAILED', None),
])
def test_transport_evidence_is_non_secret_and_does_not_release_fence(
        probe, monkeypatch, mode, kind, outcome, cause):
    def forbidden_resolver():
        raise AssertionError('real Host store resolver must not run')
    monkeypatch.setattr(c, '_programdata_known_folder', forbidden_resolver)
    monkeypatch.setattr(c, '_localappdata_known_folder', forbidden_resolver)
    p = probe
    p['provider'](mode=mode)
    result = d.run_live(p['request'], live_opt_in=True)
    assert result.outcome == outcome
    assert result.transport_failure_kind == kind
    assert result.transport_cause_type == cause
    assert result.transport_exception_type
    assert result.transport_http_status == (429 if mode == '429' else None)
    assert 'synthetic-secret' not in json.dumps(asdict(result))
    assert result.provider_attempts == 1 and result.vector_count == 0
    assert p['rows']() == [(outcome, 1, 8192, 1, 'UNKNOWN')]
    # Same operation and an independently named campaign remain fenced globally.
    with pytest.raises(c.DiagnosticDenied):
        d.run_live(p['request'], live_opt_in=True)
    new_grant = replace(p['grant'], campaign_id='synthetic-independent-next-campaign')
    with pytest.raises(c.DiagnosticDenied) as denied:
        p['usage'].reserve(new_grant, 'new-attempt', items=1, usage_units=8192, cost_micro_units=1)
    assert denied.value.code == 'RUNNER_CONFLICT'
    assert p['rows']() == [(outcome, 1, 8192, 1, 'UNKNOWN')]
    assert len(p['calls']) == len(p['credentials']) == 1


@pytest.mark.parametrize('code', ['synthetic-secret-status', 503, True])
def test_transport_evidence_bounds_cyclic_causes_and_untrusted_status(code):
    error = RuntimeError('synthetic-secret-message')
    error.__cause__ = error
    error.__context__ = socket.gaierror(-1, 'unrelated-secret-context')
    error.code = code
    evidence = d._transport_exception_evidence(error)
    assert evidence == dict(transport_exception_type='builtins.RuntimeError',
        transport_cause_type=None, transport_http_status=None, transport_failure_kind='OTHER')


@pytest.mark.parametrize('mode',['scope','chunk','payload','project','revision','model','sdk','source','dirty',
    'optin','no_pm','cost','invocations','items','risk','marker','permission','outbound','extra_tokens','missing_probe'])
def test_preflight_failure_has_zero_credential_transport(probe,monkeypatch,mode):
    p=probe
    if mode=='scope':object.__setattr__(p['grant'],'bounds_basis','not-an-approved-probe')
    elif mode=='chunk':object.__setattr__(p['approved'],'identities',('wrong',))
    elif mode=='payload':p['value']['single_probe']['payload_sha256']='0'*64
    elif mode in ('project','revision'):
        object.__setattr__(p['grant'],'project_identity' if mode=='project' else 'credential_profile_revision','wrong')
    elif mode in ('model','sdk'):
        object.__setattr__(p['grant'],'model_identity' if mode=='model' else 'sdk_baseline','wrong')
    elif mode in ('source','dirty'):
        monkeypatch.setattr(d,'resolve_repository_source',lambda _: ('f'*40 if mode=='source' else p['grant'].source_revision,mode!='dirty'))
    elif mode=='no_pm':
        def denied():raise c.DiagnosticDenied('no PM authority')
        monkeypatch.setattr(d,'load_live_binding',denied)
    elif mode in ('cost','invocations','items','risk'):
        object.__setattr__(p['grant'],dict(cost='maximum_cost_micro_units',invocations='maximum_invocations',items='maximum_items',risk='maximum_usage_units')[mode],0 if mode=='cost' else 2)
    elif mode=='marker':p['value']['single_probe']['pm_decision']='caller approved'
    elif mode=='permission':p['value']['free_tier_evidence']['data_use_permission']['pm_approval_reference']='other'
    elif mode=='outbound':p['value']['single_probe']['outbound_eligibility']='not approved'
    elif mode=='extra_tokens':p['value']['accepted_input_token_bounds']={'0'*64:8192}
    elif mode=='missing_probe':p['value'].pop('single_probe')
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=mode!='optin')
    assert p['credentials']==p['calls']==[]
    assert p['rows']()==[]


@pytest.mark.parametrize('mode',['timeout','unknown','429','dimension','nan','zero','norm','cardinality'])
def test_failure_stops_once_and_preserves_hold(probe,mode):
    p=probe;p['provider'](mode=mode)
    result=d.run_live(p['request'],live_opt_in=True)
    assert result.outcome!='SUCCESS' and len(p['calls'])==1
    assert p['rows']()[0][1:]==(1,8192,1,'UNKNOWN')
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert len(p['calls'])==1


@pytest.mark.parametrize('state',['IN_FLIGHT','UNKNOWN','TIMEOUT','SUCCESS'])
def test_global_fences_remain_unchanged(probe,state):
    p=probe
    with closing(p['usage']._connect()) as db:
        db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved) VALUES('prior','attempt',?,1,1,1)",(state,))
        db.commit()
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert p['credentials']==p['calls']==[]


def test_reservation_persistence_failure_before_credential(probe,monkeypatch):
    p=probe
    def broken(**kw):raise sqlite3.OperationalError('synthetic unavailable SQLite')
    monkeypatch.setattr(p['usage'],'_connect',broken)
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert p['credentials']==p['calls']==[]


def test_trusted_usage_is_actual_and_not_admission_cap(probe):
    p=probe;p['provider'](usage_tokens=17)
    result=d.run_live(p['request'],live_opt_in=True)
    assert result.usage=='17' and p['rows']()==[('SUCCESS',1,8192,1,'17')]
    with pytest.raises(c.DiagnosticDenied) as exc:d.run_live(p['request'],live_opt_in=True)
    assert exc.value.code=='BUDGET_EXHAUSTED'
    assert len(p['calls'])==len(p['credentials'])==1


def test_excess_actual_usage_retained_and_blocks(probe):
    p=probe;p['provider'](usage_tokens=9000)
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert p['rows']()==[('UNKNOWN',1,8192,1,'9000')]
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert len(p['calls'])==len(p['credentials'])==1


def test_sdk_no_truncation_option_is_unavailable(probe,offline_sdk):
    client,captured=offline_sdk.make_client()
    t=offline_sdk.types
    with pytest.raises(ValueError,match='auto_truncate'):
        client.models.embed_content(model='gemini-embedding-2',
            contents=[t.Content(role='user',parts=[t.Part(text=probe['text'])])],
            config=t.EmbedContentConfig(output_dimensionality=768,auto_truncate=False))
    assert captured==[] and probe['calls']==probe['credentials']==[]


def test_caller_flags_cannot_select_probe(probe):
    with pytest.raises(TypeError):d.run_live(probe['request'],live_opt_in=True,single_probe=True)
    assert probe['calls']==probe['credentials']==[]


@pytest.mark.parametrize('unknown',[('rpm',),('tpm',),('rpd',),('rpm','tpm','rpd'),()])
def test_probe_quota_numbers_or_explicit_unknown_through_real_entry(probe,unknown):
    p=probe
    quota=p['value']['free_tier_evidence']['quota_evidence']
    quota.update(rpm=100,tpm=30000,rpd=1000)
    for metric in unknown:quota[metric]='UNKNOWN'
    before=dict(quota)
    result=d.run_live(p['request'],live_opt_in=True)
    assert result.outcome=='SUCCESS' and result.category=='PROBE_OBSERVATION_ONLY'
    assert len(p['credentials'])==len(p['calls'])==1
    assert p['rows']()==[('SUCCESS',1,8192,1,'UNKNOWN')]
    parsed=safety_parse(p['value']).fields['free_tier_evidence']['quota_evidence']
    for metric in ('rpm','tpm','rpd'):
        assert parsed[metric]==before[metric] and type(parsed[metric]) is type(before[metric])


@pytest.mark.parametrize('invalid',['quota_missing','rpm_missing','tpm_missing','rpd_missing',
    'source_missing',None,0,-1,True,1.5,2**63,'unknown','1','UNLIMITED','',{},[]])
def test_invalid_probe_quota_stops_before_credential_transport(probe,invalid):
    p=probe; free=p['value']['free_tier_evidence']; quota=free['quota_evidence']
    if invalid=='quota_missing':free.pop('quota_evidence')
    elif invalid in ('rpm_missing','tpm_missing','rpd_missing'):quota.pop(invalid.split('_')[0])
    elif invalid=='source_missing':quota.pop('source')
    else:quota['rpm']=invalid
    with pytest.raises(c.DiagnosticDenied):d.run_live(p['request'],live_opt_in=True)
    assert p['credentials']==p['calls']==[] and p['rows']()==[]


@pytest.mark.parametrize('metric',['rpm','tpm','rpd'])
def test_ordinary_embedding_still_rejects_unknown_project_quota(campaign,monkeypatch,tmp_path,metric):
    prepared,binding,value,usage=configure_free(campaign,monkeypatch)
    assert not c.is_single_document_probe(binding.record)
    value['free_tier_evidence']['quota_evidence'][metric]='UNKNOWN'
    calls=mock_transport(monkeypatch,tmp_path)
    credentials=[]
    def resolve(*args):
        credentials.append(True)
        return c.resolve_diagnostic_credential(*args)
    monkeypatch.setattr(d,'resolve_diagnostic_credential',resolve)
    with pytest.raises(c.DiagnosticDenied):
        d.run_live(original_request(prepared,'query',tmp_path,1),live_opt_in=True)
    assert credentials==calls==[]
    with closing(usage._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==0
