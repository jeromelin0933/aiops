"""Independent durable authority for SPEC-014 Candidate C.

This module persists facts selected by a semantic layer.  It intentionally does
not decide build eligibility, retrieval outcomes, retention policy, or runtime
work.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
from typing import Iterator

from .contracts import (
    ActivationAuthorityRecord,
    ActivationOperationKey,
    ArtifactTrust,
    BuildChunk,
    BuildDocumentProvenance,
    BuildFailureCode,
    BuildIdentityInput,
    BuildLineageRecord,
    BuildManifestProvenance,
    BuildOperationKey,
    BuildOperationClaim,
    BuildStageState,
    BuildValidationFinding,
    BuildValidationRecord,
    BuildValidationState,
    ContentType,
    DocumentStatus,
    DurableRetrievalCompletion,
    KnowledgeCorruptionFinding,
    KnowledgeLocalReadiness,
    KnowledgeReadinessFact,
    KnowledgeReadResult,
    KnowledgeReadStatus,
    KnowledgeSnapshotEnvelope,
    KnowledgeSnapshotKey,
    KnowledgeSnapshotChunk,
    MetadataItem,
    IndexArtifactFacts,
    IndexEntryFact,
    OpaqueExternalReference,
    OpaqueReferenceType,
    OperationEnvelope,
    RetentionHoldKey,
    RetentionHoldRecord,
    RetentionHoldStatus,
    RetentionObligationKind,
    RetentionSubjectKind,
    RetrievalOperationKey,
    RetrievalOperationRead,
    RetrievalOperationState,
    RetrievalRecoveryFact,
    RetrievalFailureCode,
    RetrievalFailureFact,
    RetrievalResolution,
    RetrievalApplicability,
    RetrievalEvaluationDisposition,
    RetrievalEvaluationFact,
    RetrievalRejectionReason,
    ApplicabilityRuleFact,
    ApplicabilityRuleIdentity,
    RetrySafetyDisposition,
    SnapshotFinalizationKey,
    SnapshotSourceStatus,
    RetrievalOperationRequest,
    FrozenRetrievalOperation,
    RetrievalProfile,
    RetrievalScoreDirection,
    RetrievalQueryDisposition,
    ApplicabilityPolicy,
    CanonicalKnowledgeQuery,
    QueryFilter,
    RawRetrievalBatch,
    RawRetrievalCandidate,
    SourceClassification,
    StagedBuildRecord,
)
from .build_validation import (
    derive_build_lineage_commitment,
    derive_build_operation_commitment,
    derive_build_validation_commitment,
    derive_chunk_metadata_commitment,
    derive_staged_build_commitment,
)
from .identity import build_identity, canonical_serialize
from .retrieval_resolution import derive_retrieval_operation_commitment
from .snapshot import (
    derive_retrieval_completion_commitment,
    derive_recovery_commitment,
    derive_snapshot_commitment,
    validate_retrieval_completion_authority,
    validate_snapshot_against_authority,
)


SCHEMA_VERSION = 4
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
        "snapshot_commitment", "lineage_commitment", "resolution", "source_status",
        "finalization_id", "payload_json",
    ),
    "retrieval_completion_facts": (
        "operation_id", "record_version", "semantic_commitment", "resolution",
        "candidate_count", "payload_json",
    ),
    "retrieval_recovery_facts": (
        "operation_id", "record_version", "semantic_commitment", "revision", "payload_json",
    ),
    "retention_holds": (
        "hold_id", "record_version", "subject_kind", "subject_id", "owner_type", "owner_value",
        "obligation_kind", "semantic_commitment", "status", "revision",
    ),
    "build_operation_claims": (
        "operation_id", "record_version", "semantic_commitment", "build_identity",
        "profile_type", "profile_value", "capability_identity",
    ),
    "staged_builds": (
        "build_identity", "record_version", "operation_id", "manifest_commitment",
        "lineage_commitment", "staged_commitment", "payload_json",
    ),
    "build_validations": (
        "build_identity", "record_version", "operation_id", "staged_commitment",
        "validation_commitment", "validation_state", "payload_json",
    ),
    "frozen_retrieval_operations": (
        "operation_id", "record_version", "semantic_commitment", "frozen_build_identity",
        "activation_generation", "activation_operation_id", "validation_commitment",
        "staged_commitment", "artifact_commitment", "lineage_commitment", "revision",
        "payload_json",
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
    ("snapshot_envelopes", "finalization_id"),
    ("retrieval_completion_facts", "operation_id"),
    ("retention_holds", "hold_id"),
    ("build_operation_claims", "operation_id"),
    ("staged_builds", "build_identity"),
    ("build_validations", "build_identity"),
    ("frozen_retrieval_operations", "operation_id"),
    ("retrieval_recovery_facts", "operation_id"),
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
    ("build_operation_claims", "record_version"),
    ("staged_builds", "record_version"),
    ("build_validations", "record_version"),
    ("frozen_retrieval_operations", "record_version"),
    ("frozen_retrieval_operations", "activation_generation"),
    ("frozen_retrieval_operations", "revision"),
    ("retrieval_recovery_facts", "record_version"),
    ("retrieval_recovery_facts", "revision"),
    ("retrieval_completion_facts", "record_version"),
    ("retrieval_completion_facts", "candidate_count"),
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
    lineage_commitment TEXT NOT NULL,
    resolution TEXT NOT NULL CHECK (resolution IN ('MATCH', 'NO_MATCH', 'RETRIEVAL_UNAVAILABLE')),
    source_status TEXT NOT NULL CHECK (source_status IN ('AVAILABLE', 'UNAVAILABLE')),
    finalization_id TEXT UNIQUE,
    payload_json TEXT NOT NULL
);
CREATE TABLE retrieval_completion_facts (
    operation_id TEXT PRIMARY KEY REFERENCES frozen_retrieval_operations(operation_id),
    record_version INTEGER NOT NULL CHECK (record_version = 1),
    semantic_commitment TEXT NOT NULL UNIQUE,
    resolution TEXT NOT NULL CHECK (resolution IN ('MATCH', 'NO_MATCH')),
    candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
    payload_json TEXT NOT NULL
);
CREATE TABLE retrieval_recovery_facts (
    operation_id TEXT PRIMARY KEY REFERENCES frozen_retrieval_operations(operation_id),
    record_version INTEGER NOT NULL CHECK (record_version = 1),
    semantic_commitment TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    payload_json TEXT NOT NULL
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
CREATE TABLE build_operation_claims (
    operation_id TEXT PRIMARY KEY,
    record_version INTEGER NOT NULL CHECK (record_version = 1),
    semantic_commitment TEXT NOT NULL,
    build_identity TEXT NOT NULL UNIQUE,
    profile_type TEXT NOT NULL,
    profile_value TEXT NOT NULL,
    capability_identity TEXT NOT NULL
);
CREATE TABLE staged_builds (
    build_identity TEXT PRIMARY KEY REFERENCES build_lineage(build_identity),
    record_version INTEGER NOT NULL CHECK (record_version = 1),
    operation_id TEXT NOT NULL UNIQUE REFERENCES build_operation_claims(operation_id),
    manifest_commitment TEXT NOT NULL,
    lineage_commitment TEXT NOT NULL,
    staged_commitment TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE build_validations (
    build_identity TEXT PRIMARY KEY REFERENCES staged_builds(build_identity),
    record_version INTEGER NOT NULL CHECK (record_version = 1),
    operation_id TEXT NOT NULL UNIQUE,
    staged_commitment TEXT NOT NULL,
    validation_commitment TEXT NOT NULL,
    validation_state TEXT NOT NULL CHECK (validation_state IN ('VALIDATED', 'FAILED')),
    payload_json TEXT NOT NULL
);
CREATE TABLE frozen_retrieval_operations (
    operation_id TEXT PRIMARY KEY REFERENCES operation_envelopes(operation_id),
    record_version INTEGER NOT NULL CHECK (record_version = 1),
    semantic_commitment TEXT NOT NULL,
    frozen_build_identity TEXT NOT NULL REFERENCES staged_builds(build_identity),
    activation_generation INTEGER NOT NULL CHECK (activation_generation > 0),
    activation_operation_id TEXT NOT NULL REFERENCES activation_receipts(operation_id),
    validation_commitment TEXT NOT NULL,
    staged_commitment TEXT NOT NULL,
    artifact_commitment TEXT NOT NULL,
    lineage_commitment TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision = 1),
    payload_json TEXT NOT NULL
);
INSERT INTO knowledge_store_metadata(singleton, schema_version) VALUES (1, 4);
"""


