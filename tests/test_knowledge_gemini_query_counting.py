"""Exact S2 query counting: protected-fixture admission, sealed SDK transport.

Only synthetic authority/credential and isolated existing-schema SQLite are used.
Source guard is a test stub; no Git/source resolver or Google API is executed.
"""
from dataclasses import asdict, replace
from hashlib import sha256
import json
import sqlite3

import httpx
import pytest

from knowledge_index import gemini_diagnostic as d, gemini_diagnostic_controls as c
from test_knowledge_gemini_diagnostic_counting import counting
from test_knowledge_gemini_diagnostic_controls import safety_parse, record
from test_knowledge_gemini_diagnostic_free_tier import free_evidence
from test_knowledge_gemini_embedding2_serialization import offline_sdk


@pytest.fixture(autouse=True)
def sealed(offline_sdk):
    yield
    assert offline_sdk.blocked_calls == []


@pytest.fixture
def query_counting(counting, monkeypatch):
    f = counting
    _, body, _ = d._prepare_counting_sdk_request(c.QUERY_COUNTING_ENVELOPE)
    scope = "build:" + c.QUERY_COUNTING_BUILD
    grant = replace(f["grant"], purpose=c.QUERY_COUNTING_OPERATION, input_kind="query",
        input_scope_identity=scope, input_commitment=c.QUERY_COUNTING_INPUT,
        maximum_item_bytes=274, maximum_payload_bytes=351)
    approved = replace(f["approved"], purpose=grant.purpose, kind="query",
        scope_identity=scope, commitment=grant.input_commitment,
        identities=(c.QUERY_COUNTING_BUILD,), envelopes=(c.QUERY_COUNTING_ENVELOPE,))
    digest = c.authorization_record_digest(grant)
    e4 = replace(f["e4"], authorization_record_sha256=digest, kind="query",
        input_scope_identity=scope, ordered_identities=approved.identities,
        input_commitment=grant.input_commitment, sdk_body_bytes=351,
        sdk_body_sha256=sha256(body).hexdigest(),
        canonical_json_sha256=sha256(d._canonical_request(body)).hexdigest())
    value = f["value"]
    value.pop("complete_corpus_commitment")
    value.update(operation=grant.purpose, query_binding=c.query_counting_binding())
    value["binding"].update(kind="query", input_scope_identity=scope,
        input_commitment=grant.input_commitment, authorization_record_sha256=digest)
    value["e4_reference"].update(sdk_body_sha256=e4.sdk_body_sha256,
        canonical_json_sha256=e4.canonical_json_sha256, sdk_body_bytes=351)
    free_evidence(value, grant)
    value["free_tier_evidence"]["quota_evidence"].update(rpm="UNKNOWN", tpm="UNKNOWN", rpd="UNKNOWN")
    binding = replace(f["binding"], record=grant, approved_input=approved, sdk_byte_evidence=e4)
    f.update(grant=grant, approved=approved, e4=e4, binding=binding, body=body)
    monkeypatch.setattr(d, "load_live_binding", lambda: f["binding"])
    monkeypatch.setattr(d, "load_live_safety_artifact", lambda: safety_parse(value))

    def provider(*, fault=None):
        def handle(request):
            assert f["rows"]()[0][0] == "IN_FLIGHT" and f["credentials"] == [True]
            assert request.method == "POST" and str(request.url) == c.COUNTING_URL
            assert request.content == body and len(body) == 351
            assert request.extensions["timeout"] == dict(connect=30.,read=30.,write=30.,pool=30.)
            f["calls"].append(request)
            if fault == "timeout": raise httpx.ReadTimeout("synthetic timeout")
            if fault == "loss": raise httpx.ReadError("synthetic response loss")
            if fault == "crash": raise KeyboardInterrupt("synthetic process loss")
            if fault == "http": return httpx.Response(503, json={"error":{"code":503,"message":"synthetic"}})
            if fault == "malformed": return httpx.Response(200, content=b'{"totalTokens":true}')
            return httpx.Response(200, json={"totalTokens":128})
        monkeypatch.setattr(d,"_create_live_transport",lambda:httpx.MockTransport(handle))
    f["provider"] = provider
    return f


def run():
    return d.run_live(operation=c.QUERY_COUNTING_OPERATION, live_opt_in=True)


