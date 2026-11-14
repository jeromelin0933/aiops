"""December contracts on isolated stores; every approval is synthetic.

No actual S2/S5 runtime, exporter socket or live HTTP source is executed here.
"""
from datetime import datetime, timezone, timedelta
from dataclasses import replace, FrozenInstanceError
from hashlib import sha256
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from threading import Event, get_ident
from types import SimpleNamespace

import pytest
import yaml

from runtime_orchestration import capture_controller as c, capture_preparation as p
from runtime_orchestration.config import load_runtime_config
from incident_evidence.demo_v2 import DemoPolicyBinding, DECEMBER_SCOPE, DECEMBER_POLICY, DECEMBER_AMENDMENT_SHA256
from test_spec013_demo_v2 import policy_binding, LOKI, PROM, REV

REPO = Path(__file__).resolve().parents[1]


class AdmissionClock:
    """Controllable authority wall time, independent of the execution deadline."""
    def __init__(self, wall):
        self.wall = wall
        self.started = time.monotonic()

    def runtime_clock(self):
        from runtime_orchestration.clock import RuntimeClock
        return RuntimeClock(lambda:self.wall+timedelta(seconds=time.monotonic()-self.started),
                            time.monotonic, time.sleep)


def publication_expiry(verified, kind):
    return next(item.expires_at for item in verified.publication_validity if item.kind == kind)


@pytest.fixture(autouse=True)
def isolation(monkeypatch, tmp_path):
    def deny(*a, **kw): raise AssertionError("no network/provider invocation")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    original = sqlite3.connect
    def connect(path, *a, **kw):
        assert Path(path).resolve().is_relative_to(tmp_path)
        return original(path, *a, **kw)
    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(c, "_active_session", None)
    yield
    active = c._active_session
    if active is not None:
        active.stop.set()
        active.thread.join(5)
        assert not active.thread.is_alive(), "offline worker must be released by test"


