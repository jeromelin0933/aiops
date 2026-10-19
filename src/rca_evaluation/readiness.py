"""Secret-free, deterministic SPEC-017 external-gate readiness facts.

This module prepares facts only.  It never invokes Gemini, embeds content,
builds or activates an index, or promotes a gate to an executed result.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Mapping
import copy

from knowledge_index import (
    KnowledgeLocalReadiness, KnowledgeReadStatus, admit_production_manifest,
    load_knowledge_config,
)

from .identity import commitment, identity, require_identity
from .ledger import LedgerConflict, LedgerRecord
from .sqlite_store import SqliteEvaluationStore


class CredentialState(str, Enum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    INVALID = "INVALID"


class ReadinessFactState(str, Enum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    CONTRADICTORY = "CONTRADICTORY"


@dataclass(frozen=True, slots=True)
class CredentialReadiness:
    credential_reference: str
    credential_state: CredentialState
    validation_method: str
    validated_at: str
    environment_identity: str
    profile_identity: str
    reason: str

    def __post_init__(self) -> None:
        values = (
            self.credential_reference, self.validation_method, self.validated_at,
            self.environment_identity, self.profile_identity, self.reason,
        )
        if any(not isinstance(item, str) or not item or item != item.strip()
               for item in values):
            raise ValueError("credential readiness contains an invalid sanitized fact")
        if len(self.reason) > 160 or any(ord(ch) < 32 for ch in self.reason):
            raise ValueError("credential readiness reason is not bounded sanitized text")

    def material(self) -> dict[str, str]:
        return {
            "credential_reference": self.credential_reference,
            "credential_state": self.credential_state.value,
            "validation_method": self.validation_method,
            "validated_at": self.validated_at,
            "environment_identity": self.environment_identity,
            "profile_identity": self.profile_identity,
            "reason": self.reason,
        }


def resolve_environment_credential(
    *, credential_environment: str, required_profile_identity: str,
    profile_identity: str, validated_at: str, environment_identity: str,
    environment: Mapping[str, str] | None = None,
) -> CredentialReadiness:
    """Inspect presence/shape without retaining, hashing, logging, or returning the value."""
    if (not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", credential_environment)
            or profile_identity != required_profile_identity):
        state = CredentialState.INVALID
        reason = "credential reference or profile does not match the approved profile"
    else:
        source = os.environ if environment is None else environment
        raw = source.get(credential_environment)
        if raw is None or raw == "":
            state = CredentialState.MISSING
            reason = "credential is absent from the evaluation environment"
        elif (not isinstance(raw, str) or raw != raw.strip()
              or any(ord(ch) < 33 or ord(ch) == 127 for ch in raw)):
            state = CredentialState.INVALID
            reason = "credential is structurally invalid for later invocation"
        else:
            state = CredentialState.AVAILABLE
            reason = "credential is structurally present; provider validity is not asserted"
    return CredentialReadiness(
        f"env:{credential_environment}", state,
        "ENVIRONMENT_STRUCTURAL_PRESENCE_v1", validated_at,
        environment_identity, required_profile_identity, reason,
    )


@dataclass(frozen=True, slots=True)
class RequiredFact:
    name: str
    state: ReadinessFactState
    value: object | None
    authority: str
    reason: str

    def __post_init__(self) -> None:
        if not self.name or not self.authority or not self.reason:
            raise ValueError("required fact metadata is incomplete")
        if self.state is not ReadinessFactState.RESOLVED and self.value is not None:
            raise ValueError("unresolved/contradictory facts cannot carry a frozen value")

    def material(self) -> dict[str, object]:
        return {"name": self.name, "state": self.state.value,
                "value": self.value, "authority": self.authority,
                "reason": self.reason}


def _package_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError as exc:
        raise ValueError(f"required package is unavailable: {distribution}") from exc


def resolve_repository_identity(repository_root: str | Path) -> str:
    """Resolve the public Git identity without accepting a caller assertion."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(repository_root).resolve()), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("repository identity is unavailable") from exc
    revision = completed.stdout.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("repository identity is invalid")
    return f"git:{revision}"


@dataclass(frozen=True, slots=True)
class RealLlmReadinessCommitment:
    payload: dict[str, object]
    readiness_commitment: str

    @classmethod
    def create(cls, profile: object, config: object,
               credential: CredentialReadiness,
               *, repository_identity: str) -> "RealLlmReadinessCommitment":
        pin = config.pin
        bounds = config.bounds
        required = {
            "provider": profile.provider, "model": profile.model,
            "generation_profile_identity": profile.generation_profile_identity,
            "prompt_identity": profile.prompt_identity,
            "prompt_version": pin.prompt.version,
            "result_schema_identity": profile.result_schema_identity,
            "result_schema_version": pin.result_schema.version,
            "configuration_identity": profile.configuration_identity,
            "configuration_version": pin.configuration.version,
            "configuration_commitment": config.configuration_commitment,
            "timeout_seconds": bounds.timeout_seconds,
            "maximum_invocations": bounds.maximum_invocations,
            "hidden_retry_policy": ("DISABLED" if config.hidden_retries_disabled
                                    else "ENABLED"),
            "adapter_identity": profile.adapter,
            "credential_readiness": credential.material(),
            "repository_identity": repository_identity,
            "profile_identity": profile.profile_identity,
            "profile_commitment": profile.profile_commitment,
            "google_genai_version": _package_version("google-genai"),
        }
        for key, value in required.items():
            if value is None or value == "":
                raise ValueError(f"real LLM readiness fact is missing: {key}")
        if (pin.provider != profile.provider or pin.model != profile.model
                or pin.profile.identity != profile.generation_profile_identity
                or pin.prompt.identity != profile.prompt_identity
                or pin.result_schema.identity != profile.result_schema_identity
                or pin.configuration.identity != profile.configuration_identity
                or bounds.timeout_seconds != profile.timeout_seconds
                or bounds.maximum_invocations != profile.maximum_invocations
                or not config.hidden_retries_disabled):
            raise ValueError("real LLM profile contradicts Candidate-D configuration")
        return cls(required, commitment(required))


