from __future__ import annotations

from dataclasses import replace

import pytest

from incident_evidence import (
    CaptureFailure,
    EvidenceFailureKind,
    EvidenceStoreError,
    RetryDisposition,
    MaterialityEvaluationKind,
    MaterialityRequest,
    MaterialityResult,
    SqliteEvidenceStore,
    semantic_identity,
)

from _incident_evidence_store_testkit import command, success


def test_equivalent_replay_returns_original_result(tmp_path):
    store = SqliteEvidenceStore(tmp_path / "evidence.sqlite")
    original = store.commit_success(success())
    assert store.commit_success(success()) == original


def test_same_operation_different_semantics_fails_closed(tmp_path):
    store = SqliteEvidenceStore(tmp_path / "evidence.sqlite")
    store.commit_success(success())
    contradictory = success()
    contradictory_command = replace(command(), config_identity="config-v2")
    contradictory_snapshot = replace(contradictory.snapshot, config_identity="config-v2")
    contradictory = type(contradictory)(
        contradictory_command, contradictory_snapshot, contradictory.revision
    )
    with pytest.raises(EvidenceStoreError) as caught:
        store.commit_success(contradictory)
    assert caught.value.kind is EvidenceFailureKind.CONTRADICTORY_REPLAY


@pytest.mark.parametrize("point", ["before_transaction", "during_transaction"])
def test_precommit_and_during_transaction_faults_leave_no_authority(tmp_path, point):
    path = tmp_path / f"{point}.sqlite"

    def inject(actual):
        if actual == point:
            raise RuntimeError("controlled crash")

    store = SqliteEvidenceStore(path, fault_injector=inject)
    with pytest.raises(RuntimeError, match="controlled crash"):
        store.commit_success(success())
    store.close()
    reopened = SqliteEvidenceStore(path)
    assert reopened.read_capture_outcome("capture-1") is None
    assert reopened.enumerate_recovery_facts().snapshots == ()


def test_postcommit_response_loss_replays_original_result(tmp_path):
    path = tmp_path / "response-loss.sqlite"

    def inject(point):
        if point == "after_commit":
            raise RuntimeError("response lost")

    store = SqliteEvidenceStore(path, fault_injector=inject)
    with pytest.raises(RuntimeError, match="response lost"):
        store.commit_success(success())
    store.close()
    reopened = SqliteEvidenceStore(path)
    expected = reopened.read_capture_outcome("capture-1")
    assert reopened.commit_success(success()) == expected


def test_close_reopen_recovery_and_materiality_persistence(tmp_path):
    path = tmp_path / "recovery.sqlite"
    store = SqliteEvidenceStore(path)
    committed = store.commit_success(success())
    request = MaterialityRequest(
        MaterialityEvaluationKind.NO_BASELINE,
        committed.revision_id,
        "materiality-v1",
    )
    result = MaterialityResult(
        semantic_identity("spec013-materiality-result", request),
        request,
        None,
        ("initial evidence revision",),
    )
    assert store.commit_materiality_result(result) == result
    assert store.commit_materiality_result(result) == result
    store.close()
    reopened = SqliteEvidenceStore(path)
    facts = reopened.enumerate_recovery_facts()
    assert facts.capture_outcomes == (committed,)
    assert len(facts.snapshots) == len(facts.revisions) == len(facts.materiality_results) == 1
    assert reopened.read_materiality_result_for_request(request) == result


def test_contradictory_materiality_result_fails_closed(tmp_path):
    store = SqliteEvidenceStore(tmp_path / "materiality.sqlite")
    revision_id = store.commit_success(success()).revision_id
    request = MaterialityRequest(MaterialityEvaluationKind.NO_BASELINE, revision_id, "rule-v1")
    first = MaterialityResult("result-1", request, None, ("initial",))
    store.commit_materiality_result(first)
    with pytest.raises(EvidenceStoreError) as caught:
        store.commit_materiality_result(MaterialityResult("result-2", request, None, ("different",)))
    assert caught.value.kind is EvidenceFailureKind.CONTRADICTORY_REPLAY


@pytest.mark.parametrize(
    "provenance",
    [
        tuple(f"fact-{index}" for index in range(17)),
        tuple("x" * 300 for _ in range(16)),
    ],
)
def test_failure_provenance_collection_bound_is_enforced(tmp_path, provenance):
    store = SqliteEvidenceStore(tmp_path / "bounded-failure.sqlite")
    cmd = command("bounded-failure")
    failure = CaptureFailure(
        cmd.capture_operation_id,
        EvidenceFailureKind.SOURCE_INVALID,
        RetryDisposition.NON_RETRYABLE,
        "invalid source response",
    )
    with pytest.raises(ValueError, match="safe_provenance"):
        store.commit_failure(cmd, failure, safe_provenance=provenance)
    assert store.read_capture_outcome(cmd.capture_operation_id) is None
