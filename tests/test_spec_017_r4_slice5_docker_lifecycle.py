from dataclasses import replace
import json
from pathlib import Path

import pytest

from rca_evaluation.formal_compose import (
    FORMAL_COMPOSE_SCHEMA, FormalComposeEnvelope, FormalVolumeMount,
)
from rca_evaluation.formal_docker_lifecycle import (
    DockerProjectInspection, DockerResourceKind, DockerResourceState,
    ExecutionDockerAuthority, FormalDockerLifecycleCommand,
    FormalDockerLifecycleController,
    InspectedDockerResource, LifecycleFailure, LifecycleOperationKind,
    LifecycleResultState, OperationalState, ResourceRetention,
    SPEC011_RECOVERY_AUTHORITY, approved_interruption_matrix,
    assess_exact_ownership, docker_lifecycle_required_facts,
    expected_ownership_labels, project_health, verify_recovery_continuity,
    resolve_execution_docker_authority, volume_retention,
    resolve_campaign_docker_lifecycle_required_facts,
)
from rca_evaluation.formal_scenario_io import InjectionLifecycleState
from rca_evaluation.identity import commitment
from rca_evaluation.readiness import (
    ReadinessFactState, resolve_formal_required_facts,
)
from rca_evaluation.recovery_evaluation import (
    Freshness, RecoveryBoundary, RcaPublicState,
)
from rca_evaluation.runtime_image_readiness import (
    RUNTIME_IMAGE_PROFILE, RUNTIME_IMAGE_TAG, RuntimeFactState,
    RuntimeReadinessFact,
)
from tests.test_spec_017_r4_slice4_scenario_observation import (
    _admit_execution, _prepared_registry,
)
from tests.test_spec_017_r4_slice2_formal_compose import _topology


ROOT = Path(__file__).resolve().parents[1]


def _envelope(suffix: str = "a1b2c3d4e5f6") -> FormalComposeEnvelope:
    project = f"spec017-formal-{suffix}"
    volumes = tuple(FormalVolumeMount(role, f"{project}_{role}", target)
                    for role, target in (
                        ("formal-logs", "/app/var/rca_evaluation/logs"),
                        ("loki-data", "/loki"),
                        ("prometheus-data", "/prometheus"),
                        ("runtime-events", "/app/events"),
                        ("runtime-knowledge", "/app/var/rca_evaluation/knowledge-work"),
                        ("runtime-state", "/app/var/runtime_orchestration"),
                    ))
    values = dict(
        schema=FORMAL_COMPOSE_SCHEMA,
        preparation_id="eval:preparation:" + "1" * 64,
        planned_run_identity="eval:planned_run:" + "2" * 64,
        planned_execution_identity="eval:planned_execution:" + "3" * 64,
        scenario_id="S1", repetition=1,
        namespace_identity="eval:docker_namespace:" + "4" * 64,
        compose_project_identity=project,
        container_name_prefixes=tuple(f"{project}-{name}-"
                                      for name in ("loki", "prometheus", "runtime")),
        network_identity=f"{project}_formal", volume_mounts=volumes,
        state_volume_ownership=tuple(sorted({
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
        }.items())),
        workspace_identity=f"docker/{project}",
        log_identity=f"docker/{project}/logs",
        services=("loki", "prometheus", "runtime"),
        service_classification=(
            ("grafana", "NOT_REQUIRED"), ("loki", "FORMAL_REQUIRED"),
            ("prometheus", "FORMAL_REQUIRED"),
            ("promtail", "OPTIONAL_DIAGNOSTIC"),
            ("runtime", "FORMAL_REQUIRED"),
        ),
        runtime_image_profile=RUNTIME_IMAGE_PROFILE,
        runtime_image_tag=RUNTIME_IMAGE_TAG,
        compose_artifact_commitment="sha256:" + "5" * 64,
        base_compose_commitment="sha256:" + "6" * 64,
        prometheus_config_commitment="sha256:" + "7" * 64,
        runtime_config_commitment="sha256:" + "8" * 64,
        host_port_policy="NONE", shared_resource_guard="PASS",
        real_rag_mount_slot="/app/var/knowledge_index",
        actual_resources=RuntimeReadinessFact(
            "actual_formal_docker_resources", RuntimeFactState.UNRESOLVED,
            None, "not executed"),
    )
    provisional = FormalComposeEnvelope(
        **values, topology_commitment="sha256:" + "0" * 64)
    result = replace(provisional, topology_commitment=commitment(
        provisional.material(include_commitment=False)))
    result.validate()
    return result


