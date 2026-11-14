"""Capture authorization and safe-boundary composition; no import-time execution.

Historical competition execution stays disabled. December-only execution is
guarded by independent publication, local byte and Host clearance checks. A pin must
come from the Human-controlled publication process, never from the record being
validated. This module does not establish a new approval authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from threading import Event, Lock, Thread
from typing import Callable

from incident_evidence import CaptureCommand
from knowledge_index.identity import canonical_serialize
from . import capture_preparation as preparation
from .worker import CaptureStopBinding


CONTROLLER_VERSION = "competition-capture-controller-v1"
DECEMBER_CONTROLLER_VERSION = "december-one-shot-controller-v1"
STOP_HOOK_VERSION = "spec011-capture-first-failure-stop-v1"
DISABLED_ROUTES = ("rca", "knowledge", "query", "llm", "activation")
SOURCE_BOUND_FILES = tuple("src/runtime_orchestration/" + name for name in (
    "worker.py", "assignment.py", "pending.py", "cli.py", "capture_preparation.py",
    "capture_controller.py", "evidence_compatibility.py",
)) + ("src/metrics_generator/metrics_generator.py",)
DECEMBER_BOUND_FILES = SOURCE_BOUND_FILES + (
    "src/incident_evidence/demo_v2.py", "configs/incident_evidence.yaml",
    "configs/capture_december_demo.yaml", "configs/scenarios.yaml",
    "configs/event_runner.yaml", "configs/event_detection.yml", "configs/thresholds.yaml",
    "configs/metrics_iforest.yaml",
    "docs/spec/SPEC-011_Runtime_Orchestration_E2E.md",
    "docs/spec/SPEC-013_Incident_Evidence_Collection_Snapshot.md",
)
MODEL_SHA256 = {
    "log": "2571e5c2f6708f9e50ce41272bc0ae00f3931a783c015ed9aaaf121cd481b92d",
    "metrics": "f0180f846f159f8fc38bdbac0b350db58d4200b9b0ebf511dd3a8c76ba41bb0b",
}
# Historical Option A blockers; not assertions about December Option B.
EXECUTION_BLOCKERS = (
    "OWNED_EXPORTER_CLEANUP_AND_RUN_BINDING_REQUIRED",
    "LOCAL_FILE_AND_SQLITE_IO_HARD_BOUND_NOT_ESTABLISHED",
    "STRUCTURED_TRACE_ADAPTER_AMENDMENT_PENDING",
    "MEMBERSHIP_SERVICE_SELECTOR_APPROVAL_PENDING",
    "GLOBAL_METRIC_CONTEXT_APPROVAL_PENDING",
)


class CaptureDenied(ValueError):
    pass


class _AuthorizationNotCurrent(CaptureDenied):
    def __init__(self, kind: str):
        super().__init__(kind + " authorization is not currently valid")
        self.kind = kind


_VERIFICATION_SEAL = object()


def _digest(value: object, size: int = 64) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{%d}" % size, value) is None:
        raise CaptureDenied("invalid commitment")


def commitment(value: object) -> str:
    return sha256(canonical_serialize(value).encode("utf-8")).hexdigest()


def _time(value: str | datetime) -> datetime:
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        raise CaptureDenied("timezone-aware time required")
    return stamp.astimezone(timezone.utc)


@dataclass(frozen=True)
class HumanPublicationPin:
    """Out-of-band Owner publication facts; cannot be read from untrusted JSON."""
    issuer_identity: str
    reference: str
    revision: int
    record_sha256: str
    policy_record_sha256: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _PublicationValidity:
    """Time facts extracted only after the exact pinned record was validated."""
    kind: str
    record_sha256: str
    identity: str
    valid_from: datetime
    expires_at: datetime


@dataclass(frozen=True)
class VerifiedCaptureBinding:
    stop: CaptureStopBinding
    binding_json: str
    limits_json: str
    expires_at: datetime
    publication_validity: tuple[_PublicationValidity, ...] = ()
    grant_valid_from: datetime | None = None
    _verification_seal: object = field(default=None, repr=False, compare=False)

    @property
    def facts(self) -> dict:
        return json.loads(self.binding_json)

    @property
    def limits(self) -> dict:
        return json.loads(self.limits_json)


@dataclass(frozen=True)
class _VerifiedCaptureProof:
    seal: object
    stop: CaptureStopBinding
    binding_json: str
    limits_json: str
    expires_at: datetime
    publication_validity: tuple[_PublicationValidity, ...]
    grant_valid_from: datetime


def verify_capture_approval(
    raw_record: bytes, publication: HumanPublicationPin, *, actual_binding: dict,
    policy_records: dict[str, bytes], now: datetime, explicit_opt_in: str,
) -> VerifiedCaptureBinding:
    """Pure guard: no stores, providers, directory creation or credential access.

    actual_binding must be assembled from current read-only observations by the
    controlled caller. A Human checkpoint attestation does not replace byte hashes.
    Policy records have independent Owner-published pins, not just reference names.
    """
    if not isinstance(publication, HumanPublicationPin):
        raise CaptureDenied("Human-controlled publication pin required")
    if not isinstance(raw_record, bytes) or not 0 < len(raw_record) <= 65536:
        raise CaptureDenied("approval record exceeds bounded input")
    _digest(publication.record_sha256)
    if sha256(raw_record).hexdigest() != publication.record_sha256:
        raise CaptureDenied("approval publication hash mismatch")
    try:
        envelope = json.loads(raw_record)
        if set(envelope) != {"record", "content_sha256"}:
            raise CaptureDenied("approval envelope mismatch")
        record = envelope["record"]
        if envelope["content_sha256"] != commitment(record):
            raise CaptureDenied("approval content commitment mismatch")
        if set(record) != {"schema_version", "decision", "authority", "issuer_identity",
                           "issuer_role", "reference", "revision", "valid_from",
                           "expires_at", "binding", "limits"}:
            raise CaptureDenied("approval fields mismatch")
        december = record["binding"].get("capture_profile") == preparation.DECEMBER_PROFILE
        authority = preparation.DECEMBER_AUTHORITY if december else preparation.AUTHORITY
        if (record["schema_version"] != 1 or type(record["schema_version"]) is not int
                or record["decision"] != "APPROVE_CAPTURE"
                or record["authority"] != authority
                or record["issuer_role"] != "SPEC_LEAD_DEMO_HOST_OWNER"
                or not publication.issuer_identity or not publication.reference
                or type(publication.revision) is not int or publication.revision < 1
                or type(record["revision"]) is not int
                or (record["issuer_identity"], record["reference"], record["revision"])
                != (publication.issuer_identity, publication.reference, publication.revision)):
            raise CaptureDenied("approval issuer, authority or decision mismatch")
        current = _time(now)
        if not _time(record["valid_from"]) <= current < _time(record["expires_at"]):
            raise CaptureDenied("approval is not currently valid")
        facts = record["binding"]
        if facts != actual_binding:
            raise CaptureDenied("actual execution binding differs from approval")
        case = facts["case"]
        profile = facts.get("december_profile") if december else None
        if december:
            if (not isinstance(profile, dict) or profile.get("identity") != preparation.DECEMBER_POLICY
                    or profile.get("scope") != "DECEMBER_DEMO_ONLY"
                    or profile.get("status") != "PREPARATION_ONLY"
                    or publication.reference == preparation.DECEMBER_AUTHORITY):
                raise CaptureDenied("December scope is not a Final Capture Grant")
        expected = preparation.child_binding(case, december=profile)
        if (facts["parent_run"] != (profile["parent_run"] if december else preparation.PARENT_RUN) or facts["child"] != expected
                or facts["scenario_config_sha256"] != preparation.SCENARIO_SHA
                or facts["scenario_input_sha256"] != preparation.INPUT_SHA[case]
                or facts["controller_version"] != (DECEMBER_CONTROLLER_VERSION if december else CONTROLLER_VERSION)
                or facts["stop_hook_version"] != STOP_HOOK_VERSION
                or facts["disabled_routes"] != list(DISABLED_ROUTES)
                or facts["rca"] is not None or facts["clean_checkpoint"] is not True
                or facts["mount_mode"] != "ro"
                or facts["log_mount_source"] != str(Path(expected["log_output"]).parent)
                or explicit_opt_in != "AUTHORIZE CAPTURE " + expected["run_id"]):
            raise CaptureDenied("run, isolation, source or opt-in boundary mismatch")
        _digest(facts["source_revision"], 40)
        for name in ("runtime_config_sha256", "effective_config_sha256",
                     "source_manifest_sha256", "observability_deployment_sha256"):
            _digest(facts[name])
        source_manifest = facts["source_manifest"]
        if not isinstance(source_manifest, dict) or set(source_manifest) != set(DECEMBER_BOUND_FILES if december else SOURCE_BOUND_FILES):
            raise CaptureDenied("source manifest coverage mismatch")
        for digest in source_manifest.values():
            _digest(digest)
        if commitment(source_manifest) != facts["source_manifest_sha256"]:
            raise CaptureDenied("source manifest content mismatch")
        for model in ("log", "metrics"):
            _digest(facts["models"][model])
        if facts["models"] != MODEL_SHA256:
            raise CaptureDenied("approved model artifact identity mismatch")
        pins = dict(publication.policy_record_sha256)
        kinds = {"selector", "trace", "metric", "clearance"} if december else {"selector", "trace", "metric"}
        if (len(pins) != len(kinds) or len(publication.policy_record_sha256) != len(kinds)
                or set(pins) != kinds
                or set(policy_records) != set(pins)):
            raise CaptureDenied("independent policy publications required")
        validity = []
        for kind, digest in pins.items():
            _digest(digest)
            data = policy_records[kind]
            if not isinstance(data, bytes) or not 0 < len(data) <= 32768:
                raise CaptureDenied("policy record exceeds bounded input")
            if sha256(data).hexdigest() != digest:
                raise CaptureDenied("policy publication hash mismatch")
            policy = json.loads(data)
            if (policy["decision"] != "APPROVE" or policy["issuer_role"] != "SPEC_LEAD"
                    or policy["authority"] != authority
                    or policy["kind"] != kind or not policy["issuer_identity"]
                    or policy["identity"] != facts["policies"][kind]["identity"]
                    or digest != facts["policies"][kind]["record_sha256"]
                    or policy["semantic_commitment"] != facts["policies"][kind]["semantic_commitment"]
                    or not _time(policy["valid_from"]) <= current < _time(policy["expires_at"])):
                raise CaptureDenied("policy approval binding mismatch")
            _digest(policy["semantic_commitment"])
            if december and kind != "clearance" and policy["identity"] != {
                    "selector":"BoundedServiceOrV1", "trace":"loki-demo-bounded-json-trace-v2",
                    "metric":"GlobalLatencyContextV1"}[kind]:
                raise CaptureDenied("unknown December evidence policy")
            if december and kind == "clearance":
                if (policy.get("run_id") != expected["run_id"]
                        or policy.get("execution_binding_sha256") != commitment({k:v for k,v in facts.items() if k != "policies"})
                        or policy.get("predecessor_state") != "HUMAN_CONFIRMED_QUIESCENT_AND_RECONCILED"):
                    raise CaptureDenied("Host Owner predecessor/writer clearance required")
            validity.append(_PublicationValidity(kind, digest, policy["identity"],
                                                  _time(policy["valid_from"]), _time(policy["expires_at"])))
        limits = record["limits"]
        if set(limits) != {"max_triggers", "max_cycles", "max_seconds", "io_timeout_seconds",
                           "max_output_bytes", "max_captures"}:
            raise CaptureDenied("explicit resource limits required")
        for name, ceiling in (("max_triggers", 1), ("max_captures", 1),
                              ("max_cycles", 600), ("max_output_bytes", 8 * 1024 * 1024)):
            if type(limits[name]) is not int or not 1 <= limits[name] <= ceiling:
                raise CaptureDenied("invalid finite attempt/resource bound")
        for name, ceiling in (("max_seconds", 600), ("io_timeout_seconds", 5)):
            if (type(limits[name]) not in (int, float) or not math.isfinite(limits[name])
                    or not 0 < limits[name] <= ceiling):
                raise CaptureDenied("invalid finite duration bound")
        if december and limits["io_timeout_seconds"] != 5:
            raise CaptureDenied("current detector/SQLite I/O configuration requires explicit five-second per-operation bounds")
        if _time(record["expires_at"]) - current < timedelta(seconds=limits["max_seconds"]):
            raise CaptureDenied("insufficient approval validity for bounded run")
        stop = CaptureStopBinding(expected["run_id"], facts["source_revision"],
                                  facts["runtime_config_sha256"], commitment(facts),
                                  publication.reference)
        binding_json, limits_json = canonical_serialize(facts), canonical_serialize(limits)
        expires = _time(record["expires_at"])
        publications = tuple(sorted(validity, key=lambda value:value.kind))
        grant_start = _time(record["valid_from"])
        proof = _VerifiedCaptureProof(_VERIFICATION_SEAL, stop, binding_json, limits_json, expires,
                                      publications, grant_start)
        return VerifiedCaptureBinding(stop, binding_json, limits_json, expires,
                                      publications, grant_start, proof)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, CaptureDenied):
            raise
        raise CaptureDenied("malformed or incomplete capture approval") from exc


def execute_capture(verified, *, repository=None, base_config_path=None, demo_policy=None):
    """December-only owned execution. No components/factory injection interface.

    A timeout is a bounded WAIT, never cancellation of admitted authoritative I/O.
    No final grant or Host clearance is issued by this function.
    """
    _require_verified(verified)
    if verified.facts.get("capture_profile") != preparation.DECEMBER_PROFILE:
        raise CaptureDenied("CONTROLLER_SAFETY: BLOCKED; historical execution remains disabled")
    from .config import load_runtime_config
    from .clock import RuntimeClock
    from incident_evidence.demo_v2 import DemoPolicyBinding, DECEMBER_SCOPE
    if repository is None or base_config_path is None or not isinstance(demo_policy, DemoPolicyBinding):
        raise CaptureDenied("local source/config and independently published December policy required")
    repository, base_config_path = Path(repository).resolve(), Path(base_config_path).resolve()
    clock = RuntimeClock.system()
    _require_current_authorizations(verified, demo_policy, clock.now())
    facts = verified.facts
    if (clock.now() >= verified.expires_at or demo_policy.record["scope"] != DECEMBER_SCOPE
            or demo_policy.record["source_revision"] != facts["source_revision"]
            or demo_policy.record_sha256 != facts.get("d3_policy_sha256")):
        raise CaptureDenied("December policy/source/validity mismatch")
    for kind in ("loki", "prometheus"):
        endpoint = facts.get("endpoints", {}).get(kind)
        from .config import _local_endpoint
        try:
            _local_endpoint(endpoint, kind)
        except ValueError as exc:
            raise CaptureDenied("only bound local non-secret endpoints permitted") from exc
        if sha256(endpoint.encode()).hexdigest() != demo_policy.record[kind + "_endpoint_sha256"]:
            raise CaptureDenied("D3 deployment endpoint mismatch")
    base = load_runtime_config(base_config_path)
    child, bundle = preparation.december_config_bundle(repository, base, facts["case"])
    hashes = {name:sha256(data).hexdigest() for name,data in bundle.items()}
    if (facts["december_profile"] != preparation.december_profile(repository)
            or facts["child"] != child or facts.get("configuration_files") != hashes
            or facts["runtime_config_sha256"] != hashes["configuration/runtime.yaml"]
            or facts["effective_config_sha256"] != commitment(hashes)):
        raise CaptureDenied("current derived config differs from approved bytes")
    for name, digest in facts["source_manifest"].items():
        if sha256((repository / name).read_bytes()).hexdigest() != digest:
            raise CaptureDenied("source byte manifest mismatch")
    for kind, filename in (("log", "log_isolation_forest.pkl"), ("metrics", "metrics_isolation_forest.pkl")):
        if sha256((repository / "models" / filename).read_bytes()).hexdigest() != facts["models"][kind]:
            raise CaptureDenied("local model byte mismatch")
    # Human publication pins establish writer/deployment trust; filesystem bytes
    # establish local config trust. Neither stands in for the other.
    global _active_session
    with _session_lock:
        if _active_session is not None:
            old = _active_session
            if old.thread.is_alive() or old.report.get("status") != "SUCCESS" or not old.closed:
                raise CaptureDenied("previous execution/cleanup/receipts require reconciliation")
        # Local byte/config observations can themselves take time.
        _require_current_authorizations(verified, demo_policy, clock.now())
        session = _CaptureSession(verified, clock, demo_policy)
        session.thread = Thread(target=_run_owned, args=(session, repository, bundle, demo_policy),
                                name="capture-" + verified.stop.run_id, daemon=False)
        _active_session = session
        try:
            session.thread.start()
        except BaseException:
            session.report["status"] = "UNKNOWN"
            raise
    remaining = max(0.0, session.deadline - clock.monotonic())
    if not session.done.wait(remaining):
        session.stop.set()
        if session.components is not None:
            session.components.core._stop.request_stop()
        return {"run_id": verified.stop.run_id, "status": "TIMEOUT_UNCONFIRMED",
                "deadline_exceeded": True, "reconciliation_required": True}
    try:
        session._check_authorizations()
    except _AuthorizationNotCurrent:
        pass
    if clock.monotonic() >= session.deadline:
        session.report.update(status="TIMEOUT_UNCONFIRMED", deadline_exceeded=True)
    return dict(session.report)


_session_lock = Lock()
_active_session = None


class _CaptureSession:
    def __init__(self, verified, clock, demo_policy):
        self.verified, self.clock = verified, clock
        self.demo_policy = demo_policy
        self.deadline = clock.monotonic() + verified.limits["max_seconds"]
        self.stop, self.done = Event(), Event()
        self.components = None
        self.owned_resources = []  # retained even if factory/cleanup raises before returning components
        self.closed = False
        self.report = {"run_id": verified.stop.run_id, "status": "UNKNOWN",
                       "deadline_exceeded": False, "authorization_not_current": False, "cycles": 0,
                       "execution_binding_sha256": verified.stop.execution_binding_sha256}

    def boundary(self):
        self._check_authorizations()
        if self.clock.monotonic() >= self.deadline:
            self._stop_new_work()
            self.report["deadline_exceeded"] = True
            raise CaptureDenied("deadline exceeded; no new work admitted")
        if self.stop.is_set():
            raise CaptureDenied("stop requested; no new work admitted")
        if self.components is not None:
            core = self.components.core
            if core.capture_failure is not None or core._stop.requested:
                raise CaptureDenied("first observable runtime failure/stop")
        root = Path(self.verified.facts["child"]["root"])
        if root.exists() and sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) > self.verified.limits["max_output_bytes"]:
            raise CaptureDenied("output bound exceeded; no new work admitted")

    def _stop_new_work(self):
        self.stop.set()
        if self.components is not None:
            self.components.core._stop.request_stop()

    def _check_authorizations(self):
        try:
            _require_current_authorizations(self.verified, self.demo_policy, self.clock.now())
        except _AuthorizationNotCurrent as exc:
            self._stop_new_work()
            self.report.update(status="AUTHORIZATION_NOT_CURRENT", authorization_not_current=True,
                               authorization_kind=exc.kind)
            raise


def _run_owned(session, repository, bundle, demo_policy):
    """All Store creation/use/close and report I/O stay on this owned thread."""
    from .cli import _build_december_capture_components
    from incident_evidence import CaptureTerminalKind
    v, report = session.verified, session.report
    claimed = False
    try:
        session.boundary()
        root = _claim_new_child(v)
        claimed = True
        for name, data in bundle.items():
            session.boundary()
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if path.read_bytes() != data:
                    raise CaptureDenied("preconfigured bytes changed")
            else:
                with path.open("xb") as handle: handle.write(data)
        session.boundary()
        session.components = c = _build_december_capture_components(v, repository, demo_policy, session.clock,
                                                                   owned_resources=session.owned_resources)
        session.boundary()
        c.core.startup()  # existing recovery barrier; an authorized write, not read-only preflight
        session.boundary()
        c.scenario.start()
        session.boundary()
        if not c.scenario.trigger(v.facts["case"].upper()).accepted:
            raise CaptureDenied("one-shot trigger refused")
        for cycle in range(v.limits["max_cycles"]):
            session.boundary()
            phase = c.scenario.tick()
            session.boundary()
            if c.detection.run_due_once().failure_count:
                raise CaptureDenied("detector failure; no further intake")
            session.boundary()
            c.core.run_cycle()
            report["cycles"] = cycle + 1
            session.boundary()
            if getattr(phase.phase, "value", phase.phase) == "BASELINE":
                views = c.incident_reader.list_correlation_views()
                session.boundary()
                if len(views) != 1:
                    raise CaptureDenied("one authoritative Incident required")
                command = c.command_for(views[0].incident_id, session.clock.now())
                session.boundary()
                # One admitted synchronous command may finish its fixed atomic
                # internals after deadline. No extra command/fetch/retry is issued.
                outcome = c.evidence.capture_evidence(command)
                report.update(capture_operation_id=command.capture_operation_id,
                              terminal_kind=outcome.terminal_kind.value,
                              snapshot_id=outcome.snapshot_id, revision_id=outcome.revision_id)
                session.boundary()
                if outcome.terminal_kind is not CaptureTerminalKind.SUCCESS:
                    report["status"] = "CONFIRMED_FAILURE"
                    raise CaptureDenied("capture did not reach authoritative success")
                snapshot = c.evidence.resolve_snapshot(outcome.snapshot_id)
                session.boundary()
                revision = c.evidence.resolve_revision(outcome.revision_id)
                session.boundary()
                if (snapshot is None or revision is None or snapshot.revision_id != revision.revision_id
                        or snapshot.snapshot_id != outcome.snapshot_id or revision.revision_id != outcome.revision_id
                        or outcome.capture_operation_id != command.capture_operation_id
                        or snapshot.capture_operation_id != command.capture_operation_id
                        or snapshot.config_identity != command.config_identity
                        or snapshot.incident_id != command.incident_id or revision.incident_id != command.incident_id):
                    raise CaptureDenied("public Snapshot/Revision mismatch")
                report["status"] = "SUCCESS"
                break
            session.stop.wait(min(1.0, max(0, session.deadline-session.clock.monotonic())))
        else:
            raise CaptureDenied("cycle bound exhausted")
    except BaseException as exc:
        if report["status"] not in {"CONFIRMED_FAILURE", "AUTHORIZATION_NOT_CURRENT"}:
            report["status"] = "UNKNOWN"
        report["failure_type"] = type(exc).__name__
        if session.components is not None and session.components.core.capture_failure is not None:
            failure = session.components.core.capture_failure
            report["first_runtime_failure"] = {"stage":failure.stage,"error_type":failure.error_type}
    finally:
        if session.components is not None:
            try:
                session.components.core._stop.request_stop()
                session.components.scenario.stop()
                session.components.close()
                session.closed = True
            except BaseException as exc:
                report.update(status="UNKNOWN", cleanup_failure_type=type(exc).__name__)
        # Cleanup/report I/O may complete late. Never rewrite timeout as SUCCESS.
        try:
            session._check_authorizations()
        except _AuthorizationNotCurrent:
            pass
        if session.clock.monotonic() >= session.deadline:
            report.update(status="TIMEOUT_UNCONFIRMED", deadline_exceeded=True)
        if claimed:
            try:
                diagnostic = dict(report)
                if diagnostic["status"] == "SUCCESS":
                    diagnostic["status"] = "DOMAIN_READBACK_CONFIRMED"
                diagnostic["controller_return_status"] = "NOT_A_DOMAIN_AUTHORITY"
                _persist_report(v, diagnostic)
            except BaseException as exc:
                report.update(status="UNKNOWN", report_failure_type=type(exc).__name__)
        try:
            session._check_authorizations()
        except _AuthorizationNotCurrent:
            pass
        if session.clock.monotonic() >= session.deadline:
            report.update(status="TIMEOUT_UNCONFIRMED", deadline_exceeded=True)
        session.done.set()


def local_binding_observations(repository: Path, runtime_config_path: Path, case: str) -> dict:
    """Read-only byte evidence, never a clean-source or deployment attestation."""
    from .config import load_runtime_config
    proposal = preparation.prepare_capture(repository, load_runtime_config(runtime_config_path))
    child = preparation.child_binding(case)
    preparation.validate_capture_boundary(load_runtime_config(runtime_config_path), child)
    source_manifest = {
        name: sha256((repository / name).read_bytes()).hexdigest()
        for name in SOURCE_BOUND_FILES
    }
    return {
        "case": case, "parent_run": preparation.PARENT_RUN, "child": child,
        "scenario_config_sha256": preparation.SCENARIO_SHA,
        "scenario_input_sha256": preparation.INPUT_SHA[case],
        "runtime_config_sha256": sha256(runtime_config_path.read_bytes()).hexdigest(),
        "source_manifest": source_manifest, "source_manifest_sha256": commitment(source_manifest),
        "models": {kind: value["sha256"] for kind, value in proposal["models"].items()},
        "controller_version": CONTROLLER_VERSION, "stop_hook_version": STOP_HOOK_VERSION,
        "disabled_routes": list(DISABLED_ROUTES), "rca": None,
        "source_revision": "PENDING HUMAN CLEAN SOURCE CHECKPOINT",
        "effective_config_sha256": "PENDING POLICY APPROVALS",
        "observability_deployment_sha256": "PENDING HUMAN DEPLOYMENT EVIDENCE",
    }


def december_binding_observations(repository: Path, base_config_path: Path, case: str) -> dict:
    """Pure local byte observations. Missing human/deployment facts stay PENDING.

    This neither publishes approval nor creates child configuration/Stores.
    Final binding must add separately pinned policy/Host clearance publications.
    """
    from .config import load_runtime_config
    child, bundle = preparation.december_config_bundle(repository, load_runtime_config(base_config_path), case)
    files = {name:sha256(data).hexdigest() for name,data in bundle.items()}
    sources = {name:sha256((repository/name).read_bytes()).hexdigest() for name in DECEMBER_BOUND_FILES}
    return {"capture_profile":preparation.DECEMBER_PROFILE,
        "december_profile":preparation.december_profile(repository),
        "case":case,"parent_run":preparation.december_profile(repository)["parent_run"],"child":child,
        "configuration_files":files,"runtime_config_sha256":files["configuration/runtime.yaml"],
        "effective_config_sha256":commitment(files),"source_manifest":sources,
        "source_manifest_sha256":commitment(sources),
        "models":{kind:sha256((repository/"models"/filename).read_bytes()).hexdigest() for kind,filename in (
            ("log","log_isolation_forest.pkl"),("metrics","metrics_isolation_forest.pkl"))},
        "scenario_config_sha256":preparation.SCENARIO_SHA,"scenario_input_sha256":preparation.INPUT_SHA[case],
        "controller_version":DECEMBER_CONTROLLER_VERSION,"stop_hook_version":STOP_HOOK_VERSION,
        "disabled_routes":list(DISABLED_ROUTES),"rca":None,
        "source_revision":"PENDING HUMAN CLEAN SOURCE CHECKPOINT",
        "clean_checkpoint":False,"mount_mode":"ro", "log_mount_source":str(Path(child["log_output"]).parent),
        "observability_deployment_sha256":"PENDING HUMAN DEPLOYMENT EVIDENCE",
        "d3_policy_sha256":"PENDING INDEPENDENT POLICY PUBLICATION",
        "endpoints":{},"policies":{}}



def _claim_new_child(verified: VerifiedCaptureBinding) -> Path:
    """One-shot local intent; never used by blocked production entry.

    Existing child directories (including partial attempts) are never resumed,
    overwritten or cleared. Only a verified fixed root can be used.
    """
    _require_verified(verified)
    child = verified.facts["child"]
    december = verified.facts.get("capture_profile") == preparation.DECEMBER_PROFILE
    if child != preparation.child_binding(verified.facts["case"], december=verified.facts.get("december_profile") if december else None):
        raise CaptureDenied("child isolation mismatch")
    root = Path(child["root"])
    if root.resolve() != root.absolute():
        raise CaptureDenied("reparse/alias isolation is not established")
    if december and root.exists():
        allowed = verified.facts["configuration_files"]
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or path.resolve() != path.absolute():
                raise CaptureDenied("preconfigured alias/reparse rejected")
            if path.is_dir():
                if relative not in {"configuration", "observations"}:
                    raise CaptureDenied("unknown preconfigured directory")
            elif (path.stat().st_nlink != 1 or relative not in allowed
                    or sha256(path.read_bytes()).hexdigest() != allowed[relative]):
                raise CaptureDenied("old Store/Intent/Report or unknown preconfigured bytes")
    else:
        root.mkdir(parents=True, exist_ok=False)
    with (root / "capture-intent.json").open("x", encoding="utf-8") as handle:
        handle.write(canonical_serialize({"execution_binding_sha256": verified.stop.execution_binding_sha256,
                                         "run_id": verified.stop.run_id}))
    if json.loads((root / "capture-intent.json").read_text(encoding="utf-8")) != {
            "execution_binding_sha256": verified.stop.execution_binding_sha256,
            "run_id": verified.stop.run_id}:
        raise CaptureDenied("atomic intent readback mismatch; no domain work")
    return root


def _persist_report(verified: VerifiedCaptureBinding, report: dict) -> None:
    _require_verified(verified)
    root = Path(verified.facts["child"]["root"])
    intent = json.loads((root / "capture-intent.json").read_text(encoding="utf-8"))
    if intent != {"execution_binding_sha256": verified.stop.execution_binding_sha256,
                  "run_id": verified.stop.run_id}:
        raise CaptureDenied("partial report has no matching one-shot intent")
    with (root / "capture-report.json").open("x", encoding="utf-8") as handle:
        handle.write(canonical_serialize(report))


def _require_verified(verified: VerifiedCaptureBinding) -> None:
    if not isinstance(verified, VerifiedCaptureBinding):
        raise CaptureDenied("capture approval was not verified")
    proof = verified._verification_seal
    if (not isinstance(proof, _VerifiedCaptureProof) or proof.seal is not _VERIFICATION_SEAL
            or (verified.stop, verified.binding_json, verified.limits_json, verified.expires_at,
                verified.publication_validity, verified.grant_valid_from)
            != (proof.stop, proof.binding_json, proof.limits_json, proof.expires_at,
                proof.publication_validity, proof.grant_valid_from)):
        raise CaptureDenied("capture approval was not verified")


def _require_current_authorizations(verified, demo_policy, now):
    """Pure admission check of sealed facts; no pin replacement or renewal."""
    _require_verified(verified)
    current = _time(now)
    if not verified.grant_valid_from <= current < verified.expires_at:
        raise _AuthorizationNotCurrent("final_grant")
    try:
        demo_policy.require_current(current)
    except ValueError as exc:
        raise _AuthorizationNotCurrent("d3_policy") from exc
    for publication in verified.publication_validity:
        if not publication.valid_from <= current < publication.expires_at:
            raise _AuthorizationNotCurrent(publication.kind)


@dataclass
class CaptureComponents:
    """Owned existing components; only the guarded entry constructs production handles."""
    scenario: object
    detection: object
    core: object
    evidence: object
    incident_reader: object
    command_for: Callable[[str, datetime], CaptureCommand]
    close: Callable[[], None]


def _drive_at_safe_boundaries(*args, **kwargs) -> None:
    """Not an execution entry; fixtures must not provide a production bypass."""
    raise CaptureDenied("internal driver disabled; execute_capture safety gates apply")
