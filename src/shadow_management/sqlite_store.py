"""Independent SQLite durability adapter for SPEC-010 Shadow records.

This module owns only local storage mechanics.  It deliberately exposes no
public mutation API: Phase 3 will compose validation, ownership evidence, and
the private transaction primitives into the authoritative mutation boundary.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from math import isfinite
from pathlib import Path

from alert_correlation.contracts import (
    AnchorStrength,
    AnchorTransition,
    CorrelationFamily,
    DecisionReasonCode,
    DecisionType,
    NormalizedFingerprint,
)
from alert_correlation.state.contracts import CorrelationMutationIntent, TerminalOutcome

from .contracts import (
    ShadowDomainError,
    ShadowDomainErrorCode,
    ShadowMutationResult,
    ShadowOperationReceipt,
    ShadowReason,
    ShadowReceiptSemanticIdentity,
    ShadowRecord,
    ShadowReviewStatus,
)


_SCHEMA_VERSION = "1"
_METADATA_KEY = "shadow_store_schema_version"


class ShadowStoreIntegrityError(ShadowDomainError):
    """Persisted Shadow state cannot safely be interpreted as authoritative."""

    def __init__(self, message: str, code: ShadowDomainErrorCode) -> None:
        if code not in {
            ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD,
            ShadowDomainErrorCode.UNSUPPORTED_SHADOW_STATE_VERSION,
            ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE,
        }:
            raise TypeError("integrity errors require a Shadow integrity error code")
        super().__init__(code, message)


class SqliteShadowStore:
    """Durable local Shadow state, intentionally without a public write API."""

    def __init__(
        self, database_path: str | Path = "shadow_store.db", *, busy_timeout_seconds: float = 5.0
    ) -> None:
        if isinstance(busy_timeout_seconds, bool) or not isinstance(busy_timeout_seconds, (int, float)):
            raise TypeError("busy_timeout_seconds must be a finite non-negative number")
        busy_timeout_seconds = float(busy_timeout_seconds)
        if not isfinite(busy_timeout_seconds) or busy_timeout_seconds < 0:
            raise ValueError("busy_timeout_seconds must be a finite non-negative number")
        self._path = str(database_path)
        try:
            self._connection = sqlite3.connect(
                self._path, isolation_level=None, timeout=busy_timeout_seconds
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_seconds * 1000)}")
            self._initialize_schema()
        except sqlite3.Error as exc:
            # Initialization has not returned an authority to a caller.  A
            # SQLite busy/locked failure here is therefore safe to retry;
            # unreadable and unclassifiable stores still map to integrity.
            raise _sqlite_domain_error(
                exc, "Shadow Store cannot initialize authoritative local state"
            ) from exc

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SqliteShadowStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def readiness_check(self) -> None:
        """Validate all local authority; corrupt records must not be skipped."""
        try:
            with self._read_snapshot():
                for row in self._connection.execute("SELECT shadow_id FROM shadow_records"):
                    self._read_shadow(row[0])
                    if self._get_operation_receipt_for_shadow_locked(row[0]) is None:
                        raise ShadowStoreIntegrityError(
                            "Shadow record has no matching operation receipt",
                            ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD,
                        )
                for row in self._connection.execute("SELECT operation_id FROM operation_receipts"):
                    self._read_receipt(row[0])
        except ShadowStoreIntegrityError:
            raise
        except sqlite3.Error as exc:
            raise ShadowStoreIntegrityError(
                "Shadow Store cannot enumerate authoritative local state",
                ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE,
            ) from exc

    def get_shadow(self, shadow_id: str) -> ShadowRecord | None:
        _reference(shadow_id, "shadow_id")
        try:
            with self._read_snapshot():
                record = self._read_shadow(shadow_id)
                if record is not None:
                    self._require_shadow_receipt(record.shadow_id)
                return record
        except ShadowStoreIntegrityError:
            raise
        except sqlite3.Error as exc:
            raise ShadowStoreIntegrityError(
                "Shadow Store cannot read Shadow record",
                ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE,
            ) from exc

    def get_shadow_by_event_id(self, event_id: str) -> ShadowRecord | None:
        _reference(event_id, "event_id")
        try:
            with self._read_snapshot():
                record = self._get_shadow_by_event_id_locked(event_id)
                if record is not None:
                    self._require_shadow_receipt(record.shadow_id)
                return record
        except ShadowStoreIntegrityError:
            raise
        except sqlite3.Error as exc:
            raise ShadowStoreIntegrityError(
                "Shadow Store cannot read Event ownership",
                ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE,
            ) from exc

    def shadow_exists(self, shadow_id: str) -> bool:
        return self.get_shadow(shadow_id) is not None

    def get_operation_result(self, operation_id: str) -> ShadowMutationResult | None:
        _reference(operation_id, "operation_id")
        try:
            with self._read_snapshot():
                receipt = self._get_operation_receipt_locked(operation_id)
                return None if receipt is None else receipt.result
        except ShadowStoreIntegrityError:
            raise
        except sqlite3.Error as exc:
            raise ShadowStoreIntegrityError(
                "Shadow Store cannot read operation receipt",
                ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE,
            ) from exc

    def enumerate_shadows(
        self,
        *,
        reason: ShadowReason | None = None,
        review_status: ShadowReviewStatus | None = None,
    ) -> tuple[ShadowRecord, ...]:
        """Return a deterministic local snapshot; no Event/Incident scan occurs."""
        if reason is not None and not isinstance(reason, ShadowReason):
            raise TypeError("reason must be a ShadowReason or None")
        if review_status is not None and not isinstance(review_status, ShadowReviewStatus):
            raise TypeError("review_status must be a ShadowReviewStatus or None")
        clauses: list[str] = []
        values: list[str] = []
        if reason is not None:
            clauses.append("reason = ?")
            values.append(reason.value)
        if review_status is not None:
            clauses.append("review_status = ?")
            values.append(review_status.value)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        try:
            with self._read_snapshot():
                rows = self._connection.execute(
                    "SELECT shadow_id FROM shadow_records"
                    + where
                    + " ORDER BY entered_shadow_at ASC, shadow_id ASC",
                    values,
                ).fetchall()
                records: list[ShadowRecord] = []
                for row in rows:
                    record = self._read_shadow(row[0])
                    if record is None:  # pragma: no cover - same snapshot prevents this.
                        raise ShadowStoreIntegrityError(
                            "Shadow record disappeared during its read snapshot",
                            ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE,
                        )
                    self._require_shadow_receipt(record.shadow_id)
                    records.append(record)
                return tuple(records)
        except ShadowStoreIntegrityError:
            raise
        except sqlite3.Error as exc:
            raise _sqlite_domain_error(exc, "Shadow Store cannot enumerate Shadows") from exc

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        """Private Phase-3 scaffold for state checks and local writes together."""
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise _sqlite_domain_error(exc, "Shadow Store cannot begin local write transaction") from exc
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
                # A failed COMMIT can leave its durable outcome unknown.  It is
                # therefore not one of the explicitly safe-to-retry SQLite
                # busy/locked paths (unlike failure before BEGIN IMMEDIATE).
                raise ShadowStoreIntegrityError(
                    "Shadow Store cannot confirm local write transaction commit",
                    ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE,
                ) from exc

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

    def _initialize_schema(self) -> None:
        existing_tables = {
            row[0]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        shadow_tables = {
            "shadow_store_metadata",
            "shadow_records",
            "operation_receipts",
        }
        if (
            existing_tables & shadow_tables
            and "shadow_store_metadata" not in existing_tables
        ):
            raise ShadowStoreIntegrityError(
                "existing Shadow tables have no version authority",
                ShadowDomainErrorCode.UNSUPPORTED_SHADOW_STATE_VERSION,
            )
        if any(table.startswith("correlation_state_") for table in existing_tables):
            raise ShadowStoreIntegrityError(
                "Shadow Store must use an independent physical database",
                ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE,
            )
        with self._write_transaction():
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS shadow_store_metadata (
                    metadata_key TEXT PRIMARY KEY NOT NULL,
                    metadata_value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_records (
                    shadow_id TEXT PRIMARY KEY NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    entered_shadow_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    policy_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operation_receipts (
                    operation_id TEXT PRIMARY KEY NOT NULL,
                    semantic_identity TEXT NOT NULL,
                    shadow_id TEXT NOT NULL,
                    entered_shadow_at TEXT NOT NULL,
                    FOREIGN KEY(shadow_id) REFERENCES shadow_records(shadow_id)
                );
                """
            )
            row = self._connection.execute(
                "SELECT metadata_value FROM shadow_store_metadata WHERE metadata_key = ?",
                (_METADATA_KEY,),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO shadow_store_metadata(metadata_key, metadata_value) VALUES (?, ?)",
                    (_METADATA_KEY, _SCHEMA_VERSION),
                )
            elif row[0] != _SCHEMA_VERSION:
                raise ShadowStoreIntegrityError(
                    "unsupported Shadow Store schema semantics",
                    ShadowDomainErrorCode.UNSUPPORTED_SHADOW_STATE_VERSION,
                )
            self._validate_schema_shape()

    def _validate_schema_shape(self) -> None:
        expected_columns = {
            "shadow_store_metadata": {"metadata_key", "metadata_value"},
            "shadow_records": {
                "shadow_id", "event_id", "entered_shadow_at", "reason",
                "review_status", "policy_id", "policy_version",
            },
            "operation_receipts": {
                "operation_id", "semantic_identity", "shadow_id", "entered_shadow_at",
            },
        }
        for table, expected in expected_columns.items():
            actual = {
                row[1] for row in self._connection.execute(f"PRAGMA table_info({table})")
            }
            if actual != expected:
                raise ShadowStoreIntegrityError(
                    f"Shadow Store table {table} has unsupported schema shape",
                    ShadowDomainErrorCode.UNSUPPORTED_SHADOW_STATE_VERSION,
                )
        for table, column in (
            ("shadow_records", "shadow_id"),
            ("shadow_records", "event_id"),
            ("operation_receipts", "operation_id"),
        ):
            if not _has_unique_column(self._connection, table, column):
                raise ShadowStoreIntegrityError(
                    f"Shadow Store table {table} lacks unique {column} authority",
                    ShadowDomainErrorCode.UNSUPPORTED_SHADOW_STATE_VERSION,
                )

    def _read_shadow(self, shadow_id: str) -> ShadowRecord | None:
        row = self._connection.execute(
            """SELECT shadow_id, event_id, entered_shadow_at, reason, review_status,
                      policy_id, policy_version
                 FROM shadow_records WHERE shadow_id = ?""",
            (shadow_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return ShadowRecord(
                row[0], row[1], _parse_timestamp(row[2]), ShadowReason(row[3]),
                ShadowReviewStatus(row[4]), row[5], row[6],
            )
        except (TypeError, ValueError, IndexError, ShadowDomainError) as exc:
            raise ShadowStoreIntegrityError(
                "malformed persisted Shadow record",
                ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD,
            ) from exc

    def _get_shadow_by_event_id_locked(self, event_id: str) -> ShadowRecord | None:
        row = self._connection.execute(
            "SELECT shadow_id FROM shadow_records WHERE event_id = ?", (event_id,)
        ).fetchone()
        return None if row is None else self._read_shadow(row[0])

    def _read_receipt(self, operation_id: str) -> ShadowOperationReceipt | None:
        row = self._connection.execute(
            """SELECT operation_id, semantic_identity, shadow_id, entered_shadow_at
                 FROM operation_receipts WHERE operation_id = ?""",
            (operation_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            identity = _decode_receipt_identity(json.loads(row[1]))
            result = ShadowMutationResult(row[0], row[2], _parse_timestamp(row[3]))
            if identity.intent.operation_id != row[0]:
                raise ValueError("receipt operation_id does not match semantic identity")
            shadow = self._read_shadow(row[2])
            if shadow is None:
                raise ValueError("receipt references missing Shadow record")
            if identity.event_id != shadow.event_id:
                raise ValueError("receipt event ownership contradicts Shadow record")
            if (
                identity.intent.policy_id != shadow.policy_id
                or identity.intent.policy_version != shadow.policy_version
            ):
                raise ValueError("receipt policy trace contradicts Shadow record")
            if identity.intent.reason_code.value != shadow.reason.value:
                raise ValueError("receipt reason contradicts Shadow record")
            if shadow.entered_shadow_at != result.entered_shadow_at:
                raise ValueError("receipt result timestamp contradicts Shadow record")
            receipt_count = self._connection.execute(
                "SELECT COUNT(*) FROM operation_receipts WHERE shadow_id = ?", (shadow.shadow_id,)
            ).fetchone()[0]
            if receipt_count != 1:
                raise ValueError("Shadow record must have exactly one operation receipt")
            return ShadowOperationReceipt(identity, result)
        except ShadowStoreIntegrityError:
            raise
        except (TypeError, ValueError, KeyError, json.JSONDecodeError, ShadowDomainError) as exc:
            raise ShadowStoreIntegrityError(
                "malformed persisted operation receipt",
                ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD,
            ) from exc

    def _get_operation_receipt_locked(
        self, operation_id: str
    ) -> ShadowOperationReceipt | None:
        return self._read_receipt(operation_id)

    def _get_operation_receipt_for_shadow_locked(
        self, shadow_id: str
    ) -> ShadowOperationReceipt | None:
        rows = self._connection.execute(
            "SELECT operation_id FROM operation_receipts WHERE shadow_id = ?", (shadow_id,)
        ).fetchall()
        if len(rows) > 1:
            raise ShadowStoreIntegrityError(
                "multiple operation receipts reference one Shadow record",
                ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD,
            )
        return None if not rows else self._read_receipt(rows[0][0])

    def _require_shadow_receipt(self, shadow_id: str) -> ShadowOperationReceipt:
        receipt = self._get_operation_receipt_for_shadow_locked(shadow_id)
        if receipt is None:
            raise ShadowStoreIntegrityError(
                "Shadow record has no matching operation receipt",
                ShadowDomainErrorCode.MALFORMED_SHADOW_RECORD,
            )
        return receipt

    def _insert_shadow_record(self, record: ShadowRecord) -> None:
        """Private Phase-3 primitive; caller must already hold _write_transaction."""
        if not isinstance(record, ShadowRecord):
            raise TypeError("record must be a ShadowRecord")
        self._connection.execute(
            """INSERT INTO shadow_records(
                   shadow_id, event_id, entered_shadow_at, reason, review_status, policy_id, policy_version
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                record.shadow_id, record.event_id, _timestamp(record.entered_shadow_at),
                record.reason.value, record.review_status.value, record.policy_id, record.policy_version,
            ),
        )

    def _insert_operation_receipt(self, receipt: ShadowOperationReceipt) -> None:
        """Private Phase-3 primitive; caller must already hold _write_transaction."""
        if not isinstance(receipt, ShadowOperationReceipt):
            raise TypeError("receipt must be a ShadowOperationReceipt")
        self._connection.execute(
            """INSERT INTO operation_receipts(operation_id, semantic_identity, shadow_id, entered_shadow_at)
               VALUES (?, ?, ?, ?)""",
            (
                receipt.result.operation_id,
                _json(_encode_receipt_identity(receipt.semantic_identity)),
                receipt.result.shadow_id,
                _timestamp(receipt.result.entered_shadow_at),
            ),
        )


def _reference(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty string without surrounding whitespace")
    return value


def _has_unique_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    for index in connection.execute(f"PRAGMA index_list({table})"):
        if not index[2]:
            continue
        index_columns = [
            row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})")
        ]
        if index_columns == [column]:
            return True
    return False


def _sqlite_domain_error(exc: sqlite3.Error, context: str) -> ShadowDomainError:
    message = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and (
        "database is locked" in message or "database is busy" in message
    ):
        return ShadowDomainError(
            ShadowDomainErrorCode.TRANSIENT_SHADOW_STORE_FAILURE,
            f"{context}: SQLite busy/locked",
        )
    return ShadowStoreIntegrityError(
        context,
        ShadowDomainErrorCode.SHADOW_STORE_INTEGRITY_FAILURE,
    )


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    return datetime.fromisoformat(value)


def _json(value: dict[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _encode_fingerprint(value: NormalizedFingerprint | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"event_type": value.event_type, "identity": [list(item) for item in value.identity]}


def _decode_fingerprint(value: object) -> NormalizedFingerprint | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("identity"), list):
        raise ValueError("normalized_fingerprint must be an object or null")
    return NormalizedFingerprint(value["event_type"], tuple(tuple(item) for item in value["identity"]))


def _encode_receipt_identity(identity: ShadowReceiptSemanticIdentity) -> dict[str, object]:
    intent = identity.intent
    return {
        "event_id": identity.event_id,
        "event_type": identity.event_type,
        "intent": {
            "operation_id": intent.operation_id,
            "event_id": intent.event_id,
            "intended_terminal_outcome": intent.intended_terminal_outcome.value,
            "decision_type": intent.decision_type.value,
            "policy_id": intent.policy_id,
            "policy_version": intent.policy_version,
            "correlation_family": intent.correlation_family.value,
            "reason_code": intent.reason_code.value,
            "target_incident_id": intent.target_incident_id,
            "normalized_fingerprint": _encode_fingerprint(intent.normalized_fingerprint),
            "anchor_strength": intent.anchor_strength.value if intent.anchor_strength else None,
            "anchor_transition": intent.anchor_transition.value,
            "created_at": _timestamp(intent.created_at),
        },
    }


def _decode_receipt_identity(data: object) -> ShadowReceiptSemanticIdentity:
    if not isinstance(data, dict) or not isinstance(data.get("intent"), dict):
        raise ValueError("receipt semantic_identity must contain an intent object")
    intent = data["intent"]
    strength = intent["anchor_strength"]
    return ShadowReceiptSemanticIdentity(
        CorrelationMutationIntent(
            intent["operation_id"], intent["event_id"],
            TerminalOutcome(intent["intended_terminal_outcome"]), DecisionType(intent["decision_type"]),
            intent["policy_id"], intent["policy_version"], CorrelationFamily(intent["correlation_family"]),
            DecisionReasonCode(intent["reason_code"]), intent["target_incident_id"],
            _decode_fingerprint(intent["normalized_fingerprint"]),
            AnchorStrength(strength) if strength is not None else None,
            AnchorTransition(intent["anchor_transition"]), _parse_timestamp(intent["created_at"]),
        ),
        data["event_id"], data["event_type"],
    )
