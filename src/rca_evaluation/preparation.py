"""PM-authorized pre-admission preparation for SPEC-017 Candidate-F.

Preparation issues a durable, deterministic future-target identity and its
empty evaluation-owned directory/ownership marker.  It does not create an
evaluation run, execution, Docker resource, RAG index, gate receipt, or
denominator row.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Mapping

import yaml

from .identity import commitment, identity, require_identity
from .sqlite_store import SqliteEvaluationStore


PREPARATION_AUTHORITY = "PM-SPEC017-R4-PREADMISSION-20261016"
PREPARATION_SCHEMA = "spec017.preparation.v1"
PREPARED_CHILD_MARKER = ".candidate-f-ownership.json"


class PreparationLifecycle(str, Enum):
    """Governance states; EXECUTION is a boundary, not a Slice-0 action."""

    PREREGISTERED = "PREREGISTERED"
    PREPARED = "PREPARED"
    ADMITTED = "ADMITTED"
    EXECUTION = "EXECUTION"


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
        if (root / "real_rag").exists() and not database.exists():
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
        },
        "real_rag_child": {
            "child_identity": child_identity,
            "relative_path": relative_path,
            "profile_identity": rag_profile["profile_identity"],
            "ownership": preparation_id,
            "state": "ALLOCATED",
            "ownership_commitment": ownership_commitment,
        },
        "readiness_preparation": {
            "state": "PREPARED_NOT_VALIDATED_FOR_ADMISSION",
            "material": readiness_material,
            "commitment": commitment(readiness_material),
        },
        "effects": {
            "formal_execution": False, "external_gates": False,
            "scenario_injection": False, "denominator_consumed": False,
        },
    }


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
    if isolated_root.exists() and any(isolated_root.iterdir()):
        raise ValueError("duplicate or stale Real-RAG child allocation exists")
    isolated_root.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    marker.write_text(json.dumps(expected_marker, sort_keys=True, separators=(",", ":")),
                      encoding="utf-8")


def verify_prepared_real_rag_child(
    store: SqliteEvaluationStore, preparation_id: str,
    repository_root: str | Path,
) -> dict[str, object]:
    """Fresh-read durable allocation, filesystem ownership, and empty state."""
    require_identity(preparation_id, "preparation")
    prepared = store.get_preparation(preparation_id)
    if prepared is None:
        raise ValueError("prepared Real-RAG allocation is missing")
    child = prepared["real_rag_child"]
    lineage = prepared["planned_lineage"]
    if (child.get("ownership") != preparation_id
            or child.get("state") != "ALLOCATED"):
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
    entries = tuple(target.iterdir())
    if entries != (marker,) and set(entries) != {marker}:
        raise ValueError("prepared Real-RAG child contains unexpected state")
    try:
        actual_marker = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("prepared Real-RAG child ownership marker is missing or invalid") from exc
    if actual_marker != _expected_child_marker(prepared):
        raise ValueError("prepared Real-RAG child ownership marker mismatch")
    siblings = tuple(isolated_root.iterdir())
    if len(siblings) != 1 or siblings[0] != target:
        raise ValueError("duplicate or stale Real-RAG child allocation exists")
    return dict(child)


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