def fixture(tmp_path, *, seconds=5, publication_seconds=None, grant_seconds=3600):
    repo = tmp_path / "repository"
    for name in c.DECEMBER_BOUND_FILES + ("configs/runtime_orchestration.yaml",):
        target = repo/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO/name, target)
    (repo/"models").mkdir()
    for name in ("log", "metrics"):
        shutil.copyfile(REPO/"models"/(name+"_isolation_forest.pkl"), repo/"models"/(name+"_isolation_forest.pkl"))
    profile = yaml.safe_load((repo/p.DECEMBER_CONFIG).read_bytes())
    profile["external_root"] = str(tmp_path/"children")
    (repo/p.DECEMBER_CONFIG).write_text(yaml.safe_dump(profile), encoding="utf-8")
    now = datetime.now(timezone.utc)
    data = policy_binding().record
    endpoints = {"loki":"http://localhost:3100/loki/api/v1/query_range",
                 "prometheus":"http://localhost:9090/api/v1/query_range"}
    data.update(scope=DECEMBER_SCOPE, policy_identity=DECEMBER_POLICY,
                scope_approval_reference="OFFLINE-DECEMBER-ACCEPTANCE", scope_approval_sha256=DECEMBER_AMENDMENT_SHA256)
    for kind, value in endpoints.items(): data[kind+"_endpoint_sha256"] = sha256(value.encode()).hexdigest()
    raw_policy = json.dumps(data).encode()
    d3 = DemoPolicyBinding.from_published_record(raw_policy, expected_sha256=sha256(raw_policy).hexdigest(),
        expected_source_revision=REV, now=now, scope_approval_pin=(data["scope_approval_reference"], data["scope_approval_sha256"]))
    base_path = repo/"configs/runtime_orchestration.yaml"
    facts = c.december_binding_observations(repo, base_path, "s2")
    facts.update(source_revision=REV, clean_checkpoint=True, observability_deployment_sha256="d"*64,
                 d3_policy_sha256=d3.record_sha256, endpoints=endpoints)
    policies, pins = {}, []
    for kind, identity in (("selector","BoundedServiceOrV1"), ("trace","loki-demo-bounded-json-trace-v2"),
                           ("metric","GlobalLatencyContextV1"), ("clearance","host-clearance-v1")):
        policy = dict(decision="APPROVE", issuer_role="SPEC_LEAD",issuer_identity="OFFLINE-OWNER",
            authority=p.DECEMBER_AUTHORITY, kind=kind, identity=identity,
            semantic_commitment=c.commitment({"offline":kind}),
            valid_from=(now-timedelta(minutes=1)).isoformat(),
            expires_at=(now+timedelta(seconds=(publication_seconds or {}).get(kind,3600))).isoformat())
        if kind == "clearance":
            policy.update(run_id=facts["child"]["run_id"], predecessor_state="HUMAN_CONFIRMED_QUIESCENT_AND_RECONCILED",
                          execution_binding_sha256=c.commitment({k:v for k,v in facts.items() if k!="policies"}))
        raw = json.dumps(policy).encode()
        digest = sha256(raw).hexdigest()
        policies[kind] = raw
        pins.append((kind,digest))
        facts["policies"][kind] = dict(identity=identity, semantic_commitment=policy["semantic_commitment"], record_sha256=digest)
    record = dict(schema_version=1,decision="APPROVE_CAPTURE",authority=p.DECEMBER_AUTHORITY,
        issuer_identity="OFFLINE-OWNER",issuer_role="SPEC_LEAD_DEMO_HOST_OWNER",reference="OFFLINE-FINAL-GRANT",
        revision=1,valid_from=(now-timedelta(minutes=1)).isoformat(),expires_at=(now+timedelta(seconds=grant_seconds)).isoformat(),
        binding=facts,limits=dict(max_triggers=1,max_captures=1,max_cycles=2,max_seconds=seconds,
                                io_timeout_seconds=5,max_output_bytes=8*1024*1024))
    raw = json.dumps(dict(record=record,content_sha256=c.commitment(record))).encode()
    pin = c.HumanPublicationPin("OFFLINE-OWNER",record["reference"],1,sha256(raw).hexdigest(),tuple(pins))
    (repo/"offline-publications.json").write_text(json.dumps({"approval":raw.decode(),
        "policies":{k:v.decode() for k,v in policies.items()},
        "pin":{"issuer_identity":pin.issuer_identity,"reference":pin.reference,"revision":pin.revision,
               "record_sha256":pin.record_sha256,"policy_record_sha256":list(pin.policy_record_sha256)}}),encoding="utf-8")
    verified = c.verify_capture_approval(raw,pin,actual_binding=facts,policy_records=policies,now=now,
        explicit_opt_in="AUTHORIZE CAPTURE "+facts["child"]["run_id"])
    return repo,base_path,d3,verified


def test_december_derivation_no_writes_or_domain_changes(tmp_path):
    repo,base,d3,v = fixture(tmp_path)
    child,bundle = p.december_config_bundle(repo,load_runtime_config(base),"s2")
    assert not Path(child["root"]).exists()
    runtime_path=tmp_path/"runtime.yaml";runtime_path.write_bytes(bundle["configuration/runtime.yaml"])
    runtime=load_runtime_config(runtime_path)
    assert runtime.rca is None
    assert runtime.retry_limit == load_runtime_config(base).retry_limit
    for name in ("event_detection.yml","thresholds.yaml","metrics_iforest.yaml"):
        data=yaml.safe_load(bundle["configuration/"+name])
        assert data["output"]["event_store_path"]==child["stores"]["event"]
        for key in ("anomaly","classification","isolation_forest"):
            original=yaml.safe_load((repo/"configs"/name).read_bytes())
            if key in original: assert data[key]==original[key]


