"""Target03 repair: SDK MockTransport + native D grounding/persistence only."""
from copy import deepcopy
from dataclasses import asdict, fields, replace
from hashlib import sha256
import json
import os
import socket
from types import SimpleNamespace

import httpx
import jsonschema
import pytest

from llm_generation import google_generation_adapter as google
from llm_generation.config import load_generation_config
from llm_generation.contracts import (Claim, EvidenceReference, GenerationFailure, Guidance,
    Hypothesis, InvocationMetadata, KnowledgeReference, LocalReadStatus, ParserFailureEvidence)
from llm_generation.identity import semantic_commitment
from llm_generation.projection import project_artifact
from llm_generation.schema import result_json_schema_v1
from llm_generation.service import ProviderCapability, ProviderResponse
from llm_generation.sqlite_store import CandidateDStore, decode, encode
from rca_persistence.contracts import GenerationProvenance
from test_llm_generation_phase2 import case, _structured
from test_llm_generation_service import environment

EXTERNAL = __import__('pathlib').Path(r'C:\Users\vincent\AppData\Local\Temp\aiops-s2-cd-integration-20261120-yfgzsinh')


@pytest.fixture(autouse=True)
def sealed(monkeypatch):
    monkeypatch.setattr(os, 'environ', {})
    def denied(*args, **kwargs):
        raise AssertionError('no actual network or credential discovery')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


def test_schema_complete_native_shape(case):
    schema = result_json_schema_v1()
    data = _structured(case.content, case.config)
    data.update(schema_identity='spec015-rca-result-v1')
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(data, schema)
    assert set(schema['properties']) == set(data) == set(schema['required'])
    for key, cls in [('claims', Claim), ('hypotheses', Hypothesis), ('remediation', Guidance),
                     ('evidence_references', EvidenceReference), ('knowledge_references', KnowledgeReference)]:
        expected = {f.name for f in fields(cls)} - ({'causal_assertion'} if cls is Claim else set())
        child = schema['properties'][key]['items']
        assert set(child['properties']) == set(child['required']) == expected
        assert child['additionalProperties'] is False


def test_pinned_sdk_schema_to_grounded_durable_result_and_projection(case, tmp_path):
    from google import genai
    from google.genai import types
    config = load_generation_config(EXTERNAL/'generation-demo.json')
    assert config.pin.profile.version == '2'
    source = replace(case.content.input, pin=config.pin)
    case.config = config
    case.content = replace(case.content, input=source,
        validation_facts=(replace(case.content.validation_facts[0], commitment=config.result_schema_commitment),))
    original_read = case.attempts.get_attempt_lineage(source.try_identity.attempt_id)
    lineage = replace(original_read.attempt.lineage,
        generation_provenance=GenerationProvenance(config.pin.provider,config.pin.model,
            config.pin.prompt.identity,config.pin.configuration.identity,config.pin.profile.identity))
    case.attempts = SimpleNamespace(get_attempt_lineage=lambda _: replace(original_read,
        attempt=replace(original_read.attempt, lineage=lineage)))
    service, _, grant, resources = environment(case, tmp_path)
    data = _structured(case.content, config)
    jsonschema.validate(data, result_json_schema_v1())
    calls = []
    def handle(request):
        body = json.loads(request.content)
        calls.append(request.url.path)
        assert set(request.extensions['timeout'].values()) == {30.0}
        if request.url.path.endswith(':countTokens'):
            return httpx.Response(200, json={'totalTokens': 1024})
        assert request.url.path == '/v1beta/models/gemini-3.5-flash-lite:generateContent'
        wire = body['generationConfig']
        assert wire['responseJsonSchema'] == result_json_schema_v1()
        assert wire['responseMimeType'] == 'application/json'
        assert 'responseSchema' not in wire
        prompt = json.loads(body['contents'][0]['parts'][0]['text'])
        assert prompt['grounding_reference_catalog'][0]['canonical_path'] == '/event_projections/0'
        assert any(item['canonical_path'].startswith('/semantic_evidence/normalized_evidence/')
                   for item in prompt['grounding_reference_catalog'])
        assert len(prompt['grounding_reference_catalog']) <= 8
        assert len(request.content) <= config.bounds.maximum_request_bytes
        return httpx.Response(200, json={
            'modelVersion':config.pin.model, 'candidates':[{'finishReason':'STOP',
                'content':{'role':'model','parts':[{'text':json.dumps(data)}]}}],
            'usageMetadata':{'promptTokenCount':1024,'candidatesTokenCount':256,
                             'thoughtsTokenCount':8,'totalTokenCount':1288}})
    sdk = genai.Client(api_key='synthetic-offline-only',vertexai=False,
        http_options=types.HttpOptions(api_version='v1beta',timeout=30000,
            retry_options=types.HttpRetryOptions(attempts=1),
            client_args={'transport':httpx.MockTransport(handle),'trust_env':False}))
    try:
        cap=ProviderCapability(config.pin.provider,config.pin.model,config.pin.profile.identity,
            config.pin.result_schema.identity,config.pin.result_schema.version,True)
        service.provider=google.GoogleGenerationAdapter(cap,profile_version='2',client=sdk)
        receipt=service.execute(case.content.input,config,grant,resources)
        assert receipt.result is not None and receipt.failure is None
        assert receipt.result.content.invocation_metadata.total_tokens == 1288
        assert project_artifact(receipt.result).summary == receipt.result.content.summary
        assert CandidateDStore(service.store.path,resource_config_path=service.store.resource_config_path).result(
            receipt.result.validated_result_id).value == receipt.result
        replay=service.execute(case.content.input,config,grant,resources)
        assert replay.result == receipt.result and replay.physical_invocations == 0
        assert len(calls)==2 and service.provider.generation_calls==service.provider.token_preflight_calls==1
    finally:
        sdk.close()


