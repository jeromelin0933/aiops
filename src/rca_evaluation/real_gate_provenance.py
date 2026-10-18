"""R3 actual-invocation provenance in the single Candidate-F ledger.

An upstream result is production truth, but it is not proof that a SPEC-017
real gate invoked that upstream during the admitted evaluation execution.
This module records the evaluation-owned start/terminal envelope and validates
its correlation to gate-specific, public upstream facts.  The write helpers
are intentionally module-private; supported callers use the concrete harness
functions below, which perform the invocation before a terminal receipt can be
appended.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Callable
from uuid import uuid4

from knowledge_index import (
    ChromaIndexAdapter,
    FrozenRetrievalOperation,
    GoogleEmbeddingAdapter,
    KnowledgeProvenanceProjection,
    KnowledgeReadStatus,
    KnowledgeRetrievalService,
    RetrievalOperationRead,
    KnowledgeSnapshotService,
    ProviderInvocationLimits,
    RetrievalOperationRequest,
    RetrievalResolution,
)
from llm_generation.contracts import GenerationInput, LocalReadStatus
from llm_generation.google_generation_adapter import GoogleGenerationAdapter
from llm_generation.service import (
    AvailableResources,
    ExecutionAuthorization,
    GenerationService,
)
from llm_generation.config import GenerationConfig

from .gate_records import (
    DockerCleanup,
    DockerNamespace,
    read_docker_cleanup,
    read_docker_namespace,
    record_docker_cleanup,
)
from .gates import (
    DockerGateProfile,
    GateExecutionStatus,
    GateKind,
    GateReadiness,
    RealRagBuildContext,
    RealLlmProfile,
    RealRagProfile,
    assert_secret_free,
)
from .identity import commitment, identity, require_identity
from .sqlite_store import EvaluationLedgerReader, SqliteEvaluationStore


_TERMINAL = frozenset({
    GateExecutionStatus.EXECUTED_PASS,
    GateExecutionStatus.EXECUTED_FAIL,
    GateExecutionStatus.INVALID_EXECUTION,
})
_PROVENANCE_KIND = {
    GateKind.REAL_LLM: "LIVE_GEMINI_INVOCATION",
    GateKind.REAL_RAG: "GOVERNED_REAL_RAG_INVOCATION",
    GateKind.DOCKER_RCA_E2E: "ISOLATED_DOCKER_INVOCATION",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty timestamp text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


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


def _ready(readiness: GateReadiness, gate: GateKind, profile_identity: str,
           profile_commitment: str) -> None:
    if (not isinstance(readiness, GateReadiness)
            or readiness.status is not GateExecutionStatus.READY_NOT_EXECUTED
            or readiness.gate is not gate
            or readiness.profile_identity != profile_identity
            or readiness.profile_commitment != profile_commitment
            or readiness.authorization_state != "AUTHORIZED"):
        raise ValueError("actual invocation requires exact authorized READY gate")


@dataclass(frozen=True, slots=True)
class GateInvocationStart:
    start_id: str
    start_commitment: str
    evaluation_run_id: str
    execution_id: str
    gate: str
    profile_identity: str
    profile_commitment: str
    invocation_id: str
    started_at: str
    invocation_pins: dict[str, object]


@dataclass(frozen=True, slots=True)
class ActualInvocationReceipt:
    receipt_id: str
    receipt_commitment: str
    start_id: str
    evaluation_run_id: str
    execution_id: str
    gate: str
    profile_identity: str
    profile_commitment: str
    invocation_id: str
    started_at: str
    ended_at: str
    status: GateExecutionStatus
    provenance_kind: str
    invocation_pins: dict[str, object]
    upstream_result_reference: str
    upstream_result_commitment: str
    authoritative_typed_outcome: str
    sanitized_provenance: dict[str, object]


def _record_start(store: SqliteEvaluationStore, readiness: GateReadiness,
                  pins: dict[str, object]) -> GateInvocationStart:
    assert_secret_free(pins, path="invocation_pins")
    invocation_id = "rgi_" + uuid4().hex
    payload = {
        "evaluation_run_id": readiness.evaluation_run_id,
        "execution_id": readiness.execution_id,
        "gate": readiness.gate.value,
        "profile_identity": readiness.profile_identity,
        "profile_commitment": readiness.profile_commitment,
        "invocation_id": invocation_id,
        "started_at": _now(),
        "invocation_pins": pins,
    }
    record = store._record_actual_execution_start(**payload)
    return GateInvocationStart(record.record_id, record.semantic_commitment, **payload)


def _record_terminal(
    store: SqliteEvaluationStore, start: GateInvocationStart, *,
    status: GateExecutionStatus, ended_at: str,
    upstream_result_reference: str, upstream_result_commitment: str,
    authoritative_typed_outcome: str,
    sanitized_provenance: dict[str, object],
) -> ActualInvocationReceipt:
    if status not in _TERMINAL:
        raise ValueError("actual invocation receipt requires a terminal status")
    ended = _timestamp(ended_at, "ended_at")
    if ended < _timestamp(start.started_at, "started_at"):
        raise ValueError("actual invocation end precedes its durable start")
    assert_secret_free(sanitized_provenance, path="sanitized_provenance")
    gate = GateKind(start.gate)
    payload = {
        "start_id": start.start_id,
        "evaluation_run_id": start.evaluation_run_id,
        "execution_id": start.execution_id,
        "gate": start.gate,
        "profile_identity": start.profile_identity,
        "profile_commitment": start.profile_commitment,
        "invocation_id": start.invocation_id,
        "started_at": start.started_at,
        "ended_at": ended_at,
        "status": status.value,
        "provenance_kind": _PROVENANCE_KIND[gate],
        "invocation_pins": start.invocation_pins,
        "upstream_result_reference": _text(
            upstream_result_reference, "upstream result reference"),
        "upstream_result_commitment": _sha(
            upstream_result_commitment, "upstream result commitment"),
        "authoritative_typed_outcome": _text(
            authoritative_typed_outcome, "authoritative typed outcome"),
        "sanitized_provenance": sanitized_provenance,
    }
    # Validate the complete gate-specific terminal shape before the single
    # Candidate-F authority commits an immutable terminal truth.
    _receipt(
        identity("gate_invocation_receipt", [start.start_id, "terminal"]),
        commitment(payload), payload)
    record = store._record_actual_execution_terminal(
        start_id=start.start_id,
        status=status.value,
        ended_at=ended_at,
        provenance_kind=_PROVENANCE_KIND[gate],
        upstream_result_reference=payload["upstream_result_reference"],
        upstream_result_commitment=payload["upstream_result_commitment"],
        authoritative_typed_outcome=payload["authoritative_typed_outcome"],
        sanitized_provenance=sanitized_provenance,
    )
    return _receipt(record.record_id, record.semantic_commitment, payload)


def _receipt(record_id: str, record_commitment: str,
             payload: dict[str, object]) -> ActualInvocationReceipt:
    status = GateExecutionStatus(payload["status"])
    gate = GateKind(payload["gate"])
    if (status not in _TERMINAL
            or payload["provenance_kind"] != _PROVENANCE_KIND[gate]):
        raise ValueError("actual invocation receipt has invalid disposition/provenance")
    pins = payload["invocation_pins"]
    provenance = payload["sanitized_provenance"]
    if not isinstance(pins, dict) or not isinstance(provenance, dict):
        raise ValueError("actual invocation provenance must be bounded mappings")
    assert_secret_free(pins, path="invocation_pins")
    assert_secret_free(provenance, path="sanitized_provenance")
    if gate is GateKind.REAL_LLM:
        required_pins = {
            "provider", "model", "credential_profile_identity", "prompt_identity",
            "prompt_version", "schema_identity", "schema_version",
            "configuration_identity", "configuration_version",
        }
        if (set(pins) != required_pins
                or provenance.get("adapter") !=
                "llm_generation.google_generation_adapter.GoogleGenerationAdapter"
                or provenance.get("provider") != pins.get("provider")
                or provenance.get("model") != pins.get("model")
                or (status is GateExecutionStatus.EXECUTED_PASS
                    and provenance.get("physical_invocations") != 1)):
            raise ValueError("REAL_LLM receipt lacks exact real-provider provenance")
    elif gate is GateKind.REAL_RAG:
        required_pins = {
            "manifest_commitment", "release_reference", "embedding_provider",
            "embedding_model", "embedding_profile_identity",
            "retrieval_profile_identity", "index_engine", "index_schema_identity",
            "distance_metric",
            "chroma_reference", "retrieval_operation_identity",
            "expected_build_context",
        }
        if (set(pins) != required_pins
                or not isinstance(pins.get("expected_build_context"), dict)
                or provenance.get("embedding_adapter") !=
                "knowledge_index.google_embedding_adapter.GoogleEmbeddingAdapter"
                or provenance.get("index_adapter") !=
                "knowledge_index.chroma_index_adapter.ChromaIndexAdapter"
                or (status is GateExecutionStatus.EXECUTED_PASS and (
                    provenance.get("embedding_calls") != 1
                    or provenance.get("index_query_calls") != 1
                    or provenance.get("expected_build_context") !=
                        pins.get("expected_build_context")
                    or provenance.get("actual_build_context") !=
                        pins.get("expected_build_context")
                    or provenance.get("retrieval_operation_identity") !=
                        pins.get("retrieval_operation_identity")))):
            raise ValueError("REAL_RAG receipt lacks exact governed invocation provenance")
    else:
        required_pins = {
            "compose_commitment", "repository_commit", "project_identity",
            "volume_identities", "workspace_identity", "runtime_service",
            "runtime_entrypoint", "runtime_config", "namespace_identity",
        }
        if (set(pins) != required_pins
                or provenance.get("namespace_identity") !=
                    pins.get("namespace_identity")
                or provenance.get("project_identity") != pins.get("project_identity")
                or provenance.get("service_identity") != pins.get("runtime_service")
                or provenance.get("volume_identities") != pins.get("volume_identities")
                or (status is GateExecutionStatus.EXECUTED_PASS and (
                    provenance.get("container_identity") in {None, "unavailable"}
                    or provenance.get("image_identity") in {None, "unavailable"}
                    or provenance.get("invocation_start_id") !=
                        payload.get("start_id")
                    or provenance.get("invocation_id") !=
                        payload.get("invocation_id")
                    or provenance.get("cleanup_disposition") != "COMPLETED"
                    or not isinstance(provenance.get("probe_observation"), dict)
                    or provenance.get("probe_commitment") != commitment(
                        provenance.get("probe_observation"))))):
            raise ValueError("Docker receipt lacks exact isolated-container provenance")
        if status is GateExecutionStatus.EXECUTED_PASS:
            _validate_docker_probe_snapshot(provenance["probe_observation"])
            require_identity(
                provenance.get("cleanup_outcome_identity"), "docker_cleanup")
            _sha(provenance.get("cleanup_commitment"), "cleanup commitment")
            _sha(provenance.get("probe_commitment"), "probe commitment")
            _sha(provenance.get("command_identity"), "command identity")
            _sha(provenance.get("image_identity"), "image identity")
            start_time = _timestamp(payload["started_at"], "started_at")
            probe_time = _timestamp(
                provenance.get("probe_observed_at"), "probe_observed_at")
            cleanup_time = _timestamp(
                provenance.get("cleanup_completed_at"),
                "cleanup_completed_at")
            terminal_time = _timestamp(payload["ended_at"], "ended_at")
            if not start_time <= probe_time <= cleanup_time <= terminal_time:
                raise ValueError(
                    "Docker receipt lifecycle timestamps are out of order")
    _sha(payload["profile_commitment"], "profile commitment")
    _sha(payload["upstream_result_commitment"], "upstream result commitment")
    if _timestamp(payload["ended_at"], "ended_at") < _timestamp(
            payload["started_at"], "started_at"):
        raise ValueError("actual invocation receipt has inverted time")
    return ActualInvocationReceipt(
        record_id, record_commitment,
        payload["start_id"], payload["evaluation_run_id"],
        payload["execution_id"], payload["gate"],
        payload["profile_identity"], payload["profile_commitment"],
        payload["invocation_id"], payload["started_at"], payload["ended_at"],
        status, payload["provenance_kind"], pins,
        payload["upstream_result_reference"],
        payload["upstream_result_commitment"],
        payload["authoritative_typed_outcome"], provenance,
    )


def read_actual_invocation_receipt(
    reader: EvaluationLedgerReader, receipt_id: str,
) -> ActualInvocationReceipt | None:
    require_identity(receipt_id, "gate_invocation_receipt")
    record = reader.get(receipt_id)
    if record is None:
        return None
    if record.domain != "gate_invocation_receipt":
        raise ValueError("record is not an actual invocation receipt")
    payload = json.loads(record.semantic_json)
    start = reader.get(record.parent_id)
    if start is None or start.domain != "gate_invocation_start":
        raise ValueError("actual invocation start is unavailable")
    start_payload = json.loads(start.semantic_json)
    for field in (
        "evaluation_run_id", "execution_id", "gate", "profile_identity",
        "profile_commitment", "invocation_id", "started_at", "invocation_pins",
    ):
        if payload.get(field) != start_payload.get(field):
            raise ValueError("terminal receipt contradicts its actual invocation start")
    return _receipt(record.record_id, record.semantic_commitment, payload)


def read_gate_invocation_start(
    reader: EvaluationLedgerReader, start_id: str,
) -> GateInvocationStart | None:
    require_identity(start_id, "gate_invocation_start")
    record = reader.get(start_id)
    if record is None:
        return None
    if record.domain != "gate_invocation_start":
        raise ValueError("record is not a gate invocation start")
    payload = json.loads(record.semantic_json)
    _timestamp(payload["started_at"], "started_at")
    _sha(payload["profile_commitment"], "profile commitment")
    if not isinstance(payload["invocation_pins"], dict):
        raise ValueError("invocation pins must be a bounded mapping")
    assert_secret_free(payload["invocation_pins"], path="invocation_pins")
    return GateInvocationStart(record.record_id, record.semantic_commitment, **payload)


def actual_invocation_status(
    reader: EvaluationLedgerReader, start_id: str,
) -> GateExecutionStatus | None:
    start = read_gate_invocation_start(reader, start_id)
    if start is None:
        return None
    receipt_id = identity("gate_invocation_receipt", [start.start_id, "terminal"])
    receipt = read_actual_invocation_receipt(reader, receipt_id)
    return (receipt.status if receipt is not None
            else GateExecutionStatus.EXECUTION_STARTED)


def execute_real_llm_invocation(
    store: SqliteEvaluationStore, readiness: GateReadiness,
    profile: RealLlmProfile, service: GenerationService,
    source: GenerationInput, config: GenerationConfig,
    authorization: ExecutionAuthorization, resources: AvailableResources,
) -> ActualInvocationReceipt:
    """Invoke the existing Candidate-D service; never accepts a prebuilt result."""
    _ready(readiness, GateKind.REAL_LLM, profile.profile_identity,
           profile.profile_commitment)
    provider = getattr(service, "provider", None)
    if type(provider) is not GoogleGenerationAdapter:
        raise ValueError("REAL_LLM requires the existing Google generation adapter")
    if getattr(provider, "_client", None) is not None:
        raise ValueError("preconstructed/injected provider clients cannot attest a real gate")
    pin = source.pin
    if (config.pin != pin or pin.provider != profile.provider
            or pin.model != profile.model
            or pin.profile.identity != profile.generation_profile_identity
            or pin.prompt.identity != profile.prompt_identity
            or pin.result_schema.identity != profile.result_schema_identity
            or pin.configuration.identity != profile.configuration_identity):
        raise ValueError("REAL_LLM input/config does not match the admitted gate profile")
    pins = {
        "provider": pin.provider, "model": pin.model,
        "credential_profile_identity": pin.profile.identity,
        "prompt_identity": pin.prompt.identity,
        "prompt_version": pin.prompt.version,
        "schema_identity": pin.result_schema.identity,
        "schema_version": pin.result_schema.version,
        "configuration_identity": pin.configuration.identity,
        "configuration_version": pin.configuration.version,
    }
    start = _record_start(store, readiness, pins)
    before = provider.generation_calls
    result = service.execute(source, config, authorization, resources)
    after = provider.generation_calls
    if after - before != 1 or result.physical_invocations != 1:
        return _record_terminal(
            store, start, status=GateExecutionStatus.INVALID_EXECUTION,
            ended_at=_now(), upstream_result_reference=source.operation_id,
            upstream_result_commitment=commitment({
                "operation_id": source.operation_id,
                "try": [source.try_identity.attempt_id,
                        source.try_identity.try_ordinal],
            }), authoritative_typed_outcome="NO_ACTUAL_GENERATION_INVOCATION",
            sanitized_provenance={"adapter": type(provider).__module__ + "." + type(provider).__name__,
                                  "provider": pin.provider, "model": pin.model,
                                  "physical_invocations": result.physical_invocations},
        )
    if result.status is not LocalReadStatus.FOUND:
        status = GateExecutionStatus.INVALID_EXECUTION
        reference = source.operation_id
        result_commitment = commitment({"operation_id": source.operation_id,
                                        "status": result.status.value})
        outcome = result.status.value
    elif result.result is not None:
        public = service.store.result(result.result.validated_result_id)
        if public.status is not LocalReadStatus.FOUND or public.value != result.result:
            raise ValueError("Candidate-D public result does not match the invocation")
        if result.result.content.input != source:
            raise ValueError("Candidate-D result belongs to another invocation input")
        status = GateExecutionStatus.EXECUTED_PASS
        reference = result.result.validated_result_id
        result_commitment = "sha256:" + result.result.semantic_commitment
        outcome = "VALIDATED_GENERATION_RESULT"
    elif result.failure is not None:
        public = service.store.failure(source.operation_id, source.try_identity)
        if public.status is not LocalReadStatus.FOUND or public.value != result.failure:
            raise ValueError("Candidate-D public failure does not match the invocation")
        status = GateExecutionStatus.EXECUTED_FAIL
        reference = source.operation_id
        result_commitment = commitment({
            "operation_id": source.operation_id,
            "failure_class": result.failure.failure_class.value,
            "retry_safety": result.failure.retry_safety.value,
        })
        outcome = result.failure.failure_class.value
    else:
        raise ValueError("Candidate-D invocation returned no authoritative outcome")
    return _record_terminal(
        store, start, status=status, ended_at=_now(),
        upstream_result_reference=reference,
        upstream_result_commitment=result_commitment,
        authoritative_typed_outcome=outcome,
        sanitized_provenance={
            "adapter": type(provider).__module__ + "." + type(provider).__name__,
            "provider": pin.provider, "model": pin.model,
            "physical_invocations": result.physical_invocations,
            "preflight_invocations": result.token_preflight_calls,
        },
    )


class _ObservedEmbedding:
    def __init__(self, provider: GoogleEmbeddingAdapter) -> None:
        self.provider = provider
        self.calls = 0
        self.last_result = None

    def capability(self):
        return self.provider.capability()

    def embed_query(self, request):
        self.calls += 1
        self.last_result = self.provider.embed_query(request)
        return self.last_result


class _ObservedIndex:
    def __init__(self, index: ChromaIndexAdapter) -> None:
        self.index = index
        self.query_calls = 0

    def inspect(self, build_identity: str):
        return self.index.inspect(build_identity)

    def query(self, build_identity: str, vector: tuple[float, ...], *, candidate_limit: int):
        self.query_calls += 1
        return self.index.query(build_identity, vector, candidate_limit=candidate_limit)


def _real_rag_actual_build_context(
    frozen: FrozenRetrievalOperation,
    public: KnowledgeProvenanceProjection,
) -> dict[str, object]:
    return {
        "active_build_identity": public.frozen_build_identity,
        "activation_generation": public.activation_generation,
        "activation_operation_identity":
            public.activation_operation_identity.value,
        "validated_build_commitment": public.validation_commitment,
        "manifest_identity": public.manifest_identity,
        "manifest_commitment": public.manifest_commitment,
        "corpus_identity": public.corpus_identity,
        "corpus_version": public.corpus_version,
        "embedding_provider": public.embedding_provider,
        "embedding_model": public.embedding_model,
        "embedding_profile_identity": public.embedding_profile_identity,
        "embedding_dimension": public.embedding_dimension,
        "index_engine": public.index_engine,
        "index_schema_identity": public.index_schema_identity,
        "distance_metric": public.distance_metric,
        "artifact_commitment": public.artifact_commitment,
        "lineage_commitment": public.lineage_commitment,
        "staged_commitment": public.staged_commitment,
    }


def _validate_real_rag_frozen_binding(
    expected: RealRagBuildContext,
    frozen: FrozenRetrievalOperation,
    request: RetrievalOperationRequest,
) -> None:
    profile = frozen.request.retrieval_profile
    if (frozen.request.operation_key != request.operation_key
            or frozen.request != request
            or frozen.frozen_build_identity != expected.active_build_identity
            or frozen.activation_generation != expected.activation_generation
            or frozen.activation_operation_key.value !=
                expected.activation_operation_identity
            or frozen.validation_commitment !=
                expected.validated_build_commitment
            or frozen.staged_commitment != expected.staged_commitment
            or frozen.artifact_commitment != expected.artifact_commitment
            or frozen.lineage_commitment != expected.lineage_commitment
            or profile.provider != expected.embedding_provider
            or profile.model != expected.embedding_model
            or profile.embedding_profile_identity !=
                expected.embedding_profile_identity
            or profile.embedding_dimension != expected.embedding_dimension
            or profile.index_engine != expected.index_engine
            or profile.index_schema_identity != expected.index_schema_identity):
        raise ValueError(
            "Candidate-C frozen retrieval does not match readiness build"
        )


def _validate_real_rag_binding(
    expected: RealRagBuildContext,
    frozen: FrozenRetrievalOperation,
    public: KnowledgeProvenanceProjection,
    request: RetrievalOperationRequest,
) -> dict[str, object]:
    _validate_real_rag_frozen_binding(expected, frozen, request)
    if (public.retrieval_operation_identity != request.operation_key
            or public.frozen_build_identity != frozen.frozen_build_identity
            or public.distance_metric != expected.distance_metric):
        raise ValueError(
            "Candidate-C public retrieval identity/build does not match invocation"
        )
    actual = _real_rag_actual_build_context(frozen, public)
    if actual != expected.pins():
        raise ValueError(
            "Candidate-C public provenance does not match readiness build"
        )
    return actual


def _real_rag_invocation_pins(
    profile: RealRagProfile,
    request: RetrievalOperationRequest,
    chroma_path: str | Path,
    expected: RealRagBuildContext,
) -> dict[str, object]:
    values = profile.values()
    retrieval = request.retrieval_profile
    return {
        "manifest_commitment": values["manifest_commitment"],
        "release_reference": values["release_reference"],
        "embedding_provider": retrieval.provider,
        "embedding_model": retrieval.model,
        "embedding_profile_identity": retrieval.embedding_profile_identity,
        "retrieval_profile_identity": retrieval.profile_identity,
        "index_engine": retrieval.index_engine,
        "index_schema_identity": retrieval.index_schema_identity,
        "distance_metric": expected.distance_metric,
        "chroma_reference": str(Path(chroma_path).resolve()),
        "retrieval_operation_identity": request.operation_key.value,
        "expected_build_context": expected.pins(),
    }


def execute_real_rag_invocation(
    store: SqliteEvaluationStore, readiness: GateReadiness,
    profile: RealRagProfile, *, knowledge_store: object,
    provider: GoogleEmbeddingAdapter, index: ChromaIndexAdapter,
    request: RetrievalOperationRequest, limits: ProviderInvocationLimits,
    repository_root: str | Path,
) -> ActualInvocationReceipt:
    """Invoke Candidate-C's governed service with observed real adapters."""
    _ready(readiness, GateKind.REAL_RAG, profile.profile_identity,
           profile.profile_commitment)
    if type(provider) is not GoogleEmbeddingAdapter or type(index) is not ChromaIndexAdapter:
        raise ValueError("REAL_RAG requires existing Google/Chroma adapters")
    values = profile.values()
    rp = request.retrieval_profile
    isolated_root = (Path(repository_root).resolve()
                     / values["isolated_chroma_root"]).resolve()
    candidate_path = Path(index.storage_path).resolve()
    try:
        candidate_path.relative_to(isolated_root)
        isolated = candidate_path != isolated_root
    except ValueError:
        isolated = False
    if (rp.provider != values["embedding_provider"]
            or rp.model != values["embedding_model"]
            or rp.embedding_profile_identity != values["embedding_profile_identity"]
            or rp.index_engine != values["index_engine"]
            or rp.index_schema_identity != values["index_schema_identity"]
            or index.distance_metric != values["distance_metric"]
            or rp.profile_identity != values["retrieval_profile_identity"]
            or not isolated):
        raise ValueError("REAL_RAG request/adapters do not match the admitted profile")
    expected = readiness.real_rag_build_context
    if expected is None:
        raise ValueError("REAL_RAG readiness lacks exact Candidate-C build context")
    provider_client = getattr(provider, "_client", None)
    index_client = getattr(index, "_client", None)
    if (not type(provider_client).__module__.startswith("google.genai")
            or not type(index_client).__module__.startswith("chromadb")):
        raise ValueError("injected/fake RAG clients cannot attest a real gate")
    pins = _real_rag_invocation_pins(
        profile, request, index.storage_path, expected)
    start = _record_start(store, readiness, pins)
    observed_provider = _ObservedEmbedding(provider)
    observed_index = _ObservedIndex(index)
    retrieval = KnowledgeRetrievalService(knowledge_store, observed_provider, observed_index)
    snapshot_service = KnowledgeSnapshotService(knowledge_store, retrieval)
    outcome = snapshot_service.resolve(request, limits)
    operation = snapshot_service.read_operation(request.operation_key)
    base_provenance = {
        "embedding_adapter": type(provider).__module__ + "." + type(provider).__name__,
        "index_adapter": type(index).__module__ + "." + type(index).__name__,
        "embedding_calls": observed_provider.calls,
        "index_query_calls": observed_index.query_calls,
        "expected_build_context": expected.pins(),
        "retrieval_operation_identity": request.operation_key.value,
    }
    if (observed_provider.calls != 1 or observed_index.query_calls != 1
            or observed_provider.last_result is None
            or observed_provider.last_result.invocation_count != 1):
        return _record_terminal(
            store, start, status=GateExecutionStatus.INVALID_EXECUTION,
            ended_at=_now(), upstream_result_reference=request.operation_key.value,
            upstream_result_commitment=commitment({"operation": request.operation_key.value}),
            authoritative_typed_outcome="NO_ACTUAL_REAL_RAG_INVOCATION",
            sanitized_provenance={
                **base_provenance, "actual_build_context": "unavailable",
            },
        )
    if (operation.status is not KnowledgeReadStatus.FOUND
            or type(operation.value) is not RetrievalOperationRead):
        status = GateExecutionStatus.EXECUTED_FAIL
        reference = request.operation_key.value
        result_commitment = commitment({
            "operation": request.operation_key.value,
            "resolution": outcome.resolution.value,
        })
        actual_context: dict[str, object] | str = "unavailable"
    else:
        frozen = operation.value.frozen
        try:
            _validate_real_rag_frozen_binding(expected, frozen, request)
        except ValueError:
            return _record_terminal(
                store, start, status=GateExecutionStatus.INVALID_EXECUTION,
                ended_at=_now(),
                upstream_result_reference=request.operation_key.value,
                upstream_result_commitment=commitment({
                    "operation": request.operation_key.value,
                    "frozen_build_identity": frozen.frozen_build_identity,
                }),
                authoritative_typed_outcome="CANDIDATE_C_BUILD_BINDING_MISMATCH",
                sanitized_provenance={
                    **base_provenance,
                    "actual_build_context": {
                        "active_build_identity": frozen.frozen_build_identity,
                        "activation_generation": frozen.activation_generation,
                        "activation_operation_identity":
                            frozen.activation_operation_key.value,
                        "validated_build_commitment":
                            frozen.validation_commitment,
                        "artifact_commitment": frozen.artifact_commitment,
                        "lineage_commitment": frozen.lineage_commitment,
                        "staged_commitment": frozen.staged_commitment,
                    },
                },
            )
        if (outcome.resolution not in {RetrievalResolution.MATCH,
                                      RetrievalResolution.NO_MATCH}
                or outcome.snapshot is None):
            status = GateExecutionStatus.EXECUTED_FAIL
            reference = request.operation_key.value
            result_commitment = commitment({
                "operation": request.operation_key.value,
                "resolution": outcome.resolution.value,
            })
            actual_context = {
                "active_build_identity": frozen.frozen_build_identity,
                "activation_generation": frozen.activation_generation,
                "activation_operation_identity":
                    frozen.activation_operation_key.value,
                "validated_build_commitment": frozen.validation_commitment,
                "artifact_commitment": frozen.artifact_commitment,
                "lineage_commitment": frozen.lineage_commitment,
                "staged_commitment": frozen.staged_commitment,
            }
        else:
            provenance = snapshot_service.read_provenance(
                outcome.snapshot.snapshot_key)
            if (provenance.status is not KnowledgeReadStatus.FOUND
                    or type(provenance.value) is not
                        KnowledgeProvenanceProjection):
                raise ValueError("Candidate-C public provenance is unavailable")
            public = provenance.value
            try:
                actual_context = _validate_real_rag_binding(
                    expected, frozen, public, request)
            except ValueError:
                return _record_terminal(
                    store, start,
                    status=GateExecutionStatus.INVALID_EXECUTION,
                    ended_at=_now(),
                    upstream_result_reference=
                        outcome.snapshot.snapshot_key.value,
                    upstream_result_commitment=
                        "sha256:" + outcome.snapshot.snapshot_commitment,
                    authoritative_typed_outcome=
                        "CANDIDATE_C_BUILD_BINDING_MISMATCH",
                    sanitized_provenance={
                        **base_provenance,
                        "actual_build_context":
                            _real_rag_actual_build_context(frozen, public),
                    },
                )
            status = GateExecutionStatus.EXECUTED_PASS
            reference = outcome.snapshot.snapshot_key.value
            result_commitment = "sha256:" + outcome.snapshot.snapshot_commitment
    return _record_terminal(
        store, start, status=status, ended_at=_now(),
        upstream_result_reference=reference,
        upstream_result_commitment=result_commitment,
        authoritative_typed_outcome=outcome.resolution.value,
        sanitized_provenance={
            **base_provenance, "actual_build_context": actual_context,
        },
    )


