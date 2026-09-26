from __future__ import annotations

import sqlite3

import pytest

from incident_evidence import (
    CaptureFailure,
    CaptureTerminalKind,
    EvidenceFailureKind,
    EvidenceCompleteness,
    EvidenceSnapshot,
    EvidenceReadiness,
    EvidenceStoreError,
    EvidenceStoreIntegrityError,
    RetryDisposition,
    SqliteEvidenceStore,
    canonical_json,
)

from _incident_evidence_store_testkit import (
    bounds_facts,
    command,
    create_check_literal_case_schema,
    create_check_literal_whitespace_schema,
    create_old_schema,
    create_schema_with_trigger,
    create_schema_with_view,
    create_schema_with_sqlitex_table,
    create_schema_with_sqlitex_trigger,
    create_schema_with_sqlitex_view,
    create_weakened_check_schema,
    success,
)


def test_new_store_current_schema_reopen_and_physical_separation(tmp_path):
    evidence_path = tmp_path / "candidate-b.sqlite"
    other_path = tmp_path / "incident.sqlite"
    sqlite3.connect(other_path).close()

    store = SqliteEvidenceStore(evidence_path)
    assert store.validate_local_readiness() is EvidenceReadiness.READY
    store.commit_success(success())
    store.close()

    reopened = SqliteEvidenceStore(evidence_path)
    assert reopened.read_capture_outcome("capture-1").terminal_kind is CaptureTerminalKind.SUCCESS
    assert evidence_path != other_path
    assert sqlite3.connect(other_path).execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0] == 0
    reopened.close()


def test_explicit_path_is_required(tmp_path):
    with pytest.raises(ValueError):
        SqliteEvidenceStore("")
    with pytest.raises(ValueError):
        SqliteEvidenceStore(":memory:")


def test_recognized_old_schema_requires_migration(tmp_path):
    path = tmp_path / "old.sqlite"
    create_old_schema(path)
    with pytest.raises(EvidenceStoreError) as caught:
        SqliteEvidenceStore(path)
    assert caught.value.kind is EvidenceFailureKind.MIGRATION_REQUIRED


@pytest.mark.parametrize("foreign", [False, True])
def test_unknown_or_malformed_schema_fails_closed(tmp_path, foreign):
    path = tmp_path / "unsafe.sqlite"
    connection = sqlite3.connect(path)
    if foreign:
        connection.execute("CREATE TABLE incidents(incident_id TEXT PRIMARY KEY)")
    else:
        connection.execute("CREATE TABLE evidence_store_metadata(wrong TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(EvidenceStoreIntegrityError) as caught:
        SqliteEvidenceStore(path)
    assert caught.value.kind is EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA


def test_same_check_count_with_weakened_semantics_fails_closed(tmp_path):
    path = tmp_path / "weakened-check.sqlite"
    create_weakened_check_schema(path)
    with pytest.raises(EvidenceStoreIntegrityError) as caught:
        SqliteEvidenceStore(path)
    assert caught.value.kind is EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA


@pytest.mark.parametrize(
    "fixture",
    [create_check_literal_case_schema, create_check_literal_whitespace_schema],
)
def test_check_literal_semantic_difference_fails_closed(tmp_path, fixture):
    path = tmp_path / f"{fixture.__name__}.sqlite"
    fixture(path)
    with pytest.raises(EvidenceStoreIntegrityError) as caught:
        SqliteEvidenceStore(path)
    assert caught.value.kind is EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA


@pytest.mark.parametrize("fixture", [create_schema_with_trigger, create_schema_with_view])
def test_unexpected_schema_object_fails_closed(tmp_path, fixture):
    path = tmp_path / f"{fixture.__name__}.sqlite"
    fixture(path)
    with pytest.raises(EvidenceStoreIntegrityError) as caught:
        SqliteEvidenceStore(path)
    assert caught.value.kind is EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA


@pytest.mark.parametrize(
    "fixture",
    [
        create_schema_with_sqlitex_trigger,
        create_schema_with_sqlitex_view,
        create_schema_with_sqlitex_table,
    ],
)
def test_sqlitex_user_object_is_not_misclassified_as_internal(tmp_path, fixture):
    path = tmp_path / f"{fixture.__name__}.sqlite"
    fixture(path)
    with pytest.raises(EvidenceStoreIntegrityError) as caught:
        SqliteEvidenceStore(path)
    assert caught.value.kind is EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA


def test_real_sqlite_internal_objects_do_not_break_reopen(tmp_path):
    path = tmp_path / "sqlite-internal.sqlite"
    SqliteEvidenceStore(path).close()
    connection = sqlite3.connect(path)
    try:
        internal_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE substr(name, 1, 7) = 'sqlite_'"
            )
        }
    finally:
        connection.close()
    assert any(name.startswith("sqlite_autoindex_") for name in internal_names)
    reopened = SqliteEvidenceStore(path)
    assert reopened.validate_local_readiness() is EvidenceReadiness.READY
    reopened.close()


