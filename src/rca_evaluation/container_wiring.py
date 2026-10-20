"""Secret-free Slice 3 projection for Formal container credential/RAG wiring.

This module is deliberately read-only.  It derives exact future Compose inputs
from Candidate-F preparation, but never creates a child, mounts a path, invokes
a provider, builds/activates Candidate-C, or starts a container.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path, PurePosixPath
from typing import Mapping, Protocol

from knowledge_index import admit_production_manifest, load_knowledge_config
from llm_generation.config import load_generation_config

from .formal_compose import (
    FormalComposeCampaignTopology, resolve_formal_compose_topology,
)
from .gates import load_real_llm_profile, load_real_rag_profile
from .identity import commitment, require_identity
from .preparation import PREPARED_CHILD_MARKER, verify_prepared_real_rag_child
from .readiness import (
    CredentialReadiness, CredentialState, ReadinessFactState, RequiredFact,
    resolve_candidate_c_build, resolve_environment_credential,
)
from .sqlite_store import SqliteEvaluationStore


FORMAL_CONTAINER_WIRING_SCHEMA = "spec017.formal-container-wiring.v1"
REAL_LLM_PROFILE = "spec017-real-llm-gate-v1"
REAL_RAG_PROFILE = "spec017-real-rag-gate-v1"
CREDENTIAL_ENVIRONMENT = "GEMINI_API_KEY"
REAL_RAG_CONTAINER_TARGET = "/app/var/knowledge_index"
CORPUS_CONTAINER_TARGET = "/app/var/rca_evaluation/corpus_source"
EXECUTION_CHILD_MARKER_SCHEMA = "spec017.formal-real-rag-execution-child.v1"


class ContainerMountState(str, Enum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    CONTRADICTORY = "CONTRADICTORY"


@dataclass(frozen=True, slots=True)
class CredentialInjectionContract:
    environment_name: str
    source_kind: str
    compose_declaration: str
    default_value: None

    def material(self) -> dict[str, object]:
        return {
            "environment_name": self.environment_name,
            "source_kind": self.source_kind,
            "compose_declaration": self.compose_declaration,
            "default_value": self.default_value,
        }

    def validate(self) -> None:
        if (self.environment_name != CREDENTIAL_ENVIRONMENT
                or self.source_kind != "EXTERNAL_ENVIRONMENT"
                or self.compose_declaration != "NAME_ONLY"
                or self.default_value is not None):
            raise ValueError("Formal credential injection contract is contradictory")


@dataclass(frozen=True, slots=True)
class FormalRealRagMount:
    preparation_id: str
    planned_run_identity: str
    planned_execution_identity: str
    allocation_root_identity: str
    execution_child_identity: str
    profile_identity: str
    child_source_reference: str
    child_container_target: str
    child_mode: str
    corpus_source_reference: str
    corpus_container_target: str
    corpus_mode: str
    manifest_commitment: str
    corpus_release: str
    source_revision: str
    actual_mount_state: ContainerMountState
    mount_commitment: str

    def material(self, *, include_commitment: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "preparation_id": self.preparation_id,
            "planned_run_identity": self.planned_run_identity,
            "planned_execution_identity": self.planned_execution_identity,
            "allocation_root_identity": self.allocation_root_identity,
            "execution_child_identity": self.execution_child_identity,
            "profile_identity": self.profile_identity,
            "child_source_reference": self.child_source_reference,
            "child_container_target": self.child_container_target,
            "child_mode": self.child_mode,
            "corpus_source_reference": self.corpus_source_reference,
            "corpus_container_target": self.corpus_container_target,
            "corpus_mode": self.corpus_mode,
            "manifest_commitment": self.manifest_commitment,
            "corpus_release": self.corpus_release,
            "source_revision": self.source_revision,
            "actual_mount_state": self.actual_mount_state.value,
        }
        if include_commitment:
            value["mount_commitment"] = self.mount_commitment
        return value

    def expected_marker(self) -> dict[str, object]:
        material = {
            "schema": EXECUTION_CHILD_MARKER_SCHEMA,
            "preparation_id": self.preparation_id,
            "planned_run_identity": self.planned_run_identity,
            "planned_execution_identity": self.planned_execution_identity,
            "allocation_root_identity": self.allocation_root_identity,
            "execution_child_identity": self.execution_child_identity,
            "profile_identity": self.profile_identity,
            "child_source_reference": self.child_source_reference,
            "lifecycle_state": "ALLOCATED",
        }
        return {**material, "ownership_commitment": commitment(material)}

    def validate(self, *, allocation_root_reference: str,
                 expected_profile: str) -> None:
        require_identity(self.preparation_id, "preparation")
        require_identity(self.planned_run_identity, "planned_run")
        require_identity(self.planned_execution_identity, "planned_execution")
        source = PurePosixPath(self.child_source_reference)
        allocation = PurePosixPath(allocation_root_reference)
        expected_child = "ragchild_" + commitment({
            "preparation": self.preparation_id,
            "planned_run": self.planned_run_identity,
            "planned_execution": self.planned_execution_identity,
            "allocation_root": self.allocation_root_identity,
            "profile": expected_profile,
        })[7:]
        expected_source = allocation / "executions" / expected_child
        if (self.profile_identity != expected_profile
                or self.execution_child_identity != expected_child
                or source.is_absolute() or ".." in source.parts
                or source != expected_source
                or self.child_container_target != REAL_RAG_CONTAINER_TARGET
                or self.child_mode != "rw"
                or self.corpus_container_target != CORPUS_CONTAINER_TARGET
                or self.corpus_mode != "ro"
                or PurePosixPath(self.corpus_source_reference).is_absolute()
                or ".." in PurePosixPath(self.corpus_source_reference).parts
                or self.actual_mount_state is not ContainerMountState.UNRESOLVED
                or commitment(self.material(include_commitment=False))
                    != self.mount_commitment):
            raise ValueError("Formal Real-RAG mount wiring is contradictory")


@dataclass(frozen=True, slots=True)
class FormalContainerWiringReadiness:
    schema: str
    preparation_id: str
    credential_injection: CredentialInjectionContract
    llm_credential: CredentialReadiness
    rag_credential: CredentialReadiness
    llm_profile: dict[str, object]
    rag_profile: dict[str, object]
    mounts: tuple[FormalRealRagMount, ...]
    topology_commitment: str
    build_fact: RequiredFact
    activation_fact: RequiredFact
    wiring_commitment: str

    def semantic_material(self, *, include_commitment: bool = True) -> dict[str, object]:
        def sanitized_credential(value: CredentialReadiness) -> dict[str, str]:
            return {
                "credential_reference": value.credential_reference,
                "credential_state": value.credential_state.value,
                "validation_method": value.validation_method,
                "environment_identity": value.environment_identity,
                "profile_identity": value.profile_identity,
                "reason": value.reason,
            }

        result: dict[str, object] = {
            "schema": self.schema,
            "preparation_id": self.preparation_id,
            "credential_injection": self.credential_injection.material(),
            "llm_credential": sanitized_credential(self.llm_credential),
            "rag_credential": sanitized_credential(self.rag_credential),
            "llm_profile": self.llm_profile,
            "rag_profile": self.rag_profile,
            "mounts": [item.material() for item in self.mounts],
            "topology_commitment": self.topology_commitment,
            "build_fact": self.build_fact.material(),
            "activation_fact": self.activation_fact.material(),
        }
        if include_commitment:
            result["wiring_commitment"] = self.wiring_commitment
        return result

    def validate(self, *, allocation_root_identity: str,
                 allocation_root_reference: str) -> None:
        require_identity(self.preparation_id, "preparation")
        self.credential_injection.validate()
        for item in self.mounts:
            if item.allocation_root_identity != allocation_root_identity:
                raise ValueError("Formal Real-RAG allocation root is contradictory")
            item.validate(allocation_root_reference=allocation_root_reference,
                          expected_profile=REAL_RAG_PROFILE)
        if (self.schema != FORMAL_CONTAINER_WIRING_SCHEMA
                or len(self.mounts) != 30
                or len({item.planned_execution_identity
                        for item in self.mounts}) != 30
                or len({item.execution_child_identity for item in self.mounts}) != 30
                or len({item.child_source_reference for item in self.mounts}) != 30
                or self.llm_profile.get("profile_identity") != REAL_LLM_PROFILE
                or self.rag_profile.get("profile_identity") != REAL_RAG_PROFILE
                or self.llm_credential.credential_reference != "env:GEMINI_API_KEY"
                or self.rag_credential.credential_reference != "env:GEMINI_API_KEY"
                or commitment(self.semantic_material(include_commitment=False))
                    != self.wiring_commitment):
            raise ValueError("Formal container wiring readiness is contradictory")


@dataclass(frozen=True, slots=True)
class ContainerRealRagReadiness:
    preparation_id: str
    project_identity: str
    planned_execution_identity: str
    child_identity: str
    child_target: str
    corpus_target: str
    inspection_profile: str
    inspected_at: str
    credential: CredentialReadiness
    mount_fact: RequiredFact
    corpus_fact: RequiredFact
    build_fact: RequiredFact
    activation_fact: RequiredFact

    def assert_resolved(self) -> None:
        require_identity(self.preparation_id, "preparation")
        require_identity(self.planned_execution_identity, "planned_execution")
        if (not self.project_identity or not self.child_identity
                or not self.child_target.startswith("/")
                or not self.corpus_target.startswith("/")
                or self.inspection_profile != "SPEC017_CONTAINER_MOUNTS_v1"
                or not self.inspected_at):
            raise ValueError("container inspection provenance is incomplete")
        if self.credential.credential_state is not CredentialState.AVAILABLE:
            raise ValueError("container embedding credential is not available")
        for fact in (self.mount_fact, self.corpus_fact,
                     self.build_fact, self.activation_fact):
            if fact.state is ReadinessFactState.CONTRADICTORY:
                raise ValueError(f"contradictory required fact: {fact.name}")
            if fact.state is not ReadinessFactState.RESOLVED:
                raise ValueError(f"unresolved required fact: {fact.name}")


@dataclass(frozen=True, slots=True)
class ContainerRealRagInspectionTarget:
    preparation_id: str
    project_identity: str
    mount: FormalRealRagMount


class ContainerRealRagInspector(Protocol):
    """Typed fresh-inspection port; callers cannot pass resolved booleans."""

    def inspect(
        self, target: ContainerRealRagInspectionTarget,
    ) -> ContainerRealRagReadiness: ...


def _load_profiles(root: Path) -> tuple[object, object, object, object, object]:
    llm = load_real_llm_profile(root / "configs/rca_evaluation/real_llm.yaml")
    rag = load_real_rag_profile(root / "configs/rca_evaluation/real_rag.yaml")
    generation = load_generation_config(root / llm.config_path)
    rag_values = rag.values()
    knowledge = load_knowledge_config(root / str(rag_values["config_path"]))
    manifest = admit_production_manifest(
        root / str(rag_values["manifest_path"]), repository_root=root)
    pin = generation.pin
    if (llm.profile_identity != REAL_LLM_PROFILE
            or llm.authorization.credential_environment != CREDENTIAL_ENVIRONMENT
            or llm.provider != "google" or llm.model != "gemini-2.5-flash"
            or llm.generation_profile_identity != "spec016-google-generation-profile-v1"
            or llm.prompt_identity != "spec016-rca-prompt-v1"
            or llm.result_schema_identity != "spec015-rca-result-v1"
            or llm.configuration_identity
                != "cfg_c4c0532057d9d56f943c7bdcc4c3028b87b8fe4952e8fc460bf7df46205f8611"
            or llm.timeout_seconds != 30 or llm.maximum_invocations != 1
            or pin.provider != llm.provider or pin.model != llm.model
            or not generation.hidden_retries_disabled):
        raise ValueError("Real LLM container profile contradicts R3")
    if (rag.profile_identity != REAL_RAG_PROFILE
            or rag.authorization.credential_environment != CREDENTIAL_ENVIRONMENT
            or rag_values["release_reference"]
                != "spec014-production-knowledge-release@1.0"
            or rag_values["embedding_provider"] != "google"
            or rag_values["embedding_model"] != "text-embedding-004"
            or rag_values["embedding_profile_identity"]
                != "spec014-google-document-query-v1"
            or rag_values["index_schema_identity"] != "spec014-chroma-l2-v2"
            or rag_values["distance_metric"] != "l2"
            or rag_values["retrieval_profile_identity"] != "spec014-retrieval-v1"
            or rag_values["manifest_commitment"] != manifest.manifest_commitment
            or knowledge.distance_metric != "l2"):
        raise ValueError("Real-RAG container profile contradicts R3")
    return llm, rag, generation, knowledge, manifest


def _profile_material(llm: object, rag: object, generation: object,
                      knowledge: object, manifest: object) -> tuple[dict[str, object], dict[str, object]]:
    llm_value = {
        "profile_identity": llm.profile_identity,
        "provider": llm.provider,
        "model": llm.model,
        "generation_profile_identity": llm.generation_profile_identity,
        "prompt_identity": llm.prompt_identity,
        "result_schema_identity": llm.result_schema_identity,
        "configuration_identity": llm.configuration_identity,
        "timeout_seconds": llm.timeout_seconds,
        "maximum_invocations": llm.maximum_invocations,
        "hidden_retries": "DISABLED" if generation.hidden_retries_disabled else "ENABLED",
    }
    rag_values = rag.values()
    rag_value = {
        "profile_identity": rag.profile_identity,
        "corpus_identity": manifest.manifest.corpus_id,
        "corpus_version": manifest.manifest.corpus_version,
        "corpus_release": manifest.manifest.canonical_release_reference,
        "source_revision": manifest.manifest.source_revision,
        "manifest_commitment": manifest.manifest_commitment,
        "embedding_provider": knowledge.capability.provider,
        "embedding_model": knowledge.capability.model,
        "embedding_profile_identity": knowledge.capability.embedding_profile_identity,
        "index_schema_identity": knowledge.index_schema_identity,
        "distance_metric": knowledge.distance_metric,
        "retrieval_profile_identity": knowledge.retrieval_profile.profile_identity,
        "isolated_chroma_root": rag_values["isolated_chroma_root"],
    }
    return llm_value, rag_value


def resolve_formal_container_wiring(
    store: SqliteEvaluationStore, preparation_id: str,
    repository_root: str | Path, *, candidate_c_authority: object | None = None,
    _environment: Mapping[str, str] | None = None,
    _validated_at: str | None = None,
    _preparation_artifacts: Mapping[str, str] | None = None,
) -> FormalContainerWiringReadiness:
    """Fresh-read the supported Formal wiring; underscored inputs are test seams."""
    if type(store) is not SqliteEvaluationStore:
        raise TypeError("Formal container wiring requires SqliteEvaluationStore")
    require_identity(preparation_id, "preparation")
    root = Path(repository_root).resolve()
    prepared = store.get_preparation(preparation_id)
    if prepared is None:
        raise ValueError("Formal container wiring requires PREPARED state")
    repository = prepared.get("repository")
    if (not isinstance(repository, Mapping)
            or Path(str(repository.get("root", ""))).resolve() != root):
        raise ValueError("prepared repository context is contradictory")
    child = verify_prepared_real_rag_child(
        store, preparation_id, root,
        _preparation_artifacts=_preparation_artifacts)
    topology = resolve_formal_compose_topology(store, preparation_id, root)
    llm, rag, generation, knowledge, manifest = _load_profiles(root)
    llm_material, rag_material = _profile_material(
        llm, rag, generation, knowledge, manifest)
    if child["profile_identity"] != rag.profile_identity:
        raise ValueError("prepared Candidate-C child profile is contradictory")

    now = (_validated_at or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"))
    environment_identity = "container:spec011-runtime-formal-v1"
    llm_credential = resolve_environment_credential(
        credential_environment=CREDENTIAL_ENVIRONMENT,
        required_profile_identity=llm.profile_identity,
        profile_identity=llm.profile_identity, validated_at=now,
        environment_identity=environment_identity, environment=_environment)
    rag_credential = resolve_environment_credential(
        credential_environment=CREDENTIAL_ENVIRONMENT,
        required_profile_identity=rag.profile_identity,
        profile_identity=rag.profile_identity, validated_at=now,
        environment_identity=environment_identity, environment=_environment)
    build, activation = resolve_candidate_c_build(
        candidate_c_authority, knowledge,
        expected_manifest_commitment=manifest.manifest_commitment,
        expected_corpus_identity=manifest.manifest.corpus_id,
        expected_corpus_version=manifest.manifest.corpus_version)

    allocation_root = str(child["relative_path"])
    corpus_reference = manifest.manifest.governed_source_root.rstrip("/")
    mounts: list[FormalRealRagMount] = []
    for envelope in topology.envelopes:
        execution_child = "ragchild_" + commitment({
            "preparation": preparation_id,
            "planned_run": envelope.planned_run_identity,
            "planned_execution": envelope.planned_execution_identity,
            "allocation_root": child["child_identity"],
            "profile": rag.profile_identity,
        })[7:]
        material = {
            "preparation_id": preparation_id,
            "planned_run_identity": envelope.planned_run_identity,
            "planned_execution_identity": envelope.planned_execution_identity,
            "allocation_root_identity": child["child_identity"],
            "execution_child_identity": execution_child,
            "profile_identity": rag.profile_identity,
            "child_source_reference": str(
                PurePosixPath(allocation_root) / "executions" / execution_child),
            "child_container_target": REAL_RAG_CONTAINER_TARGET,
            "child_mode": "rw",
            "corpus_source_reference": corpus_reference,
            "corpus_container_target": CORPUS_CONTAINER_TARGET,
            "corpus_mode": "ro",
            "manifest_commitment": manifest.manifest_commitment,
            "corpus_release": manifest.manifest.canonical_release_reference,
            "source_revision": manifest.manifest.source_revision,
            "actual_mount_state": ContainerMountState.UNRESOLVED,
        }
        mount = FormalRealRagMount(
            **material, mount_commitment=commitment({
                **material,
                "actual_mount_state": material["actual_mount_state"].value,
            }))
        mount.validate(allocation_root_reference=allocation_root,
                       expected_profile=rag.profile_identity)
        mounts.append(mount)

    injection = CredentialInjectionContract(
        CREDENTIAL_ENVIRONMENT, "EXTERNAL_ENVIRONMENT", "NAME_ONLY", None)
    values = {
        "schema": FORMAL_CONTAINER_WIRING_SCHEMA,
        "preparation_id": preparation_id,
        "credential_injection": injection,
        "llm_credential": llm_credential,
        "rag_credential": rag_credential,
        "llm_profile": llm_material,
        "rag_profile": rag_material,
        "mounts": tuple(mounts),
        "topology_commitment": topology.topology_commitment,
        "build_fact": build,
        "activation_fact": activation,
    }
    provisional = FormalContainerWiringReadiness(
        **values, wiring_commitment="sha256:" + "0" * 64)
    result = FormalContainerWiringReadiness(
        **values, wiring_commitment=commitment(
            provisional.semantic_material(include_commitment=False)))
    result.validate(allocation_root_identity=str(child["child_identity"]),
                    allocation_root_reference=allocation_root)
    return result


def materialize_formal_compose_environment(
    store: SqliteEvaluationStore, preparation_id: str,
    planned_execution_identity: str, repository_root: str | Path,
) -> dict[str, str]:
    """Return only governed non-secret bind inputs for one exact execution."""
    wiring = resolve_formal_container_wiring(
        store, preparation_id, repository_root)
    matching = [item for item in wiring.mounts
                if item.planned_execution_identity == planned_execution_identity]
    if len(matching) != 1:
        raise ValueError("planned execution has no exact Formal mount wiring")
    mount = matching[0]
    evaluation_root = store.path.parent.resolve()
    child_source = (evaluation_root / mount.child_source_reference).resolve()
    corpus_source = (Path(repository_root).resolve()
                     / mount.corpus_source_reference).resolve()
    child_source.relative_to(evaluation_root)
    corpus_source.relative_to(Path(repository_root).resolve())
    return {
        "SPEC017_REAL_RAG_CHILD_SOURCE": str(child_source),
        "SPEC017_REAL_RAG_CORPUS_SOURCE": str(corpus_source),
    }


def resolve_container_real_rag_readiness(
    mount: FormalRealRagMount, *, container_root: str | Path = "/",
    project_identity: str = "direct-container-inspection",
    candidate_c_authority: object | None = None,
    _environment: Mapping[str, str] | None = None,
    _validated_at: str | None = None,
) -> ContainerRealRagReadiness:
    """Fresh container-side inspection using the R3 credential/Candidate-C sources."""
    root = Path(container_root).resolve()
    target = (root / mount.child_container_target.lstrip("/")).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("container mount target escapes container root") from exc
    marker = target / PREPARED_CHILD_MARKER
    if not target.exists():
        mount_fact = RequiredFact(
            "actual_real_rag_child_mount", ReadinessFactState.UNRESOLVED, None,
            "container filesystem", "exact Candidate-C child mount is absent")
    elif not target.is_dir() or not marker.is_file():
        mount_fact = RequiredFact(
            "actual_real_rag_child_mount", ReadinessFactState.CONTRADICTORY, None,
            "container filesystem", "Candidate-C mount ownership marker is absent")
    else:
        import json
        try:
            observed = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            observed = None
        expected = mount.expected_marker()
        if observed != expected:
            mount_fact = RequiredFact(
                "actual_real_rag_child_mount", ReadinessFactState.CONTRADICTORY,
                None, "container filesystem",
                "Candidate-C mount ownership marker is contradictory")
        else:
            mount_fact = RequiredFact(
                "actual_real_rag_child_mount", ReadinessFactState.RESOLVED,
                {"execution_child_identity": mount.execution_child_identity,
                 "mount_commitment": mount.mount_commitment},
                "container filesystem",
                "exact Candidate-C child marker fresh-read")

    _, rag, _, knowledge, manifest = _load_profiles(
        Path(__file__).resolve().parents[2])
    corpus_target = (root / mount.corpus_container_target.lstrip("/")).resolve()
    try:
        corpus_target.relative_to(root)
    except ValueError as exc:
        raise ValueError("container corpus target escapes container root") from exc
    if not corpus_target.exists():
        corpus_fact = RequiredFact(
            "actual_real_rag_corpus_mount", ReadinessFactState.UNRESOLVED, None,
            "container filesystem", "approved corpus source mount is absent")
    elif not corpus_target.is_dir():
        corpus_fact = RequiredFact(
            "actual_real_rag_corpus_mount", ReadinessFactState.CONTRADICTORY,
            None, "container filesystem", "corpus mount is not a directory")
    else:
        expected_documents = {
            document.source_path: document.expected_content_hash
            for document in manifest.manifest.documents}
        observed_names = {path.name for path in corpus_target.iterdir()
                          if path.is_file()}
        hashes_match = observed_names == set(expected_documents)
        if hashes_match:
            try:
                hashes_match = all(
                    hashlib.sha256((corpus_target / name).read_bytes()).hexdigest()
                    == digest for name, digest in expected_documents.items())
            except OSError:
                hashes_match = False
        if hashes_match:
            corpus_fact = RequiredFact(
                "actual_real_rag_corpus_mount", ReadinessFactState.RESOLVED,
                {"manifest_commitment": manifest.manifest_commitment,
                 "corpus_release": manifest.manifest.canonical_release_reference,
                 "source_revision": manifest.manifest.source_revision},
                "container filesystem plus admitted Candidate-C manifest",
                "mounted corpus exactly matches approved manifest")
        else:
            corpus_fact = RequiredFact(
                "actual_real_rag_corpus_mount", ReadinessFactState.CONTRADICTORY,
                None, "container filesystem plus admitted Candidate-C manifest",
                "mounted corpus contradicts approved files or hashes")
    credential = resolve_environment_credential(
        credential_environment=CREDENTIAL_ENVIRONMENT,
        required_profile_identity=rag.profile_identity,
        profile_identity=rag.profile_identity,
        validated_at=(_validated_at or datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z")),
        environment_identity="container:spec011-runtime-formal-v1",
        environment=_environment)
    build, activation = resolve_candidate_c_build(
        candidate_c_authority, knowledge,
        expected_manifest_commitment=manifest.manifest_commitment,
        expected_corpus_identity=manifest.manifest.corpus_id,
        expected_corpus_version=manifest.manifest.corpus_version)
    inspected_at = (_validated_at or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"))
    result = ContainerRealRagReadiness(
        mount.preparation_id, project_identity,
        mount.planned_execution_identity, mount.execution_child_identity,
        mount.child_container_target, mount.corpus_container_target,
        "SPEC017_CONTAINER_MOUNTS_v1", inspected_at,
        credential, mount_fact, corpus_fact, build, activation)
    return result