@dataclass(frozen=True, slots=True)
class DockerProbeEvidence:
    evaluation_run_id: str
    execution_id: str
    invocation_start_id: str
    invocation_id: str
    namespace_id: str
    project_identity: str
    service_identity: str
    container_identity: str
    image_identity: str
    volume_identities: tuple[str, ...]
    command_identity: str
    observed_at: str
    observation: dict[str, object]
    probe_commitment: str


def _validate_docker_probe_snapshot(snapshot: object) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) != {
            "states", "incidents", "shadow_exists",
            "shadow_has_incident_owner", "work", "work_corruptions", "rca"}:
        raise ValueError("Docker probe observation is malformed or incomplete")
    states = snapshot.get("states")
    if not isinstance(states, dict) or set(states) != {
            "DOCKER-A-CREATE", "DOCKER-B-ATTACH", "DOCKER-C-PENDING",
            "DOCKER-D-SHADOW"}:
        raise ValueError("Docker probe state observations are incomplete")
    expected_outcomes = {
        "DOCKER-A-CREATE": "CREATED_INCIDENT",
        "DOCKER-B-ATTACH": "ATTACHED_TO_INCIDENT",
        "DOCKER-C-PENDING": "CREATED_INCIDENT",
        "DOCKER-D-SHADOW": "SHADOWED",
    }
    for event_id, expected in expected_outcomes.items():
        item = states.get(event_id)
        if (not isinstance(item, dict)
                or item.get("processed") != expected
                or set(item) != {"processed", "incident_id", "pending"}
                or item.get("pending") is not False):
            raise ValueError("Docker probe did not observe successful runtime state")
    if (states["DOCKER-A-CREATE"].get("incident_id") is None
            or states["DOCKER-A-CREATE"].get("incident_id") !=
                states["DOCKER-B-ATTACH"].get("incident_id")
            or snapshot.get("shadow_exists") is not True
            or snapshot.get("shadow_has_incident_owner") is not False
            or snapshot.get("work_corruptions") != 0):
        raise ValueError("Docker probe incident/state linkage is invalid")
    incidents = snapshot.get("incidents")
    work = snapshot.get("work")
    if (not isinstance(incidents, list) or len(incidents) != 2
            or any(not isinstance(item, dict)
                   or item.get("status") != "ASSIGNED"
                   or item.get("workflow_audits") != 1
                   for item in incidents)
            or not isinstance(work, list) or len(work) != 2
            or any(not isinstance(item, dict)
                   or item.get("status") != "COMPLETED" for item in work)):
        raise ValueError("Docker probe public Incident/Runtime facts are incomplete")
    if snapshot.get("rca") != {
        "a_candidates": 0,
        "b_readiness": "READY",
        "b_integrity": "VALID",
        "c_readiness": "NOT_INITIALIZED",
        "d_readiness": "FOUND",
        "d_recovery": "FOUND",
        "d2_roots": 0,
        "d2_corruptions": 0,
    }:
        raise ValueError("Docker probe public RCA observations are incomplete")