@pytest.mark.parametrize('kind,expected',[
    ('syntax','JSON_SYNTAX'),('missing','OBJECT_FIELDS'),('extra','OBJECT_FIELDS'),
    ('duplicate','DUPLICATE_FIELDS'),('scalar','UNSUPPORTED_SCALAR'),('root_array','OBJECT_FIELDS'),
])
def test_failure_evidence_durable_sanitized_no_raw(case,tmp_path,kind,expected):
    data=_structured(case.content,case.config)
    if kind=='syntax': text='{"summary":'
    elif kind=='duplicate': text='{"summary":"a","summary":"b"}'
    elif kind=='missing': data.pop('summary');text=json.dumps(data)
    elif kind=='extra': data['unexpected']='PRIVATE_RESPONSE_MARKER';text=json.dumps(data)
    elif kind=='scalar': data['summary']=1.25;text=json.dumps(data)
    else: text='[]'
    usage=InvocationMetadata(1,12,8,20,10)
    service,provider,grant,resources=environment(case,tmp_path,outcome=ProviderResponse(text,usage))
    before=service.store.path.read_bytes()
    receipt=service.execute(case.content.input,case.config,grant,resources)
    assert receipt.result is None and receipt.failure.parser_evidence == ParserFailureEvidence(
        expected,sha256(text.encode()).hexdigest(),usage)
    assert receipt.physical_invocations==receipt.token_preflight_calls==1 and len(provider.calls)==1
    reopened=CandidateDStore(service.store.path,resource_config_path=service.store.resource_config_path)
    assert reopened.failures_for_try(case.content.input.try_identity).value[-1]==receipt.failure
    assert b'PRIVATE_RESPONSE_MARKER' not in service.store.path.read_bytes()
    assert text.encode() not in service.store.path.read_bytes() if len(text)>30 else True
    assert service.store.path.read_bytes()!=before


def test_legacy_failure_codec_and_commitment_preserved(case):
    from llm_generation.contracts import FailureClass
    from rca_persistence.contracts import AdmittedRetryDisposition
    failure=GenerationFailure(case.content.input.try_identity,'operation-legacy',
        FailureClass.STRUCTURED_PARSE,AdmittedRetryDisposition.RETRYABLE,'structured result rejected')
    encoded=encode(failure)
    assert 'parser_evidence' not in encoded['fields']
    assert decode(encoded)==failure
    old_plain={k:v for k,v in asdict(failure).items() if k not in ('parser_evidence','boundary_evidence')}
    old_plain['failure_class']=failure.failure_class.value
    old_plain['retry_safety']=failure.retry_safety.value
    expected=sha256(('spec015-result-v1\n'+json.dumps(old_plain,sort_keys=True,
        ensure_ascii=False,separators=(',',':'))).encode()).hexdigest()
    assert semantic_commitment(failure)==expected


@pytest.mark.parametrize('change', [
    {'code':'UNREVIEWED_RESPONSE_TEXT'}, {'response_sha256':'not-a-hash'}, {'usage':None},
])
def test_parser_evidence_tamper_rejected(change):
    value=dict(code='OBJECT_FIELDS',response_sha256='a'*64,usage=InvocationMetadata(1,1,1,2,0))
    value.update(change)
    with pytest.raises((TypeError,ValueError)):
        ParserFailureEvidence(**value)