def _resources(
    envelope: FormalComposeEnvelope, *,
    required_state: DockerResourceState = DockerResourceState.RUNNING,
    labels: tuple[tuple[str, str], ...] | None = None,
) -> tuple[InspectedDockerResource, ...]:
    labels = labels if labels is not None else expected_ownership_labels(envelope)
    runtime_mounts = tuple((item.volume_identity, item.target)
                           for item in envelope.volume_mounts
                           if item.role in {"formal-logs", "runtime-events",
                                           "runtime-knowledge", "runtime-state"})
    containers = tuple(InspectedDockerResource(
        DockerResourceKind.CONTAINER, f"{envelope.compose_project_identity}-{service}-1",
        required_state, labels, service=service,
        image_profile=RUNTIME_IMAGE_PROFILE,
        image_identity="sha256:" + "9" * 64,
        mounts=runtime_mounts if service == "runtime" else (),
        health=OperationalState.AVAILABLE,
    ) for service in envelope.services)
    return containers + (
        InspectedDockerResource(
            DockerResourceKind.NETWORK, envelope.network_identity,
            DockerResourceState.PRESENT, labels),
    ) + tuple(InspectedDockerResource(
        DockerResourceKind.VOLUME, item.volume_identity,
        DockerResourceState.PRESENT, labels) for item in envelope.volume_mounts) + (
        InspectedDockerResource(
            DockerResourceKind.WORKSPACE, envelope.workspace_identity,
            DockerResourceState.PRESENT, labels),
    )


def _inspection(envelope: FormalComposeEnvelope, resources=None, stamp="t1"):
    return DockerProjectInspection.create(
        envelope, _resources(envelope) if resources is None else tuple(resources),
        inspected_at=stamp)


def test_docker_observation_time_is_not_resource_semantic_identity():
    envelope = _envelope()
    first = _inspection(envelope, stamp="2026-10-19T00:00:00Z")
    second = _inspection(envelope, stamp="2026-10-19T00:01:00Z")
    assert first.resource_state_commitment == second.resource_state_commitment
    assert (first.actual_resource_set_commitment ==
            second.actual_resource_set_commitment)
    assert first.observation_commitment != second.observation_commitment
    assert first.inspection_commitment == first.observation_commitment


@pytest.mark.parametrize("drift", [
    "container", "network", "volume", "ownership", "health",
])
def test_docker_resource_semantic_identity_fails_closed_on_actual_drift(drift):
    envelope = _envelope()
    resources = list(_resources(envelope))
    if drift == "container":
        index = next(i for i, item in enumerate(resources)
                     if item.kind is DockerResourceKind.CONTAINER)
        resources[index] = replace(resources[index], identity="foreign-container")
    elif drift == "network":
        index = next(i for i, item in enumerate(resources)
                     if item.kind is DockerResourceKind.NETWORK)
        resources[index] = replace(resources[index], identity="foreign-network")
    elif drift == "volume":
        index = next(i for i, item in enumerate(resources)
                     if item.kind is DockerResourceKind.VOLUME)
        resources[index] = replace(resources[index], identity="foreign-volume")
    elif drift == "ownership":
        resources[0] = replace(
            resources[0], ownership_labels=(("foreign", "owner"),))
    else:
        resources[0] = replace(
            resources[0], health=OperationalState.UNHEALTHY)
    baseline = _inspection(envelope)
    changed = _inspection(envelope, resources, stamp="t2")
    assert changed.actual_resource_set_commitment != (
        baseline.actual_resource_set_commitment)
    assert changed.resource_state_commitment != baseline.resource_state_commitment


def test_docker_topology_identity_is_part_of_resource_semantics():
    envelope = _envelope()
    drifted = _drifted_envelope(envelope)
    assert (_inspection(envelope).resource_state_commitment !=
            _inspection(drifted).resource_state_commitment)


def _authority(envelope: FormalComposeEnvelope) -> ExecutionDockerAuthority:
    return ExecutionDockerAuthority(
        "eval:execution:" + "a" * 64, "eval:evaluation_run:" + "b" * 64,
        envelope, InjectionLifecycleState.SIGNAL_SENT,
        "sha256:" + "c" * 64, "sha256:" + "d" * 64)


