import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from rca_persistence import (
    AdmitAttemptRequest,
    AdmittedRetryDisposition,
    AttemptLineage,
    CreateAggregateRequest,
    GenerationLifecycle,
    GenerationProvenance,
    LogicalTryIdentity,
    LogicalTryOutcome,
    LogicalTryResultKind,
    RcaDomainError,
    RcaErrorCode,
    SqliteRcaStore,
)


NOW = datetime(2026, 9, 20, 8, 30, tzinfo=timezone.utc)


def _seed(store: SqliteRcaStore, attempt_id: str = "ATT-1") -> None:
    if store.get_aggregate("AGG-1") is None:
        store.create_or_discover_aggregate(
            CreateAggregateRequest("OP-AGG", "AGG-1", "INC-1", NOW)
        )
    lineage = AttemptLineage(
        attempt_id,
        "AGG-1",
        "ES-1",
        "ER-1",
        "KS-1",
        GenerationProvenance("provider", "model", "prompt", "config", "profile"),
    )
    store.admit_attempt(AdmitAttemptRequest(f"OP-{attempt_id}", lineage, NOW))


def _failure(
    ordinal: int = 1, *, attempt_id: str = "ATT-1", code: str = "TIMEOUT"
) -> LogicalTryOutcome:
    return LogicalTryOutcome(
        LogicalTryIdentity(attempt_id, ordinal),
        LogicalTryResultKind.FAILURE,
        AdmittedRetryDisposition.RETRYABLE,
        NOW + timedelta(minutes=ordinal),
        failure_code=code,
        safe_failure_message="provider result was not authoritative",
    )


def _success(ordinal: int = 1, *, attempt_id: str = "ATT-1") -> LogicalTryOutcome:
    return LogicalTryOutcome(
        LogicalTryIdentity(attempt_id, ordinal),
        LogicalTryResultKind.VALIDATED_RESULT,
        AdmittedRetryDisposition.NON_RETRYABLE,
        NOW + timedelta(minutes=ordinal),
        validated_result_id=f"VALID-{attempt_id}-{ordinal}",
    )


def test_success_and_failure_outcomes_atomically_drive_lifecycle_summary(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        _seed(store)
        _seed(store, "ATT-2")

        assert store.record_try_outcome("OP-TRY-1", _failure()) == _failure()
        failed = store.get_attempt_lineage("ATT-1")
        assert failed is not None
        assert failed.attempt.lifecycle is GenerationLifecycle.FAILED
        assert failed.attempt.latest_try_ordinal == 1
        assert failed.try_outcomes == (_failure(),)

        store.record_try_outcome("OP-TRY-2", _success(attempt_id="ATT-2"))
        completed = store.get_attempt_lineage("ATT-2")
        assert completed is not None
        assert completed.attempt.lifecycle is GenerationLifecycle.COMPLETED
        assert completed.try_outcomes == (_success(attempt_id="ATT-2"),)


def test_same_try_equivalent_replay_returns_authority_without_duplicate(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        _seed(store)
        original = store.record_try_outcome("OP-TRY-1", _failure())
        assert store.record_try_outcome("OP-TRY-1", _failure()) is not None
        assert store.record_try_outcome("OP-TRY-EQUIVALENT", _failure()) == original

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rca_operation_receipts WHERE command_kind='RECORD_TRY'"
        ).fetchone() == (1,)


def test_same_try_contradiction_and_same_operation_contradiction_fail_closed(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        _seed(store)
        store.record_try_outcome("OP-TRY-1", _failure())

        with pytest.raises(RcaDomainError) as same_try:
            store.record_try_outcome("OP-TRY-2", _failure(code="OTHER"))
        assert same_try.value.code is RcaErrorCode.IDENTITY_LINEAGE_CONFLICT

        with pytest.raises(RcaDomainError) as same_operation:
            store.record_try_outcome("OP-TRY-1", _failure(code="OTHER"))
        assert same_operation.value.code is RcaErrorCode.RECEIPT_REPLAY_CONFLICT


def test_attempt_must_exist_and_try_ordinals_cannot_gap_or_follow_success(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        with pytest.raises(RcaDomainError) as missing:
            store.record_try_outcome("OP-MISSING", _failure())
        assert missing.value.code is RcaErrorCode.INVALID_REFERENCE

        _seed(store)
        with pytest.raises(RcaDomainError) as gap:
            store.record_try_outcome("OP-GAP", _failure(2))
        assert gap.value.code is RcaErrorCode.SEMANTIC_CONFLICT

        store.record_try_outcome("OP-SUCCESS", _success())
        with pytest.raises(RcaDomainError) as after_success:
            store.record_try_outcome("OP-AFTER", _failure(2))
        assert after_success.value.code is RcaErrorCode.SEMANTIC_CONFLICT


def test_next_ordinal_respects_candidate_d_retry_disposition_without_scheduling(tmp_path) -> None:
    terminal_failure = LogicalTryOutcome(
        LogicalTryIdentity("ATT-1", 1),
        LogicalTryResultKind.FAILURE,
        AdmittedRetryDisposition.NON_RETRYABLE,
        NOW,
        failure_code="VALIDATION_REJECTED",
    )
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        _seed(store)
        store.record_try_outcome("OP-TRY-1", terminal_failure)
        with pytest.raises(RcaDomainError) as rejected:
            store.record_try_outcome("OP-TRY-2", _failure(2))
        assert rejected.value.code is RcaErrorCode.SEMANTIC_CONFLICT


def test_failed_try_allocates_no_version_authority_or_runtime_retry_state(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        _seed(store)
        store.record_try_outcome("OP-TRY-1", _failure())
        public_methods = set(SqliteRcaStore.__dict__)
        assert not any(
            fragment in method
            for method in public_methods
            for fragment in ("schedule", "wake", "retry_budget", "invoke_provider")
        )

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert not any("version" in table for table in tables)
        columns = {
            row[1]
            for table in tables
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        assert "version_number" not in columns


def test_logical_try_identity_has_no_physical_invocation_authority() -> None:
    identity = LogicalTryIdentity("ATT-1", 1)
    outcome = _failure()
    assert not hasattr(identity, "physical_invocation_id")
    assert not hasattr(outcome, "physical_invocation_count")
