"""Production external-boundary adapters for SPEC-017 preparation.

These adapters act or observe only.  Existing Candidate-F and Candidate-C
semantic layers remain the authorities for readiness, ownership, build, and
activation facts.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable, Mapping

import yaml

from .container_wiring import (
    CREDENTIAL_ENVIRONMENT, ContainerRealRagInspectionTarget,
    ContainerRealRagReadiness, FormalRealRagMount,
    materialize_formal_compose_environment,
    resolve_container_real_rag_readiness, resolve_formal_container_wiring,
)
from .external_commands import (
    CommandExecutionError, CommandExecutor, CommandResult,
    SubprocessCommandExecutor, require_success,
)
from .formal_compose import (
    FORMAL_COMPOSE_PATH, FormalComposeCampaignTopology, FormalComposeEnvelope,
)
from .formal_docker_lifecycle import (
    DockerProjectInspection, DockerResourceKind, DockerResourceState,
    InspectedDockerResource, PreparationDockerAuthority,
    expected_ownership_labels,
)
from .preparation import (
    CandidateCPreparationEvidence, CandidateCPreparationRequest,
    DockerPreparationPhase, PREPARED_CHILD_MARKER, PreparationResourceFacts,
    _artifact_manifest, prepare_authoritative_resources,
)
from .identity import commitment
from .runtime_image_readiness import RuntimeImageSourceIdentity
from .sqlite_store import SqliteEvaluationStore


_SHA256 = "sha256:"
_OWNERSHIP_KEYS = (
    "com.docker.compose.project", "spec017.preparation_id",
    "spec017.planned_execution_identity", "spec017.topology_commitment",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fs_path(path: Path) -> Path:
    """Keep deterministic long Candidate-F identities usable on Windows."""
    value = str(path)
    if os.name == "nt" and path.is_absolute() and not value.startswith("\\\\?\\"):
        return Path("\\\\?\\" + value)
    return path


def _run(
    executor: CommandExecutor, argv: tuple[str, ...], root: Path,
    operation: str, *, environment: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> str:
    return require_success(executor.execute(
        argv, cwd=root, environment=environment, timeout_seconds=timeout), operation)


class DockerRuntimeImagePreparationOperations:
    """Build only the resolver-issued, content-bound Runtime image."""

    def __init__(self, repository_root: str | Path, *,
                 executor: CommandExecutor | None = None,
                 timeout_seconds: float = 900.0) -> None:
        self._root = Path(repository_root).resolve()
        self._executor = executor or SubprocessCommandExecutor()
        self._timeout = timeout_seconds

    def build_exact(self, source: RuntimeImageSourceIdentity) -> None:
        if type(source) is not RuntimeImageSourceIdentity:
            raise TypeError("Runtime build requires resolver-issued source identity")
        source.validate()
        if (source.repository_dirty or source.image_tag.endswith(":latest")
                or source.base_image_digest.value is None
                or source.base_image_digest.value not in source.base_image_reference):
            raise ValueError("Runtime build source is not exact")
        dockerfile = (self._root / source.dockerfile_path).resolve()
        dockerfile.relative_to(self._root)
        if not dockerfile.is_file():
            raise ValueError("Runtime Dockerfile is unavailable")
        argv = (
            "docker", "build", "--file", str(dockerfile),
            "--tag", source.image_tag,
            "--build-arg", f"SOURCE_REVISION={source.repository_revision}",
            "--build-arg",
            f"RUNTIME_IMAGE_SOURCE_COMMITMENT={source.source_commitment}",
            "--pull=false", str(self._root),
        )
        _run(self._executor, argv, self._root, "exact Runtime image build",
             timeout=self._timeout)


def _ownership_override(envelope: FormalComposeEnvelope) -> str:
    labels = dict(expected_ownership_labels(envelope))
    labels["io.aiops.runtime.profile"] = envelope.runtime_image_profile
    service_values = {name: {"labels": labels} for name in envelope.services}
    volume_values = {
        mount.role: {"labels": dict(expected_ownership_labels(envelope))}
        for mount in envelope.volume_mounts
    }
    return yaml.safe_dump({
        "services": service_values,
        "networks": {"formal": {"labels": dict(expected_ownership_labels(envelope))}},
        "volumes": volume_values,
    }, sort_keys=True)


class ComposePreparationDockerOperations:
    """Create only the exact 30 evaluation-owned Compose projects."""

    def __init__(
        self, store: SqliteEvaluationStore, preparation_id: str,
        repository_root: str | Path, *, executor: CommandExecutor | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._store = store
        self._preparation_id = preparation_id
        self._root = Path(repository_root).resolve()
        self._executor = executor or SubprocessCommandExecutor()
        self._timeout = timeout_seconds

    def _validate_phase(
        self, authority: PreparationDockerAuthority,
        topology: FormalComposeCampaignTopology,
    ) -> tuple[str, ...]:
        if type(authority) is not PreparationDockerAuthority:
            raise TypeError("typed preparation Docker authority is required")
        if type(topology) is not FormalComposeCampaignTopology:
            raise TypeError("typed Formal Compose topology is required")
        authority.validate()
        topology.validate()
        projects = tuple(item.compose_project_identity for item in topology.envelopes)
        if (authority.preparation_id != self._preparation_id
                or topology.preparation_id != self._preparation_id
                or authority.project_names != projects
                or authority.topology_commitment != topology.topology_commitment):
            raise ValueError("Docker creation authority contradicts exact topology")
        return projects

    def create_exact(
        self, authority: PreparationDockerAuthority,
        topology: FormalComposeCampaignTopology,
        *, evidence: CandidateCPreparationEvidence | None = None,
        candidate_c_authority: object | None = None,
    ) -> DockerPreparationPhase:
        projects = self._validate_phase(authority, topology)
        if evidence is not None:
            if type(evidence) is not CandidateCPreparationEvidence:
                raise TypeError("typed Candidate-C recovery evidence is required")
            evidence.validate()
            if (evidence.preparation_id != self._preparation_id
                    or candidate_c_authority is None):
                raise ValueError("Candidate-C recovery evidence is contradictory")
        compose = (self._root / FORMAL_COMPOSE_PATH).resolve()
        compose.relative_to(self._root)
        if not compose.is_file():
            raise ValueError("Formal Compose artifact is unavailable")
        with tempfile.TemporaryDirectory(prefix="spec017-compose-") as temporary:
            temporary_root = Path(temporary)
            for index, envelope in enumerate(topology.envelopes):
                override = temporary_root / f"ownership-{index:02d}.yaml"
                override.write_text(_ownership_override(envelope), encoding="utf-8")
                environment = (
                    materialize_formal_compose_environment(
                        self._store, self._preparation_id,
                        envelope.planned_execution_identity, self._root)
                    if evidence is None else
                    materialize_formal_compose_environment(
                        self._store, self._preparation_id,
                        envelope.planned_execution_identity, self._root,
                        candidate_c_authority=candidate_c_authority,
                        preparation_artifacts=dict(evidence.artifacts),
                        require_candidate_c=True))
                child_source = Path(
                    environment["SPEC017_REAL_RAG_CHILD_SOURCE"]).resolve()
                isolated_root = (self._store.path.parent / "real_rag").resolve()
                child_source.relative_to(isolated_root)
                child_source.mkdir(parents=True, exist_ok=True)
                argv = (
                    "docker", "compose", "--file", str(compose), "--file",
                    str(override), "--project-name",
                    envelope.compose_project_identity, "create",
                    "--no-build", "--pull", "never", *envelope.services,
                )
                _run(self._executor, argv, self._root,
                     "exact Formal Compose creation", environment=environment,
                     timeout=self._timeout)
        phase = DockerPreparationPhase(
            self._preparation_id, topology.topology_commitment, projects)
        phase.validate()
        return phase

    def start_exact(
        self, phase: DockerPreparationPhase,
        authority: PreparationDockerAuthority,
        topology: FormalComposeCampaignTopology,
        evidence: CandidateCPreparationEvidence,
        candidate_c_authority: object,
    ) -> None:
        projects = self._validate_phase(authority, topology)
        if type(phase) is not DockerPreparationPhase:
            raise TypeError("typed Docker Phase 1 result is required")
        if type(evidence) is not CandidateCPreparationEvidence:
            raise TypeError("typed Candidate-C preparation evidence is required")
        phase.validate()
        evidence.validate()
        if (phase.preparation_id != self._preparation_id
                or phase.topology_commitment != topology.topology_commitment
                or phase.project_names != projects
                or evidence.preparation_id != self._preparation_id):
            raise ValueError("runtime start continuation identity is contradictory")
        compose = (self._root / FORMAL_COMPOSE_PATH).resolve()
        artifacts = dict(evidence.artifacts)
        with tempfile.TemporaryDirectory(prefix="spec017-compose-") as temporary:
            temporary_root = Path(temporary)
            for index, envelope in enumerate(topology.envelopes):
                override = temporary_root / f"ownership-{index:02d}.yaml"
                override.write_text(_ownership_override(envelope), encoding="utf-8")
                environment = materialize_formal_compose_environment(
                    self._store, self._preparation_id,
                    envelope.planned_execution_identity, self._root,
                    candidate_c_authority=candidate_c_authority,
                    preparation_artifacts=artifacts, require_candidate_c=True)
                argv = (
                    "docker", "compose", "--file", str(compose), "--file",
                    str(override), "--project-name",
                    envelope.compose_project_identity, "start", *envelope.services,
                )
                _run(self._executor, argv, self._root,
                     "exact Formal Compose runtime start", environment=environment,
                     timeout=self._timeout)


def _json_list(value: str, operation: str) -> list[object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{operation} returned malformed JSON") from exc
    if not isinstance(decoded, list):
        raise ValueError(f"{operation} returned malformed JSON")
    return decoded


def _ids(value: str) -> tuple[str, ...]:
    result = tuple(line.strip() for line in value.splitlines() if line.strip())
    if any(any(character.isspace() for character in item) for item in result):
        raise ValueError("Docker identifier output is malformed")
    if len(set(result)) != len(result):
        raise ValueError("Docker identifier output is ambiguous")
    return result


def _labels(item: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    raw = item.get("Labels")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("Docker labels are malformed")
    if any(not isinstance(key, str) or not isinstance(value, str)
           for key, value in raw.items()):
        raise ValueError("Docker labels are malformed")
    return tuple(sorted((key, raw.get(key, "")) for key in _OWNERSHIP_KEYS))


def _observed_mounts(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise ValueError("Docker container mounts are malformed")
    observed: list[tuple[str, str]] = []
    destinations: set[str] = set()
    for mount in value:
        if not isinstance(mount, Mapping):
            raise ValueError("Docker container mount is malformed")
        kind = mount.get("Type")
        destination = mount.get("Destination")
        if (kind not in {"volume", "bind", "tmpfs"}
                or not isinstance(destination, str) or not destination
                or destination in destinations):
            raise ValueError("Docker container mount is malformed or ambiguous")
        destinations.add(destination)
        if kind == "volume":
            name = mount.get("Name")
            if not isinstance(name, str) or not name:
                raise ValueError("Docker volume mount is malformed")
            observed.append((name, destination))
        elif kind == "bind":
            source = mount.get("Source")
            if not isinstance(source, str) or not source:
                raise ValueError("Docker bind mount is malformed")
    return tuple(sorted(observed))


def _canonical_host_path(value: str) -> str:
    """Canonicalize only documented Docker Desktop host-path translations."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Docker bind source is malformed")
    translated = value.replace("\\", "/")
    match = re.fullmatch(
        r"/(?:host_mnt/|run/desktop/mnt/host/)?([A-Za-z])(?:/|$)(.*)",
        translated)
    if os.name == "nt" and match:
        translated = f"{match.group(1)}:/{match.group(2)}"
    if os.name == "nt":
        return ntpath.normcase(ntpath.normpath(str(Path(translated).resolve())))
    return os.path.normcase(os.path.normpath(str(Path(translated).resolve())))


