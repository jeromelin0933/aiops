
"""Offline guarded Query -> Candidate-C, with no real credential/provider/store."""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType

import httpx
import pytest

from knowledge_index import gemini_diagnostic as d
from knowledge_index import gemini_diagnostic_controls as c
from knowledge_index.google_embedding_adapter import GoogleEmbeddingAdapter
from knowledge_index import QueryEmbeddingRequest, CanonicalKnowledgeQuery
from test_knowledge_gemini_diagnostic_dispatch import campaign, original_request
from test_knowledge_gemini_diagnostic_controls import facts, safety_parse
from test_knowledge_gemini_diagnostic import _host_e4_observation
from test_knowledge_gemini_embedding2_serialization import offline_sdk
from test_knowledge_gemini_build_integration import setup_build, stage
from test_knowledge_gemini_diagnostic_batching import batched
from test_knowledge_gemini_diagnostic_single_probe import probe
from test_llm_generation_phase2 import case
from test_knowledge_gemini_p0_admission import reviewed, two_reviewed


@pytest.fixture
def retrieved(setup_build, campaign, tmp_path, monkeypatch):
    """Real corpus/Chroma/Candidate-C; all provider vectors and grants synthetic."""
    from knowledge_index import (SqliteKnowledgeStore, KnowledgeBuildService,
        BuildOperationKey, BuildActivationRequest, ActivationOperationKey,
        KnowledgeSnapshotService, KnowledgeRetrievalService, QueryFilter,
        RetrievalOperationRequest, RetrievalOperationKey)
    p = setup_build
    store = SqliteKnowledgeStore(tmp_path/'knowledge-query.sqlite3')
    service = KnowledgeBuildService(store, p['provider'], p['index'])
    staged = stage(service, p)
    assert staged.record is not None and not staged.failures
    record = staged.record
    assert not service.validate(record.build_identity, BuildOperationKey('validate'),
                                maximum_probe_results=12).findings
    service.activate(BuildActivationRequest(record.build_identity, ActivationOperationKey('activate'), 0))
    base = campaign(kind='query')
    doc = next(doc for doc in p['admission'].manifest.documents
               if doc.document_id == 'ops.database-slow-query-api-timeout-diagnosis')
    from llm_generation.contracts import _DANGEROUS
    # Keep approved content intact; the existing D security boundary still rejects
    # unsafe projections. Use one unmodified operational section for this path.
    target = next(i for i, chunk in enumerate(p['chunks'])
        if chunk.document_identity == c.document_identity(p['admission'].manifest.corpus_id, doc.document_id)
        and not _DANGEROUS.search(chunk.content))
    restricted = next(i for i, chunk in enumerate(p['chunks'])
        if chunk.document_identity == c.document_identity(p['admission'].manifest.corpus_id, doc.document_id)
        and _DANGEROUS.search(chunk.content))
    metadata = {x.key:x.value for x in doc.metadata}
    # Isolation fixture only: NOT a production Incident -> filter mapping and
    # NOT evidence that a real query selected this SOP independently.
    filters = tuple(QueryFilter(k, metadata[k]) for k in p['cfg'].applicability_policy.required_filter_keys)
    snapshots = KnowledgeSnapshotService(store, KnowledgeRetrievalService(store, p['provider'], p['index']))
    calls = []
    def resolve(name, mode='match'):
        query = CanonicalKnowledgeQuery('1.0', '1.0', ('  offline '+name+'  ').strip(), filters)
        assert query.text == 'offline '+name
        if mode == 'mapped-no-match':
            from _incident_evidence_store_testkit import success
            from runtime_orchestration.knowledge_query import query_from_evidence
            query = query_from_evidence('INC-1', success(incident_id='INC-1').snapshot)
        request = QueryEmbeddingRequest(record.build_identity, query, p['cfg'].capability,
                                        replace(p['cfg'].limits, maximum_invocations=1))
        r, a, e, s = bind_query(request, base, base[3], monkeypatch, name)
        def send(req):
            calls.append(req)
            assert sha256(req.content).hexdigest() == e.sdk_body_sha256
            if mode == 'unavailable':raise httpx.ConnectError('synthetic-only')
            vector = [0.]*768
            vector[restricted if mode == 'restricted-projection' else target] = -1. if mode == 'no-match' else 1.
            return httpx.Response(200, json={'embeddings':[{'values':vector}],
                                            'usageMetadata':{'promptTokenCount':5}})
        monkeypatch.setattr(d, '_create_live_transport', lambda:httpx.MockTransport(send))
        operation = RetrievalOperationRequest(RetrievalOperationKey('query-'+name), query,
            p['cfg'].retrieval_profile, p['cfg'].applicability_policy, (),
            p['cfg'].capability.profile_reference, p['cfg'].capability.capability_identity)
        if mode == 'mapped-no-match':
            from runtime_orchestration.knowledge_query import bind_evidence_query
            operation = bind_evidence_query(operation, 'INC-1', success(incident_id='INC-1').snapshot,
                                            snapshots.read_operation(operation.operation_key))
        outcome = snapshots.resolve(operation, request.limits)
        if mode == 'unavailable':
            assert outcome.snapshot is None and outcome.resolution.value == 'RETRIEVAL_UNAVAILABLE'
            return outcome, operation, request.limits
        assert outcome.snapshot is not None
        # Durable operation replay never spends a second query attempt.
        before = len(calls)
        assert snapshots.resolve(operation, request.limits).snapshot == outcome.snapshot
        assert len(calls) == before
        return outcome.snapshot, operation, request.limits
    try:
        yield snapshots, resolve, calls, p
    finally:
        store.close()


