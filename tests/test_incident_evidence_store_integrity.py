from __future__ import annotations

import sqlite3

import pytest

from incident_evidence import (
    EvidenceFailureKind,
    EvidenceIntegrityStatus,
    EvidenceStoreIntegrityError,
    SqliteEvidenceStore,
)

from _incident_evidence_store_testkit import execute_sql, success


def test_integrity_validates_all_required_bindings(tmp_path):
    path = tmp_path / "evidence.sqlite"
    store = SqliteEvidenceStore(path)
    store.commit_success(success())
    assert store.validate_integrity() is EvidenceIntegrityStatus.VALID


def test_not_found_only_when_authority_is_reliable(tmp_path):
    path = tmp_path / "evidence.sqlite"
    store = SqliteEvidenceStore(path)
    assert store.read_capture_outcome("absent") is None
    store.commit_success(success())
    store.close()
    execute_sql(path, "DELETE FROM snapshot_revision_bindings")
    corrupt = SqliteEvidenceStore
    with pytest.raises(EvidenceStoreIntegrityError):
        corrupt(path)


def test_dangling_or_contradictory_state_is_not_silently_skipped(tmp_path):
    path = tmp_path / "evidence.sqlite"
    store = SqliteEvidenceStore(path)
    stored = store.commit_success(success())
    store.close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "UPDATE snapshot_revision_bindings SET revision_id='missing' WHERE snapshot_id=?",
        (stored.snapshot_id,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(EvidenceStoreIntegrityError) as caught:
        SqliteEvidenceStore(path)
    assert caught.value.kind in {
        EvidenceFailureKind.EVIDENCE_STORE_INTEGRITY_FAILURE,
        EvidenceFailureKind.DANGLING_EVIDENCE_REFERENCE,
    }


def test_failure_operation_with_snapshot_is_corruption(tmp_path):
    path = tmp_path / "evidence.sqlite"
    store = SqliteEvidenceStore(path)
    store.commit_success(success())
    store.close()
    execute_sql(
        path,
        """UPDATE capture_results
              SET terminal_kind='FAILURE', snapshot_id=NULL, revision_id=NULL,
                  failure_kind='SOURCE_INVALID', retry_disposition='NON_RETRYABLE',
                  failure_summary='invalid source', failure_provenance_json='[]'
            WHERE capture_operation_id='capture-1'""",
    )
    with pytest.raises(EvidenceStoreIntegrityError):
        SqliteEvidenceStore(path)


def test_snapshot_content_mutation_is_detected(tmp_path):
    path = tmp_path / "immutable.sqlite"
    store = SqliteEvidenceStore(path)
    store.commit_success(success())
    store.close()
    execute_sql(
        path,
        "UPDATE evidence_snapshots SET canonical_snapshot_content=?",
        ('{"changed":true}',),
    )
    with pytest.raises(EvidenceStoreIntegrityError):
        SqliteEvidenceStore(path)
