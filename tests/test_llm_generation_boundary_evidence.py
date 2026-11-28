"""Bounded diagnostics: synthetic SDK/errors, TEMP D store, no live authority."""
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os
import socket
from types import SimpleNamespace

import httpx
import pytest
from google.genai import errors, types
from pydantic import ValidationError

from llm_generation.contracts import GenerationFailure, ProviderBoundaryEvidence, FailureClass
from llm_generation.google_generation_adapter import GoogleGenerationAdapter, _boundary
from llm_generation.identity import semantic_commitment
from llm_generation.service import ProviderFailureKind, ProviderInvocationError
from llm_generation.sqlite_store import CandidateDStore, encode, decode
from rca_persistence.contracts import AdmittedRetryDisposition
from test_llm_generation_google_adapter import capability35, request35, Mock35Models
from test_llm_generation_phase2 import case
from test_llm_generation_service import environment

SENTINEL = 'SYNTHETIC_PRIVATE_HEADER_AND_PROMPT_MUST_NOT_PERSIST'


@pytest.fixture(autouse=True)
def sealed(monkeypatch):
    monkeypatch.setattr(os, 'environ', {})
    def denied(*a, **kw):
        raise AssertionError('no actual network or credentials')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


def provider(models):
    return GoogleGenerationAdapter(capability35(), profile_version='2', client=SimpleNamespace(models=models))


@pytest.mark.parametrize('code,status,kind', [
    (400,'INVALID_ARGUMENT',ProviderFailureKind.CAPABILITY),
    (401,'UNAUTHENTICATED',ProviderFailureKind.CAPABILITY),
    (403,'PERMISSION_DENIED',ProviderFailureKind.CAPABILITY),
    (404,'NOT_FOUND',ProviderFailureKind.CAPABILITY),
    (422,'INVALID_ARGUMENT',ProviderFailureKind.CAPABILITY),
    (429,'RESOURCE_EXHAUSTED',ProviderFailureKind.QUOTA),
    (503,'UNAVAILABLE',ProviderFailureKind.UNAVAILABLE),
])
def test_http_error_closed_evidence_without_body(code,status,kind):
    response=httpx.Response(code,request=httpx.Request('POST','https://example.invalid/fixed',
        headers={'authorization':SENTINEL}),json={'error':{'code':code,'status':status,'message':SENTINEL}})
    cls=errors.ClientError if code<500 else errors.ServerError
    error=cls(code,{'error':{'code':code,'status':status,'message':SENTINEL}},response)
    models=Mock35Models(error=error);p=provider(models)
    with pytest.raises(ProviderInvocationError) as got:p.invoke_once(request35())
    assert got.value.kind is kind and got.value.request_sent
    e=got.value.boundary_evidence
    assert (e.operation,e.stage,e.http_status,e.google_status)==('GENERATE_CONTENT','SDK_CALL',code,status)
    assert e.exception_class==('SDK_CLIENT_ERROR' if code<500 else 'SDK_SERVER_ERROR')
    assert e.response_received=='YES' and e.http_delivery=='CONFIRMED_RESPONSE'
    assert e.response_model_identity=='UNKNOWN'
    assert SENTINEL not in json.dumps(asdict(e)) and len(models.generate_calls)==1


@pytest.mark.parametrize('model,classification', [
    (None,'MISSING'), ('','UNKNOWN'), (17,'UNKNOWN'), ('other-model','MISMATCH'),
])
def test_response_identity_failure_exact_no_raw_model(model,classification):
    models=Mock35Models(model=model);p=provider(models)
    with pytest.raises(ProviderInvocationError) as got:p.invoke_once(request35())
    assert got.value.kind is ProviderFailureKind.CAPABILITY
    e=got.value.boundary_evidence
    assert e.stage=='RESPONSE_VALIDATION' and e.response_model_identity==classification
    assert e.response_received=='YES' and e.http_delivery=='UNKNOWN' and e.http_status is None
    assert p.generation_calls==len(models.generate_calls)==1