@pytest.mark.parametrize('mode,expected', [('match','MATCH'), ('no-match','NO_MATCH'),
                                         ('unavailable','RETRIEVAL_UNAVAILABLE')])
def test_guarded_query_to_chroma_immutable_snapshot(retrieved, mode, expected):
    snapshots, resolve, calls, p = retrieved
    snapshot, operation, limits = resolve(mode, mode)
    assert snapshot.resolution.value == expected
    assert len(calls) == 1 and len(p['calls']) == 6
    if mode == 'unavailable':
        assert snapshot.snapshot is None
        return
    provenance = snapshots.read_provenance(snapshot.snapshot_key).value
    assert provenance.embedding_model == 'gemini-embedding-2'
    assert provenance.corpus_identity == p['admission'].manifest.corpus_id
    # A contradictory operation cannot mutate the frozen snapshot or invoke the provider.
    from knowledge_index import CanonicalKnowledgeQuery
    changed = replace(operation, query=replace(operation.query, text='contradictory query'))
    try:
        rejected = snapshots.resolve(changed, limits)
        assert rejected.snapshot is None or rejected.snapshot == snapshot
        assert rejected.failures
    except ValueError:
        pass
    assert snapshots.read_snapshot(snapshot.snapshot_key).value == snapshot
    assert len(calls) == 1