class _Inspector:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = []

    def inspect(self, envelope):
        self.calls.append(envelope.compose_project_identity)
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class _Operations:
    def __init__(self):
        self.calls = []

    def restart_exact(self, envelope, operation_identity):
        self.calls.append(("restart", envelope.compose_project_identity,
                           operation_identity))

    def remove_exact_containers(self, envelope, operation_identity):
        self.calls.append(("containers", envelope.compose_project_identity,
                           operation_identity))

    def remove_exact_network(self, envelope, operation_identity):
        self.calls.append(("network", envelope.network_identity,
                           operation_identity))

    def remove_exact_volumes(self, envelope, volume_identities, operation_identity):
        self.calls.append(("volumes", volume_identities, operation_identity))

    def clean_exact_workspace(self, envelope, operation_identity):
        self.calls.append(("workspace", envelope.workspace_identity,
                           operation_identity))


def _post_cleanup(envelope):
    labels = expected_ownership_labels(envelope)
    retention = dict(volume_retention(envelope))
    values = []
    for item in _resources(envelope):
        state = item.state
        if item.kind in {DockerResourceKind.CONTAINER, DockerResourceKind.NETWORK}:
            state = DockerResourceState.ABSENT
        if (item.kind is DockerResourceKind.VOLUME
                and retention[item.identity] is ResourceRetention.DELETE_ALLOWED):
            state = DockerResourceState.ABSENT
        values.append(replace(item, state=state, ownership_labels=labels))
    return _inspection(envelope, values, "t2")


def _drifted_envelope(envelope):
    provisional = replace(
        envelope, runtime_config_commitment="sha256:" + "f" * 64,
        topology_commitment="sha256:" + "0" * 64)
    return replace(provisional, topology_commitment=commitment(
        provisional.material(include_commitment=False)))


def test_planned_is_not_actual_and_uninspected_required_facts_fail_closed():
    envelope = _envelope()
    assert envelope.actual_resources.state is RuntimeFactState.UNRESOLVED
    health = project_health(envelope, None)
    assert health.operational_state is OperationalState.UNRESOLVED
    assert health.formal_readiness is ReadinessFactState.UNRESOLVED
    facts = docker_lifecycle_required_facts(envelope)
    assert {item.name for item in facts} == {
        "actual_docker_ownership", "formal_required_service_health",
        "formal_docker_stale_state"}
    assert all(item.state is ReadinessFactState.UNRESOLVED for item in facts)


def test_required_fact_composition_fresh_inspects_all_thirty_envelopes(tmp_path):
    prepared, _, store, topology = _topology(tmp_path)

    class Inspector:
        def __init__(self):
            self.calls = []

        def inspect(self, envelope):
            self.calls.append(envelope.planned_execution_identity)
            return _inspection(envelope, stamp=f"inspect-{len(self.calls)}")

    inspector = Inspector()
    try:
        facts = resolve_campaign_docker_lifecycle_required_facts(
            topology, inspector)
        assert len(inspector.calls) == 30
        assert len(set(inspector.calls)) == 30
        assert {item.name for item in facts} == {
            "actual_docker_ownership", "formal_required_service_health",
            "formal_docker_stale_state", "actual_formal_docker_resources"}
        assert all(item.state is ReadinessFactState.RESOLVED for item in facts)
        detailed = tuple(item for item in facts
                         if item.name != "actual_formal_docker_resources")
        assert all(len(item.value) == 30 for item in detailed)
        legacy = next(item for item in facts
                      if item.name == "actual_formal_docker_resources")
        assert legacy.value["execution_envelope_count"] == 30

        template = json.loads((
            ROOT / "configs/rca_evaluation/formal_preregistration_v1.json"
        ).read_text(encoding="utf-8"))
        integrated = resolve_formal_required_facts(
            template, repository_root=ROOT, evaluation_store=store,
            preparation_id=prepared.preparation_id,
            docker_state_inspector=Inspector())
        lifecycle = {item.name: item for item in integrated.prerequisites
                     if item.name in {
                         "actual_docker_ownership",
                         "formal_required_service_health",
                         "formal_docker_stale_state",
                         "actual_formal_docker_resources"}}
        assert set(lifecycle) == {
            "actual_docker_ownership", "formal_required_service_health",
            "formal_docker_stale_state", "actual_formal_docker_resources"}
        assert all(item.state is ReadinessFactState.RESOLVED
                   for item in lifecycle.values())
        with pytest.raises(ValueError, match="unresolved required fact"):
            integrated.assert_resolved()
    finally:
        store.close()