def _normalize_schema_sql(value: str) -> str:
    output: list[str] = []
    in_literal = False
    index = 0
    while index < len(value):
        character = value[index]
        if character == "'":
            output.append(character)
            if in_literal and index + 1 < len(value) and value[index + 1] == "'":
                output.append("'")
                index += 2
                continue
            in_literal = not in_literal
        elif in_literal:
            output.append(character)
        elif character.isspace() or character in '`"[]':
            pass
        else:
            output.append(character.lower())
        index += 1
    return "".join(output).rstrip(";")


@lru_cache(maxsize=1)
def _expected_table_sql() -> dict[str, str]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_CREATE_SCHEMA)
        return {
            name: _normalize_schema_sql(sql)
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()


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
        actual_table_sql = {
            row["name"]: _normalize_schema_sql(row["sql"])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if actual_table_sql != _expected_table_sql():
            raise KnowledgeStoreIntegrityError(
                "knowledge store table constraints are incompatible"
            )
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
            "retrieval_completion_facts": ("operation_id",),
            "retention_holds": ("hold_id",),
            "build_operation_claims": ("operation_id",),
            "staged_builds": ("build_identity",),
            "build_validations": ("build_identity",),
            "frozen_retrieval_operations": ("operation_id",),
            "retrieval_recovery_facts": ("operation_id",),
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
            ("snapshot_envelopes", ("finalization_id",)),
            ("retrieval_completion_facts", ("operation_id",)),
            ("retrieval_completion_facts", ("semantic_commitment",)),
            ("retention_holds", ("hold_id",)),
            ("build_operation_claims", ("operation_id",)),
            ("build_operation_claims", ("build_identity",)),
            ("staged_builds", ("build_identity",)),
            ("staged_builds", ("operation_id",)),
            ("build_validations", ("build_identity",)),
            ("build_validations", ("operation_id",)),
            ("frozen_retrieval_operations", ("operation_id",)),
            ("retrieval_recovery_facts", ("operation_id",)),
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
            ("staged_builds", "build_identity", "build_lineage", "build_identity"),
            ("staged_builds", "operation_id", "build_operation_claims", "operation_id"),
            ("build_validations", "build_identity", "staged_builds", "build_identity"),
            ("frozen_retrieval_operations", "operation_id", "operation_envelopes", "operation_id"),
            ("frozen_retrieval_operations", "frozen_build_identity", "staged_builds", "build_identity"),
            ("frozen_retrieval_operations", "activation_operation_id", "activation_receipts", "operation_id"),
            ("retrieval_completion_facts", "operation_id", "frozen_retrieval_operations", "operation_id"),
            ("retrieval_recovery_facts", "operation_id", "frozen_retrieval_operations", "operation_id"),
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
            "build_operation_claims", "staged_builds", "build_validations",
            "frozen_retrieval_operations",
            "retrieval_completion_facts",
            "retrieval_recovery_facts",
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

            snapshots: dict[str, KnowledgeSnapshotEnvelope] = {}
            for row in connection.execute("SELECT * FROM snapshot_envelopes"):
                snapshot = self._decode_snapshot(row)
                build = builds.get(snapshot.frozen_build_identity)
                if build is None or snapshot.lineage_commitment != build.lineage_commitment:
                    raise ValueError("Snapshot lineage does not match its durable build")
                snapshots[snapshot.operation_key.value] = snapshot

            for row in connection.execute("SELECT * FROM retention_holds"):
                self._decode_hold(row)

            operation_claims: dict[str, BuildOperationClaim] = {}
            for row in connection.execute("SELECT * FROM build_operation_claims"):
                claim = self._decode_build_operation_claim(row)
                if claim.semantic_commitment != derive_build_operation_commitment(
                    claim.operation_key,
                    claim.build_identity,
                    claim.profile_reference,
                    claim.capability_identity,
                ):
                    raise ValueError("build operation claim commitment is inconsistent")
                operation_claims[claim.operation_key.value] = claim

            staged_builds: dict[str, StagedBuildRecord] = {}
            for row in connection.execute("SELECT * FROM staged_builds"):
                staged = self._decode_staged_build(row)
                lineage = builds.get(staged.build_identity)
                claim = operation_claims.get(staged.operation_key.value)
                if (
                    lineage is None
                    or claim is None
                    or claim.build_identity != staged.build_identity
                    or claim.profile_reference != staged.profile_reference
                    or claim.capability_identity != staged.capability_identity
                    or lineage.manifest_commitment != staged.manifest_commitment
                    or lineage.lineage_commitment != staged.lineage_commitment
                ):
                    raise ValueError("staged build does not match immutable build lineage")
                _validate_staged_record_semantics(staged)
                staged_builds[staged.build_identity] = staged

            for row in connection.execute("SELECT * FROM build_validations"):
                validation = self._decode_build_validation(row)
                staged = staged_builds.get(validation.build_identity)
                if staged is None or validation.staged_commitment != staged.staged_commitment:
                    raise ValueError("build validation does not match its staged build")
                _validate_build_validation_semantics(validation)

            frozen_operations: dict[str, FrozenRetrievalOperation] = {}
            for row in connection.execute("SELECT * FROM frozen_retrieval_operations"):
                frozen = self._decode_frozen_retrieval(row)
                staged = staged_builds.get(frozen.frozen_build_identity)
                validation_row = connection.execute(
                    "SELECT * FROM build_validations WHERE build_identity = ?",
                    (frozen.frozen_build_identity,),
                ).fetchone()
                receipt_row = connection.execute(
                    "SELECT * FROM activation_receipts WHERE operation_id = ?",
                    (frozen.activation_operation_key.value,),
                ).fetchone()
                operation_row = connection.execute(
                    "SELECT * FROM operation_envelopes WHERE operation_id = ?",
                    (frozen.request.operation_key.value,),
                ).fetchone()
                if staged is None or validation_row is None or receipt_row is None or operation_row is None:
                    raise ValueError("frozen retrieval lineage is incomplete")
                validation = self._decode_build_validation(validation_row)
                receipt = self._activation_from_receipt(receipt_row)
                operation = self._decode_operation(operation_row)
                if (
                    derive_retrieval_operation_commitment(frozen.request)
                    != frozen.semantic_commitment
                    or operation.semantic_commitment != frozen.semantic_commitment
                    or operation.frozen_build_identity != frozen.frozen_build_identity
                    or (operation.completed and operation.snapshot_key is None)
                    or receipt.generation != frozen.activation_generation
                    or receipt.active_build_identity != frozen.frozen_build_identity
                    or receipt.validated_build_commitment != frozen.validation_commitment
                    or validation.state is not BuildValidationState.VALIDATED
                    or validation.validation_commitment != frozen.validation_commitment
                    or validation.staged_commitment != frozen.staged_commitment
                    or staged.staged_commitment != frozen.staged_commitment
                    or staged.artifact.artifact_commitment != frozen.artifact_commitment
                    or staged.lineage_commitment != frozen.lineage_commitment
                    or not _retrieval_compatibility_matches(frozen.request, staged)
                ):
                    raise ValueError("frozen retrieval semantic lineage is inconsistent")
                frozen_operations[frozen.request.operation_key.value] = frozen

            completions: dict[str, DurableRetrievalCompletion] = {}
            for row in connection.execute("SELECT * FROM retrieval_completion_facts"):
                completion = self._decode_retrieval_completion(row)
                frozen = frozen_operations.get(completion.operation_key.value)
                staged = staged_builds.get(completion.frozen_build_identity)
                if frozen is None or staged is None:
                    raise ValueError("retrieval completion frozen lineage is incomplete")
                validate_retrieval_completion_authority(completion, frozen, staged)
                completions[completion.operation_key.value] = completion

            for operation_id, snapshot in snapshots.items():
                frozen = frozen_operations.get(operation_id)
                staged = staged_builds.get(snapshot.frozen_build_identity)
                if frozen is None or staged is None:
                    raise ValueError("Snapshot frozen lineage is incomplete")
                validate_snapshot_against_authority(
                    snapshot, frozen, staged, completions.get(operation_id)
                )

            for row in connection.execute("SELECT * FROM retrieval_recovery_facts"):
                recovery = self._decode_recovery(row)
                if recovery.operation_key.value not in frozen_operations:
                    raise ValueError("recovery fact lacks frozen operation")
                if recovery.semantic_commitment != derive_recovery_commitment(
                    recovery.operation_key, recovery.failures
                ):
                    raise ValueError("recovery commitment is inconsistent")
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

    def claim_build_operation(self, claim: BuildOperationClaim) -> BuildOperationClaim:
        if not isinstance(claim, BuildOperationClaim):
            raise TypeError("claim must be a BuildOperationClaim")
        _validate_build_operation_claim_semantics(claim)
        with self._transaction(immediate=True) as connection:
            operation_row = connection.execute(
                "SELECT * FROM build_operation_claims WHERE operation_id = ?",
                (claim.operation_key.value,),
            ).fetchone()
            if operation_row is not None:
                existing = self._decode_build_operation_claim(operation_row)
                if existing == claim:
                    return existing
                raise KnowledgeStoreConflictError("contradictory build operation replay")
            build_row = connection.execute(
                "SELECT * FROM build_operation_claims WHERE build_identity = ?",
                (claim.build_identity,),
            ).fetchone()
            if build_row is not None:
                existing = self._decode_build_operation_claim(build_row)
                if existing == claim:
                    return existing
                raise KnowledgeStoreConflictError(
                    "build identity is already bound to another operation"
                )
            connection.execute(
                "INSERT INTO build_operation_claims VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    claim.operation_key.value,
                    claim.record_version,
                    claim.semantic_commitment,
                    claim.build_identity,
                    claim.profile_reference.reference_type.value,
                    claim.profile_reference.value,
                    claim.capability_identity,
                ),
            )
        return claim

    def get_build_operation_claim(
        self, operation_key: BuildOperationKey
    ) -> KnowledgeReadResult:
        if not isinstance(operation_key, BuildOperationKey):
            raise TypeError("operation_key must be BuildOperationKey")
        return self._read_one(
            "build_operation_claim",
            operation_key.value,
            "SELECT * FROM build_operation_claims WHERE operation_id = ?",
            self._decode_build_operation_claim,
        )

    def create_staged_build(self, record: StagedBuildRecord) -> StagedBuildRecord:
        if not isinstance(record, StagedBuildRecord):
            raise TypeError("record must be a StagedBuildRecord")
        try:
            _validate_staged_record_semantics(record)
        except ValueError as exc:
            raise KnowledgeStoreConflictError("staged build semantic identity is invalid") from exc
        lineage = BuildLineageRecord(
            record.build_identity, record.manifest_commitment, record.lineage_commitment
        )
        with self._transaction(immediate=True) as connection:
            claim_row = connection.execute(
                "SELECT * FROM build_operation_claims WHERE operation_id = ?",
                (record.operation_key.value,),
            ).fetchone()
            if claim_row is None:
                raise KnowledgeStoreConflictError(
                    "staged build requires a durable build operation claim"
                )
            claim = self._decode_build_operation_claim(claim_row)
            if (
                claim.build_identity != record.build_identity
                or claim.profile_reference != record.profile_reference
                or claim.capability_identity != record.capability_identity
            ):
                raise KnowledgeStoreConflictError(
                    "staged build contradicts its durable operation claim"
                )
            operation_row = connection.execute(
                "SELECT * FROM staged_builds WHERE operation_id = ?",
                (record.operation_key.value,),
            ).fetchone()
            if operation_row is not None:
                existing = self._decode_staged_build(operation_row)
                if existing == record:
                    return existing
                raise KnowledgeStoreConflictError("contradictory staged-build operation replay")
            row = connection.execute(
                "SELECT * FROM staged_builds WHERE build_identity = ?", (record.build_identity,)
            ).fetchone()
            if row is not None:
                existing = self._decode_staged_build(row)
                if existing == record:
                    return existing
                raise KnowledgeStoreConflictError("contradictory immutable staged build")
            lineage_row = connection.execute(
                "SELECT * FROM build_lineage WHERE build_identity = ?", (record.build_identity,)
            ).fetchone()
            if lineage_row is None:
                connection.execute(
                    "INSERT INTO build_lineage VALUES (?, ?, ?, ?)",
                    (lineage.build_identity, lineage.record_version, lineage.manifest_commitment,
                     lineage.lineage_commitment),
                )
            elif self._decode_build(lineage_row) != lineage:
                raise KnowledgeStoreConflictError("staged build contradicts durable build lineage")
            connection.execute(
                "INSERT INTO staged_builds VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record.build_identity, record.record_version, record.operation_key.value,
                 record.manifest_commitment, record.lineage_commitment,
                 record.staged_commitment, canonical_serialize(record)),
            )
        return record

    def get_staged_build(self, build_identity: str) -> KnowledgeReadResult:
        return self._read_one(
            "staged_build", build_identity,
            "SELECT * FROM staged_builds WHERE build_identity = ?", self._decode_staged_build,
        )

    def create_build_validation(
        self, record: BuildValidationRecord
    ) -> BuildValidationRecord:
        if not isinstance(record, BuildValidationRecord):
            raise TypeError("record must be a BuildValidationRecord")
        try:
            _validate_build_validation_semantics(record)
        except ValueError as exc:
            raise KnowledgeStoreConflictError(
                "build validation semantic commitment is invalid"
            ) from exc
        with self._transaction(immediate=True) as connection:
            operation_row = connection.execute(
                "SELECT * FROM build_validations WHERE operation_id = ?",
                (record.operation_key.value,),
            ).fetchone()
            if operation_row is not None:
                existing = self._decode_build_validation(operation_row)
                if existing == record:
                    return existing
                raise KnowledgeStoreConflictError("contradictory build-validation operation replay")
            row = connection.execute(
                "SELECT * FROM build_validations WHERE build_identity = ?", (record.build_identity,)
            ).fetchone()
            if row is not None:
                existing = self._decode_build_validation(row)
                if existing == record:
                    return existing
                raise KnowledgeStoreConflictError("contradictory immutable build validation")
            staged = connection.execute(
                "SELECT * FROM staged_builds WHERE build_identity = ?", (record.build_identity,)
            ).fetchone()
            if staged is None or self._decode_staged_build(staged).staged_commitment != record.staged_commitment:
                raise KnowledgeStoreConflictError("validation does not reference the exact staged build")
            connection.execute(
                "INSERT INTO build_validations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record.build_identity, record.record_version, record.operation_key.value,
                 record.staged_commitment, record.validation_commitment, record.state.value,
                 canonical_serialize(record)),
            )
        return record

    def get_build_validation(self, build_identity: str) -> KnowledgeReadResult:
        return self._read_one(
            "build_validation", build_identity,
            "SELECT * FROM build_validations WHERE build_identity = ?",
            self._decode_build_validation,
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

    def local_readiness(
        self,
        *,
        required_profile_reference: OpaqueExternalReference | None = None,
        required_capability_identity: str | None = None,
    ) -> KnowledgeReadinessFact:
        if required_profile_reference is not None and not isinstance(
            required_profile_reference, OpaqueExternalReference
        ):
            raise TypeError("required_profile_reference must be an OpaqueExternalReference")
        if required_capability_identity is not None:
            if (
                not isinstance(required_capability_identity, str)
                or not required_capability_identity
                or required_capability_identity != required_capability_identity.strip()
                or len(required_capability_identity.encode("utf-8")) > 256
            ):
                raise ValueError("required_capability_identity is invalid")
        result = self.read_activation()
        if result.status is KnowledgeReadStatus.FOUND:
            activation = result.value
            assert isinstance(activation, ActivationAuthorityRecord)
            if required_profile_reference is not None or required_capability_identity is not None:
                staged_read = self.get_staged_build(activation.active_build_identity)
                if staged_read.status is not KnowledgeReadStatus.FOUND:
                    if staged_read.status is KnowledgeReadStatus.NOT_FOUND:
                        return KnowledgeReadinessFact(
                            KnowledgeLocalReadiness.REPAIR_REQUIRED,
                            findings=(_finding(
                                "ACTIVE_BUILD_INCOMPLETE", "staged_build",
                                activation.active_build_identity,
                                "active build lacks durable compatibility authority",
                            ),),
                        )
                    mapping = {
                        KnowledgeReadStatus.UNAVAILABLE: KnowledgeLocalReadiness.UNAVAILABLE,
                        KnowledgeReadStatus.INVALID: KnowledgeLocalReadiness.MISMATCH,
                        KnowledgeReadStatus.REPAIR_REQUIRED: KnowledgeLocalReadiness.REPAIR_REQUIRED,
                    }
                    return KnowledgeReadinessFact(mapping[staged_read.status], findings=staged_read.findings)
                staged = staged_read.value
                assert isinstance(staged, StagedBuildRecord)
                if (
                    required_profile_reference is not None
                    and staged.profile_reference != required_profile_reference
                ) or (
                    required_capability_identity is not None
                    and staged.capability_identity != required_capability_identity
                ):
                    return KnowledgeReadinessFact(
                        KnowledgeLocalReadiness.MISMATCH,
                        findings=(_finding(
                            "COMPATIBILITY_MISMATCH", "active_build",
                            activation.active_build_identity,
                            "active build does not match required Candidate-C compatibility",
                        ),),
                    )
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
            frozen_row = connection.execute(
                "SELECT * FROM frozen_retrieval_operations WHERE operation_id = ?",
                (snapshot.operation_key.value,),
            ).fetchone()
            staged_row = connection.execute(
                "SELECT * FROM staged_builds WHERE build_identity = ?",
                (snapshot.frozen_build_identity,),
            ).fetchone()
            if frozen_row is None or staged_row is None:
                raise KnowledgeStoreConflictError("Snapshot requires exact frozen build authority")
            completion_row = connection.execute(
                "SELECT * FROM retrieval_completion_facts WHERE operation_id = ?",
                (snapshot.operation_key.value,),
            ).fetchone()
            completion = (
                self._decode_retrieval_completion(completion_row)
                if completion_row is not None else None
            )
            try:
                validate_snapshot_against_authority(
                    snapshot,
                    self._decode_frozen_retrieval(frozen_row),
                    self._decode_staged_build(staged_row),
                    completion,
                )
            except (ValueError, TypeError, KeyError) as exc:
                raise KnowledgeStoreConflictError("Snapshot semantic provenance is invalid") from exc
            connection.execute(
                "INSERT INTO snapshot_envelopes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (snapshot.snapshot_key.value, snapshot.record_version, snapshot.operation_key.value,
                 snapshot.frozen_build_identity, snapshot.snapshot_commitment, snapshot.lineage_commitment,
                 snapshot.resolution.value, snapshot.source_status.value,
                 snapshot.finalization_key.value if snapshot.finalization_key else None,
                 canonical_serialize(snapshot)),
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

    def freeze_retrieval_operation(
        self, request: RetrievalOperationRequest
    ) -> FrozenRetrievalOperation:
        """Atomically bind one retrieval operation to the current exact Active build."""
        if not isinstance(request, RetrievalOperationRequest):
            raise TypeError("request must be RetrievalOperationRequest")
        commitment = derive_retrieval_operation_commitment(request)
        with self._transaction(immediate=True) as connection:
            existing_row = connection.execute(
                "SELECT * FROM frozen_retrieval_operations WHERE operation_id = ?",
                (request.operation_key.value,),
            ).fetchone()
            if existing_row is not None:
                existing = self._decode_frozen_retrieval(existing_row)
                if existing.request == request and existing.semantic_commitment == commitment:
                    return existing
                raise KnowledgeStoreConflictError("contradictory retrieval operation replay")
            if connection.execute(
                "SELECT 1 FROM operation_envelopes WHERE operation_id = ?",
                (request.operation_key.value,),
            ).fetchone() is not None:
                raise KnowledgeStoreConflictError("operation key is already bound")
            activation_row = connection.execute(
                "SELECT * FROM activation_authority WHERE singleton = 1"
            ).fetchone()
            if activation_row is None:
                raise KnowledgeStoreConflictError("retrieval requires an active build")
            activation = self._decode_activation(activation_row)
            receipt_row = connection.execute(
                "SELECT * FROM activation_receipts WHERE operation_id = ?",
                (activation.operation_key.value,),
            ).fetchone()
            staged_row = connection.execute(
                "SELECT * FROM staged_builds WHERE build_identity = ?",
                (activation.active_build_identity,),
            ).fetchone()
            validation_row = connection.execute(
                "SELECT * FROM build_validations WHERE build_identity = ?",
                (activation.active_build_identity,),
            ).fetchone()
            if receipt_row is None or staged_row is None or validation_row is None:
                raise KnowledgeStoreIntegrityError("active retrieval lineage is incomplete")
            receipt = self._activation_from_receipt(receipt_row)
            staged = self._decode_staged_build(staged_row)
            validation = self._decode_build_validation(validation_row)
            if (
                receipt != activation
                or validation.state is not BuildValidationState.VALIDATED
                or validation.validation_commitment != activation.validated_build_commitment
                or validation.staged_commitment != staged.staged_commitment
                or not staged.artifact.integrity_ok
                or staged.artifact.artifact_trust is not ArtifactTrust.APPROVED
                or not _retrieval_compatibility_matches(request, staged)
            ):
                raise KnowledgeStoreIntegrityError("active build is not retrieval compatible")
            frozen = FrozenRetrievalOperation(
                request,
                commitment,
                staged.build_identity,
                activation.generation,
                activation.operation_key,
                validation.validation_commitment,
                staged.staged_commitment,
                staged.artifact.artifact_commitment,
                staged.lineage_commitment,
            )
            connection.execute(
                "INSERT INTO operation_envelopes VALUES (?, 1, ?, ?, NULL, 0, 1)",
                (request.operation_key.value, commitment, staged.build_identity),
            )
            connection.execute(
                "INSERT INTO frozen_retrieval_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.operation_key.value,
                    frozen.record_version,
                    frozen.semantic_commitment,
                    frozen.frozen_build_identity,
                    frozen.activation_generation,
                    frozen.activation_operation_key.value,
                    frozen.validation_commitment,
                    frozen.staged_commitment,
                    frozen.artifact_commitment,
                    frozen.lineage_commitment,
                    frozen.revision,
                    canonical_serialize(frozen),
                ),
            )
        return frozen

    def get_frozen_retrieval_operation(
        self, key: RetrievalOperationKey
    ) -> KnowledgeReadResult:
        if not isinstance(key, RetrievalOperationKey):
            raise TypeError("key must be RetrievalOperationKey")
        return self._read_one(
            "frozen_retrieval_operation",
            key.value,
            "SELECT * FROM frozen_retrieval_operations WHERE operation_id = ?",
            self._decode_frozen_retrieval,
        )

    def record_retrieval_completion(
        self, fact: DurableRetrievalCompletion
    ) -> DurableRetrievalCompletion:
        if not isinstance(fact, DurableRetrievalCompletion):
            raise TypeError("fact must be DurableRetrievalCompletion")
        with self._transaction(immediate=True) as connection:
            frozen_row = connection.execute(
                "SELECT * FROM frozen_retrieval_operations WHERE operation_id = ?",
                (fact.operation_key.value,),
            ).fetchone()
            staged_row = connection.execute(
                "SELECT * FROM staged_builds WHERE build_identity = ?",
                (fact.frozen_build_identity,),
            ).fetchone()
            if frozen_row is None or staged_row is None:
                raise KnowledgeStoreConflictError(
                    "retrieval completion requires exact frozen build authority"
                )
            try:
                validate_retrieval_completion_authority(
                    fact,
                    self._decode_frozen_retrieval(frozen_row),
                    self._decode_staged_build(staged_row),
                )
            except (ValueError, TypeError, KeyError) as exc:
                raise KnowledgeStoreConflictError(
                    "retrieval completion semantic provenance is invalid"
                ) from exc
            existing_row = connection.execute(
                "SELECT * FROM retrieval_completion_facts WHERE operation_id = ?",
                (fact.operation_key.value,),
            ).fetchone()
            if existing_row is not None:
                existing = self._decode_retrieval_completion(existing_row)
                if existing == fact:
                    return existing
                raise KnowledgeStoreConflictError("contradictory retrieval completion replay")
            operation_row = connection.execute(
                "SELECT * FROM operation_envelopes WHERE operation_id = ?",
                (fact.operation_key.value,),
            ).fetchone()
            if operation_row is None or self._decode_operation(operation_row).completed:
                raise KnowledgeStoreConflictError(
                    "retrieval completion requires an incomplete frozen operation"
                )
            connection.execute(
                "INSERT INTO retrieval_completion_facts VALUES (?, ?, ?, ?, ?, ?)",
                (
                    fact.operation_key.value,
                    fact.record_version,
                    fact.semantic_commitment,
                    fact.resolution.value,
                    len(fact.raw_batch.candidates),
                    canonical_serialize(fact),
                ),
            )
            return fact

    def get_retrieval_completion(
        self, key: RetrievalOperationKey
    ) -> KnowledgeReadResult:
        if not isinstance(key, RetrievalOperationKey):
            return KnowledgeReadResult(KnowledgeReadStatus.INVALID)
        return self._read_one(
            "retrieval_completion", key.value,
            "SELECT * FROM retrieval_completion_facts WHERE operation_id = ?",
            self._decode_retrieval_completion,
        )

    def get_snapshot(self, key: KnowledgeSnapshotKey) -> KnowledgeReadResult:
        if not isinstance(key, KnowledgeSnapshotKey):
            return KnowledgeReadResult(KnowledgeReadStatus.INVALID)
        return self._read_one(
            "snapshot", key.value,
            "SELECT * FROM snapshot_envelopes WHERE snapshot_id = ?", self._decode_snapshot,
        )

    def record_retrieval_recovery(
        self, fact: RetrievalRecoveryFact
    ) -> RetrievalRecoveryFact:
        if not isinstance(fact, RetrievalRecoveryFact):
            raise TypeError("fact must be RetrievalRecoveryFact")
        with self._transaction(immediate=True) as connection:
            if connection.execute(
                "SELECT 1 FROM frozen_retrieval_operations WHERE operation_id = ?",
                (fact.operation_key.value,),
            ).fetchone() is None:
                raise KnowledgeStoreConflictError("recovery fact requires frozen operation")
            row = connection.execute(
                "SELECT * FROM retrieval_recovery_facts WHERE operation_id = ?",
                (fact.operation_key.value,),
            ).fetchone()
            if row is not None:
                existing = self._decode_recovery(row)
                if existing.semantic_commitment == fact.semantic_commitment and existing.failures == fact.failures:
                    return existing
                updated = replace(fact, revision=existing.revision + 1)
                connection.execute(
                    """UPDATE retrieval_recovery_facts
                       SET semantic_commitment = ?, revision = ?, payload_json = ?
                       WHERE operation_id = ? AND revision = ?""",
                    (updated.semantic_commitment, updated.revision, canonical_serialize(updated),
                     updated.operation_key.value, existing.revision),
                )
                return updated
            connection.execute(
                "INSERT INTO retrieval_recovery_facts VALUES (?, ?, ?, ?, ?)",
                (fact.operation_key.value, fact.record_version, fact.semantic_commitment,
                 fact.revision, canonical_serialize(fact)),
            )
            return fact

    def get_retrieval_operation_read(
        self, key: RetrievalOperationKey
    ) -> KnowledgeReadResult:
        if not isinstance(key, RetrievalOperationKey):
            return KnowledgeReadResult(KnowledgeReadStatus.INVALID)
        try:
            with self._transaction(immediate=False) as connection:
                self._assert_integrity()
                frozen_row = connection.execute(
                    "SELECT * FROM frozen_retrieval_operations WHERE operation_id = ?", (key.value,)
                ).fetchone()
                if frozen_row is None:
                    return KnowledgeReadResult(KnowledgeReadStatus.NOT_FOUND)
                frozen = self._decode_frozen_retrieval(frozen_row)
                operation_row = connection.execute(
                    "SELECT * FROM operation_envelopes WHERE operation_id = ?", (key.value,)
                ).fetchone()
                if operation_row is None:
                    return self._repair_result("BROKEN_OPERATION", "operation", key.value)
                operation = self._decode_operation(operation_row)
                recovery_row = connection.execute(
                    "SELECT * FROM retrieval_recovery_facts WHERE operation_id = ?", (key.value,)
                ).fetchone()
                recovery = self._decode_recovery(recovery_row) if recovery_row is not None else None
                completion_row = connection.execute(
                    "SELECT * FROM retrieval_completion_facts WHERE operation_id = ?", (key.value,)
                ).fetchone()
                completion = (
                    self._decode_retrieval_completion(completion_row)
                    if completion_row is not None else None
                )
                if operation.completed:
                    state = RetrievalOperationState.COMPLETED
                elif completion is not None:
                    state = RetrievalOperationState.RETRIEVAL_COMPLETED
                elif recovery is not None:
                    state = RetrievalOperationState.TRANSIENT_UNAVAILABLE
                else:
                    state = RetrievalOperationState.FROZEN
                return KnowledgeReadResult(
                    KnowledgeReadStatus.FOUND,
                    RetrievalOperationRead(
                        frozen, state, recovery, completion, operation.snapshot_key
                    ),
                )
        except KnowledgeStoreIntegrityError:
            return self._repair_result("STORE_INTEGRITY", "store", "singleton")
        except sqlite3.Error:
            return KnowledgeReadResult(KnowledgeReadStatus.UNAVAILABLE)

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
            if row is not None:
                return False
            if subject_kind is RetentionSubjectKind.BUILD:
                referenced = connection.execute(
                    """SELECT 1 FROM frozen_retrieval_operations AS f
                       JOIN operation_envelopes AS o ON o.operation_id = f.operation_id
                       WHERE f.frozen_build_identity = ?
                       LIMIT 1""",
                    (subject_identity,),
                ).fetchone()
                return referenced is None
            if subject_kind is RetentionSubjectKind.SNAPSHOT:
                return connection.execute(
                    "SELECT 1 FROM snapshot_envelopes WHERE snapshot_id = ? LIMIT 1",
                    (subject_identity,),
                ).fetchone() is None
            for snapshot_row in connection.execute("SELECT * FROM snapshot_envelopes"):
                snapshot = self._decode_snapshot(snapshot_row)
                if any(
                    subject_identity in (item.chunk_identity, item.content_commitment)
                    for item in snapshot.chunks
                ):
                    return False
            return True

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
        payload = _json_object(row["payload_json"], {
            "snapshot_key", "operation_key", "frozen_build_identity", "snapshot_commitment",
            "lineage_commitment", "schema_version", "resolution", "source_status",
            "knowledge_gap", "payload_truncated", "activation_generation", "activation_operation_key",
            "manifest_commitment", "validation_commitment", "staged_commitment",
            "artifact_commitment", "query_commitment", "retrieval_profile_identity",
            "retrieval_profile_version", "applicability_policy_identity",
            "applicability_policy_version", "profile_reference", "capability_identity",
            "external_references", "creation_metadata", "chunks", "evaluations", "failures",
            "finalization_key", "finalization_reference", "record_version",
        })
        chunks = tuple(_decode_snapshot_chunk(item) for item in _list(payload["chunks"]))
        evaluations = tuple(_decode_evaluation(item) for item in _list(payload["evaluations"]))
        failures = tuple(_decode_retrieval_failure(item) for item in _list(payload["failures"]))
        finalization_key = (
            None if payload["finalization_key"] is None
            else SnapshotFinalizationKey(_single_value(payload["finalization_key"], "finalization key"))
        )
        finalization_reference = (
            None if payload["finalization_reference"] is None
            else _decode_opaque_reference(_object(payload["finalization_reference"]))
        )
        record = KnowledgeSnapshotEnvelope(
            KnowledgeSnapshotKey(_single_value(payload["snapshot_key"], "Snapshot key")),
            RetrievalOperationKey(_single_value(payload["operation_key"], "operation key")),
            payload["frozen_build_identity"], payload["snapshot_commitment"],
            payload["lineage_commitment"], payload["schema_version"],
            RetrievalResolution(payload["resolution"]), SnapshotSourceStatus(payload["source_status"]),
            payload["knowledge_gap"], payload["payload_truncated"], payload["activation_generation"],
            ActivationOperationKey(_single_value(payload["activation_operation_key"], "activation key")),
            payload["manifest_commitment"], payload["validation_commitment"],
            payload["staged_commitment"], payload["artifact_commitment"],
            payload["query_commitment"], payload["retrieval_profile_identity"],
            payload["retrieval_profile_version"], payload["applicability_policy_identity"],
            payload["applicability_policy_version"],
            _decode_opaque_reference(_object(payload["profile_reference"])),
            payload["capability_identity"],
            tuple(_decode_opaque_reference(_object(item)) for item in _list(payload["external_references"])),
            tuple(
                MetadataItem(_object(item)["key"], _object(item)["value"])
                for item in _list(payload["creation_metadata"])
            ),
            chunks, evaluations, failures, finalization_key, finalization_reference,
            payload["record_version"],
        )
        if (
            record.snapshot_key.value != row["snapshot_id"]
            or record.operation_key.value != row["operation_id"]
            or record.frozen_build_identity != row["frozen_build_identity"]
            or record.snapshot_commitment != row["snapshot_commitment"]
            or record.lineage_commitment != row["lineage_commitment"]
            or record.resolution.value != row["resolution"]
            or record.source_status.value != row["source_status"]
            or (record.finalization_key.value if record.finalization_key else None) != row["finalization_id"]
            or record.record_version != row["record_version"]
        ):
            raise ValueError("Snapshot columns contradict semantic payload")
        return record

    @staticmethod
    def _decode_recovery(row: sqlite3.Row) -> RetrievalRecoveryFact:
        payload = _json_object(row["payload_json"], {
            "operation_key", "semantic_commitment", "failures", "revision", "record_version",
        })
        record = RetrievalRecoveryFact(
            RetrievalOperationKey(_single_value(payload["operation_key"], "operation key")),
            payload["semantic_commitment"],
            tuple(_decode_retrieval_failure(item) for item in _list(payload["failures"])),
            payload["revision"], payload["record_version"],
        )
        if (
            record.operation_key.value != row["operation_id"]
            or record.semantic_commitment != row["semantic_commitment"]
            or record.revision != row["revision"]
            or record.record_version != row["record_version"]
        ):
            raise ValueError("recovery columns contradict semantic payload")
        return record

    @staticmethod
    def _decode_hold(row: sqlite3.Row) -> RetentionHoldRecord:
        return RetentionHoldRecord(
            RetentionHoldKey(row["hold_id"]), RetentionSubjectKind(row["subject_kind"]),
            row["subject_id"], OpaqueExternalReference(OpaqueReferenceType(row["owner_type"]), row["owner_value"]),
            RetentionObligationKind(row["obligation_kind"]), row["semantic_commitment"],
            RetentionHoldStatus(row["status"]), row["revision"], row["record_version"],
        )

    @staticmethod
    def _decode_build_operation_claim(row: sqlite3.Row) -> BuildOperationClaim:
        return BuildOperationClaim(
            BuildOperationKey(row["operation_id"]),
            row["semantic_commitment"],
            row["build_identity"],
            OpaqueExternalReference(
                OpaqueReferenceType(row["profile_type"]), row["profile_value"]
            ),
            row["capability_identity"],
            row["record_version"],
        )

    @staticmethod
    def _decode_staged_build(row: sqlite3.Row) -> StagedBuildRecord:
        raw_payload = _object(json.loads(row["payload_json"]))
        legacy_keys = {
            "build_identity", "operation_key", "manifest_commitment", "lineage_commitment",
            "staged_commitment", "build_input", "profile_reference", "capability_identity",
            "document_count", "document_provenance", "chunks", "artifact", "state",
            "record_version",
        }
        if frozenset(raw_payload) not in {frozenset(legacy_keys), frozenset(legacy_keys | {"manifest_provenance"})}:
            raise ValueError("staged build payload has an incompatible field set")
        payload = raw_payload
        build_input_data = _object(payload["build_input"])
        chunks = tuple(_decode_build_chunk(item) for item in _list(payload["chunks"]))
        document_provenance = tuple(
            _decode_build_document_provenance(item)
            for item in _list(payload["document_provenance"])
        )
        artifact = _decode_index_artifact(_object(payload["artifact"]))
        profile = _decode_opaque_reference(_object(payload["profile_reference"]))
        operation = BuildOperationKey(_object(payload["operation_key"])["value"])
        manifest_provenance = (
            None if payload.get("manifest_provenance") is None
            else _decode_build_manifest_provenance(_object(payload["manifest_provenance"]))
        )
        build_input = BuildIdentityInput(
            manifest_commitment=build_input_data["manifest_commitment"],
            ordered_chunk_identities=tuple(_list(build_input_data["ordered_chunk_identities"])),
            chunking_profile_identity=build_input_data["chunking_profile_identity"],
            canonicalization_version=build_input_data["canonicalization_version"],
            embedding_provider=build_input_data["embedding_provider"],
            embedding_model=build_input_data["embedding_model"],
            embedding_profile_identity=build_input_data["embedding_profile_identity"],
            embedding_dimension=build_input_data["embedding_dimension"],
            normalization_semantics=build_input_data["normalization_semantics"],
            index_engine=build_input_data["index_engine"],
            index_schema_identity=build_input_data["index_schema_identity"],
            metadata_schema_identity=build_input_data["metadata_schema_identity"],
            build_contract_version=build_input_data["build_contract_version"],
        )
        record = StagedBuildRecord(
            payload["build_identity"], operation, payload["manifest_commitment"],
            payload["lineage_commitment"], payload["staged_commitment"], build_input,
            profile, payload["capability_identity"], payload["document_count"],
            document_provenance, chunks, artifact, BuildStageState(payload["state"]),
            payload["record_version"], manifest_provenance,
        )
        if (
            record.build_identity != row["build_identity"]
            or record.record_version != row["record_version"]
            or record.operation_key.value != row["operation_id"]
            or record.manifest_commitment != row["manifest_commitment"]
            or record.lineage_commitment != row["lineage_commitment"]
            or record.staged_commitment != row["staged_commitment"]
        ):
            raise ValueError("staged build columns contradict semantic payload")
        return record

    @staticmethod
    def _decode_build_validation(row: sqlite3.Row) -> BuildValidationRecord:
        payload = _json_object(row["payload_json"], {
            "build_identity", "operation_key", "staged_commitment", "validation_commitment",
            "state", "findings", "record_version",
        })
        findings = tuple(
            BuildValidationFinding(
                BuildFailureCode(_object(item)["code"]),
                _object(item)["field"],
                _object(item)["detail"],
            )
            for item in _list(payload["findings"])
        )
        record = BuildValidationRecord(
            payload["build_identity"],
            BuildOperationKey(_object(payload["operation_key"])["value"]),
            payload["staged_commitment"], payload["validation_commitment"],
            BuildValidationState(payload["state"]), findings, payload["record_version"],
        )
        if (
            record.build_identity != row["build_identity"]
            or record.record_version != row["record_version"]
            or record.operation_key.value != row["operation_id"]
            or record.staged_commitment != row["staged_commitment"]
            or record.validation_commitment != row["validation_commitment"]
            or record.state.value != row["validation_state"]
        ):
            raise ValueError("build validation columns contradict semantic payload")
        return record

    @staticmethod
    def _decode_frozen_retrieval(row: sqlite3.Row) -> FrozenRetrievalOperation:
        payload = _json_object(row["payload_json"], {
            "request", "semantic_commitment", "frozen_build_identity",
            "activation_generation", "activation_operation_key", "validation_commitment",
            "staged_commitment", "artifact_commitment", "lineage_commitment", "revision",
            "record_version",
        })
        request_data = _object(payload["request"])
        if set(request_data) != {
            "operation_key", "query", "retrieval_profile", "applicability_policy",
            "external_references", "profile_reference", "capability_identity",
        }:
            raise ValueError("retrieval request payload has incompatible fields")
        query_data = _object(request_data["query"])
        if set(query_data) != {"schema_version", "canonicalization_version", "text", "filters"}:
            raise ValueError("query payload has incompatible fields")
        filter_values: list[QueryFilter] = []
        for item in _list(query_data["filters"]):
            filter_data = _object(item)
            if set(filter_data) != {"key", "value"}:
                raise ValueError("query filter payload has incompatible fields")
            filter_values.append(QueryFilter(filter_data["key"], filter_data["value"]))
        filters = tuple(filter_values)
        query = CanonicalKnowledgeQuery(
            query_data["schema_version"], query_data["canonicalization_version"],
            query_data["text"], filters,
        )
        profile_data = _object(request_data["retrieval_profile"])
        if set(profile_data) != {
            "profile_identity", "version", "canonicalization_version",
            "embedding_profile_identity", "provider", "model", "embedding_dimension",
            "index_engine", "index_schema_identity", "max_query_bytes",
            "allowed_filter_keys", "top_k", "candidate_limit", "score_direction",
            "score_precision", "max_content_bytes_per_result",
            "max_metadata_bytes_per_result", "max_total_payload_bytes",
            "empty_query_disposition", "unsupported_query_disposition",
        }:
            raise ValueError("retrieval profile payload has incompatible fields")
        profile = RetrievalProfile(
            profile_identity=profile_data["profile_identity"],
            version=profile_data["version"],
            canonicalization_version=profile_data["canonicalization_version"],
            embedding_profile_identity=profile_data["embedding_profile_identity"],
            provider=profile_data["provider"], model=profile_data["model"],
            embedding_dimension=profile_data["embedding_dimension"],
            index_engine=profile_data["index_engine"],
            index_schema_identity=profile_data["index_schema_identity"],
            max_query_bytes=profile_data["max_query_bytes"],
            allowed_filter_keys=tuple(_list(profile_data["allowed_filter_keys"])),
            top_k=profile_data["top_k"], candidate_limit=profile_data["candidate_limit"],
            score_direction=RetrievalScoreDirection(profile_data["score_direction"]),
            score_precision=profile_data["score_precision"],
            max_content_bytes_per_result=profile_data["max_content_bytes_per_result"],
            max_metadata_bytes_per_result=profile_data["max_metadata_bytes_per_result"],
            max_total_payload_bytes=profile_data["max_total_payload_bytes"],
            empty_query_disposition=RetrievalQueryDisposition(
                profile_data["empty_query_disposition"]
            ),
            unsupported_query_disposition=RetrievalQueryDisposition(
                profile_data["unsupported_query_disposition"]
            ),
        )
        policy_data = _object(request_data["applicability_policy"])
        if set(policy_data) != {
            "policy_identity", "version", "required_filter_keys", "direct_threshold",
            "partial_threshold", "contextual_threshold",
        }:
            raise ValueError("applicability policy payload has incompatible fields")
        policy = ApplicabilityPolicy(
            policy_data["policy_identity"], policy_data["version"],
            tuple(_list(policy_data["required_filter_keys"])),
            policy_data["direct_threshold"], policy_data["partial_threshold"],
            policy_data["contextual_threshold"],
        )
        request = RetrievalOperationRequest(
            RetrievalOperationKey(_single_value(request_data["operation_key"], "operation key")),
            query, profile, policy,
            tuple(
                _decode_opaque_reference(_object(item))
                for item in _list(request_data["external_references"])
            ),
            _decode_opaque_reference(_object(request_data["profile_reference"])),
            request_data["capability_identity"],
        )
        record = FrozenRetrievalOperation(
            request,
            payload["semantic_commitment"], payload["frozen_build_identity"],
            payload["activation_generation"],
            ActivationOperationKey(_single_value(payload["activation_operation_key"], "activation operation key")),
            payload["validation_commitment"], payload["staged_commitment"],
            payload["artifact_commitment"], payload["lineage_commitment"],
            payload["revision"], payload["record_version"],
        )
        if (
            record.request.operation_key.value != row["operation_id"]
            or record.record_version != row["record_version"]
            or record.semantic_commitment != row["semantic_commitment"]
            or record.frozen_build_identity != row["frozen_build_identity"]
            or record.activation_generation != row["activation_generation"]
            or record.activation_operation_key.value != row["activation_operation_id"]
            or record.validation_commitment != row["validation_commitment"]
            or record.staged_commitment != row["staged_commitment"]
            or record.artifact_commitment != row["artifact_commitment"]
            or record.lineage_commitment != row["lineage_commitment"]
            or record.revision != row["revision"]
        ):
            raise ValueError("frozen retrieval columns contradict semantic payload")
        return record

    @staticmethod
    def _decode_retrieval_completion(row: sqlite3.Row) -> DurableRetrievalCompletion:
        payload = _json_object(row["payload_json"], {
            "operation_key", "frozen_build_identity", "frozen_operation_commitment",
            "validation_commitment", "staged_commitment", "artifact_commitment",
            "lineage_commitment", "query_commitment", "retrieval_profile_identity",
            "retrieval_profile_version", "applicability_policy_identity",
            "applicability_policy_version", "raw_batch", "resolution", "knowledge_gap",
            "payload_truncated", "evaluations", "semantic_commitment", "record_version",
        })
        raw_data = _object(payload["raw_batch"])
        if set(raw_data) != {"build_identity", "artifact_commitment", "candidates"}:
            raise ValueError("retrieval completion raw batch has incompatible fields")
        raw_candidates: list[RawRetrievalCandidate] = []
        for item in _list(raw_data["candidates"]):
            candidate = _object(item)
            if set(candidate) != {"chunk_identity", "score", "metadata_commitment"}:
                raise ValueError("retrieval completion candidate has incompatible fields")
            raw_candidates.append(RawRetrievalCandidate(
                candidate["chunk_identity"], candidate["score"],
                candidate["metadata_commitment"],
            ))
        record = DurableRetrievalCompletion(
            RetrievalOperationKey(_single_value(payload["operation_key"], "operation key")),
            payload["frozen_build_identity"], payload["frozen_operation_commitment"],
            payload["validation_commitment"], payload["staged_commitment"],
            payload["artifact_commitment"], payload["lineage_commitment"],
            payload["query_commitment"], payload["retrieval_profile_identity"],
            payload["retrieval_profile_version"], payload["applicability_policy_identity"],
            payload["applicability_policy_version"],
            RawRetrievalBatch(
                raw_data["build_identity"], raw_data["artifact_commitment"],
                tuple(raw_candidates),
            ),
            RetrievalResolution(payload["resolution"]), payload["knowledge_gap"],
            payload["payload_truncated"],
            tuple(_decode_evaluation(item) for item in _list(payload["evaluations"])),
            payload["semantic_commitment"], payload["record_version"],
        )
        if (
            record.operation_key.value != row["operation_id"]
            or record.record_version != row["record_version"]
            or record.semantic_commitment != row["semantic_commitment"]
            or record.resolution.value != row["resolution"]
            or len(record.raw_batch.candidates) != row["candidate_count"]
        ):
            raise ValueError("retrieval completion columns contradict semantic payload")
        return record


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("durable JSON value must be an object")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("durable JSON value must be a list")
    return value