@pytest.mark.parametrize('mode', ['match','no-match','mapped-no-match'])
def test_query_snapshot_to_validated_rca_publication(retrieved, case, tmp_path, mode):
    """Single existing A/B/C/D pipeline, isolated durable stores, synthetic LLM."""
    from _incident_evidence_store_testkit import success
    from test_spec_016_followup_refresh import _B
    from test_spec_016_attempt_retry import _Time, NOW
    from test_llm_generation_service import environment
    from test_incident_manager import _create_request
    from incident_management import SqliteIncidentStore, IncidentManager
    from rca_persistence import (SqliteRcaStore, CreateAggregateRequest, AdmitAttemptRequest,
                                 PublicationDisposition)
    from llm_generation.facade import CandidateDHandoffFacade
    from runtime_orchestration.contracts import RuntimeWorkKind, RuntimeWorkRecord, RuntimeWorkStatus
    from runtime_orchestration.sqlite_work_store import SqliteRuntimeWorkStore
    from runtime_orchestration.identity import runtime_work_id
    from runtime_orchestration.rca_continuation import rca_root_id
    from runtime_orchestration.rca_execution import RcaAttemptExecutor, RcaExecutionRequest, RcaExecutionDisposition
    from runtime_orchestration.retry import DurableRetryController
    from rca_integration import RcaPublicationCoordinator
    snapshots, resolve, _, _ = retrieved
    snapshot, _, _ = resolve(mode, mode)
    evidence = _B(tmp_path/'evidence.sqlite3')
    captured = success(incident_id='INC-1')
    evidence.store.commit_success(captured)
    source = replace(case.content.input, evidence_snapshot_id=captured.snapshot.snapshot_id,
        evidence_revision_id=captured.revision.revision_id,
        knowledge_snapshot_id=snapshot.snapshot_key.value, knowledge_resolution=snapshot.resolution)
    refs = tuple(replace(x, evidence_snapshot_id=captured.snapshot.snapshot_id) for x in case.content.evidence_references)
    case.content = replace(case.content, input=source, evidence_references=refs,
                           knowledge_gap=snapshot.knowledge_gap)
    old = case.attempts.get_attempt_lineage('attempt-1').attempt.lineage
    lineage = replace(old, evidence_snapshot_id=source.evidence_snapshot_id,
        evidence_revision_id=source.evidence_revision_id, knowledge_snapshot_id=source.knowledge_snapshot_id)
    a = SqliteRcaStore(tmp_path/'rca.sqlite3')
    a.create_or_discover_aggregate(CreateAggregateRequest('aggregate', lineage.aggregate_id, 'INC-1', NOW))
    a.admit_attempt(AdmitAttemptRequest('admit', lineage, NOW))
    case.attempts = a; case.evidence = evidence; case.knowledge = snapshots
    generation, provider, _, resources = environment(case, tmp_path/'generation')
    work = SqliteRuntimeWorkStore(tmp_path/'runtime.sqlite3')
    incident_store = SqliteIncidentStore(str(tmp_path/'incident.sqlite3'))
    manager = IncidentManager(incident_store, incident_id_factory=lambda:'INC-1')
    manager.apply_correlation_mutation(_create_request())
    root = rca_root_id('INC-1'); clock = _Time().clock()
    work.create(RuntimeWorkRecord(runtime_work_id(RuntimeWorkKind.RCA_INITIAL, root),
        RuntimeWorkKind.RCA_INITIAL, 'EVT-1', 'INITIAL', 'INITIAL', 0, 4,
        RuntimeWorkStatus.OUTSTANDING, NOW, NOW, NOW, incident_id='INC-1', operation_id=root))
    executor = RcaAttemptExecutor(candidate_a=a, candidate_d=generation, d_store=generation.store,
        handoff=CandidateDHandoffFacade(generation.store, a, evidence, snapshots), work_store=work,
        retry=DurableRetryController(work_store=work, retry_delays_seconds=(1,2,4,8), clock=clock), clock=clock)
    try:
        result = executor.step(RcaExecutionRequest('INC-1','attempt-1',case.content.input,case.config,resources))
        assert result.disposition is RcaExecutionDisposition.ARTIFACT_COMMITTED
        assert len(provider.calls) == 1
        assert result.version.artifact.knowledge_gap == snapshot.knowledge_gap
        # Evidence facts remain incident-sourced; SOP context is no root-cause proof.
        assert case.content.knowledge_references == ()
        assert snapshot.snapshot_key.value in provider.calls[0].prompt
        provenance = snapshots.read_provenance(snapshot.snapshot_key).value
        assert provenance.embedding_model == 'gemini-embedding-2'
        # Inspect the actual rendered provider prompt, not only an opaque ID.
        knowledge = json.loads(json.loads(provider.calls[0].prompt)['knowledge'])
        assert knowledge['snapshot_id'] == snapshot.snapshot_key.value
        assert knowledge['snapshot_commitment'] == snapshot.snapshot_commitment
        assert knowledge['resolution'] == snapshot.resolution.value
        assert knowledge['knowledge_gap'] == snapshot.knowledge_gap
        assert len(knowledge['chunks']) == len(snapshot.chunks)
        if mode == 'match':
            assert snapshot.chunks
            included = {fact.chunk_identity:fact for fact in provenance.chunks if fact.included}
            for projected, chunk in zip(knowledge['chunks'], snapshot.chunks, strict=True):
                fact = included[chunk.chunk_identity]
                assert projected == dict(chunk_id=chunk.chunk_identity,
                    document_id=chunk.document_identity, document_version=chunk.document_version_identity,
                    section_id=chunk.section_identity, content=chunk.content,
                    content_commitment=chunk.content_commitment, knowledge_type=chunk.knowledge_type,
                    guidance_authority=chunk.guidance_authority, sop_backed_eligible=chunk.sop_backed_eligible,
                    content_truncated=chunk.content_truncated)
                assert chunk.content and fact.content_commitment == chunk.content_commitment
                assert fact.metadata_commitment == chunk.metadata_commitment
                assert fact.section_identity == chunk.section_identity
        else:
            assert knowledge['chunks'] == [] and knowledge['knowledge_gap'] is True
        published = RcaPublicationCoordinator(a, manager, incident_store).publish(
            result.version.publication_operation_id, NOW)
        assert published.disposition is PublicationDisposition.APPLIED
        current = a.get_current(lineage.aggregate_id)
        assert current.version.artifact == result.version.artifact
        assert current.version.artifact.claim_semantics.knowledge_references == ()
        from knowledge_index import KnowledgeSnapshotKey
        key = KnowledgeSnapshotKey(current.version.artifact.provenance.knowledge_snapshot_id)
        assert snapshots.read_snapshot(key).value == snapshot
        assert snapshots.read_provenance(key).value == provenance
        # No invented per-claim citations: the artifact preserves the snapshot
        # reference, from which public C reads resolve exact section/chunk facts.
        assert current.version.artifact.provenance.knowledge_snapshot_id == knowledge['snapshot_id']
        assert current.version.artifact.knowledge_gap is (mode != 'match')
        if mode == 'mapped-no-match':
            from knowledge_index import OpaqueExternalReference, OpaqueReferenceType
            from runtime_orchestration.knowledge_query import MAPPING_VERSION
            assert OpaqueExternalReference(OpaqueReferenceType.CALLER,MAPPING_VERSION) in snapshot.external_references
            assert OpaqueExternalReference(OpaqueReferenceType.EVIDENCE_SNAPSHOT,source.evidence_snapshot_id) in snapshot.external_references
        assert incident_store.get_rca_relationship('INC-1').current_version_id == result.version.version_id
    finally:
        work.close(); a.close(); evidence.close(); incident_store.close()