def test_campaign_docker_semantics_are_stable_across_fresh_timestamps(tmp_path):
    _, _, store, topology = _topology(tmp_path)

    class Inspector:
        def __init__(self, prefix):
            self.prefix = prefix

        def inspect(self, envelope):
            return _inspection(
                envelope,
                stamp=f"{self.prefix}-{envelope.planned_execution_identity}")

    try:
        first = resolve_campaign_docker_lifecycle_required_facts(
            topology, Inspector("t1"))
        second = resolve_campaign_docker_lifecycle_required_facts(
            topology, Inspector("t2"))
        assert [(item.name, item.value) for item in first] == [
            (item.name, item.value) for item in second]
        resources = next(item for item in first
                         if item.name == "actual_formal_docker_resources")
        assert resources.value["actual_resource_set_commitment"].startswith(
            "sha256:")
    finally:
        store.close()


def test_docker_resource_aggregate_without_inspector_is_unresolved(tmp_path):
    _, _, store, topology = _topology(tmp_path)
    try:
        facts = resolve_campaign_docker_lifecycle_required_facts(topology, None)
        assert {item.name for item in facts} == {
            "actual_docker_ownership", "formal_required_service_health",
            "formal_docker_stale_state", "actual_formal_docker_resources"}
        assert all(item.state is ReadinessFactState.UNRESOLVED for item in facts)
    finally:
        store.close()


@pytest.mark.parametrize("mode,expected", (
    ("missing", ReadinessFactState.UNRESOLVED),
    ("foreign", ReadinessFactState.CONTRADICTORY),
    ("topology", ReadinessFactState.CONTRADICTORY),
    ("wrong_volume", ReadinessFactState.CONTRADICTORY),
    ("wrong_network", ReadinessFactState.CONTRADICTORY),
    ("wrong_mount", ReadinessFactState.CONTRADICTORY),
    ("unhealthy", ReadinessFactState.UNRESOLVED),
))
def test_one_bad_envelope_prevents_aggregate_docker_resolution(
    tmp_path, mode, expected,
):
    _, _, store, topology = _topology(tmp_path)

    class Inspector:
        def __init__(self):
            self.index = 0

        def inspect(self, envelope):
            self.index += 1
            if self.index != 30:
                return _inspection(envelope, stamp=f"ok-{self.index}")
            if mode == "missing":
                return _inspection(envelope, (), "missing")
            if mode == "foreign":
                return _inspection(topology.envelopes[0], stamp="foreign")
            if mode == "topology":
                return _inspection(_drifted_envelope(envelope), stamp="drift")
            resources = _resources(envelope)
            if mode == "wrong_volume":
                resources = tuple(replace(item, identity="foreign-volume")
                                  if item.kind is DockerResourceKind.VOLUME
                                  and item.state is not DockerResourceState.ABSENT
                                  else item for item in resources)
            elif mode == "wrong_network":
                resources = tuple(replace(item, identity="foreign-network")
                                  if item.kind is DockerResourceKind.NETWORK
                                  else item for item in resources)
            elif mode == "wrong_mount":
                resources = tuple(replace(item, mounts=())
                                  if item.service == "runtime" else item
                                  for item in resources)
            elif mode == "unhealthy":
                resources = tuple(replace(
                    item, state=DockerResourceState.FAILED,
                    health=OperationalState.UNHEALTHY)
                    if item.service == "runtime" else item
                    for item in resources)
            return _inspection(envelope, resources, mode)

    try:
        facts = resolve_campaign_docker_lifecycle_required_facts(
            topology, Inspector())
        legacy = next(item for item in facts
                      if item.name == "actual_formal_docker_resources")
        assert legacy.state is expected
        assert legacy.value is None
    finally:
        store.close()


def test_caller_completed_resource_mapping_is_not_an_inspector_authority(tmp_path):
    _, _, store, topology = _topology(tmp_path)
    try:
        with pytest.raises(TypeError, match="inspector"):
            resolve_campaign_docker_lifecycle_required_facts(
                topology, {"actual_formal_docker_resources": "RESOLVED"})
    finally:
        store.close()


