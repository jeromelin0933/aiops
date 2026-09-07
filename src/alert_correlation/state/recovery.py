"""Restart reconstruction and integrity classification for SPEC-007 state only."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .contracts import RetryDisposition, StateDomainErrorCode
from .sqlite_store import (
    RecoveryStateEntry, ResolvedState, SqliteCorrelationStateStore,
    StateStoreIntegrityError,
)


class RecoveryReferencePort(Protocol):
    """Read-only authority checks supplied by downstream owners or tests."""

    def event_exists(self, event_id: str) -> bool: ...
    def incident_exists(self, incident_id: str) -> bool: ...
    def shadow_exists(self, shadow_ref: str) -> bool: ...


class RecoveryAction(str, Enum):
    TERMINAL_NOOP = "TERMINAL_NOOP"
    RECONCILE_INTENT = "RECONCILE_INTENT"
    PRESERVE_PENDING = "PRESERVE_PENDING"
    PRESERVE_PENDING_BLOCKED = "PRESERVE_PENDING_BLOCKED"
    PRESERVE_BLOCKED = "PRESERVE_BLOCKED"
    PRESERVE_CLAIM = "PRESERVE_CLAIM"


@dataclass(frozen=True, slots=True)
class RecoveryIntegrityFinding:
    event_id: str
    error_code: StateDomainErrorCode
    retry_disposition: RetryDisposition = RetryDisposition.REPAIR_REQUIRED


@dataclass(frozen=True, slots=True)
class ReconstructedState:
    entry: RecoveryStateEntry
    action: RecoveryAction


@dataclass(frozen=True, slots=True)
class StartupRecoveryResult:
    states: tuple[ReconstructedState, ...]
    findings: tuple[RecoveryIntegrityFinding, ...]


class StateStoreStartupError(RuntimeError):
    """The store cannot reliably enumerate/classify state; startup must stop."""


class StateRecoveryService:
    """Read/reconstruct local state. It never scans EventStore or repairs authority."""

    def __init__(self, store: SqliteCorrelationStateStore) -> None:
        self._store = store

    def reconstruct(
        self, reference_port: RecoveryReferencePort | None = None
    ) -> StartupRecoveryResult:
        try:
            event_ids = self._store.recovery_event_ids()
        except StateStoreIntegrityError as exc:
            raise StateStoreStartupError("state store cannot be enumerated") from exc
        states: list[ReconstructedState] = []
        findings: list[RecoveryIntegrityFinding] = []
        for event_id in event_ids:
            try:
                resolved = self._store.resolve(event_id)
            except StateStoreIntegrityError as exc:
                findings.append(_finding(event_id, exc.code))
                continue
            except Exception:
                findings.append(_finding(event_id, StateDomainErrorCode.TERMINAL_OWNERSHIP_CONFLICT))
                continue
            finding = _reference_finding(resolved, reference_port)
            if finding is not None:
                findings.append(_finding(event_id, finding))
                continue
            if resolved.matching_stale_intent:
                try:
                    self._store.resolve_matching_stale_intent(event_id)
                    resolved = self._store.resolve(event_id)
                except Exception:
                    findings.append(_finding(event_id, StateDomainErrorCode.TERMINAL_OWNERSHIP_CONFLICT))
                    continue
            states.append(ReconstructedState(RecoveryStateEntry(event_id, resolved), _action_for(resolved.state)))
        return StartupRecoveryResult(tuple(states), tuple(findings))


def _action_for(state: ResolvedState) -> RecoveryAction:
    return {
        ResolvedState.TERMINAL_PROCESSED: RecoveryAction.TERMINAL_NOOP,
        ResolvedState.UNRESOLVED_MUTATION_INTENT: RecoveryAction.RECONCILE_INTENT,
        ResolvedState.ACTIVE_PENDING: RecoveryAction.PRESERVE_PENDING,
        ResolvedState.ACTIVE_PENDING_BLOCKED: RecoveryAction.PRESERVE_PENDING_BLOCKED,
        ResolvedState.BLOCKED: RecoveryAction.PRESERVE_BLOCKED,
        ResolvedState.UNSEEN: RecoveryAction.PRESERVE_CLAIM,
    }[state]


def _finding(event_id: str, code: StateDomainErrorCode) -> RecoveryIntegrityFinding:
    return RecoveryIntegrityFinding(event_id, code)


def _reference_finding(resolved, port: RecoveryReferencePort | None) -> StateDomainErrorCode | None:
    if port is None:
        return None
    record = resolved.processed or resolved.intent or resolved.pending or resolved.blocked or resolved.claim
    if record is None:
        return StateDomainErrorCode.MALFORMED_STATE_RECORD
    event_id = record.event_id
    if not port.event_exists(event_id):
        return StateDomainErrorCode.DANGLING_EVENT_REFERENCE
    processed = resolved.processed
    if processed is None:
        return None
    if processed.incident_id is not None and not port.incident_exists(processed.incident_id):
        return StateDomainErrorCode.DANGLING_INCIDENT_REFERENCE
    if processed.shadow_ref is not None and not port.shadow_exists(processed.shadow_ref):
        return StateDomainErrorCode.DANGLING_SHADOW_REFERENCE
    return None
