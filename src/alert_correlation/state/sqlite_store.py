"""SQLite-backed durable adapter for the SPEC-007 logical state store.

SQLite is an implementation detail of this adapter; the logical records remain
defined in :mod:`contracts`, and no SPEC-006 module depends on this file.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from uuid import uuid4

from ..contracts import (
    AnchorStrength, AnchorTransition, CorrelationErrorCode, CorrelationFamily,
    CorrelationEvaluationError, DecisionReasonCode, DecisionType, EvaluationPhase, NormalizedFingerprint,
)
from .contracts import (
    ActivePendingRecord, BlockedCorrelationRecord, CorrelationMutationIntent,
    CorrelationPolicyKind, FailureKind, PendingReason, ProcessedCorrelationRecord,
    RetryDisposition, StateDomainErrorCode, StateDomainValidationError,
    TerminalOutcome,
)


class ResolvedState(str, Enum):
    TERMINAL_PROCESSED = "TERMINAL_PROCESSED"
    UNRESOLVED_MUTATION_INTENT = "UNRESOLVED_MUTATION_INTENT"
    ACTIVE_PENDING_BLOCKED = "ACTIVE_PENDING_BLOCKED"
    ACTIVE_PENDING = "ACTIVE_PENDING"
    BLOCKED = "BLOCKED"
    UNSEEN = "UNSEEN"


class StateStoreIntegrityError(RuntimeError):
    """Persisted state cannot safely be interpreted and must not become UNSEEN."""

    def __init__(
        self,
        event_id: str,
        message: str,
        code: StateDomainErrorCode = StateDomainErrorCode.MALFORMED_STATE_RECORD,
    ) -> None:
        super().__init__(message)
        self.event_id = event_id
        self.code = code


@dataclass(frozen=True, slots=True)
class ProcessingClaim:
    """A fencing token granting temporary per-event mutation authority."""

    event_id: str
    claim_id: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class ClaimAbandonmentProof:
    """Explicit external proof required before replacing an active claim."""

    event_id: str
    claim_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ResolvedCorrelationState:
    state: ResolvedState
    processed: ProcessedCorrelationRecord | None = None
    intent: CorrelationMutationIntent | None = None
    pending: ActivePendingRecord | None = None
    blocked: BlockedCorrelationRecord | None = None
    claim: ProcessingClaim | None = None
    matching_stale_intent: bool = False


@dataclass(frozen=True, slots=True)
class RecoveryStateEntry:
    event_id: str
    resolved: ResolvedCorrelationState


_TABLES = ("processed", "intents", "pending", "blocked", "claims")
_SCHEMA_VERSION = 1


class SqliteCorrelationStateStore:
    """Durable, per-event bookkeeping with transactional local mutations."""

    def __init__(self, database_path: str | Path) -> None:
        self._path = str(database_path)
        self._connection = sqlite3.connect(self._path, isolation_level=None)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._initialize_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SqliteCorrelationStateStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _initialize_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS correlation_state_processed (
                event_id TEXT PRIMARY KEY NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS correlation_state_intents (
                event_id TEXT PRIMARY KEY NOT NULL,
                operation_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS correlation_state_pending (
                event_id TEXT PRIMARY KEY NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS correlation_state_blocked (
                event_id TEXT PRIMARY KEY NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS correlation_state_claims (
                event_id TEXT PRIMARY KEY NOT NULL,
                claim_id TEXT NOT NULL UNIQUE,
                fencing_token INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS correlation_state_claim_generations (
                event_id TEXT PRIMARY KEY NOT NULL,
                last_fencing_token INTEGER NOT NULL
            );
            """
        )

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

    def activate_pending(
        self, record: ActivePendingRecord, claim: ProcessingClaim | None
    ) -> ActivePendingRecord:
        """Atomically create/refresh Pending and resolve a matching active Block.

        The caller must already have received a valid external ENTER_PENDING
        Decision.  This method never evaluates a policy or retries work.
        """
        if not isinstance(record, ActivePendingRecord):
            raise TypeError("record must be an ActivePendingRecord")
        with self._write_transaction():
            self._require_claim(claim, event_id=record.event_id)
            state = self._resolve_locked(record.event_id)
            if state.processed is not None or state.intent is not None:
                self._conflict(record.event_id, "cannot activate Pending after Processed or Intent")
            if state.pending is not None:
                existing = state.pending
                immutable_existing = (
                    existing.event_id, existing.entered_pending_at, existing.expires_at,
                    existing.correlation_policy, existing.policy_id, existing.policy_version,
                )
                immutable_candidate = (
                    record.event_id, record.entered_pending_at, record.expires_at,
                    record.correlation_policy, record.policy_id, record.policy_version,
                )
                if immutable_existing != immutable_candidate:
                    self._conflict(record.event_id, "Pending immutable fields cannot be replaced")
                if existing != record:
                    self._connection.execute(
                        "UPDATE correlation_state_pending SET payload = ? WHERE event_id = ?",
                        (_json(_encode_pending(record)), record.event_id),
                    )
            else:
                self._insert("pending", record.event_id, _encode_pending(record))
            if state.blocked is not None:
                self._connection.execute(
                    "DELETE FROM correlation_state_blocked WHERE event_id = ?", (record.event_id,)
                )
        return record

    def record_evaluation_failure(
        self,
        error: CorrelationEvaluationError,
        evaluation_phase: EvaluationPhase,
        *,
        now: datetime,
        claim: ProcessingClaim | None,
    ) -> BlockedCorrelationRecord:
        """Durably map one real SPEC-006 failure under current authority.

        The Pending record, if any, is deliberately retained.  Repeated
        failures update one authoritative Block in the same BEGIN IMMEDIATE
        transaction; this method performs no evaluation, retry, or repair.
        """
        if not isinstance(error, CorrelationEvaluationError):
            raise TypeError("error must be a CorrelationEvaluationError")
        if not isinstance(evaluation_phase, EvaluationPhase):
            raise TypeError("evaluation_phase must be an EvaluationPhase")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise TypeError("now must be a timezone-aware datetime")
        with self._write_transaction():
            self._require_claim(claim, event_id=error.event_id)
            state = self._resolve_locked(error.event_id)
            if state.processed is not None or state.intent is not None:
                self._conflict(error.event_id, "cannot record evaluation failure after Processed or Intent")
            existing = state.blocked
            record = BlockedCorrelationRecord(
                event_id=error.event_id,
                failure_kind=FailureKind.CORRELATION_DOMAIN_FAILURE,
                failure_code=error.error_code,
                evaluation_phase=evaluation_phase,
                first_failed_at=now if existing is None else existing.first_failed_at,
                last_failed_at=now,
                attempt_count=1 if existing is None else existing.attempt_count + 1,
                retry_disposition=_retry_disposition_for_correlation_error(error.error_code),
                policy_id=error.policy_id,
                policy_version=error.policy_version,
                field_path=error.field_path,
                incident_id=error.incident_id,
            )
            if existing is None:
                self._insert("blocked", record.event_id, _encode_blocked(record))
            else:
                self._connection.execute(
                    "UPDATE correlation_state_blocked SET payload = ? WHERE event_id = ?",
                    (_json(_encode_blocked(record)), record.event_id),
                )
            return record

    def acquire_claim(self, event_id: str) -> ProcessingClaim | None:
        """Atomically acquire authority, or return None to the competing loser."""
        self._validate_event_id(event_id)
        with self._write_transaction():
            if self._read_claim(event_id) is not None:
                return None
            row = self._connection.execute(
                "SELECT last_fencing_token FROM correlation_state_claim_generations WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            fencing_token = 1 if row is None else row[0] + 1
            self._connection.execute(
                "INSERT INTO correlation_state_claim_generations(event_id, last_fencing_token) VALUES (?, ?) "
                "ON CONFLICT(event_id) DO UPDATE SET last_fencing_token = excluded.last_fencing_token",
                (event_id, fencing_token),
            )
            claim = ProcessingClaim(event_id, uuid4().hex, fencing_token)
            self._connection.execute(
                "INSERT INTO correlation_state_claims(event_id, claim_id, fencing_token) VALUES (?, ?, ?)",
                (claim.event_id, claim.claim_id, claim.fencing_token),
            )
            return claim

    def release_claim(self, claim: ProcessingClaim) -> None:
        with self._write_transaction():
            self._require_claim(claim)
            self._connection.execute(
                "DELETE FROM correlation_state_claims WHERE event_id = ?", (claim.event_id,)
            )

    def reclaim_claim(
        self, abandoned_claim: ProcessingClaim, proof: ClaimAbandonmentProof
    ) -> ProcessingClaim:
        """Replace an exact active claim only after an explicit abandonment proof."""
        if not isinstance(proof, ClaimAbandonmentProof):
            raise TypeError("proof must be a ClaimAbandonmentProof")
        if (
            proof.event_id != abandoned_claim.event_id
            or proof.claim_id != abandoned_claim.claim_id
            or not isinstance(proof.reason, str)
            or not proof.reason.strip()
        ):
            self._claim_error(abandoned_claim.event_id, "abandonment proof does not match claim")
        with self._write_transaction():
            self._require_claim(abandoned_claim)
            self._connection.execute(
                "DELETE FROM correlation_state_claims WHERE event_id = ?", (abandoned_claim.event_id,)
            )
            row = self._connection.execute(
                "SELECT last_fencing_token FROM correlation_state_claim_generations WHERE event_id = ?",
                (abandoned_claim.event_id,),
            ).fetchone()
            fencing_token = row[0] + 1
            self._connection.execute(
                "UPDATE correlation_state_claim_generations SET last_fencing_token = ? WHERE event_id = ?",
                (fencing_token, abandoned_claim.event_id),
            )
            replacement = ProcessingClaim(abandoned_claim.event_id, uuid4().hex, fencing_token)
            self._connection.execute(
                "INSERT INTO correlation_state_claims(event_id, claim_id, fencing_token) VALUES (?, ?, ?)",
                (replacement.event_id, replacement.claim_id, replacement.fencing_token),
            )
            return replacement

    def begin_intent(
        self, intent: CorrelationMutationIntent, claim: ProcessingClaim
    ) -> CorrelationMutationIntent:
        if not isinstance(intent, CorrelationMutationIntent):
            raise TypeError("intent must be a CorrelationMutationIntent")
        with self._write_transaction():
            self._require_claim(claim, event_id=intent.event_id)
            state = self._resolve_locked(intent.event_id)
            if state.processed is not None:
                self._conflict(intent.event_id, "cannot create Intent after Processed")
            if state.intent is not None:
                if state.intent == intent:
                    return state.intent
                self._conflict(intent.event_id, "conflicting MutationIntent already exists")
            self._insert("intents", intent.event_id, _encode_intent(intent), operation_id=intent.operation_id)
        return intent

    def finalize_processed(
        self, record: ProcessedCorrelationRecord, claim: ProcessingClaim | None = None
    ) -> ProcessedCorrelationRecord:
        """Atomically persist terminal ownership and resolve active local bookkeeping."""
        if not isinstance(record, ProcessedCorrelationRecord):
            raise TypeError("record must be a ProcessedCorrelationRecord")
        with self._write_transaction():
            state = self._resolve_locked(record.event_id)
            if state.processed is not None:
                if state.processed == record:
                    return state.processed
                self._conflict(record.event_id, "Processed ownership already has a different terminal record")
            self._require_claim(claim, event_id=record.event_id)
            if state.intent is None:
                self._conflict(record.event_id, "Processed requires a durable MutationIntent")
            if not _intent_matches_processed(state.intent, record):
                self._conflict(record.event_id, "Processed result contradicts MutationIntent")
            self._insert("processed", record.event_id, _encode_processed(record))
            self._connection.execute("DELETE FROM correlation_state_intents WHERE event_id = ?", (record.event_id,))
            self._connection.execute("DELETE FROM correlation_state_pending WHERE event_id = ?", (record.event_id,))
            self._connection.execute("DELETE FROM correlation_state_blocked WHERE event_id = ?", (record.event_id,))
        return record

    def get_processed(self, event_id: str) -> ProcessedCorrelationRecord | None:
        self._validate_event_id(event_id)
        with self._read_snapshot():
            return self._read_record("processed", event_id)

    def resolve(self, event_id: str) -> ResolvedCorrelationState:
        self._validate_event_id(event_id)
        with self._read_snapshot():
            return self._resolve_locked(event_id)

    def enumerate_recovery_states(self) -> tuple[RecoveryStateEntry, ...]:
        """Enumerate only durable states relevant to restart recovery, not EventStore."""
        with self._read_snapshot():
            event_ids: set[str] = set()
            for table in _TABLES:
                rows = self._connection.execute(
                    f"SELECT event_id FROM correlation_state_{table}"
                ).fetchall()
                event_ids.update(row[0] for row in rows)
            entries = tuple(
                RecoveryStateEntry(event_id, self._resolve_locked(event_id))
                for event_id in sorted(event_ids)
            )
        return entries

    def recovery_event_ids(self) -> tuple[str, ...]:
        """Return local recovery candidates only; this never scans EventStore."""
        try:
            with self._read_snapshot():
                event_ids: set[str] = set()
                for table in _TABLES:
                    rows = self._connection.execute(
                        f"SELECT event_id FROM correlation_state_{table}"
                    ).fetchall()
                    event_ids.update(row[0] for row in rows)
                return tuple(sorted(event_ids))
        except sqlite3.Error as exc:
            raise StateStoreIntegrityError("<store>", "store cannot enumerate recovery state") from exc

    def resolve_matching_stale_intent(self, event_id: str) -> bool:
        """Remove only an Intent proven identical to an already Processed outcome."""
        self._validate_event_id(event_id)
        with self._write_transaction():
            state = self._resolve_locked(event_id)
            if state.processed is None or state.intent is None:
                return False
            # _resolve_locked already rejects a contradictory Processed + Intent pair.
            self._connection.execute(
                "DELETE FROM correlation_state_intents WHERE event_id = ?", (event_id,)
            )
            return True

    @staticmethod
    def retention_cleanup_guard() -> None:
        """SPEC-007 deliberately has no retention, reset, or destructive cleanup API."""
        raise StateDomainValidationError(
            StateDomainErrorCode.STORE_INTEGRITY_FAILURE,
            "retention cleanup is not authorized by SPEC-007",
        )

    def _resolve_locked(self, event_id: str) -> ResolvedCorrelationState:
        processed = self._read_record("processed", event_id)
        intent = self._read_record("intents", event_id)
        pending = self._read_record("pending", event_id)
        blocked = self._read_record("blocked", event_id)
        claim = self._read_claim(event_id)
        if processed is not None:
            if intent is not None and not _intent_matches_processed(intent, processed):
                self._conflict(event_id, "Processed conflicts with stale MutationIntent")
            return ResolvedCorrelationState(
                ResolvedState.TERMINAL_PROCESSED, processed, intent, pending, blocked, claim,
                matching_stale_intent=intent is not None,
            )
        if intent is not None:
            return ResolvedCorrelationState(ResolvedState.UNRESOLVED_MUTATION_INTENT, intent=intent, pending=pending, blocked=blocked, claim=claim)
        if pending is not None and blocked is not None:
            return ResolvedCorrelationState(ResolvedState.ACTIVE_PENDING_BLOCKED, pending=pending, blocked=blocked, claim=claim)
        if pending is not None:
            return ResolvedCorrelationState(ResolvedState.ACTIVE_PENDING, pending=pending, claim=claim)
        if blocked is not None:
            return ResolvedCorrelationState(ResolvedState.BLOCKED, blocked=blocked, claim=claim)
        return ResolvedCorrelationState(ResolvedState.UNSEEN, claim=claim)

    def _read_claim(self, event_id: str) -> ProcessingClaim | None:
        row = self._connection.execute(
            "SELECT claim_id, fencing_token FROM correlation_state_claims WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return ProcessingClaim(event_id, row[0], row[1])
        except (TypeError, ValueError) as exc:
            raise StateStoreIntegrityError(event_id, "malformed persisted processing claim") from exc

    def _require_claim(self, claim: ProcessingClaim | None, *, event_id: str | None = None) -> None:
        if not isinstance(claim, ProcessingClaim):
            self._claim_error(event_id or "unknown", "a valid processing claim is required")
        if event_id is not None and claim.event_id != event_id:
            self._claim_error(event_id, "claim belongs to a different event")
        active = self._read_claim(claim.event_id)
        if active != claim:
            self._claim_error(claim.event_id, "claim is stale or no longer authoritative")

    @staticmethod
    def _claim_error(event_id: str, message: str) -> None:
        raise StateDomainValidationError(StateDomainErrorCode.MUTATION_INTENT_CONFLICT, f"{event_id}: {message}")

    def _read_record(self, table: str, event_id: str):
        row = self._connection.execute(
            f"SELECT payload FROM correlation_state_{table} WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
            if not isinstance(payload, dict):
                raise ValueError("persisted payload must be an object")
            if payload.get("schema_version") != _SCHEMA_VERSION:
                raise StateStoreIntegrityError(
                    event_id,
                    "unsupported state schema semantics",
                    StateDomainErrorCode.UNSUPPORTED_STATE_VERSION,
                )
            decoder = {
                "processed": _decode_processed, "intents": _decode_intent,
                "pending": _decode_pending, "blocked": _decode_blocked,
            }[table]
            record = decoder(payload)
            if record.event_id != event_id:
                raise ValueError("payload event_id does not match primary key")
            return record
        except StateStoreIntegrityError:
            raise
        except (TypeError, ValueError, KeyError, StateDomainValidationError) as exc:
            raise StateStoreIntegrityError(event_id, f"malformed persisted {table} record") from exc

    def _insert(self, table: str, event_id: str, payload: dict[str, object], *, operation_id: str | None = None) -> None:
        if table == "intents":
            self._connection.execute(
                "INSERT INTO correlation_state_intents(event_id, operation_id, payload) VALUES (?, ?, ?)",
                (event_id, operation_id, _json(payload)),
            )
        else:
            self._connection.execute(
                f"INSERT INTO correlation_state_{table}(event_id, payload) VALUES (?, ?)",
                (event_id, _json(payload)),
            )

    @staticmethod
    def _validate_event_id(event_id: str) -> None:
        if not isinstance(event_id, str) or not event_id or event_id != event_id.strip():
            raise ValueError("event_id must be a non-empty reference without surrounding whitespace")

    @staticmethod
    def _conflict(event_id: str, message: str) -> None:
        raise StateDomainValidationError(StateDomainErrorCode.TERMINAL_OWNERSHIP_CONFLICT, f"{event_id}: {message}")


def _retry_disposition_for_correlation_error(error_code: CorrelationErrorCode) -> RetryDisposition:
    """The approved SPEC-007 mapping for real SPEC-006 failures."""
    dispositions = {
        CorrelationErrorCode.INVALID_EVENT_ENVELOPE: RetryDisposition.NON_RETRYABLE,
        CorrelationErrorCode.MISSING_REQUIRED_IDENTITY: RetryDisposition.NON_RETRYABLE,
        CorrelationErrorCode.INVALID_IDENTITY_VALUE: RetryDisposition.NON_RETRYABLE,
        CorrelationErrorCode.POLICY_NOT_REGISTERED: RetryDisposition.REPAIR_REQUIRED,
        CorrelationErrorCode.POLICY_VERSION_UNAVAILABLE: RetryDisposition.REPAIR_REQUIRED,
        CorrelationErrorCode.INVALID_INCIDENT_VIEW: RetryDisposition.REPAIR_REQUIRED,
        CorrelationErrorCode.INCONSISTENT_CORRELATION_CONTEXT: RetryDisposition.REPAIR_REQUIRED,
    }
    try:
        return dispositions[error_code]
    except KeyError as exc:  # pragma: no cover - enum expansion must fail closed.
        raise StateDomainValidationError(
            StateDomainErrorCode.MALFORMED_STATE_RECORD,
            "unmapped SPEC-006 CorrelationErrorCode",
        ) from exc


def _json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    return datetime.fromisoformat(value)


def _fingerprint(value: NormalizedFingerprint | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"event_type": value.event_type, "identity": list(value.identity)}


def _parse_fingerprint(value: object) -> NormalizedFingerprint | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("fingerprint must be an object")
    identity = value.get("identity")
    if not isinstance(identity, list):
        raise ValueError("fingerprint identity must be a list")
    return NormalizedFingerprint(value["event_type"], tuple(tuple(item) for item in identity))


def _encode_pending(record: ActivePendingRecord) -> dict[str, object]:
    return {"schema_version": _SCHEMA_VERSION, "event_id": record.event_id, "entered_pending_at": _timestamp(record.entered_pending_at), "expires_at": _timestamp(record.expires_at), "correlation_policy": record.correlation_policy.value, "policy_id": record.policy_id, "policy_version": record.policy_version, "pending_reason": record.pending_reason.value}


def _decode_pending(data: dict[str, object]) -> ActivePendingRecord:
    return ActivePendingRecord(data["event_id"], _parse_timestamp(data["entered_pending_at"]), _parse_timestamp(data["expires_at"]), CorrelationPolicyKind(data["correlation_policy"]), data["policy_id"], data["policy_version"], PendingReason(data["pending_reason"]))


def _encode_processed(record: ProcessedCorrelationRecord) -> dict[str, object]:
    return {"schema_version": _SCHEMA_VERSION, "event_id": record.event_id, "terminal_outcome": record.terminal_outcome.value, "resolved_at": _timestamp(record.resolved_at), "incident_id": record.incident_id, "shadow_ref": record.shadow_ref, "policy_id": record.policy_id, "policy_version": record.policy_version}


def _decode_processed(data: dict[str, object]) -> ProcessedCorrelationRecord:
    return ProcessedCorrelationRecord(data["event_id"], TerminalOutcome(data["terminal_outcome"]), _parse_timestamp(data["resolved_at"]), data["incident_id"], data["shadow_ref"], data["policy_id"], data["policy_version"])


def _encode_blocked(record: BlockedCorrelationRecord) -> dict[str, object]:
    return {"schema_version": _SCHEMA_VERSION, "event_id": record.event_id, "failure_kind": record.failure_kind.value, "failure_code": record.failure_code.value if isinstance(record.failure_code, Enum) else record.failure_code, "evaluation_phase": record.evaluation_phase.value if record.evaluation_phase else None, "first_failed_at": _timestamp(record.first_failed_at), "last_failed_at": _timestamp(record.last_failed_at), "attempt_count": record.attempt_count, "retry_disposition": record.retry_disposition.value, "policy_id": record.policy_id, "policy_version": record.policy_version, "field_path": record.field_path, "incident_id": record.incident_id}


def _decode_blocked(data: dict[str, object]) -> BlockedCorrelationRecord:
    kind = FailureKind(data["failure_kind"])
    code: CorrelationErrorCode | StateDomainErrorCode | str
    if kind is FailureKind.CORRELATION_DOMAIN_FAILURE:
        code = CorrelationErrorCode(data["failure_code"])
    elif kind is FailureKind.STATE_DOMAIN_FAILURE:
        code = StateDomainErrorCode(data["failure_code"])
    else:
        code = data["failure_code"]
    phase = data["evaluation_phase"]
    return BlockedCorrelationRecord(data["event_id"], kind, code, EvaluationPhase(phase) if phase is not None else None, _parse_timestamp(data["first_failed_at"]), _parse_timestamp(data["last_failed_at"]), data["attempt_count"], RetryDisposition(data["retry_disposition"]), data.get("policy_id"), data.get("policy_version"), data.get("field_path"), data.get("incident_id"))


def _encode_intent(record: CorrelationMutationIntent) -> dict[str, object]:
    return {"schema_version": _SCHEMA_VERSION, "operation_id": record.operation_id, "event_id": record.event_id, "intended_terminal_outcome": record.intended_terminal_outcome.value, "decision_type": record.decision_type.value, "policy_id": record.policy_id, "policy_version": record.policy_version, "correlation_family": record.correlation_family.value, "reason_code": record.reason_code.value, "target_incident_id": record.target_incident_id, "normalized_fingerprint": _fingerprint(record.normalized_fingerprint), "anchor_strength": record.anchor_strength.value if record.anchor_strength else None, "anchor_transition": record.anchor_transition.value, "created_at": _timestamp(record.created_at)}


def _decode_intent(data: dict[str, object]) -> CorrelationMutationIntent:
    strength = data["anchor_strength"]
    return CorrelationMutationIntent(data["operation_id"], data["event_id"], TerminalOutcome(data["intended_terminal_outcome"]), DecisionType(data["decision_type"]), data["policy_id"], data["policy_version"], CorrelationFamily(data["correlation_family"]), DecisionReasonCode(data["reason_code"]), data["target_incident_id"], _parse_fingerprint(data["normalized_fingerprint"]), AnchorStrength(strength) if strength is not None else None, AnchorTransition(data["anchor_transition"]), _parse_timestamp(data["created_at"]))


def _intent_matches_processed(intent: CorrelationMutationIntent, processed: ProcessedCorrelationRecord) -> bool:
    if (intent.intended_terminal_outcome is not processed.terminal_outcome or (intent.policy_id, intent.policy_version) != (processed.policy_id, processed.policy_version)):
        return False
    if intent.decision_type is DecisionType.ATTACH_EXISTING:
        return intent.target_incident_id == processed.incident_id
    return True
