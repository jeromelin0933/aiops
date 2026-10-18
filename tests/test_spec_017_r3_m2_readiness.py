"""R3-M2 deterministic readiness engineering; no external invocation/build."""

from __future__ import annotations

from dataclasses import replace
import inspect
import json
from pathlib import Path

import pytest

from knowledge_index import admit_production_manifest, load_knowledge_config
from llm_generation.config import load_generation_config
from rca_evaluation.authority import (
    PM_AUTHORITY, TrustedApprovalRegistry, TrustedOracleApproval,
)
from rca_evaluation.gate_records import record_gate_authorization
from rca_evaluation.gates import (
    AuthorizationRequest, GateExecutionStatus, GateKind, RealLlmFacts,
    evaluate_real_llm, evaluate_real_rag, load_real_llm_profile,
    load_real_rag_profile,
)
from rca_evaluation.identity import commitment, identity
from rca_evaluation.ledger import LedgerRecord
from rca_evaluation.oracle import EvaluationPlan, OracleRevision
from rca_evaluation.readiness import (
    CredentialState, ReadinessFactState, RealLlmReadinessCommitment,
    RequiredFact, RequiredFactsMaterialization, allocate_real_rag_child,
    create_real_rag_readiness_commitment, materialize_required_before_execution,
    resolve_candidate_c_build, resolve_environment_credential,
    verify_supported_real_rag_child_allocation,
)
import rca_evaluation.readiness as readiness_module
from rca_evaluation.sqlite_store import SqliteEvaluationStore


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-10-16T00:00:00Z"
REPOSITORY_IDENTITY = "e9cee50ce7904fb822e8032bbe8b05a4a826cf07"


@pytest.fixture
def authority(tmp_path):
    oracle = OracleRevision.approved(
        oracle_set_id="R3-M2", semantics={
            "scenarios": {f"S{i}": {
                f"O{j}": f"approved-{i}-{j}" for j in range(1, 11)}
                for i in range(1, 7)},
            "cross_scenario_rules": ["approved disambiguation"]},
        created_at=NOW, approval_authority=PM_AUTHORITY,
        approval_reference="R3-M2-TEST", approved_at=NOW)
    approvals = TrustedApprovalRegistry((TrustedOracleApproval(
        oracle.approval_reference, PM_AUTHORITY, oracle.oracle_revision_id,
        oracle.semantic_content_commitment, oracle.approved_at,
        oracle.parent_revision_id),))
    store = SqliteEvaluationStore(
        tmp_path / "rca_evaluation" / "evaluation.db", approvals=approvals)
    store.admit_oracle(oracle)
    plan = EvaluationPlan.admit(
        oracle, fixture_revision="fixtures", production_build=REPOSITORY_IDENTITY,
        model="model", prompt="prompt", schema="schema", corpus_index="index",
        configuration=commitment({"r3": "m2"}), repetitions=1)
    store.admit_plan(plan)
    run = store.append(LedgerRecord.create(
        "evaluation_run", [plan.evaluation_plan_id, "r3-m2"],
        {"oracle_revision_id": oracle.oracle_revision_id}, plan.evaluation_plan_id))
    execution = store.append(LedgerRecord.create(
        "execution", [run.record_id, "S1", 1],
        {"scenario_id": "S1", "repetition": 1,
         "replacement_for_execution_id": None}, run.record_id))
    yield store, run.record_id, execution.record_id
    store.close()


def _credential(profile, value="structurally-present"):
    return resolve_environment_credential(
        credential_environment=profile.authorization.credential_environment,
        required_profile_identity=profile.profile_identity,
        profile_identity=profile.profile_identity, validated_at=NOW,
        environment_identity="formal-evaluation-test",
        environment={profile.authorization.credential_environment: value})