@dataclass(frozen=True, slots=True)
class RagChildAllocation:
    allocation_record_id: str
    evaluation_run_id: str
    execution_id: str
    environment_identity: str
    profile_identity: str
    child_identity: str
    relative_path: str
    ownership_commitment: str
    readiness_state: ReadinessFactState

    def material(self) -> dict[str, object]:
        return {name: (value.value if isinstance(value, Enum) else value)
                for name, value in ((field, getattr(self, field))
                                    for field in self.__dataclass_fields__)}


_OWNERSHIP_FILE = ".candidate-f-ownership.json"


def allocate_real_rag_child(
    store: SqliteEvaluationStore, *, repository_root: str | Path,
    isolated_root: str, production_root: str, evaluation_run_id: str,
    execution_id: str, environment_identity: str, profile_identity: str,
) -> RagChildAllocation:
    """Durably issue one evaluation-owned child; equivalent replay is idempotent."""
    require_identity(evaluation_run_id, "evaluation_run")
    require_identity(execution_id, "execution")
    execution = store.get(execution_id)
    if execution is None or execution.parent_id != evaluation_run_id:
        raise ValueError("RAG child execution does not belong to the evaluation run")
    repo = Path(repository_root).resolve()
    approved = (repo / isolated_root).resolve()
    evaluation_base = (repo / "var" / "rca_evaluation").resolve()
    production = (repo / production_root).resolve()
    if approved == production or approved.is_relative_to(production):
        raise ValueError("evaluation root overlaps the production Candidate-C root")
    try:
        approved.relative_to(evaluation_base)
    except ValueError as exc:
        raise ValueError("isolated RAG root escapes the approved evaluation root") from exc
    child_identity = "ragchild_" + commitment({
        "run": evaluation_run_id, "execution": execution_id,
        "environment": environment_identity, "profile": profile_identity,
    })[7:]
    child = (approved / child_identity).resolve()
    child.relative_to(approved)
    if child == production or child.is_relative_to(production):
        raise ValueError("production Candidate-C root cannot be an evaluation child")
    relative = child.relative_to(repo).as_posix()
    owner = {
        "schema": "spec017.real-rag-child-ownership.v1",
        "evaluation_run_id": evaluation_run_id, "execution_id": execution_id,
        "environment_identity": environment_identity,
        "profile_identity": profile_identity, "child_identity": child_identity,
        "relative_path": relative, "lifecycle_state": "ALLOCATED",
    }
    owner_commitment = commitment(owner)
    record = LedgerRecord.create(
        "rag_child_allocation",
        [execution_id, "REAL_RAG_CHILD_ALLOCATION", profile_identity],
        {"kind": "REAL_RAG_CHILD_ALLOCATION", **owner,
         "ownership_commitment": owner_commitment}, execution_id)
    existing = store.get(record.record_id)
    marker = child / _OWNERSHIP_FILE
    if existing is not None:
        if existing.semantic_commitment != record.semantic_commitment:
            raise LedgerConflict("contradictory RAG child ownership")
        if not marker.is_file():
            raise ValueError("allocated RAG child requires recovery; ownership marker is absent")
        try:
            persisted = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("RAG child ownership marker is invalid") from exc
        if persisted != owner or persisted.get("lifecycle_state") != "ALLOCATED":
            raise ValueError("RAG child ownership is contradictory or stale")
    else:
        if child.exists():
            raise ValueError("RAG child path collision or stale state detected")
        approved.mkdir(parents=True, exist_ok=True)
        child.mkdir()
        try:
            marker.write_text(json.dumps(owner, sort_keys=True, separators=(",", ":")),
                              encoding="utf-8")
            store._append_rag_child_allocation(record)
        except BaseException:
            # Do not delete: an interrupted allocation is explicit recovery state.
            raise
    return RagChildAllocation(
        record.record_id, evaluation_run_id, execution_id,
        environment_identity, profile_identity, child_identity, relative,
        owner_commitment, ReadinessFactState.RESOLVED)


