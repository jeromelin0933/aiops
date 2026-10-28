"""Counting-only mocks: synthetic authority/key, isolated SQLite, sealed I/O."""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import sqlite3

import httpx
import pytest

import knowledge_index.gemini_diagnostic as diagnostic
import knowledge_index.gemini_diagnostic_controls as controls
from test_knowledge_gemini_diagnostic_controls import record, safety_case, safety_parse
from test_knowledge_gemini_diagnostic_free_tier import free_evidence
from test_knowledge_gemini_embedding2_serialization import offline_sdk


@pytest.fixture(autouse=True)
def sealed(offline_sdk):
    yield
    assert offline_sdk.blocked_calls == []


@pytest.fixture
def counting(tmp_path, monkeypatch):
    scope, text = diagnostic._counting_content()
    contents, body = diagnostic._serialize_counting(text)
    grant = record(kind="document", purpose=controls.COUNTING_OPERATION,
        input_scope_identity=scope, input_commitment=controls.COUNTING_INPUT,
        maximum_invocations=1, maximum_items=1, maximum_item_tokens=None,
        maximum_aggregate_tokens=None, maximum_usage_units=1,
        maximum_item_bytes=528, maximum_payload_bytes=591, maximum_cost_micro_units=1,
        bounds_basis="counting-minimum-v1", cost_basis="counting-free-v1",
        valid_from="2000-01-01T00:00:00+00:00", expires_at="2099-01-01T00:00:00+00:00",
        normalization_tolerance=None, project_identity="synthetic-project", credential_profile_revision="r1")
    approved = controls.ApprovedDiagnosticInput(1, grant.campaign_id, grant.source_revision,
        grant.purpose, "document", scope, controls.COUNTING_INPUT, (controls.COUNTING_CHUNK,), (text,))
    e4 = controls.DiagnosticSdkByteEvidence("HOST_CONTROLLED_E4_ONLY", "PINNED_SDK_HTTPX_REQUEST_BODY",
        "synthetic-counting-e4", datetime(2099,1,1,tzinfo=timezone.utc), grant.campaign_id,
        grant.source_revision, controls.authorization_record_digest(grant), "google", "gemini-embedding-2",
        "google-genai==2.25.0", "v1", "document", scope, approved.identities, approved.commitment,
        sha256(diagnostic._canonical_request(body)).hexdigest(), sha256(body).hexdigest(), len(body),
        "POST", controls.COUNTING_URL, None, 30000, 1,
        pm_approval_reference=grant.pm_approval_reference, valid_from=datetime(2000,1,1,tzinfo=timezone.utc))
    value = safety_case()[3]
    value.pop("token_evidence")
    value.update(operation=controls.COUNTING_OPERATION, project_identity=grant.project_identity,
        complete_corpus_commitment=controls.COUNTING_CORPUS,
        outbound_eligibility="APPROVED_SYNTHETIC_NON_SECRET")
    value["provider_limits"] = dict(rpm="UNKNOWN", tpm="UNKNOWN", rpd="UNKNOWN", payload="UNKNOWN",
                                    source=dict(value["pricing_evidence"]["source"]))
    value["binding"].update(campaign_id=grant.campaign_id, authorization_record_sha256=controls.authorization_record_digest(grant),
        source_revision=grant.source_revision, input_scope_identity=scope, input_commitment=grant.input_commitment,
        kind="document", request_url=controls.COUNTING_URL, bounds_basis=grant.bounds_basis, cost_basis=grant.cost_basis)
    value["credential_binding"].update(profile_identity=grant.credential_profile_identity)
    value["e4_reference"].update(reference=e4.reference, sdk_body_sha256=e4.sdk_body_sha256,
        canonical_json_sha256=e4.canonical_json_sha256, sdk_body_bytes=e4.sdk_body_bytes)
    free_evidence(value, grant)
    value["free_tier_evidence"]["quota_evidence"].update(rpm="UNKNOWN", tpm="UNKNOWN", rpd="UNKNOWN")
    binding = controls.LiveHostBinding(grant, approved, None, grant.runner_identity, e4)
    monkeypatch.setattr(diagnostic, "load_live_binding", lambda: binding)
    monkeypatch.setattr(diagnostic, "load_live_safety_artifact", lambda: safety_parse(value))
    monkeypatch.setattr(diagnostic, "resolve_repository_source", lambda _: (grant.source_revision, True))
    monkeypatch.setattr(diagnostic.socket, "gethostname", lambda: grant.authorized_host)
    temporary = controls.TemporaryDiagnosticUsage(tmp_path / "attempts.sqlite", enable_counting=True)
    live = object.__new__(controls.LiveDiagnosticUsage)
    live._path = temporary._path
    # Bypass native path I/O only, retaining the formal schema and state checks.
    monkeypatch.setattr(controls.LiveDiagnosticUsage, "_connect", controls.TemporaryDiagnosticUsage._connect)
    monkeypatch.setattr(diagnostic, "LiveDiagnosticUsage", lambda: live)
    monkeypatch.setitem(controls.os.environ, "GEMINI_CREDENTIAL_PROFILE", grant.credential_profile_identity)
    monkeypatch.setitem(controls.os.environ, "GEMINI_CREDENTIAL_REVISION", "r1")
    monkeypatch.setitem(controls.os.environ, "GEMINI_API_KEY", "synthetic-counting-key")
    credentials, calls = [], []
    resolver = diagnostic.resolve_diagnostic_credential
    def resolve(*args):
        assert rows()[0][0] == "IN_FLIGHT"
        assert json.loads(rows()[0][-1])["code"] == "ATTEMPT_RESERVED"
        credentials.append(True)
        return resolver(*args)
    monkeypatch.setattr(diagnostic, "resolve_diagnostic_credential", resolve)
    def rows():
        with sqlite3.connect(str(temporary._path)) as db:
            return db.execute("SELECT outcome,usage_reserved,cost_reserved,usage_observed,observation_json FROM attempts").fetchall()
    def provider(*, count=17, body=None, status=200, failure=None):
        def handle(request):
            assert rows()[0][0] == "IN_FLIGHT"
            assert str(request.url) == controls.COUNTING_URL and request.method == "POST"
            assert request.content == globals_body and len(request.content) == 591
            calls.append(request)
            if failure == "timeout": raise httpx.ReadTimeout("synthetic-counting-key")
            if failure == "network": raise httpx.ConnectError("synthetic-counting-key")
            if failure == "crash": raise KeyboardInterrupt("synthetic crash")
            data = body if body is not None else json.dumps({"totalTokens":count}).encode()
            if status != 200:
                data = json.dumps({"error":{"code":status, "message":"synthetic-counting-key"}}).encode()
            return httpx.Response(status, content=data)
        monkeypatch.setattr(diagnostic, "_create_live_transport", lambda: httpx.MockTransport(handle))
        return calls
    globals_body = body
    return dict(grant=grant, approved=approved, e4=e4, value=value, binding=binding, rows=rows,
                store=live, credentials=credentials, calls=calls, provider=provider, path=temporary._path)


