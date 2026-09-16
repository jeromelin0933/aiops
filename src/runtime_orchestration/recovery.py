"""Startup recovery classification over public authoritative store surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from alert_correlation.state import (
    ClaimAbandonmentProof,
    ProcessingClaim,
    ResolvedCorrelationState,
    ResolvedState,
    RetryDisposition,
    StateDomainErrorCode,
    StateDomainValidationError,
    StateStoreIntegrityError,
    TerminalOutcome,
)

from .clock import canonical_utc


class StateRecoveryReadPort(Protocol):
    """Public SPEC-007 capabilities used by Runtime startup."""

    def recovery_event_ids(self) -> tuple[str, ...]: ...
    def resolve(self, event_id: str) -> ResolvedCorrelationState: ...
    def reclaim_claim(
        self, abandoned_claim: ProcessingClaim, proof: ClaimAbandonmentProof
    ) -> ProcessingClaim: ...


class RuntimeDispatchKind(str, Enum):
    PROCESSED = "PROCESSED"
    UNRESOLVED_INTENT = "UNRESOLVED_INTENT"
    ACTIVE_PENDING = "ACTIVE_PENDING"
    EXPIRED_PENDING = "EXPIRED_PENDING"
    ACTIVE_PENDING_BLOCKED = "ACTIVE_PENDING_BLOCKED"
    EXPIRED_PENDING_BLOCKED = "EXPIRED_PENDING_BLOCKED"
    BLOCKED = "BLOCKED"
    CLAIM_RECLAIM_CANDIDATE = "CLAIM_RECLAIM_CANDIDATE"
    UNSEEN = "UNSEEN"


@dataclass(frozen=True, slots=True)
class RuntimeStateClassification:
    event_id: str
    dispatch_kind: RuntimeDispatchKind
    correlation_terminal: bool = False
    assignment_reconciliation_candidate: bool = False
    pending_expires_at: datetime | None = None
    retry_disposition: RetryDisposition | None = None
    claim_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeStateFinding:
    event_id: str
    error_code: StateDomainErrorCode
    retry_disposition: RetryDisposition = RetryDisposition.REPAIR_REQUIRED


@dataclass(frozen=True, slots=True)
class RuntimeStateRecoveryResult:
    classifications: tuple[RuntimeStateClassification, ...]
    findings: tuple[RuntimeStateFinding, ...]


class RuntimeStateStoreStartupError(RuntimeError):
    """SPEC-007 cannot provide a complete recovery candidate set."""


class RuntimeStateRecoveryClassifier:
    """Read and classify state; never evaluates policy or performs mutation."""

    def __init__(self, state_store: StateRecoveryReadPort) -> None:
        self._state_store = state_store

    def classify(
        self, *, authoritative_event_ids: tuple[str, ...], now: datetime
    ) -> RuntimeStateRecoveryResult:
        absolute_now = canonical_utc(now, field="recovery now")
        try:
            recovery_ids = self._state_store.recovery_event_ids()
        except Exception as exc:
            raise RuntimeStateStoreStartupError(
                "SPEC-007 recovery state cannot be enumerated reliably"
            ) from exc
        if (
            not isinstance(recovery_ids, tuple)
            or any(not isinstance(item, str) or not item for item in recovery_ids)
            or tuple(sorted(set(recovery_ids))) != recovery_ids
        ):
            raise RuntimeStateStoreStartupError(
                "SPEC-007 recovery enumeration is not deterministic and unique"
            )

        event_id_set = set(authoritative_event_ids)
        candidate_ids = tuple(sorted(event_id_set | set(recovery_ids)))
        classifications: list[RuntimeStateClassification] = []
        findings: list[RuntimeStateFinding] = []
        for event_id in candidate_ids:
            if event_id not in event_id_set:
                findings.append(
                    RuntimeStateFinding(
                        event_id, StateDomainErrorCode.DANGLING_EVENT_REFERENCE
                    )
                )
                continue
            try:
                resolved = self._state_store.resolve(event_id)
                classifications.append(_classify_resolved(event_id, resolved, absolute_now))
            except StateStoreIntegrityError as exc:
                findings.append(RuntimeStateFinding(event_id, exc.code))
            except StateDomainValidationError as exc:
                findings.append(RuntimeStateFinding(event_id, exc.code))
            except Exception as exc:
                raise RuntimeStateStoreStartupError(
                    f"SPEC-007 state for {event_id} cannot be resolved reliably"
                ) from exc
        return RuntimeStateRecoveryResult(tuple(classifications), tuple(findings))

    def safe_reclaim(
        self, event_id: str, proof: ClaimAbandonmentProof
    ) -> ProcessingClaim:
        """Reclaim only the exact current claim with formal abandonment proof."""
        if not isinstance(proof, ClaimAbandonmentProof):
            raise TypeError("proof must be a ClaimAbandonmentProof")
        resolved = self._state_store.resolve(event_id)
        claim = resolved.claim
        if claim is None:
            raise ValueError(f"{event_id} has no current claim to reclaim")
        if proof.event_id != event_id or proof.claim_id != claim.claim_id:
            raise ValueError("abandonment proof does not match the current claim")
        return self._state_store.reclaim_claim(claim, proof)


def _classify_resolved(
    event_id: str, resolved: ResolvedCorrelationState, now: datetime
) -> RuntimeStateClassification:
    record = resolved.processed or resolved.intent or resolved.pending or resolved.blocked or resolved.claim
    if record is not None and record.event_id != event_id:
        raise ValueError("resolved SPEC-007 state belongs to a different Event")
    if resolved.state is ResolvedState.TERMINAL_PROCESSED:
        processed = resolved.processed
        if processed is None:
            raise ValueError("TERMINAL_PROCESSED has no Processed record")
        return RuntimeStateClassification(
            event_id,
            RuntimeDispatchKind.PROCESSED,
            correlation_terminal=True,
            assignment_reconciliation_candidate=(
                processed.terminal_outcome is TerminalOutcome.CREATED_INCIDENT
            ),
        )
    if resolved.state is ResolvedState.UNRESOLVED_MUTATION_INTENT:
        return RuntimeStateClassification(event_id, RuntimeDispatchKind.UNRESOLVED_INTENT)
    if resolved.state in {
        ResolvedState.ACTIVE_PENDING,
        ResolvedState.ACTIVE_PENDING_BLOCKED,
    }:
        pending = resolved.pending
        if pending is None:
            raise ValueError("Pending resolved state has no Pending record")
        expired = now >= canonical_utc(pending.expires_at, field="Pending.expires_at")
        if resolved.state is ResolvedState.ACTIVE_PENDING_BLOCKED:
            blocked = resolved.blocked
            if blocked is None:
                raise ValueError("ACTIVE_PENDING_BLOCKED has no Blocked record")
            return RuntimeStateClassification(
                event_id,
                RuntimeDispatchKind.EXPIRED_PENDING_BLOCKED
                if expired
                else RuntimeDispatchKind.ACTIVE_PENDING_BLOCKED,
                pending_expires_at=canonical_utc(pending.expires_at),
                retry_disposition=blocked.retry_disposition,
            )
        return RuntimeStateClassification(
            event_id,
            RuntimeDispatchKind.EXPIRED_PENDING
            if expired
            else RuntimeDispatchKind.ACTIVE_PENDING,
            pending_expires_at=canonical_utc(pending.expires_at),
        )
    if resolved.state is ResolvedState.BLOCKED:
        blocked = resolved.blocked
        if blocked is None:
            raise ValueError("BLOCKED has no Blocked record")
        return RuntimeStateClassification(
            event_id,
            RuntimeDispatchKind.BLOCKED,
            retry_disposition=blocked.retry_disposition,
        )
    if resolved.state is ResolvedState.UNSEEN and resolved.claim is not None:
        return RuntimeStateClassification(
            event_id,
            RuntimeDispatchKind.CLAIM_RECLAIM_CANDIDATE,
            claim_id=resolved.claim.claim_id,
        )
    if resolved.state is ResolvedState.UNSEEN:
        return RuntimeStateClassification(event_id, RuntimeDispatchKind.UNSEEN)
    raise ValueError(f"unsupported SPEC-007 resolved state: {resolved.state}")
