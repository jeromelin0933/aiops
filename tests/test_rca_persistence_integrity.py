import json
import sqlite3

import pytest

from rca_persistence import (
    PublicationDisposition,
    PublicationResult,
    RcaDomainError,
    RcaErrorCode,
    SqliteRcaStore,
)

from test_rca_persistence_version_artifact import NOW, commit, seed, target


def _published(database) -> None:
    with SqliteRcaStore(database) as store:
        seed(store)
        commit(store)
        store.complete_authorized_publication(
            PublicationResult(target(), PublicationDisposition.APPLIED, NOW, "VER-1")
        )


@pytest.mark.parametrize("corruption", ["dangling", "wrong_aggregate"])
def test_dangling_or_wrong_current_is_corruption_not_not_found(tmp_path, corruption) -> None:
    database = tmp_path / f"{corruption}.db"
    _published(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        if corruption == "dangling":
            connection.execute(
                "UPDATE rca_currents SET current_version_id='VER-MISSING'"
            )
        else:
            connection.execute(
                "UPDATE rca_currents SET aggregate_id='AGG-WRONG'"
            )
    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code in {
        RcaErrorCode.INTEGRITY_CORRUPTION,
        RcaErrorCode.SCHEMA_INCOMPATIBILITY,
    }
    assert raised.value.code is not RcaErrorCode.NOT_FOUND


def test_receipt_result_contradiction_fails_closed(tmp_path) -> None:
    database = tmp_path / "contradiction.db"
    _published(database)
    with sqlite3.connect(database) as connection:
        encoded = connection.execute(
            "SELECT target_identity FROM rca_publication_results"
        ).fetchone()[0]
        payload = json.loads(encoded)
        payload["target_version_id"] = "VER-X"
        connection.execute(
            "UPDATE rca_publication_results SET target_identity=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        )
    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION


def test_publication_result_and_current_replacement_roll_back_together(tmp_path) -> None:
    database = tmp_path / "atomic.db"
    with SqliteRcaStore(database) as store:
        seed(store)
        commit(store)
        with sqlite3.connect(database) as connection:
            connection.execute(
                """CREATE TRIGGER reject_current BEFORE INSERT ON rca_currents
                   BEGIN SELECT RAISE(ABORT, 'fault'); END"""
            )
        with pytest.raises(RcaDomainError):
            store.complete_authorized_publication(
                PublicationResult(target(), PublicationDisposition.APPLIED, NOW, "VER-1")
            )
        assert store.get_current("AGG-1") is None
        assert store.get_publication_result("PUB-1").disposition is PublicationDisposition.A_SIDE_COMMITTED


def test_multiple_current_schema_corruption_is_never_not_found(tmp_path) -> None:
    database = tmp_path / "multiple.db"
    _published(database)
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT * FROM rca_currents").fetchone()
        connection.execute("DROP TABLE rca_currents")
        connection.execute(
            """CREATE TABLE rca_currents(
                   aggregate_id TEXT, current_version_id TEXT, freshness TEXT,
                   material_evidence_revision_basis TEXT, updated_at TEXT)"""
        )
        connection.executemany(
            "INSERT INTO rca_currents VALUES (?, ?, ?, ?, ?)",
            [row, row],
        )
    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.SCHEMA_INCOMPATIBILITY


def test_candidate_a_schema_has_no_incident_or_runtime_authority(tmp_path) -> None:
    database = tmp_path / "boundary.db"
    with SqliteRcaStore(database):
        pass
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert not any("incident" in name or "runtime" in name for name in tables)


def test_missing_freshness_lineage_fails_closed(tmp_path) -> None:
    database = tmp_path / "missing-freshness.db"
    _published(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM rca_freshness_history")
    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION


def test_current_read_rejects_projection_that_contradicts_freshness_lineage(
    tmp_path,
) -> None:
    database = tmp_path / "freshness-projection.db"
    _published(database)
    store = SqliteRcaStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE rca_currents SET material_evidence_revision_basis='ER-CORRUPT'"
        )
    with pytest.raises(RcaDomainError) as raised:
        store.get_current("AGG-1")
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    store.close()


def test_live_current_read_fails_closed_when_current_row_is_missing(tmp_path) -> None:
    database = tmp_path / "missing-current-live.db"
    _published(database)
    store = SqliteRcaStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM rca_currents")
    with pytest.raises(RcaDomainError) as raised:
        store.get_current("AGG-1")
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    store.close()


def test_live_current_read_fails_closed_from_applied_evidence_alone(tmp_path) -> None:
    database = tmp_path / "missing-current-applied.db"
    _published(database)
    store = SqliteRcaStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM rca_currents")
        connection.execute("DELETE FROM rca_freshness_history")
    with pytest.raises(RcaDomainError) as raised:
        store.get_current("AGG-1")
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION
    store.close()


def test_reopen_fails_closed_when_current_row_is_missing(tmp_path) -> None:
    database = tmp_path / "missing-current-reopen.db"
    _published(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM rca_currents")
    with pytest.raises(RcaDomainError) as raised:
        SqliteRcaStore(database)
    assert raised.value.code is RcaErrorCode.INTEGRITY_CORRUPTION


def test_full_poc_history_and_receipts_are_retained_after_replacement(tmp_path) -> None:
    database = tmp_path / "retention.db"
    _published(database)
    with sqlite3.connect(database) as connection:
        before = connection.execute("SELECT COUNT(*) FROM rca_operation_receipts").fetchone()[0]
        assert connection.execute("SELECT COUNT(*) FROM rca_publication_results").fetchone() == (1,)
    with SqliteRcaStore(database) as store:
        assert len(store.get_version_history("AGG-1")) == 1
        assert store.get_attempt_lineage("ATT-1") is not None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM rca_operation_receipts").fetchone() == (before,)