def test_credential_states_are_typed_deterministic_and_secret_free():
    profile = load_real_llm_profile(ROOT / "configs/rca_evaluation/real_llm.yaml")
    missing = resolve_environment_credential(
        credential_environment="GEMINI_API_KEY",
        required_profile_identity=profile.profile_identity,
        profile_identity=profile.profile_identity, validated_at=NOW,
        environment_identity="formal-evaluation-test", environment={})
    assert missing.credential_state is CredentialState.MISSING
    assert missing == resolve_environment_credential(
        credential_environment="GEMINI_API_KEY",
        required_profile_identity=profile.profile_identity,
        profile_identity=profile.profile_identity, validated_at=NOW,
        environment_identity="formal-evaluation-test", environment={})
    available_a = _credential(profile, "TOP-SECRET-A")
    available_b = _credential(profile, "TOP-SECRET-B")
    assert available_a == available_b
    serialized = json.dumps(available_a.material(), sort_keys=True)
    assert "TOP-SECRET" not in serialized
    assert available_a.credential_state is CredentialState.AVAILABLE
    invalid = resolve_environment_credential(
        credential_environment="GEMINI_API_KEY",
        required_profile_identity=profile.profile_identity,
        profile_identity="wrong-profile", validated_at=NOW,
        environment_identity="formal-evaluation-test",
        environment={"GEMINI_API_KEY": "TOP-SECRET"})
    assert invalid.credential_state is CredentialState.INVALID


def test_llm_commitment_is_stable_secret_independent_and_complete():
    profile = load_real_llm_profile(ROOT / "configs/rca_evaluation/real_llm.yaml")
    config = load_generation_config(ROOT / profile.config_path)
    first = RealLlmReadinessCommitment.create(
        profile, config, _credential(profile, "SECRET-A"),
        repository_identity=REPOSITORY_IDENTITY)
    replay = RealLlmReadinessCommitment.create(
        profile, config, _credential(profile, "SECRET-B"),
        repository_identity=REPOSITORY_IDENTITY)
    assert first == replay
    assert "SECRET" not in json.dumps(first.payload, sort_keys=True)
    changed = replace(_credential(profile), validated_at="2026-10-16T00:00:01Z")
    assert RealLlmReadinessCommitment.create(
        profile, config, changed,
        repository_identity=REPOSITORY_IDENTITY).readiness_commitment != first.readiness_commitment
    with pytest.raises(ValueError, match="missing"):
        RealLlmReadinessCommitment.create(
            profile, config, _credential(profile), repository_identity="")


def test_caller_boolean_is_not_credential_authority(authority):
    store, run_id, execution_id = authority
    profile = load_real_llm_profile(ROOT / "configs/rca_evaluation/real_llm.yaml")
    authorization = record_gate_authorization(
        store, evaluation_run_id=run_id, gate=GateKind.REAL_LLM.value,
        profile_identity=profile.profile_identity,
        profile_commitment=profile.profile_commitment,
        authorization_source="R3-M2", authorizing_actor="SPEC Lead", issued_at=NOW)
    result = evaluate_real_llm(
        profile, RealLlmFacts(
            AuthorizationRequest(authorization.authorization_id, credential_ready=True),
            True), repository_root=ROOT, authority=store.reader(),
        evaluation_run_id=run_id, execution_id=execution_id)
    assert result.status is GateExecutionStatus.NOT_READY
    assert result.readiness_commitment is None


def _allocate(store, run_id, execution_id, root, **overrides):
    values = {
        "repository_root": root,
        "isolated_root": "var/rca_evaluation/real_rag",
        "production_root": "var/knowledge_index/chroma",
        "evaluation_run_id": run_id, "execution_id": execution_id,
        "environment_identity": "formal-evaluation-test",
        "profile_identity": "spec017-real-rag-gate-v1",
    }
    values.update(overrides)
    return allocate_real_rag_child(store, **values)


def test_rag_child_fresh_allocation_and_equivalent_replay(authority, tmp_path):
    store, run_id, execution_id = authority
    first = _allocate(store, run_id, execution_id, tmp_path)
    replay = _allocate(store, run_id, execution_id, tmp_path)
    assert replay == first
    assert (tmp_path / first.relative_path / ".candidate-f-ownership.json").is_file()
    assert store.get(first.allocation_record_id) is not None
    with pytest.raises(ValueError, match="protected"):
        store.append(store.get(first.allocation_record_id))


def test_allocation_persistence_has_no_issuance_seal_or_token():
    assert not hasattr(readiness_module, "_RAG_CHILD_ISSUANCE_SEAL")
    assert not hasattr(readiness_module, "is_rag_child_issuance_authority")
    assert tuple(inspect.signature(
        SqliteEvaluationStore._append_rag_child_allocation).parameters) == (
            "self", "record")


