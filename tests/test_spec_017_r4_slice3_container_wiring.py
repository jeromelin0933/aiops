from __future__ import annotations

from dataclasses import replace
import inspect
import json
from pathlib import Path
import shutil

import pytest
import yaml

from rca_evaluation.container_wiring import (
    CORPUS_CONTAINER_TARGET, CREDENTIAL_ENVIRONMENT,
    REAL_RAG_CONTAINER_TARGET, ContainerMountState,
    materialize_formal_compose_environment,
    resolve_container_real_rag_readiness, resolve_formal_container_wiring,
)
from rca_evaluation.formal_admission import RepositoryState
from rca_evaluation.formal_compose import FORMAL_COMPOSE_PATH
from rca_evaluation.identity import commitment, identity
from rca_evaluation.preparation import PREPARED_CHILD_MARKER, prepare_formal_campaign
from rca_evaluation.readiness import (
    CredentialState, ReadinessFactState, resolve_formal_required_facts,
)
from rca_evaluation.sqlite_store import SqliteEvaluationStore


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/rca_evaluation/formal_preregistration_v1.json"
HEAD = "e9cee50ce7904fb822e8032bbe8b05a4a826cf07"
BRANCH = "feature/spec-017-formal-evaluation-readiness"
CAMPAIGN = "formal-campaign-2026-10"
FAKE_SECRET = "SPEC017_TEST_SECRET_DO_NOT_PERSIST"


def _prepare(tmp_path: Path):
    evaluation = tmp_path / "rca_evaluation"
    report = prepare_formal_campaign(
        repository_root=ROOT, preregistration_path=CONFIG,
        campaign_key=CAMPAIGN, database_path=evaluation / "evaluation.db",
        evaluation_root=evaluation,
        repository_state=RepositoryState(BRANCH, HEAD, False))
    return report, evaluation


def _wiring(tmp_path: Path, environment=None):
    prepared, evaluation = _prepare(tmp_path)
    store = SqliteEvaluationStore(evaluation / "evaluation.db")
    wiring = resolve_formal_container_wiring(
        store, prepared.preparation_id, ROOT,
        _environment={} if environment is None else environment,
        _validated_at="2026-10-16T00:00:00Z")
    return prepared, evaluation, store, wiring


def _reseal_mount(mount, **changes):
    provisional = replace(
        mount, **changes, mount_commitment="sha256:" + "0" * 64)
    material = provisional.material(include_commitment=False)
    return replace(provisional, mount_commitment=commitment(material))


def test_compose_uses_name_only_external_credential_and_no_secret_default():
    text = (ROOT / FORMAL_COMPOSE_PATH).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    runtime = raw["services"]["runtime"]
    assert runtime["environment"] == [CREDENTIAL_ENVIRONMENT]
    assert FAKE_SECRET not in text
    assert "GEMINI_API_KEY:-" not in text
    assert "GEMINI_API_KEY:" not in text
    binds = [item for item in runtime["volumes"] if isinstance(item, dict)]
    assert {item["target"] for item in binds} == {
        REAL_RAG_CONTAINER_TARGET, CORPUS_CONTAINER_TARGET}
    assert next(item for item in binds
                if item["target"] == CORPUS_CONTAINER_TARGET)["read_only"] is True


@pytest.mark.parametrize(
    ("environment", "expected"),
    [({}, CredentialState.MISSING),
     ({CREDENTIAL_ENVIRONMENT: FAKE_SECRET}, CredentialState.AVAILABLE),
     ({CREDENTIAL_ENVIRONMENT: " invalid value "}, CredentialState.INVALID)],
)
def test_container_credentials_use_r3_typed_resolver(tmp_path, environment, expected):
    _, _, store, wiring = _wiring(tmp_path, environment)
    try:
        assert wiring.llm_credential.credential_state is expected
        assert wiring.rag_credential.credential_state is expected
        assert wiring.llm_credential.credential_reference == "env:GEMINI_API_KEY"
        assert wiring.rag_credential.credential_reference == "env:GEMINI_API_KEY"
    finally:
        store.close()


