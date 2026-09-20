"""Independent SQLite authority for SPEC-012 Aggregate and Attempt facts.

This Phase-2 adapter intentionally stops at obligation-first Aggregate and
immutable Attempt-core persistence.  Logical Try outcomes, Artifacts,
Versions, publication, Current, and runtime orchestration belong to later
phases and are not represented by writable placeholders here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from math import isfinite
from pathlib import Path
from threading import RLock

from .contracts import (
    AdmitAttemptRequest,
    AttemptLineage,
    AttemptLineageRead,
    CreateAggregateRequest,
    GenerationAttempt,
    GenerationLifecycle,
    GenerationProvenance,
    RcaAggregate,
    RcaDomainError,
    RcaErrorCode,
    require_equivalent_attempt_lineage,
)


SCHEMA_VERSION = "2"
_RECOGNIZED_OLDER_SCHEMA_VERSIONS = frozenset({"1"})
_METADATA_KEY = "rca_store_schema_version"
_TABLES = frozenset(
    {"rca_store_metadata", "rca_aggregates", "rca_attempts", "rca_operation_receipts"}
)


class RcaStoreIntegrityError(RcaDomainError):
    """The persisted Candidate-A authority cannot safely be interpreted."""

    def __init__(self, code: RcaErrorCode, message: str) -> None:
        if code not in {
            RcaErrorCode.INTEGRITY_CORRUPTION,
            RcaErrorCode.SCHEMA_INCOMPATIBILITY,
        }:
            raise TypeError("RCA store integrity errors require an integrity/schema code")
        super().__init__(code, message)


class SqliteRcaStore:
    """Configurable-path Candidate-A SQLite authority for Phase 2."""

    def __init__(
        self,
        database_path: str | Path = "rca_store.db",
        *,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        if isinstance(busy_timeout_seconds, bool) or not isinstance(
            busy_timeout_seconds, (int, float)
        ):
            raise TypeError("busy_timeout_seconds must be a finite non-negative number")
        timeout = float(busy_timeout_seconds)
        if not isfinite(timeout) or timeout < 0:
            raise ValueError("busy_timeout_seconds must be a finite non-negative number")
        if not isinstance(database_path, (str, Path)):
            raise TypeError("database_path must be a string or Path")

        self._path = str(database_path)
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        try:
            self._connection = sqlite3.connect(
                self._path,
                isolation_level=None,
                timeout=timeout,
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
            self._initialize_or_recognize_schema()
            self.validate_local_readiness()
        except RcaDomainError:
            self._close_after_failed_initialization()
            raise
        except sqlite3.Error as exc:
            self._close_after_failed_initialization()
            raise _sqlite_error(exc, "RCA Store cannot initialize local authority") from exc

    def _close_after_failed_initialization(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RcaDomainError(
                RcaErrorCode.LOCAL_READINESS_FAILURE, "RCA Store is closed"
            )
        return self._connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> "SqliteRcaStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_or_discover_aggregate(
        self, request: CreateAggregateRequest
    ) -> RcaAggregate:
        if not isinstance(request, CreateAggregateRequest):
            raise TypeError("request must be CreateAggregateRequest")
        try:
            with self._write_transaction():
                replay = self._read_receipt(request.operation_id)
                if replay is not None:
                    self._require_receipt_identity(replay, "CREATE_AGGREGATE", request)
                    aggregate = self._read_aggregate(request.aggregate_id)
                    if aggregate is None:
                        self._corruption("Aggregate receipt references a missing Aggregate")
                    return aggregate

                by_id = self._read_aggregate(request.aggregate_id)
                by_incident = self._read_aggregate_by_incident(request.incident_id)
                if by_id is not None or by_incident is not None:
                    if by_id != RcaAggregate(request.aggregate_id, request.incident_id) or by_incident != by_id:
                        raise RcaDomainError(
                            RcaErrorCode.IDENTITY_LINEAGE_CONFLICT,
                            "Aggregate identity contradicts immutable Incident binding",
                            operation_id=request.operation_id,
                            aggregate_id=request.aggregate_id,
                        )
                    aggregate = by_id
                else:
                    aggregate = RcaAggregate(request.aggregate_id, request.incident_id)
                    self._db.execute(
                        "INSERT INTO rca_aggregates(aggregate_id, incident_id, created_at) VALUES (?, ?, ?)",
                        (aggregate.aggregate_id, aggregate.incident_id, _timestamp(request.authoritative_now)),
                    )
                self._insert_receipt("CREATE_AGGREGATE", request, aggregate.aggregate_id)
                return aggregate
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot create or discover Aggregate") from exc

    def admit_attempt(self, request: AdmitAttemptRequest) -> GenerationAttempt:
        if not isinstance(request, AdmitAttemptRequest):
            raise TypeError("request must be AdmitAttemptRequest")
        try:
            with self._write_transaction():
                replay = self._read_receipt(request.operation_id)
                if replay is not None:
                    self._require_receipt_identity(replay, "ADMIT_ATTEMPT", request)
                    view = self._read_attempt_lineage(request.lineage.attempt_id)
                    if view is None:
                        self._corruption("Attempt receipt references a missing Attempt")
                    return view.attempt

                if self._read_aggregate(request.lineage.aggregate_id) is None:
                    raise RcaDomainError(
                        RcaErrorCode.INVALID_REFERENCE,
                        "Attempt references an unknown Aggregate",
                        operation_id=request.operation_id,
                        aggregate_id=request.lineage.aggregate_id,
                        attempt_id=request.lineage.attempt_id,
                    )
                view = self._read_attempt_lineage(request.lineage.attempt_id)
                if view is not None:
                    require_equivalent_attempt_lineage(view.attempt.lineage, request.lineage)
                    attempt = view.attempt
                else:
                    lineage = request.lineage
                    provenance = lineage.generation_provenance
                    attempt = GenerationAttempt(lineage, GenerationLifecycle.PENDING)
                    self._db.execute(
                        """INSERT INTO rca_attempts(
                               attempt_id, aggregate_id, evidence_snapshot_id,
                               evidence_revision_id, knowledge_snapshot_id,
                               provider_id, model_id, prompt_id, configuration_id,
                               credential_profile_id, lifecycle, latest_try_ordinal, admitted_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                        (
                            lineage.attempt_id,
                            lineage.aggregate_id,
                            lineage.evidence_snapshot_id,
                            lineage.evidence_revision_id,
                            lineage.knowledge_snapshot_id,
                            provenance.provider_id,
                            provenance.model_id,
                            provenance.prompt_id,
                            provenance.configuration_id,
                            provenance.credential_profile_id,
                            GenerationLifecycle.PENDING.value,
                            _timestamp(request.authoritative_now),
                        ),
                    )
                self._insert_receipt("ADMIT_ATTEMPT", request, request.lineage.attempt_id)
                return attempt
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot admit Attempt") from exc

    def get_aggregate(self, aggregate_id: str) -> RcaAggregate | None:
        _reference(aggregate_id, "aggregate_id")
        try:
            with self._read_snapshot():
                aggregate = self._read_aggregate(aggregate_id)
                if aggregate is not None:
                    self._require_record_receipt("CREATE_AGGREGATE", aggregate_id)
                return aggregate
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot read Aggregate") from exc

    def get_aggregate_by_incident(self, incident_id: str) -> RcaAggregate | None:
        _reference(incident_id, "incident_id")
        try:
            with self._read_snapshot():
                aggregate = self._read_aggregate_by_incident(incident_id)
                if aggregate is not None:
                    self._require_record_receipt("CREATE_AGGREGATE", aggregate.aggregate_id)
                return aggregate
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot read Incident Aggregate binding") from exc

    def get_attempt_lineage(self, attempt_id: str) -> AttemptLineageRead | None:
        _reference(attempt_id, "attempt_id")
        try:
            with self._read_snapshot():
                view = self._read_attempt_lineage(attempt_id)
                if view is not None:
                    self._require_record_receipt("ADMIT_ATTEMPT", attempt_id)
                return view
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot read Attempt lineage") from exc

    def validate_local_readiness(self) -> None:
        """Validate all Phase-2 local authority without skipping bad records."""
        try:
            with self._read_snapshot():
                self._validate_authority_locked()
        except RcaDomainError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_error(exc, "RCA Store cannot validate local readiness") from exc

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise _sqlite_error(exc, "RCA Store cannot begin write transaction") from exc
            try:
                # Every mutation gates on the same fresh, transaction-local
                # authority validation as readiness.  This prevents a command
                # from extending a store whose unrelated records are already
                # contradictory or whose complete enumeration is unreliable.
                self._validate_authority_locked()
                yield
            except BaseException:
                self._db.rollback()
                raise
            else:
                try:
                    self._db.commit()
                except sqlite3.Error as exc:
                    self._db.rollback()
                    raise RcaStoreIntegrityError(
                        RcaErrorCode.INTEGRITY_CORRUPTION,
                        "RCA Store cannot confirm local transaction commit",
                    ) from exc

    @contextmanager
    def _read_snapshot(self) -> Iterator[None]:
        with self._lock:
            try:
                self._db.execute("BEGIN")
                yield
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    def _initialize_or_recognize_schema(self) -> None:
        tables = self._user_tables()
        if not tables:
            initialized = False
            with self._write_transaction_without_schema_gate():
                # A concurrent initializer may have committed while this
                # connection waited for the SQLite write lock.  Re-read only
                # after BEGIN IMMEDIATE owns serialization.
                if self._user_tables():
                    pass
                else:
                    schema = """
                    CREATE TABLE rca_store_metadata (
                        metadata_key TEXT PRIMARY KEY NOT NULL,
                        metadata_value TEXT NOT NULL
                    );
                    CREATE TABLE rca_aggregates (
                        aggregate_id TEXT PRIMARY KEY NOT NULL,
                        incident_id TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE rca_attempts (
                        attempt_id TEXT PRIMARY KEY NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        evidence_snapshot_id TEXT NOT NULL,
                        evidence_revision_id TEXT NOT NULL,
                        knowledge_snapshot_id TEXT NOT NULL,
                        provider_id TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        prompt_id TEXT NOT NULL,
                        configuration_id TEXT NOT NULL,
                        credential_profile_id TEXT NOT NULL,
                        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('PENDING','GENERATING','COMPLETED','FAILED')),
                        latest_try_ordinal INTEGER,
                        admitted_at TEXT NOT NULL,
                        FOREIGN KEY(aggregate_id) REFERENCES rca_aggregates(aggregate_id),
                        CHECK(latest_try_ordinal IS NULL OR latest_try_ordinal > 0)
                    );
                    CREATE TABLE rca_operation_receipts (
                        operation_id TEXT PRIMARY KEY NOT NULL,
                        command_kind TEXT NOT NULL CHECK(command_kind IN ('CREATE_AGGREGATE','ADMIT_ATTEMPT')),
                        semantic_identity TEXT NOT NULL,
                        result_identity TEXT NOT NULL,
                        occurred_at TEXT NOT NULL
                    );
                    CREATE INDEX rca_attempts_by_aggregate
                        ON rca_attempts(aggregate_id, attempt_id);
                    CREATE INDEX rca_receipts_by_result
                        ON rca_operation_receipts(command_kind, result_identity);
                    """
                    for statement in schema.split(";"):
                        if statement.strip():
                            self._db.execute(statement)
                    self._db.execute(
                        "INSERT INTO rca_store_metadata(metadata_key, metadata_value) VALUES (?, ?)",
                        (_METADATA_KEY, SCHEMA_VERSION),
                    )
                    initialized = True
            if initialized:
                return
            tables = self._user_tables()

        if "rca_store_metadata" not in tables:
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "existing database has no Candidate-A schema version authority",
            )
        try:
            rows = self._db.execute(
                "SELECT metadata_key, metadata_value FROM rca_store_metadata"
            ).fetchall()
        except sqlite3.Error as exc:
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A schema metadata is malformed",
            ) from exc
        if len(rows) != 1 or rows[0][0] != _METADATA_KEY or not isinstance(rows[0][1], str):
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A schema metadata is malformed",
            )
        version = rows[0][1]
        if version in _RECOGNIZED_OLDER_SCHEMA_VERSIONS:
            raise RcaDomainError(
                RcaErrorCode.MIGRATION_REQUIRED,
                f"Candidate-A schema version {version} requires governed migration",
            )
        if version != SCHEMA_VERSION:
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A schema version is unknown or unsafe",
            )
        self._validate_current_schema()

    @contextmanager
    def _write_transaction_without_schema_gate(self) -> Iterator[None]:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            yield
        except BaseException:
            self._db.rollback()
            raise
        else:
            self._db.commit()

    def _validate_current_schema(self) -> None:
        if self._user_tables() != _TABLES:
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A database contains missing or unexpected authority tables",
            )
        expected_columns = {
            "rca_store_metadata": {"metadata_key", "metadata_value"},
            "rca_aggregates": {"aggregate_id", "incident_id", "created_at"},
            "rca_attempts": {
                "attempt_id", "aggregate_id", "evidence_snapshot_id",
                "evidence_revision_id", "knowledge_snapshot_id", "provider_id",
                "model_id", "prompt_id", "configuration_id", "credential_profile_id",
                "lifecycle", "latest_try_ordinal", "admitted_at",
            },
            "rca_operation_receipts": {
                "operation_id", "command_kind", "semantic_identity",
                "result_identity", "occurred_at",
            },
        }
        for table, expected in expected_columns.items():
            actual = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
            if actual != expected:
                raise RcaStoreIntegrityError(
                    RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                    f"Candidate-A table {table} has an unsupported schema shape",
                )
        for table, column in (
            ("rca_store_metadata", "metadata_key"),
            ("rca_aggregates", "aggregate_id"),
            ("rca_aggregates", "incident_id"),
            ("rca_attempts", "attempt_id"),
            ("rca_operation_receipts", "operation_id"),
        ):
            if not _has_unique_column(self._db, table, column):
                raise RcaStoreIntegrityError(
                    RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                    f"Candidate-A table {table} lacks unique {column} authority",
                )
        metadata = self._db.execute(
            "SELECT metadata_key, metadata_value FROM rca_store_metadata"
        ).fetchall()
        if metadata != [(_METADATA_KEY, SCHEMA_VERSION)]:
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A current schema metadata is contradictory",
            )
        foreign_keys = self._db.execute("PRAGMA foreign_key_list(rca_attempts)").fetchall()
        if len(foreign_keys) != 1 or (
            foreign_keys[0][2], foreign_keys[0][3], foreign_keys[0][4]
        ) != ("rca_aggregates", "aggregate_id", "aggregate_id"):
            raise RcaStoreIntegrityError(
                RcaErrorCode.SCHEMA_INCOMPATIBILITY,
                "Candidate-A Attempt table lacks its local Aggregate reference",
            )

    def _validate_authority_locked(self) -> None:
        self._validate_current_schema()
        integrity = self._db.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            self._corruption("SQLite integrity_check failed")
        if self._db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            self._corruption("SQLite foreign_key_check found dangling authority")

        aggregate_ids = [
            row[0]
            for row in self._db.execute(
                "SELECT aggregate_id FROM rca_aggregates ORDER BY aggregate_id"
            )
        ]
        attempt_ids = [
            row[0]
            for row in self._db.execute(
                "SELECT attempt_id FROM rca_attempts ORDER BY attempt_id"
            )
        ]
        for aggregate_id in aggregate_ids:
            if self._read_aggregate(aggregate_id) is None:
                self._corruption("Aggregate disappeared during readiness snapshot")
            self._require_record_receipt("CREATE_AGGREGATE", aggregate_id)
        for attempt_id in attempt_ids:
            if self._read_attempt_lineage(attempt_id) is None:
                self._corruption("Attempt disappeared during readiness snapshot")
            self._require_record_receipt("ADMIT_ATTEMPT", attempt_id)
        for row in self._db.execute(
            "SELECT operation_id FROM rca_operation_receipts ORDER BY operation_id"
        ):
            self._validate_receipt(self._read_receipt(row[0]))

    def _user_tables(self) -> frozenset[str]:
        return frozenset(
            row[0]
            for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )

    def _read_aggregate(self, aggregate_id: str) -> RcaAggregate | None:
        row = self._db.execute(
            "SELECT aggregate_id, incident_id, created_at FROM rca_aggregates WHERE aggregate_id = ?",
            (aggregate_id,),
        ).fetchone()
        return None if row is None else self._decode_aggregate(row)

    def _read_aggregate_by_incident(self, incident_id: str) -> RcaAggregate | None:
        rows = self._db.execute(
            "SELECT aggregate_id, incident_id, created_at FROM rca_aggregates WHERE incident_id = ?",
            (incident_id,),
        ).fetchall()
        if len(rows) > 1:
            self._corruption("one Incident resolves to multiple RCA Aggregates")
        return None if not rows else self._decode_aggregate(rows[0])

    def _decode_aggregate(self, row: sqlite3.Row | tuple[object, ...]) -> RcaAggregate:
        try:
            aggregate = RcaAggregate(row[0], row[1])  # type: ignore[arg-type]
            _parse_timestamp(row[2])
            return aggregate
        except (IndexError, TypeError, ValueError, RcaDomainError) as exc:
            raise RcaStoreIntegrityError(
                RcaErrorCode.INTEGRITY_CORRUPTION, "malformed persisted RCA Aggregate"
            ) from exc

    def _read_attempt_lineage(self, attempt_id: str) -> AttemptLineageRead | None:
        row = self._db.execute(
            """SELECT attempt_id, aggregate_id, evidence_snapshot_id, evidence_revision_id,
                      knowledge_snapshot_id, provider_id, model_id, prompt_id,
                      configuration_id, credential_profile_id, lifecycle,
                      latest_try_ordinal, admitted_at
                 FROM rca_attempts WHERE attempt_id = ?""",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            if self._read_aggregate(row[1]) is None:
                raise ValueError("Attempt references missing Aggregate")
            provenance = GenerationProvenance(row[5], row[6], row[7], row[8], row[9])
            lineage = AttemptLineage(row[0], row[1], row[2], row[3], row[4], provenance)
            attempt = GenerationAttempt(lineage, GenerationLifecycle(row[10]), row[11])
            _parse_timestamp(row[12])
            # Phase 2 cannot hold Try facts, so only the coherent initial summary is legal.
            return AttemptLineageRead(attempt, ())
        except (IndexError, TypeError, ValueError, RcaDomainError) as exc:
            raise RcaStoreIntegrityError(
                RcaErrorCode.INTEGRITY_CORRUPTION, "malformed or contradictory persisted Attempt"
            ) from exc

    def _insert_receipt(
        self,
        command_kind: str,
        request: CreateAggregateRequest | AdmitAttemptRequest,
        result_identity: str,
    ) -> None:
        self._db.execute(
            """INSERT INTO rca_operation_receipts(
                   operation_id, command_kind, semantic_identity, result_identity, occurred_at
               ) VALUES (?, ?, ?, ?, ?)""",
            (
                request.operation_id,
                command_kind,
                _semantic_identity(command_kind, request),
                result_identity,
                _timestamp(request.authoritative_now),
            ),
        )

    def _read_receipt(self, operation_id: str) -> tuple[object, ...] | None:
        row = self._db.execute(
            """SELECT operation_id, command_kind, semantic_identity, result_identity, occurred_at
                 FROM rca_operation_receipts WHERE operation_id = ?""",
            (operation_id,),
        ).fetchone()
        return None if row is None else tuple(row)

    def _require_receipt_identity(
        self,
        receipt: tuple[object, ...],
        command_kind: str,
        request: CreateAggregateRequest | AdmitAttemptRequest,
    ) -> None:
        self._validate_receipt(receipt)
        expected_result = (
            request.aggregate_id
            if isinstance(request, CreateAggregateRequest)
            else request.lineage.attempt_id
        )
        if (
            receipt[1] != command_kind
            or receipt[2] != _semantic_identity(command_kind, request)
            or receipt[3] != expected_result
        ):
            raise RcaDomainError(
                RcaErrorCode.RECEIPT_REPLAY_CONFLICT,
                "operation_id resolves to a contradictory Candidate-A command",
                operation_id=request.operation_id,
            )

    def _require_record_receipt(self, command_kind: str, result_identity: str) -> None:
        rows = self._db.execute(
            """SELECT operation_id, command_kind, semantic_identity, result_identity, occurred_at
                 FROM rca_operation_receipts
                WHERE command_kind = ? AND result_identity = ?
                ORDER BY operation_id""",
            (command_kind, result_identity),
        ).fetchall()
        if not rows:
            self._corruption("authoritative RCA record has no admission receipt")
        for row in rows:
            self._validate_receipt(tuple(row))

    def _validate_receipt(self, receipt: tuple[object, ...] | None) -> None:
        if receipt is None:
            self._corruption("missing RCA operation receipt")
        try:
            operation_id, kind, encoded, result_identity, occurred_at = receipt
            _reference(operation_id, "operation_id")
            _reference(result_identity, "result_identity")
            _parse_timestamp(occurred_at)
            payload = json.loads(encoded)  # type: ignore[arg-type]
            if not isinstance(payload, dict) or payload.get("operation_id") != operation_id:
                raise ValueError("receipt identity does not match operation_id")
            if kind == "CREATE_AGGREGATE":
                request = CreateAggregateRequest(
                    payload["operation_id"], payload["aggregate_id"], payload["incident_id"],
                    _parse_timestamp(occurred_at),
                )
                aggregate = self._read_aggregate(result_identity)  # type: ignore[arg-type]
                if aggregate != RcaAggregate(request.aggregate_id, request.incident_id):
                    raise ValueError("Aggregate receipt contradicts its result")
            elif kind == "ADMIT_ATTEMPT":
                request = _attempt_request_from_payload(payload, _parse_timestamp(occurred_at))
                view = self._read_attempt_lineage(result_identity)  # type: ignore[arg-type]
                if view is None or view.attempt.lineage != request.lineage:
                    raise ValueError("Attempt receipt contradicts its result")
            else:
                raise ValueError("unknown receipt command kind")
        except RcaStoreIntegrityError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, RcaDomainError) as exc:
            raise RcaStoreIntegrityError(
                RcaErrorCode.INTEGRITY_CORRUPTION,
                "malformed or contradictory RCA operation receipt",
            ) from exc

    @staticmethod
    def _corruption(message: str) -> None:
        raise RcaStoreIntegrityError(RcaErrorCode.INTEGRITY_CORRUPTION, message)