def test_f01_validity_proof_matches_pinned_bytes_and_rejects_replacement(tmp_path):
    repo,_,_,verified=fixture(tmp_path)
    original=json.loads((repo/"offline-publications.json").read_text(encoding="utf-8"))
    assert {item.kind for item in verified.publication_validity}=={"selector","trace","metric","clearance"}
    for item in verified.publication_validity:
        raw=original["policies"][item.kind].encode()
        data=json.loads(raw)
        assert (item.record_sha256,item.identity,item.valid_from,item.expires_at)==(
            sha256(raw).hexdigest(),data["identity"],c._time(data["valid_from"]),c._time(data["expires_at"]))
        with pytest.raises(FrozenInstanceError):item.expires_at+=timedelta(days=1)
    forged=replace(verified,publication_validity=tuple(replace(x,expires_at=x.expires_at+timedelta(days=1))
                                                     for x in verified.publication_validity))
    with pytest.raises(c.CaptureDenied,match="not verified"):c.execute_capture(forged)
    with pytest.raises(c.CaptureDenied,match="not verified"):
        c.execute_capture(replace(verified,publication_validity=()))
    with pytest.raises(c.CaptureDenied,match="not verified"):
        c.execute_capture(replace(verified,grant_valid_from=verified.grant_valid_from-timedelta(days=1)))


@pytest.mark.parametrize("kind", ["selector","trace","metric","clearance"])
def test_f01_verified_then_expired_before_execute_no_worker_or_intent(tmp_path,monkeypatch,kind):
    from runtime_orchestration.clock import RuntimeClock
    repo,base,d3,v=fixture(tmp_path,publication_seconds={kind:30})
    clock=AdmissionClock(publication_expiry(v,kind))
    assert clock.wall<v.expires_at
    d3.require_current(clock.wall)
    monkeypatch.setattr(RuntimeClock,"system",staticmethod(clock.runtime_clock))
    def forbidden(*a,**kw):raise AssertionError("no Worker before current authorization")
    monkeypatch.setattr(c,"Thread",forbidden)
    with pytest.raises(c.CaptureDenied,match=kind+" authorization"):
        c.execute_capture(v,repository=repo,base_config_path=base,demo_policy=d3)
    assert c._active_session is None
    assert not Path(v.facts["child"]["root"]).exists()
    archived=json.loads((repo/"offline-publications.json").read_text(encoding="utf-8"))
    pin=archived["pin"]
    pin["policy_record_sha256"]=tuple(tuple(item) for item in pin["policy_record_sha256"])
    with pytest.raises(c.CaptureDenied,match="policy approval binding"):
        c.verify_capture_approval(archived["approval"].encode(),c.HumanPublicationPin(**pin),
            actual_binding=v.facts,policy_records={k:data.encode() for k,data in archived["policies"].items()},
            now=clock.runtime_clock().now(),explicit_opt_in="AUTHORIZE CAPTURE "+v.stop.run_id)


@pytest.mark.parametrize("kind", ["selector","trace","metric","clearance"])
def test_f01_expiry_at_next_boundary_stops_work_not_deadline(tmp_path,kind):
    _,_,d3,v=fixture(tmp_path,publication_seconds={kind:30})
    clock=AdmissionClock(v.grant_valid_from+timedelta(minutes=1))
    session=c._CaptureSession(v,clock.runtime_clock(),d3)
    session.boundary()
    clock.wall=publication_expiry(v,kind)
    with pytest.raises(c.CaptureDenied,match=kind+" authorization"):session.boundary()
    assert session.stop.is_set()
    assert session.report["status"]=="AUTHORIZATION_NOT_CURRENT"
    assert session.report["authorization_kind"]==kind
    assert session.report["deadline_exceeded"] is False
    assert not Path(v.facts["child"]["root"]).exists()


