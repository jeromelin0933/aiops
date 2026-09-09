"""Independent SQLite persistence adapter for SPEC-008 Incident authority.

The public surface is read-only.  Package code may use the private
``_transaction`` unit of work so the Phase 3 manager can perform all
state-dependent validation and writes under one ``BEGIN IMMEDIATE`` boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
from typing import Iterator, Mapping

from src.alert_correlation import (
    AnchorStrength,
    AnchorTransition,
    CorrelationFamily,
    DecisionReasonCode,
    DecisionType,
    IncidentCorrelationView,
    NormalizedFingerprint,
)
from src.alert_correlation.state import TerminalOutcome

from .contracts import (
    IncidentAuditAction,
    IncidentAuditEffect,
    IncidentAuditEntry,
    IncidentCorrelationContext,
    IncidentDomainError,
    IncidentErrorCode,
    IncidentMutationCompletion,
    IncidentOperationReceipt,
    IncidentOperationResult,
    IncidentRecord,
    IncidentSeverity,
    IncidentStatus,
    OperationReceiptSemanticIdentity,
)


DEFAULT_DATABASE_PATH = "incident_store.db"
SCHEMA_VERSION = 1
STATE_VERSION = 1
_BUSY_TIMEOUT_MS = 5000

_EXPECTED_TABLES = frozenset(
    {
        "incident_store_metadata",
        "incidents",
        "incident_events",
        "incident_operation_receipts",
        "incident_audit",
    }
)

# Exact implemented SPEC-007 authority tables.  These names are inspected only
# through sqlite_master to reject physical co-location; their contents are
# never read, mutated, joined, or included in a SPEC-008 transaction model.
_SPEC_007_AUTHORITY_TABLES = frozenset(
    {
        "correlation_state_processed",
        "correlation_state_intents",
        "correlation_state_pending",
        "correlation_state_blocked",
        "correlation_state_claims",
        "correlation_state_claim_generations",
    }
)

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE incident_store_metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE incidents (
        incident_id TEXT PRIMARY KEY,
        state_version INTEGER NOT NULL,
        anchor_event_id TEXT,
        status TEXT NOT NULL,
        severity TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_correlated_at TEXT NOT NULL,
        closed_at TEXT,
        assignee TEXT,
        reviewer TEXT,
        correlation_context TEXT NOT NULL,
        rca_status TEXT NOT NULL,
        rca_ref TEXT,
        external_refs TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE incident_events (
        event_id TEXT PRIMARY KEY,
        incident_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        UNIQUE (incident_id, ordinal),
        UNIQUE (event_id, incident_id),
        FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE incident_operation_receipts (
        operation_id TEXT PRIMARY KEY,
        state_version INTEGER NOT NULL,
        event_id TEXT NOT NULL UNIQUE,
        mutation_kind TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        completion_result TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        immutable_mutation_identity TEXT NOT NULL,
        UNIQUE (operation_id, event_id, incident_id),
        FOREIGN KEY (event_id, incident_id)
            REFERENCES incident_events(event_id, incident_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE incident_audit (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        state_version INTEGER NOT NULL,
        operation_id TEXT NOT NULL UNIQUE,
        event_id TEXT NOT NULL,
        incident_id TEXT NOT NULL,
        policy_id TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        action TEXT NOT NULL,
        effects TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        FOREIGN KEY (operation_id, event_id, incident_id)
            REFERENCES incident_operation_receipts(operation_id, event_id, incident_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
            DEFERRABLE INITIALLY DEFERRED
    )
    """,
    "CREATE INDEX idx_incident_events_incident_order ON incident_events(incident_id, ordinal)",
    "CREATE INDEX idx_incident_audit_incident_order ON incident_audit(incident_id, audit_id)",
)


def _domain_error(code: IncidentErrorCode, message: str) -> IncidentDomainError:
    return IncidentDomainError(code, message)


def _translate_sqlite_error(exc: sqlite3.Error) -> IncidentDomainError:
    if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
        return _domain_error(
            IncidentErrorCode.TRANSIENT_INCIDENT_STORE_FAILURE,
            "Incident Store is temporarily locked",
        )
    return _domain_error(
        IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
        f"Incident Store operation failed: {exc}",
    )


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _load_json(value: object, field_name: str) -> object:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be serialized JSON text")
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} contains malformed JSON") from exc


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be timestamp text")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _fingerprint(value: NormalizedFingerprint | None) -> object:
    if value is None:
        return None
    return {"event_type": value.event_type, "identity": [list(item) for item in value.identity]}