def _json_object(value: object, expected_keys: set[str]) -> dict[str, object]:
    if not isinstance(value, str):
        raise ValueError("durable payload must be text")
    payload = _object(json.loads(value))
    if set(payload) != expected_keys:
        raise ValueError("durable payload has an incompatible field set")
    return payload


def _decode_opaque_reference(value: dict[str, object]) -> OpaqueExternalReference:
    if set(value) != {"reference_type", "value"}:
        raise ValueError("opaque reference payload has incompatible fields")
    return OpaqueExternalReference(OpaqueReferenceType(value["reference_type"]), value["value"])


def _decode_retrieval_failure(value: object) -> RetrievalFailureFact:
    data = _object(value)
    if set(data) != {"code", "field", "detail", "retry_safety"}:
        raise ValueError("retrieval failure payload has incompatible fields")
    return RetrievalFailureFact(
        RetrievalFailureCode(data["code"]), data["field"], data["detail"],
        RetrySafetyDisposition(data["retry_safety"]),
    )


def _decode_evaluation(value: object) -> RetrievalEvaluationFact:
    data = _object(value)
    if set(data) != {
        "chunk_identity", "score", "applicability", "included", "content_truncated",
        "canonical_rank", "policy_identity", "policy_version", "query_predicates",
        "metadata_predicates", "rule_facts", "disposition", "rejection_reason",
    }:
        raise ValueError("evaluation payload has incompatible fields")
    query_predicates = tuple(
        QueryFilter(_object(item)["key"], _object(item)["value"])
        for item in _list(data["query_predicates"])
    )
    metadata_predicates = tuple(
        MetadataItem(_object(item)["key"], _object(item)["value"])
        for item in _list(data["metadata_predicates"])
    )
    rule_facts = tuple(
        ApplicabilityRuleFact(
            ApplicabilityRuleIdentity(_object(item)["rule_identity"]),
            _object(item)["matched"],
        )
        for item in _list(data["rule_facts"])
    )
    return RetrievalEvaluationFact(
        data["chunk_identity"], data["score"], RetrievalApplicability(data["applicability"]),
        data["included"], data["content_truncated"], data["canonical_rank"],
        data["policy_identity"], data["policy_version"], query_predicates,
        metadata_predicates, rule_facts,
        RetrievalEvaluationDisposition(data["disposition"]),
        RetrievalRejectionReason(data["rejection_reason"]),
    )