def test_f01_local_preflight_expiry_rechecked_before_worker(tmp_path,monkeypatch):
    from runtime_orchestration.clock import RuntimeClock
    repo,base,d3,v=fixture(tmp_path,publication_seconds={"clearance":30})
    clock=AdmissionClock(v.grant_valid_from+timedelta(minutes=1))
    monkeypatch.setattr(RuntimeClock,"system",staticmethod(clock.runtime_clock))
    original=p.december_config_bundle
    def slow_observation(*a,**kw):
        result=original(*a,**kw)
        clock.wall=publication_expiry(v,"clearance")
        return result
    monkeypatch.setattr(p,"december_config_bundle",slow_observation)
    with pytest.raises(c.CaptureDenied,match="clearance authorization"):
        c.execute_capture(v,repository=repo,base_config_path=base,demo_policy=d3)
    assert c._active_session is None
    assert not Path(v.facts["child"]["root"]).exists()


@pytest.mark.parametrize("kind", ["final_grant","d3_policy"])
def test_f01_final_grant_and_d3_still_checked_at_boundary(tmp_path,kind):
    _,_,d3,v=fixture(tmp_path,grant_seconds=7200)
    clock=AdmissionClock(v.grant_valid_from+timedelta(minutes=1))
    session=c._CaptureSession(v,clock.runtime_clock(),d3)
    session.boundary()
    clock.wall=v.expires_at if kind=="final_grant" else c._time(d3.record["expires_at"])
    with pytest.raises(c.CaptureDenied,match=kind+" authorization"):session.boundary()
    assert session.report["deadline_exceeded"] is False


@pytest.mark.parametrize("kind", ["selector","trace","metric","clearance"])
@pytest.mark.parametrize("next_unit", ["trigger","cycle","capture"])
def test_f01_running_expiry_prevents_next_unit_preserves_intent(tmp_path,monkeypatch,kind,next_unit):
    from runtime_orchestration.clock import RuntimeClock
    from runtime_orchestration.worker import RuntimeStopController
    from runtime_orchestration import cli
    repo,base,d3,v=fixture(tmp_path,publication_seconds={kind:30})
    clock=AdmissionClock(datetime.now(timezone.utc))
    monkeypatch.setattr(RuntimeClock,"system",staticmethod(clock.runtime_clock))
    calls=[]
    def expire():clock.wall=publication_expiry(v,kind)
    class Scenario:
        def start(self):
            calls.append("start")
            if next_unit=="trigger":expire()
        def trigger(self,*a):calls.append("trigger");return SimpleNamespace(accepted=True)
        def tick(self):calls.append("tick");return SimpleNamespace(phase="BASELINE")
        def stop(self):calls.append("stop")
    def detected():
        calls.append("detection")
        if next_unit=="cycle":expire()
        return SimpleNamespace(failure_count=0)
    def views():
        calls.append("public_incident_read")
        if next_unit=="capture":expire()
        return (SimpleNamespace(incident_id="OFFLINE-INCIDENT"),)
    def forbidden_capture(*a):raise AssertionError("no new CaptureCommand after expiry")
    core=SimpleNamespace(_stop=RuntimeStopController(),capture_failure=None,
        startup=lambda:calls.append("startup"),run_cycle=lambda:calls.append("cycle"))
    components=c.CaptureComponents(Scenario(),SimpleNamespace(run_due_once=detected),core,
        SimpleNamespace(capture_evidence=forbidden_capture),SimpleNamespace(list_correlation_views=views),
        forbidden_capture,lambda:calls.append("close"))
    monkeypatch.setattr(cli,"_build_december_capture_components",lambda *a,**kw:components)
    result=c.execute_capture(v,repository=repo,base_config_path=base,demo_policy=d3)
    assert result["status"]=="AUTHORIZATION_NOT_CURRENT"
    assert result["authorization_kind"]==kind and result["deadline_exceeded"] is False
    assert next_unit not in calls
    assert calls[-2:]==["stop","close"]
    assert core._stop.requested and c._active_session.closed
    intent=Path(v.facts["child"]["root"])/"capture-intent.json"
    assert json.loads(intent.read_text())["execution_binding_sha256"]==v.stop.execution_binding_sha256
    assert not Path(v.facts["child"]["stores"]["evidence"]).exists()


