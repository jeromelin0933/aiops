import sqlite3

import pytest

from src.incident_management import IncidentDomainError, IncidentErrorCode, SCHEMA_VERSION, SqliteIncidentStore
from src.incident_management.sqlite_store import _SCHEMA_STATEMENTS
from test_incident_sqlite_store import _seed


V1_TABLES = {
    "incident_store_metadata", "incidents", "incident_events", "incident_operation_receipts", "incident_audit",
}
WORKFLOW_TABLES = {
    "incident_workflow_operation_receipts", "incident_assignment_state", "incident_resolution_submissions",
    "incident_review_attempts", "incident_workflow_audit",
}


def _downgrade_empty_database_to_v1(database):
    with sqlite3.connect(database) as connection:
        for table in ("incident_workflow_audit", "incident_review_attempts", "incident_resolution_submissions",
                      "incident_assignment_state", "incident_workflow_operation_receipts"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE incident_store_metadata SET schema_version = 1 WHERE singleton = 1")


def _table_names(database):
    with sqlite3.connect(database) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def test_v1_schema_is_upgraded_additively_and_reopens(tmp_path):
    database = tmp_path / "incident.db"
    store = SqliteIncidentStore(str(database))
    record, receipt = _seed(store)
    store.close()
    _downgrade_empty_database_to_v1(database)

    with SqliteIncidentStore(str(database)) as store:
        # Existing authority data is neither reconstructed nor reset by the
        # additive DDL migration.
        assert store.get_incident("INC-1") == record
        assert store.get_operation_result("OP-1") == receipt.result
        assert store.get_assignment_state("unknown", "1") is None
        store.validate_readiness()
    assert _table_names(database) == V1_TABLES | WORKFLOW_TABLES
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT schema_version FROM incident_store_metadata").fetchone() == (SCHEMA_VERSION,)


def test_partial_v1_migration_fails_closed_and_keeps_old_metadata(tmp_path):
    database = tmp_path / "incident.db"
    SqliteIncidentStore(str(database)).close()
    _downgrade_empty_database_to_v1(database)
    with sqlite3.connect(database) as connection:
        connection.execute(_SCHEMA_STATEMENTS[7])

    with pytest.raises(IncidentDomainError) as raised:
        SqliteIncidentStore(str(database))
    assert raised.value.code is IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT schema_version FROM incident_store_metadata").fetchone() == (1,)
    assert _table_names(database) == V1_TABLES | {"incident_workflow_operation_receipts"}


def test_newer_schema_version_fails_closed_without_schema_rewrite(tmp_path):
    database = tmp_path / "incident.db"
    SqliteIncidentStore(str(database)).close()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE incident_store_metadata SET schema_version = ?", (SCHEMA_VERSION + 1,))

    with pytest.raises(IncidentDomainError) as raised:
        SqliteIncidentStore(str(database))
    assert raised.value.code is IncidentErrorCode.UNSUPPORTED_INCIDENT_STATE_VERSION
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT schema_version FROM incident_store_metadata").fetchone() == (SCHEMA_VERSION + 1,)
