from __future__ import annotations

from dataclasses import replace
import inspect
import json
from pathlib import Path

import pytest
import yaml

from rca_evaluation.formal_admission import RepositoryState
from rca_evaluation.formal_compose import (
    FORMAL_COMPOSE_PATH, SERVICE_CLASSIFICATION, FormalComposeEnvelope,
    FormalServiceClassification, FormalVolumeMount,
    resolve_formal_compose_topology,
)
from rca_evaluation.identity import commitment
from rca_evaluation.preparation import prepare_formal_campaign
from rca_evaluation.readiness import (
    ReadinessFactState, resolve_formal_required_facts,
)
from rca_evaluation.runtime_image_readiness import (
    RUNTIME_IMAGE_PROFILE, RUNTIME_IMAGE_TAG, RuntimeFactState,
)
from rca_evaluation.sqlite_store import SqliteEvaluationStore


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/rca_evaluation/formal_preregistration_v1.json"
HEAD = "e9cee50ce7904fb822e8032bbe8b05a4a826cf07"
BRANCH = "feature/spec-017-formal-evaluation-readiness"
CAMPAIGN = "formal-campaign-2026-10"


def _prepare(tmp_path: Path):
    evaluation = tmp_path / "rca_evaluation"
    report = prepare_formal_campaign(
        repository_root=ROOT, preregistration_path=CONFIG,
        campaign_key=CAMPAIGN, database_path=evaluation / "evaluation.db",
        evaluation_root=evaluation,
        repository_state=RepositoryState(BRANCH, HEAD, False))
    return report, evaluation


def _topology(tmp_path: Path):
    prepared, evaluation = _prepare(tmp_path)
    store = SqliteEvaluationStore(evaluation / "evaluation.db")
    topology = resolve_formal_compose_topology(
        store, prepared.preparation_id, ROOT)
    return prepared, evaluation, store, topology


def _reseal(envelope: FormalComposeEnvelope, **changes) -> FormalComposeEnvelope:
    provisional = replace(
        envelope, **changes, topology_commitment="sha256:" + "0" * 64)
    return replace(
        provisional,
        topology_commitment=commitment(
            provisional.material(include_commitment=False)))


def test_same_preparation_replays_same_project_and_topology_commitment(tmp_path):
    prepared, _, store, first = _topology(tmp_path)
    try:
        second = resolve_formal_compose_topology(store, prepared.preparation_id, ROOT)
        assert second == first
        assert second.topology_commitment == first.topology_commitment
        assert second.envelopes[0].compose_project_identity == (
            first.envelopes[0].compose_project_identity)
    finally:
        store.close()


def test_different_execution_slots_have_disjoint_project_network_and_volumes(tmp_path):
    _, _, store, topology = _topology(tmp_path)
    try:
        first, second = topology.envelopes[:2]
        assert first.planned_execution_identity != second.planned_execution_identity
        assert first.compose_project_identity != second.compose_project_identity
        assert first.network_identity != second.network_identity
        assert {item.volume_identity for item in first.volume_mounts}.isdisjoint(
            item.volume_identity for item in second.volume_mounts)
    finally:
        store.close()


def test_formal_artifact_has_no_fixed_names_ports_or_shared_logs():
    text = (ROOT / FORMAL_COMPOSE_PATH).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    assert "container_name" not in text
    assert "ports:" not in text
    assert "./logs" not in text
    assert set(raw["services"]) == {"runtime", "loki", "prometheus"}
    assert all(service["networks"] == ["formal"]
               for service in raw["services"].values())


def test_formal_volumes_and_workspace_are_execution_owned(tmp_path):
    _, evaluation, store, topology = _topology(tmp_path)
    try:
        envelope = topology.envelopes[0]
        for mount in envelope.volume_mounts:
            assert mount.volume_identity.startswith(
                envelope.compose_project_identity + "_")
            assert mount.volume_identity not in {
                "runtime-events", "runtime-state", "runtime-knowledge",
                "grafana-storage", "logs"}
        assert envelope.log_identity != "./logs"
        ownership = dict(envelope.state_volume_ownership)
        assert ownership["candidate_a"] == "runtime-state"
        assert ownership["candidate_b_evidence"] == "runtime-state"
        assert ownership["candidate_c_knowledge"] == "runtime-knowledge"
        assert ownership["candidate_d_generation"] == "runtime-state"
        assert ownership["incident_store"] == "runtime-state"
        assert ownership["correlation_state"] == "runtime-state"
        assert ownership["formal_logs"] == "formal-logs"
        workspace = (evaluation / envelope.workspace_identity).resolve()
        workspace.relative_to(evaluation.resolve())
        assert not workspace.exists()
    finally:
        store.close()


def test_network_is_explicitly_project_scoped(tmp_path):
    _, _, store, topology = _topology(tmp_path)
    try:
        envelope = topology.envelopes[0]
        assert envelope.network_identity == (
            envelope.compose_project_identity + "_formal")
        assert envelope.network_identity not in {"default", "aiops_default"}
    finally:
        store.close()


