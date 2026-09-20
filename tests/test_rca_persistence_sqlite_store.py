import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from rca_persistence import (
    SCHEMA_VERSION,
    AdmitAttemptRequest,
    AttemptLineage,
    CreateAggregateRequest,
    GenerationLifecycle,
    GenerationProvenance,
    RcaAggregate,
    RcaDomainError,
    RcaErrorCode,
    SqliteRcaStore,
)


NOW = datetime(2026, 9, 20, 8, 30, tzinfo=timezone.utc)


def _lineage(
    attempt_id: str = "ATT-1",
    aggregate_id: str = "AGG-1",
    evidence_revision_id: str = "ER-1",
) -> AttemptLineage:
    return AttemptLineage(
        attempt_id,
        aggregate_id,
        "ES-1",
        evidence_revision_id,
        "KS-1",
        GenerationProvenance("provider-1", "model-1", "prompt-1", "config-1", "profile-1"),
    )


def _aggregate_request(
    operation_id: str = "OP-AGG-1",
    aggregate_id: str = "AGG-1",
    incident_id: str = "INC-1",
    now: datetime = NOW,
) -> CreateAggregateRequest:
    return CreateAggregateRequest(operation_id, aggregate_id, incident_id, now)


def _attempt_request(
    operation_id: str = "OP-ATT-1",
    lineage: AttemptLineage | None = None,
    now: datetime = NOW,
) -> AdmitAttemptRequest:
    return AdmitAttemptRequest(operation_id, lineage or _lineage(), now)


def _seed_aggregate(store: SqliteRcaStore) -> RcaAggregate:
    return store.create_or_discover_aggregate(_aggregate_request())


def test_new_schema_initializes_with_explicit_current_version_and_is_ready(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        store.validate_local_readiness()

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        metadata = connection.execute(
            "SELECT metadata_key, metadata_value FROM rca_store_metadata"
        ).fetchall()
    assert tables == {
        "rca_store_metadata",
        "rca_aggregates",
        "rca_attempts",
        "rca_operation_receipts",
        "rca_publication_results",
        "rca_currents",
        "rca_freshness_history",
    }
    assert metadata == [("rca_store_schema_version", SCHEMA_VERSION)]


def test_concurrent_new_path_initialization_serializes_to_one_schema(tmp_path) -> None:
    database = tmp_path / "new-rca.db"
    barrier = Barrier(2)

    def initialize() -> None:
        barrier.wait()
        with SqliteRcaStore(database) as store:
            store.validate_local_readiness()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: initialize(), range(2)))

    with SqliteRcaStore(database) as reopened:
        reopened.validate_local_readiness()


def test_recognized_older_schema_requires_governed_migration(tmp_path) -> None:
    database = tmp_path / "older.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE rca_store_metadata(metadata_key TEXT PRIMARY KEY, metadata_value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO rca_store_metadata VALUES ('rca_store_schema_version', '1')"
        )

    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.MIGRATION_REQUIRED


@pytest.mark.parametrize("version", ["999", "future", "", "02"])
def test_unknown_schema_version_fails_closed_without_rewrite(tmp_path, version) -> None:
    database = tmp_path / "unknown.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE rca_store_metadata(metadata_key TEXT PRIMARY KEY, metadata_value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO rca_store_metadata VALUES ('rca_store_schema_version', ?)", (version,)
        )

    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.SCHEMA_INCOMPATIBILITY
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT metadata_value FROM rca_store_metadata"
        ).fetchone() == (version,)


