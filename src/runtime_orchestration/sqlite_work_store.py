"""Independent stdlib SQLite adapter for the SPEC-011 D2 logical store."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .clock import canonical_utc, format_utc, parse_utc
from .contracts import (
    RuntimeWorkCorruption,
    RuntimeWorkEnumeration,
    RuntimeWorkKind,
    RuntimeWorkRecord,
    RuntimeWorkStatus,
)


class RuntimeWorkStoreError(RuntimeError):
    pass


class RuntimeWorkStoreIntegrityError(RuntimeWorkStoreError):
    """The complete recovery set cannot be enumerated reliably."""


class UnsupportedRuntimeWorkStoreVersion(RuntimeWorkStoreIntegrityError):
    pass


class RuntimeWorkRecordCorruptError(RuntimeWorkStoreError):
    def __init__(self, corruption: RuntimeWorkCorruption) -> None:
        super().__init__(f"corrupt Runtime work {corruption.record_key}: {corruption.detail}")
        self.corruption = corruption


class ContradictoryRuntimeWorkError(RuntimeWorkStoreError):
    pass


class RuntimeWorkConcurrencyError(RuntimeWorkStoreError):
    pass


class RuntimeWorkNotFoundError(RuntimeWorkStoreError):
    pass


class RuntimeWorkStoreClosedError(RuntimeWorkStoreError):
    pass


_SCHEMA_VERSION = 1
_TABLES = frozenset({"runtime_work_store_metadata", "runtime_work_records"})
_METADATA_COLUMNS = (
    ("singleton", "INTEGER", 1, 1),
    ("schema_version", "INTEGER", 1, 0),
)
_WORK_COLUMNS = (
    ("work_id", "TEXT", 1, 1),
    ("record_version", "INTEGER", 1, 0),
    ("work_kind", "TEXT", 1, 0),
    ("event_id", "TEXT", 1, 0),
    ("incident_id", "TEXT", 0, 0),
    ("stage", "TEXT", 1, 0),
    ("next_action", "TEXT", 1, 0),
    ("operation_id", "TEXT", 0, 0),
    ("workflow_operation_id", "TEXT", 0, 0),
    ("attempt_count", "INTEGER", 1, 0),
    ("retry_limit", "INTEGER", 1, 0),
    ("next_retry_at", "TEXT", 0, 0),
    ("last_attempt_at", "TEXT", 0, 0),
    ("source_domain", "TEXT", 0, 0),
    ("source_error_code", "TEXT", 0, 0),
    ("source_retry_disposition", "TEXT", 0, 0),
    ("status", "TEXT", 1, 0),
    ("created_at", "TEXT", 1, 0),
    ("updated_at", "TEXT", 1, 0),
    ("observed_at", "TEXT", 1, 0),
    ("revision", "INTEGER", 1, 0),
)


class SqliteRuntimeWorkStore:
    """Durable Runtime-only work with strict integrity and CAS updates.

    The adapter rejects any database containing non-Runtime tables. This makes
    accidental reuse of a SPEC-007/008/009/010 database fail closed.
    """

    def __init__(self, database_path: str | Path) -> None:
        self._path = str(database_path)
        self._closed = False
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(self._path, isolation_level=None)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._initialize_or_validate_schema()
        except sqlite3.Error as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._closed = True
            raise RuntimeWorkStoreIntegrityError(
                "Runtime Work Store cannot be opened or read reliably"
            ) from exc
        except BaseException:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            self._closed = True
            raise

    @property
    def database_path(self) -> str:
        return self._path

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> "SqliteRuntimeWorkStore":
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create(self, record: RuntimeWorkRecord) -> RuntimeWorkRecord:
        self._ensure_open()
        if not isinstance(record, RuntimeWorkRecord):
            raise TypeError("record must be RuntimeWorkRecord")
        if record.revision != 0:
            raise ValueError("a new Runtime work record must have revision 0")
        try:
            with self._write_transaction():
                row = self._select_row(record.work_id)
                if row is not None:
                    existing = self._decode_or_raise(row)
                    if _immutable_identity(existing) != _immutable_identity(record):
                        raise ContradictoryRuntimeWorkError(
                            f"work_id {record.work_id} has contradictory stable identity"
                        )
                    if _without_revision(existing) == _without_revision(record):
                        return existing
                    raise ContradictoryRuntimeWorkError(
                        f"work_id {record.work_id} already exists with different work state"
                    )
                persisted = replace(record, revision=1)
                self._connection.execute(
                    """
                    INSERT INTO runtime_work_records (
                        work_id, record_version, work_kind, event_id, incident_id,
                        stage, next_action, operation_id, workflow_operation_id,
                        attempt_count, retry_limit, next_retry_at, last_attempt_at,
                        source_domain, source_error_code, source_retry_disposition,
                        status, created_at, updated_at, observed_at, revision
                    ) VALUES (
                        :work_id, :record_version, :work_kind, :event_id, :incident_id,
                        :stage, :next_action, :operation_id, :workflow_operation_id,
                        :attempt_count, :retry_limit, :next_retry_at, :last_attempt_at,
                        :source_domain, :source_error_code, :source_retry_disposition,
                        :status, :created_at, :updated_at, :observed_at, :revision
                    )
                    """,
                    _encode_record(persisted),
                )
                return persisted
        except sqlite3.Error as exc:
            raise RuntimeWorkStoreIntegrityError("Runtime work create failed") from exc

    def get(self, work_id: str) -> RuntimeWorkRecord | None:
        self._ensure_open()
        _validate_identifier(work_id, "work_id")
        try:
            with self._read_snapshot():
                self._require_integrity()
                row = self._select_row(work_id)
                return None if row is None else self._decode_or_raise(row)
        except sqlite3.Error as exc:
            raise RuntimeWorkStoreIntegrityError("Runtime work lookup failed") from exc

    def enumerate_all(self) -> RuntimeWorkEnumeration:
        """Return all valid rows and every safely isolated corrupt row explicitly."""
        self._ensure_open()
        try:
            with self._read_snapshot():
                self._require_integrity()
                rows = self._connection.execute(
                    "SELECT rowid, * FROM runtime_work_records ORDER BY work_id, rowid"
                ).fetchall()
                records: list[RuntimeWorkRecord] = []
                corruptions: list[RuntimeWorkCorruption] = []
                for row in rows:
                    try:
                        records.append(_decode_record(row))
                    except (TypeError, ValueError, KeyError) as exc:
                        corruptions.append(_corruption(row, exc))
                return RuntimeWorkEnumeration(tuple(records), tuple(corruptions))
        except sqlite3.Error as exc:
            raise RuntimeWorkStoreIntegrityError(
                "Runtime Work Store cannot enumerate the complete recovery set"
            ) from exc

    def enumerate_outstanding(self) -> RuntimeWorkEnumeration:
        """Enumerate recovery work without ever hiding corrupt rows."""
        all_work = self.enumerate_all()
        return RuntimeWorkEnumeration(
            tuple(
                record
                for record in all_work.records
                if record.status is RuntimeWorkStatus.OUTSTANDING
            ),
            all_work.isolated_corruptions,
        )

    def update(
        self, record: RuntimeWorkRecord, *, expected_revision: int
    ) -> RuntimeWorkRecord:
        self._ensure_open()
        if not isinstance(record, RuntimeWorkRecord):
            raise TypeError("record must be RuntimeWorkRecord")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")
        if record.revision not in (0, expected_revision):
            raise RuntimeWorkConcurrencyError("candidate revision does not match expectation")
        try:
            with self._write_transaction():
                row = self._select_row(record.work_id)
                if row is None:
                    raise RuntimeWorkNotFoundError(record.work_id)
                existing = self._decode_or_raise(row)
                if existing.revision != expected_revision:
                    raise RuntimeWorkConcurrencyError("Runtime work revision changed")
                _validate_transition(existing, record)
                if _without_revision(existing) == _without_revision(record):
                    return existing
                persisted = replace(record, revision=expected_revision + 1)
                values = _encode_record(persisted)
                cursor = self._connection.execute(
                    """
                    UPDATE runtime_work_records SET
                        record_version=:record_version, work_kind=:work_kind,
                        event_id=:event_id, incident_id=:incident_id, stage=:stage,
                        next_action=:next_action, operation_id=:operation_id,
                        workflow_operation_id=:workflow_operation_id,
                        attempt_count=:attempt_count, retry_limit=:retry_limit,
                        next_retry_at=:next_retry_at, last_attempt_at=:last_attempt_at,
                        source_domain=:source_domain, source_error_code=:source_error_code,
                        source_retry_disposition=:source_retry_disposition,
                        status=:status, created_at=:created_at, updated_at=:updated_at,
                        observed_at=:observed_at, revision=:revision
                    WHERE work_id=:work_id AND revision=:expected_revision
                    """,
                    {**values, "expected_revision": expected_revision},
                )
                if cursor.rowcount != 1:
                    raise RuntimeWorkConcurrencyError("Runtime work compare-and-swap failed")
                return persisted
        except sqlite3.Error as exc:
            raise RuntimeWorkStoreIntegrityError("Runtime work update failed") from exc

    def complete(
        self, work_id: str, *, observed_at: datetime, expected_revision: int | None = None
    ) -> RuntimeWorkRecord:
        observed = canonical_utc(observed_at, field="observed_at")
        current = self.get(work_id)
        if current is None:
            raise RuntimeWorkNotFoundError(work_id)
        if expected_revision is not None and current.revision != expected_revision:
            raise RuntimeWorkConcurrencyError("Runtime work revision changed")
        if current.status is RuntimeWorkStatus.COMPLETED:
            return current
        if current.is_terminal:
            raise ContradictoryRuntimeWorkError("terminal Runtime work cannot be completed again")
        if observed < current.updated_at:
            raise ValueError("completion observation cannot precede the current work update")
        completed = replace(
            current,
            status=RuntimeWorkStatus.COMPLETED,
            next_action="NONE",
            next_retry_at=None,
            updated_at=observed,
            observed_at=observed,
        )
        return self.update(completed, expected_revision=current.revision)

    def _initialize_or_validate_schema(self) -> None:
        tables = self._table_names()
        if not tables:
            self._connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE runtime_work_store_metadata (
                    singleton INTEGER PRIMARY KEY NOT NULL CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL
                );
                INSERT INTO runtime_work_store_metadata(singleton, schema_version) VALUES (1, 1);
                CREATE TABLE runtime_work_records (
                    work_id TEXT PRIMARY KEY NOT NULL,
                    record_version INTEGER NOT NULL,
                    work_kind TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    incident_id TEXT,
                    stage TEXT NOT NULL,
                    next_action TEXT NOT NULL,
                    operation_id TEXT,
                    workflow_operation_id TEXT,
                    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
                    retry_limit INTEGER NOT NULL CHECK (retry_limit >= 1),
                    next_retry_at TEXT,
                    last_attempt_at TEXT,
                    source_domain TEXT,
                    source_error_code TEXT,
                    source_retry_disposition TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    CHECK (
                        (source_domain IS NULL AND source_error_code IS NULL AND source_retry_disposition IS NULL)
                        OR
                        (source_domain IS NOT NULL AND source_error_code IS NOT NULL AND source_retry_disposition IS NOT NULL)
                    )
                );
                COMMIT;
                """
            )
        elif tables != _TABLES:
            raise RuntimeWorkStoreIntegrityError(
                "database is not an independent Runtime Work Store"
            )
        self._validate_schema()

    def _validate_schema(self) -> None:
        self._require_integrity()
        if self._column_shape("runtime_work_store_metadata") != _METADATA_COLUMNS:
            raise RuntimeWorkStoreIntegrityError("malformed Runtime Work Store metadata schema")
        if self._column_shape("runtime_work_records") != _WORK_COLUMNS:
            raise RuntimeWorkStoreIntegrityError("malformed Runtime Work Store record schema")
        rows = self._connection.execute(
            "SELECT singleton, schema_version FROM runtime_work_store_metadata"
        ).fetchall()
        if len(rows) != 1 or rows[0]["singleton"] != 1:
            raise RuntimeWorkStoreIntegrityError("malformed Runtime Work Store metadata")
        if rows[0]["schema_version"] != _SCHEMA_VERSION:
            raise UnsupportedRuntimeWorkStoreVersion(
                f"unsupported Runtime Work Store schema version: {rows[0]['schema_version']}"
            )

    def _require_integrity(self) -> None:
        rows = self._connection.execute("PRAGMA quick_check").fetchall()
        if len(rows) != 1 or rows[0][0] != "ok":
            raise RuntimeWorkStoreIntegrityError("SQLite integrity check failed")

    def _table_names(self) -> frozenset[str]:
        rows = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def _column_shape(self, table: str) -> tuple[tuple[str, str, int, int], ...]:
        rows = self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        return tuple((row[1], row[2].upper(), row[3], row[5]) for row in rows)

    def _select_row(self, work_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT rowid, * FROM runtime_work_records WHERE work_id = ?", (work_id,)
        ).fetchone()

    @staticmethod
    def _decode_or_raise(row: sqlite3.Row) -> RuntimeWorkRecord:
        try:
            return _decode_record(row)
        except (TypeError, ValueError, KeyError) as exc:
            raise RuntimeWorkRecordCorruptError(_corruption(row, exc)) from exc

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    @contextmanager
    def _read_snapshot(self) -> Iterator[None]:
        self._connection.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeWorkStoreClosedError("Runtime Work Store is closed")