def test_original_rca_projection_security_boundary_remains_fail_closed(retrieved):
    from llm_generation.service import canonical_knowledge_projection
    snapshots, resolve, calls, _ = retrieved
    snapshot, _, _ = resolve('restricted', 'restricted-projection')
    assert snapshot.resolution.value == 'MATCH'
    provenance = snapshots.read_provenance(snapshot.snapshot_key).value
    with pytest.raises(ValueError, match='bounded, non-secret'):
        canonical_knowledge_projection(snapshot, provenance)
    assert len(calls) == 1

@pytest.fixture(autouse=True)
def sealed(offline_sdk,monkeypatch):
    def forbidden():pytest.fail('formal host store resolver forbidden')
    monkeypatch.setattr(c,'_localappdata_known_folder',forbidden)
    monkeypatch.setattr(c,'_programdata_known_folder',forbidden)
    yield
    assert offline_sdk.blocked_calls==[]

def thaw(value):
    if isinstance(value,(dict,MappingProxyType)):return {k:thaw(v) for k,v in value.items()}
    if isinstance(value,tuple):return [thaw(v) for v in value]
    return value

def bind_query(request,base,usage,monkeypatch,name='one'):
    """All numerical evidence here is SYNTHETIC, never a live budget/token claim."""
    _,binding,artifact,_=base
    kind,ids,texts=d._envelopes(request);text=texts[0]
    r=replace(binding.record,campaign_id='offline-query-'+name,input_kind='query',
        credential_profile_identity=request.capability.profile_reference.value,
        input_scope_identity='build:'+request.frozen_build_identity,
        input_commitment=d.input_commitment(kind,ids,texts),maximum_invocations=1,maximum_items=1,
        maximum_item_bytes=16384,maximum_payload_bytes=100000,maximum_usage_units=1000000)
    a=c.ApprovedDiagnosticInput(1,r.campaign_id,r.source_revision,r.purpose,'query',
                               r.input_scope_identity,r.input_commitment,ids,texts)
    prepared=d._preflight(load_record=lambda:r,
        resolve_facts=lambda *_:facts(credential_profile_identity=r.credential_profile_identity),
        safety=lambda rec,ts:(d._offline_proof(rec),d._offline_safety_basis(rec,ts),
                             sum(len(t.encode()) for t in ts),100),
        request=request,live_opt_in=True,offline_harness=True)
    e,_=_host_e4_observation(prepared,a)
    value=thaw(artifact.fields)
    value['binding'].update(campaign_id=r.campaign_id,source_revision=r.source_revision,
        authorization_record_sha256=c.authorization_record_digest(r),kind='query',
        input_scope_identity=r.input_scope_identity,input_commitment=r.input_commitment)
    value['credential_binding']['profile_identity']=r.credential_profile_identity
    value['e4_reference'].update(reference=e.reference,sdk_body_sha256=e.sdk_body_sha256,
        canonical_json_sha256=e.canonical_json_sha256,sdk_body_bytes=e.sdk_body_bytes)
    bounds={sha256(text.encode()).hexdigest():len(text.encode())+16}
    value['accepted_input_token_bounds']=bounds
    value['token_evidence'].update(data_sha256=sha256(json.dumps(bounds,sort_keys=True,separators=(',',':')).encode()).hexdigest())
    s=safety_parse(value)
    b=c.LiveHostBinding(r,a,None,r.runner_identity,e)
    monkeypatch.setattr(d,'load_live_binding',lambda:b)
    monkeypatch.setattr(d,'load_live_safety_artifact',lambda:s)
    monkeypatch.setattr(d,'resolve_repository_source',lambda _:(r.source_revision,True))
    monkeypatch.setattr(d.socket,'gethostname',lambda:r.authorized_host)
    monkeypatch.setattr(d,'LiveDiagnosticUsage',lambda:usage)
    monkeypatch.setenv('GEMINI_CREDENTIAL_PROFILE',r.credential_profile_identity)
    monkeypatch.setenv('GEMINI_CREDENTIAL_REVISION','r1')
    monkeypatch.setenv('GEMINI_API_KEY','synthetic-query-only')
    return r,a,e,s

