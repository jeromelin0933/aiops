"""Independent durable authority for SPEC-014 Candidate C.

This module persists facts selected by a semantic layer.  It intentionally does
not decide build eligibility, retrieval outcomes, retention policy, or runtime
work.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import sqlite3
from typing import Iterator

from .contracts import (
    ActivationAuthorityRecord,
    ActivationOperationKey,
    BuildLineageRecord,
    KnowledgeCorruptionFinding,
    KnowledgeLocalReadiness,
    KnowledgeReadinessFact,
    KnowledgeReadResult,
    KnowledgeReadStatus,
    KnowledgeSnapshotEnvelope,
    KnowledgeSnapshotKey,
    OpaqueExternalReference,
    OpaqueReferenceType,
    OperationEnvelope,
    RetentionHoldKey,
    RetentionHoldRecord,
    RetentionHoldStatus,
    RetentionObligationKind,
    RetentionSubjectKind,
    RetrievalOperationKey,
)


SCHEMA_VERSION = 1
RECORD_VERSION = 1


class KnowledgeStoreError(RuntimeError):
    """Base error for the Candidate-C durable boundary."""


class KnowledgeStoreClosedError(KnowledgeStoreError):
    pass


class KnowledgeStoreIntegrityError(KnowledgeStoreError):
    status = KnowledgeReadStatus.REPAIR_REQUIRED


class KnowledgeStoreUnavailableError(KnowledgeStoreError):
    pass


class UnsupportedKnowledgeStoreVersion(KnowledgeStoreIntegrityError):
    pass


class KnowledgeStoreConflictError(KnowledgeStoreError):
    pass


class KnowledgeStoreConcurrencyError(KnowledgeStoreError):
    pass


class KnowledgeStoreCommitOutcomeUnknown(KnowledgeStoreError):
    """The caller must resolve the operation key through durable replay."""


_TABLE_COLUMNS = {
    "knowledge_store_metadata": ("singleton", "schema_version"),
    "build_lineage": (
        "build_identity", "record_version", "manifest_commitment", "lineage_commitment"
    ),
    "activation_authority": (
        "singleton", "record_version", "generation", "operation_id", "active_build_identity",
        "validated_build_commitment", "result_commitment",
    ),
    "activation_receipts": (
        "operation_id", "record_version", "requested_build_identity", "expected_generation",
        "committed_generation", "validated_build_commitment", "result_commitment",
    ),
    "operation_envelopes": (
        "operation_id", "record_version", "semantic_commitment", "frozen_build_identity",
        "snapshot_id", "completed", "revision",
    ),
    "snapshot_envelopes": (
        "snapshot_id", "record_version", "operation_id", "frozen_build_identity",
        "snapshot_commitment", "lineage_commitment",
    ),
    "retention_holds": (
        "hold_id", "record_version", "subject_kind", "subject_id", "owner_type", "owner_value",
        "obligation_kind", "semantic_commitment", "status", "revision",
    ),
}

_NULLABLE_COLUMNS = {
    ("knowledge_store_metadata", "singleton"),
    ("build_lineage", "build_identity"),
    ("activation_receipts", "operation_id"),
    ("activation_authority", "singleton"),
    ("operation_envelopes", "operation_id"),
    ("operation_envelopes", "frozen_build_identity"),
    ("operation_envelopes", "snapshot_id"),
    ("snapshot_envelopes", "snapshot_id"),
    ("retention_holds", "hold_id"),
}

_INTEGER_COLUMNS = {
    ("knowledge_store_metadata", "singleton"),
    ("knowledge_store_metadata", "schema_version"),
    ("build_lineage", "record_version"),
    ("activation_receipts", "record_version"),
    ("activation_receipts", "expected_generation"),
    ("activation_receipts", "committed_generation"),
    ("activation_authority", "singleton"),
    ("activation_authority", "record_version"),
    ("activation_authority", "generation"),
    ("operation_envelopes", "record_version"),
    ("operation_envelopes", "completed"),
    ("operation_envelopes", "revision"),
    ("snapshot_envelopes", "record_version"),
    ("retention_holds", "record_version"),
    ("retention_holds", "revision"),
}

_CREATE_SCHEMA = """
CREATE TABLE knowledge_store_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL
);
CREATE TABLE build_lineage (
    build_identity TEXT PRIMARY KEY,
    record_version INTEGER NOT NULL CHECK (record_version = 1),
    manifest_commitment TEXT NOT NULL,
    lineage_commitment TEXT NOT NULL
);
CREATE TABLE activation_receipts (
    operation_id TEXT PRIMARY KEY,
    record_version INTEGER NOT NULL CHECK (record_version = 1),
    requested_build_identity TEXT NOT NULL REFERENCES build_lineage(build_identity),
    expected_generation INTEGER NOT NULL CHECK (expected_generation >= 0),
    committed_generation INTEGER NOT NULL CHECK (committed_generation > 0),
    validated_build_commitment TEXT NOT NULL,
    result_commitment TEXT NOT NULL
);
CREATE TABLE activation_authority (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    record_version INTEGER NOT NULL CHECK (record_version = 1),
    generation INTEGER NOT NULL CHECK (generation > 0),
    operation_id TEXT NOT NULL UNIQUE REFERENCES activation_receipts(operation_id),
    active_build_identity TEXT NOT NULL REFERENCES build_lineage(build_identity),
    validated_build_commitment TEXT NOT NULL,
    result_commitment TEXT NOT NULL
);
CREATE TABLE operation_envelopes (
    operation_id TEXT PRIMARY KEY,
    record_version INTEGER NOT NULL CHECK (record_version = 1),
    semantic_commitment TEXT NOT NULL,
    frozen_build_identity TEXT REFERENCES build_lineage(build_identity),
    snapshot_id TEXT UNIQUE,
    completed INTEGER NOT NULL CHECK (completed IN (0, 1)),
    revision INTEGER NOT NULL CHECK (revision > 0),
    CHECK ((completed = 0 AND snapshot_id IS NULL) OR (completed = 1 AND snapshot_id IS NOT NULL))
);
CREATE TABLE snapshot_envelopes (
    snapshot_id TEXT PRIMARY KEY,
    record_version INTEGER NOT NULL CHECK (record_version = 1),
    operation_id TEXT NOT NULL UNIQUE REFERENCES operation_envelopes(operation_id),
    frozen_build_identity TEXT NOT NULL REFERENCES build_lineage(build_identity),
    snapshot_commitment TEXT NOT NULL,
    lineage_commitment TEXT NOT NULL
);
CREATE TABLE retention_holds (
    hold_id TEXT PRIMARY KEY,
    record_version INTEGER NOT NULL CHECK (record_version = 1),
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    owner_type TEXT NOT NULL,
    owner_value TEXT NOT NULL,
    obligation_kind TEXT NOT NULL,
    semantic_commitment TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'RELEASED')),
    revision INTEGER NOT NULL CHECK (revision > 0)
);
INSERT INTO knowledge_store_metadata(singleton, schema_version) VALUES (1, 1);
"""


def _finding(code: str, kind: str, key: str, detail: str) -> KnowledgeCorruptionFinding:
    return KnowledgeCorruptionFinding(code=code, record_kind=kind, record_key=key, detail=detail)


class SqliteKnowledgeStore:
    """Candidate-C-only SQLite authority with strict reopen validation."""

    def __init__(self, database_path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self._path = Path(database_path)
        if self._path.exists() and self._path.is_dir():
            raise KnowledgeStoreError("knowledge database path must be a file")
        self._connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self._path, isolation_level=None, timeout=busy_timeout_ms / 1000
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            self._connection = connection
            tables = self._table_names()
            if not tables:
                connection.executescript(_CREATE_SCHEMA)
            self._validate_schema()
            self._assert_integrity()
        except sqlite3.OperationalError as exc:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise KnowledgeStoreUnavailableError("knowledge store is unavailable") from exc
        except sqlite3.DatabaseError as exc:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise KnowledgeStoreIntegrityError("knowledge store is corrupt or unreadable") from exc
        except Exception:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise

    @property
    def database_path(self) -> Path:
        return self._path

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "SqliteKnowledgeStore":
        self._require_connection()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise KnowledgeStoreClosedError("knowledge store is closed")
        return self._connection

    def _table_names(self) -> set[str]:
        connection = self._require_connection()
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    def _validate_schema(self) -> None:
        connection = self._require_connection()
        if self._table_names() != set(_TABLE_COLUMNS):
            raise KnowledgeStoreIntegrityError("knowledge store table set is incompatible")
        for table, expected_columns in _TABLE_COLUMNS.items():
            table_info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            actual = tuple(row[1] for row in table_info)
            if actual != expected_columns:
                raise KnowledgeStoreIntegrityError(f"knowledge store {table} column shape is incompatible")
            for row in table_info:
                column = row[1]
                expected_type = "INTEGER" if (table, column) in _INTEGER_COLUMNS else "TEXT"
                expected_not_null = 0 if (table, column) in _NULLABLE_COLUMNS else 1
                if row[2] != expected_type or row[3] != expected_not_null or row[4] is not None:
                    raise KnowledgeStoreIntegrityError(
                        f"knowledge store {table}.{column} shape is incompatible"
                    )
        metadata = connection.execute(
            "SELECT singleton, schema_version FROM knowledge_store_metadata"
        ).fetchall()
        if len(metadata) != 1 or metadata[0]["singleton"] != 1:
            raise KnowledgeStoreIntegrityError("knowledge store schema metadata is malformed")
        if metadata[0]["schema_version"] != SCHEMA_VERSION:
            raise UnsupportedKnowledgeStoreVersion("knowledge store schema version is unsupported")
        expected_primary_keys = {
            "knowledge_store_metadata": ("singleton",),
            "build_lineage": ("build_identity",),
            "activation_receipts": ("operation_id",),
            "activation_authority": ("singleton",),
            "operation_envelopes": ("operation_id",),
            "snapshot_envelopes": ("snapshot_id",),
            "retention_holds": ("hold_id",),
        }
        for table, expected in expected_primary_keys.items():
            primary = tuple(
                row[1]
                for row in sorted(
                    connection.execute(f'PRAGMA table_info("{table}")'), key=lambda row: row[5]
                )
                if row[5]
            )
            if primary != expected:
                raise KnowledgeStoreIntegrityError(f"knowledge store {table} primary key is incompatible")
        expected_unique = {
            ("build_lineage", ("build_identity",)),
            ("activation_receipts", ("operation_id",)),
            ("activation_authority", ("operation_id",)),
            ("operation_envelopes", ("operation_id",)),
            ("operation_envelopes", ("snapshot_id",)),
            ("snapshot_envelopes", ("snapshot_id",)),
            ("snapshot_envelopes", ("operation_id",)),
            ("retention_holds", ("hold_id",)),
        }
        actual_unique: set[tuple[str, tuple[str, ...]]] = set()
        for table in _TABLE_COLUMNS:
            for index in connection.execute(f'PRAGMA index_list("{table}")'):
                if index[2]:
                    columns = tuple(
                        row[2] for row in connection.execute(f'PRAGMA index_info("{index[1]}")')
                    )
                    actual_unique.add((table, columns))
        if actual_unique != expected_unique:
            raise KnowledgeStoreIntegrityError("knowledge store unique constraints are incompatible")
        required_foreign_keys = {
            ("activation_receipts", "requested_build_identity", "build_lineage", "build_identity"),
            ("activation_authority", "operation_id", "activation_receipts", "operation_id"),
            ("activation_authority", "active_build_identity", "build_lineage", "build_identity"),
            ("operation_envelopes", "frozen_build_identity", "build_lineage", "build_identity"),
            ("snapshot_envelopes", "operation_id", "operation_envelopes", "operation_id"),
            ("snapshot_envelopes", "frozen_build_identity", "build_lineage", "build_identity"),
        }
        actual_foreign_keys = {
            (table, row[3], row[2], row[4])
            for table in _TABLE_COLUMNS
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        if actual_foreign_keys != required_foreign_keys:
            raise KnowledgeStoreIntegrityError("knowledge store foreign keys are incompatible")

    def _assert_integrity(self) -> None:
        connection = self._require_connection()
        result = connection.execute("PRAGMA integrity_check").fetchall()
        if len(result) != 1 or result[0][0] != "ok":
            raise KnowledgeStoreIntegrityError("knowledge store integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise KnowledgeStoreIntegrityError("knowledge store foreign key check failed")
        versioned_tables = (
            "build_lineage", "activation_receipts", "activation_authority",
            "operation_envelopes", "snapshot_envelopes", "retention_holds",
        )
        for table in versioned_tables:
            if connection.execute(
                f'SELECT 1 FROM "{table}" WHERE record_version != ? LIMIT 1',
                (RECORD_VERSION,),
            ).fetchone() is not None:
                raise KnowledgeStoreIntegrityError("knowledge store record version is unsupported")
        broken_activation = connection.execute(
            """SELECT 1 FROM activation_authority AS a
               LEFT JOIN activation_receipts AS r ON r.operation_id = a.operation_id
               LEFT JOIN build_lineage AS b ON b.build_identity = a.active_build_identity
               WHERE r.operation_id IS NULL OR b.build_identity IS NULL
                  OR r.requested_build_identity != a.active_build_identity
                  OR r.committed_generation != a.generation
                  OR r.validated_build_commitment != a.validated_build_commitment
                  OR r.result_commitment != a.result_commitment
               LIMIT 1"""
        ).fetchone()
        if broken_activation is not None:
            raise KnowledgeStoreIntegrityError("activation authority lineage is inconsistent")
        broken_operation = connection.execute(
            """SELECT 1 FROM operation_envelopes AS o
               LEFT JOIN snapshot_envelopes AS s ON s.operation_id = o.operation_id
               WHERE (o.completed = 1 AND
                      (s.snapshot_id IS NULL OR s.snapshot_id != o.snapshot_id
                       OR s.frozen_build_identity != o.frozen_build_identity))
                  OR (o.completed = 0 AND s.snapshot_id IS NOT NULL)
               LIMIT 1"""
        ).fetchone()
        if broken_operation is not None:
            raise KnowledgeStoreIntegrityError("operation and Snapshot mapping is inconsistent")
        self._validate_durable_rows()

    def _validate_durable_rows(self) -> None:
        """Decode every authoritative row through Candidate-C-owned contracts."""
        connection = self._require_connection()
        try:
            builds = {
                record.build_identity: record
                for record in (
                    self._decode_build(row)
                    for row in connection.execute("SELECT * FROM build_lineage")
                )
            }

            receipt_generations: set[int] = set()
            for row in connection.execute("SELECT * FROM activation_receipts"):
                receipt = self._activation_from_receipt(row)
                expected_generation = row["expected_generation"]
                if (
                    type(expected_generation) is not int
                    or expected_generation < 0
                    or receipt.generation != expected_generation + 1
                    or receipt.generation in receipt_generations
                ):
                    raise ValueError("activation receipt generation is inconsistent")
                receipt_generations.add(receipt.generation)

            activation_generation: int | None = None
            for row in connection.execute("SELECT * FROM activation_authority"):
                activation_generation = self._decode_activation(row).generation
            expected_receipt_generations = (
                set() if activation_generation is None else set(range(1, activation_generation + 1))
            )
            if receipt_generations != expected_receipt_generations:
                raise ValueError("activation receipt history is inconsistent")

            for row in connection.execute("SELECT * FROM operation_envelopes"):
                self._decode_operation(row)

            for row in connection.execute("SELECT * FROM snapshot_envelopes"):
                snapshot = self._decode_snapshot(row)
                build = builds.get(snapshot.frozen_build_identity)
                if build is None or snapshot.lineage_commitment != build.lineage_commitment:
                    raise ValueError("Snapshot lineage does not match its durable build")

            for row in connection.execute("SELECT * FROM retention_holds"):
                self._decode_hold(row)
        except (ValueError, TypeError, KeyError) as exc:
            raise KnowledgeStoreIntegrityError(
                "knowledge store contains a semantically invalid durable record"
            ) from exc

    @contextmanager
    def _transaction(self, *, immediate: bool) -> Iterator[sqlite3.Connection]:
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            if immediate:
                self._assert_integrity()
            yield connection
        except Exception:
            connection.rollback()
            raise
        try:
            connection.commit()
        except sqlite3.Error as exc:
            raise KnowledgeStoreCommitOutcomeUnknown(
                "commit outcome unknown; resolve using the same durable operation key"
            ) from exc

    def create_build_lineage(self, record: BuildLineageRecord) -> BuildLineageRecord:
        if not isinstance(record, BuildLineageRecord):
            raise TypeError("record must be a BuildLineageRecord")
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM build_lineage WHERE build_identity = ?", (record.build_identity,)
            ).fetchone()
            if row is not None:
                existing = self._decode_build(row)
                if existing == record:
                    return existing
                raise KnowledgeStoreConflictError("contradictory build lineage replay")
            connection.execute(
                "INSERT INTO build_lineage VALUES (?, ?, ?, ?)",
                (record.build_identity, record.record_version, record.manifest_commitment,
                 record.lineage_commitment),
            )
        return record

    def get_build_lineage(self, build_identity: str) -> KnowledgeReadResult:
        return self._read_one(
            "build_lineage", build_identity,
            "SELECT * FROM build_lineage WHERE build_identity = ?", self._decode_build,
        )

    def commit_activation(
        self, record: ActivationAuthorityRecord, *, expected_generation: int
    ) -> ActivationAuthorityRecord:
        if not isinstance(record, ActivationAuthorityRecord):
            raise TypeError("record must be an ActivationAuthorityRecord")
        if isinstance(expected_generation, bool) or not isinstance(expected_generation, int) or expected_generation < 0:
            raise ValueError("expected_generation must be a non-negative integer")
        with self._transaction(immediate=True) as connection:
            receipt = connection.execute(
                "SELECT * FROM activation_receipts WHERE operation_id = ?",
                (record.operation_key.value,),
            ).fetchone()
            if receipt is not None:
                existing = self._activation_from_receipt(receipt)
                if existing == record and receipt["expected_generation"] == expected_generation:
                    return existing
                raise KnowledgeStoreConflictError("contradictory activation operation replay")
            if connection.execute(
                "SELECT 1 FROM build_lineage WHERE build_identity = ?", (record.active_build_identity,)
            ).fetchone() is None:
                raise KnowledgeStoreConflictError("activation target build is not durable")
            current = connection.execute("SELECT generation FROM activation_authority WHERE singleton = 1").fetchone()
            current_generation = 0 if current is None else current["generation"]
            if current_generation != expected_generation:
                raise KnowledgeStoreConcurrencyError("activation generation changed")
            if record.generation != expected_generation + 1:
                raise KnowledgeStoreConflictError("committed generation is not the next generation")
            values = (
                record.operation_key.value, record.record_version, record.active_build_identity,
                expected_generation, record.generation, record.validated_build_commitment,
                record.result_commitment,
            )
            connection.execute("INSERT INTO activation_receipts VALUES (?, ?, ?, ?, ?, ?, ?)", values)
            connection.execute(
                """INSERT INTO activation_authority VALUES (1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(singleton) DO UPDATE SET
                     record_version=excluded.record_version, generation=excluded.generation,
                     operation_id=excluded.operation_id, active_build_identity=excluded.active_build_identity,
                     validated_build_commitment=excluded.validated_build_commitment,
                     result_commitment=excluded.result_commitment""",
                (record.record_version, record.generation, record.operation_key.value,
                 record.active_build_identity, record.validated_build_commitment, record.result_commitment),
            )
        return record

    def read_activation(self) -> KnowledgeReadResult:
        try:
            with self._transaction(immediate=False) as connection:
                self._assert_integrity()
                row = connection.execute("SELECT * FROM activation_authority WHERE singleton = 1").fetchone()
                if row is None:
                    return KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND)
                try:
                    record = self._decode_activation(row)
                except (ValueError, TypeError, KeyError):
                    return self._repair_result("MALFORMED_ACTIVATION", "activation", "singleton")
                build = connection.execute(
                    "SELECT * FROM build_lineage WHERE build_identity = ?", (record.active_build_identity,)
                ).fetchone()
                receipt = connection.execute(
                    "SELECT * FROM activation_receipts WHERE operation_id = ?", (record.operation_key.value,)
                ).fetchone()
                if build is None or receipt is None or self._activation_from_receipt(receipt) != record:
                    return self._repair_result("BROKEN_ACTIVATION", "activation", "singleton")
                return KnowledgeReadResult(KnowledgeReadStatus.FOUND, record)
        except KnowledgeStoreIntegrityError:
            return self._repair_result("STORE_INTEGRITY", "store", "singleton")
        except sqlite3.Error:
            return KnowledgeReadResult(KnowledgeReadStatus.UNAVAILABLE)

    def local_readiness(self) -> KnowledgeReadinessFact:
        result = self.read_activation()
        if result.status is KnowledgeReadStatus.FOUND:
            activation = result.value
            assert isinstance(activation, ActivationAuthorityRecord)
            return KnowledgeReadinessFact(
                KnowledgeLocalReadiness.READY,
                active_build_identity=activation.active_build_identity,
                generation=activation.generation,
            )
        mapping = {
            KnowledgeReadStatus.NOT_FOUND: KnowledgeLocalReadiness.NOT_INITIALIZED,
            KnowledgeReadStatus.UNAVAILABLE: KnowledgeLocalReadiness.UNAVAILABLE,
            KnowledgeReadStatus.INVALID: KnowledgeLocalReadiness.MISMATCH,
            KnowledgeReadStatus.REPAIR_REQUIRED: KnowledgeLocalReadiness.REPAIR_REQUIRED,
        }
        return KnowledgeReadinessFact(mapping[result.status], findings=result.findings)

    def create_operation(self, envelope: OperationEnvelope) -> OperationEnvelope:
        if not isinstance(envelope, OperationEnvelope) or envelope.completed:
            raise ValueError("create_operation requires an incomplete OperationEnvelope")
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM operation_envelopes WHERE operation_id = ?", (envelope.operation_key.value,)
            ).fetchone()
            if row is not None:
                existing = self._decode_operation(row)
                if existing == envelope:
                    return existing
                raise KnowledgeStoreConflictError("contradictory operation replay")
            if envelope.frozen_build_identity is not None and connection.execute(
                "SELECT 1 FROM build_lineage WHERE build_identity = ?", (envelope.frozen_build_identity,)
            ).fetchone() is None:
                raise KnowledgeStoreConflictError("operation build is not durable")
            connection.execute(
                "INSERT INTO operation_envelopes VALUES (?, ?, ?, ?, NULL, 0, ?)",
                (envelope.operation_key.value, envelope.record_version, envelope.semantic_commitment,
                 envelope.frozen_build_identity, envelope.revision),
            )
        return envelope

    def complete_operation_with_snapshot(
        self,
        snapshot: KnowledgeSnapshotEnvelope,
        *,
        expected_revision: int,
    ) -> OperationEnvelope:
        if not isinstance(snapshot, KnowledgeSnapshotEnvelope):
            raise TypeError("snapshot must be a KnowledgeSnapshotEnvelope")
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM operation_envelopes WHERE operation_id = ?", (snapshot.operation_key.value,)
            ).fetchone()
            if row is None:
                raise KnowledgeStoreConflictError("operation does not exist")
            operation = self._decode_operation(row)
            if operation.completed:
                existing = connection.execute(
                    "SELECT * FROM snapshot_envelopes WHERE operation_id = ?", (snapshot.operation_key.value,)
                ).fetchone()
                if existing is not None and self._decode_snapshot(existing) == snapshot:
                    return operation
                raise KnowledgeStoreConflictError("contradictory Snapshot replay")
            if operation.revision != expected_revision:
                raise KnowledgeStoreConcurrencyError("operation revision changed")
            if operation.frozen_build_identity != snapshot.frozen_build_identity:
                raise KnowledgeStoreConflictError("Snapshot build does not match frozen operation build")
            build = connection.execute(
                "SELECT * FROM build_lineage WHERE build_identity = ?", (snapshot.frozen_build_identity,)
            ).fetchone()
            if build is None or self._decode_build(build).lineage_commitment != snapshot.lineage_commitment:
                raise KnowledgeStoreConflictError("Snapshot lineage does not match durable build lineage")
            connection.execute(
                "INSERT INTO snapshot_envelopes VALUES (?, ?, ?, ?, ?, ?)",
                (snapshot.snapshot_key.value, snapshot.record_version, snapshot.operation_key.value,
                 snapshot.frozen_build_identity, snapshot.snapshot_commitment, snapshot.lineage_commitment),
            )
            cursor = connection.execute(
                """UPDATE operation_envelopes SET snapshot_id = ?, completed = 1, revision = revision + 1
                   WHERE operation_id = ? AND revision = ? AND completed = 0""",
                (snapshot.snapshot_key.value, snapshot.operation_key.value, expected_revision),
            )
            if cursor.rowcount != 1:
                raise KnowledgeStoreConcurrencyError("operation completion lost its revision guard")
            completed = connection.execute(
                "SELECT * FROM operation_envelopes WHERE operation_id = ?", (snapshot.operation_key.value,)
            ).fetchone()
            assert completed is not None
            return self._decode_operation(completed)

    def get_operation(self, key: RetrievalOperationKey) -> KnowledgeReadResult:
        return self._read_one(
            "operation", key.value,
            "SELECT * FROM operation_envelopes WHERE operation_id = ?", self._decode_operation,
        )

    def get_snapshot(self, key: KnowledgeSnapshotKey) -> KnowledgeReadResult:
        return self._read_one(
            "snapshot", key.value,
            "SELECT * FROM snapshot_envelopes WHERE snapshot_id = ?", self._decode_snapshot,
        )

    def create_retention_hold(self, record: RetentionHoldRecord) -> RetentionHoldRecord:
        if not isinstance(record, RetentionHoldRecord) or record.status is not RetentionHoldStatus.ACTIVE:
            raise ValueError("new retention hold must be ACTIVE")
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM retention_holds WHERE hold_id = ?", (record.hold_key.value,)
            ).fetchone()
            if row is not None:
                existing = self._decode_hold(row)
                if existing == record:
                    return existing
                raise KnowledgeStoreConflictError("contradictory retention hold replay")
            connection.execute(
                "INSERT INTO retention_holds VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.hold_key.value, record.record_version, record.subject_kind.value,
                 record.subject_identity, record.owner.reference_type.value, record.owner.value,
                 record.obligation_kind.value, record.semantic_commitment, record.status.value,
                 record.revision),
            )
        return record

    def release_retention_hold(
        self, key: RetentionHoldKey, *, expected_revision: int
    ) -> RetentionHoldRecord:
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM retention_holds WHERE hold_id = ?", (key.value,)
            ).fetchone()
            if row is None:
                raise KnowledgeStoreConflictError("retention hold does not exist")
            existing = self._decode_hold(row)
            if existing.status is RetentionHoldStatus.RELEASED:
                return existing
            if existing.revision != expected_revision:
                raise KnowledgeStoreConcurrencyError("retention hold revision changed")
            connection.execute(
                "UPDATE retention_holds SET status = 'RELEASED', revision = revision + 1 WHERE hold_id = ?",
                (key.value,),
            )
            return replace(existing, status=RetentionHoldStatus.RELEASED, revision=existing.revision + 1)

    def get_retention_hold(self, key: RetentionHoldKey) -> KnowledgeReadResult:
        return self._read_one(
            "retention", key.value,
            "SELECT * FROM retention_holds WHERE hold_id = ?", self._decode_hold,
        )

    def cleanup_eligible(self, subject_kind: RetentionSubjectKind, subject_identity: str) -> bool:
        with self._transaction(immediate=False) as connection:
            self._assert_integrity()
            row = connection.execute(
                """SELECT 1 FROM retention_holds
                   WHERE subject_kind = ? AND subject_id = ? AND status = 'ACTIVE' LIMIT 1""",
                (subject_kind.value, subject_identity),
            ).fetchone()
            return row is None

    def _read_one(self, kind: str, key: str, sql: str, decoder: object) -> KnowledgeReadResult:
        try:
            with self._transaction(immediate=False) as connection:
                self._assert_integrity()
                row = connection.execute(sql, (key,)).fetchone()
                if row is None:
                    return KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND)
                try:
                    value = decoder(row)  # type: ignore[operator]
                except (ValueError, TypeError, KeyError):
                    return self._repair_result("MALFORMED_RECORD", kind, key)
                return KnowledgeReadResult(KnowledgeReadStatus.FOUND, value)
        except KnowledgeStoreIntegrityError:
            return self._repair_result("STORE_INTEGRITY", "store", "singleton")
        except sqlite3.Error:
            return KnowledgeReadResult(KnowledgeReadStatus.UNAVAILABLE)

    @staticmethod
    def _repair_result(code: str, kind: str, key: str) -> KnowledgeReadResult:
        return KnowledgeReadResult(
            KnowledgeReadStatus.REPAIR_REQUIRED,
            findings=(_finding(code, kind, key, "durable state failed Candidate-C integrity validation"),),
        )

    @staticmethod
    def _decode_build(row: sqlite3.Row) -> BuildLineageRecord:
        return BuildLineageRecord(row["build_identity"], row["manifest_commitment"],
                                  row["lineage_commitment"], row["record_version"])

    @staticmethod
    def _activation_from_receipt(row: sqlite3.Row) -> ActivationAuthorityRecord:
        return ActivationAuthorityRecord(
            row["committed_generation"], ActivationOperationKey(row["operation_id"]),
            row["requested_build_identity"], row["validated_build_commitment"],
            row["result_commitment"], row["record_version"],
        )

    @staticmethod
    def _decode_activation(row: sqlite3.Row) -> ActivationAuthorityRecord:
        return ActivationAuthorityRecord(
            row["generation"], ActivationOperationKey(row["operation_id"]),
            row["active_build_identity"], row["validated_build_commitment"],
            row["result_commitment"], row["record_version"],
        )

    @staticmethod
    def _decode_operation(row: sqlite3.Row) -> OperationEnvelope:
        if type(row["completed"]) is not int or row["completed"] not in (0, 1):
            raise ValueError("operation completion state is invalid")
        snapshot = KnowledgeSnapshotKey(row["snapshot_id"]) if row["snapshot_id"] is not None else None
        return OperationEnvelope(
            RetrievalOperationKey(row["operation_id"]), row["semantic_commitment"],
            row["frozen_build_identity"], snapshot, bool(row["completed"]), row["revision"],
            row["record_version"],
        )

    @staticmethod
    def _decode_snapshot(row: sqlite3.Row) -> KnowledgeSnapshotEnvelope:
        return KnowledgeSnapshotEnvelope(
            KnowledgeSnapshotKey(row["snapshot_id"]), RetrievalOperationKey(row["operation_id"]),
            row["frozen_build_identity"], row["snapshot_commitment"], row["lineage_commitment"],
            row["record_version"],
        )

    @staticmethod
    def _decode_hold(row: sqlite3.Row) -> RetentionHoldRecord:
        return RetentionHoldRecord(
            RetentionHoldKey(row["hold_id"]), RetentionSubjectKind(row["subject_kind"]),
            row["subject_id"], OpaqueExternalReference(OpaqueReferenceType(row["owner_type"]), row["owner_value"]),
            RetentionObligationKind(row["obligation_kind"]), row["semantic_commitment"],
            RetentionHoldStatus(row["status"]), row["revision"], row["record_version"],
        )