def test_exact_r3_llm_and_real_rag_profiles_are_preserved(tmp_path):
    _, _, store, wiring = _wiring(tmp_path, {CREDENTIAL_ENVIRONMENT: FAKE_SECRET})
    try:
        assert wiring.llm_profile == {
            "profile_identity": "spec017-real-llm-gate-v1",
            "provider": "google", "model": "gemini-2.5-flash",
            "generation_profile_identity": "spec016-google-generation-profile-v1",
            "prompt_identity": "spec016-rca-prompt-v1",
            "result_schema_identity": "spec015-rca-result-v1",
            "configuration_identity": "cfg_c4c0532057d9d56f943c7bdcc4c3028b87b8fe4952e8fc460bf7df46205f8611",
            "timeout_seconds": 30, "maximum_invocations": 1,
            "hidden_retries": "DISABLED"}
        assert wiring.rag_profile["profile_identity"] == "spec017-real-rag-gate-v1"
        assert wiring.rag_profile["corpus_identity"] == "spec014-production-knowledge"
        assert wiring.rag_profile["corpus_release"] == "spec014-production-knowledge-release@1.0"
        assert wiring.rag_profile["source_revision"] == "7e577de0ad85946c00c7259af3e1636f9eabd5ae"
        assert wiring.rag_profile["embedding_model"] == "text-embedding-004"
        assert wiring.rag_profile["embedding_profile_identity"] == "spec014-google-document-query-v1"
        assert wiring.rag_profile["index_schema_identity"] == "spec014-chroma-l2-v2"
        assert wiring.rag_profile["distance_metric"] == "l2"
        assert wiring.rag_profile["retrieval_profile_identity"] == "spec014-retrieval-v1"
    finally:
        store.close()


def test_same_slot_replays_same_mount_and_different_slots_are_disjoint(tmp_path):
    prepared, _, store, first = _wiring(tmp_path)
    try:
        second = resolve_formal_container_wiring(
            store, prepared.preparation_id, ROOT, _environment={},
            _validated_at="2026-10-16T00:00:00Z")
        assert second == first
        assert first.mounts[0] == second.mounts[0]
        assert first.mounts[0].execution_child_identity != first.mounts[1].execution_child_identity
        assert first.mounts[0].child_source_reference != first.mounts[1].child_source_reference
        assert len({item.execution_child_identity for item in first.mounts}) == 30
    finally:
        store.close()


@pytest.mark.parametrize("mutation", ["owner", "profile", "execution", "escape", "production"])
def test_arbitrary_or_contradictory_mount_material_is_rejected(tmp_path, mutation):
    _, _, store, wiring = _wiring(tmp_path)
    try:
        mount = wiring.mounts[0]
        if mutation == "owner":
            changed = _reseal_mount(mount, allocation_root_identity="ragchild_wrong")
        elif mutation == "profile":
            changed = _reseal_mount(mount, profile_identity="foreign-rag-profile")
        elif mutation == "execution":
            changed = _reseal_mount(
                mount, planned_execution_identity=identity(
                    "planned_execution", ["foreign", "S1", 1]))
        elif mutation == "escape":
            changed = _reseal_mount(mount, child_source_reference="../shared")
        else:
            changed = _reseal_mount(
                mount, child_source_reference="var/knowledge_index/chroma")
        with pytest.raises(ValueError, match="contradictory"):
            changed.validate(
                allocation_root_reference=mount.child_source_reference.split("/executions/")[0],
                expected_profile="spec017-real-rag-gate-v1")
    finally:
        store.close()


def test_missing_or_mismatched_preparation_marker_blocks_wiring(tmp_path):
    prepared, evaluation = _prepare(tmp_path)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        child = store.get_preparation(prepared.preparation_id)["real_rag_child"]
        marker = evaluation / child["relative_path"] / PREPARED_CHILD_MARKER
        original = marker.read_text(encoding="utf-8")
        marker.unlink()
        with pytest.raises(ValueError, match="marker"):
            resolve_formal_container_wiring(store, prepared.preparation_id, ROOT)
        marker.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="marker"):
            resolve_formal_container_wiring(store, prepared.preparation_id, ROOT)
        marker.write_text(original, encoding="utf-8")


def test_actual_mount_build_and_activation_remain_unresolved(tmp_path):
    _, _, store, wiring = _wiring(tmp_path)
    try:
        current = resolve_container_real_rag_readiness(
            wiring.mounts[0], container_root=tmp_path / "container",
            _environment={CREDENTIAL_ENVIRONMENT: FAKE_SECRET},
            _validated_at="2026-10-16T00:00:00Z")
        assert current.credential.credential_state is CredentialState.AVAILABLE
        assert current.mount_fact.state is ReadinessFactState.UNRESOLVED
        assert current.corpus_fact.state is ReadinessFactState.UNRESOLVED
        assert current.build_fact.state is ReadinessFactState.UNRESOLVED
        assert current.activation_fact.state is ReadinessFactState.UNRESOLVED
        with pytest.raises(ValueError, match="unresolved"):
            current.assert_resolved()
    finally:
        store.close()