@pytest.fixture
def query_case(campaign,tmp_path,monkeypatch):
    base=campaign(kind='query')
    request=original_request(base[0],'query',tmp_path,1)
    usage=base[3]
    r,a,e,s=bind_query(request,base,usage,monkeypatch)
    calls=[];credentials=[]
    def resolve(*args):
        with closing(usage._connect(read_only=True)) as db:
            assert db.execute('SELECT outcome FROM attempts WHERE campaign_id=?',(r.campaign_id,)).fetchone()==('IN_FLIGHT',)
        credentials.append(True)
        return c.resolve_diagnostic_credential(*args)
    monkeypatch.setattr(d,'resolve_diagnostic_credential',resolve)
    def transport(mode):
        def send(req):
            calls.append(req)
            assert sha256(req.content).hexdigest()==e.sdk_body_sha256
            body=json.loads(req.content);items=body['requests']
            assert len(items)==1 and items[0]['outputDimensionality']==768
            assert items[0]['content']['parts'][0]['text']==a.envelopes[0]
            assert not {'taskType','autoTruncate'} & set(items[0])
            if mode=='timeout':raise httpx.ReadTimeout('synthetic query timeout')
            vector=[1.]+[0.]*767
            if mode=='dimension':vector.pop()
            if mode=='zero':vector[0]=0.
            if mode=='norm':vector[0]=2.
            payload={'embeddings':[{'values':vector}]}
            if mode!='unknown_usage':payload['usageMetadata']={'promptTokenCount':5}
            return httpx.Response(200,json=payload)
        monkeypatch.setattr(d,'_create_live_transport',lambda:httpx.MockTransport(send))
    return request,usage,calls,credentials,transport,r

