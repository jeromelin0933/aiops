"""OFFLINE ONLY: real approved corpus, synthetic authority/key/usage/vectors.

The production campaign, reservation, completion and Build/Chroma/Snapshot code
run unchanged against isolated stores. This is NOT real embedding/RAG evidence.
"""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import httpx
import pytest
import yaml

from knowledge_index import (
    ActivationOperationKey, BuildActivationRequest, BuildOperationKey,
    CanonicalKnowledgeQuery, KnowledgeBuildService, KnowledgeRetrievalService,
    KnowledgeSnapshotService, OpaqueExternalReference, OpaqueReferenceType,
    ProviderEmbeddingRequest, QueryEmbeddingResult, QueryFilter,
    RetrievalOperationKey, RetrievalOperationRequest, SqliteKnowledgeStore, EmbeddingVector,
)
from knowledge_index import gemini_diagnostic as d
from knowledge_index import gemini_diagnostic_controls as c
from knowledge_index.config import load_knowledge_config, select_gemini_embedding2_capability
from knowledge_index.cli import _identity_input, _provider, _run, build_parser
from knowledge_index.chunking import plan_chunks
from knowledge_index.chroma_index_adapter import ChromaIndexAdapter
from knowledge_index.identity import build_identity
from knowledge_index.manifest import admit_production_manifest
from knowledge_index.google_embedding_adapter import GoogleEmbeddingAdapter
from test_knowledge_gemini_diagnostic_batching import batched
from test_knowledge_gemini_embedding2_serialization import offline_sdk
from test_knowledge_gemini_diagnostic_single_probe import probe

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def sealed(offline_sdk, monkeypatch):
    def forbidden():
        pytest.fail('real Host store resolver is forbidden')
    monkeypatch.setattr(c, '_programdata_known_folder', forbidden)
    monkeypatch.setattr(c, '_localappdata_known_folder', forbidden)
    yield
    assert offline_sdk.blocked_calls == []


@pytest.fixture
def setup_build(tmp_path, monkeypatch, batched):
    # The SDK isolation fixture deliberately supplies an empty environment.
    # Keep optional Chroma cache path discovery inside the isolated test root.
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    cap = select_gemini_embedding2_capability(
        OpaqueExternalReference(OpaqueReferenceType.CREDENTIAL_PROFILE, 'synthetic-build-profile'))
    cfg = load_knowledge_config(ROOT/'configs/knowledge_index.yaml')
    cfg = replace(cfg, capability=cap, normalization_semantics='provider-normalized-conditional-v1',
        limits=replace(cfg.limits, maximum_invocations=6, maximum_batch_items=54,
                       maximum_request_bytes=100000),
        retrieval_profile=replace(cfg.retrieval_profile, model=cap.model,
            profile_identity='offline-gemini2-retrieval-unready-v1',
            embedding_profile_identity=cap.embedding_profile_identity),
        applicability_policy=replace(cfg.applicability_policy,
            policy_identity='offline-gemini2-policy-unready-v1'))
    admission = admit_production_manifest('configs/knowledge_manifest.json', repository_root=ROOT)
    chunks = plan_chunks(admission, source_root=ROOT/'docs/knowledge', profile=cfg.chunking)
    identity = _identity_input(cfg, admission, chunks)
    mapping = tuple(sorted((x.document_identity, next(doc.document_id for doc in admission.manifest.documents
        if c.document_identity(admission.manifest.corpus_id, doc.document_id) == x.document_identity))
        for x in admission.admitted_documents))
    request = ProviderEmbeddingRequest(build_identity(identity), cap, chunks, cfg.limits,
                                       mapping, admission.manifest.corpus_id)
    request, binding, value, usage = batched(known_usage=False, request_override=request)
    calls = []
    ids = {text:i for i,text in enumerate(d._envelopes(request)[2])}
    bounds = value['accepted_input_token_bounds']

    def transport(*, failure=None):
        def send(r):
            body = json.loads(r.content)
            texts = [row['content']['parts'][0]['text'] for row in body['requests']]
            calls.append(body)
            if len(calls) == 2 and failure == 'timeout':
                raise httpx.ReadTimeout('synthetic-only')
            vectors = [[0.]*ids[text]+[1.]+[0.]*(767-ids[text]) for text in texts]
            if len(calls) == 2:
                if failure == 'partial':vectors.pop()
                if failure == 'dimension':vectors[0].pop()
                if failure == 'zero':vectors[0]=[0.]*768
                if failure == 'norm':vectors[0][ids[texts[0]]]=2.
            payload = {'embeddings':[{'values':v} for v in vectors]}
            if failure != 'unknown_usage':
                payload['usageMetadata']={'promptTokenCount':sum(bounds[d.sha256(t.encode()).hexdigest()] for t in texts)}
            return httpx.Response(200, json=payload)
        monkeypatch.setattr(d,'_create_live_transport',lambda:httpx.MockTransport(send))
    transport()
    provider = GoogleEmbeddingAdapter(None, cap, controlled_live_opt_in=True)
    index = ChromaIndexAdapter(tmp_path/'chroma', index_schema_identity=cfg.index_schema_identity,
                               distance_metric=cfg.distance_metric)
    raw = json.loads((ROOT/cfg.manifest_path).read_bytes())
    return dict(cfg=cfg, admission=admission, chunks=chunks, identity=identity, request=request,
        binding=binding, value=value, usage=usage, calls=calls, transport=transport,
        provider=provider, index=index, raw=raw)


