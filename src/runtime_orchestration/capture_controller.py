"""Capture authorization and safe-boundary composition; no import-time execution.

There is deliberately no production execute CLI while the explicit safety and
SPEC-013 compatibility gates below remain unresolved. A publication pin must
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
from typing import Callable

from incident_evidence import CaptureCommand
from knowledge_index.identity import canonical_serialize
from . import capture_preparation as preparation
from .worker import CaptureStopBinding


CONTROLLER_VERSION = "competition-capture-controller-v1"
STOP_HOOK_VERSION = "spec011-capture-first-failure-stop-v1"
DISABLED_ROUTES = ("rca", "knowledge", "query", "llm", "activation")
SOURCE_BOUND_FILES = tuple("src/runtime_orchestration/" + name for name in (
    "worker.py", "assignment.py", "pending.py", "cli.py", "capture_preparation.py",
    "capture_controller.py", "evidence_compatibility.py",
)) + ("src/metrics_generator/metrics_generator.py",)
MODEL_SHA256 = {
    "log": "2571e5c2f6708f9e50ce41272bc0ae00f3931a783c015ed9aaaf121cd481b92d",
    "metrics": "f0180f846f159f8fc38bdbac0b350db58d4200b9b0ebf511dd3a8c76ba41bb0b",
}
# Source-supported facts, not caller-supplied assurance flags.
EXECUTION_BLOCKERS = (
    "OWNED_EXPORTER_CLEANUP_AND_RUN_BINDING_REQUIRED",
    "LOCAL_FILE_AND_SQLITE_IO_HARD_BOUND_NOT_ESTABLISHED",
    "STRUCTURED_TRACE_ADAPTER_AMENDMENT_PENDING",
    "MEMBERSHIP_SERVICE_SELECTOR_APPROVAL_PENDING",
    "GLOBAL_METRIC_CONTEXT_APPROVAL_PENDING",
)


class CaptureDenied(ValueError):
    pass


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
class VerifiedCaptureBinding:
    stop: CaptureStopBinding
    binding_json: str
    limits_json: str
    expires_at: datetime
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
        if (record["schema_version"] != 1 or type(record["schema_version"]) is not int
                or record["decision"] != "APPROVE_CAPTURE"
                or record["authority"] != preparation.AUTHORITY
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
        expected = preparation.child_binding(case)
        if (facts["parent_run"] != preparation.PARENT_RUN or facts["child"] != expected
                or facts["scenario_config_sha256"] != preparation.SCENARIO_SHA
                or facts["scenario_input_sha256"] != preparation.INPUT_SHA[case]
                or facts["controller_version"] != CONTROLLER_VERSION
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
        if not isinstance(source_manifest, dict) or set(source_manifest) != set(SOURCE_BOUND_FILES):
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
        if (len(pins) != 3 or len(publication.policy_record_sha256) != 3
                or set(pins) != {"selector", "trace", "metric"}
                or set(policy_records) != set(pins)):
            raise CaptureDenied("independent policy publications required")
        for kind, digest in pins.items():
            _digest(digest)
            data = policy_records[kind]
            if not isinstance(data, bytes) or not 0 < len(data) <= 32768:
                raise CaptureDenied("policy record exceeds bounded input")
            if sha256(data).hexdigest() != digest:
                raise CaptureDenied("policy publication hash mismatch")
            policy = json.loads(data)
            if (policy["decision"] != "APPROVE" or policy["issuer_role"] != "SPEC_LEAD"
                    or policy["authority"] != preparation.AUTHORITY
                    or policy["kind"] != kind or not policy["issuer_identity"]
                    or policy["identity"] != facts["policies"][kind]["identity"]
                    or digest != facts["policies"][kind]["record_sha256"]
                    or policy["semantic_commitment"] != facts["policies"][kind]["semantic_commitment"]
                    or not _time(policy["valid_from"]) <= current < _time(policy["expires_at"])):
                raise CaptureDenied("policy approval binding mismatch")
            _digest(policy["semantic_commitment"])
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
        if _time(record["expires_at"]) - current < timedelta(seconds=limits["max_seconds"]):
            raise CaptureDenied("insufficient approval validity for bounded run")
        stop = CaptureStopBinding(expected["run_id"], facts["source_revision"],
                                  facts["runtime_config_sha256"], commitment(facts),
                                  publication.reference)
        binding_json, limits_json = canonical_serialize(facts), canonical_serialize(limits)
        expires = _time(record["expires_at"])
        proof = _VerifiedCaptureProof(_VERIFICATION_SEAL, stop, binding_json, limits_json, expires)
        return VerifiedCaptureBinding(stop, binding_json, limits_json, expires, proof)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, CaptureDenied):
            raise
        raise CaptureDenied("malformed or incomplete capture approval") from exc


def execute_capture(*args, **kwargs) -> None:
    """No production construction while independently confirmed gates are open.

    There is no caller flag to suppress these blockers. In particular, a signed
    or pinned assertion cannot make an unbounded I/O operation safely bounded.
    """
    raise CaptureDenied("CONTROLLER_SAFETY: BLOCKED; " + "; ".join(EXECUTION_BLOCKERS))


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


def _claim_new_child(verified: VerifiedCaptureBinding) -> Path:
    """One-shot local intent; never used by blocked production entry.

    Existing child directories (including partial attempts) are never resumed,
    overwritten or cleared. Only a verified fixed root can be used.
    """
    _require_verified(verified)
    child = verified.facts["child"]
    if child != preparation.child_binding(verified.facts["case"]):
        raise CaptureDenied("child isolation mismatch")
    root = Path(child["root"])
    if root.resolve() != root.absolute():
        raise CaptureDenied("reparse/alias isolation is not established")
    root.mkdir(parents=True, exist_ok=False)
    with (root / "capture-intent.json").open("x", encoding="utf-8") as handle:
        handle.write(canonical_serialize({"execution_binding_sha256": verified.stop.execution_binding_sha256,
                                         "run_id": verified.stop.run_id}))
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
            or (verified.stop, verified.binding_json, verified.limits_json, verified.expires_at)
            != (proof.stop, proof.binding_json, proof.limits_json, proof.expires_at)):
        raise CaptureDenied("capture approval was not verified")


@dataclass
class CaptureComponents:
    """Existing component handles, used by the isolated composition regression.

    No replacement domain algorithms or retry scheduler live in the controller.
    Production construction is intentionally withheld by execute_capture().
    """
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