@pytest.mark.parametrize('mode',['success','unknown_usage','timeout','dimension','zero','norm'])
def test_guarded_query_projection_and_fences(query_case,mode):
    req,usage,calls,creds,transport,r=query_case;transport(mode)
    provider=GoogleEmbeddingAdapter(None,req.capability,controlled_live_opt_in=True)
    result=provider.embed_query(req)
    if mode=='success':assert len(result.vector)==768 and result.failure_detail is None
    else:assert result.vector==() and result.failure_detail
    assert result.invocation_count==len(calls)==len(creds)==1
    with pytest.raises(c.DiagnosticDenied):provider.embed_query(req)
    assert len(calls)==len(creds)==1

def test_query_is_not_document_authority_or_implicit_opt_in(query_case,monkeypatch):
    req,usage,calls,creds,transport,r=query_case;transport('success')
    assert GoogleEmbeddingAdapter(None,req.capability).embed_query(req).failure_detail
    with pytest.raises(c.DiagnosticDenied):d.embed_query_for_retrieval(req,live_opt_in=False)
    binding=d.load_live_binding()
    monkeypatch.setattr(d,'load_live_binding',lambda:replace(binding,record=replace(r,input_kind='document')))
    with pytest.raises(c.DiagnosticDenied):
        GoogleEmbeddingAdapter(None,req.capability,controlled_live_opt_in=True).embed_query(req)
    assert calls==creds==[]

def test_historical_unknowns_still_block_queries(query_case):
    req,usage,calls,creds,transport,r=query_case;transport('success')
    with closing(usage._connect()) as db:
        for row in (c._P0_HISTORY_ROW,c._P0_SECOND_HISTORY_ROW):
            v=dict(row);v.pop('observation_json')
            db.execute('INSERT INTO attempts('+','.join(v)+') VALUES('+','.join('?' for _ in v)+')',tuple(v.values()))
        db.commit()
    with pytest.raises(c.DiagnosticDenied) as denied:
        GoogleEmbeddingAdapter(None,req.capability,controlled_live_opt_in=True).embed_query(req)
    assert denied.value.code=='RUNNER_CONFLICT'
    assert calls==creds==[]


def test_query_completion_failure_releases_no_vector(query_case, monkeypatch):
    req, usage, calls, creds, transport, _ = query_case
    transport('success')
    def fail(*args, **kwargs):
        raise c.DiagnosticDenied('synthetic completion failure', code='PERSISTENCE_FAILURE')
    monkeypatch.setattr(usage, '_finish_provider_usage', fail)
    with pytest.raises(c.DiagnosticDenied, match='completion'):
        GoogleEmbeddingAdapter(None, req.capability, controlled_live_opt_in=True).embed_query(req)
    assert len(calls) == len(creds) == 1  # A post-transport failure never claims zero calls.


def test_query_canonicalization_and_cli_opt_in_boundary(query_case, monkeypatch):
    from knowledge_index import cli
    from knowledge_index.contracts import KnowledgeValidationError
    req, _, calls, creds, _, _ = query_case
    with pytest.raises(KnowledgeValidationError):
        CanonicalKnowledgeQuery('1.0', '1.0', ' not canonical ')
    with pytest.raises(KnowledgeValidationError):
        CanonicalKnowledgeQuery('1.0', '2.0', 'query')
    parser = cli.build_parser()
    args = parser.parse_args(['retrieve','--operation-key','query','--query','query','--live-opt-in'])
    assert args.live_opt_in is True
    args.live_opt_in = False
    from types import SimpleNamespace
    def forbidden(*a, **kw):pytest.fail('no store or historical client without opt-in')
    monkeypatch.setattr(cli, '_store', forbidden)
    monkeypatch.setattr(cli, 'create_google_client', forbidden)
    for live in (False, True):
        adapter = cli._provider(SimpleNamespace(capability=req.capability), live=live)
        assert isinstance(adapter, GoogleEmbeddingAdapter)
        assert adapter._client is None and adapter._controlled_live_opt_in is live
    with pytest.raises(ValueError, match='explicit'):
        cli._run(args, SimpleNamespace(capability=req.capability))
    assert calls == creds == []


