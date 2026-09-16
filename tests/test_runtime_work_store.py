import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from runtime_orchestration.contracts import (
    RuntimeWorkKind,
    RuntimeWorkRecord,
    RuntimeWorkStatus,
)
from runtime_orchestration.identity import runtime_work_id
from runtime_orchestration.sqlite_work_store import (
    ContradictoryRuntimeWorkError,
    RuntimeWorkRecordCorruptError,
    RuntimeWorkStoreClosedError,
    RuntimeWorkStoreIntegrityError,
    SqliteRuntimeWorkStore,
    UnsupportedRuntimeWorkStoreVersion,
)


UTC_NOW = datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc)
OFFSET_NOW = datetime(2026, 9, 13, 10, 0, tzinfo=timezone(timedelta(hours=8)))


def work_record(
    *,
    event_id: str = "EVT-1",
    incident_id: str = "INC-1",
    attempt_count: int = 0,
    now: datetime = UTC_NOW,
) -> RuntimeWorkRecord:
    work_id = runtime_work_id(RuntimeWorkKind.AUTO_ASSIGN, event_id, incident_id)
    return RuntimeWorkRecord(
        work_id=work_id,
        work_kind=RuntimeWorkKind.AUTO_ASSIGN,
        event_id=event_id,
        incident_id=incident_id,
        stage="POST_CORRELATION",
        next_action="AUTO_ASSIGN",
        workflow_operation_id="WFO-1",
        attempt_count=attempt_count,
        retry_limit=4,
        next_retry_at=now + timedelta(seconds=1),
        last_attempt_at=now if attempt_count else None,
        source_domain="SPEC-009" if attempt_count else None,
        source_error_code="TEMPORARY_UNAVAILABLE" if attempt_count else None,
        source_retry_disposition="RETRYABLE" if attempt_count else None,
        status=RuntimeWorkStatus.OUTSTANDING,
        created_at=now,
        updated_at=now,
        observed_at=now,
    )


def test_empty_store_and_deterministic_enumeration(tmp_path):
    store = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
    assert store.enumerate_all().records == ()
    assert store.enumerate_all().isolated_corruptions == ()
    store.create(work_record(event_id="EVT-Z", incident_id="INC-Z"))
    store.create(work_record(event_id="EVT-A", incident_id="INC-A"))
    work_ids = [item.work_id for item in store.enumerate_all().records]
    assert work_ids == sorted(work_ids)


def test_write_read_update_complete_and_equivalent_replay(tmp_path):
    store = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
    original = work_record()
    created = store.create(original)
    assert created.revision == 1
    assert store.create(original) == created
    assert store.get(created.work_id) == created

    later = UTC_NOW + timedelta(seconds=1)
    updated = store.update(
        replace(
            created,
            attempt_count=1,
            last_attempt_at=later,
            next_retry_at=later + timedelta(seconds=2),
            source_domain="SPEC-009",
            source_error_code="TEMPORARY_UNAVAILABLE",
            source_retry_disposition="RETRYABLE",
            updated_at=later,
            observed_at=later,
        ),
        expected_revision=created.revision,
    )
    assert updated.revision == 2
    assert updated.attempt_count == 1
    completed = store.complete(
        updated.work_id,
        observed_at=later + timedelta(seconds=1),
        expected_revision=updated.revision,
    )
    assert completed.status is RuntimeWorkStatus.COMPLETED
    assert completed.next_retry_at is None
    assert store.complete(completed.work_id, observed_at=later + timedelta(days=1)) == completed


def test_contradictory_work_and_terminal_reopen_fail_closed(tmp_path):
    store = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
    original = work_record()
    created = store.create(original)
    contradictory = replace(original, event_id="EVT-OTHER")
    with pytest.raises(ContradictoryRuntimeWorkError, match="contradictory"):
        store.create(contradictory)

    completed = store.complete(created.work_id, observed_at=UTC_NOW + timedelta(seconds=1))
    with pytest.raises(ContradictoryRuntimeWorkError, match="cannot be reopened"):
        store.update(
            replace(
                completed,
                status=RuntimeWorkStatus.OUTSTANDING,
                next_action="AUTO_ASSIGN",
                updated_at=UTC_NOW + timedelta(seconds=2),
                observed_at=UTC_NOW + timedelta(seconds=2),
            ),
            expected_revision=completed.revision,
        )


