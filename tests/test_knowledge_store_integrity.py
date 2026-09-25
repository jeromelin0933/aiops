import sqlite3

import pytest

from knowledge_index import (
    KnowledgeLocalReadiness,
    KnowledgeReadStatus,
    KnowledgeStoreIntegrityError,
    OpaqueExternalReference,
    OpaqueReferenceType,
    RetentionHoldKey,
    RetentionHoldRecord,
    RetentionObligationKind,
    RetentionSubjectKind,
    SqliteKnowledgeStore,
    UnsupportedKnowledgeStoreVersion,
)
from _knowledge_store_testkit import activation, build, digest, operation, snapshot


def _create(path) -> None:
    SqliteKnowledgeStore(path).close()


def test_unsupported_schema_version_fails_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    _create(path)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE knowledge_store_metadata SET schema_version = 99")
    connection.commit()
    connection.close()
    with pytest.raises(UnsupportedKnowledgeStoreVersion):
        SqliteKnowledgeStore(path)


@pytest.mark.parametrize("mutation", ["DROP TABLE retention_holds", "CREATE TABLE surprise(value TEXT)"])
def test_missing_or_extra_table_fails_reopen(tmp_path, mutation: str) -> None:
    path = tmp_path / "knowledge.sqlite3"
    _create(path)
    connection = sqlite3.connect(path)
    connection.execute(mutation)
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


def test_wrong_column_shape_fails_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE partial(unexpected TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


def test_malformed_schema_metadata_fails_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    _create(path)
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM knowledge_store_metadata")
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


def test_invalid_record_version_is_repair_required_not_not_found(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    record = build()
    store = SqliteKnowledgeStore(path)
    store.create_build_lineage(record)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute("UPDATE build_lineage SET record_version = 99")
    connection.commit()
    connection.close()
    try:
        assert store.get_build_lineage(record.build_identity).status is KnowledgeReadStatus.REPAIR_REQUIRED
    finally:
        store.close()


def test_broken_reference_is_repair_required_not_not_initialized(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    record = build()
    store = SqliteKnowledgeStore(path)
    store.create_build_lineage(record)
    store.commit_activation(activation(record), expected_generation=0)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DELETE FROM build_lineage")
    connection.commit()
    connection.close()
    try:
        assert store.read_activation().status is KnowledgeReadStatus.REPAIR_REQUIRED
    finally:
        store.close()


def test_foreign_key_check_is_enforced_at_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    record = build()
    with SqliteKnowledgeStore(path) as store:
        store.create_build_lineage(record)
        store.commit_activation(activation(record), expected_generation=0)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DELETE FROM build_lineage")
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


def test_integrity_failure_does_not_destructively_repair(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    _create(path)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE foreign_authority(value TEXT)")
    connection.execute("INSERT INTO foreign_authority VALUES ('keep-me')")
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT value FROM foreign_authority").fetchone() == ("keep-me",)
    connection.close()


def test_invalid_build_identity_fails_strict_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    _create(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO build_lineage VALUES (?, 1, ?, ?)",
        ("not-a-build-identity", f"kmf_{digest('a')}", digest("b")),
    )
    connection.commit()
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError) as caught:
        SqliteKnowledgeStore(path)
    assert caught.value.status is KnowledgeReadStatus.REPAIR_REQUIRED


def test_invalid_manifest_commitment_fails_strict_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    _create(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO build_lineage VALUES (?, 1, ?, ?)",
        (f"kbld_{digest('a')}", "not-a-manifest-commitment", digest("b")),
    )
    connection.commit()
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


def test_invalid_durable_enum_fails_strict_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    _create(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO retention_holds VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 1)",
        (
            "hold-invalid-enum", "BUILD", f"kbld_{digest('a')}", "UNKNOWN_OWNER_TYPE",
            "opaque-owner", "OUTSTANDING_OPERATION", digest("b"), "ACTIVE",
        ),
    )
    connection.commit()
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


def test_semantically_wrong_snapshot_lineage_fails_strict_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    record = build()
    with SqliteKnowledgeStore(path) as store:
        store.create_build_lineage(record)
        store.create_operation(operation(record))
        store.complete_operation_with_snapshot(snapshot(record), expected_revision=1)
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE snapshot_envelopes SET lineage_commitment = ?", (digest("e"),)
    )
    connection.commit()
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


def test_legitimate_empty_store_remains_not_initialized_after_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    _create(path)
    with SqliteKnowledgeStore(path) as store:
        assert store.local_readiness().status is KnowledgeLocalReadiness.NOT_INITIALIZED


def test_all_valid_durable_record_types_survive_strict_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    record = build()
    hold = RetentionHoldRecord(
        RetentionHoldKey("hold-valid-reopen"),
        RetentionSubjectKind.BUILD,
        record.build_identity,
        OpaqueExternalReference(OpaqueReferenceType.RCA_ATTEMPT, "opaque-owner"),
        RetentionObligationKind.OUTSTANDING_OPERATION,
        digest("e"),
    )
    with SqliteKnowledgeStore(path) as store:
        store.create_build_lineage(record)
        store.commit_activation(activation(record), expected_generation=0)
        store.create_operation(operation(record))
        store.complete_operation_with_snapshot(snapshot(record), expected_revision=1)
        store.create_retention_hold(hold)
    with SqliteKnowledgeStore(path) as reopened:
        assert reopened.read_activation().status is KnowledgeReadStatus.FOUND
        assert reopened.get_snapshot(snapshot(record).snapshot_key).status is KnowledgeReadStatus.FOUND
        assert reopened.get_retention_hold(hold.hold_key).value == hold