def test_exact_query_success_durable_readback_not_embedding_usage(query_counting):
    f=query_counting;f["provider"]();result=run()
    assert result.outcome=="SUCCESS" and result.observed_token_count==128 and result.usage=="UNKNOWN"
    assert len(f["calls"])==len(f["credentials"])==result.provider_attempts==1
    row=f["rows"]()[0];obs=json.loads(row[-1])
    assert row[:4]==("SUCCESS",1,1,"UNKNOWN")
    assert obs["operation"]==c.QUERY_COUNTING_OPERATION and obs["query_binding"]==c.query_counting_binding()
    assert obs["body_sha256"]==c.QUERY_COUNTING_BODY and obs["response_sha256"]
    assert not {"corpus_commitment","chunk_identity","global_ordinal"}&obs.keys()
    assert obs["reserved_at"] and obs["recorded_at"]
    assert b"synthetic-counting-key" not in f["path"].read_bytes()
    with pytest.raises(c.DiagnosticDenied):run()
    assert len(f["calls"])==len(f["credentials"])==1


@pytest.mark.parametrize("fault,expected", [("timeout","TIMEOUT"),("loss","UNKNOWN"),
                                           ("http","UNKNOWN"),("malformed","FAILED")])
def test_sent_failure_and_unknown_preserve_attempt_no_retry(query_counting,fault,expected):
    f=query_counting;f["provider"](fault=fault);outcome=run()
    assert outcome.outcome==expected and f["rows"]()[0][0]==expected
    assert f["rows"]()[0][1:4]==(1,1,"UNKNOWN")
    with pytest.raises(c.DiagnosticDenied):run()
    assert len(f["calls"])==len(f["credentials"])==1


def test_crash_keeps_inflight_no_second_send(query_counting):
    f=query_counting;f["provider"](fault="crash")
    with pytest.raises(KeyboardInterrupt):run()
    assert f["rows"]()[0][0]=="IN_FLIGHT"
    with pytest.raises(c.DiagnosticDenied):run()
    assert len(f["calls"])==1


@pytest.mark.parametrize("mismatch", ["source","profile","query","e4","generation","model",
                                    "normalization","dimension","host","runner","expiry","sdk"])
def test_mismatch_denied_before_credential_and_reservation(query_counting,monkeypatch,mismatch):
    f=query_counting
    if mismatch=="source":monkeypatch.setattr(d,"resolve_repository_source",lambda _: ("a"*40,True))
    if mismatch=="profile":f["value"]["query_binding"]["embedding_profile"]="legacy-profile"
    if mismatch=="generation":f["value"]["query_binding"]["generation"]=2
    if mismatch=="query":f["binding"]=replace(f["binding"],approved_input=replace(f["approved"],envelopes=("altered",)))
    if mismatch=="e4":f["binding"]=replace(f["binding"],sdk_byte_evidence=replace(f["e4"],sdk_body_sha256="f"*64))
    if mismatch=="model":f["value"]["binding"]["model"]="text-embedding-004"
    if mismatch=="normalization":f["value"]["query_binding"]["normalization"]="other-vector-space"
    if mismatch=="dimension":f["value"]["query_binding"]["dimension"]=3072
    if mismatch=="host":monkeypatch.setattr(d.socket,"gethostname",lambda:"other-host")
    if mismatch=="runner":f["binding"]=replace(f["binding"],runner_sid="other-runner")
    if mismatch=="expiry":f["binding"]=replace(f["binding"],record=replace(f["grant"],expires_at="2020-01-01T00:00:00+00:00"))
    if mismatch=="sdk":f["binding"]=replace(f["binding"],sdk_byte_evidence=replace(f["e4"],sdk="unapproved"))
    with pytest.raises(c.DiagnosticDenied):run()
    assert not f["rows"]() and not f["credentials"] and not f["calls"]


def test_final_source_revalidation_after_reservation(query_counting,monkeypatch):
    f=query_counting;f["provider"]();old=d.resolve_diagnostic_credential
    def changed(*args):
        credential=old(*args)
        monkeypatch.setattr(d,"resolve_repository_source",lambda _: (f["grant"].source_revision,False))
        return credential
    monkeypatch.setattr(d,"resolve_diagnostic_credential",changed)
    result=run()
    assert result.outcome=="FAILED" and result.code=="SOURCE_MISMATCH"
    assert not f["calls"] and f["rows"]()[0][1:4]==(1,1,"UNKNOWN")


