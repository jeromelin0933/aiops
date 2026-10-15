"""S5 explicit opt-in real-gate profiles, readiness, and safe evidence hooks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping

import yaml

from knowledge_index import (
    ActivationAuthorityRecord,
    KnowledgeLocalReadiness,
    KnowledgeReadStatus,
    StagedBuildRecord,
    admit_production_manifest,
    load_knowledge_config,
)
from llm_generation.config import load_generation_config

from .gate_records import (
    read_gate_authorization, read_gate_execution_receipt,
)
from .identity import canonical_bytes, commitment, require_identity
from .sqlite_store import EvaluationLedgerReader


class GateKind(str, Enum):
    REAL_LLM = "REAL_LLM"
    REAL_RAG = "REAL_RAG"
    DOCKER_RCA_E2E = "DOCKER_RCA_E2E"


class GateExecutionStatus(str, Enum):
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    NOT_READY = "NOT_READY"
    READY_NOT_EXECUTED = "READY_NOT_EXECUTED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTED_PASS = "EXECUTED_PASS"
    EXECUTED_FAIL = "EXECUTED_FAIL"
    INVALID_EXECUTION = "INVALID_EXECUTION"


class ExecutionProvenance(str, Enum):
    REAL_PROVIDER = "REAL_PROVIDER"
    FAKE_PROVIDER = "FAKE_PROVIDER"
    REAL_RAG_ACTIVE_OR_LKG = "REAL_RAG_ACTIVE_OR_LKG"
    FAKE_RAG = "FAKE_RAG"
    SPEC011_DOCKER_RUNTIME = "SPEC011_DOCKER_RUNTIME"
    HOST_ONLY = "HOST_ONLY"


_FORBIDDEN_NORMALIZED_KEYS = frozenset({
    "authorization", "authorizationheader", "xapikey", "apikey", "clientsecret",
    "secret", "secretvalue", "password", "accesstoken", "refreshtoken", "token",
    "credential", "credentials", "credentialvalue", "privatekey", "rawauthorization",
    "rawproviderpayload", "providerpayload", "unboundedproviderpayload", "privatedbdump",
})
_SAFE_SENSITIVE_SUFFIXES = (
    "reference", "identity", "commitment", "environment", "state", "available", "ready",
)


def _normalized_key(value: str) -> str:
    return re.sub(r"[\s_-]+", "", value).lower()


def assert_secret_free(value: object, *, path: str = "root") -> None:
    """Reject credential material while allowing profile/env-var references."""
    if value is None or type(value) in {bool, int}:
        return
    if isinstance(value, str):
        lowered = value.lower()
        if (lowered.startswith("bearer ") or "-----begin private key-----" in lowered
                or re.fullmatch(r"AIza[0-9A-Za-z_-]{20,}", value)
                or re.fullmatch(r"sk-[0-9A-Za-z_-]{16,}", value)
                or re.fullmatch(r"AKIA[0-9A-Z]{16}", value)
                or re.fullmatch(r"eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{8,}", value)):
            raise ValueError(f"secret material is forbidden at {path}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_secret_free(item, path=f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("evidence/config keys must be strings")
            normalized = _normalized_key(key)
            risky = (normalized in _FORBIDDEN_NORMALIZED_KEYS
                     or any(token in normalized for token in (
                         "authorization", "apikey", "secret", "password", "token",
                         "credential", "privatekey",
                     )))
            # ``authorization`` is a profile schema section containing only
            # environment-variable names.  A scalar/list with that key is
            # still credential-bearing and is rejected.
            structural_authorization = normalized == "authorization" and isinstance(item, Mapping)
            if risky and not structural_authorization and not normalized.endswith(_SAFE_SENSITIVE_SUFFIXES):
                raise ValueError(f"secret field is forbidden at {path}.{key}")
            assert_secret_free(item, path=f"{path}.{key}")
        return
    raise ValueError(f"unsupported evidence/config value at {path}")


def _mapping(value: object, label: str) -> dict:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise ValueError(f"{label} must be a string mapping")
    return value


def _keys(value: dict, expected: set[str], label: str) -> dict:
    if set(value) != expected:
        raise ValueError(f"{label} has missing or unsupported keys")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty trimmed text")
    return value


def _load_yaml(path: str | Path) -> tuple[dict, str]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("gate profile is unreadable") from exc
    root = _mapping(raw, "gate profile")
    assert_secret_free(root)
    return root, commitment(root)


@dataclass(frozen=True, slots=True)
class AuthorizationRequirements:
    opt_in_environment: str
    authorization_environment: str
    credential_environment: str | None = None


def _authorization(raw: object, *, credential: bool) -> AuthorizationRequirements:
    data = _mapping(raw, "authorization")
    expected = {"opt_in_environment", "authorization_environment"}
    if credential:
        expected.add("credential_environment")
    _keys(data, expected, "authorization")
    return AuthorizationRequirements(
        _text(data["opt_in_environment"], "opt-in environment"),
        _text(data["authorization_environment"], "authorization environment"),
        _text(data["credential_environment"], "credential environment") if credential else None,
    )


@dataclass(frozen=True, slots=True)
class RealLlmProfile:
    profile_commitment: str
    profile_identity: str
    default_enabled: bool
    authorization: AuthorizationRequirements
    config_path: str
    adapter: str
    provider: str
    model: str
    generation_profile_identity: str
    prompt_identity: str
    result_schema_identity: str
    configuration_identity: str
    timeout_seconds: int
    maximum_invocations: int
    provenance_reference: str
    public_result_reference: str


def load_real_llm_profile(path: str | Path) -> RealLlmProfile:
    root, digest = _load_yaml(path)
    _keys(root, {"version", "gate", "profile_identity", "default_enabled", "authorization", "generation", "evidence"}, "real LLM profile")
    if root["version"] != "1.0" or root["gate"] != GateKind.REAL_LLM.value or root["default_enabled"] is not False:
        raise ValueError("real LLM profile must be version 1.0 and default off")
    generation = _keys(_mapping(root["generation"], "generation"), {
        "config_path", "adapter", "provider", "model", "profile_identity",
        "prompt_identity", "result_schema_identity", "configuration_identity",
        "timeout_seconds", "maximum_invocations",
    }, "generation")
    evidence = _keys(_mapping(root["evidence"], "evidence"), {
        "provenance_reference", "public_result_reference",
    }, "evidence")
    texts = {key: _text(value, key) for key, value in generation.items()
             if key not in {"timeout_seconds", "maximum_invocations"}}
    texts["generation_profile_identity"] = texts.pop("profile_identity")
    if type(generation["timeout_seconds"]) is not int or generation["timeout_seconds"] <= 0:
        raise ValueError("LLM timeout must be positive")
    if generation["maximum_invocations"] != 1:
        raise ValueError("real LLM profile cannot add retries")
    return RealLlmProfile(
        digest, _text(root["profile_identity"], "profile identity"), False,
        _authorization(root["authorization"], credential=True),
        **texts, timeout_seconds=generation["timeout_seconds"],
        maximum_invocations=generation["maximum_invocations"],
        provenance_reference=_text(evidence["provenance_reference"], "provenance reference"),
        public_result_reference=_text(evidence["public_result_reference"], "public result reference"),
    )


@dataclass(frozen=True, slots=True)
class RealRagProfile:
    profile_commitment: str
    profile_identity: str
    default_enabled: bool
    authorization: AuthorizationRequirements
    settings: bytes

    def values(self) -> dict:
        return json.loads(self.settings)


def load_real_rag_profile(path: str | Path) -> RealRagProfile:
    root, digest = _load_yaml(path)
    _keys(root, {"version", "gate", "profile_identity", "default_enabled", "authorization", "knowledge"}, "real RAG profile")
    if root["version"] != "1.0" or root["gate"] != GateKind.REAL_RAG.value or root["default_enabled"] is not False:
        raise ValueError("real RAG profile must be version 1.0 and default off")
    knowledge = _keys(_mapping(root["knowledge"], "knowledge"), {
        "config_path", "manifest_path", "release_reference", "manifest_commitment",
        "embedding_adapter", "embedding_provider", "embedding_model",
        "embedding_profile_reference", "embedding_profile_identity", "index_engine",
        "index_schema_identity", "retrieval_profile_identity", "active_or_lkg_required",
        "public_retrieval_required", "isolated_chroma_root",
    }, "knowledge")
    if knowledge["active_or_lkg_required"] is not True or knowledge["public_retrieval_required"] is not True:
        raise ValueError("real RAG requires Active/LKG and public retrieval")
    for key, value in knowledge.items():
        if key not in {"active_or_lkg_required", "public_retrieval_required"}:
            _text(value, key)
    return RealRagProfile(digest, _text(root["profile_identity"], "profile identity"), False,
                          _authorization(root["authorization"], credential=True),
                          canonical_bytes(knowledge))


@dataclass(frozen=True, slots=True)
class DockerGateProfile:
    profile_commitment: str
    profile_identity: str
    default_enabled: bool
    authorization: AuthorizationRequirements
    settings: bytes

    def values(self) -> dict:
        return json.loads(self.settings)


def load_docker_gate_profile(path: str | Path) -> DockerGateProfile:
    root, digest = _load_yaml(path)
    _keys(root, {"version", "gate", "profile_identity", "default_enabled", "authorization", "docker"}, "Docker profile")
    if root["version"] != "1.0" or root["gate"] != GateKind.DOCKER_RCA_E2E.value or root["default_enabled"] is not False:
        raise ValueError("Docker profile must be version 1.0 and default off")
    docker = _keys(_mapping(root["docker"], "docker"), {
        "compose_file", "runtime_service", "runtime_dockerfile", "runtime_entrypoint",
        "runtime_config", "compose_project_prefix", "volume_namespace_prefix",
        "required_volume_roles", "forbidden_shared_names", "cleanup_requires_exact_ownership",
    }, "docker")
    if docker["cleanup_requires_exact_ownership"] is not True:
        raise ValueError("Docker cleanup must require exact ownership")
    for key in ("required_volume_roles", "forbidden_shared_names"):
        if not isinstance(docker[key], list) or not docker[key]:
            raise ValueError(f"{key} must be non-empty")
    return DockerGateProfile(digest, _text(root["profile_identity"], "profile identity"), False,
                             _authorization(root["authorization"], credential=False),
                             canonical_bytes(docker))


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    expected_authorization_id: str | None
    credential_ready: bool = False


@dataclass(frozen=True, slots=True)
class PrerequisiteCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class RealRagBuildContext:
    active_build_identity: str
    activation_generation: int
    activation_operation_identity: str
    validated_build_commitment: str
    manifest_identity: str
    manifest_commitment: str
    corpus_identity: str
    corpus_version: str
    embedding_provider: str
    embedding_model: str
    embedding_profile_identity: str
    embedding_dimension: int
    index_engine: str
    index_schema_identity: str
    artifact_commitment: str
    lineage_commitment: str
    staged_commitment: str

    def pins(self) -> dict[str, object]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class GateReadiness:
    gate: GateKind
    status: GateExecutionStatus
    profile_commitment: str
    profile_identity: str
    authorization_id: str | None
    authorization_commitment: str | None
    authorization_state: str
    checks: tuple[PrerequisiteCheck, ...]
    evaluation_run_id: str
    execution_id: str
    real_rag_build_context: RealRagBuildContext | None = None


def _status(gate: GateKind, profile_identity: str, profile_commitment: str,
            authorization: AuthorizationRequest,
            authority: EvaluationLedgerReader,
            checks: tuple[PrerequisiteCheck, ...], *, evaluation_run_id: str,
            execution_id: str,
            real_rag_build_context: RealRagBuildContext | None = None,
            ) -> GateReadiness:
    require_identity(evaluation_run_id, "evaluation_run")
    require_identity(execution_id, "execution")
    record = (read_gate_authorization(
        authority, authorization.expected_authorization_id)
              if authorization.expected_authorization_id else None)
    if record is not None and (
        record.evaluation_run_id, record.gate, record.profile_identity,
        record.profile_commitment
    ) != (evaluation_run_id, gate.value, profile_identity, profile_commitment):
        record = None
    if record is None:
        status = GateExecutionStatus.NOT_AUTHORIZED
        state = "NOT_AUTHORIZED"
    elif not all(item.passed for item in checks):
        status = GateExecutionStatus.NOT_READY
        state = "AUTHORIZED"
    else:
        status = GateExecutionStatus.READY_NOT_EXECUTED
        state = "AUTHORIZED"
    return GateReadiness(
        gate, status, profile_commitment, profile_identity,
        record.authorization_id if record else None,
        record.authorization_commitment if record else None,
        state, checks, evaluation_run_id, execution_id,
        real_rag_build_context,
    )


@dataclass(frozen=True, slots=True)
class RealLlmFacts:
    authorization: AuthorizationRequest
    required_profile_available: bool


def evaluate_real_llm(profile: RealLlmProfile, facts: RealLlmFacts,
                      *, repository_root: str | Path,
                      authority: EvaluationLedgerReader,
                      evaluation_run_id: str, execution_id: str) -> GateReadiness:
    config_path = Path(repository_root) / profile.config_path
    checks = [PrerequisiteCheck("credential_ready", facts.authorization.credential_ready,
                                "credential availability only; value not captured"),
              PrerequisiteCheck("required_profile_available", facts.required_profile_available,
                                "existing Gemini adapter/profile")]
    try:
        config = load_generation_config(config_path)
        pin = config.pin
        matches = (
            pin.provider == profile.provider and pin.model == profile.model
            and pin.profile.identity == profile.generation_profile_identity
            and pin.prompt.identity == profile.prompt_identity
            and pin.result_schema.identity == profile.result_schema_identity
            and pin.configuration.identity == profile.configuration_identity
            and config.bounds.timeout_seconds == profile.timeout_seconds
            and config.bounds.maximum_invocations == profile.maximum_invocations
        )
        checks.append(PrerequisiteCheck("governed_generation_profile", matches,
                                        "existing Candidate-D configuration pins"))
    except (OSError, ValueError):
        checks.append(PrerequisiteCheck("governed_generation_profile", False,
                                        "generation configuration unavailable"))
    return _status(GateKind.REAL_LLM, profile.profile_identity,
                   profile.profile_commitment, facts.authorization,
                   authority, tuple(checks), evaluation_run_id=evaluation_run_id,
                   execution_id=execution_id)


@dataclass(frozen=True, slots=True)
class RealRagFacts:
    authorization: AuthorizationRequest
    authority_state: str | None
    active_build_identity: str | None
    chroma_index_available: bool
    public_retrieval_available: bool
    actual_manifest_commitment: str | None
    actual_embedding_profile_identity: str | None
    actual_index_schema_identity: str | None
    chroma_path: str | None
    real_embedding_and_index: bool
    candidate_c_authority: object | None = None


def _read_real_rag_build_context(
    candidate_c: object, config: object,
) -> RealRagBuildContext | None:
    try:
        activation_read = candidate_c.read_activation()
        readiness = candidate_c.local_readiness(
            required_profile_reference=config.capability.profile_reference,
            required_capability_identity=config.capability.capability_identity,
        )
        if (activation_read.status is not KnowledgeReadStatus.FOUND
                or type(activation_read.value) is not ActivationAuthorityRecord
                or readiness.status is not KnowledgeLocalReadiness.READY):
            return None
        activation = activation_read.value
        if ((readiness.active_build_identity, readiness.generation) !=
                (activation.active_build_identity, activation.generation)):
            return None
        staged_read = candidate_c.get_staged_build(
            activation.active_build_identity)
        if (staged_read.status is not KnowledgeReadStatus.FOUND
                or type(staged_read.value) is not StagedBuildRecord):
            return None
        staged = staged_read.value
        manifest = staged.manifest_provenance
        build_input = staged.build_input
        artifact = staged.artifact
        if manifest is None:
            return None
        return RealRagBuildContext(
            activation.active_build_identity,
            activation.generation,
            activation.operation_key.value,
            activation.validated_build_commitment,
            manifest.manifest_identity,
            manifest.manifest_commitment,
            manifest.corpus_identity,
            manifest.corpus_version,
            build_input.embedding_provider,
            build_input.embedding_model,
            build_input.embedding_profile_identity,
            build_input.embedding_dimension,
            build_input.index_engine,
            build_input.index_schema_identity,
            artifact.artifact_commitment,
            staged.lineage_commitment,
            staged.staged_commitment,
        )
    except (AttributeError, TypeError, ValueError):
        return None


def evaluate_real_rag(profile: RealRagProfile, facts: RealRagFacts,
                      *, repository_root: str | Path,
                      authority: EvaluationLedgerReader,
                      evaluation_run_id: str, execution_id: str) -> GateReadiness:
    values = profile.values()
    root = Path(repository_root).resolve()
    config = None
    try:
        config = load_knowledge_config(root / values["config_path"])
        admission = admit_production_manifest(
            root / values["manifest_path"], repository_root=root)
        governed = (
            admission.accepted and admission.manifest_commitment == values["manifest_commitment"]
            and admission.manifest.canonical_release_reference == values["release_reference"]
            and values["embedding_adapter"] ==
                "knowledge_index.google_embedding_adapter.GoogleEmbeddingAdapter"
            and config.capability.provider == values["embedding_provider"]
            and config.capability.model == values["embedding_model"]
            and config.capability.profile_reference.value == values["embedding_profile_reference"]
            and config.capability.embedding_profile_identity == values["embedding_profile_identity"]
            and values["index_engine"] == "chromadb"
            and config.index_schema_identity == values["index_schema_identity"]
            and config.retrieval_profile.profile_identity == values["retrieval_profile_identity"]
            and values["active_or_lkg_required"] is True
            and values["public_retrieval_required"] is True
        )
    except (OSError, ValueError):
        governed = False
    build_context = (_read_real_rag_build_context(
        facts.candidate_c_authority, config)
        if facts.candidate_c_authority is not None and config is not None
        else None)
    exact_build = bool(build_context and (
        build_context.active_build_identity == facts.active_build_identity
        and build_context.manifest_commitment == values["manifest_commitment"]
        and build_context.embedding_provider == values["embedding_provider"]
        and build_context.embedding_model == values["embedding_model"]
        and build_context.embedding_profile_identity ==
            values["embedding_profile_identity"]
        and build_context.index_engine == values["index_engine"]
        and build_context.index_schema_identity == values["index_schema_identity"]
    ))
    isolated = False
    if facts.chroma_path is not None:
        try:
            raw_candidate = Path(facts.chroma_path)
            if ".." in raw_candidate.parts:
                raise ValueError("Chroma traversal is forbidden")
            evaluation_base = (root / "var" / "rca_evaluation").resolve()
            isolated_root = (root / values["isolated_chroma_root"]).resolve()
            if config is None:
                raise ValueError("governed Knowledge config unavailable")
            production_index = (root / config.chroma_path).resolve()
            candidate = (raw_candidate if raw_candidate.is_absolute()
                         else root / raw_candidate).resolve()
            isolated_root.relative_to(evaluation_base)
            candidate.relative_to(isolated_root)
            isolated = candidate != isolated_root and candidate != production_index
        except (OSError, RuntimeError, ValueError):
            isolated = False
    checks = (
        PrerequisiteCheck("credential_ready", facts.authorization.credential_ready,
                          "credential availability only; value not captured"),
        PrerequisiteCheck("governed_manifest_and_profile", governed and
                          facts.actual_manifest_commitment == values["manifest_commitment"] and
                          facts.actual_embedding_profile_identity == values["embedding_profile_identity"] and
                          facts.actual_index_schema_identity == values["index_schema_identity"],
                          "SPEC-014 pinned manifest/profile/index"),
        PrerequisiteCheck("active_or_lkg", facts.authority_state in {"ACTIVE", "LKG"}
                          and exact_build,
                          "exact Candidate-C activation/build authority"),
        PrerequisiteCheck("chroma_index", facts.chroma_index_available,
                          "isolated Chroma artifact available"),
        PrerequisiteCheck("public_retrieval", facts.public_retrieval_available,
                          "Candidate-C public retrieval path"),
        PrerequisiteCheck("real_not_fake", facts.real_embedding_and_index,
                          "real embedding and Chroma provenance"),
        PrerequisiteCheck("isolated_chroma", isolated,
                          "evaluation-owned non-default Chroma path"),
    )
    return _status(GateKind.REAL_RAG, profile.profile_identity,
                   profile.profile_commitment, facts.authorization,
                   authority, checks, evaluation_run_id=evaluation_run_id,
                   execution_id=execution_id,
                   real_rag_build_context=build_context if exact_build else None)


def _evidence_id(payload: dict) -> str:
    return "eval:gate_evidence:" + hashlib.sha256(canonical_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class GateEvidence:
    evidence_id: str
    gate: GateKind
    status: GateExecutionStatus
    canonical_json: bytes
    evidence_commitment: str


def _assert_evidence_references(references: Mapping[str, object]) -> None:
    for key in references:
        normalized = _normalized_key(key)
        if (normalized not in {"gate", "bounded"}
                and not normalized.endswith(("reference", "identity", "identities", "commitment"))):
            raise ValueError("unknown evidence reference field is not allowlisted")
    assert_secret_free(references, path="references")


def capture_gate_evidence(
    readiness: GateReadiness, *, repository_commit: str, environment_identity: str,
    references: Mapping[str, object], execution_receipt_id: str | None = None,
    actual_invocation_receipt_id: str | None = None,
    authority: EvaluationLedgerReader | None = None,
) -> GateEvidence:
    if execution_receipt_id is not None and actual_invocation_receipt_id is not None:
        raise ValueError("upstream-only and actual invocation receipts are mutually exclusive")
    receipt = None
    actual_receipt = None
    status = readiness.status
    if execution_receipt_id is not None:
        if authority is None:
            raise ValueError("single Candidate-F evaluation store is required")
        receipt = read_gate_execution_receipt(authority, execution_receipt_id)
        if receipt is None:
            raise ValueError("execution receipt is absent from Candidate-F ledger")
        if (readiness.status is not GateExecutionStatus.READY_NOT_EXECUTED
                or (receipt.evaluation_run_id, receipt.execution_id) != (
                    readiness.evaluation_run_id, readiness.execution_id)
                or (receipt.gate, receipt.profile_identity,
                    receipt.profile_commitment) != (
                    readiness.gate.value, readiness.profile_identity,
                    readiness.profile_commitment)):
            raise ValueError("execution receipt does not bind exact run/execution/gate/profile")
        # R1 records only an upstream public result observation.  It is not
        # proof of a real-provider/RAG/Docker invocation, so deterministic
        # evidence cannot be promoted beyond READY_NOT_EXECUTED.  R3 owns the
        # actual invocation provenance and execution disposition.
        status = GateExecutionStatus.READY_NOT_EXECUTED
    if actual_invocation_receipt_id is not None:
        if authority is None:
            raise ValueError("single Candidate-F evaluation store is required")
        # Local import avoids a module cycle: the R3 harness reuses these gate
        # profile/readiness contracts.
        from .real_gate_provenance import read_actual_invocation_receipt
        actual_receipt = read_actual_invocation_receipt(
            authority, actual_invocation_receipt_id)
        if actual_receipt is None:
            raise ValueError("actual invocation receipt is absent from Candidate-F ledger")
        if (readiness.status is not GateExecutionStatus.READY_NOT_EXECUTED
                or (actual_receipt.evaluation_run_id,
                    actual_receipt.execution_id) != (
                    readiness.evaluation_run_id, readiness.execution_id)
                or (actual_receipt.gate, actual_receipt.profile_identity,
                    actual_receipt.profile_commitment) != (
                    readiness.gate.value, readiness.profile_identity,
                    readiness.profile_commitment)):
            raise ValueError("actual receipt does not bind exact run/execution/gate/profile")
        if (readiness.gate is GateKind.REAL_RAG
                and (readiness.real_rag_build_context is None
                     or actual_receipt.invocation_pins.get(
                         "expected_build_context") !=
                     readiness.real_rag_build_context.pins())):
            raise ValueError(
                "actual REAL_RAG receipt does not bind readiness build context"
            )
        status = actual_receipt.status
    _assert_evidence_references(references)
    payload = {
        "gate": readiness.gate.value, "status": status.value,
        "profile_identity": readiness.profile_identity,
        "profile_commitment": readiness.profile_commitment,
        "evaluation_run_id": readiness.evaluation_run_id,
        "execution_id": readiness.execution_id,
        "repository_commit": _text(repository_commit, "repository commit"),
        "environment_identity": _text(environment_identity, "environment identity"),
        "authorization_state": readiness.authorization_state,
        "authorization_identity": readiness.authorization_id,
        "authorization_commitment": readiness.authorization_commitment,
        "readiness_checks": [
            {"name": item.name, "passed": item.passed, "detail": item.detail}
            for item in readiness.checks
        ],
        "references": dict(references),
        "execution_receipt": ({
            "receipt_id": receipt.receipt_id,
            "evaluation_run_id": receipt.evaluation_run_id,
            "execution_id": receipt.execution_id,
            "harness_invocation_identity": receipt.harness_invocation_identity,
            "upstream_result_reference": receipt.upstream_result_reference,
            "upstream_result_commitment": receipt.upstream_result_commitment,
            "authoritative_typed_outcome": receipt.authoritative_typed_outcome,
            "started_at": receipt.external_started_at,
            "ended_at": receipt.external_ended_at,
            "sanitized_provenance": receipt.sanitized_provenance,
            "receipt_commitment": receipt.receipt_commitment,
        } if receipt else None),
        "actual_invocation_receipt": ({
            "receipt_id": actual_receipt.receipt_id,
            "start_id": actual_receipt.start_id,
            "invocation_id": actual_receipt.invocation_id,
            "started_at": actual_receipt.started_at,
            "ended_at": actual_receipt.ended_at,
            "status": actual_receipt.status.value,
            "provenance_kind": actual_receipt.provenance_kind,
            "invocation_pins": actual_receipt.invocation_pins,
            "upstream_result_reference": actual_receipt.upstream_result_reference,
            "upstream_result_commitment": actual_receipt.upstream_result_commitment,
            "authoritative_typed_outcome": actual_receipt.authoritative_typed_outcome,
            "sanitized_provenance": actual_receipt.sanitized_provenance,
            "receipt_commitment": actual_receipt.receipt_commitment,
        } if actual_receipt else None),
    }
    assert_secret_free(payload)
    frozen = canonical_bytes(payload)
    return GateEvidence(_evidence_id(payload), readiness.gate, status, frozen,
                        commitment(payload))
