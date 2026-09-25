import json
import sqlite3

import pytest

from knowledge_index import (
    BuildOperationKey,
    KnowledgeBuildService,
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
from _knowledge_build_testkit import (
    DeterministicIndex,
    DeterministicProvider,
    limits,
    manifest_and_plan,
)
from _knowledge_store_testkit import activation, build, digest, operation
from _knowledge_retrieval_testkit import request
from _knowledge_snapshot_testkit import environment


def _create(path) -> None:
    SqliteKnowledgeStore(path).close()


def _create_staged_build(path, source_root, *, validate: bool):
    source_root.mkdir()
    raw, chunks, identity_input = manifest_and_plan(source_root)
    with SqliteKnowledgeStore(path) as store:
        service = KnowledgeBuildService(
            store, DeterministicProvider(), DeterministicIndex()
        )
        staged = service.stage(
            operation_key=BuildOperationKey("stage-integrity"),
            raw_manifest=raw,
            source_root=source_root,
            chunks=chunks,
            identity_input=identity_input,
            limits=limits(),
            required_capability_identity="embedding-capability-v1",
        ).record
        assert staged is not None
        if validate:
            service.validate(
                staged.build_identity,
                BuildOperationKey("validate-integrity"),
                maximum_probe_results=2,
            )
    return staged


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


@pytest.mark.parametrize("mutation", ["missing", "weakened"])
def test_missing_or_weakened_check_constraint_fails_strict_reopen(tmp_path, mutation) -> None:
    path = tmp_path / f"knowledge-{mutation}.sqlite3"
    _create(path)
    connection = sqlite3.connect(path)
    sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'snapshot_envelopes'"
    ).fetchone()[0]
    original = "CHECK (resolution IN ('MATCH', 'NO_MATCH', 'RETRIEVAL_UNAVAILABLE'))"
    replacement = "" if mutation == "missing" else (
        "CHECK (resolution IN ('MATCH', 'NO_MATCH', 'RETRIEVAL_UNAVAILABLE', 'INVALID'))"
    )
    assert original in sql
    connection.execute("PRAGMA writable_schema = ON")
    connection.execute(
        "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'snapshot_envelopes'",
        (sql.replace(original, replacement),),
    )
    version = connection.execute("PRAGMA schema_version").fetchone()[0]
    connection.execute(f"PRAGMA schema_version = {version + 1}")
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError, match="constraints"):
        SqliteKnowledgeStore(path)


def test_valid_schema_v4_check_constraints_survive_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge-valid-v4.sqlite3"
    _create(path)
    with SqliteKnowledgeStore(path) as reopened:
        assert reopened.local_readiness().status is KnowledgeLocalReadiness.NOT_INITIALIZED


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
    store, _, _, _, service = environment(tmp_path)
    service.resolve(request(), limits())
    store.close()
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
    store, staged, _, _, service = environment(tmp_path)
    snapshot = service.resolve(request(), limits()).snapshot
    assert snapshot is not None
    hold = RetentionHoldRecord(
        RetentionHoldKey("hold-valid-reopen"),
        RetentionSubjectKind.BUILD,
        staged.build_identity,
        OpaqueExternalReference(OpaqueReferenceType.RCA_ATTEMPT, "opaque-owner"),
        RetentionObligationKind.OUTSTANDING_OPERATION,
        digest("e"),
    )
    store.create_retention_hold(hold)
    store.close()
    with SqliteKnowledgeStore(path) as reopened:
        assert reopened.read_activation().status is KnowledgeReadStatus.FOUND
        assert reopened.get_snapshot(snapshot.snapshot_key).status is KnowledgeReadStatus.FOUND
        assert reopened.get_retention_hold(hold.hold_key).value == hold


def test_malformed_staged_build_payload_fails_strict_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    _create(path)
    record = build()
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO build_lineage VALUES (?, 1, ?, ?)",
        (record.build_identity, record.manifest_commitment, record.lineage_commitment),
    )
    connection.execute(
        "INSERT INTO staged_builds VALUES (?, 1, ?, ?, ?, ?, ?)",
        (
            record.build_identity, "stage-corrupt", record.manifest_commitment,
            record.lineage_commitment, digest("staged"), '{"unexpected":true}',
        ),
    )
    connection.commit()
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