def test_matching_identity_with_malformed_usage_is_not_model_failure():
    p=provider(Mock35Models(usage={'prompt_token_count':1,'candidates_token_count':None,'total_token_count':2}))
    with pytest.raises(ProviderInvocationError) as got:p.invoke_once(request35())
    assert got.value.kind is ProviderFailureKind.MALFORMED
    assert got.value.boundary_evidence.response_model_identity=='MATCHED'


def test_success_preserves_match_and_does_not_invent_http_status():
    models=Mock35Models();p=provider(models)
    assert p.input_token_upper_bound('synthetic observation',30)==10
    result=p.invoke_once(request35())
    e=result.boundary_evidence
    assert e.response_model_identity=='MATCHED' and e.http_status is None
    assert e.http_delivery=='UNKNOWN' and result.metadata.total_tokens==15
    assert p.token_preflight_calls==p.generation_calls==1


def test_sdk_transport_response_is_delivery_evidence_without_headers_or_body():
    response=types.GenerateContentResponse(model_version='gemini-3.5-flash-lite',
        sdk_http_response=types.HttpResponse(headers={'authorization':SENTINEL},body=SENTINEL))
    e=_boundary('GENERATE_CONTENT','RESPONSE_VALIDATION',response=response,model='MATCHED')
    assert e.http_delivery=='CONFIRMED_RESPONSE' and e.http_status is None
    assert SENTINEL not in json.dumps(asdict(e))


@pytest.mark.parametrize('error,label', [
    (ValueError(SENTINEL),'VALUE_ERROR'), (RuntimeError(SENTINEL),'UNKNOWN_ERROR'),
    (httpx.ReadTimeout(SENTINEL),'TIMEOUT'), (httpx.ConnectError(SENTINEL),'TRANSPORT_ERROR'),
])
def test_no_response_never_becomes_delivery_or_zero_usage(error,label):
    p=provider(Mock35Models(error=error))
    with pytest.raises(ProviderInvocationError) as got:p.invoke_once(request35())
    assert got.value.kind in (ProviderFailureKind.UNAVAILABLE,ProviderFailureKind.TIMEOUT)
    e=got.value.boundary_evidence
    assert e.exception_class==label and e.stage=='SDK_CALL'
    assert e.response_received==e.http_delivery=='UNKNOWN' and e.http_status is None
    assert SENTINEL not in json.dumps(asdict(e))


def test_local_sdk_validation_classified_without_claiming_unsent():
    error=ValidationError.from_exception_data('Synthetic',[
        {'type':'int_parsing','loc':('config',),'input':SENTINEL}])
    p=provider(Mock35Models(error=error))
    with pytest.raises(ProviderInvocationError) as got:p.invoke_once(request35())
    assert got.value.boundary_evidence.exception_class=='SDK_VALIDATION_ERROR'
    assert got.value.boundary_evidence.http_delivery=='UNKNOWN'
    assert SENTINEL not in json.dumps(asdict(got.value.boundary_evidence))


@pytest.mark.parametrize('error,label', [
    (ValidationError.from_exception_data('Synthetic', [
        {'type':'int_parsing','loc':('config',),'input':SENTINEL}]), 'SDK_VALIDATION_ERROR'),
    (ValueError(SENTINEL), 'VALUE_ERROR'),
])
def test_count_initialization_preserves_factory_exception(monkeypatch,error,label):
    from google import genai
    monkeypatch.setenv('GEMINI_API_KEY', 'SYNTHETIC_OFFLINE_ONLY')
    calls=[]
    def fail_initialization(*args, **kwargs):
        calls.append('initialize')
        raise error
    monkeypatch.setattr(genai, 'Client', fail_initialization)
    p=GoogleGenerationAdapter(capability35(), profile_version='2')
    with pytest.raises(ProviderInvocationError) as got:
        p.input_token_upper_bound('synthetic',30)
    e=got.value.boundary_evidence
    assert e.exception_class==label
    assert (e.operation,e.stage)==('COUNT_TOKENS','CLIENT_INITIALIZATION')
    assert e.http_status is e.google_status is None
    assert e.response_model_identity=='UNKNOWN'
    assert (e.response_received,e.http_delivery)==('NO','NOT_DISPATCHED')
    assert got.value.kind is ProviderFailureKind.UNAVAILABLE and not got.value.request_sent
    assert p.token_preflight_calls==p.generation_calls==0 and calls==['initialize']
    assert SENTINEL not in json.dumps(asdict(e))
    assert SENTINEL not in str(got.value)