@pytest.mark.parametrize("name", ["old.sqlite3","capture-report.json","capture-intent.json","unknown"])
def test_preconfigured_whitelist_rejects_old_data(tmp_path,name):
    _,_,_,v=fixture(tmp_path)
    root=Path(v.facts["child"]["root"]);root.mkdir(parents=True)
    (root/name).write_text("untouched")
    with pytest.raises(c.CaptureDenied):c._claim_new_child(v)
    assert (root/name).read_text()=="untouched"


def test_clean_preconfiguration_and_one_shot(tmp_path):
    repo,base,_,v=fixture(tmp_path)
    child,bundle=p.december_config_bundle(repo,load_runtime_config(base),"s2")
    root=Path(child["root"])
    (root/"observations").mkdir(parents=True)
    for name,data in bundle.items():
        path=root/name;path.parent.mkdir(exist_ok=True);path.write_bytes(data)
    assert c._claim_new_child(v)==root
    before=(root/"capture-intent.json").read_bytes()
    with pytest.raises(c.CaptureDenied):c._claim_new_child(v)
    assert (root/"capture-intent.json").read_bytes()==before


def test_windows_same_child_verified_claim_two_real_processes(tmp_path):
    # Actual Guard and _claim_new_child in two Windows processes; no Domain work.
    if sys.platform != "win32":pytest.skip("Windows evidence required")
    repo,_,_,v=fixture(tmp_path)
    root=Path(v.facts["child"]["root"]);(root/"observations").mkdir(parents=True)
    gate=tmp_path/"gate";intent=root/"capture-intent.json"
    code='''import pathlib,sys,time,json
from datetime import datetime,timezone
from runtime_orchestration import capture_controller as c
data=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
record=json.loads(data["approval"])["record"]
pin=data["pin"];pin["policy_record_sha256"]=tuple(tuple(x) for x in pin["policy_record_sha256"])
v=c.verify_capture_approval(data["approval"].encode(),c.HumanPublicationPin(**pin),actual_binding=record["binding"],
 policy_records={k:v.encode() for k,v in data["policies"].items()},now=datetime.now(timezone.utc),
 explicit_opt_in="AUTHORIZE CAPTURE "+record["binding"]["child"]["run_id"])
while not pathlib.Path(sys.argv[2]).exists():time.sleep(.001)
try:
 c._claim_new_child(v)
 print("CLAIMED")
except (FileExistsError,c.CaptureDenied):print("REJECTED")
'''
    processes=[subprocess.Popen([sys.executable,"-B","-c",code,str(repo/"offline-publications.json"),str(gate)],stdout=subprocess.PIPE,text=True) for _ in range(2)]
    gate.write_text("release")
    values=[process.communicate(timeout=10)[0].strip() for process in processes]
    assert sorted(values)==["CLAIMED","REJECTED"]
    assert json.loads(intent.read_text())["run_id"]==v.stop.run_id
    assert not Path(v.facts["child"]["stores"]["incident"]).exists()


def test_timeout_retains_owner_and_blocks_reentry(tmp_path,monkeypatch):
    from runtime_orchestration import cli
    repo,base,d3,v=fixture(tmp_path,seconds=.2)
    entered,release=Event(),Event()
    calls=[]
    def blocked(*args,**kwargs):
        calls.append(get_ident());entered.set()
        assert release.wait(5)
        raise RuntimeError("injected construction failure after timeout")
    monkeypatch.setattr(cli,"_build_december_capture_components",blocked)
    try:
        result=c.execute_capture(v,repository=repo,base_config_path=base,demo_policy=d3)
        assert entered.is_set() and result["status"]=="TIMEOUT_UNCONFIRMED"
        assert c._active_session.thread.is_alive()
        with pytest.raises(c.CaptureDenied,match="previous execution"):
            c.execute_capture(v,repository=repo,base_config_path=base,demo_policy=d3)
        assert len(calls)==1 and calls[0]!=get_ident()
    finally:
        release.set();c._active_session.thread.join(5)
    assert c._active_session.report["status"]=="TIMEOUT_UNCONFIRMED"
    with pytest.raises(c.CaptureDenied,match="previous execution"):
        c.execute_capture(v,repository=repo,base_config_path=base,demo_policy=d3)


