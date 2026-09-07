"""SPEC-007 state-domain contracts.

This package owns validation of durable correlation bookkeeping only.  It does
not evaluate policies or perform Incident or Shadow mutations.
"""

from .contracts import (
    ActivePendingRecord,
    BlockedCorrelationRecord,
    CorrelationMutationIntent,
    CorrelationPolicyKind,
    FailureKind,
    PendingGraceConfig,
    PendingReason,
    ProcessedCorrelationRecord,
    RetryDisposition,
    StateDomainErrorCode,
    StateDomainValidationError,
    TerminalOutcome,
    terminal_outcome_for_decision,
    validate_active_pending_record,
)
from .sqlite_store import (
    ClaimAbandonmentProof,
    ProcessingClaim,
    RecoveryStateEntry,
    ResolvedCorrelationState,
    ResolvedState,
    SqliteCorrelationStateStore,
    StateStoreIntegrityError,
)
from .pending import (
    BlockRetryStatus,
    PendingPhaseResolution,
    PendingPolicyUnavailableError,
    PendingStateService,
)
from .recovery import (
    RecoveryAction,
    RecoveryIntegrityFinding,
    RecoveryReferencePort,
    ReconstructedState,
    StartupRecoveryResult,
    StateRecoveryService,
    StateStoreStartupError,
)

__all__ = [
    "ActivePendingRecord",
    "BlockedCorrelationRecord",
    "CorrelationMutationIntent",
    "CorrelationPolicyKind",
    "FailureKind",
    "PendingGraceConfig",
    "PendingReason",
    "ProcessedCorrelationRecord",
    "RetryDisposition",
    "StateDomainErrorCode",
    "StateDomainValidationError",
    "TerminalOutcome",
    "terminal_outcome_for_decision",
    "validate_active_pending_record",
    "RecoveryStateEntry",
    "ResolvedCorrelationState",
    "ResolvedState",
    "SqliteCorrelationStateStore",
    "StateStoreIntegrityError",
    "ClaimAbandonmentProof",
    "ProcessingClaim",
    "BlockRetryStatus",
    "PendingPhaseResolution",
    "PendingPolicyUnavailableError",
    "PendingStateService",
    "RecoveryAction",
    "RecoveryIntegrityFinding",
    "RecoveryReferencePort",
    "ReconstructedState",
    "StartupRecoveryResult",
    "StateRecoveryService",
    "StateStoreStartupError",
]