def _encode_record(record: RuntimeWorkRecord) -> dict[str, object]:
    return {
        "work_id": record.work_id,
        "record_version": 1,
        "work_kind": record.work_kind.value,
        "event_id": record.event_id,
        "incident_id": record.incident_id,
        "stage": record.stage,
        "next_action": record.next_action,
        "operation_id": record.operation_id,
        "workflow_operation_id": record.workflow_operation_id,
        "attempt_count": record.attempt_count,
        "retry_limit": record.retry_limit,
        "next_retry_at": _optional_time(record.next_retry_at),
        "last_attempt_at": _optional_time(record.last_attempt_at),
        "source_domain": record.source_domain,
        "source_error_code": record.source_error_code,
        "source_retry_disposition": record.source_retry_disposition,
        "status": record.status.value,
        "created_at": format_utc(record.created_at, field="created_at"),
        "updated_at": format_utc(record.updated_at, field="updated_at"),
        "observed_at": format_utc(record.observed_at, field="observed_at"),
        "revision": record.revision,
    }


def _decode_record(row: sqlite3.Row) -> RuntimeWorkRecord:
    if row["record_version"] != 1:
        raise ValueError(f"unsupported record version: {row['record_version']}")
    return RuntimeWorkRecord(
        work_id=row["work_id"],
        work_kind=RuntimeWorkKind(row["work_kind"]),
        event_id=row["event_id"],
        incident_id=row["incident_id"],
        stage=row["stage"],
        next_action=row["next_action"],
        operation_id=row["operation_id"],
        workflow_operation_id=row["workflow_operation_id"],
        attempt_count=row["attempt_count"],
        retry_limit=row["retry_limit"],
        next_retry_at=_parse_optional_time(row["next_retry_at"], "next_retry_at"),
        last_attempt_at=_parse_optional_time(row["last_attempt_at"], "last_attempt_at"),
        source_domain=row["source_domain"],
        source_error_code=row["source_error_code"],
        source_retry_disposition=row["source_retry_disposition"],
        status=RuntimeWorkStatus(row["status"]),
        created_at=parse_utc(row["created_at"], field="created_at"),
        updated_at=parse_utc(row["updated_at"], field="updated_at"),
        observed_at=parse_utc(row["observed_at"], field="observed_at"),
        revision=row["revision"],
    )