@pytest.mark.parametrize("count", [0,1,17,8193,2**63-1])
def test_trusted_counting_result_separate_from_embedding_usage(counting, count):
    c = counting; c["provider"](count=count)
    result = diagnostic.run_live(operation=controls.COUNTING_OPERATION, live_opt_in=True)
    assert result.outcome == "SUCCESS" and result.observed_token_count == count
    assert result.usage == "UNKNOWN" and result.provider_attempts == len(c["calls"]) == 1
    row = c["rows"]()[0]; evidence = json.loads(row[-1])
    assert row[:4] == ("SUCCESS",1,1,"UNKNOWN")
    assert evidence["operation"] == controls.COUNTING_OPERATION
    assert evidence["observed_token_count"] == count and evidence["input_commitment"] == controls.COUNTING_INPUT
    assert evidence["body_sha256"] == controls.COUNTING_BODY and evidence["reserved_at"]
    assert evidence["response_sha256"] and evidence["recorded_at"]
    assert b"synthetic-counting-key" not in c["path"].read_bytes()
    with pytest.raises(controls.DiagnosticDenied): diagnostic.run_counting_observation(live_opt_in=True)
    assert len(c["calls"]) == len(c["credentials"]) == 1


def test_exact_sdk_serialization_and_schema_roundtrip(counting):
    c=counting
    assert controls.DiagnosticAuthorizationRecord.from_json(json.dumps(asdict(c["grant"])).encode()) == c["grant"]
    wire=asdict(c["e4"])
    for key in ("valid_from","valid_until"): wire[key]=wire[key].isoformat()
    assert controls.DiagnosticSdkByteEvidence.from_json(json.dumps(wire).encode()) == c["e4"]
    prepared=diagnostic._prepare_counting(c["binding"], safety_parse(c["value"]))
    assert len(prepared.body)==591 and sha256(prepared.body).hexdigest()==controls.COUNTING_BODY
    assert len(prepared.contents[0].parts[0].text.encode())==528
    assert set(json.loads(prepared.body))=={"contents"}
    assert c["credentials"]==c["calls"]==[]