def _decode_snapshot_chunk(value: object) -> KnowledgeSnapshotChunk:
    data = _object(value)
    legacy_keys = {
        "chunk_identity", "document_identity", "document_version_identity", "section_identity",
        "content_commitment", "metadata_commitment", "score", "applicability", "content",
        "metadata", "content_truncated",
    }
    governed_keys = legacy_keys | {
        "knowledge_type", "guidance_authority", "sop_backed_eligible"
    }
    if frozenset(data) not in {frozenset(legacy_keys), frozenset(governed_keys)}:
        raise ValueError("Snapshot chunk payload has incompatible fields")
    metadata = tuple(
        MetadataItem(_object(item)["key"], _object(item)["value"])
        for item in _list(data["metadata"])
    )
    return KnowledgeSnapshotChunk(
        data["chunk_identity"], data["document_identity"], data["document_version_identity"],
        data["section_identity"], data["content_commitment"], data["metadata_commitment"],
        data["score"], RetrievalApplicability(data["applicability"]), data["content"],
        metadata, data["content_truncated"], data.get("knowledge_type", ""),
        data.get("guidance_authority", ""), data.get("sop_backed_eligible"),
    )


def _single_value(value: object, name: str) -> object:
    data = _object(value)
    if set(data) != {"value"}:
        raise ValueError(f"{name} payload has incompatible fields")
    return data["value"]


