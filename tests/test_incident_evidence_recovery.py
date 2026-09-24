from __future__ import annotations

import pytest

from incident_evidence import (
    EvidenceIntegrityStatus,
    EvidenceReadiness,
    EvidenceStoreIntegrityError,
    MaterialityEvaluationKind,
    MaterialityRequest,
    SqliteEvidenceStore,
    compare_materiality,
    enumerate_recovery_facts,
)

from _incident_evidence_store_testkit import execute_sql, success


def test_recovery_enumerates_capture_bindings_materiality_and_boundary_facts(tmp_path):
    path = tmp_path / "recovery.sqlite"
    store = SqliteEvidenceStore(path)
    outcome = store.commit_success(success())
    materiality = compare_materiality(
        store,
        MaterialityRequest(
            MaterialityEvaluationKind.NO_BASELINE,
            outcome.revision_id,
            "evidence-materiality-v1",
        ),
    )
    facts = enumerate_recovery_facts(store)
    assert facts.capture_outcomes == (outcome,)
    assert facts.snapshots[0].revision_id == facts.revisions[0].revision_id
    assert facts.materiality_results == (materiality,)
    assert facts.snapshots[0].snapshot_content["episode"]
    assert facts.snapshots[0].snapshot_content["windows"]
    assert facts.snapshots[0].snapshot_content["post_context"]
    assert facts.integrity_status is EvidenceIntegrityStatus.VALID
    assert facts.local_readiness is EvidenceReadiness.READY
    assert facts.repair_findings == ()


def test_corrupt_recovery_enumeration_fails_closed_without_partial_result(tmp_path):
    path = tmp_path / "corrupt.sqlite"
    store = SqliteEvidenceStore(path)
    store.commit_success(success())
    store.close()
    execute_sql(
        path,
        "UPDATE evidence_revisions SET canonical_semantic_content = ?",
        ("{}",),
    )
    with pytest.raises(EvidenceStoreIntegrityError):
        SqliteEvidenceStore(path)