def stage(service, p):
    return service.stage(operation_key=BuildOperationKey('offline-gemini2-stage'), raw_manifest=p['raw'],
        source_root=ROOT/'docs/knowledge', chunks=p['chunks'], identity_input=p['identity'],
        limits=p['cfg'].limits, required_capability_identity=p['cfg'].capability.capability_identity)


def test_mock_campaign_to_real_chroma_validate_and_bounded_snapshot(setup_build, tmp_path):
    p=setup_build
    captured=[]
    class ProviderSpy:
        def capability(self):return p['provider'].capability()
        def embed(self, request):
            try:
                result=p['provider'].embed(request)
            except c.DiagnosticDenied as exc:
                pytest.fail('isolated campaign fixture denied: '+str(exc))
            captured.append(result)
            return result
    with SqliteKnowledgeStore(tmp_path/'knowledge.sqlite3') as store:
        service=KnowledgeBuildService(store,ProviderSpy(),p['index'])
        result=stage(service,p)
        assert not result.failures and result.record is not None
        record=result.record
        assert record.chunks==p['chunks'] and record.document_count==6
        assert captured[0].invocation_count==6 and len(captured[0].embeddings)==54
        assert tuple(x.chunk_identity for x in captured[0].embeddings)==tuple(x.chunk_identity for x in p['chunks'])
        assert record.artifact.model=='gemini-embedding-2' and len(record.artifact.entries)==54
        assert stage(service,p).record==record and len(p['calls'])==6
        validation=service.validate(record.build_identity,BuildOperationKey('offline-gemini2-validate'),maximum_probe_results=12)
        assert not validation.findings
        # Explicit activation in an ISOLATED test store, never Production/Demo authority.
        service.activate(BuildActivationRequest(record.build_identity,ActivationOperationKey('offline-activate'),0))
        class MockQuery:
            def capability(self):return p['cfg'].capability
            def embed_query(self, request):
                return QueryEmbeddingResult(request.capability,
                    ((-1. if request.query.text=='offline no match' else 1.),)+(0.,)*767,
                    1,cost_units=1,rate_units=1,quota_units=1,resource_units=1)
        snapshots=KnowledgeSnapshotService(store,KnowledgeRetrievalService(store,MockQuery(),p['index']))
        doc=p['admission'].manifest.documents[0]
        metadata={x.key:x.value for x in doc.metadata}
        filters=tuple(QueryFilter(k,metadata[k]) for k in p['cfg'].applicability_policy.required_filter_keys)
        resolutions=[]
        for n,text in enumerate(('offline match','offline no match')):
            outcome=snapshots.resolve(RetrievalOperationRequest(RetrievalOperationKey('offline-query-'+str(n)),
                CanonicalKnowledgeQuery('1.0','1.0',text,filters),p['cfg'].retrieval_profile,
                p['cfg'].applicability_policy,(),p['cfg'].capability.profile_reference,
                p['cfg'].capability.capability_identity),replace(p['cfg'].limits,maximum_invocations=1))
            assert outcome.snapshot is not None
            resolutions.append(outcome.snapshot.resolution.value)
            provenance=snapshots.read_provenance(outcome.snapshot.snapshot_key)
            assert provenance.value.embedding_model=='gemini-embedding-2'
            assert provenance.value.corpus_identity==p['admission'].manifest.corpus_id
        assert resolutions==['MATCH','NO_MATCH']
        assert len(p['calls'])==6  # Query fixture never resends document embeddings.