def test_lineage_commitment_tampering_fails_strict_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    staged = _create_staged_build(path, tmp_path / "source", validate=False)
    replacement = digest("tampered-lineage")
    connection = sqlite3.connect(path)
    payload = json.loads(connection.execute(
        "SELECT payload_json FROM staged_builds WHERE build_identity = ?",
        (staged.build_identity,),
    ).fetchone()[0])
    payload["lineage_commitment"] = replacement
    connection.execute(
        "UPDATE build_lineage SET lineage_commitment = ? WHERE build_identity = ?",
        (replacement, staged.build_identity),
    )
    connection.execute(
        "UPDATE staged_builds SET lineage_commitment = ?, payload_json = ? WHERE build_identity = ?",
        (replacement, json.dumps(payload, sort_keys=True), staged.build_identity),
    )
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


def test_staged_commitment_tampering_fails_strict_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    staged = _create_staged_build(path, tmp_path / "source", validate=False)
    replacement = digest("tampered-stage")
    connection = sqlite3.connect(path)
    payload = json.loads(connection.execute(
        "SELECT payload_json FROM staged_builds WHERE build_identity = ?",
        (staged.build_identity,),
    ).fetchone()[0])
    payload["staged_commitment"] = replacement
    connection.execute(
        "UPDATE staged_builds SET staged_commitment = ?, payload_json = ? WHERE build_identity = ?",
        (replacement, json.dumps(payload, sort_keys=True), staged.build_identity),
    )
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


def test_validation_commitment_tampering_fails_strict_reopen(tmp_path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    staged = _create_staged_build(path, tmp_path / "source", validate=True)
    replacement = digest("tampered-validation")
    connection = sqlite3.connect(path)
    payload = json.loads(connection.execute(
        "SELECT payload_json FROM build_validations WHERE build_identity = ?",
        (staged.build_identity,),
    ).fetchone()[0])
    payload["validation_commitment"] = replacement
    connection.execute(
        "UPDATE build_validations SET validation_commitment = ?, payload_json = ? WHERE build_identity = ?",
        (replacement, json.dumps(payload, sort_keys=True), staged.build_identity),
    )
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


@pytest.mark.parametrize("field", ["artifact_commitment", "validation_commitment"])
def test_frozen_retrieval_semantic_tampering_fails_reopen(tmp_path, field) -> None:
    from dataclasses import replace

    from knowledge_index import canonical_serialize
    from _knowledge_retrieval_testkit import request, stage_activate

    path = tmp_path / "knowledge.sqlite3"
    with SqliteKnowledgeStore(path) as store:
        stage_activate(store, tmp_path / "retrieval-source", "retrieval")
        frozen = store.freeze_retrieval_operation(request())
    changed = replace(frozen, **{field: "f" * 64})
    connection = sqlite3.connect(path)
    connection.execute(
        f"UPDATE frozen_retrieval_operations SET {field} = ?, payload_json = ? WHERE operation_id = ?",
        ("f" * 64, canonical_serialize(changed), request().operation_key.value),
    )
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)


def test_frozen_retrieval_profile_tampering_with_new_commitment_fails_reopen(tmp_path) -> None:
    from dataclasses import replace

    from knowledge_index import canonical_serialize, derive_retrieval_operation_commitment
    from _knowledge_retrieval_testkit import request, stage_activate

    path = tmp_path / "knowledge.sqlite3"
    with SqliteKnowledgeStore(path) as store:
        stage_activate(store, tmp_path / "retrieval-source", "retrieval")
        frozen = store.freeze_retrieval_operation(request())
    changed_request = replace(frozen.request, capability_identity="embedding-capability-v2")
    commitment = derive_retrieval_operation_commitment(changed_request)
    changed = replace(frozen, request=changed_request, semantic_commitment=commitment)
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE operation_envelopes SET semantic_commitment = ? WHERE operation_id = ?",
        (commitment, frozen.request.operation_key.value),
    )
    connection.execute(
        "UPDATE frozen_retrieval_operations SET semantic_commitment = ?, payload_json = ? WHERE operation_id = ?",
        (commitment, canonical_serialize(changed), frozen.request.operation_key.value),
    )
    connection.commit()
    connection.close()
    with pytest.raises(KnowledgeStoreIntegrityError):
        SqliteKnowledgeStore(path)