def _decode_build_document_provenance(value: object) -> BuildDocumentProvenance:
    data = _object(value)
    legacy_keys = {
        "document_identity", "document_version_identity", "source_path", "content_hash",
        "approval_reference", "source_classification", "outbound_eligible", "content_type",
        "metadata",
    }
    governed_keys = legacy_keys | {
        "knowledge_type", "guidance_authority", "document_status", "approval_state",
        "production_eligible",
    }
    if frozenset(data) not in {frozenset(legacy_keys), frozenset(governed_keys)}:
        raise ValueError("build document provenance payload has incompatible fields")
    metadata = tuple(
        MetadataItem(_object(item)["key"], _object(item)["value"])
        for item in _list(data["metadata"])
    )
    return BuildDocumentProvenance(
        data["document_identity"],
        data["document_version_identity"],
        data["source_path"],
        data["content_hash"],
        data["approval_reference"],
        SourceClassification(data["source_classification"]),
        data["outbound_eligible"],
        ContentType(data["content_type"]),
        metadata,
        data.get("knowledge_type", ""),
        data.get("guidance_authority", ""),
        (
            None if data.get("document_status") is None
            else DocumentStatus(data["document_status"])
        ),
        data.get("approval_state", ""),
        data.get("production_eligible"),
    )


def _decode_build_manifest_provenance(value: dict[str, object]) -> BuildManifestProvenance:
    if set(value) != {
        "manifest_identity", "manifest_schema_identity", "manifest_schema_version",
        "canonicalization_version", "manifest_commitment", "corpus_identity",
        "corpus_version",
    }:
        raise ValueError("build manifest provenance payload has incompatible fields")
    return BuildManifestProvenance(
        value["manifest_identity"], value["manifest_schema_identity"],
        value["manifest_schema_version"], value["canonicalization_version"],
        value["manifest_commitment"], value["corpus_identity"], value["corpus_version"],
    )