def test_supported_real_rag_api_has_no_caller_reader_injection_seam():
    parameters = inspect.signature(evaluate_real_rag).parameters
    assert "evaluation_store" in parameters
    assert "authority" not in parameters
    supported = inspect.signature(
        verify_supported_real_rag_child_allocation).parameters
    assert "evaluation_store" in supported
    assert "authority" not in supported


def test_rag_child_collision_stale_contradiction_and_root_escape_fail_closed(
    authority, tmp_path,
):
    store, run_id, execution_id = authority
    child_identity = "ragchild_" + commitment({
        "run": run_id, "execution": execution_id,
        "environment": "formal-evaluation-test",
        "profile": "spec017-real-rag-gate-v1",
    })[7:]
    collision_root = tmp_path / "collision"
    unrelated = (collision_root / "var/rca_evaluation/real_rag" /
                 child_identity)
    unrelated.mkdir(parents=True)
    (unrelated / "unrelated.txt").write_text("unrelated", encoding="utf-8")
    with pytest.raises(ValueError, match="collision or stale"):
        _allocate(store, run_id, execution_id, collision_root)

    probe_root = tmp_path / "probe"
    probe = _allocate(store, run_id, execution_id, probe_root)
    # The durable replay points at probe_root; changing repository root is a
    # contradictory replay because its sealed relative path differs in context.
    with pytest.raises(ValueError, match="ownership|marker|collision|stale"):
        _allocate(store, run_id, execution_id, tmp_path / "other")
    marker = probe_root / probe.relative_path / ".candidate-f-ownership.json"
    owner = json.loads(marker.read_text(encoding="utf-8"))
    owner["lifecycle_state"] = "COMPLETED"
    marker.write_text(json.dumps(owner), encoding="utf-8")
    with pytest.raises(ValueError, match="contradictory or stale"):
        _allocate(store, run_id, execution_id, probe_root)
    with pytest.raises(ValueError):
        _allocate(store, run_id, execution_id, tmp_path / "escape",
                  isolated_root="../outside")


def test_rag_child_rejects_production_root(authority, tmp_path):
    store, run_id, execution_id = authority
    with pytest.raises(ValueError, match="production"):
        _allocate(store, run_id, execution_id, tmp_path,
                  isolated_root="var/knowledge_index/chroma")


def test_rag_child_reference_is_fresh_read_from_ledger_and_marker(authority, tmp_path):
    store, run_id, execution_id = authority
    child = _allocate(store, run_id, execution_id, tmp_path)
    values = dict(
        evaluation_store=store, repository_root=tmp_path,
        isolated_root="var/rca_evaluation/real_rag",
        production_root="var/knowledge_index/chroma",
        evaluation_run_id=run_id, execution_id=execution_id,
        profile_identity="spec017-real-rag-gate-v1",
    )
    assert verify_supported_real_rag_child_allocation(child, **values) == child

    for changed in (
        replace(child, allocation_record_id=identity("observation", ["missing"])),
        replace(child, evaluation_run_id=identity("evaluation_run", ["wrong"])),
        replace(child, execution_id=identity("execution", ["wrong"])),
        replace(child, profile_identity="wrong-profile"),
        replace(child, child_identity="wrong-child"),
        replace(child, relative_path="var/rca_evaluation/real_rag/wrong"),
        replace(child, ownership_commitment="sha256:" + "0" * 64),
    ):
        with pytest.raises(ValueError):
            verify_supported_real_rag_child_allocation(changed, **values)
    with pytest.raises(ValueError, match="production"):
        verify_supported_real_rag_child_allocation(
            child, **{**values,
                      "production_root": "var/rca_evaluation/real_rag"})


