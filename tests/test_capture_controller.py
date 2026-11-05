"""Isolated controller contracts. All approval records here are synthetic."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path, PureWindowsPath
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from runtime_orchestration import capture_controller as c
from runtime_orchestration import capture_preparation as p
from runtime_orchestration.worker import CorrelationRuntimeCore
from test_runtime_host_e2e import _HostRuntime, _event, NOW
from capture_controller_fixture_harness import drive_isolated_components


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    def deny(*args, **kwargs):
        raise AssertionError("network/provider forbidden")
    for obj, name in ((socket.socket, "connect"), (socket, "getaddrinfo"),
                      (socket, "create_connection")):
        monkeypatch.setattr(obj, name, deny)
    original = sqlite3.connect
    def local_only(database, *args, **kwargs):
        assert Path(database).resolve().is_relative_to(tmp_path)
        return original(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", local_only)
    monkeypatch.setattr(p, "EXTERNAL_ROOT", PureWindowsPath(tmp_path / "handoff"))


def approved_fixture():
    policies, pins, bindings = {}, [], {}
    for kind in ("selector", "trace", "metric"):
        data = {"decision": "APPROVE", "issuer_role": "SPEC_LEAD",
                "issuer_identity": "OFFLINE-FIXTURE-OWNER", "authority": p.AUTHORITY,
                "kind": kind, "identity": "OFFLINE-ONLY-" + kind,
                "semantic_commitment": c.commitment({"offline": kind}),
                "valid_from": (NOW-timedelta(minutes=1)).isoformat(),
                "expires_at": (NOW+timedelta(hours=1)).isoformat()}
        raw = json.dumps(data).encode()
        digest = sha256(raw).hexdigest()
        policies[kind] = raw
        pins.append((kind, digest))
        bindings[kind] = {"identity": data["identity"], "semantic_commitment": data["semantic_commitment"],
                          "record_sha256": digest}
    source_manifest = {name:c.commitment({"OFFLINE_FIXTURE_SOURCE":name}) for name in c.SOURCE_BOUND_FILES}
    facts = {"case": "s5", "parent_run": p.PARENT_RUN, "child": p.child_binding("s5"),
             "source_revision": "a"*40, "source_manifest":source_manifest,
             "source_manifest_sha256":c.commitment(source_manifest),
             "runtime_config_sha256": "c"*64, "effective_config_sha256": "d"*64,
             "observability_deployment_sha256": "e"*64,
             "models": dict(c.MODEL_SHA256),
             "scenario_config_sha256": p.SCENARIO_SHA, "scenario_input_sha256": p.INPUT_SHA["s5"],
             "controller_version": c.CONTROLLER_VERSION, "stop_hook_version": c.STOP_HOOK_VERSION,
             "disabled_routes": list(c.DISABLED_ROUTES), "rca": None,
             "clean_checkpoint": True, "mount_mode": "ro",
             "log_mount_source": str(Path(p.child_binding("s5")["log_output"]).parent),
             "policies": bindings}
    record = {"schema_version": 1, "decision": "APPROVE_CAPTURE", "authority": p.AUTHORITY,
              "issuer_identity": "OFFLINE-FIXTURE-OWNER", "issuer_role": "SPEC_LEAD_DEMO_HOST_OWNER",
              "reference": "OFFLINE-FIXTURE-NOT-A-LIVE-GRANT", "revision": 1,
              "valid_from": (NOW-timedelta(minutes=1)).isoformat(),
              "expires_at": (NOW+timedelta(hours=1)).isoformat(), "binding": facts,
              "limits": {"max_triggers": 1, "max_captures": 1, "max_cycles": 2,
                         "max_seconds": 10, "io_timeout_seconds": 2, "max_output_bytes": 1024*1024}}
    return record, policies, pins


def verify(record=None, *, actual=None, opt_in=None, now=NOW, policies_override=None):
    fixture, policies, pins = approved_fixture()
    record = fixture if record is None else record
    try:
        content_sha = c.commitment(record)
    except ValueError:
        content_sha = "0"*64  # Exercise parser rejection of non-finite JSON, not fixture serialization.
    raw = json.dumps({"record": record, "content_sha256": content_sha}).encode()
    publication = c.HumanPublicationPin("OFFLINE-FIXTURE-OWNER", "OFFLINE-FIXTURE-NOT-A-LIVE-GRANT",
                                       1, sha256(raw).hexdigest(), tuple(pins))
    return c.verify_capture_approval(raw, publication, actual_binding=record["binding"] if actual is None else actual,
        policy_records=policies if policies_override is None else policies_override, now=now,
        explicit_opt_in=opt_in if opt_in is not None else "AUTHORIZE CAPTURE " + p.child_binding("s5")["run_id"])


def test_pinned_record_full_binding_and_independent_policies():
    result = verify()
    assert result.stop.run_id == p.PARENT_RUN + "-s5"
    assert result.stop.execution_binding_sha256 == c.commitment(result.facts)
    assert result.limits["max_triggers"] == 1


@pytest.mark.parametrize("field,value", [("decision", "PENDING"), ("authority", "other"),
    ("issuer_identity", "OTHER"), ("issuer_role", "CALLER"), ("revision", 2),
    ("schema_version", True), ("expires_at", NOW.isoformat())])
def test_republished_but_invalid_approval_rejected(field, value):
    record, _, _ = approved_fixture()
    record[field] = value
    with pytest.raises(c.CaptureDenied): verify(record)


@pytest.mark.parametrize("field,value", [("source_revision", "PENDING"), ("mount_mode", "rw"),
    ("clean_checkpoint", False), ("rca", {}), ("disabled_routes", []),
    ("scenario_input_sha256", "0"*64), ("stop_hook_version", "old")])
def test_actual_binding_is_not_replaced_by_approval(field, value):
    record, _, _ = approved_fixture()
    actual = deepcopy(record["binding"])
    actual[field] = value
    with pytest.raises(c.CaptureDenied): verify(record, actual=actual)


def test_pinned_bytes_cannot_be_replaced_by_nonempty_reference():
    record, policies, pins = approved_fixture()
    raw = json.dumps({"record": record, "content_sha256": c.commitment(record)}).encode()
    pin = c.HumanPublicationPin("OFFLINE-FIXTURE-OWNER", record["reference"], 1, "0"*64, tuple(pins))
    with pytest.raises(c.CaptureDenied, match="publication hash"):
        c.verify_capture_approval(raw, pin, actual_binding=record["binding"], policy_records=policies,
            now=NOW, explicit_opt_in="AUTHORIZE CAPTURE " + p.child_binding("s5")["run_id"])


@pytest.mark.parametrize("failure", ["missing-policy", "changed-policy", "opt-in", "expired", "path", "model"])
def test_fail_closed_before_any_store(failure):
    record, policies, _ = approved_fixture()
    kwargs = {}
    if failure == "missing-policy": policies.pop("trace"); kwargs["policies_override"] = policies
    if failure == "changed-policy": policies["metric"] += b" "; kwargs["policies_override"] = policies
    if failure == "opt-in": kwargs["opt_in"] = "yes"
    if failure == "expired": kwargs["now"] = NOW + timedelta(days=1)
    if failure == "path": record["binding"]["child"]["stores"]["event"] = "var/knowledge.sqlite3"
    if failure == "model":
        actual = deepcopy(record["binding"]); actual["models"]["log"] = "0"*64; kwargs["actual"] = actual
    with pytest.raises(c.CaptureDenied): verify(record, **kwargs)
    assert not Path(p.child_binding("s5")["root"]).exists()


@pytest.mark.parametrize("field,value", [("max_cycles", 0), ("max_triggers", 2),
    ("max_captures", 2), ("max_seconds", float("inf")), ("io_timeout_seconds", 0),
    ("max_output_bytes", 1024**3)])
def test_finite_bounds_required(field, value):
    record, _, _ = approved_fixture(); record["limits"][field] = value
    with pytest.raises(c.CaptureDenied): verify(record)


def test_repeat_execution_does_not_clear_partial_intent():
    approved = verify()
    root = c._claim_new_child(approved)
    original = (root/"capture-intent.json").read_bytes()
    with pytest.raises(FileExistsError): c._claim_new_child(approved)
    assert (root/"capture-intent.json").read_bytes() == original


def test_production_entry_blocked_even_with_valid_synthetic_approval():
    approved = verify()
    with pytest.raises(c.CaptureDenied, match="CONTROLLER_SAFETY: BLOCKED"):
        c.execute_capture(approved)
    assert not Path(approved.facts["child"]["root"]).exists()


def test_internal_driver_is_not_a_capture_bypass():
    approved=verify()
    with pytest.raises(c.CaptureDenied,match="internal driver disabled"):
        c._drive_at_safe_boundaries(approved, object())
    assert not Path(approved.facts["child"]["root"]).exists()


def test_binding_dataclass_is_not_authorization_for_local_writes():
    valid=verify()
    forged=c.VerifiedCaptureBinding(valid.stop,valid.binding_json,valid.limits_json,valid.expires_at)
    with pytest.raises(c.CaptureDenied,match="not verified"):c._claim_new_child(forged)
    assert not Path(valid.facts["child"]["root"]).exists()


def test_verified_binding_cannot_be_replaced_while_reusing_proof():
    valid=verify()
    forged=replace(valid, limits_json='{"max_cycles":999999}')
    with pytest.raises(c.CaptureDenied,match="not verified"):c._claim_new_child(forged)


def test_source_manifest_covers_current_capture_dependencies():
    record,_,_=approved_fixture()
    record["binding"]["source_manifest"].pop("src/metrics_generator/metrics_generator.py")
    record["binding"]["source_manifest_sha256"]=c.commitment(record["binding"]["source_manifest"])
    with pytest.raises(c.CaptureDenied,match="coverage mismatch"):verify(record)


def test_readonly_manifest_observer_binds_inspector_and_exporter_without_stores(tmp_path):
    import yaml
    repository=tmp_path/"fixture-repository"
    for name in c.SOURCE_BOUND_FILES:
        target=repository/name;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text("# isolated source fixture\n")
    (repository/"configs").mkdir()
    actual_root=Path(__file__).parents[1]
    (repository/"configs/scenarios.yaml").write_bytes((actual_root/"configs/scenarios.yaml").read_bytes())
    (repository/"models").mkdir()
    for kind in ("log","metrics"):
        (repository/"models"/(kind+"_isolation_forest.pkl")).write_bytes(b"not a pickle; never deserialized")
    raw=yaml.safe_load((actual_root/"configs/runtime_orchestration.yaml").read_text(encoding="utf-8"))
    raw.pop("rca")
    raw["work_store"]["path"]=p.STORE_PATHS["work"]
    raw["authority_stores"]={name:p.STORE_PATHS[kind] for name,kind in (
        ("event_store_path","event"),("correlation_state_path","correlation"),
        ("incident_store_path","incident"),("shadow_store_path","shadow"))}
    config=tmp_path/"runtime-fixture.yaml";config.write_text(yaml.safe_dump(raw))
    observed=c.local_binding_observations(repository,config,"s5")
    assert set(observed["source_manifest"])==set(c.SOURCE_BOUND_FILES)
    assert observed["source_manifest_sha256"]==c.commitment(observed["source_manifest"])
    assert observed["source_revision"].startswith("PENDING")
    assert not Path(p.child_binding("s5")["root"]).exists()


def test_readiness_cli_cannot_execute_or_accept_opt_in(capsys):
    from scripts.prepare_demo_capture import main
    with pytest.raises(SystemExit):
        main(["--execute", "--live-opt-in"])


def test_mismatched_publication_or_insufficient_validity_rejected():
    record, _, _ = approved_fixture()
    record["expires_at"] = (NOW+timedelta(seconds=5)).isoformat()
    with pytest.raises(c.CaptureDenied, match="insufficient approval"):
        verify(record)


def components(tmp_path, monkeypatch, approved):
    from incident_evidence import EvidenceCaptureService, SqliteEvidenceStore, EvidenceSource, SourceStatus, load_evidence_policy
    from test_incident_evidence_capture import FakeAdapter
    c._claim_new_child(approved)
    event = _event("OFFLINE-CONTROLLER-EVENT", "brute_force_detected", NOW, source_ip="192.0.2.70")
    event["detection_method"] = "isolation_forest"
    runtime = _HostRuntime(tmp_path/"isolated-domain", [event])
    old = runtime.core
    core = CorrelationRuntimeCore(bootstrap=runtime.bootstrap, initial_dispatcher=runtime.pending,
        pending_scheduler=old._pending_scheduler, auto_assign=runtime.assignment,
        retry=runtime.retry, retry_executor=old._retry_executor, clock=runtime.clock,
        stop=old._stop, capture_stop_binding=approved.stop)
    policy = load_evidence_policy(Path(__file__).parents[1]/"configs/incident_evidence.yaml")
    store = SqliteEvidenceStore(tmp_path/"evidence.sqlite3")
    service = EvidenceCaptureService(store=store, incident_reader=runtime.incidents,
        event_reader=runtime.event_store, policy=policy,
        adapters={s: FakeAdapter(SourceStatus.AVAILABLE) for s in EvidenceSource})
    calls = []
    scenario = SimpleNamespace(start=lambda:calls.append("start"),
        trigger=lambda name:SimpleNamespace(accepted=True),
        tick=lambda:SimpleNamespace(phase=SimpleNamespace(value="BASELINE")),
        stop=lambda:calls.append("stop"))
    detection = SimpleNamespace(run_due_once=lambda:SimpleNamespace(failure_count=0))
    def command(incident_id, now):
        return c.CaptureCommand("OFFLINE-CAPTURE-ONLY", incident_id, now,
            policy.capture_contract_version, policy.canonicalization_version,
            policy.source_policy_version, policy.bounds_policy_version, service._effective_config_identity)
    def close(): store.close(); runtime.close()
    return c.CaptureComponents(scenario,detection,core,service,runtime.incidents,command,close), calls


def test_existing_capture_service_public_read_in_isolated_composition(tmp_path, monkeypatch):
    approved = verify(); parts, calls = components(tmp_path, monkeypatch, approved)
    result = drive_isolated_components(approved,parts,monotonic=lambda:1.0,
        utc_now=lambda:NOW+timedelta(seconds=120),output_bytes=lambda:0)
    assert result["status"] == "SUCCESS"
    assert result["snapshot_id"] and result["revision_id"]
    assert result["cycles"] == 1 and calls == ["start", "stop"]
    assert result["provenance"]["execution_binding_sha256"] == approved.stop.execution_binding_sha256


@pytest.mark.parametrize("failure", ["detection", "clock", "duration", "resource", "partial", "timeout"])
def test_safe_boundary_failure_preserves_partial_report(tmp_path, monkeypatch, failure):
    approved = verify(); parts, calls = components(tmp_path, monkeypatch, approved)
    clock = iter([1.0]*50)
    size = lambda:0
    if failure == "detection": parts.detection.run_due_once = lambda:SimpleNamespace(failure_count=1)
    if failure == "clock": clock = iter([1.0, 0.0])
    if failure == "duration": clock = iter([1.0, 50.0])
    if failure == "resource": size = lambda:2*1024*1024
    if failure in ("partial", "timeout"):
        def fail(*a,**kw): raise TimeoutError() if failure == "timeout" else RuntimeError()
        monkeypatch.setattr(parts.evidence,"capture_evidence",fail)
    result = drive_isolated_components(approved,parts,monotonic=lambda:next(clock),
        utc_now=lambda:NOW+timedelta(seconds=120),output_bytes=size)
    assert result["status"] == "STOPPED" and result["snapshot_id"] is None
    assert "failure_type" in result
    assert calls[-1] == "stop"
    report_path = Path(approved.facts["child"]["root"])/"capture-report.json"
    assert json.loads(report_path.read_text())["status"] == "STOPPED"


def test_persistence_ambiguity_is_not_reported_as_success(tmp_path, monkeypatch):
    approved = verify(); parts, _ = components(tmp_path, monkeypatch, approved)
    def fail(*args): raise OSError("synthetic write failure")
    monkeypatch.setattr(c, "_persist_report", fail)
    result = drive_isolated_components(approved,parts,monotonic=lambda:1.0,
        utc_now=lambda:NOW+timedelta(seconds=120),output_bytes=lambda:0)
    assert result["status"] == "STOPPED" and result["persistence_failure_type"] == "OSError"
    assert result["snapshot_id"]  # Preserve the real isolated committed reference; never retry.