@pytest.mark.parametrize("mutation", ["project", "production_prefix", "volume", "path"])
def test_shared_or_escaping_resource_material_is_rejected(tmp_path, mutation):
    _, _, store, topology = _topology(tmp_path)
    try:
        envelope = topology.envelopes[0]
        if mutation == "project":
            changed = _reseal(envelope, compose_project_identity="production")
        elif mutation == "production_prefix":
            changed = _reseal(
                envelope, compose_project_identity="production-formal")
        elif mutation == "volume":
            mounts = (FormalVolumeMount(
                envelope.volume_mounts[0].role, "runtime-state",
                envelope.volume_mounts[0].target), *envelope.volume_mounts[1:])
            changed = _reseal(envelope, volume_mounts=mounts)
        else:
            changed = _reseal(envelope, workspace_identity="../shared")
        with pytest.raises(ValueError, match="contradictory or shared"):
            changed.validate()
    finally:
        store.close()


def test_topology_uses_slice1_profile_but_actual_resources_remain_unresolved(tmp_path):
    _, _, store, topology = _topology(tmp_path)
    try:
        assert len(topology.envelopes) == 30
        for envelope in topology.envelopes:
            assert envelope.runtime_image_profile == RUNTIME_IMAGE_PROFILE
            assert envelope.runtime_image_profile == "spec011-runtime-formal-v1"
            assert envelope.runtime_image_tag == RUNTIME_IMAGE_TAG
            assert envelope.actual_resources.state is RuntimeFactState.UNRESOLVED
            assert envelope.actual_resources.value is None
    finally:
        store.close()


def test_materialization_is_read_only_and_candidate_f_remains_single_authority(tmp_path):
    prepared, evaluation = _prepare(tmp_path)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        before = {
            table: store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("preparation_records", "ledger", "plans", "oracle_revisions")
        }
        children = set(evaluation.rglob("*"))
        topology = resolve_formal_compose_topology(
            store, prepared.preparation_id, ROOT)
        after = {
            table: store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        assert after == before
        assert set(evaluation.rglob("*")) == children
        assert topology.envelopes[0].actual_resources.state is RuntimeFactState.UNRESOLVED
        assert store.has_no_formal_execution_facts()


def test_service_classification_keeps_dashboards_out_of_formal_blockers():
    assert SERVICE_CLASSIFICATION == {
        "runtime": FormalServiceClassification.FORMAL_REQUIRED,
        "loki": FormalServiceClassification.FORMAL_REQUIRED,
        "prometheus": FormalServiceClassification.FORMAL_REQUIRED,
        "promtail": FormalServiceClassification.OPTIONAL_DIAGNOSTIC,
        "grafana": FormalServiceClassification.NOT_REQUIRED,
    }


def test_runtime_entrypoint_and_spec011_semantics_are_reused():
    raw = yaml.safe_load((ROOT / FORMAL_COMPOSE_PATH).read_text(encoding="utf-8"))
    runtime = raw["services"]["runtime"]
    assert runtime["command"] == [
        "python", "scripts/run_correlation_runtime.py", "--config",
        "configs/runtime_orchestration.docker.yaml"]
    assert "build" not in runtime
    assert not any(key in runtime for key in ("restart", "deploy", "healthcheck"))


def test_final_admission_required_fact_integration_is_fail_closed(tmp_path):
    prepared, evaluation = _prepare(tmp_path)
    template = json.loads(CONFIG.read_text(encoding="utf-8"))
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        resolution = resolve_formal_required_facts(
            template, repository_root=ROOT, evaluation_store=store,
            preparation_id=prepared.preparation_id)
    topology = next(fact for fact in resolution.prerequisites
                    if fact.name == "formal_compose_topology")
    actual = next(fact for fact in resolution.prerequisites
                  if fact.name == "actual_formal_docker_resources")
    assert topology.state is ReadinessFactState.RESOLVED
    assert topology.value["execution_envelope_count"] == 30
    assert len(topology.value["execution_envelopes"]) == 30
    assert len(topology.value["execution_envelopes"][0]["volume_identities"]) == 6
    assert actual.state is ReadinessFactState.UNRESOLVED
    with pytest.raises(ValueError, match="unresolved required fact"):
        resolution.assert_resolved()


def test_supported_required_fact_api_has_no_caller_topology_assertion():
    parameters = inspect.signature(resolve_formal_required_facts).parameters
    assert "topology" not in parameters
    assert "topology_facts" not in parameters
    assert "compose_project_identity" not in parameters


def test_no_docker_or_cleanup_command_capability_exists_in_slice2_module():
    import rca_evaluation.formal_compose as module

    source = inspect.getsource(module)
    assert "subprocess" not in source
    assert "compose down" not in source
    assert "docker volume" not in source
    assert "docker network" not in source