def test_rag_child_marker_missing_corrupt_mismatch_and_stale_fail_closed(
    authority, tmp_path,
):
    store, run_id, execution_id = authority
    child = _allocate(store, run_id, execution_id, tmp_path)
    marker = tmp_path / child.relative_path / ".candidate-f-ownership.json"
    original = marker.read_text(encoding="utf-8")
    values = dict(
        evaluation_store=store, repository_root=tmp_path,
        isolated_root="var/rca_evaluation/real_rag",
        production_root="var/knowledge_index/chroma",
        evaluation_run_id=run_id, execution_id=execution_id,
        profile_identity="spec017-real-rag-gate-v1",
    )
    marker.unlink()
    with pytest.raises(ValueError, match="absent"):
        verify_supported_real_rag_child_allocation(child, **values)
    marker.write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt"):
        verify_supported_real_rag_child_allocation(child, **values)
    owner = json.loads(original)
    owner["child_identity"] = "mismatch"
    marker.write_text(json.dumps(owner), encoding="utf-8")
    with pytest.raises(ValueError, match="mismatches"):
        verify_supported_real_rag_child_allocation(child, **values)
    owner = json.loads(original)
    owner["lifecycle_state"] = "COMPLETED"
    marker.write_text(json.dumps(owner), encoding="utf-8")
    with pytest.raises(ValueError, match="mismatch|stale"):
        verify_supported_real_rag_child_allocation(child, **values)


def test_rag_child_ledger_payload_mismatch_fails_closed(authority, tmp_path):
    store, run_id, execution_id = authority
    child = _allocate(store, run_id, execution_id, tmp_path)
    record = store.get(child.allocation_record_id)
    payload = json.loads(record.semantic_json)
    payload["child_identity"] = "corrupt-ledger-child"
    corrupt_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    store._db.execute("DROP TRIGGER ledger_no_update")
    store._db.execute(
        "UPDATE ledger SET semantic_json=? WHERE record_id=?",
        (corrupt_json, child.allocation_record_id))
    store._db.execute(
        "CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger "
        "BEGIN SELECT RAISE(ABORT, 'immutable'); END")

    with pytest.raises(ValueError, match="authority is unavailable"):
        verify_supported_real_rag_child_allocation(
            child, evaluation_store=store, repository_root=tmp_path,
            isolated_root="var/rca_evaluation/real_rag",
            production_root="var/knowledge_index/chroma",
            evaluation_run_id=run_id, execution_id=execution_id,
            profile_identity="spec017-real-rag-gate-v1",
        )


def test_supported_path_rejects_stale_durable_allocation(authority, tmp_path):
    store, run_id, execution_id = authority
    child = _allocate(store, run_id, execution_id, tmp_path)
    record = store.get(child.allocation_record_id)
    payload = json.loads(record.semantic_json)
    payload["lifecycle_state"] = "COMPLETED"
    stale_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    store._db.execute("DROP TRIGGER ledger_no_update")
    store._db.execute(
        "UPDATE ledger SET semantic_json=?, semantic_commitment=? WHERE record_id=?",
        (stale_json, commitment(payload), child.allocation_record_id))
    store._db.execute(
        "CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger "
        "BEGIN SELECT RAISE(ABORT, 'immutable'); END")

    with pytest.raises(ValueError, match="unavailable|lifecycle"):
        verify_supported_real_rag_child_allocation(
            child, evaluation_store=store, repository_root=tmp_path,
            isolated_root="var/rca_evaluation/real_rag",
            production_root="var/knowledge_index/chroma",
            evaluation_run_id=run_id, execution_id=execution_id,
            profile_identity="spec017-real-rag-gate-v1",
        )


def test_rag_commitment_keeps_build_and_activation_unresolved(authority, tmp_path):
    store, run_id, execution_id = authority
    profile = load_real_rag_profile(ROOT / "configs/rca_evaluation/real_rag.yaml")
    config = load_knowledge_config(ROOT / profile.values()["config_path"])
    manifest = admit_production_manifest(
        ROOT / profile.values()["manifest_path"], repository_root=ROOT)
    child = _allocate(store, run_id, execution_id, tmp_path)
    build, activation = resolve_candidate_c_build(None, config)
    result = create_real_rag_readiness_commitment(
        profile, config, manifest, child,
        repository_identity=REPOSITORY_IDENTITY,
        build_fact=build, activation_fact=activation,
        credential=_credential(profile))
    replay = create_real_rag_readiness_commitment(
        profile, config, manifest, child,
        repository_identity=REPOSITORY_IDENTITY,
        build_fact=build, activation_fact=activation,
        credential=_credential(profile))
    assert result == replay
    assert not result.ready
    assert build.state is ReadinessFactState.UNRESOLVED
    assert activation.state is ReadinessFactState.UNRESOLVED
    malformed_build, malformed_activation = resolve_candidate_c_build(object(), config)
    assert malformed_build.state is ReadinessFactState.CONTRADICTORY
    assert malformed_activation.state is ReadinessFactState.CONTRADICTORY
    with pytest.raises(ValueError, match="contradicts"):
        create_real_rag_readiness_commitment(
            replace(profile, settings=profile.settings.replace(b'"l2"', b'"cosine"')),
            config, manifest, child, repository_identity=REPOSITORY_IDENTITY,
            build_fact=build, activation_fact=activation,
            credential=_credential(profile))


