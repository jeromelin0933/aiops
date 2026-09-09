"""SPEC-010 Shadow-domain contracts.

This package deliberately contains no persistence, Incident mutation, or runtime
orchestration.  Those concerns begin in later approved phases.
"""

from .contracts import (
    IncidentOwnershipLookup,
    ShadowDomainError,
    ShadowDomainErrorCode,
    ShadowDomainValidationError,
    ShadowEventProjection,
    ShadowMutationRequest,
    ShadowMutationResult,
    ShadowOperationReceipt,
    ShadowReadPort,
    ShadowReason,
    ShadowReceiptSemanticIdentity,
    ShadowRecord,
    ShadowReviewStatus,
    SHADOW_ERROR_RETRY_DISPOSITIONS,
    receipt_semantic_identity,
    retry_disposition_for_shadow_error,
    validate_legal_shadow_mutation,
    validate_shadow_creation_reason,
)
from .sqlite_store import ShadowStoreIntegrityError, SqliteShadowStore
from .manager import ShadowManager

__all__ = [
    "IncidentOwnershipLookup",
    "ShadowDomainError",
    "ShadowDomainErrorCode",
    "ShadowDomainValidationError",
    "ShadowEventProjection",
    "ShadowMutationRequest",
    "ShadowMutationResult",
    "ShadowOperationReceipt",
    "ShadowReadPort",
    "ShadowReason",
    "ShadowReceiptSemanticIdentity",
    "ShadowRecord",
    "ShadowReviewStatus",
    "SHADOW_ERROR_RETRY_DISPOSITIONS",
    "receipt_semantic_identity",
    "retry_disposition_for_shadow_error",
    "validate_legal_shadow_mutation",
    "validate_shadow_creation_reason",
    "ShadowStoreIntegrityError",
    "SqliteShadowStore",
    "ShadowManager",
]