def verify_real_rag_child_allocation(
    reference: RagChildAllocation, *, authority: object,
    repository_root: str | Path, isolated_root: str, production_root: str,
    evaluation_run_id: str, execution_id: str, profile_identity: str,
) -> RagChildAllocation:
    """Fresh-read and verify an exact Candidate-F durable child allocation.

    ``reference`` is only a lookup/reference DTO.  The ledger record is the
    authority; the ownership marker is an integrity guard over that record.
    """
    if type(reference) is not RagChildAllocation:
        raise TypeError("RAG child allocation reference is required")
    try:
        authority.readiness_check()
        record = authority.get(reference.allocation_record_id)
        if record is not None:
            record.validate()
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ValueError("Candidate-F allocation authority is unavailable") from exc
    if record is None or record.domain != "rag_child_allocation":
        raise ValueError("durable RAG child allocation does not exist")
    if record.record_id != reference.allocation_record_id:
        raise ValueError("RAG child allocation record identity mismatch")
    try:
        payload = json.loads(record.semantic_json)
        identity_key = json.loads(record.identity_key_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("durable RAG child allocation is corrupt") from exc
    expected_fields = {
        "kind", "schema", "evaluation_run_id", "execution_id",
        "environment_identity", "profile_identity", "child_identity",
        "relative_path", "lifecycle_state", "ownership_commitment",
    }
    if (set(payload) != expected_fields
            or payload.get("kind") != "REAL_RAG_CHILD_ALLOCATION"
            or payload.get("schema") != "spec017.real-rag-child-ownership.v1"
            or payload.get("lifecycle_state") != "ALLOCATED"
            or record.parent_id != execution_id
            or identity_key != [execution_id, "REAL_RAG_CHILD_ALLOCATION",
                                profile_identity]):
        raise ValueError("durable RAG child allocation type or lifecycle mismatch")
    execution = authority.get(execution_id)
    run = authority.get(evaluation_run_id)
    if (execution is None or execution.domain != "execution"
            or execution.parent_id != evaluation_run_id
            or run is None or run.domain != "evaluation_run"):
        raise ValueError("RAG child allocation run/execution lineage mismatch")
    owner = {key: payload[key] for key in expected_fields
             if key not in {"kind", "ownership_commitment"}}
    owner_commitment = commitment(owner)
    if payload.get("ownership_commitment") != owner_commitment:
        raise ValueError("durable RAG child ownership payload mismatch")
    verified = RagChildAllocation(
        record.record_id, payload["evaluation_run_id"], payload["execution_id"],
        payload["environment_identity"], payload["profile_identity"],
        payload["child_identity"], payload["relative_path"], owner_commitment,
        ReadinessFactState.RESOLVED,
    )
    if (verified != reference
            or verified.evaluation_run_id != evaluation_run_id
            or verified.execution_id != execution_id
            or verified.profile_identity != profile_identity):
        raise ValueError("RAG child allocation reference contradicts durable authority")

    repo = Path(repository_root).resolve()
    evaluation_base = (repo / "var" / "rca_evaluation").resolve()
    approved = (repo / isolated_root).resolve()
    production = (repo / production_root).resolve()
    raw_relative = Path(verified.relative_path)
    if raw_relative.is_absolute() or ".." in raw_relative.parts:
        raise ValueError("RAG child allocation path is invalid")
    child = (repo / raw_relative).resolve()
    try:
        approved.relative_to(evaluation_base)
        child.relative_to(approved)
    except ValueError as exc:
        raise ValueError("RAG child allocation escapes the approved evaluation root") from exc
    if (child == approved or approved == production
            or approved.is_relative_to(production)
            or child == production or child.is_relative_to(production)):
        raise ValueError("production Candidate-C root cannot be an evaluation child")
    marker = child / _OWNERSHIP_FILE
    if not marker.is_file():
        raise ValueError("RAG child ownership marker is absent")
    try:
        marker_owner = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("RAG child ownership marker is corrupt") from exc
    if marker_owner != owner:
        raise ValueError("RAG child ownership marker mismatches durable authority")
    if marker_owner.get("lifecycle_state") != "ALLOCATED":
        raise ValueError("RAG child ownership marker lifecycle is stale")
    return verified


def verify_supported_real_rag_child_allocation(
    reference: RagChildAllocation, *, evaluation_store: SqliteEvaluationStore,
    repository_root: str | Path, isolated_root: str, production_root: str,
    evaluation_run_id: str, execution_id: str, profile_identity: str,
) -> RagChildAllocation:
    """Fresh-read allocation authority for supported Formal/Real-RAG composition."""
    try:
        reader = evaluation_store.reader()
    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
        raise ValueError("Candidate-F allocation authority is unavailable") from exc
    return verify_real_rag_child_allocation(
        reference, authority=reader,
        repository_root=repository_root, isolated_root=isolated_root,
        production_root=production_root, evaluation_run_id=evaluation_run_id,
        execution_id=execution_id, profile_identity=profile_identity,
    )


@dataclass(frozen=True, slots=True)
class RealRagReadinessCommitment:
    payload: dict[str, object]
    readiness_commitment: str
    build_fact: RequiredFact
    activation_fact: RequiredFact

    @property
    def ready(self) -> bool:
        credential = self.payload.get("credential_readiness", {})
        return (isinstance(credential, dict)
                and credential.get("credential_state") == CredentialState.AVAILABLE.value
                and self.build_fact.state is ReadinessFactState.RESOLVED
                and self.activation_fact.state is ReadinessFactState.RESOLVED)


def create_real_rag_readiness_commitment(
    profile: object, config: object, manifest: object,
    allocation: RagChildAllocation, *, repository_identity: str,
    build_fact: RequiredFact, activation_fact: RequiredFact,
    credential: CredentialReadiness,
) -> RealRagReadinessCommitment:
    values = profile.values()
    manifest_value = manifest.manifest
    payload = {
        "corpus_identity": manifest_value.corpus_id,
        "corpus_version": manifest_value.corpus_version,
        "corpus_release": manifest_value.canonical_release_reference,
        "release_approval_reference": manifest_value.release_approval_reference,
        "source_revision": manifest_value.source_revision,
        "knowledge_manifest_commitment": manifest.manifest_commitment,
        "embedding_provider": config.capability.provider,
        "embedding_model": config.capability.model,
        "embedding_profile_identity": config.capability.embedding_profile_identity,
        "embedding_profile_reference": config.capability.profile_reference.value,
        "index_engine": values["index_engine"],
        "index_schema_identity": config.index_schema_identity,
        "distance_metric": config.distance_metric,
        "retrieval_profile_identity": config.retrieval_profile.profile_identity,
        "applicability_profile_identity": config.applicability_policy.policy_identity,
        "isolated_evaluation_root": values["isolated_chroma_root"],
        "child_identity": allocation.child_identity,
        "child_ownership_commitment": allocation.ownership_commitment,
        "credential_readiness": credential.material(),
        "expected_build": build_fact.material(),
        "activation_lineage": activation_fact.material(),
        "repository_identity": repository_identity,
        "profile_identity": profile.profile_identity,
        "profile_commitment": profile.profile_commitment,
        "chromadb_version": _package_version("chromadb"),
        "google_genai_version": _package_version("google-genai"),
    }
    if (values["manifest_commitment"] != manifest.manifest_commitment
            or values["release_reference"] != manifest_value.canonical_release_reference
            or values["embedding_provider"] != config.capability.provider
            or values["embedding_model"] != config.capability.model
            or values["embedding_profile_identity"] != config.capability.embedding_profile_identity
            or values["index_schema_identity"] != config.index_schema_identity
            or values["retrieval_profile_identity"] != config.retrieval_profile.profile_identity
            or values["distance_metric"] != config.distance_metric
            or config.distance_metric != "l2"
            or credential.credential_reference
                != f"env:{profile.authorization.credential_environment}"
            or credential.profile_identity != profile.profile_identity):
        raise ValueError("real RAG profile contradicts Candidate-C public configuration")
    return RealRagReadinessCommitment(
        payload, commitment(payload), build_fact, activation_fact)


def resolve_candidate_c_build(
    candidate_c: object | None, config: object, *,
    expected_manifest_commitment: str | None = None,
    expected_corpus_identity: str | None = None,
    expected_corpus_version: str | None = None,
) -> tuple[RequiredFact, RequiredFact]:
    """Resolve only Candidate-C public projections; private SQLite is never read."""
    if candidate_c is None:
        return (
            RequiredFact("real_rag_build", ReadinessFactState.UNRESOLVED, None,
                         "Candidate-C public API", "validated real build is absent"),
            RequiredFact("real_rag_activation", ReadinessFactState.UNRESOLVED, None,
                         "Candidate-C public API", "activation lineage is absent"),
        )
    try:
        activation = candidate_c.read_activation()
        readiness = candidate_c.local_readiness(
            required_profile_reference=config.capability.profile_reference,
            required_capability_identity=config.capability.capability_identity)
        if activation.status is not KnowledgeReadStatus.FOUND:
            return resolve_candidate_c_build(None, config)
        active = activation.value
        if (readiness.status is not KnowledgeLocalReadiness.READY
                or readiness.active_build_identity != active.active_build_identity
                or readiness.generation != active.generation):
            contradictory = RequiredFact(
                "real_rag_build", ReadinessFactState.CONTRADICTORY, None,
                "Candidate-C public API", "public readiness and activation contradict")
            return contradictory, RequiredFact(
                "real_rag_activation", ReadinessFactState.CONTRADICTORY, None,
                "Candidate-C public API", "public activation lineage contradicts readiness")
        staged = candidate_c.get_staged_build(active.active_build_identity)
        if staged.status is not KnowledgeReadStatus.FOUND:
            return resolve_candidate_c_build(None, config)
        value = staged.value
        manifest = value.manifest_provenance
        if (manifest is None
                or value.build_input.distance_metric != "l2"
                or value.artifact.distance_metric != "l2"
                or value.build_input.index_schema_identity != config.index_schema_identity
                or value.build_input.embedding_profile_identity
                    != config.capability.embedding_profile_identity
                or value.build_input.embedding_provider != config.capability.provider
                or value.build_input.embedding_model != config.capability.model
                or (expected_manifest_commitment is not None
                    and manifest.manifest_commitment != expected_manifest_commitment)
                or (expected_corpus_identity is not None
                    and manifest.corpus_identity != expected_corpus_identity)
                or (expected_corpus_version is not None
                    and manifest.corpus_version != expected_corpus_version)):
            contradictory = RequiredFact(
                "real_rag_build", ReadinessFactState.CONTRADICTORY, None,
                "Candidate-C public API", "public build profile contradicts approved L2 facts")
            return contradictory, RequiredFact(
                "real_rag_activation", ReadinessFactState.CONTRADICTORY, None,
                "Candidate-C public API", "activation references a contradictory build")
        return (
            RequiredFact("real_rag_build", ReadinessFactState.RESOLVED,
                         active.active_build_identity, "Candidate-C public API",
                         "validated public build facts match"),
            RequiredFact("real_rag_activation", ReadinessFactState.RESOLVED,
                         {"generation": active.generation,
                          "operation_identity": active.operation_key.value,
                          "validated_build_commitment": active.validated_build_commitment},
                         "Candidate-C public API", "public activation lineage matches"),
        )
    except (AttributeError, TypeError, ValueError):
        contradictory = RequiredFact(
            "real_rag_build", ReadinessFactState.CONTRADICTORY, None,
            "Candidate-C public API", "public build facts are malformed")
        return contradictory, RequiredFact(
            "real_rag_activation", ReadinessFactState.CONTRADICTORY, None,
            "Candidate-C public API", "public activation facts are malformed")


@dataclass(frozen=True, slots=True)
class RequiredFactsMaterialization:
    """Typed replacements for exact JSON-pointer locations in pre-registration."""

    facts: tuple[tuple[str, RequiredFact], ...]
    materialization_commitment: str

    @classmethod
    def create(cls, facts: Mapping[str, RequiredFact]) -> "RequiredFactsMaterialization":
        if not facts or any(not isinstance(path, str) or not path.startswith("/")
                            or type(fact) is not RequiredFact
                            for path, fact in facts.items()):
            raise ValueError("materialization requires typed facts at JSON pointers")
        ordered = tuple(sorted(facts.items()))
        payload = [{"path": path, "fact": fact.material()} for path, fact in ordered]
        return cls(ordered, commitment(payload))


@dataclass(frozen=True, slots=True)
class FormalRequiredFactsResolution:
    """One allowlisted resolution cut used by supported Formal admission."""

    materialization: RequiredFactsMaterialization
    prerequisites: tuple[RequiredFact, ...]

    def assert_resolved(self) -> None:
        for fact in self.prerequisites:
            if fact.state is ReadinessFactState.CONTRADICTORY:
                raise ValueError(f"contradictory required fact: {fact.name}")
            if fact.state is not ReadinessFactState.RESOLVED:
                raise ValueError(f"unresolved required fact: {fact.name}")


@dataclass(frozen=True, slots=True)
class ResolvedMaterializedFormalConfig:
    """One authoritative raw-template to executable-config resolution cut."""

    raw_template_commitment: str
    materialized_config: Mapping[str, object]
    materialized_config_commitment: str
    required_facts_commitment: str


_FORMAL_REQUIRED_POINTERS = frozenset({
    "/evaluation_plan/production_build",
    "/evaluation_plan/corpus_index",
    "/manifest_inputs/environment_identity",
    "/manifest_inputs/approved_spec_commitments/knowledge_document_revisions",
    "/manifest_inputs/approved_spec_commitments/knowledge_build_identity",
    "/manifest_inputs/approved_spec_commitments/docker_image_digest",
    "/manifest_inputs/approved_spec_commitments/docker_namespace_identity",
    "/manifest_inputs/approved_spec_commitments/tool_versions/python",
    "/manifest_inputs/approved_spec_commitments/tool_versions/docker",
})


def _required_pointers(value: object, prefix: str = "") -> set[str]:
    result: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            result.update(_required_pointers(item, f"{prefix}/{key}"))
    elif isinstance(value, list) and value == ["REQUIRED_BEFORE_EXECUTION"]:
        result.add(prefix)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.update(_required_pointers(item, f"{prefix}/{index}"))
    elif value == "REQUIRED_BEFORE_EXECUTION":
        result.add(prefix)
    return result


def _formal_fact(name: str, value: object | None, authority: str,
                 reason: str, *, contradictory: bool = False) -> RequiredFact:
    state = (ReadinessFactState.CONTRADICTORY if contradictory
             else ReadinessFactState.RESOLVED if value is not None
             else ReadinessFactState.UNRESOLVED)
    return RequiredFact(name, state, value if state is ReadinessFactState.RESOLVED else None,
                        authority, reason)


def resolve_formal_required_facts(
    template: Mapping[str, object], *, repository_root: str | Path,
    candidate_c_authority: object | None = None,
    runtime_authority: object | None = None,
    allocation_reference: RagChildAllocation | None = None,
    evaluation_store: SqliteEvaluationStore | None = None,
    evaluation_run_id: str | None = None,
    execution_id: str | None = None,
    preparation_id: str | None = None,
    docker_state_inspector: object | None = None,
    container_real_rag_inspector: object | None = None,
) -> FormalRequiredFactsResolution:
    """Resolve Formal facts from public/governed sources, never caller values.

    Optional objects are authority/service references, not completed facts.  The
    production credential inspection deliberately uses ``environment=None``.
    """
    pointers = _required_pointers(template)
    unknown = pointers - _FORMAL_REQUIRED_POINTERS
    if unknown:
        raise ValueError(f"required fact pointer is not allowlisted: {sorted(unknown)[0]}")
    root = Path(repository_root).resolve()
    facts: dict[str, RequiredFact] = {}
    prerequisites: list[RequiredFact] = []

    try:
        repository = resolve_repository_identity(root).removeprefix("git:")
        repository_fact = _formal_fact(
            "production_build", repository, "read-only Git inspection",
            "actual repository HEAD resolved")
    except (OSError, ValueError, subprocess.SubprocessError):
        repository_fact = _formal_fact(
            "production_build", None, "read-only Git inspection",
            "actual repository HEAD is unavailable")
    facts["/evaluation_plan/production_build"] = repository_fact

    environment_value = (
        f"process:{platform.system().lower()}:{platform.machine().lower()}:"
        f"python-{platform.python_version()}"
    )
    facts["/manifest_inputs/environment_identity"] = _formal_fact(
        "environment_identity", environment_value, "real process environment",
        "bounded process/platform identity resolved")
    facts["/manifest_inputs/approved_spec_commitments/tool_versions/python"] = _formal_fact(
        "python_version", platform.python_version(), "running Python interpreter",
        "actual interpreter version resolved")

    try:
        profile_path = root / "configs/rca_evaluation/real_rag.yaml"
        import yaml
        raw_profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        rag_profile_identity = raw_profile["profile_identity"]
        rag_profile_valid = rag_profile_identity == "spec017-real-rag-gate-v1"
        configured_rag_profiles = template.get("manifest_inputs", {}).get(
            "rag_profile_references")
        prerequisites.append(_formal_fact(
            "real_rag_profile",
            rag_profile_identity if (rag_profile_valid
                                     and configured_rag_profiles == [rag_profile_identity])
            else None,
            "governed Real-RAG profile/config",
            "exact governed Real-RAG profile resolved" if rag_profile_valid
            else "Real-RAG profile contradicts the approved identity",
            contradictory=not (rag_profile_valid
                               and configured_rag_profiles == [rag_profile_identity])))
        credential_environment = raw_profile["authorization"]["credential_environment"]
        knowledge_path = root / raw_profile["knowledge"]["config_path"]
        config = load_knowledge_config(knowledge_path)
        manifest = admit_production_manifest(
            root / raw_profile["knowledge"]["manifest_path"], repository_root=root)
        documents = [
            item.committed_source_reference
            for item in manifest.manifest.documents
        ]
        facts["/manifest_inputs/approved_spec_commitments/knowledge_document_revisions"] = _formal_fact(
            "knowledge_document_revisions", documents,
            "Candidate-C admitted public manifest", "approved document revisions resolved")
        build, activation = resolve_candidate_c_build(
            candidate_c_authority, config,
            expected_manifest_commitment=manifest.manifest_commitment,
            expected_corpus_identity=manifest.manifest.corpus_id,
            expected_corpus_version=manifest.manifest.corpus_version,
        )
        prerequisites.extend((build, activation))
        build_value = build.value if build.state is ReadinessFactState.RESOLVED else None
        facts["/evaluation_plan/corpus_index"] = _formal_fact(
            "corpus_index", build_value, "Candidate-C public API",
            build.reason, contradictory=build.state is ReadinessFactState.CONTRADICTORY)
        facts["/manifest_inputs/approved_spec_commitments/knowledge_build_identity"] = _formal_fact(
            "knowledge_build_identity", build_value, "Candidate-C public API",
            build.reason, contradictory=build.state is ReadinessFactState.CONTRADICTORY)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rag_credential = resolve_environment_credential(
            credential_environment=credential_environment,
            required_profile_identity=rag_profile_identity,
            profile_identity=rag_profile_identity, validated_at=now,
            environment_identity=environment_value, environment=None)
        prerequisites.append(_formal_fact(
            "real_rag_embedding_credential",
            rag_credential.material() if rag_credential.credential_state is CredentialState.AVAILABLE else None,
            "real process environment", rag_credential.reason,
            contradictory=rag_credential.credential_state is CredentialState.INVALID))
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        for pointer, name in (
            ("/evaluation_plan/corpus_index", "corpus_index"),
            ("/manifest_inputs/approved_spec_commitments/knowledge_document_revisions",
             "knowledge_document_revisions"),
            ("/manifest_inputs/approved_spec_commitments/knowledge_build_identity",
             "knowledge_build_identity"),
        ):
            facts[pointer] = _formal_fact(
                name, None, "Candidate-C public API", "governed Candidate-C facts unavailable")
        prerequisites.append(_formal_fact(
            "real_rag_embedding_credential", None, "real process environment",
            "governed Real-RAG credential readiness unavailable"))

    try:
        llm_raw = yaml.safe_load((root / "configs/rca_evaluation/real_llm.yaml").read_text(
            encoding="utf-8"))
        llm_profile_identity = llm_raw["profile_identity"]
        configured_llm_profiles = template.get("manifest_inputs", {}).get(
            "provider_profile_references")
        llm_profile_valid = (
            llm_profile_identity == "spec017-real-llm-gate-v1"
            and configured_llm_profiles == [llm_profile_identity])
        prerequisites.append(_formal_fact(
            "real_llm_profile", llm_profile_identity if llm_profile_valid else None,
            "governed Real-LLM profile/config",
            "exact governed Real-LLM profile resolved" if llm_profile_valid
            else "Real-LLM profile contradicts the approved identity",
            contradictory=not llm_profile_valid))
        llm_credential = resolve_environment_credential(
            credential_environment=llm_raw["authorization"]["credential_environment"],
            required_profile_identity=llm_profile_identity,
            profile_identity=llm_profile_identity,
            validated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            environment_identity=environment_value, environment=None)
        prerequisites.append(_formal_fact(
            "real_llm_credential",
            llm_credential.material() if llm_credential.credential_state is CredentialState.AVAILABLE else None,
            "real process environment", llm_credential.reason,
            contradictory=llm_credential.credential_state is CredentialState.INVALID))
    except (KeyError, OSError, TypeError, ValueError):
        prerequisites.append(_formal_fact(
            "real_llm_credential", None, "real process environment",
            "governed Real-LLM credential readiness unavailable"))

    prepared = None
    if preparation_id is not None and evaluation_store is not None:
        try:
            prepared = evaluation_store.get_preparation(preparation_id)
        except (TypeError, ValueError, RuntimeError):
            prepared = None
    runtime = None
    runtime_contradictory = False
    try:
        runtime = runtime_authority.formal_readiness() if runtime_authority is not None else None
        if runtime is not None:
            from .runtime_image_readiness import RuntimeFormalReadinessFacts
            if type(runtime) is not RuntimeFormalReadinessFacts:
                raise TypeError("typed Runtime Formal readiness is required")
            runtime.validate()
    except (AttributeError, KeyError, OSError, RuntimeError,
            subprocess.SubprocessError, TypeError, ValueError, yaml.YAMLError):
        runtime = None
        runtime_contradictory = runtime_authority is not None

    image_value = None
    docker_value = None
    if runtime is not None:
        from .runtime_image_readiness import RuntimeFactState

        def runtime_fact(source: object, name: str) -> RequiredFact:
            state = source.state
            return _formal_fact(
                name, source.value if state is RuntimeFactState.AVAILABLE else None,
                "SPEC-011 Runtime image readiness projection", source.reason,
                contradictory=state is RuntimeFactState.CONTRADICTORY)

        source = runtime.source
        source_definition = runtime_fact(
            source.source_definition, "runtime_image_source_definition")
        clean_source = runtime_fact(source.formal_build_source, "formal_build_source")
        base_digest = runtime_fact(source.base_image_digest, "base_image_digest")
        client = runtime_fact(runtime.docker_client, "docker_client")
        engine = runtime_fact(runtime.docker_engine, "docker_engine")
        compose_tool = runtime_fact(runtime.docker_compose, "docker_compose")
        actual_image = runtime_fact(runtime.actual_image, "actual_runtime_image")
        approved_runtime = template.get("manifest_inputs", {}).get(
            "approved_spec_commitments", {})
        declared_compose = approved_runtime.get("docker_compose_digest")
        declared_config = approved_runtime.get("docker_config_digest")
        source_config = _formal_fact(
            "runtime_image_declared_configuration",
            {"compose_commitment": source.compose_commitment,
             "build_context_identity": source.build_context_identity,
             "formal_config_commitment": source.formal_config_commitment,
             "runtime_config_commitment": source.runtime_config_commitment,
             "dependency_commitment": source.dependency_commitment}
            if (declared_compose == source.compose_commitment
                and declared_config == source.build_context_identity) else None,
            "repository source plus Formal pre-registration",
            "declared Docker/Compose commitments match current source"
            if (declared_compose == source.compose_commitment
                and declared_config == source.formal_config_commitment)
            else "declared Docker/Compose commitments contradict current source",
            contradictory=not (
                declared_compose == source.compose_commitment
                and declared_config == source.build_context_identity),
        )
        dependency = _formal_fact(
            "runtime_dependency_reproducibility",
            source.dependency_reproducibility.value,
            "repository dependency definition",
            "dependency pinning classification fresh-read")
        prerequisites.extend((
            source_definition, clean_source, source_config, dependency,
            base_digest, client, engine, compose_tool, actual_image,
        ))
        if actual_image.state is ReadinessFactState.RESOLVED:
            image_value = actual_image.value["image_digest"]
        if all(item.state is ReadinessFactState.RESOLVED
               for item in (client, engine, compose_tool)):
            docker_value = (
                f"client={client.value};engine={engine.value['version']};"
                f"api={engine.value['api_version']};compose={compose_tool.value}")
    elif runtime_contradictory:
        prerequisites.append(_formal_fact(
            "runtime_image_readiness", None,
            "SPEC-011 Runtime image readiness projection",
            "Runtime readiness schema or material is contradictory",
            contradictory=True))
    else:
        prerequisites.append(_formal_fact(
            "runtime_image_readiness", None,
            "SPEC-011 Runtime image readiness projection",
            "Runtime readiness authority is unavailable"))

    facts["/manifest_inputs/approved_spec_commitments/docker_image_digest"] = _formal_fact(
        "docker_image_digest", image_value,
        "SPEC-011 Runtime image readiness projection",
        "actual image digest fresh-read" if image_value else "actual image is unresolved",
        contradictory=runtime_contradictory)
    facts["/manifest_inputs/approved_spec_commitments/tool_versions/docker"] = _formal_fact(
        "docker_version", docker_value,
        "SPEC-011 Runtime image readiness projection",
        "Docker client/Engine/Compose versions fresh-read"
        if docker_value else "complete Docker toolchain is unresolved",
        contradictory=runtime_contradictory)
    namespace = (prepared.get("docker_namespace", {}).get("namespace_identity")
                 if prepared is not None else None)
    facts["/manifest_inputs/approved_spec_commitments/docker_namespace_identity"] = _formal_fact(
        "docker_namespace_identity", namespace, "SqliteEvaluationStore",
        "durable prepared Docker namespace fresh-read"
        if namespace else "durable prepared Docker namespace is unavailable")
    prerequisites.extend((
        facts["/manifest_inputs/approved_spec_commitments/docker_image_digest"],
        facts["/manifest_inputs/approved_spec_commitments/docker_namespace_identity"],
        facts["/manifest_inputs/approved_spec_commitments/tool_versions/docker"],
    ))

    if prepared is not None and evaluation_store is not None:
        try:
            from .formal_compose import resolve_formal_compose_topology
            topology = resolve_formal_compose_topology(
                evaluation_store, preparation_id, root)
            prerequisites.append(_formal_fact(
                "formal_compose_topology",
                {"schema": topology.schema,
                 "preparation_id": topology.preparation_id,
                 "topology_commitment": topology.topology_commitment,
                 "execution_envelope_count": len(topology.envelopes),
                 "execution_envelopes": [
                     {"planned_execution_identity": item.planned_execution_identity,
                      "compose_project_identity": item.compose_project_identity,
                      "network_identity": item.network_identity,
                      "volume_identities": [mount.volume_identity
                                            for mount in item.volume_mounts],
                      "state_volume_ownership": dict(
                          item.state_volume_ownership),
                      "workspace_identity": item.workspace_identity,
                      "log_identity": item.log_identity}
                     for item in topology.envelopes],
                 "shared_resource_guard": "PASS",
                 "host_port_policy": "NONE"},
                "Candidate-F preparation plus repository Formal Compose artifact",
                "30 isolated repetition-major execution envelopes fresh-derived"))
            # Slice 5 adds typed current-state prerequisites without treating
            # the declarative controller/topology as actual Docker truth.
            from .formal_docker_lifecycle import (
                resolve_campaign_docker_lifecycle_required_facts,
            )
            prerequisites.extend(
                resolve_campaign_docker_lifecycle_required_facts(
                    topology, docker_state_inspector))
        except (AttributeError, KeyError, OSError, RuntimeError,
                TypeError, ValueError, yaml.YAMLError):
            prerequisites.append(_formal_fact(
                "formal_compose_topology", None,
                "Candidate-F preparation plus repository Formal Compose artifact",
                "Formal Compose topology is missing, stale, or contradictory",
                contradictory=True))

        try:
            from .container_wiring import resolve_formal_container_wiring
            container_wiring = resolve_formal_container_wiring(
                evaluation_store, preparation_id, root,
                candidate_c_authority=candidate_c_authority)
            prerequisites.append(_formal_fact(
                "formal_container_credential_injection",
                container_wiring.credential_injection.material(),
                "repository Compose artifact plus R3 credential resolver",
                "name-only external credential injection contract fresh-derived"))
            prerequisites.append(_formal_fact(
                "formal_real_rag_mount_wiring",
                {"schema": container_wiring.schema,
                 "preparation_id": container_wiring.preparation_id,
                 "wiring_commitment": container_wiring.wiring_commitment,
                 "topology_commitment": container_wiring.topology_commitment,
                 "execution_mount_count": len(container_wiring.mounts),
                 "execution_mounts": [
                     {"planned_execution_identity": item.planned_execution_identity,
                      "execution_child_identity": item.execution_child_identity,
                      "mount_commitment": item.mount_commitment}
                     for item in container_wiring.mounts]},
                "SqliteEvaluationStore plus governed R3/Candidate-C configuration",
                "30 exact isolated child/corpus mount plans fresh-derived"))
            if container_real_rag_inspector is None:
                prerequisites.extend((
                    _formal_fact(
                        "actual_real_rag_child_mount", None,
                        "typed container filesystem inspector",
                        "actual execution child mount has not been inspected"),
                    _formal_fact(
                        "actual_real_rag_corpus_mount", None,
                        "typed container filesystem inspector",
                        "approved corpus source mount has not been inspected"),
                ))
            else:
                from .container_wiring import (
                    ContainerRealRagInspectionTarget,
                    ContainerRealRagReadiness,
                )
                inspected = []
                for envelope, mount in zip(topology.envelopes, container_wiring.mounts):
                    target = ContainerRealRagInspectionTarget(
                        preparation_id, envelope.compose_project_identity, mount)
                    current = container_real_rag_inspector.inspect(target)
                    if type(current) is not ContainerRealRagReadiness:
                        raise TypeError("typed container Real-RAG inspection is required")
                    if (current.preparation_id != target.preparation_id
                            or current.project_identity != target.project_identity
                            or current.planned_execution_identity !=
                                mount.planned_execution_identity
                            or current.child_identity != mount.execution_child_identity
                            or current.child_target != mount.child_container_target
                            or current.corpus_target != mount.corpus_container_target):
                        raise ValueError("container inspection target/provenance mismatch")
                    inspected.append(current)
                for name, attribute in (
                    ("actual_real_rag_child_mount", "mount_fact"),
                    ("actual_real_rag_corpus_mount", "corpus_fact"),
                ):
                    source_facts = [getattr(item, attribute) for item in inspected]
                    states = {item.state for item in source_facts}
                    state = (ReadinessFactState.CONTRADICTORY
                             if ReadinessFactState.CONTRADICTORY in states
                             else ReadinessFactState.RESOLVED
                             if states == {ReadinessFactState.RESOLVED}
                             else ReadinessFactState.UNRESOLVED)
                    value = ([item.value for item in source_facts]
                             if state is ReadinessFactState.RESOLVED else None)
                    prerequisites.append(RequiredFact(
                        name, state, value, "typed container filesystem inspector",
                        "all 30 exact mounts fresh-inspected" if value is not None
                        else "one or more exact mounts are unresolved/contradictory"))
        except (AttributeError, KeyError, OSError, RuntimeError,
                TypeError, ValueError, yaml.YAMLError):
            prerequisites.append(_formal_fact(
                "formal_container_wiring", None,
                "Candidate-F preparation plus governed R3/Candidate-C configuration",
                "Formal credential/Real-RAG wiring is missing or contradictory",
                contradictory=True))

    allocation_fact: RequiredFact
    if (allocation_reference is not None and evaluation_store is not None
            and evaluation_run_id is not None and execution_id is not None):
        try:
            verified = verify_supported_real_rag_child_allocation(
                allocation_reference, evaluation_store=evaluation_store,
                repository_root=root,
                isolated_root=raw_profile["knowledge"]["isolated_chroma_root"],
                production_root=config.chroma_path,
                evaluation_run_id=evaluation_run_id, execution_id=execution_id,
                profile_identity=rag_profile_identity)
            allocation_fact = _formal_fact(
                "real_rag_child_allocation", verified.material(),
                "SqliteEvaluationStore", "durable child allocation fresh-read verified")
        except (AttributeError, KeyError, OSError, TypeError, ValueError):
            allocation_fact = _formal_fact(
                "real_rag_child_allocation", None, "SqliteEvaluationStore",
                "durable child allocation is missing or contradictory")
    elif prepared is not None:
        try:
            from .preparation import verify_prepared_real_rag_child
            child = verify_prepared_real_rag_child(
                evaluation_store, preparation_id, root)
            allocation_fact = _formal_fact(
                "real_rag_child_allocation", child,
                "SqliteEvaluationStore",
                "durable prepared child and current ownership marker verified")
        except (AttributeError, KeyError, OSError, TypeError, ValueError,
                RuntimeError):
            allocation_fact = _formal_fact(
                "real_rag_child_allocation", None, "SqliteEvaluationStore",
                "current prepared child allocation is missing or contradictory",
                contradictory=True)
    else:
        allocation_fact = _formal_fact(
            "real_rag_child_allocation", None, "SqliteEvaluationStore",
            "durable child allocation is absent")
    prerequisites.append(allocation_fact)

    selected = {pointer: facts[pointer] for pointer in pointers}
    if not selected:
        # ``create`` intentionally requires at least one pointer.  A frozen R1
        # fixture has no resolver work and is admitted under its existing checks.
        selected = {"/evaluation_plan/production_build": repository_fact}
    return FormalRequiredFactsResolution(
        RequiredFactsMaterialization.create(selected), tuple(prerequisites))


def materialize_required_before_execution(
    template: Mapping[str, object], materialization: RequiredFactsMaterialization,
) -> dict[str, object]:
    """Replace placeholders only from resolved typed facts; fail closed otherwise."""
    if type(materialization) is not RequiredFactsMaterialization:
        raise TypeError("typed Candidate-F readiness materialization is required")
    result = copy.deepcopy(dict(template))
    for pointer, fact in materialization.facts:
        if fact.state is ReadinessFactState.CONTRADICTORY:
            raise ValueError(f"contradictory required fact: {fact.name}")
        if fact.state is not ReadinessFactState.RESOLVED:
            raise ValueError(f"unresolved required fact: {fact.name}")
        parts = pointer.lstrip("/").split("/")
        target: object = result
        for raw in parts[:-1]:
            part = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(target, dict) and part in target:
                target = target[part]
            elif isinstance(target, list) and part.isdigit() and int(part) < len(target):
                target = target[int(part)]
            else:
                raise ValueError(f"materialization path is absent: {pointer}")
        leaf = parts[-1].replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict) and leaf in target:
            current = target[leaf]
        elif isinstance(target, list) and leaf.isdigit() and int(leaf) < len(target):
            current = target[int(leaf)]
        else:
            current = None
        if current not in ("REQUIRED_BEFORE_EXECUTION", ["REQUIRED_BEFORE_EXECUTION"]):
            raise ValueError(f"materialization target is not unresolved: {pointer}")
        if isinstance(target, list):
            target[int(leaf)] = fact.value
        else:
            target[leaf] = fact.value
    return result