def _validate_docker_terminal_binding(
    reader: EvaluationLedgerReader,
    readiness: GateReadiness,
    profile: DockerGateProfile,
    start: GateInvocationStart,
    probe: DockerProbeEvidence,
    cleanup_id: str,
    terminal_at: str,
) -> DockerCleanup:
    if type(probe) is not DockerProbeEvidence:
        raise ValueError("Docker terminal requires harness-issued probe evidence")
    namespace = read_docker_namespace(reader, probe.namespace_id)
    cleanup = read_docker_cleanup(reader, cleanup_id)
    if namespace is None or cleanup is None:
        raise ValueError("Docker terminal requires durable namespace and cleanup")
    pins = start.invocation_pins
    if ((probe.evaluation_run_id, probe.execution_id,
         probe.invocation_start_id, probe.invocation_id) != (
            readiness.evaluation_run_id, readiness.execution_id,
            start.start_id, start.invocation_id)
            or (start.gate, start.profile_identity,
                start.profile_commitment) != (
                    GateKind.DOCKER_RCA_E2E.value,
                    profile.profile_identity, profile.profile_commitment)
            or (namespace.evaluation_run_id, namespace.execution_id,
                namespace.profile_identity, namespace.profile_commitment) != (
                    readiness.evaluation_run_id, readiness.execution_id,
                    profile.profile_identity, profile.profile_commitment)
            or probe.project_identity != namespace.project_name
            or probe.service_identity != profile.values()["runtime_service"]
            or probe.volume_identities != namespace.volume_identities
            or pins.get("namespace_identity") != namespace.namespace_id
            or pins.get("project_identity") != namespace.project_name
            or pins.get("volume_identities") != list(namespace.volume_identities)
            or pins.get("workspace_identity") != namespace.workspace_root
            or pins.get("runtime_service") != probe.service_identity
            or probe.container_identity in {"", "unavailable"}
            or probe.image_identity in {"", "unavailable"}
            or probe.probe_commitment != commitment(probe.observation)):
        raise ValueError("Docker probe does not bind the exact invocation context")
    _sha(probe.image_identity, "image identity")
    _sha(probe.command_identity, "command identity")
    _validate_docker_probe_snapshot(probe.observation)
    if ((cleanup.namespace_id, cleanup.project_name,
         cleanup.volume_identities, cleanup.workspace_root) != (
            namespace.namespace_id, namespace.project_name,
            namespace.volume_identities, namespace.workspace_root)
            or cleanup.invocation_start_id != start.start_id
            or cleanup.invocation_id != start.invocation_id
            or cleanup.invocation_started_at != start.started_at
            or cleanup.disposition != "COMPLETED"):
        raise ValueError("Docker cleanup does not bind the completed invocation")
    issued = _timestamp(namespace.issued_at, "namespace issued_at")
    started = _timestamp(start.started_at, "started_at")
    observed = _timestamp(probe.observed_at, "probe observed_at")
    completed = _timestamp(cleanup.completed_at, "cleanup completed_at")
    terminal = _timestamp(terminal_at, "terminal_at")
    if not issued <= started <= observed <= completed <= terminal:
        raise ValueError("Docker terminal lifecycle timestamps are out of order")
    return cleanup