def _reference(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character in value for character in ("\n", "\r", "\x00"))
    ):
        raise ValueError(f"{field_name} must be a non-empty single-line reference")
    return value


def _timestamp(value: object) -> str:
    from datetime import datetime

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.isoformat()


def _parse_timestamp(value: object):
    from datetime import datetime

    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must retain timezone")
    return parsed


def _semantic_identity(
    command_kind: str, request: CreateAggregateRequest | AdmitAttemptRequest
) -> str:
    if command_kind == "CREATE_AGGREGATE" and isinstance(request, CreateAggregateRequest):
        payload: dict[str, object] = {
            "operation_id": request.operation_id,
            "aggregate_id": request.aggregate_id,
            "incident_id": request.incident_id,
        }
    elif command_kind == "ADMIT_ATTEMPT" and isinstance(request, AdmitAttemptRequest):
        payload = {"operation_id": request.operation_id, "lineage": asdict(request.lineage)}
    else:
        raise TypeError("command kind and request type do not match")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _attempt_request_from_payload(payload: dict[str, object], occurred_at) -> AdmitAttemptRequest:
    lineage_data = payload.get("lineage")
    if not isinstance(lineage_data, dict):
        raise ValueError("Attempt receipt has no lineage object")
    provenance_data = lineage_data.get("generation_provenance")
    if not isinstance(provenance_data, dict):
        raise ValueError("Attempt receipt has no generation provenance")
    provenance = GenerationProvenance(
        provenance_data["provider_id"], provenance_data["model_id"],
        provenance_data["prompt_id"], provenance_data["configuration_id"],
        provenance_data["credential_profile_id"],
    )
    lineage = AttemptLineage(
        lineage_data["attempt_id"], lineage_data["aggregate_id"],
        lineage_data["evidence_snapshot_id"], lineage_data["evidence_revision_id"],
        lineage_data["knowledge_snapshot_id"], provenance,
    )
    return AdmitAttemptRequest(payload["operation_id"], lineage, occurred_at)  # type: ignore[arg-type]


def _has_unique_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    for index in connection.execute(f"PRAGMA index_list({table})"):
        if not index[2]:
            continue
        columns = [row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})")]
        if columns == [column]:
            return True
    return False


def _sqlite_error(exc: sqlite3.Error, context: str) -> RcaDomainError:
    message = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and (
        "locked" in message
        or "busy" in message
        or "unable to open" in message
        or "readonly" in message
        or "disk i/o" in message
        or "database or disk is full" in message
    ):
        return RcaDomainError(RcaErrorCode.LOCAL_READINESS_FAILURE, f"{context}: {exc}")
    return RcaStoreIntegrityError(RcaErrorCode.INTEGRITY_CORRUPTION, context)
