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
    AssignmentSelectionMode,
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
    IncidentTimelineEntry,
    IncidentTimelineSource,
    OperationReceiptSemanticIdentity,
    ResolutionSubmission,
    ReviewAttempt,
    SopFollowed,
    WorkflowAction,
    WorkflowAuditEffect,
    WorkflowAuditEntry,
    WorkflowCompletion,
    WorkflowDomainError,
    WorkflowErrorCode,
    WorkflowOperationReceipt,
    WorkflowOperationResult,
    WorkflowReceiptSemanticIdentity,
)


DEFAULT_DATABASE_PATH = "incident_store.db"
SCHEMA_VERSION = 3
STATE_VERSION = 1
_BUSY_TIMEOUT_MS = 5000

_V1_TABLES = frozenset(
    {
        "incident_store_metadata",
        "incidents",
        "incident_events",
        "incident_operation_receipts",
        "incident_audit",
    }
)

_WORKFLOW_TABLES = frozenset(
    {
        "incident_workflow_operation_receipts",
        "incident_assignment_state",
        "incident_resolution_submissions",
        "incident_review_attempts",
        "incident_workflow_audit",
    }
)
_EXPECTED_TABLES = _V1_TABLES | _WORKFLOW_TABLES

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
    """
    CREATE TABLE incident_workflow_operation_receipts (
        workflow_operation_id TEXT PRIMARY KEY,
        state_version INTEGER NOT NULL,
        incident_id TEXT NOT NULL,
        action TEXT NOT NULL,
        completion_result TEXT NOT NULL,
        resulting_status TEXT NOT NULL,
        result_reference TEXT,
        completed_at TEXT NOT NULL,
        immutable_workflow_identity TEXT NOT NULL,
        assignment_policy_id TEXT,
        assignment_policy_version TEXT,
        selected_assignee TEXT,
        bound_reviewer TEXT,
        selection_mode TEXT,
        CHECK ((assignment_policy_id IS NULL) = (assignment_policy_version IS NULL)),
        CHECK ((selected_assignee IS NULL) = (bound_reviewer IS NULL)),
        CHECK ((selected_assignee IS NULL) = (selection_mode IS NULL)),
        FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE incident_assignment_state (
        policy_id TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        state_version INTEGER NOT NULL,
        engineers TEXT NOT NULL,
        default_reviewer TEXT NOT NULL,
        next_cursor INTEGER NOT NULL CHECK (next_cursor >= 0),
        PRIMARY KEY (policy_id, policy_version)
    )
    """,
    """
    CREATE TABLE incident_resolution_submissions (
        resolution_submission_id TEXT PRIMARY KEY,
        state_version INTEGER NOT NULL,
        incident_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        actual_action TEXT NOT NULL,
        resolution_note TEXT NOT NULL,
        sop_followed TEXT NOT NULL,
        additional_note TEXT,
        deviation_reason TEXT,
        submitted_by TEXT NOT NULL,
        submitted_at TEXT NOT NULL,
        UNIQUE (incident_id, revision),
        FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE incident_review_attempts (
        review_attempt_id TEXT PRIMARY KEY,
        state_version INTEGER NOT NULL,
        incident_id TEXT NOT NULL,
        resolution_revision INTEGER NOT NULL CHECK (resolution_revision >= 1),
        reviewer TEXT NOT NULL,
        review_approved INTEGER NOT NULL CHECK (review_approved IN (0, 1)),
        review_note TEXT,
        recovery_verified INTEGER NOT NULL CHECK (recovery_verified IN (0, 1)),
        recovery_note TEXT,
        reviewed_at TEXT NOT NULL,
        FOREIGN KEY (incident_id, resolution_revision)
            REFERENCES incident_resolution_submissions(incident_id, revision)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE incident_workflow_audit (
        workflow_audit_id TEXT PRIMARY KEY,
        state_version INTEGER NOT NULL,
        workflow_operation_id TEXT NOT NULL UNIQUE,
        incident_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        old_status TEXT NOT NULL,
        new_status TEXT NOT NULL,
        effects TEXT NOT NULL,
        reference_id TEXT,
        assignment_policy_id TEXT,
        assignment_policy_version TEXT,
        CHECK ((assignment_policy_id IS NULL) = (assignment_policy_version IS NULL)),
        FOREIGN KEY (workflow_operation_id) REFERENCES incident_workflow_operation_receipts(workflow_operation_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    """,
    "CREATE INDEX idx_resolution_incident_revision ON incident_resolution_submissions(incident_id, revision DESC)",
    "CREATE INDEX idx_review_incident_time ON incident_review_attempts(incident_id, reviewed_at, review_attempt_id)",
    "CREATE INDEX idx_workflow_audit_incident_time ON incident_workflow_audit(incident_id, occurred_at, workflow_audit_id)",
)