def test_exact_ownership_accepts_only_expected_project_network_volumes_and_labels():
    envelope = _envelope()
    assert assess_exact_ownership(
        envelope, _inspection(envelope)).state is OperationalState.AVAILABLE
    foreign = _inspection(envelope, (
        replace(_resources(envelope)[0], identity="foreign-runtime-1"),
        *_resources(envelope)[1:]))
    result = assess_exact_ownership(envelope, foreign)
    assert result.failure is LifecycleFailure.OWNERSHIP_CONFLICT
    wrong_network = tuple(replace(item, identity="prod_default")
                          if item.kind is DockerResourceKind.NETWORK else item
                          for item in _resources(envelope))
    assert assess_exact_ownership(
        envelope, _inspection(envelope, wrong_network)).state is OperationalState.CONTRADICTORY
    wrong_volume = tuple(replace(item, identity="runtime-state")
                         if item.kind is DockerResourceKind.VOLUME else item
                         for item in _resources(envelope))
    assert assess_exact_ownership(
        envelope, _inspection(envelope, wrong_volume)).state is OperationalState.CONTRADICTORY
    bad_labels = tuple(sorted({
        **dict(expected_ownership_labels(envelope)),
        "spec017.preparation_id": "eval:preparation:" + "f" * 64,
    }.items()))
    assert assess_exact_ownership(
        envelope, _inspection(envelope, _resources(envelope, labels=bad_labels))
    ).failure is LifecycleFailure.OWNERSHIP_CONFLICT


def test_foreign_project_and_arbitrary_caller_name_cannot_be_controller_target():
    envelope = _envelope()
    inspection = replace(_inspection(envelope), project_identity="production")
    controller = FormalDockerLifecycleController(
        inspector=_Inspector(inspection), operations=_Operations())
    result = controller.restart(_authority(envelope))
    assert result.state is LifecycleResultState.BLOCKED
    assert result.failure is LifecycleFailure.OWNERSHIP_CONFLICT


def test_health_is_operational_only_and_optional_services_do_not_block():
    envelope = _envelope()
    healthy = project_health(envelope, _inspection(envelope))
    assert healthy.operational_state is OperationalState.AVAILABLE
    assert healthy.formal_readiness is ReadinessFactState.UNRESOLVED
    assert healthy.optional_services == (("promtail", OperationalState.UNAVAILABLE),)
    assert healthy.ignored_services == ("grafana",)
    stopped = _resources(envelope)
    stopped = tuple(replace(item, state=DockerResourceState.STOPPED)
                    if item.service == "loki" else item for item in stopped)
    assert project_health(
        envelope, _inspection(envelope, stopped)).operational_state is OperationalState.UNAVAILABLE
    unknown_health = tuple(replace(item, health=OperationalState.UNRESOLVED)
                           if item.service == "runtime" else item
                           for item in _resources(envelope))
    running_unknown = project_health(
        envelope, _inspection(envelope, unknown_health))
    assert running_unknown.operational_state is OperationalState.UNRESOLVED
    assert running_unknown.formal_readiness is ReadinessFactState.UNRESOLVED


def test_restart_preserves_identity_volumes_and_delegates_recovery_to_spec011():
    envelope = _envelope()
    before = _inspection(envelope, stamp="before")
    after = _inspection(envelope, stamp="after")
    inspector, operations = _Inspector(before, after), _Operations()
    result = FormalDockerLifecycleController(
        inspector=inspector, operations=operations).restart(_authority(envelope))
    assert result.state is LifecycleResultState.SUCCEEDED
    assert result.kind is LifecycleOperationKind.RESTART
    assert result.details == (
        SPEC011_RECOVERY_AUTHORITY, "SIGNAL_SENT", "sha256:" + "c" * 64)
    assert operations.calls[0][0:2] == (
        "restart", envelope.compose_project_identity)
    assert inspector.calls == [envelope.compose_project_identity] * 2
    assert tuple(item.volume_identity for item in envelope.volume_mounts) == tuple(
        item.volume_identity for item in _envelope().volume_mounts)
    assert not any(call[0] in {"inject", "retry", "recover"}
                   for call in operations.calls)


def test_restart_operation_replay_is_deterministic_and_does_not_allocate():
    envelope = _envelope()
    operations1, operations2 = _Operations(), _Operations()
    first = FormalDockerLifecycleController(
        inspector=_Inspector(_inspection(envelope)),
        operations=operations1).restart(_authority(envelope))
    second = FormalDockerLifecycleController(
        inspector=_Inspector(_inspection(envelope)),
        operations=operations2).restart(_authority(envelope))
    assert first.operation_identity == second.operation_identity
    assert first.state is second.state is LifecycleResultState.SUCCEEDED
    assert _authority(envelope).envelope.planned_execution_identity == (
        _authority(envelope).envelope.planned_execution_identity)


def test_equivalent_operation_identity_ignores_fresh_inspection_timestamp():
    envelope = _envelope()
    first = FormalDockerLifecycleController(
        inspector=_Inspector(_inspection(envelope, stamp="first")),
        operations=_Operations()).restart(_authority(envelope))
    second = FormalDockerLifecycleController(
        inspector=_Inspector(_inspection(envelope, stamp="later")),
        operations=_Operations()).restart(_authority(envelope))
    assert first.operation_identity == second.operation_identity


