"""PM-authorized pre-admission preparation for SPEC-017 Candidate-F.

Preparation issues a durable, deterministic future-target identity and may
record exact evaluation-owned preparation resources.  It never creates an
evaluation run, execution, gate receipt, scenario result, or denominator row.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping, Protocol

import yaml

from .identity import commitment, identity, require_identity
from .sqlite_store import SqliteEvaluationStore


PREPARATION_AUTHORITY = "PM-SPEC017-R4-PREADMISSION-20261016"
PREPARATION_SCHEMA = "spec017.preparation.v1"
PREPARED_CHILD_MARKER = ".candidate-f-ownership.json"


def _walk_files(root: Path) -> tuple[tuple[str, Path], ...]:
    """Enumerate files safely even when deterministic identities exceed MAX_PATH."""
    raw_root = str(root.resolve())
    filesystem_root = ("\\\\?\\" + raw_root
                       if os.name == "nt" and not raw_root.startswith("\\\\?\\")
                       else raw_root)
    values: list[tuple[str, Path]] = []
    for directory, _, files in os.walk(filesystem_root):
        for name in files:
            actual = Path(directory) / name
            relative = os.path.relpath(str(actual), filesystem_root).replace("\\", "/")
            values.append((relative, actual))
    return tuple(sorted(values, key=lambda item: item[0]))


def _filesystem_path(path: Path) -> Path:
    value = str(path.resolve())
    return Path("\\\\?\\" + value) if os.name == "nt" else Path(value)


def _expected_execution_children(
    prepared: Mapping[str, object],
) -> tuple[tuple[str, dict[str, object]], ...]:
    """Derive execution-child markers from the same frozen lineage inputs."""
    from .oracle import SCENARIOS

    lineage = prepared["planned_lineage"]
    child = prepared["real_rag_child"]
    allocation = str(child["relative_path"])
    values = []
    for repetition in range(1, 6):
        for scenario in SCENARIOS:
            planned_execution = identity(
                "planned_execution", [lineage["planned_run_identity"],
                                      repetition, scenario])
            execution_child = "ragchild_" + commitment({
                "preparation": prepared["preparation_id"],
                "planned_run": lineage["planned_run_identity"],
                "planned_execution": planned_execution,
                "allocation_root": child["child_identity"],
                "profile": child["profile_identity"],
            })[7:]
            source_reference = f"{allocation}/executions/{execution_child}"
            marker = {
                "schema": "spec017.formal-real-rag-execution-child.v1",
                "preparation_id": prepared["preparation_id"],
                "planned_run_identity": lineage["planned_run_identity"],
                "planned_execution_identity": planned_execution,
                "allocation_root_identity": child["child_identity"],
                "execution_child_identity": execution_child,
                "profile_identity": child["profile_identity"],
                "child_source_reference": source_reference,
                "lifecycle_state": "ALLOCATED",
            }
            values.append((execution_child, {
                **marker, "ownership_commitment": commitment(marker)}))
    return tuple(values)


class PreparationLifecycle(str, Enum):
    """Governance states; EXECUTION is a boundary, not a Slice-0 action."""

    PREREGISTERED = "PREREGISTERED"
    PREPARED = "PREPARED"
    ADMITTED = "ADMITTED"
    EXECUTION = "EXECUTION"


class PreparationResourceState(str, Enum):
    """Typed resource progress; none of these states is execution authority."""

    IDENTITY_ONLY = "IDENTITY_ONLY"
    PREPARING = "PREPARING"
    PREPARED_RESOURCES = "PREPARED_RESOURCES"
    INVALID = "INVALID"


class PreparationResourceDomain(str, Enum):
    RUNTIME_IMAGE = "RUNTIME_IMAGE"
    DOCKER_TOPOLOGY = "DOCKER_TOPOLOGY"
    DOCKER_OWNERSHIP = "DOCKER_OWNERSHIP"
    DOCKER_HEALTH = "DOCKER_HEALTH"
    DOCKER_STALE_STATE = "DOCKER_STALE_STATE"
    DOCKER_ACTUAL_RESOURCES = "DOCKER_ACTUAL_RESOURCES"
    REAL_RAG_BUILD = "REAL_RAG_BUILD"
    REAL_RAG_ACTIVATION = "REAL_RAG_ACTIVATION"
    REAL_RAG_CHILD_MOUNT = "REAL_RAG_CHILD_MOUNT"
    REAL_RAG_CORPUS_MOUNT = "REAL_RAG_CORPUS_MOUNT"


_BOUNDED_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/@+-]{0,511}")
_COMMITMENT = re.compile(r"sha256:[0-9a-f]{64}")
_RESOURCE_AUTHORITIES = {
    PreparationResourceDomain.RUNTIME_IMAGE: "SPEC011_RUNTIME_IMAGE_INSPECTOR_v1",
    PreparationResourceDomain.DOCKER_TOPOLOGY: "SPEC017_FORMAL_COMPOSE_RESOLVER_v1",
    PreparationResourceDomain.DOCKER_OWNERSHIP: "SPEC017_DOCKER_STATE_INSPECTOR_v1",
    PreparationResourceDomain.DOCKER_HEALTH: "SPEC017_DOCKER_STATE_INSPECTOR_v1",
    PreparationResourceDomain.DOCKER_STALE_STATE:
        "SPEC017_DOCKER_STATE_INSPECTOR_v1",
    PreparationResourceDomain.DOCKER_ACTUAL_RESOURCES:
        "SPEC017_DOCKER_STATE_INSPECTOR_v1",
    PreparationResourceDomain.REAL_RAG_BUILD: "CANDIDATE_C_PUBLIC_BUILD_v1",
    PreparationResourceDomain.REAL_RAG_ACTIVATION: "CANDIDATE_C_PUBLIC_ACTIVATION_v1",
    PreparationResourceDomain.REAL_RAG_CHILD_MOUNT: "SPEC017_CONTAINER_MOUNTS_v1",
    PreparationResourceDomain.REAL_RAG_CORPUS_MOUNT: "SPEC017_CONTAINER_MOUNTS_v1",
}


@dataclass(frozen=True, slots=True)
class PreparationResourceFact:
    """Positive-schema authoritative observation; no free-form metadata."""

    domain: PreparationResourceDomain
    preparation_id: str
    planned_run_identity: str
    expected_target_identity: str
    actual_observed_identity: str
    authority_profile: str
    semantic_identity_commitment: str
    observed_at: str
    fact_commitment: str

    def material(self, *, include_commitment: bool = True) -> dict[str, str]:
        result = {
            "domain": self.domain.value,
            "preparation_id": self.preparation_id,
            "planned_run_identity": self.planned_run_identity,
            "expected_target_identity": self.expected_target_identity,
            "actual_observed_identity": self.actual_observed_identity,
            "authority_profile": self.authority_profile,
            "semantic_identity_commitment": self.semantic_identity_commitment,
            "observed_at": self.observed_at,
        }
        if include_commitment:
            result["fact_commitment"] = self.fact_commitment
        return result

    @classmethod
    def create(
        cls, domain: PreparationResourceDomain, preparation_id: str,
        planned_run_identity: str, expected_target_identity: str,
        actual_observed_identity: str, observed_at: str, *,
        semantic_identity_commitment: str | None = None,
    ) -> "PreparationResourceFact":
        authority = _RESOURCE_AUTHORITIES[domain]
        semantic_material = {
            "domain": domain.value, "preparation_id": preparation_id,
            "planned_run_identity": planned_run_identity,
            "expected_target_identity": expected_target_identity,
            "actual_observed_identity": actual_observed_identity,
            "authority_profile": authority,
        }
        semantic_identity_commitment = (
            semantic_identity_commitment or commitment(semantic_material))
        material = {
            **semantic_material,
            "semantic_identity_commitment": semantic_identity_commitment,
            "observed_at": observed_at,
        }
        result = cls(domain, preparation_id, planned_run_identity,
                     expected_target_identity, actual_observed_identity,
                     authority, semantic_identity_commitment, observed_at,
                     commitment(material))
        result.validate()
        return result

    def validate(self) -> None:
        require_identity(self.preparation_id, "preparation")
        require_identity(self.planned_run_identity, "planned_run")
        actual_valid = (
            bool(re.fullmatch(r"kbld_[0-9a-f]{64}", self.actual_observed_identity))
            if self.domain is PreparationResourceDomain.REAL_RAG_BUILD
            else bool(_COMMITMENT.fullmatch(self.actual_observed_identity)))
        expected_valid = (
            self.expected_target_identity == "spec011-runtime-formal-v1"
            if self.domain is PreparationResourceDomain.RUNTIME_IMAGE
            else bool(re.fullmatch(r"kbld_[0-9a-f]{64}", self.expected_target_identity))
            if self.domain is PreparationResourceDomain.REAL_RAG_ACTIVATION
            else bool(_COMMITMENT.fullmatch(self.expected_target_identity))
            if self.domain is not PreparationResourceDomain.DOCKER_TOPOLOGY
            else self.expected_target_identity.startswith("eval:docker_namespace:"))
        try:
            datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
            time_valid = True
        except (TypeError, ValueError):
            time_valid = False
        if (self.authority_profile != _RESOURCE_AUTHORITIES.get(self.domain)
                or any(not _BOUNDED_IDENTITY.fullmatch(value) for value in (
                    self.expected_target_identity,
                    self.actual_observed_identity,
                    self.authority_profile))
                or not actual_valid or not expected_valid or not time_valid
                or len(self.observed_at) > 64
                or not _COMMITMENT.fullmatch(
                    self.semantic_identity_commitment)
                or not _COMMITMENT.fullmatch(self.fact_commitment)
                or commitment(self.material(include_commitment=False)) !=
                    self.fact_commitment):
            raise ValueError("preparation resource fact is invalid")


@dataclass(frozen=True, slots=True)
class PreparationResourceFacts:
    facts: tuple[PreparationResourceFact, ...]
    facts_commitment: str

    @classmethod
    def create(cls, facts: tuple[PreparationResourceFact, ...]) -> "PreparationResourceFacts":
        expected = tuple(PreparationResourceDomain)
        if tuple(item.domain for item in facts) != expected:
            raise ValueError("preparation resource fact domains are incomplete")
        for fact in facts:
            fact.validate()
        material = [item.material() for item in facts]
        result = cls(facts, commitment(material))
        result.validate()
        return result

    def validate(self) -> None:
        if (not _COMMITMENT.fullmatch(self.facts_commitment)
                or commitment([item.material() for item in self.facts]) !=
                    self.facts_commitment):
            raise ValueError("preparation resource facts commitment is invalid")
        for fact in self.facts:
            fact.validate()

    def material(self) -> dict[str, object]:
        return {"facts": [item.material() for item in self.facts],
                "facts_commitment": self.facts_commitment}


def prepared_resource_semantic_identity(
    facts: PreparationResourceFacts | Mapping[str, object],
) -> tuple[tuple[str, str, str, str, str, str, str], ...]:
    """Return the one bounded durable/fresh semantic comparison contract.

    Observation time is deliberately excluded. Domain-specific correctness is
    promoted into ``semantic_identity_commitment`` before persistence.
    """
    raw_facts = (facts.facts if isinstance(facts, PreparationResourceFacts)
                 else facts.get("facts"))
    if not isinstance(raw_facts, (tuple, list)):
        raise ValueError("preparation resource semantic facts are absent")
    result = []
    for item in raw_facts:
        if isinstance(item, PreparationResourceFact):
            values = (
                item.domain.value, item.preparation_id,
                item.planned_run_identity, item.expected_target_identity,
                item.actual_observed_identity, item.authority_profile,
                item.semantic_identity_commitment,
            )
        elif isinstance(item, Mapping):
            values = tuple(item.get(name) for name in (
                "domain", "preparation_id", "planned_run_identity",
                "expected_target_identity", "actual_observed_identity",
                "authority_profile", "semantic_identity_commitment"))
        else:
            raise ValueError("preparation resource semantic fact is malformed")
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("preparation resource semantic identity is malformed")
        result.append(values)
    if tuple(item[0] for item in result) != tuple(
            domain.value for domain in PreparationResourceDomain):
        raise ValueError("preparation resource semantic domains are incomplete")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CandidateCPreparationRequest:
    preparation_id: str
    planned_run_identity: str
    child_identity: str
    child_path: str
    profile_identity: str
    manifest_commitment: str
    corpus_identity: str
    corpus_release: str
    source_revision: str = "7e577de0ad85946c00c7259af3e1636f9eabd5ae"
    embedding_model: str = "text-embedding-004"
    embedding_profile_identity: str = "spec014-google-document-query-v1"
    index_schema_identity: str = "spec014-chroma-l2-v2"
    distance_metric: str = "l2"
    retrieval_profile_identity: str = "spec014-retrieval-v1"
    gate_profile_identity: str = "spec017-real-rag-gate-v1"
    execution_mounts: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class DockerPreparationPhase:
    """Ephemeral continuation identity for one exact Docker preparation."""

    preparation_id: str
    topology_commitment: str
    project_names: tuple[str, ...]

    def validate(self) -> None:
        require_identity(self.preparation_id, "preparation")
        if (not self.topology_commitment.startswith("sha256:")
                or len(self.topology_commitment) != 71
                or len(self.project_names) != 30
                or len(set(self.project_names)) != len(self.project_names)
                or any(not item for item in self.project_names)):
            raise ValueError("Docker preparation phase identity is malformed")


@dataclass(frozen=True, slots=True)
class CandidateCPreparationEvidence:
    """Ephemeral evidence derived from Candidate-C public authority."""

    preparation_id: str
    child_identity: str
    manifest_commitment: str
    corpus_identity: str
    build_identity: str
    activation_identity: str
    validation_commitment: str
    artifact_commitment: str
    artifacts: tuple[tuple[str, str], ...]

    def validate(self) -> None:
        require_identity(self.preparation_id, "preparation")
        if (not self.child_identity.startswith("ragchild_")
                or len(self.child_identity) != len("ragchild_") + 64):
            raise ValueError("Candidate-C child identity is malformed")
        if (not self.build_identity.startswith("kbld_")
                or len(self.build_identity) != len("kbld_") + 64):
            raise ValueError("Candidate-C build identity is malformed")
        if (not self.manifest_commitment.startswith("kmf_")
                or not self.corpus_identity
                or any(not value.startswith("sha256:") or len(value) != 71
                       for value in (self.activation_identity,
                                     self.validation_commitment,
                                     self.artifact_commitment))
                or not self.artifacts
                or tuple(sorted(self.artifacts)) != self.artifacts
                or len(dict(self.artifacts)) != len(self.artifacts)
                or any(not path or Path(path).is_absolute() or ".." in Path(path).parts
                       or not digest.startswith("sha256:") or len(digest) != 71
                       for path, digest in self.artifacts)):
            raise ValueError("Candidate-C preparation evidence is malformed")


class PreparationDockerOperations(Protocol):
    def create_exact(
        self, authority: object, topology: object, *,
        evidence: CandidateCPreparationEvidence | None = None,
        candidate_c_authority: object | None = None,
    ) -> DockerPreparationPhase: ...

    def start_exact(
        self, phase: DockerPreparationPhase, authority: object, topology: object,
        evidence: CandidateCPreparationEvidence, candidate_c_authority: object,
    ) -> None: ...


class CandidateCPreparationOperations(Protocol):
    def recover_exact(
        self, request: CandidateCPreparationRequest,
    ) -> CandidateCPreparationEvidence: ...

    def prepare_exact(
        self, request: CandidateCPreparationRequest,
    ) -> CandidateCPreparationEvidence: ...


@dataclass(frozen=True, slots=True)
class PreparationReport:
    preparation_id: str
    preparation_commitment: str
    planned_run_identity: str
    planned_execution_identity: str
    docker_namespace_identity: str
    docker_project_name: str
    real_rag_child_identity: str
    real_rag_child_reference: str
    lifecycle_state: str
    database_path: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, object], database_path: Path) -> "PreparationReport":
        lineage = payload["planned_lineage"]
        docker = payload["docker_namespace"]
        child = payload["real_rag_child"]
        if not all(isinstance(item, Mapping) for item in (lineage, docker, child)):
            raise ValueError("prepared lineage payload is malformed")
        return cls(
            str(payload["preparation_id"]), commitment(dict(payload)),
            str(lineage["planned_run_identity"]),
            str(lineage["planned_execution_identity"]),
            str(docker["namespace_identity"]), str(docker["project_name"]),
            str(child["child_identity"]), str(child["relative_path"]),
            str(payload["lifecycle_state"]), str(database_path.resolve()),
        )


def _real_rag_root_has_state(isolated_root: Path) -> bool:
    """Return whether the exact evaluation-owned root has pre-existing state."""
    if isolated_root.is_symlink():
        return True
    if not isolated_root.exists():
        return False
    return not isolated_root.is_dir() or any(isolated_root.iterdir())


def _namespace_shape(database_path: Path, evaluation_root: Path) -> None:
    database = database_path.resolve()
    root = evaluation_root.resolve()
    if (root.name != "rca_evaluation" or database.parent != root
            or database.suffix != ".db"):
        raise ValueError("preparation requires a dedicated rca_evaluation DB namespace")
    if root.exists():
        allowed = {database.name, database.name + "-journal",
                   database.name + "-shm", database.name + "-wal", "real_rag"}
        if any(item.name not in allowed for item in root.iterdir()):
            raise ValueError("preparation namespace contains unauthorized preexisting state")
        if (not database.exists()
                and _real_rag_root_has_state(root / "real_rag")):
            raise ValueError("stale Real-RAG state exists before preparation authority")


def _build_payload(
    *, repository_root: Path, config: dict[str, object], campaign_key: str,
    repository_branch: str, repository_commit: str, database_path: Path,
) -> dict[str, object]:
    preregistration_commitment = commitment(config)
    preparation_id = identity("preparation", [
        preregistration_commitment, campaign_key, repository_branch,
        repository_commit, str(database_path.resolve()),
    ])
    planned_run = identity("planned_run", [preparation_id, campaign_key])
    planned_execution = identity(
        "planned_execution", [planned_run, "READINESS_TARGET"])
    namespace = identity("docker_namespace", [
        preparation_id, planned_run, "FORMAL_FUTURE_TARGET"])
    project_name = "spec017-" + namespace.rsplit(":", 1)[1][:20]

    rag_profile = yaml.safe_load((
        repository_root / "configs/rca_evaluation/real_rag.yaml"
    ).read_text(encoding="utf-8"))
    isolated_root = (database_path.resolve().parent / "real_rag").resolve()
    child_identity = "ragchild_" + commitment({
        "preparation": preparation_id,
        "planned_run": planned_run,
        "planned_execution": planned_execution,
        "profile": rag_profile["profile_identity"],
    })[7:]
    child_path = (isolated_root / child_identity).resolve()
    child_path.relative_to(isolated_root)
    relative_path = child_path.relative_to(database_path.resolve().parent).as_posix()
    owner = {
        "schema": "spec017.prepared-real-rag-child-ownership.v1",
        "preparation_id": preparation_id,
        "planned_run_identity": planned_run,
        "planned_execution_identity": planned_execution,
        "profile_identity": rag_profile["profile_identity"],
        "child_identity": child_identity,
        "relative_path": relative_path,
        "lifecycle_state": "ALLOCATED",
    }
    ownership_commitment = commitment(owner)
    readiness_material = {
        "provider_profiles": config["manifest_inputs"]["provider_profile_references"],
        "rag_profiles": config["manifest_inputs"]["rag_profile_references"],
        "repository_commit": repository_commit,
        "configuration_commitment": preregistration_commitment,
        "docker_namespace_identity": namespace,
        "real_rag_child_identity": child_identity,
    }
    return {
        "schema": PREPARATION_SCHEMA,
        "authority_reference": PREPARATION_AUTHORITY,
        "lifecycle_state": PreparationLifecycle.PREPARED.value,
        "preparation_id": preparation_id,
        "preregistration_identity": preregistration_commitment,
        "campaign_identity": commitment({"campaign_key": campaign_key}),
        "campaign_key": campaign_key,
        "repository": {
            "root": str(repository_root), "branch": repository_branch,
            "commit": repository_commit,
            "configuration_reference": config["evaluation_plan"]["configuration_reference"],
            "configuration_commitment": preregistration_commitment,
        },
        "planned_lineage": {
            "planned_run_identity": planned_run,
            "planned_execution_identity": planned_execution,
            "counts_as_formal_execution": False,
        },
        "docker_namespace": {
            "namespace_identity": namespace, "project_name": project_name,
            "ownership": preparation_id,
            "state": "PREPARED_IDENTITY_ONLY",
            "resources_created": False,
            "resource_state": PreparationResourceState.IDENTITY_ONLY.value,
            "resource_commitment": None,
            "topology_commitment": None,
            "actual_ownership_commitment": None,
        },
        "real_rag_child": {
            "child_identity": child_identity,
            "relative_path": relative_path,
            "profile_identity": rag_profile["profile_identity"],
            "ownership": preparation_id,
            "state": "ALLOCATED",
            "ownership_commitment": ownership_commitment,
            "resource_state": PreparationResourceState.IDENTITY_ONLY.value,
            "corpus_identity": None,
            "index_identity": None,
            "build_identity": None,
            "activation_identity": None,
            "artifacts": [],
            "resource_commitment": None,
        },
        "readiness_preparation": {
            "state": "PREPARED_NOT_VALIDATED_FOR_ADMISSION",
            "material": readiness_material,
            "commitment": commitment(readiness_material),
        },
        "resource_facts": None,
        "effects": {
            "formal_execution": False, "external_gates": False,
            "scenario_injection": False, "denominator_consumed": False,
        },
    }


def _prepared_resources_payload(
    prepared: Mapping[str, object], *, facts: PreparationResourceFacts,
    artifacts: Mapping[str, str], corpus_identity: str,
) -> dict[str, object]:
    """Private positive-schema serializer for internally observed facts."""
    facts.validate()
    prepared_resource_semantic_identity(facts)
    result = copy.deepcopy(dict(prepared))
    docker = result.get("docker_namespace")
    child = result.get("real_rag_child")
    if not isinstance(docker, dict) or not isinstance(child, dict):
        raise ValueError("preparation resource payload is malformed")
    if (facts.facts[0].preparation_id != prepared.get("preparation_id")
            or any(item.preparation_id != prepared.get("preparation_id")
                   or item.planned_run_identity !=
                        prepared["planned_lineage"]["planned_run_identity"]
                   for item in facts.facts)):
        raise ValueError("preparation resource facts belong to another preparation")
    by_domain = {item.domain: item for item in facts.facts}
    runtime_image_identity = by_domain[
        PreparationResourceDomain.RUNTIME_IMAGE].actual_observed_identity
    docker_topology_commitment = by_domain[
        PreparationResourceDomain.DOCKER_TOPOLOGY].actual_observed_identity
    docker_ownership_commitment = by_domain[
        PreparationResourceDomain.DOCKER_OWNERSHIP].actual_observed_identity
    service_health_commitment = by_domain[
        PreparationResourceDomain.DOCKER_HEALTH].actual_observed_identity
    build_identity = by_domain[
        PreparationResourceDomain.REAL_RAG_BUILD].actual_observed_identity
    activation_identity = by_domain[
        PreparationResourceDomain.REAL_RAG_ACTIVATION].actual_observed_identity
    mount_commitment = commitment({
        "child": by_domain[PreparationResourceDomain.REAL_RAG_CHILD_MOUNT]
            .actual_observed_identity,
        "corpus": by_domain[PreparationResourceDomain.REAL_RAG_CORPUS_MOUNT]
            .actual_observed_identity,
    })
    index_identity = build_identity
    if (not isinstance(artifacts, Mapping) or not artifacts
            or len(artifacts) > 128):
        raise ValueError("prepared Candidate-C artifact manifest is invalid")
    normalized: list[dict[str, str]] = []
    for relative, digest in sorted(artifacts.items()):
        path = Path(relative)
        if (not isinstance(relative, str) or not relative or path.is_absolute()
                or ".." in path.parts or relative == PREPARED_CHILD_MARKER
                or not isinstance(digest, str)
                or not digest.startswith("sha256:") or len(digest) != 71):
            raise ValueError("prepared Candidate-C artifact is invalid")
        normalized.append({"relative_path": path.as_posix(), "digest": digest})
    docker_material = {
        "namespace_identity": docker["namespace_identity"],
        "project_name": docker["project_name"],
        "ownership": docker["ownership"],
        "topology_commitment": docker_topology_commitment,
        "actual_ownership_commitment": docker_ownership_commitment,
        "runtime_image_identity": runtime_image_identity,
        "service_health_commitment": service_health_commitment,
    }
    docker.update({
        "state": "PREPARED_RESOURCES", "resources_created": True,
        "resource_state": PreparationResourceState.PREPARED_RESOURCES.value,
        "topology_commitment": docker_topology_commitment,
        "actual_ownership_commitment": docker_ownership_commitment,
        "resource_commitment": commitment(docker_material),
    })
    child_material = {
        "child_identity": child["child_identity"],
        "ownership": child["ownership"], "corpus_identity": corpus_identity,
        "index_identity": index_identity, "build_identity": build_identity,
        "activation_identity": activation_identity, "artifacts": normalized,
        "mount_commitment": mount_commitment,
    }
    child.update({
        "resource_state": PreparationResourceState.PREPARED_RESOURCES.value,
        "corpus_identity": corpus_identity, "index_identity": index_identity,
        "build_identity": build_identity, "activation_identity": activation_identity,
        "artifacts": normalized, "resource_commitment": commitment(child_material),
    })
    readiness = result["readiness_preparation"]
    readiness["state"] = "PREPARED_RESOURCES_NOT_VALIDATED_FOR_ADMISSION"
    readiness["material"] = {
        **readiness["material"], "runtime_image_identity": runtime_image_identity,
        "service_health_commitment": service_health_commitment,
        "mount_commitment": mount_commitment,
        "docker_resource_commitment": docker["resource_commitment"],
        "real_rag_resource_commitment": child["resource_commitment"],
    }
    readiness["commitment"] = commitment(readiness["material"])
    result["resource_facts"] = facts.material()
    return result


def identity_only_preparation_payload(
    prepared: Mapping[str, object],
) -> dict[str, object]:
    """Project immutable preparation identity for Final Admission freshness."""
    result = copy.deepcopy(dict(prepared))
    docker = result["docker_namespace"]
    child = result["real_rag_child"]
    docker.update({
        "state": "PREPARED_IDENTITY_ONLY", "resources_created": False,
        "resource_state": PreparationResourceState.IDENTITY_ONLY.value,
        "resource_commitment": None, "topology_commitment": None,
        "actual_ownership_commitment": None,
    })
    child.update({
        "resource_state": PreparationResourceState.IDENTITY_ONLY.value,
        "corpus_identity": None, "index_identity": None, "build_identity": None,
        "activation_identity": None, "artifacts": [], "resource_commitment": None,
    })
    readiness = result["readiness_preparation"]
    for name in (
        "runtime_image_identity", "service_health_commitment", "mount_commitment",
        "docker_resource_commitment", "real_rag_resource_commitment"):
        readiness["material"].pop(name, None)
    readiness["state"] = "PREPARED_NOT_VALIDATED_FOR_ADMISSION"
    readiness["commitment"] = commitment(readiness["material"])
    result["resource_facts"] = None
    return result


def _expected_child_marker(prepared: Mapping[str, object]) -> dict[str, object]:
    lineage = prepared.get("planned_lineage")
    child = prepared.get("real_rag_child")
    if not isinstance(lineage, Mapping) or not isinstance(child, Mapping):
        raise ValueError("prepared child lineage is malformed")
    owner = {
        "schema": "spec017.prepared-real-rag-child-ownership.v1",
        "preparation_id": prepared.get("preparation_id"),
        "planned_run_identity": lineage.get("planned_run_identity"),
        "planned_execution_identity": lineage.get("planned_execution_identity"),
        "profile_identity": child.get("profile_identity"),
        "child_identity": child.get("child_identity"),
        "relative_path": child.get("relative_path"),
        "lifecycle_state": "ALLOCATED",
    }
    if commitment(owner) != child.get("ownership_commitment"):
        raise ValueError("prepared child ownership commitment is contradictory")
    return {**owner, "ownership_commitment": child["ownership_commitment"]}


def _child_paths(
    store: SqliteEvaluationStore, prepared: Mapping[str, object],
    repository_root: Path,
) -> tuple[Path, Path]:
    child = prepared["real_rag_child"]
    isolated_root = (store.path.parent / "real_rag").resolve()
    target = (store.path.parent / str(child["relative_path"])).resolve()
    try:
        target.relative_to(isolated_root)
    except ValueError as exc:
        raise ValueError("prepared child escapes its evaluation-owned root") from exc
    rag_profile = yaml.safe_load((
        repository_root / "configs/rca_evaluation/real_rag.yaml"
    ).read_text(encoding="utf-8"))
    knowledge_config = yaml.safe_load((
        repository_root / rag_profile["knowledge"]["config_path"]
    ).read_text(encoding="utf-8"))
    production = (repository_root / knowledge_config["index"]["path"]).resolve()
    if (target == production or target.is_relative_to(production)
            or production.is_relative_to(target)
            or isolated_root == production
            or isolated_root.is_relative_to(production)
            or production.is_relative_to(isolated_root)):
        raise ValueError("prepared child overlaps production/shared Candidate-C root")
    return isolated_root, target


def _issue_prepared_child(
    store: SqliteEvaluationStore, prepared: Mapping[str, object],
    repository_root: Path,
) -> None:
    """Create only the empty allocation directory and integrity marker."""
    isolated_root, target = _child_paths(store, prepared, repository_root)
    expected_marker = _expected_child_marker(prepared)
    marker = target / PREPARED_CHILD_MARKER
    if target.exists():
        # Existing state is accepted only as an exact replay.  Missing markers,
        # extra content, and malformed ownership are never auto-repaired.
        verify_prepared_real_rag_child(
            store, str(prepared["preparation_id"]), repository_root)
        return
    if _real_rag_root_has_state(isolated_root):
        raise ValueError("duplicate or stale Real-RAG child allocation exists")
    isolated_root.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    marker.write_text(json.dumps(expected_marker, sort_keys=True, separators=(",", ":")),
                      encoding="utf-8")


def verify_prepared_real_rag_child(
    store: SqliteEvaluationStore, preparation_id: str,
    repository_root: str | Path,
    *, _preparation_artifacts: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Fresh-read durable allocation and exact allowlisted prepared artifacts."""
    require_identity(preparation_id, "preparation")
    prepared = store.get_preparation(preparation_id)
    if prepared is None:
        raise ValueError("prepared Real-RAG allocation is missing")
    child = prepared["real_rag_child"]
    lineage = prepared["planned_lineage"]
    if (child.get("ownership") != preparation_id
            or child.get("state") != "ALLOCATED"
            or child.get("resource_state") not in {
                None, PreparationResourceState.IDENTITY_ONLY.value,
                PreparationResourceState.PREPARING.value,
                PreparationResourceState.PREPARED_RESOURCES.value}):
        raise ValueError("prepared Real-RAG allocation owner/lifecycle mismatch")
    require_identity(lineage.get("planned_run_identity"), "planned_run")
    require_identity(lineage.get("planned_execution_identity"), "planned_execution")
    isolated_root, target = _child_paths(
        store, prepared, Path(repository_root).resolve())
    if not target.is_dir():
        raise ValueError("prepared Real-RAG child directory is missing")
    marker = target / PREPARED_CHILD_MARKER
    if not marker.is_file():
        raise ValueError("prepared Real-RAG child ownership marker is missing")
    try:
        actual_marker = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("prepared Real-RAG child ownership marker is missing or invalid") from exc
    if actual_marker != _expected_child_marker(prepared):
        raise ValueError("prepared Real-RAG child ownership marker mismatch")
    expected_artifacts = (
        [{"relative_path": relative, "digest": digest}
         for relative, digest in sorted(_preparation_artifacts.items())]
        if _preparation_artifacts is not None else child.get("artifacts", []))
    allowed = {PREPARED_CHILD_MARKER}
    authoritative_artifacts: dict[str, str] = {}
    for artifact in expected_artifacts:
        relative = artifact.get("relative_path") if isinstance(artifact, Mapping) else None
        digest = artifact.get("digest") if isinstance(artifact, Mapping) else None
        if (not isinstance(relative, str) or not relative
                or Path(relative).is_absolute() or ".." in Path(relative).parts
                or not isinstance(digest, str)):
            raise ValueError("prepared Candidate-C artifact manifest is invalid")
        actual = (target / relative).resolve()
        try:
            actual.relative_to(target)
        except ValueError as exc:
            raise ValueError("prepared Candidate-C artifact escapes child") from exc
        filesystem_actual = _filesystem_path(actual)
        if (not filesystem_actual.is_file()
                or "sha256:" + hashlib.sha256(
                    filesystem_actual.read_bytes()).hexdigest() != digest):
            raise ValueError("prepared Candidate-C artifact lineage mismatch")
        allowed.add(Path(relative).as_posix())
        authoritative_artifacts[Path(relative).as_posix()] = digest
    has_candidate_resources = (
        child.get("resource_state") ==
            PreparationResourceState.PREPARED_RESOURCES.value
        or _preparation_artifacts is not None)
    if has_candidate_resources:
        candidate_artifacts = {
            relative.removeprefix("candidate-c/"): digest
            for relative, digest in authoritative_artifacts.items()
            if relative.startswith("candidate-c/")
        }
        if not candidate_artifacts:
            raise ValueError("prepared Candidate-C authoritative artifacts are absent")
        for execution_child, expected_execution_marker in (
                _expected_execution_children(prepared)):
            prefix = f"executions/{execution_child}/"
            marker_relative = prefix + PREPARED_CHILD_MARKER
            marker_path = _filesystem_path(target / marker_relative)
            try:
                actual_execution_marker = json.loads(
                    marker_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "prepared execution-child marker is missing or invalid") from exc
            if actual_execution_marker != expected_execution_marker:
                raise ValueError("prepared execution-child marker mismatch")
            allowed.add(marker_relative)
            for suffix, digest in candidate_artifacts.items():
                relative = prefix + suffix
                actual = _filesystem_path(target / relative)
                if (not actual.is_file() or "sha256:" + hashlib.sha256(
                        actual.read_bytes()).hexdigest() != digest):
                    raise ValueError(
                        "prepared execution-child Candidate-C artifact mismatch")
                allowed.add(relative)
    observed = {relative for relative, _ in _walk_files(target)}
    if observed != allowed:
        raise ValueError("prepared Real-RAG child contains unexpected state")
    if child.get("resource_state") == PreparationResourceState.PREPARED_RESOURCES.value:
        durable = prepared.get("resource_facts")
        facts = ({item.get("domain"): item for item in durable.get("facts", ())}
                 if isinstance(durable, Mapping) else {})
        if (facts.get(PreparationResourceDomain.REAL_RAG_BUILD.value, {}).get(
                "actual_observed_identity") != child.get("build_identity")
                or facts.get(
                    PreparationResourceDomain.REAL_RAG_ACTIVATION.value, {}).get(
                        "actual_observed_identity") != child.get("activation_identity")
                or facts.get(PreparationResourceDomain.REAL_RAG_BUILD.value, {}).get(
                    "expected_target_identity") != child.get("corpus_identity")):
            raise ValueError("prepared Candidate-C build/activation lineage mismatch")
    siblings = tuple(isolated_root.iterdir())
    if len(siblings) != 1 or siblings[0] != target:
        raise ValueError("duplicate or stale Real-RAG child allocation exists")
    return dict(child)


