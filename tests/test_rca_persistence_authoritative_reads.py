import sqlite3

import pytest

from rca_persistence import (
    CurrentFreshness,
    CurrentRca,
    PublicationDisposition,
    PublicationResult,
    RcaDomainError,
    RcaErrorCode,
    SqliteRcaStore,
)

from test_rca_persistence_version_artifact import NOW, artifact, commit, seed, target


def _prepare_published_version(database) -> None:
    with SqliteRcaStore(database) as store:
        seed(store)
        commit(store)
        store.complete_authorized_publication(
            PublicationResult(target(), PublicationDisposition.APPLIED, NOW, "VER-1")
        )


def _corrupt_version_authority(database, surviving_reference: str) -> None:
    with sqlite3.connect(database) as connection:
        if surviving_reference == "commit_receipt":
            connection.execute(
                """UPDATE rca_operation_receipts SET result_identity='VER-OTHER'
                     WHERE command_kind='COMMIT_ARTIFACT'"""
            )
            return
        connection.execute(
            "DELETE FROM rca_operation_receipts WHERE command_kind='COMMIT_ARTIFACT'"
        )
        if surviving_reference != "publication_result":
            connection.execute("DELETE FROM rca_publication_results")
        if surviving_reference != "current":
            connection.execute("DELETE FROM rca_currents")
        if surviving_reference != "freshness_history":
            connection.execute("DELETE FROM rca_freshness_history")


def test_current_read_is_one_coherent_authoritative_snapshot(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        seed(store)
        commit(store)
        result = PublicationResult(target(), PublicationDisposition.APPLIED, NOW, "VER-1")
        store.complete_authorized_publication(result)

        read = store.get_current("AGG-1")
        assert read is not None
        assert read.current.current_version_id == read.version.version_id == "VER-1"
        assert read.version.artifact == read.artifact == artifact()
        assert read.attempt_lineage.attempt.lineage.attempt_id == read.version.attempt_id
        assert read.publication_result == result
        assert store.get_publication_result("PUB-1") == result
        assert store.get_freshness_lineage("AGG-1") == (
            CurrentRca("AGG-1", "VER-1", CurrentFreshness.FRESH, "ER-1"),
        )


def test_legitimate_absence_remains_distinct_from_integrity_failure(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        assert store.get_aggregate("AGG-MISSING") is None
        assert store.get_aggregate_by_incident("INC-MISSING") is None
        assert store.get_current("AGG-MISSING") is None
        assert store.get_version("VER-MISSING") is None
        assert store.get_publication_result("PUB-MISSING") is None


def test_never_published_aggregate_has_legal_current_absence(tmp_path) -> None:
    with SqliteRcaStore(tmp_path / "rca.db") as store:
        seed(store)
        assert store.get_aggregate("AGG-1") is not None
        assert store.get_current("AGG-1") is None
        assert store.get_freshness_lineage("AGG-1") == ()


def test_missing_aggregate_with_surviving_admission_receipt_fails_point_reads_closed(
    tmp_path,
) -> None:
    database = tmp_path / "missing-aggregate.db"
    store = SqliteRcaStore(database)
    seed(store)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM rca_attempts")
        connection.execute("DELETE FROM rca_aggregates")

    reads = (
        lambda: store.get_aggregate("AGG-1"),
        lambda: store.get_aggregate_by_incident("INC-1"),
        lambda: store.get_current("AGG-1"),
        lambda: store.get_freshness_lineage("AGG-1"),
        lambda: store.get_version_history("AGG-1"),
    )
    for read in reads:
        with pytest.raises(RcaDomainError) as raised:
            read()
        assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    store.close()


def test_missing_aggregate_with_surviving_receipt_fails_closed_after_reopen(
    tmp_path,
) -> None:
    database = tmp_path / "missing-aggregate-reopen.db"
    with SqliteRcaStore(database) as store:
        seed(store)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM rca_attempts")
        connection.execute("DELETE FROM rca_aggregates")

    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION


@pytest.mark.parametrize(
    "surviving_reference",
    ["publication_result", "current", "freshness_history", "commit_receipt"],
)
def test_missing_version_with_durable_reference_fails_point_read_closed(
    tmp_path, surviving_reference
) -> None:
    database = tmp_path / f"missing-version-{surviving_reference}.db"
    if surviving_reference == "commit_receipt":
        store = SqliteRcaStore(database)
        seed(store)
        commit(store)
    else:
        _prepare_published_version(database)
        store = SqliteRcaStore(database)
    _corrupt_version_authority(database, surviving_reference)

    with pytest.raises(RcaDomainError) as raised:
        store.get_version("VER-1")
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    store.close()


@pytest.mark.parametrize(
    "surviving_reference",
    ["publication_result", "current", "freshness_history", "commit_receipt"],
)
def test_missing_version_with_durable_reference_fails_closed_after_reopen(
    tmp_path, surviving_reference
) -> None:
    database = tmp_path / f"missing-version-reopen-{surviving_reference}.db"
    if surviving_reference == "commit_receipt":
        with SqliteRcaStore(database) as store:
            seed(store)
            commit(store)
    else:
        _prepare_published_version(database)
    _corrupt_version_authority(database, surviving_reference)

    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