@pytest.mark.parametrize("field", ["project_identity","credential_profile_revision"])
def test_new_counting_fields_required_only_for_counting(counting,field):
    wire=asdict(counting["grant"])
    wire.pop(field)
    with pytest.raises(controls.DiagnosticDenied): controls.DiagnosticAuthorizationRecord.from_json(json.dumps(wire).encode())
    legacy=record()
    original=asdict(legacy)
    original.pop("project_identity")
    original.pop("credential_profile_revision")
    assert controls.DiagnosticAuthorizationRecord.from_json(json.dumps(original).encode())==legacy
    assert controls.authorization_record_digest(legacy)==sha256(json.dumps(original,sort_keys=True,ensure_ascii=True,separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("field,value", [("model_identity","other"),("api_version","v1beta"),
    ("sdk_baseline","other"),("input_commitment","0"*64),("maximum_invocations",2),
    ("maximum_payload_bytes",592),("maximum_item_bytes",529),("maximum_item_tokens",1),
    ("maximum_cost_micro_units",0),("credential_profile_identity","wrong-profile"),
    ("credential_profile_revision","wrong-revision"),("project_identity","wrong-project"),
    ("expires_at","2001-01-01T00:00:00+00:00")])
def test_invalid_authority_denied_before_credentials(counting, field, value):
    c=counting; c["provider"]()
    object.__setattr__(c["grant"],field,value)
    with pytest.raises(controls.DiagnosticDenied): diagnostic.run_counting_observation(live_opt_in=True)
    assert c["credentials"]==c["calls"]==[]


@pytest.mark.parametrize("mode", ["opt_in","no_pm","dirty","wrong_source","input","payload","operation","free_permission","quota_missing"])
def test_preflight_denials_never_discover_credentials(counting, monkeypatch, mode):
    c=counting; c["provider"]()
    if mode=="no_pm":
        def denied(): raise controls.DiagnosticDenied("no PM campaign")
        monkeypatch.setattr(diagnostic,"load_live_binding",denied)
    elif mode in ("dirty","wrong_source"):
        monkeypatch.setattr(diagnostic,"resolve_repository_source",lambda _: ("a"*40 if mode=="wrong_source" else c["grant"].source_revision,mode!="dirty"))
    elif mode=="input": object.__setattr__(c["approved"],"envelopes",("substitute",))
    elif mode=="payload": object.__setattr__(c["e4"],"sdk_body_sha256","0"*64)
    elif mode=="operation":
        replacement=record(valid_from=c["grant"].valid_from, expires_at=c["grant"].expires_at)
        monkeypatch.setattr(diagnostic,"load_live_binding",lambda:replace(c["binding"],record=replacement))
    elif mode=="free_permission": c["value"]["free_tier_evidence"]["data_use_permission"]["pm_approval_reference"]="other"
    elif mode=="quota_missing": c["value"]["free_tier_evidence"].pop("quota_evidence")
    with pytest.raises(controls.DiagnosticDenied): diagnostic.run_counting_observation(live_opt_in=mode!="opt_in")
    assert c["credentials"]==c["calls"]==[]


@pytest.mark.parametrize("state,usage", [("UNKNOWN","UNKNOWN"),("TIMEOUT","UNKNOWN"),("IN_FLIGHT","UNKNOWN"),("SUCCESS","UNKNOWN"),("SUCCESS","0")])
def test_atomic_eligibility_precedes_credentials(counting,state,usage):
    c=counting;c["provider"]()
    with sqlite3.connect(str(c["path"])) as db:
        db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved,usage_observed) VALUES(?,?,?,1,1,1,?)",
            (c["grant"].campaign_id,"prior",state,usage))
    with pytest.raises(controls.DiagnosticDenied) as exc: diagnostic.run_counting_observation(live_opt_in=True)
    assert exc.value.code == ("BUDGET_EXHAUSTED" if usage=="0" else "RUNNER_CONFLICT")
    assert len(c["rows"]())==1 and c["credentials"]==c["calls"]==[]