def _parse_fingerprint(value: object) -> NormalizedFingerprint | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"event_type", "identity"}:
        raise ValueError("normalized_fingerprint has an invalid shape")
    identity = value["identity"]
    if not isinstance(identity, list):
        raise ValueError("normalized_fingerprint.identity must be a list")
    return NormalizedFingerprint(
        event_type=value["event_type"],
        identity=tuple(tuple(item) if isinstance(item, list) else item for item in identity),
    )


def _context(value: IncidentCorrelationContext) -> str:
    return _json(
        {
            "correlation_family": value.correlation_family.value,
            "anchor_strength": value.anchor_strength.value,
            "anchor_event_id": value.anchor_event_id,
            "anchor_event_type": value.anchor_event_type,
            "normalized_fingerprint": _fingerprint(value.normalized_fingerprint),
            "anchor_policy_id": value.anchor_policy_id,
            "anchor_policy_version": value.anchor_policy_version,
            "promoted_from_weak": value.promoted_from_weak,
        }
    )


def _parse_context(value: object) -> IncidentCorrelationContext:
    data = _load_json(value, "correlation_context")
    if not isinstance(data, dict) or set(data) != {
        "correlation_family",
        "anchor_strength",
        "anchor_event_id",
        "anchor_event_type",
        "normalized_fingerprint",
        "anchor_policy_id",
        "anchor_policy_version",
        "promoted_from_weak",
    }:
        raise ValueError("correlation_context has an invalid shape")
    return IncidentCorrelationContext(
        correlation_family=CorrelationFamily(data["correlation_family"]),
        anchor_strength=AnchorStrength(data["anchor_strength"]),
        anchor_event_id=data["anchor_event_id"],
        anchor_event_type=data["anchor_event_type"],
        normalized_fingerprint=_parse_fingerprint(data["normalized_fingerprint"]),
        anchor_policy_id=data["anchor_policy_id"],
        anchor_policy_version=data["anchor_policy_version"],
        promoted_from_weak=data["promoted_from_weak"],
    )


def _identity(value: OperationReceiptSemanticIdentity) -> str:
    return _json(
        {
            "event_id": value.event_id,
            "intended_terminal_outcome": value.intended_terminal_outcome.value,
            "decision_type": value.decision_type.value,
            "policy_id": value.policy_id,
            "policy_version": value.policy_version,
            "correlation_family": value.correlation_family.value,
            "reason_code": value.reason_code.value,
            "target_incident_id": value.target_incident_id,
            "normalized_fingerprint": _fingerprint(value.normalized_fingerprint),
            "anchor_strength": value.anchor_strength.value if value.anchor_strength else None,
            "anchor_transition": value.anchor_transition.value,
            "intent_created_at": _timestamp(value.intent_created_at),
            "event_type": value.event_type,
            "event_detected_at": _timestamp(value.event_detected_at),
            "event_severity": value.event_severity.name,
        }
    )


def _parse_identity(value: object) -> OperationReceiptSemanticIdentity:
    data = _load_json(value, "immutable_mutation_identity")
    expected = {
        "event_id",
        "intended_terminal_outcome",
        "decision_type",
        "policy_id",
        "policy_version",
        "correlation_family",
        "reason_code",
        "target_incident_id",
        "normalized_fingerprint",
        "anchor_strength",
        "anchor_transition",
        "intent_created_at",
        "event_type",
        "event_detected_at",
        "event_severity",
    }
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError("immutable_mutation_identity has an invalid shape")
    strength = data["anchor_strength"]
    return OperationReceiptSemanticIdentity(
        event_id=data["event_id"],
        intended_terminal_outcome=TerminalOutcome(data["intended_terminal_outcome"]),
        decision_type=DecisionType(data["decision_type"]),
        policy_id=data["policy_id"],
        policy_version=data["policy_version"],
        correlation_family=CorrelationFamily(data["correlation_family"]),
        reason_code=DecisionReasonCode(data["reason_code"]),
        target_incident_id=data["target_incident_id"],
        normalized_fingerprint=_parse_fingerprint(data["normalized_fingerprint"]),
        anchor_strength=AnchorStrength(strength) if strength is not None else None,
        anchor_transition=AnchorTransition(data["anchor_transition"]),
        intent_created_at=_parse_timestamp(data["intent_created_at"], "intent_created_at"),
        event_type=data["event_type"],
        event_detected_at=_parse_timestamp(data["event_detected_at"], "event_detected_at"),
        event_severity=IncidentSeverity[data["event_severity"]],
    )