@pytest.mark.parametrize('failure', ['timeout','partial','dimension','zero','norm','unknown_usage'])
def test_partial_or_unknown_campaign_never_stages_index(setup_build,tmp_path,failure):
    p=setup_build; p['transport'](failure=failure)
    with SqliteKnowledgeStore(tmp_path/'knowledge.sqlite3') as store:
        result=stage(KnowledgeBuildService(store,p['provider'],p['index']),p)
        assert result.record is None and result.failures
        assert p['index'].inspect(p['request'].build_identity) is None
        assert len(p['calls'])==(1 if failure=='unknown_usage' else 2)
        before=len(p['calls'])
        with pytest.raises(c.DiagnosticDenied):p['provider'].embed(p['request'])
        assert len(p['calls'])==before


def test_config_and_cli_select_controlled_adapter_without_historical_client(tmp_path,monkeypatch):
    from knowledge_index import cli
    value=yaml.safe_load((ROOT/'configs/knowledge_index.yaml').read_text())
    cap=select_gemini_embedding2_capability(OpaqueExternalReference(OpaqueReferenceType.CREDENTIAL_PROFILE,'synthetic-build-profile'))
    value['embedding'].update(model=cap.model,capability_identity=cap.capability_identity,
        embedding_profile_identity=cap.embedding_profile_identity,profile_reference=cap.profile_reference.value,
        normalization_semantics='provider-normalized-conditional-v1')
    value['embedding']['limits']['maximum_invocations']=6
    path=tmp_path/'gemini.yaml';path.write_text(yaml.safe_dump(value))
    cfg=load_knowledge_config(path)
    def forbidden(*a,**kw):pytest.fail('no historical client/store before explicit opt-in')
    monkeypatch.setattr(cli,'create_google_client',forbidden)
    monkeypatch.setattr(cli,'_store',forbidden)
    assert _provider(cfg,live=True).capability()==cap
    with pytest.raises(ValueError,match='explicit'):_run(build_parser().parse_args(['rebuild']),cfg)
    with pytest.raises(ValueError,match='query'):_run(build_parser().parse_args(['retrieve','--operation-key','offline','--query','test']),cfg)
    value['embedding']['capability_identity']='wrong-profile'
    path.write_text(yaml.safe_dump(value))
    with pytest.raises(ValueError):load_knowledge_config(path)


@pytest.mark.parametrize('change', ['invocations','payload','profile','order'])
def test_build_request_mismatch_denies_before_attempt(setup_build,change):
    p=setup_build; request=p['request']
    if change=='invocations':request=replace(request,limits=replace(request.limits,maximum_invocations=1))
    elif change=='payload':request=replace(request,limits=replace(request.limits,maximum_request_bytes=1))
    elif change=='profile':
        request=replace(request,capability=replace(request.capability,
            profile_reference=OpaqueExternalReference(OpaqueReferenceType.CREDENTIAL_PROFILE,'wrong-profile')))
    elif change=='order':request=replace(request,chunks=tuple(reversed(request.chunks)))
    try:
        result=p['provider'].embed(request)
    except c.DiagnosticDenied:
        pass
    else:
        assert result.failure is not None
    assert p['calls']==[]
    with p['usage']._connect(read_only=True) as db:
        assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()==(0,)