def test_supported_command_fresh_resolves_candidate_f_execution_and_project(tmp_path):
    report, prepared, store, _ = _prepared_registry(tmp_path)
    execution = report.campaign.executions[0]
    try:
        _admit_execution(store, report, execution)
        authority = resolve_execution_docker_authority(
            store, prepared.preparation_id, execution.execution_id, ROOT)
        inspection = _inspection(authority.envelope)
        operations = _Operations()
        command = FormalDockerLifecycleCommand(
            store=store, preparation_id=prepared.preparation_id,
            repository_root=ROOT, inspector=_Inspector(inspection),
            operations=operations)
        result = command.restart(execution.execution_id)
        assert result.state is LifecycleResultState.SUCCEEDED
        assert operations.calls[0][1] == authority.envelope.compose_project_identity
        with pytest.raises(ValueError, match="execution"):
            command.restart("eval:execution:" + "f" * 64)
    finally:
        store.close()


def test_restart_rejects_wrong_image_mount_and_partial_project():
    envelope = _envelope()
    resources = _resources(envelope)
    wrong_image = tuple(replace(item, image_profile="other")
                        if item.service == "runtime" else item for item in resources)
    partial = tuple(item for item in resources if item.service != "loki")
    for value in (wrong_image, partial):
        operations = _Operations()
        result = FormalDockerLifecycleController(
            inspector=_Inspector(_inspection(envelope, value)),
            operations=operations).restart(_authority(envelope))
        assert result.state is LifecycleResultState.BLOCKED
        assert not operations.calls


def test_cleanup_targets_only_exact_owned_deletable_resources_then_postchecks():
    envelope = _envelope()
    operations = _Operations()
    result = FormalDockerLifecycleController(
        inspector=_Inspector(_inspection(envelope), _post_cleanup(envelope)),
        operations=operations).cleanup(_authority(envelope))
    assert result.state is LifecycleResultState.SUCCEEDED
    assert [item[0] for item in operations.calls] == [
        "containers", "network", "volumes"]
    deleted = operations.calls[-1][1]
    assert deleted == tuple(item.volume_identity for item in envelope.volume_mounts
                            if item.role in {"loki-data", "prometheus-data"})
    assert not any(item[0] in {"prune", "system_prune", "volume_prune",
                               "network_prune"} for item in operations.calls)
    assert not any("rca_evaluation.db" in repr(item) for item in operations.calls)


def test_cleanup_foreign_or_ambiguous_ownership_is_rejected_before_mutation():
    envelope = _envelope()
    bad_labels = tuple(sorted({
        **dict(expected_ownership_labels(envelope)),
        "com.docker.compose.project": "foreign",
    }.items()))
    operations = _Operations()
    result = FormalDockerLifecycleController(
        inspector=_Inspector(_inspection(
            envelope, _resources(envelope, labels=bad_labels))),
        operations=operations).cleanup(_authority(envelope))
    assert result.failure is LifecycleFailure.OWNERSHIP_CONFLICT
    assert not operations.calls


def test_cleanup_absent_is_idempotent_and_never_guesses_another_resource():
    envelope = _envelope()
    inspection = _post_cleanup(envelope)
    operations = _Operations()
    controller = FormalDockerLifecycleController(
        inspector=_Inspector(inspection), operations=operations)
    first = controller.cleanup(_authority(envelope))
    second = controller.cleanup(_authority(envelope))
    assert first.state is second.state is LifecycleResultState.IDEMPOTENT_ABSENT
    assert first.operation_identity == second.operation_identity
    assert not operations.calls


def test_cleanup_all_absent_is_not_idempotent_success():
    envelope = _envelope()
    absent = tuple(replace(item, state=DockerResourceState.ABSENT)
                   for item in _resources(envelope))
    result = FormalDockerLifecycleController(
        inspector=_Inspector(_inspection(envelope, absent)),
        operations=_Operations()).cleanup(_authority(envelope))
    assert result.state is LifecycleResultState.BLOCKED
    assert result.failure is LifecycleFailure.POSTCHECK_FAILED
    assert any("preserved" in item for item in result.details)