def test_rag_commitment_binds_sanitized_embedding_credential(authority, tmp_path):
    store, run_id, execution_id = authority
    profile = load_real_rag_profile(ROOT / "configs/rca_evaluation/real_rag.yaml")
    config = load_knowledge_config(ROOT / profile.values()["config_path"])
    manifest = admit_production_manifest(
        ROOT / profile.values()["manifest_path"], repository_root=ROOT)
    child = _allocate(store, run_id, execution_id, tmp_path)
    build, activation = resolve_candidate_c_build(None, config)

    def committed(credential, selected_profile=profile):
        return create_real_rag_readiness_commitment(
            selected_profile, config, manifest, child,
            repository_identity=REPOSITORY_IDENTITY,
            build_fact=build, activation_fact=activation,
            credential=credential)

    missing = resolve_environment_credential(
        credential_environment="GEMINI_API_KEY",
        required_profile_identity=profile.profile_identity,
        profile_identity=profile.profile_identity, validated_at=NOW,
        environment_identity="formal-evaluation-test", environment={})
    invalid = resolve_environment_credential(
        credential_environment="GEMINI_API_KEY",
        required_profile_identity=profile.profile_identity,
        profile_identity="wrong", validated_at=NOW,
        environment_identity="formal-evaluation-test",
        environment={"GEMINI_API_KEY": "present"})
    available_a = _credential(profile, "SECRET-A")
    available_b = _credential(profile, "SECRET-B")
    results = [committed(item) for item in (missing, invalid, available_a)]
    assert [item.payload["credential_readiness"]["credential_state"]
            for item in results] == ["MISSING", "INVALID", "AVAILABLE"]
    assert len({item.readiness_commitment for item in results}) == 3
    assert committed(available_a) == committed(available_b)
    assert not results[0].ready and not results[1].ready
    serialized = json.dumps([item.payload for item in results], sort_keys=True)
    assert "SECRET-A" not in serialized and "SECRET-B" not in serialized
    changed_environment = replace(available_a, environment_identity="other-environment")
    assert committed(changed_environment).readiness_commitment != committed(
        available_a).readiness_commitment
    changed_profile = replace(profile, profile_identity="spec017-real-rag-gate-v2")
    changed_profile_credential = replace(
        available_a, profile_identity=changed_profile.profile_identity)
    assert committed(changed_profile_credential, changed_profile).readiness_commitment != committed(
        available_a).readiness_commitment
    changed_authorization = replace(
        profile.authorization, credential_environment="OTHER_GEMINI_API_KEY")
    changed_reference_profile = replace(profile, authorization=changed_authorization)
    changed_reference = replace(
        available_a, credential_reference="env:OTHER_GEMINI_API_KEY")
    assert committed(changed_reference, changed_reference_profile).readiness_commitment != committed(
        available_a).readiness_commitment


def test_required_fact_materialization_resolves_only_typed_authority_facts():
    template = {"plan": {"build": "REQUIRED_BEFORE_EXECUTION"}}
    resolved = RequiredFact(
        "production_build", ReadinessFactState.RESOLVED,
        REPOSITORY_IDENTITY, "read-only Git inspection", "exact HEAD resolved")
    materialization = RequiredFactsMaterialization.create(
        {"/plan/build": resolved})
    assert materialize_required_before_execution(
        template, materialization)["plan"]["build"] == REPOSITORY_IDENTITY
    with pytest.raises(TypeError, match="typed"):
        materialize_required_before_execution(template, {"/plan/build": "caller"})
    for state in (ReadinessFactState.UNRESOLVED, ReadinessFactState.CONTRADICTORY):
        blocked = RequiredFactsMaterialization.create({
            "/plan/build": RequiredFact(
                "production_build", state, None, "public authority", "not usable")})
        with pytest.raises(ValueError, match=state.value.lower()):
            materialize_required_before_execution(template, blocked)