def test_close_reopen_durability_and_retry_count_persistence(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    first = SqliteRuntimeWorkStore(database)
    created = first.create(work_record(attempt_count=2))
    first.close()
    first.close()
    with pytest.raises(RuntimeWorkStoreClosedError):
        first.get(created.work_id)

    reopened = SqliteRuntimeWorkStore(database)
    restored = reopened.get(created.work_id)
    assert restored is not None
    assert restored.attempt_count == 2
    assert restored.retry_limit == 4
    assert restored.workflow_operation_id == "WFO-1"


def test_retry_budget_and_attempt_count_cannot_reset(tmp_path):
    store = SqliteRuntimeWorkStore(tmp_path / "runtime.sqlite3")
    current = store.create(work_record(attempt_count=3))
    later = UTC_NOW + timedelta(seconds=2)
    with pytest.raises(ContradictoryRuntimeWorkError, match="attempt count"):
        store.update(
            replace(current, attempt_count=0, updated_at=later, observed_at=later),
            expected_revision=current.revision,
        )
    with pytest.raises(ContradictoryRuntimeWorkError, match="immutable"):
        store.update(
            replace(current, retry_limit=5, updated_at=later, observed_at=later),
            expected_revision=current.revision,
        )
    assert store.get(current.work_id).attempt_count == 3


def test_timezone_equivalent_instant_is_persisted_as_canonical_utc(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    store = SqliteRuntimeWorkStore(database)
    created = store.create(work_record(now=OFFSET_NOW))
    assert created.created_at == UTC_NOW
    store.close()
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT created_at, next_retry_at FROM runtime_work_records"
        ).fetchone()
    assert row == (
        "2026-09-13T02:00:00.000000Z",
        "2026-09-13T02:00:01.000000Z",
    )
    assert SqliteRuntimeWorkStore(database).get(created.work_id).created_at == UTC_NOW


def test_malformed_or_unsupported_store_schema_fails_fast(tmp_path):
    malformed = tmp_path / "malformed.sqlite3"
    with sqlite3.connect(malformed) as connection:
        connection.execute("CREATE TABLE runtime_work_records(work_id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeWorkStoreIntegrityError, match="independent|malformed"):
        SqliteRuntimeWorkStore(malformed)

    unsupported = tmp_path / "unsupported.sqlite3"
    store = SqliteRuntimeWorkStore(unsupported)
    store.close()
    with sqlite3.connect(unsupported) as connection:
        connection.execute("UPDATE runtime_work_store_metadata SET schema_version = 2")
    with pytest.raises(UnsupportedRuntimeWorkStoreVersion):
        SqliteRuntimeWorkStore(unsupported)


def test_partial_record_corruption_is_explicitly_isolated_not_silently_skipped(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    store = SqliteRuntimeWorkStore(database)
    good = store.create(work_record(event_id="EVT-A", incident_id="INC-A"))
    bad = store.create(work_record(event_id="EVT-B", incident_id="INC-B"))
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runtime_work_records SET status = 'UNKNOWN_STATUS' WHERE work_id = ?",
            (bad.work_id,),
        )
    reopened = SqliteRuntimeWorkStore(database)
    result = reopened.enumerate_all()
    assert [record.work_id for record in result.records] == [good.work_id]
    assert [item.record_key for item in result.isolated_corruptions] == [bad.work_id]
    with pytest.raises(RuntimeWorkRecordCorruptError):
        reopened.get(bad.work_id)


def test_unsupported_record_version_is_explicitly_isolated(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    store = SqliteRuntimeWorkStore(database)
    bad = store.create(work_record())
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE runtime_work_records SET record_version = 2 WHERE work_id = ?",
            (bad.work_id,),
        )
    result = SqliteRuntimeWorkStore(database).enumerate_all()
    assert result.records == ()
    assert result.isolated_corruptions[0].record_key == bad.work_id
    assert "unsupported record version" in result.isolated_corruptions[0].detail


def test_store_wide_unreadable_and_shared_domain_database_fail_fast(tmp_path):
    unreadable = tmp_path / "unreadable.sqlite3"
    unreadable.write_bytes(b"not a sqlite database")
    with pytest.raises(RuntimeWorkStoreIntegrityError, match="cannot be opened"):
        SqliteRuntimeWorkStore(unreadable)

    shared = tmp_path / "domain.sqlite3"
    with sqlite3.connect(shared) as connection:
        connection.execute("CREATE TABLE incident_records(incident_id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeWorkStoreIntegrityError, match="independent"):
        SqliteRuntimeWorkStore(shared)


def test_d2_schema_has_references_only_and_no_domain_payload_copy(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    store = SqliteRuntimeWorkStore(database)
    store.close()
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runtime_work_records)")
        }
    assert tables == {"runtime_work_store_metadata", "runtime_work_records"}
    forbidden = {
        "event_payload",
        "decision",
        "candidate_snapshot",
        "pending",
        "blocked",
        "intent",
        "processed",
        "incident_status",
        "assignee",
        "reviewer",
        "shadow_record",
        "resolution_evidence",
        "rca",
        "payload",
    }
    assert columns.isdisjoint(forbidden)