def test_atomic_success_and_revision_reuse(tmp_path):
    store = SqliteEvidenceStore(tmp_path / "evidence.sqlite")
    first = store.commit_success(success("capture-1"))
    second = store.commit_success(success("capture-2"))
    assert first.snapshot_id != second.snapshot_id
    assert first.revision_id == second.revision_id
    assert store.resolve_snapshot(first.snapshot_id).revision_id == first.revision_id
    assert store.resolve_revision(first.revision_id).incident_id == "incident-1"


def test_atomic_failure_has_no_snapshot(tmp_path):
    store = SqliteEvidenceStore(tmp_path / "evidence.sqlite")
    cmd = command("capture-failed")
    failure = CaptureFailure(
        cmd.capture_operation_id,
        EvidenceFailureKind.SOURCE_INVALID,
        RetryDisposition.NON_RETRYABLE,
        "source response failed validation",
    )
    outcome = store.commit_failure(cmd, failure, safe_provenance=("shape mismatch",))
    assert outcome.terminal_kind is CaptureTerminalKind.FAILURE
    assert outcome.failure == failure
    assert store.enumerate_recovery_facts().snapshots == ()


def test_revision_same_id_different_content_is_rejected_before_store():
    valid = success().revision
    contradictory_content = valid.semantic_content
    contradictory_content["events"][0]["value"] = "different"
    with pytest.raises(ValueError, match="revision_id contradicts"):
        type(valid)(
            valid.revision_id,
            valid.incident_id,
            valid.canonicalization_version,
            canonical_json(contradictory_content),
            valid.integrity_identity,
        )


def _rebuild_snapshot(base, content):
    return EvidenceSnapshot.from_content(
        base.command,
        revision_id=base.revision.revision_id,
        completeness=base.snapshot.completeness,
        source_statuses=base.snapshot.source_statuses,
        snapshot_content=content,
    )


def test_empty_required_provenance_is_rejected():
    base = success()
    content = base.snapshot.snapshot_content
    content["provenance"]["LOKI"] = {}
    with pytest.raises(ValueError, match="missing required facts"):
        _rebuild_snapshot(base, content)


def test_malformed_query_provenance_is_rejected():
    base = success()
    content = base.snapshot.snapshot_content
    del content["provenance"]["LOKI"]["query"]["query_version"]
    with pytest.raises(ValueError, match="query.*missing required facts"):
        _rebuild_snapshot(base, content)


def test_missing_required_bounds_fact_is_rejected():
    base = success()
    content = base.snapshot.snapshot_content
    del content["bounds"]["LOKI"]["sampling_applied"]
    with pytest.raises(ValueError, match="bounds.*missing required facts"):
        _rebuild_snapshot(base, content)


@pytest.mark.parametrize(
    "changes",
    [
        {
            "observed_candidate_count": 2,
            "included_count": 1,
            "omitted_count": 1,
            "omission_reason": "unexplained loss",
        },
        {
            "sampling_applied": True,
            "sampling_policy_identity": "sample-v1",
        },
        {
            "truncation_applied": True,
            "truncation_reason": "record limit",
        },
        {
            "aggregation_applied": True,
            "aggregation_rule_identity": "aggregate-v1",
            "aggregation_lossy": True,
        },
    ],
)
def test_full_rejects_completeness_affecting_loss(changes):
    base = success()
    content = base.snapshot.snapshot_content
    content["bounds"]["LOKI"].update(changes)
    with pytest.raises(ValueError):
        _rebuild_snapshot(base, content)


def test_full_allows_proven_lossless_dedup(tmp_path):
    capture = success(
        loki_bounds=bounds_facts(
            observed_candidate_count=2,
            included_count=1,
            omitted_count=1,
            omission_reason="canonical duplicate",
            dedup_applied=True,
            dedup_input_count=2,
            dedup_output_count=1,
            completeness_impact=EvidenceCompleteness.FULL,
        )
    )
    outcome = SqliteEvidenceStore(tmp_path / "lossless.sqlite").commit_success(capture)
    assert outcome.revision_id == capture.revision.revision_id


def test_degraded_requires_persisted_basis():
    with pytest.raises(ValueError, match="requires a persisted degradation basis"):
        success(completeness=EvidenceCompleteness.DEGRADED)


def test_degraded_with_completeness_affecting_omission_is_allowed(tmp_path):
    capture = success(
        completeness=EvidenceCompleteness.DEGRADED,
        loki_bounds=bounds_facts(
            observed_candidate_count=2,
            included_count=1,
            omitted_count=1,
            omission_reason="record limit",
            truncation_applied=True,
            truncation_reason="record limit",
            completeness_impact=EvidenceCompleteness.DEGRADED,
        ),
    )
    outcome = SqliteEvidenceStore(tmp_path / "degraded.sqlite").commit_success(capture)
    assert outcome.snapshot_id == capture.snapshot.snapshot_id