@pytest.mark.parametrize("commit_then_raise",[False,True])
def test_persistence_response_loss_not_success_or_resent(query_counting,monkeypatch,commit_then_raise):
    f=query_counting;f["provider"]();original=f["store"]._connect
    class Fault(sqlite3.Connection):
        def commit(self):
            if f["calls"]:
                if commit_then_raise:super().commit()
                raise sqlite3.OperationalError("synthetic persistence response loss")
            super().commit()
    def connect(*,read_only=False):
        return original(read_only=True) if read_only else sqlite3.connect(str(f["path"]),isolation_level=None,factory=Fault)
    monkeypatch.setattr(f["store"],"_connect",connect)
    with pytest.raises(c.DiagnosticDenied) as exc:run()
    assert exc.value.code=="PERSISTENCE_FAILURE" and len(f["calls"])==1
    assert f["rows"]()[0][0]==("SUCCESS" if commit_then_raise else "IN_FLIGHT")
    with pytest.raises(c.DiagnosticDenied):run()
    assert len(f["calls"])==1


def test_count_success_does_not_clear_global_unknown_usage_fence(query_counting):
    f=query_counting;f["provider"]();run()
    with sqlite3.connect(str(f["path"])) as db:
        assert c._blocking_fence_count(db)==1
    assert f["rows"]()[0][3]=="UNKNOWN"
    with pytest.raises(c.DiagnosticDenied) as exc:
        f["store"].reserve(record(),"new-embedding",items=1,usage_units=1,cost_micro_units=1)
    assert exc.value.code=="RUNNER_CONFLICT"


def test_independent_purpose_does_not_admit_embedding_or_document(query_counting):
    f=query_counting
    with pytest.raises(c.DiagnosticDenied):d._prepare_counting(f["binding"],None)
    with pytest.raises(c.DiagnosticDenied):f["store"].finish(f["grant"],"unreserved","SUCCESS")
    with pytest.raises(c.DiagnosticDenied):d.run_live(object(),operation=c.QUERY_COUNTING_OPERATION,live_opt_in=True)
    assert not f["rows"]() and not f["credentials"] and not f["calls"]


def test_query_counting_protected_host_publication_loader(query_counting,monkeypatch,tmp_path):
    from test_knowledge_gemini_diagnostic_controls import e4_host_fixture
    f=query_counting;paths,_,_,_=e4_host_fixture(tmp_path,monkeypatch)
    grant=replace(f["grant"],runner_identity="S-1-5-21-100")
    digest=c.authorization_record_digest(grant)
    e4=replace(f["e4"],authorization_record_sha256=digest)
    value=f["value"];value["binding"]["authorization_record_sha256"]=digest
    material=dict(schema_version=1,campaign_id=grant.campaign_id,source_revision=grant.source_revision,
        purpose=grant.purpose,kind="query",scope_identity=grant.input_scope_identity,
        input_commitment=grant.input_commitment,ordered_items=[dict(frozen_build_identity=c.QUERY_COUNTING_BUILD,
            canonical_query=c.QUERY_COUNTING_TEXT,envelope=c.QUERY_COUNTING_ENVELOPE)])
    wire=asdict(e4)
    for name in ('valid_from','valid_until'):wire[name]=wire[name].isoformat()
    paths.campaign.write_text(json.dumps(asdict(grant)),encoding='utf-8')
    paths.approved_input.write_text(json.dumps(material),encoding='utf-8')
    paths.sdk_request_evidence.write_text(json.dumps(wire),encoding='utf-8')
    paths.safety_evidence.write_text(json.dumps(value),encoding='utf-8')
    monkeypatch.setattr(c,'resolve_repository_source',lambda _:(grant.source_revision,True))
    loaded=c.load_live_binding()
    assert loaded.record.purpose==c.QUERY_COUNTING_OPERATION and loaded.approved_input.kind=='query'
    assert c.load_live_safety_artifact().fields['query_binding']['generation']==1
    assert not f['credentials'] and not f['calls']
