"""Durable Candidate-D authority, replay and restart behavior."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
import sqlite3
import pytest

from llm_generation.contracts import FailureClass, GenerationFailure, LocalReadStatus, ValidatedGenerationResult
from llm_generation.sqlite_store import CandidateDStore
from rca_persistence.contracts import AdmittedRetryDisposition
from test_llm_generation_phase2 import case
from test_llm_generation_causal_refinement import _case as causal_case
from llm_generation.validation import derive_causal_support_proof


def test_result_replay_and_restart(case, tmp_path):
    path = tmp_path / "candidate_d.sqlite"
    store = CandidateDStore(path)
    assert store.initialize() is LocalReadStatus.FOUND
    result = ValidatedGenerationResult.from_content(case.content)
    assert store.result(result.validated_result_id).status is LocalReadStatus.NOT_FOUND
    assert store.commit_result(result).value == result
    assert store.commit_result(result).value == result
    reopened = CandidateDStore(path)
    assert reopened.result(result.validated_result_id).value == result
    assert reopened.replay(case.content.input).value == result
    recovery = reopened.recovery()
    assert recovery.status is LocalReadStatus.FOUND
    assert len(recovery.value.results) == 1
    assert recovery.value.results[0].semantic_commitment == result.semantic_commitment
    assert not recovery.value.failures


def test_contradiction_and_integrity(case, tmp_path):
    path = tmp_path / "candidate_d.sqlite"
    store = CandidateDStore(path)
    assert store.initialize() is LocalReadStatus.FOUND
    result = ValidatedGenerationResult.from_content(case.content)
    assert store.commit_result(result).status is LocalReadStatus.FOUND
    contradiction = ValidatedGenerationResult.from_content(
        replace(case.content, summary="Different supported summary")
    )
    assert store.commit_result(contradiction).status is LocalReadStatus.REPAIR_REQUIRED
    assert store.result(result.validated_result_id).value == result
    with sqlite3.connect(path) as db:
        db.execute("UPDATE authority SET commitment=? WHERE result_id=?",
                   ("0" * 64, result.validated_result_id))
    assert store.result(result.validated_result_id).status is LocalReadStatus.REPAIR_REQUIRED
    assert store.replay(case.content.input).status is LocalReadStatus.REPAIR_REQUIRED
    assert store.recovery().status is LocalReadStatus.REPAIR_REQUIRED


def test_failure_replay_and_no_try_authority(case, tmp_path):
    store = CandidateDStore(tmp_path / "candidate_d.sqlite")
    assert store.initialize() is LocalReadStatus.FOUND
    source = case.content.input
    failure = GenerationFailure(source.try_identity, source.operation_id,
                                FailureClass.PROVIDER_UNAVAILABLE,
                                AdmittedRetryDisposition.RETRYABLE, "provider unavailable")
    assert store.commit_failure(failure).value == failure
    assert CandidateDStore(store.path).failure(source.operation_id, source.try_identity).value == failure
    assert store.commit_failure(failure).value == failure
    changed = replace(failure, safe_detail="timeout")
    assert store.commit_failure(changed).status is LocalReadStatus.REPAIR_REQUIRED
    assert store.replay(source).status is LocalReadStatus.NOT_FOUND
    assert store.recovery().value.failures == (failure,)


def test_concurrent_writes(case, tmp_path):
    store = CandidateDStore(tmp_path / "candidate_d.sqlite")
    assert store.initialize() is LocalReadStatus.FOUND
    result = ValidatedGenerationResult.from_content(case.content)
    with ThreadPoolExecutor(max_workers=8) as pool:
        reads = list(pool.map(store.commit_result, [result] * 8))
    assert all(item.status is LocalReadStatus.FOUND and item.value == result for item in reads)
    other = ValidatedGenerationResult.from_content(replace(case.content, summary="Other summary"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = {item.status for item in pool.map(store.commit_result, [result, other])}
    assert statuses == {LocalReadStatus.FOUND, LocalReadStatus.REPAIR_REQUIRED}


def test_schema_and_provider_independence(case, tmp_path):
    path = tmp_path / "candidate_d.sqlite"
    store = CandidateDStore(path)
    assert store.initialize() is LocalReadStatus.FOUND
    # Provider failure is an invocation fact and does not alter local readiness.
    assert store.readiness() is LocalReadStatus.FOUND
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    assert store.initialize() is LocalReadStatus.REPAIR_REQUIRED
    assert store.readiness() is LocalReadStatus.REPAIR_REQUIRED
    assert store.result("missing").status is LocalReadStatus.REPAIR_REQUIRED


def test_schema_two_causal_round_trip(tmp_path):
    content, config, reader = causal_case()
    proof = derive_causal_support_proof(content, config, reader, "c3")
    result = ValidatedGenerationResult.from_content(replace(content, causal_proofs=(proof,)))
    store = CandidateDStore(tmp_path / "candidate_d.sqlite")
    assert store.initialize() is LocalReadStatus.FOUND
    assert store.commit_result(result).value == result
    reopened = CandidateDStore(store.path)
    restored = reopened.result(result.validated_result_id)
    assert restored.status is LocalReadStatus.FOUND
    assert restored.value.content.causal_proofs == (proof,)
    assert restored.value.content.claims[-1].causal_assertion == content.claims[-1].causal_assertion


def test_independent_identities_and_complete_enumeration(case, tmp_path):
    store = CandidateDStore(tmp_path / "candidate_d.sqlite")
    assert store.initialize() is LocalReadStatus.FOUND
    first = ValidatedGenerationResult.from_content(case.content)
    second_source = replace(case.content.input, operation_id="operation-2")
    second = ValidatedGenerationResult.from_content(replace(case.content, input=second_source))
    with ThreadPoolExecutor(max_workers=2) as pool:
        writes = list(pool.map(store.commit_result, (first, second)))
    assert all(item.status is LocalReadStatus.FOUND for item in writes)
    recovered = CandidateDStore(store.path).recovery()
    assert recovered.status is LocalReadStatus.FOUND
    assert {item.validated_result_id for item in recovered.value.results} == {
        first.validated_result_id, second.validated_result_id,
    }
    assert recovered.value == CandidateDStore(store.path).recovery().value


def test_result_failure_collision_is_repair_required(case, tmp_path):
    store = CandidateDStore(tmp_path / "candidate_d.sqlite")
    assert store.initialize() is LocalReadStatus.FOUND
    result = ValidatedGenerationResult.from_content(case.content)
    source = case.content.input
    failure = GenerationFailure(source.try_identity, source.operation_id,
                                FailureClass.PROVIDER_TIMEOUT,
                                AdmittedRetryDisposition.RETRYABLE, "timeout")
    assert store.commit_result(result).status is LocalReadStatus.FOUND
    assert store.commit_failure(failure).status is LocalReadStatus.REPAIR_REQUIRED
    assert store.recovery().value.failures == ()


def test_missing_unavailable_and_no_raw_payload(case, tmp_path):
    path = tmp_path / "candidate_d.sqlite"
    store = CandidateDStore(path)
    assert store.result("dvr_" + "a" * 64).status is LocalReadStatus.UNAVAILABLE
    assert store.initialize() is LocalReadStatus.FOUND
    assert store.result("dvr_" + "a" * 64).status is LocalReadStatus.NOT_FOUND
    assert store.result("absent").status is LocalReadStatus.INVALID
    result = ValidatedGenerationResult.from_content(case.content)
    assert store.commit_result(result).status is LocalReadStatus.FOUND
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(authority)")}
    assert columns == {"identity", "kind", "result_id", "commitment", "payload"}
    assert not {"raw_request", "raw_response", "authorization", "api_key"} & columns


def test_resource_readiness_and_secret_rejection(case, tmp_path, monkeypatch):
    path = tmp_path / "candidate_d.sqlite"
    monkeypatch.setenv("CANDIDATE_D_STORE_PATH", str(path))
    store = CandidateDStore.from_environment(resource_config_path=tmp_path / "missing-config.json")
    assert store.path == path
    assert store.initialize() is LocalReadStatus.REPAIR_REQUIRED
    assert store.recovery().status is LocalReadStatus.FOUND
    assert CandidateDStore(path).local_readiness() is LocalReadStatus.INVALID
    config = case.config
    raw = {
        "version": config.version, "provider": config.pin.provider, "model": config.pin.model,
        "profile": asdict(config.pin.profile), "prompt": asdict(config.pin.prompt),
        "result_schema": asdict(config.pin.result_schema),
        "configuration": asdict(config.pin.configuration), "bounds": asdict(config.bounds),
        "hidden_retries_disabled": config.hidden_retries_disabled,
        "prompt_template": config.prompt_template,
        "result_schema_commitment": config.result_schema_commitment,
        "configuration_commitment": config.configuration_commitment,
    }
    resource = tmp_path / "generation.json"
    resource.write_text(json.dumps(raw), encoding="utf-8")
    ready = CandidateDStore(path, resource_config_path=resource)
    assert ready.local_readiness() is LocalReadStatus.FOUND
    source = case.content.input
    with pytest.raises(ValueError):
        GenerationFailure(source.try_identity, source.operation_id,
                          FailureClass.PROVIDER_UNAVAILABLE,
                          AdmittedRetryDisposition.RETRYABLE,
                          "Authorization: Bearer secret-token")
    with pytest.raises(ValueError):
        GenerationFailure(source.try_identity, source.operation_id,
                          FailureClass.INVALID_INPUT,
                          AdmittedRetryDisposition.NON_RETRYABLE,
                          "scenario_id=S1")


def test_lineage_tamper_poisoning_is_store_wide(case, tmp_path):
    path = tmp_path / "candidate_d.sqlite"
    store = CandidateDStore(path)
    assert store.initialize() is LocalReadStatus.FOUND
    result = ValidatedGenerationResult.from_content(case.content)
    assert store.commit_result(result).status is LocalReadStatus.FOUND
    with sqlite3.connect(path) as db:
        db.execute("UPDATE authority SET identity=? WHERE result_id=?",
                   ('["other-operation","attempt-1",1]', result.validated_result_id))
    assert store.recovery().status is LocalReadStatus.REPAIR_REQUIRED
    assert store.result(result.validated_result_id).status is LocalReadStatus.REPAIR_REQUIRED
    assert store.result("dvr_" + "0" * 64).status is LocalReadStatus.REPAIR_REQUIRED


def test_physical_corruption_is_never_absence(tmp_path):
    path = tmp_path / "candidate_d.sqlite"
    path.write_bytes(b"not a SQLite database")
    store = CandidateDStore(path)
    assert store.readiness() in {LocalReadStatus.UNAVAILABLE, LocalReadStatus.REPAIR_REQUIRED}
    assert store.result("dvr_" + "a" * 64).status is not LocalReadStatus.NOT_FOUND
    assert store.recovery().status is not LocalReadStatus.FOUND


def test_interrupted_commit_rolls_back(case, tmp_path, monkeypatch):
    from llm_generation import sqlite_store as module

    path = tmp_path / "candidate_d.sqlite"
    store = CandidateDStore(path)
    assert store.initialize() is LocalReadStatus.FOUND
    result = ValidatedGenerationResult.from_content(case.content)
    original_connect = sqlite3.connect

    class FailingCommit(sqlite3.Connection):
        def commit(self):
            raise OSError("simulated local commit interruption")

    def interrupted_connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=FailingCommit)

    with monkeypatch.context() as patch:
        patch.setattr(module.sqlite3, "connect", interrupted_connect)
        assert store.commit_result(result).status is LocalReadStatus.UNAVAILABLE
    reopened = CandidateDStore(path)
    assert reopened.result(result.validated_result_id).status is LocalReadStatus.NOT_FOUND
    assert reopened.recovery().value.results == ()


def test_explicit_new_bootstrap_and_healthy_existing_store(case, tmp_path):
    path = tmp_path / "candidate_d.sqlite"
    store = CandidateDStore(path)
    assert not path.exists()
    assert store.initialize() is LocalReadStatus.FOUND
    result = ValidatedGenerationResult.from_content(case.content)
    assert store.commit_result(result).status is LocalReadStatus.FOUND
    reopened = CandidateDStore(path)
    assert reopened.initialize() is LocalReadStatus.FOUND
    assert reopened.result(result.validated_result_id).value == result


def test_preexisting_empty_files_never_bootstrap(tmp_path):
    zero = tmp_path / "zero.sqlite"
    zero.touch()
    store = CandidateDStore(zero)
    assert store.initialize() is LocalReadStatus.REPAIR_REQUIRED
    assert zero.stat().st_size == 0
    assert store.readiness() is not LocalReadStatus.FOUND
    assert store.recovery().status is not LocalReadStatus.FOUND
    assert store.result("dvr_" + "a" * 64).status is not LocalReadStatus.NOT_FOUND
    assert zero.stat().st_size == 0
    with sqlite3.connect(zero) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()

    empty_sqlite = tmp_path / "empty_container.sqlite"
    with sqlite3.connect(empty_sqlite) as db:
        db.execute("PRAGMA application_id=1")
    existing = CandidateDStore(empty_sqlite)
    assert existing.initialize() is LocalReadStatus.REPAIR_REQUIRED
    with sqlite3.connect(empty_sqlite) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("PRAGMA application_id").fetchone()[0] == 1
        assert not db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()


def test_truncated_authority_fails_closed_after_reopen(case, tmp_path):
    path = tmp_path / "candidate_d.sqlite"
    store = CandidateDStore(path)
    assert store.initialize() is LocalReadStatus.FOUND
    result = ValidatedGenerationResult.from_content(case.content)
    assert store.commit_result(result).status is LocalReadStatus.FOUND
    path.write_bytes(b"")

    reopened = CandidateDStore(path)
    assert reopened.initialize() is LocalReadStatus.REPAIR_REQUIRED
    assert path.stat().st_size == 0
    assert reopened.readiness() is not LocalReadStatus.FOUND
    assert reopened.local_readiness() is not LocalReadStatus.FOUND
    assert reopened.recovery().status is not LocalReadStatus.FOUND
    assert reopened.result(result.validated_result_id).status is not LocalReadStatus.NOT_FOUND
    assert reopened.failure(case.content.input.operation_id,
                            case.content.input.try_identity).status is not LocalReadStatus.NOT_FOUND
    assert path.stat().st_size == 0
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