def _artifact_manifest(target: Path) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for relative, path in _walk_files(target):
        if (relative == PREPARED_CHILD_MARKER
                or relative.startswith("executions/")):
            continue
        artifacts[relative] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if not artifacts:
        raise ValueError("Candidate-C preparation produced no authoritative artifacts")
    return artifacts


def observe_authoritative_preparation_facts(
    *, store: SqliteEvaluationStore, preparation_id: str,
    repository_root: str | Path, runtime_authority: object,
    docker_state_inspector: object, candidate_c_authority: object,
    container_real_rag_inspector: object,
    _preparation_artifacts: Mapping[str, str] | None = None,
) -> PreparationResourceFacts:
    """Fresh-read all eight domains through supported resolver/inspector ports."""
    from .container_wiring import (
        ContainerRealRagInspectionTarget, ContainerRealRagReadiness,
        resolve_formal_container_wiring,
    )
    from .formal_compose import resolve_formal_compose_topology
    from .formal_docker_lifecycle import (
        resolve_campaign_docker_lifecycle_required_facts,
    )
    from .readiness import ReadinessFactState
    from .runtime_image_readiness import (
        PreparedRuntimeImage, RuntimeFactState, RuntimeFormalReadinessFacts,
    )

    prepared = store.get_preparation(preparation_id)
    if prepared is None:
        raise ValueError("authoritative resource observation requires PREPARED state")
    planned_run = prepared["planned_lineage"]["planned_run_identity"]
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    runtime = runtime_authority.formal_readiness()
    if type(runtime) is not RuntimeFormalReadinessFacts:
        raise TypeError("typed Runtime image inspector is required")
    runtime.validate()
    source = runtime.source
    if (source.repository_dirty
            or runtime.actual_image.state is not RuntimeFactState.AVAILABLE
            or source.base_image_digest.state is not RuntimeFactState.AVAILABLE):
        raise ValueError("Runtime image inspection is unresolved or contradictory")
    image = runtime.actual_image.value
    image_material = {
        "repository_revision": source.repository_revision,
        "source_commitment": source.source_commitment,
        "dockerfile_commitment": source.dockerfile_commitment,
        "base_image_digest": source.base_image_digest.value,
        "dependency_commitment": source.dependency_commitment,
        "runtime_profile": source.logical_image_identity,
        "image_id": image["image_id"], "image_digest": image["image_digest"],
    }
    inspected_image = PreparedRuntimeImage(
        source.repository_revision, source.source_commitment,
        source.dockerfile_commitment, source.base_image_digest.value,
        source.dependency_commitment, source.logical_image_identity,
        image["image_id"], image["image_digest"],
        commitment(image_material))
    inspected_image.validate()

    topology = resolve_formal_compose_topology(store, preparation_id, repository_root)
    docker_facts = resolve_campaign_docker_lifecycle_required_facts(
        topology, docker_state_inspector)
    docker_by_name = {item.name: item for item in docker_facts}
    for name in ("actual_docker_ownership", "formal_required_service_health",
                 "formal_docker_stale_state", "actual_formal_docker_resources"):
        if docker_by_name[name].state is not ReadinessFactState.RESOLVED:
            raise ValueError(f"authoritative Docker fact is not resolved: {name}")
    docker_resources = docker_by_name["actual_formal_docker_resources"].value
    docker_source = docker_resources["inspection_results_commitment"]

    wiring = resolve_formal_container_wiring(
        store, preparation_id, repository_root,
        candidate_c_authority=candidate_c_authority,
        _preparation_artifacts=_preparation_artifacts)
    if (wiring.build_fact.state is not ReadinessFactState.RESOLVED
            or wiring.activation_fact.state is not ReadinessFactState.RESOLVED):
        raise ValueError("Candidate-C public build/activation is not resolved")
    inspections: list[ContainerRealRagReadiness] = []
    for envelope, mount in zip(topology.envelopes, wiring.mounts):
        target = ContainerRealRagInspectionTarget(
            preparation_id, envelope.compose_project_identity, mount)
        current = container_real_rag_inspector.inspect(target)
        if type(current) is not ContainerRealRagReadiness:
            raise TypeError("typed container Real-RAG inspector is required")
        if (current.preparation_id != preparation_id
                or current.project_identity != envelope.compose_project_identity
                or current.planned_execution_identity !=
                    mount.planned_execution_identity
                or current.child_identity != mount.execution_child_identity
                or current.child_target != mount.child_container_target
                or current.corpus_target != mount.corpus_container_target):
            raise ValueError("container inspector provenance contradicts target")
        current.assert_resolved()
        if (current.build_fact.value != wiring.build_fact.value
                or current.activation_fact.value != wiring.activation_fact.value):
            raise ValueError("container Candidate-C lineage contradicts public authority")
        inspections.append(current)

    child_expected = commitment([item.mount_commitment for item in wiring.mounts])
    child_actual = commitment([item.mount_fact.value for item in inspections])
    corpus_expected = commitment([{
        "target": item.corpus_container_target,
        "manifest": item.manifest_commitment,
        "release": item.corpus_release,
    } for item in wiring.mounts])
    corpus_actual = commitment([item.corpus_fact.value for item in inspections])
    ownership_identity = commitment(docker_by_name["actual_docker_ownership"].value)
    health_identity = commitment(docker_by_name["formal_required_service_health"].value)
    stale_state_identity = commitment(
        docker_by_name["formal_docker_stale_state"].value)
    actual_resource_set_identity = docker_resources[
        "actual_resource_set_commitment"]
    build_identity = str(wiring.build_fact.value)
    activation_identity = commitment(wiring.activation_fact.value)
    manifest_commitment = wiring.mounts[0].manifest_commitment

    return PreparationResourceFacts.create((
        PreparationResourceFact.create(
            PreparationResourceDomain.RUNTIME_IMAGE, preparation_id, planned_run,
            source.logical_image_identity, inspected_image.image_digest,
            observed_at,
            semantic_identity_commitment=
                inspected_image.preparation_commitment),
        PreparationResourceFact.create(
            PreparationResourceDomain.DOCKER_TOPOLOGY, preparation_id, planned_run,
            prepared["docker_namespace"]["namespace_identity"],
            topology.topology_commitment, observed_at),
        PreparationResourceFact.create(
            PreparationResourceDomain.DOCKER_OWNERSHIP, preparation_id, planned_run,
            topology.topology_commitment, ownership_identity, observed_at),
        PreparationResourceFact.create(
            PreparationResourceDomain.DOCKER_HEALTH, preparation_id, planned_run,
            topology.topology_commitment, health_identity, observed_at),
        PreparationResourceFact.create(
            PreparationResourceDomain.DOCKER_STALE_STATE, preparation_id,
            planned_run, topology.topology_commitment, stale_state_identity,
            observed_at),
        PreparationResourceFact.create(
            PreparationResourceDomain.DOCKER_ACTUAL_RESOURCES, preparation_id,
            planned_run, topology.topology_commitment,
            actual_resource_set_identity, observed_at,
            semantic_identity_commitment=docker_source),
        PreparationResourceFact.create(
            PreparationResourceDomain.REAL_RAG_BUILD, preparation_id, planned_run,
            manifest_commitment, build_identity, observed_at),
        PreparationResourceFact.create(
            PreparationResourceDomain.REAL_RAG_ACTIVATION, preparation_id, planned_run,
            build_identity, activation_identity, observed_at),
        PreparationResourceFact.create(
            PreparationResourceDomain.REAL_RAG_CHILD_MOUNT, preparation_id, planned_run,
            child_expected, child_actual, observed_at),
        PreparationResourceFact.create(
            PreparationResourceDomain.REAL_RAG_CORPUS_MOUNT, preparation_id, planned_run,
            corpus_expected, corpus_actual, observed_at),
    ))