def test_malformed_or_unsafe_current_schema_fails_closed(tmp_path) -> None:
    malformed = tmp_path / "malformed.db"
    with sqlite3.connect(malformed) as connection:
        connection.execute("CREATE TABLE rca_store_metadata(wrong TEXT)")
    with pytest.raises(RcaDomainError) as malformed_error:
        SqliteRcaStore(malformed)
    assert malformed_error.value.code is RcaErrorCode.SCHEMA_INCOMPATIBILITY

    unsafe = tmp_path / "unsafe.db"
    with sqlite3.connect(unsafe) as connection:
        connection.execute(
            "CREATE TABLE rca_store_metadata(metadata_key TEXT PRIMARY KEY, metadata_value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO rca_store_metadata VALUES ('rca_store_schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        connection.execute("CREATE TABLE unexpected_authority(secret TEXT)")
    with pytest.raises(RcaDomainError) as unsafe_error:
        SqliteRcaStore(unsafe)
    assert unsafe_error.value.code is RcaErrorCode.SCHEMA_INCOMPATIBILITY


def test_aggregate_create_discover_replay_is_obligation_first(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        created = _seed_aggregate(store)
        same_operation = store.create_or_discover_aggregate(
            _aggregate_request(now=NOW + timedelta(hours=1))
        )
        equivalent_obligation = store.create_or_discover_aggregate(
            _aggregate_request("OP-AGG-2", now=NOW + timedelta(hours=2))
        )

        assert created == same_operation == equivalent_obligation == RcaAggregate("AGG-1", "INC-1")
        assert store.get_aggregate("AGG-1") == created
        assert store.get_aggregate_by_incident("INC-1") == created

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM rca_aggregates").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM rca_operation_receipts WHERE command_kind='CREATE_AGGREGATE'"
        ).fetchone() == (2,)


def test_aggregate_binding_and_operation_replay_conflicts_are_typed(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        _seed_aggregate(store)
        with pytest.raises(RcaDomainError) as binding:
            store.create_or_discover_aggregate(_aggregate_request("OP-2", "AGG-2", "INC-1"))
        assert binding.value.code is RcaErrorCode.IDENTITY_LINEAGE_CONFLICT

        with pytest.raises(RcaDomainError) as aggregate_identity:
            store.create_or_discover_aggregate(_aggregate_request("OP-3", "AGG-1", "INC-2"))
        assert aggregate_identity.value.code is RcaErrorCode.IDENTITY_LINEAGE_CONFLICT

        with pytest.raises(RcaDomainError) as operation:
            store.create_or_discover_aggregate(_aggregate_request("OP-AGG-1", "AGG-X", "INC-X"))
        assert operation.value.code is RcaErrorCode.RECEIPT_REPLAY_CONFLICT


def test_attempt_admission_equivalent_replay_and_authoritative_read(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        _seed_aggregate(store)
        admitted = store.admit_attempt(_attempt_request())
        replayed = store.admit_attempt(_attempt_request(now=NOW + timedelta(hours=1)))
        equivalent = store.admit_attempt(
            _attempt_request("OP-ATT-2", now=NOW + timedelta(hours=2))
        )
        view = store.get_attempt_lineage("ATT-1")

        assert admitted == replayed == equivalent
        assert admitted.lifecycle is GenerationLifecycle.PENDING
        assert admitted.latest_try_ordinal is None
        assert view is not None
        assert view.attempt == admitted
        assert view.try_outcomes == ()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM rca_attempts").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM rca_operation_receipts WHERE command_kind='ADMIT_ATTEMPT'"
        ).fetchone() == (2,)


def test_authorized_generating_transition_is_replay_safe_and_restart_durable(
    tmp_path,
) -> None:
    database = tmp_path / "generating.db"
    with SqliteRcaStore(database) as store:
        _seed_aggregate(store)
        store.admit_attempt(_attempt_request())
        transitioned = store.mark_attempt_generating("OP-GEN-1", "ATT-1", NOW)
        replayed = store.mark_attempt_generating(
            "OP-GEN-1", "ATT-1", NOW + timedelta(hours=1)
        )
        assert transitioned == replayed
        assert transitioned.lifecycle is GenerationLifecycle.GENERATING
        assert transitioned.lineage == _lineage()
        assert transitioned.latest_try_ordinal is None

        with pytest.raises(RcaDomainError) as illegal:
            store.mark_attempt_generating("OP-GEN-2", "ATT-1", NOW)
        assert illegal.value.code is RcaErrorCode.SEMANTIC_CONFLICT

        with pytest.raises(RcaDomainError) as contradictory:
            store.mark_attempt_generating("OP-GEN-1", "ATT-X", NOW)
        assert contradictory.value.code is RcaErrorCode.RECEIPT_REPLAY_CONFLICT

    with SqliteRcaStore(database) as reopened:
        view = reopened.get_attempt_lineage("ATT-1")
        assert view is not None
        assert view.attempt.lifecycle is GenerationLifecycle.GENERATING
        assert view.try_outcomes == ()
        assert reopened.mark_attempt_generating(
            "OP-GEN-1", "ATT-1", NOW + timedelta(days=1)
        ) == view.attempt


def test_generating_transition_rejects_missing_attempt(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "missing-generating.db") as store:
        with pytest.raises(RcaDomainError) as raised:
            store.mark_attempt_generating("OP-GEN", "ATT-MISSING", NOW)
        assert raised.value.code is RcaErrorCode.INVALID_REFERENCE


def test_attempt_requires_aggregate_and_rejects_contradictory_lineage(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        with pytest.raises(RcaDomainError) as missing:
            store.admit_attempt(_attempt_request())
        assert missing.value.code is RcaErrorCode.INVALID_REFERENCE

        _seed_aggregate(store)
        store.admit_attempt(_attempt_request())
        with pytest.raises(RcaDomainError) as lineage:
            store.admit_attempt(
                _attempt_request("OP-ATT-2", replace(_lineage(), evidence_revision_id="ER-2"))
            )
        assert lineage.value.code is RcaErrorCode.IDENTITY_LINEAGE_CONFLICT

        with pytest.raises(RcaDomainError) as operation:
            store.admit_attempt(_attempt_request("OP-ATT-1", _lineage("ATT-X")))
        assert operation.value.code is RcaErrorCode.RECEIPT_REPLAY_CONFLICT


def test_restart_reopen_preserves_aggregate_attempt_and_receipts(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        aggregate = _seed_aggregate(store)
        attempt = store.admit_attempt(_attempt_request())

    with SqliteRcaStore(database) as reopened:
        reopened.validate_local_readiness()
        assert reopened.get_aggregate("AGG-1") == aggregate
        assert reopened.get_aggregate_by_incident("INC-1") == aggregate
        assert reopened.get_attempt_lineage("ATT-1").attempt == attempt
        assert reopened.create_or_discover_aggregate(_aggregate_request()) == aggregate
        assert reopened.admit_attempt(_attempt_request()) == attempt


def test_absence_is_not_found_but_corruption_is_integrity_failure(tmp_path) -> None:
    database = tmp_path / "rca.db"
    store = SqliteRcaStore(database)
    assert store.get_aggregate("AGG-MISSING") is None
    assert store.get_attempt_lineage("ATT-MISSING") is None
    _seed_aggregate(store)
    store.close()

    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM rca_operation_receipts WHERE command_kind='CREATE_AGGREGATE'"
        )
    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    assert raised.value.code is not RcaErrorCode.NOT_FOUND


def test_malformed_attempt_summary_fails_closed_instead_of_normalizing(tmp_path) -> None:
    database = tmp_path / "rca.db"
    store = SqliteRcaStore(database)
    _seed_aggregate(store)
    store.admit_attempt(_attempt_request())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE rca_attempts SET lifecycle='COMPLETED', latest_try_ordinal=1 WHERE attempt_id='ATT-1'"
        )
    with pytest.raises(RcaDomainError) as raised:
        store.get_attempt_lineage("ATT-1")
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    store.close()


def test_concurrent_conflicting_aggregate_binding_serializes_to_one_authority(tmp_path) -> None:
    database = tmp_path / "rca.db"
    SqliteRcaStore(database).close()
    barrier = Barrier(2)

    def create(operation_id: str, aggregate_id: str):
        with SqliteRcaStore(database) as store:
            barrier.wait()
            try:
                return store.create_or_discover_aggregate(
                    _aggregate_request(operation_id, aggregate_id, "INC-1")
                )
            except RcaDomainError as exc:
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda args: create(*args), [("OP-1", "AGG-1"), ("OP-2", "AGG-2")]))

    assert sum(isinstance(item, RcaAggregate) for item in results) == 1
    assert results.count(RcaErrorCode.IDENTITY_LINEAGE_CONFLICT) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM rca_aggregates").fetchone() == (1,)


def test_concurrent_equivalent_attempt_admission_has_one_core_and_replay_receipts(tmp_path) -> None:
    database = tmp_path / "rca.db"
    with SqliteRcaStore(database) as store:
        _seed_aggregate(store)
    barrier = Barrier(2)

    def admit(operation_id: str):
        with SqliteRcaStore(database) as store:
            barrier.wait()
            return store.admit_attempt(_attempt_request(operation_id))

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = list(executor.map(admit, ["OP-ATT-A", "OP-ATT-B"]))

    assert attempts[0] == attempts[1]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM rca_attempts").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM rca_operation_receipts WHERE command_kind='ADMIT_ATTEMPT'"
        ).fetchone() == (2,)