def _decode_build_chunk(value: object) -> BuildChunk:
    data = _object(value)
    if set(data) != {
        "chunk_identity", "document_identity", "document_version_identity", "section_identity",
        "ordinal", "content", "content_hash", "metadata_commitment",
    }:
        raise ValueError("build chunk payload has incompatible fields")
    return BuildChunk(
        data["chunk_identity"], data["document_identity"], data["document_version_identity"],
        data["section_identity"], data["ordinal"], data["content"], data["content_hash"],
        data["metadata_commitment"],
    )


def _decode_index_artifact(value: dict[str, object]) -> IndexArtifactFacts:
    if set(value) != {
        "build_identity", "artifact_commitment", "artifact_trust", "provider", "model",
        "embedding_profile_identity", "embedding_dimension", "index_engine",
        "index_schema_identity", "entries", "integrity_ok",
    }:
        raise ValueError("index artifact payload has incompatible fields")
    entries = tuple(
        IndexEntryFact(
            _object(item)["chunk_identity"],
            _object(item)["metadata_commitment"],
            _object(item)["embedding_dimension"],
        )
        for item in _list(value["entries"])
    )
    return IndexArtifactFacts(
        value["build_identity"], value["artifact_commitment"],
        ArtifactTrust(value["artifact_trust"]), value["provider"], value["model"],
        value["embedding_profile_identity"], value["embedding_dimension"],
        value["index_engine"], value["index_schema_identity"], entries,
        value["integrity_ok"],
    )