def test_container_mount_marker_is_fresh_read_and_contradiction_blocks(tmp_path):
    _, _, store, wiring = _wiring(tmp_path)
    try:
        mount = wiring.mounts[0]
        target = tmp_path / "container" / mount.child_container_target.lstrip("/")
        target.mkdir(parents=True)
        marker = target / PREPARED_CHILD_MARKER
        marker.write_text(json.dumps(mount.expected_marker()), encoding="utf-8")
        current = resolve_container_real_rag_readiness(
            mount, container_root=tmp_path / "container", _environment={})
        assert current.mount_fact.state is ReadinessFactState.RESOLVED
        corpus = (tmp_path / "container"
                  / mount.corpus_container_target.lstrip("/"))
        shutil.copytree(ROOT / "docs/knowledge", corpus)
        current = resolve_container_real_rag_readiness(
            mount, container_root=tmp_path / "container", _environment={})
        assert current.corpus_fact.state is ReadinessFactState.RESOLVED
        (corpus / "credential_abuse_brute_force_response.md").write_text(
            "contradiction", encoding="utf-8")
        current = resolve_container_real_rag_readiness(
            mount, container_root=tmp_path / "container", _environment={})
        assert current.corpus_fact.state is ReadinessFactState.CONTRADICTORY
        marker.write_text("{}", encoding="utf-8")
        current = resolve_container_real_rag_readiness(
            mount, container_root=tmp_path / "container", _environment={})
        assert current.mount_fact.state is ReadinessFactState.CONTRADICTORY
    finally:
        store.close()


def test_supported_compose_inputs_are_derived_and_create_nothing(tmp_path):
    prepared, evaluation, store, wiring = _wiring(tmp_path)
    try:
        before = set(evaluation.rglob("*"))
        values = materialize_formal_compose_environment(
            store, prepared.preparation_id,
            wiring.mounts[0].planned_execution_identity, ROOT)
        after = set(evaluation.rglob("*"))
        assert set(values) == {
            "SPEC017_REAL_RAG_CHILD_SOURCE", "SPEC017_REAL_RAG_CORPUS_SOURCE"}
        assert Path(values["SPEC017_REAL_RAG_CHILD_SOURCE"]).is_relative_to(evaluation)
        assert Path(values["SPEC017_REAL_RAG_CHILD_SOURCE"]).resolve() == (
            evaluation / wiring.mounts[0].child_source_reference).resolve()
        assert Path(values["SPEC017_REAL_RAG_CORPUS_SOURCE"]).resolve() == (
            ROOT / "docs/knowledge").resolve()
        assert before == after
        assert not Path(values["SPEC017_REAL_RAG_CHILD_SOURCE"]).exists()
    finally:
        store.close()


def test_synthetic_secret_is_absent_from_payload_commitment_database_and_logs(
    tmp_path, caplog,
):
    prepared, evaluation, store, wiring = _wiring(
        tmp_path, {CREDENTIAL_ENVIRONMENT: FAKE_SECRET})
    try:
        serialized = json.dumps(wiring.semantic_material(), sort_keys=True)
        assert FAKE_SECRET not in serialized
        assert FAKE_SECRET not in wiring.wiring_commitment
        assert FAKE_SECRET.encode() not in (evaluation / "evaluation.db").read_bytes()
        assert FAKE_SECRET not in caplog.text
        assert wiring.llm_credential.credential_state is CredentialState.AVAILABLE
        assert wiring.rag_credential.credential_state is CredentialState.AVAILABLE
        assert store.get_preparation(prepared.preparation_id) is not None
    finally:
        store.close()


def test_required_fact_integration_is_fresh_and_fail_closed(tmp_path, monkeypatch):
    prepared, evaluation = _prepare(tmp_path)
    template = json.loads(CONFIG.read_text(encoding="utf-8"))
    monkeypatch.setenv(CREDENTIAL_ENVIRONMENT, FAKE_SECRET)
    with SqliteEvaluationStore(evaluation / "evaluation.db") as store:
        resolution = resolve_formal_required_facts(
            template, repository_root=ROOT, evaluation_store=store,
            preparation_id=prepared.preparation_id)
    injection = next(f for f in resolution.prerequisites
                     if f.name == "formal_container_credential_injection")
    wiring = next(f for f in resolution.prerequisites
                  if f.name == "formal_real_rag_mount_wiring")
    actual = next(f for f in resolution.prerequisites
                  if f.name == "actual_real_rag_child_mount")
    assert injection.state is ReadinessFactState.RESOLVED
    assert injection.value["default_value"] is None
    assert wiring.state is ReadinessFactState.RESOLVED
    assert wiring.value["execution_mount_count"] == 30
    assert actual.state is ReadinessFactState.UNRESOLVED
    assert FAKE_SECRET not in json.dumps([f.material() for f in resolution.prerequisites])
    with pytest.raises(ValueError, match="unresolved required fact"):
        resolution.assert_resolved()


def test_supported_required_fact_api_has_no_caller_wiring_or_credential_fact():
    parameters = inspect.signature(resolve_formal_required_facts).parameters
    assert "container_wiring" not in parameters
    assert "credential_fact" not in parameters
    assert "mount_source" not in parameters


def test_slice3_module_has_no_execution_or_environment_dump_capability():
    import rca_evaluation.container_wiring as module

    source = inspect.getsource(module)
    assert "subprocess" not in source
    assert "os.environ" not in source
    assert "docker compose" not in source.lower()
    assert "KnowledgeBuildService" not in source
    assert "GoogleEmbeddingAdapter" not in source