def test_restart_does_not_reclaim_existing_intent(tmp_path,monkeypatch):
    repo,base,d3,v=fixture(tmp_path)
    c._claim_new_child(v)
    monkeypatch.setattr(c,"_active_session",None) # memory loss cannot erase durable claim
    result=c.execute_capture(v,repository=repo,base_config_path=base,demo_policy=d3)
    assert result["status"]=="UNKNOWN"
    assert c._active_session.components is None
    assert not Path(v.facts["child"]["stores"]["incident"]).exists()


def test_source_change_denied_before_intent(tmp_path):
    repo,base,d3,v=fixture(tmp_path)
    (repo/"src/incident_evidence/demo_v2.py").write_text("modified")
    with pytest.raises(c.CaptureDenied,match="source byte"):
        c.execute_capture(v,repository=repo,base_config_path=base,demo_policy=d3)
    assert not Path(v.facts["child"]["root"]).exists()


def test_december_policy_separate_scope_pin_and_projection(tmp_path):
    _,_,d3,_=fixture(tmp_path)
    assert d3.projection()["policy_identity"]==DECEMBER_POLICY
    assert d3.projection()["scope"]==DECEMBER_SCOPE
    raw=json.dumps(d3.record).encode()
    with pytest.raises(ValueError,match="independent December"):
        DemoPolicyBinding.from_published_record(raw,expected_sha256=sha256(raw).hexdigest(),
            expected_source_revision=REV,now=datetime.now(timezone.utc))
    assert policy_binding().projection()["scope"]=="COMPETITION_DEMO_ONLY"


@pytest.mark.parametrize("field,value",[("scope","UNKNOWN_DEMO_SCOPE"),("policy_identity","unknown"),
    ("scope_approval_sha256","0"*64),("version","unknown-version"),("unexpected",True)])
def test_december_unknown_policy_or_scope_fails_closed(tmp_path,field,value):
    _,_,d3,_=fixture(tmp_path)
    data=d3.record;data[field]=value
    raw=json.dumps(data).encode()
    with pytest.raises(ValueError):
        DemoPolicyBinding.from_published_record(raw,expected_sha256=sha256(raw).hexdigest(),
            expected_source_revision=REV,now=datetime.now(timezone.utc),
            scope_approval_pin=(data["scope_approval_reference"],data["scope_approval_sha256"]))


def test_scope_amendment_commitment_matches_accepted_spec():
    text=(REPO/"docs/spec/SPEC-013_Incident_Evidence_Collection_Snapshot.md").read_text(encoding="utf-8")
    start=text.index("### 24.8 December Demo D3 Scope")
    body=text[start:text.index("\nChange History",start)].strip()+"\n"
    assert sha256(body.encode()).hexdigest()==DECEMBER_AMENDMENT_SHA256


@pytest.mark.parametrize("run_id", ["competition-capture-20261104-01-s2", "competition-capture-20261104-01-s5",
    "december-capture-20261214-01-s2", "december-capture-20261214-01-s5"])
def test_exact_worker_compatibility_allowlist_is_not_authorization(run_id):
    from runtime_orchestration.worker import CaptureStopBinding
    value=CaptureStopBinding(run_id,"a"*40,"b"*64,"c"*64,"OFFLINE-ONLY")
    with pytest.raises(c.CaptureDenied,match="not verified"):
        c.execute_capture(value)