def test_runtime_query_opt_in_is_explicit_and_separate(monkeypatch):
    from runtime_orchestration import cli
    from runtime_orchestration.worker import RuntimeStopController
    from types import SimpleNamespace
    seen = []
    class App:
        worker = SimpleNamespace(stop_controller=RuntimeStopController())
        def run(self, mode):pass
        def close(self):pass
    def factory(*a, **kw):
        seen.append(kw)
        return App()
    assert cli.main([], application_factory=factory) == 0
    assert cli.main(['--knowledge-live-opt-in'], application_factory=factory) == 0
    assert 'knowledge_live_opt_in' not in seen[0]
    assert seen[1]['knowledge_live_opt_in'] is True


@pytest.mark.parametrize('opt_in', [False, True])
def test_runtime_composes_guarded_gemini2_without_historical_client(tmp_path, monkeypatch, opt_in):
    import sys
    import yaml
    from types import SimpleNamespace
    from runtime_orchestration import cli
    from runtime_orchestration.config import load_runtime_config
    from knowledge_index.config import select_gemini_embedding2_capability
    from knowledge_index import OpaqueExternalReference, OpaqueReferenceType
    from test_spec_016_runtime_host import _cli_config
    root = tmp_path/'runtime-composition'
    config_path = _cli_config(root)
    path = root/'configs/knowledge_index.yaml'
    value = yaml.safe_load(path.read_text(encoding='utf-8'))
    cap = select_gemini_embedding2_capability(OpaqueExternalReference(
        OpaqueReferenceType.CREDENTIAL_PROFILE, 'synthetic-query-profile'))
    value['embedding'].update(model=cap.model, profile_reference=cap.profile_reference.value,
        capability_identity=cap.capability_identity, embedding_profile_identity=cap.embedding_profile_identity,
        normalization_semantics='provider-normalized-conditional-v1')
    path.write_text(yaml.safe_dump(value), encoding='utf-8')
    class Client:
        def close(self):pass
    monkeypatch.setitem(sys.modules, 'chromadb', SimpleNamespace(PersistentClient=lambda **kw:Client()))
    monkeypatch.setitem(sys.modules, 'chromadb.config', SimpleNamespace(Settings=lambda **kw:object()))
    def forbidden(*args, **kw):pytest.fail('no credential/client discovery during composition')
    monkeypatch.setattr(cli, 'create_google_client', forbidden)
    monkeypatch.setattr(cli, 'create_google_generation_client', forbidden)
    observed = []
    original = cli.GoogleEmbeddingAdapter
    def provider(client, capability, **kwargs):
        observed.append((client, capability, kwargs))
        return original(client, capability, **kwargs)
    monkeypatch.setattr(cli, 'GoogleEmbeddingAdapter', provider)
    app = cli.build_runtime_application(load_runtime_config(config_path), project_root=root,
        config_path=config_path, knowledge_live_opt_in=opt_in)
    try:
        assert observed == [(None, cap, {'controlled_live_opt_in':opt_in})]
    finally:
        app.close()


@pytest.mark.parametrize('kind', ['query','full-document'])
def test_single_probe_two_review_grant_does_not_extend_to_other_operations(two_reviewed, kind):
    r = two_reviewed['target']
    with pytest.raises(c.DiagnosticDenied) as denied:
        if kind == 'query':
            replace(r, input_kind='query', bounds_basis='fixture-bounds',
                    project_identity=None, credential_profile_revision=None)
        else:
            replace(r, maximum_items=54, maximum_invocations=6,
                    bounds_basis='SEQUENTIAL_BATCH_PLAN_V1:'+('a'*64),
                    project_identity=None, credential_profile_revision=None)
    assert denied.value.code == 'HISTORICAL_REVIEW_MISMATCH'
    assert two_reviewed['calls'] == two_reviewed['credentials'] == []