def resolve_materialized_formal_config(
    template: Mapping[str, object], *, repository_root: str | Path,
    candidate_c_authority: object | None = None,
    runtime_authority: object | None = None,
    allocation_reference: RagChildAllocation | None = None,
    evaluation_store: SqliteEvaluationStore | None = None,
    evaluation_run_id: str | None = None,
    execution_id: str | None = None,
    preparation_id: str | None = None,
    docker_state_inspector: object | None = None,
    container_real_rag_inspector: object | None = None,
    _required_facts_resolver: object | None = None,
) -> ResolvedMaterializedFormalConfig:
    """Fresh-resolve and materialize the supported Formal configuration.

    The physical preregistration remains the raw-template authority.  This
    function is the single semantic bridge from that template to the complete,
    secret-free execution configuration accepted by Final Admission.
    """
    raw_commitment = commitment(dict(template))
    resolver = _required_facts_resolver or resolve_formal_required_facts
    if not callable(resolver):
        raise TypeError("Formal required-fact resolver must be callable")
    resolution = resolver(
        template, repository_root=repository_root,
        candidate_c_authority=candidate_c_authority,
        runtime_authority=runtime_authority,
        allocation_reference=allocation_reference,
        evaluation_store=evaluation_store,
        evaluation_run_id=evaluation_run_id,
        execution_id=execution_id,
        preparation_id=preparation_id,
        docker_state_inspector=docker_state_inspector,
        container_real_rag_inspector=container_real_rag_inspector,
    )
    resolution.assert_resolved()
    materialized = (
        materialize_required_before_execution(template, resolution.materialization)
        if _required_pointers(template) else copy.deepcopy(dict(template))
    )
    if _required_pointers(materialized):
        raise ValueError("Formal configuration remains unresolved after materialization")
    return ResolvedMaterializedFormalConfig(
        raw_template_commitment=raw_commitment,
        materialized_config=materialized,
        materialized_config_commitment=commitment(materialized),
        required_facts_commitment=(
            resolution.materialization.materialization_commitment),
    )
