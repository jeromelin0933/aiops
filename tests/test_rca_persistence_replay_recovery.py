import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

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


def _outcome(ordinal: int, code: str = "TIMEOUT") -> LogicalTryOutcome:
    return LogicalTryOutcome(
        LogicalTryIdentity("ATT-1", ordinal),
        LogicalTryResultKind.FAILURE,
        AdmittedRetryDisposition.RETRYABLE,
        NOW,
        failure_code=code,
    )


def _create_database(database) -> None:
    with SqliteRcaStore(database) as store:
        store.create_or_discover_aggregate(
            CreateAggregateRequest("OP-AGG", "AGG-1", "INC-1", NOW)
        )
        lineage = AttemptLineage(
            "ATT-1",
            "AGG-1",
            "ES-1",
            "ER-1",
            "KS-1",
            GenerationProvenance("provider", "model", "prompt", "config", "profile"),
        )
        store.admit_attempt(AdmitAttemptRequest("OP-ATT", lineage, NOW))


def _create_two_try_history(database) -> None:
    _create_database(database)
    with SqliteRcaStore(database) as store:
        store.record_try_outcome("OP-TRY-1", _outcome(1))
        store.record_try_outcome("OP-TRY-2", _outcome(2))


def _corrupt_first_outcome(database, corruption: str) -> None:
    with sqlite3.connect(database) as connection:
        encoded = connection.execute(
            "SELECT semantic_identity FROM rca_operation_receipts WHERE operation_id='OP-TRY-1'"
        ).fetchone()[0]
        payload = json.loads(encoded)
        outcome = payload["outcome"]
        if corruption == "validated_result":
            outcome["result_kind"] = "VALIDATED_RESULT"
            outcome["retry_disposition"] = "NON_RETRYABLE"
            outcome["validated_result_id"] = "VALID-1"
            outcome["failure_code"] = None
            outcome["safe_failure_message"] = None
        else:
            outcome["retry_disposition"] = corruption
        connection.execute(
            "UPDATE rca_operation_receipts SET semantic_identity=? WHERE operation_id='OP-TRY-1'",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        )


def test_restart_and_response_loss_replay_preserve_try_history_and_lifecycle(tmp_path) -> None:
    database = tmp_path / "rca.db"
    _create_database(database)
    with SqliteRcaStore(database) as store:
        expected = store.record_try_outcome("OP-TRY-1", _outcome(1))

    with SqliteRcaStore(database) as reopened:
        view = reopened.get_attempt_lineage("ATT-1")
        assert view is not None
        assert view.try_outcomes == (expected,)
        assert view.attempt.lifecycle is GenerationLifecycle.FAILED
        assert reopened.record_try_outcome("OP-TRY-1", _outcome(1)) == expected


@pytest.mark.parametrize("corruption", ["lifecycle", "outcome"])
def test_corrupt_try_or_lifecycle_contradiction_is_not_not_found(tmp_path, corruption) -> None:
    database = tmp_path / f"{corruption}.db"
    _create_database(database)
    with SqliteRcaStore(database) as store:
        store.record_try_outcome("OP-TRY-1", _outcome(1))

    with sqlite3.connect(database) as connection:
        if corruption == "lifecycle":
            connection.execute(
                "UPDATE rca_attempts SET lifecycle='PENDING' WHERE attempt_id='ATT-1'"
            )
        else:
            encoded = connection.execute(
                "SELECT semantic_identity FROM rca_operation_receipts WHERE operation_id='OP-TRY-1'"
            ).fetchone()[0]
            payload = json.loads(encoded)
            payload["outcome"]["failure_code"] = None
            connection.execute(
                "UPDATE rca_operation_receipts SET semantic_identity=? WHERE operation_id='OP-TRY-1'",
                (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
            )

    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    assert raised.value.code is not RcaErrorCode.NOT_FOUND


def test_orphan_try_history_is_corruption_not_attempt_absence(tmp_path) -> None:
    database = tmp_path / "orphan.db"
    _create_database(database)
    store = SqliteRcaStore(database)
    store.record_try_outcome("OP-TRY-1", _outcome(1))
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM rca_attempts WHERE attempt_id='ATT-1'")

    with pytest.raises(RcaDomainError) as raised:
        store.get_attempt_lineage("ATT-1")
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    store.close()


@pytest.mark.parametrize(
    "corruption", ["validated_result", "NON_RETRYABLE", "REPAIR_REQUIRED"]
)
def test_terminal_outcome_followed_by_later_try_fails_closed_after_reopen(
    tmp_path, corruption
) -> None:
    database = tmp_path / f"terminal-{corruption}.db"
    _create_two_try_history(database)
    _corrupt_first_outcome(database, corruption)

    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION


def test_terminal_history_contradiction_fails_authoritative_read_and_readiness(
    tmp_path,
) -> None:
    database = tmp_path / "live-corruption.db"
    _create_two_try_history(database)
    store = SqliteRcaStore(database)
    _corrupt_first_outcome(database, "NON_RETRYABLE")

    with pytest.raises(RcaDomainError) as read_error:
        store.get_attempt_lineage("ATT-1")
    assert read_error.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    with pytest.raises(RcaDomainError) as readiness_error:
        store.validate_local_readiness()
    assert readiness_error.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    store.close()


def test_legal_retry_history_remains_coherent_after_reopen(tmp_path) -> None:
    database = tmp_path / "legal-retry.db"
    _create_two_try_history(database)

    with SqliteRcaStore(database) as reopened:
        reopened.validate_local_readiness()
        view = reopened.get_attempt_lineage("ATT-1")
        assert view is not None
        assert view.try_outcomes == (_outcome(1), _outcome(2))
        assert view.attempt.lifecycle is GenerationLifecycle.FAILED


def test_concurrent_equivalent_same_try_race_has_one_authoritative_outcome(tmp_path) -> None:
    database = tmp_path / "same.db"
    _create_database(database)
    barrier = Barrier(2)

    def record(operation_id: str):
        with SqliteRcaStore(database) as store:
            barrier.wait()
            return store.record_try_outcome(operation_id, _outcome(1))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(record, ["OP-TRY-A", "OP-TRY-B"]))

    assert results == [_outcome(1), _outcome(1)]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rca_operation_receipts WHERE command_kind='RECORD_TRY'"
        ).fetchone() == (1,)


def test_concurrent_next_ordinal_race_serializes_to_one_outcome(tmp_path) -> None:
    database = tmp_path / "next.db"
    _create_database(database)
    with SqliteRcaStore(database) as store:
        store.record_try_outcome("OP-TRY-1", _outcome(1))
    barrier = Barrier(2)

    def record(operation_id: str, code: str):
        with SqliteRcaStore(database) as store:
            barrier.wait()
            try:
                return store.record_try_outcome(operation_id, _outcome(2, code))
            except RcaDomainError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: record(*args),
                [("OP-TRY-2A", "TIMEOUT-A"), ("OP-TRY-2B", "TIMEOUT-B")],
            )
        )

    assert sum(isinstance(item, LogicalTryOutcome) for item in results) == 1
    assert results.count(RcaErrorCode.IDENTITY_LINEAGE_CONFLICT) == 1
    with SqliteRcaStore(database) as store:
        view = store.get_attempt_lineage("ATT-1")
        assert view is not None
        assert tuple(item.identity.try_ordinal for item in view.try_outcomes) == (1, 2)
        assert view.attempt.latest_try_ordinal == 2