@pytest.mark.parametrize('after_reservation', [False,True])
def test_persistence_failure_never_releases_vectors_or_stages(setup_build,tmp_path,monkeypatch,after_reservation):
    p=setup_build
    original=p['usage']._connect
    connections=[]
    def fail_io(*a,**kw):
        connections.append(True)
        if not after_reservation or len(connections)>1:
            raise sqlite3.OperationalError('isolated I/O fault')
        return original(*a,**kw)
    monkeypatch.setattr(p['usage'],'_connect',fail_io)
    credentials=[]
    resolve=d.resolve_diagnostic_credential
    def credential(*a):credentials.append(True);return resolve(*a)
    monkeypatch.setattr(d,'resolve_diagnostic_credential',credential)
    with SqliteKnowledgeStore(tmp_path/'knowledge.sqlite3') as store:
        result=stage(KnowledgeBuildService(store,p['provider'],p['index']),p)
        assert result.record is None and result.failures
        assert p['index'].inspect(p['request'].build_identity) is None
    assert len(p['calls'])==len(credentials)==(1 if after_reservation else 0)
    with original(read_only=True) as db:
        rows=db.execute('SELECT outcome,usage_reserved,cost_reserved FROM attempts').fetchall()
        if after_reservation:
            assert len(rows)==1 and rows[0][0]=='IN_FLIGHT' and rows[0][1]>0 and rows[0][2]==1
        else:assert rows==[]


def test_probe_and_caller_client_cannot_supply_build_vectors(probe):
    p=probe
    provider=GoogleEmbeddingAdapter(None,p['request'].capability,controlled_live_opt_in=True)
    with pytest.raises(c.DiagnosticDenied):provider.embed(p['request'])
    assert p['calls']==p['credentials']==[] and p['rows']()==[]
    with pytest.raises(ValueError):
        GoogleEmbeddingAdapter(object(),p['request'].capability,controlled_live_opt_in=True)


def test_existing_cli_rebuild_uses_same_guarded_build_path(setup_build,tmp_path,monkeypatch):
    from knowledge_index import cli
    p=setup_build
    monkeypatch.setattr(cli,'_store',lambda cfg:SqliteKnowledgeStore(tmp_path/'knowledge.sqlite3'))
    monkeypatch.setattr(cli,'_index',lambda cfg:p['index'])
    def forbidden(*a,**kw):pytest.fail('historical SDK client must not be selected')
    monkeypatch.setattr(cli,'create_google_client',forbidden)
    result=_run(build_parser().parse_args(['rebuild','--live-opt-in']),p['cfg'])
    assert result.record is not None and not result.failures
    assert result.record.build_identity==p['request'].build_identity and len(p['calls'])==6


@pytest.mark.parametrize('tamper',['order','profile','invocations','norm'])
def test_build_rechecks_provider_result_before_index_stage(setup_build,tmp_path,tamper):
    p=setup_build
    class FaultyResult:
        def capability(self):return p['provider'].capability()
        def embed(self, request):
            result=p['provider'].embed(request)
            if tamper=='order':return replace(result,embeddings=tuple(reversed(result.embeddings)))
            if tamper=='profile':return replace(result,capability=replace(result.capability,
                profile_reference=OpaqueExternalReference(OpaqueReferenceType.CREDENTIAL_PROFILE,'wrong-profile')))
            if tamper=='invocations':return replace(result,invocation_count=7)
            first=result.embeddings[0]
            return replace(result,embeddings=(EmbeddingVector(first.chunk_identity,(2.,)+(0.,)*767),)+result.embeddings[1:])
    with SqliteKnowledgeStore(tmp_path/'knowledge.sqlite3') as store:
        result=stage(KnowledgeBuildService(store,FaultyResult(),p['index']),p)
        assert result.record is None and result.failures
        assert p['index'].inspect(p['request'].build_identity) is None
        assert len(p['calls'])==6
