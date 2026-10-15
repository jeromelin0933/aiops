"""Evaluation-owned Docker isolation backed by the single Candidate-F ledger."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re

import yaml

from .gate_records import (
    DockerCleanup, DockerNamespace, read_docker_namespace,
    record_docker_cleanup, record_docker_namespace,
)
from .gates import (
    AuthorizationRequest, DockerGateProfile, GateKind, GateReadiness,
    PrerequisiteCheck, _status,
)
from .sqlite_store import EvaluationLedgerReader, SqliteEvaluationStore


_SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{5,62}")


class DockerLifecyclePhase(str, Enum):
    BUILD = "BUILD"
    START = "START"
    STOP = "STOP"
    CLEANUP = "CLEANUP"


def allocate_namespace(
    authority: SqliteEvaluationStore,
    profile: DockerGateProfile, *, evaluation_run_id: str,
    execution_id: str, issuance_nonce: str, issued_at: str,
) -> DockerNamespace:
    """Harness allocation path; normal execution callers only receive the record id."""
    values = profile.values()
    if values["compose_project_prefix"] != values["volume_namespace_prefix"]:
        raise ValueError("Docker project and volume prefixes must agree")
    record = record_docker_namespace(
        authority,
        evaluation_run_id=evaluation_run_id, execution_id=execution_id,
        profile_identity=profile.profile_identity,
        profile_commitment=profile.profile_commitment,
        project_prefix=values["compose_project_prefix"],
        volume_roles=tuple(values["required_volume_roles"]),
        workspace_prefix="var/rca_evaluation/docker",
        issuance_nonce=issuance_nonce, issued_at=issued_at)
    _validate_issuance(profile, record)
    return record


def _validate_issuance(profile: DockerGateProfile,
                       record: DockerNamespace) -> None:
    values = profile.values()
    expected = tuple(
        f"{record.project_name}_{role}" for role in values["required_volume_roles"])
    forbidden = set(values["forbidden_shared_names"])
    if ((record.profile_identity, record.profile_commitment) != (
            profile.profile_identity, profile.profile_commitment)
            or not _SAFE_NAME.fullmatch(record.project_name)
            or record.project_name in forbidden
            or record.volume_identities != expected
            or any(not _SAFE_NAME.fullmatch(item) or item in forbidden
                   for item in record.volume_identities)
            or record.workspace_root !=
                f"var/rca_evaluation/docker/{record.project_name}"):
        raise ValueError("Docker issuance is not exact evaluation-owned authority")


@dataclass(frozen=True, slots=True)
class DockerReadinessFacts:
    authorization: AuthorizationRequest
    docker_cli_available: bool
    compose_available: bool
    runtime_image_available: bool
    issuance_id: str | None


def evaluate_docker_harness(
    profile: DockerGateProfile, facts: DockerReadinessFacts, *,
    repository_root: str | Path, authority: EvaluationLedgerReader,
    evaluation_run_id: str, execution_id: str,
) -> GateReadiness:
    values = profile.values()
    root = Path(repository_root)
    files_ready = all((root / values[key]).is_file() for key in (
        "compose_file", "runtime_dockerfile", "runtime_entrypoint", "runtime_config"))
    compose_reuses_runtime = False
    if files_ready:
        try:
            compose = yaml.safe_load(
                (root / values["compose_file"]).read_text(encoding="utf-8"))
            service = compose["services"][values["runtime_service"]]
            command = service["command"]
            compose_reuses_runtime = (
                service["build"]["dockerfile"] == values["runtime_dockerfile"]
                and values["runtime_entrypoint"] in command
                and values["runtime_config"] in command)
        except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError):
            compose_reuses_runtime = False
    issuance_valid = False
    if facts.issuance_id:
        try:
            record = read_docker_namespace(authority, facts.issuance_id)
            if record is None:
                raise ValueError("durable Docker issuance missing")
            _validate_issuance(profile, record)
            issuance_valid = (
                record.evaluation_run_id == evaluation_run_id
                and record.execution_id == execution_id)
        except (TypeError, ValueError):
            issuance_valid = False
    checks = (
        PrerequisiteCheck("docker_cli", facts.docker_cli_available,
                          "Docker CLI availability; no command executed"),
        PrerequisiteCheck("docker_compose", facts.compose_available,
                          "Compose capability availability; no command executed"),
        PrerequisiteCheck("existing_spec011_runtime",
                          files_ready and compose_reuses_runtime,
                          "existing Dockerfile, Runtime entrypoint, and config"),
        PrerequisiteCheck("runtime_image", facts.runtime_image_available,
                          "existing runtime image readiness"),
        PrerequisiteCheck("isolated_namespace", issuance_valid,
                          "durable unique Candidate-F namespace issuance"),
    )
    return _status(
        GateKind.DOCKER_RCA_E2E, profile.profile_identity,
        profile.profile_commitment, facts.authorization, authority, checks,
        evaluation_run_id=evaluation_run_id, execution_id=execution_id)


def cleanup_targets(
    authority: EvaluationLedgerReader, profile: DockerGateProfile, *,
    issuance_id: str, evaluation_run_id: str, execution_id: str,
    compose_project_name: str, volume_identities: tuple[str, ...],
    workspace_root: str,
) -> tuple[str, tuple[str, ...]]:
    """Re-read original durable issuance before authorizing cleanup targets."""
    record = read_docker_namespace(authority, issuance_id)
    if record is None:
        raise ValueError("durable Docker issuance is missing")
    _validate_issuance(profile, record)
    if ((record.evaluation_run_id, record.execution_id) != (
            evaluation_run_id, execution_id)
            or compose_project_name != record.project_name
            or volume_identities != record.volume_identities
            or workspace_root != record.workspace_root):
        raise ValueError("Docker cleanup ownership is foreign, ambiguous, or mismatched")
    return record.project_name, record.volume_identities


def lifecycle_command(
    authority: EvaluationLedgerReader, profile: DockerGateProfile, *,
    issuance_id: str, evaluation_run_id: str, execution_id: str,
    phase: DockerLifecyclePhase,
) -> tuple[str, ...]:
    record = read_docker_namespace(authority, issuance_id)
    if record is None:
        raise ValueError("durable Docker issuance is missing")
    cleanup_targets(
        authority, profile, issuance_id=issuance_id,
        evaluation_run_id=evaluation_run_id, execution_id=execution_id,
        compose_project_name=record.project_name,
        volume_identities=record.volume_identities,
        workspace_root=record.workspace_root)
    values = profile.values()
    base = ("docker", "compose", "-p", record.project_name,
            "-f", values["compose_file"])
    if phase is DockerLifecyclePhase.BUILD:
        return base + ("build", values["runtime_service"])
    if phase is DockerLifecyclePhase.START:
        return base + ("up", "-d", values["runtime_service"])
    if phase is DockerLifecyclePhase.STOP:
        return base + ("stop", "-t", "30", values["runtime_service"])
    if phase is DockerLifecyclePhase.CLEANUP:
        return base + ("down", "--volumes", "--remove-orphans")
    raise TypeError("unsupported Docker lifecycle phase")


def record_cleanup_outcome(
    authority: SqliteEvaluationStore, *, issuance_id: str,
    completed_at: str, disposition: str,
    invocation_start_id: str | None = None,
    invocation_id: str | None = None,
    invocation_started_at: str | None = None,
) -> DockerCleanup:
    namespace = read_docker_namespace(authority.reader(), issuance_id)
    if namespace is None:
        raise ValueError("durable Docker namespace is missing")
    return record_docker_cleanup(
        authority, namespace_id=issuance_id,
        project_name=namespace.project_name,
        volume_identities=namespace.volume_identities,
        workspace_root=namespace.workspace_root,
        completed_at=completed_at, disposition=disposition,
        invocation_start_id=invocation_start_id,
        invocation_id=invocation_id,
        invocation_started_at=invocation_started_at)
