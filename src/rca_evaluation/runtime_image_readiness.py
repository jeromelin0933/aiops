"""Read-only SPEC-017 projection of SPEC-011 Runtime image readiness.

The resolver inspects source definitions and Docker metadata.  It never builds
an image, starts a container, mutates Runtime state, or persists authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import re
import subprocess
from typing import Callable, Sequence

import yaml

from .identity import commitment


RUNTIME_IMAGE_READINESS_SCHEMA = "spec017.runtime-image-readiness.v1"
RUNTIME_IMAGE_PROFILE = "spec011-runtime-formal-v1"
RUNTIME_IMAGE_TAG = "aiops-spec011-runtime:formal"

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9_.-]+)?")
_PINNED = re.compile(r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s;]+(?:\s*;.*)?")


class RuntimeFactState(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNRESOLVED = "UNRESOLVED"
    CONTRADICTORY = "CONTRADICTORY"


class DependencyReproducibility(str, Enum):
    EXACTLY_PINNED = "EXACTLY_PINNED"
    PARTIALLY_PINNED = "PARTIALLY_PINNED"
    UNPINNED = "UNPINNED"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeReadinessFact:
    name: str
    state: RuntimeFactState
    value: object | None
    reason: str

    def __post_init__(self) -> None:
        if (not self.name or not self.reason or self.name != self.name.strip()
                or self.reason != self.reason.strip() or len(self.reason) > 240):
            raise ValueError("Runtime readiness fact metadata is invalid")
        if self.state is RuntimeFactState.AVAILABLE and self.value is None:
            raise ValueError("available Runtime readiness fact requires a value")
        if self.state is not RuntimeFactState.AVAILABLE and self.value is not None:
            raise ValueError("unresolved Runtime readiness fact cannot carry a value")

    def material(self) -> dict[str, object]:
        return {"name": self.name, "state": self.state.value,
                "value": self.value, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class RuntimeImageSourceIdentity:
    repository_revision: str
    repository_dirty: bool
    dockerfile_path: str
    dockerfile_commitment: str
    build_context_identity: str
    runtime_config_path: str
    runtime_config_commitment: str
    compose_path: str
    compose_commitment: str
    formal_config_path: str
    formal_config_commitment: str
    dependency_path: str
    dependency_commitment: str
    dependency_reproducibility: DependencyReproducibility
    entrypoint_path: str
    entrypoint_commitment: str
    logical_image_identity: str
    image_tag: str
    base_image_reference: str
    base_image_digest: RuntimeReadinessFact
    source_definition: RuntimeReadinessFact
    formal_build_source: RuntimeReadinessFact
    source_commitment: str

    def material(self, *, include_commitment: bool = True) -> dict[str, object]:
        result = {
            "repository_revision": self.repository_revision,
            "repository_dirty": self.repository_dirty,
            "dockerfile": {"path": self.dockerfile_path,
                           "commitment": self.dockerfile_commitment},
            "build_context_identity": self.build_context_identity,
            "runtime_config": {"path": self.runtime_config_path,
                               "commitment": self.runtime_config_commitment},
            "compose": {"path": self.compose_path,
                        "commitment": self.compose_commitment},
            "formal_config": {"path": self.formal_config_path,
                              "commitment": self.formal_config_commitment},
            "dependency_definition": {
                "path": self.dependency_path,
                "commitment": self.dependency_commitment,
                "reproducibility": self.dependency_reproducibility.value,
            },
            "entrypoint": {"path": self.entrypoint_path,
                           "commitment": self.entrypoint_commitment},
            "logical_image_identity": self.logical_image_identity,
            "image_tag": self.image_tag,
            "base_image_reference": self.base_image_reference,
            "base_image_digest": self.base_image_digest.material(),
            "source_definition": self.source_definition.material(),
            "formal_build_source": self.formal_build_source.material(),
        }
        if include_commitment:
            result["source_commitment"] = self.source_commitment
        return result

    def validate(self) -> None:
        commitments = (
            self.dockerfile_commitment, self.build_context_identity,
            self.runtime_config_commitment, self.compose_commitment,
            self.formal_config_commitment, self.dependency_commitment,
            self.entrypoint_commitment, self.source_commitment,
        )
        if (not re.fullmatch(r"[0-9a-f]{40}", self.repository_revision)
                or self.logical_image_identity != RUNTIME_IMAGE_PROFILE
                or self.image_tag != RUNTIME_IMAGE_TAG
                or any(not _SHA256.fullmatch(value) for value in commitments)
                or commitment(self.material(include_commitment=False))
                    != self.source_commitment):
            raise ValueError("Runtime image source commitment is contradictory")


@dataclass(frozen=True, slots=True)
class RuntimeFormalReadinessFacts:
    schema: str
    source: RuntimeImageSourceIdentity
    docker_client: RuntimeReadinessFact
    docker_engine: RuntimeReadinessFact
    docker_compose: RuntimeReadinessFact
    actual_image: RuntimeReadinessFact
    readiness_commitment: str

    def material(self, *, include_commitment: bool = True) -> dict[str, object]:
        result = {
            "schema": self.schema,
            "source": self.source.material(),
            "docker_client": self.docker_client.material(),
            "docker_engine": self.docker_engine.material(),
            "docker_compose": self.docker_compose.material(),
            "actual_image": self.actual_image.material(),
        }
        if include_commitment:
            result["readiness_commitment"] = self.readiness_commitment
        return result

    def validate(self) -> None:
        if (self.schema != RUNTIME_IMAGE_READINESS_SCHEMA
                or (self.docker_client.name, self.docker_engine.name,
                    self.docker_compose.name, self.actual_image.name) != (
                        "docker_client", "docker_engine", "docker_compose",
                        "actual_runtime_image")):
            raise ValueError("unsupported Runtime image readiness schema")
        self.source.validate()
        if commitment(self.material(include_commitment=False)) != self.readiness_commitment:
            raise ValueError("Runtime image readiness commitment is contradictory")


CommandRunner = Callable[[Sequence[str], Path], CommandResult]


def _default_runner(command: Sequence[str], cwd: Path) -> CommandResult:
    try:
        result = subprocess.run(
            tuple(command), cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandResult(127, "", type(exc).__name__)
    return CommandResult(result.returncode, result.stdout.strip(), result.stderr.strip())


def _content_fact(root: Path, relative: str) -> tuple[str, str]:
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError(f"Runtime source path is unavailable: {relative}")
    text = path.read_text(encoding="utf-8")
    return text, commitment({"path": relative, "content": text})


def _dependency_state(text: str) -> DependencyReproducibility:
    entries = [line.strip() for line in text.splitlines()
               if line.strip() and not line.lstrip().startswith("#")]
    if not entries:
        return DependencyReproducibility.UNPINNED
    pinned = sum(bool(_PINNED.fullmatch(line)) for line in entries)
    if pinned == len(entries):
        return DependencyReproducibility.EXACTLY_PINNED
    if pinned:
        return DependencyReproducibility.PARTIALLY_PINNED
    return DependencyReproducibility.UNPINNED


def _version_fact(name: str, result: CommandResult, prefix: str) -> RuntimeReadinessFact:
    if result.returncode != 0:
        return RuntimeReadinessFact(
            name, RuntimeFactState.UNRESOLVED, None, f"{prefix} is unavailable")
    match = _VERSION.search(result.stdout)
    if match is None:
        return RuntimeReadinessFact(
            name, RuntimeFactState.CONTRADICTORY, None,
            f"{prefix} returned an unsupported version format")
    return RuntimeReadinessFact(
        name, RuntimeFactState.AVAILABLE, match.group(0),
        f"{prefix} version fresh-read")


class RuntimeImageReadinessResolver:
    """Fresh, read-only projection used as ``runtime_authority`` by Candidate-F."""

    def __init__(self, repository_root: str | Path, *,
                 _command_runner: CommandRunner | None = None) -> None:
        self._root = Path(repository_root).resolve()
        self._run = _command_runner or _default_runner

    def _command(self, *parts: str) -> CommandResult:
        result = self._run(parts, self._root)
        if type(result) is not CommandResult:
            raise TypeError("Runtime readiness command seam returned an invalid result")
        return result

    def _repository(self) -> tuple[str, bool]:
        revision = self._command("git", "rev-parse", "HEAD")
        status = self._command("git", "status", "--porcelain")
        value = revision.stdout.strip().lower()
        if (revision.returncode != 0 or status.returncode != 0
                or not re.fullmatch(r"[0-9a-f]{40}", value)):
            raise ValueError("current repository identity is unavailable")
        return value, bool(status.stdout.strip())

    def _source(self) -> RuntimeImageSourceIdentity:
        revision, dirty = self._repository()
        dockerfile_path = "docker/runtime/Dockerfile"
        runtime_config_path = "configs/runtime_orchestration.docker.yaml"
        compose_path = "docker-compose.yml"
        formal_config_path = "configs/rca_evaluation/docker_e2e.yaml"
        dependency_path = "requirements.txt"
        dockerfile, dockerfile_hash = _content_fact(self._root, dockerfile_path)
        _, runtime_config_hash = _content_fact(self._root, runtime_config_path)
        compose_text, compose_hash = _content_fact(self._root, compose_path)
        formal_text, formal_hash = _content_fact(self._root, formal_config_path)
        dependencies, dependency_hash = _content_fact(self._root, dependency_path)
        formal = yaml.safe_load(formal_text)
        compose = yaml.safe_load(compose_text)
        values = formal["docker"]
        entrypoint_path = values["runtime_entrypoint"]
        _, entrypoint_hash = _content_fact(self._root, entrypoint_path)
        service = compose["services"][values["runtime_service"]]
        source_binding = all(value in dockerfile for value in (
            "ARG SOURCE_REVISION", "ARG RUNTIME_IMAGE_SOURCE_COMMITMENT",
            "org.opencontainers.image.revision",
            "io.aiops.runtime.source-commitment",
            f"io.aiops.runtime.profile=\"{RUNTIME_IMAGE_PROFILE}\"",
        ))
        coherent = (
            values["runtime_dockerfile"] == dockerfile_path
            and values["runtime_config"] == runtime_config_path
            and values["compose_file"] == compose_path
            and service["build"]["context"] == "."
            and service["build"]["dockerfile"] == dockerfile_path
            and entrypoint_path in service["command"]
            and runtime_config_path in service["command"]
            and source_binding
        )
        source_definition = RuntimeReadinessFact(
            "runtime_image_source_definition",
            RuntimeFactState.AVAILABLE if coherent else RuntimeFactState.CONTRADICTORY,
            "SOURCE_DEFINITION_READY" if coherent else None,
            ("Dockerfile/Compose/Runtime configuration is coherent"
             if coherent else "Dockerfile/Compose/Runtime configuration is contradictory"),
        )
        formal_source = RuntimeReadinessFact(
            "formal_build_source",
            RuntimeFactState.UNRESOLVED if dirty else RuntimeFactState.AVAILABLE,
            None if dirty else f"git:{revision}",
            ("current repository is dirty; clean committed source is required"
             if dirty else "clean committed repository source fresh-read"),
        )
        base_match = re.search(r"(?m)^FROM\s+([^\s]+)", dockerfile)
        base = base_match.group(1) if base_match else "UNRESOLVED"
        digest_match = re.search(r"@(?P<digest>sha256:[0-9a-f]{64})$", base)
        base_digest = RuntimeReadinessFact(
            "base_image_digest",
            RuntimeFactState.AVAILABLE if digest_match else RuntimeFactState.UNRESOLVED,
            digest_match.group("digest") if digest_match else None,
            ("content-addressed base image digest is declared"
             if digest_match else "base image reference is not digest-resolved"),
        )
        build_context = commitment({
            "dockerfile_commitment": dockerfile_hash,
            "runtime_config_commitment": runtime_config_hash,
            "compose_commitment": compose_hash,
            "formal_config_commitment": formal_hash,
            "dependency_commitment": dependency_hash,
            "entrypoint_commitment": entrypoint_hash,
        })
        values_without_commitment = {
            "repository_revision": revision,
            "repository_dirty": dirty,
            "dockerfile": {"path": dockerfile_path, "commitment": dockerfile_hash},
            "build_context_identity": build_context,
            "runtime_config": {"path": runtime_config_path,
                               "commitment": runtime_config_hash},
            "compose": {"path": compose_path, "commitment": compose_hash},
            "formal_config": {"path": formal_config_path,
                              "commitment": formal_hash},
            "dependency_definition": {
                "path": dependency_path, "commitment": dependency_hash,
                "reproducibility": _dependency_state(dependencies).value,
            },
            "entrypoint": {"path": entrypoint_path,
                           "commitment": entrypoint_hash},
            "logical_image_identity": RUNTIME_IMAGE_PROFILE,
            "image_tag": RUNTIME_IMAGE_TAG,
            "base_image_reference": base,
            "base_image_digest": base_digest.material(),
            "source_definition": source_definition.material(),
            "formal_build_source": formal_source.material(),
        }
        return RuntimeImageSourceIdentity(
            revision, dirty, dockerfile_path, dockerfile_hash, build_context,
            runtime_config_path, runtime_config_hash, compose_path, compose_hash,
            formal_config_path, formal_hash, dependency_path, dependency_hash,
            _dependency_state(dependencies), entrypoint_path, entrypoint_hash,
            RUNTIME_IMAGE_PROFILE, RUNTIME_IMAGE_TAG, base, base_digest,
            source_definition, formal_source, commitment(values_without_commitment),
        )

    def _engine(self) -> RuntimeReadinessFact:
        result = self._command(
            "docker", "version", "--format", "{{json .Server}}")
        if result.returncode != 0:
            return RuntimeReadinessFact(
                "docker_engine", RuntimeFactState.UNRESOLVED, None,
                "Docker Engine/server is unavailable")
        try:
            value = json.loads(result.stdout)
            version, api = value["Version"], value["ApiVersion"]
            if not (_VERSION.fullmatch(version) and _VERSION.fullmatch(api)):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError,
                json.JSONDecodeError):
            return RuntimeReadinessFact(
                "docker_engine", RuntimeFactState.CONTRADICTORY, None,
                "Docker Engine returned unsupported version facts")
        return RuntimeReadinessFact(
            "docker_engine", RuntimeFactState.AVAILABLE,
            {"version": version, "api_version": api},
            "Docker Engine/server version fresh-read")

    def _actual_image(self, source: RuntimeImageSourceIdentity) -> RuntimeReadinessFact:
        result = self._command("docker", "image", "inspect", source.image_tag)
        if result.returncode != 0:
            return RuntimeReadinessFact(
                "actual_runtime_image", RuntimeFactState.UNRESOLVED, None,
                "runtime image is not built or is unavailable")
        try:
            items = json.loads(result.stdout)
            if not isinstance(items, list) or len(items) != 1:
                raise ValueError
            item = items[0]
            image_id = item["Id"]
            labels = item["Config"]["Labels"]
            repo_digests = item.get("RepoDigests") or []
            if (not _SHA256.fullmatch(image_id)
                    or labels.get("org.opencontainers.image.revision")
                        != source.repository_revision
                    or labels.get("io.aiops.runtime.source-commitment")
                        != source.source_commitment
                    or labels.get("io.aiops.runtime.profile")
                        != source.logical_image_identity
                    or not isinstance(repo_digests, list)
                    or any(not isinstance(value, str)
                           or not re.fullmatch(
                               r"[^@\s]+@sha256:[0-9a-f]{64}", value)
                           for value in repo_digests)):
                raise ValueError
            digests = sorted(value for value in repo_digests
                             if "@sha256:" in value)
            actual_digest = digests[0].split("@", 1)[1] if digests else image_id
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return RuntimeReadinessFact(
                "actual_runtime_image", RuntimeFactState.CONTRADICTORY, None,
                "runtime image metadata contradicts current source/profile")
        return RuntimeReadinessFact(
            "actual_runtime_image", RuntimeFactState.AVAILABLE,
            {"image_id": image_id, "image_digest": actual_digest,
             "repo_digests": digests,
             "verified_labels": {
                 "org.opencontainers.image.revision":
                     labels["org.opencontainers.image.revision"],
                 "io.aiops.runtime.source-commitment":
                     labels["io.aiops.runtime.source-commitment"],
                 "io.aiops.runtime.profile":
                     labels["io.aiops.runtime.profile"],
             }},
            "content-addressed image identity and source labels fresh-read")

    def formal_readiness(self) -> RuntimeFormalReadinessFacts:
        """Fresh-read source/tool/image facts without Runtime or Docker mutation."""
        source = self._source()
        client = _version_fact(
            "docker_client", self._command("docker", "--version"), "Docker client")
        compose = _version_fact(
            "docker_compose", self._command("docker", "compose", "version"),
            "Docker Compose")
        facts_without_commitment = {
            "schema": RUNTIME_IMAGE_READINESS_SCHEMA,
            "source": source.material(),
            "docker_client": client.material(),
            "docker_engine": (engine := self._engine()).material(),
            "docker_compose": compose.material(),
            "actual_image": (actual := self._actual_image(source)).material(),
        }
        result = RuntimeFormalReadinessFacts(
            RUNTIME_IMAGE_READINESS_SCHEMA, source, client, engine, compose,
            actual, commitment(facts_without_commitment))
        result.validate()
        return result