def execute_docker_invocation(
    store: SqliteEvaluationStore, readiness: GateReadiness,
    profile: DockerGateProfile, *, namespace_id: str,
    repository_root: str | Path, repository_commit: str,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ActualInvocationReceipt:
    """Run the existing compose/runtime path and observe container provenance.

    The default runner is the real subprocess boundary.  Supplying another
    runner is rejected, so deterministic fakes cannot mint formal Docker PASS.
    """
    _ready(readiness, GateKind.DOCKER_RCA_E2E, profile.profile_identity,
           profile.profile_commitment)
    if command_runner is not subprocess.run:
        raise ValueError("injected command runners cannot attest Docker execution")
    namespace = read_docker_namespace(store.reader(), namespace_id)
    if namespace is None or (namespace.evaluation_run_id, namespace.execution_id,
                             namespace.profile_identity,
                             namespace.profile_commitment) != (
        readiness.evaluation_run_id, readiness.execution_id,
        profile.profile_identity, profile.profile_commitment,
    ):
        raise ValueError("Docker invocation requires its exact durable namespace")
    root = Path(repository_root).resolve()
    values = profile.values()
    compose = (root / values["compose_file"]).resolve()
    pins = {
        "compose_commitment": commitment({"content": compose.read_text(encoding="utf-8")}),
        "repository_commit": _text(repository_commit, "repository commit"),
        "project_identity": namespace.project_name,
        "volume_identities": list(namespace.volume_identities),
        "workspace_identity": namespace.workspace_root,
        "runtime_service": values["runtime_service"],
        "runtime_entrypoint": values["runtime_entrypoint"],
        "runtime_config": values["runtime_config"],
        "namespace_identity": namespace.namespace_id,
    }
    start = _record_start(store, readiness, pins)
    base = ["docker", "compose", "-p", namespace.project_name, "-f", str(compose)]
    up = command_runner(base + ["up", "-d", "--build", values["runtime_service"]],
                        cwd=root, text=True, capture_output=True, check=False)
    ps = command_runner(base + ["ps", "--format", "json"], cwd=root, text=True,
                        capture_output=True, check=False)
    probe = command_runner(
        base + ["exec", "-T", values["runtime_service"], "python",
                "docker/runtime/e2e_probe.py", "snapshot"],
        cwd=root, text=True, capture_output=True, check=False,
    )
    probe_observed_at = _now()
    try:
        services = []
        if ps.returncode == 0 and ps.stdout.strip():
            try:
                services = json.loads(ps.stdout)
            except json.JSONDecodeError:
                services = [json.loads(line) for line in ps.stdout.splitlines()
                            if line.strip()]
        if isinstance(services, dict):
            services = [services]
        runtime = [item for item in services if isinstance(item, dict)
                   and item.get("Service") == values["runtime_service"]]
        inspect_result = None
        inspected = None
        if len(runtime) == 1 and runtime[0].get("ID"):
            inspect_result = command_runner(
                ["docker", "inspect", str(runtime[0]["ID"])], cwd=root,
                text=True, capture_output=True, check=False)
            if inspect_result.returncode == 0:
                decoded = json.loads(inspect_result.stdout)
                if isinstance(decoded, list) and len(decoded) == 1:
                    inspected = decoded[0]
        try:
            snapshot = (json.loads(probe.stdout.splitlines()[-1])
                        if probe.returncode == 0 and probe.stdout.splitlines()
                        else None)
        except (json.JSONDecodeError, IndexError):
            snapshot = None
        labels = (((inspected or {}).get("Config") or {}).get("Labels") or {})
        mounts = tuple(sorted(
            item.get("Name") for item in ((inspected or {}).get("Mounts") or [])
            if isinstance(item, dict) and item.get("Type") == "volume"
            and isinstance(item.get("Name"), str)
        ))
        command = tuple(str(item) for item in (
            ((inspected or {}).get("Config") or {}).get("Cmd") or []))
        image_digest = str((inspected or {}).get("Image") or "")
        try:
            _validate_docker_probe_snapshot(snapshot)
            probe_valid = True
        except ValueError:
            probe_valid = False
        valid = (up.returncode == ps.returncode == probe.returncode == 0
                 and len(runtime) == 1 and runtime[0].get("Project") == namespace.project_name
                 and inspect_result is not None and inspect_result.returncode == 0
                 and labels.get("com.docker.compose.project") == namespace.project_name
                 and labels.get("com.docker.compose.service") == values["runtime_service"]
                 and mounts == tuple(sorted(namespace.volume_identities))
                 and values["runtime_entrypoint"] in command
                 and values["runtime_config"] in command
                 and bool(runtime[0].get("ID")) and bool(runtime[0].get("Image"))
                 and bool(image_digest)
                 and probe_valid)
        status = (GateExecutionStatus.EXECUTED_PASS if valid
                  else GateExecutionStatus.EXECUTED_FAIL)
        container = runtime[0] if runtime else {}
        result_commitment = commitment({
            "container_id": container.get("ID"),
            "image": container.get("Image"),
            "probe": snapshot,
        })
        terminal_reference = str(container.get("ID") or namespace.project_name)
        terminal_outcome = ("SPEC011_DOCKER_PROBE_PASS" if valid
                            else "DOCKER_HARNESS_FAILURE")
        terminal_provenance = {
            "namespace_identity": namespace.namespace_id,
            "project_identity": namespace.project_name,
            "container_identity": str(container.get("ID") or "unavailable"),
            "service_identity": values["runtime_service"],
            "image_identity": image_digest or "unavailable",
            "volume_identities": list(mounts),
            "command_identity": commitment({"command": list(command)}),
            "probe_commitment": commitment(snapshot) if isinstance(snapshot, dict)
                else commitment({"probe": "unavailable"}),
            "probe_observed_at": probe_observed_at,
            "probe_observation": snapshot if isinstance(snapshot, dict) else {},
            "invocation_start_id": start.start_id,
            "invocation_id": start.invocation_id,
        }
        probe_evidence = DockerProbeEvidence(
            readiness.evaluation_run_id, readiness.execution_id,
            start.start_id, start.invocation_id, namespace.namespace_id,
            namespace.project_name, values["runtime_service"],
            terminal_provenance["container_identity"],
            terminal_provenance["image_identity"], mounts,
            terminal_provenance["command_identity"], probe_observed_at,
            snapshot if isinstance(snapshot, dict) else {},
            terminal_provenance["probe_commitment"],
        )
    finally:
        down = command_runner(base + ["down", "-v", "--remove-orphans"], cwd=root,
                              text=True, capture_output=True, check=False)
        cleanup = record_docker_cleanup(
            store, namespace_id=namespace.namespace_id,
            project_name=namespace.project_name,
            volume_identities=namespace.volume_identities,
            workspace_root=namespace.workspace_root, completed_at=_now(),
            disposition="COMPLETED" if down.returncode == 0 else "FAILED",
            invocation_start_id=start.start_id,
            invocation_id=start.invocation_id,
            invocation_started_at=start.started_at,
        )
    terminal_at = _now()
    terminal_provenance["cleanup_outcome_identity"] = cleanup.cleanup_id
    terminal_provenance["cleanup_commitment"] = cleanup.cleanup_commitment
    terminal_provenance["cleanup_disposition"] = cleanup.disposition
    terminal_provenance["cleanup_completed_at"] = cleanup.completed_at
    if cleanup.disposition != "COMPLETED" and status is GateExecutionStatus.EXECUTED_PASS:
        status = GateExecutionStatus.EXECUTED_FAIL
        terminal_outcome = "DOCKER_CLEANUP_FAILURE"
    if status is GateExecutionStatus.EXECUTED_PASS:
        try:
            cleanup = _validate_docker_terminal_binding(
                store.reader(), readiness, profile, start, probe_evidence,
                cleanup.cleanup_id, terminal_at)
        except ValueError:
            status = GateExecutionStatus.INVALID_EXECUTION
            terminal_outcome = "DOCKER_PROBE_CLEANUP_BINDING_INVALID"
    receipt = _record_terminal(
        store, start, status=status, ended_at=terminal_at,
        upstream_result_reference=terminal_reference,
        upstream_result_commitment=result_commitment,
        authoritative_typed_outcome=terminal_outcome,
        sanitized_provenance=terminal_provenance,
    )
    return receipt