@pytest.mark.parametrize('evidence', [
    ProviderBoundaryEvidence('GENERATE_CONTENT','CLIENT_INITIALIZATION',400,
        'INVALID_ARGUMENT','SDK_CLIENT_ERROR','MISMATCH','YES','CONFIRMED_RESPONSE'),
    ProviderBoundaryEvidence('GENERATE_CONTENT','CLIENT_INITIALIZATION',None,
        None,'UNKNOWN_ERROR','UNKNOWN','UNKNOWN','UNKNOWN'),
    None,
])
def test_count_initialization_preserves_sanitized_facts(monkeypatch,evidence):
    p=GoogleGenerationAdapter(capability35(), profile_version='2')
    def fail_initialization(timeout):
        raise ProviderInvocationError(ProviderFailureKind.UNAVAILABLE,
            request_sent=evidence is not None and evidence.response_received=='YES',
            boundary_evidence=evidence)
    monkeypatch.setattr(p, '_client_for', fail_initialization)
    with pytest.raises(ProviderInvocationError) as got:
        p.input_token_upper_bound('synthetic',30)
    e=got.value.boundary_evidence
    assert (e.operation,e.stage)==('COUNT_TOKENS','CLIENT_INITIALIZATION')
    if evidence is not None:
        assert e==replace(evidence,operation='COUNT_TOKENS')
        assert got.value.request_sent==(evidence.response_received=='YES')
    else:
        assert e.exception_class=='UNKNOWN_ERROR'
        assert e.http_status is e.google_status is None
        assert e.response_model_identity=='UNKNOWN'
        assert (e.response_received,e.http_delivery)==('UNKNOWN','UNKNOWN')
        assert not got.value.request_sent
    assert got.value.kind is ProviderFailureKind.UNAVAILABLE
    assert p.token_preflight_calls==p.generation_calls==0


def test_exception_text_is_never_used_as_status_evidence():
    p=provider(Mock35Models(error=RuntimeError('400 INVALID_ARGUMENT '+SENTINEL)))
    with pytest.raises(ProviderInvocationError) as got:p.invoke_once(request35())
    e=got.value.boundary_evidence
    assert e.http_status is e.google_status is None
    assert e.http_delivery=='UNKNOWN' and e.exception_class=='UNKNOWN_ERROR'


def test_sdk_error_without_transport_response_does_not_prove_delivery():
    error=errors.ClientError(400,{'error':{'status':'INVALID_ARGUMENT','message':SENTINEL}})
    p=provider(Mock35Models(error=error))
    with pytest.raises(ProviderInvocationError) as got:p.invoke_once(request35())
    e=got.value.boundary_evidence
    assert e.exception_class=='SDK_CLIENT_ERROR' and e.google_status=='INVALID_ARGUMENT'
    assert e.http_status is None and e.http_delivery==e.response_received=='UNKNOWN'


def test_request_mismatch_has_no_dispatch_and_no_counter():
    models=Mock35Models();p=provider(models)
    with pytest.raises(ProviderInvocationError) as got:p.invoke_once(request35(model='wrong-model'))
    assert got.value.kind is ProviderFailureKind.CAPABILITY and not got.value.request_sent
    assert got.value.boundary_evidence.http_delivery=='NOT_DISPATCHED'
    assert got.value.boundary_evidence.stage=='REQUEST_VALIDATION'
    assert p.generation_calls==0 and not models.generate_calls


