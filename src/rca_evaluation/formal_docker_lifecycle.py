"""Slice 5 read/command boundaries for one governed Formal Docker envelope.

This module deliberately contains no Docker SDK or subprocess invocation.  A
future authorized composition supplies an inspector and a narrowly typed
operations port.  Names and paths always originate in Candidate-F's prepared
``FormalComposeEnvelope``; the ports never accept arbitrary command strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Protocol

from .evaluation_controls import (
    CONTROL_ARTIFACT_COMMITMENT, EvaluationControlArtifact,
    load_evaluation_control_artifact,
)
from .formal_compose import (
    SERVICE_CLASSIFICATION, FormalComposeCampaignTopology, FormalComposeEnvelope,
    FormalServiceClassification, resolve_formal_compose_topology,
)
from .formal_scenario_io import InjectionLifecycleState, injection_lifecycle_state
from .identity import commitment, require_identity
from .ledger import LedgerRecord
from .readiness import ReadinessFactState, RequiredFact
from .recovery_evaluation import RecoveryBoundary, RcaPublicState, _evaluate_restart_states
from .runtime_image_readiness import RUNTIME_IMAGE_PROFILE
from .sqlite_store import SqliteEvaluationStore


LIFECYCLE_SCHEMA = "spec017.formal-docker-lifecycle.v1"
CONTROL_ARTIFACT_PATH = (
    "configs/rca_evaluation/fixtures/spec017_r2s4_evaluation_controls_v2.json"
)
SPEC011_RECOVERY_AUTHORITY = "SPEC-011_STARTUP_RECOVERY"
_OWNER_LABELS = (
    "com.docker.compose.project", "spec017.preparation_id",
    "spec017.planned_execution_identity", "spec017.topology_commitment",
)


class DockerResourceKind(str, Enum):
    CONTAINER = "CONTAINER"
    NETWORK = "NETWORK"
    VOLUME = "VOLUME"
    WORKSPACE = "WORKSPACE"


class DockerResourceState(str, Enum):
    ABSENT = "ABSENT"
    PRESENT = "PRESENT"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class OperationalState(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNHEALTHY = "UNHEALTHY"
    UNRESOLVED = "UNRESOLVED"
    CONTRADICTORY = "CONTRADICTORY"


class LifecycleFailure(str, Enum):
    UNRESOLVED = "UNRESOLVED"
    OWNERSHIP_CONFLICT = "OWNERSHIP_CONFLICT"
    RESOURCE_MISSING = "RESOURCE_MISSING"
    RESOURCE_STATE_CONFLICT = "RESOURCE_STATE_CONFLICT"
    HEALTH_FAILURE = "HEALTH_FAILURE"
    RESTART_FAILED = "RESTART_FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    POSTCHECK_FAILED = "POSTCHECK_FAILED"


class LifecycleOperationKind(str, Enum):
    RESTART = "RESTART"
    CLEANUP = "CLEANUP"


class ResourceRetention(str, Enum):
    DELETE_ALLOWED = "DELETE_ALLOWED"
    PRESERVE_REQUIRED = "PRESERVE_REQUIRED"
    NOT_OWNED = "NOT_OWNED"


class LifecycleResultState(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    IDEMPOTENT_ABSENT = "IDEMPOTENT_ABSENT"
    BLOCKED = "BLOCKED"
    UNRESOLVED = "UNRESOLVED"


def expected_ownership_labels(envelope: FormalComposeEnvelope) -> tuple[tuple[str, str], ...]:
    envelope.validate()
    return tuple(sorted({
        "com.docker.compose.project": envelope.compose_project_identity,
        "spec017.preparation_id": envelope.preparation_id,
        "spec017.planned_execution_identity": envelope.planned_execution_identity,
        "spec017.topology_commitment": envelope.topology_commitment,
    }.items()))


@dataclass(frozen=True, slots=True)
class InspectedDockerResource:
    kind: DockerResourceKind
    identity: str
    state: DockerResourceState
    ownership_labels: tuple[tuple[str, str], ...]
    service: str | None = None
    image_profile: str | None = None
    image_identity: str | None = None
    mounts: tuple[tuple[str, str], ...] = ()
    health: OperationalState | None = None

    def validate_shape(self) -> None:
        if (not isinstance(self.kind, DockerResourceKind)
                or not isinstance(self.state, DockerResourceState)
                or not isinstance(self.identity, str) or not self.identity
                or tuple(sorted(self.ownership_labels)) != self.ownership_labels
                or len(dict(self.ownership_labels)) != len(self.ownership_labels)
                or self.kind is DockerResourceKind.CONTAINER
                    and (not self.service or not self.image_profile
                         or not isinstance(self.health, OperationalState))
                or self.kind is not DockerResourceKind.CONTAINER
                    and (self.service is not None or self.image_profile is not None
                         or self.image_identity is not None or self.mounts
                         or self.health is not None)):
            raise ValueError("Docker inspection resource is malformed")


@dataclass(frozen=True, slots=True)
class DockerProjectInspection:
    schema: str
    project_identity: str
    topology_commitment: str
    resources: tuple[InspectedDockerResource, ...]
    inspected_at: str
    inspection_commitment: str

    @classmethod
    def create(
        cls, envelope: FormalComposeEnvelope,
        resources: tuple[InspectedDockerResource, ...], *, inspected_at: str,
    ) -> "DockerProjectInspection":
        envelope.validate()
        if not isinstance(inspected_at, str) or not inspected_at:
            raise ValueError("Docker inspection time is required")
        for resource in resources:
            resource.validate_shape()
        material = {
            "schema": LIFECYCLE_SCHEMA,
            "project_identity": envelope.compose_project_identity,
            "topology_commitment": envelope.topology_commitment,
            "resources": [resource_material(item) for item in resources],
            "inspected_at": inspected_at,
        }
        return cls(
            LIFECYCLE_SCHEMA, envelope.compose_project_identity,
            envelope.topology_commitment, resources, inspected_at,
            commitment(material))

    def validate(self) -> None:
        material = {
            "schema": self.schema, "project_identity": self.project_identity,
            "topology_commitment": self.topology_commitment,
            "resources": [resource_material(item) for item in self.resources],
            "inspected_at": self.inspected_at,
        }
        for resource in self.resources:
            resource.validate_shape()
        if (self.schema != LIFECYCLE_SCHEMA
                or len({(item.kind, item.identity) for item in self.resources})
                    != len(self.resources)
                or commitment(material) != self.inspection_commitment):
            raise ValueError("Docker inspection is contradictory")


def resource_material(resource: InspectedDockerResource) -> dict[str, object]:
    return {
        "kind": resource.kind.value, "identity": resource.identity,
        "state": resource.state.value,
        "ownership_labels": dict(resource.ownership_labels),
        "service": resource.service, "image_profile": resource.image_profile,
        "image_identity": resource.image_identity,
        "mounts": [list(item) for item in resource.mounts],
        "health": resource.health.value if resource.health is not None else None,
    }


class DockerStateInspector(Protocol):
    def inspect(self, envelope: FormalComposeEnvelope) -> DockerProjectInspection: ...


class FormalDockerOperations(Protocol):
    def restart_exact(
        self, envelope: FormalComposeEnvelope, operation_identity: str,
    ) -> None: ...

    def remove_exact_containers(
        self, envelope: FormalComposeEnvelope, operation_identity: str,
    ) -> None: ...

    def remove_exact_network(
        self, envelope: FormalComposeEnvelope, operation_identity: str,
    ) -> None: ...

    def remove_exact_volumes(
        self, envelope: FormalComposeEnvelope, volume_identities: tuple[str, ...],
        operation_identity: str,
    ) -> None: ...

    def clean_exact_workspace(
        self, envelope: FormalComposeEnvelope, operation_identity: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class OwnershipAssessment:
    state: OperationalState
    failure: LifecycleFailure | None
    details: tuple[str, ...]


def assess_exact_ownership(
    envelope: FormalComposeEnvelope, inspection: DockerProjectInspection,
) -> OwnershipAssessment:
    """Fail closed on a missing, partial, duplicate, foreign, or stale project."""
    envelope.validate()
    try:
        inspection.validate()
    except (AttributeError, TypeError, ValueError):
        return OwnershipAssessment(
            OperationalState.CONTRADICTORY, LifecycleFailure.OWNERSHIP_CONFLICT,
            ("inspection integrity/commitment mismatch",))
    if (inspection.project_identity != envelope.compose_project_identity
            or inspection.topology_commitment != envelope.topology_commitment):
        return OwnershipAssessment(
            OperationalState.CONTRADICTORY, LifecycleFailure.OWNERSHIP_CONFLICT,
            ("project/topology ownership mismatch",))
    if not inspection.resources:
        return OwnershipAssessment(
            OperationalState.UNRESOLVED, LifecycleFailure.RESOURCE_MISSING,
            ("exact project resources are absent",))
    labels = expected_ownership_labels(envelope)
    details: list[str] = []
    for item in inspection.resources:
        if item.ownership_labels != labels:
            details.append(f"ownership labels mismatch:{item.kind.value}:{item.identity}")
    expected_volumes = {item.volume_identity for item in envelope.volume_mounts}
    actual_volumes = {
        item.identity for item in inspection.resources
        if item.kind is DockerResourceKind.VOLUME
        and item.state is not DockerResourceState.ABSENT
    }
    networks = tuple(
        item for item in inspection.resources
        if item.kind is DockerResourceKind.NETWORK
        and item.state is not DockerResourceState.ABSENT)
    containers = tuple(
        item for item in inspection.resources
        if item.kind is DockerResourceKind.CONTAINER
        and item.state is not DockerResourceState.ABSENT)
    services = tuple(item.service for item in containers)
    if actual_volumes != expected_volumes:
        details.append("execution volume set mismatch")
    if len(networks) != 1 or networks[0].identity != envelope.network_identity:
        details.append("execution network mismatch")
    workspaces = tuple(
        item for item in inspection.resources
        if item.kind is DockerResourceKind.WORKSPACE
        and item.state is not DockerResourceState.ABSENT)
    if (len(workspaces) != 1
            or workspaces[0].identity != envelope.workspace_identity):
        details.append("execution workspace mismatch")
    if set(services) != set(envelope.services) or len(services) != len(set(services)):
        details.append("required container/service set mismatch")
    prefixes = dict(zip(envelope.services, envelope.container_name_prefixes))
    for item in containers:
        if (item.service not in prefixes
                or not item.identity.startswith(prefixes[item.service])
                or item.image_profile != RUNTIME_IMAGE_PROFILE
                or set(item.mounts) != {
                    (mount.volume_identity, mount.target)
                    for mount in envelope.volume_mounts
                    if mount.role in {"formal-logs", "runtime-events",
                                      "runtime-knowledge", "runtime-state"}
                } and item.service == "runtime"):
            details.append(f"container identity/image/mount mismatch:{item.identity}")
    if details:
        return OwnershipAssessment(
            OperationalState.CONTRADICTORY, LifecycleFailure.OWNERSHIP_CONFLICT,
            tuple(sorted(set(details))))
    return OwnershipAssessment(OperationalState.AVAILABLE, None, ())


@dataclass(frozen=True, slots=True)
class FormalHealthProjection:
    operational_state: OperationalState
    required_services: tuple[tuple[str, OperationalState], ...]
    optional_services: tuple[tuple[str, OperationalState], ...]
    ignored_services: tuple[str, ...]
    formal_readiness: ReadinessFactState
    reason: str


def project_health(
    envelope: FormalComposeEnvelope,
    inspection: DockerProjectInspection | None,
) -> FormalHealthProjection:
    """Operational health is never promoted to whole Formal readiness."""
    required = tuple(sorted(
        name for name, classification in SERVICE_CLASSIFICATION.items()
        if classification is FormalServiceClassification.FORMAL_REQUIRED))
    optional = tuple(sorted(
        name for name, classification in SERVICE_CLASSIFICATION.items()
        if classification is FormalServiceClassification.OPTIONAL_DIAGNOSTIC))
    ignored = tuple(sorted(
        name for name, classification in SERVICE_CLASSIFICATION.items()
        if classification is FormalServiceClassification.NOT_REQUIRED))
    if inspection is None:
        unresolved = tuple((name, OperationalState.UNRESOLVED) for name in required)
        return FormalHealthProjection(
            OperationalState.UNRESOLVED, unresolved,
            tuple((name, OperationalState.UNRESOLVED) for name in optional),
            ignored, ReadinessFactState.UNRESOLVED,
            "actual Docker health has not been fresh-inspected")
    ownership = assess_exact_ownership(envelope, inspection)
    if ownership.state is OperationalState.CONTRADICTORY:
        return FormalHealthProjection(
            OperationalState.CONTRADICTORY,
            tuple((name, OperationalState.CONTRADICTORY) for name in required),
            tuple((name, OperationalState.UNRESOLVED) for name in optional),
            ignored, ReadinessFactState.CONTRADICTORY,
            ";".join(ownership.details))
    containers = {item.service: item for item in inspection.resources
                  if item.kind is DockerResourceKind.CONTAINER}

    def state(name: str) -> OperationalState:
        item = containers.get(name)
        if item is None or item.state is DockerResourceState.ABSENT:
            return OperationalState.UNAVAILABLE
        if item.state is DockerResourceState.FAILED:
            return OperationalState.UNHEALTHY
        if item.state is DockerResourceState.RUNNING:
            return item.health
        return OperationalState.UNAVAILABLE

    required_states = tuple((name, state(name)) for name in required)
    optional_states = tuple((name, state(name)) for name in optional)
    required_values = {value for _, value in required_states}
    operational = (
        OperationalState.CONTRADICTORY
        if OperationalState.CONTRADICTORY in required_values
        else OperationalState.UNHEALTHY
        if OperationalState.UNHEALTHY in required_values
        else OperationalState.UNRESOLVED
        if OperationalState.UNRESOLVED in required_values
        else OperationalState.UNAVAILABLE
        if OperationalState.UNAVAILABLE in required_values
        else OperationalState.AVAILABLE)
    return FormalHealthProjection(
        operational, required_states, optional_states, ignored,
        ReadinessFactState.UNRESOLVED,
        "container health cannot resolve credential, Real-RAG, image, or Formal facts")


@dataclass(frozen=True, slots=True)
class InterruptionContract:
    boundary: RecoveryBoundary
    fixture_identity: str
    exact_control_hook_identity: str
    control_reference: str
    crash_point: str
    restart_reopen_method: str
    preconditions: tuple[str, ...]
    forbidden_preconditions: tuple[str, ...]
    post_restart_checks: tuple[str, ...]
    duplicate_prohibitions: tuple[str, ...]
    control_commitment: str


def approved_interruption_matrix(
    repository_root: str | Path,
) -> tuple[InterruptionContract, ...]:
    artifact: EvaluationControlArtifact = load_evaluation_control_artifact(
        Path(repository_root).resolve() / CONTROL_ARTIFACT_PATH)
    if artifact.artifact_commitment != CONTROL_ARTIFACT_COMMITMENT:
        raise ValueError("restart control artifact commitment is contradictory")
    values = tuple(InterruptionContract(
        RecoveryBoundary(item.boundary_identity), item.fixture_identity,
        item.exact_control_hook_identity, item.control_reference,
        item.crash_point, item.restart_reopen_method,
        item.pre_crash_required_facts, item.pre_crash_forbidden_facts,
        item.expected_terminal_public_facts, item.no_duplicate_assertions,
        item.control_commitment,
    ) for item in artifact.restart_controls)
    if tuple(item.boundary for item in values) != tuple(RecoveryBoundary):
        raise ValueError("approved five-point interruption order is unavailable")
    return values


@dataclass(frozen=True, slots=True)
class ExecutionDockerAuthority:
    execution_identity: str
    evaluation_run_identity: str
    envelope: FormalComposeEnvelope
    injection_state: InjectionLifecycleState
    event_binding_commitment: str | None
    authority_commitment: str


def _record_id(execution_id: str, label: str) -> str:
    return LedgerRecord.create("observation", [execution_id, label], {}, execution_id).record_id


def resolve_execution_docker_authority(
    store: SqliteEvaluationStore, preparation_id: str, execution_identity: str,
    repository_root: str | Path,
) -> ExecutionDockerAuthority:
    """Fresh-read Candidate-F and bind an actual execution to one planned envelope."""
    if type(store) is not SqliteEvaluationStore:
        raise TypeError("Docker lifecycle requires SqliteEvaluationStore")
    require_identity(execution_identity, "execution")
    execution = store.get(execution_identity)
    if execution is None or execution.domain != "execution" or not execution.parent_id:
        raise ValueError("durable Formal execution is unavailable")
    store.require_formal_run(execution.parent_id)
    payload = json.loads(execution.semantic_json)
    scenario = payload.get("scenario_id")
    repetition = payload.get("repetition")
    topology = resolve_formal_compose_topology(
        store, preparation_id, repository_root)
    matches = tuple(item for item in topology.envelopes
                    if item.scenario_id == scenario and item.repetition == repetition)
    if len(matches) != 1:
        raise ValueError("execution has no exact prepared Docker envelope")
    envelope = matches[0]
    injection_state = injection_lifecycle_state(store, execution_identity)
    binding = store.get(_record_id(execution_identity, "formal-event-binding"))
    binding_commitment = binding.semantic_commitment if binding is not None else None
    material = {
        "execution_identity": execution_identity,
        "evaluation_run_identity": execution.parent_id,
        "planned_execution_identity": envelope.planned_execution_identity,
        "topology_commitment": envelope.topology_commitment,
        "injection_state": injection_state.value,
        "event_binding_commitment": binding_commitment,
    }
    return ExecutionDockerAuthority(
        execution_identity, execution.parent_id, envelope, injection_state,
        binding_commitment, commitment(material))


@dataclass(frozen=True, slots=True)
class LifecycleOperationResult:
    operation_identity: str
    kind: LifecycleOperationKind
    state: LifecycleResultState
    failure: LifecycleFailure | None
    pre_inspection_commitment: str
    post_inspection_commitment: str | None
    details: tuple[str, ...]


def _operation_identity(
    authority: ExecutionDockerAuthority, kind: LifecycleOperationKind,
    expected_inspection: DockerProjectInspection,
) -> str:
    return "docker_lifecycle:" + commitment({
        "schema": LIFECYCLE_SCHEMA,
        "execution_identity": authority.execution_identity,
        "planned_execution_identity": authority.envelope.planned_execution_identity,
        "kind": kind.value,
        "target_topology_commitment": authority.envelope.topology_commitment,
        # Fresh-read time is receipt provenance, not lifecycle semantics.
        "expected_current_state": commitment({
            "project_identity": expected_inspection.project_identity,
            "topology_commitment": expected_inspection.topology_commitment,
            "resources": [resource_material(item)
                          for item in expected_inspection.resources],
        }),
    })[7:]


_PRESERVED_VOLUME_ROLES = frozenset({
    "formal-logs", "runtime-events", "runtime-knowledge", "runtime-state",
})


def volume_retention(envelope: FormalComposeEnvelope) -> tuple[tuple[str, ResourceRetention], ...]:
    envelope.validate()
    return tuple((item.volume_identity,
                  ResourceRetention.PRESERVE_REQUIRED
                  if item.role in _PRESERVED_VOLUME_ROLES
                  else ResourceRetention.DELETE_ALLOWED)
                 for item in envelope.volume_mounts)


@dataclass(frozen=True, slots=True)
class CleanupPostcondition:
    delete_targets_absent: bool
    preserved_resources_valid: bool
    findings: tuple[str, ...]


def assess_cleanup_postconditions(
    envelope: FormalComposeEnvelope,
    inspection: DockerProjectInspection,
) -> CleanupPostcondition:
    """Validate a cleanup state without requiring deleted resources to exist."""
    envelope.validate()
    findings: list[str] = []
    try:
        inspection.validate()
    except (AttributeError, TypeError, ValueError):
        return CleanupPostcondition(
            False, False, ("inspection integrity/commitment mismatch",))
    if inspection.project_identity != envelope.compose_project_identity:
        findings.append("postcheck project identity mismatch")
    if inspection.topology_commitment != envelope.topology_commitment:
        findings.append("postcheck topology commitment mismatch")

    expected_labels = expected_ownership_labels(envelope)
    retention = dict(volume_retention(envelope))
    expected_volumes = set(retention)
    expected_services = set(envelope.services)
    prefixes = dict(zip(envelope.services, envelope.container_name_prefixes))
    by_identity = {item.identity: item for item in inspection.resources}
    delete_targets_absent = True

    for item in inspection.resources:
        present = item.state is not DockerResourceState.ABSENT
        if item.kind is DockerResourceKind.CONTAINER:
            if (item.service not in expected_services
                    or not item.identity.startswith(prefixes[item.service])):
                findings.append(f"unexpected container identity:{item.identity}")
            if present:
                delete_targets_absent = False
                findings.append(f"cleanup container still exists:{item.identity}")
        elif item.kind is DockerResourceKind.NETWORK:
            if item.identity != envelope.network_identity:
                findings.append(f"unexpected network identity:{item.identity}")
            if present:
                delete_targets_absent = False
                findings.append(f"cleanup network still exists:{item.identity}")
        elif item.kind is DockerResourceKind.VOLUME:
            if item.identity not in expected_volumes:
                findings.append(f"unexpected volume identity:{item.identity}")
                continue
            if retention[item.identity] is ResourceRetention.DELETE_ALLOWED:
                if present:
                    delete_targets_absent = False
                    findings.append(
                        f"delete-allowed volume still exists:{item.identity}")
            elif present and item.ownership_labels != expected_labels:
                findings.append(
                    f"preserved volume ownership mismatch:{item.identity}")
        elif item.kind is DockerResourceKind.WORKSPACE:
            if item.identity != envelope.workspace_identity:
                findings.append(f"unexpected workspace identity:{item.identity}")
            elif present and item.ownership_labels != expected_labels:
                findings.append("preserved workspace ownership mismatch")

    preserved_valid = True
    for identity, policy in retention.items():
        if policy is not ResourceRetention.PRESERVE_REQUIRED:
            continue
        item = by_identity.get(identity)
        if (item is None or item.kind is not DockerResourceKind.VOLUME
                or item.state is DockerResourceState.ABSENT):
            findings.append(f"preserved evidence missing:{identity}")
            preserved_valid = False
        elif item.ownership_labels != expected_labels:
            preserved_valid = False
    workspace = by_identity.get(envelope.workspace_identity)
    if (workspace is None or workspace.kind is not DockerResourceKind.WORKSPACE
            or workspace.state is DockerResourceState.ABSENT):
        findings.append("preserved execution workspace/log identity missing")
        preserved_valid = False
    elif workspace.ownership_labels != expected_labels:
        preserved_valid = False
    if any("mismatch" in item or "unexpected" in item for item in findings):
        preserved_valid = False
    return CleanupPostcondition(
        delete_targets_absent, preserved_valid,
        tuple(sorted(set(findings))))


class FormalDockerLifecycleController:
    """Exact-envelope lifecycle only; never production work recovery/retry."""

    def __init__(
        self, *, inspector: DockerStateInspector,
        operations: FormalDockerOperations,
    ) -> None:
        if not callable(getattr(inspector, "inspect", None)):
            raise TypeError("read-only Docker inspector is required")
        required = ("restart_exact", "remove_exact_containers",
                    "remove_exact_network", "remove_exact_volumes",
                    "clean_exact_workspace")
        if any(not callable(getattr(operations, name, None)) for name in required):
            raise TypeError("narrow exact-resource Docker operations are required")
        self._inspector = inspector
        self._operations = operations

    def restart(self, authority: ExecutionDockerAuthority) -> LifecycleOperationResult:
        try:
            pre = self._inspector.inspect(authority.envelope)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            unresolved = commitment({"kind": "RESTART", "state": "UNRESOLVED",
                                     "error_class": type(exc).__name__})
            return LifecycleOperationResult(
                "docker_lifecycle:" + unresolved[7:],
                LifecycleOperationKind.RESTART,
                LifecycleResultState.UNRESOLVED, LifecycleFailure.UNRESOLVED,
                unresolved, None, ("Docker inspection unavailable",))
        ownership = assess_exact_ownership(authority.envelope, pre)
        operation = _operation_identity(authority, LifecycleOperationKind.RESTART, pre)
        if ownership.state is not OperationalState.AVAILABLE:
            return LifecycleOperationResult(
                operation, LifecycleOperationKind.RESTART,
                LifecycleResultState.BLOCKED, ownership.failure,
                pre.inspection_commitment, None, ownership.details)
        # This controller has no injection port.  Existing post-authorization
        # states are intentionally carried through rather than replayed.
        try:
            self._operations.restart_exact(authority.envelope, operation)
        except (OSError, RuntimeError, TypeError, ValueError):
            return LifecycleOperationResult(
                operation, LifecycleOperationKind.RESTART,
                LifecycleResultState.BLOCKED, LifecycleFailure.RESTART_FAILED,
                pre.inspection_commitment, None, ("exact restart operation failed",))
        try:
            post = self._inspector.inspect(authority.envelope)
        except (OSError, RuntimeError, TypeError, ValueError):
            return LifecycleOperationResult(
                operation, LifecycleOperationKind.RESTART,
                LifecycleResultState.BLOCKED, LifecycleFailure.POSTCHECK_FAILED,
                pre.inspection_commitment, None,
                ("post-restart fresh inspection failed",))
        post_ownership = assess_exact_ownership(authority.envelope, post)
        if post_ownership.state is not OperationalState.AVAILABLE:
            return LifecycleOperationResult(
                operation, LifecycleOperationKind.RESTART,
                LifecycleResultState.BLOCKED, LifecycleFailure.RESTART_FAILED,
                pre.inspection_commitment, post.inspection_commitment,
                post_ownership.details)
        return LifecycleOperationResult(
            operation, LifecycleOperationKind.RESTART,
            LifecycleResultState.SUCCEEDED, None, pre.inspection_commitment,
            post.inspection_commitment,
            (SPEC011_RECOVERY_AUTHORITY, authority.injection_state.value,
             authority.event_binding_commitment or "NO_EVENT_BINDING"))

    def cleanup(self, authority: ExecutionDockerAuthority) -> LifecycleOperationResult:
        try:
            pre = self._inspector.inspect(authority.envelope)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            unresolved = commitment({"kind": "CLEANUP", "state": "UNRESOLVED",
                                     "error_class": type(exc).__name__})
            return LifecycleOperationResult(
                "docker_lifecycle:" + unresolved[7:],
                LifecycleOperationKind.CLEANUP,
                LifecycleResultState.UNRESOLVED, LifecycleFailure.UNRESOLVED,
                unresolved, None, ("Docker inspection unavailable",))
        operation = _operation_identity(authority, LifecycleOperationKind.CLEANUP, pre)
        try:
            pre.validate()
        except (AttributeError, TypeError, ValueError):
            return LifecycleOperationResult(
                operation, LifecycleOperationKind.CLEANUP,
                LifecycleResultState.BLOCKED,
                LifecycleFailure.OWNERSHIP_CONFLICT,
                pre.inspection_commitment, None,
                ("inspection integrity/commitment mismatch",))
        if (pre.project_identity != authority.envelope.compose_project_identity
                or pre.topology_commitment
                    != authority.envelope.topology_commitment):
            return LifecycleOperationResult(
                operation, LifecycleOperationKind.CLEANUP,
                LifecycleResultState.BLOCKED,
                LifecycleFailure.OWNERSHIP_CONFLICT,
                pre.inspection_commitment, None,
                ("project/topology ownership mismatch",))
        replay_state = assess_cleanup_postconditions(authority.envelope, pre)
        if replay_state.delete_targets_absent:
            if (not replay_state.preserved_resources_valid
                    or replay_state.findings):
                return LifecycleOperationResult(
                    operation, LifecycleOperationKind.CLEANUP,
                    LifecycleResultState.BLOCKED,
                    LifecycleFailure.POSTCHECK_FAILED,
                    pre.inspection_commitment, pre.inspection_commitment,
                    replay_state.findings or (
                        "preserve-required cleanup state is invalid",))
            return LifecycleOperationResult(
                operation, LifecycleOperationKind.CLEANUP,
                LifecycleResultState.IDEMPOTENT_ABSENT, None,
                pre.inspection_commitment, pre.inspection_commitment,
                ("delete-allowed targets already absent; preserved resources verified",))
        ownership = assess_exact_ownership(authority.envelope, pre)
        if ownership.state is not OperationalState.AVAILABLE:
            return LifecycleOperationResult(
                operation, LifecycleOperationKind.CLEANUP,
                LifecycleResultState.BLOCKED, ownership.failure,
                pre.inspection_commitment, None, ownership.details)
        deletable = tuple(identity for identity, retention
                          in volume_retention(authority.envelope)
                          if retention is ResourceRetention.DELETE_ALLOWED)
        try:
            self._operations.remove_exact_containers(authority.envelope, operation)
            self._operations.remove_exact_network(authority.envelope, operation)
            if deletable:
                self._operations.remove_exact_volumes(
                    authority.envelope, deletable, operation)
        except (OSError, RuntimeError, TypeError, ValueError):
            return LifecycleOperationResult(
                operation, LifecycleOperationKind.CLEANUP,
                LifecycleResultState.BLOCKED, LifecycleFailure.CLEANUP_FAILED,
                pre.inspection_commitment, None,
                ("exact cleanup operation failed",))
        # Workspace/logs contain forensic material and are preserved here.
        try:
            post = self._inspector.inspect(authority.envelope)
        except (OSError, RuntimeError, TypeError, ValueError):
            return LifecycleOperationResult(
                operation, LifecycleOperationKind.CLEANUP,
                LifecycleResultState.BLOCKED, LifecycleFailure.POSTCHECK_FAILED,
                pre.inspection_commitment, None,
                ("post-cleanup fresh inspection failed",))
        postcondition = assess_cleanup_postconditions(authority.envelope, post)
        if (not postcondition.delete_targets_absent
                or not postcondition.preserved_resources_valid
                or postcondition.findings):
            return LifecycleOperationResult(
                operation, LifecycleOperationKind.CLEANUP,
                LifecycleResultState.BLOCKED, LifecycleFailure.POSTCHECK_FAILED,
                pre.inspection_commitment, post.inspection_commitment,
                postcondition.findings or (
                    "cleanup postconditions are incomplete",))
        return LifecycleOperationResult(
            operation, LifecycleOperationKind.CLEANUP,
            LifecycleResultState.SUCCEEDED, None, pre.inspection_commitment,
            post.inspection_commitment,
            ("fresh post-cleanup inspection passed",))


class FormalDockerLifecycleCommand:
    """Supported Candidate-F composition; fresh-resolves ownership per command."""

    def __init__(
        self, *, store: SqliteEvaluationStore, preparation_id: str,
        repository_root: str | Path, inspector: DockerStateInspector,
        operations: FormalDockerOperations,
    ) -> None:
        if type(store) is not SqliteEvaluationStore:
            raise TypeError("Formal Docker lifecycle command requires Candidate-F store")
        self._store = store
        self._preparation_id = preparation_id
        self._repository_root = Path(repository_root).resolve()
        self._controller = FormalDockerLifecycleController(
            inspector=inspector, operations=operations)

    def _resolve(self, execution_identity: str) -> ExecutionDockerAuthority:
        return resolve_execution_docker_authority(
            self._store, self._preparation_id, execution_identity,
            self._repository_root)

    @staticmethod
    def _verify_candidate_f_continuity(
        before: ExecutionDockerAuthority, after: ExecutionDockerAuthority,
    ) -> tuple[str, ...]:
        findings = []
        if (before.execution_identity != after.execution_identity
                or before.evaluation_run_identity != after.evaluation_run_identity
                or before.envelope.planned_execution_identity
                    != after.envelope.planned_execution_identity
                or before.envelope.topology_commitment
                    != after.envelope.topology_commitment):
            findings.append("Candidate-F execution/envelope lineage changed")
        if (before.event_binding_commitment is not None
                and before.event_binding_commitment
                    != after.event_binding_commitment):
            findings.append("authoritative Event binding was not preserved")
        order = tuple(InjectionLifecycleState)
        if order.index(after.injection_state) < order.index(before.injection_state):
            findings.append("injection lifecycle regressed after Docker operation")
        return tuple(findings)

    def restart(self, execution_identity: str) -> LifecycleOperationResult:
        before = self._resolve(execution_identity)
        result = self._controller.restart(before)
        if result.state is not LifecycleResultState.SUCCEEDED:
            return result
        try:
            after = self._resolve(execution_identity)
            findings = self._verify_candidate_f_continuity(before, after)
        except (AttributeError, KeyError, OSError, RuntimeError,
                TypeError, ValueError) as exc:
            findings = (f"Candidate-F post-restart fresh-read failed:{type(exc).__name__}",)
        if findings:
            return LifecycleOperationResult(
                result.operation_identity, result.kind,
                LifecycleResultState.BLOCKED,
                LifecycleFailure.POSTCHECK_FAILED,
                result.pre_inspection_commitment,
                result.post_inspection_commitment, findings)
        return result

    def cleanup(self, execution_identity: str) -> LifecycleOperationResult:
        before = self._resolve(execution_identity)
        result = self._controller.cleanup(before)
        if result.state not in {
                LifecycleResultState.SUCCEEDED,
                LifecycleResultState.IDEMPOTENT_ABSENT}:
            return result
        try:
            after = self._resolve(execution_identity)
            findings = self._verify_candidate_f_continuity(before, after)
        except (AttributeError, KeyError, OSError, RuntimeError,
                TypeError, ValueError) as exc:
            findings = (f"Candidate-F post-cleanup fresh-read failed:{type(exc).__name__}",)
        if findings:
            return LifecycleOperationResult(
                result.operation_identity, result.kind,
                LifecycleResultState.BLOCKED,
                LifecycleFailure.POSTCHECK_FAILED,
                result.pre_inspection_commitment,
                result.post_inspection_commitment, findings)
        return result


def verify_recovery_continuity(
    *, before: RcaPublicState, after: RcaPublicState,
    boundary: RecoveryBoundary, expected_operation_identity: str,
    completed_operation_identity: str | None, signal_injection_count: int,
    publication_reconciled_before_resume: bool,
) -> tuple[bool, tuple[str, ...]]:
    """Delegate duplicate/effect checks to the existing Candidate-F evaluator."""
    evaluation = _evaluate_restart_states(
        before=before, after=after,
        expected_operation_id=expected_operation_identity,
        completed_operation_id=completed_operation_identity,
        boundary=boundary, signal_injection_count=signal_injection_count,
        publication_reconciled_before_resume=
            publication_reconciled_before_resume)
    return evaluation.passed, evaluation.details


def docker_lifecycle_required_facts(
    envelope: FormalComposeEnvelope,
    inspection: DockerProjectInspection | None = None,
) -> tuple[RequiredFact, ...]:
    """Typed additive Final-Admission prerequisites; no caller-completed truth."""
    envelope.validate()
    if inspection is None:
        return (
            RequiredFact("actual_docker_ownership", ReadinessFactState.UNRESOLVED,
                         None, "future authorized Docker inspection",
                         "exact Docker ownership has not been fresh-inspected"),
            RequiredFact("formal_required_service_health",
                         ReadinessFactState.UNRESOLVED, None,
                         "future authorized Docker inspection",
                         "required service operational state is unresolved"),
            RequiredFact("formal_docker_stale_state",
                         ReadinessFactState.UNRESOLVED, None,
                         "future authorized Docker lifecycle inspection",
                         "stale/collision state has not been resolved"),
        )
    ownership = assess_exact_ownership(envelope, inspection)
    health = project_health(envelope, inspection)
    contradictory = ownership.state is OperationalState.CONTRADICTORY
    ownership_state = (ReadinessFactState.CONTRADICTORY if contradictory
                       else ReadinessFactState.RESOLVED
                       if ownership.state is OperationalState.AVAILABLE
                       else ReadinessFactState.UNRESOLVED)
    operational_state = (ReadinessFactState.CONTRADICTORY
                         if health.operational_state is OperationalState.CONTRADICTORY
                         else ReadinessFactState.RESOLVED
                         if health.operational_state is OperationalState.AVAILABLE
                         else ReadinessFactState.UNRESOLVED)
    return (
        RequiredFact("actual_docker_ownership", ownership_state,
                     {"inspection_commitment": inspection.inspection_commitment}
                     if ownership_state is ReadinessFactState.RESOLVED else None,
                     "read-only Docker state inspector",
                     "exact project ownership fresh-inspected"
                     if ownership_state is ReadinessFactState.RESOLVED
                     else ";".join(ownership.details)),
        RequiredFact("formal_required_service_health", operational_state,
                     {name: state.value for name, state in health.required_services}
                     if operational_state is ReadinessFactState.RESOLVED else None,
                     "read-only Docker state inspector", health.reason),
        RequiredFact("formal_docker_stale_state", ownership_state,
                     "NO_FOREIGN_OR_STALE_RESOURCE"
                     if ownership_state is ReadinessFactState.RESOLVED else None,
                     "read-only Docker state inspector",
                     "no foreign collision or stale partial project"
                     if ownership_state is ReadinessFactState.RESOLVED
                     else ";".join(ownership.details)),
    )


def resolve_campaign_docker_lifecycle_required_facts(
    topology: FormalComposeCampaignTopology,
    inspector: DockerStateInspector | None,
) -> tuple[RequiredFact, ...]:
    """Fresh-inspect all 30 envelopes through a service, never caller facts."""
    topology.validate()
    aggregate_names = (
        "actual_docker_ownership", "formal_required_service_health",
        "formal_docker_stale_state")

    def unresolved_campaign_facts(
        state: ReadinessFactState, reason: str, authority: str,
    ) -> tuple[RequiredFact, ...]:
        return tuple(RequiredFact(name, state, None, authority, reason)
                     for name in (*aggregate_names,
                                  "actual_formal_docker_resources"))

    if inspector is None:
        return unresolved_campaign_facts(
            ReadinessFactState.UNRESOLVED,
            "actual Docker resources have not been fresh-inspected",
            "future authorized Docker inspection")
    if not callable(getattr(inspector, "inspect", None)):
        raise TypeError("typed read-only Docker state inspector is required")
    per_execution: list[tuple[str, tuple[RequiredFact, ...]]] = []
    try:
        for envelope in topology.envelopes:
            inspected = inspector.inspect(envelope)
            if type(inspected) is not DockerProjectInspection:
                raise TypeError("Docker inspector returned an untyped result")
            per_execution.append((
                envelope.planned_execution_identity,
                docker_lifecycle_required_facts(envelope, inspected)))
    except (OSError, RuntimeError) as exc:
        return unresolved_campaign_facts(
            ReadinessFactState.UNRESOLVED,
            f"current Docker inspection unavailable:{type(exc).__name__}",
            "read-only Docker state inspector")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return unresolved_campaign_facts(
            ReadinessFactState.CONTRADICTORY,
            f"current Docker inspection contradictory:{type(exc).__name__}",
            "read-only Docker state inspector")
    result = []
    for index, name in enumerate(aggregate_names):
        values = tuple((execution, facts[index])
                       for execution, facts in per_execution)
        states = {fact.state for _, fact in values}
        state = (ReadinessFactState.CONTRADICTORY
                 if ReadinessFactState.CONTRADICTORY in states
                 else ReadinessFactState.RESOLVED
                 if states == {ReadinessFactState.RESOLVED}
                 else ReadinessFactState.UNRESOLVED)
        material = ([
            {"planned_execution_identity": execution,
             "value": fact.value}
            for execution, fact in values]
            if state is ReadinessFactState.RESOLVED else None)
        result.append(RequiredFact(
            name, state, material, "read-only Docker state inspector",
            "all 30 execution envelopes fresh-inspected and coherent"
            if state is ReadinessFactState.RESOLVED
            else "one or more execution Docker facts are unresolved/contradictory"))
    aggregate_states = {item.state for item in result}
    legacy_state = (
        ReadinessFactState.CONTRADICTORY
        if ReadinessFactState.CONTRADICTORY in aggregate_states
        else ReadinessFactState.RESOLVED
        if aggregate_states == {ReadinessFactState.RESOLVED}
        else ReadinessFactState.UNRESOLVED)
    legacy_value = None
    if legacy_state is ReadinessFactState.RESOLVED:
        legacy_value = {
            "topology_commitment": topology.topology_commitment,
            "execution_envelope_count": len(topology.envelopes),
            "inspection_results_commitment": commitment({
                item.name: item.value for item in result}),
        }
    result.append(RequiredFact(
        "actual_formal_docker_resources", legacy_state, legacy_value,
        "read-only Docker state inspector",
        "derived from the same 30-envelope ownership, health, and stale-state inspection"
        if legacy_state is ReadinessFactState.RESOLVED
        else "30-envelope Docker resource inspection is unresolved/contradictory"))
    return tuple(result)


__all__ = [
    "CONTROL_ARTIFACT_PATH", "DockerProjectInspection", "DockerResourceKind",
    "DockerResourceState", "ExecutionDockerAuthority",
    "FormalDockerLifecycleCommand", "FormalDockerLifecycleController",
    "FormalHealthProjection",
    "InspectedDockerResource", "InterruptionContract", "LifecycleFailure",
    "LifecycleOperationKind", "LifecycleOperationResult", "LifecycleResultState",
    "OperationalState", "ResourceRetention", "SPEC011_RECOVERY_AUTHORITY",
    "approved_interruption_matrix", "assess_exact_ownership",
    "assess_cleanup_postconditions",
    "docker_lifecycle_required_facts", "expected_ownership_labels",
    "project_health", "resolve_execution_docker_authority",
    "resolve_campaign_docker_lifecycle_required_facts",
    "verify_recovery_continuity", "volume_retention",
]
