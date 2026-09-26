from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from incident_evidence import (
    MaterialityEvaluationKind,
    MaterialityRequest,
    SqliteEvidenceStore,
    compare_materiality,
)

from _incident_evidence_store_testkit import success


RULE = "evidence-materiality-v1"


def _seed(path):
    store = SqliteEvidenceStore(path)
    baseline = store.commit_success(success("capture-a", evidence="A")).revision_id
    candidate = store.commit_success(success("capture-b", evidence="B")).revision_id
    store.close()
    return MaterialityRequest(
        MaterialityEvaluationKind.PAIRWISE, candidate, RULE, baseline
    )


def test_equivalent_replay_and_restart_return_same_durable_result(tmp_path):
    path = tmp_path / "replay.sqlite"
    request = _seed(path)
    store = SqliteEvidenceStore(path)
    first = compare_materiality(store, request)
    assert compare_materiality(store, request) == first
    store.close()
    reopened = SqliteEvidenceStore(path)
    assert compare_materiality(reopened, request) == first
    assert reopened.read_materiality_result(first.materiality_result_id) == first


def test_materiality_postcommit_response_loss_replays_original(tmp_path):
    path = tmp_path / "response-loss.sqlite"
    request = _seed(path)

    def inject(point):
        if point == "after_commit":
            raise RuntimeError("response lost")

    store = SqliteEvidenceStore(path, fault_injector=inject)
    with pytest.raises(RuntimeError, match="response lost"):
        compare_materiality(store, request)
    store.close()
    reopened = SqliteEvidenceStore(path)
    persisted = reopened.read_materiality_result_for_request(request)
    assert persisted is not None
    assert compare_materiality(reopened, request) == persisted


def test_concurrent_equivalent_materiality_requests_converge(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    request = _seed(path)
    barrier = Barrier(2)

    def evaluate(_):
        store = SqliteEvidenceStore(path)
        barrier.wait()
        try:
            return compare_materiality(store, request)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(evaluate, range(2)))
    assert results[0] == results[1]
    assert len(SqliteEvidenceStore(path).enumerate_recovery_facts().materiality_results) == 1