def test_counting_error_has_its_own_operation_no_generate():
    response=httpx.Response(400,request=httpx.Request('POST','https://example.invalid/count'))
    error=errors.ClientError(400,{'error':{'status':'INVALID_ARGUMENT','message':SENTINEL}},response)
    models=Mock35Models(count_error=error);p=provider(models)
    with pytest.raises(ProviderInvocationError) as got:p.input_token_upper_bound('synthetic',30)
    assert got.value.boundary_evidence.operation=='COUNT_TOKENS'
    assert got.value.boundary_evidence.http_status==400
    assert p.token_preflight_calls==1 and p.generation_calls==0


def test_service_persistence_failure_readback_no_retry(case,tmp_path):
    e=ProviderBoundaryEvidence('GENERATE_CONTENT','SDK_CALL',400,'INVALID_ARGUMENT',
        'SDK_CLIENT_ERROR','UNKNOWN','YES','CONFIRMED_RESPONSE')
    service,p,grant,resources=environment(case,tmp_path)
    def fail(request):
        p.calls.append(request)
        raise ProviderInvocationError(ProviderFailureKind.CAPABILITY,request_sent=True,boundary_evidence=e)
    p.invoke_once=fail
    receipt=service.execute(case.content.input,case.config,grant,resources)
    failure=receipt.failure
    assert failure.boundary_evidence==e and failure.failure_class is FailureClass.CAPABILITY_MISMATCH
    assert failure.retry_safety is AdmittedRetryDisposition.NON_RETRYABLE and not failure.same_try_eligible
    assert failure.safe_detail=='provider boundary failed'
    reopened=CandidateDStore(service.store.path,resource_config_path=service.store.resource_config_path)
    assert reopened.failures_for_try(case.content.input.try_identity).value[-1]==failure
    before=service.store.path.read_bytes()
    replay=service.execute(case.content.input,case.config,grant,resources)
    assert replay.physical_invocations==0 and len(p.calls)==1
    assert service.store.path.read_bytes()==before and SENTINEL.encode() not in before


def test_historical_failure_payload_and_digest_default_unchanged(case):
    f=GenerationFailure(case.content.input.try_identity,'legacy-operation',FailureClass.STRUCTURED_PARSE,
        AdmittedRetryDisposition.RETRYABLE,'structured result rejected')
    wire=encode(f)
    assert 'parser_evidence' not in wire['fields'] and 'boundary_evidence' not in wire['fields']
    assert decode(wire)==f and decode(wire).boundary_evidence is None
    old={k:v for k,v in asdict(f).items() if k not in ('parser_evidence','boundary_evidence')}
    old['failure_class']=f.failure_class.value;old['retry_safety']=f.retry_safety.value
    assert semantic_commitment(f)==sha256(('spec015-result-v1\n'+json.dumps(old,
        sort_keys=True,ensure_ascii=False,separators=(',',':'))).encode()).hexdigest()


@pytest.mark.parametrize('change',[
    {'http_status':True},{'http_status':999},{'google_status':SENTINEL},
    {'exception_class':SENTINEL},{'response_model_identity':SENTINEL},
    {'http_delivery':'DELIVERED_AND_BILLED'},{'stage':SENTINEL},
    {'response_received':'NO'},
])
def test_boundary_evidence_tamper_rejected(change):
    v=dict(operation='GENERATE_CONTENT',stage='SDK_CALL',http_status=400,google_status='INVALID_ARGUMENT',
        exception_class='SDK_CLIENT_ERROR',response_model_identity='UNKNOWN',response_received='YES',
        http_delivery='CONFIRMED_RESPONSE')
    v.update(change)
    with pytest.raises((TypeError,ValueError)):ProviderBoundaryEvidence(**v)
