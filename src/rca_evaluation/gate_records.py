"""S5 facts recorded in the single Candidate-F evaluation ledger.

These helpers are deliberately narrow.  They validate and append evaluation
facts to ``SqliteEvaluationStore``; they do not attest that a real provider,
real RAG, or Docker gate executed.  R3 owns that provenance decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from .identity import canonical_bytes, commitment, require_identity
from .ledger import LedgerRecord
from .sqlite_store import EvaluationLedgerReader, SqliteEvaluationStore


GATES = frozenset({"REAL_LLM", "REAL_RAG", "DOCKER_RCA_E2E"})
UPSTREAM_ONLY_PROVENANCE = {
    "REAL_LLM": "CANDIDATE_D_PUBLIC_RESULT",
    "REAL_RAG": "CANDIDATE_C_PUBLIC_RETRIEVAL",
    "DOCKER_RCA_E2E": "SPEC011_PUBLIC_RUNTIME",
}


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty trimmed text")
    return value


def _sha(value: object, label: str) -> str:
    text = _text(value, label)
    if not text.startswith("sha256:") or len(text) != 71:
        raise ValueError(f"{label} must be a SHA-256 commitment")
    try:
        int(text[7:], 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be a SHA-256 commitment") from exc
    return text


def _timestamp(value: object, label: str) -> datetime:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def _gate(value: object) -> str:
    gate = _text(value, "gate")
    if gate not in GATES:
        raise ValueError("unsupported real gate")
    return gate


def _store(value: object) -> SqliteEvaluationStore:
    if type(value) is not SqliteEvaluationStore:
        raise TypeError("single Candidate-F SqliteEvaluationStore required")
    value.readiness_check()
    return value


def _reader(value: object) -> EvaluationLedgerReader:
    if type(value) is not EvaluationLedgerReader:
        raise TypeError("store-issued Candidate-F evaluation reader required")
    value.readiness_check()
    return value


def _payload(record: LedgerRecord, domain: str) -> dict:
    if record.domain != domain:
        raise ValueError(f"expected {domain} record")
    return json.loads(record.semantic_json)


@dataclass(frozen=True, slots=True)
class GateAuthorization:
    authorization_id: str
    authorization_commitment: str
    evaluation_run_id: str
    gate: str
    profile_identity: str
    profile_commitment: str
    authorization_source: str
    authorizing_actor: str
    issued_at: str


@dataclass(frozen=True, slots=True)
class GateExecutionReceipt:
    receipt_id: str
    receipt_commitment: str
    evaluation_run_id: str
    execution_id: str
    gate: str
    profile_identity: str
    profile_commitment: str
    harness_invocation_identity: str
    external_started_at: str
    external_ended_at: str
    upstream_result_reference: str
    upstream_result_commitment: str
    authoritative_typed_outcome: str
    sanitized_provenance: str


@dataclass(frozen=True, slots=True)
class DockerNamespace:
    namespace_id: str
    namespace_commitment: str
    evaluation_run_id: str
    execution_id: str
    profile_identity: str
    profile_commitment: str
    project_name: str
    volume_identities: tuple[str, ...]
    workspace_root: str
    issuance_nonce_commitment: str
    issued_at: str

    @property
    def issuance_id(self) -> str:
        return self.namespace_id

    @property
    def issuance_commitment(self) -> str:
        return self.namespace_commitment


@dataclass(frozen=True, slots=True)
class DockerCleanup:
    cleanup_id: str
    cleanup_commitment: str
    namespace_id: str
    project_name: str
    volume_identities: tuple[str, ...]
    workspace_root: str
    completed_at: str
    disposition: str
    invocation_start_id: str | None = None
    invocation_id: str | None = None
    invocation_started_at: str | None = None

    @property
    def outcome_id(self) -> str:
        return self.cleanup_id

    @property
    def issuance_id(self) -> str:
        return self.namespace_id


def record_gate_authorization(
    store: SqliteEvaluationStore, *, evaluation_run_id: str, gate: str,
    profile_identity: str, profile_commitment: str,
    authorization_source: str, authorizing_actor: str, issued_at: str,
) -> GateAuthorization:
    _store(store)
    require_identity(evaluation_run_id, "evaluation_run")
    gate = _gate(gate)
    profile_identity = _text(profile_identity, "profile identity")
    profile_commitment = _sha(profile_commitment, "profile commitment")
    payload = {
        "evaluation_run_id": evaluation_run_id,
        "gate": gate,
        "profile_identity": profile_identity,
        "profile_commitment": profile_commitment,
        "authorization_source": _text(authorization_source, "authorization source"),
        "authorizing_actor": _text(authorizing_actor, "authorizing actor"),
        "issued_at": _text(issued_at, "issued_at"),
    }
    _timestamp(payload["issued_at"], "issued_at")
    record = store.append(LedgerRecord.create(
        "gate_authorization",
        [evaluation_run_id, gate, profile_identity, profile_commitment],
        payload, evaluation_run_id,
    ))
    return GateAuthorization(record.record_id, record.semantic_commitment, **payload)


def read_gate_authorization(
    reader: EvaluationLedgerReader, authorization_id: str,
) -> GateAuthorization | None:
    _reader(reader)
    require_identity(authorization_id, "gate_authorization")
    record = reader.get(authorization_id)
    if record is None:
        return None
    payload = _payload(record, "gate_authorization")
    result = GateAuthorization(record.record_id, record.semantic_commitment, **payload)
    _gate(result.gate); _sha(result.profile_commitment, "profile commitment")
    _timestamp(result.issued_at, "issued_at")
    return result


def record_gate_execution_receipt(
    store: SqliteEvaluationStore, *, evaluation_run_id: str,
    execution_id: str, gate: str, profile_identity: str,
    profile_commitment: str, harness_invocation_identity: str,
    external_started_at: str, external_ended_at: str,
    upstream_result_reference: str, upstream_result_commitment: str,
    authoritative_typed_outcome: str, sanitized_provenance: str,
) -> GateExecutionReceipt:
    _store(store)
    require_identity(evaluation_run_id, "evaluation_run")
    require_identity(execution_id, "execution")
    gate = _gate(gate)
    expected_provenance = UPSTREAM_ONLY_PROVENANCE[gate]
    if sanitized_provenance != expected_provenance:
        raise ValueError("R1 receipt may record upstream public provenance only")
    profile_identity = _text(profile_identity, "profile identity")
    profile_commitment = _sha(profile_commitment, "profile commitment")
    started = _timestamp(external_started_at, "external_started_at")
    ended = _timestamp(external_ended_at, "external_ended_at")
    if ended < started:
        raise ValueError("receipt end precedes start")
    payload = {
        "evaluation_run_id": evaluation_run_id,
        "execution_id": execution_id,
        "gate": gate,
        "profile_identity": profile_identity,
        "profile_commitment": profile_commitment,
        "harness_invocation_identity": _text(
            harness_invocation_identity, "harness invocation identity"),
        "external_started_at": external_started_at,
        "external_ended_at": external_ended_at,
        "upstream_result_reference": _text(
            upstream_result_reference, "upstream result reference"),
        "upstream_result_commitment": _sha(
            upstream_result_commitment, "upstream result commitment"),
        "authoritative_typed_outcome": _text(
            authoritative_typed_outcome, "authoritative typed outcome"),
        "sanitized_provenance": sanitized_provenance,
    }
    record = store.append(LedgerRecord.create(
        "gate_execution_receipt",
        [execution_id, gate, profile_commitment], payload, execution_id,
    ))
    return GateExecutionReceipt(record.record_id, record.semantic_commitment, **payload)


def read_gate_execution_receipt(
    reader: EvaluationLedgerReader, receipt_id: str,
) -> GateExecutionReceipt | None:
    _reader(reader)
    require_identity(receipt_id, "gate_execution_receipt")
    record = reader.get(receipt_id)
    if record is None:
        return None
    payload = _payload(record, "gate_execution_receipt")
    result = GateExecutionReceipt(record.record_id, record.semantic_commitment, **payload)
    if result.sanitized_provenance != UPSTREAM_ONLY_PROVENANCE[_gate(result.gate)]:
        raise ValueError("receipt provenance is not an R1 upstream-only fact")
    return result


def record_docker_namespace(
    store: SqliteEvaluationStore, *, evaluation_run_id: str,
    execution_id: str, profile_identity: str, profile_commitment: str,
    project_prefix: str, volume_roles: tuple[str, ...], workspace_prefix: str,
    issuance_nonce: str, issued_at: str,
) -> DockerNamespace:
    _store(store)
    require_identity(evaluation_run_id, "evaluation_run")
    require_identity(execution_id, "execution")
    profile_identity = _text(profile_identity, "profile identity")
    profile_commitment = _sha(profile_commitment, "profile commitment")
    project_prefix = _text(project_prefix, "project prefix")
    workspace_prefix = _text(workspace_prefix, "workspace prefix")
    if not volume_roles or len(set(volume_roles)) != len(volume_roles):
        raise ValueError("Docker volume roles must be non-empty and unique")
    for role in volume_roles:
        _text(role, "volume role")
    nonce_commitment = commitment({"issuance_nonce": _text(
        issuance_nonce, "issuance nonce")})
    suffix = hashlib.sha256(canonical_bytes([
        evaluation_run_id, execution_id, profile_commitment,
        nonce_commitment])).hexdigest()[:16]
    project = f"{project_prefix}-{suffix}"
    volumes = tuple(f"{project}_{role}" for role in volume_roles)
    payload = {
        "evaluation_run_id": evaluation_run_id,
        "execution_id": execution_id,
        "profile_identity": profile_identity,
        "profile_commitment": profile_commitment,
        "project_name": project,
        "volume_identities": list(volumes),
        "workspace_root": f"{workspace_prefix.rstrip('/')}/{project}",
        "issuance_nonce_commitment": nonce_commitment,
        "issued_at": _text(issued_at, "issued_at"),
    }
    _timestamp(payload["issued_at"], "issued_at")
    record = store.append(LedgerRecord.create(
        "docker_namespace", [execution_id, profile_commitment], payload,
        execution_id,
    ))
    payload["volume_identities"] = volumes
    return DockerNamespace(record.record_id, record.semantic_commitment, **payload)


def read_docker_namespace(
    reader: EvaluationLedgerReader, namespace_id: str,
) -> DockerNamespace | None:
    _reader(reader)
    require_identity(namespace_id, "docker_namespace")
    record = reader.get(namespace_id)
    if record is None:
        return None
    payload = _payload(record, "docker_namespace")
    payload["volume_identities"] = tuple(payload["volume_identities"])
    result = DockerNamespace(record.record_id, record.semantic_commitment, **payload)
    _sha(result.profile_commitment, "profile commitment")
    _sha(result.issuance_nonce_commitment, "nonce commitment")
    _timestamp(result.issued_at, "issued_at")
    return result


def record_docker_cleanup(
    store: SqliteEvaluationStore, *, namespace_id: str,
    project_name: str, volume_identities: tuple[str, ...],
    workspace_root: str, completed_at: str, disposition: str,
    invocation_start_id: str | None = None,
    invocation_id: str | None = None,
    invocation_started_at: str | None = None,
) -> DockerCleanup:
    namespace = read_docker_namespace(store.reader(), namespace_id)
    if namespace is None:
        raise ValueError("durable Docker namespace is missing")
    if (project_name, volume_identities, workspace_root) != (
            namespace.project_name, namespace.volume_identities,
            namespace.workspace_root):
        raise ValueError("Docker cleanup target does not match durable namespace")
    completed = _timestamp(completed_at, "completed_at")
    invocation_fields = (
        invocation_start_id, invocation_id, invocation_started_at)
    if any(value is not None for value in invocation_fields):
        if any(value is None for value in invocation_fields):
            raise ValueError("Docker cleanup invocation binding is incomplete")
        require_identity(invocation_start_id, "gate_invocation_start")
        invocation_id = _text(invocation_id, "invocation identity")
        started = _timestamp(invocation_started_at, "invocation_started_at")
        start = store.reader().get(invocation_start_id)
        if start is None or start.domain != "gate_invocation_start":
            raise ValueError("Docker cleanup invocation start is missing")
        start_payload = json.loads(start.semantic_json)
        if (start_payload.get("gate") != "DOCKER_RCA_E2E"
                or start_payload.get("evaluation_run_id") !=
                    namespace.evaluation_run_id
                or start_payload.get("execution_id") != namespace.execution_id
                or start_payload.get("profile_identity") !=
                    namespace.profile_identity
                or start_payload.get("profile_commitment") !=
                    namespace.profile_commitment
                or start_payload.get("invocation_id") != invocation_id
                or start_payload.get("started_at") != invocation_started_at):
            raise ValueError(
                "Docker cleanup does not bind the exact invocation start")
        if completed < started:
            raise ValueError("Docker cleanup precedes its invocation start")
    payload = {
        "namespace_id": namespace_id,
        "project_name": _text(project_name, "project name"),
        "volume_identities": list(volume_identities),
        "workspace_root": _text(workspace_root, "workspace root"),
        "completed_at": _text(completed_at, "completed_at"),
        "disposition": _text(disposition, "cleanup disposition"),
    }
    if invocation_start_id is not None:
        payload.update({
            "invocation_start_id": invocation_start_id,
            "invocation_id": invocation_id,
            "invocation_started_at": invocation_started_at,
        })
    record = store.append(LedgerRecord.create(
        "docker_cleanup", [namespace_id, "cleanup"], payload, namespace_id,
    ))
    payload["volume_identities"] = volume_identities
    return DockerCleanup(record.record_id, record.semantic_commitment, **payload)


def read_docker_cleanup(
    reader: EvaluationLedgerReader, cleanup_id: str,
) -> DockerCleanup | None:
    _reader(reader)
    require_identity(cleanup_id, "docker_cleanup")
    record = reader.get(cleanup_id)
    if record is None:
        return None
    payload = _payload(record, "docker_cleanup")
    payload["volume_identities"] = tuple(payload["volume_identities"])
    result = DockerCleanup(record.record_id, record.semantic_commitment, **payload)
    _timestamp(result.completed_at, "completed_at")
    return result