_V2_TO_V3_STATEMENTS = (
    "ALTER TABLE incident_workflow_operation_receipts ADD COLUMN assignment_policy_id TEXT",
    "ALTER TABLE incident_workflow_operation_receipts ADD COLUMN assignment_policy_version TEXT",
    "ALTER TABLE incident_workflow_operation_receipts ADD COLUMN selected_assignee TEXT",
    "ALTER TABLE incident_workflow_operation_receipts ADD COLUMN bound_reviewer TEXT",
    "ALTER TABLE incident_workflow_operation_receipts ADD COLUMN selection_mode TEXT",
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


def _workflow_identity(value: WorkflowReceiptSemanticIdentity) -> str:
    resolution = value.resolution
    review = value.review
    return _json({
        "action": value.action.value, "incident_id": value.incident_id, "actor": value.actor,
        "target_assignee": value.target_assignee,
        "resolution": None if resolution is None else {
            "actual_action": resolution.actual_action, "resolution_note": resolution.resolution_note,
            "sop_followed": resolution.sop_followed.value, "additional_note": resolution.additional_note,
            "deviation_reason": resolution.deviation_reason,
        },
        "review": None if review is None else {
            "target_resolution_revision": review.target_resolution_revision,
            "review_approved": review.review_approved, "review_note": review.review_note,
            "recovery_verified": review.recovery_verified, "recovery_note": review.recovery_note,
        },
    })


def _parse_workflow_identity(value: object) -> WorkflowReceiptSemanticIdentity:
    data = _load_json(value, "immutable_workflow_identity")
    expected = {"action", "incident_id", "actor", "target_assignee", "resolution", "review"}
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError("immutable_workflow_identity has an invalid shape")
    resolution_data, review_data = data["resolution"], data["review"]
    resolution = None
    if resolution_data is not None:
        if not isinstance(resolution_data, dict) or set(resolution_data) != {"actual_action", "resolution_note", "sop_followed", "additional_note", "deviation_reason"}:
            raise ValueError("resolution identity has an invalid shape")
        from .contracts import ResolutionSubmissionPayload
        resolution = ResolutionSubmissionPayload(**{**resolution_data, "sop_followed": SopFollowed(resolution_data["sop_followed"])})
    review = None
    if review_data is not None:
        if not isinstance(review_data, dict) or set(review_data) != {"target_resolution_revision", "review_approved", "review_note", "recovery_verified", "recovery_note"}:
            raise ValueError("review identity has an invalid shape")
        from .contracts import ReviewAttemptPayload
        review = ReviewAttemptPayload(**review_data)
    return WorkflowReceiptSemanticIdentity(WorkflowAction(data["action"]), data["incident_id"], data["actor"], data["target_assignee"], resolution, review)


def _workflow_error(code: WorkflowErrorCode, message: str) -> WorkflowDomainError:
    return WorkflowDomainError(code, message)


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

    # SPEC-009 write primitives intentionally remain package-private.  They
    # are only composable by the IncidentManager under this transaction.
    def _get_workflow_receipt(self, workflow_operation_id: str) -> WorkflowOperationReceipt | None:
        row = self._connection.execute("SELECT * FROM incident_workflow_operation_receipts WHERE workflow_operation_id = ?", (workflow_operation_id,)).fetchone()
        return None if row is None else _decode_workflow_receipt(row)

    def _insert_workflow_receipt(self, receipt: WorkflowOperationReceipt) -> None:
        result = receipt.result
        self._connection.execute(
            """INSERT INTO incident_workflow_operation_receipts(
            workflow_operation_id,state_version,incident_id,action,completion_result,resulting_status,
            result_reference,completed_at,immutable_workflow_identity,assignment_policy_id,assignment_policy_version,
            selected_assignee,bound_reviewer,selection_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (result.workflow_operation_id, STATE_VERSION, result.incident_id, result.action.value,
             result.completion_result.value, result.resulting_status.value, result.result_reference,
              _timestamp(result.completed_at), _workflow_identity(receipt.immutable_workflow_identity),
              result.assignment_policy_id, result.assignment_policy_version, result.selected_assignee,
              result.bound_reviewer, result.selection_mode.value if result.selection_mode else None),
        )

    def _get_assignment_state(self, policy_id: str, policy_version: str) -> tuple[tuple[str, ...], str, int] | None:
        row = self._connection.execute("SELECT * FROM incident_assignment_state WHERE policy_id = ? AND policy_version = ?", (policy_id, policy_version)).fetchone()
        return None if row is None else _decode_assignment_state(row)

    def _upsert_assignment_state(self, policy_id: str, policy_version: str, engineers: tuple[str, ...], default_reviewer: str, next_cursor: int) -> None:
        self._connection.execute(
            """INSERT INTO incident_assignment_state(policy_id,policy_version,state_version,engineers,default_reviewer,next_cursor)
            VALUES (?,?,?,?,?,?) ON CONFLICT(policy_id,policy_version) DO UPDATE SET
            state_version=excluded.state_version, engineers=excluded.engineers, default_reviewer=excluded.default_reviewer, next_cursor=excluded.next_cursor""",
            (policy_id, policy_version, STATE_VERSION, _json(list(engineers)), default_reviewer, next_cursor),
        )

    def _insert_resolution_submission(self, submission: ResolutionSubmission) -> None:
        self._connection.execute(
            """INSERT INTO incident_resolution_submissions(resolution_submission_id,state_version,incident_id,revision,actual_action,resolution_note,sop_followed,additional_note,deviation_reason,submitted_by,submitted_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (submission.resolution_submission_id, STATE_VERSION, submission.incident_id, submission.revision,
             submission.actual_action, submission.resolution_note, submission.sop_followed.value,
             submission.additional_note, submission.deviation_reason, submission.submitted_by, _timestamp(submission.submitted_at)),
        )

    def _list_resolution_submissions(self, incident_id: str) -> tuple[ResolutionSubmission, ...]:
        rows = self._connection.execute("SELECT * FROM incident_resolution_submissions WHERE incident_id = ? ORDER BY revision", (incident_id,)).fetchall()
        submissions = tuple(_decode_resolution_submission(row) for row in rows)
        if [item.revision for item in submissions] != list(range(1, len(submissions) + 1)):
            raise _workflow_error(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "Resolution revisions must be contiguous and never reused")
        return submissions

    def _insert_review_attempt(self, attempt: ReviewAttempt) -> None:
        self._connection.execute(
            """INSERT INTO incident_review_attempts(review_attempt_id,state_version,incident_id,resolution_revision,reviewer,review_approved,review_note,recovery_verified,recovery_note,reviewed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (attempt.review_attempt_id, STATE_VERSION, attempt.incident_id, attempt.resolution_revision,
             attempt.reviewer, int(attempt.review_approved), attempt.review_note, int(attempt.recovery_verified),
             attempt.recovery_note, _timestamp(attempt.reviewed_at)),
        )

    def _insert_workflow_audit(self, entry: WorkflowAuditEntry) -> None:
        self._connection.execute(
            """INSERT INTO incident_workflow_audit(workflow_audit_id,state_version,workflow_operation_id,incident_id,actor,action,occurred_at,old_status,new_status,effects,reference_id,assignment_policy_id,assignment_policy_version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (entry.workflow_audit_id, STATE_VERSION, entry.workflow_operation_id, entry.incident_id, entry.actor,
             entry.action.value, _timestamp(entry.occurred_at), entry.old_status.value, entry.new_status.value,
             _json([effect.value for effect in entry.effects]), entry.reference_id, entry.assignment_policy_id, entry.assignment_policy_version),
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
                # ``present`` was captured before initialization.  Re-read it
                # before applying the common migration/validation path so a
                # newly-created v2 authority is not mistaken for a partial one.
                present = self._table_names(connection) & _EXPECTED_TABLES
            elif not _V1_TABLES.issubset(present):
                raise _domain_error(
                    IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
                    "Incident Store schema is incomplete",
                )
            self._migrate_schema(connection, present)
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
        version = rows[0]["schema_version"]
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            raise _domain_error(
                IncidentErrorCode.UNSUPPORTED_INCIDENT_STATE_VERSION,
                "Incident Store schema version is unsupported or newer than this binary",
            )
        if version != SCHEMA_VERSION:
            raise _domain_error(
                IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE,
                "Incident Store schema migration is incomplete",
            )

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection, present: set[str]) -> None:
        """Apply only forward, additive migrations in the metadata transaction."""
        row = connection.execute(
            "SELECT schema_version FROM incident_store_metadata WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise _domain_error(IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE, "Incident Store metadata is missing")
        version = row["schema_version"]
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            raise _domain_error(IncidentErrorCode.UNSUPPORTED_INCIDENT_STATE_VERSION, "Incident Store schema version is unsupported or newer than this binary")
        if version == SCHEMA_VERSION:
            if present != _EXPECTED_TABLES:
                raise _domain_error(IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE, "Incident Store schema is incomplete or contradictory")
            return
        if version == 1 and present == _V1_TABLES:
            # Metadata remains v1 until all additive workflow tables exist.
            for statement in _SCHEMA_STATEMENTS[7:]:
                connection.execute(statement)
        elif version == 2 and present == _EXPECTED_TABLES:
            # Metadata remains v2 until every receipt provenance column exists.
            for statement in _V2_TO_V3_STATEMENTS:
                connection.execute(statement)
        else:
            raise _domain_error(IncidentErrorCode.INCIDENT_STORE_INTEGRITY_FAILURE, "Incident Store schema cannot be migrated safely")
        connection.execute("UPDATE incident_store_metadata SET schema_version = ? WHERE singleton = 1", (SCHEMA_VERSION,))

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

    def get_workflow_operation_result(self, workflow_operation_id: str) -> WorkflowOperationResult | None:
        _validate_reference(workflow_operation_id, "workflow_operation_id")
        with self._read_transaction() as connection:
            self._validate_read_authority(connection)
            row = connection.execute("SELECT * FROM incident_workflow_operation_receipts WHERE workflow_operation_id = ?", (workflow_operation_id,)).fetchone()
            return None if row is None else _decode_workflow_receipt(row).result

    def get_assignment_state(self, policy_id: str, policy_version: str) -> tuple[tuple[str, ...], str, int] | None:
        _validate_reference(policy_id, "policy_id")
        _validate_reference(policy_version, "policy_version")
        with self._read_transaction() as connection:
            self._validate_read_authority(connection)
            row = connection.execute("SELECT * FROM incident_assignment_state WHERE policy_id = ? AND policy_version = ?", (policy_id, policy_version)).fetchone()
            return None if row is None else _decode_assignment_state(row)

    def get_latest_resolution_submission(self, incident_id: str) -> ResolutionSubmission | None:
        _validate_reference(incident_id, "incident_id")
        with self._read_transaction() as connection:
            self._validate_read_authority(connection)
            # A latest-only query must not hide a malformed or non-monotonic
            # earlier revision.  It is authoritative workflow state too.
            _validate_resolution_history(connection, incident_id)
            row = connection.execute("SELECT * FROM incident_resolution_submissions WHERE incident_id = ? ORDER BY revision DESC LIMIT 1", (incident_id,)).fetchone()
            return None if row is None else _decode_resolution_submission(row)

    def list_resolution_submissions(self, incident_id: str) -> tuple[ResolutionSubmission, ...]:
        _validate_reference(incident_id, "incident_id")
        with self._read_transaction() as connection:
            self._validate_read_authority(connection)
            rows = connection.execute("SELECT * FROM incident_resolution_submissions WHERE incident_id = ? ORDER BY revision", (incident_id,)).fetchall()
            submissions = tuple(_decode_resolution_submission(row) for row in rows)
            if [item.revision for item in submissions] != list(range(1, len(submissions) + 1)):
                raise _workflow_error(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "Resolution revisions must be contiguous and never reused")
            return submissions

    def list_review_attempts(self, incident_id: str) -> tuple[ReviewAttempt, ...]:
        _validate_reference(incident_id, "incident_id")
        with self._read_transaction() as connection:
            self._validate_read_authority(connection)
            rows = connection.execute("SELECT * FROM incident_review_attempts WHERE incident_id = ? ORDER BY reviewed_at, review_attempt_id", (incident_id,)).fetchall()
            return tuple(_decode_review_attempt(row) for row in rows)

    def list_workflow_audit(self, incident_id: str) -> tuple[WorkflowAuditEntry, ...]:
        _validate_reference(incident_id, "incident_id")
        with self._read_transaction() as connection:
            self._validate_read_authority(connection)
            rows = connection.execute("SELECT * FROM incident_workflow_audit WHERE incident_id = ? ORDER BY occurred_at, workflow_audit_id", (incident_id,)).fetchall()
            return tuple(_decode_workflow_audit(row) for row in rows)

    def list_unified_incident_timeline(self, incident_id: str) -> tuple[IncidentTimelineEntry, ...]:
        _validate_reference(incident_id, "incident_id")
        with self._read_transaction() as connection:
            self._validate_read_authority(connection)
            entries: list[IncidentTimelineEntry] = []
            for row in connection.execute("SELECT * FROM incident_audit WHERE incident_id = ? ORDER BY audit_id", (incident_id,)).fetchall():
                audit = _decode_audit(row)
                entries.append(IncidentTimelineEntry(audit.occurred_at, IncidentTimelineSource.CORRELATION,
                    f"{row['audit_id']:020d}", correlation_audit=audit))
            for row in connection.execute("SELECT * FROM incident_workflow_audit WHERE incident_id = ? ORDER BY workflow_audit_id", (incident_id,)).fetchall():
                audit = _decode_workflow_audit(row)
                entries.append(IncidentTimelineEntry(audit.occurred_at, IncidentTimelineSource.WORKFLOW,
                    audit.workflow_audit_id, workflow_audit=audit))
            return tuple(sorted(entries, key=lambda entry: (entry.occurred_at, 0 if entry.source is IncidentTimelineSource.CORRELATION else 1, entry.source_local_order)))

    def list_incidents_by_workflow_status(self, status: IncidentStatus) -> tuple[IncidentRecord, ...]:
        if not isinstance(status, IncidentStatus):
            raise ValueError("status must be an IncidentStatus")
        with self._read_transaction() as connection:
            self._validate_read_authority(connection)
            rows = connection.execute("SELECT incident_id FROM incidents WHERE status = ? ORDER BY incident_id", (status.value,)).fetchall()
            return tuple(_read_incident(connection, row["incident_id"]) for row in rows)

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
            workflow_receipts = connection.execute("SELECT * FROM incident_workflow_operation_receipts ORDER BY workflow_operation_id").fetchall()
            for row in workflow_receipts:
                _decode_workflow_receipt(row)
            for row in connection.execute("SELECT * FROM incident_assignment_state ORDER BY policy_id, policy_version").fetchall():
                _decode_assignment_state(row)
            incident_ids = connection.execute("SELECT incident_id FROM incidents ORDER BY incident_id").fetchall()
            for incident_row in incident_ids:
                incident_id = incident_row["incident_id"]
                _validate_resolution_history(connection, incident_id)
                for row in connection.execute("SELECT * FROM incident_review_attempts WHERE incident_id = ?", (incident_id,)).fetchall():
                    _decode_review_attempt(row)
                for row in connection.execute("SELECT * FROM incident_workflow_audit WHERE incident_id = ?", (incident_id,)).fetchall():
                    _decode_workflow_audit(row)
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
            workflow_contradiction = connection.execute(
                """SELECT a.workflow_audit_id FROM incident_workflow_audit AS a
                JOIN incident_workflow_operation_receipts AS r ON r.workflow_operation_id = a.workflow_operation_id
                WHERE a.incident_id != r.incident_id OR a.action != r.action LIMIT 1"""
            ).fetchone()
            if workflow_contradiction is not None:
                raise _workflow_error(WorkflowErrorCode.INCIDENT_WORKFLOW_INTEGRITY_FAILURE, "workflow receipt and audit contradict")

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


def _require_workflow_state_version(row: sqlite3.Row, record_kind: str) -> None:
    if row["state_version"] != STATE_VERSION:
        raise _workflow_error(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, f"unsupported persisted {record_kind} state version")


def _decode_workflow_receipt(row: sqlite3.Row) -> WorkflowOperationReceipt:
    try:
        _require_workflow_state_version(row, "workflow operation receipt")
        return WorkflowOperationReceipt(
            WorkflowOperationResult(row["workflow_operation_id"], row["incident_id"], WorkflowAction(row["action"]),
                WorkflowCompletion(row["completion_result"]), IncidentStatus(row["resulting_status"]), row["result_reference"],
                _parse_timestamp(row["completed_at"], "completed_at"), row["assignment_policy_id"],
                row["assignment_policy_version"], row["selected_assignee"], row["bound_reviewer"],
                AssignmentSelectionMode(row["selection_mode"]) if row["selection_mode"] is not None else None),
            _parse_workflow_identity(row["immutable_workflow_identity"]),
        )
    except WorkflowDomainError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _workflow_error(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, f"malformed persisted workflow receipt: {exc}") from exc


def _decode_assignment_state(row: sqlite3.Row) -> tuple[tuple[str, ...], str, int]:
    try:
        _require_workflow_state_version(row, "assignment state")
        engineers = _load_json(row["engineers"], "engineers")
        if not isinstance(engineers, list) or not engineers or any(not isinstance(item, str) or not item or item != item.strip() for item in engineers) or len(engineers) != len(set(engineers)):
            raise ValueError("engineers must be a non-empty ordered unique reference list")
        cursor = row["next_cursor"]
        if not isinstance(cursor, int) or cursor < 0 or cursor >= len(engineers):
            raise ValueError("next_cursor must identify a roster position")
        reviewer = row["default_reviewer"]
        if not isinstance(reviewer, str) or not reviewer or reviewer != reviewer.strip():
            raise ValueError("default_reviewer is invalid")
        return tuple(engineers), reviewer, cursor
    except (KeyError, TypeError, ValueError) as exc:
        raise _workflow_error(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, f"malformed persisted assignment state: {exc}") from exc


def _decode_resolution_submission(row: sqlite3.Row) -> ResolutionSubmission:
    try:
        _require_workflow_state_version(row, "resolution submission")
        return ResolutionSubmission(row["resolution_submission_id"], row["incident_id"], row["revision"], row["actual_action"],
            row["resolution_note"], SopFollowed(row["sop_followed"]), row["additional_note"], row["deviation_reason"],
            row["submitted_by"], _parse_timestamp(row["submitted_at"], "submitted_at"))
    except WorkflowDomainError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _workflow_error(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, f"malformed persisted resolution submission: {exc}") from exc


def _decode_review_attempt(row: sqlite3.Row) -> ReviewAttempt:
    try:
        _require_workflow_state_version(row, "review attempt")
        if row["review_approved"] not in (0, 1) or row["recovery_verified"] not in (0, 1):
            raise ValueError("review facts must be SQLite booleans")
        return ReviewAttempt(row["review_attempt_id"], row["incident_id"], row["resolution_revision"], row["reviewer"],
            bool(row["review_approved"]), row["review_note"], bool(row["recovery_verified"]), row["recovery_note"],
            _parse_timestamp(row["reviewed_at"], "reviewed_at"))
    except WorkflowDomainError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _workflow_error(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, f"malformed persisted review attempt: {exc}") from exc


def _decode_workflow_audit(row: sqlite3.Row) -> WorkflowAuditEntry:
    try:
        _require_workflow_state_version(row, "workflow audit")
        effects = _load_json(row["effects"], "effects")
        if not isinstance(effects, list):
            raise ValueError("effects must be a list")
        return WorkflowAuditEntry(row["workflow_audit_id"], row["workflow_operation_id"], row["incident_id"], row["actor"],
            WorkflowAction(row["action"]), _parse_timestamp(row["occurred_at"], "occurred_at"), IncidentStatus(row["old_status"]),
            IncidentStatus(row["new_status"]), tuple(WorkflowAuditEffect(value) for value in effects), row["reference_id"],
            row["assignment_policy_id"], row["assignment_policy_version"])
    except WorkflowDomainError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _workflow_error(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, f"malformed persisted workflow audit: {exc}") from exc


def _validate_resolution_history(connection: sqlite3.Connection, incident_id: str) -> None:
    rows = connection.execute("SELECT * FROM incident_resolution_submissions WHERE incident_id = ? ORDER BY revision", (incident_id,)).fetchall()
    submissions = tuple(_decode_resolution_submission(row) for row in rows)
    if [item.revision for item in submissions] != list(range(1, len(submissions) + 1)):
        raise _workflow_error(WorkflowErrorCode.MALFORMED_WORKFLOW_STATE, "Resolution revisions must be contiguous and never reused")


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
