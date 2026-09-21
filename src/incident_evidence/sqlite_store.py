"""Independent, fail-closed SQLite Evidence Store for SPEC-013 Phase 2."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Callable, Iterator

from .contracts import (
    CaptureCommand,
    CaptureFailure,
    CaptureSuccess,
    CaptureTerminalKind,
    CaptureTerminalOutcome,
    EvidenceCompleteness,
    EvidenceIntegrityStatus,
    EvidenceReadiness,
    EvidenceRecoveryFacts,
    EvidenceRevision,
    EvidenceSnapshot,
    EvidenceSource,
    MaterialityEvaluationKind,
    MaterialityJudgement,
    MaterialityRequest,
    MaterialityResult,
    SourceStatus,
)
from .errors import (
    EvidenceDomainError,
    EvidenceFailureKind,
    EvidenceStoreError,
    EvidenceStoreIntegrityError,
    RetryDisposition,
)
from .identity import (
    canonical_json,
    capture_command_semantic_identity,
    capture_command_semantic_projection,
    semantic_identity,
)
from .time_semantics import format_utc


SCHEMA_VERSION = "1"
STORE_DOMAIN = "SPEC-013-CANDIDATE-B-EVIDENCE"
_TABLES = {
    "evidence_store_metadata",
    "evidence_revisions",
    "evidence_snapshots",
    "snapshot_revision_bindings",
    "capture_results",
    "materiality_results",
}


class SqliteEvidenceStore:
    """Candidate-B local authority; never reads or mutates another domain store."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float = 5.0,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if isinstance(path, Path):
            path = str(path)
        if not isinstance(path, str) or not path or not path.strip():
            raise ValueError("Evidence Store path must be explicit and non-empty")
        if path == ":memory:":
            raise ValueError("Evidence Store requires an independent durable file path")
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.path = str(Path(path).resolve())
        self._fault_injector = fault_injector
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(
                self.path,
                timeout=float(timeout_seconds),
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
            self._initialize_or_recognize_schema()
            self.validate_integrity()
        except BaseException:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise

    def __enter__(self) -> "SqliteEvidenceStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
                self._connection = None  # type: ignore[assignment]

    def validate_local_readiness(self) -> EvidenceReadiness:
        self.validate_integrity()
        return EvidenceReadiness.READY

    def validate_integrity(self) -> EvidenceIntegrityStatus:
        with self._lock:
            try:
                self._validate_schema_recognition()
                result = self._connection.execute("PRAGMA integrity_check").fetchall()
                if result != [("ok",)]:
                    raise EvidenceStoreIntegrityError("SQLite physical integrity check failed")
                if self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise EvidenceStoreIntegrityError("Evidence Store has dangling foreign keys")
                with self._read_transaction():
                    revision_ids = [row[0] for row in self._connection.execute(
                        "SELECT revision_id FROM evidence_revisions ORDER BY revision_id"
                    )]
                    snapshot_ids = [row[0] for row in self._connection.execute(
                        "SELECT snapshot_id FROM evidence_snapshots ORDER BY snapshot_id"
                    )]
                    operation_ids = [row[0] for row in self._connection.execute(
                        "SELECT capture_operation_id FROM capture_results ORDER BY capture_operation_id"
                    )]
                    materiality_ids = [row[0] for row in self._connection.execute(
                        "SELECT materiality_result_id FROM materiality_results ORDER BY materiality_result_id"
                    )]
                    for revision_id in revision_ids:
                        self._read_revision_locked(revision_id)
                    for snapshot_id in snapshot_ids:
                        self._read_snapshot_locked(snapshot_id)
                    for operation_id in operation_ids:
                        self._read_outcome_locked(operation_id)
                    for result_id in materiality_ids:
                        self._read_materiality_locked(result_id)
                    self._validate_global_counts_locked()
                return EvidenceIntegrityStatus.VALID
            except EvidenceDomainError:
                raise
            except (sqlite3.Error, TypeError, ValueError, KeyError, IndexError) as exc:
                raise EvidenceStoreIntegrityError(
                    "Evidence Store authority cannot be validated"
                ) from exc

    def commit_success(self, success: CaptureSuccess) -> CaptureTerminalOutcome:
        if not isinstance(success, CaptureSuccess):
            raise TypeError("success must be a CaptureSuccess")
        identity = capture_command_semantic_identity(success.command)
        self._inject("before_transaction")
        with self._lock:
            with self._write_transaction():
                existing = self._read_outcome_locked(success.command.capture_operation_id)
                if existing is not None:
                    self._require_same_command(existing, identity)
                    if existing.terminal_kind is not CaptureTerminalKind.SUCCESS:
                        raise self._contradictory("terminal FAILURE cannot become SUCCESS")
                    return existing
                self._create_or_verify_revision_locked(success.revision)
                self._insert_snapshot_locked(success.snapshot)
                self._connection.execute(
                    "INSERT INTO snapshot_revision_bindings(snapshot_id, revision_id) VALUES (?, ?)",
                    (success.snapshot.snapshot_id, success.revision.revision_id),
                )
                self._inject("during_transaction")
                self._connection.execute(
                    """INSERT INTO capture_results(
                           capture_operation_id, command_identity, command_json, terminal_kind,
                           snapshot_id, revision_id, failure_kind, retry_disposition,
                           failure_summary, failure_provenance_json
                       ) VALUES (?, ?, ?, 'SUCCESS', ?, ?, NULL, NULL, NULL, NULL)""",
                    (
                        success.command.capture_operation_id,
                        identity,
                        canonical_json(capture_command_semantic_projection(success.command)),
                        success.snapshot.snapshot_id,
                        success.revision.revision_id,
                    ),
                )
                outcome = self._read_outcome_locked(success.command.capture_operation_id)
                if outcome is None:  # pragma: no cover - same transaction inserted it.
                    raise EvidenceStoreIntegrityError("SUCCESS result was not observable")
        self._inject("after_commit")
        return outcome

    def commit_failure(
        self,
        command: CaptureCommand,
        failure: CaptureFailure,
        *,
        safe_provenance: tuple[str, ...] = (),
    ) -> CaptureTerminalOutcome:
        if not isinstance(command, CaptureCommand):
            raise TypeError("command must be a CaptureCommand")
        if not isinstance(failure, CaptureFailure):
            raise TypeError("failure must be a CaptureFailure")
        if failure.capture_operation_id != command.capture_operation_id:
            raise ValueError("CaptureFailure does not belong to CaptureCommand")
        candidate = CaptureTerminalOutcome(
            command.capture_operation_id,
            capture_command_semantic_identity(command),
            CaptureTerminalKind.FAILURE,
            failure=failure,
            safe_provenance=safe_provenance,
        )
        self._inject("before_transaction")
        with self._lock:
            with self._write_transaction():
                existing = self._read_outcome_locked(command.capture_operation_id)
                if existing is not None:
                    self._require_same_command(existing, candidate.command_semantic_identity)
                    if existing != candidate:
                        raise self._contradictory("terminal result cannot be replaced")
                    return existing
                self._inject("during_transaction")
                self._connection.execute(
                    """INSERT INTO capture_results(
                           capture_operation_id, command_identity, command_json, terminal_kind,
                           snapshot_id, revision_id, failure_kind, retry_disposition,
                           failure_summary, failure_provenance_json
                       ) VALUES (?, ?, ?, 'FAILURE', NULL, NULL, ?, ?, ?, ?)""",
                    (
                        command.capture_operation_id,
                        candidate.command_semantic_identity,
                        canonical_json(capture_command_semantic_projection(command)),
                        failure.kind.value,
                        failure.retry_disposition.value,
                        failure.safe_summary,
                        canonical_json(candidate.safe_provenance),
                    ),
                )
                outcome = self._read_outcome_locked(command.capture_operation_id)
                if outcome is None:  # pragma: no cover
                    raise EvidenceStoreIntegrityError("FAILURE result was not observable")
        self._inject("after_commit")
        return outcome

    def commit_materiality_result(self, result: MaterialityResult) -> MaterialityResult:
        if not isinstance(result, MaterialityResult):
            raise TypeError("result must be a MaterialityResult")
        request_identity = self._materiality_request_identity(result.request)
        result_json = self._encode_materiality(result)
        self._inject("before_transaction")
        with self._lock:
            with self._write_transaction():
                existing_by_id = self._read_materiality_locked(result.materiality_result_id)
                row = self._connection.execute(
                    "SELECT materiality_result_id FROM materiality_results WHERE request_identity = ?",
                    (request_identity,),
                ).fetchone()
                existing_by_request = (
                    None if row is None else self._read_materiality_locked(row[0])
                )
                existing = existing_by_id or existing_by_request
                if existing is not None:
                    if existing != result:
                        raise self._contradictory("Materiality identity/request has contradictory result")
                    return existing
                self._require_materiality_revisions_locked(result.request)
                self._inject("during_transaction")
                self._connection.execute(
                    """INSERT INTO materiality_results(
                           materiality_result_id, request_identity, request_json, result_json,
                           evaluation_kind, baseline_revision_id, candidate_revision_id,
                           materiality_rule_version, judgement
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        result.materiality_result_id,
                        request_identity,
                        canonical_json(self._materiality_request_projection(result.request)),
                        result_json,
                        result.request.evaluation_kind.value,
                        result.request.baseline_revision_id,
                        result.request.candidate_revision_id,
                        result.request.materiality_rule_version,
                        result.judgement.value if result.judgement else None,
                    ),
                )
        self._inject("after_commit")
        return result

    def read_capture_outcome(self, capture_operation_id: str) -> CaptureTerminalOutcome | None:
        self._reference(capture_operation_id, "capture_operation_id")
        self.validate_integrity()
        with self._lock, self._read_transaction():
            return self._read_outcome_locked(capture_operation_id)

    def resolve_snapshot(self, snapshot_id: str) -> EvidenceSnapshot | None:
        self._reference(snapshot_id, "snapshot_id")
        self.validate_integrity()
        with self._lock, self._read_transaction():
            return self._read_snapshot_locked(snapshot_id)

    def resolve_revision(self, revision_id: str) -> EvidenceRevision | None:
        self._reference(revision_id, "revision_id")
        self.validate_integrity()
        with self._lock, self._read_transaction():
            return self._read_revision_locked(revision_id)

    def read_materiality_result(self, materiality_result_id: str) -> MaterialityResult | None:
        self._reference(materiality_result_id, "materiality_result_id")
        self.validate_integrity()
        with self._lock, self._read_transaction():
            return self._read_materiality_locked(materiality_result_id)

    def read_materiality_result_for_request(
        self, request: MaterialityRequest
    ) -> MaterialityResult | None:
        if not isinstance(request, MaterialityRequest):
            raise TypeError("request must be a MaterialityRequest")
        self.validate_integrity()
        identity = self._materiality_request_identity(request)
        with self._lock, self._read_transaction():
            row = self._connection.execute(
                "SELECT materiality_result_id FROM materiality_results WHERE request_identity = ?",
                (identity,),
            ).fetchone()
            return None if row is None else self._read_materiality_locked(row[0])

    def enumerate_recovery_facts(self) -> EvidenceRecoveryFacts:
        self.validate_integrity()
        with self._lock, self._read_transaction():
            outcomes = tuple(
                self._required(self._read_outcome_locked(row[0]), "Capture Result")
                for row in self._connection.execute(
                    "SELECT capture_operation_id FROM capture_results ORDER BY capture_operation_id"
                )
            )
            snapshots = tuple(
                self._required(self._read_snapshot_locked(row[0]), "Snapshot")
                for row in self._connection.execute(
                    "SELECT snapshot_id FROM evidence_snapshots ORDER BY snapshot_id"
                )
            )
            revisions = tuple(
                self._required(self._read_revision_locked(row[0]), "Revision")
                for row in self._connection.execute(
                    "SELECT revision_id FROM evidence_revisions ORDER BY revision_id"
                )
            )
            materiality = tuple(
                self._required(self._read_materiality_locked(row[0]), "Materiality Result")
                for row in self._connection.execute(
                    "SELECT materiality_result_id FROM materiality_results ORDER BY materiality_result_id"
                )
            )
            return EvidenceRecoveryFacts(outcomes, snapshots, revisions, materiality)

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise self._sqlite_error(exc, "cannot begin Evidence Store transaction") from exc
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            try:
                self._connection.commit()
            except sqlite3.Error as exc:
                self._connection.rollback()
                raise EvidenceStoreIntegrityError(
                    "Evidence Store cannot confirm transaction commit"
                ) from exc

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _initialize_or_recognize_schema(self) -> None:
        tables = self._user_tables()
        if not tables:
            created = False
            with self._write_transaction():
                # Another connection may have initialized the explicit path
                # while this connection was waiting for BEGIN IMMEDIATE.
                if not self._user_tables():
                    for statement in self._schema_statements():
                        self._connection.execute(statement)
                    self._connection.executemany(
                        "INSERT INTO evidence_store_metadata(metadata_key, metadata_value) VALUES (?, ?)",
                        (("store_domain", STORE_DOMAIN), ("schema_version", SCHEMA_VERSION)),
                    )
                    created = True
            if created:
                return
            tables = self._user_tables()
        if "evidence_store_metadata" not in tables:
            raise EvidenceStoreIntegrityError(
                "existing database is unversioned, malformed, or belongs to another domain",
                kind=EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
            )
        try:
            metadata = dict(self._connection.execute(
                "SELECT metadata_key, metadata_value FROM evidence_store_metadata"
            ))
        except sqlite3.Error as exc:
            raise EvidenceStoreIntegrityError(
                "Evidence Store metadata is malformed",
                kind=EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
            ) from exc
        if metadata.get("store_domain") != STORE_DOMAIN:
            raise EvidenceStoreIntegrityError(
                "database is not Candidate-B Evidence Store authority",
                kind=EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
            )
        version = metadata.get("schema_version")
        if version == "0":
            raise EvidenceStoreError(
                EvidenceFailureKind.MIGRATION_REQUIRED,
                "recognized older Evidence Store schema requires governed migration",
            )
        if version != SCHEMA_VERSION:
            raise EvidenceStoreIntegrityError(
                "unknown Evidence Store schema version",
                kind=EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
            )
        self._validate_schema_recognition()

    def _validate_schema_recognition(self) -> None:
        user_objects = {
            (row[0], row[1], row[2])
            for row in self._connection.execute(
                """SELECT type, name, tbl_name FROM sqlite_master
                     WHERE substr(name, 1, 7) <> 'sqlite_'"""
            )
        }
        expected_objects = {("table", table, table) for table in _TABLES}
        if user_objects != expected_objects:
            raise EvidenceStoreIntegrityError(
                "Evidence Store schema contains unexpected user-defined objects",
                kind=EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
            )
        if self._user_tables() != _TABLES:
            raise EvidenceStoreIntegrityError(
                "Evidence Store schema contains missing or foreign tables",
                kind=EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
            )
        expected = {
            "evidence_store_metadata": ("metadata_key", "metadata_value"),
            "evidence_revisions": (
                "revision_id", "incident_id", "canonicalization_version",
                "canonical_semantic_content", "integrity_identity",
            ),
            "evidence_snapshots": (
                "snapshot_id", "capture_operation_id", "incident_id", "snapshot_at",
                "capture_contract_version", "canonicalization_version",
                "source_policy_version", "bounds_policy_version", "config_identity",
                "completeness", "source_statuses_json", "canonical_snapshot_content",
                "integrity_identity",
            ),
            "snapshot_revision_bindings": ("snapshot_id", "revision_id"),
            "capture_results": (
                "capture_operation_id", "command_identity", "command_json", "terminal_kind",
                "snapshot_id", "revision_id", "failure_kind", "retry_disposition",
                "failure_summary", "failure_provenance_json",
            ),
            "materiality_results": (
                "materiality_result_id", "request_identity", "request_json", "result_json",
                "evaluation_kind", "baseline_revision_id", "candidate_revision_id",
                "materiality_rule_version", "judgement",
            ),
        }
        for table, columns in expected.items():
            actual = tuple(row[1] for row in self._connection.execute(f"PRAGMA table_info({table})"))
            if actual != columns:
                raise EvidenceStoreIntegrityError(
                    f"Evidence Store table {table} has unsupported schema shape",
                    kind=EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
                )
        metadata = dict(self._connection.execute(
            "SELECT metadata_key, metadata_value FROM evidence_store_metadata"
        ))
        if metadata != {"store_domain": STORE_DOMAIN, "schema_version": SCHEMA_VERSION}:
            raise EvidenceStoreIntegrityError("Evidence Store metadata is contradictory")
        for table, column in (
            ("evidence_revisions", "revision_id"),
            ("evidence_revisions", "integrity_identity"),
            ("evidence_snapshots", "snapshot_id"),
            ("evidence_snapshots", "capture_operation_id"),
            ("evidence_snapshots", "integrity_identity"),
            ("capture_results", "capture_operation_id"),
            ("capture_results", "snapshot_id"),
            ("materiality_results", "materiality_result_id"),
            ("materiality_results", "request_identity"),
        ):
            if not self._is_unique_column(table, column):
                raise EvidenceStoreIntegrityError(
                    f"Evidence Store lacks unique {table}.{column} authority",
                    kind=EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
                )
        nullable = {
            "capture_results": {
                "snapshot_id", "revision_id", "failure_kind", "retry_disposition",
                "failure_summary", "failure_provenance_json",
            },
            "materiality_results": {"baseline_revision_id", "judgement"},
        }
        primary_keys = {
            "evidence_store_metadata": "metadata_key",
            "evidence_revisions": "revision_id",
            "evidence_snapshots": "snapshot_id",
            "snapshot_revision_bindings": "snapshot_id",
            "capture_results": "capture_operation_id",
            "materiality_results": "materiality_result_id",
        }
        for table, columns in expected.items():
            info = self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            if any(str(row[2]).upper() != "TEXT" for row in info):
                raise EvidenceStoreIntegrityError(
                    f"Evidence Store table {table} has unsafe column affinity",
                    kind=EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
                )
            for row in info:
                should_be_nullable = row[1] in nullable.get(table, set())
                if bool(row[3]) == should_be_nullable:
                    raise EvidenceStoreIntegrityError(
                        f"Evidence Store table {table} has unsafe nullability",
                        kind=EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
                    )
                if bool(row[5]) != (row[1] == primary_keys[table]):
                    raise EvidenceStoreIntegrityError(
                        f"Evidence Store table {table} has unsafe primary-key shape",
                        kind=EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
                    )
        expected_foreign_keys = {
            "snapshot_revision_bindings": {
                ("snapshot_id", "evidence_snapshots", "snapshot_id"),
                ("revision_id", "evidence_revisions", "revision_id"),
            },
            "materiality_results": {
                ("baseline_revision_id", "evidence_revisions", "revision_id"),
                ("candidate_revision_id", "evidence_revisions", "revision_id"),
            },
        }
        for table in expected:
            actual_foreign_keys = {
                (row[3], row[2], row[4])
                for row in self._connection.execute(f"PRAGMA foreign_key_list({table})")
            }
            if actual_foreign_keys != expected_foreign_keys.get(table, set()):
                raise EvidenceStoreIntegrityError(
                    f"Evidence Store table {table} has unsafe foreign-key topology",
                    kind=EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
                )
        expected_sql = {}
        for statement in self._schema_statements():
            match = re.match(r"\s*CREATE\s+TABLE\s+([A-Za-z0-9_]+)", statement, re.IGNORECASE)
            if match is None:  # pragma: no cover - constants are implementation-owned.
                raise AssertionError("invalid Evidence Store schema statement")
            expected_sql[match.group(1)] = self._canonical_schema_sql(statement)
        for table, expected_statement in expected_sql.items():
            row = self._connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            actual_statement = None if row is None else row[0]
            if (
                not isinstance(actual_statement, str)
                or self._canonical_schema_sql(actual_statement) != expected_statement
            ):
                raise EvidenceStoreIntegrityError(
                    f"Evidence Store table {table} has altered constraint semantics",
                    kind=EvidenceFailureKind.UNSUPPORTED_EVIDENCE_SCHEMA,
                )

    @staticmethod
    def _canonical_schema_sql(value: str) -> str:
        """Normalize outside quoted tokens while preserving literal semantics."""
        normalized: list[str] = []
        quote: str | None = None
        index = 0
        while index < len(value):
            character = value[index]
            if quote is not None:
                normalized.append(character)
                if character == quote:
                    if quote != "]" and index + 1 < len(value) and value[index + 1] == quote:
                        normalized.append(value[index + 1])
                        index += 2
                        continue
                    quote = None
                index += 1
                continue
            if character in {"'", '"', "`"}:
                quote = character
                normalized.append(character)
            elif character == "[":
                quote = "]"
                normalized.append(character)
            elif not character.isspace():
                normalized.append(character.lower())
            index += 1
        return "".join(normalized).rstrip(";")

    @staticmethod
    def _schema_statements() -> tuple[str, ...]:
        return (
            """CREATE TABLE evidence_store_metadata (
                   metadata_key TEXT PRIMARY KEY NOT NULL,
                   metadata_value TEXT NOT NULL)""",
            """CREATE TABLE evidence_revisions (
                   revision_id TEXT PRIMARY KEY NOT NULL,
                   incident_id TEXT NOT NULL,
                   canonicalization_version TEXT NOT NULL,
                   canonical_semantic_content TEXT NOT NULL,
                   integrity_identity TEXT NOT NULL UNIQUE)""",
            """CREATE TABLE evidence_snapshots (
                   snapshot_id TEXT PRIMARY KEY NOT NULL,
                   capture_operation_id TEXT NOT NULL UNIQUE,
                   incident_id TEXT NOT NULL,
                   snapshot_at TEXT NOT NULL,
                   capture_contract_version TEXT NOT NULL,
                   canonicalization_version TEXT NOT NULL,
                   source_policy_version TEXT NOT NULL,
                   bounds_policy_version TEXT NOT NULL,
                   config_identity TEXT NOT NULL,
                   completeness TEXT NOT NULL CHECK(completeness IN ('FULL','DEGRADED')),
                   source_statuses_json TEXT NOT NULL,
                   canonical_snapshot_content TEXT NOT NULL,
                   integrity_identity TEXT NOT NULL UNIQUE)""",
            """CREATE TABLE snapshot_revision_bindings (
                   snapshot_id TEXT PRIMARY KEY NOT NULL REFERENCES evidence_snapshots(snapshot_id),
                   revision_id TEXT NOT NULL REFERENCES evidence_revisions(revision_id))""",
            """CREATE TABLE capture_results (
                   capture_operation_id TEXT PRIMARY KEY NOT NULL,
                   command_identity TEXT NOT NULL,
                   command_json TEXT NOT NULL,
                   terminal_kind TEXT NOT NULL CHECK(terminal_kind IN ('SUCCESS','FAILURE')),
                   snapshot_id TEXT UNIQUE,
                   revision_id TEXT,
                   failure_kind TEXT,
                   retry_disposition TEXT,
                   failure_summary TEXT,
                   failure_provenance_json TEXT,
                   CHECK((terminal_kind='SUCCESS' AND snapshot_id IS NOT NULL AND revision_id IS NOT NULL
                          AND failure_kind IS NULL AND retry_disposition IS NULL
                          AND failure_summary IS NULL AND failure_provenance_json IS NULL)
                      OR (terminal_kind='FAILURE' AND snapshot_id IS NULL AND revision_id IS NULL
                          AND failure_kind IS NOT NULL AND retry_disposition IS NOT NULL
                          AND failure_summary IS NOT NULL AND failure_provenance_json IS NOT NULL)))""",
            """CREATE TABLE materiality_results (
                   materiality_result_id TEXT PRIMARY KEY NOT NULL,
                   request_identity TEXT NOT NULL UNIQUE,
                   request_json TEXT NOT NULL,
                   result_json TEXT NOT NULL,
                   evaluation_kind TEXT NOT NULL CHECK(evaluation_kind IN ('PAIRWISE','NO_BASELINE')),
                   baseline_revision_id TEXT REFERENCES evidence_revisions(revision_id),
                   candidate_revision_id TEXT NOT NULL REFERENCES evidence_revisions(revision_id),
                   materiality_rule_version TEXT NOT NULL,
                   judgement TEXT,
                   CHECK((evaluation_kind='PAIRWISE' AND baseline_revision_id IS NOT NULL AND judgement IS NOT NULL)
                      OR (evaluation_kind='NO_BASELINE' AND baseline_revision_id IS NULL AND judgement IS NULL)))""",
        )

    def _create_or_verify_revision_locked(self, revision: EvidenceRevision) -> None:
        existing = self._read_revision_locked(revision.revision_id)
        if existing is None:
            collision = self._connection.execute(
                "SELECT revision_id FROM evidence_revisions WHERE integrity_identity = ?",
                (revision.integrity_identity,),
            ).fetchone()
            if collision is not None:
                raise EvidenceStoreIntegrityError("Revision integrity identity collision")
            self._connection.execute(
                "INSERT INTO evidence_revisions VALUES (?, ?, ?, ?, ?)",
                (
                    revision.revision_id,
                    revision.incident_id,
                    revision.canonicalization_version,
                    revision.canonical_semantic_content,
                    revision.integrity_identity,
                ),
            )
        elif existing != revision:
            raise EvidenceStoreIntegrityError("same Revision identity has contradictory content")

    def _insert_snapshot_locked(self, snapshot: EvidenceSnapshot) -> None:
        if self._read_snapshot_locked(snapshot.snapshot_id) is not None:
            raise EvidenceStoreIntegrityError("Snapshot identity already exists")
        statuses = [[source.value, status.value] for source, status in snapshot.source_statuses]
        self._connection.execute(
            """INSERT INTO evidence_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot.snapshot_id,
                snapshot.capture_operation_id,
                snapshot.incident_id,
                format_utc(snapshot.snapshot_at),
                snapshot.capture_contract_version,
                snapshot.canonicalization_version,
                snapshot.source_policy_version,
                snapshot.bounds_policy_version,
                snapshot.config_identity,
                snapshot.completeness.value,
                canonical_json(statuses),
                snapshot.canonical_snapshot_content,
                self._snapshot_integrity_identity(snapshot),
            ),
        )

    def _read_revision_locked(self, revision_id: str) -> EvidenceRevision | None:
        row = self._connection.execute(
            "SELECT * FROM evidence_revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return EvidenceRevision(*row)
        except (TypeError, ValueError) as exc:
            raise EvidenceStoreIntegrityError("malformed or contradictory Evidence Revision") from exc

    def _read_snapshot_locked(self, snapshot_id: str) -> EvidenceSnapshot | None:
        row = self._connection.execute(
            "SELECT * FROM evidence_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            statuses_raw = json.loads(row[10])
            statuses = tuple((EvidenceSource(item[0]), SourceStatus(item[1])) for item in statuses_raw)
            binding = self._connection.execute(
                "SELECT revision_id FROM snapshot_revision_bindings WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchall()
            if len(binding) != 1:
                raise ValueError("Snapshot must have exactly one Revision binding")
            revision = self._read_revision_locked(binding[0][0])
            if revision is None:
                raise ValueError("Snapshot binding references missing Revision")
            snapshot = EvidenceSnapshot(
                row[0], row[1], row[2], self._parse_time(row[3]), row[4], row[5], row[6],
                row[7], row[8], binding[0][0], EvidenceCompleteness(row[9]), statuses, row[11],
            )
            if row[12] != self._snapshot_integrity_identity(snapshot):
                raise ValueError("Snapshot integrity identity is invalid")
            if snapshot.incident_id != revision.incident_id:
                raise ValueError("Snapshot and Revision Incident differ")
            if snapshot.canonicalization_version != revision.canonicalization_version:
                raise ValueError("Snapshot and Revision canonicalization differ")
            return snapshot
        except EvidenceStoreIntegrityError:
            raise
        except (TypeError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
            raise EvidenceStoreIntegrityError("malformed or contradictory Evidence Snapshot") from exc

    def _read_outcome_locked(self, operation_id: str) -> CaptureTerminalOutcome | None:
        row = self._connection.execute(
            "SELECT * FROM capture_results WHERE capture_operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            command_data = json.loads(row[2])
            command = self._decode_command(command_data)
            if capture_command_semantic_identity(command) != row[1] or command.capture_operation_id != row[0]:
                raise ValueError("Capture Result command identity is contradictory")
            kind = CaptureTerminalKind(row[3])
            if kind is CaptureTerminalKind.SUCCESS:
                snapshot = self._read_snapshot_locked(row[4])
                revision = self._read_revision_locked(row[5])
                if snapshot is None or revision is None:
                    raise ValueError("SUCCESS has dangling Snapshot/Revision")
                if snapshot.capture_operation_id != row[0] or snapshot.revision_id != row[5]:
                    raise ValueError("SUCCESS authority is contradictory")
                # Re-run the complete persistence-boundary contract on reads;
                # integrity cannot rely only on validations performed before
                # the original commit.
                CaptureSuccess(command, snapshot, revision)
                return CaptureTerminalOutcome(row[0], row[1], kind, row[4], row[5])
            failure = CaptureFailure(
                row[0], EvidenceFailureKind(row[6]), RetryDisposition(row[7]), row[8]
            )
            provenance = tuple(json.loads(row[9]))
            snapshot_count = self._connection.execute(
                "SELECT COUNT(*) FROM evidence_snapshots WHERE capture_operation_id = ?", (row[0],)
            ).fetchone()[0]
            if snapshot_count:
                raise ValueError("FAILURE operation owns a successful Snapshot")
            return CaptureTerminalOutcome(
                row[0], row[1], kind, failure=failure, safe_provenance=provenance
            )
        except EvidenceStoreIntegrityError:
            raise
        except (TypeError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
            raise EvidenceStoreIntegrityError("malformed or contradictory Capture Result") from exc

    def _read_materiality_locked(self, result_id: str) -> MaterialityResult | None:
        row = self._connection.execute(
            "SELECT * FROM materiality_results WHERE materiality_result_id = ?", (result_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            result = self._decode_materiality(json.loads(row[3]))
            request = result.request
            if result.materiality_result_id != row[0]:
                raise ValueError("Materiality result identity differs from payload")
            if self._materiality_request_identity(request) != row[1]:
                raise ValueError("Materiality request identity differs from payload")
            if canonical_json(self._materiality_request_projection(request)) != row[2]:
                raise ValueError("Materiality request projection differs from payload")
            expected = (
                request.evaluation_kind.value, request.baseline_revision_id,
                request.candidate_revision_id, request.materiality_rule_version,
                result.judgement.value if result.judgement else None,
            )
            if expected != (row[4], row[5], row[6], row[7], row[8]):
                raise ValueError("Materiality indexed fields contradict payload")
            self._require_materiality_revisions_locked(request)
            return result
        except EvidenceStoreIntegrityError:
            raise
        except (TypeError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
            raise EvidenceStoreIntegrityError("malformed or contradictory Materiality Result") from exc

    def _require_materiality_revisions_locked(self, request: MaterialityRequest) -> None:
        candidate = self._read_revision_locked(request.candidate_revision_id)
        if candidate is None:
            raise EvidenceStoreIntegrityError(
                "Materiality candidate Revision is missing",
                kind=EvidenceFailureKind.DANGLING_EVIDENCE_REFERENCE,
            )
        if request.baseline_revision_id is not None:
            baseline = self._read_revision_locked(request.baseline_revision_id)
            if baseline is None:
                raise EvidenceStoreIntegrityError(
                    "Materiality baseline Revision is missing",
                    kind=EvidenceFailureKind.DANGLING_EVIDENCE_REFERENCE,
                )
            if baseline.incident_id != candidate.incident_id:
                raise EvidenceStoreIntegrityError("Materiality Revisions belong to different Incidents")

    def _validate_global_counts_locked(self) -> None:
        counts = dict(self._connection.execute(
            "SELECT terminal_kind, COUNT(*) FROM capture_results GROUP BY terminal_kind"
        ))
        snapshots = self._connection.execute("SELECT COUNT(*) FROM evidence_snapshots").fetchone()[0]
        bindings = self._connection.execute("SELECT COUNT(*) FROM snapshot_revision_bindings").fetchone()[0]
        if snapshots != bindings or snapshots != counts.get("SUCCESS", 0):
            raise EvidenceStoreIntegrityError("SUCCESS/Snapshot/Revision binding counts contradict")
        orphan_revisions = self._connection.execute(
            """SELECT COUNT(*) FROM evidence_revisions r
                 WHERE NOT EXISTS (SELECT 1 FROM snapshot_revision_bindings b WHERE b.revision_id=r.revision_id)"""
        ).fetchone()[0]
        if orphan_revisions:
            raise EvidenceStoreIntegrityError("Revision exists without a committed Snapshot binding")

    @staticmethod
    def _snapshot_integrity_identity(snapshot: EvidenceSnapshot) -> str:
        return semantic_identity(
            "spec013-snapshot-integrity",
            {
                "snapshot_id": snapshot.snapshot_id,
                "capture_operation_id": snapshot.capture_operation_id,
                "incident_id": snapshot.incident_id,
                "snapshot_at": snapshot.snapshot_at,
                "capture_contract_version": snapshot.capture_contract_version,
                "canonicalization_version": snapshot.canonicalization_version,
                "source_policy_version": snapshot.source_policy_version,
                "bounds_policy_version": snapshot.bounds_policy_version,
                "config_identity": snapshot.config_identity,
                "revision_id": snapshot.revision_id,
                "completeness": snapshot.completeness,
                "source_statuses": snapshot.source_statuses,
                "snapshot_content": json.loads(snapshot.canonical_snapshot_content),
            },
        )

    def _user_tables(self) -> set[str]:
        return {
            row[0] for row in self._connection.execute(
                """SELECT name FROM sqlite_master
                     WHERE type='table' AND substr(name, 1, 7) <> 'sqlite_'"""
            )
        }

    def _is_unique_column(self, table: str, column: str) -> bool:
        primary = [row for row in self._connection.execute(f"PRAGMA table_info({table})") if row[5]]
        if len(primary) == 1 and primary[0][1] == column:
            return True
        for index in self._connection.execute(f"PRAGMA index_list({table})"):
            if index[2] and [row[2] for row in self._connection.execute(
                f"PRAGMA index_info({index[1]})"
            )] == [column]:
                return True
        return False

    @staticmethod
    def _decode_command(data: object) -> CaptureCommand:
        if not isinstance(data, dict):
            raise ValueError("command payload must be an object")
        return CaptureCommand(
            data["capture_operation_id"], data["incident_id"],
            SqliteEvidenceStore._parse_time(data["snapshot_at"]),
            data["capture_contract_version"], data["canonicalization_version"],
            data["source_policy_version"], data["bounds_policy_version"], data["config_identity"],
        )

    @staticmethod
    def _materiality_request_projection(request: MaterialityRequest) -> dict[str, object]:
        return {
            "evaluation_kind": request.evaluation_kind.value,
            "baseline_revision_id": request.baseline_revision_id,
            "candidate_revision_id": request.candidate_revision_id,
            "materiality_rule_version": request.materiality_rule_version,
        }

    @classmethod
    def _materiality_request_identity(cls, request: MaterialityRequest) -> str:
        return semantic_identity("spec013-materiality-request", cls._materiality_request_projection(request))

    @classmethod
    def _encode_materiality(cls, result: MaterialityResult) -> str:
        return canonical_json({
            "materiality_result_id": result.materiality_result_id,
            "request": cls._materiality_request_projection(result.request),
            "judgement": result.judgement.value if result.judgement else None,
            "reason_facts": result.reason_facts,
        })

    @staticmethod
    def _decode_materiality(data: object) -> MaterialityResult:
        if not isinstance(data, dict) or not isinstance(data.get("request"), dict):
            raise ValueError("Materiality payload must contain a request")
        request_data = data["request"]
        request = MaterialityRequest(
            MaterialityEvaluationKind(request_data["evaluation_kind"]),
            request_data["candidate_revision_id"],
            request_data["materiality_rule_version"],
            request_data["baseline_revision_id"],
        )
        judgement = data["judgement"]
        return MaterialityResult(
            data["materiality_result_id"], request,
            MaterialityJudgement(judgement) if judgement is not None else None,
            tuple(data["reason_facts"]),
        )

    @staticmethod
    def _parse_time(value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError("timestamp must be text")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _reference(value: object, field: str) -> str:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{field} must be a non-empty trimmed identity")
        return value

    @staticmethod
    def _required(value: object | None, subject: str) -> object:
        if value is None:
            raise EvidenceStoreIntegrityError(f"{subject} disappeared during recovery enumeration")
        return value

    @staticmethod
    def _contradictory(message: str) -> EvidenceStoreError:
        return EvidenceStoreError(EvidenceFailureKind.CONTRADICTORY_REPLAY, message)

    @staticmethod
    def _require_same_command(outcome: CaptureTerminalOutcome, identity: str) -> None:
        if outcome.command_semantic_identity != identity:
            raise SqliteEvidenceStore._contradictory(
                "capture_operation_id was reused with different command semantics"
            )

    @staticmethod
    def _sqlite_error(exc: sqlite3.Error, context: str) -> EvidenceStoreError:
        message = str(exc).lower()
        if isinstance(exc, sqlite3.OperationalError) and (
            "locked" in message or "busy" in message
        ):
            return EvidenceStoreError(
                EvidenceFailureKind.TRANSIENT_EVIDENCE_STORE_FAILURE,
                f"{context}: SQLite busy/locked",
            )
        return EvidenceStoreIntegrityError(context)

    def _inject(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)


__all__ = ["SCHEMA_VERSION", "STORE_DOMAIN", "SqliteEvidenceStore"]