def _validate_staged_record_semantics(record: StagedBuildRecord) -> None:
    chunk_identities = tuple(chunk.chunk_identity for chunk in record.chunks)
    build_input = record.build_input
    artifact = record.artifact
    provenance_by_version = {
        item.document_version_identity: item for item in record.document_provenance
    }
    if (
        build_identity(build_input) != record.build_identity
        or build_input.manifest_commitment != record.manifest_commitment
        or build_input.ordered_chunk_identities != chunk_identities
        or len(set(chunk_identities)) != len(chunk_identities)
        or record.document_count != len(record.document_provenance)
        or artifact.provider != build_input.embedding_provider
        or artifact.model != build_input.embedding_model
        or artifact.embedding_profile_identity != build_input.embedding_profile_identity
        or artifact.embedding_dimension != build_input.embedding_dimension
        or artifact.index_engine != build_input.index_engine
        or artifact.index_schema_identity != build_input.index_schema_identity
        or derive_build_lineage_commitment(
            record.build_identity,
            record.manifest_commitment,
            record.chunks,
            record.profile_reference,
            record.capability_identity,
            record.manifest_provenance,
        ) != record.lineage_commitment
        or derive_staged_build_commitment(
            record.lineage_commitment, record.artifact, record.build_input
        ) != record.staged_commitment
        or any(
            (provenance := provenance_by_version.get(chunk.document_version_identity)) is None
            or provenance.document_identity != chunk.document_identity
            or chunk.metadata_commitment
            != derive_chunk_metadata_commitment(
                record.manifest_commitment,
                provenance,
                chunk_identity=chunk.chunk_identity,
                section_identity=chunk.section_identity,
                ordinal=chunk.ordinal,
                content_hash=chunk.content_hash,
            )
            for chunk in record.chunks
        )
    ):
        raise ValueError("staged build semantic inputs are inconsistent")