class _IncidentStoreTransaction:
    """Package-private transaction primitives; not a public write adapter."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def _get_incident(self, incident_id: str) -> IncidentRecord | None:
        return _read_incident(self._connection, incident_id)

    def _get_operation_receipt(self, operation_id: str) -> IncidentOperationReceipt | None:
        row = self._connection.execute(
            "SELECT * FROM incident_operation_receipts WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return None if row is None else _decode_receipt(row)

    def _get_event_owner(self, event_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT incident_id FROM incident_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return None if row is None else row["incident_id"]

    def _insert_incident_state(self, record: IncidentRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO incidents(
                incident_id, state_version, anchor_event_id, status, severity,
                created_at, updated_at, last_correlated_at, closed_at, assignee,
                reviewer, correlation_context, rca_status, rca_ref, external_refs
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.incident_id,
                STATE_VERSION,
                record.anchor_event_id,
                record.status.value,
                record.severity.name,
                _timestamp(record.created_at),
                _timestamp(record.updated_at),
                _timestamp(record.last_correlated_at),
                _timestamp(record.closed_at) if record.closed_at else None,
                record.assignee,
                record.reviewer,
                _context(record.correlation_context),
                record.rca_status,
                record.rca_ref,
                _json(list(record.external_refs)),
            ),
        )

    def _replace_incident_state(self, record: IncidentRecord) -> None:
        cursor = self._connection.execute(
            """
            UPDATE incidents SET
                state_version = ?, anchor_event_id = ?, status = ?, severity = ?,
                created_at = ?, updated_at = ?, last_correlated_at = ?, closed_at = ?,
                assignee = ?, reviewer = ?, correlation_context = ?, rca_status = ?,
                rca_ref = ?, external_refs = ?
            WHERE incident_id = ?
            """,
            (
                STATE_VERSION,
                record.anchor_event_id,
                record.status.value,
                record.severity.name,
                _timestamp(record.created_at),
                _timestamp(record.updated_at),
                _timestamp(record.last_correlated_at),
                _timestamp(record.closed_at) if record.closed_at else None,
                record.assignee,
                record.reviewer,
                _context(record.correlation_context),
                record.rca_status,
                record.rca_ref,
                _json(list(record.external_refs)),
                record.incident_id,
            ),
        )
        if cursor.rowcount != 1:
            raise _domain_error(IncidentErrorCode.INCIDENT_NOT_FOUND, "Incident does not exist")

    def _insert_event_reference(self, incident_id: str, event_id: str, ordinal: int) -> None:
        self._connection.execute(
            "INSERT INTO incident_events(incident_id, event_id, ordinal) VALUES (?, ?, ?)",
            (incident_id, event_id, ordinal),
        )

    def _insert_operation_receipt(self, receipt: IncidentOperationReceipt) -> None:
        result = receipt.result
        self._connection.execute(
            """
            INSERT INTO incident_operation_receipts(
                operation_id, state_version, event_id, mutation_kind, incident_id,
                completion_result, completed_at, immutable_mutation_identity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.operation_id,
                STATE_VERSION,
                result.event_id,
                result.mutation_kind.value,
                result.incident_id,
                result.completion_result.value,
                _timestamp(result.completed_at),
                _identity(receipt.immutable_mutation_identity),
            ),
        )

    def _insert_audit_entry(self, entry: IncidentAuditEntry) -> None:
        self._connection.execute(
            """
            INSERT INTO incident_audit(
                state_version, operation_id, event_id, incident_id, policy_id,
                policy_version, reason_code, action, effects, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                STATE_VERSION,
                entry.operation_id,
                entry.event_id,
                entry.incident_id,
                entry.policy_id,
                entry.policy_version,
                entry.reason_code.value,
                entry.action.value,
                _json([effect.value for effect in entry.effects]),
                _timestamp(entry.occurred_at),
            ),
        )


class SqliteIncidentStore:
    """Read API plus a package-private atomic unit-of-work boundary."""

    def __init__(self, database_path: str = DEFAULT_DATABASE_PATH) -> None:
        if not isinstance(database_path, str) or not database_path:
            raise ValueError("database_path must be a non-empty string")
        self.database_path = database_path
        self._closed = False
        connection: sqlite3.Connection | None = None
        try:
            connection = self._open_connection()
            self._initialize_or_validate_schema(connection)
        except IncidentDomainError:
            raise
        except sqlite3.Error as exc:
            raise _translate_sqlite_error(exc) from exc
        finally:
            if connection is not None:
                connection.close()

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "SqliteIncidentStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        return connection

    def _operation_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("Incident Store is closed")
        return self._open_connection()

    def _initialize_or_validate_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            tables = self._table_names(connection)
            self._validate_physical_separation(tables)
            present = tables & _EXPECTED_TABLES
            if not present:
                if tables:
                    raise _domain_error(
                        IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
                        "refusing to initialize Incident authority in a non-empty database",
                    )
                for statement in _SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO incident_store_metadata(singleton, schema_version) VALUES (1, ?)",
                    (SCHEMA_VERSION,),
                )
            elif present != _EXPECTED_TABLES:
                raise _domain_error(
                    IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
                    "Incident Store schema is incomplete",
                )
            self._validate_schema_version(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _table_names(connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {row["name"] for row in rows}

    @staticmethod
    def _validate_physical_separation(tables: set[str]) -> None:
        current_tables = tables
        if current_tables & _SPEC_007_AUTHORITY_TABLES:
            raise _domain_error(
                IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
                "SPEC-008 must not share the SPEC-007 physical database",
            )

    @staticmethod
    def _validate_schema_version(connection: sqlite3.Connection) -> None:
        try:
            rows = connection.execute(
                "SELECT singleton, schema_version FROM incident_store_metadata"
            ).fetchall()
        except sqlite3.Error as exc:
            raise _translate_sqlite_error(exc) from exc
        if len(rows) != 1 or rows[0]["singleton"] != 1:
            raise _domain_error(
                IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
                "Incident Store metadata must contain exactly one authority row",
            )
        if rows[0]["schema_version"] != SCHEMA_VERSION:
            raise _domain_error(
                IncidentErrorCode.UNSUPPORTED_INCIDENT_STATE_VERSION,
                "Incident Store schema version is unsupported",
            )

    @contextmanager
    def _transaction(self) -> Iterator[_IncidentStoreTransaction]:
        """Private BEGIN IMMEDIATE UoW for guarded package-level mutation."""
        connection: sqlite3.Connection | None = None
        try:
            connection = self._operation_connection()
            connection.execute("BEGIN IMMEDIATE")
            yield _IncidentStoreTransaction(connection)
            connection.commit()
        except IncidentDomainError:
            if connection is not None:
                connection.rollback()
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise _translate_sqlite_error(exc) from exc
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._operation_connection()
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except IncidentDomainError:
            if connection is not None:
                connection.rollback()
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise _translate_sqlite_error(exc) from exc
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def get_incident(self, incident_id: str) -> IncidentRecord | None:
        _validate_reference(incident_id, "incident_id")
        with self._read_transaction() as connection:
            return _read_incident(connection, incident_id)

    def incident_exists(self, incident_id: str) -> bool:
        return self.get_incident(incident_id) is not None

    def event_has_incident_owner(self, event_id: str) -> bool:
        """Return whether an Event has coherent authoritative Incident ownership."""
        _validate_reference(event_id, "event_id")
        with self._read_transaction() as connection:
            self._validate_read_authority(connection)
            # Absence is authoritative only when every Incident can still be
            # reconstructed from the same ownership snapshot.
            _read_all_incidents(connection)
            return _read_event_has_incident_owner(connection, event_id)

    def get_operation_result(self, operation_id: str) -> IncidentOperationResult | None:
        _validate_reference(operation_id, "operation_id")
        with self._read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM incident_operation_receipts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            return None if row is None else _decode_receipt(row).result

    def get_correlation_view(self, incident_id: str) -> IncidentCorrelationView | None:
        record = self.get_incident(incident_id)
        return None if record is None else _to_view(record)

    def list_correlation_views(self) -> tuple[IncidentCorrelationView, ...]:
        with self._read_transaction() as connection:
            records = _read_all_incidents(connection)
            return tuple(_to_view(record) for record in records)

    def validate_readiness(self) -> None:
        self.validate_integrity()

    def validate_integrity(self) -> None:
        with self._read_transaction() as connection:
            self._validate_read_authority(connection)
            _read_all_incidents(connection)
            receipt_rows = connection.execute(
                "SELECT * FROM incident_operation_receipts ORDER BY operation_id"
            ).fetchall()
            for row in receipt_rows:
                _decode_receipt(row)
            contradiction = connection.execute(
                """
                SELECT e.event_id
                FROM incident_events AS e
                LEFT JOIN incident_operation_receipts AS r
                  ON r.event_id = e.event_id AND r.incident_id = e.incident_id
                LEFT JOIN incident_audit AS a
                  ON a.operation_id = r.operation_id
                 AND a.event_id = r.event_id
                 AND a.incident_id = r.incident_id
                WHERE r.operation_id IS NULL OR a.audit_id IS NULL
                LIMIT 1
                """
            ).fetchone()
            if contradiction is not None:
                raise _domain_error(
                    IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
                    "Incident evidence, receipt, and audit authorities are incomplete",
                )

    def _validate_read_authority(self, connection: sqlite3.Connection) -> None:
        self._validate_physical_separation(self._table_names(connection))
        self._validate_schema_version(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise _domain_error(
                IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
                "SQLite integrity_check failed",
            )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise _domain_error(
                IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
                "Incident Store contains a foreign-key contradiction",
            )


def _validate_reference(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty reference without surrounding whitespace")


def _require_state_version(row: sqlite3.Row, record_kind: str) -> None:
    if row["state_version"] != STATE_VERSION:
        raise _domain_error(
            IncidentErrorCode.UNSUPPORTED_INCIDENT_STATE_VERSION,
            f"unsupported persisted {record_kind} state version",
        )


def _read_incident(connection: sqlite3.Connection, incident_id: str) -> IncidentRecord | None:
    row = connection.execute(
        "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        _require_state_version(row, "Incident")
        event_rows = connection.execute(
            "SELECT event_id, ordinal FROM incident_events WHERE incident_id = ? ORDER BY ordinal",
            (incident_id,),
        ).fetchall()
        if [event_row["ordinal"] for event_row in event_rows] != list(range(len(event_rows))):
            raise ValueError("Incident Event ordinals must be contiguous from zero")
        audit_rows = connection.execute(
            "SELECT * FROM incident_audit WHERE incident_id = ? ORDER BY audit_id",
            (incident_id,),
        ).fetchall()
        external_refs = _load_json(row["external_refs"], "external_refs")
        if not isinstance(external_refs, list):
            raise ValueError("external_refs must be a list")
        return IncidentRecord(
            incident_id=row["incident_id"],
            event_ids=tuple(event_row["event_id"] for event_row in event_rows),
            anchor_event_id=row["anchor_event_id"],
            status=IncidentStatus(row["status"]),
            severity=IncidentSeverity[row["severity"]],
            created_at=_parse_timestamp(row["created_at"], "created_at"),
            updated_at=_parse_timestamp(row["updated_at"], "updated_at"),
            last_correlated_at=_parse_timestamp(row["last_correlated_at"], "last_correlated_at"),
            closed_at=_parse_timestamp(row["closed_at"], "closed_at") if row["closed_at"] is not None else None,
            assignee=row["assignee"],
            reviewer=row["reviewer"],
            correlation_context=_parse_context(row["correlation_context"]),
            audit_trail=tuple(_decode_audit(audit_row) for audit_row in audit_rows),
            rca_status=row["rca_status"],
            rca_ref=row["rca_ref"],
            external_refs=tuple(external_refs),
        )
    except IncidentDomainError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _domain_error(
            IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
            f"malformed persisted Incident record: {exc}",
        ) from exc


def _read_event_has_incident_owner(
    connection: sqlite3.Connection, event_id: str
) -> bool:
    ownership = connection.execute(
        "SELECT incident_id FROM incident_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    receipt_rows = connection.execute(
        "SELECT * FROM incident_operation_receipts WHERE event_id = ?", (event_id,)
    ).fetchall()
    audit_rows = connection.execute(
        "SELECT * FROM incident_audit WHERE event_id = ?", (event_id,)
    ).fetchall()

    if ownership is None:
        if receipt_rows or audit_rows:
            raise _domain_error(
                IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
                "Event without ownership has contradictory receipt or audit evidence",
            )
        return False

    incident_id = ownership["incident_id"]
    incident = _read_incident(connection, incident_id)
    if incident is None or event_id not in incident.event_ids:
        raise _domain_error(
            IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
            "Event ownership does not resolve to a coherent Incident",
        )
    if len(receipt_rows) != 1 or len(audit_rows) != 1:
        raise _domain_error(
            IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
            "Event ownership requires exactly one receipt and business audit",
        )

    receipt = _decode_receipt(receipt_rows[0])
    audit = _decode_audit(audit_rows[0])
    if (
        receipt.result.event_id != event_id
        or receipt.result.incident_id != incident_id
        or audit.operation_id != receipt.result.operation_id
        or audit.event_id != event_id
        or audit.incident_id != incident_id
    ):
        raise _domain_error(
            IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
            "Event ownership, receipt, and audit evidence contradict",
        )
    return True


def _read_all_incidents(connection: sqlite3.Connection) -> tuple[IncidentRecord, ...]:
    rows = connection.execute("SELECT incident_id FROM incidents ORDER BY incident_id").fetchall()
    records: list[IncidentRecord] = []
    for row in rows:
        record = _read_incident(connection, row["incident_id"])
        if record is None:
            raise _domain_error(
                IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
                "Incident disappeared within a coherent read transaction",
            )
        records.append(record)
    return tuple(records)


def _decode_audit(row: sqlite3.Row) -> IncidentAuditEntry:
    try:
        _require_state_version(row, "audit")
        effects = _load_json(row["effects"], "effects")
        if not isinstance(effects, list):
            raise ValueError("effects must be a list")
        return IncidentAuditEntry(
            operation_id=row["operation_id"],
            event_id=row["event_id"],
            incident_id=row["incident_id"],
            policy_id=row["policy_id"],
            policy_version=row["policy_version"],
            reason_code=DecisionReasonCode(row["reason_code"]),
            action=IncidentAuditAction(row["action"]),
            effects=tuple(IncidentAuditEffect(effect) for effect in effects),
            occurred_at=_parse_timestamp(row["occurred_at"], "occurred_at"),
        )
    except IncidentDomainError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _domain_error(
            IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
            f"malformed persisted Incident audit: {exc}",
        ) from exc


def _decode_receipt(row: sqlite3.Row) -> IncidentOperationReceipt:
    try:
        _require_state_version(row, "operation receipt")
        result = IncidentOperationResult(
            operation_id=row["operation_id"],
            event_id=row["event_id"],
            mutation_kind=DecisionType(row["mutation_kind"]),
            incident_id=row["incident_id"],
            completion_result=IncidentMutationCompletion(row["completion_result"]),
            completed_at=_parse_timestamp(row["completed_at"], "completed_at"),
        )
        return IncidentOperationReceipt(
            result=result,
            immutable_mutation_identity=_parse_identity(row["immutable_mutation_identity"]),
        )
    except IncidentDomainError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _domain_error(
            IncidentErrorCode.MALFORMED_INCIDENT_RECORD,
            f"malformed persisted operation receipt: {exc}",
        ) from exc


def _to_view(record: IncidentRecord) -> IncidentCorrelationView:
    context = record.correlation_context
    return IncidentCorrelationView(
        incident_id=record.incident_id,
        status=record.status.value,
        last_correlated_at=record.last_correlated_at,
        correlation_family=context.correlation_family,
        anchor_strength=context.anchor_strength,
        normalized_fingerprint=context.normalized_fingerprint,
        anchor_event_type=context.anchor_event_type,
    )


__all__ = [
    "DEFAULT_DATABASE_PATH",
    "SCHEMA_VERSION",
    "STATE_VERSION",
    "SqliteIncidentStore",
]