def _optional_time(value: datetime | None) -> str | None:
    return None if value is None else format_utc(value)


def _parse_optional_time(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    return parse_utc(value, field=field)  # type: ignore[arg-type]


def _corruption(row: sqlite3.Row, exc: BaseException) -> RuntimeWorkCorruption:
    raw_key = row["work_id"]
    key = raw_key if isinstance(raw_key, str) and raw_key else f"<rowid:{row['rowid']}>"
    return RuntimeWorkCorruption(key, "MALFORMED_RUNTIME_WORK_RECORD", str(exc))


def _immutable_identity(record: RuntimeWorkRecord) -> tuple[object, ...]:
    return (
        record.work_id,
        record.work_kind,
        record.event_id,
        record.incident_id,
        record.operation_id,
        record.workflow_operation_id,
        record.retry_limit,
        record.created_at,
    )


def _without_revision(record: RuntimeWorkRecord) -> RuntimeWorkRecord:
    return replace(record, revision=0)


def _validate_transition(existing: RuntimeWorkRecord, candidate: RuntimeWorkRecord) -> None:
    if _immutable_identity(existing) != _immutable_identity(candidate):
        raise ContradictoryRuntimeWorkError("stable Runtime work identity fields are immutable")
    if existing.is_terminal and candidate != existing:
        raise ContradictoryRuntimeWorkError("terminal Runtime work cannot be reopened or changed")
    if candidate.attempt_count < existing.attempt_count:
        raise ContradictoryRuntimeWorkError("automatic retry attempt count cannot be reset")
    if candidate.updated_at < existing.updated_at:
        raise ContradictoryRuntimeWorkError("updated_at cannot move backwards")
    if candidate.observed_at < existing.observed_at:
        raise ContradictoryRuntimeWorkError("observed_at cannot move backwards")


def _validate_identifier(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty, trimmed string")