@pytest.mark.parametrize("role", (
    "runtime-events", "runtime-state", "runtime-knowledge", "formal-logs",
))
def test_cleanup_idempotent_replay_rejects_missing_preserved_volume(role):
    envelope = _envelope()
    target = next(item.volume_identity for item in envelope.volume_mounts
                  if item.role == role)
    resources = tuple(replace(item, state=DockerResourceState.ABSENT)
                      if item.identity == target else item
                      for item in _post_cleanup(envelope).resources)
    result = FormalDockerLifecycleController(
        inspector=_Inspector(_inspection(envelope, resources, "replay")),
        operations=_Operations()).cleanup(_authority(envelope))
    assert result.state is LifecycleResultState.BLOCKED
    assert result.failure is LifecycleFailure.POSTCHECK_FAILED


def test_cleanup_idempotent_replay_rejects_missing_workspace():
    envelope = _envelope()
    resources = tuple(replace(item, state=DockerResourceState.ABSENT)
                      if item.kind is DockerResourceKind.WORKSPACE else item
                      for item in _post_cleanup(envelope).resources)
    result = FormalDockerLifecycleController(
        inspector=_Inspector(_inspection(envelope, resources, "replay")),
        operations=_Operations()).cleanup(_authority(envelope))
    assert result.state is LifecycleResultState.BLOCKED
    assert result.failure is LifecycleFailure.POSTCHECK_FAILED


@pytest.mark.parametrize("kind", (DockerResourceKind.VOLUME,
                                   DockerResourceKind.WORKSPACE))
def test_cleanup_postcheck_rejects_empty_ownership_on_preserved_resource(kind):
    envelope = _envelope()
    post = _post_cleanup(envelope)
    resources = []
    changed = False
    preserved = dict(volume_retention(envelope))
    for item in post.resources:
        should_change = (
            kind is DockerResourceKind.WORKSPACE
            and item.kind is DockerResourceKind.WORKSPACE
            or kind is DockerResourceKind.VOLUME
            and item.kind is DockerResourceKind.VOLUME
            and preserved[item.identity] is ResourceRetention.PRESERVE_REQUIRED)
        if should_change and not changed:
            item = replace(item, ownership_labels=())
            changed = True
        resources.append(item)
    result = FormalDockerLifecycleController(
        inspector=_Inspector(_inspection(envelope),
                             _inspection(envelope, resources, "post")),
        operations=_Operations()).cleanup(_authority(envelope))
    assert result.state is LifecycleResultState.BLOCKED
    assert result.failure is LifecycleFailure.POSTCHECK_FAILED


def test_cleanup_postcheck_rejects_foreign_project_and_topology():
    envelope = _envelope()
    foreign_project = _post_cleanup(_envelope("f1e2d3c4b5a6"))
    drifted_topology = _post_cleanup(_drifted_envelope(envelope))
    for post in (foreign_project, drifted_topology):
        result = FormalDockerLifecycleController(
            inspector=_Inspector(_inspection(envelope), post),
            operations=_Operations()).cleanup(_authority(envelope))
        assert result.state is LifecycleResultState.BLOCKED
        assert result.failure is LifecycleFailure.POSTCHECK_FAILED


def test_cleanup_postcheck_rejects_preserved_foreign_ownership_drift():
    envelope = _envelope()
    post = _post_cleanup(envelope)
    foreign_labels = tuple(sorted({
        **dict(expected_ownership_labels(envelope)),
        "spec017.preparation_id": "eval:preparation:" + "e" * 64,
    }.items()))
    resources = tuple(replace(item, ownership_labels=foreign_labels)
                      if (item.kind is DockerResourceKind.WORKSPACE
                          or item.kind is DockerResourceKind.VOLUME
                          and item.state is not DockerResourceState.ABSENT)
                      else item for item in post.resources)
    result = FormalDockerLifecycleController(
        inspector=_Inspector(_inspection(envelope),
                             _inspection(envelope, resources, "post")),
        operations=_Operations()).cleanup(_authority(envelope))
    assert result.state is LifecycleResultState.BLOCKED
    assert result.failure is LifecycleFailure.POSTCHECK_FAILED


def test_cleanup_command_success_without_fresh_postcheck_is_not_pass():
    envelope = _envelope()
    same = _inspection(envelope)
    operations = _Operations()
    result = FormalDockerLifecycleController(
        inspector=_Inspector(same, same), operations=operations
    ).cleanup(_authority(envelope))
    assert result.state is LifecycleResultState.BLOCKED
    assert result.failure is LifecycleFailure.POSTCHECK_FAILED


