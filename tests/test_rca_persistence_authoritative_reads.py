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
        assert store.get_attempt_lineage("ATT-MISSING") is None
        assert store.get_current("AGG-MISSING") is None
        assert store.get_freshness_lineage("AGG-MISSING") == ()
        assert store.get_version("VER-MISSING") is None
        assert store.get_version_history("AGG-MISSING") == ()
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


def _remove_aggregate_and_admission_receipt(database) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "DELETE FROM rca_operation_receipts WHERE command_kind='CREATE_AGGREGATE'"
        )
        connection.execute("DELETE FROM rca_aggregates WHERE aggregate_id='AGG-1'")


def _remove_attempt_and_attempt_receipts(database) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """DELETE FROM rca_operation_receipts
                WHERE command_kind IN ('ADMIT_ATTEMPT','RECORD_TRY')"""
        )
        connection.execute("DELETE FROM rca_attempts WHERE attempt_id='ATT-1'")


def test_missing_aggregate_with_surviving_attempt_fails_point_read_closed(
    tmp_path,
) -> None:
    database = tmp_path / "aggregate-attempt-reference.db"
    store = SqliteRcaStore(database)
    seed(store)
    _remove_aggregate_and_admission_receipt(database)

    with pytest.raises(RcaDomainError) as raised:
        store.get_aggregate("AGG-1")
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    store.close()


def test_missing_aggregate_with_surviving_version_fails_identity_and_history_reads_closed(
    tmp_path,
) -> None:
    database = tmp_path / "aggregate-version-reference.db"
    store = SqliteRcaStore(database)
    seed(store)
    commit(store)
    _remove_attempt_and_attempt_receipts(database)
    _remove_aggregate_and_admission_receipt(database)

    reads = (
        lambda: store.get_aggregate_by_incident("INC-1"),
        lambda: store.get_version_history("AGG-1"),
    )
    for read in reads:
        with pytest.raises(RcaDomainError) as raised:
            read()
        assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    store.close()


def test_missing_aggregate_with_surviving_current_and_freshness_fails_reads_closed(
    tmp_path,
) -> None:
    database = tmp_path / "aggregate-current-reference.db"
    _prepare_published_version(database)
    store = SqliteRcaStore(database)
    _remove_aggregate_and_admission_receipt(database)

    for read in (
        lambda: store.get_current("AGG-1"),
        lambda: store.get_freshness_lineage("AGG-1"),
    ):
        with pytest.raises(RcaDomainError) as raised:
            read()
        assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    store.close()


def test_missing_attempt_with_surviving_version_fails_point_read_closed(
    tmp_path,
) -> None:
    database = tmp_path / "attempt-version-reference.db"
    store = SqliteRcaStore(database)
    seed(store)
    commit(store)
    _remove_attempt_and_attempt_receipts(database)

    with pytest.raises(RcaDomainError) as raised:
        store.get_attempt_lineage("ATT-1")
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    store.close()


@pytest.mark.parametrize("missing_authority", ["aggregate", "attempt"])
def test_downstream_dangling_authority_fails_closed_after_reopen(
    tmp_path, missing_authority
) -> None:
    database = tmp_path / f"downstream-dangling-{missing_authority}.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        commit(store)
    if missing_authority == "aggregate":
        _remove_aggregate_and_admission_receipt(database)
    else:
        _remove_attempt_and_attempt_receipts(database)

    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