def _validate_build_operation_claim_semantics(claim: BuildOperationClaim) -> None:
    if claim.semantic_commitment != derive_build_operation_commitment(
        claim.operation_key,
        claim.build_identity,
        claim.profile_reference,
        claim.capability_identity,
    ):
        raise ValueError("build operation semantic commitment is inconsistent")


def _validate_build_validation_semantics(record: BuildValidationRecord) -> None:
    if record.validation_commitment != derive_build_validation_commitment(
        record.build_identity,
        record.operation_key,
        record.staged_commitment,
        record.state,
        record.findings,
    ):
        raise ValueError("build validation semantic commitment is inconsistent")


def _retrieval_compatibility_matches(
    request: RetrievalOperationRequest, staged: StagedBuildRecord
) -> bool:
    profile = request.retrieval_profile
    artifact = staged.artifact
    return (
        request.profile_reference == staged.profile_reference
        and request.capability_identity == staged.capability_identity
        and profile.embedding_profile_identity == artifact.embedding_profile_identity
        and profile.provider == artifact.provider
        and profile.model == artifact.model
        and profile.embedding_dimension == artifact.embedding_dimension
        and profile.index_engine == artifact.index_engine
        and profile.index_schema_identity == artifact.index_schema_identity
        and profile.canonicalization_version == staged.build_input.canonicalization_version
    )
