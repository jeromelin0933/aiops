from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from incident_evidence import EvidenceFailureKind, EvidenceStoreError, SqliteEvidenceStore

from _incident_evidence_store_testkit import success


def test_concurrent_empty_store_initialization_converges(tmp_path):
    path = tmp_path / "initialize.sqlite"
    barrier = Barrier(2)

    def initialize(_):
        barrier.wait()
        store = SqliteEvidenceStore(path)
        store.close()
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(initialize, range(2))) == [True, True]


def test_concurrent_equivalent_commits_converge_across_connections(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    SqliteEvidenceStore(path).close()
    barrier = Barrier(2)

    def commit():
        store = SqliteEvidenceStore(path)
        barrier.wait()
        try:
            return store.commit_success(success())
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: commit(), range(2)))
    assert results[0] == results[1]
    facts = SqliteEvidenceStore(path).enumerate_recovery_facts()
    assert len(facts.capture_outcomes) == len(facts.snapshots) == len(facts.revisions) == 1


def test_concurrent_contradictory_commands_fail_closed(tmp_path):
    path = tmp_path / "contradictory.sqlite"
    SqliteEvidenceStore(path).close()
    barrier = Barrier(2)
    candidates = [success(), success()]
    changed_command = replace(candidates[1].command, config_identity="config-v2")
    changed_snapshot = replace(candidates[1].snapshot, config_identity="config-v2")
    candidates[1] = type(candidates[1])(changed_command, changed_snapshot, candidates[1].revision)

    def commit(candidate):
        store = SqliteEvidenceStore(path)
        barrier.wait()
        try:
            return store.commit_success(candidate)
        except EvidenceStoreError as exc:
            return exc
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, candidates))
    errors = [result for result in results if isinstance(result, EvidenceStoreError)]
    assert len(errors) == 1
    assert errors[0].kind is EvidenceFailureKind.CONTRADICTORY_REPLAY
    facts = SqliteEvidenceStore(path).enumerate_recovery_facts()
    assert len(facts.capture_outcomes) == len(facts.snapshots) == 1
