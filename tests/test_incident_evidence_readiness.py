from __future__ import annotations

import pytest

from incident_evidence import (
    EvidenceReadiness,
    EvidenceStoreIntegrityError,
    SqliteEvidenceStore,
    validate_local_readiness,
)

from _incident_evidence_store_testkit import execute_sql, success


def test_readiness_is_candidate_b_local_only(tmp_path):
    store = SqliteEvidenceStore(tmp_path / "ready.sqlite")
    store.commit_success(success())
    assert validate_local_readiness(store) is EvidenceReadiness.READY
    assert set(EvidenceReadiness) == {EvidenceReadiness.READY}


def test_incomplete_or_corrupt_local_authority_is_not_ready(tmp_path):
    path = tmp_path / "not-ready.sqlite"
    store = SqliteEvidenceStore(path)
    store.commit_success(success())
    store.close()
    execute_sql(path, "DELETE FROM snapshot_revision_bindings")
    with pytest.raises(EvidenceStoreIntegrityError):
        SqliteEvidenceStore(path)
