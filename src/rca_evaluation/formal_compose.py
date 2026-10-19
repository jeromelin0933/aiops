"""Deterministic, non-executing Formal Compose topology projection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
import re
from typing import Mapping

import yaml

from .identity import commitment, identity, require_identity
from .oracle import SCENARIOS
from .runtime_image_readiness import (
    RUNTIME_IMAGE_PROFILE, RUNTIME_IMAGE_TAG, RuntimeFactState,
    RuntimeReadinessFact,
)
from .sqlite_store import SqliteEvaluationStore


FORMAL_COMPOSE_SCHEMA = "spec017.formal-compose-topology.v1"
FORMAL_COMPOSE_PATH = "docker-compose.formal.yml"
FORMAL_PROMETHEUS_PATH = "docker/prometheus/formal-prometheus.yml"
RUNTIME_CONFIG_PATH = "configs/runtime_orchestration.docker.yaml"
BASE_COMPOSE_PATH = "docker-compose.yml"

_SAFE_RESOURCE = re.compile(r"[a-z0-9][a-z0-9_-]{5,62}")
_REQUIRED_SERVICES = ("loki", "prometheus", "runtime")
_VOLUME_TARGETS = (
    ("formal-logs", "/app/var/rca_evaluation/logs"),
    ("loki-data", "/loki"),
    ("prometheus-data", "/prometheus"),
    ("runtime-events", "/app/events"),
    ("runtime-knowledge", "/app/var/rca_evaluation/knowledge-work"),
    ("runtime-state", "/app/var/runtime_orchestration"),
)
_STATE_VOLUME_OWNERSHIP = tuple(sorted({
    "candidate_a": "runtime-state",
    "candidate_b_evidence": "runtime-state",
    "candidate_c_knowledge": "runtime-knowledge",
    "candidate_d_generation": "runtime-state",
    "correlation_state": "runtime-state",
    "event_store": "runtime-events",
    "formal_logs": "formal-logs",
    "incident_store": "runtime-state",
    "loki_data": "loki-data",
    "prometheus_data": "prometheus-data",
    "runtime_continuation": "runtime-state",
    "runtime_work": "runtime-state",
    "shadow_store": "runtime-state",
}.items()))
_SERVICE_VOLUMES = {
    "runtime": (
        "runtime-events:/app/events",
        "runtime-state:/app/var/runtime_orchestration",
        "runtime-knowledge:/app/var/rca_evaluation/knowledge-work",
        "formal-logs:/app/var/rca_evaluation/logs",
        {"type": "bind",
         "source": "${SPEC017_REAL_RAG_CHILD_SOURCE:?governed Candidate-F child source is required}",
         "target": "/app/var/knowledge_index"},
        {"type": "bind",
         "source": "${SPEC017_REAL_RAG_CORPUS_SOURCE:?governed manifest corpus source is required}",
         "target": "/app/var/rca_evaluation/corpus_source",
         "read_only": True},
    ),
    "prometheus": (
        "./docker/prometheus/formal-prometheus.yml:/etc/prometheus/prometheus.yml:ro",
        "prometheus-data:/prometheus",
    ),
    "loki": ("loki-data:/loki",),
}
_SHARED_NAMES = frozenset({
    "aiops", "default", "dev", "development", "prod", "production",
    "runtime-events", "runtime-state", "runtime-knowledge", "logs",
    "grafana-storage",
})


def _is_shared(value: str) -> bool:
    return (value in _SHARED_NAMES
            or value.startswith(("aiops-", "dev-", "prod-", "production-")))


class FormalServiceClassification(str, Enum):
    FORMAL_REQUIRED = "FORMAL_REQUIRED"
    OPTIONAL_DIAGNOSTIC = "OPTIONAL_DIAGNOSTIC"
    NOT_REQUIRED = "NOT_REQUIRED"


SERVICE_CLASSIFICATION = {
    "runtime": FormalServiceClassification.FORMAL_REQUIRED,
    "loki": FormalServiceClassification.FORMAL_REQUIRED,
    "prometheus": FormalServiceClassification.FORMAL_REQUIRED,
    "promtail": FormalServiceClassification.OPTIONAL_DIAGNOSTIC,
    "grafana": FormalServiceClassification.NOT_REQUIRED,
}


@dataclass(frozen=True, slots=True)
class FormalVolumeMount:
    role: str
    volume_identity: str
    target: str

    def material(self) -> dict[str, str]:
        return {"role": self.role, "volume_identity": self.volume_identity,
                "target": self.target}


@dataclass(frozen=True, slots=True)
class FormalComposeEnvelope:
    schema: str
    preparation_id: str
    planned_run_identity: str
    planned_execution_identity: str
    scenario_id: str
    repetition: int
    namespace_identity: str
    compose_project_identity: str
    container_name_prefixes: tuple[str, ...]
    network_identity: str
    volume_mounts: tuple[FormalVolumeMount, ...]
    state_volume_ownership: tuple[tuple[str, str], ...]
    workspace_identity: str
    log_identity: str
    services: tuple[str, ...]
    service_classification: tuple[tuple[str, str], ...]
    runtime_image_profile: str
    runtime_image_tag: str
    compose_artifact_commitment: str
    base_compose_commitment: str
    prometheus_config_commitment: str
    runtime_config_commitment: str
    host_port_policy: str
    shared_resource_guard: str
    real_rag_mount_slot: str
    actual_resources: RuntimeReadinessFact
    topology_commitment: str

    def material(self, *, include_commitment: bool = True) -> dict[str, object]:
        result = {
            "schema": self.schema,
            "preparation_id": self.preparation_id,
            "planned_run_identity": self.planned_run_identity,
            "planned_execution_identity": self.planned_execution_identity,
            "scenario_id": self.scenario_id,
            "repetition": self.repetition,
            "namespace_identity": self.namespace_identity,
            "compose_project_identity": self.compose_project_identity,
            "container_name_prefixes": list(self.container_name_prefixes),
            "network_identity": self.network_identity,
            "volume_mounts": [item.material() for item in self.volume_mounts],
            "state_volume_ownership": dict(self.state_volume_ownership),
            "workspace_identity": self.workspace_identity,
            "log_identity": self.log_identity,
            "services": list(self.services),
            "service_classification": dict(self.service_classification),
            "runtime_image_profile": self.runtime_image_profile,
            "runtime_image_tag": self.runtime_image_tag,
            "compose_artifact_commitment": self.compose_artifact_commitment,
            "base_compose_commitment": self.base_compose_commitment,
            "prometheus_config_commitment": self.prometheus_config_commitment,
            "runtime_config_commitment": self.runtime_config_commitment,
            "host_port_policy": self.host_port_policy,
            "shared_resource_guard": self.shared_resource_guard,
            "real_rag_mount_slot": self.real_rag_mount_slot,
            "actual_resources": self.actual_resources.material(),
        }
        if include_commitment:
            result["topology_commitment"] = self.topology_commitment
        return result

    def validate(self) -> None:
        require_identity(self.preparation_id, "preparation")
        require_identity(self.planned_run_identity, "planned_run")
        require_identity(self.planned_execution_identity, "planned_execution")
        require_identity(self.namespace_identity, "docker_namespace")
        paths = (self.workspace_identity, self.log_identity)
        volume_ids = tuple(item.volume_identity for item in self.volume_mounts)
        if (self.schema != FORMAL_COMPOSE_SCHEMA
                or self.scenario_id not in SCENARIOS
                or type(self.repetition) is not int or not 1 <= self.repetition <= 5
                or _is_shared(self.compose_project_identity)
                or not _SAFE_RESOURCE.fullmatch(self.compose_project_identity)
                or self.network_identity != f"{self.compose_project_identity}_formal"
                or self.services != _REQUIRED_SERVICES
                or self.state_volume_ownership != _STATE_VOLUME_OWNERSHIP
                or self.runtime_image_profile != RUNTIME_IMAGE_PROFILE
                or self.runtime_image_tag != RUNTIME_IMAGE_TAG
                or self.host_port_policy != "NONE"
                or self.shared_resource_guard != "PASS"
                or self.real_rag_mount_slot != "/app/var/knowledge_index"
                or self.actual_resources.state is not RuntimeFactState.UNRESOLVED
                or any(PurePosixPath(path).is_absolute()
                       or ".." in PurePosixPath(path).parts for path in paths)
                or any(_is_shared(value)
                       or not value.startswith(self.compose_project_identity + "_")
                       for value in volume_ids)
                or any(not prefix.startswith(self.compose_project_identity + "-")
                       for prefix in self.container_name_prefixes)
                or commitment(self.material(include_commitment=False))
                    != self.topology_commitment):
            raise ValueError("Formal Compose topology is contradictory or shared")


@dataclass(frozen=True, slots=True)
class FormalComposeCampaignTopology:
    schema: str
    preparation_id: str
    envelopes: tuple[FormalComposeEnvelope, ...]
    topology_commitment: str

    def material(self, *, include_commitment: bool = True) -> dict[str, object]:
        result = {"schema": self.schema, "preparation_id": self.preparation_id,
                  "envelopes": [item.material() for item in self.envelopes]}
        if include_commitment:
            result["topology_commitment"] = self.topology_commitment
        return result

    def validate(self) -> None:
        for envelope in self.envelopes:
            envelope.validate()
        expected = tuple((scenario, repetition) for repetition in range(1, 6)
                         for scenario in SCENARIOS)
        observed = tuple((item.scenario_id, item.repetition)
                         for item in self.envelopes)
        projects = {item.compose_project_identity for item in self.envelopes}
        resources = {
            resource for item in self.envelopes
            for resource in (item.network_identity,
                             *(mount.volume_identity for mount in item.volume_mounts))
        }
        if (self.schema != FORMAL_COMPOSE_SCHEMA
                or observed != expected or len(self.envelopes) != 30
                or len(projects) != 30 or len(resources) != 30 * 7
                or commitment(self.material(include_commitment=False))
                    != self.topology_commitment):
            raise ValueError("Formal Compose campaign topology is contradictory")


def _source(root: Path, relative: str) -> tuple[str, str]:
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError(f"Formal Compose source is unavailable: {relative}")
    text = path.read_text(encoding="utf-8")
    return text, commitment({"path": relative, "content": text})


def _validate_artifact(root: Path) -> tuple[str, str, str, str]:
    compose_text, compose_hash = _source(root, FORMAL_COMPOSE_PATH)
    _, base_hash = _source(root, BASE_COMPOSE_PATH)
    _, prometheus_hash = _source(root, FORMAL_PROMETHEUS_PATH)
    _, runtime_hash = _source(root, RUNTIME_CONFIG_PATH)
    raw = yaml.safe_load(compose_text)
    services = raw.get("services") if isinstance(raw, Mapping) else None
    volumes = raw.get("volumes") if isinstance(raw, Mapping) else None
    networks = raw.get("networks") if isinstance(raw, Mapping) else None
    metadata = raw.get("x-spec017-formal") if isinstance(raw, Mapping) else None
    if (not isinstance(services, Mapping) or tuple(sorted(services)) != _REQUIRED_SERVICES
            or not isinstance(volumes, Mapping)
            or set(volumes) != {role for role, _ in _VOLUME_TARGETS}
            or not isinstance(networks, Mapping) or set(networks) != {"formal"}
            or not isinstance(metadata, Mapping)
            or metadata.get("runtime_image_profile") != RUNTIME_IMAGE_PROFILE
            or metadata.get("service_classification") != {
                name: value.value for name, value in SERVICE_CLASSIFICATION.items()}
            or metadata.get("future_slots") != {
                "real_rag_child_mount": "/app/var/knowledge_index",
                "real_rag_corpus_mount": "/app/var/rca_evaluation/corpus_source",
                "credential_wiring": "EXTERNAL_ENVIRONMENT_NAME_ONLY"}
            or services["runtime"].get("image") != RUNTIME_IMAGE_TAG
            or services["runtime"].get("command") != [
                "python", "scripts/run_correlation_runtime.py", "--config",
                RUNTIME_CONFIG_PATH]
            or services["runtime"].get("environment") != ["GEMINI_API_KEY"]
            or set(services["runtime"].get("depends_on", []))
                != {"loki", "prometheus"}
            or services["prometheus"].get("image") != "prom/prometheus:v2.51.0"
            or services["loki"].get("image") != "grafana/loki:2.9.0"):
        raise ValueError("Formal Compose artifact schema is invalid")
    serialized = yaml.safe_dump(raw, sort_keys=True)
    if ("container_name" in serialized or "ports:" in serialized
            or "./logs" in serialized):
        raise ValueError("Formal Compose artifact uses a shared/global resource")
    for service in services.values():
        if service.get("networks") != ["formal"]:
            raise ValueError("Formal service is outside the isolated network")
    if any(tuple(service.get("volumes", ())) != _SERVICE_VOLUMES[name]
           for name, service in services.items()):
        raise ValueError("Formal service volume topology is contradictory")
    return compose_hash, base_hash, prometheus_hash, runtime_hash


def _envelope(prepared: Mapping[str, object], *, scenario_id: str,
              repetition: int, source: tuple[str, str, str, str]) -> FormalComposeEnvelope:
    if scenario_id not in SCENARIOS or type(repetition) is not int or not 1 <= repetition <= 5:
        raise ValueError("Formal execution slot is invalid")
    lineage = prepared["planned_lineage"]
    docker = prepared["docker_namespace"]
    run_id = lineage["planned_run_identity"]
    planned_execution = identity(
        "planned_execution", [run_id, repetition, scenario_id])
    suffix = commitment({"namespace": docker["namespace_identity"],
                         "planned_execution": planned_execution})[7:19]
    project = f"{docker['project_name']}-{suffix}"
    volumes = tuple(FormalVolumeMount(
        role, f"{project}_{role}", target) for role, target in _VOLUME_TARGETS)
    classifications = tuple(sorted(
        (name, value.value) for name, value in SERVICE_CLASSIFICATION.items()))
    unresolved = RuntimeReadinessFact(
        "actual_formal_docker_resources", RuntimeFactState.UNRESOLVED, None,
        "container/network/volume existence is not executed in Slice 2")
    compose_hash, base_hash, prometheus_hash, runtime_hash = source
    values = dict(
        schema=FORMAL_COMPOSE_SCHEMA,
        preparation_id=prepared["preparation_id"],
        planned_run_identity=run_id,
        planned_execution_identity=planned_execution,
        scenario_id=scenario_id, repetition=repetition,
        namespace_identity=docker["namespace_identity"],
        compose_project_identity=project,
        container_name_prefixes=tuple(
            f"{project}-{service}-" for service in _REQUIRED_SERVICES),
        network_identity=f"{project}_formal", volume_mounts=volumes,
        state_volume_ownership=_STATE_VOLUME_OWNERSHIP,
        workspace_identity=f"docker/{project}",
        log_identity=f"docker/{project}/logs",
        services=_REQUIRED_SERVICES,
        service_classification=classifications,
        runtime_image_profile=RUNTIME_IMAGE_PROFILE,
        runtime_image_tag=RUNTIME_IMAGE_TAG,
        compose_artifact_commitment=compose_hash,
        base_compose_commitment=base_hash,
        prometheus_config_commitment=prometheus_hash,
        runtime_config_commitment=runtime_hash,
        host_port_policy="NONE", shared_resource_guard="PASS",
        real_rag_mount_slot="/app/var/knowledge_index",
        actual_resources=unresolved,
    )
    provisional = FormalComposeEnvelope(**values, topology_commitment="sha256:" + "0" * 64)
    result = FormalComposeEnvelope(
        **values, topology_commitment=commitment(
            provisional.material(include_commitment=False)))
    result.validate()
    return result


def resolve_formal_compose_topology(
    store: SqliteEvaluationStore, preparation_id: str,
    repository_root: str | Path,
) -> FormalComposeCampaignTopology:
    """Fresh-read Candidate-F preparation and derive all 30 expected envelopes."""
    if type(store) is not SqliteEvaluationStore:
        raise TypeError("Formal Compose topology requires SqliteEvaluationStore")
    root = Path(repository_root).resolve()
    prepared = store.get_preparation(preparation_id)
    if prepared is None:
        raise ValueError("Formal Compose topology requires PREPARED state")
    docker = prepared["docker_namespace"]
    repository = prepared.get("repository")
    resource_state = docker.get("resource_state", "IDENTITY_ONLY")
    state_pair = (docker.get("state"), docker.get("resources_created"))
    valid_state = (
        resource_state == "IDENTITY_ONLY"
        and state_pair == ("PREPARED_IDENTITY_ONLY", False)
        or resource_state == "PREPARING"
        and state_pair[1] is False
        or resource_state == "PREPARED_RESOURCES"
        and state_pair == ("PREPARED_RESOURCES", True)
    )
    if (not isinstance(repository, Mapping)
            or Path(str(repository.get("root", ""))).resolve() != root
            or not valid_state
            or _is_shared(docker.get("project_name", ""))):
        raise ValueError("prepared Docker namespace is shared or contradictory")
    source = _validate_artifact(root)
    envelopes = tuple(
        _envelope(prepared, scenario_id=scenario, repetition=repetition,
                  source=source)
        for repetition in range(1, 6) for scenario in SCENARIOS)
    provisional = FormalComposeCampaignTopology(
        FORMAL_COMPOSE_SCHEMA, preparation_id, envelopes, "sha256:" + "0" * 64)
    result = FormalComposeCampaignTopology(
        FORMAL_COMPOSE_SCHEMA, preparation_id, envelopes,
        commitment(provisional.material(include_commitment=False)))
    result.validate()
    durable_topology = docker.get("topology_commitment")
    if (resource_state == "PREPARED_RESOURCES"
            and durable_topology != result.topology_commitment):
        raise ValueError("fresh Formal Compose topology contradicts PREPARED facts")
    return result