def prepare_authoritative_resources(
    *, store: SqliteEvaluationStore, preparation_id: str,
    repository_root: str | Path, runtime_authority: object,
    runtime_image_operations: object, docker_operations: object,
    docker_state_inspector: object, candidate_c_operations: object,
    candidate_c_authority: object, container_real_rag_inspector: object,
) -> PreparationResourceFacts:
    """Supported PREPARED operation; adapters supply capability, never fact values."""
    from .formal_compose import resolve_formal_compose_topology
    from .formal_docker_lifecycle import resolve_preparation_docker_authority
    from .runtime_image_readiness import PreparedRuntimeImage

    if type(store) is not SqliteEvaluationStore:
        raise TypeError("authoritative preparation requires SqliteEvaluationStore")
    prepared = store.get_preparation(preparation_id)
    if prepared is None or prepared["docker_namespace"].get("resource_state") != "IDENTITY_ONLY":
        raise ValueError("authoritative preparation requires identity-only PREPARED state")
    prepared_image = runtime_authority.prepare_image(runtime_image_operations)
    if type(prepared_image) is not PreparedRuntimeImage:
        raise TypeError("Runtime image preparation must return its supported typed result")
    prepared_image.validate()

    topology = resolve_formal_compose_topology(store, preparation_id, repository_root)
    docker_authority = resolve_preparation_docker_authority(
        store, preparation_id, repository_root)
    _, target = _child_paths(store, prepared, Path(repository_root).resolve())
    from knowledge_index import admit_production_manifest
    rag_profile = yaml.safe_load((
        Path(repository_root).resolve()
        / "configs/rca_evaluation/real_rag.yaml").read_text(encoding="utf-8"))
    manifest = admit_production_manifest(
        Path(repository_root).resolve()
        / rag_profile["knowledge"]["manifest_path"],
        repository_root=Path(repository_root).resolve())
    raw_child = prepared["real_rag_child"]
    recovery_request = CandidateCPreparationRequest(
        preparation_id, prepared["planned_lineage"]["planned_run_identity"],
        raw_child["child_identity"], str(target), raw_child["profile_identity"],
        manifest.manifest_commitment, manifest.manifest.corpus_id,
        manifest.manifest.canonical_release_reference,
        manifest.manifest.source_revision,
        "text-embedding-004", "spec014-google-document-query-v1",
        "spec014-chroma-l2-v2", "l2", "spec014-retrieval-v1",
        raw_child["profile_identity"])
    recovered_evidence: CandidateCPreparationEvidence | None = None
    try:
        child = verify_prepared_real_rag_child(
            store, preparation_id, repository_root)
    except ValueError:
        recover = getattr(candidate_c_operations, "recover_exact", None)
        if not callable(recover):
            raise
        recovered_evidence = recover(recovery_request)
        if type(recovered_evidence) is not CandidateCPreparationEvidence:
            raise TypeError("Candidate-C recovery must return public typed evidence")
        recovered_evidence.validate()
        if (recovered_evidence.preparation_id != preparation_id
                or recovered_evidence.child_identity != raw_child["child_identity"]
                or recovered_evidence.manifest_commitment !=
                    manifest.manifest_commitment
                or recovered_evidence.corpus_identity != manifest.manifest.corpus_id):
            raise ValueError("Candidate-C recovery evidence is contradictory")
        child = verify_prepared_real_rag_child(
            store, preparation_id, repository_root,
            _preparation_artifacts=dict(recovered_evidence.artifacts))

    from .container_wiring import resolve_formal_container_wiring
    wiring = resolve_formal_container_wiring(
        store, preparation_id, repository_root,
        candidate_c_authority=(candidate_c_authority
                               if recovered_evidence is not None else None),
        _environment={},
        _preparation_artifacts=(dict(recovered_evidence.artifacts)
                                if recovered_evidence is not None else None))
    request = CandidateCPreparationRequest(
        recovery_request.preparation_id, recovery_request.planned_run_identity,
        recovery_request.child_identity, recovery_request.child_path,
        recovery_request.profile_identity, recovery_request.manifest_commitment,
        recovery_request.corpus_identity, recovery_request.corpus_release,
        recovery_request.source_revision, recovery_request.embedding_model,
        recovery_request.embedding_profile_identity,
        recovery_request.index_schema_identity, recovery_request.distance_metric,
        recovery_request.retrieval_profile_identity,
        recovery_request.gate_profile_identity, wiring.mounts)
    if not callable(getattr(docker_operations, "create_exact", None)):
        raise TypeError("typed preparation Docker operations are required")
    docker_phase = docker_operations.create_exact(
        docker_authority, topology, evidence=recovered_evidence,
        candidate_c_authority=(candidate_c_authority
                               if recovered_evidence is not None else None))
    if type(docker_phase) is not DockerPreparationPhase:
        raise TypeError("Docker Phase 1 must return its supported typed result")
    docker_phase.validate()
    if (docker_phase.preparation_id != preparation_id
            or docker_phase.topology_commitment != topology.topology_commitment
            or docker_phase.project_names != docker_authority.project_names):
        raise ValueError("Docker Phase 1 result contradicts exact authority")

    from .formal_docker_lifecycle import (
        DockerResourceKind, DockerResourceState, OperationalState,
        assess_exact_ownership,
    )
    for envelope in topology.envelopes:
        inspection = docker_state_inspector.inspect(envelope)
        ownership = assess_exact_ownership(envelope, inspection)
        if ownership.state is not OperationalState.AVAILABLE:
            raise ValueError("Docker Phase 1 exact ownership is not established")
        runtime = tuple(
            item for item in inspection.resources
            if item.kind is DockerResourceKind.CONTAINER
            and item.service == "runtime")
        if (len(runtime) != 1
                or (runtime[0].state is DockerResourceState.RUNNING
                    and recovered_evidence is None)):
            raise ValueError("Runtime process is running before Candidate-C authority")
    if not callable(getattr(candidate_c_operations, "prepare_exact", None)):
        raise TypeError("typed Candidate-C preparation operations are required")
    evidence = (recovered_evidence if recovered_evidence is not None
                else candidate_c_operations.prepare_exact(request))
    if type(evidence) is not CandidateCPreparationEvidence:
        raise TypeError("Candidate-C preparation must return public typed evidence")
    evidence.validate()
    if (evidence.preparation_id != preparation_id
            or evidence.child_identity != child["child_identity"]
            or evidence.manifest_commitment != request.manifest_commitment
            or evidence.corpus_identity != request.corpus_identity):
        raise ValueError("Candidate-C evidence contradicts the preparation request")
    artifacts = dict(evidence.artifacts)
    if artifacts != _artifact_manifest(target):
        raise ValueError("Candidate-C evidence contradicts prepared artifacts")

    from .readiness import ReadinessFactState, resolve_candidate_c_build
    from knowledge_index import load_knowledge_config
    config = load_knowledge_config(
        Path(repository_root).resolve() / "configs/knowledge_index.yaml")
    build, activation = resolve_candidate_c_build(
        candidate_c_authority, config,
        expected_manifest_commitment=request.manifest_commitment,
        expected_corpus_identity=request.corpus_identity)
    if (build.state is not ReadinessFactState.RESOLVED
            or activation.state is not ReadinessFactState.RESOLVED
            or build.value != evidence.build_identity
            or commitment(activation.value) != evidence.activation_identity):
        raise ValueError("Candidate-C public authority contradicts preparation evidence")
    verify_prepared_real_rag_child(
        store, preparation_id, repository_root,
        _preparation_artifacts=artifacts)

    if not callable(getattr(docker_operations, "start_exact", None)):
        raise TypeError("typed preparation Docker runtime start is required")
    docker_operations.start_exact(
        docker_phase, docker_authority, topology, evidence,
        candidate_c_authority)

    facts = observe_authoritative_preparation_facts(
        store=store, preparation_id=preparation_id,
        repository_root=repository_root, runtime_authority=runtime_authority,
        docker_state_inspector=docker_state_inspector,
        candidate_c_authority=candidate_c_authority,
        container_real_rag_inspector=container_real_rag_inspector,
        _preparation_artifacts=artifacts)
    if (facts.facts[0].actual_observed_identity != prepared_image.image_digest
            or facts.facts[0].semantic_identity_commitment !=
                prepared_image.preparation_commitment):
        raise ValueError("post-build Runtime image inspection drifted")
    by_domain = {item.domain: item for item in facts.facts}
    corpus_identity = by_domain[
        PreparationResourceDomain.REAL_RAG_BUILD].expected_target_identity
    payload = _prepared_resources_payload(
        prepared, facts=facts, artifacts=artifacts,
        corpus_identity=corpus_identity)
    store._record_prepared_resources(preparation_id, payload)
    return facts