@pytest.mark.parametrize("run_id", ["december-capture-other-s2", "december-capture-20261214-01-s3", "anything"])
def test_unknown_worker_run_rejected(run_id):
    from runtime_orchestration.worker import CaptureStopBinding
    with pytest.raises(ValueError):CaptureStopBinding(run_id,"a"*40,"b"*64,"c"*64,"OFFLINE-ONLY")


@pytest.mark.parametrize("fault", ["none", "late-response", "lost-response", "report-failure",
                                  "publication-expired-in-commit", "publication-expired-during-cleanup"])
def test_formal_factory_offline_domain_public_readback(tmp_path,monkeypatch,fault):
    """Real Runtime/Incident/Evidence stores, mock detector/generator/transports.

    Does not execute a real scenario or claim real S2/S5 evidence.
    """
    from event_detection.store.event_store import EventStore
    from runtime_orchestration import cli
    from scenario_runtime import integration, runtime
    from event_detection import runner
    from incident_evidence import SqliteEvidenceStore, EvidenceCaptureService
    from incident_evidence.demo_v2 import LokiDemoV2Adapter, PrometheusDemoV2Adapter, VERSION
    from test_runtime_host_e2e import _event
    expiry_fault=fault.startswith("publication-expired")
    repo,base,d3,v=fixture(tmp_path,publication_seconds={"selector":30} if expiry_fault else None)
    clock=AdmissionClock(datetime.now(timezone.utc))
    if expiry_fault:
        from runtime_orchestration.clock import RuntimeClock
        monkeypatch.setattr(RuntimeClock,"system",staticmethod(clock.runtime_clock))
    owners=[];sources=[];constructed=[]
    class Generator:
        def __init__(self, config):
            def close(**kw):
                owners.append(("close",get_ident()))
                if fault=="publication-expired-during-cleanup":clock.wall=publication_expiry(v,"selector")
            self.metrics_generator=SimpleNamespace(stop_exporter=close)
        def start(self):owners.append(("start",get_ident()))
        def tick(self,*a):raise AssertionError("no actual generator")
    class Scenario:
        def __init__(self,*a,**kw):pass
        def start(self):pass
        def trigger(self,*a):return SimpleNamespace(accepted=True)
        def tick(self):return SimpleNamespace(phase="BASELINE")
        def stop(self):pass
    class Detection:
        def __init__(self,path,**kw):
            config=yaml.safe_load(Path(path).read_bytes())
            for settings in config["pipelines"].values():
                data=yaml.safe_load(Path(settings["config_path"]).read_bytes())
                assert data["output"]["event_store_path"]==v.facts["child"]["stores"]["event"]
            constructed.append(get_ident())
        def run_due_once(self):
            event=_event("OFFLINE-EVENT","brute_force_detected",datetime.now(timezone.utc),source_ip="192.0.2.10",trace_id="t-1")
            event["downstream_service"]="core-db"
            event["detection_method"]="isolation_forest"
            EventStore(Path(v.facts["child"]["stores"]["event"])).write(event)
            return SimpleNamespace(failure_count=0)
        def stop(self):pass
    def logs(self,request,params):
        sources.append("LOKI")
        return {"status":"success","data":{"resultType":"streams","result":[
            {"stream":{"service_name":"auth-api","downstream_service":"core-db","source_ip":"192.0.2.10"},
             "values":[[str(int(params["start"])+10**9),'{"trace_id":"t-1","message":"offline observation"}']]}]}}
    def metrics(self,request,params):
        sources.append("PROMETHEUS")
        return {"status":"success","data":{"resultType":"matrix","result":[
            {"metric":{"__name__":"api_p95_latency_ms","job":"offline"},
             "values":[[request.logical_window.start.timestamp()+1,"4500"]]}]}}
    monkeypatch.setattr(integration,"GeneratorAdapter",Generator)
    monkeypatch.setattr(runtime,"MockDataRuntime",Scenario)
    monkeypatch.setattr(runner,"EventDetectionRunner",Detection)
    monkeypatch.setattr(LokiDemoV2Adapter,"_fetch",logs)
    monkeypatch.setattr(PrometheusDemoV2Adapter,"_fetch",metrics)
    def forbidden(*a,**kw):raise AssertionError("RCA/provider construction forbidden")
    monkeypatch.setattr(cli,"_build_rca_host",forbidden)
    release,committed=Event(),Event()
    original=EvidenceCaptureService.capture_evidence
    def commit_then_fault(self,*args,**kw):
        result=original(self,*args,**kw)
        committed.set()
        if fault=="late-response":assert release.wait(10)
        if fault=="lost-response":raise OSError("injected response loss after commit")
        return result
    monkeypatch.setattr(EvidenceCaptureService,"capture_evidence",commit_then_fault)
    if fault=="publication-expired-in-commit":
        inject=SqliteEvidenceStore._inject
        def expire_during_transaction(self,point):
            if point=="during_transaction":clock.wall=publication_expiry(v,"selector")
            return inject(self,point)
        monkeypatch.setattr(SqliteEvidenceStore,"_inject",expire_during_transaction)
    if fault=="report-failure":monkeypatch.setattr(c,"_persist_report",lambda *a: (_ for _ in ()).throw(OSError("injected report failure")))
    try:
        result=c.execute_capture(v,repository=repo,base_config_path=base,demo_policy=d3)
        assert committed.is_set(),result
        assert result["status"]=={"none":"SUCCESS","late-response":"TIMEOUT_UNCONFIRMED",
            "lost-response":"UNKNOWN","report-failure":"UNKNOWN",
            "publication-expired-in-commit":"AUTHORIZATION_NOT_CURRENT",
            "publication-expired-during-cleanup":"AUTHORIZATION_NOT_CURRENT"}[fault],result
        if expiry_fault:
            assert result["authorization_kind"]=="selector" and result["deadline_exceeded"] is False
    finally:
        release.set()
    assert sources==["LOKI","PROMETHEUS"]
    c._active_session.thread.join(5)
    assert c._active_session.closed
    assert constructed==[owners[0][1]] and owners[0][1]==owners[-1][1]!=get_ident()
    with SqliteEvidenceStore(Path(v.facts["child"]["stores"]["evidence"])) as store:
        operation_id="capture-"+v.stop.run_id
        outcome=store.read_capture_outcome(operation_id)
        snapshot=store.resolve_snapshot(outcome.snapshot_id,accepted_demo_version=VERSION)
        revision=store.resolve_revision(outcome.revision_id,accepted_demo_version=VERSION)
        assert outcome.snapshot_id==snapshot.snapshot_id and snapshot.revision_id==revision.revision_id
        assert snapshot.snapshot_content["demo_v2"]["binding"]["scope"]==DECEMBER_SCOPE
        assert store.read_capture_outcome(operation_id)==outcome
        from incident_evidence import EvidenceSnapshot
        for field,value in (("policy_identity","unknown"),("scope_approval_sha256","0"*64),("scope","UNKNOWN")):
            content=snapshot.snapshot_content
            content["demo_v2"]["binding"][field]=value
            with pytest.raises(ValueError):
                EvidenceSnapshot.from_content(c._active_session.components.command_for(snapshot.incident_id,snapshot.snapshot_at),
                    revision_id=revision.revision_id,completeness=snapshot.completeness,
                    source_statuses=snapshot.source_statuses,snapshot_content=content)
    assert sources==["LOKI","PROMETHEUS"] # Public read/reopen never re-fetches.
    if fault!="none":
        with pytest.raises(c.CaptureDenied,match="selector authorization" if expiry_fault else "previous execution"):
            c.execute_capture(v,repository=repo,base_config_path=base,demo_policy=d3)