def test_lock_conflict(counting):
    c=counting;c["provider"]()
    diagnostic._SEQUENTIAL_RUNNER_LOCK.acquire()
    try:
        with pytest.raises(controls.DiagnosticDenied) as exc: diagnostic.run_counting_observation(live_opt_in=True)
        assert exc.value.code=="RUNNER_CONFLICT" and c["credentials"]==c["calls"]==[]
    finally: diagnostic._SEQUENTIAL_RUNNER_LOCK.release()


@pytest.mark.parametrize("body", [b'{}',b'null',b'[]',b'{',b'\xff',b'{"totalTokens":true}',
    b'{"totalTokens":"1"}',b'{"totalTokens":1.0}',b'{"totalTokens":-1}',b'{"totalTokens":9223372036854775808}',
    b'{"totalTokens":NaN}',b'{"totalTokens":1,"totalTokens":2}'])
def test_invalid_response_is_typed_and_usage_remains_unknown(counting,body):
    c=counting;c["provider"](body=body)
    result=diagnostic.run_counting_observation(live_opt_in=True)
    assert result.outcome=="FAILED" and result.code=="INVALID_PROVIDER_RESPONSE"
    assert result.observed_token_count is None and result.usage=="UNKNOWN"
    assert len(c["calls"])==1 and c["rows"]()[0][1:4]==(1,1,"UNKNOWN")


@pytest.mark.parametrize("status,failure,outcome,code", [(429,None,"FAILED","RATE_LIMIT_QUOTA"),
    (404,None,"FAILED","UNSUPPORTED_MODEL"),(405,None,"FAILED","UNSUPPORTED_OPERATION"),
    (200,"timeout","TIMEOUT","TRANSPORT_TIMEOUT"),(200,"network","UNKNOWN","TRANSPORT_FAILURE")])
def test_provider_failure_never_retries(counting,status,failure,outcome,code):
    c=counting;c["provider"](status=status,failure=failure)
    result=diagnostic.run_counting_observation(live_opt_in=True)
    assert (result.outcome,result.code)==(outcome,code) and len(c["calls"])==1
    assert c["rows"]()[0][1:4]==(1,1,"UNKNOWN")


def test_credential_failure_preserves_attempt_and_typed_evidence(counting,monkeypatch):
    c=counting;c["provider"]()
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(controls.DiagnosticDenied) as exc: diagnostic.run_counting_observation(live_opt_in=True)
    assert exc.value.code=="CREDENTIAL_UNAVAILABLE" and len(c["credentials"])==1 and not c["calls"]
    row=c["rows"]()[0]
    assert row[:4]==("FAILED",1,1,"UNKNOWN") and json.loads(row[-1])["code"]=="CREDENTIAL_UNAVAILABLE"