def validate_prepared_resource_freshness(
    *, store: SqliteEvaluationStore, preparation_id: str,
    repository_root: str | Path, runtime_authority: object,
    docker_state_inspector: object, candidate_c_authority: object,
    container_real_rag_inspector: object,
) -> PreparationResourceFacts:
    """Require fresh semantic identity to equal immutable PREPARED observations."""
    prepared = store.get_preparation(preparation_id)
    if (prepared is None
            or prepared["docker_namespace"].get("resource_state") !=
                PreparationResourceState.PREPARED_RESOURCES.value):
        raise ValueError("Final Admission requires authoritative PREPARED resources")
    durable = prepared.get("resource_facts")
    if not isinstance(durable, Mapping):
        raise ValueError("durable PREPARED resource facts are absent")
    fresh = observe_authoritative_preparation_facts(
        store=store, preparation_id=preparation_id,
        repository_root=repository_root, runtime_authority=runtime_authority,
        docker_state_inspector=docker_state_inspector,
        candidate_c_authority=candidate_c_authority,
        container_real_rag_inspector=container_real_rag_inspector)
    durable_semantics = prepared_resource_semantic_identity(durable)
    fresh_semantics = prepared_resource_semantic_identity(fresh)
    if durable_semantics != fresh_semantics:
        raise ValueError("fresh authoritative resources contradict PREPARED facts")
    return fresh