def test_cleanup_preserves_forensic_state_and_candidate_f_evidence_boundary():
    envelope = _envelope()
    retention = dict(volume_retention(envelope))
    for item in envelope.volume_mounts:
        assert retention[item.volume_identity] is (
            ResourceRetention.DELETE_ALLOWED
            if item.role in {"loki-data", "prometheus-data"}
            else ResourceRetention.PRESERVE_REQUIRED)
    broken = list(_post_cleanup(envelope).resources)
    state_volume = next(item.volume_identity for item in envelope.volume_mounts
                        if item.role == "runtime-state")
    broken = [replace(item, state=DockerResourceState.ABSENT)
              if item.identity == state_volume else item for item in broken]
    result = FormalDockerLifecycleController(
        inspector=_Inspector(_inspection(envelope),
                             _inspection(envelope, broken, "post")),
        operations=_Operations()).cleanup(_authority(envelope))
    assert result.failure is LifecycleFailure.POSTCHECK_FAILED
    assert any("preserved evidence missing" in item for item in result.details)


def test_approved_exact_five_interruption_points_are_loaded_not_invented():
    matrix = approved_interruption_matrix(ROOT)
    assert tuple(item.boundary for item in matrix) == tuple(RecoveryBoundary)
    assert tuple(item.fixture_identity for item in matrix) == (
        "spec017:S5:restart-evidence:v2",
        "spec017:S5:restart-knowledge:v2",
        "spec017:S5:restart-generation:v2",
        "spec017:S5:restart-artifact-commit:v2",
        "spec017:S5:restart-publication:v2",
    )
    assert all(item.preconditions and item.post_restart_checks
               and item.duplicate_prohibitions for item in matrix)


def _state(*, tries=(), versions=(), publications=(), effects=(), ordinal=0):
    return RcaPublicState(
        "version:1", Freshness.FRESH, ("attempt:1",), tuple(tries),
        tuple(versions), tuple(publications), "frontier:1", ordinal,
        semantic_effects=tuple(effects))


@pytest.mark.parametrize("boundary", tuple(RecoveryBoundary))
def test_each_approved_interruption_uses_existing_duplicate_recovery_authority(boundary):
    operation = "operation:1"
    before = _state()
    kwargs = {}
    identity = f"effect:{boundary.value}"
    if boundary is RecoveryBoundary.GENERATION:
        kwargs["tries"] = (identity,)
    elif boundary is RecoveryBoundary.ARTIFACT_COMMIT:
        kwargs["versions"] = (identity,)
    elif boundary is RecoveryBoundary.PUBLICATION:
        kwargs["publications"] = (identity,)
    effectful = boundary in {
        RecoveryBoundary.GENERATION, RecoveryBoundary.ARTIFACT_COMMIT,
        RecoveryBoundary.PUBLICATION}
    after = _state(
        effects=((boundary.value, identity, operation),) if effectful else (),
        ordinal=1 if effectful else 0, **kwargs)
    passed, details = verify_recovery_continuity(
        before=before, after=after, boundary=boundary,
        expected_operation_identity=operation,
        completed_operation_identity=operation, signal_injection_count=1,
        publication_reconciled_before_resume=True)
    assert passed, details
    duplicated, duplicate_details = verify_recovery_continuity(
        before=before, after=after, boundary=boundary,
        expected_operation_identity=operation,
        completed_operation_identity=operation, signal_injection_count=2,
        publication_reconciled_before_resume=True)
    assert not duplicated
    assert "scenario signal was duplicated" in duplicate_details


def test_health_contradiction_and_actual_required_fact_resolution_fail_closed():
    envelope = _envelope()
    labels = tuple(sorted({
        **dict(expected_ownership_labels(envelope)),
        "spec017.topology_commitment": "sha256:" + "0" * 64,
    }.items()))
    inspection = _inspection(envelope, _resources(envelope, labels=labels))
    health = project_health(envelope, inspection)
    assert health.operational_state is OperationalState.CONTRADICTORY
    facts = docker_lifecycle_required_facts(envelope, inspection)
    assert all(item.state is ReadinessFactState.CONTRADICTORY for item in facts)


def test_no_runtime_scheduler_retry_recovery_or_ground_truth_capability_exists():
    names = set(dir(FormalDockerLifecycleController))
    assert names.isdisjoint({
        "schedule", "retry", "recover_runtime", "inject", "oracle",
        "expected_root_cause", "system_prune", "volume_prune", "network_prune",
    })
    matrix = approved_interruption_matrix(ROOT)
    serialized = repr(matrix).lower()
    assert "oracle" not in serialized
    assert "expected_root_cause" not in serialized