def test_crash_restart_retains_fence(counting):
    c=counting;c["provider"](failure="crash")
    with pytest.raises(KeyboardInterrupt): diagnostic.run_counting_observation(live_opt_in=True)
    assert c["rows"]()[0][0]=="IN_FLIGHT"
    with pytest.raises(controls.DiagnosticDenied): diagnostic.run_counting_observation(live_opt_in=True)
    assert len(c["calls"])==len(c["credentials"])==1


def test_counting_cannot_complete_embedding_receipt_or_auto_embed(counting):
    c=counting;c["provider"]()
    with pytest.raises(controls.DiagnosticDenied): diagnostic.run_live(live_opt_in=True)
    with pytest.raises(controls.DiagnosticDenied): c["store"]._finish_provider_usage(c["grant"],"fake","SUCCESS",None,controls.COUNTING_BODY)
    with pytest.raises(controls.DiagnosticDenied): c["store"]._finish_counting(c["grant"],"fake","SUCCESS","COUNTING_RESULT_OBTAINED",object())
    assert not c["calls"] and not c["credentials"]
    diagnostic.run_counting_observation(live_opt_in=True)
    assert len(c["calls"])==1 and all(str(r.url).endswith(":countTokens") for r in c["calls"])


def test_retry_misconfiguration_denied_before_send(counting,monkeypatch):
    c=counting;c["provider"]()
    factory=diagnostic._create_diagnostic_live_sdk_client
    def bad(*args):
        sdk=factory(*args)
        sdk._api_client._http_options.retry_options.attempts=2
        return sdk
    monkeypatch.setattr(diagnostic,"_create_diagnostic_live_sdk_client",bad)
    result=diagnostic.run_counting_observation(live_opt_in=True)
    assert result.outcome=="FAILED" and not c["calls"] and c["rows"]()[0][1:4]==(1,1,"UNKNOWN")


@pytest.mark.parametrize("commit_then_raise", [False,True])
def test_persistence_ambiguity_never_returns_success_or_resends(counting,monkeypatch,commit_then_raise):
    c=counting;c["provider"]()
    class Fault(sqlite3.Connection):
        def commit(self):
            if c["calls"]:
                if commit_then_raise: super().commit()
                raise sqlite3.OperationalError("synthetic persistence fault")
            return super().commit()
    def connect(*,read_only=False):
        return sqlite3.connect(str(c["path"]),factory=Fault,timeout=0,isolation_level=None)
    monkeypatch.setattr(c["store"],"_connect",connect)
    with pytest.raises(controls.DiagnosticDenied) as exc: diagnostic.run_counting_observation(live_opt_in=True)
    assert exc.value.code=="PERSISTENCE_FAILURE" and c["rows"]()[0][3]=="UNKNOWN"
    with pytest.raises(controls.DiagnosticDenied): diagnostic.run_counting_observation(live_opt_in=True)
    assert len(c["calls"])==len(c["credentials"])==1


def test_old_store_is_not_automatically_migrated(counting):
    c=counting;c["provider"]()
    with sqlite3.connect(str(c["path"])) as db:
        db.execute("DROP TABLE attempts")
        db.execute(controls._FORMAL_ATTEMPTS_DDL)
    with pytest.raises(controls.DiagnosticDenied) as exc: diagnostic.run_counting_observation(live_opt_in=True)
    assert exc.value.code=="PERSISTENCE_FAILURE" and not c["credentials"] and not c["calls"]
    with sqlite3.connect(str(c["path"])) as db:
        assert "observation_json" not in [r[1] for r in db.execute("PRAGMA table_info(attempts)")]