def _exact_bind_mounts(value: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(value, list):
        raise ValueError("target Runtime container mounts are malformed")
    result: dict[str, Mapping[str, object]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("target Runtime container mount is malformed")
        kind = item.get("Type")
        destination = item.get("Destination")
        rw = item.get("RW")
        if (kind not in {"bind", "volume", "tmpfs"}
                or not isinstance(destination, str) or not destination
                or not isinstance(rw, bool) or destination in result):
            raise ValueError("target Runtime container mount is malformed or ambiguous")
        if kind == "bind":
            if not isinstance(item.get("Source"), str) or not item.get("Source"):
                raise ValueError("target Runtime bind mount is malformed")
        elif kind == "volume":
            if not isinstance(item.get("Name"), str) or not item.get("Name"):
                raise ValueError("target Runtime volume mount is malformed")
        result[destination] = item
    return result


def _relative_file_names(output: str, operation: str) -> tuple[str, ...]:
    names = tuple(line.strip().replace("\\", "/")
                  for line in output.splitlines() if line.strip())
    if len(set(names)) != len(names):
        raise ValueError(f"{operation} returned ambiguous files")
    for name in names:
        path = Path(name)
        if path.is_absolute() or not name or ".." in path.parts:
            raise ValueError(f"{operation} returned malformed files")
    return tuple(sorted(names))


class DockerCliStateInspector:
    """Read-only projection of current Docker resources into the existing DTO."""

    def __init__(self, repository_root: str | Path, *,
                 executor: CommandExecutor | None = None,
                 timeout_seconds: float = 30.0,
                 clock: Callable[[], str] = _utc_now) -> None:
        self._root = Path(repository_root).resolve()
        self._executor = executor or SubprocessCommandExecutor()
        self._timeout = timeout_seconds
        self._clock = clock

    def _query_ids(self, kind: str, envelope: FormalComposeEnvelope) -> tuple[str, ...]:
        all_states = ("--all",) if kind == "container" else ()
        output = _run(self._executor, (
            "docker", kind, "ls", *all_states, "-q", "--filter",
            f"label=com.docker.compose.project={envelope.compose_project_identity}",
        ), self._root, f"Docker {kind} listing", timeout=self._timeout)
        return _ids(output)

    def _inspect(self, kind: str, identities: set[str]) -> list[Mapping[str, object]]:
        if not identities:
            return []
        output = _run(self._executor, (
            "docker", kind, "inspect", *tuple(sorted(identities)),
        ), self._root, f"Docker {kind} inspection", timeout=self._timeout)
        values = _json_list(output, f"Docker {kind} inspection")
        if any(not isinstance(item, Mapping) for item in values):
            raise ValueError(f"Docker {kind} inspection returned malformed resources")
        unique: dict[str, Mapping[str, object]] = {}
        for item in values:
            assert isinstance(item, Mapping)
            key = item.get("Id") or item.get("ID") or item.get("Name")
            if not isinstance(key, str) or not key:
                raise ValueError(f"Docker {kind} inspection returned an unidentified resource")
            if key in unique:
                raise ValueError(f"Docker {kind} inspection returned duplicate identity")
            unique[key] = item
        return list(unique.values())

    def _expected_if_present(self, kind: str, names: set[str]) -> set[str]:
        present: set[str] = set()
        for name in sorted(names):
            result = self._executor.execute(
                ("docker", kind, "inspect", name), cwd=self._root,
                timeout_seconds=self._timeout)
            if type(result) is not CommandResult:
                raise TypeError("command executor returned an invalid result")
            if result.returncode == 0:
                present.add(name)
            elif result.returncode not in (1,):
                raise CommandExecutionError(
                    f"Docker {kind} exact-name inspection failed")
        return present

    def inspect(self, envelope: FormalComposeEnvelope) -> DockerProjectInspection:
        if type(envelope) is not FormalComposeEnvelope:
            raise TypeError("typed Formal Compose envelope is required")
        envelope.validate()
        compose_values = yaml.safe_load((self._root / FORMAL_COMPOSE_PATH).read_text(
            encoding="utf-8"))
        if (not isinstance(compose_values, Mapping)
                or not isinstance(compose_values.get("services"), Mapping)):
            raise ValueError("Formal Compose service configuration is malformed")
        runtime_image_items = _json_list(_run(self._executor, (
            "docker", "image", "inspect", envelope.runtime_image_tag,
        ), self._root, "Docker Runtime image inspection",
            timeout=self._timeout), "Docker Runtime image inspection")
        if (len(runtime_image_items) != 1
                or not isinstance(runtime_image_items[0], Mapping)
                or not str(runtime_image_items[0].get("Id", "")).startswith(_SHA256)):
            raise ValueError("Docker Runtime image identity is malformed")
        container_ids = set(self._query_ids("container", envelope))
        for prefix in envelope.container_name_prefixes:
            output = _run(self._executor, (
                "docker", "container", "ls", "--all", "-q", "--filter",
                f"name=^/{prefix}",
            ), self._root, "Docker exact-name container listing",
                timeout=self._timeout)
            container_ids.update(_ids(output))
        container_items = self._inspect("container", container_ids)
        resources: list[InspectedDockerResource] = []
        for item in container_items:
            raw_name = item.get("Name")
            if not isinstance(raw_name, str) or not raw_name:
                raise ValueError("Docker container identity is malformed")
            name = raw_name.lstrip("/")
            config = item.get("Config")
            state = item.get("State")
            network = item.get("NetworkSettings")
            if not all(isinstance(value, Mapping)
                       for value in (config, state, network)):
                raise ValueError("Docker container inspection is malformed")
            labels = config.get("Labels")  # type: ignore[union-attr]
            if labels is None:
                labels = {}
            if not isinstance(labels, Mapping):
                raise ValueError("Docker container labels are malformed")
            service = labels.get("com.docker.compose.service")
            networks = network.get("Networks")  # type: ignore[union-attr]
            if networks is None:
                networks = {}
            if (not isinstance(service, str) or not service
                    or not isinstance(networks, Mapping)
                    or any(not isinstance(key, str) or not key
                           or not isinstance(value, Mapping)
                           for key, value in networks.items())):
                raise ValueError("Docker container service/network observation is malformed")
            image_reference = config.get("Image")
            image_identity = item.get("Image")
            if (not isinstance(image_reference, str) or not image_reference
                    or not isinstance(image_identity, str) or not image_identity):
                raise ValueError("Docker container image observation is malformed")
            status = state.get("Status")  # type: ignore[union-attr]
            if status not in {"running", "exited", "created", "dead", "paused",
                              "restarting", "removing"}:
                raise ValueError("Docker container status observation is malformed")
            resource_state = {
                "running": DockerResourceState.RUNNING,
                "exited": DockerResourceState.STOPPED,
                "created": DockerResourceState.PRESENT,
                "dead": DockerResourceState.FAILED,
            }.get(status, DockerResourceState.FAILED)
            health_value = state.get("Health")  # type: ignore[union-attr]
            if health_value is not None and not isinstance(health_value, Mapping):
                raise ValueError("Docker container health observation is malformed")
            health_status = (health_value.get("Status")
                             if isinstance(health_value, Mapping) else None)
            if (health_value is not None and health_status not in {
                    "starting", "healthy", "unhealthy"}):
                raise ValueError("Docker container health status is malformed")
            observed_mounts = _observed_mounts(item.get("Mounts"))
            resources.append(InspectedDockerResource(
                DockerResourceKind.CONTAINER, name, resource_state,
                _labels({"Labels": labels}), service=service,
                image_profile=str(labels.get("io.aiops.runtime.profile", "")),
                image_identity=image_identity, mounts=observed_mounts,
                image_reference=image_reference,
                networks=tuple(sorted(networks)), observed_status=status,
                observed_health_status=health_status))

        services = [item.service for item in resources
                    if item.kind is DockerResourceKind.CONTAINER]
        if len(services) != len(set(services)):
            raise ValueError("Docker container project/service identity is ambiguous")

        expected_networks = {envelope.network_identity}
        network_ids = set(self._query_ids("network", envelope))
        network_ids.update(network for item in resources
                           if item.kind is DockerResourceKind.CONTAINER
                           for network in item.networks)
        network_ids.update(self._expected_if_present("network", expected_networks))
        for item in self._inspect("network", network_ids):
            resources.append(InspectedDockerResource(
                DockerResourceKind.NETWORK, str(item.get("Name", "")),
                DockerResourceState.PRESENT, _labels(item)))

        expected_volumes = {item.volume_identity for item in envelope.volume_mounts}
        volume_ids = set(self._query_ids("volume", envelope))
        volume_ids.update(self._expected_if_present("volume", expected_volumes))
        for item in self._inspect("volume", volume_ids):
            resources.append(InspectedDockerResource(
                DockerResourceKind.VOLUME, str(item.get("Name", "")),
                DockerResourceState.PRESENT, _labels(item)))

        workspace = (self._root / envelope.workspace_identity).resolve()
        try:
            workspace.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("Docker workspace observation escapes repository") from exc
        if workspace.is_dir():
            resources.append(InspectedDockerResource(
                DockerResourceKind.WORKSPACE, envelope.workspace_identity,
                DockerResourceState.PRESENT, ()))
        resource_keys = [(item.kind, item.identity) for item in resources]
        if len(resource_keys) != len(set(resource_keys)):
            raise ValueError("Docker inspection returned duplicate resource identity")
        return DockerProjectInspection.create(
            envelope, tuple(resources), inspected_at=self._clock())


class CandidateCAuthorityProxy:
    """Stable public API proxy; it never fabricates Candidate-C results."""

    def __init__(self) -> None:
        self._authority: object | None = None

    def bind(self, authority: object) -> None:
        if self._authority is not None and self._authority is not authority:
            raise ValueError("Candidate-C authority proxy is already bound")
        self._authority = authority

    def _call(self, name: str, *args, **kwargs):
        method = getattr(self._authority, name, None)
        if not callable(method):
            raise RuntimeError("Candidate-C authority is not prepared")
        return method(*args, **kwargs)

    def read_activation(self):
        return self._call("read_activation")

    def local_readiness(self, **kwargs):
        return self._call("local_readiness", **kwargs)

    def get_staged_build(self, build_identity: str):
        return self._call("get_staged_build", build_identity)


CandidateCFactory = Callable[[CandidateCPreparationRequest], tuple[object, object, object]]


class KnowledgeIndexCandidateCPreparationOperations:
    """Bridge the Candidate-F request to Candidate-C stage/validate/activate APIs."""

    def __init__(self, repository_root: str | Path, *,
                 factory: CandidateCFactory | None = None) -> None:
        self._root = Path(repository_root).resolve()
        self._factory = factory or self._production_factory
        self.authority = CandidateCAuthorityProxy()
        self._closeable: object | None = None

    def _production_factory(self, request: CandidateCPreparationRequest):
        from knowledge_index import (
            ChromaIndexAdapter, GoogleEmbeddingAdapter, KnowledgeBuildService,
            SqliteKnowledgeStore, load_knowledge_config,
        )
        from knowledge_index.google_embedding_adapter import create_google_client
        config = load_knowledge_config(self._root / "configs/knowledge_index.yaml")
        child = Path(request.child_path).resolve()
        generated_root = child.parent
        production_root = (self._root / config.chroma_path).resolve()
        if (generated_root.name != "real_rag"
                or generated_root.parent.name != "rca_evaluation"
                or child == production_root or child.is_relative_to(production_root)):
            raise ValueError("Candidate-C child is outside the isolated evaluation root")
        (child / "candidate-c").mkdir(exist_ok=True)
        store = SqliteKnowledgeStore(child / "candidate-c" / "knowledge.sqlite3")
        index = ChromaIndexAdapter(
            child / "candidate-c" / "chroma",
            index_schema_identity=config.index_schema_identity,
            distance_metric=config.distance_metric)
        provider = GoogleEmbeddingAdapter(
            create_google_client(timeout_seconds=config.limits.timeout_seconds),
            config.capability)
        self._closeable = store
        return KnowledgeBuildService(store, provider, index), store, config

    def recover_exact(
        self, request: CandidateCPreparationRequest,
    ) -> CandidateCPreparationEvidence:
        """Recover only Candidate-C public authority after a lost response."""
        from knowledge_index import (
            KnowledgeLocalReadiness, KnowledgeReadStatus, load_knowledge_config,
        )
        if type(request) is not CandidateCPreparationRequest:
            raise TypeError("typed Candidate-C recovery request is required")
        child = Path(request.child_path).resolve()
        candidate_root = child / "candidate-c"
        if not candidate_root.is_dir():
            raise ValueError("Candidate-C public preparation is absent")
        service, authority, config = self._factory(request)
        expected_config = load_knowledge_config(
            self._root / "configs/knowledge_index.yaml")
        for field in ("capability", "limits", "chunking", "normalization_semantics",
                      "index_schema_identity", "distance_metric"):
            if getattr(config, field) != getattr(expected_config, field):
                raise ValueError("Candidate-C recovery configuration is contradictory")
        activation = authority.read_activation()
        if activation.status is not KnowledgeReadStatus.FOUND:
            raise ValueError("Candidate-C public activation authority is absent")
        staged = authority.get_staged_build(activation.value.active_build_identity)
        readiness = service.recover_active()
        if (staged.status is not KnowledgeReadStatus.FOUND
                or readiness.status is not KnowledgeLocalReadiness.READY
                or readiness.active_build_identity !=
                    activation.value.active_build_identity
                or readiness.generation != activation.value.generation):
            raise ValueError("Candidate-C public recovery authority is contradictory")
        self.authority.bind(authority)
        activation_value = {
            "generation": activation.value.generation,
            "operation_identity": activation.value.operation_key.value,
            "validated_build_commitment":
                activation.value.validated_build_commitment,
        }
        evidence = CandidateCPreparationEvidence(
            request.preparation_id, request.child_identity,
            request.manifest_commitment, request.corpus_identity,
            activation.value.active_build_identity, commitment(activation_value),
            activation.value.validated_build_commitment,
            staged.value.artifact.artifact_commitment,
            tuple(sorted(_artifact_manifest(child).items())))
        evidence.validate()
        return evidence

    def prepare_exact(
        self, request: CandidateCPreparationRequest,
    ) -> CandidateCPreparationEvidence:
        from knowledge_index import (
            ActivationOperationKey, BuildActivationRequest, BuildIdentityInput,
            BuildOperationKey, BuildValidationState, KnowledgeLocalReadiness,
            KnowledgeReadStatus, admit_production_manifest, plan_chunks,
        )
        if type(request) is not CandidateCPreparationRequest:
            raise TypeError("typed Candidate-C preparation request is required")
        child = Path(request.child_path).resolve()
        if (not child.is_dir() or not (child / PREPARED_CHILD_MARKER).is_file()
                or request.profile_identity != "spec017-real-rag-gate-v1"):
            raise ValueError("Candidate-C isolated child is unavailable or contradictory")
        config_path = self._root / "configs/knowledge_index.yaml"
        from knowledge_index import load_knowledge_config
        expected_config = load_knowledge_config(config_path)
        manifest = admit_production_manifest(
            self._root / expected_config.manifest_path, repository_root=self._root)
        if (request.manifest_commitment != manifest.manifest_commitment
                or request.corpus_identity != manifest.manifest.corpus_id
                or request.corpus_release != manifest.manifest.canonical_release_reference
                or request.source_revision != manifest.manifest.source_revision
                or request.source_revision !=
                    "7e577de0ad85946c00c7259af3e1636f9eabd5ae"
                or request.embedding_model != expected_config.capability.model
                or request.embedding_model != "text-embedding-004"
                or request.embedding_profile_identity !=
                    expected_config.capability.embedding_profile_identity
                or request.index_schema_identity != expected_config.index_schema_identity
                or request.index_schema_identity != "spec014-chroma-l2-v2"
                or request.distance_metric != expected_config.distance_metric
                or request.distance_metric != "l2"
                or request.retrieval_profile_identity !=
                    expected_config.retrieval_profile.profile_identity
                or request.gate_profile_identity != request.profile_identity):
            raise ValueError("Candidate-C request contradicts the frozen profile")
        service, authority, config = self._factory(request)
        for field in ("capability", "limits", "chunking", "normalization_semantics",
                      "index_schema_identity", "distance_metric"):
            if getattr(config, field) != getattr(expected_config, field):
                raise ValueError("Candidate-C factory configuration is contradictory")
        raw_manifest = json.loads(
            (self._root / config.manifest_path).read_text(encoding="utf-8"))
        source_root = self._root / manifest.manifest.governed_source_root
        chunks = plan_chunks(manifest, source_root=source_root,
                             profile=config.chunking)
        identity_input = BuildIdentityInput(
            manifest.manifest_commitment,
            tuple(item.chunk_identity for item in chunks),
            config.chunking.profile_identity, "1.0", config.capability.provider,
            config.capability.model, config.capability.embedding_profile_identity,
            config.capability.embedding_dimension, config.normalization_semantics,
            "chromadb", config.index_schema_identity,
            "spec014-knowledge-metadata-v1", "spec014-build-contract-v2",
            config.distance_metric)
        suffix = hashlib.sha256(
            f"{request.preparation_id}:{request.child_identity}".encode()).hexdigest()[:24]
        staged = service.stage(
            operation_key=BuildOperationKey("spec017-stage-" + suffix),
            raw_manifest=raw_manifest, source_root=source_root, chunks=chunks,
            identity_input=identity_input, limits=config.limits,
            required_capability_identity=config.capability.capability_identity)
        if staged.record is None or staged.failures:
            raise RuntimeError("Candidate-C stage did not produce an authoritative build")
        validated = service.validate(
            staged.record.build_identity,
            BuildOperationKey("spec017-validate-" + suffix),
            maximum_probe_results=config.retrieval_profile.candidate_limit)
        if validated.state is not BuildValidationState.VALIDATED or validated.findings:
            raise RuntimeError("Candidate-C validation failed")
        current = authority.read_activation()
        if current.status is KnowledgeReadStatus.NOT_FOUND:
            generation = 0
        elif current.status is KnowledgeReadStatus.FOUND:
            generation = current.value.generation
        else:
            raise RuntimeError("Candidate-C activation authority is unavailable")
        activated = service.activate(BuildActivationRequest(
            staged.record.build_identity,
            ActivationOperationKey("spec017-activate-" + suffix), generation))
        if activated.authority.active_build_identity != staged.record.build_identity:
            raise RuntimeError("Candidate-C activation mismatched the validated build")
        readiness = service.recover_active()
        if (readiness.status is not KnowledgeLocalReadiness.READY
                or readiness.active_build_identity != staged.record.build_identity
                or readiness.generation != activated.authority.generation):
            raise RuntimeError("Candidate-C public readiness contradicts activation")
        self.authority.bind(authority)
        if (len(request.execution_mounts) != 30
                or any(type(item) is not FormalRealRagMount
                       for item in request.execution_mounts)):
            raise ValueError("Candidate-C execution-child topology is incomplete")
        source = child / "candidate-c"
        for mount in request.execution_mounts:
            mount.validate(
                allocation_root_reference=mount.child_source_reference.split(
                    "/executions/", 1)[0],
                expected_profile=request.profile_identity)
            execution = (child.parent.parent / mount.child_source_reference).resolve()
            execution.relative_to(child)
            filesystem_execution = _fs_path(execution)
            if filesystem_execution.exists() and any(filesystem_execution.iterdir()):
                expected = {PREPARED_CHILD_MARKER, *(
                    item.name for item in source.iterdir())}
                if ({item.name for item in filesystem_execution.iterdir()} != expected
                        or json.loads((filesystem_execution / PREPARED_CHILD_MARKER).read_text(
                            encoding="utf-8")) != mount.expected_marker()
                        or any(
                            item.is_file() and (
                                not (filesystem_execution / item.name).is_file()
                                or hashlib.sha256(item.read_bytes()).digest()
                                != hashlib.sha256((filesystem_execution / item.name).read_bytes()).digest())
                            for item in source.iterdir())):
                    raise ValueError(
                        "Candidate-C execution child contains unknown state")
                continue
            filesystem_execution.mkdir(parents=True, exist_ok=True)
            for item in source.iterdir():
                destination = filesystem_execution / item.name
                if item.is_dir():
                    shutil.copytree(item, destination)
                else:
                    shutil.copy2(item, destination)
            (filesystem_execution / PREPARED_CHILD_MARKER).write_text(
                json.dumps(mount.expected_marker(), sort_keys=True,
                           separators=(",", ":")), encoding="utf-8")
        public_activation = self.authority.read_activation()
        if (public_activation.status is not KnowledgeReadStatus.FOUND
                or public_activation.value.active_build_identity !=
                    staged.record.build_identity):
            raise RuntimeError("Candidate-C public activation authority is unavailable")
        activation_value = {
            "generation": public_activation.value.generation,
            "operation_identity": public_activation.value.operation_key.value,
            "validated_build_commitment":
                public_activation.value.validated_build_commitment,
        }
        artifacts = tuple(sorted(_artifact_manifest(child).items()))
        evidence = CandidateCPreparationEvidence(
            request.preparation_id, request.child_identity,
            request.manifest_commitment, request.corpus_identity,
            staged.record.build_identity, commitment(activation_value),
            validated.validation_commitment,
            staged.record.artifact.artifact_commitment, artifacts)
        evidence.validate()
        return evidence

    def close(self) -> None:
        close = getattr(self._closeable, "close", None)
        if callable(close):
            close()


class DockerContainerRealRagInspector:
    """Read exact container mounts and delegate semantic checks to the resolver."""

    _CREDENTIAL_PROBE = (
        "import os;v=os.environ.get('GEMINI_API_KEY','');"
        "print('AVAILABLE' if v and v==v.strip() else 'MISSING_OR_INVALID')"
    )

    def __init__(
        self, repository_root: str | Path, candidate_c_authority: object, *,
        executor: CommandExecutor | None = None,
        timeout_seconds: float = 30.0,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self._root = Path(repository_root).resolve()
        self._authority = candidate_c_authority
        self._executor = executor or SubprocessCommandExecutor()
        self._timeout = timeout_seconds
        self._clock = clock

    def _exec(self, container: str, *argv: str, operation: str) -> str:
        return _run(self._executor, ("docker", "exec", container, *argv),
                    self._root, operation, timeout=self._timeout)

    def inspect(self, target: ContainerRealRagInspectionTarget) -> ContainerRealRagReadiness:
        if type(target) is not ContainerRealRagInspectionTarget:
            raise TypeError("typed container Real-RAG inspection target is required")
        mount = target.mount
        ids = _ids(_run(self._executor, (
            "docker", "container", "ls", "-q", "--filter",
            f"label=com.docker.compose.project={target.project_identity}",
            "--filter", "label=com.docker.compose.service=runtime",
        ), self._root, "target Runtime container lookup", timeout=self._timeout))
        if len(ids) != 1:
            raise ValueError("exact target Runtime container is missing or ambiguous")
        container = ids[0]
        inspected = _json_list(_run(self._executor, (
            "docker", "container", "inspect", container,
        ), self._root, "target Runtime container inspection",
            timeout=self._timeout), "target Runtime container inspection")
        if len(inspected) != 1 or not isinstance(inspected[0], Mapping):
            raise ValueError("target Runtime container inspection is malformed")
        observed_container = inspected[0]
        config = observed_container.get("Config")
        mounts = observed_container.get("Mounts")
        labels = config.get("Labels") if isinstance(config, Mapping) else None
        if (not isinstance(labels, Mapping) or not isinstance(mounts, list)
                or labels.get("com.docker.compose.project") != target.project_identity
                or labels.get("com.docker.compose.service") != "runtime"):
            raise ValueError("target Runtime container provenance is contradictory")
        by_destination = _exact_bind_mounts(mounts)
        child_mount = by_destination.get(mount.child_container_target)
        corpus_mount = by_destination.get(mount.corpus_container_target)
        if (not isinstance(child_mount, Mapping)
                or not isinstance(corpus_mount, Mapping)
                or child_mount.get("Type") != "bind"
                or corpus_mount.get("Type") != "bind"
                or child_mount.get("RW") is not True
                or corpus_mount.get("RW") is not False):
            raise ValueError("actual container Real-RAG mounts are contradictory")
        evaluation_root = (self._root / "var/rca_evaluation").resolve()
        expected_child = (evaluation_root / mount.child_source_reference).resolve()
        expected_corpus = (self._root / mount.corpus_source_reference).resolve()
        try:
            expected_child.relative_to(
                (evaluation_root / "real_rag").resolve())
            expected_corpus.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("expected container mount source escapes approved root") from exc
        if (_canonical_host_path(str(child_mount["Source"]))
                != _canonical_host_path(str(expected_child))
                or _canonical_host_path(str(corpus_mount["Source"]))
                != _canonical_host_path(str(expected_corpus))):
            raise ValueError("actual container Real-RAG mount source is contradictory")
        filesystem_child = _fs_path(expected_child)
        if not filesystem_child.is_dir():
            raise ValueError("authoritative Candidate-C execution child is absent")
        allocation_reference, separator, _ = mount.child_source_reference.partition(
            "/executions/")
        authoritative_source = _fs_path(
            (evaluation_root / allocation_reference / "candidate-c").resolve())
        if not separator or not authoritative_source.is_dir():
            raise ValueError("authoritative Candidate-C artifact source is absent")
        authoritative_artifacts = {
            path.relative_to(authoritative_source).as_posix():
                hashlib.sha256(path.read_bytes()).hexdigest()
            for path in authoritative_source.rglob("*") if path.is_file()
        }
        if not authoritative_artifacts:
            raise ValueError("authoritative Candidate-C artifacts are absent")
        observed_host_files = {
            path.relative_to(filesystem_child).as_posix(): path
            for path in filesystem_child.rglob("*") if path.is_file()
        }
        if set(observed_host_files) != {
                PREPARED_CHILD_MARKER, *authoritative_artifacts}:
            raise ValueError("prepared Candidate-C execution artifact set is contradictory")
        for relative, digest in authoritative_artifacts.items():
            if hashlib.sha256(observed_host_files[relative].read_bytes()).hexdigest() != digest:
                raise ValueError("prepared Candidate-C execution artifact hash is contradictory")
        marker = self._exec(
            container, "cat",
            f"{mount.child_container_target}/{PREPARED_CHILD_MARKER}",
            operation="container ownership marker inspection")
        credential = self._exec(
            container, "python", "-c", self._CREDENTIAL_PROBE,
            operation="container credential presence inspection")
        from knowledge_index import admit_production_manifest
        manifest = admit_production_manifest(
            self._root / "configs/knowledge_manifest.json",
            repository_root=self._root)
        artifact_output = self._exec(
            container, "find", mount.child_container_target, "-type", "f",
            "-printf", "%P\n", operation="container Candidate-C artifact listing")
        observed_artifacts = _relative_file_names(
            artifact_output, "container Candidate-C artifact listing")
        expected_artifacts = tuple(sorted({
            PREPARED_CHILD_MARKER, *authoritative_artifacts}))
        if (observed_artifacts != expected_artifacts
                or PREPARED_CHILD_MARKER not in expected_artifacts
                or len(expected_artifacts) < 2):
            raise ValueError("mounted Candidate-C artifact set is contradictory")
        for relative in expected_artifacts:
            if relative == PREPARED_CHILD_MARKER:
                continue
            expected_digest = authoritative_artifacts[relative]
            output = self._exec(
                container, "sha256sum",
                f"{mount.child_container_target}/{relative}",
                operation="container Candidate-C artifact hash inspection")
            fields = output.split()
            if len(fields) < 2 or fields[0] != expected_digest:
                raise ValueError("mounted Candidate-C artifact hash is contradictory")
        names_output = self._exec(
            container, "find", mount.corpus_container_target, "-maxdepth", "1",
            "-type", "f", "-printf", "%f\n",
            operation="container corpus filename inspection")
        observed_names = {line.strip() for line in names_output.splitlines()
                          if line.strip()}
        expected_names = {item.source_path for item in manifest.manifest.documents}
        with tempfile.TemporaryDirectory(prefix="spec017-container-inspection-") as temp:
            container_root = Path(temp)
            child = container_root / mount.child_container_target.lstrip("/")
            corpus = container_root / mount.corpus_container_target.lstrip("/")
            child.mkdir(parents=True)
            corpus.mkdir(parents=True)
            (child / PREPARED_CHILD_MARKER).write_text(marker, encoding="utf-8")
            if observed_names != expected_names:
                (corpus / ".unexpected-entry").write_bytes(b"contradictory")
            for document in manifest.manifest.documents:
                output = self._exec(
                    container, "sha256sum",
                    f"{mount.corpus_container_target}/{document.source_path}",
                    operation="container corpus hash inspection")
                fields = output.split()
                if len(fields) < 2 or fields[0] != document.expected_content_hash:
                    (corpus / document.source_path).write_bytes(b"contradictory")
                else:
                    shutil.copyfile(
                        self._root / manifest.manifest.governed_source_root
                        / document.source_path,
                        corpus / document.source_path)
            environment = ({CREDENTIAL_ENVIRONMENT: "PRESENT"}
                           if credential == "AVAILABLE" else {})
            return resolve_container_real_rag_readiness(
                mount, container_root=container_root,
                project_identity=target.project_identity,
                candidate_c_authority=self._authority,
                _environment=environment, _validated_at=self._clock())


def prepare_production_resources(
    *, store: SqliteEvaluationStore, preparation_id: str,
    repository_root: str | Path, explicitly_authorized: bool,
    executor: CommandExecutor | None = None,
) -> PreparationResourceFacts:
    """Explicit opt-in composition of concrete adapters into the existing workflow."""
    if explicitly_authorized is not True:
        raise PermissionError("production preparation requires explicit authorization")
    from .runtime_image_readiness import (
        CommandResult as RuntimeCommandResult, RuntimeImageReadinessResolver,
    )

    root = Path(repository_root).resolve()
    commands = executor or SubprocessCommandExecutor()

    def readiness_runner(argv, cwd):
        observed = commands.execute(argv, cwd=cwd, timeout_seconds=30.0)
        return RuntimeCommandResult(
            observed.returncode, observed.stdout, observed.stderr)

    runtime = RuntimeImageReadinessResolver(root, _command_runner=readiness_runner)
    candidate = KnowledgeIndexCandidateCPreparationOperations(root)
    container = DockerContainerRealRagInspector(
        root, candidate.authority, executor=commands)
    try:
        return prepare_authoritative_resources(
            store=store, preparation_id=preparation_id, repository_root=root,
            runtime_authority=runtime,
            runtime_image_operations=DockerRuntimeImagePreparationOperations(
                root, executor=commands),
            docker_operations=ComposePreparationDockerOperations(
                store, preparation_id, root, executor=commands),
            docker_state_inspector=DockerCliStateInspector(
                root, executor=commands),
            candidate_c_operations=candidate,
            candidate_c_authority=candidate.authority,
            container_real_rag_inspector=container)
    finally:
        candidate.close()


__all__ = [
    "CandidateCAuthorityProxy", "ComposePreparationDockerOperations",
    "DockerCliStateInspector", "DockerContainerRealRagInspector",
    "DockerRuntimeImagePreparationOperations",
    "KnowledgeIndexCandidateCPreparationOperations",
    "prepare_production_resources",
]