def prepare_formal_campaign(
    *, repository_root: str | Path, preregistration_path: str | Path,
    campaign_key: str, database_path: str | Path, evaluation_root: str | Path,
    repository_state: object | None = None,
) -> PreparationReport:
    """Create or recover the exact preparation record in Candidate-F."""
    # Late imports avoid making formal admission and preparation authorities
    # mutually constructible through module initialization.
    from .formal_admission import _load_preregistration, inspect_repository

    root = Path(repository_root).resolve()
    database = Path(database_path).resolve()
    evaluation = Path(evaluation_root).resolve()
    if not isinstance(campaign_key, str) or not campaign_key.strip():
        raise ValueError("campaign_key must be non-empty")
    _namespace_shape(database, evaluation)
    config = _load_preregistration(preregistration_path)
    state = repository_state or inspect_repository(root)
    required_branch = config["repository"]["required_branch"]
    if (state.branch != required_branch or state.dirty
            or not isinstance(state.commit, str) or len(state.commit) != 40):
        raise ValueError("preparation repository/config lineage is not exact")
    payload = _build_payload(
        repository_root=root, config=config, campaign_key=campaign_key,
        repository_branch=state.branch, repository_commit=state.commit,
        database_path=database)
    with SqliteEvaluationStore(database) as store:
        persisted = store.prepare(payload)
        _issue_prepared_child(store, persisted, root)
    return PreparationReport.from_payload(persisted, database)


def recover_preparation(
    store: SqliteEvaluationStore, preparation_id: str,
) -> PreparationReport:
    """Fresh-read exact prepared identities; never allocates replacements."""
    require_identity(preparation_id, "preparation")
    payload = store.get_preparation(preparation_id)
    if payload is None:
        raise ValueError("preparation does not exist")
    verify_prepared_real_rag_child(
        store, preparation_id, str(payload["repository"]["root"]))
    return PreparationReport.from_payload(payload, store.path)