def test_sdk_conversion_drift_denied_before_credentials(counting,monkeypatch):
    from google.genai import models
    c=counting;c["provider"]()
    original=models._CountTokensParameters_to_mldev
    def drift(*args):
        result=original(*args)
        result["unexpected"]=True
        return result
    monkeypatch.setattr(models,"_CountTokensParameters_to_mldev",drift)
    with pytest.raises(controls.DiagnosticDenied) as exc: diagnostic.run_counting_observation(live_opt_in=True)
    assert exc.value.code=="PAYLOAD_MISMATCH" and not c["credentials"] and not c["calls"]


@pytest.mark.parametrize("field,value", [("GEMINI_CREDENTIAL_PROFILE","wrong"),("GEMINI_CREDENTIAL_REVISION","wrong"),
                                        ("GEMINI_API_KEY","invalid key")])
def test_resolver_mismatch_is_durable_without_secret_leak(counting,monkeypatch,field,value):
    c=counting;c["provider"]()
    monkeypatch.setenv(field,value)
    with pytest.raises(controls.DiagnosticDenied) as exc: diagnostic.run_counting_observation(live_opt_in=True)
    assert exc.value.code in ("CREDENTIAL_PROFILE_MISMATCH","CREDENTIAL_INVALID")
    assert len(c["credentials"])==1 and not c["calls"]
    assert value not in str(exc.value) and c["rows"]()[0][1:4]==(1,1,"UNKNOWN")


def test_final_guard_denies_source_change_after_reservation(counting,monkeypatch):
    c=counting;c["provider"]()
    resolver=diagnostic.resolve_diagnostic_credential
    def changed(*args):
        result=resolver(*args)
        monkeypatch.setattr(diagnostic,"resolve_repository_source",lambda _: (c["grant"].source_revision,False))
        return result
    monkeypatch.setattr(diagnostic,"resolve_diagnostic_credential",changed)
    result=diagnostic.run_counting_observation(live_opt_in=True)
    assert result.outcome=="FAILED" and result.code=="SOURCE_MISMATCH"
    assert not c["calls"] and c["rows"]()[0][1:4]==(1,1,"UNKNOWN")


def test_same_fixed_host_loader_accepts_counting_artifacts(counting,monkeypatch,tmp_path):
    from test_knowledge_gemini_diagnostic_controls import e4_host_fixture
    from knowledge_index.identity import document_identity
    c=counting
    paths,_,_,_=e4_host_fixture(tmp_path,monkeypatch)
    grant=replace(c["grant"],runner_identity="S-1-5-21-100")
    digest=controls.authorization_record_digest(grant)
    e4=replace(c["e4"],authorization_record_sha256=digest)
    value=c["value"]
    value["binding"]["authorization_record_sha256"]=digest
    source="ops.shared-database-network-dependency-interruption"
    text=c["approved"].envelopes[0]
    material=dict(schema_version=1,campaign_id=grant.campaign_id,source_revision=grant.source_revision,
        purpose=controls.COUNTING_OPERATION,kind="document",scope_identity=grant.input_scope_identity,
        input_commitment=controls.COUNTING_INPUT,ordered_items=[dict(chunk_identity=controls.COUNTING_CHUNK,
            document_identity=document_identity(grant.input_scope_identity,source),approved_document_id=source,
            chunk_content=text.split(" | text: ",1)[1],envelope=text)])
    wire=asdict(e4)
    for key in ("valid_from","valid_until"):wire[key]=wire[key].isoformat()
    paths.campaign.write_text(json.dumps(asdict(grant)),encoding="utf-8")
    paths.approved_input.write_text(json.dumps(material),encoding="utf-8")
    paths.sdk_request_evidence.write_text(json.dumps(wire),encoding="utf-8")
    paths.safety_evidence.write_text(json.dumps(value),encoding="utf-8")
    monkeypatch.setattr(controls,"resolve_repository_source",lambda _: (grant.source_revision,True))
    loaded=controls.load_live_binding()
    assert loaded.record.purpose==controls.COUNTING_OPERATION
    assert loaded.sdk_byte_evidence.output_dimensionality is None
    assert controls.load_live_safety_artifact().fields["operation"]==controls.COUNTING_OPERATION
    assert not c["credentials"] and not c["calls"]
